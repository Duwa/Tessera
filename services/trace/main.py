"""
Tessera Trace Service — port 8010
===================================
Tessera's own observability layer. No LangSmith, no external dependency.

Every agent run is a tree of spans. Spans carry Tessera-specific signals
that no external tool can compute because they require the Digital Twin:
  - belief_delta   : how this step shifted the org's belief vector
  - fitness_position: current NK fitness score
  - branch         : which workforce branch this step belongs to

Storage  : Redis (real-time + history). Falls back to in-memory for dev.
Live SSE : Redis pub/sub → Server-Sent Events to the Inspector UI.
"""

import os, json, uuid
from datetime import datetime, timezone
from typing import Optional, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
MAX_RUNS   = 500
MAX_SPANS  = 2000

# ── Redis setup (sync for writes, async for SSE) ─────────────────
try:
    import redis as _redis_lib
    _r = _redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    _r.ping()
    REDIS_OK = True
except Exception:
    REDIS_OK = False
    _mem: dict = {}   # fallback in-memory store

app = FastAPI(title="Tessera Trace", description="Tessera's own observability layer", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Models ────────────────────────────────────────────────────────

class RunCreate(BaseModel):
    run_id: Optional[str] = None
    name: str
    org_id: str = "tessera-demo"
    branch: Optional[str] = None
    metadata: Optional[dict] = None

class RunUpdate(BaseModel):
    status: Optional[str] = None        # running | completed | failed
    qf_ratio: Optional[float] = None
    alert_level: Optional[str] = None

class SpanCreate(BaseModel):
    span_id: Optional[str] = None
    run_id: str
    parent_span_id: Optional[str] = None
    type: str = "llm"                   # llm | tool | node | agent | chain
    name: str
    input: Optional[Any] = None
    output: Optional[Any] = None
    tokens_input: int = 0
    tokens_output: int = 0
    latency_ms: Optional[int] = None
    ts_start: Optional[str] = None
    ts_end: Optional[str] = None
    belief_delta: Optional[float] = None
    fitness_position: Optional[float] = None
    branch: Optional[str] = None
    org_id: str = "tessera-demo"
    error: Optional[str] = None
    metadata: Optional[dict] = None


# ── Storage helpers ───────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _set(key: str, val: Any):
    if REDIS_OK:
        _r.set(key, json.dumps(val))
    else:
        _mem[key] = val

def _get(key: str) -> Any:
    if REDIS_OK:
        v = _r.get(key)
        return json.loads(v) if v else None
    return _mem.get(key)

def _lpush(key: str, val: Any, maxlen: int = MAX_SPANS):
    if REDIS_OK:
        _r.rpush(key, json.dumps(val))
        _r.ltrim(key, -maxlen, -1)
    else:
        lst = _mem.setdefault(key, [])
        lst.append(val)
        if len(lst) > maxlen:
            _mem[key] = lst[-maxlen:]

def _lall(key: str) -> list:
    if REDIS_OK:
        return [json.loads(i) for i in _r.lrange(key, 0, -1)]
    return _mem.get(key, [])

def _pub(channel: str, data: Any):
    if REDIS_OK:
        try:
            _r.publish(channel, json.dumps(data))
        except Exception:
            pass


# ── Routes ───────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"service": "trace", "port": 8010, "redis": REDIS_OK}

@app.get("/health")
def health():
    return {"status": "up", "service": "trace", "redis": REDIS_OK}


@app.post("/run")
def create_run(req: RunCreate):
    run_id = req.run_id or str(uuid.uuid4())
    run = {
        "run_id":          run_id,
        "name":            req.name,
        "org_id":          req.org_id,
        "branch":          req.branch,
        "status":          "running",
        "started_at":      _now(),
        "ended_at":        None,
        "span_count":      0,
        "tokens_input":    0,
        "tokens_output":   0,
        "total_latency_ms": 0,
        "qf_ratio":        None,
        "alert_level":     "green",
        "metadata":        req.metadata or {},
    }
    _set(f"tr:run:{run_id}", run)
    if REDIS_OK:
        _r.lpush("tr:run_index", run_id)
        _r.ltrim("tr:run_index", 0, MAX_RUNS - 1)
    else:
        idx = _mem.setdefault("tr:run_index", [])
        idx.insert(0, run_id)
    _pub("tr:live:all", {**run, "event": "run_created"})
    return run


@app.patch("/run/{run_id}")
def update_run(run_id: str, req: RunUpdate):
    run = _get(f"tr:run:{run_id}")
    if not run:
        raise HTTPException(404, f"Run {run_id} not found")
    if req.status:
        run["status"] = req.status
        if req.status in ("completed", "failed"):
            run["ended_at"] = _now()
    if req.qf_ratio is not None:
        run["qf_ratio"] = round(req.qf_ratio, 4)
    if req.alert_level:
        run["alert_level"] = req.alert_level
    _set(f"tr:run:{run_id}", run)
    _pub("tr:live:all", {**run, "event": "run_updated"})
    return run


@app.post("/span")
def add_span(req: SpanCreate):
    span_id = req.span_id or str(uuid.uuid4())
    span = {
        "span_id":          span_id,
        "run_id":           req.run_id,
        "parent_span_id":   req.parent_span_id,
        "type":             req.type,
        "name":             req.name,
        "input":            req.input,
        "output":           req.output,
        "tokens_input":     req.tokens_input,
        "tokens_output":    req.tokens_output,
        "latency_ms":       req.latency_ms,
        "ts_start":         req.ts_start or _now(),
        "ts_end":           req.ts_end,
        "belief_delta":     req.belief_delta,
        "fitness_position": req.fitness_position,
        "branch":           req.branch,
        "org_id":           req.org_id,
        "error":            req.error,
        "metadata":         req.metadata or {},
    }
    _lpush(f"tr:spans:{req.run_id}", span)

    # Update run aggregates
    run = _get(f"tr:run:{req.run_id}")
    if run:
        run["span_count"]      = run.get("span_count", 0) + 1
        run["tokens_input"]    = run.get("tokens_input", 0) + (req.tokens_input or 0)
        run["tokens_output"]   = run.get("tokens_output", 0) + (req.tokens_output or 0)
        run["total_latency_ms"] = run.get("total_latency_ms", 0) + (req.latency_ms or 0)
        _set(f"tr:run:{req.run_id}", run)

    # Publish for live streaming
    _pub(f"tr:live:{req.run_id}", {**span, "event": "span"})
    _pub("tr:live:all", {**span, "event": "span"})
    return span


@app.get("/run/{run_id}")
def get_run(run_id: str):
    run = _get(f"tr:run:{run_id}")
    if not run:
        raise HTTPException(404, f"Run {run_id} not found")
    spans = _lall(f"tr:spans:{run_id}")
    return {**run, "spans": spans}


@app.get("/runs")
def list_runs(limit: int = Query(50, le=200)):
    if REDIS_OK:
        ids = _r.lrange("tr:run_index", 0, limit - 1)
    else:
        ids = _mem.get("tr:run_index", [])[:limit]
    runs = [r for r in (_get(f"tr:run:{i}") for i in ids) if r]
    return {"runs": runs, "count": len(runs)}


@app.delete("/run/{run_id}")
def delete_run(run_id: str):
    if REDIS_OK:
        _r.delete(f"tr:run:{run_id}", f"tr:spans:{run_id}")
        _r.lrem("tr:run_index", 0, run_id)
    else:
        _mem.pop(f"tr:run:{run_id}", None)
        _mem.pop(f"tr:spans:{run_id}", None)
        idx = _mem.get("tr:run_index", [])
        if run_id in idx:
            idx.remove(run_id)
    return {"deleted": run_id}


@app.delete("/runs")
def clear_all_runs():
    """Clear all runs — useful for resetting the inspector view."""
    if REDIS_OK:
        ids = _r.lrange("tr:run_index", 0, -1)
        for i in ids:
            _r.delete(f"tr:run:{i}", f"tr:spans:{i}")
        _r.delete("tr:run_index")
    else:
        _mem.clear()
    return {"cleared": True}


@app.get("/live")
async def live_stream(run_id: Optional[str] = None):
    """
    Server-Sent Events stream. run_id=None → all runs.
    Requires Redis pub/sub. Falls back to a single 'not available' message.
    """
    channel = f"tr:live:{run_id}" if run_id else "tr:live:all"

    async def gen():
        if not REDIS_OK:
            yield f"data: {json.dumps({'error': 'Redis not available for live streaming'})}\n\n"
            return

        try:
            import redis.asyncio as aioredis
            ar = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            ps = ar.pubsub()
            await ps.subscribe(channel)
            yield f"data: {json.dumps({'event': 'connected', 'channel': channel})}\n\n"
            try:
                async for msg in ps.listen():
                    if msg["type"] == "message":
                        yield f"data: {msg['data']}\n\n"
                    await asyncio.sleep(0)
            finally:
                await ps.unsubscribe(channel)
                await ar.aclose()
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8010)))
