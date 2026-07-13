"""
trace_walkthrough.py
Shows exactly what Tessera receives for each sample fax — no API keys needed.

Three faxes:
  FAX-4401  Routine cardiology referral           → auto-routed to cardiology
  FAX-4402  STAT critical potassium lab result     → held for human review
  FAX-4403  Oncology prescription                 → auto-routed to pharmacy

Run:  python trace_walkthrough.py
"""
import json
import sys
sys.stdout.reconfigure(encoding="utf-8")

# ── FAX-4401: routine referral — auto-routed ─────────────────────────────────

TRACE_FAX_4401 = [
    {
        "phase": "TesseraFaxCallback.on_chain_start",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "agent_start",
            "source": "fax_triage", "agent": "fax_triage", "fax_id": "FAX-4401",
        },
    },
    {
        "phase": "ocr_node  [raw_text_mock path — skips vision call in test mode]",
        "endpoint": "(no API call — mock text used)",
        "payload": {},
    },
    {
        "phase": "classify_node  [LLM call: doc_type + confidence]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "fax_classified",
            "source": "fax_triage", "fax_id": "FAX-4401",
            "doc_type": "referral", "confidence": 0.97, "urgency": "routine",
        },
    },
    {
        "phase": "extract_phi_node  [LLM call: structured PHI extraction]",
        "endpoint": "POST /governance/events  (one event per PHI field)",
        "payload": {
            "org_id": "demo-org", "event_type": "phi_access",
            "source": "fax_triage", "fax_id": "FAX-4401",
            "phi_field": "patient_name", "node": "extract_phi", "value_present": True,
        },
    },
    {
        "phase": "extract_phi_node  [PHI field 2 of 4]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "phi_access",
            "source": "fax_triage", "fax_id": "FAX-4401",
            "phi_field": "patient_dob", "node": "extract_phi", "value_present": True,
        },
    },
    {
        "phase": "extract_phi_node  [PHI field 3 of 4]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "phi_access",
            "source": "fax_triage", "fax_id": "FAX-4401",
            "phi_field": "patient_mrn", "node": "extract_phi", "value_present": True,
        },
    },
    {
        "phase": "extract_phi_node  [PHI field 4 of 4]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "phi_access",
            "source": "fax_triage", "fax_id": "FAX-4401",
            "phi_field": "referring_provider", "node": "extract_phi", "value_present": True,
        },
    },
    {
        "phase": "route_node  [confidence 0.97 >= 0.85, urgency=routine → auto_route]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "fax_routed",
            "source": "fax_triage", "fax_id": "FAX-4401",
            "routing_decision": "auto_route", "target_department": "Cardiology",
            "confidence": 0.97, "reason": "Routine referral with 97% confidence",
        },
    },
    {
        "phase": "ehr_node  [FHIR Task pushed to Epic work queue]",
        "endpoint": "POST /fhir/Task  (to EHR connector)",
        "payload": {
            "resourceType": "Task", "status": "requested",
            "intent": "order", "priority": "routine",
            "code": {"text": "referral"},
            "for": {"reference": "Patient/MRN-284710"},
            "requester": {"display": "Dr. Sarah Chen"},
            "owner": {"display": "St. Mary's Cardiology"},
        },
    },
    {
        "phase": "TesseraFaxCallback.on_chain_end",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "agent_complete",
            "source": "fax_triage", "fax_id": "FAX-4401",
            "duration_ms": 2340, "auto_resolved": True, "confidence": 0.97,
        },
    },
]

# ── FAX-4402: STAT critical lab — held for human review ──────────────────────

TRACE_FAX_4402 = [
    {
        "phase": "TesseraFaxCallback.on_chain_start",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "agent_start",
            "source": "fax_triage", "agent": "fax_triage", "fax_id": "FAX-4402",
        },
    },
    {
        "phase": "classify_node  [STAT keyword detected → urgency=stat]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "fax_classified",
            "source": "fax_triage", "fax_id": "FAX-4402",
            "doc_type": "lab_result", "confidence": 0.99, "urgency": "stat",
        },
    },
    {
        "phase": "extract_phi_node  [PHI fields — patient_name, patient_dob, patient_mrn]",
        "endpoint": "POST /governance/events  (3x phi_access events)",
        "payload": {
            "org_id": "demo-org", "event_type": "phi_access",
            "source": "fax_triage", "fax_id": "FAX-4402",
            "phi_field": "patient_name", "node": "extract_phi", "value_present": True,
        },
    },
    {
        "phase": "route_node  [urgency=stat → hold regardless of confidence]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "fax_held",
            "source": "fax_triage", "fax_id": "FAX-4402",
            "routing_decision": "hold_for_review",
            "target_department": "Dr. R. Patel / Ordering Physician",
            "confidence": 0.99,
            "reason": "STAT urgency — requires immediate human attention",
        },
    },
    {
        "phase": "hold_node  [ITSM ticket created for human reviewer]",
        "endpoint": "POST /itsm/tickets",
        "payload": {
            "org_id": "demo-org", "source": "fax_triage",
            "title": "Fax review required: lab_result — STAT",
            "description": (
                "Fax FAX-4402 requires human review.\n"
                "Reason: STAT urgency — requires immediate human attention\n"
                "Doc type: lab_result (confidence 99%)\n"
                "Urgency: STAT\n"
                "Summary: Critical potassium lab result for patient. Physician callback required.\n"
                "Suggested routing: Ordering Physician / Dr. R. Patel"
            ),
            "priority": "high",
        },
    },
    {
        "phase": "TesseraFaxCallback.on_chain_end",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "agent_complete",
            "source": "fax_triage", "fax_id": "FAX-4402",
            "duration_ms": 1890, "auto_resolved": False, "confidence": 0.99,
        },
    },
]

# ── FAX-4403: oncology prescription — auto-routed to pharmacy ─────────────────

TRACE_FAX_4403 = [
    {
        "phase": "classify_node  [prescription, routine]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "fax_classified",
            "source": "fax_triage", "fax_id": "FAX-4403",
            "doc_type": "prescription", "confidence": 0.94, "urgency": "routine",
        },
    },
    {
        "phase": "extract_phi_node  [patient_name, patient_dob, patient_mrn, referring_provider]",
        "endpoint": "POST /governance/events  (4x phi_access events)",
        "payload": {
            "org_id": "demo-org", "event_type": "phi_access",
            "source": "fax_triage", "fax_id": "FAX-4403",
            "phi_field": "medication_name", "node": "extract_phi", "value_present": True,
        },
    },
    {
        "phase": "route_node  [confidence 0.94 >= 0.85, routine → auto_route]",
        "endpoint": "POST /governance/events",
        "payload": {
            "org_id": "demo-org", "event_type": "fax_routed",
            "source": "fax_triage", "fax_id": "FAX-4403",
            "routing_decision": "auto_route",
            "target_department": "Oncology Pharmacy",
            "confidence": 0.94,
            "reason": "Routine prescription with 94% confidence",
        },
    },
    {
        "phase": "ehr_node  [FHIR Task pushed to pharmacy queue]",
        "endpoint": "POST /fhir/Task",
        "payload": {
            "resourceType": "Task", "status": "requested",
            "intent": "order", "priority": "routine",
            "code": {"text": "prescription"},
            "for": {"reference": "Patient/MRN-330091"},
            "owner": {"display": "Westside Oncology Pharmacy"},
        },
    },
]

# ── What the audit trail looks like after all 3 faxes ──────────────────────

AUDIT_SUMMARY_AFTER_THREE_FAXES = {
    "org_id": "demo-org",
    "total_events": 18,
    "phi_access_events": 11,      # critical HIPAA metric
    "auto_resolved": 2,           # FAX-4401, FAX-4403
    "escalations": 1,             # FAX-4402 STAT
    "sources": {"fax_triage": 18},
    "computed_roai": 9.6,         # 2/3 auto-routed × value_per_run / compute_cost
    "deflection_rate": 0.67,
    "phi_fields_most_accessed": [
        {"field": "patient_name", "count": 3},
        {"field": "patient_dob",  "count": 3},
        {"field": "patient_mrn",  "count": 3},
        {"field": "referring_provider", "count": 2},
        {"field": "medication_name",    "count": 1},
    ],
}

# ── What Tessera's HIPAA report maps this to ───────────────────────────────

HIPAA_CONTROL_EVIDENCE = {
    "control": "§164.312(b) — Audit Controls",
    "requirement": "Hardware, software, and/or procedural mechanisms that record and examine activity in information systems that contain or use ePHI",
    "evidence_generated": [
        "11 phi_access events across 3 faxes, each hash-chained in the audit log",
        "Every event carries: fax_id, phi_field, node, timestamp, org_id",
        "No PHI values stored in audit events — only field names and access flags",
        "Audit trail exportable as CSV via GET /audit/export?org_id=demo-org&format=csv",
    ],
    "status": "PASS",
    "auditor_note": (
        "Tessera's fax triage agent demonstrates real-time ePHI access logging "
        "without storing PHI in the audit trail itself. Each access event is "
        "tamper-evident via hash chain. Evidence package generated automatically "
        "by gov.compliance-reporter blueprint."
    ),
}


if __name__ == "__main__":
    print("=" * 64)
    print("FAX-4401 (routine cardiology referral) — what Tessera receives")
    print("=" * 64)
    for i, event in enumerate(TRACE_FAX_4401, 1):
        print(f"\n  [{i}] {event['phase']}")
        if event["payload"]:
            print(f"      -> {event['endpoint']}")
            print(f"      {json.dumps(event['payload'], indent=6)}")

    print("\n" + "=" * 64)
    print("FAX-4402 (STAT critical lab) — what Tessera receives")
    print("=" * 64)
    for i, event in enumerate(TRACE_FAX_4402, 1):
        print(f"\n  [{i}] {event['phase']}")
        if event["payload"]:
            print(f"      -> {event['endpoint']}")
            print(f"      {json.dumps(event['payload'], indent=6)}")

    print("\n" + "=" * 64)
    print("FAX-4403 (oncology prescription) — abbreviated")
    print("=" * 64)
    for i, event in enumerate(TRACE_FAX_4403, 1):
        print(f"\n  [{i}] {event['phase']}")
        if event["payload"]:
            print(f"      -> {event['endpoint']}")
            print(f"      {json.dumps(event['payload'], indent=6)}")

    print("\n" + "=" * 64)
    print("GET /governance/events/summary — after 3 faxes")
    print("=" * 64)
    print(json.dumps(AUDIT_SUMMARY_AFTER_THREE_FAXES, indent=2))

    print("\n" + "=" * 64)
    print("HIPAA §164.312(b) — how Tessera maps this to audit evidence")
    print("=" * 64)
    print(json.dumps(HIPAA_CONTROL_EVIDENCE, indent=2))
