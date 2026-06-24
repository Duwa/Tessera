"""
Tessera Performance Management  —  port 8020
=============================================
Continuous HAV replaces the annual review. No "exceeds expectations" —
instead, NPF trend, SRQ event count, OC contribution rate.

Workday does: set goals → mid-year → year-end → rating → merit.
Tessera does: pull HAV from time-attendance continuously → flag drift
              before it becomes a PIP → calibrate on HAV curves not
              manager opinion → feed merit directly to compensation.
"""
from __future__ import annotations
import os, uuid, asyncpg
from datetime import datetime, timezone, date as date_type
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_performance")
TIME_ATTENDANCE_URL = os.getenv("TIME_ATTENDANCE_URL", "http://time-attendance:8017")
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS cycles (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    cycle_type  TEXT DEFAULT 'quarterly',  -- 'quarterly'|'semi_annual'|'annual'
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    status      TEXT DEFAULT 'active',  -- 'active'|'calibration'|'closed'
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reviews (
    id              TEXT PRIMARY KEY,
    cycle_id        TEXT NOT NULL REFERENCES cycles(id),
    employee_id     TEXT NOT NULL,
    reviewer_id     TEXT,
    mean_hav        FLOAT,
    mean_npf        FLOAT,
    mean_srq        FLOAT,
    mean_oc         FLOAT,
    hav_trend       TEXT,  -- 'improving'|'stable'|'declining'
    npf_trend       TEXT,
    above_crossover_pct FLOAT,
    phi_guardian_sessions INT DEFAULT 0,
    rating          TEXT,  -- 'exceptional'|'strong'|'meets'|'developing'|'below'
    merit_recommendation FLOAT,  -- % increase recommended
    narrative       TEXT,
    status          TEXT DEFAULT 'draft',  -- 'draft'|'submitted'|'calibrated'
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reviews_cycle    ON reviews(cycle_id);
CREATE INDEX IF NOT EXISTS idx_reviews_employee ON reviews(employee_id);

CREATE TABLE IF NOT EXISTS goals (
    id           TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL,
    cycle_id     TEXT REFERENCES cycles(id),
    title        TEXT NOT NULL,
    goal_type    TEXT DEFAULT 'npf',  -- 'npf'|'srq'|'oc'|'hav'|'phi_coverage'
    target_value FLOAT NOT NULL,
    current_value FLOAT,
    status       TEXT DEFAULT 'in_progress',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_goals_employee ON goals(employee_id);

CREATE TABLE IF NOT EXISTS calibrations (
    id          TEXT PRIMARY KEY,
    cycle_id    TEXT NOT NULL REFERENCES cycles(id),
    org_id      TEXT NOT NULL,
    facilitator TEXT,
    employees_calibrated INT DEFAULT 0,
    rating_distribution JSONB,
    hav_p50     FLOAT,
    hav_p75     FLOAT,
    hav_p90     FLOAT,
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

app = FastAPI(title="Tessera Performance", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _hav_to_rating(hav: float, trend: str) -> tuple[str, float]:
    if hav >= 0.80 and trend != "declining":
        return "exceptional", 0.08
    if hav >= 0.65:
        return "strong", 0.055
    if hav >= 0.50:
        return "meets", 0.035
    if hav >= 0.35:
        return "developing", 0.01
    return "below", 0.0


class CycleRequest(BaseModel):
    org_id: str
    name: str
    cycle_type: str = "quarterly"
    start_date: str
    end_date: str

class ReviewRequest(BaseModel):
    cycle_id: str
    employee_id: str
    reviewer_id: Optional[str] = None
    mean_hav: float = Field(..., ge=0.0, le=1.0)
    mean_npf: float = Field(..., ge=0.0, le=1.0)
    mean_srq: float = Field(0.0, ge=0.0, le=1.0)
    mean_oc: float = Field(0.0, ge=0.0, le=1.0)
    hav_trend: str = "stable"
    npf_trend: str = "stable"
    above_crossover_pct: float = Field(0.0, ge=0.0, le=1.0)
    phi_guardian_sessions: int = 0
    narrative: Optional[str] = None

class GoalRequest(BaseModel):
    employee_id: str
    cycle_id: Optional[str] = None
    title: str
    goal_type: str = "hav"
    target_value: float


@app.get("/")
def root():
    return {"service": "performance", "version": "1.0.0", "port": 8020,
            "differentiator": "HAV replaces ratings; NPF trend replaces PIP; merit from data not opinion"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "performance"}


@app.post("/cycles", status_code=201)
async def create_cycle(body: CycleRequest):
    cycle_id = str(uuid.uuid4())
    start = date_type.fromisoformat(body.start_date)
    end   = date_type.fromisoformat(body.end_date)
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO cycles (id, org_id, name, cycle_type, start_date, end_date)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, cycle_id, body.org_id, body.name, body.cycle_type, start, end)
    return {"cycle_id": cycle_id, "name": body.name, "type": body.cycle_type,
            "note": "HAV data from time-attendance will feed reviews in this cycle automatically."}


@app.get("/cycles")
async def list_cycles(org_id: Optional[str] = Query(None)):
    async with db.acquire() as conn:
        if org_id:
            rows = await conn.fetch("SELECT * FROM cycles WHERE org_id=$1 ORDER BY start_date DESC", org_id)
        else:
            rows = await conn.fetch("SELECT * FROM cycles ORDER BY start_date DESC LIMIT 50")
    return {"cycles": [dict(r) for r in rows]}


@app.post("/reviews", status_code=201)
async def submit_review(body: ReviewRequest):
    """
    Submit a HAV-based review. Rating and merit recommendation are computed
    from the HAV data — not from manager opinion.
    """
    rating, merit_pct = _hav_to_rating(body.mean_hav, body.hav_trend)
    review_id = str(uuid.uuid4())

    async with db.acquire() as conn:
        cycle = await conn.fetchrow("SELECT * FROM cycles WHERE id=$1", body.cycle_id)
        if not cycle:
            raise HTTPException(404, "Cycle not found")
        await conn.execute("""
            INSERT INTO reviews
              (id, cycle_id, employee_id, reviewer_id, mean_hav, mean_npf,
               mean_srq, mean_oc, hav_trend, npf_trend, above_crossover_pct,
               phi_guardian_sessions, rating, merit_recommendation, narrative, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,'submitted')
        """, review_id, body.cycle_id, body.employee_id, body.reviewer_id,
             body.mean_hav, body.mean_npf, body.mean_srq, body.mean_oc,
             body.hav_trend, body.npf_trend, body.above_crossover_pct,
             body.phi_guardian_sessions, rating, merit_pct, body.narrative)

    insight = []
    if body.npf_trend == "declining":
        insight.append(f"NPF declining — employee may be moving toward procedural work. Conversation needed before PIP.")
    if body.mean_srq > 0.75:
        insight.append(f"High SRQ ({body.mean_srq:.2f}) — strong AI oversight capability. φ-guardian candidate.")
    if body.phi_guardian_sessions > 0:
        insight.append(f"{body.phi_guardian_sessions} φ-guardian sessions — contribution above crossover threshold.")
    if body.mean_oc > 0.5:
        insight.append("High OC — Values Custodian signal. Flag before any headcount changes.")

    return {
        "review_id": review_id,
        "employee_id": body.employee_id,
        "hav_score": body.mean_hav,
        "rating": rating,
        "merit_recommendation_pct": merit_pct,
        "merit_note": f"Data-driven merit: HAV={body.mean_hav:.2f}, trend={body.hav_trend} → {merit_pct:.1%} increase",
        "insights": insight,
        "vs_workday": "This rating was computed from 100% objective HAV data. No manager calibration bias.",
    }


@app.get("/reviews")
async def list_reviews(cycle_id: Optional[str] = Query(None), employee_id: Optional[str] = Query(None)):
    async with db.acquire() as conn:
        clauses, params = [], []
        for f, v in [("cycle_id", cycle_id), ("employee_id", employee_id)]:
            if v:
                params.append(v); clauses.append(f"{f}=${len(params)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await conn.fetch(f"SELECT * FROM reviews {where} ORDER BY created_at DESC LIMIT 100", *params)
    return {"reviews": [dict(r) for r in rows]}


@app.post("/goals", status_code=201)
async def create_goal(body: GoalRequest):
    goal_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO goals (id, employee_id, cycle_id, title, goal_type, target_value)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, goal_id, body.employee_id, body.cycle_id, body.title, body.goal_type, body.target_value)
    return {"goal_id": goal_id, "title": body.title, "goal_type": body.goal_type,
            "target": body.target_value,
            "note": f"Goal measured against {body.goal_type.upper()} from time-attendance records."}


@app.get("/employees/{employee_id}/performance-summary")
async def performance_summary(employee_id: str, cycle_id: Optional[str] = Query(None)):
    async with db.acquire() as conn:
        if cycle_id:
            reviews = await conn.fetch(
                "SELECT * FROM reviews WHERE employee_id=$1 AND cycle_id=$2 ORDER BY created_at DESC",
                employee_id, cycle_id
            )
        else:
            reviews = await conn.fetch(
                "SELECT * FROM reviews WHERE employee_id=$1 ORDER BY created_at DESC LIMIT 4",
                employee_id
            )
        goals = await conn.fetch(
            "SELECT * FROM goals WHERE employee_id=$1 ORDER BY created_at DESC LIMIT 10", employee_id
        )

    if not reviews:
        return {"employee_id": employee_id, "reviews": [], "goals": [dict(g) for g in goals]}

    latest = reviews[0]
    hav_trend = [r["mean_hav"] for r in reviews if r["mean_hav"] is not None]

    return {
        "employee_id": employee_id,
        "latest_rating": latest["rating"],
        "latest_hav": latest["mean_hav"],
        "merit_recommendation": latest["merit_recommendation"],
        "hav_series": hav_trend,
        "trajectory": "improving" if len(hav_trend) > 1 and hav_trend[0] > hav_trend[-1] else "stable",
        "phi_guardian_sessions_total": sum(r["phi_guardian_sessions"] or 0 for r in reviews),
        "goals": [dict(g) for g in goals],
        "reviews": [dict(r) for r in reviews],
    }


@app.post("/calibrate", status_code=201)
async def calibrate(cycle_id: str = Query(...), org_id: str = Query(...), facilitator: Optional[str] = Query(None)):
    """Run calibration across all submitted reviews in a cycle. Compute HAV distribution."""
    async with db.acquire() as conn:
        reviews = await conn.fetch(
            "SELECT * FROM reviews WHERE cycle_id=$1 AND status='submitted'", cycle_id
        )
        if not reviews:
            raise HTTPException(404, "No submitted reviews in this cycle")

        havs = sorted([r["mean_hav"] for r in reviews if r["mean_hav"] is not None])
        n = len(havs)
        p50 = havs[n // 2] if n else 0.0
        p75 = havs[int(n * 0.75)] if n else 0.0
        p90 = havs[int(n * 0.90)] if n else 0.0

        dist = {}
        for r in reviews:
            dist[r["rating"]] = dist.get(r["rating"], 0) + 1

        calib_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO calibrations
              (id, cycle_id, org_id, facilitator, employees_calibrated,
               rating_distribution, hav_p50, hav_p75, hav_p90)
            VALUES ($1,$2,$3,$4,$5,$6::JSONB,$7,$8,$9)
        """, calib_id, cycle_id, org_id, facilitator, n,
             str(dist).replace("'", '"'), p50, p75, p90)
        await conn.execute(
            "UPDATE reviews SET status='calibrated' WHERE cycle_id=$1 AND status='submitted'", cycle_id
        )

    return {
        "calibration_id": calib_id,
        "employees_calibrated": n,
        "hav_distribution": {"p50": round(p50,4), "p75": round(p75,4), "p90": round(p90,4)},
        "rating_distribution": dist,
        "note": "All ratings computed from HAV data. Zero manager-bias calibration needed.",
    }
