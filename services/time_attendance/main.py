"""
Tessera Time & Attendance Service  —  port 8017
================================================
HAV-native time tracking. Replaces man-hours with contribution measurement.

Core thesis (Paper 5 — HAVCPN):
  Above φ* (HAV crossover threshold), man-hours is a structurally inadequate
  governance instrument. This service tracks what man-hours cannot:

  HAV(h, session) = 0.50×NPF + 0.30×SRQ + 0.20×OC

  NPF  — Non-Procedure Fraction: time outside repeated-endeavour mode
  SRQ  — SLA Recovery Quality: quality of human response to AI SLO breaches
  OC   — Origination Capacity: novel framings beyond org memory

φ-aware scheduling:
  The twin service publishes φ (AI fraction) and φ* in real time.
  Above φ*, humans are scheduled as SLA guardians and Values Custodians,
  not just by business hours.

Payroll integration:
  HAV summary feeds directly into payroll alignment premium:
  Total Compensation = Salary + Token Budget + (r_AP × HAV × Salary)
  r_AP = 5% at φ < 0.25, 25% at φ > 0.75 (interpolated).

This is what Workday cannot do: it counts hours. Tessera measures whether
the human was doing something the AI couldn't.
"""

from __future__ import annotations

import os
import uuid
import httpx
import asyncpg
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://tessera:tessera@localhost:5432/tessera_timeattendance",
)
TWIN_URL    = os.getenv("TWIN_URL", "http://twin:8004")
DEFAULT_K   = int(os.getenv("DEFAULT_K", "4"))

db: asyncpg.Pool | None = None


# ── HAV helpers ───────────────────────────────────────────────────────────────

def _phi_star(K: int = 4, org_type: str = "profit") -> float:
    base = 0.25 if K >= 6 else (0.32 if K >= 3 else 0.44)
    return round(base * 0.70 if org_type == "nonprofit" else base, 4)


def _compute_hav(npf: float, srq: float, oc: float) -> float:
    return round(0.50 * npf + 0.30 * srq + 0.20 * oc, 4)


def _r_ap(mean_phi: float) -> float:
    if mean_phi < 0.25:
        return 0.05
    if mean_phi > 0.75:
        return 0.25
    return round(0.05 + (mean_phi - 0.25) * (0.20 / 0.50), 4)


def _shift_type(phi: float, phi_star_val: float, min_npf: float) -> str:
    if phi > phi_star_val:
        return "values_custodian" if min_npf >= 0.70 else "phi_guardian"
    return "standard"


async def _fetch_phi(sim_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{TWIN_URL}/sim/{sim_id}/phi")
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {}


# ── Database schema ───────────────────────────────────────────────────────────

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS sessions (
    id                     TEXT PRIMARY KEY,
    employee_id            TEXT NOT NULL,
    org_id                 TEXT,
    checkin_at             TIMESTAMPTZ NOT NULL,
    checkout_at            TIMESTAMPTZ,
    task_type              TEXT NOT NULL DEFAULT 'mixed',
    declared_npf           FLOAT,
    actual_npf             FLOAT,
    srq_score              FLOAT DEFAULT 0.0,
    oc_score               FLOAT DEFAULT 0.0,
    hav_score              FLOAT,
    phi_at_checkin         FLOAT,
    phi_star               FLOAT,
    above_crossover        BOOLEAN DEFAULT FALSE,
    shift_type             TEXT DEFAULT 'standard',
    total_minutes          FLOAT,
    non_procedural_minutes FLOAT,
    state                  TEXT DEFAULT 'active',
    notes                  TEXT,
    created_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_employee  ON sessions(employee_id);
CREATE INDEX IF NOT EXISTS idx_sessions_org       ON sessions(org_id);
CREATE INDEX IF NOT EXISTS idx_sessions_state     ON sessions(state);
CREATE INDEX IF NOT EXISTS idx_sessions_checkin   ON sessions(checkin_at);

CREATE TABLE IF NOT EXISTS srq_events (
    id                    TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL REFERENCES sessions(id),
    employee_id           TEXT NOT NULL,
    ai_agent_id           TEXT,
    slo_type              TEXT,
    recovery_quality      FLOAT NOT NULL CHECK (recovery_quality BETWEEN 0 AND 1),
    fitness_delta         FLOAT DEFAULT 0.0,
    recovery_time_minutes FLOAT,
    logged_at             TIMESTAMPTZ DEFAULT NOW(),
    notes                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_srq_session  ON srq_events(session_id);
CREATE INDEX IF NOT EXISTS idx_srq_employee ON srq_events(employee_id);

CREATE TABLE IF NOT EXISTS oc_events (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions(id),
    employee_id       TEXT NOT NULL,
    framing_type      TEXT DEFAULT 'novel_solution',
    beyond_org_memory BOOLEAN DEFAULT TRUE,
    impact_score      FLOAT DEFAULT 0.5 CHECK (impact_score BETWEEN 0 AND 1),
    logged_at         TIMESTAMPTZ DEFAULT NOW(),
    description       TEXT
);

CREATE INDEX IF NOT EXISTS idx_oc_session  ON oc_events(session_id);
CREATE INDEX IF NOT EXISTS idx_oc_employee ON oc_events(employee_id);

CREATE TABLE IF NOT EXISTS schedules (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL,
    shift_date       DATE NOT NULL,
    shift_start      TIME NOT NULL,
    shift_end        TIME NOT NULL,
    employee_id      TEXT NOT NULL,
    shift_type       TEXT NOT NULL DEFAULT 'standard',
    min_npf_required FLOAT DEFAULT 0.0,
    phi_forecast     FLOAT,
    phi_star         FLOAT,
    above_crossover  BOOLEAN DEFAULT FALSE,
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_schedules_org      ON schedules(org_id, shift_date);
CREATE INDEX IF NOT EXISTS idx_schedules_employee ON schedules(employee_id, shift_date);

CREATE TABLE IF NOT EXISTS absences (
    id           TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL,
    absence_date DATE NOT NULL,
    absence_type TEXT NOT NULL DEFAULT 'annual_leave',
    hours        FLOAT DEFAULT 8.0,
    hav_impact   FLOAT,
    approved     BOOLEAN DEFAULT FALSE,
    approved_by  TEXT,
    notes        TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_absences_employee ON absences(employee_id, absence_date);
"""


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
    yield
    await db.close()


app = FastAPI(
    title="Tessera Time & Attendance",
    description="HAV-native time tracking with φ-aware scheduling",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class CheckinRequest(BaseModel):
    employee_id: str
    org_id: Optional[str] = None
    task_type: str = Field("mixed", description="'procedural' | 'non_procedural' | 'mixed'")
    declared_npf: Optional[float] = Field(None, ge=0.0, le=1.0)
    phi_at_checkin: Optional[float] = Field(None, ge=0.0, le=1.0)
    phi_star: Optional[float] = Field(None, ge=0.0, le=1.0)
    sim_id: Optional[str] = Field(None, description="Twin sim_id — auto-fetches live φ")
    notes: Optional[str] = None


class CheckoutRequest(BaseModel):
    actual_npf: float = Field(..., ge=0.0, le=1.0)
    non_procedural_minutes: Optional[float] = Field(None, ge=0.0)
    notes: Optional[str] = None


class SRQEventRequest(BaseModel):
    ai_agent_id: Optional[str] = None
    slo_type: Optional[str] = None
    recovery_quality: float = Field(..., ge=0.0, le=1.0)
    fitness_delta: float = Field(0.0)
    recovery_time_minutes: Optional[float] = None
    notes: Optional[str] = None


class OCEventRequest(BaseModel):
    framing_type: str = Field(
        "novel_solution",
        description="'problem_reframe' | 'novel_solution' | 'template_creation' | 'policy_origination'",
    )
    beyond_org_memory: bool = True
    impact_score: float = Field(0.5, ge=0.0, le=1.0)
    description: Optional[str] = None


class ScheduleRequest(BaseModel):
    org_id: str
    shift_date: str = Field(..., description="YYYY-MM-DD")
    shift_start: str = Field(..., description="HH:MM")
    shift_end: str = Field(..., description="HH:MM")
    employee_id: str
    shift_type: str = Field("standard", description="'standard' | 'phi_guardian' | 'values_custodian'")
    min_npf_required: float = Field(0.0, ge=0.0, le=1.0)
    phi_forecast: Optional[float] = Field(None, ge=0.0, le=1.0)
    phi_star: Optional[float] = Field(None, ge=0.0, le=1.0)
    notes: Optional[str] = None


class AbsenceRequest(BaseModel):
    employee_id: str
    absence_date: str = Field(..., description="YYYY-MM-DD")
    absence_type: str = Field("annual_leave", description="'annual_leave' | 'sick' | 'unpaid' | 'personal'")
    hours: float = Field(8.0, gt=0.0)
    hav_impact: Optional[float] = Field(None, ge=0.0, le=1.0)
    notes: Optional[str] = None


# ── Root / Health ─────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "time-attendance",
        "version": "1.0.0",
        "port": 8017,
        "description": "HAV-native time tracking with φ-aware scheduling",
        "grounded_in": "Rajendra (2026d) — HAVCPN Paper 5",
        "core_formula": "HAV = 0.50×NPF + 0.30×SRQ + 0.20×OC",
        "differentiator": "Workday counts hours. Tessera measures whether the human was doing something the AI couldn't.",
    }


@app.get("/health")
async def health():
    try:
        async with db.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "service": "time-attendance"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Check-in ──────────────────────────────────────────────────────────────────

@app.post("/checkin", status_code=201)
async def checkin(body: CheckinRequest):
    """
    Start a session. If sim_id is provided, φ is fetched live from the twin.
    shift_type is auto-assigned: standard / phi_guardian / values_custodian.
    """
    phi       = body.phi_at_checkin
    phi_star_v = body.phi_star

    if body.sim_id:
        phi_data = await _fetch_phi(body.sim_id)
        if phi_data:
            phi        = phi_data.get("phi_current", phi)
            phi_star_v = phi_data.get("phi_star", phi_star_v)

    above      = bool(phi and phi_star_v and phi > phi_star_v)
    min_npf    = 0.70 if above else 0.0
    stype      = _shift_type(phi or 0.0, phi_star_v or 0.32, min_npf)
    session_id = str(uuid.uuid4())
    now        = datetime.now(timezone.utc)

    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions
              (id, employee_id, org_id, checkin_at, task_type, declared_npf,
               phi_at_checkin, phi_star, above_crossover, shift_type, state, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'active',$11)
            """,
            session_id, body.employee_id, body.org_id, now,
            body.task_type, body.declared_npf,
            phi, phi_star_v, above, stype, body.notes,
        )

    return {
        "session_id":      session_id,
        "employee_id":     body.employee_id,
        "checkin_at":      now.isoformat(),
        "shift_type":      stype,
        "phi_at_checkin":  phi,
        "phi_star":        phi_star_v,
        "above_crossover": above,
        "guidance": (
            f"φ-guardian shift: φ={phi:.3f} > φ*={phi_star_v:.2f}. "
            "SLA recovery (SRQ) and novel framing (OC) are your primary contributions today."
            if above else
            "Standard shift. Log SRQ and OC events if they occur."
        ),
    }


# ── Check-out ─────────────────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/checkout")
async def checkout(session_id: str, body: CheckoutRequest):
    """
    Close session and compute HAV = 0.50×NPF + 0.30×SRQ + 0.20×OC.
    This score is the payroll input — feeds alignment premium in payroll service.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1", session_id)
        if not row:
            raise HTTPException(404, "Session not found")
        if row["state"] == "completed":
            raise HTTPException(409, "Session already checked out")

        now        = datetime.now(timezone.utc)
        checkin_at = row["checkin_at"]
        if checkin_at.tzinfo is None:
            checkin_at = checkin_at.replace(tzinfo=timezone.utc)

        total_min    = (now - checkin_at).total_seconds() / 60.0
        non_proc_min = (
            body.non_procedural_minutes
            if body.non_procedural_minutes is not None
            else total_min * body.actual_npf
        )

        srq_rows = await conn.fetch(
            "SELECT recovery_quality FROM srq_events WHERE session_id=$1", session_id
        )
        srq_score = (
            sum(r["recovery_quality"] for r in srq_rows) / len(srq_rows)
            if srq_rows else 0.0
        )

        oc_rows = await conn.fetch(
            "SELECT impact_score, beyond_org_memory FROM oc_events WHERE session_id=$1",
            session_id,
        )
        valid_oc  = [r for r in oc_rows if r["beyond_org_memory"]]
        oc_score  = (
            min(1.0, sum(r["impact_score"] for r in valid_oc) / len(valid_oc))
            if valid_oc else 0.0
        )

        hav = _compute_hav(body.actual_npf, srq_score, oc_score)

        await conn.execute(
            """
            UPDATE sessions SET
                checkout_at=$1, actual_npf=$2, srq_score=$3, oc_score=$4,
                hav_score=$5, total_minutes=$6, non_procedural_minutes=$7,
                state='completed'
            WHERE id=$8
            """,
            now, body.actual_npf, srq_score, oc_score,
            hav, total_min, non_proc_min, session_id,
        )

    phi       = row["phi_at_checkin"]
    phi_star_v = row["phi_star"]
    above     = row["above_crossover"]

    return {
        "session_id":   session_id,
        "employee_id":  row["employee_id"],
        "checkin_at":   row["checkin_at"].isoformat(),
        "checkout_at":  now.isoformat(),
        "total_minutes": round(total_min, 1),
        "non_procedural_minutes": round(non_proc_min, 1),
        "hav_breakdown": {
            "npf":     round(body.actual_npf, 4),
            "srq":     round(srq_score, 4),
            "oc":      round(oc_score, 4),
            "hav":     hav,
            "formula": "0.50×NPF + 0.30×SRQ + 0.20×OC",
        },
        "shift_type":      row["shift_type"],
        "above_crossover": above,
        "srq_events":      len(srq_rows),
        "oc_events":       len(oc_rows),
        "phi_context": {
            "phi":       phi,
            "phi_star":  phi_star_v,
            "note": (
                f"φ={phi:.3f} > φ*={phi_star_v:.2f} — HAV regime active. "
                "Man-hours would have undercounted this contribution."
                if above else
                f"φ={phi:.3f} ≤ φ*={phi_star_v:.2f} — both regimes equivalent at this autonomy level."
            ) if phi and phi_star_v else "No φ context recorded.",
        },
    }


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1", session_id)
        if not row:
            raise HTTPException(404, "Session not found")
        srq = await conn.fetch("SELECT * FROM srq_events WHERE session_id=$1", session_id)
        oc  = await conn.fetch("SELECT * FROM oc_events WHERE session_id=$1", session_id)

    def _fmt(r: asyncpg.Record) -> dict:
        d = dict(r)
        for k in ("checkin_at", "checkout_at", "created_at", "logged_at"):
            if k in d and d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    return {**_fmt(row), "srq_events": [_fmt(r) for r in srq], "oc_events": [_fmt(r) for r in oc]}


@app.get("/sessions")
async def list_sessions(
    employee_id: Optional[str] = Query(None),
    org_id: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    async with db.acquire() as conn:
        clauses, params = [], []
        for field, val in [("employee_id", employee_id), ("org_id", org_id), ("state", state)]:
            if val:
                params.append(val)
                clauses.append(f"{field}=${len(params)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await conn.fetch(
            f"SELECT * FROM sessions {where} ORDER BY checkin_at DESC LIMIT {limit}",
            *params,
        )

    def _fmt(r):
        d = dict(r)
        for k in ("checkin_at", "checkout_at", "created_at"):
            if k in d and d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    return {"sessions": [_fmt(r) for r in rows], "count": len(rows)}


# ── SRQ events ────────────────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/srq", status_code=201)
async def log_srq(session_id: str, body: SRQEventRequest):
    """
    Log an SLA Recovery Quality event during an active session.
    Called when a human recovers from an AI SLO breach.
    This is the primary signal that justifies the SRQ component of HAV.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT employee_id, state FROM sessions WHERE id=$1", session_id
        )
        if not row:
            raise HTTPException(404, "Session not found")
        if row["state"] == "completed":
            raise HTTPException(409, "Cannot log SRQ to a completed session — check out first")

        event_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO srq_events
              (id, session_id, employee_id, ai_agent_id, slo_type,
               recovery_quality, fitness_delta, recovery_time_minutes, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            event_id, session_id, row["employee_id"], body.ai_agent_id,
            body.slo_type, body.recovery_quality, body.fitness_delta,
            body.recovery_time_minutes, body.notes,
        )

    return {
        "event_id":         event_id,
        "session_id":       session_id,
        "recovery_quality": body.recovery_quality,
        "fitness_delta":    body.fitness_delta,
        "message": (
            f"SRQ event recorded. Recovery quality {body.recovery_quality:.2f} "
            "will lift the SRQ component (0.30×SRQ) at checkout."
        ),
    }


# ── OC events ─────────────────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/oc", status_code=201)
async def log_oc(session_id: str, body: OCEventRequest):
    """
    Log an Origination Capacity event — a novel framing beyond org memory.
    These protect against the Nonprofit Elimination Paradox: man-hours-governed
    orgs above φ* systematically eliminate the humans who log OC events,
    calling them 'unproductive'. HAV makes these contributions visible.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT employee_id, state FROM sessions WHERE id=$1", session_id
        )
        if not row:
            raise HTTPException(404, "Session not found")
        if row["state"] == "completed":
            raise HTTPException(409, "Cannot log OC to a completed session")

        event_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO oc_events
              (id, session_id, employee_id, framing_type, beyond_org_memory,
               impact_score, description)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            event_id, session_id, row["employee_id"], body.framing_type,
            body.beyond_org_memory, body.impact_score, body.description,
        )

    return {
        "event_id":          event_id,
        "session_id":        session_id,
        "framing_type":      body.framing_type,
        "beyond_org_memory": body.beyond_org_memory,
        "impact_score":      body.impact_score,
        "message": (
            f"OC event recorded — framing type '{body.framing_type}' beyond org memory. "
            "Contributes to 0.20×OC component at checkout."
            if body.beyond_org_memory else
            "OC event recorded but marked within existing org memory. No HAV OC contribution."
        ),
    }


# ── HAV summary (payroll feed) ────────────────────────────────────────────────

@app.get("/employees/{employee_id}/hav-summary")
async def hav_summary(
    employee_id: str,
    period_start: Optional[str] = Query(None, description="YYYY-MM-DD"),
    period_end:   Optional[str] = Query(None, description="YYYY-MM-DD"),
    org_type: str = Query("profit"),
    K: int = Query(4),
):
    """
    HAV summary for a pay period. Direct input to payroll alignment premium.
    Total Compensation = Salary + Token Budget + (r_AP × mean_HAV × Salary)
    """
    async with db.acquire() as conn:
        clauses = ["employee_id=$1", "state='completed'"]
        params  = [employee_id]
        if period_start:
            params.append(period_start)
            clauses.append(f"checkin_at >= ${len(params)}::TIMESTAMPTZ")
        if period_end:
            params.append(period_end)
            clauses.append(f"checkin_at < (${len(params)}::DATE + INTERVAL '1 day')::TIMESTAMPTZ")
        rows = await conn.fetch(
            f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} ORDER BY checkin_at",
            *params,
        )

    n = len(rows)
    if n == 0:
        return {
            "employee_id": employee_id,
            "period": {"start": period_start, "end": period_end},
            "sessions": 0,
            "mean_hav": None,
            "alignment_premium_rate": 0.05,
            "payroll_signal": {"note": "No completed sessions in period."},
        }

    mean_hav  = sum(r["hav_score"]  or 0 for r in rows) / n
    mean_npf  = sum(r["actual_npf"] or 0 for r in rows) / n
    mean_srq  = sum(r["srq_score"]  or 0 for r in rows) / n
    mean_oc   = sum(r["oc_score"]   or 0 for r in rows) / n
    total_hrs = sum(r["total_minutes"] or 0 for r in rows) / 60.0
    np_hrs    = sum(r["non_procedural_minutes"] or 0 for r in rows) / 60.0

    phi_vals  = [r["phi_at_checkin"] for r in rows if r["phi_at_checkin"] is not None]
    mean_phi  = sum(phi_vals) / len(phi_vals) if phi_vals else 0.0
    r_ap      = _r_ap(mean_phi)
    phi_s     = _phi_star(K, org_type)

    phi_guardian_n  = sum(1 for r in rows if r["shift_type"] == "phi_guardian")
    vc_n            = sum(1 for r in rows if r["shift_type"] == "values_custodian")
    above_n         = sum(1 for r in rows if r["above_crossover"])

    return {
        "employee_id": employee_id,
        "period": {"start": period_start, "end": period_end},
        "sessions":   n,
        "mean_hav":   round(mean_hav, 4),
        "mean_npf":   round(mean_npf, 4),
        "mean_srq":   round(mean_srq, 4),
        "mean_oc":    round(mean_oc,  4),
        "total_hours":          round(total_hrs, 2),
        "non_procedural_hours": round(np_hrs, 2),
        "phi_guardian_sessions":    phi_guardian_n,
        "values_custodian_sessions": vc_n,
        "above_crossover_sessions":  above_n,
        "mean_phi":  round(mean_phi, 4),
        "phi_star":  phi_s,
        "alignment_premium_rate": r_ap,
        "payroll_signal": {
            "formula":  "Total Compensation = Salary + Token Budget + (r_AP × HAV × Salary)",
            "r_ap":     r_ap,
            "mean_hav": round(mean_hav, 4),
            "note": (
                f"{above_n}/{n} sessions above φ*={phi_s}. "
                "Man-hours alone would have missed these contributions — "
                "alignment premium justified."
                if above_n > 0 else
                "All sessions below φ*. Man-hours and HAV equivalent this period."
            ),
        },
    }


@app.get("/employees/{employee_id}/sessions")
async def employee_sessions(
    employee_id: str,
    state: Optional[str] = Query(None),
    limit: int = Query(20, le=100),
):
    async with db.acquire() as conn:
        clauses, params = ["employee_id=$1"], [employee_id]
        if state:
            params.append(state)
            clauses.append(f"state=${len(params)}")
        rows = await conn.fetch(
            f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} "
            f"ORDER BY checkin_at DESC LIMIT {limit}",
            *params,
        )

    def _fmt(r):
        d = dict(r)
        for k in ("checkin_at", "checkout_at", "created_at"):
            if k in d and d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    return {"employee_id": employee_id, "sessions": [_fmt(r) for r in rows]}


# ── Bulk session import (historical / customer onboarding) ────────────────────

class ImportedSession(BaseModel):
    employee_id: str
    org_id:      Optional[str] = "demo-org"
    checkin_at:  str           # ISO-8601 timestamp
    checkout_at: str           # ISO-8601 timestamp
    actual_npf:  float = Field(..., ge=0.0, le=1.0)
    srq_score:   float = Field(0.5, ge=0.0, le=1.0)
    oc_score:    float = Field(0.4, ge=0.0, le=1.0)
    phi_at_checkin: Optional[float] = None
    phi_star:    Optional[float] = None
    task_type:   str = "mixed"
    source:      Optional[str] = None   # "workday", "bamboohr", "survey", "manual"
    notes:       Optional[str] = None


class BulkImportRequest(BaseModel):
    sessions: list[ImportedSession]
    phi_star_default: float = 0.32


@app.post("/sessions/import", status_code=201)
async def bulk_import_sessions(body: BulkImportRequest):
    """
    Import pre-computed completed sessions in bulk.
    Used for historical data onboarding from Workday, BambooHR, surveys, etc.
    Each session's HAV is computed server-side from (npf, srq, oc).
    """
    inserted, skipped = 0, 0
    async with db.acquire() as conn:
        for s in body.sessions:
            try:
                hav = _compute_hav(s.actual_npf, s.srq_score, s.oc_score)
                phi = s.phi_at_checkin or 0.087
                phi_s = s.phi_star or body.phi_star_default
                above = phi > phi_s
                stype = _shift_type(phi, phi_s, s.actual_npf)
                cin  = datetime.fromisoformat(s.checkin_at.replace("Z", "+00:00"))
                cout = datetime.fromisoformat(s.checkout_at.replace("Z", "+00:00"))
                total_min = (cout - cin).total_seconds() / 60.0
                np_min    = total_min * s.actual_npf
                sid = str(uuid.uuid4())
                await conn.execute(
                    """
                    INSERT INTO sessions
                      (id, employee_id, org_id, checkin_at, checkout_at,
                       task_type, declared_npf, actual_npf, srq_score, oc_score,
                       hav_score, phi_at_checkin, phi_star, above_crossover,
                       shift_type, total_minutes, non_procedural_minutes, state, notes)
                    VALUES
                      ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,'completed',$18)
                    ON CONFLICT DO NOTHING
                    """,
                    sid, s.employee_id, s.org_id, cin, cout,
                    s.task_type, s.actual_npf, s.actual_npf, s.srq_score, s.oc_score,
                    hav, phi, phi_s, above,
                    stype, total_min, np_min,
                    f"[{s.source or 'import'}] {s.notes or ''}".strip(),
                )
                inserted += 1
            except Exception as exc:
                skipped += 1
    return {"inserted": inserted, "skipped": skipped, "total": len(body.sessions)}


# ── φ-aware scheduling ────────────────────────────────────────────────────────

@app.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleRequest):
    """
    Create a shift. If phi_forecast > phi_star, shift_type auto-escalates
    to phi_guardian (general) or values_custodian (high NPF required).
    """
    phi    = body.phi_forecast
    phi_s  = body.phi_star or _phi_star(DEFAULT_K)
    above  = bool(phi and phi > phi_s)
    stype  = body.shift_type
    if above and stype == "standard":
        stype = "phi_guardian"

    sched_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO schedules
              (id, org_id, shift_date, shift_start, shift_end, employee_id,
               shift_type, min_npf_required, phi_forecast, phi_star,
               above_crossover, notes)
            VALUES ($1,$2,$3::DATE,$4::TIME,$5::TIME,$6,$7,$8,$9,$10,$11,$12)
            """,
            sched_id, body.org_id, body.shift_date, body.shift_start,
            body.shift_end, body.employee_id, stype, body.min_npf_required,
            phi, phi_s, above, body.notes,
        )

    return {
        "schedule_id":       sched_id,
        "employee_id":       body.employee_id,
        "org_id":            body.org_id,
        "shift_date":        body.shift_date,
        "shift_start":       body.shift_start,
        "shift_end":         body.shift_end,
        "shift_type":        stype,
        "phi_forecast":      phi,
        "phi_star":          phi_s,
        "above_crossover":   above,
        "min_npf_required":  body.min_npf_required,
        "guidance": (
            f"φ-guardian shift: forecast φ={phi:.3f} > φ*={phi_s:.2f}. "
            f"Assign high-NPF employees. Min NPF {body.min_npf_required:.0%} required."
        ) if above else "Standard shift — man-hours and HAV equivalent at this φ.",
    }


@app.get("/schedules")
async def list_schedules(
    org_id:      Optional[str] = Query(None),
    employee_id: Optional[str] = Query(None),
    shift_date:  Optional[str] = Query(None),
    shift_type:  Optional[str] = Query(None),
):
    async with db.acquire() as conn:
        clauses, params = [], []
        for field, val in [
            ("org_id", org_id), ("employee_id", employee_id), ("shift_type", shift_type)
        ]:
            if val:
                params.append(val)
                clauses.append(f"{field}=${len(params)}")
        if shift_date:
            params.append(shift_date)
            clauses.append(f"shift_date=${len(params)}::DATE")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await conn.fetch(
            f"SELECT * FROM schedules {where} ORDER BY shift_date, shift_start LIMIT 200",
            *params,
        )

    def _fmt(r):
        d = dict(r)
        d["shift_date"]  = str(d["shift_date"])
        d["shift_start"] = str(d["shift_start"])
        d["shift_end"]   = str(d["shift_end"])
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        return d

    return {"schedules": [_fmt(r) for r in rows], "count": len(rows)}


# ── φ-coverage dashboard ──────────────────────────────────────────────────────

@app.get("/phi-coverage")
async def phi_coverage(
    org_id:     Optional[str] = Query(None),
    shift_date: Optional[str] = Query(None),
    sim_id:     Optional[str] = Query(None),
):
    """
    Show current φ window from the twin and human coverage status.
    Raises a COVERAGE GAP alert if φ > φ* but no φ-guardians are on shift.
    """
    phi_data = {}
    if sim_id:
        phi_data = await _fetch_phi(sim_id)

    phi        = phi_data.get("phi_current")
    phi_star_v = phi_data.get("phi_star")
    crossover  = phi_data.get("crossover", False)

    async with db.acquire() as conn:
        # Scheduled shifts
        s_clauses, s_params = [], []
        if org_id:
            s_params.append(org_id); s_clauses.append(f"org_id=${len(s_params)}")
        if shift_date:
            s_params.append(shift_date); s_clauses.append(f"shift_date=${len(s_params)}::DATE")
        s_where = ("WHERE " + " AND ".join(s_clauses)) if s_clauses else ""
        schedules = await conn.fetch(
            f"SELECT * FROM schedules {s_where} ORDER BY shift_start LIMIT 200",
            *s_params,
        )

        # Currently active sessions
        a_clauses, a_params = ["state='active'"], []
        if org_id:
            a_params.append(org_id); a_clauses.append(f"org_id=${len(a_params)}")
        active_sessions = await conn.fetch(
            f"SELECT * FROM sessions WHERE {' AND '.join(a_clauses)}",
            *a_params,
        )

    phi_guardian_scheds = [s for s in schedules if s["shift_type"] in ("phi_guardian", "values_custodian")]
    guardians_on_shift  = [s for s in active_sessions if s["shift_type"] in ("phi_guardian", "values_custodian")]
    vc_on_shift         = [s for s in active_sessions if s["shift_type"] == "values_custodian"]

    return {
        "sim_phi": {
            "phi":             phi,
            "phi_star":        phi_star_v,
            "crossover_active": crossover,
            "regime":          "HAV" if crossover else "Standard",
        },
        "coverage": {
            "total_scheduled":         len(schedules),
            "phi_guardian_scheduled":  len(phi_guardian_scheds),
            "active_sessions":         len(active_sessions),
            "guardians_on_shift_now":  len(guardians_on_shift),
            "values_custodians_now":   len(vc_on_shift),
        },
        "alert": (
            "COVERAGE GAP: φ above crossover but no φ-guardians on shift. "
            "AI SLO breach risk unmitigated."
            if crossover and len(guardians_on_shift) == 0 else None
        ),
    }


# ── Absences ──────────────────────────────────────────────────────────────────

@app.post("/absences", status_code=201)
async def log_absence(body: AbsenceRequest):
    absence_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO absences
              (id, employee_id, absence_date, absence_type, hours, hav_impact, notes)
            VALUES ($1,$2,$3::DATE,$4,$5,$6,$7)
            """,
            absence_id, body.employee_id, body.absence_date,
            body.absence_type, body.hours, body.hav_impact, body.notes,
        )

    return {
        "absence_id":   absence_id,
        "employee_id":  body.employee_id,
        "absence_date": body.absence_date,
        "absence_type": body.absence_type,
        "hours":        body.hours,
        "hav_impact":   body.hav_impact,
        "warning": (
            f"Values Custodian absence risk: HAV impact {body.hav_impact:.2f}. "
            "Nonprofit Elimination Paradox — this employee's absence may be "
            "misread as an efficiency opportunity under man-hours governance."
            if body.hav_impact and body.hav_impact > 0.60 else None
        ),
    }


@app.get("/org-hav-summary")
async def org_hav_summary(
    org_id:    Optional[str] = Query(None),
    last_days: int           = Query(30),
):
    """
    Org-wide HAV aggregate across all completed sessions.
    Primary input to twin calibration — replaces seeded performance data.
    """
    async with db.acquire() as conn:
        clauses = ["state='completed'", "hav_score IS NOT NULL"]
        params  = []
        if org_id:
            params.append(org_id)
            clauses.append(f"org_id=${len(params)}")
        params.append(last_days)
        clauses.append(f"checkin_at >= NOW() - INTERVAL '1 day' * ${len(params)}")
        where = "WHERE " + " AND ".join(clauses)
        rows = await conn.fetch(
            f"SELECT hav_score, actual_npf, srq_score, oc_score, "
            f"shift_type, above_crossover, phi_at_checkin "
            f"FROM sessions {where}",
            *params,
        )

    n = len(rows)
    if n == 0:
        return {
            "org_id": org_id, "n_sessions": 0, "last_days": last_days,
            "mean_hav": None, "mean_npf": None, "mean_srq": None, "mean_oc": None,
            "note": "No completed sessions — twin calibration will use performance review data.",
        }

    mean_hav = sum(r["hav_score"]  or 0 for r in rows) / n
    mean_npf = sum(r["actual_npf"] or 0 for r in rows) / n
    mean_srq = sum(r["srq_score"]  or 0 for r in rows) / n
    mean_oc  = sum(r["oc_score"]   or 0 for r in rows) / n
    phi_guardian_n = sum(1 for r in rows if r["shift_type"] in ("phi_guardian", "values_custodian"))
    above_n        = sum(1 for r in rows if r["above_crossover"])

    return {
        "org_id":    org_id,
        "n_sessions": n,
        "last_days":  last_days,
        "mean_hav":   round(mean_hav, 4),
        "mean_npf":   round(mean_npf, 4),
        "mean_srq":   round(mean_srq, 4),
        "mean_oc":    round(mean_oc,  4),
        "phi_guardian_sessions": phi_guardian_n,
        "above_crossover_sessions": above_n,
        "data_source": "time_attendance",
        "note": f"{n} real sessions over last {last_days} days — HAV measured, not seeded.",
    }


@app.get("/absences")
async def list_absences(
    employee_id:  Optional[str] = Query(None),
    period_start: Optional[str] = Query(None),
    period_end:   Optional[str] = Query(None),
):
    async with db.acquire() as conn:
        clauses, params = [], []
        if employee_id:
            params.append(employee_id); clauses.append(f"employee_id=${len(params)}")
        if period_start:
            params.append(period_start); clauses.append(f"absence_date >= ${len(params)}::DATE")
        if period_end:
            params.append(period_end); clauses.append(f"absence_date <= ${len(params)}::DATE")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await conn.fetch(
            f"SELECT * FROM absences {where} ORDER BY absence_date DESC LIMIT 200",
            *params,
        )

    def _fmt(r):
        d = dict(r)
        d["absence_date"] = str(d["absence_date"])
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        return d

    return {"absences": [_fmt(r) for r in rows], "count": len(rows)}
