"""
Tessera Knowledge Base  —  port 8026
=====================================
KB articles, full-text search, ticket deflection tracking.

ServiceNow's KM moat: "$-per-ticket-deflected". Every article that answers
a question before a ticket is filed saves ~$22 (avg cost of an L1 ticket).
Tessera tracks that number explicitly.

HAV differentiator: articles authored by high-HAV humans (high OC, high NPF)
are surfaced first — they contain novel framings that can't be auto-generated.
Values Custodian articles are flagged as institutional knowledge that would be
lost if that human were eliminated.
"""
from __future__ import annotations
import os, uuid, asyncpg
from datetime import datetime, timezone
from typing import Optional, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_knowledge")
TICKET_DEFLECTION_VALUE = float(os.getenv("TICKET_DEFLECTION_VALUE", "22.0"))  # $ saved per deflection
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS categories (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT UNIQUE NOT NULL,
    parent_id   TEXT REFERENCES categories(id),
    description TEXT,
    icon        TEXT,
    sort_order  INT DEFAULT 0,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS articles (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    category_id     TEXT REFERENCES categories(id),
    title           TEXT NOT NULL,
    slug            TEXT NOT NULL,
    body            TEXT NOT NULL,
    summary         TEXT,
    tags            TEXT[] DEFAULT '{}',
    author_id       TEXT NOT NULL,
    author_hav      FLOAT,   -- HAV score of author at time of writing
    author_npf      FLOAT,
    author_oc       FLOAT,
    is_vc_knowledge BOOLEAN DEFAULT FALSE,  -- authored by a Values Custodian
    status          TEXT DEFAULT 'draft',   -- 'draft'|'published'|'archived'
    version         INT DEFAULT 1,
    view_count      INT DEFAULT 0,
    helpful_count   INT DEFAULT 0,
    not_helpful_count INT DEFAULT 0,
    deflection_count  INT DEFAULT 0,
    deflection_value  FLOAT DEFAULT 0.0,
    search_vector   TSVECTOR,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    published_at    TIMESTAMPTZ,
    UNIQUE (org_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_articles_org      ON articles(org_id);
CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category_id);
CREATE INDEX IF NOT EXISTS idx_articles_status   ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_hav      ON articles(author_hav DESC);
CREATE INDEX IF NOT EXISTS idx_articles_fts      ON articles USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS idx_articles_trgm_title ON articles USING GIN(title gin_trgm_ops);

CREATE TABLE IF NOT EXISTS article_versions (
    id          TEXT PRIMARY KEY,
    article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    version     INT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    changed_by  TEXT NOT NULL,
    change_note TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_versions_article ON article_versions(article_id);

CREATE TABLE IF NOT EXISTS deflection_events (
    id           TEXT PRIMARY KEY,
    article_id   TEXT NOT NULL REFERENCES articles(id),
    org_id       TEXT NOT NULL,
    session_id   TEXT,
    query        TEXT,
    deflected    BOOLEAN DEFAULT FALSE,  -- TRUE = user did NOT submit ticket after reading
    ticket_id    TEXT,                   -- if they submitted anyway, link it
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deflect_article ON deflection_events(article_id);
CREATE INDEX IF NOT EXISTS idx_deflect_org     ON deflection_events(org_id);

CREATE TABLE IF NOT EXISTS article_feedback (
    id           TEXT PRIMARY KEY,
    article_id   TEXT NOT NULL REFERENCES articles(id),
    user_id      TEXT,
    helpful      BOOLEAN NOT NULL,
    comment      TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
        # Seed default categories
        await conn.execute("""
            INSERT INTO categories (id, name, slug, description, sort_order)
            VALUES
              ('cat-it',    'IT Support',       'it-support',      'Hardware, software, access issues', 1),
              ('cat-hr',    'HR & People',       'hr-people',       'Policies, benefits, onboarding',   2),
              ('cat-sec',   'Security',          'security',        'Passwords, phishing, access',      3),
              ('cat-proc',  'Processes',         'processes',       'How-to guides and procedures',     4),
              ('cat-ai',    'AI & Automation',   'ai-automation',   'Working with AI tools and agents', 5),
              ('cat-phi',   'HAV & Governance',  'hav-governance',  'phi-guardian protocols, HAV SLAs', 6)
            ON CONFLICT (slug) DO NOTHING
        """)
    yield
    await db.close()

app = FastAPI(title="Tessera Knowledge Base", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _search_vector(title: str, body: str, tags: list) -> str:
    tag_str = " ".join(tags)
    return f"setweight(to_tsvector('english', {repr(title)}), 'A') || setweight(to_tsvector('english', {repr(body[:4000])}), 'B') || setweight(to_tsvector('english', {repr(tag_str)}), 'C')"


class CategoryRequest(BaseModel):
    name: str
    slug: str
    parent_id: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    sort_order: int = 0

class ArticleRequest(BaseModel):
    org_id: str
    category_id: Optional[str] = None
    title: str
    slug: str
    body: str
    summary: Optional[str] = None
    tags: List[str] = []
    author_id: str
    author_hav: Optional[float] = Field(None, ge=0.0, le=1.0)
    author_npf: Optional[float] = Field(None, ge=0.0, le=1.0)
    author_oc: Optional[float] = Field(None, ge=0.0, le=1.0)

class ArticleUpdateRequest(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    summary: Optional[str] = None
    tags: Optional[List[str]] = None
    category_id: Optional[str] = None
    changed_by: str
    change_note: Optional[str] = None

class FeedbackRequest(BaseModel):
    user_id: Optional[str] = None
    helpful: bool
    comment: Optional[str] = None

class DeflectionRequest(BaseModel):
    article_id: str
    org_id: str
    session_id: Optional[str] = None
    query: Optional[str] = None
    deflected: bool = True
    ticket_id: Optional[str] = None


@app.get("/")
def root():
    return {"service": "knowledge", "version": "1.0.0", "port": 8026,
            "differentiator": "HAV-ranked articles; VC-knowledge protection; $-per-deflection tracking"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "knowledge"}


# ── CATEGORIES ────────────────────────────────────────────────────────────────

@app.get("/categories")
async def list_categories():
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM categories WHERE active=TRUE ORDER BY sort_order, name"
        )
    return {"categories": [dict(r) for r in rows]}


@app.post("/categories", status_code=201)
async def create_category(body: CategoryRequest):
    cat_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO categories (id, name, slug, parent_id, description, icon, sort_order)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, cat_id, body.name, body.slug, body.parent_id,
             body.description, body.icon, body.sort_order)
    return {"category_id": cat_id, "name": body.name, "slug": body.slug}


# ── ARTICLES ──────────────────────────────────────────────────────────────────

@app.post("/articles", status_code=201)
async def create_article(body: ArticleRequest):
    is_vc = bool(body.author_hav and body.author_hav >= 0.70
                 and body.author_npf and body.author_npf >= 0.65)
    article_id = str(uuid.uuid4())

    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO articles
              (id, org_id, category_id, title, slug, body, summary, tags,
               author_id, author_hav, author_npf, author_oc, is_vc_knowledge,
               search_vector)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                    setweight(to_tsvector('english', $4), 'A') ||
                    setweight(to_tsvector('english', $6), 'B') ||
                    setweight(to_tsvector('english', $14), 'C'))
        """, article_id, body.org_id, body.category_id, body.title, body.slug,
             body.body, body.summary, body.tags,
             body.author_id, body.author_hav, body.author_npf, body.author_oc,
             is_vc, " ".join(body.tags))

    vc_note = (
        "IMPORTANT: This article was authored by a Values Custodian (HAV≥0.70, NPF≥0.65). "
        "It likely contains institutional knowledge that cannot be auto-generated. "
        "Flag before removing author from the org."
        if is_vc else None
    )
    return {
        "article_id": article_id,
        "title": body.title,
        "slug": body.slug,
        "status": "draft",
        "is_vc_knowledge": is_vc,
        "vc_warning": vc_note,
        "next": f"POST /articles/{article_id}/publish to make it searchable",
    }


@app.post("/articles/{article_id}/publish")
async def publish_article(article_id: str):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE articles SET status='published', published_at=NOW(), updated_at=NOW() "
            "WHERE id=$1 RETURNING id, title, is_vc_knowledge",
            article_id
        )
        if not row:
            raise HTTPException(404, "Article not found")
    return {"article_id": article_id, "status": "published", "title": row["title"],
            "is_vc_knowledge": row["is_vc_knowledge"]}


@app.patch("/articles/{article_id}")
async def update_article(article_id: str, body: ArticleUpdateRequest):
    async with db.acquire() as conn:
        art = await conn.fetchrow("SELECT * FROM articles WHERE id=$1", article_id)
        if not art:
            raise HTTPException(404, "Article not found")

        new_title = body.title or art["title"]
        new_body  = body.body  or art["body"]
        new_tags  = body.tags  or list(art["tags"])
        new_version = art["version"] + 1

        # Archive current version
        await conn.execute("""
            INSERT INTO article_versions (id, article_id, version, title, body, changed_by, change_note)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, str(uuid.uuid4()), article_id, art["version"],
             art["title"], art["body"], body.changed_by, body.change_note)

        await conn.execute("""
            UPDATE articles SET
                title=$1, body=$2, tags=$3, category_id=COALESCE($4, category_id),
                summary=COALESCE($5, summary), version=$6, updated_at=NOW(),
                search_vector = setweight(to_tsvector('english', $1), 'A') ||
                                setweight(to_tsvector('english', $2), 'B') ||
                                setweight(to_tsvector('english', $7), 'C')
            WHERE id=$8
        """, new_title, new_body, new_tags, body.category_id,
             body.summary, new_version, " ".join(new_tags), article_id)

    return {"article_id": article_id, "version": new_version, "updated": True}


@app.get("/articles/{article_id}")
async def get_article(article_id: str):
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM articles WHERE id=$1", article_id)
        if not row:
            raise HTTPException(404, "Article not found")
        await conn.execute(
            "UPDATE articles SET view_count=view_count+1 WHERE id=$1", article_id
        )
    return dict(row)


@app.get("/articles")
async def list_articles(
    org_id: str = Query(...),
    category_id: Optional[str] = Query(None),
    status: str = Query("published"),
    is_vc_knowledge: Optional[bool] = Query(None),
    limit: int = Query(20, le=100),
    offset: int = 0,
):
    async with db.acquire() as conn:
        clauses = ["org_id=$1", "status=$2"]
        params  = [org_id, status]
        idx = 3
        if category_id:
            params.append(category_id); clauses.append(f"category_id=${idx}"); idx += 1
        if is_vc_knowledge is not None:
            params.append(is_vc_knowledge); clauses.append(f"is_vc_knowledge=${idx}"); idx += 1
        where = " AND ".join(clauses)
        params += [limit, offset]
        rows = await conn.fetch(
            f"SELECT id, title, slug, summary, tags, author_id, author_hav, "
            f"is_vc_knowledge, view_count, deflection_count, deflection_value, "
            f"helpful_count, not_helpful_count, published_at "
            f"FROM articles WHERE {where} "
            f"ORDER BY is_vc_knowledge DESC, author_hav DESC NULLS LAST, view_count DESC "
            f"LIMIT ${idx} OFFSET ${idx+1}", *params
        )
    return {"articles": [dict(r) for r in rows]}


# ── SEARCH ────────────────────────────────────────────────────────────────────

@app.get("/search")
async def search(
    q: str = Query(..., min_length=2),
    org_id: str = Query(...),
    limit: int = Query(10, le=50),
):
    """
    Full-text + trigram search. Results ranked by:
    1. FTS rank (relevance)
    2. HAV of author (high-HAV authors surface first)
    3. Deflection count (articles that actually resolved issues)
    """
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, slug, summary, tags, author_hav, is_vc_knowledge,
                   view_count, deflection_count, deflection_value,
                   ts_rank(search_vector, plainto_tsquery('english', $1)) AS rank
            FROM articles
            WHERE org_id=$2 AND status='published'
              AND (search_vector @@ plainto_tsquery('english', $1)
                   OR title ILIKE $3)
            ORDER BY rank DESC,
                     is_vc_knowledge DESC,
                     COALESCE(author_hav, 0) DESC,
                     deflection_count DESC
            LIMIT $4
        """, q, org_id, f"%{q}%", limit)

    return {
        "query": q,
        "results": [dict(r) for r in rows],
        "count": len(rows),
        "note": "Results ranked: relevance → VC authorship → HAV → deflection history",
    }


# ── FEEDBACK & DEFLECTION ─────────────────────────────────────────────────────

@app.post("/articles/{article_id}/feedback", status_code=201)
async def submit_feedback(article_id: str, body: FeedbackRequest):
    async with db.acquire() as conn:
        art = await conn.fetchrow("SELECT id FROM articles WHERE id=$1", article_id)
        if not art:
            raise HTTPException(404, "Article not found")
        fb_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO article_feedback (id, article_id, user_id, helpful, comment)
            VALUES ($1,$2,$3,$4,$5)
        """, fb_id, article_id, body.user_id, body.helpful, body.comment)
        if body.helpful:
            await conn.execute(
                "UPDATE articles SET helpful_count=helpful_count+1 WHERE id=$1", article_id
            )
        else:
            await conn.execute(
                "UPDATE articles SET not_helpful_count=not_helpful_count+1 WHERE id=$1", article_id
            )
    return {"feedback_id": fb_id, "helpful": body.helpful}


@app.post("/deflection", status_code=201)
async def record_deflection(body: DeflectionRequest):
    """
    Record whether a KB article deflected a ticket.
    deflected=True means user found the answer and did NOT submit a ticket.
    This is the core ServiceNow KM metric: cost savings per deflection.
    """
    async with db.acquire() as conn:
        ev_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO deflection_events
              (id, article_id, org_id, session_id, query, deflected, ticket_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, ev_id, body.article_id, body.org_id, body.session_id,
             body.query, body.deflected, body.ticket_id)

        if body.deflected:
            await conn.execute("""
                UPDATE articles SET
                    deflection_count = deflection_count + 1,
                    deflection_value = deflection_value + $1
                WHERE id=$2
            """, TICKET_DEFLECTION_VALUE, body.article_id)

    return {
        "event_id": ev_id,
        "deflected": body.deflected,
        "value_saved": TICKET_DEFLECTION_VALUE if body.deflected else 0.0,
        "note": f"${TICKET_DEFLECTION_VALUE:.2f} saved per deflection (avg L1 ticket cost)",
    }


# ── REPORTS ───────────────────────────────────────────────────────────────────

@app.get("/reports/deflection")
async def deflection_report(org_id: str = Query(...)):
    """The number ServiceNow leads every sales pitch with."""
    async with db.acquire() as conn:
        totals = await conn.fetchrow("""
            SELECT COUNT(*) AS total_events,
                   SUM(CASE WHEN deflected THEN 1 ELSE 0 END) AS deflections,
                   SUM(CASE WHEN NOT deflected THEN 1 ELSE 0 END) AS tickets_submitted
            FROM deflection_events WHERE org_id=$1
        """, org_id)
        top = await conn.fetch("""
            SELECT a.title, a.author_hav, a.is_vc_knowledge,
                   a.deflection_count, a.deflection_value,
                   a.helpful_count, a.not_helpful_count
            FROM articles a
            WHERE a.org_id=$1 AND a.deflection_count > 0
            ORDER BY a.deflection_value DESC LIMIT 10
        """, org_id)
        vc_value = await conn.fetchval("""
            SELECT COALESCE(SUM(deflection_value), 0)
            FROM articles WHERE org_id=$1 AND is_vc_knowledge=TRUE
        """, org_id)

    total_ev  = totals["total_events"] or 0
    deflected = totals["deflections"] or 0
    rate      = round(deflected / total_ev * 100, 1) if total_ev else 0.0
    total_val = sum(r["deflection_value"] for r in top)

    return {
        "org_id": org_id,
        "deflection_rate": f"{rate}%",
        "total_deflections": deflected,
        "tickets_submitted_anyway": totals["tickets_submitted"] or 0,
        "total_value_saved": round(sum(r["deflection_value"] for r in top), 2),
        "vc_knowledge_value_saved": round(vc_value, 2),
        "top_articles": [dict(r) for r in top],
        "insight": (
            f"Values Custodian articles saved ${vc_value:,.2f}. "
            "This is institutional knowledge that disappears if these humans are eliminated."
            if vc_value > 0 else
            "No VC-authored articles yet. High-HAV authors produce the highest-deflection articles."
        ),
    }


@app.get("/reports/vc-knowledge")
async def vc_knowledge_report(org_id: str = Query(...)):
    """Which institutional knowledge is at risk if VCs are eliminated?"""
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, author_id, author_hav, author_npf, author_oc,
                   deflection_count, deflection_value, view_count, tags
            FROM articles
            WHERE org_id=$1 AND is_vc_knowledge=TRUE AND status='published'
            ORDER BY deflection_value DESC
        """, org_id)
    total_value = sum(r["deflection_value"] for r in rows)
    return {
        "org_id": org_id,
        "vc_articles": len(rows),
        "total_value_at_risk": round(total_value, 2),
        "articles": [dict(r) for r in rows],
        "warning": (
            f"{len(rows)} articles authored by Values Custodians represent "
            f"${total_value:,.2f} in annual deflection value. "
            "Eliminating these humans destroys institutional knowledge that AI cannot replicate."
        ) if rows else "No VC-authored articles published yet.",
    }
