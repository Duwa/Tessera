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
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    description     TEXT,
    skills_required TEXT,   -- JSON array
    location        TEXT DEFAULT 'Remote · USA',
    employment_type TEXT DEFAULT 'Full-time',
    salary_min      FLOAT,
    salary_max      FLOAT
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

MIGRATE = """
ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS skills_required TEXT;
ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Remote · USA';
ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS employment_type TEXT DEFAULT 'Full-time';
ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS salary_min FLOAT;
ALTER TABLE requisitions ADD COLUMN IF NOT EXISTS salary_max FLOAT;
"""

# Learning paths per skill — partner integrations
LEARNING_PATHS = {
    "Python": [
        {"title": "Python for Everybody", "partner": "Coursera", "duration": "8 weeks", "level": "Beginner"},
        {"title": "Complete Python Bootcamp", "partner": "Udemy", "duration": "22 hrs", "level": "Beginner"},
    ],
    "FastAPI": [
        {"title": "FastAPI Full Course", "partner": "Udemy", "duration": "6 hrs", "level": "Intermediate"},
        {"title": "Building APIs with FastAPI", "partner": "Pluralsight", "duration": "3 hrs", "level": "Intermediate"},
    ],
    "Docker": [
        {"title": "Docker Mastery", "partner": "Udemy", "duration": "19 hrs", "level": "Intermediate"},
        {"title": "Docker for Developers", "partner": "Pluralsight", "duration": "4 hrs", "level": "Beginner"},
    ],
    "Kubernetes": [
        {"title": "Kubernetes for Developers", "partner": "Coursera", "duration": "6 weeks", "level": "Advanced"},
        {"title": "Certified Kubernetes Application Developer", "partner": "Linux Foundation", "duration": "Self-paced", "level": "Advanced"},
    ],
    "PostgreSQL": [
        {"title": "Learn PostgreSQL", "partner": "Udemy", "duration": "8 hrs", "level": "Beginner"},
        {"title": "PostgreSQL for Developers", "partner": "Pluralsight", "duration": "5 hrs", "level": "Intermediate"},
    ],
    "Petri Nets": [
        {"title": "Formal Methods in Software Engineering", "partner": "Coursera", "duration": "10 weeks", "level": "Advanced"},
        {"title": "Process Mining & CPN", "partner": "edX", "duration": "8 weeks", "level": "Advanced"},
    ],
    "Machine Learning": [
        {"title": "Machine Learning Specialization", "partner": "Coursera", "duration": "3 months", "level": "Intermediate"},
        {"title": "Practical ML with Python", "partner": "Udemy", "duration": "24 hrs", "level": "Intermediate"},
    ],
    "React": [
        {"title": "React - The Complete Guide", "partner": "Udemy", "duration": "49 hrs", "level": "Beginner"},
        {"title": "React Fundamentals", "partner": "Pluralsight", "duration": "4 hrs", "level": "Beginner"},
    ],
    "TypeScript": [
        {"title": "Understanding TypeScript", "partner": "Udemy", "duration": "15 hrs", "level": "Intermediate"},
        {"title": "TypeScript Fundamentals", "partner": "Pluralsight", "duration": "3 hrs", "level": "Beginner"},
    ],
    "Workday": [
        {"title": "Workday HCM Fundamentals", "partner": "Workday Learning", "duration": "40 hrs", "level": "Beginner"},
        {"title": "Workday Reporting", "partner": "Dice Learning", "duration": "12 hrs", "level": "Intermediate"},
    ],
    "SAP SuccessFactors": [
        {"title": "SAP SuccessFactors HCM", "partner": "SAP Learning Hub", "duration": "Self-paced", "level": "Intermediate"},
        {"title": "SuccessFactors Implementation", "partner": "Udemy", "duration": "10 hrs", "level": "Advanced"},
    ],
    "AI Governance": [
        {"title": "Responsible AI Practices", "partner": "Coursera", "duration": "4 weeks", "level": "Intermediate"},
        {"title": "AI Ethics & Governance", "partner": "edX", "duration": "6 weeks", "level": "Intermediate"},
    ],
    "Data Science": [
        {"title": "Data Science Professional Certificate", "partner": "Coursera", "duration": "10 months", "level": "Beginner"},
        {"title": "Applied Data Science", "partner": "edX", "duration": "3 months", "level": "Intermediate"},
    ],
    "Product Management": [
        {"title": "Product Management First Steps", "partner": "LinkedIn Learning", "duration": "2 hrs", "level": "Beginner"},
        {"title": "Digital Product Management", "partner": "Coursera", "duration": "7 months", "level": "Intermediate"},
    ],
    "AWS": [
        {"title": "AWS Cloud Practitioner", "partner": "AWS Training", "duration": "Self-paced", "level": "Beginner"},
        {"title": "AWS Solutions Architect", "partner": "Coursera", "duration": "4 months", "level": "Intermediate"},
    ],
    "Healthcare IT": [
        {"title": "Healthcare IT Fundamentals", "partner": "Dice Learning", "duration": "15 hrs", "level": "Beginner"},
        {"title": "HIPAA & Healthcare Compliance", "partner": "Coursera", "duration": "4 weeks", "level": "Beginner"},
    ],
    "Process Mining": [
        {"title": "Process Mining: Data Science in Action", "partner": "Coursera", "duration": "6 weeks", "level": "Intermediate"},
        {"title": "Celonis Process Mining", "partner": "Celonis Academy", "duration": "Self-paced", "level": "Intermediate"},
    ],
    "Go": [
        {"title": "Go: The Complete Developer's Guide", "partner": "Udemy", "duration": "9 hrs", "level": "Intermediate"},
        {"title": "Programming with Google Go", "partner": "Coursera", "duration": "3 months", "level": "Beginner"},
    ],
}

DEMO_JOBS = [
    {
        "id": "job-gov-001",
        "title": "Senior AI Governance Engineer",
        "department": "Engineering",
        "location": "Remote · USA",
        "employment_type": "Full-time",
        "salary_min": 145000, "salary_max": 185000,
        "phi_role": "phi_guardian",
        "description": "Design and implement Tessera's formal governance layer — the Coloured Petri Net model that sits between every agent and every business decision. You'll work at the intersection of formal methods, distributed systems, and real-world org dynamics. This is not a typical engineering role. You'll be building the mathematics of organizational trust.",
        "skills_required": ["Python", "Petri Nets", "FastAPI", "PostgreSQL", "Docker", "AI Governance"],
        "tags": ["Governance", "Backend", "Research"],
        "posted_days_ago": 2,
    },
    {
        "id": "job-ml-001",
        "title": "ML Engineer — Organizational Intelligence",
        "department": "Data & AI",
        "location": "Remote · USA",
        "employment_type": "Full-time",
        "salary_min": 135000, "salary_max": 170000,
        "phi_role": "standard",
        "description": "Build the models behind φ (organizational intelligence), LBI (Layer Bioelectric Index), and ROAI (Return on Agent Investment). You'll train on real org data, calibrate the Digital Twin, and develop the Leap Channel — a statistical detector for novel agent behavior that has no precedent in any other platform.",
        "skills_required": ["Machine Learning", "Python", "Data Science", "PostgreSQL", "Docker"],
        "tags": ["ML", "Research", "AI"],
        "posted_days_ago": 5,
    },
    {
        "id": "job-fe-001",
        "title": "Senior Frontend Engineer — Platform UI",
        "department": "Engineering",
        "location": "Remote · USA",
        "employment_type": "Full-time",
        "salary_min": 125000, "salary_max": 160000,
        "phi_role": "standard",
        "description": "Own the Tessera platform UI — a real-time governance dashboard used by enterprise HR, IT, and operations teams. You'll build the HAVCPN visualization, the Digital Twin, and the HCM modules that replace Workday. Canvas animations, WebSocket live data, and enterprise UX at scale.",
        "skills_required": ["React", "TypeScript", "Python", "Docker"],
        "tags": ["Frontend", "UI", "Enterprise"],
        "posted_days_ago": 7,
    },
    {
        "id": "job-hcm-001",
        "title": "HCM Implementation Consultant",
        "department": "Customer Success",
        "location": "Hybrid · East Coast",
        "employment_type": "Full-time",
        "salary_min": 95000, "salary_max": 130000,
        "phi_role": "standard",
        "description": "Lead enterprise customers through their migration from Workday, SAP SuccessFactors, or Oracle HCM to Tessera. You'll map their existing processes to the formal governance model, calibrate their Digital Twin, and train their teams on governed agent operations. Deep HCM expertise required — understanding of the governance layer a major advantage.",
        "skills_required": ["Workday", "SAP SuccessFactors", "Product Management", "Healthcare IT"],
        "tags": ["Consulting", "HCM", "Customer Success"],
        "posted_days_ago": 10,
    },
    {
        "id": "job-pm-001",
        "title": "Product Manager — Agent Platform",
        "department": "Product",
        "location": "Remote · USA",
        "employment_type": "Full-time",
        "salary_min": 130000, "salary_max": 165000,
        "phi_role": "phi_guardian",
        "description": "Define and drive the Tessera Agent Factory — the module that lets enterprises build governed AI agents native to their HCM platform. You'll work with engineering, customers, and research to determine what agents get built, what boundaries they operate within, and how the Leap Channel evolves as agent capability grows. This is a deeply technical PM role at the frontier of AI governance.",
        "skills_required": ["Product Management", "AI Governance", "Python", "AWS"],
        "tags": ["Product", "AI", "Strategy"],
        "posted_days_ago": 1,
    },
    {
        "id": "job-arch-001",
        "title": "Healthcare Solutions Architect",
        "department": "Solutions",
        "location": "Remote · USA",
        "employment_type": "Full-time",
        "salary_min": 140000, "salary_max": 175000,
        "phi_role": "standard",
        "description": "Design Tessera deployments for healthcare organizations — hospitals, health systems, and payers replacing their HCM and governance stack. You'll architect the fax processing pipeline, prior auth governance, and HIPAA-compliant agent workflows. Deep understanding of healthcare IT, HL7/FHIR, and workflow automation required.",
        "skills_required": ["Healthcare IT", "AWS", "Docker", "Process Mining", "PostgreSQL"],
        "tags": ["Healthcare", "Architecture", "Solutions"],
        "posted_days_ago": 4,
    },
    {
        "id": "job-be-001",
        "title": "Backend Engineer — Governance Services",
        "department": "Engineering",
        "location": "Remote · USA",
        "employment_type": "Full-time",
        "salary_min": 120000, "salary_max": 155000,
        "phi_role": "standard",
        "description": "Build and scale the services that power Tessera's governance layer — the audit trail, the CPN event bus, the trace service, and the governance rules engine. You'll work in Python/FastAPI with PostgreSQL, Redis, and Kafka, serving real-time governance events to enterprise customers at scale.",
        "skills_required": ["Python", "FastAPI", "PostgreSQL", "Docker", "Go", "AWS"],
        "tags": ["Backend", "Infrastructure", "Governance"],
        "posted_days_ago": 6,
    },
    {
        "id": "job-ds-001",
        "title": "Data Scientist — Process Mining",
        "department": "Data & AI",
        "location": "Remote · USA",
        "employment_type": "Full-time",
        "salary_min": 115000, "salary_max": 148000,
        "phi_role": "standard",
        "description": "Mine real organizational process data to calibrate the Tessera Digital Twin. You'll analyze how tasks actually flow through human-agent systems, identify where governance is strong and where it's eroding, and feed those insights back into the CPN model. Process mining expertise and statistical modeling are core to this role.",
        "skills_required": ["Data Science", "Process Mining", "Python", "Machine Learning", "PostgreSQL"],
        "tags": ["Data", "Research", "Analytics"],
        "posted_days_ago": 8,
    },
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
        await conn.execute(MIGRATE)
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


# ─── PUBLIC CAREER SITE API (no auth) ──────────────────────────────────────

class PublicApplicationRequest(BaseModel):
    job_id: str
    name: str
    email: str
    linkedin_url: Optional[str] = None
    skills: List[str] = []
    note: Optional[str] = None

@app.get("/public/jobs")
async def public_jobs(department: Optional[str] = Query(None), search: Optional[str] = Query(None)):
    """Public job listings — no auth required."""
    import json
    jobs = []
    # Pull live DB requisitions first
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM requisitions WHERE status='open' ORDER BY created_at DESC LIMIT 50"
        )
    for r in rows:
        d = dict(r)
        skills = json.loads(d.get("skills_required") or "[]")
        jobs.append({
            "id": d["id"], "title": d["title"], "department": d.get("department", ""),
            "location": d.get("location", "Remote · USA"),
            "employment_type": d.get("employment_type", "Full-time"),
            "salary_min": d.get("salary_min"), "salary_max": d.get("salary_max"),
            "phi_role": d.get("phi_role", "standard"),
            "description": d.get("description", ""),
            "skills_required": skills,
            "tags": [],
            "posted_days_ago": (datetime.now(timezone.utc) - d["created_at"]).days,
        })
    # Merge in demo jobs for any titles not already in DB
    db_titles = {j["title"] for j in jobs}
    for dj in DEMO_JOBS:
        if dj["title"] not in db_titles:
            jobs.append(dj)

    # Filter
    if department:
        jobs = [j for j in jobs if j.get("department", "").lower() == department.lower()]
    if search:
        q = search.lower()
        jobs = [j for j in jobs if q in j["title"].lower() or q in j.get("description","").lower()
                or any(q in s.lower() for s in j.get("skills_required", []))]
    return {"jobs": jobs, "total": len(jobs)}


@app.get("/public/jobs/{job_id}")
async def public_job_detail(job_id: str):
    """Full job detail with learning paths per skill."""
    import json
    # Check DB first
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM requisitions WHERE id=$1 AND status='open'", job_id)
    if row:
        d = dict(row)
        skills = json.loads(d.get("skills_required") or "[]")
        job = {
            "id": d["id"], "title": d["title"], "department": d.get("department", ""),
            "location": d.get("location", "Remote · USA"),
            "employment_type": d.get("employment_type", "Full-time"),
            "salary_min": d.get("salary_min"), "salary_max": d.get("salary_max"),
            "phi_role": d.get("phi_role", "standard"),
            "description": d.get("description", ""),
            "skills_required": skills,
            "tags": [],
            "posted_days_ago": (datetime.now(timezone.utc) - d["created_at"]).days,
        }
    else:
        job = next((j for j in DEMO_JOBS if j["id"] == job_id), None)
        if not job:
            raise HTTPException(404, "Job not found")

    # Attach learning paths
    job["learning_paths"] = {
        skill: LEARNING_PATHS.get(skill, [])
        for skill in job.get("skills_required", [])
    }
    return job


@app.post("/public/skills-gap")
async def skills_gap(job_id: str = Query(...), candidate_skills: str = Query(...)):
    """Compute skill gap between candidate and job. Returns gap + learning paths."""
    import json
    detail = await public_job_detail(job_id)
    required = set(detail.get("skills_required", []))
    have = set(s.strip() for s in candidate_skills.split(",") if s.strip())
    matched = required & have
    missing = required - have
    match_pct = round(len(matched) / len(required) * 100) if required else 100
    return {
        "job_id": job_id,
        "job_title": detail["title"],
        "required_skills": list(required),
        "matched_skills": list(matched),
        "missing_skills": list(missing),
        "match_percentage": match_pct,
        "status": "strong" if match_pct >= 80 else "developing" if match_pct >= 50 else "early",
        "learning_paths": {
            skill: LEARNING_PATHS.get(skill, [])
            for skill in missing
        },
    }


@app.post("/public/apply", status_code=201)
async def public_apply(body: PublicApplicationRequest):
    """Public job application — no auth required."""
    import json
    # Find or create a requisition for this job
    async with db.acquire() as conn:
        req = await conn.fetchrow("SELECT id FROM requisitions WHERE id=$1", body.job_id)
        if not req:
            # Create from demo job data
            demo = next((j for j in DEMO_JOBS if j["id"] == body.job_id), None)
            if not demo:
                raise HTTPException(404, "Job not found")
            await conn.execute("""
                INSERT INTO requisitions
                  (id, org_id, title, department, location, employment_type,
                   salary_min, salary_max, skills_required, description, phi_role)
                VALUES ($1,'public',$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (id) DO NOTHING
            """, demo["id"], demo["title"], demo.get("department"),
                 demo.get("location"), demo.get("employment_type"),
                 float(demo.get("salary_min") or 0), float(demo.get("salary_max") or 0),
                 json.dumps(demo.get("skills_required", [])),
                 demo.get("description", ""), demo.get("phi_role", "standard"))

        cand_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO candidates (id, requisition_id, name, email)
            VALUES ($1,$2,$3,$4)
        """, cand_id, body.job_id, body.name, body.email)

    # Compute skill gap
    gap = await skills_gap(job_id=body.job_id, candidate_skills=",".join(body.skills))

    return {
        "application_id": cand_id,
        "status": "received",
        "message": f"Thank you {body.name}. Your application is in the governed pipeline.",
        "match_percentage": gap["match_percentage"],
        "matched_skills": gap["matched_skills"],
        "missing_skills": gap["missing_skills"],
        "learning_paths": gap["learning_paths"],
        "next_step": (
            "Strong match — expect a screening call within 3 business days."
            if gap["match_percentage"] >= 70 else
            "We've added you to our talent community. Complete the suggested learning paths to strengthen your application."
        ),
    }
