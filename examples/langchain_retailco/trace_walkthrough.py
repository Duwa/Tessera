"""
trace_walkthrough.py
Shows exactly what Tessera receives for each of the three sample tickets —
without calling Claude or Tessera. Run this to understand the data flow.
"""
import json
import sys
sys.stdout.reconfigure(encoding="utf-8")

# ── What Tessera's /governance/events receives for TKT-2001 (standard refund) ──

TRACE_TKT_2001 = [
    {
        "phase": "TesseraCallback.on_chain_start",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":     "demo-org",
            "event_type": "agent_start",
            "source":     "langchain",
            "agent":      "retailco_support",
            "input_chars": 312,
        },
    },
    {
        "phase": "TesseraCallback.on_llm_end  [triage LLM call]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":            "demo-org",
            "event_type":        "llm_usage",
            "source":            "langchain",
            "agent":             "retailco_support",
            "prompt_tokens":     280,
            "completion_tokens": 95,
            "total_tokens":      375,
        },
    },
    {
        "phase": "TesseraCallback.on_tool_start  [resolution: lookup_order]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":     "demo-org",
            "event_type": "tool_call",
            "source":     "langchain",
            "agent":      "retailco_support",
            "tool":       "lookup_order",
        },
    },
    {
        "phase": "TesseraCallback.on_tool_start  [resolution: issue_refund]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":     "demo-org",
            "event_type": "tool_call",
            "source":     "langchain",
            "agent":      "retailco_support",
            "tool":       "issue_refund",
        },
    },
    {
        "phase": "TesseraCallback.on_tool_start  [resolution: send_customer_email]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":     "demo-org",
            "event_type": "tool_call",
            "source":     "langchain",
            "agent":      "retailco_support",
            "tool":       "send_customer_email",
        },
    },
    {
        "phase": "TesseraCallback.on_chain_end  [auto-resolved OK]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":        "demo-org",
            "event_type":    "agent_complete",
            "source":        "langchain",
            "agent":         "retailco_support",
            "duration_ms":   1840,
            "auto_resolved": True,
            "confidence":    0.92,
        },
    },
]

# ── What Tessera receives for TKT-2002 (Platinum tier complaint → escalation) ──

TRACE_TKT_2002 = [
    {
        "phase": "TesseraCallback.on_chain_start",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":     "demo-org",
            "event_type": "agent_start",
            "source":     "langchain",
            "agent":      "retailco_support",
            "input_chars": 388,
        },
    },
    {
        "phase": "TesseraCallback.on_tool_start  [escalate_to_human]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":     "demo-org",
            "event_type": "tool_call",
            "source":     "langchain",
            "agent":      "retailco_support",
            "tool":       "escalate_to_human",
        },
    },
    {
        "phase": "TesseraCallback.on_tool_end  [escalate=True detected → ITSM ticket]",
        "endpoint": "POST /itsm/tickets",
        "payload": {
            "org_id":      "demo-org",
            "source":      "langchain",
            "title":       "Platinum customer: missing order ORD-1002, requesting refund + replacement",
            "description": "Customer CUST-887 (Platinum) ordered 5 days ago, order in transit. "
                           "Requesting full refund AND express replacement. High anger signal.",
            "ai_draft":    "Dear valued Platinum member, I sincerely apologise for the delay "
                           "with order ORD-1002. I've arranged an express replacement at no cost "
                           "and have initiated your refund of $69.98. You'll receive both within "
                           "48 hours. A $50 credit has also been added to your account.",
            "priority":    "high",
        },
    },
    {
        "phase": "TesseraCallback.on_chain_end  [escalated — human takes over]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id":        "demo-org",
            "event_type":    "agent_complete",
            "source":        "langchain",
            "agent":         "retailco_support",
            "duration_ms":   2210,
            "auto_resolved": False,
            "confidence":    0.0,
        },
    },
]

# ── What /governance/events/summary returns after both tickets processed ──

EVENTS_SUMMARY_AFTER_TWO_TICKETS = {
    "org_id":        "demo-org",
    "total_events":  10,          # 5 events per ticket (start + llm + tools + end)
    "auto_resolved": 1,           # TKT-2001 resolved itself
    "escalations":   1,           # TKT-2002 → ITSM
    "sources":       {"langchain": 10},
    "computed_roai": 0.45,        # 1/10 auto-resolved × 4.5 scaling factor
    "deflection_rate": 0.1,
}

# ── What the human agent sees in Tessera ITSM for TKT-2002 ──

ITSM_TICKET_VIEW = {
    "id":          "TKT-2002",
    "title":       "Platinum customer: missing order ORD-1002, requesting refund + replacement",
    "priority":    "high",
    "source":      "langchain",
    "status":      "open",
    "ai_draft": (
        "Dear valued Platinum member, I sincerely apologise for the delay "
        "with order ORD-1002. I've arranged an express replacement at no cost "
        "and have initiated your refund of $69.98. You'll receive both within "
        "48 hours. A $50 credit has also been added to your account."
    ),
    "human_action_needed": (
        "Agent could not auto-resolve: Platinum tier + express replacement "
        "requires manager approval. Review draft above and adjust credit amount "
        "if needed before sending."
    ),
}

if __name__ == "__main__":
    print("=" * 64)
    print("TKT-2001 (standard refund) — what Tessera receives")
    print("=" * 64)
    for i, event in enumerate(TRACE_TKT_2001, 1):
        print(f"\n  [{i}] {event['phase']}")
        print(f"      -> {event['endpoint']}")
        print(f"      {json.dumps(event['payload'], indent=6)}")

    print("\n" + "=" * 64)
    print("TKT-2002 (Platinum escalation) — what Tessera receives")
    print("=" * 64)
    for i, event in enumerate(TRACE_TKT_2002, 1):
        print(f"\n  [{i}] {event['phase']}")
        print(f"      -> {event['endpoint']}")
        print(f"      {json.dumps(event['payload'], indent=6)}")

    print("\n" + "=" * 64)
    print("GET /governance/events/summary — after both tickets")
    print("=" * 64)
    print(json.dumps(EVENTS_SUMMARY_AFTER_TWO_TICKETS, indent=2))

    print("\n" + "=" * 64)
    print("Tessera ITSM — what the human agent sees for TKT-2002")
    print("=" * 64)
    print(json.dumps(ITSM_TICKET_VIEW, indent=2))
