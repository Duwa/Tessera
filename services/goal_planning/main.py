"""
Goal-Plan-Account (GPA) Service
================================
Manages the three-tier hierarchy for agentic automation projects:
  Goal        → measurable business objective (strategic / dept / pipeline)
  Plan Step   → ordered sequence of agent or human tasks to achieve the goal
  Accountability → who owns the goal (VC or standard human + HAV score)

Port 8029
"""

import os
import uuid
import asyncpg
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tessera.goal_planning")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_goals")

pool: asyncpg.Pool = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    department  TEXT,
    level       TEXT NOT NULL DEFAULT 'department',
    title       TEXT NOT NULL,
    description TEXT,
    kpi_metric  TEXT,
    target_value FLOAT,
    current_value FLOAT DEFAULT 0,
    unit        TEXT,
    target_date TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    owner_email TEXT,
    phi_impact  FLOAT DEFAULT 0,
    parent_goal_id TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plan_steps (
    id                      TEXT PRIMARY KEY,
    goal_id                 TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    step_order              INT NOT NULL,
    title                   TEXT NOT NULL,
    description             TEXT,
    executor_type           TEXT NOT NULL DEFAULT 'human',
    agent_id                TEXT,
    agent_name              TEXT,
    agent_framework         TEXT,
    human_name              TEXT,
    human_email             TEXT,
    estimated_runs_per_day  INT DEFAULT 0,
    estimated_value_monthly FLOAT DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'pending',
    azure_deployed          BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS accountability (
    id          TEXT PRIMARY KEY,
    goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    human_name  TEXT NOT NULL,
    human_email TEXT,
    is_vc       BOOLEAN DEFAULT FALSE,
    hav_score   FLOAT,
    role        TEXT NOT NULL DEFAULT 'owner',
    phi_role    TEXT DEFAULT 'standard',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLES)
    logger.info("Goal Planning DB ready")
    yield
    await pool.close()


app = FastAPI(title="Tessera Goal Planning", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── SCHEMAS ────────────────────────────────────────────────────────────────

class GoalCreate(BaseModel):
    org_id: str
    department: Optional[str] = None
    level: str = "department"          # strategic | department | pipeline
    title: str
    description: Optional[str] = None
    kpi_metric: Optional[str] = None
    target_value: Optional[float] = None
    unit: Optional[str] = None
    target_date: Optional[str] = None
    status: str = "active"
    owner_email: Optional[str] = None
    phi_impact: float = 0.0
    parent_goal_id: Optional[str] = None

class PlanStepCreate(BaseModel):
    step_order: int
    title: str
    description: Optional[str] = None
    executor_type: str = "agent"       # agent | human | hybrid
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_framework: Optional[str] = None
    human_name: Optional[str] = None
    human_email: Optional[str] = None
    estimated_runs_per_day: int = 0
    estimated_value_monthly: float = 0.0
    status: str = "active"
    azure_deployed: bool = False

class AccountCreate(BaseModel):
    human_name: str
    human_email: Optional[str] = None
    is_vc: bool = False
    hav_score: Optional[float] = None
    role: str = "owner"               # owner | sponsor | reviewer
    phi_role: str = "standard"        # phi_guardian | standard

class ProgressUpdate(BaseModel):
    current_value: float
    status: Optional[str] = None


# ── HELPERS ────────────────────────────────────────────────────────────────

def _row(r) -> dict:
    return dict(r) if r else None

async def _goal_full(conn, goal_id: str) -> dict:
    g = await conn.fetchrow("SELECT * FROM goals WHERE id=$1", goal_id)
    if not g:
        return None
    goal = _row(g)

    steps = await conn.fetch(
        "SELECT * FROM plan_steps WHERE goal_id=$1 ORDER BY step_order", goal_id
    )
    goal["plan_steps"] = [_row(s) for s in steps]

    accs = await conn.fetch(
        "SELECT * FROM accountability WHERE goal_id=$1 ORDER BY created_at", goal_id
    )
    goal["accountability"] = [_row(a) for a in accs]

    children = await conn.fetch(
        "SELECT id FROM goals WHERE parent_goal_id=$1 ORDER BY created_at", goal_id
    )
    goal["child_goal_ids"] = [r["id"] for r in children]

    # Derived metrics
    total_monthly = sum(
        (s.get("estimated_value_monthly") or 0) for s in goal["plan_steps"]
    )
    agent_steps = [s for s in goal["plan_steps"] if s["executor_type"] == "agent"]
    goal["total_value_monthly"] = total_monthly
    goal["agent_count"] = len(agent_steps)
    goal["total_runs_per_day"] = sum((s.get("estimated_runs_per_day") or 0) for s in agent_steps)

    return goal


# ── HEALTH ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "goal-planning"}


# ── GOALS ─────────────────────────────────────────────────────────────────

@app.post("/goals", status_code=201)
async def create_goal(body: GoalCreate):
    gid = f"goal-{uuid.uuid4().hex[:12]}"
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO goals
              (id, org_id, department, level, title, description, kpi_metric,
               target_value, unit, target_date, status, owner_email, phi_impact, parent_goal_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        """, gid, body.org_id, body.department, body.level, body.title,
             body.description, body.kpi_metric, body.target_value, body.unit,
             body.target_date, body.status, body.owner_email, body.phi_impact,
             body.parent_goal_id)
        return {"goal_id": gid}


@app.get("/goals")
async def list_goals(org_id: str, level: Optional[str] = None, department: Optional[str] = None):
    async with pool.acquire() as conn:
        if level:
            rows = await conn.fetch(
                "SELECT * FROM goals WHERE org_id=$1 AND level=$2 ORDER BY created_at", org_id, level
            )
        elif department:
            rows = await conn.fetch(
                "SELECT * FROM goals WHERE org_id=$1 AND department=$2 ORDER BY created_at", org_id, department
            )
        else:
            rows = await conn.fetch("SELECT * FROM goals WHERE org_id=$1 ORDER BY created_at", org_id)
        goals = []
        for r in rows:
            g = await _goal_full(conn, r["id"])
            goals.append(g)
        return {"goals": goals, "total": len(goals)}


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: str):
    async with pool.acquire() as conn:
        g = await _goal_full(conn, goal_id)
        if not g:
            raise HTTPException(404, "Goal not found")
        return g


@app.put("/goals/{goal_id}/progress")
async def update_progress(goal_id: str, body: ProgressUpdate):
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM goals WHERE id=$1", goal_id)
        if not exists:
            raise HTTPException(404, "Goal not found")
        if body.status:
            await conn.execute(
                "UPDATE goals SET current_value=$1, status=$2 WHERE id=$3",
                body.current_value, body.status, goal_id
            )
        else:
            await conn.execute(
                "UPDATE goals SET current_value=$1 WHERE id=$2",
                body.current_value, goal_id
            )
        return {"updated": True}


# ── PLAN STEPS ────────────────────────────────────────────────────────────

@app.post("/goals/{goal_id}/steps", status_code=201)
async def add_step(goal_id: str, body: PlanStepCreate):
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM goals WHERE id=$1", goal_id)
        if not exists:
            raise HTTPException(404, "Goal not found")
        sid = f"step-{uuid.uuid4().hex[:12]}"
        await conn.execute("""
            INSERT INTO plan_steps
              (id, goal_id, step_order, title, description, executor_type,
               agent_id, agent_name, agent_framework, human_name, human_email,
               estimated_runs_per_day, estimated_value_monthly, status, azure_deployed)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        """, sid, goal_id, body.step_order, body.title, body.description,
             body.executor_type, body.agent_id, body.agent_name, body.agent_framework,
             body.human_name, body.human_email, body.estimated_runs_per_day,
             body.estimated_value_monthly, body.status, body.azure_deployed)
        return {"step_id": sid}


# ── ACCOUNTABILITY ────────────────────────────────────────────────────────

@app.post("/goals/{goal_id}/accountability", status_code=201)
async def add_accountability(goal_id: str, body: AccountCreate):
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM goals WHERE id=$1", goal_id)
        if not exists:
            raise HTTPException(404, "Goal not found")
        aid = f"acct-{uuid.uuid4().hex[:12]}"
        await conn.execute("""
            INSERT INTO accountability
              (id, goal_id, human_name, human_email, is_vc, hav_score, role, phi_role)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """, aid, goal_id, body.human_name, body.human_email, body.is_vc,
             body.hav_score, body.role, body.phi_role)
        return {"accountability_id": aid}


# ── GPA TREE ─────────────────────────────────────────────────────────────

@app.get("/orgs/{org_id}/gpa")
async def gpa_tree(org_id: str):
    """
    Returns the full Goal-Plan-Account tree for an org.
    Structure: strategic goals → dept goals (as children) → plan steps + accountability
    """
    async with pool.acquire() as conn:
        strategics = await conn.fetch(
            "SELECT * FROM goals WHERE org_id=$1 AND level='strategic' ORDER BY created_at", org_id
        )
        depts = await conn.fetch(
            "SELECT * FROM goals WHERE org_id=$1 AND level='department' ORDER BY created_at", org_id
        )

        # Build dept goals with their full trees
        dept_goals = []
        for d in depts:
            g = await _goal_full(conn, d["id"])
            dept_goals.append(g)

        # Build strategic goals with their full trees and attach dept children
        strategic_goals = []
        for s in strategics:
            sg = await _goal_full(conn, s["id"])
            # Attach dept goals that are children of this strategic goal
            sg["dept_goals"] = [dg for dg in dept_goals if dg.get("parent_goal_id") == s["id"]]
            strategic_goals.append(sg)

        # Dept goals not attached to any strategic goal (standalone)
        strategic_ids = {s["id"] for s in strategics}
        attached_ids = {dg["id"] for dg in dept_goals if dg.get("parent_goal_id") in strategic_ids}
        standalone_depts = [dg for dg in dept_goals if dg["id"] not in attached_ids]

        # Aggregate metrics
        all_goals = [g for sg in strategic_goals for g in sg["dept_goals"]] + standalone_depts
        total_agents = sum(g["agent_count"] for g in all_goals)
        total_value = sum(g["total_value_monthly"] for g in all_goals)
        total_runs = sum(g["total_runs_per_day"] for g in all_goals)

        return {
            "org_id": org_id,
            "strategic_goals": strategic_goals,
            "standalone_dept_goals": standalone_depts,
            "summary": {
                "total_goals": len(strategics) + len(depts),
                "total_agent_steps": total_agents,
                "total_value_monthly": round(total_value, 2),
                "total_runs_per_day": total_runs,
            },
        }


# ── SEED MARKET360 ───────────────────────────────────────────────────────

@app.post("/seed/market360", status_code=201)
async def seed_market360():
    """Seed the Market360 Goal-Plan-Account example for all 4 departments."""
    seeded = []

    async with pool.acquire() as conn:
        # Wipe existing market360 data
        existing = await conn.fetch("SELECT id FROM goals WHERE org_id='market360'")
        for r in existing:
            await conn.execute("DELETE FROM goals WHERE id=$1", r["id"])

    # 1. Strategic goal
    sg_body = GoalCreate(
        org_id="market360",
        department=None,
        level="strategic",
        title="Automate Market360 operations to free human capacity for high-HAV work",
        description="Deploy AI agents across all 4 departments to automate repetitive pipeline work. "
                    "Target: 80% automation coverage, φ ≤ 0.32, monthly AI value > $50K.",
        kpi_metric="automation_coverage_pct",
        target_value=80.0,
        current_value=35.0,
        unit="%",
        target_date="2026-12-31",
        status="active",
        owner_email="james.okonkwo@demo.com",
        phi_impact=0.119,
    )
    sg_resp = await create_goal(sg_body)
    sg_id = sg_resp["goal_id"]
    seeded.append(("strategic", sg_id))

    # Strategic accountability
    for acct in [
        AccountCreate(human_name="James Okonkwo", human_email="james.okonkwo@demo.com",
                      is_vc=False, hav_score=0.75, role="owner", phi_role="standard"),
        AccountCreate(human_name="Maya Chen", human_email="maya.chen@demo.com",
                      is_vc=True, hav_score=0.82, role="sponsor", phi_role="phi_guardian"),
    ]:
        await add_accountability(sg_id, acct)

    # 2. Department goals with plan steps and accountability
    dept_configs = [
        {
            "department": "customers",
            "title": "Reduce manual request triage by 85% and improve churn retention by 15%",
            "description": "CustomerIQ-01 handles classification & routing at scale. "
                           "ChurnGuard-01 predicts at-risk customers and triggers retention playbooks.",
            "kpi_metric": "triage_automation_pct",
            "target_value": 85.0, "current_value": 52.0, "unit": "%",
            "owner_email": "maya.chen@demo.com",
            "phi_impact": 0.027,
            "target_date": "2026-09-30",
            "steps": [
                dict(step_order=1, title="CustomerIQ-01 — classify & route inbound requests",
                     description="LangGraph state machine: classify intent → route to team → log to CRM",
                     executor_type="agent", agent_name="CustomerIQ-01", agent_framework="langgraph",
                     estimated_runs_per_day=420, estimated_value_monthly=56700, status="active", azure_deployed=True),
                dict(step_order=2, title="ChurnGuard-01 — predict churn risk and trigger retention",
                     description="Custom Anthropic: score churn risk → generate offer → push to CRM",
                     executor_type="agent", agent_name="ChurnGuard-01", agent_framework="custom",
                     estimated_runs_per_day=180, estimated_value_monthly=64800, status="active", azure_deployed=True),
                dict(step_order=3, title="Maya Chen — VC oversight and HAV escalations",
                     description="Human review of flagged high-value customer interventions. HAV-required.",
                     executor_type="human", human_name="Maya Chen", human_email="maya.chen@demo.com",
                     estimated_runs_per_day=8, estimated_value_monthly=0, status="active"),
            ],
            "accountability": [
                AccountCreate(human_name="Maya Chen", human_email="maya.chen@demo.com",
                              is_vc=True, hav_score=0.82, role="owner", phi_role="phi_guardian"),
            ],
        },
        {
            "department": "sales",
            "title": "Increase lead-to-opportunity conversion by 20% and cut proposal time by 60%",
            "description": "LeadScorer-01 enriches and scores leads automatically. "
                           "ProposalBot-01 generates first-draft proposals in under 10 minutes.",
            "kpi_metric": "lead_conversion_increase_pct",
            "target_value": 20.0, "current_value": 8.0, "unit": "%",
            "owner_email": "sofia.reyes@demo.com",
            "phi_impact": 0.032,
            "target_date": "2026-09-30",
            "steps": [
                dict(step_order=1, title="LeadScorer-01 — score and enrich inbound leads",
                     description="Custom: score lead → enrich from LinkedIn → assign rep → log activity",
                     executor_type="agent", agent_name="LeadScorer-01", agent_framework="custom",
                     estimated_runs_per_day=300, estimated_value_monthly=72000, status="active", azure_deployed=True),
                dict(step_order=2, title="ProposalBot-01 — generate first-draft sales proposals",
                     description="CrewAI crew: context extractor + proposal writer + pricing agent",
                     executor_type="agent", agent_name="ProposalBot-01", agent_framework="crewai",
                     estimated_runs_per_day=45, estimated_value_monthly=47250, status="active", azure_deployed=False),
                dict(step_order=3, title="Sofia Reyes — review high-value proposals (>$50K)",
                     description="Human approval gate for enterprise-tier proposals. HAV judgment required.",
                     executor_type="human", human_name="Sofia Reyes", human_email="sofia.reyes@demo.com",
                     estimated_runs_per_day=5, estimated_value_monthly=0, status="active"),
            ],
            "accountability": [
                AccountCreate(human_name="Sofia Reyes", human_email="sofia.reyes@demo.com",
                              is_vc=False, hav_score=0.69, role="owner", phi_role="standard"),
                AccountCreate(human_name="Maya Chen", human_email="maya.chen@demo.com",
                              is_vc=True, hav_score=0.82, role="reviewer", phi_role="phi_guardian"),
            ],
        },
        {
            "department": "planning",
            "title": "Achieve forecast accuracy ±5% and detect budget variance within 24 hours",
            "description": "DemandPlanner-01 runs daily demand forecasts. "
                           "BudgetAlert-01 monitors spend every 15 minutes and fires alerts on >5% variance.",
            "kpi_metric": "forecast_accuracy_pct",
            "target_value": 95.0, "current_value": 88.0, "unit": "%",
            "owner_email": "priya.sharma@demo.com",
            "phi_impact": 0.028,
            "target_date": "2026-08-31",
            "steps": [
                dict(step_order=1, title="DemandPlanner-01 — aggregate signals and run forecast",
                     description="AutoGen: UserProxy→AssistantAgent, aggregates CRM+external feeds, outputs forecast report",
                     executor_type="agent", agent_name="DemandPlanner-01", agent_framework="autogen",
                     estimated_runs_per_day=12, estimated_value_monthly=30600, status="active", azure_deployed=False),
                dict(step_order=2, title="BudgetAlert-01 — monitor spend vs plan",
                     description="Custom: check spend vs plan → compute variance → trigger alert if >5%",
                     executor_type="agent", agent_name="BudgetAlert-01", agent_framework="custom",
                     estimated_runs_per_day=96, estimated_value_monthly=8640, status="active", azure_deployed=False),
                dict(step_order=3, title="Priya Sharma — VC sign-off on strategic forecast changes",
                     description="Values Custodian review for forecast adjustments that affect headcount decisions.",
                     executor_type="human", human_name="Priya Sharma", human_email="priya.sharma@demo.com",
                     estimated_runs_per_day=2, estimated_value_monthly=0, status="active"),
            ],
            "accountability": [
                AccountCreate(human_name="Priya Sharma", human_email="priya.sharma@demo.com",
                              is_vc=True, hav_score=0.78, role="owner", phi_role="phi_guardian"),
            ],
        },
        {
            "department": "fulfillment",
            "title": "Achieve order routing time <30 seconds and proactive delay alerts within 1 hour",
            "description": "OrderRouter-01 routes every order to the optimal warehouse. "
                           "DeliveryChaser-01 monitors carrier APIs and drafts delay notifications.",
            "kpi_metric": "routing_time_seconds",
            "target_value": 30.0, "current_value": 47.0, "unit": "s",
            "owner_email": "alex.mercer@demo.com",
            "phi_impact": 0.032,
            "target_date": "2026-07-31",
            "steps": [
                dict(step_order=1, title="OrderRouter-01 — select optimal warehouse and create pick ticket",
                     description="LangGraph: check stock levels → select warehouse → create pick ticket → update ERP",
                     executor_type="agent", agent_name="OrderRouter-01", agent_framework="langgraph",
                     estimated_runs_per_day=650, estimated_value_monthly=42900, status="active", azure_deployed=True),
                dict(step_order=2, title="DeliveryChaser-01 — monitor carrier and notify on delay",
                     description="Custom: poll carrier API → detect delay >1h → draft customer notification → log event",
                     executor_type="agent", agent_name="DeliveryChaser-01", agent_framework="custom",
                     estimated_runs_per_day=240, estimated_value_monthly=12960, status="active", azure_deployed=True),
                dict(step_order=3, title="Alex Mercer — exception handling for lost / damaged orders",
                     description="Human escalation path for insurance claims and partner disputes.",
                     executor_type="human", human_name="Alex Mercer", human_email="alex.mercer@demo.com",
                     estimated_runs_per_day=4, estimated_value_monthly=0, status="active"),
            ],
            "accountability": [
                AccountCreate(human_name="Alex Mercer", human_email="alex.mercer@demo.com",
                              is_vc=False, hav_score=0.71, role="owner", phi_role="standard"),
                AccountCreate(human_name="Priya Sharma", human_email="priya.sharma@demo.com",
                              is_vc=True, hav_score=0.78, role="sponsor", phi_role="phi_guardian"),
            ],
        },
    ]

    for cfg in dept_configs:
        steps = cfg.pop("steps")
        accts = cfg.pop("accountability")
        dept_body = GoalCreate(org_id="market360", level="department", parent_goal_id=sg_id, **cfg)
        dg_resp = await create_goal(dept_body)
        dg_id = dg_resp["goal_id"]
        seeded.append(("dept", dg_id))

        for s in steps:
            await add_step(dg_id, PlanStepCreate(**s))
        for a in accts:
            await add_accountability(dg_id, a)

    return {
        "seeded": len(seeded),
        "strategic_goal_id": sg_id,
        "dept_goals": [{"level": lv, "goal_id": gid} for lv, gid in seeded[1:]],
    }
