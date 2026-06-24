"""
Tessera WorkOS Layer
====================
Port 8014 — Enterprise SSO, Directory Sync (SCIM 2.0), Admin Portal

SSO
  SAML 2.0 SP  Okta, Azure AD, PingFederate, ADFS, Google Workspace
  OIDC RP      Okta, Azure AD, Google, any OIDC-compliant provider
  Flow: /sso/authorize → IdP → /sso/saml/acs or /sso/oidc/callback → profile code
  Callers exchange the one-time code at GET /sso/profile/{code} for user attributes

Directory Sync (SCIM 2.0)
  We ARE the SCIM server — IdP pushes user/group changes to us
  Each directory has a bearer token; IdP includes it on every SCIM call
  On provision/deprovision we notify the Identity service so access is cut instantly

Admin Portal
  POST /portal/links  generate a magic link for org admins to self-configure
  GET  /portal/{tok}  validate token, return org's SSO + SCIM setup status

Connections API
  POST /connections              create SSO connection (saml or oidc)
  GET  /connections              list connections for org
  GET  /connections/{id}         connection detail + SP config to give IdP
  PATCH /connections/{id}        update connection
  POST /connections/{id}/activate  go live
  GET  /connections/{id}/metadata  download SP metadata XML

Directories API
  POST /directories              create SCIM directory
  GET  /directories              list directories
  GET  /directories/{id}         get directory
  POST /directories/{id}/token   rotate bearer token
  DELETE /directories/{id}       delete directory + all synced users/groups

SCIM 2.0 endpoints (IdP calls these)
  /scim/v2/Users  GET POST
  /scim/v2/Users/{id}  GET PUT PATCH DELETE
  /scim/v2/Groups  GET POST
  /scim/v2/Groups/{id}  GET PUT PATCH DELETE

Reports
  GET /reports/summary    connections + directory health per org
"""

import asyncio
import base64
import json
import os
import secrets
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote, urlencode

import asyncpg
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from lxml import etree
from pydantic import BaseModel

# ── CONFIG ────────────────────────────────────────────────────────────
DATABASE_URL  = os.getenv("DATABASE_URL",  "postgresql://tessera:tessera@postgres:5432/tessera_workos")
IDENTITY_URL  = os.getenv("IDENTITY_URL",  "http://identity:8012")
TRACE_URL     = os.getenv("TRACE_URL",     "http://trace:8010")
BASE_URL      = os.getenv("BASE_URL",      "http://localhost:8014")
PROFILE_TTL   = 300   # 5 min — one-time profile code expiry
PORTAL_TTL    = 3600  # 1 hr — admin portal link expiry

SCIM_USER_SCHEMA  = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_SCHEMA  = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"

app = FastAPI(title="Tessera WorkOS", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db: asyncpg.Pool = None

# ── SCHEMA ────────────────────────────────────────────────────────────
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS sso_connections (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           UUID        NOT NULL,
    name             TEXT        NOT NULL,
    provider         TEXT        NOT NULL,
    connection_type  TEXT        NOT NULL CHECK (connection_type IN ('saml','oidc')),
    status           TEXT        NOT NULL DEFAULT 'draft'
                                 CHECK (status IN ('draft','active','inactive')),
    domains          TEXT[]      DEFAULT '{}',
    attribute_map    JSONB       DEFAULT '{}',

    -- SAML
    idp_entity_id    TEXT,
    idp_sso_url      TEXT,
    idp_certificate  TEXT,

    -- OIDC
    client_id        TEXT,
    client_secret    TEXT,
    discovery_url    TEXT,
    scopes           TEXT[]      DEFAULT '{openid,profile,email}',

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sso_sessions (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID        NOT NULL REFERENCES sso_connections(id) ON DELETE CASCADE,
    org_id        UUID        NOT NULL,
    state         TEXT        UNIQUE NOT NULL,
    nonce         TEXT,
    redirect_uri  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS sso_profile_codes (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    code          TEXT        UNIQUE NOT NULL,
    connection_id UUID        NOT NULL,
    org_id        UUID        NOT NULL,
    profile       JSONB       NOT NULL,
    used          BOOLEAN     NOT NULL DEFAULT false,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scim_directories (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID        NOT NULL,
    name         TEXT        NOT NULL,
    provider     TEXT        NOT NULL,
    bearer_token TEXT        UNIQUE NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active','inactive')),
    last_sync_at TIMESTAMPTZ,
    user_count   INT         NOT NULL DEFAULT 0,
    group_count  INT         NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scim_users (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    directory_id  UUID        NOT NULL REFERENCES scim_directories(id) ON DELETE CASCADE,
    org_id        UUID        NOT NULL,
    external_id   TEXT        NOT NULL,
    username      TEXT        NOT NULL,
    email         TEXT        NOT NULL,
    first_name    TEXT,
    last_name     TEXT,
    display_name  TEXT,
    active        BOOLEAN     NOT NULL DEFAULT true,
    raw_attrs     JSONB       DEFAULT '{}',
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(directory_id, external_id)
);

CREATE TABLE IF NOT EXISTS scim_groups (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    directory_id  UUID        NOT NULL REFERENCES scim_directories(id) ON DELETE CASCADE,
    org_id        UUID        NOT NULL,
    external_id   TEXT        NOT NULL,
    display_name  TEXT        NOT NULL,
    member_ids    TEXT[]      DEFAULT '{}',
    raw_data      JSONB       DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(directory_id, external_id)
);

CREATE TABLE IF NOT EXISTS portal_links (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id     UUID        NOT NULL,
    token      TEXT        UNIQUE NOT NULL,
    intent     TEXT        NOT NULL DEFAULT 'sso',
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN     NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conn_org    ON sso_connections(org_id);
CREATE INDEX IF NOT EXISTS idx_conn_domain ON sso_connections USING GIN(domains);
CREATE INDEX IF NOT EXISTS idx_sdir_org    ON scim_directories(org_id);
CREATE INDEX IF NOT EXISTS idx_suser_dir   ON scim_users(directory_id);
CREATE INDEX IF NOT EXISTS idx_suser_email ON scim_users(email);
CREATE INDEX IF NOT EXISTS idx_sgrp_dir    ON scim_groups(directory_id);
"""

# ── LIFECYCLE ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(SCHEMA)

@app.on_event("shutdown")
async def shutdown():
    if db:
        await db.close()

# ── HELPERS ───────────────────────────────────────────────────────────
def _s(d: dict) -> dict:
    result = {}
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            result[k] = str(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        elif isinstance(v, list):
            result[k] = [str(i) if isinstance(i, uuid.UUID) else i for i in v]
        else:
            result[k] = v
    return result

def _get_org(org_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(400, "Invalid org_id")

# ── SAML SP HELPERS ───────────────────────────────────────────────────
def _build_authn_request(sp_entity_id: str, acs_url: str,
                          idp_sso_url: str, relay_state: str = "") -> str:
    """Generate SAMLRequest and return full redirect URL."""
    rid     = f"_{uuid.uuid4().hex}"
    instant = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    xml = (
        f'<samlp:AuthnRequest'
        f' xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
        f' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"'
        f' ID="{rid}" Version="2.0" IssueInstant="{instant}"'
        f' Destination="{idp_sso_url}"'
        f' AssertionConsumerServiceURL="{acs_url}"'
        f' ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
        f'<saml:Issuer>{sp_entity_id}</saml:Issuer>'
        f'</samlp:AuthnRequest>'
    )
    deflated = zlib.compress(xml.encode())[2:-4]
    encoded  = base64.b64encode(deflated).decode()
    params   = {"SAMLRequest": encoded}
    if relay_state:
        params["RelayState"] = relay_state
    return f"{idp_sso_url}?{urlencode(params)}"

def _parse_saml_response(saml_b64: str, idp_certificate: str) -> dict:
    """
    Parse + validate SAMLResponse.
    Validates XML signature using the IdP certificate, then extracts profile.
    """
    try:
        xml_bytes = base64.b64decode(saml_b64)
    except Exception:
        raise ValueError("SAMLResponse is not valid base64")

    # Validate XML signature
    cert = idp_certificate.strip()
    if not cert.startswith("-----"):
        cert = f"-----BEGIN CERTIFICATE-----\n{cert}\n-----END CERTIFICATE-----"
    try:
        from signxml import XMLVerifier
        XMLVerifier().verify(xml_bytes, x509_cert=cert)
    except Exception as e:
        raise ValueError(f"SAML signature invalid: {e}")

    root = etree.fromstring(xml_bytes)
    ns   = {
        "saml":  "urn:oasis:names:tc:SAML:2.0:assertion",
        "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    }

    # Check status
    sc = root.find(".//samlp:StatusCode", ns)
    if sc is not None and "Success" not in sc.get("Value", ""):
        raise ValueError("SAML response status is not Success")

    name_id = root.find(".//saml:NameID", ns)
    email   = name_id.text.strip() if name_id is not None else None

    attrs: dict = {}
    for attr in root.findall(".//saml:Attribute", ns):
        key    = attr.get("Name", "").split("/")[-1].split(":")[-1]
        values = [v.text for v in attr.findall("saml:AttributeValue", ns) if v.text]
        attrs[key] = values[0] if len(values) == 1 else values

    return {
        "email":          email or attrs.get("email") or attrs.get("emailAddress"),
        "first_name":     attrs.get("firstName") or attrs.get("givenName"),
        "last_name":      attrs.get("lastName")  or attrs.get("surname"),
        "idp_id":         email or attrs.get("sub", ""),
        "groups":         attrs.get("groups", []) if isinstance(attrs.get("groups"), list) else [],
        "raw_attributes": attrs,
    }

# ── OIDC HELPERS ──────────────────────────────────────────────────────
async def _oidc_discovery(discovery_url: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(discovery_url)
        r.raise_for_status()
        return r.json()

async def _oidc_exchange(token_url: str, client_id: str, client_secret: str,
                          code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(token_url, data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  redirect_uri,
            "client_id":     client_id,
            "client_secret": client_secret,
        })
        if r.status_code != 200:
            raise ValueError(f"Token exchange failed: {r.text}")
        return r.json()

async def _oidc_userinfo(userinfo_url: str, access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(userinfo_url, headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()

# ── SCIM AUTH DEPENDENCY ──────────────────────────────────────────────
async def _scim_auth(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "SCIM bearer token required")
    token = authorization.split(" ", 1)[1]
    directory = await db.fetchrow(
        "SELECT * FROM scim_directories WHERE bearer_token=$1 AND status='active'",
        token
    )
    if not directory:
        raise HTTPException(401, "Invalid or inactive SCIM token")
    await db.execute(
        "UPDATE scim_directories SET last_sync_at=now() WHERE id=$1", directory["id"]
    )
    return dict(directory)

def _scim_user_to_dict(row: dict) -> dict:
    return {
        "schemas":     [SCIM_USER_SCHEMA],
        "id":          str(row["id"]),
        "externalId":  row["external_id"],
        "userName":    row["username"],
        "name": {
            "givenName":  row["first_name"] or "",
            "familyName": row["last_name"]  or "",
            "formatted":  row["display_name"] or "",
        },
        "emails": [{"value": row["email"], "primary": True}],
        "active":       row["active"],
        "meta": {
            "resourceType": "User",
            "created":      row["created_at"].isoformat() if isinstance(row["created_at"], datetime) else row["created_at"],
            "lastModified": row["updated_at"].isoformat() if isinstance(row["updated_at"], datetime) else row["updated_at"],
        }
    }

def _scim_group_to_dict(row: dict) -> dict:
    return {
        "schemas":     [SCIM_GROUP_SCHEMA],
        "id":          str(row["id"]),
        "externalId":  row["external_id"],
        "displayName": row["display_name"],
        "members":     [{"value": m} for m in (row.get("member_ids") or [])],
        "meta": {
            "resourceType": "Group",
            "created":      row["created_at"].isoformat() if isinstance(row["created_at"], datetime) else row["created_at"],
        }
    }

def _scim_list(resources: list, total: int) -> dict:
    return {
        "schemas":      [SCIM_LIST_SCHEMA],
        "totalResults": total,
        "startIndex":   1,
        "itemsPerPage": len(resources),
        "Resources":    resources,
    }

def _scim_error(status: int, detail: str) -> dict:
    return {"schemas": [SCIM_ERROR_SCHEMA], "status": str(status), "detail": detail}

async def _notify_identity_deprovision(email: str, org_id: str):
    """Best-effort notification to identity service that a user was SCIM-deprovisioned."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            await c.post(f"{IDENTITY_URL}/deprovision/scim",
                         json={"email": email, "org_id": org_id})
    except Exception:
        pass

# ── PYDANTIC MODELS ───────────────────────────────────────────────────
class ConnectionCreate(BaseModel):
    org_id:          str
    name:            str
    provider:        str
    connection_type: Literal["saml", "oidc"]
    domains:         List[str] = []
    attribute_map:   Dict[str, str] = {}
    idp_entity_id:   Optional[str] = None
    idp_sso_url:     Optional[str] = None
    idp_certificate: Optional[str] = None
    client_id:       Optional[str] = None
    client_secret:   Optional[str] = None
    discovery_url:   Optional[str] = None
    scopes:          List[str]     = ["openid", "profile", "email"]

class ConnectionUpdate(BaseModel):
    name:            Optional[str]       = None
    domains:         Optional[List[str]] = None
    attribute_map:   Optional[Dict[str, str]] = None
    idp_entity_id:   Optional[str]       = None
    idp_sso_url:     Optional[str]       = None
    idp_certificate: Optional[str]       = None
    client_id:       Optional[str]       = None
    client_secret:   Optional[str]       = None
    discovery_url:   Optional[str]       = None
    scopes:          Optional[List[str]] = None

class DirectoryCreate(BaseModel):
    org_id:   str
    name:     str
    provider: str

class PortalLinkCreate(BaseModel):
    org_id:  str
    intent:  Literal["sso", "dsync", "audit_logs"] = "sso"

class ScimUserCreate(BaseModel):
    userName:    str
    externalId:  Optional[str] = None
    name:        Optional[Dict[str, str]] = None
    emails:      Optional[List[Dict]]     = None
    active:      bool = True
    displayName: Optional[str] = None

# ── HEALTH ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "workos"}

# ── SSO CONNECTIONS ───────────────────────────────────────────────────
@app.post("/connections", status_code=201)
async def create_connection(body: ConnectionCreate):
    oid = _get_org(body.org_id)
    row = await db.fetchrow(
        """INSERT INTO sso_connections
             (org_id, name, provider, connection_type, domains, attribute_map,
              idp_entity_id, idp_sso_url, idp_certificate,
              client_id, client_secret, discovery_url, scopes)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
        oid, body.name, body.provider, body.connection_type, body.domains,
        json.dumps(body.attribute_map),
        body.idp_entity_id, body.idp_sso_url, body.idp_certificate,
        body.client_id, body.client_secret, body.discovery_url, body.scopes
    )
    return _s(dict(row))

@app.get("/connections")
async def list_connections(org_id: str):
    oid  = _get_org(org_id)
    rows = await db.fetch(
        "SELECT * FROM sso_connections WHERE org_id=$1 ORDER BY created_at", oid
    )
    return [_s(dict(r)) for r in rows]

@app.get("/connections/{conn_id}")
async def get_connection(conn_id: str):
    row = await db.fetchrow(
        "SELECT * FROM sso_connections WHERE id=$1", uuid.UUID(conn_id)
    )
    if not row:
        raise HTTPException(404, "Connection not found")
    d = _s(dict(row))
    # Include SP config so org admin knows what to give IdP
    d["sp_config"] = {
        "entity_id": f"{BASE_URL}/sso/saml/metadata/{conn_id}",
        "acs_url":   f"{BASE_URL}/sso/saml/acs",
        "metadata_url": f"{BASE_URL}/connections/{conn_id}/metadata",
    }
    return d

@app.patch("/connections/{conn_id}")
async def update_connection(conn_id: str, body: ConnectionUpdate):
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not fields:
        raise HTTPException(400, "No fields to update")
    if "attribute_map" in fields:
        fields["attribute_map"] = json.dumps(fields["attribute_map"])
    params  = [uuid.UUID(conn_id)]
    setters = []
    for i, (k, v) in enumerate(fields.items(), start=2):
        setters.append(f"{k}=${i}")
        params.append(v)
    row = await db.fetchrow(
        f"UPDATE sso_connections SET {', '.join(setters)}, updated_at=now() "
        f"WHERE id=$1 RETURNING *",
        *params
    )
    if not row:
        raise HTTPException(404, "Connection not found")
    return _s(dict(row))

@app.post("/connections/{conn_id}/activate")
async def activate_connection(conn_id: str):
    row = await db.fetchrow(
        "UPDATE sso_connections SET status='active', updated_at=now() "
        "WHERE id=$1 RETURNING *",
        uuid.UUID(conn_id)
    )
    if not row:
        raise HTTPException(404, "Connection not found")
    return {"message": "Connection activated", "id": conn_id}

@app.delete("/connections/{conn_id}")
async def delete_connection(conn_id: str):
    result = await db.execute(
        "DELETE FROM sso_connections WHERE id=$1", uuid.UUID(conn_id)
    )
    if result == "DELETE 0":
        raise HTTPException(404, "Connection not found")
    return {"message": "Connection deleted"}

@app.get("/connections/{conn_id}/metadata")
async def sp_metadata(conn_id: str):
    row = await db.fetchrow(
        "SELECT * FROM sso_connections WHERE id=$1", uuid.UUID(conn_id)
    )
    if not row or row["connection_type"] != "saml":
        raise HTTPException(404, "SAML connection not found")
    entity_id = f"{BASE_URL}/sso/saml/metadata/{conn_id}"
    acs_url   = f"{BASE_URL}/sso/saml/acs"
    xml = f"""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="{entity_id}">
  <md:SPSSODescriptor
      AuthnRequestsSigned="false"
      WantAssertionsSigned="true"
      protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:AssertionConsumerService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        Location="{acs_url}"
        index="1"/>
  </md:SPSSODescriptor>
</md:EntityDescriptor>"""
    return Response(content=xml, media_type="application/xml")

# ── DOMAIN LOOKUP ─────────────────────────────────────────────────────
@app.get("/sso/domain/{domain}")
async def lookup_domain(domain: str):
    row = await db.fetchrow(
        "SELECT * FROM sso_connections WHERE $1=ANY(domains) AND status='active'",
        domain
    )
    if not row:
        raise HTTPException(404, f"No active SSO connection for domain '{domain}'")
    return {
        "connection_id":   str(row["id"]),
        "connection_type": row["connection_type"],
        "provider":        row["provider"],
        "org_id":          str(row["org_id"]),
    }

# ── SSO FLOW ──────────────────────────────────────────────────────────
@app.get("/sso/authorize")
async def sso_authorize(
    connection_id: str,
    redirect_uri:  str = "",
    relay_state:   str = "",
):
    conn = await db.fetchrow(
        "SELECT * FROM sso_connections WHERE id=$1 AND status='active'",
        uuid.UUID(connection_id)
    )
    if not conn:
        raise HTTPException(404, "Active SSO connection not found")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(16)
    await db.execute(
        """INSERT INTO sso_sessions
             (connection_id, org_id, state, nonce, redirect_uri, expires_at)
           VALUES ($1,$2,$3,$4,$5,$6)""",
        conn["id"], conn["org_id"], state, nonce,
        redirect_uri or "",
        datetime.now(timezone.utc) + timedelta(minutes=10)
    )

    if conn["connection_type"] == "saml":
        sp_entity = f"{BASE_URL}/sso/saml/metadata/{connection_id}"
        acs_url   = f"{BASE_URL}/sso/saml/acs"
        redirect  = _build_authn_request(
            sp_entity, acs_url,
            conn["idp_sso_url"],
            relay_state or state
        )
        return RedirectResponse(redirect, status_code=302)

    # OIDC
    discovery = await _oidc_discovery(conn["discovery_url"])
    auth_url  = discovery["authorization_endpoint"]
    cb_url    = f"{BASE_URL}/sso/oidc/callback"
    params    = {
        "response_type": "code",
        "client_id":     conn["client_id"],
        "redirect_uri":  cb_url,
        "scope":         " ".join(conn["scopes"] or ["openid", "profile", "email"]),
        "state":         state,
        "nonce":         nonce,
    }
    return RedirectResponse(f"{auth_url}?{urlencode(params)}", status_code=302)

@app.post("/sso/saml/acs")
async def saml_acs(request: Request):
    """SAML Assertion Consumer Service — IdP posts SAMLResponse here."""
    form = await request.form()
    saml_response = form.get("SAMLResponse")
    relay_state   = form.get("RelayState", "")

    if not saml_response:
        raise HTTPException(400, "Missing SAMLResponse")

    # Find session by relay_state (we set relay_state = state in authorize)
    session = await db.fetchrow(
        """SELECT s.*, c.idp_certificate, c.attribute_map
           FROM sso_sessions s
           JOIN sso_connections c ON s.connection_id = c.id
           WHERE s.state=$1 AND s.expires_at > now()""",
        relay_state
    )
    if not session:
        raise HTTPException(400, "Invalid or expired SAML session")

    try:
        profile = await asyncio.to_thread(
            _parse_saml_response, saml_response, session["idp_certificate"]
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    code = await _store_profile_code(
        session["connection_id"], session["org_id"], profile
    )
    await db.execute("DELETE FROM sso_sessions WHERE id=$1", session["id"])

    redirect_uri = session["redirect_uri"]
    if redirect_uri:
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}code={code}", status_code=302)
    return {"code": code, "profile_url": f"{BASE_URL}/sso/profile/{code}"}

@app.get("/sso/oidc/callback")
async def oidc_callback(request: Request):
    """OIDC redirect callback — IdP sends code + state here."""
    params = dict(request.query_params)
    state  = params.get("state")
    code   = params.get("code")
    error  = params.get("error")

    if error:
        raise HTTPException(400, f"OIDC error: {params.get('error_description', error)}")
    if not state or not code:
        raise HTTPException(400, "Missing state or code")

    session = await db.fetchrow(
        """SELECT s.*, c.client_id, c.client_secret, c.discovery_url
           FROM sso_sessions s
           JOIN sso_connections c ON s.connection_id = c.id
           WHERE s.state=$1 AND s.expires_at > now()""",
        state
    )
    if not session:
        raise HTTPException(400, "Invalid or expired OIDC session")

    discovery = await _oidc_discovery(session["discovery_url"])
    cb_url    = f"{BASE_URL}/sso/oidc/callback"
    try:
        tokens   = await _oidc_exchange(
            discovery["token_endpoint"],
            session["client_id"], session["client_secret"],
            code, cb_url
        )
        userinfo = await _oidc_userinfo(
            discovery["userinfo_endpoint"], tokens["access_token"]
        )
    except Exception as e:
        raise HTTPException(400, f"OIDC token exchange failed: {e}")

    profile = {
        "email":          userinfo.get("email"),
        "first_name":     userinfo.get("given_name"),
        "last_name":      userinfo.get("family_name"),
        "idp_id":         userinfo.get("sub"),
        "groups":         userinfo.get("groups", []),
        "raw_attributes": userinfo,
    }

    profile_code = await _store_profile_code(
        session["connection_id"], session["org_id"], profile
    )
    await db.execute("DELETE FROM sso_sessions WHERE id=$1", session["id"])

    redirect_uri = session["redirect_uri"]
    if redirect_uri:
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}code={profile_code}", status_code=302)
    return {"code": profile_code, "profile_url": f"{BASE_URL}/sso/profile/{profile_code}"}

@app.get("/sso/profile/{code}")
async def get_sso_profile(code: str):
    """Exchange a one-time code (from SSO callback) for the user's profile."""
    row = await db.fetchrow(
        "SELECT * FROM sso_profile_codes WHERE code=$1 AND used=false AND expires_at > now()",
        code
    )
    if not row:
        raise HTTPException(401, "Invalid or expired profile code")
    await db.execute(
        "UPDATE sso_profile_codes SET used=true WHERE id=$1", row["id"]
    )
    return {
        "connection_id": str(row["connection_id"]),
        "org_id":        str(row["org_id"]),
        "profile":       row["profile"],
    }

async def _store_profile_code(connection_id, org_id, profile: dict) -> str:
    code = secrets.token_urlsafe(32)
    await db.execute(
        """INSERT INTO sso_profile_codes
             (code, connection_id, org_id, profile, expires_at)
           VALUES ($1,$2,$3,$4,$5)""",
        code, connection_id, org_id, json.dumps(profile),
        datetime.now(timezone.utc) + timedelta(seconds=PROFILE_TTL)
    )
    return code

# ── SCIM DIRECTORIES ──────────────────────────────────────────────────
@app.post("/directories", status_code=201)
async def create_directory(body: DirectoryCreate):
    oid   = _get_org(body.org_id)
    token = secrets.token_urlsafe(48)
    row   = await db.fetchrow(
        """INSERT INTO scim_directories (org_id, name, provider, bearer_token)
           VALUES ($1,$2,$3,$4) RETURNING *""",
        oid, body.name, body.provider, token
    )
    d = _s(dict(row))
    d["scim_base_url"] = f"{BASE_URL}/scim/v2"
    return d

@app.get("/directories")
async def list_directories(org_id: str):
    oid  = _get_org(org_id)
    rows = await db.fetch(
        "SELECT * FROM scim_directories WHERE org_id=$1 ORDER BY created_at", oid
    )
    return [_s(dict(r)) for r in rows]

@app.get("/directories/{dir_id}")
async def get_directory(dir_id: str):
    row = await db.fetchrow(
        "SELECT * FROM scim_directories WHERE id=$1", uuid.UUID(dir_id)
    )
    if not row:
        raise HTTPException(404, "Directory not found")
    d = _s(dict(row))
    d["scim_base_url"] = f"{BASE_URL}/scim/v2"
    return d

@app.post("/directories/{dir_id}/token")
async def rotate_token(dir_id: str):
    new_token = secrets.token_urlsafe(48)
    row = await db.fetchrow(
        "UPDATE scim_directories SET bearer_token=$1, updated_at=now() "
        "WHERE id=$2 RETURNING id",
        new_token, uuid.UUID(dir_id)
    )
    if not row:
        raise HTTPException(404, "Directory not found")
    return {"bearer_token": new_token, "message": "Token rotated — update your IdP configuration"}

@app.delete("/directories/{dir_id}")
async def delete_directory(dir_id: str):
    result = await db.execute(
        "DELETE FROM scim_directories WHERE id=$1", uuid.UUID(dir_id)
    )
    if result == "DELETE 0":
        raise HTTPException(404, "Directory not found")
    return {"message": "Directory and all synced users/groups deleted"}

# ── SCIM 2.0 — USERS ─────────────────────────────────────────────────
@app.get("/scim/v2/Users")
async def scim_list_users(
    directory: dict = Depends(_scim_auth),
    startIndex: int = Query(1, ge=1),
    count:      int = Query(100, ge=1, le=1000),
    filter:     Optional[str] = None,
):
    did    = directory["id"]
    offset = startIndex - 1
    if filter and "userName eq" in filter:
        username = filter.split('"')[1]
        rows = await db.fetch(
            "SELECT * FROM scim_users WHERE directory_id=$1 AND username=$2", did, username
        )
    else:
        rows = await db.fetch(
            "SELECT * FROM scim_users WHERE directory_id=$1 ORDER BY created_at LIMIT $2 OFFSET $3",
            did, count, offset
        )
    total = await db.fetchval(
        "SELECT COUNT(*) FROM scim_users WHERE directory_id=$1", did
    )
    return _scim_list([_scim_user_to_dict(_s(dict(r))) for r in rows], total)

@app.post("/scim/v2/Users", status_code=201)
async def scim_create_user(request: Request, directory: dict = Depends(_scim_auth)):
    body     = await request.json()
    did      = directory["id"]
    oid      = directory["org_id"]
    ext_id   = body.get("externalId") or body.get("id") or str(uuid.uuid4())
    username = body.get("userName", "")
    email    = ""
    for e in body.get("emails", []):
        if e.get("primary"):
            email = e.get("value", "")
            break
    if not email:
        email = username

    name     = body.get("name", {})
    fn       = name.get("givenName", "")
    ln       = name.get("familyName", "")
    display  = body.get("displayName") or f"{fn} {ln}".strip()
    active   = body.get("active", True)

    try:
        row = await db.fetchrow(
            """INSERT INTO scim_users
                 (directory_id, org_id, external_id, username, email,
                  first_name, last_name, display_name, active, raw_attrs)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               ON CONFLICT (directory_id, external_id)
               DO UPDATE SET username=$4, email=$5, first_name=$6, last_name=$7,
                 display_name=$8, active=$9, raw_attrs=$10, updated_at=now()
               RETURNING *""",
            did, oid, ext_id, username, email, fn, ln, display, active,
            json.dumps(body)
        )
    except Exception as e:
        raise HTTPException(409, str(e))

    await db.execute(
        "UPDATE scim_directories SET user_count=(SELECT COUNT(*) FROM scim_users WHERE directory_id=$1) WHERE id=$1",
        did
    )
    return _scim_user_to_dict(_s(dict(row)))

@app.get("/scim/v2/Users/{user_id}")
async def scim_get_user(user_id: str, directory: dict = Depends(_scim_auth)):
    row = await db.fetchrow(
        "SELECT * FROM scim_users WHERE id=$1 AND directory_id=$2",
        uuid.UUID(user_id), directory["id"]
    )
    if not row:
        raise HTTPException(404, json.dumps(_scim_error(404, "User not found")))
    return _scim_user_to_dict(_s(dict(row)))

@app.put("/scim/v2/Users/{user_id}")
async def scim_replace_user(user_id: str, request: Request,
                             directory: dict = Depends(_scim_auth)):
    body    = await request.json()
    did     = directory["id"]
    name    = body.get("name", {})
    fn      = name.get("givenName", "")
    ln      = name.get("familyName", "")
    display = body.get("displayName") or f"{fn} {ln}".strip()
    email   = ""
    for e in body.get("emails", []):
        if e.get("primary"):
            email = e.get("value", "")
            break
    active  = body.get("active", True)
    row = await db.fetchrow(
        """UPDATE scim_users
           SET username=$1, email=$2, first_name=$3, last_name=$4,
               display_name=$5, active=$6, raw_attrs=$7, updated_at=now()
           WHERE id=$8 AND directory_id=$9 RETURNING *""",
        body.get("userName", email), email, fn, ln, display, active,
        json.dumps(body), uuid.UUID(user_id), did
    )
    if not row:
        raise HTTPException(404, "User not found")
    if not active:
        asyncio.create_task(_notify_identity_deprovision(email, str(directory["org_id"])))
    return _scim_user_to_dict(_s(dict(row)))

@app.patch("/scim/v2/Users/{user_id}")
async def scim_patch_user(user_id: str, request: Request,
                           directory: dict = Depends(_scim_auth)):
    body = await request.json()
    uid  = uuid.UUID(user_id)
    did  = directory["id"]
    for op in body.get("Operations", []):
        operation = op.get("op", "").lower()
        path      = op.get("path", "")
        value     = op.get("value")
        if operation == "replace":
            if path == "active" or (isinstance(value, dict) and "active" in value):
                new_active = value if isinstance(value, bool) else value.get("active", True)
                row = await db.fetchrow(
                    "UPDATE scim_users SET active=$1, updated_at=now() "
                    "WHERE id=$2 AND directory_id=$3 RETURNING email, org_id",
                    new_active, uid, did
                )
                if row and not new_active:
                    asyncio.create_task(
                        _notify_identity_deprovision(row["email"], str(row["org_id"]))
                    )
    row = await db.fetchrow(
        "SELECT * FROM scim_users WHERE id=$1 AND directory_id=$2", uid, did
    )
    if not row:
        raise HTTPException(404, "User not found")
    return _scim_user_to_dict(_s(dict(row)))

@app.delete("/scim/v2/Users/{user_id}", status_code=204)
async def scim_delete_user(user_id: str, directory: dict = Depends(_scim_auth)):
    row = await db.fetchrow(
        "DELETE FROM scim_users WHERE id=$1 AND directory_id=$2 RETURNING email, org_id",
        uuid.UUID(user_id), directory["id"]
    )
    if not row:
        raise HTTPException(404, "User not found")
    await db.execute(
        "UPDATE scim_directories SET user_count=(SELECT COUNT(*) FROM scim_users WHERE directory_id=$1) WHERE id=$1",
        directory["id"]
    )
    asyncio.create_task(_notify_identity_deprovision(row["email"], str(row["org_id"])))

# ── SCIM 2.0 — GROUPS ────────────────────────────────────────────────
@app.get("/scim/v2/Groups")
async def scim_list_groups(
    directory: dict = Depends(_scim_auth),
    startIndex: int = Query(1, ge=1),
    count:      int = Query(100, ge=1, le=1000),
):
    did    = directory["id"]
    offset = startIndex - 1
    rows   = await db.fetch(
        "SELECT * FROM scim_groups WHERE directory_id=$1 ORDER BY created_at LIMIT $2 OFFSET $3",
        did, count, offset
    )
    total  = await db.fetchval(
        "SELECT COUNT(*) FROM scim_groups WHERE directory_id=$1", did
    )
    return _scim_list([_scim_group_to_dict(_s(dict(r))) for r in rows], total)

@app.post("/scim/v2/Groups", status_code=201)
async def scim_create_group(request: Request, directory: dict = Depends(_scim_auth)):
    body    = await request.json()
    did     = directory["id"]
    oid     = directory["org_id"]
    ext_id  = body.get("externalId") or str(uuid.uuid4())
    members = [m.get("value", "") for m in body.get("members", [])]
    row = await db.fetchrow(
        """INSERT INTO scim_groups
             (directory_id, org_id, external_id, display_name, member_ids, raw_data)
           VALUES ($1,$2,$3,$4,$5,$6)
           ON CONFLICT (directory_id, external_id)
           DO UPDATE SET display_name=$4, member_ids=$5, raw_data=$6, updated_at=now()
           RETURNING *""",
        did, oid, ext_id, body.get("displayName", ""), members, json.dumps(body)
    )
    await db.execute(
        "UPDATE scim_directories SET group_count=(SELECT COUNT(*) FROM scim_groups WHERE directory_id=$1) WHERE id=$1",
        did
    )
    return _scim_group_to_dict(_s(dict(row)))

@app.get("/scim/v2/Groups/{group_id}")
async def scim_get_group(group_id: str, directory: dict = Depends(_scim_auth)):
    row = await db.fetchrow(
        "SELECT * FROM scim_groups WHERE id=$1 AND directory_id=$2",
        uuid.UUID(group_id), directory["id"]
    )
    if not row:
        raise HTTPException(404, "Group not found")
    return _scim_group_to_dict(_s(dict(row)))

@app.put("/scim/v2/Groups/{group_id}")
async def scim_replace_group(group_id: str, request: Request,
                              directory: dict = Depends(_scim_auth)):
    body    = await request.json()
    members = [m.get("value", "") for m in body.get("members", [])]
    row = await db.fetchrow(
        """UPDATE scim_groups SET display_name=$1, member_ids=$2,
           raw_data=$3, updated_at=now()
           WHERE id=$4 AND directory_id=$5 RETURNING *""",
        body.get("displayName", ""), members, json.dumps(body),
        uuid.UUID(group_id), directory["id"]
    )
    if not row:
        raise HTTPException(404, "Group not found")
    return _scim_group_to_dict(_s(dict(row)))

@app.delete("/scim/v2/Groups/{group_id}", status_code=204)
async def scim_delete_group(group_id: str, directory: dict = Depends(_scim_auth)):
    result = await db.execute(
        "DELETE FROM scim_groups WHERE id=$1 AND directory_id=$2",
        uuid.UUID(group_id), directory["id"]
    )
    if result == "DELETE 0":
        raise HTTPException(404, "Group not found")
    await db.execute(
        "UPDATE scim_directories SET group_count=(SELECT COUNT(*) FROM scim_groups WHERE directory_id=$1) WHERE id=$1",
        directory["id"]
    )

# ── ADMIN PORTAL ──────────────────────────────────────────────────────
@app.post("/portal/links", status_code=201)
async def create_portal_link(body: PortalLinkCreate):
    oid   = _get_org(body.org_id)
    token = secrets.token_urlsafe(48)
    await db.execute(
        "INSERT INTO portal_links (org_id, token, intent, expires_at) VALUES ($1,$2,$3,$4)",
        oid, token, body.intent,
        datetime.now(timezone.utc) + timedelta(seconds=PORTAL_TTL)
    )
    return {
        "link":       f"{BASE_URL}/portal/{token}",
        "token":      token,
        "intent":     body.intent,
        "expires_in": PORTAL_TTL,
    }

@app.get("/portal/{token}")
async def use_portal_link(token: str):
    link = await db.fetchrow(
        "SELECT * FROM portal_links WHERE token=$1 AND used=false AND expires_at > now()",
        token
    )
    if not link:
        raise HTTPException(401, "Portal link invalid or expired")
    oid = link["org_id"]
    connections = await db.fetch(
        "SELECT id, name, provider, connection_type, status FROM sso_connections WHERE org_id=$1", oid
    )
    directories = await db.fetch(
        "SELECT id, name, provider, status, user_count, group_count FROM scim_directories WHERE org_id=$1", oid
    )
    return {
        "org_id":      str(oid),
        "intent":      link["intent"],
        "connections": [_s(dict(r)) for r in connections],
        "directories": [_s(dict(r)) for r in directories],
    }

# ── REPORTS ───────────────────────────────────────────────────────────
@app.get("/reports/summary")
async def report_summary(org_id: str):
    oid = _get_org(org_id)
    conns = await db.fetch(
        "SELECT connection_type, status, COUNT(*) AS count FROM sso_connections "
        "WHERE org_id=$1 GROUP BY connection_type, status", oid
    )
    dirs = await db.fetch(
        "SELECT provider, status, user_count, group_count FROM scim_directories WHERE org_id=$1", oid
    )
    return {
        "sso_connections": [dict(r) for r in conns],
        "directories":     [_s(dict(r)) for r in dirs],
        "total_scim_users": sum(r["user_count"] for r in dirs),
    }
