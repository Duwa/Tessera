"""
tessera_fax_callback.py
Extends TesseraCallback with healthcare-specific PHI field detection.

Every time the fax agent extracts a PHI field, a phi_access event is posted
to Tessera's audit trail — satisfying HIPAA §164.312(b) audit controls.
"""
import os
import time
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


TESSERA_BASE  = os.getenv("TESSERA_URL",  "http://localhost/api/v1")
TESSERA_ORG   = os.getenv("TESSERA_ORG",  "demo-org")
TESSERA_TOKEN = os.getenv("TESSERA_TOKEN", "")

# Fields that constitute PHI under HIPAA Safe Harbor (45 CFR §164.514)
PHI_FIELDS = frozenset({
    "patient_name", "patient_dob", "patient_dod", "patient_mrn",
    "patient_address", "patient_phone", "patient_email",
    "patient_ssn", "patient_insurance_id", "patient_account_number",
    "referring_provider", "attending_provider", "npi",
    "diagnosis_code", "procedure_code", "medication_name",
    "prescription_number", "claim_number",
})


class TesseraFaxCallback(BaseCallbackHandler):
    """
    Governance callback for the healthcare fax triage agent.

    Emits:
    - agent_start / agent_complete / agent_error  (chain lifecycle)
    - llm_usage                                   (token cost tracking)
    - tool_call                                   (tool invocations)
    - phi_access                                  (every PHI field touched)
    - fax_classified                              (doc type + confidence)
    - fax_routed / fax_held                       (routing outcome)

    phi_access events are hash-chained in the audit trail and form the
    primary HIPAA §164.312(b) evidence for the compliance reporter.
    """

    def __init__(self, agent_name: str = "fax_triage", fax_id: str = "unknown"):
        super().__init__()
        self.agent_name = agent_name
        self.fax_id     = fax_id
        self._run_start: Dict[str, float] = {}

    # ── chain lifecycle ──────────────────────────────────────────────────────

    def on_chain_start(self, serialized, inputs, *, run_id, **kwargs):
        self._run_start[str(run_id)] = time.monotonic()
        self._post("agent_start", {
            "agent":   self.agent_name,
            "fax_id":  self.fax_id,
            "input_chars": len(str(inputs)),
        }, run_id=run_id)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._post("agent_complete", {
            "agent":        self.agent_name,
            "fax_id":       self.fax_id,
            "duration_ms":  self._elapsed(run_id),
            "auto_resolved": outputs.get("routing_decision") == "auto_route",
            "confidence":   outputs.get("confidence", 0.0),
        }, run_id=run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._post("agent_error", {
            "agent":      self.agent_name,
            "fax_id":     self.fax_id,
            "error":      type(error).__name__,
            "duration_ms": self._elapsed(run_id),
        }, run_id=run_id)

    # ── LLM token tracking ───────────────────────────────────────────────────

    def on_llm_end(self, response: LLMResult, *, run_id, **kwargs):
        usage = {}
        if response.llm_output:
            usage = response.llm_output.get("usage", {}) or \
                    response.llm_output.get("token_usage", {})
        if usage:
            self._post("llm_usage", {
                "agent":             self.agent_name,
                "fax_id":            self.fax_id,
                "prompt_tokens":     usage.get("prompt_tokens",     0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens":      usage.get("total_tokens",      0),
            }, run_id=run_id)

    # ── tool calls ───────────────────────────────────────────────────────────

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._post("tool_call", {
            "agent":  self.agent_name,
            "fax_id": self.fax_id,
            "tool":   serialized.get("name", "unknown_tool"),
        }, run_id=run_id)

    # ── PHI-specific governance events ───────────────────────────────────────

    def record_phi_access(self, fields: Dict[str, Any], node: str = "extract_phi") -> None:
        """
        Call this after extract_phi_node returns structured fields.
        Posts one phi_access audit event per PHI field touched.
        These events are hash-chained in the Tessera audit trail.
        """
        phi_fields_touched = {k: v for k, v in fields.items()
                              if k in PHI_FIELDS and v and v != "unknown"}
        for field_name, field_value in phi_fields_touched.items():
            self._post("phi_access", {
                "agent":       self.agent_name,
                "fax_id":      self.fax_id,
                "phi_field":   field_name,
                "node":        node,
                # Never log the actual value — log that it was accessed
                "value_present": True,
            })

    def record_classification(self, doc_type: str, confidence: float, urgency: str) -> None:
        """Call after classify_node to log the fax classification decision."""
        self._post("fax_classified", {
            "agent":      self.agent_name,
            "fax_id":     self.fax_id,
            "doc_type":   doc_type,
            "confidence": confidence,
            "urgency":    urgency,
        })

    def record_routing(self, decision: str, target_department: str,
                       confidence: float, reason: str) -> None:
        """Call after route_node with the final routing outcome."""
        event = "fax_routed" if decision == "auto_route" else "fax_held"
        self._post(event, {
            "agent":             self.agent_name,
            "fax_id":            self.fax_id,
            "routing_decision":  decision,
            "target_department": target_department,
            "confidence":        confidence,
            "reason":            reason,
        })

    # ── helpers ──────────────────────────────────────────────────────────────

    def _elapsed(self, run_id) -> int:
        start = self._run_start.pop(str(run_id), time.monotonic())
        return int((time.monotonic() - start) * 1000)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if TESSERA_TOKEN:
            h["Authorization"] = f"Bearer {TESSERA_TOKEN}"
        return h

    def _post(self, event_type: str, data: Dict[str, Any],
              run_id: Optional[UUID] = None) -> None:
        """Fire-and-forget POST to /governance/events. Never raises."""
        payload = {
            "org_id":     TESSERA_ORG,
            "event_type": event_type,
            "source":     "fax_triage",
            "run_id":     str(run_id) if run_id else None,
            **data,
        }
        try:
            httpx.post(
                f"{TESSERA_BASE}/governance/events",
                json=payload,
                headers=self._headers(),
                timeout=3.0,
            )
        except Exception:
            pass  # governance exhaust must never block patient-care workflows
