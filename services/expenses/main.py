"""
HACM Expense Service  —  port 8006
====================================
Manages expenses for the whole organization — human employees AND AI agents.

Odoo hr_expense covers:
    Employee expense reports, receipt uploads, manager approval,
    journal entries, reimbursement to employee bank account.

HACM extends it with:
    - AI agent token spend as a first-class expense category
    - Expense-to-activation mapping: did this spend improve performance?
    - Token vs cash expense unified ledger
    - B* awareness: is AI spend generating positive returns?
    - Automatic expense policy checking (per Huang model thresholds)

Key insight: in HACM, an AI agent's token consumption IS an expense.
It shows up in the same ledger, same approval flow, same reporting —
alongside the human team's travel, meals, and equipment.
This is the accounting view of the Huang (2026) token-as-compensation model.
"""

import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import date, datetime
from enum import Enum

app = FastAPI(title="HACM Expense Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── ENUMS ──────────────────────────────────────────────────────────────

class ExpenseState(str, Enum):
    DRAFT      = "draft"
    SUBMITTED  = "submitted"
    APPROVED   = "approved"
    REFUSED    = "refused"
    POSTED     = "posted"       # journal entry created
    PAID       = "paid"


class ExpenseCategory(str, Enum):
    # Standard (mirrors Odoo hr_expense categories)
    TRAVEL          = "travel"
    MEALS           = "meals"
    ACCOMMODATION   = "accommodation"
    EQUIPMENT       = "equipment"
    TRAINING        = "training"
    SOFTWARE        = "software"
    COMMUNICATION   = "communication"
    OTHER           = "other"

    # HACM-only: AI capital expense categories
    AI_TOKEN_SPEND  = "ai_token_spend"     # agent token consumption
    AI_MODEL_COST   = "ai_model_cost"      # model API subscription
    AI_TRAINING     = "ai_training"        # fine-tuning, custom models
    AI_INFRA        = "ai_infra"           # compute, storage for agents


class ExpenseType(str, Enum):
    HUMAN      = "human"        # employee expense
    AI_AGENT   = "ai_agent"     # agent token/compute expense
    MIXED      = "mixed"        # team expense covering both


class PaymentMethod(str, Enum):
    EMPLOYEE_CARD   = "employee_card"
    COMPANY_CARD    = "company_card"
    CASH            = "cash"
    TOKEN_BUDGET    = "token_budget"    # HACM-only: AI agent spend
    DIRECT_BILL     = "direct_bill"


# ── IN-MEMORY STORE ───────────────────────────────────────────────────
_expenses: Dict[str, dict] = {}
_reports: Dict[str, dict] = {}
_policies: Dict[str, dict] = {
    "default": {
        "max_single_expense": 5000.0,
        "requires_receipt_above": 25.0,
        "max_meal_per_day": 75.0,
        "max_hotel_per_night": 250.0,
        "ai_token_auto_approve_below": 500.0,   # HACM-only
        "ai_spend_requires_roi_above": 0.0,     # flag if ROI negative
    }
}
_counter = {"expense": 0, "report": 0}


# ── SCHEMAS ──────────────────────────────────────────────────────────

class ExpenseLineIn(BaseModel):
    name: str = Field(..., description="Description of the expense")
    category: ExpenseCategory
    expense_date: date
    total_amount: float = Field(..., ge=0)
    currency: str = "USD"
    payment_method: PaymentMethod = PaymentMethod.EMPLOYEE_CARD
    receipt_attached: bool = False
    notes: Optional[str] = None

    # Human expense fields (mirrors Odoo)
    employee_id: Optional[str] = None
    project_code: Optional[str] = None
    billable: bool = False

    # AI agent expense fields (HACM-only)
    agent_id: Optional[str] = None
    tokens_consumed: Optional[float] = None
    token_cost_per_unit: Optional[float] = None
    task_context: Optional[str] = None
    fitness_impact: Optional[float] = None      # measured activation gain
    mandate_coherent: Optional[bool] = None     # was agent within mandate?


class ExpenseReportIn(BaseModel):
    name: str
    employee_id: str
    expense_date_from: date
    expense_date_to: date
    expense_lines: List[ExpenseLineIn] = Field(default_factory=list)
    include_ai_spend: bool = True               # include agent expenses in this report


class ExpenseApprovalIn(BaseModel):
    report_id: str
    action: str                                 # "approve" or "refuse"
    manager_id: str
    comment: Optional[str] = None


# ── BUSINESS LOGIC ────────────────────────────────────────────────────

def _policy_check(line: ExpenseLineIn, policy: dict) -> dict:
    """Check one expense line against policy. Returns violations."""
    violations = []
    warnings = []

    if line.total_amount > policy["max_single_expense"]:
        violations.append(f"Amount ${line.total_amount:,.2f} exceeds single-expense limit ${policy['max_single_expense']:,.0f}")

    if line.total_amount > policy["requires_receipt_above"] and not line.receipt_attached:
        violations.append(f"Receipt required for expenses above ${policy['requires_receipt_above']:.0f}")

    if line.category == ExpenseCategory.MEALS and line.total_amount > policy["max_meal_per_day"]:
        warnings.append(f"Meal expense ${line.total_amount:.2f} exceeds daily limit ${policy['max_meal_per_day']:.0f}")

    if line.category == ExpenseCategory.ACCOMMODATION and line.total_amount > policy["max_hotel_per_night"]:
        warnings.append(f"Accommodation ${line.total_amount:.2f} exceeds nightly limit ${policy['max_hotel_per_night']:.0f}")

    # HACM-only: AI spend checks
    if line.category == ExpenseCategory.AI_TOKEN_SPEND:
        if line.fitness_impact is not None and line.fitness_impact < policy["ai_spend_requires_roi_above"]:
            warnings.append(f"AI spend generated negative fitness impact ({line.fitness_impact:.3f}). Review token utilization.")
        if line.mandate_coherent is False:
            violations.append("Agent was outside mandate during this spend. Requires governance review.")
        if line.total_amount <= policy["ai_token_auto_approve_below"]:
            warnings.append(f"AI spend under ${policy['ai_token_auto_approve_below']:.0f} — eligible for auto-approval")

    return {"violations": violations, "warnings": warnings, "auto_approvable": len(violations) == 0}


def _categorize_spend(lines: List[dict]) -> dict:
    human_total = sum(l["total_amount"] for l in lines if l["expense_type"] == "human")
    ai_total = sum(l["total_amount"] for l in lines if l["expense_type"] == "ai_agent")
    by_category = {}
    for l in lines:
        cat = l["category"]
        by_category[cat] = by_category.get(cat, 0) + l["total_amount"]
    return {
        "human_total": round(human_total, 2),
        "ai_total": round(ai_total, 2),
        "combined_total": round(human_total + ai_total, 2),
        "ai_pct": round(ai_total / max(human_total + ai_total, 0.01) * 100, 1),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda x: -x[1])},
    }


# ── ENDPOINTS ─────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"service": "expenses", "port": 8006, "status": "ok"}

@app.get("/health")
def health():
    return {"status": "up", "service": "expenses",
            "total_expenses": len(_expenses), "total_reports": len(_reports)}


@app.post("/expenses")
def create_expense(line: ExpenseLineIn):
    """Create a single expense line (human or AI agent)."""
    _counter["expense"] += 1
    exp_id = f"EXP-{_counter['expense']:04d}"

    # Determine type
    exp_type = "ai_agent" if line.category in (
        ExpenseCategory.AI_TOKEN_SPEND, ExpenseCategory.AI_MODEL_COST,
        ExpenseCategory.AI_TRAINING, ExpenseCategory.AI_INFRA
    ) else "human"

    policy = _policies.get("default", {})
    policy_result = _policy_check(line, policy)

    expense = {
        "expense_id": exp_id,
        "created_at": datetime.utcnow().isoformat(),
        "state": ExpenseState.DRAFT.value,
        "expense_type": exp_type,
        **line.model_dump(),
        "expense_date": str(line.expense_date),
        "policy_check": policy_result,
    }
    _expenses[exp_id] = expense
    return expense


@app.get("/expenses")
def list_expenses(employee_id: Optional[str] = None, agent_id: Optional[str] = None,
                   expense_type: Optional[str] = None, state: Optional[str] = None):
    items = list(_expenses.values())
    if employee_id:
        items = [e for e in items if e.get("employee_id") == employee_id]
    if agent_id:
        items = [e for e in items if e.get("agent_id") == agent_id]
    if expense_type:
        items = [e for e in items if e.get("expense_type") == expense_type]
    if state:
        items = [e for e in items if e.get("state") == state]
    return {"total": len(items), "expenses": items}


@app.get("/expenses/{expense_id}")
def get_expense(expense_id: str):
    e = _expenses.get(expense_id)
    if not e:
        raise HTTPException(status_code=404, detail="Expense not found")
    return e


@app.post("/reports")
def create_report(report: ExpenseReportIn):
    """
    Create an expense report — groups expense lines for approval.
    HACM combines human and AI agent expenses in one report.
    Odoo hr_expense only covers human employees.
    """
    _counter["report"] += 1
    report_id = f"RPT-{_counter['report']:04d}"
    policy = _policies.get("default", {})

    # Process lines
    processed_lines = []
    for line in report.expense_lines:
        exp_type = "ai_agent" if line.category in (
            ExpenseCategory.AI_TOKEN_SPEND, ExpenseCategory.AI_MODEL_COST,
            ExpenseCategory.AI_TRAINING, ExpenseCategory.AI_INFRA
        ) else "human"
        policy_result = _policy_check(line, policy)
        processed_lines.append({
            **line.model_dump(),
            "expense_date": str(line.expense_date),
            "expense_type": exp_type,
            "policy_check": policy_result,
        })

    spend_summary = _categorize_spend(processed_lines)
    all_ok = all(l["policy_check"]["violations"] == [] for l in processed_lines)
    has_violations = not all_ok

    rec = {
        "report_id": report_id,
        "name": report.name,
        "employee_id": report.employee_id,
        "date_from": str(report.expense_date_from),
        "date_to": str(report.expense_date_to),
        "created_at": datetime.utcnow().isoformat(),
        "state": ExpenseState.SUBMITTED.value,
        "include_ai_spend": report.include_ai_spend,
        "lines": processed_lines,
        "spend_summary": spend_summary,
        "policy_violations": has_violations,
        "auto_approvable": all_ok,
        "total_lines": len(processed_lines),
        # HACM insight: compare AI spend to B* threshold
        "hacm_insight": _hacm_expense_insight(spend_summary),
    }
    _reports[report_id] = rec
    return rec


@app.get("/reports")
def list_reports(employee_id: Optional[str] = None, state: Optional[str] = None):
    items = list(_reports.values())
    if employee_id:
        items = [r for r in items if r.get("employee_id") == employee_id]
    if state:
        items = [r for r in items if r.get("state") == state]
    return {"total": len(items), "reports": items}


@app.post("/reports/{report_id}/approve")
def approve_report(report_id: str, approval: ExpenseApprovalIn):
    r = _reports.get(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="Report not found")
    if approval.action == "approve":
        r["state"] = ExpenseState.APPROVED.value
    elif approval.action == "refuse":
        r["state"] = ExpenseState.REFUSED.value
    r["approved_by"] = approval.manager_id
    r["approval_comment"] = approval.comment
    r["approved_at"] = datetime.utcnow().isoformat()
    return r


@app.get("/summary")
def org_expense_summary():
    """Org-wide expense summary — human + AI combined."""
    all_lines = []
    for r in _reports.values():
        all_lines.extend(r.get("lines", []))
    for e in _expenses.values():
        all_lines.append(e)

    if not all_lines:
        return {"message": "No expenses recorded yet", "total": 0}

    summary = _categorize_spend(all_lines)
    return {
        "total_expense_lines": len(all_lines),
        "total_reports": len(_reports),
        **summary,
        "hacm_insight": _hacm_expense_insight(summary),
    }


@app.post("/ai-spend")
def log_ai_spend(agent_id: str, tokens_consumed: float,
                  token_cost_per_unit: float = 0.001,
                  task_context: Optional[str] = None,
                  fitness_impact: Optional[float] = None,
                  mandate_coherent: bool = True):
    """
    Convenience endpoint: log AI agent token spend as an expense.
    Called automatically by the twin service when agents consume tokens.
    No Odoo equivalent.
    """
    line = ExpenseLineIn(
        name=f"Token spend — {agent_id}" + (f" — {task_context}" if task_context else ""),
        category=ExpenseCategory.AI_TOKEN_SPEND,
        expense_date=date.today(),
        total_amount=round(tokens_consumed * token_cost_per_unit, 4),
        payment_method=PaymentMethod.TOKEN_BUDGET,
        receipt_attached=True,  # auto-generated receipt
        agent_id=agent_id,
        tokens_consumed=tokens_consumed,
        token_cost_per_unit=token_cost_per_unit,
        task_context=task_context,
        fitness_impact=fitness_impact,
        mandate_coherent=mandate_coherent,
    )
    return create_expense(line)


@app.get("/categories")
def get_categories():
    return {
        "human_categories": [
            {"id": c.value, "label": c.value.replace("_", " ").title()}
            for c in ExpenseCategory
            if not c.value.startswith("ai_")
        ],
        "ai_categories": [
            {"id": c.value, "label": c.value.replace("_", " ").title()}
            for c in ExpenseCategory
            if c.value.startswith("ai_")
        ],
        "hacm_note": "AI categories have no equivalent in Odoo hr_expense. "
                     "Token spend is the Huang (2026) compensation model in accounting form.",
    }


@app.get("/policy")
def get_policy():
    return _policies.get("default", {})


@app.put("/policy")
def update_policy(policy: dict):
    _policies["default"].update(policy)
    return _policies["default"]


# ── HACM INSIGHT ──────────────────────────────────────────────────────

def _hacm_expense_insight(summary: dict) -> dict:
    ai_total = summary.get("ai_total", 0)
    human_total = summary.get("human_total", 0)
    ai_pct = summary.get("ai_pct", 0)

    if ai_total == 0:
        return {
            "label": "No AI spend recorded",
            "message": "AI agent token consumption not yet being tracked as expenses. Enable /ai-spend logging.",
            "color": "#A85A10",
        }
    if ai_pct < 5:
        return {
            "label": "AI spend very low",
            "message": f"AI expenses are {ai_pct:.1f}% of total. Per Huang (2026), AI capability investment should be ~50% of salary value. Consider whether agents are under-utilized.",
            "color": "#A85A10",
        }
    return {
        "label": f"AI: {ai_pct:.0f}% of total spend",
        "message": f"${ai_total:,.2f} AI spend alongside ${human_total:,.2f} human expenses. "
                   f"Review against token budget B* threshold to ensure positive ROI.",
        "color": "#3D8016",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8006)))
