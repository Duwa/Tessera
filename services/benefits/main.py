"""
Tessera Benefits Administration  —  port 8018
==============================================
HAV-native benefits. Not just "enrolled in a plan" — benefits allocation
reflects human alignment value. Values Custodians (high HAV, high NPF) are
flagged for retention alerts before man-hours governance eliminates them.

Workday does: enroll → pay premium → process claims.
Tessera adds:  HAV-weighted flex allocation, Values Custodian protection,
               and automatic retention alerts when high-HAV humans are at risk.
"""
from __future__ import annotations
import os, uuid, asyncpg
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_benefits")

db: asyncpg.Pool | None = None

FLEX_BUDGET_BASE   = float(os.getenv("FLEX_BUDGET_BASE",   "2000"))  # USD/yr base flex
FLEX_BUDGET_MAX    = float(os.getenv("FLEX_BUDGET_MAX",    "6000"))  # USD/yr for HAV=1.0
HAV_VC_THRESHOLD   = float(os.getenv("HAV_VC_THRESHOLD",   "0.70"))  # Values Custodian floor

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS benefit_plans (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    plan_type   TEXT NOT NULL,  -- 'health'|'dental'|'vision'|'fsa'|'hsa'|'life'|'flex'
    tier        TEXT DEFAULT 'standard',  -- 'standard'|'enhanced'|'premium'
    annual_cost FLOAT DEFAULT 0.0,
    description TEXT,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS enrollments (
    id              TEXT PRIMARY KEY,
    employee_id     TEXT NOT NULL,
    plan_id         TEXT NOT NULL REFERENCES benefit_plans(id),
    effective_date  DATE NOT NULL,
    end_date        DATE,
    status          TEXT DEFAULT 'active',  -- 'active'|'terminated'|'pending'
    hav_at_enrollment FLOAT,
    flex_allocation   FLOAT DEFAULT 0.0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_enroll_employee ON enrollments(employee_id);
CREATE INDEX IF NOT EXISTS idx_enroll_plan ON enrollments(plan_id);

CREATE TABLE IF NOT EXISTS retention_alerts (
    id           TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL,
    org_id       TEXT,
    mean_hav     FLOAT NOT NULL,
    mean_npf     FLOAT NOT NULL,
    phi          FLOAT,
    phi_star     FLOAT,
    alert_type   TEXT NOT NULL,  -- 'values_custodian_at_risk'|'nep_warning'
    severity     TEXT DEFAULT 'high',
    resolved     BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_employee ON retention_alerts(employee_id);
CREATE INDEX IF NOT EXISTS idx_alerts_resolved ON retention_alerts(resolved);

CREATE TABLE IF NOT EXISTS life_events (
    id           TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL,
    event_type   TEXT NOT NULL,  -- 'marriage'|'birth'|'divorce'|'dependent_loss'
    event_date   DATE NOT NULL,
    processed    BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
        # Seed default plans
        await conn.execute("""
            INSERT INTO benefit_plans (id, name, plan_type, tier, annual_cost, description)
            VALUES
              ('plan-health-std',  'Health PPO Standard',    'health',  'standard', 3600, 'Standard PPO network'),
              ('plan-health-prem', 'Health PPO Premium',     'health',  'premium',  5400, 'Premium PPO + HSA'),
              ('plan-dental-std',  'Dental Standard',        'dental',  'standard',  480, 'Preventive + basic restorative'),
              ('plan-vision-std',  'Vision Standard',        'vision',  'standard',  120, 'Annual exam + frames'),
              ('plan-fsa',         'Healthcare FSA',         'fsa',     'standard',    0, 'Pre-tax flex spend account'),
              ('plan-hsa',         'Health Savings Account', 'hsa',     'standard',    0, 'HSA with employer seed'),
              ('plan-flex',        'HAV Flex Benefit',       'flex',    'enhanced',    0, 'HAV-weighted flexible allocation'),
              ('plan-life',        'Life Insurance 2x',      'life',    'standard',  240, '2x salary life insurance')
            ON CONFLICT (id) DO NOTHING
        """)
    yield
    await db.close()

app = FastAPI(title="Tessera Benefits", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class EnrollRequest(BaseModel):
    employee_id: str
    plan_id: str
    effective_date: str
    hav_score: Optional[float] = Field(None, ge=0.0, le=1.0)

class LifeEventRequest(BaseModel):
    employee_id: str
    event_type: str
    event_date: str

class RetentionAlertRequest(BaseModel):
    employee_id: str
    org_id: Optional[str] = None
    mean_hav: float = Field(..., ge=0.0, le=1.0)
    mean_npf: float = Field(..., ge=0.0, le=1.0)
    phi: Optional[float] = None
    phi_star: Optional[float] = None


@app.get("/")
def root():
    return {"service": "benefits", "version": "1.0.0", "port": 8018,
            "differentiator": "HAV-weighted flex allocation + Values Custodian retention alerts"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "benefits"}


@app.get("/plans")
async def list_plans(plan_type: Optional[str] = Query(None)):
    async with db.acquire() as conn:
        if plan_type:
            rows = await conn.fetch("SELECT * FROM benefit_plans WHERE plan_type=$1 AND active=TRUE", plan_type)
        else:
            rows = await conn.fetch("SELECT * FROM benefit_plans WHERE active=TRUE ORDER BY plan_type")
    return {"plans": [dict(r) for r in rows]}


@app.post("/enroll", status_code=201)
async def enroll(body: EnrollRequest):
    """Enroll employee. HAV score drives flex benefit allocation."""
    hav = body.hav_score or 0.0
    flex_alloc = FLEX_BUDGET_BASE + (FLEX_BUDGET_MAX - FLEX_BUDGET_BASE) * hav

    async with db.acquire() as conn:
        plan = await conn.fetchrow("SELECT * FROM benefit_plans WHERE id=$1", body.plan_id)
        if not plan:
            raise HTTPException(404, "Plan not found")
        enrollment_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO enrollments (id, employee_id, plan_id, effective_date, hav_at_enrollment, flex_allocation)
            VALUES ($1,$2,$3,$4::DATE,$5,$6)
        """, enrollment_id, body.employee_id, body.plan_id, body.effective_date, hav, flex_alloc)

    return {
        "enrollment_id": enrollment_id,
        "employee_id": body.employee_id,
        "plan": plan["name"],
        "plan_type": plan["plan_type"],
        "effective_date": body.effective_date,
        "hav_at_enrollment": hav,
        "flex_allocation_annual": round(flex_alloc, 2),
        "note": (
            f"Values Custodian tier (HAV={hav:.2f}): enhanced flex allocation ${flex_alloc:,.0f}/yr. "
            "Retention-critical — flag before any headcount decisions."
            if hav >= HAV_VC_THRESHOLD else
            f"Standard flex allocation ${flex_alloc:,.0f}/yr based on HAV={hav:.2f}."
        ),
    }


@app.get("/employees/{employee_id}/benefits")
async def employee_benefits(employee_id: str):
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT e.*, p.name AS plan_name, p.plan_type, p.tier, p.annual_cost
            FROM enrollments e JOIN benefit_plans p ON e.plan_id = p.id
            WHERE e.employee_id=$1 AND e.status='active'
        """, employee_id)
        alerts = await conn.fetch(
            "SELECT * FROM retention_alerts WHERE employee_id=$1 AND resolved=FALSE", employee_id
        )

    total_cost = sum(r["annual_cost"] for r in rows)
    flex = next((r["flex_allocation"] for r in rows if r["plan_type"] == "flex"), 0.0)

    return {
        "employee_id": employee_id,
        "active_plans": [dict(r) for r in rows],
        "total_employer_cost_annual": round(total_cost, 2),
        "flex_allocation_annual": round(flex, 2),
        "open_retention_alerts": len(alerts),
        "alerts": [dict(a) for a in alerts],
    }


@app.post("/retention-alert", status_code=201)
async def create_retention_alert(body: RetentionAlertRequest):
    """
    Raise a Values Custodian retention alert. Called when HAV is high but
    man-hours metrics might flag this employee for elimination.
    Implements protection against the Nonprofit Elimination Paradox.
    """
    is_vc = body.mean_hav >= HAV_VC_THRESHOLD and body.mean_npf >= 0.65
    phi_above = bool(body.phi and body.phi_star and body.phi > body.phi_star)
    alert_type = "values_custodian_at_risk" if is_vc else "nep_warning"
    severity = "critical" if (is_vc and phi_above) else "high" if is_vc else "medium"

    alert_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO retention_alerts
              (id, employee_id, org_id, mean_hav, mean_npf, phi, phi_star, alert_type, severity)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, alert_id, body.employee_id, body.org_id, body.mean_hav,
             body.mean_npf, body.phi, body.phi_star, alert_type, severity)

    return {
        "alert_id": alert_id,
        "alert_type": alert_type,
        "severity": severity,
        "is_values_custodian": is_vc,
        "phi_above_crossover": phi_above,
        "message": (
            f"CRITICAL: {body.employee_id} is a Values Custodian (HAV={body.mean_hav:.2f}, "
            f"NPF={body.mean_npf:.2f}). Any headcount decision eliminating this role "
            "risks irreversible mission drift. Man-hours metrics are structurally blind "
            "to this employee's contribution above φ*."
            if severity == "critical" else
            f"Values Custodian at risk: HAV={body.mean_hav:.2f}. Review before any org changes."
        ),
    }


@app.get("/retention-alerts")
async def list_alerts(resolved: bool = Query(False), org_id: Optional[str] = Query(None)):
    async with db.acquire() as conn:
        if org_id:
            rows = await conn.fetch(
                "SELECT * FROM retention_alerts WHERE resolved=$1 AND org_id=$2 ORDER BY created_at DESC",
                resolved, org_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM retention_alerts WHERE resolved=$1 ORDER BY created_at DESC", resolved
            )
    return {"alerts": [dict(r) for r in rows], "count": len(rows)}


@app.post("/life-events", status_code=201)
async def log_life_event(body: LifeEventRequest):
    event_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO life_events (id, employee_id, event_type, event_date)
            VALUES ($1,$2,$3,$4::DATE)
        """, event_id, body.employee_id, body.event_type, body.event_date)
    return {
        "event_id": event_id,
        "employee_id": body.employee_id,
        "event_type": body.event_type,
        "event_date": body.event_date,
        "action_required": "Review benefit elections within 30 days of qualifying life event.",
    }
