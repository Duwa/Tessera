"""HACM Payroll Service — port 8002"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import date
from payroll_engine import HACMPayrollEngine, HacmContract, ContractType, PayslipState

app = FastAPI(title="Tessera Payroll Service", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ContractIn(BaseModel):
    employee_id: str
    employee_name: str
    unit_type: str = "human"
    contract_type: str = "full_time"
    start_date: date
    end_date: Optional[date] = None
    annual_salary: float = 0.0
    monthly_wage: float = 0.0
    housing_allowance: float = 0.0
    transport_allowance: float = 0.0
    meal_allowance: float = 0.0
    monthly_token_allocation: float = 0.0
    token_cost_per_unit: float = 0.001
    agent_model: Optional[str] = None
    skill_uplift: float = 1.0
    department: str = "Engineering"

class SinglePayslipRequest(BaseModel):
    contract: ContractIn
    date_from: date
    date_to: date
    tokens_consumed: float = 0.0
    tasks_completed: int = 0

class PayslipRunRequest(BaseModel):
    run_id: str
    name: str
    date_from: date
    date_to: date
    contracts: List[ContractIn]
    token_consumption: Dict[str, float] = Field(default_factory=dict)

@app.get("/")
def root(): return {"service": "payroll", "version": "2.0.0", "port": 8002}

@app.get("/health")
def health(): return {"status": "up", "service": "payroll"}

@app.post("/payslip")
def compute_payslip(req: SinglePayslipRequest):
    c = _build_contract(req.contract)
    if c.unit_type == "human":
        slip = HACMPayrollEngine.compute_human_payslip(c, req.date_from, req.date_to, req.tokens_consumed, req.tasks_completed)
    else:
        slip = HACMPayrollEngine.compute_agent_payslip(c, req.date_from, req.date_to, req.tokens_consumed, req.tasks_completed)
    return _serialize_slip(slip)

@app.post("/run")
def run_payroll(req: PayslipRunRequest):
    contracts = [_build_contract(c) for c in req.contracts]
    run = HACMPayrollEngine.run_payroll(req.run_id, req.name, req.date_from, req.date_to, contracts, req.token_consumption)
    return {
        "run_id": run.run_id, "name": run.name,
        "date_from": str(run.date_from), "date_to": str(run.date_to),
        "headcount_human": run.headcount_human, "headcount_ai": run.headcount_ai,
        "total_capital_units": run.total_capital_units,
        "total_gross": run.total_gross, "total_net": run.total_net,
        "total_deductions": run.total_deductions, "total_employer_cost": run.total_employer_cost,
        "total_token_allocated": run.total_token_allocated,
        "total_token_consumed": run.total_token_consumed,
        "total_token_cost": run.total_token_cost,
        "token_utilization_pct": run.token_utilization_pct,
        "b_star_status": run.b_star_status, "huang_ratio": run.huang_ratio,
        "department_breakdown": run.department_breakdown,
        "payslips": [_serialize_slip(s) for s in run.payslips],
    }

@app.get("/odoo-comparison")
def odoo_comparison():
    """What Tessera adds vs Odoo payroll."""
    return {
        "odoo_covers": ["basic_wage","gross_wage","net_wage","tax_deductions","allowances","payslip_lines","payslip_run","contract_types"],
        "tessera_adds": {
            "token_allocation": "AI capability budget per person — no Odoo equivalent",
            "dual_total_compensation": "Net salary + token allocation = true cost",
            "b_star_status": "Threshold detection per payslip and org run",
            "huang_benchmark": "Role-weighted target column on every payslip",
            "token_utilization_scoring": "Under-use flagged as org failure",
            "ai_agent_payslips": "AI agents are capital units with token cost ledgers",
            "department_breakdown": "Per-department token ROI in every pay run",
            "employer_total_cost": "Gross + employer taxes + token budget = true org cost",
        }
    }

@app.get("/tax-brackets")
def tax_brackets():
    return {
        "country": "US", "year": 2025,
        "brackets": [
            {"min": 0, "max": 11600, "rate": 0.10}, {"min": 11600, "max": 47150, "rate": 0.12},
            {"min": 47150, "max": 100525, "rate": 0.22}, {"min": 100525, "max": 191950, "rate": 0.24},
            {"min": 191950, "max": 243725, "rate": 0.32}, {"min": 243725, "max": 609350, "rate": 0.35},
            {"min": 609350, "max": None, "rate": 0.37},
        ],
        "social_security_rate": 0.062, "social_security_wage_base": 168600,
        "medicare_rate": 0.0145, "huang_token_ratio": 0.50, "b_star_per_person": 120.0,
    }

def _build_contract(c: ContractIn) -> HacmContract:
    ct_map = {"full_time": ContractType.FULL_TIME, "part_time": ContractType.PART_TIME,
              "contract": ContractType.CONTRACT, "ai_agent": ContractType.AI_AGENT}
    return HacmContract(
        employee_id=c.employee_id, employee_name=c.employee_name,
        unit_type=c.unit_type, contract_type=ct_map.get(c.contract_type, ContractType.FULL_TIME),
        start_date=c.start_date, end_date=c.end_date,
        annual_salary=c.annual_salary, monthly_wage=c.monthly_wage,
        housing_allowance=c.housing_allowance, transport_allowance=c.transport_allowance,
        meal_allowance=c.meal_allowance, monthly_token_allocation=c.monthly_token_allocation,
        token_cost_per_unit=c.token_cost_per_unit, agent_model=c.agent_model,
        skill_uplift=c.skill_uplift, department=c.department,
    )

def _serialize_slip(slip):
    return {
        "payslip_id": slip.payslip_id, "employee_id": slip.contract.employee_id,
        "employee_name": slip.contract.employee_name, "unit_type": slip.contract.unit_type,
        "date_from": str(slip.date_from), "date_to": str(slip.date_to),
        "gross_salary": slip.gross_salary, "total_deductions": slip.total_deductions,
        "net_salary": slip.net_salary, "tokens_allocated": slip.tokens_allocated,
        "tokens_consumed": slip.tokens_consumed, "tokens_cost": slip.tokens_cost,
        "total_token_allocation": slip.total_token_allocation,
        "total_compensation": slip.total_compensation,
        "total_employer_cost": slip.total_employer_cost,
        "b_star_status": slip.b_star_status, "b_star_threshold": slip.b_star_threshold,
        "huang_benchmark": slip.huang_benchmark, "huang_variance": slip.huang_variance,
        "token_utilization_pct": slip.token_utilization_pct,
        "cost_per_outcome": slip.cost_per_outcome,
        "lines": [{"code": l.rule_code, "name": l.rule_name, "category": l.category.value,
                   "total": l.total, "note": l.note, "odoo_equivalent": l.odoo_equivalent}
                  for l in slip.lines],
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8002)))
