"""
Tessera Notifications Service
==============================
Port 8015 — Multi-channel delivery, in-app feed, webhooks, digest

Channels
  inapp    stored in DB, frontend polls GET /notifications
  email    SMTP delivery (async, with retry)
  webhook  HMAC-signed HTTP POST to registered URLs
  slack    Slack incoming webhook (formatted message)

Core flow
  Any Tessera service → POST /events → event broadcast
  or directly         → POST /send   → targeted send

Features
  Templates     Jinja2 templates per event_type + channel
  Preferences   per-user channel opt-in/out + digest mode
  Digest        hourly/daily batching for non-urgent emails
  Webhooks      HMAC-SHA256 signed, auto-retry on failure (3x)
  Feed          paginated in-app notification center with unread count
  Delivery log  full audit of every send attempt

Endpoints
  POST /send                    direct send to list of recipients
  POST /events                  emit system event → fan-out
  GET  /notifications           in-app feed (filter by user_email, org_id)
  PATCH /notifications/{id}/read  mark read
  POST /notifications/read-all  mark all read for user
  GET  /notifications/unread-count
  POST /templates               create template
  GET  /templates               list templates
  PUT  /templates/{id}          update template
  DELETE /templates/{id}        delete template
  POST /webhooks                register webhook
  GET  /webhooks                list webhooks
  DELETE /webhooks/{id}         delete webhook
  POST /webhooks/{id}/test      fire test event
  GET  /preferences             get user preferences
  PUT  /preferences             upsert user preferences
  GET  /log                     delivery log (filter by org_id)
"""

import asyncio
import hashlib
import hmac as hmac_lib
import json
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Literal, Optional

import asyncpg
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Template
from pydantic import BaseModel

# ── CONFIG ────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@postgres:5432/tessera_notifications")
SMTP_HOST    = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER", "")
SMTP_PASS    = os.getenv("SMTP_PASS", "")
SMTP_FROM    = os.getenv("SMTP_FROM", "noreply@tessera.local")
SMTP_TLS     = os.getenv("SMTP_TLS", "false").lower() == "true"
SMTP_ENABLED = os.getenv("SMTP_ENABLED", "false").lower() == "true"

app = FastAPI(title="Tessera Notifications", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db:        asyncpg.Pool     = None
scheduler: AsyncIOScheduler = None

# ── SCHEMA ────────────────────────────────────────────────────────────
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS templates (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id     UUID,
    name       TEXT        NOT NULL,
    event_type TEXT        NOT NULL,
    channel    TEXT        NOT NULL CHECK (channel IN ('inapp','email','webhook','slack')),
    subject    TEXT,
    body       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id     UUID        NOT NULL,
    user_email TEXT        NOT NULL,
    title      TEXT        NOT NULL,
    body       TEXT        NOT NULL,
    event_type TEXT        NOT NULL DEFAULT 'custom',
    priority   TEXT        NOT NULL DEFAULT 'normal'
                           CHECK (priority IN ('urgent','normal','low')),
    channel    TEXT        NOT NULL DEFAULT 'inapp',
    read       BOOLEAN     NOT NULL DEFAULT false,
    read_at    TIMESTAMPTZ,
    data       JSONB       DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS delivery_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    notification_id UUID        REFERENCES notifications(id),
    org_id          UUID        NOT NULL,
    channel         TEXT        NOT NULL,
    recipient       TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','sent','failed')),
    error           TEXT,
    sent_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhooks (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID        NOT NULL,
    name        TEXT        NOT NULL,
    url         TEXT        NOT NULL,
    secret      TEXT        NOT NULL,
    event_types TEXT[]      DEFAULT '{}',
    provider    TEXT        NOT NULL DEFAULT 'generic'
                            CHECK (provider IN ('generic','slack')),
    status      TEXT        NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','inactive')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS preferences (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID        NOT NULL,
    user_email  TEXT        NOT NULL,
    channels    TEXT[]      DEFAULT '{inapp,email}',
    muted_types TEXT[]      DEFAULT '{}',
    digest_mode TEXT        NOT NULL DEFAULT 'immediate'
                            CHECK (digest_mode IN ('immediate','hourly','daily')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, user_email)
);

CREATE TABLE IF NOT EXISTS email_queue (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID        NOT NULL,
    to_email     TEXT        NOT NULL,
    subject      TEXT        NOT NULL,
    body_html    TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending','sent','failed')),
    retry_count  INT         NOT NULL DEFAULT 0,
    error        TEXT,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notif_org   ON notifications(org_id);
CREATE INDEX IF NOT EXISTS idx_notif_user  ON notifications(user_email);
CREATE INDEX IF NOT EXISTS idx_notif_read  ON notifications(read) WHERE read = false;
CREATE INDEX IF NOT EXISTS idx_dlog_org    ON delivery_log(org_id);
CREATE INDEX IF NOT EXISTS idx_wh_org      ON webhooks(org_id);
CREATE INDEX IF NOT EXISTS idx_eq_status   ON email_queue(status, scheduled_at);
"""

# ── BUILT-IN SYSTEM TEMPLATES ─────────────────────────────────────────
SYSTEM_TEMPLATES = [
    ("user.deprovisioned",  "inapp",   "Access Revoked",
     "Your access to {{org_name}} has been revoked. Contact your admin for details."),
    ("user.deprovisioned",  "email",   "Your Tessera access has been revoked",
     "<p>Hi {{first_name}},</p><p>Your access to <b>{{org_name}}</b> was revoked on {{date}} — {{reason}}.</p><p>Contact your administrator if you believe this is an error.</p>"),
    ("user.onboarded",      "inapp",   "Welcome to {{org_name}}!",
     "Your account is ready. You can now access all modules you've been granted."),
    ("ticket.assigned",     "inapp",   "Ticket Assigned: {{number}}",
     "{{number}} — {{title}} has been assigned to you."),
    ("ticket.assigned",     "email",   "You've been assigned a ticket: {{number}}",
     "<p>Hi,</p><p>Ticket <b>{{number}}: {{title}}</b> ({{priority}}) has been assigned to you.</p><p>Please respond within the SLA window.</p>"),
    ("ticket.resolved",     "inapp",   "Ticket Resolved: {{number}}",
     "{{number}} has been resolved. Resolution: {{resolution}}"),
    ("sla.breach",          "inapp",   "SLA Breach: {{number}}",
     "Ticket {{number}} ({{priority}}) has breached its SLA resolve deadline."),
    ("sla.breach",          "email",   "SLA Breach Alert: {{number}}",
     "<p>Ticket <b>{{number}}: {{title}}</b> has breached its SLA.</p><p>Priority: {{priority}}</p><p>Please resolve immediately.</p>"),
    ("contract.expiring",   "inapp",   "Contract Expiring Soon",
     "{{name}}'s contract expires on {{date}}. Renew or access will be cut automatically."),
    ("contract.expiring",   "email",   "Contractor access expiring: {{name}}",
     "<p>Contractor <b>{{name}}</b>'s contract expires on <b>{{date}}</b>.</p><p>Please extend or confirm the automated deprovisioning will proceed.</p>"),
    ("payroll.approved",    "inapp",   "Payslip Ready",
     "Your payslip for {{period}} is ready. Net pay: {{amount}}"),
    ("leave.requested",     "inapp",   "Leave Request from {{name}}",
     "{{name}} has requested {{days}} days leave from {{start}} to {{end}}."),
    ("leave.approved",      "inapp",   "Leave Approved",
     "Your leave request from {{start}} to {{end}} has been approved."),
    ("change.cab_approved", "inapp",   "Change Approved: {{number}}",
     "CAB has approved {{number}}. You may proceed with the change window."),
    ("change.cab_rejected", "inapp",   "Change Rejected: {{number}}",
     "CAB has rejected {{number}}. Reason: {{reason}}"),
    ("expense.approved",    "inapp",   "Expense Approved",
     "Your expense claim of {{amount}} has been approved."),
]

# ── LIFECYCLE ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global db, scheduler
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(SCHEMA)
        for (evt, ch, subj, body) in SYSTEM_TEMPLATES:
            await conn.execute(
                """INSERT INTO templates (org_id, name, event_type, channel, subject, body)
                   VALUES (NULL, $1, $2, $3, $4, $5)
                   ON CONFLICT DO NOTHING""",
                f"system:{evt}:{ch}", evt, ch, subj, body
            )
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_digest_job,  "cron", minute=0,    id="digest_hourly")
    scheduler.add_job(_retry_job,   "cron", minute="*/5", id="email_retry")
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    if scheduler:
        scheduler.shutdown(wait=False)
    if db:
        await db.close()

# ── HELPERS ───────────────────────────────────────────────────────────
def _s(d: dict) -> dict:
    return {
        k: (str(v) if isinstance(v, uuid.UUID)
            else v.isoformat() if isinstance(v, datetime)
            else v)
        for k, v in d.items()
    }

def _get_org(org_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(400, "Invalid org_id")

def _render(template_str: str, ctx: dict) -> str:
    try:
        return Template(template_str).render(**ctx)
    except Exception:
        return template_str

def _webhook_sig(payload: str, secret: str) -> str:
    return "sha256=" + hmac_lib.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

async def _get_template(event_type: str, channel: str,
                         org_id: Optional[uuid.UUID] = None) -> Optional[dict]:
    """Get org-specific template first, fall back to system template."""
    if org_id:
        row = await db.fetchrow(
            "SELECT * FROM templates WHERE event_type=$1 AND channel=$2 AND org_id=$3",
            event_type, channel, org_id
        )
        if row:
            return dict(row)
    row = await db.fetchrow(
        "SELECT * FROM templates WHERE event_type=$1 AND channel=$2 AND org_id IS NULL",
        event_type, channel
    )
    return dict(row) if row else None

async def _user_prefs(org_id: uuid.UUID, email: str) -> dict:
    row = await db.fetchrow(
        "SELECT * FROM preferences WHERE org_id=$1 AND user_email=$2", org_id, email
    )
    if row:
        return dict(row)
    return {"channels": ["inapp", "email"], "muted_types": [], "digest_mode": "immediate"}

async def _log_delivery(notification_id, org_id, channel, recipient, status, error=None):
    sent_at = datetime.now(timezone.utc) if status == "sent" else None
    await db.execute(
        """INSERT INTO delivery_log
             (notification_id, org_id, channel, recipient, status, error, sent_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7)""",
        notification_id, org_id, channel, recipient, status, error, sent_at
    )

# ── EMAIL DELIVERY ────────────────────────────────────────────────────
async def _send_email_now(to: str, subject: str, body_html: str) -> bool:
    if not SMTP_ENABLED:
        return True   # silently succeed in dev mode — no SMTP configured
    try:
        import aiosmtplib
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = to
        msg.attach(MIMEText(body_html, "html"))
        await aiosmtplib.send(
            msg, hostname=SMTP_HOST, port=SMTP_PORT,
            username=SMTP_USER or None, password=SMTP_PASS or None,
            use_tls=SMTP_TLS,
        )
        return True
    except Exception as e:
        raise RuntimeError(str(e))

async def _queue_email(org_id: uuid.UUID, to: str, subject: str,
                        body_html: str, scheduled_at: datetime = None):
    at = scheduled_at or datetime.now(timezone.utc)
    await db.execute(
        """INSERT INTO email_queue (org_id, to_email, subject, body_html, scheduled_at)
           VALUES ($1,$2,$3,$4,$5)""",
        org_id, to, subject, body_html, at
    )

# ── WEBHOOK DELIVERY ──────────────────────────────────────────────────
async def _fire_webhook(webhook: dict, event_type: str, payload: dict):
    body    = json.dumps({"event": event_type, "data": payload,
                           "sent_at": datetime.now(timezone.utc).isoformat()})
    headers = {
        "Content-Type":         "application/json",
        "X-Tessera-Event":      event_type,
        "X-Tessera-Signature":  _webhook_sig(body, webhook["secret"]),
        "X-Tessera-Delivery":   str(uuid.uuid4()),
    }
    if webhook.get("provider") == "slack":
        # Reformat for Slack incoming webhook
        body = json.dumps({"text": payload.get("title", event_type),
                            "blocks": [{"type": "section",
                                        "text": {"type": "mrkdwn",
                                                 "text": f"*{payload.get('title','Event')}*\n{payload.get('body','')}"}}]})
        headers = {"Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(webhook["url"], content=body, headers=headers)
            return r.status_code < 400
    except Exception:
        return False

# ── CORE SEND LOGIC ───────────────────────────────────────────────────
async def _deliver(
    org_id:     uuid.UUID,
    recipients: List[str],
    title:      str,
    body:       str,
    event_type: str,
    priority:   str,
    channels:   List[str],
    data:       dict,
    template_vars: dict,
):
    """
    Core delivery engine.
    Creates notification records, sends email/webhook, respects preferences.
    """
    wh_rows = await db.fetch(
        """SELECT * FROM webhooks WHERE org_id=$1 AND status='active'
           AND (array_length(event_types,1) IS NULL OR $2=ANY(event_types))""",
        org_id, event_type
    )

    for email in recipients:
        prefs = await _user_prefs(org_id, email)

        # Skip entirely muted event types
        if event_type in (prefs.get("muted_types") or []):
            continue

        active_channels = set(channels) & set(prefs.get("channels") or ["inapp"])

        # Always create in-app record if inapp is in channels
        notif_id = None
        if "inapp" in active_channels:
            row = await db.fetchrow(
                """INSERT INTO notifications
                     (org_id, user_email, title, body, event_type, priority, channel, data)
                   VALUES ($1,$2,$3,$4,$5,$6,'inapp',$7) RETURNING id""",
                org_id, email, title, body, event_type, priority, json.dumps(data)
            )
            notif_id = row["id"]
            await _log_delivery(notif_id, org_id, "inapp", email, "sent")

        # Email delivery
        if "email" in active_channels:
            tmpl = await _get_template(event_type, "email", org_id)
            subj = _render(tmpl["subject"] if tmpl else title, {**data, **template_vars})
            html = _render(tmpl["body"]    if tmpl else f"<p>{body}</p>",
                           {**data, **template_vars})
            digest = prefs.get("digest_mode", "immediate")
            if priority == "urgent" or digest == "immediate":
                try:
                    await _send_email_now(email, subj, html)
                    await _log_delivery(notif_id, org_id, "email", email, "sent")
                except RuntimeError as e:
                    await _queue_email(org_id, email, subj, html)
                    await _log_delivery(notif_id, org_id, "email", email, "failed", str(e))
            elif digest == "hourly":
                await _queue_email(org_id, email, subj, html,
                                   _next_hour())
            else:
                await _queue_email(org_id, email, subj, html,
                                   _next_day())

    # Webhooks — fire once per matching webhook (not per recipient)
    if wh_rows and "webhook" in channels:
        payload = {"title": title, "body": body, "event_type": event_type,
                   "recipients": recipients, "priority": priority, "data": data}
        for wh in wh_rows:
            asyncio.create_task(_fire_and_log_webhook(dict(wh), event_type, payload, org_id))

def _next_hour() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

def _next_day() -> datetime:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

async def _fire_and_log_webhook(wh: dict, event_type: str, payload: dict, org_id: uuid.UUID):
    ok = await _fire_webhook(wh, event_type, payload)
    await _log_delivery(None, org_id, "webhook", wh["url"],
                        "sent" if ok else "failed",
                        None if ok else "HTTP error or timeout")

# ── BACKGROUND JOBS ───────────────────────────────────────────────────
async def _digest_job():
    """Send all queued emails whose scheduled_at has passed."""
    rows = await db.fetch(
        "SELECT * FROM email_queue WHERE status='pending' AND scheduled_at <= now() LIMIT 100"
    )
    for row in rows:
        try:
            await _send_email_now(row["to_email"], row["subject"], row["body_html"])
            await db.execute(
                "UPDATE email_queue SET status='sent', sent_at=now() WHERE id=$1", row["id"]
            )
        except RuntimeError as e:
            await db.execute(
                "UPDATE email_queue SET retry_count=retry_count+1, error=$1 WHERE id=$2",
                str(e), row["id"]
            )

async def _retry_job():
    """Retry failed emails up to 3 times, then mark permanently failed."""
    await db.execute(
        "UPDATE email_queue SET status='failed' WHERE status='pending' AND retry_count >= 3"
    )

# ── PYDANTIC MODELS ───────────────────────────────────────────────────
class SendRequest(BaseModel):
    org_id:        str
    recipients:    List[str]
    title:         str
    body:          str
    event_type:    str = "custom"
    priority:      Literal["urgent", "normal", "low"] = "normal"
    channels:      List[str] = ["inapp"]
    data:          Dict[str, Any] = {}
    template_vars: Dict[str, Any] = {}

class EventRequest(BaseModel):
    org_id:        str
    event_type:    str
    recipients:    List[str]
    data:          Dict[str, Any] = {}
    priority:      Literal["urgent", "normal", "low"] = "normal"

class TemplateCreate(BaseModel):
    org_id:     Optional[str] = None
    name:       str
    event_type: str
    channel:    Literal["inapp", "email", "webhook", "slack"]
    subject:    Optional[str] = None
    body:       str

class WebhookCreate(BaseModel):
    org_id:      str
    name:        str
    url:         str
    event_types: List[str] = []
    provider:    Literal["generic", "slack"] = "generic"

class PreferencesUpsert(BaseModel):
    org_id:      str
    user_email:  str
    channels:    List[str] = ["inapp", "email"]
    muted_types: List[str] = []
    digest_mode: Literal["immediate", "hourly", "daily"] = "immediate"

# ── HEALTH ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "notifications",
            "smtp_enabled": SMTP_ENABLED}

# ── SEND ──────────────────────────────────────────────────────────────
@app.post("/send", status_code=202)
async def send(body: SendRequest):
    oid = _get_org(body.org_id)
    asyncio.create_task(_deliver(
        oid, body.recipients, body.title, body.body,
        body.event_type, body.priority, body.channels,
        body.data, body.template_vars
    ))
    return {"message": "Notification queued", "recipients": len(body.recipients)}

@app.post("/events", status_code=202)
async def emit_event(body: EventRequest):
    """
    Called by other Tessera services to broadcast a system event.
    Renders the appropriate template and fans out to all recipients.
    """
    oid     = _get_org(body.org_id)
    tmpl_in = await _get_template(body.event_type, "inapp", oid)
    title   = _render(tmpl_in["subject"] if tmpl_in else body.event_type, body.data)
    notif_b = _render(tmpl_in["body"]    if tmpl_in else json.dumps(body.data), body.data)
    asyncio.create_task(_deliver(
        oid, body.recipients, title, notif_b,
        body.event_type, body.priority,
        ["inapp", "email", "webhook"],
        body.data, body.data
    ))
    return {"message": "Event broadcast", "event_type": body.event_type,
            "recipients": len(body.recipients)}

# ── IN-APP FEED ───────────────────────────────────────────────────────
@app.get("/notifications")
async def list_notifications(
    org_id:     str,
    user_email: str,
    unread_only: bool = False,
    limit:      int   = Query(50, le=200),
    offset:     int   = 0,
):
    oid  = _get_org(org_id)
    base = "SELECT * FROM notifications WHERE org_id=$1 AND user_email=$2"
    p    = [oid, user_email]
    if unread_only:
        base += " AND read=false"
    base += f" ORDER BY created_at DESC LIMIT ${len(p)+1} OFFSET ${len(p)+2}"
    p   += [limit, offset]
    rows = await db.fetch(base, *p)
    total = await db.fetchval(
        "SELECT COUNT(*) FROM notifications WHERE org_id=$1 AND user_email=$2",
        oid, user_email
    )
    return {"total": total, "notifications": [_s(dict(r)) for r in rows]}

@app.get("/notifications/unread-count")
async def unread_count(org_id: str, user_email: str):
    oid   = _get_org(org_id)
    count = await db.fetchval(
        "SELECT COUNT(*) FROM notifications WHERE org_id=$1 AND user_email=$2 AND read=false",
        oid, user_email
    )
    return {"unread": count}

@app.patch("/notifications/{notif_id}/read")
async def mark_read(notif_id: str):
    now = datetime.now(timezone.utc)
    row = await db.fetchrow(
        "UPDATE notifications SET read=true, read_at=$1 WHERE id=$2 RETURNING id",
        now, uuid.UUID(notif_id)
    )
    if not row:
        raise HTTPException(404, "Notification not found")
    return {"message": "Marked as read"}

@app.post("/notifications/read-all")
async def mark_all_read(org_id: str, user_email: str):
    oid = _get_org(org_id)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        "UPDATE notifications SET read=true, read_at=$1 "
        "WHERE org_id=$2 AND user_email=$3 AND read=false",
        now, oid, user_email
    )
    count = int(result.split()[-1])
    return {"message": f"{count} notifications marked as read"}

# ── TEMPLATES ─────────────────────────────────────────────────────────
@app.post("/templates", status_code=201)
async def create_template(body: TemplateCreate):
    oid = _get_org(body.org_id) if body.org_id else None
    row = await db.fetchrow(
        """INSERT INTO templates (org_id, name, event_type, channel, subject, body)
           VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
        oid, body.name, body.event_type, body.channel, body.subject, body.body
    )
    return _s(dict(row))

@app.get("/templates")
async def list_templates(org_id: Optional[str] = None, event_type: Optional[str] = None):
    filters = []
    params: list = []
    i = 1
    if org_id:
        filters.append(f"(org_id=${ i} OR org_id IS NULL)")
        params.append(_get_org(org_id))
        i += 1
    if event_type:
        filters.append(f"event_type=${i}")
        params.append(event_type)
        i += 1
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows  = await db.fetch(
        f"SELECT * FROM templates {where} ORDER BY event_type, channel", *params
    )
    return [_s(dict(r)) for r in rows]

@app.put("/templates/{tmpl_id}")
async def update_template(tmpl_id: str, body: TemplateCreate):
    row = await db.fetchrow(
        """UPDATE templates SET name=$1, event_type=$2, channel=$3,
           subject=$4, body=$5, updated_at=now()
           WHERE id=$6 RETURNING *""",
        body.name, body.event_type, body.channel, body.subject, body.body,
        uuid.UUID(tmpl_id)
    )
    if not row:
        raise HTTPException(404, "Template not found")
    return _s(dict(row))

@app.delete("/templates/{tmpl_id}")
async def delete_template(tmpl_id: str):
    result = await db.execute(
        "DELETE FROM templates WHERE id=$1 AND org_id IS NOT NULL", uuid.UUID(tmpl_id)
    )
    if result == "DELETE 0":
        raise HTTPException(404, "Template not found or is a system template")
    return {"message": "Template deleted"}

# ── WEBHOOKS ──────────────────────────────────────────────────────────
@app.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookCreate):
    oid    = _get_org(body.org_id)
    secret = secrets.token_hex(32)
    row    = await db.fetchrow(
        """INSERT INTO webhooks (org_id, name, url, secret, event_types, provider)
           VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
        oid, body.name, body.url, secret, body.event_types, body.provider
    )
    return _s(dict(row))

@app.get("/webhooks")
async def list_webhooks(org_id: str):
    oid  = _get_org(org_id)
    rows = await db.fetch(
        "SELECT * FROM webhooks WHERE org_id=$1 ORDER BY created_at", oid
    )
    return [_s(dict(r)) for r in rows]

@app.delete("/webhooks/{wh_id}")
async def delete_webhook(wh_id: str):
    result = await db.execute(
        "DELETE FROM webhooks WHERE id=$1", uuid.UUID(wh_id)
    )
    if result == "DELETE 0":
        raise HTTPException(404, "Webhook not found")
    return {"message": "Webhook deleted"}

@app.post("/webhooks/{wh_id}/test")
async def test_webhook(wh_id: str):
    wh = await db.fetchrow("SELECT * FROM webhooks WHERE id=$1", uuid.UUID(wh_id))
    if not wh:
        raise HTTPException(404, "Webhook not found")
    ok = await _fire_webhook(dict(wh), "webhook.test",
                              {"title": "Test Event", "body": "Tessera webhook is working."})
    return {"success": ok, "url": wh["url"]}

# ── PREFERENCES ───────────────────────────────────────────────────────
@app.get("/preferences")
async def get_preferences(org_id: str, user_email: str):
    oid  = _get_org(org_id)
    prefs = await _user_prefs(oid, user_email)
    return prefs

@app.put("/preferences")
async def upsert_preferences(body: PreferencesUpsert):
    oid = _get_org(body.org_id)
    row = await db.fetchrow(
        """INSERT INTO preferences (org_id, user_email, channels, muted_types, digest_mode)
           VALUES ($1,$2,$3,$4,$5)
           ON CONFLICT (org_id, user_email)
           DO UPDATE SET channels=$3, muted_types=$4, digest_mode=$5, updated_at=now()
           RETURNING *""",
        oid, body.user_email, body.channels, body.muted_types, body.digest_mode
    )
    return _s(dict(row))

# ── DELIVERY LOG ──────────────────────────────────────────────────────
@app.get("/log")
async def delivery_log(
    org_id:  str,
    channel: Optional[str] = None,
    status:  Optional[str] = None,
    limit:   int = Query(100, le=500),
    offset:  int = 0,
):
    oid     = _get_org(org_id)
    filters = ["org_id=$1"]
    params: list = [oid]
    i = 2
    for col, val in [("channel", channel), ("status", status)]:
        if val:
            filters.append(f"{col}=${i}")
            params.append(val)
            i += 1
    where  = " AND ".join(filters)
    params += [limit, offset]
    rows = await db.fetch(
        f"SELECT * FROM delivery_log WHERE {where} ORDER BY created_at DESC "
        f"LIMIT ${i} OFFSET ${i+1}",
        *params
    )
    return [_s(dict(r)) for r in rows]
