"""
Tessera Succession Planning  —  port 8021
==========================================
HAV-indexed succession. Protects high-NPF humans from elimination.

Workday does: identify successors → readiness rating → development plan.
Tessera adds:  HAV-indexed succession pools; Values Custodian flags on critical
               roles; "elimination risk" score when φ > φ* and NPF is high.
"""
from __future__ import annotations
import os, uuid, asyncpg
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_succession")
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS critical_roles (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    title           TEXT NOT NULL,
    department      TEXT,
    phi_role_type   TEXT DEFAULT 'standard',
    current_holder  TEXT,
    min_hav_req     FLOAT DEFAULT 0.0,
    min_npf_req     FLOAT DEFAULT 0.0,
    is_values_custodian_role BOOLEAN DEFAULT FALSE,
    elimination_risk FLOAT DEFAULT 0.0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS succession_plans (
    id              TEXT PRIMARY KEY,
    role_id         TEXT NOT NULL REFERENCES critical_roles(id),
    employee_id     TEXT NOT NULL,
    readiness       TEXT DEFAULT 'developing',  -- 'ready_now'|'ready_1yr'|'ready_3yr'|'developing'
    mean_hav        FLOAT,
    mean_npf        FLOAT,
    hav_gap         FLOAT,
    development_focus TEXT,
    is_vc_protected BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plans_role ON succession_plans(role_id);
CREATE INDEX IF NOT EXISTS idx_plans_employee ON succession_plans(employee_id);

CREATE TABLE IF NOT EXISTS development_actions (
    id          TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL REFERENCES succession_plans(id),
    action_type TEXT NOT NULL,  -- 'npf_stretch'|'srq_training'|'oc_project'|'phi_guardian_rotation'
    description TEXT NOT NULL,
    target_metric TEXT,
    target_value FLOAT,
    due_date    DATE,
    status      TEXT DEFAULT 'pending',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
    yield
    await db.close()

app = FastAPI(title="Tessera Succession", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _phi_star(K: int = 4) -> float:
    return 0.25 if K >= 6 else (0.32 if K >= 3 else 0.44)

def _elimination_risk(mean_hav: float, mean_npf: float, phi: float, phi_star_val: float) -> float:
    """
    High-HAV, high-NPF humans above φ* face systematic elimination in
    man-hours-governed orgs. This score quantifies that risk.
    """
    if mean_hav < 0.6 or mean_npf < 0.5:
        return 0.0
    phi_excess = max(0.0, phi - phi_star_val)
    return round(min(1.0, mean_hav * mean_npf * phi_excess * 3.0), 4)


class CriticalRoleRequest(BaseModel):
    org_id: str
    title: str
    department: Optional[str] = None
    phi_role_type: str = "standard"
    current_holder: Optional[str] = None
    min_hav_req: float = Field(0.0, ge=0.0, le=1.0)
    min_npf_req: float = Field(0.0, ge=0.0, le=1.0)
    phi: Optional[float] = None
    org_k: int = 4

class SuccessionPlanRequest(BaseModel):
    role_id: str
    employee_id: str
    readiness: str = "developing"
    mean_hav: float = Field(..., ge=0.0, le=1.0)
    mean_npf: float = Field(..., ge=0.0, le=1.0)
    development_focus: Optional[str] = None

class DevActionRequest(BaseModel):
    plan_id: str
    action_type: str
    description: str
    target_metric: Optional[str] = None
    target_value: Optional[float] = None
    due_date: Optional[str] = None


@app.get("/")
def root():
    return {"service": "succession", "version": "1.0.0", "port": 8021,
            "differentiator": "HAV-indexed succession; Values Custodian protection; elimination risk scoring"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "succession"}


@app.post("/roles", status_code=201)
async def create_role(body: CriticalRoleRequest):
    phi_star_val = _phi_star(body.org_k)
    is_vc_role = body.phi_role_type in ("values_custodian", "phi_guardian")
    elim_risk = 0.0
    if body.phi:
        elim_risk = _elimination_risk(body.min_hav_req, body.min_npf_req, body.phi, phi_star_val)

    role_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO critical_roles
              (id, org_id, title, department, phi_role_type, current_holder,
               min_hav_req, min_npf_req, is_values_custodian_role, elimination_risk)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """, role_id, body.org_id, body.title, body.department, body.phi_role_type,
             body.current_holder, body.min_hav_req, body.min_npf_req, is_vc_role, elim_risk)

    return {
        "role_id": role_id,
        "title": body.title,
        "phi_role_type": body.phi_role_type,
        "is_values_custodian_role": is_vc_role,
        "elimination_risk": elim_risk,
        "phi_star": phi_star_val,
        "warning": (
            f"RISK: This role shows elimination_risk={elim_risk:.2f}. "
            "Man-hours governance above φ* tends to eliminate high-HAV humans. "
            "Succession coverage is critical before any restructuring."
            if elim_risk > 0.3 else None
        ),
    }


@app.get("/roles")
async def list_roles(org_id: Optional[str] = Query(None)):
    async with db.acquire() as conn:
        if org_id:
            rows = await conn.fetch(
                "SELECT * FROM critical_roles WHERE org_id=$1 ORDER BY elimination_risk DESC", org_id
            )
        else:
            rows = await conn.fetch("SELECT * FROM critical_roles ORDER BY elimination_risk DESC LIMIT 100")
    return {"roles": [dict(r) for r in rows]}


@app.post("/plans", status_code=201)
async def create_plan(body: SuccessionPlanRequest):
    async with db.acquire() as conn:
        role = await conn.fetchrow("SELECT * FROM critical_roles WHERE id=$1", body.role_id)
        if not role:
            raise HTTPException(404, "Critical role not found")

        hav_gap  = max(0.0, role["min_hav_req"] - body.mean_hav)
        npf_gap  = max(0.0, role["min_npf_req"] - body.mean_npf)
        is_vc    = bool(role["is_values_custodian_role"] and body.mean_hav >= 0.65)
        readiness = body.readiness
        if hav_gap > 0.20 or npf_gap > 0.20:
            readiness = "developing"

        plan_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO succession_plans
              (id, role_id, employee_id, readiness, mean_hav, mean_npf,
               hav_gap, development_focus, is_vc_protected)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, plan_id, body.role_id, body.employee_id, readiness,
             body.mean_hav, body.mean_npf, hav_gap, body.development_focus, is_vc)

    return {
        "plan_id": plan_id,
        "employee_id": body.employee_id,
        "role": role["title"],
        "readiness": readiness,
        "hav_gap": round(hav_gap, 4),
        "npf_gap": round(npf_gap, 4),
        "is_vc_protected": is_vc,
        "development_focus": body.development_focus or (
            "Build NPF through unstructured exploration projects" if npf_gap > 0.1 else
            "Build SRQ through AI oversight rotations" if hav_gap > 0.1 else
            "Ready for role"
        ),
    }


@app.get("/roles/{role_id}/successors")
async def list_successors(role_id: str):
    async with db.acquire() as conn:
        role = await conn.fetchrow("SELECT * FROM critical_roles WHERE id=$1", role_id)
        if not role:
            raise HTTPException(404, "Role not found")
        plans = await conn.fetch(
            "SELECT * FROM succession_plans WHERE role_id=$1 ORDER BY mean_hav DESC", role_id
        )
    return {
        "role": dict(role),
        "successors": [dict(p) for p in plans],
        "coverage_status": (
            "covered" if any(p["readiness"] == "ready_now" for p in plans) else
            "partial" if plans else "uncovered"
        ),
        "vc_protected_count": sum(1 for p in plans if p["is_vc_protected"]),
    }


@app.post("/development-actions", status_code=201)
async def add_action(body: DevActionRequest):
    async with db.acquire() as conn:
        plan = await conn.fetchrow("SELECT id FROM succession_plans WHERE id=$1", body.plan_id)
        if not plan:
            raise HTTPException(404, "Succession plan not found")
        action_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO development_actions
              (id, plan_id, action_type, description, target_metric, target_value, due_date)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, action_id, body.plan_id, body.action_type, body.description,
             body.target_metric, body.target_value,
             body.due_date if body.due_date else None)

    hav_impact = {
        "npf_stretch": "Increases NPF fraction — directly raises HAV weight 0.50",
        "srq_training": "Improves SRQ — raises HAV weight 0.30",
        "oc_project": "Generates OC events — raises HAV weight 0.20",
        "phi_guardian_rotation": "Rotation into φ-guardian shifts; all three HAV components activated",
    }
    return {
        "action_id": action_id,
        "action_type": body.action_type,
        "hav_impact": hav_impact.get(body.action_type, "General development"),
    }
