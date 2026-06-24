"""
Tessera ITSM Service
====================
Port 8013 — ServiceNow equivalent

Ticket types
  incident  INC-####  unexpected disruption, P1–P4
  request   REQ-####  standard service request
  problem   PRB-####  root cause investigation
  change    CHG-####  planned infrastructure change (CAB approval)

Modules
  Tickets       create, list, filter, update, assign, resolve, close
  Comments      internal notes + customer-visible updates
  CMDB          asset registry (servers, laptops, network, software, services)
  SLA           deadlines on creation, breach detection, compliance reports
  Change/CAB    multi-approver workflow for change tickets
  Reports       summary, SLA compliance, team workload

SLA defaults (overridable per org via /sla/policies)
  P1  response 1h   resolve 4h
  P2  response 4h   resolve 8h
  P3  response 8h   resolve 24h
  P4  response 24h  resolve 72h
"""

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import asyncpg
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

# ── CONFIG ────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@postgres:5432/tessera_itsm")
TRACE_URL    = os.getenv("TRACE_URL", "http://trace:8010")

TICKET_PREFIX = {"incident": "INC", "request": "REQ", "problem": "PRB", "change": "CHG"}

# Default SLA hours keyed by (ticket_type, priority)
SLA_DEFAULTS: dict[tuple, tuple] = {
    ("incident", "P1"): (1,   4),
    ("incident", "P2"): (4,   8),
    ("incident", "P3"): (8,  24),
    ("incident", "P4"): (24, 72),
    ("request",  "P1"): (2,   8),
    ("request",  "P2"): (8,  24),
    ("request",  "P3"): (24, 48),
    ("request",  "P4"): (48, 120),
    ("problem",  "P1"): (4,  48),
    ("problem",  "P2"): (8,  72),
    ("problem",  "P3"): (24, 168),
    ("problem",  "P4"): (48, 336),
    ("change",   "P1"): (4,  24),
    ("change",   "P2"): (8,  48),
    ("change",   "P3"): (24, 168),
    ("change",   "P4"): (48, 336),
}

app = FastAPI(title="Tessera ITSM", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db:        asyncpg.Pool     = None
scheduler: AsyncIOScheduler = None

# ── SCHEMA ────────────────────────────────────────────────────────────
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS ticket_counters (
    ticket_type TEXT PRIMARY KEY,
    last_number INT NOT NULL DEFAULT 0
);
INSERT INTO ticket_counters (ticket_type)
VALUES ('incident'),('request'),('problem'),('change')
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS tickets (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    number            TEXT        UNIQUE NOT NULL,
    org_id            UUID        NOT NULL,
    ticket_type       TEXT        NOT NULL
                                  CHECK (ticket_type IN ('incident','request','problem','change')),
    title             TEXT        NOT NULL,
    description       TEXT        NOT NULL,
    priority          TEXT        NOT NULL DEFAULT 'P3'
                                  CHECK (priority IN ('P1','P2','P3','P4')),
    status            TEXT        NOT NULL DEFAULT 'open'
                                  CHECK (status IN
                                    ('open','in_progress','pending','resolved','closed','cancelled')),
    category          TEXT,
    subcategory       TEXT,
    reporter_email    TEXT        NOT NULL,
    assignee_email    TEXT,
    team              TEXT,
    ci_id             UUID,
    parent_id         UUID        REFERENCES tickets(id),
    resolution        TEXT,
    sla_response_at   TIMESTAMPTZ,
    sla_resolve_at    TIMESTAMPTZ,
    first_response_at TIMESTAMPTZ,
    resolved_at       TIMESTAMPTZ,
    closed_at         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id    UUID        NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_email TEXT        NOT NULL,
    body         TEXT        NOT NULL,
    is_internal  BOOLEAN     NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cmdb_items (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        UUID        NOT NULL,
    name          TEXT        NOT NULL,
    ci_type       TEXT        NOT NULL
                              CHECK (ci_type IN
                                ('server','laptop','network','software','service','other')),
    status        TEXT        NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','maintenance','retired')),
    owner_email   TEXT,
    location      TEXT,
    ip_address    TEXT,
    serial_number TEXT,
    vendor        TEXT,
    model         TEXT,
    purchase_date DATE,
    warranty_end  DATE,
    tags          TEXT[]      DEFAULT '{}',
    metadata      JSONB       DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sla_policies (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         UUID        NOT NULL,
    name           TEXT        NOT NULL,
    ticket_type    TEXT        NOT NULL,
    priority       TEXT        NOT NULL,
    response_hours FLOAT       NOT NULL,
    resolve_hours  FLOAT       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, ticket_type, priority)
);

CREATE TABLE IF NOT EXISTS change_approvals (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id      UUID        NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    approver_email TEXT        NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending','approved','rejected')),
    comment        TEXT,
    decided_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tickets_org        ON tickets(org_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status     ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee   ON tickets(assignee_email);
CREATE INDEX IF NOT EXISTS idx_tickets_type       ON tickets(ticket_type);
CREATE INDEX IF NOT EXISTS idx_tickets_priority   ON tickets(priority);
CREATE INDEX IF NOT EXISTS idx_tickets_sla        ON tickets(sla_resolve_at);
CREATE INDEX IF NOT EXISTS idx_cmdb_org           ON cmdb_items(org_id);
"""

# ── LIFECYCLE ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global db, scheduler
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(SCHEMA)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_sla_escalation_job, "interval", hours=1, id="sla_escalate",
                      next_run_time=datetime.now())
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    if scheduler:
        scheduler.shutdown(wait=False)
    if db:
        await db.close()

# ── HELPERS ───────────────────────────────────────────────────────────
async def _next_number(conn: asyncpg.Connection, ticket_type: str) -> str:
    row = await conn.fetchrow(
        "UPDATE ticket_counters SET last_number = last_number + 1 "
        "WHERE ticket_type = $1 RETURNING last_number",
        ticket_type
    )
    return f"{TICKET_PREFIX[ticket_type]}-{row['last_number']:04d}"

async def _sla_hours(conn: asyncpg.Connection, org_id: uuid.UUID,
                     ticket_type: str, priority: str) -> tuple[float, float]:
    policy = await conn.fetchrow(
        "SELECT response_hours, resolve_hours FROM sla_policies "
        "WHERE org_id=$1 AND ticket_type=$2 AND priority=$3",
        org_id, ticket_type, priority
    )
    if policy:
        return policy["response_hours"], policy["resolve_hours"]
    return SLA_DEFAULTS.get((ticket_type, priority), (24.0, 72.0))

def _serialize(d: dict) -> dict:
    result = {}
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            result[k] = str(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        elif isinstance(v, date):
            result[k] = v.isoformat()
        elif isinstance(v, list):
            result[k] = [str(i) if isinstance(i, uuid.UUID) else i for i in v]
        else:
            result[k] = v
    return result

def _ticket(row) -> dict:
    return _serialize(dict(row))

def _get_org(org_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(400, "Invalid org_id")

# ── SLA ESCALATION JOB ────────────────────────────────────────────────
async def _sla_escalation_job():
    """
    Hourly: find tickets that have breached their resolve SLA.
    Escalates P1/P2 by bumping priority (if not already P1) and logs to trace.
    """
    now = datetime.now(timezone.utc)
    async with db.acquire() as conn:
        breached = await conn.fetch(
            """SELECT id, number, ticket_type, priority, org_id, assignee_email, title
               FROM tickets
               WHERE status NOT IN ('resolved','closed','cancelled')
               AND sla_resolve_at IS NOT NULL AND sla_resolve_at < $1""",
            now
        )
        for t in breached:
            # Auto-escalate: P3 → P2, P2 → P1
            if t["priority"] in ("P3", "P2"):
                new_priority = "P2" if t["priority"] == "P3" else "P1"
                await conn.execute(
                    "UPDATE tickets SET priority=$1, updated_at=now() WHERE id=$2",
                    new_priority, t["id"]
                )
                await conn.execute(
                    """INSERT INTO ticket_comments (ticket_id, author_email, body, is_internal)
                       VALUES ($1, 'system@tessera', $2, true)""",
                    t["id"],
                    f"SLA breach detected. Priority auto-escalated from "
                    f"{t['priority']} to {new_priority}."
                )
            asyncio.create_task(_emit_sla_trace(
                str(t["id"]), t["number"], str(t["org_id"]), t["priority"]
            ))

async def _emit_sla_trace(ticket_id: str, number: str, org_id: str, priority: str):
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.post(f"{TRACE_URL}/run", json={
                "name": f"sla_breach:{number}",
                "org_id": org_id,
                "branch": "alignment"
            })
            run_id = r.json().get("run_id", str(uuid.uuid4()))
            await c.post(f"{TRACE_URL}/span", json={
                "run_id": run_id, "name": "sla_breach",
                "model": "system", "branch": "alignment",
                "tokens_in": 0, "tokens_out": 0, "latency_ms": 0,
                "input_text": f"ticket={number} priority={priority}",
                "output_text": "escalated"
            })
            await c.patch(f"{TRACE_URL}/run/{run_id}",
                          json={"status": "completed", "qf_ratio": 0.0})
    except Exception:
        pass

# ── PYDANTIC MODELS ───────────────────────────────────────────────────
class TicketCreate(BaseModel):
    org_id:      str
    ticket_type: Literal["incident", "request", "problem", "change"]
    title:       str
    description: str
    priority:    Literal["P1", "P2", "P3", "P4"] = "P3"
    reporter_email: str
    category:    Optional[str] = None
    subcategory: Optional[str] = None
    assignee_email: Optional[str] = None
    team:        Optional[str] = None
    ci_id:       Optional[str] = None
    parent_id:   Optional[str] = None

class TicketUpdate(BaseModel):
    title:          Optional[str] = None
    description:    Optional[str] = None
    priority:       Optional[Literal["P1", "P2", "P3", "P4"]] = None
    status:         Optional[Literal["open", "in_progress", "pending",
                                     "resolved", "closed", "cancelled"]] = None
    category:       Optional[str] = None
    subcategory:    Optional[str] = None
    assignee_email: Optional[str] = None
    team:           Optional[str] = None
    ci_id:          Optional[str] = None
    resolution:     Optional[str] = None

class CommentCreate(BaseModel):
    author_email: str
    body:         str
    is_internal:  bool = False

class CmdbCreate(BaseModel):
    org_id:        str
    name:          str
    ci_type:       Literal["server", "laptop", "network", "software", "service", "other"]
    owner_email:   Optional[str] = None
    location:      Optional[str] = None
    ip_address:    Optional[str] = None
    serial_number: Optional[str] = None
    vendor:        Optional[str] = None
    model:         Optional[str] = None
    purchase_date: Optional[date] = None
    warranty_end:  Optional[date] = None
    tags:          List[str] = []
    metadata:      Dict[str, Any] = {}

class CmdbUpdate(BaseModel):
    name:          Optional[str] = None
    ci_type:       Optional[Literal["server", "laptop", "network", "software", "service", "other"]] = None
    status:        Optional[Literal["active", "maintenance", "retired"]] = None
    owner_email:   Optional[str] = None
    location:      Optional[str] = None
    ip_address:    Optional[str] = None
    serial_number: Optional[str] = None
    vendor:        Optional[str] = None
    model:         Optional[str] = None
    purchase_date: Optional[date] = None
    warranty_end:  Optional[date] = None
    tags:          Optional[List[str]] = None
    metadata:      Optional[Dict[str, Any]] = None

class SLAPolicyCreate(BaseModel):
    org_id:         str
    name:           str
    ticket_type:    Literal["incident", "request", "problem", "change"]
    priority:       Literal["P1", "P2", "P3", "P4"]
    response_hours: float
    resolve_hours:  float

class ApprovalCreate(BaseModel):
    approver_email: str

class ApprovalDecision(BaseModel):
    status:  Literal["approved", "rejected"]
    comment: Optional[str] = None

class ResolveRequest(BaseModel):
    resolution:   str
    resolved_by:  str

# ── HEALTH ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "itsm"}

# ── TICKETS ───────────────────────────────────────────────────────────
@app.post("/tickets", status_code=201)
async def create_ticket(body: TicketCreate):
    org_id = _get_org(body.org_id)
    now    = datetime.now(timezone.utc)
    async with db.acquire() as conn:
        async with conn.transaction():
            number = await _next_number(conn, body.ticket_type)
            resp_h, res_h = await _sla_hours(conn, org_id, body.ticket_type, body.priority)
            sla_response_at = now + timedelta(hours=resp_h)
            sla_resolve_at  = now + timedelta(hours=res_h)
            ci_id     = uuid.UUID(body.ci_id)   if body.ci_id     else None
            parent_id = uuid.UUID(body.parent_id) if body.parent_id else None
            row = await conn.fetchrow(
                """INSERT INTO tickets
                     (number, org_id, ticket_type, title, description, priority,
                      reporter_email, category, subcategory, assignee_email, team,
                      ci_id, parent_id, sla_response_at, sla_resolve_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                   RETURNING *""",
                number, org_id, body.ticket_type, body.title, body.description,
                body.priority, body.reporter_email, body.category, body.subcategory,
                body.assignee_email, body.team, ci_id, parent_id,
                sla_response_at, sla_resolve_at
            )
    return _ticket(row)

@app.get("/tickets")
async def list_tickets(
    org_id:      str,
    ticket_type: Optional[str] = None,
    status:      Optional[str] = None,
    priority:    Optional[str] = None,
    assignee:    Optional[str] = None,
    team:        Optional[str] = None,
    limit:       int = Query(50, le=500),
    offset:      int = 0,
):
    oid = _get_org(org_id)
    filters = ["org_id=$1"]
    params: list = [oid]
    i = 2
    for col, val in [("ticket_type", ticket_type), ("status", status),
                     ("priority", priority), ("assignee_email", assignee),
                     ("team", team)]:
        if val:
            filters.append(f"{col}=${i}")
            params.append(val)
            i += 1
    where  = " AND ".join(filters)
    params += [limit, offset]
    rows = await db.fetch(
        f"SELECT * FROM tickets WHERE {where} ORDER BY created_at DESC LIMIT ${i} OFFSET ${i+1}",
        *params
    )
    total = await db.fetchval(f"SELECT COUNT(*) FROM tickets WHERE {where}", *params[:-2])
    return {"total": total, "tickets": [_ticket(r) for r in rows]}

@app.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str):
    row = await db.fetchrow("SELECT * FROM tickets WHERE id=$1", uuid.UUID(ticket_id))
    if not row:
        raise HTTPException(404, "Ticket not found")
    return _ticket(row)

@app.patch("/tickets/{ticket_id}")
async def update_ticket(ticket_id: str, body: TicketUpdate):
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not fields:
        raise HTTPException(400, "No fields to update")
    tid = uuid.UUID(ticket_id)

    # Handle CI ID conversion
    if "ci_id" in fields and fields["ci_id"]:
        fields["ci_id"] = uuid.UUID(fields["ci_id"])

    # Auto-set timestamps on status transitions
    now = datetime.now(timezone.utc)
    current = await db.fetchrow("SELECT * FROM tickets WHERE id=$1", tid)
    if not current:
        raise HTTPException(404, "Ticket not found")

    if "status" in fields:
        new_status = fields["status"]
        if new_status == "resolved" and not current["resolved_at"]:
            fields["resolved_at"] = now
        elif new_status == "closed" and not current["closed_at"]:
            fields["closed_at"] = now
        if new_status == "in_progress" and not current["first_response_at"]:
            fields["first_response_at"] = now

    params = [tid]
    setters = []
    for i, (k, v) in enumerate(fields.items(), start=2):
        setters.append(f"{k}=${i}")
        params.append(v)
    row = await db.fetchrow(
        f"UPDATE tickets SET {', '.join(setters)}, updated_at=now() "
        f"WHERE id=$1 RETURNING *",
        *params
    )
    return _ticket(row)

@app.post("/tickets/{ticket_id}/assign")
async def assign_ticket(ticket_id: str, assignee_email: str, team: Optional[str] = None):
    now = datetime.now(timezone.utc)
    row = await db.fetchrow(
        """UPDATE tickets SET assignee_email=$1, team=$2, status='in_progress',
           first_response_at=COALESCE(first_response_at, $3), updated_at=now()
           WHERE id=$4 RETURNING *""",
        assignee_email, team, now, uuid.UUID(ticket_id)
    )
    if not row:
        raise HTTPException(404, "Ticket not found")
    await db.execute(
        """INSERT INTO ticket_comments (ticket_id, author_email, body, is_internal)
           VALUES ($1, 'system@tessera', $2, true)""",
        uuid.UUID(ticket_id),
        f"Ticket assigned to {assignee_email}" + (f" ({team})" if team else "")
    )
    return _ticket(row)

@app.post("/tickets/{ticket_id}/resolve")
async def resolve_ticket(ticket_id: str, body: ResolveRequest):
    now = datetime.now(timezone.utc)
    row = await db.fetchrow(
        """UPDATE tickets SET status='resolved', resolution=$1,
           resolved_at=$2, updated_at=now() WHERE id=$3 RETURNING *""",
        body.resolution, now, uuid.UUID(ticket_id)
    )
    if not row:
        raise HTTPException(404, "Ticket not found")
    await db.execute(
        """INSERT INTO ticket_comments (ticket_id, author_email, body, is_internal)
           VALUES ($1, $2, $3, false)""",
        uuid.UUID(ticket_id), body.resolved_by,
        f"Resolved: {body.resolution}"
    )
    return _ticket(row)

@app.post("/tickets/{ticket_id}/close")
async def close_ticket(ticket_id: str, closed_by: str):
    now = datetime.now(timezone.utc)
    row = await db.fetchrow(
        "UPDATE tickets SET status='closed', closed_at=$1, updated_at=now() "
        "WHERE id=$2 RETURNING *",
        now, uuid.UUID(ticket_id)
    )
    if not row:
        raise HTTPException(404, "Ticket not found")
    return _ticket(row)

# ── COMMENTS ──────────────────────────────────────────────────────────
@app.post("/tickets/{ticket_id}/comments", status_code=201)
async def add_comment(ticket_id: str, body: CommentCreate):
    tid = uuid.UUID(ticket_id)
    exists = await db.fetchval("SELECT 1 FROM tickets WHERE id=$1", tid)
    if not exists:
        raise HTTPException(404, "Ticket not found")
    # Mark first response time on first non-internal comment
    if not body.is_internal:
        await db.execute(
            "UPDATE tickets SET first_response_at=COALESCE(first_response_at, now()), "
            "updated_at=now() WHERE id=$1",
            tid
        )
    row = await db.fetchrow(
        """INSERT INTO ticket_comments (ticket_id, author_email, body, is_internal)
           VALUES ($1,$2,$3,$4) RETURNING *""",
        tid, body.author_email, body.body, body.is_internal
    )
    return _serialize(dict(row))

@app.get("/tickets/{ticket_id}/comments")
async def get_comments(ticket_id: str, include_internal: bool = False):
    tid = uuid.UUID(ticket_id)
    if include_internal:
        rows = await db.fetch(
            "SELECT * FROM ticket_comments WHERE ticket_id=$1 ORDER BY created_at", tid
        )
    else:
        rows = await db.fetch(
            "SELECT * FROM ticket_comments WHERE ticket_id=$1 AND is_internal=false "
            "ORDER BY created_at", tid
        )
    return [_serialize(dict(r)) for r in rows]

# ── CMDB ──────────────────────────────────────────────────────────────
@app.post("/cmdb", status_code=201)
async def create_ci(body: CmdbCreate):
    import json
    oid = _get_org(body.org_id)
    row = await db.fetchrow(
        """INSERT INTO cmdb_items
             (org_id, name, ci_type, owner_email, location, ip_address,
              serial_number, vendor, model, purchase_date, warranty_end, tags, metadata)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
        oid, body.name, body.ci_type, body.owner_email, body.location,
        body.ip_address, body.serial_number, body.vendor, body.model,
        body.purchase_date, body.warranty_end, body.tags,
        json.dumps(body.metadata)
    )
    return _serialize(dict(row))

@app.get("/cmdb")
async def list_cmdb(
    org_id:  str,
    ci_type: Optional[str] = None,
    status:  Optional[str] = None,
    limit:   int = Query(50, le=500),
    offset:  int = 0,
):
    oid = _get_org(org_id)
    filters = ["org_id=$1"]
    params: list = [oid]
    i = 2
    for col, val in [("ci_type", ci_type), ("status", status)]:
        if val:
            filters.append(f"{col}=${i}")
            params.append(val)
            i += 1
    where = " AND ".join(filters)
    params += [limit, offset]
    rows = await db.fetch(
        f"SELECT * FROM cmdb_items WHERE {where} ORDER BY name LIMIT ${i} OFFSET ${i+1}",
        *params
    )
    total = await db.fetchval(f"SELECT COUNT(*) FROM cmdb_items WHERE {where}", *params[:-2])
    return {"total": total, "items": [_serialize(dict(r)) for r in rows]}

@app.get("/cmdb/{ci_id}")
async def get_ci(ci_id: str):
    row = await db.fetchrow("SELECT * FROM cmdb_items WHERE id=$1", uuid.UUID(ci_id))
    if not row:
        raise HTTPException(404, "CI not found")
    return _serialize(dict(row))

@app.put("/cmdb/{ci_id}")
async def update_ci(ci_id: str, body: CmdbUpdate):
    import json
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not fields:
        raise HTTPException(400, "No fields to update")
    if "metadata" in fields:
        fields["metadata"] = json.dumps(fields["metadata"])
    params = [uuid.UUID(ci_id)]
    setters = []
    for i, (k, v) in enumerate(fields.items(), start=2):
        setters.append(f"{k}=${i}")
        params.append(v)
    row = await db.fetchrow(
        f"UPDATE cmdb_items SET {', '.join(setters)}, updated_at=now() "
        f"WHERE id=$1 RETURNING *",
        *params
    )
    if not row:
        raise HTTPException(404, "CI not found")
    return _serialize(dict(row))

@app.delete("/cmdb/{ci_id}")
async def retire_ci(ci_id: str):
    row = await db.fetchrow(
        "UPDATE cmdb_items SET status='retired', updated_at=now() "
        "WHERE id=$1 RETURNING id, name",
        uuid.UUID(ci_id)
    )
    if not row:
        raise HTTPException(404, "CI not found")
    return {"message": "CI retired", "id": str(row["id"]), "name": row["name"]}

@app.get("/cmdb/{ci_id}/tickets")
async def ci_tickets(ci_id: str, limit: int = 20):
    rows = await db.fetch(
        "SELECT * FROM tickets WHERE ci_id=$1 ORDER BY created_at DESC LIMIT $2",
        uuid.UUID(ci_id), limit
    )
    return [_ticket(r) for r in rows]

# ── SLA ───────────────────────────────────────────────────────────────
@app.get("/sla/breaches")
async def sla_breaches(org_id: str):
    oid = _get_org(org_id)
    now = datetime.now(timezone.utc)
    rows = await db.fetch(
        """SELECT id, number, ticket_type, priority, title, assignee_email,
                  sla_resolve_at, status, created_at
           FROM tickets
           WHERE org_id=$1 AND status NOT IN ('resolved','closed','cancelled')
           AND sla_resolve_at IS NOT NULL AND sla_resolve_at < $2
           ORDER BY priority, sla_resolve_at""",
        oid, now
    )
    return {
        "count": len(rows),
        "breaches": [_serialize(dict(r)) for r in rows]
    }

@app.get("/sla/at-risk")
async def sla_at_risk(org_id: str, hours_ahead: float = 4.0):
    oid   = _get_org(org_id)
    now   = datetime.now(timezone.utc)
    limit = now + timedelta(hours=hours_ahead)
    rows  = await db.fetch(
        """SELECT id, number, ticket_type, priority, title, assignee_email,
                  sla_resolve_at, status
           FROM tickets
           WHERE org_id=$1 AND status NOT IN ('resolved','closed','cancelled')
           AND sla_resolve_at BETWEEN $2 AND $3
           ORDER BY sla_resolve_at""",
        oid, now, limit
    )
    return {
        "hours_ahead": hours_ahead,
        "count": len(rows),
        "at_risk": [_serialize(dict(r)) for r in rows]
    }

@app.post("/sla/policies", status_code=201)
async def create_sla_policy(body: SLAPolicyCreate):
    oid = _get_org(body.org_id)
    try:
        row = await db.fetchrow(
            """INSERT INTO sla_policies
                 (org_id, name, ticket_type, priority, response_hours, resolve_hours)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (org_id, ticket_type, priority)
               DO UPDATE SET name=$2, response_hours=$5, resolve_hours=$6
               RETURNING *""",
            oid, body.name, body.ticket_type, body.priority,
            body.response_hours, body.resolve_hours
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    return _serialize(dict(row))

@app.get("/sla/policies")
async def list_sla_policies(org_id: str):
    oid  = _get_org(org_id)
    rows = await db.fetch(
        "SELECT * FROM sla_policies WHERE org_id=$1 ORDER BY ticket_type, priority", oid
    )
    return [_serialize(dict(r)) for r in rows]

# ── CHANGE APPROVALS (CAB) ────────────────────────────────────────────
@app.post("/tickets/{ticket_id}/approvals", status_code=201)
async def add_approver(ticket_id: str, body: ApprovalCreate):
    tid = uuid.UUID(ticket_id)
    ticket = await db.fetchrow(
        "SELECT ticket_type FROM tickets WHERE id=$1", tid
    )
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket["ticket_type"] != "change":
        raise HTTPException(400, "Approvals only apply to change tickets")
    row = await db.fetchrow(
        """INSERT INTO change_approvals (ticket_id, approver_email)
           VALUES ($1, $2) RETURNING *""",
        tid, body.approver_email
    )
    return _serialize(dict(row))

@app.get("/tickets/{ticket_id}/approvals")
async def list_approvals(ticket_id: str):
    rows = await db.fetch(
        "SELECT * FROM change_approvals WHERE ticket_id=$1 ORDER BY created_at",
        uuid.UUID(ticket_id)
    )
    pending   = sum(1 for r in rows if r["status"] == "pending")
    approved  = sum(1 for r in rows if r["status"] == "approved")
    rejected  = sum(1 for r in rows if r["status"] == "rejected")
    cab_clear = rejected == 0 and pending == 0 and approved > 0
    return {
        "cab_cleared": cab_clear,
        "summary": {"pending": pending, "approved": approved, "rejected": rejected},
        "approvals": [_serialize(dict(r)) for r in rows]
    }

@app.patch("/approvals/{approval_id}")
async def decide_approval(approval_id: str, body: ApprovalDecision):
    now = datetime.now(timezone.utc)
    row = await db.fetchrow(
        """UPDATE change_approvals
           SET status=$1, comment=$2, decided_at=$3
           WHERE id=$4 AND status='pending' RETURNING *""",
        body.status, body.comment, now, uuid.UUID(approval_id)
    )
    if not row:
        raise HTTPException(404, "Approval not found or already decided")
    # If any approval is rejected, block the change ticket
    if body.status == "rejected":
        await db.execute(
            "UPDATE tickets SET status='pending', updated_at=now() WHERE id=$1",
            row["ticket_id"]
        )
    return _serialize(dict(row))

# ── REPORTS ───────────────────────────────────────────────────────────
@app.get("/reports/summary")
async def report_summary(org_id: str):
    oid = _get_org(org_id)
    by_status = await db.fetch(
        "SELECT status, COUNT(*) AS count FROM tickets WHERE org_id=$1 GROUP BY status", oid
    )
    by_type = await db.fetch(
        "SELECT ticket_type, COUNT(*) AS count FROM tickets WHERE org_id=$1 GROUP BY ticket_type", oid
    )
    by_priority = await db.fetch(
        """SELECT priority, COUNT(*) AS count FROM tickets
           WHERE org_id=$1 AND status NOT IN ('closed','cancelled')
           GROUP BY priority ORDER BY priority""", oid
    )
    open_p1 = await db.fetchval(
        "SELECT COUNT(*) FROM tickets WHERE org_id=$1 AND priority='P1' "
        "AND status NOT IN ('resolved','closed','cancelled')", oid
    )
    return {
        "by_status":   {r["status"]:       r["count"] for r in by_status},
        "by_type":     {r["ticket_type"]:  r["count"] for r in by_type},
        "by_priority": {r["priority"]:     r["count"] for r in by_priority},
        "open_p1":     open_p1,
    }

@app.get("/reports/sla")
async def report_sla(org_id: str):
    oid = _get_org(org_id)
    total = await db.fetchval(
        "SELECT COUNT(*) FROM tickets WHERE org_id=$1 AND status IN ('resolved','closed')", oid
    )
    within_sla = await db.fetchval(
        """SELECT COUNT(*) FROM tickets
           WHERE org_id=$1 AND status IN ('resolved','closed')
           AND resolved_at IS NOT NULL AND sla_resolve_at IS NOT NULL
           AND resolved_at <= sla_resolve_at""", oid
    )
    breached_open = await db.fetchval(
        """SELECT COUNT(*) FROM tickets
           WHERE org_id=$1 AND status NOT IN ('resolved','closed','cancelled')
           AND sla_resolve_at IS NOT NULL AND sla_resolve_at < now()""", oid
    )
    compliance = round((within_sla / total * 100), 1) if total else 0
    return {
        "total_resolved": total,
        "within_sla":     within_sla,
        "sla_compliance": f"{compliance}%",
        "currently_breached": breached_open,
    }

@app.get("/reports/team")
async def report_team(org_id: str):
    oid  = _get_org(org_id)
    rows = await db.fetch(
        """SELECT COALESCE(team, 'Unassigned') AS team,
                  COUNT(*) AS total,
                  SUM(CASE WHEN status NOT IN ('resolved','closed','cancelled') THEN 1 ELSE 0 END) AS open,
                  SUM(CASE WHEN priority='P1' AND status NOT IN ('resolved','closed','cancelled') THEN 1 ELSE 0 END) AS open_p1
           FROM tickets WHERE org_id=$1
           GROUP BY team ORDER BY open DESC""",
        oid
    )
    return [dict(r) for r in rows]
