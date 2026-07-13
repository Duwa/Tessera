"""
retailco_support_agent.py
RetailCo customer support pipeline using LangChain + Claude.

Pipeline:
  Ticket → TriageAgent → (auto-resolvable?) → ResolutionAgent → resolved
                                           → (complex?)        → Tessera ITSM ticket

Run:
    TESSERA_URL=http://localhost/api/v1 \
    TESSERA_ORG=demo-org \
    ANTHROPIC_API_KEY=sk-ant-... \
    python retailco_support_agent.py
"""
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from tessera_callback import TesseraCallback

# ── LLM ─────────────────────────────────────────────────────────────────────

llm = ChatAnthropic(
    model="claude-sonnet-4-6",
    max_tokens=2048,
    temperature=0,
)

tessera = TesseraCallback(agent_name="retailco_support")

# ── Domain types ─────────────────────────────────────────────────────────────

class TicketCategory(str, Enum):
    REFUND       = "refund"
    DELIVERY     = "delivery"
    TECHNICAL    = "technical"
    BILLING      = "billing"
    ACCOUNT      = "account"
    UNKNOWN      = "unknown"


class TicketUrgency(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"
    URGENT = "urgent"


@dataclass
class SupportTicket:
    ticket_id:     str
    customer_id:   str
    customer_tier: str          # "standard" | "gold" | "platinum"
    subject:       str
    body:          str
    order_id:      Optional[str] = None


@dataclass
class TriageResult:
    category:       TicketCategory
    urgency:        TicketUrgency
    auto_resolvable: bool
    reason:         str
    draft_context:  str = ""


@dataclass
class ResolutionResult:
    resolved:        bool
    action_taken:    str
    response_sent:   str
    escalated:       bool = False
    escalation_note: str = ""
    confidence:      float = 0.0


# ── Simulated backend tools ──────────────────────────────────────────────────
# In production these call real APIs. Here they return plausible mock data
# so the agent logic is fully exercisable without live systems.

@tool
def lookup_order(order_id: str) -> dict:
    """
    Look up an order by ID. Returns status, estimated delivery, and items.
    Use before issuing a refund or answering delivery questions.
    """
    mock_orders = {
        "ORD-1001": {
            "order_id":       "ORD-1001",
            "status":         "delivered",
            "delivered_date": "2026-06-20",
            "items":          [{"name": "Wireless Headphones", "qty": 1, "price": 89.99}],
            "refundable":     True,
            "refund_window_days": 30,
        },
        "ORD-1002": {
            "order_id":       "ORD-1002",
            "status":         "in_transit",
            "estimated_delivery": "2026-06-30",
            "items":          [{"name": "Laptop Stand", "qty": 2, "price": 34.99}],
            "refundable":     False,
        },
    }
    return mock_orders.get(order_id, {"error": f"Order {order_id} not found"})


@tool
def issue_refund(order_id: str, reason: str, amount: Optional[float] = None) -> dict:
    """
    Issue a full or partial refund for a delivered order.
    Returns confirmation_id if successful.
    Only call after lookup_order confirms the order is refundable.
    """
    return {
        "success":         True,
        "confirmation_id": f"REF-{order_id[-4:]}-X9",
        "order_id":        order_id,
        "amount_refunded": amount or "full",
        "eta_days":        3,
    }


@tool
def update_delivery_address(order_id: str, new_address: str) -> dict:
    """
    Update the delivery address for an in-transit order.
    Only possible before the parcel reaches the local depot.
    """
    return {
        "success":   True,
        "order_id":  order_id,
        "new_address": new_address,
        "note":      "Address updated. Carrier notified.",
    }


@tool
def send_customer_email(customer_id: str, subject: str, body: str) -> dict:
    """
    Send a resolution email to the customer.
    Always call this as the final step of a successful resolution.
    """
    return {
        "success":    True,
        "customer_id": customer_id,
        "subject":    subject,
        "message_id": f"MSG-{hash(customer_id + subject) % 99999:05d}",
    }


@tool
def escalate_to_human(
    summary: str,
    context: str,
    draft_response: str,
    priority: str = "medium",
) -> dict:
    """
    Escalate a ticket to a human support agent via Tessera ITSM.
    Use when:
      - The customer is Platinum tier and angry
      - A refund > $200 is required
      - The issue is account security related
      - You are not confident in the resolution
    The escalation creates a Tessera ticket with your draft response attached.
    """
    # TesseraCallback.on_tool_end() watches for {"escalate": True} and fires
    # the ITSM POST automatically — no extra code needed here.
    return {
        "escalate":  True,
        "summary":   summary,
        "context":   context,
        "draft":     draft_response,
        "priority":  priority,
        "note":      "Tessera ITSM ticket created. A human agent will pick this up within 2 hours.",
    }


RESOLUTION_TOOLS = [lookup_order, issue_refund, update_delivery_address,
                    send_customer_email, escalate_to_human]

# ── Stage 1: Triage ──────────────────────────────────────────────────────────

TRIAGE_SYSTEM = """You are RetailCo's ticket triage specialist.
Analyse the support ticket and return a JSON object with these exact keys:
  category        — one of: refund, delivery, technical, billing, account, unknown
  urgency         — one of: low, medium, high, urgent
  auto_resolvable — true if a software agent can fully resolve this without human judgment
  reason          — one sentence explaining your classification
  draft_context   — key facts the resolution agent needs (order IDs, amounts, dates)

Auto-resolvable is TRUE when:
  - Standard/Gold tier customer with a routine refund under $200
  - Delivery tracking question with a known order ID
  - Simple account password reset

Auto-resolvable is FALSE when:
  - Platinum tier customer expressing strong dissatisfaction
  - Refund over $200 or outside the 30-day window
  - Suspected fraud or account security issue
  - You cannot determine the intent from the ticket body

Return only the JSON object, no preamble."""


def triage(ticket: SupportTicket) -> TriageResult:
    prompt = f"""Customer tier: {ticket.customer_tier}
Subject: {ticket.subject}
Body: {ticket.body}
Order ID (if mentioned): {ticket.order_id or "not provided"}"""

    response = llm.invoke(
        [SystemMessage(content=TRIAGE_SYSTEM), HumanMessage(content=prompt)],
        config={"callbacks": [tessera]},
    )

    try:
        raw = json.loads(response.content)
        return TriageResult(
            category        = TicketCategory(raw.get("category", "unknown")),
            urgency         = TicketUrgency(raw.get("urgency", "medium")),
            auto_resolvable = bool(raw.get("auto_resolvable", False)),
            reason          = raw.get("reason", ""),
            draft_context   = raw.get("draft_context", ""),
        )
    except (json.JSONDecodeError, ValueError):
        return TriageResult(
            category        = TicketCategory.UNKNOWN,
            urgency         = TicketUrgency.MEDIUM,
            auto_resolvable = False,
            reason          = "Triage parse failed — routing to human",
        )


# ── Stage 2: Resolution ──────────────────────────────────────────────────────

RESOLUTION_SYSTEM = """You are RetailCo's resolution agent. You have tools to:
  - look up orders (lookup_order)
  - issue refunds (issue_refund)
  - update delivery addresses (update_delivery_address)
  - send the customer a resolution email (send_customer_email)
  - escalate to a human agent (escalate_to_human)

Rules:
1. Always call lookup_order before issuing a refund.
2. Always call send_customer_email as the last step of a successful resolution.
3. Be concise and friendly in customer emails.
4. If you escalate, include a complete draft response so the human agent
   can resolve quickly.
5. Set confidence to a float 0.0–1.0 in your final JSON output.

After completing your work, return a JSON object:
{
  "resolved":      true/false,
  "action_taken":  "one-line summary of what you did",
  "response_sent": "the email body you sent (or would have sent)",
  "escalated":     true/false,
  "confidence":    0.85
}"""


def resolve(ticket: SupportTicket, triage_result: TriageResult) -> ResolutionResult:
    context = f"""Ticket ID:     {ticket.ticket_id}
Customer:      {ticket.customer_id} ({ticket.customer_tier} tier)
Subject:       {ticket.subject}
Body:          {ticket.body}
Order ID:      {ticket.order_id or "not provided"}
Triage notes:  {triage_result.draft_context}"""

    llm_with_tools = llm.bind_tools(RESOLUTION_TOOLS)

    messages = [
        SystemMessage(content=RESOLUTION_SYSTEM),
        HumanMessage(content=context),
    ]

    # Agentic loop: run until the model stops calling tools
    while True:
        response = llm_with_tools.invoke(
            messages,
            config={"callbacks": [tessera]},
        )
        messages.append(response)

        if not response.tool_calls:
            break

        # Execute each tool call and append results
        for tc in response.tool_calls:
            tool_map = {t.name: t for t in RESOLUTION_TOOLS}
            tool_fn  = tool_map.get(tc["name"])
            if tool_fn:
                result = tool_fn.invoke(tc["args"])
                # TesseraCallback.on_tool_end sees escalate=True and posts
                # to Tessera ITSM automatically
                messages.append({
                    "role":          "tool",
                    "tool_call_id":  tc["id"],
                    "content":       json.dumps(result),
                })

    # Extract final JSON from the last assistant message
    try:
        raw = json.loads(response.content)
        return ResolutionResult(
            resolved        = bool(raw.get("resolved", False)),
            action_taken    = raw.get("action_taken", ""),
            response_sent   = raw.get("response_sent", ""),
            escalated       = bool(raw.get("escalated", False)),
            escalation_note = raw.get("escalation_note", ""),
            confidence      = float(raw.get("confidence", 0.5)),
        )
    except (json.JSONDecodeError, ValueError):
        return ResolutionResult(
            resolved     = False,
            action_taken = "Resolution parse failed",
            response_sent= "",
            escalated    = True,
            confidence   = 0.0,
        )


# ── Pipeline entry point ─────────────────────────────────────────────────────

def process_ticket(ticket: SupportTicket) -> dict:
    """
    Full pipeline: triage → (auto-resolve | escalate).
    Returns a summary dict suitable for logging or a response API.
    """
    print(f"\n{'='*60}")
    print(f"Processing ticket {ticket.ticket_id} — {ticket.subject}")
    print(f"Customer tier: {ticket.customer_tier}")

    # Stage 1 — triage
    triage_result = triage(ticket)
    print(f"\n[Triage]  category={triage_result.category.value}  "
          f"urgency={triage_result.urgency.value}  "
          f"auto_resolvable={triage_result.auto_resolvable}")
    print(f"          reason: {triage_result.reason}")

    # Stage 2 — resolution (always runs; agent decides whether to escalate)
    resolution = resolve(ticket, triage_result)
    print(f"\n[Resolve] resolved={resolution.resolved}  "
          f"escalated={resolution.escalated}  "
          f"confidence={resolution.confidence:.2f}")
    print(f"          action: {resolution.action_taken}")

    return {
        "ticket_id":    ticket.ticket_id,
        "category":     triage_result.category.value,
        "urgency":      triage_result.urgency.value,
        "auto_resolved": resolution.resolved and not resolution.escalated,
        "escalated":    resolution.escalated,
        "confidence":   resolution.confidence,
        "action":       resolution.action_taken,
    }


# ── Sample tickets ───────────────────────────────────────────────────────────

SAMPLE_TICKETS = [
    SupportTicket(
        ticket_id   = "TKT-2001",
        customer_id = "CUST-441",
        customer_tier = "standard",
        subject     = "Need a refund for my headphones",
        body        = "Hi, I ordered wireless headphones last week (order ORD-1001) "
                      "but they stopped working after two days. I'd like a full refund please.",
        order_id    = "ORD-1001",
    ),
    SupportTicket(
        ticket_id   = "TKT-2002",
        customer_id = "CUST-887",
        customer_tier = "platinum",
        subject     = "WHERE IS MY ORDER — this is unacceptable",
        body        = "I placed an order 5 days ago (ORD-1002) and it still hasn't arrived. "
                      "I'm a Platinum member and this level of service is completely unacceptable. "
                      "I want a full refund AND a replacement sent express.",
        order_id    = "ORD-1002",
    ),
    SupportTicket(
        ticket_id   = "TKT-2003",
        customer_id = "CUST-229",
        customer_tier = "gold",
        subject     = "Can you change my delivery address?",
        body        = "Hi, I just realised I need my order ORD-1002 sent to my office "
                      "instead — 42 Business Park, London EC1A 1BB. Is that still possible?",
        order_id    = "ORD-1002",
    ),
]


if __name__ == "__main__":
    results = []
    for ticket in SAMPLE_TICKETS:
        result = process_ticket(ticket)
        results.append(result)

    print(f"\n{'='*60}")
    print("PIPELINE SUMMARY")
    print(f"{'='*60}")
    total       = len(results)
    auto_closed = sum(1 for r in results if r["auto_resolved"])
    escalated   = sum(1 for r in results if r["escalated"])
    print(f"Total tickets processed : {total}")
    print(f"Auto-resolved           : {auto_closed} ({100*auto_closed//total}%)")
    print(f"Escalated to human      : {escalated} ({100*escalated//total}%)")
    print(f"Deflection rate         : {auto_closed/total:.1%}")
    print(f"\nGovernance exhaust is live at:")
    print(f"  GET {os.getenv('TESSERA_URL','http://localhost/api/v1')}"
          f"/governance/events/summary?org_id={os.getenv('TESSERA_ORG','demo-org')}")
