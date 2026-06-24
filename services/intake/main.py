"""
Tessera Sprint Intake Service — port 8009
==========================================
Secure Claude API proxy for the Sprint Planner.
Automatically emits a trace span to Tessera Inspector after every AI call.

Endpoints:
  POST /generate  — project intake → BDD user stories
  POST /bdd       — plain English task → single BDD scenario
  GET  /health
"""

import os, time, uuid, asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TRACE_URL         = os.getenv("TRACE_URL", "http://trace:8010")

app = FastAPI(title="Tessera Sprint Intake Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class AIRequest(BaseModel):
    messages: list[dict[str, Any]]
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1500


@app.get("/")
def root():
    return {"service": "intake", "port": 8009, "key_set": bool(ANTHROPIC_API_KEY)}


@app.get("/health")
def health():
    return {"status": "up", "service": "intake", "api_key_configured": bool(ANTHROPIC_API_KEY)}


@app.post("/generate")
async def generate_stories(req: AIRequest):
    _require_key()
    t0 = time.perf_counter()
    result = await _proxy_anthropic(req)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    asyncio.create_task(_emit_trace(
        run_name="Sprint Planner — Generate Stories",
        span_name="generate_stories",
        branch="value",
        model=req.model,
        tokens_in=result.get("usage", {}).get("input_tokens", 0),
        tokens_out=result.get("usage", {}).get("output_tokens", 0),
        latency_ms=latency_ms,
        input_text=(req.messages[-1].get("content", "") if req.messages else "")[:500],
        output_text=_first_text(result)[:500],
    ))
    return result


@app.post("/bdd")
async def convert_to_bdd(req: AIRequest):
    _require_key()
    t0 = time.perf_counter()
    result = await _proxy_anthropic(req)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    asyncio.create_task(_emit_trace(
        run_name="Sprint Planner — BDD Convert",
        span_name="convert_to_bdd",
        branch="alignment",
        model=req.model,
        tokens_in=result.get("usage", {}).get("input_tokens", 0),
        tokens_out=result.get("usage", {}).get("output_tokens", 0),
        latency_ms=latency_ms,
        input_text=(req.messages[-1].get("content", "") if req.messages else "")[:500],
        output_text=_first_text(result)[:500],
    ))
    return result


def _require_key():
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY not set. Create a .env file in the project root with ANTHROPIC_API_KEY=sk-..."
        )


def _first_text(result: dict) -> str:
    try:
        return result["content"][0]["text"]
    except Exception:
        return ""


async def _proxy_anthropic(req: AIRequest) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={"model": req.model, "max_tokens": req.max_tokens, "messages": req.messages},
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Anthropic API error: {e.response.text}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))


async def _emit_trace(
    run_name: str, span_name: str, branch: str, model: str,
    tokens_in: int, tokens_out: int, latency_ms: int,
    input_text: str, output_text: str,
):
    """Fire-and-forget: push a run + LLM span to the trace service."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r  = await c.post(f"{TRACE_URL}/run", json={"name": run_name, "org_id": "tessera", "branch": branch})
            run_id = r.json().get("run_id", str(uuid.uuid4()))
            total = tokens_in + tokens_out or 1
            await c.post(f"{TRACE_URL}/span", json={
                "run_id": run_id, "type": "llm", "name": span_name,
                "branch": branch, "org_id": "tessera",
                "input": input_text, "output": output_text,
                "tokens_input": tokens_in, "tokens_output": tokens_out,
                "latency_ms": latency_ms,
                "belief_delta": round(tokens_out / total * 0.05, 4),
            })
            await c.patch(f"{TRACE_URL}/run/{run_id}", json={
                "status": "completed",
                "qf_ratio": round(tokens_out / max(latency_ms / 1000, 0.1), 2),
            })
    except Exception:
        pass  # trace emission must never block or fail the response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8009)))
