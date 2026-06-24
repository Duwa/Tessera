"""
Tessera Compensation Engine  —  port 8022
==========================================
Full alignment premium engine. Merit cycles from HAV, not manager opinion.
Total comp = Salary + Token Budget + (r_AP × HAV × Salary).

Workday does: salary bands → merit matrix → stock → total rewards.
Tessera adds:  alignment premium tier (5%→25%), HAV-driven merit, token
               budget as a distinct comp component, φ-crossover premium.
"""
from __future__ import annotations
import os, uuid, asyncpg
from datetime import date as date_type
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_compensation")
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS salary_bands (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    job_family  TEXT NOT NULL,
    level       TEXT NOT NULL,
    min_salary  FLOAT NOT NULL,
    mid_salary  FLOAT NOT NULL,
    max_salary  FLOAT NOT NULL,
    token_budget_target FLOAT DEFAULT 0.0,
    effective_date DATE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bands_org ON salary_bands(org_id);

CREATE TABLE IF NOT EXISTS compensation_records (
    id                    TEXT PRIMARY KEY,
    employee_id           TEXT NOT NULL,
    org_id                TEXT,
    effective_date        DATE NOT NULL,
    base_salary           FLOAT NOT NULL,
    token_budget          FLOAT DEFAULT 0.0,
    mean_hav              FLOAT,
    phi                   FLOAT,
    phi_star              FLOAT,
    alignment_premium_rate FLOAT NOT NULL,
    alignment_premium_amt FLOAT NOT NULL,
    total_comp            FLOAT NOT NULL,
    is_above_crossover    BOOLEAN DEFAULT FALSE,
    cycle_id              TEXT,
    reason                TEXT,
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comp_employee ON compensation_records(employee_id);

CREATE TABLE IF NOT EXISTS merit_cycles (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    name            TEXT NOT NULL,
    effective_date  DATE NOT NULL,
    budget_pct      FLOAT NOT NULL,  -- % of total payroll allocated
    status          TEXT DEFAULT 'planning',  -- 'planning'|'in_review'|'approved'|'applied'
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merit_recommendations (
    id              TEXT PRIMARY KEY,
    cycle_id        TEXT NOT NULL REFERENCES merit_cycles(id),
    employee_id     TEXT NOT NULL,
    current_salary  FLOAT NOT NULL,
    mean_hav        FLOAT NOT NULL,
    hav_trend       TEXT DEFAULT 'stable',
    recommended_increase_pct FLOAT NOT NULL,
    recommended_new_salary FLOAT NOT NULL,
    new_ap_rate     FLOAT NOT NULL,
    new_total_comp  FLOAT NOT NULL,
    status          TEXT DEFAULT 'pending',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_merit_rec_cycle ON merit_recommendations(cycle_id);
"""

def _r_ap(mean_phi: float) -> float:
    if mean_phi < 0.25: return 0.05
    if mean_phi > 0.75: return 0.25
    return round(0.05 + (mean_phi - 0.25) * (0.20 / 0.50), 4)

def _merit_pct(mean_hav: float, hav_trend: str) -> float:
    if mean_hav >= 0.80 and hav_trend != "declining": return 0.08
    if mean_hav >= 0.65: return 0.055
    if mean_hav >= 0.50: return 0.035
    if mean_hav >= 0.35: return 0.01
    return 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
    yield
    await db.close()

app = FastAPI(title="Tessera Compensation", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class BandRequest(BaseModel):
    org_id: str
    job_family: str
    level: str
    min_salary: float
    mid_salary: float
    max_salary: float
    token_budget_target: float = 0.0
    effective_date: str

class CompRequest(BaseModel):
    employee_id: str
    org_id: Optional[str] = None
    effective_date: str
    base_salary: float
    token_budget: float = 0.0
    mean_hav: float = Field(..., ge=0.0, le=1.0)
    phi: Optional[float] = Field(None, ge=0.0, le=1.0)
    phi_star: Optional[float] = None
    cycle_id: Optional[str] = None
    reason: Optional[str] = None

class MeritCycleRequest(BaseModel):
    org_id: str
    name: str
    effective_date: str
    budget_pct: float = Field(..., gt=0.0, le=0.20)

class MeritRecommendationRequest(BaseModel):
    cycle_id: str
    employee_id: str
    current_salary: float
    mean_hav: float = Field(..., ge=0.0, le=1.0)
    hav_trend: str = "stable"
    token_budget: float = 0.0
    phi: Optional[float] = None


@app.get("/")
def root():
    return {"service": "compensation", "version": "1.0.0", "port": 8022,
            "differentiator": "Alignment premium engine; HAV-driven merit; token budget as first-class comp"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "compensation"}


@app.post("/bands", status_code=201)
async def create_band(body: BandRequest):
    band_id = str(uuid.uuid4())
    eff = date_type.fromisoformat(body.effective_date)
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO salary_bands
              (id, org_id, job_family, level, min_salary, mid_salary,
               max_salary, token_budget_target, effective_date)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, band_id, body.org_id, body.job_family, body.level,
             body.min_salary, body.mid_salary, body.max_salary,
             body.token_budget_target, eff)
    return {"band_id": band_id, "job_family": body.job_family, "level": body.level,
            "range": {"min": body.min_salary, "mid": body.mid_salary, "max": body.max_salary},
            "token_budget_target": body.token_budget_target}


@app.post("/records", status_code=201)
async def create_comp_record(body: CompRequest):
    """
    Record a compensation change. Alignment premium is computed from HAV + φ.
    Total comp = Salary + Token Budget + (r_AP × HAV × Salary).
    """
    phi_val   = body.phi or 0.0
    r_ap      = _r_ap(phi_val)
    ap_amt    = r_ap * body.mean_hav * body.base_salary
    total     = body.base_salary + body.token_budget + ap_amt
    above_x   = bool(body.phi and body.phi_star and body.phi > body.phi_star)

    rec_id = str(uuid.uuid4())
    eff = date_type.fromisoformat(body.effective_date)
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO compensation_records
              (id, employee_id, org_id, effective_date, base_salary, token_budget,
               mean_hav, phi, phi_star, alignment_premium_rate, alignment_premium_amt,
               total_comp, is_above_crossover, cycle_id, reason)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        """, rec_id, body.employee_id, body.org_id, eff,
             body.base_salary, body.token_budget, body.mean_hav, body.phi,
             body.phi_star, r_ap, ap_amt, total, above_x, body.cycle_id, body.reason)

    return {
        "record_id": rec_id,
        "employee_id": body.employee_id,
        "effective_date": body.effective_date,
        "compensation": {
            "base_salary": body.base_salary,
            "token_budget": body.token_budget,
            "alignment_premium_rate": r_ap,
            "alignment_premium_amt": round(ap_amt, 2),
            "total_comp": round(total, 2),
        },
        "formula": "Total = Base + Token Budget + (r_AP × HAV × Base)",
        "is_above_crossover": above_x,
        "phi_premium_context": (
            f"Employee is above φ* crossover — r_AP={r_ap:.0%} at φ={phi_val:.2f}. "
            "High alignment premium signals they're working in the non-procedural zone."
            if above_x else
            f"r_AP={r_ap:.0%} based on φ={phi_val:.2f}."
        ),
    }


@app.get("/employees/{employee_id}/compensation")
async def employee_comp(employee_id: str):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM compensation_records WHERE employee_id=$1 ORDER BY effective_date DESC LIMIT 6",
            employee_id
        )
    if not rows:
        raise HTTPException(404, "No compensation records found")
    latest = rows[0]
    return {
        "employee_id": employee_id,
        "current": dict(latest),
        "history": [dict(r) for r in rows],
        "ap_trend": [r["alignment_premium_rate"] for r in rows],
        "total_comp_trend": [r["total_comp"] for r in rows],
    }


@app.post("/merit-cycles", status_code=201)
async def create_merit_cycle(body: MeritCycleRequest):
    cycle_id = str(uuid.uuid4())
    eff = date_type.fromisoformat(body.effective_date)
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO merit_cycles (id, org_id, name, effective_date, budget_pct)
            VALUES ($1,$2,$3,$4,$5)
        """, cycle_id, body.org_id, body.name, eff, body.budget_pct)
    return {"cycle_id": cycle_id, "name": body.name,
            "budget_pct": body.budget_pct,
            "note": "Merit recommendations in this cycle are computed from HAV data — no manager input required."}


@app.post("/merit-recommendations", status_code=201)
async def recommend_merit(body: MeritRecommendationRequest):
    async with db.acquire() as conn:
        cycle = await conn.fetchrow("SELECT * FROM merit_cycles WHERE id=$1", body.cycle_id)
        if not cycle:
            raise HTTPException(404, "Merit cycle not found")

        pct    = _merit_pct(body.mean_hav, body.hav_trend)
        new_sal = round(body.current_salary * (1 + pct), 2)
        phi_val = body.phi or 0.0
        r_ap    = _r_ap(phi_val)
        ap_amt  = r_ap * body.mean_hav * new_sal
        new_total = new_sal + body.token_budget + ap_amt

        rec_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO merit_recommendations
              (id, cycle_id, employee_id, current_salary, mean_hav, hav_trend,
               recommended_increase_pct, recommended_new_salary, new_ap_rate, new_total_comp)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """, rec_id, body.cycle_id, body.employee_id, body.current_salary,
             body.mean_hav, body.hav_trend, pct, new_sal, r_ap, new_total)

    return {
        "recommendation_id": rec_id,
        "employee_id": body.employee_id,
        "merit": {
            "current_salary": body.current_salary,
            "increase_pct": pct,
            "new_base_salary": new_sal,
            "new_ap_rate": r_ap,
            "new_alignment_premium": round(ap_amt, 2),
            "new_total_comp": round(new_total, 2),
        },
        "rationale": f"HAV={body.mean_hav:.2f}, trend={body.hav_trend} → {pct:.1%} merit increase. Zero manager bias.",
    }


@app.get("/merit-cycles/{cycle_id}/recommendations")
async def cycle_recommendations(cycle_id: str):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM merit_recommendations WHERE cycle_id=$1 ORDER BY mean_hav DESC", cycle_id
        )
    total_increase = sum(r["recommended_new_salary"] - r["current_salary"] for r in rows)
    return {
        "cycle_id": cycle_id,
        "recommendations": [dict(r) for r in rows],
        "summary": {
            "count": len(rows),
            "total_salary_increase": round(total_increase, 2),
            "avg_increase_pct": round(
                sum(r["recommended_increase_pct"] for r in rows) / len(rows) if rows else 0, 4
            ),
        },
    }
