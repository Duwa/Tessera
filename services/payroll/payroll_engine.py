"""
HACM Payroll Engine — Tessera v2
==================================
Rivals and beats Odoo Payroll (hr.payslip) by adding:

ODOO DOES:
  - Salary rules, gross/net, deductions, tax tables
  - Payslip runs (batch), contract types
  - Integration with attendance/time-off

TESSERA ADDS ON TOP:
  1. Dual compensation stream: salary + AI token allocation
  2. B* threshold detection per payslip and per org run
  3. Huang (2026) benchmark column on every payslip
  4. AI agent payslips (no Odoo equivalent)
  5. Token utilization scoring — under-use = org failure
  6. Employer total cost = salary + employer taxes + token budget
  7. Skills-linked token uplift (agents specialising = more token budget)
  8. Cost-per-outcome analytics (token spend / tasks resolved)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict
from datetime import date
import math


# ── ENUMS ─────────────────────────────────────────────────────────────

class PayslipState(str, Enum):
    DRAFT = "draft"
    VERIFIED = "verified"
    DONE = "done"
    CANCELLED = "cancelled"

class ContractType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    AI_AGENT = "ai_agent"

class SalaryRuleCategory(str, Enum):
    BASIC = "BASIC"
    ALLOWANCE = "ALW"
    DEDUCTION = "DED"
    GROSS = "GROSS"
    NET = "NET"
    TOKEN = "TOKEN"
    TOTAL_COMP = "TOTAL_COMP"
    BENCHMARK = "BENCHMARK"   # NEW: Huang comparison lines


# ── TAX TABLES (US 2025) ──────────────────────────────────────────────

US_FEDERAL_BRACKETS = [
    (0, 11600, 0.10), (11600, 47150, 0.12), (47150, 100525, 0.22),
    (100525, 191950, 0.24), (191950, 243725, 0.32),
    (243725, 609350, 0.35), (609350, float("inf"), 0.37),
]
SS_RATE = 0.062
SS_WAGE_BASE = 168600
MEDICARE_RATE = 0.0145
ADD_MEDICARE_RATE = 0.009
ADD_MEDICARE_THRESHOLD = 200000

# Odoo does NOT have these — HACM-specific constants
B_STAR_PER_PERSON = 120.0      # Rajendra (2026) §8.4
HUANG_SALARY_RATIO = 0.50      # Huang (2026) benchmark


@dataclass
class HacmContract:
    employee_id: str
    employee_name: str
    unit_type: str
    contract_type: ContractType
    start_date: date
    end_date: Optional[date] = None
    annual_salary: float = 0.0
    monthly_wage: float = 0.0
    hourly_rate: Optional[float] = None
    housing_allowance: float = 0.0
    transport_allowance: float = 0.0
    meal_allowance: float = 0.0
    monthly_token_allocation: float = 0.0
    token_cost_per_unit: float = 0.001
    agent_model: Optional[str] = None
    agent_hourly_rate: float = 0.0
    mandate_text: Optional[str] = None
    # NEW: skill uplift multiplier (linked agents → higher allocation)
    skill_uplift: float = 1.0
    # NEW: department for Huang role-based allocation
    department: str = "Engineering"

    def __post_init__(self):
        if self.monthly_wage == 0.0 and self.annual_salary > 0:
            self.monthly_wage = self.annual_salary / 12
        if self.monthly_token_allocation == 0.0 and self.monthly_wage > 0:
            self.monthly_token_allocation = self.monthly_wage * HUANG_SALARY_RATIO * self.skill_uplift


@dataclass
class HacmPayslipLine:
    rule_code: str
    rule_name: str
    category: SalaryRuleCategory
    quantity: float = 1.0
    rate: float = 0.0
    amount: float = 0.0
    total: float = 0.0
    note: str = ""
    odoo_equivalent: str = ""   # NEW: maps to Odoo rule code or "NO_EQUIVALENT"

    def __post_init__(self):
        if self.total == 0.0 and self.amount != 0.0:
            self.total = self.quantity * self.rate * self.amount if self.rate else self.amount


@dataclass
class HacmPayslip:
    payslip_id: str
    contract: HacmContract
    date_from: date
    date_to: date
    state: PayslipState = PayslipState.DRAFT
    tokens_allocated: float = 0.0
    tokens_consumed: float = 0.0
    tokens_cost: float = 0.0
    lines: List[HacmPayslipLine] = field(default_factory=list)
    gross_salary: float = 0.0
    total_deductions: float = 0.0
    net_salary: float = 0.0
    total_token_allocation: float = 0.0
    total_compensation: float = 0.0
    employer_ss: float = 0.0
    employer_medicare: float = 0.0
    total_employer_cost: float = 0.0
    # NEW fields (no Odoo equivalent)
    b_star_threshold: float = 0.0
    b_star_status: str = ""
    huang_benchmark: float = 0.0
    huang_variance: float = 0.0
    token_utilization_pct: float = 0.0
    cost_per_outcome: float = 0.0    # token cost / tasks resolved (AI agents)
    roi_multiple: float = 0.0


@dataclass
class HacmPayslipRun:
    run_id: str
    name: str
    date_from: date
    date_to: date
    state: PayslipState = PayslipState.DRAFT
    payslips: List[HacmPayslip] = field(default_factory=list)
    total_gross: float = 0.0
    total_net: float = 0.0
    total_deductions: float = 0.0
    total_employer_cost: float = 0.0
    total_token_allocated: float = 0.0
    total_token_consumed: float = 0.0
    total_token_cost: float = 0.0
    token_utilization_pct: float = 0.0
    b_star_status: str = ""
    huang_ratio: float = 0.0
    headcount_human: int = 0
    headcount_ai: int = 0
    total_capital_units: int = 0
    # NEW
    org_fitness_estimate: float = 0.0
    capital_efficiency: float = 0.0   # output per $ total comp
    department_breakdown: Dict = field(default_factory=dict)


class HACMPayrollEngine:

    @staticmethod
    def compute_federal_tax(annual_gross: float) -> float:
        tax = 0.0
        for lower, upper, rate in US_FEDERAL_BRACKETS:
            if annual_gross <= lower:
                break
            tax += (min(annual_gross, upper) - lower) * rate
        return tax / 12

    @staticmethod
    def compute_ss(monthly_wage: float) -> float:
        return min(monthly_wage, SS_WAGE_BASE / 12) * SS_RATE

    @staticmethod
    def compute_medicare(monthly_wage: float) -> float:
        base = monthly_wage * MEDICARE_RATE
        annual = monthly_wage * 12
        if annual > ADD_MEDICARE_THRESHOLD:
            base += (annual - ADD_MEDICARE_THRESHOLD) / 12 * ADD_MEDICARE_RATE
        return base

    @classmethod
    def compute_human_payslip(cls, contract, date_from, date_to,
                               tokens_consumed=0.0, tasks_completed=0,
                               payslip_id=None) -> HacmPayslip:
        slip = HacmPayslip(
            payslip_id=payslip_id or f"SLIP-{contract.employee_id}-{date_from}",
            contract=contract, date_from=date_from, date_to=date_to,
            tokens_allocated=contract.monthly_token_allocation,
            tokens_consumed=tokens_consumed,
            tokens_cost=tokens_consumed * contract.token_cost_per_unit,
        )
        lines = []

        # BASIC + ALLOWANCES (mirrors Odoo)
        basic = contract.monthly_wage
        lines.append(HacmPayslipLine("BASIC", "Basic Salary", SalaryRuleCategory.BASIC,
                     amount=basic, total=basic, odoo_equivalent="BASIC"))
        gross = basic
        for code, name, val in [
            ("HRA", "Housing Allowance", contract.housing_allowance),
            ("TA",  "Transport Allowance", contract.transport_allowance),
            ("MA",  "Meal Allowance", contract.meal_allowance),
        ]:
            if val:
                lines.append(HacmPayslipLine(code, name, SalaryRuleCategory.ALLOWANCE,
                             amount=val, total=val, odoo_equivalent=code))
                gross += val

        slip.gross_salary = gross
        lines.append(HacmPayslipLine("GROSS", "Gross Salary", SalaryRuleCategory.GROSS,
                     amount=gross, total=gross, odoo_equivalent="GROSS"))

        # DEDUCTIONS (mirrors Odoo)
        ss = cls.compute_ss(basic)
        medicare = cls.compute_medicare(basic)
        fed_tax = cls.compute_federal_tax(gross * 12)
        for code, name, val in [
            ("SS_EE", "Social Security (Employee)", -ss),
            ("MEDICARE", "Medicare", -medicare),
            ("FED_TAX", "Federal Income Tax", -fed_tax),
        ]:
            lines.append(HacmPayslipLine(code, name, SalaryRuleCategory.DEDUCTION,
                         amount=val, total=val, odoo_equivalent=code))

        total_ded = ss + medicare + fed_tax
        slip.total_deductions = total_ded
        net = gross - total_ded
        slip.net_salary = net
        lines.append(HacmPayslipLine("NET", "Net Salary", SalaryRuleCategory.NET,
                     amount=net, total=net, odoo_equivalent="NET"))

        # ── TOKEN LINES — NO ODOO EQUIVALENT ─────────────────────────
        token_alloc = contract.monthly_token_allocation
        token_used_cost = tokens_consumed * contract.token_cost_per_unit
        token_delta = token_alloc - token_used_cost
        util = (token_used_cost / token_alloc * 100) if token_alloc > 0 else 0
        slip.token_utilization_pct = util

        lines.append(HacmPayslipLine("TOKEN_ALLOC", "AI Token Allocation",
                     SalaryRuleCategory.TOKEN, amount=token_alloc, total=token_alloc,
                     note=f"Huang (2026): {HUANG_SALARY_RATIO*100:.0f}% of salary. Not taxable.",
                     odoo_equivalent="NO_EQUIVALENT"))
        lines.append(HacmPayslipLine("TOKEN_USED", "Token Utilization (Actual)",
                     SalaryRuleCategory.TOKEN, amount=token_used_cost, total=token_used_cost,
                     note=f"{util:.0f}% of allocation. {'⚠ Under-utilized' if util < 40 else '✓ Good utilization'}",
                     odoo_equivalent="NO_EQUIVALENT"))
        lines.append(HacmPayslipLine("TOKEN_DELTA", "Token Balance",
                     SalaryRuleCategory.TOKEN, amount=token_delta, total=token_delta,
                     note="Unspent allocation. Per Huang model, zero utilization = org cost with no return.",
                     odoo_equivalent="NO_EQUIVALENT"))

        # ── BENCHMARK LINE — NO ODOO EQUIVALENT ──────────────────────
        huang_benchmark = basic * HUANG_SALARY_RATIO
        huang_variance = token_alloc - huang_benchmark
        slip.huang_benchmark = huang_benchmark
        slip.huang_variance = huang_variance
        lines.append(HacmPayslipLine("HUANG_BENCH", "Huang (2026) Benchmark",
                     SalaryRuleCategory.BENCHMARK,
                     amount=huang_benchmark, total=huang_benchmark,
                     note=f"Recommended allocation. Variance: ${huang_variance:+.2f}/mo.",
                     odoo_equivalent="NO_EQUIVALENT"))

        # ── B* THRESHOLD LINE — NO ODOO EQUIVALENT ──────────────────
        b_star = B_STAR_PER_PERSON
        slip.b_star_threshold = b_star
        b_status = "ABOVE_B*" if token_alloc >= b_star else ("NEAR_B*" if token_alloc >= b_star * 0.5 else "BELOW_B*")
        slip.b_star_status = b_status
        lines.append(HacmPayslipLine("B_STAR", f"B* Status ({b_status})",
                     SalaryRuleCategory.BENCHMARK,
                     amount=token_alloc - b_star, total=token_alloc - b_star,
                     note=f"B*=${b_star}/person/mo. {'Positive fitness returns.' if token_alloc >= b_star else 'Investment not clearing threshold.'}",
                     odoo_equivalent="NO_EQUIVALENT"))

        # TOTAL COMP
        total_comp = net + token_alloc
        slip.total_token_allocation = token_alloc
        slip.total_compensation = total_comp
        lines.append(HacmPayslipLine("TOTAL_COMP", "Total Compensation (Dual)",
                     SalaryRuleCategory.TOTAL_COMP, amount=total_comp, total=total_comp,
                     note="Net salary + AI token allocation. True total compensation.",
                     odoo_equivalent="NO_EQUIVALENT"))

        slip.employer_ss = basic * SS_RATE
        slip.employer_medicare = basic * MEDICARE_RATE
        slip.total_employer_cost = gross + slip.employer_ss + slip.employer_medicare + token_alloc
        slip.lines = lines
        slip.state = PayslipState.VERIFIED
        return slip

    @classmethod
    def compute_agent_payslip(cls, contract, date_from, date_to,
                               tokens_consumed=0.0, tasks_completed=0,
                               decisions_participated=0, payslip_id=None) -> HacmPayslip:
        """AI agent cost ledger. No Odoo equivalent at all."""
        slip = HacmPayslip(
            payslip_id=payslip_id or f"AGENT-{contract.employee_id}-{date_from}",
            contract=contract, date_from=date_from, date_to=date_to,
            tokens_allocated=contract.monthly_token_allocation,
            tokens_consumed=tokens_consumed,
            tokens_cost=tokens_consumed * contract.token_cost_per_unit,
        )
        lines = []
        alloc = contract.monthly_token_allocation
        cost = tokens_consumed * contract.token_cost_per_unit
        util = (cost / alloc * 100) if alloc > 0 else 0
        delta = alloc - cost
        cost_per_task = (cost / tasks_completed) if tasks_completed > 0 else 0
        slip.token_utilization_pct = util
        slip.cost_per_outcome = cost_per_task

        lines.append(HacmPayslipLine("TOKEN_ALLOC", "Token Budget Allocated",
                     SalaryRuleCategory.TOKEN, amount=alloc, total=alloc,
                     odoo_equivalent="NO_EQUIVALENT"))
        lines.append(HacmPayslipLine("TOKEN_USED", "Token Cost (Actual)",
                     SalaryRuleCategory.TOKEN, amount=-cost, total=-cost,
                     note=f"{tokens_consumed:,.0f} tokens @ ${contract.token_cost_per_unit:.4f}. {util:.0f}% utilized.",
                     odoo_equivalent="NO_EQUIVALENT"))
        lines.append(HacmPayslipLine("TOKEN_DELTA", "Budget Remaining",
                     SalaryRuleCategory.TOKEN, amount=delta, total=delta,
                     note=f"Tasks completed: {tasks_completed}. Cost/task: ${cost_per_task:.3f}.",
                     odoo_equivalent="NO_EQUIVALENT"))
        if tasks_completed > 0:
            lines.append(HacmPayslipLine("COST_PER_TASK", "Cost per Task Resolved",
                         SalaryRuleCategory.BENCHMARK, amount=cost_per_task, total=cost_per_task,
                         note=f"Vs human equivalent: compare to human hourly rate × task hours.",
                         odoo_equivalent="NO_EQUIVALENT"))
        lines.append(HacmPayslipLine("TOTAL_COMP", "Total Agent Cost This Period",
                     SalaryRuleCategory.TOTAL_COMP, amount=cost, total=cost,
                     note="Token spend only. No salary. Pure AI capital cost.",
                     odoo_equivalent="NO_EQUIVALENT"))

        b_star = B_STAR_PER_PERSON
        slip.b_star_threshold = b_star
        slip.b_star_status = "ABOVE_B*" if alloc >= b_star else "BELOW_B*"
        slip.total_token_allocation = alloc
        slip.total_compensation = cost
        slip.total_employer_cost = cost
        slip.lines = lines
        slip.state = PayslipState.VERIFIED
        return slip

    @classmethod
    def run_payroll(cls, run_id, name, date_from, date_to,
                    contracts, token_consumption, b_star_per_person=120.0) -> HacmPayslipRun:
        run = HacmPayslipRun(run_id=run_id, name=name, date_from=date_from, date_to=date_to)
        dept_totals = {}

        for contract in contracts:
            consumed = token_consumption.get(contract.employee_id, 0.0)
            if contract.unit_type == "human":
                slip = cls.compute_human_payslip(contract, date_from, date_to, consumed)
                run.headcount_human += 1
                dept = contract.department
                dept_totals.setdefault(dept, {"gross": 0, "tokens": 0, "count": 0})
                dept_totals[dept]["gross"] += slip.gross_salary
                dept_totals[dept]["tokens"] += slip.total_token_allocation
                dept_totals[dept]["count"] += 1
            else:
                slip = cls.compute_agent_payslip(contract, date_from, date_to, consumed)
                run.headcount_ai += 1
            run.payslips.append(slip)

        run.total_capital_units = len(run.payslips)
        run.total_gross = sum(s.gross_salary for s in run.payslips)
        run.total_net = sum(s.net_salary for s in run.payslips)
        run.total_deductions = sum(s.total_deductions for s in run.payslips)
        run.total_employer_cost = sum(s.total_employer_cost for s in run.payslips)
        run.total_token_allocated = sum(s.tokens_allocated for s in run.payslips)
        run.total_token_consumed = sum(s.tokens_consumed for s in run.payslips)
        run.total_token_cost = sum(s.tokens_cost for s in run.payslips)
        if run.total_token_allocated > 0:
            run.token_utilization_pct = run.total_token_cost / run.total_token_allocated * 100
        b_star_monthly = run.headcount_human * b_star_per_person
        monthly_budget = run.total_token_allocated
        if monthly_budget < b_star_monthly * 0.5:
            run.b_star_status = "BELOW_BSTAR"
        elif monthly_budget < b_star_monthly:
            run.b_star_status = "NEAR_BSTAR"
        else:
            run.b_star_status = "ABOVE_BSTAR"
        avg_salary = run.total_gross / max(run.headcount_human, 1)
        huang_target = avg_salary * HUANG_SALARY_RATIO * run.headcount_human
        run.huang_ratio = monthly_budget / max(huang_target, 1.0)
        run.department_breakdown = dept_totals
        run.state = PayslipState.DONE
        return run
