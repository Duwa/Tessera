"""
Tessera Agent Factory & Lifecycle Registry — port 8011
=======================================================
Two responsibilities:

1. AGENT FACTORY (original)
   POST /select   — mandate → framework recommendation
   POST /generate — mandate → full agent config (nodes, edges, tools)

2. AGENT LIFECYCLE REGISTRY (new)
   POST /agents/onboard          — register a new AI agent (updates φ)
   GET  /agents                  — list all agents for an org
   GET  /agents/{id}             — agent detail + lifecycle events
   GET  /agents/{id}/retirement-preview  — φ impact before retiring
   POST /agents/{id}/retire      — initiate retirement cycle

Used by Market360 and any org managing AI agents alongside humans.
"""
from __future__ import annotations
import os, time, uuid, json, asyncpg
from typing import Optional, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx

# ─── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TRACE_URL         = os.getenv("TRACE_URL",   "http://trace:8010")
PEOPLE_URL        = os.getenv("PEOPLE_URL",  "http://people:8005")
DATABASE_URL      = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_agents")
MODEL             = "claude-sonnet-4-6"

# ─── Tables ───────────────────────────────────────────────────────────────────
CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS agent_registry (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT,
    department       TEXT NOT NULL,
    pipeline         TEXT NOT NULL,
    framework        TEXT DEFAULT 'custom',
    status           TEXT DEFAULT 'active',   -- active | retiring | retired | paused
    phi_contribution FLOAT DEFAULT 0.01,      -- fractional φ increase from this agent
    tasks_automated  TEXT[],                   -- list of task names this agent handles
    daily_runs       INT DEFAULT 0,
    value_per_run    FLOAT DEFAULT 0.0,
    oversight_human  TEXT,                     -- email of assigned VC overseer
    onboarded_by     TEXT,
    onboarded_at     TIMESTAMPTZ DEFAULT NOW(),
    retirement_reason TEXT,
    handoff_plan     TEXT,
    knowledge_captured TEXT,
    retired_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ar_org  ON agent_registry(org_id);
CREATE INDEX IF NOT EXISTS idx_ar_status ON agent_registry(status);

CREATE TABLE IF NOT EXISTS agent_lifecycle_events (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL REFERENCES agent_registry(id),
    event_type TEXT NOT NULL,  -- onboarded | paused | resumed | retiring | retired
    actor      TEXT,
    notes      TEXT,
    phi_before FLOAT,
    phi_after  FLOAT,
    ts         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ale_agent ON agent_lifecycle_events(agent_id);
"""

db: asyncpg.Pool | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
    yield
    await db.close()

app = FastAPI(title="Tessera Agent Factory & Lifecycle", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─── Pydantic models ──────────────────────────────────────────────────────────

class MandateRequest(BaseModel):
    mandate: str
    branch: str = "value"
    context: Optional[str] = None
    framework: Optional[str] = None

class AgentOnboardRequest(BaseModel):
    org_id: str
    name: str
    description: Optional[str] = None
    department: str                          # e.g. "sales", "fulfillment"
    pipeline: str                            # e.g. "lead-scoring", "order-routing"
    framework: str = "custom"
    phi_contribution: float = Field(0.01, ge=0.001, le=0.5)
    tasks_automated: List[str] = []
    daily_runs: int = 0
    value_per_run: float = 0.0
    oversight_human: Optional[str] = None   # VC overseer email
    onboarded_by: Optional[str] = None

class AgentRetireRequest(BaseModel):
    retirement_reason: str
    handoff_plan: str                        # who/what takes over
    knowledge_captured: Optional[str] = None
    retired_by: Optional[str] = None


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"service": "agent-factory", "version": "2.0.0", "port": 8011,
            "capabilities": ["framework-selection", "config-generation", "agent-lifecycle"]}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "agent-factory"}


# ─── AGENT LIFECYCLE REGISTRY ─────────────────────────────────────────────────

@app.post("/agents/onboard", status_code=201)
async def onboard_agent(body: AgentOnboardRequest):
    """
    Register a new AI agent. Updates org φ by adding phi_contribution.
    Records a lifecycle event. Optionally pings the People service to
    register the agent as a capital unit.
    """
    agent_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        # Get current org φ (sum of contributions from active agents)
        rows = await conn.fetch(
            "SELECT phi_contribution FROM agent_registry WHERE org_id=$1 AND status='active'",
            body.org_id
        )
        phi_before = round(sum(r["phi_contribution"] for r in rows), 4)
        phi_after  = round(phi_before + body.phi_contribution, 4)

        await conn.execute("""
            INSERT INTO agent_registry
              (id, org_id, name, description, department, pipeline, framework,
               status, phi_contribution, tasks_automated, daily_runs, value_per_run,
               oversight_human, onboarded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,'active',$8,$9,$10,$11,$12,$13)
        """, agent_id, body.org_id, body.name, body.description,
             body.department, body.pipeline, body.framework,
             body.phi_contribution, body.tasks_automated,
             body.daily_runs, body.value_per_run,
             body.oversight_human, body.onboarded_by)

        await conn.execute("""
            INSERT INTO agent_lifecycle_events (id, agent_id, event_type, actor, notes, phi_before, phi_after)
            VALUES ($1,$2,'onboarded',$3,$4,$5,$6)
        """, str(uuid.uuid4()), agent_id, body.onboarded_by,
             f"Onboarded to {body.department}/{body.pipeline}", phi_before, phi_after)

    # Register as AI unit in People service (best-effort)
    await _register_capital_unit(body.org_id, agent_id, body.name, body.department, body.pipeline)

    return {
        "agent_id": agent_id,
        "name": body.name,
        "status": "active",
        "org_phi_before": phi_before,
        "org_phi_after": phi_after,
        "phi_delta": round(phi_after - phi_before, 4),
        "message": f"Agent '{body.name}' onboarded to {body.department}/{body.pipeline}. φ: {phi_before:.3f} → {phi_after:.3f}",
    }


@app.get("/agents")
async def list_agents(
    org_id: str = Query("demo-org"),
    status: Optional[str] = None,
    department: Optional[str] = None,
):
    """List all agents for an org with current φ summary."""
    async with db.acquire() as conn:
        conditions = ["org_id=$1"]
        params: list = [org_id]
        if status:
            conditions.append(f"status=${len(params)+1}")
            params.append(status)
        if department:
            conditions.append(f"department=${len(params)+1}")
            params.append(department)
        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"SELECT * FROM agent_registry WHERE {where} ORDER BY onboarded_at DESC",
            *params
        )

    agents = [_ser(r) for r in rows]
    active = [a for a in agents if a["status"] == "active"]
    total_phi = round(sum(a["phi_contribution"] for a in active), 4)

    # φ* for K=4
    phi_star = 0.32
    return {
        "agents": agents,
        "total": len(agents),
        "active": len(active),
        "retiring": sum(1 for a in agents if a["status"] == "retiring"),
        "retired": sum(1 for a in agents if a["status"] == "retired"),
        "org_phi_from_agents": total_phi,
        "phi_star": phi_star,
        "above_crossover": total_phi > phi_star,
        "phi_headroom": round(phi_star - total_phi, 4),
    }


@app.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    """Get agent detail + lifecycle events."""
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_registry WHERE id=$1", agent_id)
        if not row:
            raise HTTPException(404, "Agent not found")
        events = await conn.fetch(
            "SELECT * FROM agent_lifecycle_events WHERE agent_id=$1 ORDER BY ts DESC",
            agent_id
        )
    return {
        **_ser(row),
        "lifecycle_events": [_ser(e) for e in events],
    }


@app.get("/agents/{agent_id}/retirement-preview")
async def retirement_preview(agent_id: str):
    """
    Show what happens to org φ if this agent is retired.
    Also surfaces: tasks that need human coverage, VC oversight implications.
    """
    async with db.acquire() as conn:
        agent = await conn.fetchrow("SELECT * FROM agent_registry WHERE id=$1", agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        if agent["status"] == "retired":
            raise HTTPException(400, "Agent already retired")

        active_rows = await conn.fetch(
            "SELECT phi_contribution FROM agent_registry WHERE org_id=$1 AND status='active'",
            agent["org_id"]
        )

    phi_current = round(sum(r["phi_contribution"] for r in active_rows), 4)
    phi_after   = round(phi_current - agent["phi_contribution"], 4)
    phi_star    = 0.32

    tasks = agent["tasks_automated"] or []
    hav_tasks = [t for t in tasks if any(kw in t.lower() for kw in ["judgment","review","approval","oversight","strategy","planning","escalation"])]

    return {
        "agent_id": agent_id,
        "agent_name": agent["name"],
        "department": agent["department"],
        "pipeline": agent["pipeline"],
        "phi_current": phi_current,
        "phi_after_retirement": phi_after,
        "phi_delta": round(phi_after - phi_current, 4),
        "phi_star": phi_star,
        "currently_above_crossover": phi_current > phi_star,
        "will_be_above_crossover": phi_after > phi_star,
        "governance_improvement": phi_current > phi_star and phi_after <= phi_star,
        "tasks_requiring_coverage": tasks,
        "tasks_requiring_human_judgment": hav_tasks,
        "hire_human_recommended": len(hav_tasks) > 0,
        "daily_runs_affected": agent["daily_runs"],
        "estimated_value_impact": round(agent["daily_runs"] * agent["value_per_run"] * 30, 2),
        "warning": (
            f"Retiring this agent will drop φ by {abs(agent['phi_contribution']):.3f}. "
            + (f"{len(hav_tasks)} tasks require human judgment — hire before retiring." if hav_tasks else
               "All tasks are procedural — safe to hand off to another agent.")
        ),
    }


@app.post("/agents/{agent_id}/retire")
async def retire_agent(agent_id: str, body: AgentRetireRequest):
    """
    Retire an agent. Captures knowledge, records handoff plan, updates φ.
    Retirement is final — use /pause for temporary suspension.
    """
    async with db.acquire() as conn:
        agent = await conn.fetchrow("SELECT * FROM agent_registry WHERE id=$1", agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        if agent["status"] == "retired":
            raise HTTPException(400, "Agent is already retired")

        active_rows = await conn.fetch(
            "SELECT phi_contribution FROM agent_registry WHERE org_id=$1 AND status='active'",
            agent["org_id"]
        )
        phi_before = round(sum(r["phi_contribution"] for r in active_rows), 4)
        phi_after  = round(phi_before - agent["phi_contribution"], 4)

        await conn.execute("""
            UPDATE agent_registry
            SET status='retired', retirement_reason=$2, handoff_plan=$3,
                knowledge_captured=$4, retired_at=NOW()
            WHERE id=$1
        """, agent_id, body.retirement_reason, body.handoff_plan, body.knowledge_captured)

        await conn.execute("""
            INSERT INTO agent_lifecycle_events (id, agent_id, event_type, actor, notes, phi_before, phi_after)
            VALUES ($1,$2,'retired',$3,$4,$5,$6)
        """, str(uuid.uuid4()), agent_id, body.retired_by,
             f"Retired: {body.retirement_reason[:200]}", phi_before, phi_after)

    return {
        "agent_id": agent_id,
        "agent_name": agent["name"],
        "status": "retired",
        "org_phi_before": phi_before,
        "org_phi_after": phi_after,
        "phi_delta": round(phi_after - phi_before, 4),
        "governance_improvement": phi_before > 0.32 and phi_after <= 0.32,
        "message": f"Agent '{agent['name']}' retired. φ: {phi_before:.3f} → {phi_after:.3f}. Handoff: {body.handoff_plan[:100]}",
    }


@app.patch("/agents/{agent_id}/pause")
async def pause_agent(agent_id: str, actor: str = "system"):
    """Temporarily pause an agent (keeps φ contribution; use retire for permanent removal)."""
    async with db.acquire() as conn:
        r = await conn.fetchrow("SELECT status FROM agent_registry WHERE id=$1", agent_id)
        if not r: raise HTTPException(404, "Agent not found")
        if r["status"] != "active": raise HTTPException(400, f"Cannot pause agent in status '{r['status']}'")
        await conn.execute("UPDATE agent_registry SET status='paused' WHERE id=$1", agent_id)
        await conn.execute("""
            INSERT INTO agent_lifecycle_events (id, agent_id, event_type, actor) VALUES ($1,$2,'paused',$3)
        """, str(uuid.uuid4()), agent_id, actor)
    return {"agent_id": agent_id, "status": "paused"}


@app.patch("/agents/{agent_id}/resume")
async def resume_agent(agent_id: str, actor: str = "system"):
    """Resume a paused agent."""
    async with db.acquire() as conn:
        r = await conn.fetchrow("SELECT status FROM agent_registry WHERE id=$1", agent_id)
        if not r: raise HTTPException(404, "Agent not found")
        if r["status"] != "paused": raise HTTPException(400, f"Agent is not paused (current: {r['status']})")
        await conn.execute("UPDATE agent_registry SET status='active' WHERE id=$1", agent_id)
        await conn.execute("""
            INSERT INTO agent_lifecycle_events (id, agent_id, event_type, actor) VALUES ($1,$2,'resumed',$3)
        """, str(uuid.uuid4()), agent_id, actor)
    return {"agent_id": agent_id, "status": "active"}


# ─── AGENT FACTORY (original endpoints) ───────────────────────────────────────

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


@app.post("/select")
async def select_framework(req: MandateRequest):
    _require_key()
    prompt = SELECT_PROMPT.format(
        mandate=req.mandate, branch=req.branch,
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
    _require_key()
    framework = req.framework or "custom"
    prompt = GENERATE_PROMPT.format(
        framework=framework, mandate=req.mandate,
        branch=req.branch, context=req.context or "None provided",
    )
    t0 = time.perf_counter()
    raw = await _claude(prompt, max_tokens=2500)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    result = _parse_json(raw["content"][0]["text"])
    await _trace("Agent Factory — Generate", "generate_config", req.branch,
                 raw.get("usage", {}), latency_ms, req.mandate[:300], result)
    return result


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ser(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, 'isoformat'):
            d[k] = v.isoformat()
    return d


def _require_key():
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY not set. Add it to your .env file."
        )


async def _register_capital_unit(org_id: str, agent_id: str, name: str, dept: str, pipeline: str):
    """Register the agent as an AI capital unit in the People service (best-effort)."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            await c.post(f"{PEOPLE_URL}/units", json={
                "unit_id": agent_id,
                "unit_type": "ai",
                "name": name,
                "role": f"Automation Agent — {pipeline}",
                "department": dept,
                "org_id": org_id,
                "hav_score": 0.0,
            })
    except Exception:
        pass


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
