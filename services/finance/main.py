"""
Tessera Finance / General Ledger  —  port 8025
===============================================
Unified AI+Human P&L. V_net from twin feeds the financial ledger.
Human cost includes alignment premium. AI cost includes deployment + oversight.

Workday does: GL → chart of accounts → journal entries → financial reports.
Tessera adds:  AI agent cost tracking, HAV economic value posting,
               alignment premium as a P&L line, V_net ledger from twin.
"""
from __future__ import annotations
import os, uuid, asyncpg
from typing import Optional, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tessera:tessera@localhost:5432/tessera_finance")
TWIN_URL     = os.getenv("TWIN_URL", "http://twin:8004")
db: asyncpg.Pool | None = None

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT NOT NULL,
    account_type TEXT NOT NULL,  -- 'asset'|'liability'|'equity'|'revenue'|'expense'
    account_class TEXT,  -- 'human_comp'|'ai_cost'|'alignment_premium'|'hav_value'|'standard'
    active       BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (org_id, code)
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    period       TEXT NOT NULL,  -- 'YYYY-MM'
    entry_date   DATE NOT NULL,
    description  TEXT NOT NULL,
    entry_type   TEXT DEFAULT 'standard',  -- 'standard'|'hav_value'|'ai_cost'|'alignment_premium'|'v_net'
    posted       BOOLEAN DEFAULT FALSE,
    total_debit  FLOAT DEFAULT 0.0,
    total_credit FLOAT DEFAULT 0.0,
    hav_metadata JSONB,  -- HAV, phi, sim_id when relevant
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_je_org_period ON journal_entries(org_id, period);
CREATE INDEX IF NOT EXISTS idx_je_type ON journal_entries(entry_type);

CREATE TABLE IF NOT EXISTS journal_lines (
    id           TEXT PRIMARY KEY,
    entry_id     TEXT NOT NULL REFERENCES journal_entries(id),
    account_id   TEXT NOT NULL REFERENCES accounts(id),
    debit        FLOAT DEFAULT 0.0,
    credit       FLOAT DEFAULT 0.0,
    memo         TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jl_entry ON journal_lines(entry_id);
CREATE INDEX IF NOT EXISTS idx_jl_account ON journal_lines(account_id);

CREATE TABLE IF NOT EXISTS ai_cost_ledger (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    period          TEXT NOT NULL,
    sim_id          TEXT,
    agent_type      TEXT,
    token_spend     FLOAT DEFAULT 0.0,
    deployment_cost FLOAT DEFAULT 0.0,
    oversight_cost  FLOAT DEFAULT 0.0,
    v_net_value     FLOAT DEFAULT 0.0,
    net_ai_pnl      FLOAT DEFAULT 0.0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_cost_org ON ai_cost_ledger(org_id, period);
"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute(CREATE_TABLES)
        # Seed chart of accounts for new orgs
        await conn.execute("""
            INSERT INTO accounts (id, org_id, code, name, account_type, account_class)
            VALUES
              ('sys-1000','system','1000','Cash and Equivalents',        'asset',   'standard'),
              ('sys-2000','system','2000','Accounts Payable',            'liability','standard'),
              ('sys-4000','system','4000','Revenue',                     'revenue', 'standard'),
              ('sys-5100','system','5100','Human Compensation',          'expense', 'human_comp'),
              ('sys-5110','system','5110','Alignment Premium Expense',   'expense', 'alignment_premium'),
              ('sys-5200','system','5200','AI Deployment Cost',          'expense', 'ai_cost'),
              ('sys-5210','system','5210','AI Oversight Cost',           'expense', 'ai_cost'),
              ('sys-5300','system','5300','HAV Economic Value Credit',   'revenue', 'hav_value'),
              ('sys-5400','system','5400','V_net AI Contribution',       'revenue', 'v_net')
            ON CONFLICT (org_id, code) DO NOTHING
        """)
    yield
    await db.close()

app = FastAPI(title="Tessera Finance", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class AccountRequest(BaseModel):
    org_id: str
    code: str
    name: str
    account_type: str
    account_class: str = "standard"

class JournalLineInput(BaseModel):
    account_id: str
    debit: float = 0.0
    credit: float = 0.0
    memo: Optional[str] = None

class JournalEntryRequest(BaseModel):
    org_id: str
    period: str
    entry_date: str
    description: str
    entry_type: str = "standard"
    lines: List[JournalLineInput]
    hav_metadata: Optional[dict] = None

class AlignmentPremiumEntryRequest(BaseModel):
    org_id: str
    period: str
    entry_date: str
    employee_id: str
    base_salary: float
    alignment_premium_amt: float
    mean_hav: float
    phi: Optional[float] = None

class AiCostRequest(BaseModel):
    org_id: str
    period: str
    sim_id: Optional[str] = None
    agent_type: Optional[str] = None
    token_spend: float = 0.0
    deployment_cost: float = 0.0
    oversight_cost: float = 0.0
    v_net_value: float = 0.0


@app.get("/")
def root():
    return {"service": "finance", "version": "1.0.0", "port": 8025,
            "differentiator": "Unified AI+Human P&L; V_net ledger; alignment premium as P&L line"}

@app.get("/health")
async def health():
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "service": "finance"}


@app.get("/accounts")
async def list_accounts(org_id: Optional[str] = Query(None)):
    async with db.acquire() as conn:
        if org_id:
            rows = await conn.fetch(
                "SELECT * FROM accounts WHERE org_id IN ($1,'system') AND active=TRUE ORDER BY code",
                org_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM accounts WHERE active=TRUE ORDER BY code LIMIT 200"
            )
    return {"accounts": [dict(r) for r in rows]}


@app.post("/accounts", status_code=201)
async def create_account(body: AccountRequest):
    acct_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO accounts (id, org_id, code, name, account_type, account_class)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, acct_id, body.org_id, body.code, body.name, body.account_type, body.account_class)
    return {"account_id": acct_id, "code": body.code, "name": body.name}


@app.post("/journal-entries", status_code=201)
async def create_je(body: JournalEntryRequest):
    total_debit  = sum(l.debit  for l in body.lines)
    total_credit = sum(l.credit for l in body.lines)
    if abs(total_debit - total_credit) > 0.01:
        raise HTTPException(400,
            f"Journal entry must balance. Debits={total_debit:.2f} Credits={total_credit:.2f}")

    je_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO journal_entries
              (id, org_id, period, entry_date, description, entry_type,
               total_debit, total_credit, hav_metadata)
            VALUES ($1,$2,$3,$4::DATE,$5,$6,$7,$8,$9::JSONB)
        """, je_id, body.org_id, body.period, body.entry_date,
             body.description, body.entry_type, total_debit, total_credit,
             str(body.hav_metadata).replace("'", '"') if body.hav_metadata else None)

        for ln in body.lines:
            ln_id = str(uuid.uuid4())
            await conn.execute("""
                INSERT INTO journal_lines (id, entry_id, account_id, debit, credit, memo)
                VALUES ($1,$2,$3,$4,$5,$6)
            """, ln_id, je_id, ln.account_id, ln.debit, ln.credit, ln.memo)

    return {"entry_id": je_id, "total_debit": total_debit, "total_credit": total_credit,
            "balanced": True, "entry_type": body.entry_type}


@app.post("/alignment-premium-entry", status_code=201)
async def post_alignment_premium(body: AlignmentPremiumEntryRequest):
    """
    Post alignment premium as a distinct P&L line — this is Tessera's
    key differentiator vs Workday which lumps everything into salary expense.
    """
    je_id  = str(uuid.uuid4())
    phi_str = f"phi={body.phi:.3f}" if body.phi else "phi=N/A"
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO journal_entries
              (id, org_id, period, entry_date, description, entry_type, total_debit, total_credit)
            VALUES ($1,$2,$3,$4::DATE,$5,'alignment_premium',$6,$7)
        """, je_id, body.org_id, body.period, body.entry_date,
             f"Alignment premium: emp={body.employee_id} HAV={body.mean_hav:.3f} {phi_str}",
             body.alignment_premium_amt, body.alignment_premium_amt)

        # Debit: Alignment Premium Expense (5110)
        # Credit: Accrued Compensation (via AP 2000)
        for ln_id, acct, debit, credit, memo in [
            (str(uuid.uuid4()), "sys-5110", body.alignment_premium_amt, 0.0,
             f"HAV={body.mean_hav:.3f} alignment premium"),
            (str(uuid.uuid4()), "sys-2000", 0.0, body.alignment_premium_amt,
             "Accrued alignment premium payable"),
        ]:
            await conn.execute("""
                INSERT INTO journal_lines (id, entry_id, account_id, debit, credit, memo)
                VALUES ($1,$2,$3,$4,$5,$6)
            """, ln_id, je_id, acct, debit, credit, memo)

    return {
        "entry_id": je_id,
        "employee_id": body.employee_id,
        "alignment_premium": body.alignment_premium_amt,
        "mean_hav": body.mean_hav,
        "vs_workday": (
            "Workday posts salary + premium together under labor cost. "
            "Tessera separates alignment premium so investors can see human-AI value creation explicitly."
        ),
    }


@app.post("/ai-cost", status_code=201)
async def post_ai_cost(body: AiCostRequest):
    total_cost = body.deployment_cost + body.oversight_cost + body.token_spend
    net_pnl    = body.v_net_value - total_cost

    acl_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO ai_cost_ledger
              (id, org_id, period, sim_id, agent_type, token_spend,
               deployment_cost, oversight_cost, v_net_value, net_ai_pnl)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """, acl_id, body.org_id, body.period, body.sim_id, body.agent_type,
             body.token_spend, body.deployment_cost, body.oversight_cost,
             body.v_net_value, net_pnl)

    return {
        "ledger_id": acl_id,
        "period": body.period,
        "ai_p_l": {
            "total_cost": round(total_cost, 2),
            "v_net_value": body.v_net_value,
            "net_ai_pnl": round(net_pnl, 2),
        },
        "interpretation": (
            "Positive AI P&L — deployment covers its costs." if net_pnl > 0 else
            "Negative AI P&L — AI oversight costs exceed value delivered. "
            "Review deployment scope or increase human oversight quality (SRQ)."
        ),
    }


@app.get("/reports/unified-pl")
async def unified_pl(org_id: str = Query(...), period: str = Query(...)):
    """
    Unified P&L: human comp + alignment premium + AI cost + V_net value.
    This is what Workday can't show — the full AI+Human picture.
    """
    async with db.acquire() as conn:
        entries = await conn.fetch("""
            SELECT entry_type, SUM(total_debit) as debits, SUM(total_credit) as credits
            FROM journal_entries
            WHERE org_id=$1 AND period=$2 AND posted=TRUE
            GROUP BY entry_type
        """, org_id, period)
        ai_costs = await conn.fetch("""
            SELECT SUM(token_spend) as tokens, SUM(deployment_cost) as deploy,
                   SUM(oversight_cost) as oversight, SUM(v_net_value) as v_net,
                   SUM(net_ai_pnl) as net_pnl
            FROM ai_cost_ledger WHERE org_id=$1 AND period=$2
        """, org_id, period)

    entry_map = {r["entry_type"]: dict(r) for r in entries}
    ai = dict(ai_costs[0]) if ai_costs else {}

    return {
        "org_id": org_id,
        "period": period,
        "unified_pl": {
            "human_comp": entry_map.get("standard", {}).get("debits", 0),
            "alignment_premium": entry_map.get("alignment_premium", {}).get("debits", 0),
            "ai_total_cost": (ai.get("tokens") or 0) + (ai.get("deploy") or 0) + (ai.get("oversight") or 0),
            "ai_v_net_value": ai.get("v_net") or 0,
            "ai_net_pnl": ai.get("net_pnl") or 0,
            "hav_value_credited": entry_map.get("hav_value", {}).get("credits", 0),
        },
        "vs_workday": "Workday shows only human labor cost. Tessera shows the full AI+Human P&L.",
    }
