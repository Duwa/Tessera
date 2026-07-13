"""
tessera_callback.py
Drop-in LangChain callback handler for Tessera governance.

Usage:
    from tessera_callback import TesseraCallback
    chain = LLMChain(llm=llm, prompt=prompt, callbacks=[TesseraCallback()])
"""
import os
import time
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


TESSERA_BASE  = os.getenv("TESSERA_URL",   "http://localhost/api/v1")
TESSERA_ORG   = os.getenv("TESSERA_ORG",   "demo-org")
TESSERA_TOKEN = os.getenv("TESSERA_TOKEN",  "")   # Bearer token from /auth/login


class TesseraCallback(BaseCallbackHandler):
    """
    Emits agent lifecycle events to Tessera's governance exhaust.
    Also creates ITSM tickets when the agent escalates to a human.

    Thread-safe: each run_id gets its own timing slot so parallel
    agent runs don't clobber each other's metrics.
    """

    def __init__(self, agent_name: str = "langchain_agent"):
        super().__init__()
        self.agent_name = agent_name
        self._run_start: Dict[str, float] = {}   # run_id → start timestamp

    # ── chain lifecycle ──────────────────────────────────────────────────────

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._run_start[str(run_id)] = time.monotonic()
        self._post("agent_start", {
            "agent":       serialized.get("name", self.agent_name),
            "input_chars": len(str(inputs)),
        }, run_id=run_id)

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        elapsed = self._elapsed(run_id)
        self._post("agent_complete", {
            "agent":         self.agent_name,
            "duration_ms":   elapsed,
            "auto_resolved": outputs.get("resolved", False),
            "confidence":    outputs.get("confidence", 0.0),
            "output_chars":  len(str(outputs)),
        }, run_id=run_id)

    def on_chain_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._post("agent_error", {
            "agent":       self.agent_name,
            "error":       type(error).__name__,
            "duration_ms": self._elapsed(run_id),
        }, run_id=run_id)

    # ── LLM token tracking ───────────────────────────────────────────────────

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        usage = {}
        if response.llm_output:
            usage = response.llm_output.get("usage", {}) or \
                    response.llm_output.get("token_usage", {})
        if usage:
            self._post("llm_usage", {
                "agent":              self.agent_name,
                "prompt_tokens":      usage.get("prompt_tokens",     0),
                "completion_tokens":  usage.get("completion_tokens", 0),
                "total_tokens":       usage.get("total_tokens",      0),
            }, run_id=run_id)

    # ── tool calls ───────────────────────────────────────────────────────────

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        tool_name = serialized.get("name", "unknown_tool")
        self._post("tool_call", {
            "agent": self.agent_name,
            "tool":  tool_name,
        }, run_id=run_id)

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        # Tool output that signals human escalation triggers an ITSM ticket.
        # The resolution agent is expected to return a JSON-serialisable dict
        # containing {"escalate": true, "summary": "...", "draft": "..."}.
        if isinstance(output, dict) and output.get("escalate"):
            self._create_itsm_ticket(
                summary=output.get("summary", "Agent escalation"),
                context=output.get("context", ""),
                draft_response=output.get("draft", ""),
                priority=output.get("priority", "medium"),
            )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _elapsed(self, run_id: UUID) -> int:
        start = self._run_start.pop(str(run_id), time.monotonic())
        return int((time.monotonic() - start) * 1000)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if TESSERA_TOKEN:
            h["Authorization"] = f"Bearer {TESSERA_TOKEN}"
        return h

    def _post(self, event_type: str, data: Dict[str, Any], run_id: Optional[UUID] = None) -> None:
        """Fire-and-forget POST to /governance/events. Never raises."""
        payload = {
            "org_id":     TESSERA_ORG,
            "event_type": event_type,
            "source":     "langchain",
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
            pass  # governance exhaust must never break the agent

    def _create_itsm_ticket(
        self,
        summary: str,
        context: str,
        draft_response: str,
        priority: str = "medium",
    ) -> None:
        """Create a real Tessera ITSM ticket when the agent escalates."""
        try:
            httpx.post(
                f"{TESSERA_BASE}/itsm/tickets",
                json={
                    "org_id":      TESSERA_ORG,
                    "source":      "langchain",
                    "title":       summary,
                    "description": context,
                    "ai_draft":    draft_response,
                    "priority":    priority,
                },
                headers=self._headers(),
                timeout=5.0,
            )
        except Exception:
            pass
