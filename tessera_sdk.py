"""
Tessera SDK — Python tracing client
=====================================
Instruments any agent code to send traces to the Tessera Trace service.
No LangSmith, no OpenTelemetry. Just Tessera's own trace protocol.

Install: pip install httpx   (only external dep)

Usage
-----
from tessera_sdk import TesseraTracer

tracer = TesseraTracer(endpoint="http://localhost/api/v1/trace", org_id="my-org")

# --- Context manager style ---
async with tracer.run("Customer Reply Agent", branch="alignment") as run:
    async with run.span("validate", type="node") as s:
        s["input"] = ticket_id
        result = validate(ticket_id)
        s["output"] = result

    async with run.span("generate_reply", type="llm",
                         tokens_input=312, tokens_output=187,
                         belief_delta=-0.03, fitness_position=0.69) as s:
        reply = await call_claude(prompt)
        s["output"] = reply

# --- Decorator style ---
@tracer.node(branch="alignment")
async def draft_reply(state: dict, *, _run_id=None, _parent_id=None):
    reply = await call_claude(state["prompt"])
    return {"reply": reply}

# --- Manual span ---
span = await tracer.llm_span(
    run_id=run_id,
    name="classify_intent",
    input_text=prompt,
    output_text=response,
    tokens_in=180, tokens_out=42,
    latency_ms=340,
    branch="alignment",
    belief_delta=0.01,
)
"""

import uuid, json, time, functools
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, Any

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False
    try:
        import urllib.request as _urllib
        _HAS_URLLIB = True
    except ImportError:
        _HAS_URLLIB = False


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TesseraTracer:
    """
    Main entry point. One instance per application / agent process.
    Thread-safe for async code.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost/api/v1/trace",
        org_id: str = "tessera-demo",
        silent: bool = True,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.org_id   = org_id
        self.silent   = silent   # swallow errors instead of raising

    # ── HTTP helpers ─────────────────────────────────────────────

    def _post_sync(self, path: str, body: dict) -> dict:
        url = f"{self.endpoint}/{path.lstrip('/')}"
        payload = json.dumps(body).encode()
        try:
            if _HAS_HTTPX:
                r = httpx.post(url, content=payload,
                               headers={"content-type": "application/json"}, timeout=5.0)
                return r.json()
            elif _HAS_URLLIB:
                req = _urllib.Request(url, data=payload,
                                      headers={"Content-Type": "application/json"})
                with _urllib.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read())
        except Exception as e:
            if not self.silent:
                raise
        return {}

    async def _post(self, path: str, body: dict) -> dict:
        url = f"{self.endpoint}/{path.lstrip('/')}"
        try:
            if _HAS_HTTPX:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.post(url, json=body)
                    return r.json()
            else:
                # Fallback to sync (blocking, but works)
                return self._post_sync(path, body)
        except Exception as e:
            if not self.silent:
                raise
        return {}

    # ── Run ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def run(self, name: str, branch: Optional[str] = None, org_id: Optional[str] = None):
        """
        Open a trace run. All spans created inside belong to this run.

        async with tracer.run("My Agent", branch="alignment") as run:
            async with run.span("step_1", type="node") as s:
                ...
        """
        data = await self._post("run", {
            "name":   name,
            "org_id": org_id or self.org_id,
            "branch": branch,
        })
        run_id = data.get("run_id") or str(uuid.uuid4())
        ctx = _RunCtx(run_id=run_id, tracer=self)
        try:
            yield ctx
            await self._post(f"run/{run_id}", {"status": "completed"})
        except Exception:
            await self._post(f"run/{run_id}", {"status": "failed"})
            raise

    async def open_run(self, name: str, branch: Optional[str] = None) -> str:
        """Open a run and return its run_id. You close it manually with close_run()."""
        data = await self._post("run", {"name": name, "org_id": self.org_id, "branch": branch})
        return data.get("run_id") or str(uuid.uuid4())

    async def close_run(self, run_id: str, status: str = "completed",
                        qf_ratio: Optional[float] = None, alert_level: Optional[str] = None):
        body: dict = {"status": status}
        if qf_ratio is not None:
            body["qf_ratio"] = qf_ratio
        if alert_level:
            body["alert_level"] = alert_level
        await self._post(f"run/{run_id}", body)

    # ── Span helpers ─────────────────────────────────────────────

    async def llm_span(
        self,
        run_id: str,
        name: str,
        input_text: str,
        output_text: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
        parent_span_id: Optional[str] = None,
        branch: Optional[str] = None,
        belief_delta: Optional[float] = None,
        fitness_position: Optional[float] = None,
    ) -> dict:
        """Record a completed LLM call."""
        return await self._post("span", {
            "run_id": run_id, "parent_span_id": parent_span_id,
            "type": "llm", "name": name,
            "input": input_text[:2000], "output": output_text[:2000],
            "tokens_input": tokens_in, "tokens_output": tokens_out,
            "latency_ms": latency_ms, "branch": branch,
            "belief_delta": belief_delta, "fitness_position": fitness_position,
            "org_id": self.org_id, "ts_start": _iso(), "ts_end": _iso(),
        })

    async def tool_span(
        self,
        run_id: str,
        name: str,
        input_data: Any,
        output_data: Any,
        latency_ms: int = 0,
        parent_span_id: Optional[str] = None,
    ) -> dict:
        """Record a tool call."""
        return await self._post("span", {
            "run_id": run_id, "parent_span_id": parent_span_id,
            "type": "tool", "name": name,
            "input":  json.dumps(input_data)  if not isinstance(input_data,  str) else input_data,
            "output": json.dumps(output_data) if not isinstance(output_data, str) else output_data,
            "latency_ms": latency_ms, "org_id": self.org_id,
            "ts_start": _iso(), "ts_end": _iso(),
        })

    # ── Decorator ────────────────────────────────────────────────

    def node(self, branch: Optional[str] = None, span_name: Optional[str] = None):
        """
        Decorator that wraps an async function as a traced node span.
        The decorated function receives _run_id and _parent_id kwargs automatically.

        @tracer.node(branch="alignment")
        async def draft_reply(state, *, _run_id=None, _parent_id=None):
            ...
        """
        def decorator(fn):
            @functools.wraps(fn)
            async def wrapper(*args, _run_id: Optional[str] = None,
                              _parent_id: Optional[str] = None, **kwargs):
                name = span_name or fn.__name__
                t0 = time.perf_counter()
                span_data: dict = {
                    "run_id": _run_id or "untracked",
                    "parent_span_id": _parent_id,
                    "type": "node", "name": name, "branch": branch,
                    "org_id": self.org_id, "ts_start": _iso(),
                    "input": str(args[0])[:500] if args else None,
                }
                try:
                    result = await fn(*args, **kwargs)
                    span_data["output"]     = str(result)[:500] if result is not None else None
                    span_data["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                    span_data["ts_end"]     = _iso()
                    await self._post("span", span_data)
                    return result
                except Exception as e:
                    span_data["error"]      = str(e)
                    span_data["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                    await self._post("span", span_data)
                    raise
            return wrapper
        return decorator


class _RunCtx:
    """Context returned by `async with tracer.run(...)`. Provides span() context manager."""

    def __init__(self, run_id: str, tracer: TesseraTracer):
        self.run_id  = run_id
        self.tracer  = tracer
        self._stack: list[str] = []   # parent span id stack

    @asynccontextmanager
    async def span(
        self,
        name: str,
        type: str = "node",
        parent_id: Optional[str] = None,
        branch: Optional[str] = None,
        **extra,
    ):
        """
        Open a span within this run. Nest these for parent/child relationships.
        extra kwargs (tokens_input, belief_delta, fitness_position, etc.) are forwarded.

        async with run.span("draft", type="llm", tokens_input=200) as s:
            s["input"] = prompt
            reply = await llm(prompt)
            s["output"] = reply
        """
        span_id = str(uuid.uuid4())
        parent  = parent_id or (self._stack[-1] if self._stack else None)
        t0 = time.perf_counter()
        s: dict = {
            "span_id": span_id, "run_id": self.run_id, "parent_span_id": parent,
            "type": type, "name": name, "branch": branch, "org_id": self.tracer.org_id,
            "ts_start": _iso(),
            **extra,
        }
        self._stack.append(span_id)
        try:
            yield s            # caller mutates s["input"], s["output"], etc.
            s["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            s["ts_end"]     = _iso()
            await self.tracer._post("span", s)
        except Exception as e:
            s["error"]      = str(e)
            s["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            await self.tracer._post("span", s)
            raise
        finally:
            if self._stack and self._stack[-1] == span_id:
                self._stack.pop()
