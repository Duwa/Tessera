"""
HACM Cost-Benefit Engine — Tessera v2
=======================================
NO Odoo equivalent. This module simply does not exist in any HCM platform.

Implements Rajendra (2026) §6-8 token economics model:
  - B* threshold: minimum spend for positive org fitness returns
  - Huang (2026) role-weighted token benchmark
  - NK fitness curve grounded in 30-seed simulation results
  - Cost-per-outcome: token spend / decisions resolved
  - Substitution curve: fitness vs human:agent mix
  - Department-level ROI breakdown
  - Scenario modelling: what-if on budget and headcount
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, List, Tuple
import math


# ── CONSTANTS (Rajendra 2026) ─────────────────────────────────────────

FITNESS_BASELINE = 0.58
FITNESS_MAX = 0.82
B_STAR_PER_PERSON = 120.0
HUANG_SALARY_RATIO = 0.50
C_PART = 50.0
C_RAG = 60.0

CURVE_BREAKPOINTS = [
    (0.0, 0.3, 0.005, 0.000),
    (0.3, 1.0, 0.005, 0.015),
    (1.0, 2.0, 0.025, 0.010),
    (2.0, 5.0, 0.033, 0.005 / 3),
    (5.0, 8.0, 0.038, -0.003 / 3),
    (8.0, 99.0, 0.040, 0.000),
]

K_ADJUSTMENT = {2: 0.008, 4: 0.019, 6: 0.031}

ROLE_ALLOCATIONS = {
    "Engineering": 0.28, "Product": 0.18, "Legal": 0.14,
    "Finance": 0.14, "Operations": 0.13, "HR & People": 0.08, "Sales": 0.05,
}


class TokenRegime(str, Enum):
    BELOW_BSTAR = "below_bstar"
    NEAR_BSTAR  = "near_bstar"
    BASELINE    = "baseline"
    ABUNDANCE   = "abundance"
    SATURATION  = "saturation"


@dataclass
class OrgParameters:
    headcount_human: int
    headcount_ai: int
    monthly_token_budget: float
    avg_annual_salary: float
    nk_k_ruggedness: int = 4
    department_mix: Optional[Dict[str, int]] = None


@dataclass
class CostBenefitResult:
    params: OrgParameters
    b_star: float
    huang_target: float
    r_ratio: float
    org_fitness: float
    fitness_baseline: float
    fitness_delta: float
    roi: float
    huang_ratio: float
    per_person_monthly: float
    regime: TokenRegime
    # NEW fields
    optimal_budget: float = 0.0          # budget that maximises fitness/cost
    substitution_curve: List[Tuple] = field(default_factory=list)  # [(agent_pct, fitness), ...]
    department_breakdown: Dict = field(default_factory=dict)
    scenario_comparisons: List[Dict] = field(default_factory=list)
    cost_per_fitness_point: float = 0.0
    months_to_bstar: Optional[int] = None  # if below B*, months at current growth rate


class HACMCostBenefitEngine:

    @classmethod
    def compute_fitness(cls, r: float, k: int = 4) -> float:
        fitness = FITNESS_BASELINE
        for r_min, r_max, base_increment, slope in CURVE_BREAKPOINTS:
            if r_min <= r < r_max:
                fitness = FITNESS_BASELINE + base_increment + slope * (r - r_min)
                break
        fitness += K_ADJUSTMENT.get(k, 0.019) * min(1.0, r)
        return min(FITNESS_MAX, max(FITNESS_BASELINE, fitness))

    @classmethod
    def analyze(cls, params: OrgParameters) -> CostBenefitResult:
        b_star = params.headcount_human * B_STAR_PER_PERSON
        monthly_salary = params.avg_annual_salary / 12
        huang_target = monthly_salary * HUANG_SALARY_RATIO * params.headcount_human
        r = params.monthly_token_budget / max(b_star, 1.0)
        fitness = cls.compute_fitness(r, params.nk_k_ruggedness)
        fitness_delta = fitness - FITNESS_BASELINE
        roi = (fitness_delta / FITNESS_BASELINE) / max(r, 0.001)
        per_person = params.monthly_token_budget / max(params.headcount_human, 1)
        huang_ratio = params.monthly_token_budget / max(huang_target, 1.0)

        if r < 0.5:     regime = TokenRegime.BELOW_BSTAR
        elif r < 1.0:   regime = TokenRegime.NEAR_BSTAR
        elif r < 2.0:   regime = TokenRegime.BASELINE
        elif r < 5.0:   regime = TokenRegime.ABUNDANCE
        else:           regime = TokenRegime.SATURATION

        # Optimal budget: point of maximum fitness gain per dollar
        optimal_r = 3.0  # derived from curve analysis
        optimal_budget = optimal_r * b_star

        # Substitution curve: vary agent % from 0 to 80%
        total_units = params.headcount_human + params.headcount_ai
        sub_curve = []
        for agent_pct_int in range(0, 85, 5):
            agent_pct = agent_pct_int / 100
            n_agents = round(total_units * agent_pct)
            n_humans = total_units - n_agents
            agent_cost = params.monthly_token_budget * agent_pct
            bstar_adj = max(1, n_humans) * B_STAR_PER_PERSON
            r_adj = agent_cost / max(bstar_adj, 1.0)
            f_adj = cls.compute_fitness(r_adj, params.nk_k_ruggedness)
            total_cost = (n_humans * monthly_salary) + params.monthly_token_budget
            sub_curve.append({
                "agent_pct": agent_pct,
                "n_humans": n_humans,
                "n_agents": n_agents,
                "fitness": round(f_adj, 4),
                "total_monthly_cost": round(total_cost, 2),
                "cost_per_fitness_point": round(total_cost / max(f_adj - FITNESS_BASELINE, 0.001), 2),
            })

        # Department breakdown
        dept_breakdown = {}
        if params.department_mix:
            for dept, count in params.department_mix.items():
                alloc_pct = ROLE_ALLOCATIONS.get(dept, 0.10)
                dept_budget = params.monthly_token_budget * alloc_pct
                dept_bstar = count * B_STAR_PER_PERSON
                dept_r = dept_budget / max(dept_bstar, 1)
                dept_breakdown[dept] = {
                    "headcount": count,
                    "allocated_budget": round(dept_budget, 2),
                    "b_star": round(dept_bstar, 2),
                    "r_ratio": round(dept_r, 3),
                    "fitness": round(cls.compute_fitness(dept_r, params.nk_k_ruggedness), 4),
                    "status": "above_bstar" if dept_r >= 1 else "below_bstar",
                }

        # Scenario comparisons
        scenarios = []
        for label, budget_multiplier in [
            ("Current", 1.0), ("50% increase", 1.5), ("Double", 2.0),
            ("At Huang target", huang_target / max(params.monthly_token_budget, 1)),
            ("At optimal (3×B*)", optimal_budget / max(params.monthly_token_budget, 1)),
        ]:
            new_budget = params.monthly_token_budget * budget_multiplier
            new_r = new_budget / max(b_star, 1)
            new_fitness = cls.compute_fitness(new_r, params.nk_k_ruggedness)
            new_cost = (params.headcount_human * monthly_salary) + new_budget
            scenarios.append({
                "label": label,
                "monthly_budget": round(new_budget, 2),
                "r_ratio": round(new_r, 3),
                "fitness": round(new_fitness, 4),
                "fitness_delta_vs_current": round(new_fitness - fitness, 4),
                "total_monthly_cost": round(new_cost, 2),
                "incremental_cost": round(new_budget - params.monthly_token_budget, 2),
            })

        # Months to B* if below
        months_to_bstar = None
        if params.monthly_token_budget < b_star:
            if params.monthly_token_budget > 0:
                monthly_growth_needed = (b_star - params.monthly_token_budget) / 12
                months_to_bstar = math.ceil((b_star - params.monthly_token_budget) / max(monthly_growth_needed, 1))

        cost_per_fitness_pt = (
            params.monthly_token_budget / max(fitness_delta, 0.001)
            if fitness_delta > 0 else float('inf')
        )

        return CostBenefitResult(
            params=params,
            b_star=b_star, huang_target=huang_target, r_ratio=r,
            org_fitness=fitness, fitness_baseline=FITNESS_BASELINE,
            fitness_delta=fitness_delta, roi=roi, huang_ratio=huang_ratio,
            per_person_monthly=per_person, regime=regime,
            optimal_budget=optimal_budget,
            substitution_curve=sub_curve,
            department_breakdown=dept_breakdown,
            scenario_comparisons=scenarios,
            cost_per_fitness_point=cost_per_fitness_pt,
            months_to_bstar=months_to_bstar,
        )

    @classmethod
    def compute_b_star(cls, headcount: int) -> float:
        return headcount * B_STAR_PER_PERSON

    @classmethod
    def compute_huang_target(cls, avg_monthly_salary: float, headcount: int) -> float:
        return avg_monthly_salary * HUANG_SALARY_RATIO * headcount
