"""
Tessera Workforce Planning  —  port 8024
==========================================
φ-scenario modeling. The decision is not just "how many headcount" but
"hire human vs deploy AI" — and it depends entirely on where the org's φ sits.

Workday does: headcount plans → budget → open reqs.
Tessera adds:  HAV-capacity modeling, φ-scenario comparison,
               "V_net" cost model per role decision, nudge-acceleration tracking.
"""
from __future__ import annotations
import os, uuid, asyncpg
from typing import Optional, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_workforce")
TWIN_URL     = os.getenv("TWIN_URL", "http://twin:8004")
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS headcount_plans (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    period      TEXT NOT NULL,  -- 'Q1-2026' etc
    current_phi FLOAT,
    phi_star    FLOAT,
    status      TEXT DEFAULT 'draft',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS role_decisions (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES headcount_plans(id),
    role_title      TEXT NOT NULL,
    department      TEXT,
    decision_type   TEXT NOT NULL,  -- 'hire_human'|'deploy_ai'|'hybrid'|'defer'
    hav_required    FLOAT,
    npf_required    FLOAT,
    v_net_human     FLOAT,
    v_net_ai        FLOAT,
    recommended     TEXT,
    rationale       TEXT,
    headcount       INT DEFAULT 1,
    status          TEXT DEFAULT 'proposed',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rd_plan ON role_decisions(plan_id);

CREATE TABLE IF NOT EXISTS phi_scenarios (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    scenario_name   TEXT NOT NULL,
    base_phi        FLOAT NOT NULL,
    delta_phi       FLOAT NOT NULL,  -- projected change from hiring/AI deployment
    projected_phi   FLOAT NOT NULL,
    phi_star        FLOAT NOT NULL,
    stays_above_star BOOLEAN,
    nudge_acceleration FLOAT,  -- (1-NPF)*phi per epoch
    hav_capacity_change FLOAT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_phi_scenarios_org ON phi_scenarios(org_id);

CREATE TABLE IF NOT EXISTS capacity_forecasts (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES headcount_plans(id),
    period_month    INT NOT NULL,  -- months from plan start
    projected_hav_capacity FLOAT,
    projected_phi   FLOAT,
    projected_humans INT,
    projected_ai_agents INT,
    vc_count_projected INT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

def _phi_star(K: int = 4, nonprofit: bool = False) -> float:
    base = 0.25 if K >= 6 else (0.32 if K >= 3 else 0.44)
    return round(base * 0.70 if nonprofit else base, 4)

def _v_net_human(fitness: float, v_d: float, c_comp: float, c_gov: float,
                 c_probe: float, edge_count: int) -> float:
    return round(fitness * v_d - c_comp - c_gov - edge_count * c_probe, 4)

def _v_net_ai(deployment_value: float, c_deployment: float,
              c_oversight: float, c_edge: float, edge_count: int) -> float:
    return round(deployment_value - c_deployment - c_oversight - edge_count * c_edge, 4)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
    yield
    await db.close()

app = FastAPI(title="Tessera Workforce Planning", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class PlanRequest(BaseModel):
    org_id: str
    name: str
    period: str
    current_phi: Optional[float] = None
    org_k: int = 4
    nonprofit: bool = False

class RoleDecisionRequest(BaseModel):
    plan_id: str
    role_title: str
    department: Optional[str] = None
    hav_required: float = Field(0.0, ge=0.0, le=1.0)
    npf_required: float = Field(0.0, ge=0.0, le=1.0)
    human_fitness: float = Field(0.7, ge=0.0, le=1.0)
    human_value_delivery: float = Field(100000.0)
    human_comp_cost: float = Field(80000.0)
    human_gov_cost: float = Field(5000.0)
    ai_deployment_value: float = Field(80000.0)
    ai_deployment_cost: float = Field(20000.0)
    ai_oversight_cost: float = Field(15000.0)
    probe_cost: float = Field(500.0)
    edge_count: int = 5
    headcount: int = 1

class PhiScenarioRequest(BaseModel):
    org_id: str
    scenario_name: str
    base_phi: float = Field(..., ge=0.0, le=1.0)
    delta_phi: float
    org_k: int = 4
    nonprofit: bool = False
    mean_npf: float = Field(0.5, ge=0.0, le=1.0)

class CapacityForecastRequest(BaseModel):
    plan_id: str
    periods: int = Field(12, ge=1, le=60)
    current_hav_capacity: float
    current_humans: int
    current_ai_agents: int
    phi_growth_rate: float = Field(0.02)
    hav_growth_rate: float = Field(0.05)


@app.get("/")
def root():
    return {"service": "workforce_planning", "version": "1.0.0", "port": 8024,
            "differentiator": "φ-scenario modeling; hire-human vs deploy-AI; V_net cost model"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "workforce_planning"}


@app.post("/plans", status_code=201)
async def create_plan(body: PlanRequest):
    phi_star_val = _phi_star(body.org_k, body.nonprofit)
    plan_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO headcount_plans (id, org_id, name, period, current_phi, phi_star)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, plan_id, body.org_id, body.name, body.period, body.current_phi, phi_star_val)

    above_star = body.current_phi and body.current_phi > phi_star_val
    return {
        "plan_id": plan_id,
        "name": body.name,
        "period": body.period,
        "phi_star": phi_star_val,
        "current_phi": body.current_phi,
        "is_above_crossover": above_star,
        "planning_note": (
            "Org is above φ*. Standard hire-and-manage models are suboptimal. "
            "Prioritize φ-guardian and Values Custodian roles in headcount planning."
            if above_star else
            "Org is below φ*. AI deployment costs still dominate. "
            "Each standard hire increases φ — watch for crossover approach."
        ),
    }


@app.post("/role-decisions", status_code=201)
async def decide_role(body: RoleDecisionRequest):
    """
    Run V_net comparison: hire human vs deploy AI for this role.
    Recommendation is data-driven from the cost model, not gut feeling.
    """
    v_human = _v_net_human(
        body.human_fitness, body.human_value_delivery,
        body.human_comp_cost, body.human_gov_cost,
        body.probe_cost, body.edge_count
    )
    v_ai = _v_net_ai(
        body.ai_deployment_value, body.ai_deployment_cost,
        body.ai_oversight_cost, body.probe_cost, body.edge_count
    )

    if body.hav_required >= 0.65 or body.npf_required >= 0.60:
        recommended = "hire_human"
        rationale   = (
            f"Role requires HAV≥{body.hav_required:.2f} or NPF≥{body.npf_required:.2f}. "
            "AI cannot generate HAV or NPF. Human required."
        )
    elif v_human > v_ai * 1.1:
        recommended = "hire_human"
        rationale   = f"V_net(human)={v_human:,.0f} exceeds V_net(AI)={v_ai:,.0f} by >10%."
    elif v_ai > v_human * 1.1:
        recommended = "deploy_ai"
        rationale   = f"V_net(AI)={v_ai:,.0f} exceeds V_net(human)={v_human:,.0f} by >10%."
    else:
        recommended = "hybrid"
        rationale   = "V_net values within 10% — hybrid deployment optimizes coverage."

    dec_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO role_decisions
              (id, plan_id, role_title, department, decision_type, hav_required,
               npf_required, v_net_human, v_net_ai, recommended, rationale, headcount)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        """, dec_id, body.plan_id, body.role_title, body.department,
             recommended, body.hav_required, body.npf_required,
             v_human, v_ai, recommended, rationale, body.headcount)

    return {
        "decision_id": dec_id,
        "role": body.role_title,
        "v_net_human": v_human,
        "v_net_ai": v_ai,
        "recommendation": recommended,
        "rationale": rationale,
        "hav_creates": recommended == "hire_human",
        "note": "HAV is only created by humans. Deploy AI for procedural; hire humans for HAV-positive roles.",
    }


@app.post("/phi-scenarios", status_code=201)
async def create_scenario(body: PhiScenarioRequest):
    phi_star_val = _phi_star(body.org_k, body.nonprofit)
    projected  = round(body.base_phi + body.delta_phi, 4)
    stays_above = projected > phi_star_val
    nudge_acc   = round((1 - body.mean_npf) * projected, 6)

    sc_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO phi_scenarios
              (id, org_id, scenario_name, base_phi, delta_phi, projected_phi,
               phi_star, stays_above_star, nudge_acceleration)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, sc_id, body.org_id, body.scenario_name, body.base_phi,
             body.delta_phi, projected, phi_star_val, stays_above, nudge_acc)

    return {
        "scenario_id": sc_id,
        "scenario_name": body.scenario_name,
        "base_phi": body.base_phi,
        "delta_phi": body.delta_phi,
        "projected_phi": projected,
        "phi_star": phi_star_val,
        "stays_above_crossover": stays_above,
        "nudge_acceleration": nudge_acc,
        "interpretation": (
            f"Projected phi={projected:.3f} remains above phi*={phi_star_val:.2f}. "
            f"Nudge acceleration={(nudge_acc):.4f}/epoch — org beliefs converge faster. "
            "Values Custodian retention is critical."
            if stays_above else
            f"Projected phi={projected:.3f} drops below phi*={phi_star_val:.2f}. "
            "Standard management models adequate at this level."
        ),
    }


@app.get("/plans")
async def list_plans(org_id: str = "demo-org"):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM headcount_plans WHERE org_id=$1 ORDER BY created_at DESC LIMIT 20", org_id
        )
    return {"plans": [dict(r) for r in rows], "total": len(rows)}


@app.get("/plans/{plan_id}")
async def get_plan(plan_id: str):
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM headcount_plans WHERE id=$1", plan_id)
    if not row:
        raise HTTPException(404, "Plan not found")
    return dict(row)


@app.get("/phi-scenarios")
async def list_scenarios(org_id: str = "demo-org"):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM phi_scenarios WHERE org_id=$1 ORDER BY created_at DESC LIMIT 30", org_id
        )
    return {"scenarios": [dict(r) for r in rows], "total": len(rows)}


@app.get("/plans/{plan_id}/decisions")
async def plan_decisions(plan_id: str):
    async with db.acquire() as conn:
        plan = await conn.fetchrow("SELECT * FROM headcount_plans WHERE id=$1", plan_id)
        if not plan:
            raise HTTPException(404, "Plan not found")
        decisions = await conn.fetch(
            "SELECT * FROM role_decisions WHERE plan_id=$1 ORDER BY created_at", plan_id
        )
    human_count = sum(1 for d in decisions if d["recommended"] in ("hire_human", "hybrid"))
    ai_count    = sum(1 for d in decisions if d["recommended"] in ("deploy_ai", "hybrid"))
    return {
        "plan": dict(plan),
        "decisions": [dict(d) for d in decisions],
        "summary": {
            "total_roles": len(decisions),
            "hire_human": human_count,
            "deploy_ai": ai_count,
        },
    }
