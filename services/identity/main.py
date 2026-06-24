"""
Tessera Identity Service
========================
Port 8012 — Authentication, Users, Orgs, Auto-Deprovisioning

Endpoints
---------
POST /register               create org + first super_admin user
POST /login                  email+password → JWT
POST /magic-link             request passwordless link
POST /magic-link/verify      exchange token → JWT
POST /logout                 revoke all sessions instantly
POST /refresh                swap refresh token for new pair
GET  /verify                 validate JWT (called by all other services)
GET  /me                     current user + org

GET  /users                  list org users (admin)
GET  /users/{id}             get user
PUT  /users/{id}             update user fields
PATCH /users/{id}/status     activate / suspend / terminate
DELETE /users/{id}           hard delete (super_admin only)

GET  /orgs                   list all orgs (super_admin)
GET  /orgs/{id}              get org
GET  /orgs/{id}/members      list members
POST /orgs/{id}/invite       add member to org
DELETE /orgs/{id}/members/{uid}  remove + deprovision

GET  /deprovision/queue      users pending auto-cut
POST /deprovision/{id}       immediate manual deprovision
GET  /deprovision/log        audit trail of all revocations

Design notes
------------
- Sessions stored in Redis, not just JWT expiry — instant revocation on deprovision
- Hourly background job cuts contractors past end date, employees past termination+grace
- /verify is the single trust gate for every other Tessera service
"""

import asyncio
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

import asyncpg
import httpx
import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr

# ── CONFIG ────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@postgres:5432/tessera_identity")
REDIS_HOST   = os.getenv("REDIS_HOST", "redis")
REDIS_PORT   = int(os.getenv("REDIS_PORT", 6379))
JWT_SECRET   = os.getenv("JWT_SECRET", "change-me-in-prod-use-32-random-bytes")
JWT_ALGO     = "HS256"
ACCESS_TTL   = 3600          # 1 hour
REFRESH_TTL  = 86400 * 30    # 30 days
TRACE_URL    = os.getenv("TRACE_URL", "http://trace:8010")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(title="Tessera Identity", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db:        asyncpg.Pool        = None
rdb:       aioredis.Redis      = None
scheduler: AsyncIOScheduler    = None

# ── SCHEMA ────────────────────────────────────────────────────────────
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS orgs (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    slug       TEXT UNIQUE NOT NULL,
    domain     TEXT,
    plan       TEXT NOT NULL DEFAULT 'starter',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email             TEXT NOT NULL,
    password_hash     TEXT,
    first_name        TEXT NOT NULL DEFAULT '',
    last_name         TEXT NOT NULL DEFAULT '',
    user_type         TEXT NOT NULL DEFAULT 'employee'
                          CHECK (user_type IN ('employee','contractor','vendor','service_account')),
    role              TEXT NOT NULL DEFAULT 'member'
                          CHECK (role IN ('super_admin','org_admin','member')),
    status            TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','suspended','terminated')),
    contract_end_date DATE,
    termination_date  DATE,
    grace_hours       INT NOT NULL DEFAULT 24,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, email)
);

CREATE TABLE IF NOT EXISTS magic_links (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deprovision_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL,
    user_email   TEXT NOT NULL,
    org_id       UUID NOT NULL,
    reason       TEXT NOT NULL,
    triggered_by TEXT NOT NULL DEFAULT 'system',
    revoked_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# ── LIFECYCLE ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global db, rdb, scheduler
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(SCHEMA)
    rdb = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_auto_deprovision_job, "interval", hours=1, id="deprovision",
                      next_run_time=datetime.now())
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    if scheduler:
        scheduler.shutdown(wait=False)
    if db:
        await db.close()
    if rdb:
        await rdb.aclose()

# ── SESSION HELPERS ───────────────────────────────────────────────────
async def _create_session(user_id: str) -> tuple[str, str]:
    sid = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    access = jwt.encode(
        {"sub": user_id, "sid": sid, "exp": now + timedelta(seconds=ACCESS_TTL)},
        JWT_SECRET, algorithm=JWT_ALGO
    )
    refresh = jwt.encode(
        {"sub": user_id, "sid": sid, "type": "refresh",
         "exp": now + timedelta(seconds=REFRESH_TTL)},
        JWT_SECRET, algorithm=JWT_ALGO
    )
    pipe = rdb.pipeline()
    pipe.set(f"sess:{sid}", user_id, ex=REFRESH_TTL)
    pipe.sadd(f"usersess:{user_id}", sid)
    pipe.expire(f"usersess:{user_id}", REFRESH_TTL)
    await pipe.execute()
    return access, refresh

async def _revoke_user_sessions(user_id: str):
    """Kill every active session for this user — instant effect regardless of token TTL."""
    sids = await rdb.smembers(f"usersess:{user_id}")
    if sids:
        pipe = rdb.pipeline()
        for sid in sids:
            pipe.delete(f"sess:{sid}")
        pipe.delete(f"usersess:{user_id}")
        await pipe.execute()

async def _verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")
    sid = payload.get("sid")
    if not sid or not await rdb.exists(f"sess:{sid}"):
        raise HTTPException(401, "Session revoked or expired")
    return payload

# ── AUTH DEPENDENCIES ─────────────────────────────────────────────────
async def _current_user(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authorization header required")
    payload = await _verify_token(authorization.split(" ", 1)[1])
    user = await db.fetchrow("SELECT * FROM users WHERE id=$1", uuid.UUID(payload["sub"]))
    if not user or user["status"] != "active":
        raise HTTPException(401, "User inactive or not found")
    return dict(user)

async def _admin_user(user=Depends(_current_user)) -> dict:
    if user["role"] not in ("super_admin", "org_admin"):
        raise HTTPException(403, "Admin access required")
    return user

# ── PYDANTIC MODELS ───────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    org_name: str
    org_slug: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    org_slug: str

class MagicLinkRequest(BaseModel):
    email: EmailStr
    org_slug: str

class MagicLinkVerify(BaseModel):
    token: str

class RefreshRequest(BaseModel):
    refresh_token: str

class UserUpdate(BaseModel):
    first_name:        Optional[str]  = None
    last_name:         Optional[str]  = None
    role:              Optional[Literal["super_admin", "org_admin", "member"]] = None
    user_type:         Optional[Literal["employee", "contractor", "vendor", "service_account"]] = None
    contract_end_date: Optional[date] = None
    termination_date:  Optional[date] = None
    grace_hours:       Optional[int]  = None

class StatusUpdate(BaseModel):
    status: Literal["active", "suspended", "terminated"]
    reason: Optional[str] = "Manual status change"

class InviteMember(BaseModel):
    email:             EmailStr
    first_name:        str
    last_name:         str
    role:              Literal["org_admin", "member"] = "member"
    user_type:         Literal["employee", "contractor", "vendor", "service_account"] = "employee"
    contract_end_date: Optional[date] = None
    grace_hours:       int = 24

# ── DEPROVISIONING ENGINE ─────────────────────────────────────────────
async def _deprovision_user(user_id: str, reason: str, triggered_by: str = "system"):
    """
    Terminate a user: mark DB status, purge all Redis sessions, write audit log.
    Idempotent — safe to call if already terminated.
    """
    async with db.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", uuid.UUID(user_id))
        if not user or user["status"] == "terminated":
            return
        await conn.execute(
            "UPDATE users SET status='terminated', updated_at=now() WHERE id=$1",
            uuid.UUID(user_id)
        )
        await conn.execute(
            """INSERT INTO deprovision_log (user_id, user_email, org_id, reason, triggered_by)
               VALUES ($1, $2, $3, $4, $5)""",
            user["id"], user["email"], user["org_id"], reason, triggered_by
        )
    await _revoke_user_sessions(user_id)
    asyncio.create_task(_emit_deprovision_trace(
        user_id, user["email"], str(user["org_id"]), reason
    ))

async def _emit_deprovision_trace(user_id: str, email: str, org_id: str, reason: str):
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.post(f"{TRACE_URL}/run", json={
                "name": f"deprovision:{email}",
                "org_id": org_id,
                "branch": "alignment"
            })
            run_id = r.json().get("run_id", str(uuid.uuid4()))
            await c.post(f"{TRACE_URL}/span", json={
                "run_id": run_id, "name": "access_revoked",
                "model": "system", "branch": "alignment",
                "tokens_in": 0, "tokens_out": 0, "latency_ms": 0,
                "input_text": f"user_id={user_id} reason={reason}",
                "output_text": "all_sessions_revoked"
            })
            await c.patch(f"{TRACE_URL}/run/{run_id}",
                          json={"status": "completed", "qf_ratio": 1.0})
    except Exception:
        pass

async def _auto_deprovision_job():
    """
    Hourly scheduler job.
    - Contractors: cut when contract_end_date < today (no grace)
    - Employees / vendors: cut when termination_date + grace_hours <= now
    """
    today = date.today()
    now   = datetime.now(timezone.utc)
    async with db.acquire() as conn:
        contractors = await conn.fetch(
            """SELECT id FROM users
               WHERE status='active' AND user_type='contractor'
               AND contract_end_date IS NOT NULL AND contract_end_date < $1""",
            today
        )
        for u in contractors:
            await _deprovision_user(str(u["id"]), "Contract end date passed", "scheduler")

        leaving = await conn.fetch(
            """SELECT id, termination_date, grace_hours FROM users
               WHERE status='active' AND user_type IN ('employee','vendor')
               AND termination_date IS NOT NULL"""
        )
        for u in leaving:
            cut_at = (
                datetime.combine(u["termination_date"], datetime.min.time())
                .replace(tzinfo=timezone.utc)
                + timedelta(hours=u["grace_hours"])
            )
            if now >= cut_at:
                await _deprovision_user(
                    str(u["id"]),
                    f"Termination date passed (grace={u['grace_hours']}h)",
                    "scheduler"
                )

# ── HEALTH ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "identity"}

# ── AUTH ──────────────────────────────────────────────────────────────
@app.post("/register")
async def register(body: RegisterRequest):
    """Bootstrap: create an org + its first super_admin user."""
    async with db.acquire() as conn:
        if await conn.fetchrow("SELECT id FROM orgs WHERE slug=$1", body.org_slug):
            raise HTTPException(409, f"Org slug '{body.org_slug}' already taken")
        org = await conn.fetchrow(
            "INSERT INTO orgs (name, slug) VALUES ($1, $2) RETURNING *",
            body.org_name, body.org_slug
        )
        try:
            user = await conn.fetchrow(
                """INSERT INTO users
                     (org_id, email, password_hash, first_name, last_name, role)
                   VALUES ($1, $2, $3, $4, $5, 'super_admin') RETURNING *""",
                org["id"], body.email, pwd_ctx.hash(body.password),
                body.first_name, body.last_name
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, "Email already registered in this org")
    access, refresh = await _create_session(str(user["id"]))
    return {
        "access_token":  access,
        "refresh_token": refresh,
        "user": _user_dict(user),
        "org":  _serialize(dict(org))
    }

@app.post("/login")
async def login(body: LoginRequest):
    org = await db.fetchrow("SELECT * FROM orgs WHERE slug=$1", body.org_slug)
    if not org:
        raise HTTPException(404, "Org not found")
    user = await db.fetchrow(
        "SELECT * FROM users WHERE org_id=$1 AND email=$2", org["id"], body.email
    )
    if not user or not user["password_hash"] or not pwd_ctx.verify(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    if user["status"] != "active":
        raise HTTPException(403, f"Account is {user['status']}")
    access, refresh = await _create_session(str(user["id"]))
    return {"access_token": access, "refresh_token": refresh, "user": _user_dict(user)}

@app.post("/magic-link")
async def request_magic_link(body: MagicLinkRequest):
    org = await db.fetchrow("SELECT * FROM orgs WHERE slug=$1", body.org_slug)
    if not org:
        raise HTTPException(404, "Org not found")
    user = await db.fetchrow(
        "SELECT * FROM users WHERE org_id=$1 AND email=$2", org["id"], body.email
    )
    if not user:
        return {"message": "If that email is registered, a link has been sent"}
    token = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    await db.execute(
        "INSERT INTO magic_links (user_id, token, expires_at) VALUES ($1, $2, $3)",
        user["id"], token, expires
    )
    # TODO: send via email service. Token returned for dev convenience.
    return {"message": "Magic link created", "token": token, "expires_at": expires.isoformat()}

@app.post("/magic-link/verify")
async def verify_magic_link(body: MagicLinkVerify):
    link = await db.fetchrow(
        "SELECT * FROM magic_links WHERE token=$1 AND used=false AND expires_at > now()",
        body.token
    )
    if not link:
        raise HTTPException(401, "Invalid or expired magic link")
    await db.execute("UPDATE magic_links SET used=true WHERE id=$1", link["id"])
    user = await db.fetchrow("SELECT * FROM users WHERE id=$1", link["user_id"])
    if not user or user["status"] != "active":
        raise HTTPException(403, "Account inactive")
    access, refresh = await _create_session(str(user["id"]))
    return {"access_token": access, "refresh_token": refresh, "user": _user_dict(user)}

@app.post("/logout")
async def logout(user=Depends(_current_user)):
    await _revoke_user_sessions(str(user["id"]))
    return {"message": "Logged out — all sessions revoked"}

@app.post("/refresh")
async def refresh_token(body: RefreshRequest):
    payload = await _verify_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(401, "Not a refresh token")
    user = await db.fetchrow("SELECT * FROM users WHERE id=$1", uuid.UUID(payload["sub"]))
    if not user or user["status"] != "active":
        raise HTTPException(401, "User inactive")
    old_sid = payload["sid"]
    pipe = rdb.pipeline()
    pipe.delete(f"sess:{old_sid}")
    pipe.srem(f"usersess:{str(user['id'])}", old_sid)
    await pipe.execute()
    access, refresh = await _create_session(str(user["id"]))
    return {"access_token": access, "refresh_token": refresh}

@app.get("/verify")
async def verify(authorization: str = Header(None)):
    """
    Called by every other Tessera service to validate a request token.
    Returns user_id, org_id, role, user_type so callers can enforce their own RBAC.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authorization header required")
    payload = await _verify_token(authorization.split(" ", 1)[1])
    user = await db.fetchrow("SELECT * FROM users WHERE id=$1", uuid.UUID(payload["sub"]))
    if not user or user["status"] != "active":
        raise HTTPException(401, "User inactive or deprovisioned")
    return {
        "valid":     True,
        "user_id":   str(user["id"]),
        "org_id":    str(user["org_id"]),
        "email":     user["email"],
        "role":      user["role"],
        "user_type": user["user_type"],
        "status":    user["status"],
    }

@app.get("/me")
async def me(user=Depends(_current_user)):
    org = await db.fetchrow("SELECT * FROM orgs WHERE id=$1", user["org_id"])
    return {"user": _user_dict(user), "org": _serialize(dict(org))}

# ── USERS ─────────────────────────────────────────────────────────────
@app.get("/users")
async def list_users(user=Depends(_admin_user)):
    rows = await db.fetch(
        "SELECT * FROM users WHERE org_id=$1 ORDER BY created_at DESC", user["org_id"]
    )
    return [_user_dict(r) for r in rows]

@app.get("/users/{user_id}")
async def get_user(user_id: str, current=Depends(_current_user)):
    row = await db.fetchrow(
        "SELECT * FROM users WHERE id=$1 AND org_id=$2",
        uuid.UUID(user_id), current["org_id"]
    )
    if not row:
        raise HTTPException(404, "User not found")
    return _user_dict(row)

@app.put("/users/{user_id}")
async def update_user(user_id: str, body: UserUpdate, current=Depends(_admin_user)):
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not fields:
        raise HTTPException(400, "No fields to update")
    params = [uuid.UUID(user_id)]
    setters = []
    for i, (k, v) in enumerate(fields.items(), start=2):
        setters.append(f"{k}=${i}")
        params.append(v)
    params.append(current["org_id"])
    row = await db.fetchrow(
        f"UPDATE users SET {', '.join(setters)}, updated_at=now() "
        f"WHERE id=$1 AND org_id=${len(params)} RETURNING *",
        *params
    )
    if not row:
        raise HTTPException(404, "User not found")
    return _user_dict(row)

@app.patch("/users/{user_id}/status")
async def update_status(user_id: str, body: StatusUpdate, current=Depends(_admin_user)):
    if body.status == "terminated":
        await _deprovision_user(user_id, body.reason, current["email"])
        return {"message": "User deprovisioned — all sessions revoked", "user_id": user_id}
    if body.status == "suspended":
        await _revoke_user_sessions(user_id)
    await db.execute(
        "UPDATE users SET status=$1, updated_at=now() WHERE id=$2 AND org_id=$3",
        body.status, uuid.UUID(user_id), current["org_id"]
    )
    return {"message": f"User status → {body.status}", "user_id": user_id}

@app.delete("/users/{user_id}")
async def delete_user(user_id: str, current=Depends(_admin_user)):
    if current["role"] != "super_admin":
        raise HTTPException(403, "Only super_admin can hard-delete users")
    await _revoke_user_sessions(user_id)
    result = await db.execute(
        "DELETE FROM users WHERE id=$1 AND org_id=$2",
        uuid.UUID(user_id), current["org_id"]
    )
    if result == "DELETE 0":
        raise HTTPException(404, "User not found")
    return {"message": "User deleted"}

# ── ORGS ──────────────────────────────────────────────────────────────
@app.get("/orgs")
async def list_orgs(current=Depends(_current_user)):
    if current["role"] != "super_admin":
        raise HTTPException(403, "Super admin only")
    rows = await db.fetch("SELECT * FROM orgs ORDER BY created_at DESC")
    return [_serialize(dict(r)) for r in rows]

@app.get("/orgs/{org_id}")
async def get_org(org_id: str, current=Depends(_current_user)):
    row = await db.fetchrow("SELECT * FROM orgs WHERE id=$1", uuid.UUID(org_id))
    if not row:
        raise HTTPException(404, "Org not found")
    return _serialize(dict(row))

@app.get("/orgs/{org_id}/members")
async def list_members(org_id: str, current=Depends(_admin_user)):
    if str(current["org_id"]) != org_id and current["role"] != "super_admin":
        raise HTTPException(403, "Access denied")
    rows = await db.fetch(
        "SELECT * FROM users WHERE org_id=$1 ORDER BY created_at", uuid.UUID(org_id)
    )
    return [_user_dict(r) for r in rows]

@app.post("/orgs/{org_id}/invite")
async def invite_member(org_id: str, body: InviteMember, current=Depends(_admin_user)):
    if str(current["org_id"]) != org_id and current["role"] != "super_admin":
        raise HTTPException(403, "Access denied")
    try:
        user = await db.fetchrow(
            """INSERT INTO users
                 (org_id, email, first_name, last_name, role, user_type,
                  contract_end_date, grace_hours)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *""",
            uuid.UUID(org_id), body.email, body.first_name, body.last_name,
            body.role, body.user_type, body.contract_end_date, body.grace_hours
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Email already exists in this org")
    return _user_dict(user)

@app.delete("/orgs/{org_id}/members/{user_id}")
async def remove_member(org_id: str, user_id: str, current=Depends(_admin_user)):
    if str(current["org_id"]) != org_id and current["role"] != "super_admin":
        raise HTTPException(403, "Access denied")
    await _deprovision_user(user_id, "Removed from org", current["email"])
    return {"message": "Member deprovisioned and removed"}

# ── DEPROVISIONING ────────────────────────────────────────────────────
@app.get("/deprovision/queue")
async def deprovision_queue(current=Depends(_admin_user)):
    """Users who will be or should have been auto-cut."""
    today = date.today()
    now   = datetime.now(timezone.utc)
    async with db.acquire() as conn:
        contractors = await conn.fetch(
            """SELECT id, email, first_name, last_name, user_type, contract_end_date
               FROM users WHERE status='active' AND user_type='contractor'
               AND contract_end_date IS NOT NULL AND contract_end_date <= $1
               AND org_id=$2""",
            today, current["org_id"]
        )
        leaving = await conn.fetch(
            """SELECT id, email, first_name, last_name, user_type,
                      termination_date, grace_hours
               FROM users WHERE status='active' AND user_type IN ('employee','vendor')
               AND termination_date IS NOT NULL AND org_id=$1""",
            current["org_id"]
        )
    queue = [dict(r) | {"reason": "contract_expired"} for r in contractors]
    for u in leaving:
        cut_at = (
            datetime.combine(u["termination_date"], datetime.min.time())
            .replace(tzinfo=timezone.utc)
            + timedelta(hours=u["grace_hours"])
        )
        if now >= cut_at:
            queue.append(dict(u) | {"reason": "termination_passed"})
    return {"count": len(queue), "queue": [_serialize(q) for q in queue]}

@app.post("/deprovision/{user_id}")
async def force_deprovision(user_id: str, current=Depends(_admin_user)):
    await _deprovision_user(user_id, "Manual deprovision via API", current["email"])
    return {"message": "Deprovisioned immediately — all sessions revoked", "user_id": user_id}

@app.get("/deprovision/log")
async def deprovision_log(current=Depends(_admin_user)):
    rows = await db.fetch(
        "SELECT * FROM deprovision_log WHERE org_id=$1 ORDER BY revoked_at DESC LIMIT 100",
        current["org_id"]
    )
    return [_serialize(dict(r)) for r in rows]

# ── SERIALISATION HELPERS ─────────────────────────────────────────────
def _serialize(d: dict) -> dict:
    import uuid as _uuid
    return {
        k: (
            str(v)        if isinstance(v, _uuid.UUID)
            else v.isoformat() if isinstance(v, (datetime, date))
            else v
        )
        for k, v in d.items()
    }

def _user_dict(row) -> dict:
    d = dict(row)
    d.pop("password_hash", None)
    return _serialize(d)
