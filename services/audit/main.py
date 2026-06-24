"""
Tessera Audit Service
=====================
Port 8016 — Immutable, tamper-evident, cross-service event log

Design principles
  Append-only   no UPDATE/DELETE on audit_events (retention cleanup excepted)
  Hash chain    every event hashes the previous one — any tampering breaks the chain
  Universal     all Tessera services POST here on every significant action
  Queryable     multi-filter search, full-text on description, time range

Action naming convention  {resource}.{verb}
  user.login              user.logout          user.login_failed
  user.created            user.updated         user.deprovisioned
  user.suspended          user.password_reset
  ticket.created          ticket.assigned      ticket.resolved       ticket.closed
  ticket.escalated        ticket.sla_breached
  access.granted          access.revoked       access.denied
  sso.login               sso.login_failed     sso.connection_activated
  scim.user_created       scim.user_deprovisioned
  payroll.run_approved    expense.approved     expense.rejected
  change.cab_approved     change.cab_rejected
  data.exported           config.changed
  notification.sent       webhook.fired

Severity
  info      normal operations
  warning   unusual / elevated risk (failed logins, SLA breach)
  critical  security events (deprovision, bulk export, config change)

Endpoints
  POST /events            log one event
  POST /events/bulk       log many events atomically (max 500)
  GET  /events            search with filters + pagination
  GET  /events/{id}       single event detail
  GET  /events/{id}/verify  verify this event's hash

  GET  /reports/summary   event counts by action / service / outcome
  GET  /reports/security  critical + warning events
  GET  /reports/actor     most active actors
  GET  /reports/timeline  hourly event counts over a period

  GET  /verify            full org hash-chain integrity check
  GET  /export            download log as JSON or CSV

  GET  /retention         org retention policy
  PUT  /retention         update retention days
"""

import asyncio
import csv
import hashlib
import io
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ── CONFIG ────────────────────────────────────────────────────────────
DATABASE_URL          = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@postgres:5432/tessera_audit")
DEFAULT_RETENTION_DAYS = int(os.getenv("DEFAULT_RETENTION_DAYS", "365"))

app = FastAPI(title="Tessera Audit", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db:        asyncpg.Pool     = None
scheduler: AsyncIOScheduler = None

# ── SCHEMA ────────────────────────────────────────────────────────────
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS audit_events (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        UUID        NOT NULL,
    seq           BIGINT      GENERATED ALWAYS AS IDENTITY,

    actor_id      TEXT,
    actor_email   TEXT        NOT NULL DEFAULT 'system',
    actor_type    TEXT        NOT NULL DEFAULT 'user'
                              CHECK (actor_type IN ('user','service','system')),
    actor_ip      TEXT,

    action        TEXT        NOT NULL,
    resource_type TEXT,
    resource_id   TEXT,
    resource_name TEXT,

    service       TEXT        NOT NULL,
    description   TEXT        NOT NULL,
    metadata      JSONB       DEFAULT '{}',

    outcome       TEXT        NOT NULL DEFAULT 'success'
                              CHECK (outcome IN ('success','failure','partial')),
    severity      TEXT        NOT NULL DEFAULT 'info'
                              CHECK (severity IN ('info','warning','critical')),

    prev_hash     TEXT,
    event_hash    TEXT,

    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retention_policies (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         UUID        UNIQUE NOT NULL,
    retention_days INT         NOT NULL DEFAULT 365,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ae_org       ON audit_events(org_id);
CREATE INDEX IF NOT EXISTS idx_ae_actor     ON audit_events(actor_email);
CREATE INDEX IF NOT EXISTS idx_ae_action    ON audit_events(action);
CREATE INDEX IF NOT EXISTS idx_ae_resource  ON audit_events(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_ae_service   ON audit_events(service);
CREATE INDEX IF NOT EXISTS idx_ae_severity  ON audit_events(severity);
CREATE INDEX IF NOT EXISTS idx_ae_outcome   ON audit_events(outcome);
CREATE INDEX IF NOT EXISTS idx_ae_time      ON audit_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ae_seq       ON audit_events(org_id, seq);
"""

# ── LIFECYCLE ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global db, scheduler
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(SCHEMA)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_retention_job, "cron", hour=2, minute=0, id="retention")
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    if scheduler:
        scheduler.shutdown(wait=False)
    if db:
        await db.close()

# ── HASH CHAIN ────────────────────────────────────────────────────────
def _compute_hash(event_id: str, seq: int, actor_email: str, action: str,
                   description: str, metadata: Any, prev_hash: str) -> str:
    """
    SHA-256 over canonical event fields.
    Chaining prev_hash means any modification to a past event
    invalidates every subsequent hash in the org's sequence.
    """
    content = json.dumps({
        "id":          event_id,
        "seq":         seq,
        "actor_email": actor_email,
        "action":      action,
        "description": description,
        "metadata":    metadata if isinstance(metadata, dict) else {},
        "prev_hash":   prev_hash or "",
    }, sort_keys=True, default=str)
    return hashlib.sha256(content.encode()).hexdigest()

async def _last_hash(conn: asyncpg.Connection, org_id: uuid.UUID) -> str:
    """Return the event_hash of the most recent event for this org."""
    row = await conn.fetchrow(
        "SELECT event_hash FROM audit_events WHERE org_id=$1 ORDER BY seq DESC LIMIT 1",
        org_id
    )
    return row["event_hash"] if row else ""

# ── HELPERS ───────────────────────────────────────────────────────────
def _s(d: dict) -> dict:
    return {
        k: (str(v)         if isinstance(v, uuid.UUID)
            else v.isoformat() if isinstance(v, datetime)
            else v)
        for k, v in d.items()
    }

def _get_org(org_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(400, "Invalid org_id")

async def _insert_event(conn: asyncpg.Connection, org_id: uuid.UUID, e: dict) -> dict:
    """
    Insert a single event with hash chaining.
    Must be called inside a transaction with row-level locking to keep seq monotonic.
    """
    prev_hash = await _last_hash(conn, org_id)
    eid       = uuid.uuid4()
    meta      = e.get("metadata") or {}

    row = await conn.fetchrow(
        """INSERT INTO audit_events
             (id, org_id, actor_id, actor_email, actor_type, actor_ip,
              action, resource_type, resource_id, resource_name,
              service, description, metadata, outcome, severity,
              prev_hash, event_hash, occurred_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
           RETURNING *""",
        eid, org_id,
        e.get("actor_id"),
        e.get("actor_email", "system"),
        e.get("actor_type", "user"),
        e.get("actor_ip"),
        e["action"],
        e.get("resource_type"),
        e.get("resource_id"),
        e.get("resource_name"),
        e["service"],
        e["description"],
        json.dumps(meta),
        e.get("outcome", "success"),
        e.get("severity", "info"),
        prev_hash,
        "pending",   # placeholder; compute after we know seq
        e.get("occurred_at") or datetime.now(timezone.utc)
    )

    # Now we have seq — compute the real hash
    event_hash = _compute_hash(
        str(row["id"]), row["seq"],
        row["actor_email"], row["action"],
        row["description"], meta, prev_hash
    )
    await conn.execute(
        "UPDATE audit_events SET event_hash=$1 WHERE id=$2",
        event_hash, row["id"]
    )
    d = _s(dict(row))
    d["event_hash"] = event_hash
    return d

# ── RETENTION JOB ─────────────────────────────────────────────────────
async def _retention_job():
    """Daily: delete events older than org's retention policy."""
    async with db.acquire() as conn:
        orgs = await conn.fetch("SELECT org_id, retention_days FROM retention_policies")
        for o in orgs:
            cutoff = datetime.now(timezone.utc) - timedelta(days=o["retention_days"])
            deleted = await conn.execute(
                "DELETE FROM audit_events WHERE org_id=$1 AND occurred_at < $2",
                o["org_id"], cutoff
            )
        # Default policy for orgs without explicit policy
        default_cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_RETENTION_DAYS)
        await conn.execute(
            """DELETE FROM audit_events
               WHERE occurred_at < $1
               AND org_id NOT IN (SELECT org_id FROM retention_policies)""",
            default_cutoff
        )

# ── PYDANTIC MODELS ───────────────────────────────────────────────────
class AuditEvent(BaseModel):
    org_id:        str
    actor_id:      Optional[str] = None
    actor_email:   str           = "system"
    actor_type:    Literal["user", "service", "system"] = "user"
    actor_ip:      Optional[str] = None
    action:        str
    resource_type: Optional[str] = None
    resource_id:   Optional[str] = None
    resource_name: Optional[str] = None
    service:       str
    description:   str
    metadata:      Dict[str, Any] = {}
    outcome:       Literal["success", "failure", "partial"] = "success"
    severity:      Literal["info", "warning", "critical"]   = "info"
    occurred_at:   Optional[datetime] = None

class BulkRequest(BaseModel):
    events: List[AuditEvent]

class RetentionUpdate(BaseModel):
    org_id:         str
    retention_days: int

# ── HEALTH ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "audit"}

# ── LOG EVENTS ────────────────────────────────────────────────────────
@app.post("/events", status_code=201)
async def log_event(body: AuditEvent):
    oid = _get_org(body.org_id)
    async with db.acquire() as conn:
        async with conn.transaction():
            # SELECT FOR UPDATE on last row prevents race conditions in seq chain
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", str(oid)
            )
            row = await _insert_event(conn, oid, body.model_dump())
    return row

@app.post("/events/bulk", status_code=201)
async def log_bulk(body: BulkRequest):
    if len(body.events) > 500:
        raise HTTPException(400, "Maximum 500 events per bulk request")
    results = []
    # Group by org to minimise lock contention
    by_org: dict[str, list] = {}
    for e in body.events:
        by_org.setdefault(e.org_id, []).append(e.model_dump())
    async with db.acquire() as conn:
        async with conn.transaction():
            for org_id_str, events in by_org.items():
                oid = _get_org(org_id_str)
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", str(oid)
                )
                for e in events:
                    row = await _insert_event(conn, oid, e)
                    results.append(row)
    return {"count": len(results), "events": results}

# ── QUERY EVENTS ──────────────────────────────────────────────────────
@app.get("/events")
async def list_events(
    org_id:        str,
    actor_email:   Optional[str] = None,
    action:        Optional[str] = None,   # supports prefix wildcard: "user.*"
    resource_type: Optional[str] = None,
    resource_id:   Optional[str] = None,
    service:       Optional[str] = None,
    severity:      Optional[str] = None,
    outcome:       Optional[str] = None,
    search:        Optional[str] = None,   # full-text on description
    from_dt:       Optional[datetime] = None,
    to_dt:         Optional[datetime] = None,
    limit:         int = Query(100, le=1000),
    offset:        int = 0,
):
    oid     = _get_org(org_id)
    filters = ["org_id=$1"]
    params: list = [oid]
    i = 2

    for col, val in [
        ("actor_email",   actor_email),
        ("resource_type", resource_type),
        ("resource_id",   resource_id),
        ("service",       service),
        ("severity",      severity),
        ("outcome",       outcome),
    ]:
        if val:
            filters.append(f"{col}=${i}")
            params.append(val)
            i += 1

    if action:
        # Support prefix wildcard "user.*" → LIKE 'user.%'
        if action.endswith("*"):
            filters.append(f"action LIKE ${i}")
            params.append(action[:-1] + "%")
        else:
            filters.append(f"action=${i}")
            params.append(action)
        i += 1

    if search:
        filters.append(f"description ILIKE ${i}")
        params.append(f"%{search}%")
        i += 1

    if from_dt:
        filters.append(f"occurred_at >= ${i}")
        params.append(from_dt)
        i += 1

    if to_dt:
        filters.append(f"occurred_at <= ${i}")
        params.append(to_dt)
        i += 1

    where  = " AND ".join(filters)
    params_count = params[:]
    params += [limit, offset]

    rows = await db.fetch(
        f"SELECT * FROM audit_events WHERE {where} "
        f"ORDER BY occurred_at DESC LIMIT ${i} OFFSET ${i+1}",
        *params
    )
    total = await db.fetchval(
        f"SELECT COUNT(*) FROM audit_events WHERE {where}", *params_count
    )
    return {
        "total":  total,
        "events": [_s(dict(r)) for r in rows],
    }

@app.get("/events/{event_id}")
async def get_event(event_id: str):
    row = await db.fetchrow(
        "SELECT * FROM audit_events WHERE id=$1", uuid.UUID(event_id)
    )
    if not row:
        raise HTTPException(404, "Event not found")
    return _s(dict(row))

@app.get("/events/{event_id}/verify")
async def verify_event(event_id: str):
    """Verify this specific event's hash matches what it should be."""
    row = await db.fetchrow(
        "SELECT * FROM audit_events WHERE id=$1", uuid.UUID(event_id)
    )
    if not row:
        raise HTTPException(404, "Event not found")

    meta = row["metadata"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    expected = _compute_hash(
        str(row["id"]), row["seq"],
        row["actor_email"], row["action"],
        row["description"], meta,
        row["prev_hash"] or ""
    )
    valid = expected == row["event_hash"]
    return {
        "event_id":      str(row["id"]),
        "seq":           row["seq"],
        "stored_hash":   row["event_hash"],
        "expected_hash": expected,
        "valid":         valid,
        "tampered":      not valid,
    }

# ── HASH CHAIN INTEGRITY ──────────────────────────────────────────────
@app.get("/verify")
async def verify_chain(
    org_id: str,
    limit:  int = Query(1000, le=10000),
):
    """
    Walk the org's full event chain in seq order, verifying each hash.
    Returns the first breach found (if any) and overall integrity status.
    """
    oid  = _get_org(org_id)
    rows = await db.fetch(
        "SELECT * FROM audit_events WHERE org_id=$1 ORDER BY seq LIMIT $2",
        oid, limit
    )
    if not rows:
        return {"status": "empty", "events_checked": 0}

    breaches   = []
    prev_h     = ""
    for row in rows:
        meta = row["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        # Verify prev_hash linkage
        if row["prev_hash"] != prev_h:
            breaches.append({
                "event_id": str(row["id"]),
                "seq":      row["seq"],
                "issue":    "prev_hash_mismatch",
            })

        # Verify own hash
        expected = _compute_hash(
            str(row["id"]), row["seq"],
            row["actor_email"], row["action"],
            row["description"], meta,
            row["prev_hash"] or ""
        )
        if expected != row["event_hash"]:
            breaches.append({
                "event_id": str(row["id"]),
                "seq":      row["seq"],
                "issue":    "hash_mismatch",
            })

        prev_h = row["event_hash"]

    return {
        "status":         "intact" if not breaches else "TAMPERED",
        "events_checked": len(rows),
        "breaches":       breaches,
        "chain_valid":    len(breaches) == 0,
    }

# ── REPORTS ───────────────────────────────────────────────────────────
@app.get("/reports/summary")
async def report_summary(org_id: str, days: int = 30):
    oid    = _get_org(org_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    by_service  = await db.fetch(
        "SELECT service, COUNT(*) AS count FROM audit_events "
        "WHERE org_id=$1 AND occurred_at>=$2 GROUP BY service ORDER BY count DESC",
        oid, cutoff
    )
    by_outcome  = await db.fetch(
        "SELECT outcome, COUNT(*) AS count FROM audit_events "
        "WHERE org_id=$1 AND occurred_at>=$2 GROUP BY outcome",
        oid, cutoff
    )
    by_severity = await db.fetch(
        "SELECT severity, COUNT(*) AS count FROM audit_events "
        "WHERE org_id=$1 AND occurred_at>=$2 GROUP BY severity",
        oid, cutoff
    )
    top_actions = await db.fetch(
        "SELECT action, COUNT(*) AS count FROM audit_events "
        "WHERE org_id=$1 AND occurred_at>=$2 GROUP BY action ORDER BY count DESC LIMIT 10",
        oid, cutoff
    )
    total = await db.fetchval(
        "SELECT COUNT(*) FROM audit_events WHERE org_id=$1 AND occurred_at>=$2",
        oid, cutoff
    )
    return {
        "period_days":  days,
        "total_events": total,
        "by_service":   {r["service"]: r["count"] for r in by_service},
        "by_outcome":   {r["outcome"]: r["count"] for r in by_outcome},
        "by_severity":  {r["severity"]: r["count"] for r in by_severity},
        "top_actions":  [dict(r) for r in top_actions],
    }

@app.get("/reports/security")
async def report_security(org_id: str, days: int = 7):
    oid    = _get_org(org_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows   = await db.fetch(
        """SELECT * FROM audit_events
           WHERE org_id=$1 AND occurred_at>=$2
           AND (severity IN ('warning','critical') OR outcome='failure')
           ORDER BY occurred_at DESC LIMIT 500""",
        oid, cutoff
    )
    critical = [_s(dict(r)) for r in rows if r["severity"] == "critical"]
    warnings = [_s(dict(r)) for r in rows if r["severity"] == "warning"]
    failures = [_s(dict(r)) for r in rows if r["outcome"] == "failure"]
    return {
        "period_days": days,
        "critical":    {"count": len(critical), "events": critical[:50]},
        "warnings":    {"count": len(warnings), "events": warnings[:50]},
        "failures":    {"count": len(failures), "events": failures[:50]},
    }

@app.get("/reports/actor")
async def report_actor(org_id: str, days: int = 30):
    oid    = _get_org(org_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows   = await db.fetch(
        """SELECT actor_email, actor_type,
                  COUNT(*) AS total,
                  SUM(CASE WHEN outcome='failure' THEN 1 ELSE 0 END) AS failures,
                  SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical_actions,
                  MAX(occurred_at) AS last_seen
           FROM audit_events
           WHERE org_id=$1 AND occurred_at>=$2
           GROUP BY actor_email, actor_type
           ORDER BY total DESC LIMIT 50""",
        oid, cutoff
    )
    return {"period_days": days, "actors": [_s(dict(r)) for r in rows]}

@app.get("/reports/timeline")
async def report_timeline(org_id: str, days: int = 7):
    """Hourly event counts for the last N days — useful for anomaly detection."""
    oid    = _get_org(org_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows   = await db.fetch(
        """SELECT
             date_trunc('hour', occurred_at) AS hour,
             COUNT(*) AS total,
             SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical
           FROM audit_events
           WHERE org_id=$1 AND occurred_at>=$2
           GROUP BY hour ORDER BY hour""",
        oid, cutoff
    )
    return {
        "period_days": days,
        "timeline": [_s(dict(r)) for r in rows],
    }

# ── EXPORT ────────────────────────────────────────────────────────────
@app.get("/export")
async def export_log(
    org_id:   str,
    format:   Literal["json", "csv"] = "json",
    days:     int = 90,
    severity: Optional[str] = None,
    service:  Optional[str] = None,
):
    """Download audit log as JSON or CSV — for compliance reporting."""
    oid    = _get_org(org_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filters = ["org_id=$1", "occurred_at>=$2"]
    params: list = [oid, cutoff]
    i = 3
    if severity:
        filters.append(f"severity=${i}")
        params.append(severity)
        i += 1
    if service:
        filters.append(f"service=${i}")
        params.append(service)
        i += 1
    where = " AND ".join(filters)
    rows  = await db.fetch(
        f"SELECT * FROM audit_events WHERE {where} ORDER BY occurred_at DESC",
        *params
    )

    if format == "json":
        data    = json.dumps([_s(dict(r)) for r in rows], indent=2, default=str)
        headers = {"Content-Disposition": f'attachment; filename="audit_{org_id}_{days}d.json"'}
        return StreamingResponse(
            io.StringIO(data), media_type="application/json", headers=headers
        )

    # CSV
    output  = io.StringIO()
    columns = ["id", "seq", "occurred_at", "actor_email", "actor_type", "actor_ip",
               "action", "resource_type", "resource_id", "resource_name",
               "service", "description", "outcome", "severity", "event_hash"]
    writer  = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        d = _s(dict(row))
        d.pop("metadata", None)
        d.pop("prev_hash", None)
        writer.writerow(d)
    output.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="audit_{org_id}_{days}d.csv"'}
    return StreamingResponse(output, media_type="text/csv", headers=headers)

# ── RETENTION ─────────────────────────────────────────────────────────
@app.get("/retention")
async def get_retention(org_id: str):
    oid = _get_org(org_id)
    row = await db.fetchrow(
        "SELECT * FROM retention_policies WHERE org_id=$1", oid
    )
    if not row:
        return {"org_id": org_id, "retention_days": DEFAULT_RETENTION_DAYS,
                "policy": "default"}
    return _s(dict(row))

@app.put("/retention")
async def set_retention(body: RetentionUpdate):
    if body.retention_days < 30:
        raise HTTPException(400, "Minimum retention is 30 days")
    if body.retention_days > 3650:
        raise HTTPException(400, "Maximum retention is 3650 days (10 years)")
    oid = _get_org(body.org_id)
    row = await db.fetchrow(
        """INSERT INTO retention_policies (org_id, retention_days)
           VALUES ($1,$2)
           ON CONFLICT (org_id)
           DO UPDATE SET retention_days=$2, updated_at=now()
           RETURNING *""",
        oid, body.retention_days
    )
    return _s(dict(row))
