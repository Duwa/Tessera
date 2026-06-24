"""
Tessera Agent Factory — port 8011
===================================
Given a mandate, selects the optimal agent framework and generates
a complete, runnable agent configuration. Traces every call to Inspector.

Endpoints:
  POST /select    — mandate → framework recommendation + reasoning
  POST /generate  — mandate → full agent config (nodes, edges, tools, SDK snippet)
  GET  /health
"""

import os, time, uuid, json
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TRACE_URL         = os.getenv("TRACE_URL", "http://trace:8010")
MODEL             = "claude-sonnet-4-6"

app = FastAPI(title="Tessera Agent Factory", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class MandateRequest(BaseModel):
    mandate: str
    branch: str = "value"
    context: Optional[str] = None
    framework: Optional[str] = None   # used by /generate to lock in a framework


SELECT_PROMPT = """\
You are Tessera's Agent Factory. Given a mandate, recommend the best agent framework.

Available frameworks:
- langgraph   : stateful multi-step agents with conditional routing, loops, tool use, streaming, human-in-the-loop
- crewai      : multi-agent collaboration with distinct roles (researcher, writer, reviewer, orchestrator)
- autogen     : conversational agents that generate and execute code; great for analysis and data tasks
- custom      : simple linear pipelines — when control, traceability, and minimal dependencies matter most

Mandate : {mandate}
Branch  : {branch}
Context : {context}

Reply with valid JSON only — no markdown fences, no prose outside the JSON:
{{
  "framework": "<langgraph|crewai|autogen|custom>",
  "confidence": <0.0-1.0>,
  "reasoning": "<2-3 sentences explaining the fit>",
  "alternative": "<second-best framework name and one sentence why it's the runner-up>",
  "key_considerations": ["<3-4 short bullet points the team should keep in mind during implementation>"]
}}"""

GENERATE_PROMPT = """\
You are Tessera's Agent Factory. Generate a complete, practical agent configuration.

Framework : {framework}
Mandate   : {mandate}
Branch    : {branch}
Context   : {context}

Reply with valid JSON only — no markdown fences:
{{
  "agent_name": "<PascalCase name>",
  "framework": "{framework}",
  "branch": "{branch}",
  "description": "<one sentence>",
  "nodes": [
    {{"id": "<snake_case>", "type": "<llm|tool|router|memory|human>", "name": "<display name>", "description": "<what it does>", "model": "<claude-sonnet-4-6 or null>"}}
  ],
  "edges": [
    {{"from": "<id>", "to": "<id>", "condition": "<always|on_success|on_failure|conditional: description>"}}
  ],
  "tools": [
    {{"name": "<tool_name>", "description": "<what it does>", "returns": "<description of return value>"}}
  ],
  "state_schema": {{
    "<field_name>": "<str|int|float|list|dict|bool>"
  }},
  "entry_point": "<node_id>",
  "tessera_sdk": "from tessera_sdk import TesseraTracer\\ntracer = TesseraTracer()\\n\\nasync def run(mandate: str):\\n    async with tracer.run('<agent_name>', branch='<branch>') as run:\\n        # instrument your nodes here\\n        pass"
}}"""


@app.get("/health")
def health():
    return {"status": "up", "service": "agent-factory", "api_key_configured": bool(ANTHROPIC_API_KEY)}


@app.post("/select")
async def select_framework(req: MandateRequest):
    """Analyse a mandate and recommend the best agent framework."""
    _require_key()
    prompt = SELECT_PROMPT.format(
        mandate=req.mandate,
        branch=req.branch,
        context=req.context or "None provided",
    )
    t0 = time.perf_counter()
    raw = await _claude(prompt, max_tokens=700)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    result = _parse_json(raw["content"][0]["text"])
    await _trace("Agent Factory — Select", "select_framework", req.branch,
                 raw.get("usage", {}), latency_ms, req.mandate[:300], result)
    return result


@app.post("/generate")
async def generate_config(req: MandateRequest):
    """Generate a full agent configuration for the given mandate."""
    _require_key()
    framework = req.framework or "custom"
    prompt = GENERATE_PROMPT.format(
        framework=framework,
        mandate=req.mandate,
        branch=req.branch,
        context=req.context or "None provided",
    )
    t0 = time.perf_counter()
    raw = await _claude(prompt, max_tokens=2500)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    result = _parse_json(raw["content"][0]["text"])
    await _trace("Agent Factory — Generate", "generate_config", req.branch,
                 raw.get("usage", {}), latency_ms, req.mandate[:300], result)
    return result


# ── Helpers ───────────────────────────────────────────────────────


def _require_key():
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY not set. Add it to your .env file in the project root."
        )


async def _claude(prompt: str, max_tokens: int = 1000) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={"model": MODEL, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]},
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code,
                                detail=f"Anthropic error: {e.response.text}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text, "parse_error": "Claude response was not valid JSON"}


async def _trace(run_name: str, span_name: str, branch: str,
                 usage: dict, latency_ms: int, inp: str, out: dict):
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.post(f"{TRACE_URL}/run",
                             json={"name": run_name, "org_id": "tessera", "branch": branch})
            run_id = r.json().get("run_id", str(uuid.uuid4()))
            tokens_in  = usage.get("input_tokens", 0)
            tokens_out = usage.get("output_tokens", 0)
            await c.post(f"{TRACE_URL}/span", json={
                "run_id": run_id, "type": "llm", "name": span_name,
                "branch": branch, "org_id": "tessera",
                "input": inp, "output": json.dumps(out)[:600],
                "tokens_input": tokens_in, "tokens_output": tokens_out,
                "latency_ms": latency_ms,
                "belief_delta": round(tokens_out / max(tokens_in + tokens_out, 1) * 0.04, 4),
            })
            await c.patch(f"{TRACE_URL}/run/{run_id}", json={
                "status": "completed",
                "qf_ratio": round(tokens_out / max(latency_ms / 1000, 0.1), 2),
            })
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8011)))
