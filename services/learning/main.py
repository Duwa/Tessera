"""HACM Learning Service — port 8003"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import date
from learning_engine import (
    HACMLearningEngine, HumanState, AgentState,
    LearningRegime, AgentSophistication
)

app = FastAPI(title="Tessera Learning Service", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class AgentStateIn(BaseModel):
    agent_id: str
    agent_name: str
    model_version: str = "claude-sonnet-4"
    fitness_score: float = 0.7
    mandate_coherence: float = 0.85
    rag_depth: int = 3
    rag_activations_per_epoch: int = 10
    failure_flag: bool = False
    failure_count_this_epoch: int = 0
    decisions_participated: int = 10
    decisions_resolved: int = 8
    resolution_rate: float = 0.8
    task_domains: List[str] = Field(default_factory=lambda: ["research"])
    avg_tokens_per_task: float = 380.0

class HumanStateIn(BaseModel):
    employee_id: str
    employee_name: str
    role: str
    department: str
    fitness_score: float = 0.65
    activation_score: float = 0.70
    belief_alignment: float = 0.60
    belief_vector: List[float] = Field(default_factory=lambda: [0.5]*12)
    primary_regime: str = "C"
    hcm_level: float = 0.5
    self_directed_rate: float = 0.02
    token_utilization_pct: float = 45.0
    agents_worked_with: List[str] = Field(default_factory=list)
    completed_learning_this_epoch: List[str] = Field(default_factory=list)

class LearningPlanRequest(BaseModel):
    human: HumanStateIn
    agents: List[AgentStateIn]
    org_template_vector: List[float] = Field(default_factory=lambda: [0.6]*12)
    epoch: int = 1

@app.get("/")
def root(): return {"service": "learning", "version": "2.0.0", "port": 8003}

@app.get("/health")
def health(): return {"status": "up", "service": "learning"}

@app.post("/plan")
def generate_plan(req: LearningPlanRequest):
    regime_map = {"A": LearningRegime.A, "B": LearningRegime.B, "C": LearningRegime.C}
    human = HumanState(
        employee_id=req.human.employee_id, employee_name=req.human.employee_name,
        role=req.human.role, department=req.human.department,
        fitness_score=req.human.fitness_score, activation_score=req.human.activation_score,
        belief_alignment=req.human.belief_alignment, belief_vector=req.human.belief_vector,
        primary_regime=regime_map.get(req.human.primary_regime, LearningRegime.C),
        hcm_level=req.human.hcm_level, self_directed_rate=req.human.self_directed_rate,
        token_utilization_pct=req.human.token_utilization_pct,
        agents_worked_with=req.human.agents_worked_with,
        completed_learning_this_epoch=req.human.completed_learning_this_epoch,
    )
    agents = [AgentState(
        agent_id=a.agent_id, agent_name=a.agent_name, model_version=a.model_version,
        fitness_score=a.fitness_score, mandate_coherence=a.mandate_coherence,
        rag_depth=a.rag_depth, rag_activations_per_epoch=a.rag_activations_per_epoch,
        failure_flag=a.failure_flag, failure_count_this_epoch=a.failure_count_this_epoch,
        decisions_participated=a.decisions_participated, decisions_resolved=a.decisions_resolved,
        resolution_rate=a.resolution_rate, task_domains=a.task_domains,
        avg_tokens_per_task=a.avg_tokens_per_task,
    ) for a in req.agents]

    plan = HACMLearningEngine.generate_plan(human, agents, req.org_template_vector, req.epoch)
    return {
        "employee_id": plan.employee_id, "employee_name": plan.employee_name,
        "epoch": plan.epoch, "generated_at": plan.generated_at,
        "one_line_summary": plan.one_line_summary, "primary_theme": plan.primary_theme,
        "immediate_count": plan.immediate_count, "this_week_count": plan.this_week_count,
        "total_hours": plan.total_hours, "replacement_risk_score": plan.replacement_risk_score,
        "huang_learning_gap": plan.huang_learning_gap, "payroll_link": plan.payroll_link,
        "opportunities": [
            {"opportunity_id": o.opportunity_id, "title": o.title,
             "gap_type": o.gap_type.value, "urgency": o.urgency.value,
             "regime": o.regime.value, "format": o.format.value,
             "estimated_hours": o.estimated_hours, "can_do_alongside_work": o.can_do_alongside_work,
             "why_now": o.why_now, "what_you_will_be_able_to_do": o.what_you_will_be_able_to_do,
             "completion_indicator": o.completion_indicator,
             "suggested_resources": o.suggested_resources,
             "agent_id": o.agent_id, "token_utilization_link": o.token_utilization_link,
             "replacement_risk": o.replacement_risk, "odoo_equivalent": o.odoo_equivalent}
            for o in plan.opportunities
        ],
    }

@app.get("/odoo-comparison")
def odoo_comparison():
    return {
        "odoo_elearning": ["Static course catalog", "Videos/PDFs/quizzes", "Certifications",
                           "Karma points/leaderboards", "Forums", "Paid courses", "Progress tracking"],
        "tessera_replaces_with": {
            "dynamic_generation": "Every opportunity generated from live agent state — no static catalog",
            "belief_vector_gaps": "12-dimensional belief analysis — no Odoo equivalent",
            "three_regimes": "A/B/C learning regimes from Rajendra (2026) tripartite model",
            "shadow_agent_format": "Watch an agent work — not possible in Odoo",
            "payroll_integration": "Low token utilization triggers learning plan — unique to Tessera",
            "replacement_risk_score": "Flags when an agent is outpacing the human — no Odoo equivalent",
            "paired_task_format": "Do a real task alongside agent — no Odoo equivalent",
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8003)))
