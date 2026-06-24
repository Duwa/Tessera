"""
tokens_p5.py
------------
Token extensions for Paper 5: Human Alignment Value (HAV).

"Human Alignment Value and the Obsolescence of Man-Hours: A Computational
Theory of Human Contribution in Human-AI Hybrid Organisations"
(Rajendra 2026e).

New types:
  MeasurementRegime     MAN_HOURS / HAV
  HumanContributionToken  per-contribution π indicator and fitness delta
  HAVRecord               per human per epoch: NPF, SRQ, OC, HAV composite

All Paper 4 token classes are re-exported unchanged.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List

# Re-export everything from Paper 4
from tokens_p4 import (
    ProcessTopology, RoleMode, VNetRecord,
    SolutionP3, DecisionRecordP3,
    Problem, HumanAgent, AIAgent, Opportunity, TokenReservoir,
    OrgMemory, MemEntry, DecisionRecord,
    ProblemType, SolutionType, OpportunityState, DecisionMode, AllocPolicy,
)


class MeasurementRegime(str, Enum):
    """
    How human contribution is measured (Rajendra 2026e, §6.1).

    MAN_HOURS  Legacy: count hours regardless of procedurality.
               Above φ*, actively incentivises Track 2 pathology via the
               Track 2 nudge mechanism: belief shift toward AI centroid
               at rate (1−NPF) × φ per epoch.

    HAV        Human Alignment Value: rewards non-procedural contribution.
               No belief nudge.  Preserves external fitness reference capacity.
               Outperforms MAN_HOURS above the crossover threshold φ*.
    """
    MAN_HOURS = "MAN_HOURS"
    HAV       = "HAV"


@dataclass
class HumanContributionToken:
    """
    One contribution r by a human agent in one opportunity.

    pi_indicator: 1 = repeated endeavour (procedural, HAV = 0)
                  0 = non-procedural judgment (HAV > 0)
    fitness_delta: improvement in fitness attributable to this contribution.
                   Positive for SLA breach recovery events.
    is_recovery:   True if this contribution was triggered by an SLO breach.
    is_origination: True if the framing novelty < θ_novel (OC contribution).
    """
    human_id:        int
    opportunity_id:  int
    pi_indicator:    int    # 0 or 1
    fitness_delta:   float  # can be negative (failed recovery)
    is_recovery:     bool   = False
    is_origination:  bool   = False


@dataclass
class HAVRecord:
    """
    HAV composite for one human agent over one epoch (Rajendra 2026e, §3.4).

    HAV(h,T) = w1×NPF + w2×SRQ + w3×OC
    Base weights: w1=0.50, w2=0.30, w3=0.20

    Alignment Premium = r_AP × HAV × Salary
      r_AP = 0.05 at φ < 0.25 (below crossover)
           = 0.25 at φ > 0.75 (well above crossover)
           = interpolated in between

    Note: HAV is NOT a property of the person.  It is a property of the mode
    in which the person operates in a specific context at a specific time.
    """
    human_id:           int
    epoch:              int
    n_contributions:    int     # total contributions this epoch
    n_non_procedural:   int     # contributions with π(r) = 0
    npf:                float   # Non-Procedure Fraction
    srq:                float   # SLA Recovery Quality (sum of fitness deltas from breaches)
    oc:                 float   # Origination Capacity (fraction of novel framings)
    hav_composite:      float   # 0.50×NPF + 0.30×SRQ + 0.20×OC
    alignment_premium_rate: float  # r_AP based on current φ
    alignment_premium:  float   # r_AP × HAV × 1.0 (salary normalized)
    mode:               RoleMode = RoleMode.ASSISTANT
