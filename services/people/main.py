"""
HACM People Service  —  port 8005
Registry of all capital units: human employees, AI agents, and autonomous units.

Capital unit types:
  human       — biological agent, has HAV, earns wages, φ-guardian eligible
  ai_agent    — software autonomous unit, no physical presence, digital labor
  autonomous  — physical autonomous unit, any form factor (arm, drone, vehicle, humanoid)
                form is metadata — governance driven by labor_domain + autonomy_level
"""
import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, Dict
from datetime import datetime

app = FastAPI(title="HACM People Service", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_units: Dict[str, dict] = {}


class CapitalUnitIn(BaseModel):
    employee_id: str
    name: str
    unit_type: str = Field(..., description="human | ai_agent | autonomous")
    department: Optional[str] = None
    role_title: Optional[str] = None
    annual_salary: Optional[float] = None
    monthly_token_allocation: float = 0.0
    agent_model: Optional[str] = None
    mandate_text: Optional[str] = None
    # Autonomous unit fields (ignored for human/ai_agent)
    physical_form: Optional[str] = None        # arm | drone | vehicle | humanoid | mobile | surgical
    labor_domain: Optional[str] = None         # physical | digital | cognitive | hybrid
    autonomy_level: Optional[float] = Field(None, ge=0.0, le=1.0)  # 0=supervised, 1=fully autonomous
    hav_influence: Optional[float] = Field(None, ge=-1.0, le=0.0)  # effect on human NPF (always ≤0)
    labor_replaces: Optional[list] = None      # list of task types this unit takes from humans


@app.get("/")
def root():
    return {
        "service": "people", "version": "2.0.0", "port": 8005,
        "capital_types": ["human", "ai_agent", "autonomous"],
        "note": "autonomous covers any physical autonomous unit regardless of form factor"
    }


@app.get("/health")
def health():
    counts = _type_counts()
    return {"status": "up", "service": "people", **counts}


def _type_counts():
    all_units = list(_units.values())
    return {
        "total_capital_units": len(all_units),
        "human_count":     sum(1 for u in all_units if u["unit_type"] == "human"),
        "ai_agent_count":  sum(1 for u in all_units if u["unit_type"] == "ai_agent"),
        "autonomous_count": sum(1 for u in all_units if u["unit_type"] == "autonomous"),
    }


@app.post("/units", status_code=201)
def create_unit(unit: CapitalUnitIn):
    if unit.unit_type not in ("human", "ai_agent", "autonomous"):
        raise HTTPException(400, f"unit_type must be human | ai_agent | autonomous, got '{unit.unit_type}'")
    _units[unit.employee_id] = {
        **unit.model_dump(),
        "created_at": datetime.utcnow().isoformat(),
        "activation_score": None,
        "fitness_score": None,
        "belief_alignment": None,
    }
    return {"status": "created", "employee_id": unit.employee_id, "unit_type": unit.unit_type}


@app.get("/units")
def list_units(unit_type: Optional[str] = Query(None), department: Optional[str] = Query(None)):
    units = list(_units.values())
    if unit_type:
        units = [u for u in units if u["unit_type"] == unit_type]
    if department:
        units = [u for u in units if u.get("department") == department]
    return {"total": len(units), "units": units}


@app.get("/units/{employee_id}")
def get_unit(employee_id: str):
    unit = _units.get(employee_id)
    if not unit:
        raise HTTPException(404, "Capital unit not found")
    return unit


@app.patch("/units/{employee_id}/activation")
def update_activation(employee_id: str, activation_score: float, fitness_score: float, belief_alignment: float):
    if employee_id not in _units:
        raise HTTPException(404, "Not found")
    _units[employee_id].update({
        "activation_score": activation_score,
        "fitness_score": fitness_score,
        "belief_alignment": belief_alignment,
        "last_updated": datetime.utcnow().isoformat(),
    })
    return {"status": "updated", "employee_id": employee_id}


@app.get("/composition")
def org_composition(org_id: Optional[str] = Query(None)):
    """
    Capital composition snapshot for twin calibration.
    Returns headcount by type + autonomy-weighted φ estimate.
    """
    all_units = list(_units.values())
    humans    = [u for u in all_units if u["unit_type"] == "human"]
    agents    = [u for u in all_units if u["unit_type"] == "ai_agent"]
    autos     = [u for u in all_units if u["unit_type"] == "autonomous"]

    n_h = len(humans)
    n_a = len(agents)
    n_x = len(autos)

    # Effective φ: autonomous units weighted by autonomy_level
    effective_non_human = n_a + sum(u.get("autonomy_level") or 0.8 for u in autos)
    phi_effective = effective_non_human / max(1, n_h + effective_non_human)

    # Total HAV influence from non-human capital
    total_hav_influence = sum(u.get("hav_influence") or -0.05 for u in agents + autos)

    return {
        "n_human":        n_h,
        "n_ai_agent":     n_a,
        "n_autonomous":   n_x,
        "phi_effective":  round(phi_effective, 4),
        "total_hav_influence": round(total_hav_influence, 4),
        "departments":    list({u.get("department") for u in humans if u.get("department")}),
        "autonomous_forms": list({u.get("physical_form") for u in autos if u.get("physical_form")}),
        "labor_domains":  list({u.get("labor_domain") for u in autos if u.get("labor_domain")}),
        "total_monthly_token_allocation": sum(u.get("monthly_token_allocation", 0) for u in all_units),
        "note": "phi_effective weights autonomous units by their autonomy_level"
    }


@app.get("/summary")
def org_summary():
    all_units = list(_units.values())
    counts = _type_counts()
    humans = [u for u in all_units if u["unit_type"] == "human"]
    return {
        **counts,
        "total_monthly_token_allocation": sum(u.get("monthly_token_allocation", 0) for u in all_units),
        "departments": list({u.get("department") for u in humans if u.get("department")}),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8005)))
