"""
Tessera Service Catalog  —  port 8027
=======================================
Structured catalog of requestable services with form fields, approval
workflows, and fulfillment stages. This is what makes an ITSM portal feel
real vs a raw ticket form.

ServiceNow's Service Catalog is the primary end-user touchpoint.
Users browse categories → select an item → fill a dynamic form → submit.
The back-end routes to a fulfillment team and tracks stage-by-stage.

HAV differentiator: items that require a φ-guardian fulfiller are tagged.
SLA quality on fulfillment is scored using HAV of the fulfiller, not just
"was it on time" — a procedurally-completed request has lower quality than
one where the fulfiller applied novel judgment.
"""
from __future__ import annotations
import os, uuid, asyncpg
from typing import Optional, List, Any, Dict
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_catalog")
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS catalog_categories (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT UNIQUE NOT NULL,
    description TEXT,
    icon        TEXT,
    sort_order  INT DEFAULT 0,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS catalog_items (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    category_id     TEXT REFERENCES catalog_categories(id),
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    description     TEXT,
    short_desc      TEXT,
    fulfillment_team TEXT,
    requires_phi_guardian BOOLEAN DEFAULT FALSE,
    min_fulfiller_hav    FLOAT DEFAULT 0.0,
    sla_hours       FLOAT DEFAULT 24.0,
    requires_approval BOOLEAN DEFAULT FALSE,
    approval_group  TEXT,
    form_schema     JSONB NOT NULL DEFAULT '[]',  -- JSON array of field definitions
    active          BOOLEAN DEFAULT TRUE,
    request_count   INT DEFAULT 0,
    avg_fulfill_hav FLOAT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (org_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_items_org      ON catalog_items(org_id);
CREATE INDEX IF NOT EXISTS idx_items_category ON catalog_items(category_id);
CREATE INDEX IF NOT EXISTS idx_items_active   ON catalog_items(active);

CREATE TABLE IF NOT EXISTS catalog_requests (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    item_id         TEXT NOT NULL REFERENCES catalog_items(id),
    requester_id    TEXT NOT NULL,
    requester_email TEXT NOT NULL,
    form_data       JSONB NOT NULL DEFAULT '{}',
    status          TEXT DEFAULT 'submitted',
    -- 'submitted'|'pending_approval'|'approved'|'rejected'|'in_progress'|'fulfilled'|'cancelled'
    current_stage   TEXT,
    priority        TEXT DEFAULT 'P3',
    fulfiller_id    TEXT,
    fulfiller_email TEXT,
    fulfiller_hav   FLOAT,
    fulfillment_quality FLOAT,  -- HAV-weighted quality score
    sla_due_at      TIMESTAMPTZ,
    fulfilled_at    TIMESTAMPTZ,
    rejection_reason TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_requests_org       ON catalog_requests(org_id);
CREATE INDEX IF NOT EXISTS idx_requests_item      ON catalog_requests(item_id);
CREATE INDEX IF NOT EXISTS idx_requests_requester ON catalog_requests(requester_id);
CREATE INDEX IF NOT EXISTS idx_requests_status    ON catalog_requests(status);

CREATE TABLE IF NOT EXISTS fulfillment_stages (
    id          TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    stage_order INT NOT NULL,
    assigned_team TEXT,
    sla_hours   FLOAT DEFAULT 4.0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS request_stage_history (
    id          TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL REFERENCES catalog_requests(id),
    stage_name  TEXT NOT NULL,
    actor_id    TEXT,
    actor_email TEXT,
    actor_hav   FLOAT,
    action      TEXT NOT NULL,  -- 'started'|'completed'|'blocked'|'skipped'
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stage_hist_request ON request_stage_history(request_id);

CREATE TABLE IF NOT EXISTS approvals (
    id             TEXT PRIMARY KEY,
    request_id     TEXT NOT NULL REFERENCES catalog_requests(id),
    approver_id    TEXT,
    approver_email TEXT NOT NULL,
    status         TEXT DEFAULT 'pending',  -- 'pending'|'approved'|'rejected'
    comment        TEXT,
    decided_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
        # Seed standard categories
        await conn.execute("""
            INSERT INTO catalog_categories (id, name, slug, description, sort_order)
            VALUES
              ('scat-it',    'IT Services',      'it-services',    'Hardware, software, access',     1),
              ('scat-hr',    'HR Services',      'hr-services',    'Leave, benefits, documents',     2),
              ('scat-onb',   'Onboarding',       'onboarding',     'New hire setup',                 3),
              ('scat-sec',   'Security',         'security',       'Access requests, certs',         4),
              ('scat-ai',    'AI & Agents',      'ai-agents',      'Request AI agent deployments',   5),
              ('scat-fin',   'Finance',          'finance',        'Expense approvals, POs',         6)
            ON CONFLICT (slug) DO NOTHING
        """)
        # Seed example catalog items with form schemas
        await conn.execute("""
            INSERT INTO catalog_items
              (id, org_id, category_id, name, slug, description, short_desc,
               fulfillment_team, sla_hours, requires_approval, form_schema)
            VALUES
              ('item-laptop', 'system', 'scat-it',
               'Laptop Request', 'laptop-request',
               'Request a new or replacement laptop for your role.',
               'New/replacement laptop',
               'IT Procurement', 48, TRUE,
               '[{"field":"os","label":"Operating System","type":"select","options":["macOS","Windows","Linux"],"required":true},{"field":"specs","label":"Required Specs","type":"text","required":false},{"field":"justification","label":"Business Justification","type":"textarea","required":true}]'::JSONB),
              ('item-access', 'system', 'scat-sec',
               'Application Access Request', 'app-access',
               'Request access to an internal or external application.',
               'App access',
               'IT Security', 8, TRUE,
               '[{"field":"app_name","label":"Application Name","type":"text","required":true},{"field":"access_level","label":"Access Level","type":"select","options":["read","write","admin"],"required":true},{"field":"business_need","label":"Business Need","type":"textarea","required":true}]'::JSONB),
              ('item-ai-agent', 'system', 'scat-ai',
               'AI Agent Deployment Request', 'ai-agent-deploy',
               'Request deployment of an AI agent for your team. Requires phi-guardian review.',
               'Deploy AI agent',
               'AI Governance', 72, TRUE,
               '[{"field":"agent_purpose","label":"Agent Purpose","type":"textarea","required":true},{"field":"data_access","label":"Data it will access","type":"textarea","required":true},{"field":"phi_context","label":"Current org phi","type":"number","required":false},{"field":"oversight_owner","label":"Human oversight owner","type":"text","required":true}]'::JSONB)
            ON CONFLICT (org_id, slug) DO NOTHING
        """)
        # Tag AI agent item as phi-guardian required
        await conn.execute("""
            UPDATE catalog_items SET requires_phi_guardian=TRUE, min_fulfiller_hav=0.65
            WHERE id='item-ai-agent'
        """)
    yield
    await db.close()

app = FastAPI(title="Tessera Service Catalog", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class CatalogItemRequest(BaseModel):
    org_id: str
    category_id: Optional[str] = None
    name: str
    slug: str
    description: Optional[str] = None
    short_desc: Optional[str] = None
    fulfillment_team: Optional[str] = None
    requires_phi_guardian: bool = False
    min_fulfiller_hav: float = 0.0
    sla_hours: float = 24.0
    requires_approval: bool = False
    approval_group: Optional[str] = None
    form_schema: List[Dict[str, Any]] = Field(default_factory=list)

class StageRequest(BaseModel):
    name: str
    description: Optional[str] = None
    stage_order: int
    assigned_team: Optional[str] = None
    sla_hours: float = 4.0

class CatalogRequestSubmit(BaseModel):
    org_id: str
    item_id: str
    requester_id: str
    requester_email: str
    form_data: Dict[str, Any] = {}
    priority: str = "P3"
    notes: Optional[str] = None

class ApprovalDecision(BaseModel):
    approver_id: Optional[str] = None
    approver_email: str
    status: str  # 'approved'|'rejected'
    comment: Optional[str] = None

class FulfillRequest(BaseModel):
    fulfiller_id: Optional[str] = None
    fulfiller_email: str
    fulfiller_hav: Optional[float] = Field(None, ge=0.0, le=1.0)
    notes: Optional[str] = None

class StageAction(BaseModel):
    stage_name: str
    actor_id: Optional[str] = None
    actor_email: str
    actor_hav: Optional[float] = Field(None, ge=0.0, le=1.0)
    action: str  # 'started'|'completed'|'blocked'|'skipped'
    notes: Optional[str] = None


@app.get("/")
def root():
    return {"service": "service_catalog", "version": "1.0.0", "port": 8027,
            "differentiator": "Structured catalog with form schemas; phi-guardian fulfillment tagging; HAV quality scoring"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "service_catalog"}


# ── CATALOG BROWSE ────────────────────────────────────────────────────────────

@app.get("/categories")
async def list_categories():
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM catalog_categories WHERE active=TRUE ORDER BY sort_order, name"
        )
    return {"categories": [dict(r) for r in rows]}


@app.get("/items")
async def list_items(
    org_id: str = Query(...),
    category_id: Optional[str] = Query(None),
    requires_phi_guardian: Optional[bool] = Query(None),
    limit: int = Query(50, le=200),
):
    async with db.acquire() as conn:
        clauses = ["(ci.org_id=$1 OR ci.org_id='system')", "ci.active=TRUE"]
        params  = [org_id]
        idx = 2
        if category_id:
            params.append(category_id); clauses.append(f"ci.category_id=${idx}"); idx += 1
        if requires_phi_guardian is not None:
            params.append(requires_phi_guardian)
            clauses.append(f"ci.requires_phi_guardian=${idx}"); idx += 1
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT ci.*, cc.name AS category_name FROM catalog_items ci "
            f"LEFT JOIN catalog_categories cc ON ci.category_id=cc.id "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY ci.requires_phi_guardian DESC, ci.request_count DESC "
            f"LIMIT ${idx}", *params
        )
    return {"items": [dict(r) for r in rows]}


@app.get("/items/{item_id}")
async def get_item(item_id: str):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ci.*, cc.name AS category_name FROM catalog_items ci "
            "LEFT JOIN catalog_categories cc ON ci.category_id=cc.id "
            "WHERE ci.id=$1", item_id
        )
        if not row:
            raise HTTPException(404, "Item not found")
        stages = await conn.fetch(
            "SELECT * FROM fulfillment_stages WHERE item_id=$1 ORDER BY stage_order", item_id
        )
    return {**dict(row), "fulfillment_stages": [dict(s) for s in stages]}


@app.post("/items", status_code=201)
async def create_item(body: CatalogItemRequest):
    import json
    item_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO catalog_items
              (id, org_id, category_id, name, slug, description, short_desc,
               fulfillment_team, requires_phi_guardian, min_fulfiller_hav,
               sla_hours, requires_approval, approval_group, form_schema)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::JSONB)
        """, item_id, body.org_id, body.category_id, body.name, body.slug,
             body.description, body.short_desc, body.fulfillment_team,
             body.requires_phi_guardian, body.min_fulfiller_hav, body.sla_hours,
             body.requires_approval, body.approval_group,
             json.dumps(body.form_schema))
    return {
        "item_id": item_id, "name": body.name, "slug": body.slug,
        "requires_phi_guardian": body.requires_phi_guardian,
        "note": (
            "This item requires a φ-guardian fulfiller (min_fulfiller_hav="
            f"{body.min_fulfiller_hav:.2f}). Route to high-HAV team members."
            if body.requires_phi_guardian else None
        ),
    }


@app.post("/items/{item_id}/stages", status_code=201)
async def add_stage(item_id: str, body: StageRequest):
    async with db.acquire() as conn:
        item = await conn.fetchrow("SELECT id FROM catalog_items WHERE id=$1", item_id)
        if not item:
            raise HTTPException(404, "Item not found")
        stage_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO fulfillment_stages
              (id, item_id, name, description, stage_order, assigned_team, sla_hours)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, stage_id, item_id, body.name, body.description,
             body.stage_order, body.assigned_team, body.sla_hours)
    return {"stage_id": stage_id, "name": body.name, "stage_order": body.stage_order}


# ── REQUESTS ──────────────────────────────────────────────────────────────────

@app.post("/requests", status_code=201)
async def submit_request(body: CatalogRequestSubmit):
    import json
    from datetime import datetime, timezone, timedelta

    async with db.acquire() as conn:
        item = await conn.fetchrow("SELECT * FROM catalog_items WHERE id=$1", body.item_id)
        if not item:
            raise HTTPException(404, "Catalog item not found")
        if not item["active"]:
            raise HTTPException(400, "This catalog item is not available")

        sla_due = datetime.now(timezone.utc) + timedelta(hours=float(item["sla_hours"]))
        initial_status = "pending_approval" if item["requires_approval"] else "submitted"

        req_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO catalog_requests
              (id, org_id, item_id, requester_id, requester_email, form_data,
               status, priority, sla_due_at, notes)
            VALUES ($1,$2,$3,$4,$5,$6::JSONB,$7,$8,$9,$10)
        """, req_id, body.org_id, body.item_id, body.requester_id,
             body.requester_email, json.dumps(body.form_data),
             initial_status, body.priority, sla_due, body.notes)

        await conn.execute(
            "UPDATE catalog_items SET request_count=request_count+1 WHERE id=$1", body.item_id
        )

        phi_warning = None
        if item["requires_phi_guardian"]:
            phi_warning = (
                f"This item requires a φ-guardian fulfiller "
                f"(min HAV={item['min_fulfiller_hav']:.2f}). "
                "Assign to a high-HAV team member — procedural fulfillment will not meet quality bar."
            )

    return {
        "request_id": req_id,
        "item": item["name"],
        "status": initial_status,
        "sla_due_at": sla_due.isoformat(),
        "requires_approval": bool(item["requires_approval"]),
        "requires_phi_guardian": bool(item["requires_phi_guardian"]),
        "phi_guardian_warning": phi_warning,
        "next": (
            f"Approval required. POST /requests/{req_id}/approve"
            if item["requires_approval"] else
            f"Assign fulfiller: POST /requests/{req_id}/assign"
        ),
    }


@app.post("/requests/{request_id}/approve")
async def decide_approval(request_id: str, body: ApprovalDecision):
    async with db.acquire() as conn:
        req = await conn.fetchrow("SELECT * FROM catalog_requests WHERE id=$1", request_id)
        if not req:
            raise HTTPException(404, "Request not found")

        appr_id = str(uuid.uuid4())
        from datetime import datetime, timezone
        await conn.execute("""
            INSERT INTO approvals (id, request_id, approver_id, approver_email, status, comment, decided_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, appr_id, request_id, body.approver_id, body.approver_email,
             body.status, body.comment, datetime.now(timezone.utc))

        new_status = "approved" if body.status == "approved" else "rejected"
        reject_reason = body.comment if body.status == "rejected" else None
        await conn.execute("""
            UPDATE catalog_requests SET status=$1, rejection_reason=$2, updated_at=NOW()
            WHERE id=$3
        """, new_status, reject_reason, request_id)

    return {
        "approval_id": appr_id,
        "request_id": request_id,
        "decision": body.status,
        "next": f"POST /requests/{request_id}/assign" if body.status == "approved" else "Rejected. Notify requester.",
    }


@app.post("/requests/{request_id}/assign")
async def assign_fulfiller(request_id: str, body: FulfillRequest):
    async with db.acquire() as conn:
        req = await conn.fetchrow(
            "SELECT cr.*, ci.requires_phi_guardian, ci.min_fulfiller_hav "
            "FROM catalog_requests cr JOIN catalog_items ci ON cr.item_id=ci.id "
            "WHERE cr.id=$1", request_id
        )
        if not req:
            raise HTTPException(404, "Request not found")

        # φ-guardian gate: if item requires it, check fulfiller HAV
        if req["requires_phi_guardian"] and body.fulfiller_hav is not None:
            if body.fulfiller_hav < req["min_fulfiller_hav"]:
                return {
                    "assigned": False,
                    "reason": (
                        f"φ-guardian item requires fulfiller HAV≥{req['min_fulfiller_hav']:.2f}. "
                        f"This fulfiller has HAV={body.fulfiller_hav:.2f}. "
                        "Assign a higher-HAV team member."
                    ),
                }

        await conn.execute("""
            UPDATE catalog_requests SET
                fulfiller_id=$1, fulfiller_email=$2, fulfiller_hav=$3,
                status='in_progress', updated_at=NOW()
            WHERE id=$4
        """, body.fulfiller_id, body.fulfiller_email, body.fulfiller_hav, request_id)

        hist_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO request_stage_history
              (id, request_id, stage_name, actor_id, actor_email, actor_hav, action)
            VALUES ($1,$2,'Assignment',$3,$4,$5,'started')
        """, hist_id, request_id, body.fulfiller_id, body.fulfiller_email, body.fulfiller_hav)

    return {"assigned": True, "fulfiller_email": body.fulfiller_email,
            "fulfiller_hav": body.fulfiller_hav, "status": "in_progress"}


@app.post("/requests/{request_id}/stage")
async def log_stage(request_id: str, body: StageAction):
    async with db.acquire() as conn:
        req = await conn.fetchrow("SELECT id FROM catalog_requests WHERE id=$1", request_id)
        if not req:
            raise HTTPException(404, "Request not found")
        hist_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO request_stage_history
              (id, request_id, stage_name, actor_id, actor_email, actor_hav, action, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """, hist_id, request_id, body.stage_name, body.actor_id,
             body.actor_email, body.actor_hav, body.action, body.notes)
        await conn.execute(
            "UPDATE catalog_requests SET current_stage=$1, updated_at=NOW() WHERE id=$2",
            body.stage_name, request_id
        )
    return {"stage_id": hist_id, "stage_name": body.stage_name, "action": body.action}


@app.post("/requests/{request_id}/fulfill")
async def fulfill_request(request_id: str, body: FulfillRequest):
    """Mark fulfilled. Compute fulfillment quality from fulfiller HAV."""
    from datetime import datetime, timezone
    async with db.acquire() as conn:
        req = await conn.fetchrow(
            "SELECT cr.*, ci.requires_phi_guardian, ci.min_fulfiller_hav, ci.sla_hours "
            "FROM catalog_requests cr JOIN catalog_items ci ON cr.item_id=ci.id "
            "WHERE cr.id=$1", request_id
        )
        if not req:
            raise HTTPException(404, "Request not found")

        fulfiller_hav = body.fulfiller_hav or req["fulfiller_hav"] or 0.5
        # Quality = base 0.6 + HAV contribution × 0.4
        quality = round(0.60 + fulfiller_hav * 0.40, 4)
        now = datetime.now(timezone.utc)

        await conn.execute("""
            UPDATE catalog_requests SET
                status='fulfilled', fulfilled_at=$1,
                fulfillment_quality=$2, updated_at=NOW()
            WHERE id=$3
        """, now, quality, request_id)

        # Update rolling avg fulfillment HAV on the item
        await conn.execute("""
            UPDATE catalog_items SET
                avg_fulfill_hav = COALESCE((avg_fulfill_hav + $1) / 2.0, $1)
            WHERE id=$2
        """, fulfiller_hav, req["item_id"])

    return {
        "request_id": request_id,
        "status": "fulfilled",
        "fulfillment_quality": quality,
        "fulfiller_hav": fulfiller_hav,
        "quality_note": (
            f"Quality={quality:.2f}: fulfiller HAV={fulfiller_hav:.2f} "
            "above φ-guardian threshold — non-procedural fulfillment."
            if fulfiller_hav >= 0.65 else
            f"Quality={quality:.2f}: standard fulfillment."
        ),
    }


@app.get("/requests")
async def list_requests(
    org_id: str = Query(...),
    status: Optional[str] = Query(None),
    requester_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    async with db.acquire() as conn:
        clauses = ["cr.org_id=$1"]
        params  = [org_id]
        idx = 2
        for f, v in [("cr.status", status), ("cr.requester_id", requester_id)]:
            if v:
                params.append(v); clauses.append(f"{f}=${idx}"); idx += 1
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT cr.*, ci.name AS item_name, ci.requires_phi_guardian "
            f"FROM catalog_requests cr JOIN catalog_items ci ON cr.item_id=ci.id "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY cr.created_at DESC LIMIT ${idx}", *params
        )
    return {"requests": [dict(r) for r in rows]}


@app.get("/requests/{request_id}")
async def get_request(request_id: str):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cr.*, ci.name AS item_name, ci.form_schema, ci.requires_phi_guardian "
            "FROM catalog_requests cr JOIN catalog_items ci ON cr.item_id=ci.id "
            "WHERE cr.id=$1", request_id
        )
        if not row:
            raise HTTPException(404, "Request not found")
        history = await conn.fetch(
            "SELECT * FROM request_stage_history WHERE request_id=$1 ORDER BY created_at", request_id
        )
    return {**dict(row), "stage_history": [dict(h) for h in history]}


# ── REPORTS ───────────────────────────────────────────────────────────────────

@app.get("/reports/summary")
async def report_summary(org_id: str = Query(...)):
    async with db.acquire() as conn:
        by_status = await conn.fetch("""
            SELECT status, COUNT(*) AS count
            FROM catalog_requests WHERE org_id=$1
            GROUP BY status ORDER BY count DESC
        """, org_id)
        top_items = await conn.fetch("""
            SELECT ci.name, ci.requires_phi_guardian, ci.request_count,
                   ci.avg_fulfill_hav,
                   AVG(cr.fulfillment_quality) AS avg_quality
            FROM catalog_items ci
            LEFT JOIN catalog_requests cr ON ci.id=cr.item_id AND cr.org_id=$1
            WHERE ci.org_id IN ($1,'system')
            GROUP BY ci.id ORDER BY ci.request_count DESC LIMIT 10
        """, org_id)
        phi_items = await conn.fetchval("""
            SELECT COUNT(*) FROM catalog_requests cr
            JOIN catalog_items ci ON cr.item_id=ci.id
            WHERE cr.org_id=$1 AND ci.requires_phi_guardian=TRUE
            AND cr.status='in_progress'
        """, org_id)

    return {
        "by_status": {r["status"]: r["count"] for r in by_status},
        "top_requested_items": [dict(r) for r in top_items],
        "phi_guardian_items_in_flight": phi_items,
        "note": (
            f"{phi_items} φ-guardian requests in-flight. "
            "These require high-HAV fulfillers — monitor assignment quality."
            if phi_items else "No φ-guardian requests in flight."
        ),
    }
