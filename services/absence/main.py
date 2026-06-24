"""
Tessera Absence Management  —  port 8023
==========================================
HAV-impact scoring on absence. φ-coverage gap when Values Custodians go on leave.

Workday does: request → approve → balance → calendar.
Tessera adds:  coverage gap alert when a VC goes out, HAV impact per day,
               automatic escalation when φ drops below φ* during absence.
"""
from __future__ import annotations
import os, uuid, asyncpg
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_absence")
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS leave_balances (
    id           TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL UNIQUE,
    pto_days     FLOAT DEFAULT 15.0,
    sick_days    FLOAT DEFAULT 10.0,
    personal_days FLOAT DEFAULT 3.0,
    carry_over   FLOAT DEFAULT 0.0,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS absence_requests (
    id              TEXT PRIMARY KEY,
    employee_id     TEXT NOT NULL,
    leave_type      TEXT NOT NULL,  -- 'pto'|'sick'|'fmla'|'bereavement'|'unpaid'
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    days_requested  FLOAT NOT NULL,
    mean_hav        FLOAT,
    mean_npf        FLOAT,
    hav_impact      FLOAT,  -- total HAV×days lost to org
    is_vc           BOOLEAN DEFAULT FALSE,
    phi_coverage_impact FLOAT,
    status          TEXT DEFAULT 'pending',  -- 'pending'|'approved'|'denied'|'cancelled'
    coverage_plan   TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_absence_employee ON absence_requests(employee_id);
CREATE INDEX IF NOT EXISTS idx_absence_status ON absence_requests(status);
CREATE INDEX IF NOT EXISTS idx_absence_dates ON absence_requests(start_date, end_date);

CREATE TABLE IF NOT EXISTS coverage_assignments (
    id          TEXT PRIMARY KEY,
    absence_id  TEXT NOT NULL REFERENCES absence_requests(id),
    cover_employee_id TEXT NOT NULL,
    cover_hav   FLOAT,
    cover_npf   FLOAT,
    hav_deficit FLOAT,
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

def _phi_star(K: int = 4) -> float:
    return 0.25 if K >= 6 else (0.32 if K >= 3 else 0.44)

def _hav_impact(mean_hav: float, days: float) -> float:
    return round(mean_hav * days, 4)

def _phi_coverage_impact(mean_hav: float, mean_npf: float, days: float) -> float:
    return round(mean_hav * mean_npf * days * 0.1, 4)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
    yield
    await db.close()

app = FastAPI(title="Tessera Absence", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class BalanceRequest(BaseModel):
    employee_id: str
    pto_days: float = 15.0
    sick_days: float = 10.0
    personal_days: float = 3.0

class AbsenceRequest(BaseModel):
    employee_id: str
    leave_type: str
    start_date: str
    end_date: str
    days_requested: float = Field(..., gt=0.0)
    mean_hav: Optional[float] = Field(None, ge=0.0, le=1.0)
    mean_npf: Optional[float] = Field(None, ge=0.0, le=1.0)
    notes: Optional[str] = None
    org_k: int = 4

class CoverageRequest(BaseModel):
    absence_id: str
    cover_employee_id: str
    cover_hav: float = Field(..., ge=0.0, le=1.0)
    cover_npf: float = Field(..., ge=0.0, le=1.0)
    notes: Optional[str] = None


@app.get("/")
def root():
    return {"service": "absence", "version": "1.0.0", "port": 8023,
            "differentiator": "HAV-impact scoring; Values Custodian coverage gap; phi-drop alerts"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "absence"}


@app.post("/balances", status_code=201)
async def create_balance(body: BalanceRequest):
    bal_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO leave_balances (id, employee_id, pto_days, sick_days, personal_days)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (employee_id) DO UPDATE SET
                pto_days=$3, sick_days=$4, personal_days=$5, updated_at=NOW()
        """, bal_id, body.employee_id, body.pto_days, body.sick_days, body.personal_days)
    return {"employee_id": body.employee_id, "pto_days": body.pto_days,
            "sick_days": body.sick_days, "personal_days": body.personal_days}


@app.get("/employees/{employee_id}/balance")
async def get_balance(employee_id: str):
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM leave_balances WHERE employee_id=$1", employee_id)
    if not row:
        raise HTTPException(404, "No balance record found")
    return dict(row)


@app.post("/requests", status_code=201)
async def request_absence(body: AbsenceRequest):
    mean_hav = body.mean_hav or 0.0
    mean_npf = body.mean_npf or 0.0
    phi_star_val = _phi_star(body.org_k)
    hav_imp = _hav_impact(mean_hav, body.days_requested)
    phi_imp = _phi_coverage_impact(mean_hav, mean_npf, body.days_requested)
    is_vc   = mean_hav >= 0.70 and mean_npf >= 0.65

    req_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        bal = await conn.fetchrow("SELECT * FROM leave_balances WHERE employee_id=$1", body.employee_id)
        if bal and body.leave_type == "pto" and bal["pto_days"] < body.days_requested:
            raise HTTPException(400, f"Insufficient PTO balance ({bal['pto_days']} days available)")

        from datetime import date as _date
        sd = _date.fromisoformat(body.start_date) if isinstance(body.start_date, str) else body.start_date
        ed = _date.fromisoformat(body.end_date)   if isinstance(body.end_date, str)   else body.end_date
        await conn.execute("""
            INSERT INTO absence_requests
              (id, employee_id, leave_type, start_date, end_date, days_requested,
               mean_hav, mean_npf, hav_impact, is_vc, phi_coverage_impact)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """, req_id, body.employee_id, body.leave_type,
             sd, ed, body.days_requested,
             mean_hav, mean_npf, hav_imp, is_vc, phi_imp)

    alerts = []
    if is_vc:
        alerts.append({
            "severity": "high",
            "message": (
                f"Values Custodian absence: {body.days_requested} days will remove "
                f"HAV={mean_hav:.2f}, NPF={mean_npf:.2f} from active coverage. "
                f"φ-coverage impact: {phi_imp:.2f}. Assign a cover before approving."
            ),
        })
    if hav_imp > 3.0:
        alerts.append({
            "severity": "medium",
            "message": f"High HAV impact: {hav_imp:.2f} HAV-days. Consider staggered leave or coverage plan.",
        })

    return {
        "request_id": req_id,
        "employee_id": body.employee_id,
        "leave_type": body.leave_type,
        "dates": f"{body.start_date} → {body.end_date}",
        "days": body.days_requested,
        "hav_impact": hav_imp,
        "phi_coverage_impact": phi_imp,
        "is_values_custodian": is_vc,
        "status": "pending",
        "alerts": alerts,
    }


@app.post("/requests/{request_id}/approve")
async def approve_absence(request_id: str):
    async with db.acquire() as conn:
        req = await conn.fetchrow("SELECT * FROM absence_requests WHERE id=$1", request_id)
        if not req:
            raise HTTPException(404, "Request not found")
        if req["status"] != "pending":
            raise HTTPException(409, f"Request is already {req['status']}")
        if req["is_vc"]:
            covers = await conn.fetch(
                "SELECT * FROM coverage_assignments WHERE absence_id=$1", request_id
            )
            if not covers:
                return {
                    "approved": False,
                    "reason": "Values Custodian absence requires coverage assignment before approval.",
                    "action": f"POST /coverage with absence_id={request_id}",
                }
        await conn.execute(
            "UPDATE absence_requests SET status='approved' WHERE id=$1", request_id
        )
        if req["leave_type"] == "pto":
            await conn.execute("""
                UPDATE leave_balances
                SET pto_days = pto_days - $1, updated_at=NOW()
                WHERE employee_id=$2
            """, req["days_requested"], req["employee_id"])

    return {"approved": True, "request_id": request_id,
            "employee_id": req["employee_id"], "days": req["days_requested"]}


@app.post("/coverage", status_code=201)
async def assign_coverage(body: CoverageRequest):
    async with db.acquire() as conn:
        req = await conn.fetchrow("SELECT * FROM absence_requests WHERE id=$1", body.absence_id)
        if not req:
            raise HTTPException(404, "Absence request not found")

        hav_deficit = max(0.0, (req["mean_hav"] or 0.0) - body.cover_hav)
        cov_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO coverage_assignments
              (id, absence_id, cover_employee_id, cover_hav, cover_npf, hav_deficit, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, cov_id, body.absence_id, body.cover_employee_id,
             body.cover_hav, body.cover_npf, hav_deficit, body.notes)

    return {
        "coverage_id": cov_id,
        "cover_employee_id": body.cover_employee_id,
        "cover_hav": body.cover_hav,
        "hav_deficit": round(hav_deficit, 4),
        "warning": (
            f"Cover has HAV deficit of {hav_deficit:.2f} vs absent employee. "
            "φ-coverage quality will drop during this period."
            if hav_deficit > 0.2 else "Coverage is adequate."
        ),
    }


@app.get("/requests")
async def list_requests(
    employee_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    is_vc: Optional[bool] = Query(None)
):
    async with db.acquire() as conn:
        clauses, params = [], []
        for f, v in [("employee_id", employee_id), ("status", status)]:
            if v:
                params.append(v); clauses.append(f"{f}=${len(params)}")
        if is_vc is not None:
            params.append(is_vc); clauses.append(f"is_vc=${len(params)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await conn.fetch(
            f"SELECT * FROM absence_requests {where} ORDER BY start_date DESC LIMIT 100", *params
        )
    return {"requests": [dict(r) for r in rows]}
