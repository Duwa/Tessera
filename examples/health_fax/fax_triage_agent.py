"""
fax_triage_agent.py
Healthcare fax triage pipeline using LangGraph + Claude vision.

Pipeline (LangGraph state machine):
  eFax webhook → ocr_node → classify_node → extract_phi_node
                → route_node → (auto_route) → ehr_node → notify_node
                             → (hold)       → hold_node

Every PHI field access is logged to Tessera's HIPAA audit trail.
STAT faxes and low-confidence classifications always go to human review.

Run:
    TESSERA_URL=http://localhost/api/v1 \\
    TESSERA_ORG=demo-org \\
    ANTHROPIC_API_KEY=sk-ant-... \\
    python fax_triage_agent.py
"""
import json
import os
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from tessera_fax_callback import TesseraFaxCallback

# ── LLM ─────────────────────────────────────────────────────────────────────

llm = ChatAnthropic(
    model="claude-sonnet-4-6",
    max_tokens=2048,
    temperature=0,
)

# ── State schema ─────────────────────────────────────────────────────────────

class FaxState(TypedDict):
    # Input
    fax_id:          str
    fax_image_b64:   Optional[str]   # base64 TIFF/PDF — passed to Claude vision
    raw_text_mock:   Optional[str]   # for testing without a live fax image

    # After ocr_node
    raw_text:        str

    # After classify_node
    doc_type:        str             # referral|lab_result|prescription|prior_auth|eob|unknown
    confidence:      float
    urgency:         str             # stat|routine

    # After extract_phi_node
    patient_name:    str
    patient_dob:     str
    patient_mrn:     str
    referring_provider:   str
    target_department:    str
    summary:         str             # 2-sentence human-readable summary for reviewers

    # After route_node
    routing_decision: str            # auto_route|hold_for_review
    routing_reason:   str

    # After ehr_node / hold_node
    ehr_task_id:     Optional[str]
    escalated:       bool
    escalation_note: str


# ── Shared callback instance (set per fax) ───────────────────────────────────

_tessera: TesseraFaxCallback = TesseraFaxCallback()


# ── Node implementations ──────────────────────────────────────────────────────

OCR_SYSTEM = """You are a medical document OCR specialist.
Extract every word from the fax image exactly as written — preserve spacing and line breaks.
If you cannot read a word clearly, write [illegible].
Return only the extracted text, no commentary."""


def ocr_node(state: FaxState) -> dict:
    """
    Read the fax image using Claude vision (or use raw_text_mock for testing).
    In production, fax_image_b64 is the base64-encoded TIFF received from eFax webhook.
    """
    if state.get("raw_text_mock"):
        return {"raw_text": state["raw_text_mock"]}

    if not state.get("fax_image_b64"):
        return {"raw_text": "[ERROR: no fax image provided]"}

    response = llm.invoke(
        [HumanMessage(content=[
            {"type": "text", "text": OCR_SYSTEM},
            {"type": "image_url", "image_url": {
                "url": f"data:image/tiff;base64,{state['fax_image_b64']}"
            }},
        ])],
        config={"callbacks": [_tessera]},
    )
    return {"raw_text": response.content}


CLASSIFY_SYSTEM = """You are a medical document classifier working at a busy healthcare facility.
Classify the fax document and return JSON with exactly these keys:

  doc_type   — one of: referral, lab_result, prescription, prior_auth, eob, unknown
  confidence — float 0.0–1.0 (your certainty in the classification)
  urgency    — "stat" if the document is marked STAT, URGENT, CRITICAL, or contains critical lab values; otherwise "routine"
  reasoning  — one sentence explaining your classification

Return only the JSON object."""


def classify_node(state: FaxState) -> dict:
    response = llm.invoke(
        [
            SystemMessage(content=CLASSIFY_SYSTEM),
            HumanMessage(content=f"Document text:\n\n{state['raw_text']}"),
        ],
        config={"callbacks": [_tessera]},
    )
    try:
        raw = json.loads(response.content)
        doc_type   = raw.get("doc_type",   "unknown")
        confidence = float(raw.get("confidence", 0.5))
        urgency    = raw.get("urgency",    "routine")
    except (json.JSONDecodeError, ValueError):
        doc_type, confidence, urgency = "unknown", 0.0, "routine"

    _tessera.record_classification(doc_type, confidence, urgency)
    return {"doc_type": doc_type, "confidence": confidence, "urgency": urgency}


EXTRACT_SYSTEM = """You are a medical records specialist extracting structured data from a healthcare fax.
Return a JSON object with exactly these keys (use "unknown" for any field you cannot find):

  patient_name       — full name of the patient
  patient_dob        — date of birth (MM/DD/YYYY or as written)
  patient_mrn        — medical record number (if present, else "unknown")
  referring_provider — name of the referring physician or practice
  target_department  — which department/specialty this document should be routed to
  summary            — 2-sentence plain-English summary for a human reviewer

Return only the JSON object. Do NOT include any PHI in the summary field."""


def extract_phi_node(state: FaxState) -> dict:
    response = llm.invoke(
        [
            SystemMessage(content=EXTRACT_SYSTEM),
            HumanMessage(content=(
                f"Document type: {state['doc_type']}\n\n"
                f"Document text:\n{state['raw_text']}"
            )),
        ],
        config={"callbacks": [_tessera]},
    )
    try:
        raw = json.loads(response.content)
    except (json.JSONDecodeError, ValueError):
        raw = {}

    fields = {
        "patient_name":       raw.get("patient_name",       "unknown"),
        "patient_dob":        raw.get("patient_dob",        "unknown"),
        "patient_mrn":        raw.get("patient_mrn",        "unknown"),
        "referring_provider": raw.get("referring_provider", "unknown"),
        "target_department":  raw.get("target_department",  "unknown"),
    }

    # Log PHI access to Tessera audit trail (HIPAA §164.312(b))
    _tessera.record_phi_access(fields, node="extract_phi")

    return {**fields, "summary": raw.get("summary", "")}


def route_node(state: FaxState) -> dict:
    """
    Pure routing logic — no LLM call.
    STAT faxes and low-confidence classifications always go to human review.
    """
    if state["urgency"] == "stat":
        decision = "hold_for_review"
        reason = "STAT urgency — requires immediate human attention"
    elif state["confidence"] < 0.85:
        decision = "hold_for_review"
        reason = f"Classification confidence {state['confidence']:.0%} below threshold — human verification required"
    elif state["doc_type"] == "unknown":
        decision = "hold_for_review"
        reason = "Document type could not be determined"
    else:
        decision = "auto_route"
        reason = f"Routine {state['doc_type']} with {state['confidence']:.0%} confidence"

    _tessera.record_routing(
        decision=decision,
        target_department=state.get("target_department", "unknown"),
        confidence=state["confidence"],
        reason=reason,
    )
    return {"routing_decision": decision, "routing_reason": reason}


def ehr_node(state: FaxState) -> dict:
    """
    Push a FHIR Task to the EHR work queue.
    In production: POST /fhir/Task to Epic/Cerner SMART on FHIR endpoint.
    """
    # Simulated FHIR Task creation
    task_id = f"TASK-{hash(state['fax_id']) % 99999:05d}"
    print(f"  [EHR]   FHIR Task created: {task_id}")
    print(f"          → Department: {state['target_department']}")
    print(f"          → Type:       {state['doc_type']}")
    return {"ehr_task_id": task_id, "escalated": False, "escalation_note": ""}


def notify_node(state: FaxState) -> dict:
    """Send routing notification to the target provider/coordinator."""
    print(f"  [Notify] {state['target_department']} alerted — "
          f"FHIR Task {state['ehr_task_id']} in their queue")
    return {}


def hold_node(state: FaxState) -> dict:
    """
    Place fax in human review queue with a pre-filled summary.
    Posts to Tessera ITSM so the reviewer sees it in their feed.
    """
    note = (
        f"Fax {state['fax_id']} requires human review.\n"
        f"Reason: {state['routing_reason']}\n"
        f"Doc type: {state['doc_type']} (confidence {state['confidence']:.0%})\n"
        f"Urgency: {state['urgency'].upper()}\n"
        f"Summary: {state.get('summary', 'N/A')}\n"
        f"Suggested routing: {state.get('target_department', 'unknown')}"
    )

    try:
        import httpx as _httpx
        _httpx.post(
            f"{os.getenv('TESSERA_URL', 'http://localhost/api/v1')}/itsm/tickets",
            json={
                "org_id":   os.getenv("TESSERA_ORG", "demo-org"),
                "source":   "fax_triage",
                "title":    f"Fax review required: {state['doc_type']} — {state['urgency'].upper()}",
                "description": note,
                "priority": "high" if state["urgency"] == "stat" else "medium",
            },
            timeout=5.0,
        )
    except Exception:
        pass

    print(f"  [Hold]  Fax {state['fax_id']} → human review queue")
    print(f"          Reason: {state['routing_reason']}")
    return {"escalated": True, "escalation_note": note}


# ── Conditional routing ───────────────────────────────────────────────────────

def _routing_edge(state: FaxState) -> str:
    return "ehr_update" if state["routing_decision"] == "auto_route" else "hold_for_review"


# ── Build the LangGraph ───────────────────────────────────────────────────────

def build_fax_graph():
    graph = StateGraph(FaxState)

    graph.add_node("ocr",              ocr_node)
    graph.add_node("classify",         classify_node)
    graph.add_node("extract_phi",      extract_phi_node)
    graph.add_node("route",            route_node)
    graph.add_node("ehr_update",       ehr_node)
    graph.add_node("notify",           notify_node)
    graph.add_node("hold_for_review",  hold_node)

    graph.set_entry_point("ocr")
    graph.add_edge("ocr",         "classify")
    graph.add_edge("classify",    "extract_phi")
    graph.add_edge("extract_phi", "route")
    graph.add_conditional_edges("route", _routing_edge, {
        "ehr_update":      "ehr_update",
        "hold_for_review": "hold_for_review",
    })
    graph.add_edge("ehr_update",      "notify")
    graph.add_edge("notify",          END)
    graph.add_edge("hold_for_review", END)

    return graph.compile()


FAX_GRAPH = build_fax_graph()


# ── Pipeline entry point ──────────────────────────────────────────────────────

def process_fax(fax_id: str, fax_text: str) -> dict:
    """
    Process a single fax through the full triage pipeline.
    fax_text is used as raw_text_mock (skips OCR for testing).
    In production, pass fax_image_b64 instead.
    """
    global _tessera
    _tessera = TesseraFaxCallback(agent_name="fax_triage", fax_id=fax_id)

    print(f"\n{'='*62}")
    print(f"Processing fax {fax_id}")

    initial_state: FaxState = {
        "fax_id":          fax_id,
        "fax_image_b64":   None,
        "raw_text_mock":   fax_text,
        "raw_text":        "",
        "doc_type":        "",
        "confidence":      0.0,
        "urgency":         "routine",
        "patient_name":    "unknown",
        "patient_dob":     "unknown",
        "patient_mrn":     "unknown",
        "referring_provider":  "unknown",
        "target_department":   "unknown",
        "summary":         "",
        "routing_decision": "",
        "routing_reason":   "",
        "ehr_task_id":     None,
        "escalated":       False,
        "escalation_note": "",
    }

    final = FAX_GRAPH.invoke(initial_state)

    print(f"\n[Result] doc_type={final['doc_type']}  "
          f"confidence={final['confidence']:.0%}  "
          f"urgency={final['urgency']}")
    print(f"         routing={final['routing_decision']}  "
          f"→ {final.get('target_department','unknown')}")

    return {
        "fax_id":           fax_id,
        "doc_type":         final["doc_type"],
        "confidence":       final["confidence"],
        "urgency":          final["urgency"],
        "routing_decision": final["routing_decision"],
        "target_department": final["target_department"],
        "ehr_task_id":      final.get("ehr_task_id"),
        "escalated":        final["escalated"],
    }


# ── Sample faxes ─────────────────────────────────────────────────────────────

SAMPLE_FAXES = [
    (
        "FAX-4401",
        """
PATIENT REFERRAL
Date: June 28, 2026
From: Dr. Sarah Chen, MD — Riverside Primary Care
To:   St. Mary's Hospital Cardiology Department
Fax:  (555) 812-0044

Patient: James T. Morrison
DOB: 03/14/1958
MRN: 284710
Insurance: BlueCross PPO #BC9920141

Reason for Referral:
Mr. Morrison presents with intermittent chest pain and dyspnoea on exertion
over the past 6 weeks. ECG shows occasional PACs. Request cardiology
evaluation and stress test.

Urgency: Routine — please schedule within 14 days.

Signed,
Dr. Sarah Chen
NPI 1234567890
        """,
    ),
    (
        "FAX-4402",
        """
CRITICAL LAB RESULT — STAT

Lakeside Clinical Laboratory
Date: June 28, 2026  Time: 14:33

Patient: Elena Vasquez
DOB: 07/22/1971
MRN: 119843
Ordering Physician: Dr. R. Patel

TEST RESULT:
  Potassium (K+): 6.8 mEq/L  *** CRITICAL HIGH ***  Reference: 3.5–5.0

CRITICAL VALUE — STAT NOTIFICATION REQUIRED
Physician has been paged. Direct callback required within 30 minutes.

Lab Director: Dr. W. Huang, MD, PhD
        """,
    ),
    (
        "FAX-4403",
        """
PRESCRIPTION

Dr. Michael Torres, MD
Westside Oncology Group
NPI: 9876543210

Patient: Robert Nguyen         DOB: 11/05/1962
MRN: 330091                    Date: June 28, 2026

Rx:
  Ondansetron 8mg ODT
  Sig: Take 1 tablet 30 min before chemotherapy, then q8h x 24h PRN nausea
  Qty: 12 tablets    Refills: 0

  Dexamethasone 4mg tablet
  Sig: Take 2 tablets 30 min before chemotherapy, then 1 tablet BID x 2 days
  Qty: 10 tablets    Refills: 0

DEA: AM1234563
Signature: Dr. Michael Torres
        """,
    ),
]


if __name__ == "__main__":
    results = []
    for fax_id, fax_text in SAMPLE_FAXES:
        result = process_fax(fax_id, fax_text)
        results.append(result)

    print(f"\n{'='*62}")
    print("PIPELINE SUMMARY")
    print(f"{'='*62}")
    total      = len(results)
    auto_routed = sum(1 for r in results if r["routing_decision"] == "auto_route")
    held       = sum(1 for r in results if r["escalated"])
    stats      = sum(1 for r in results if r["urgency"] == "stat")
    print(f"Faxes processed   : {total}")
    print(f"Auto-routed       : {auto_routed}  ({100*auto_routed//total}%)")
    print(f"Held for review   : {held}  ({100*held//total}%)")
    print(f"STAT faxes        : {stats}")
    print(f"\nGovernance exhaust live at:")
    tessera_url = os.getenv("TESSERA_URL", "http://localhost/api/v1")
    org         = os.getenv("TESSERA_ORG", "demo-org")
    print(f"  GET {tessera_url}/governance/events/summary?org_id={org}")
    print(f"  GET {tessera_url}/audit/reports/summary?org_id={org}  (PHI access log)")
