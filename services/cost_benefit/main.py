"""HACM Cost-Benefit Service — port 8001"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Dict, Optional, List
from cost_benefit_engine import HACMCostBenefitEngine, OrgParameters

app = FastAPI(title="Tessera Cost-Benefit Service", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class OrgParamsIn(BaseModel):
    headcount_human: int = 25
    headcount_ai: int = 8
    monthly_token_budget: float = 3000.0
    avg_annual_salary: float = 85000.0
    nk_k_ruggedness: int = 4
    department_mix: Optional[Dict[str, int]] = None

@app.get("/")
def root(): return {"service": "cost_benefit", "version": "2.0.0", "port": 8001}

@app.get("/health")
def health(): return {"status": "up", "service": "cost_benefit"}

@app.post("/analyze")
def analyze(req: OrgParamsIn):
    params = OrgParameters(
        headcount_human=req.headcount_human, headcount_ai=req.headcount_ai,
        monthly_token_budget=req.monthly_token_budget, avg_annual_salary=req.avg_annual_salary,
        nk_k_ruggedness=req.nk_k_ruggedness, department_mix=req.department_mix,
    )
    result = HACMCostBenefitEngine.analyze(params)
    return {
        "b_star": result.b_star, "huang_target": result.huang_target, "r_ratio": result.r_ratio,
        "org_fitness": result.org_fitness, "fitness_baseline": result.fitness_baseline,
        "fitness_delta": result.fitness_delta, "roi": result.roi, "huang_ratio": result.huang_ratio,
        "per_person_monthly": result.per_person_monthly, "regime": result.regime.value,
        "optimal_budget": result.optimal_budget, "cost_per_fitness_point": result.cost_per_fitness_point,
        "months_to_bstar": result.months_to_bstar,
        "substitution_curve": result.substitution_curve,
        "department_breakdown": result.department_breakdown,
        "scenario_comparisons": result.scenario_comparisons,
    }

@app.get("/b-star")
def b_star(headcount: int = 25):
    return {"b_star": HACMCostBenefitEngine.compute_b_star(headcount),
            "per_person": 120.0, "headcount": headcount}

@app.get("/huang-target")
def huang_target(avg_monthly_salary: float = 7000.0, headcount: int = 25):
    return {"huang_target": HACMCostBenefitEngine.compute_huang_target(avg_monthly_salary, headcount),
            "ratio": 0.50, "headcount": headcount}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8001)))
