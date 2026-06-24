"""
Tessera Recruiting / ATS  —  port 8019
=======================================
HAV-native hiring pipeline. Every requisition specifies the φ-role it fills:
is this a standard hire, a φ-guardian, or a Values Custodian replacement?

Workday does: req → application → interview → offer → hire.
Tessera adds:  HAV-potential scoring, φ-role targeting, AI-human collaboration
               tracking in the hiring process itself.
"""
from __future__ import annotations
import os, uuid, asyncpg
from datetime import datetime, timezone
from typing import Optional, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_recruiting")
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS requisitions (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    title           TEXT NOT NULL,
    department      TEXT,
    phi_role        TEXT DEFAULT 'standard',  -- 'standard'|'phi_guardian'|'values_custodian'
    min_hav_target  FLOAT DEFAULT 0.0,
    min_npf_target  FLOAT DEFAULT 0.0,
    phi_context     FLOAT,   -- current org φ when req was opened
    headcount       INT DEFAULT 1,
    status          TEXT DEFAULT 'open',  -- 'open'|'filled'|'cancelled'
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS candidates (
    id              TEXT PRIMARY KEY,
    requisition_id  TEXT NOT NULL REFERENCES requisitions(id),
    name            TEXT NOT NULL,
    email           TEXT,
    hav_potential   FLOAT,  -- scored during process
    npf_potential   FLOAT,
    srq_potential   FLOAT,
    oc_potential    FLOAT,
    ai_human_collab_score FLOAT,  -- how well candidate works alongside AI
    stage           TEXT DEFAULT 'applied',  -- 'applied'|'screening'|'interview'|'offer'|'hired'|'rejected'
    rejection_reason TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_candidates_req ON candidates(requisition_id);
CREATE INDEX IF NOT EXISTS idx_candidates_stage ON candidates(stage);

CREATE TABLE IF NOT EXISTS interviews (
    id              TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL REFERENCES candidates(id),
    interviewer_id  TEXT,
    interview_type  TEXT DEFAULT 'structured',  -- 'structured'|'hav_simulation'|'phi_guardian_test'
    npf_observed    FLOAT,
    srq_observed    FLOAT,
    oc_observed     FLOAT,
    overall_score   FLOAT,
    notes           TEXT,
    conducted_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS offers (
    id              TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL REFERENCES candidates(id),
    requisition_id  TEXT NOT NULL REFERENCES requisitions(id),
    base_salary     FLOAT NOT NULL,
    token_budget    FLOAT DEFAULT 0.0,
    alignment_premium_rate FLOAT DEFAULT 0.05,
    total_comp_est  FLOAT,
    status          TEXT DEFAULT 'pending',  -- 'pending'|'accepted'|'declined'|'expired'
    created_at      TIMESTAMPTZ DEFAULT NOW()
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

app = FastAPI(title="Tessera Recruiting", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class RequisitionRequest(BaseModel):
    org_id: str
    title: str
    department: Optional[str] = None
    phi_role: str = Field("standard", description="'standard'|'phi_guardian'|'values_custodian'")
    min_hav_target: float = Field(0.0, ge=0.0, le=1.0)
    min_npf_target: float = Field(0.0, ge=0.0, le=1.0)
    phi_context: Optional[float] = None
    headcount: int = Field(1, ge=1)

class CandidateRequest(BaseModel):
    requisition_id: str
    name: str
    email: Optional[str] = None

class ScoreRequest(BaseModel):
    npf_potential: float = Field(..., ge=0.0, le=1.0)
    srq_potential: float = Field(..., ge=0.0, le=1.0)
    oc_potential: float = Field(..., ge=0.0, le=1.0)
    ai_human_collab_score: float = Field(..., ge=0.0, le=1.0)

class InterviewRequest(BaseModel):
    interviewer_id: Optional[str] = None
    interview_type: str = "structured"
    npf_observed: float = Field(..., ge=0.0, le=1.0)
    srq_observed: float = Field(..., ge=0.0, le=1.0)
    oc_observed: float = Field(..., ge=0.0, le=1.0)
    notes: Optional[str] = None

class OfferRequest(BaseModel):
    base_salary: float
    token_budget: float = 0.0
    alignment_premium_rate: float = Field(0.05, ge=0.0, le=0.25)


@app.get("/")
def root():
    return {"service": "recruiting", "version": "1.0.0", "port": 8019,
            "differentiator": "HAV-potential scoring; φ-role targeting; alignment premium in offer letter"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "recruiting"}


@app.post("/requisitions", status_code=201)
async def create_req(body: RequisitionRequest):
    req_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO requisitions
              (id, org_id, title, department, phi_role, min_hav_target,
               min_npf_target, phi_context, headcount)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, req_id, body.org_id, body.title, body.department, body.phi_role,
             body.min_hav_target, body.min_npf_target, body.phi_context, body.headcount)

    return {
        "requisition_id": req_id,
        "title": body.title,
        "phi_role": body.phi_role,
        "min_hav_target": body.min_hav_target,
        "screening_criteria": {
            "min_hav_potential": body.min_hav_target,
            "min_npf_potential": body.min_npf_target,
            "note": (
                f"φ-guardian role. Candidates must demonstrate SRQ capability and "
                f"novel framing (OC) above φ*. AI-human collaboration score critical."
                if body.phi_role in ("phi_guardian", "values_custodian") else
                "Standard role. HAV potential is a positive signal, not a gate."
            ),
        },
    }


@app.get("/requisitions")
async def list_reqs(org_id: Optional[str] = Query(None), status: str = Query("open")):
    async with db.acquire() as conn:
        if org_id:
            rows = await conn.fetch(
                "SELECT * FROM requisitions WHERE org_id=$1 AND status=$2 ORDER BY created_at DESC",
                org_id, status
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM requisitions WHERE status=$1 ORDER BY created_at DESC", status
            )
    return {"requisitions": [dict(r) for r in rows]}


@app.get("/candidates")
async def list_candidates(requisition_id: Optional[str] = Query(None), org_id: Optional[str] = Query(None)):
    async with db.acquire() as conn:
        if requisition_id:
            rows = await conn.fetch(
                "SELECT c.*, r.title as req_title, r.phi_role FROM candidates c "
                "JOIN requisitions r ON c.requisition_id=r.id "
                "WHERE c.requisition_id=$1 ORDER BY c.hav_potential DESC NULLS LAST",
                requisition_id
            )
        elif org_id:
            rows = await conn.fetch(
                "SELECT c.*, r.title as req_title, r.phi_role FROM candidates c "
                "JOIN requisitions r ON c.requisition_id=r.id "
                "WHERE r.org_id=$1 ORDER BY c.hav_potential DESC NULLS LAST",
                org_id
            )
        else:
            rows = await conn.fetch(
                "SELECT c.*, r.title as req_title, r.phi_role FROM candidates c "
                "JOIN requisitions r ON c.requisition_id=r.id "
                "ORDER BY c.hav_potential DESC NULLS LAST LIMIT 100"
            )
    return {"candidates": [dict(r) for r in rows], "total": len(rows)}


@app.post("/candidates", status_code=201)
async def apply(body: CandidateRequest):
    async with db.acquire() as conn:
        req = await conn.fetchrow("SELECT * FROM requisitions WHERE id=$1", body.requisition_id)
        if not req:
            raise HTTPException(404, "Requisition not found")
        if req["status"] != "open":
            raise HTTPException(409, "Requisition is not open")
        cand_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO candidates (id, requisition_id, name, email)
            VALUES ($1,$2,$3,$4)
        """, cand_id, body.requisition_id, body.name, body.email)

    return {"candidate_id": cand_id, "name": body.name,
            "stage": "applied", "requisition": req["title"]}


@app.post("/candidates/{candidate_id}/score")
async def score_candidate(candidate_id: str, body: ScoreRequest):
    """Score candidate HAV potential. Gates φ-guardian roles."""
    async with db.acquire() as conn:
        cand = await conn.fetchrow(
            "SELECT c.*, r.phi_role, r.min_hav_target, r.min_npf_target "
            "FROM candidates c JOIN requisitions r ON c.requisition_id=r.id "
            "WHERE c.id=$1", candidate_id
        )
        if not cand:
            raise HTTPException(404, "Candidate not found")

        hav_potential = round(0.50*body.npf_potential + 0.30*body.srq_potential + 0.20*body.oc_potential, 4)
        await conn.execute("""
            UPDATE candidates SET
                hav_potential=$1, npf_potential=$2, srq_potential=$3,
                oc_potential=$4, ai_human_collab_score=$5, stage='screening'
            WHERE id=$6
        """, hav_potential, body.npf_potential, body.srq_potential,
             body.oc_potential, body.ai_human_collab_score, candidate_id)

    passes_hav  = hav_potential >= cand["min_hav_target"]
    passes_npf  = body.npf_potential >= cand["min_npf_target"]
    advances    = passes_hav and passes_npf

    return {
        "candidate_id": candidate_id,
        "hav_potential": hav_potential,
        "breakdown": {
            "npf_potential": body.npf_potential,
            "srq_potential": body.srq_potential,
            "oc_potential":  body.oc_potential,
            "ai_human_collab_score": body.ai_human_collab_score,
        },
        "phi_role": cand["phi_role"],
        "passes_screening": advances,
        "recommendation": (
            "Advance to interview" if advances else
            f"Does not meet {cand['phi_role']} minimum HAV={cand['min_hav_target']:.2f}. "
            "Consider for standard role."
        ),
    }


@app.post("/candidates/{candidate_id}/interview", status_code=201)
async def add_interview(candidate_id: str, body: InterviewRequest):
    async with db.acquire() as conn:
        cand = await conn.fetchrow("SELECT id FROM candidates WHERE id=$1", candidate_id)
        if not cand:
            raise HTTPException(404, "Candidate not found")
        iv_id = str(uuid.uuid4())
        overall = round(0.50*body.npf_observed + 0.30*body.srq_observed + 0.20*body.oc_observed, 4)
        await conn.execute("""
            INSERT INTO interviews
              (id, candidate_id, interviewer_id, interview_type, npf_observed,
               srq_observed, oc_observed, overall_score, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, iv_id, candidate_id, body.interviewer_id, body.interview_type,
             body.npf_observed, body.srq_observed, body.oc_observed, overall, body.notes)
        await conn.execute("UPDATE candidates SET stage='interview' WHERE id=$1", candidate_id)

    return {"interview_id": iv_id, "overall_score": overall,
            "recommendation": "Strong φ-guardian candidate" if overall >= 0.70 else "Continue evaluating"}


@app.post("/candidates/{candidate_id}/offer", status_code=201)
async def make_offer(candidate_id: str, body: OfferRequest):
    """Issue offer with HAV-based alignment premium built in from day one."""
    async with db.acquire() as conn:
        cand = await conn.fetchrow(
            "SELECT c.*, r.id AS req_id FROM candidates c JOIN requisitions r ON c.requisition_id=r.id WHERE c.id=$1",
            candidate_id
        )
        if not cand:
            raise HTTPException(404, "Candidate not found")

        hav = cand["hav_potential"] or 0.0
        ap_amount = body.alignment_premium_rate * hav * body.base_salary
        total_comp = body.base_salary + body.token_budget + ap_amount

        offer_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO offers
              (id, candidate_id, requisition_id, base_salary, token_budget,
               alignment_premium_rate, total_comp_est)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, offer_id, candidate_id, cand["req_id"], body.base_salary,
             body.token_budget, body.alignment_premium_rate, total_comp)
        await conn.execute("UPDATE candidates SET stage='offer' WHERE id=$1", candidate_id)

    return {
        "offer_id": offer_id,
        "candidate_id": candidate_id,
        "compensation": {
            "base_salary": body.base_salary,
            "token_budget": body.token_budget,
            "alignment_premium": round(ap_amount, 2),
            "alignment_premium_rate": body.alignment_premium_rate,
            "total_comp_estimated": round(total_comp, 2),
            "formula": "Salary + Token Budget + (r_AP × HAV × Salary)",
        },
        "note": "Alignment premium grows as φ rises — candidate participates in AI value creation.",
    }


@app.post("/candidates/{candidate_id}/hire")
async def hire(candidate_id: str, employee_id: str = Query(...)):
    async with db.acquire() as conn:
        cand = await conn.fetchrow(
            "SELECT c.*, r.id AS req_id FROM candidates c JOIN requisitions r ON c.requisition_id=r.id WHERE c.id=$1",
            candidate_id
        )
        if not cand:
            raise HTTPException(404, "Candidate not found")
        await conn.execute("UPDATE candidates SET stage='hired' WHERE id=$1", candidate_id)
        await conn.execute(
            "UPDATE requisitions SET status='filled' WHERE id=$1", cand["req_id"]
        )
    return {"status": "hired", "candidate_id": candidate_id, "employee_id": employee_id,
            "hav_potential": cand["hav_potential"],
            "message": "Employee record ready. Sync HAV baseline to time-attendance service."}
