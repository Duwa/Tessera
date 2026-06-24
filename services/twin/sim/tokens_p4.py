"""
tokens_p4.py
------------
Token extensions for Paper 4: Process Topology, Role Oscillation, V_net.

"The Boundary Within: Process Topology, Role Oscillation, and the Cost of
Misalignment in Human-AI Hybrid Organisations" (Rajendra 2026d).

New types:
  ProcessTopology   FLAT / SEQ_GATEWAY / PAR_GATEWAY
                    Guard condition on T_resolve.  SEQ_GATEWAY is the
                    dominant topology: catches misalignment with prob lambda_mis
                    before T_resolve fires.

  RoleMode          RPA / ASSISTANT / AUTONOMOUS / FULLY_AUTONOMOUS
                    Human-side role in each opportunity resolution.

  VNetRecord        One observation of the V_net cost model per epoch:
                    V_net = Σ[fitness_t×v_d] − Σ[c_comp+c_gov] − |E|×c_probe − c_rem×I{s2}

All Paper 3 token classes are re-exported unchanged.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# Re-export everything from Paper 3
from tokens_p3 import (
    SolutionP3, DecisionRecordP3,
    Problem, HumanAgent, AIAgent, Opportunity, TokenReservoir,
    OrgMemory, MemEntry, DecisionRecord,
    ProblemType, SolutionType, OpportunityState, DecisionMode, AllocPolicy,
)


class ProcessTopology(str, Enum):
    """
    BPMN process topology — guard condition on T_resolve (Rajendra 2026d, §3).

    FLAT          Paper 3 baseline.  T_resolve fires immediately after RAG.
                  No alignment audit gate.

    SEQ_GATEWAY   T_gateway fires first when ai_frac > θ_gateway OR
                  alignment_gap > θ_gap.  Catches misalignment with prob
                  λ_mis.  Dominant topology per Proposition 4.1.

    PAR_GATEWAY   Fast path (no audit) + slow path (audit) run in parallel.
                  DOMINATED by SEQ_GATEWAY because only the slow path audits,
                  giving λ_mis effective coverage of ~0.5×λ_mis.
    """
    FLAT         = "FLAT"
    SEQ_GATEWAY  = "SEQ_GATEWAY"
    PAR_GATEWAY  = "PAR_GATEWAY"


class RoleMode(str, Enum):
    """
    Four-mode human-AI spectrum (Rajendra 2026d §4; extended in 2026e §4).

    RPA              Human executes repeated endeavour.  HAV = 0.  AI can replace.
    ASSISTANT        Human reviews and judges AI outputs before resolution.
                     HAV > 0.  λ_mis is the critical variable.
    AUTONOMOUS       AI executes; human is SLA guardian (Mode 3 in Paper 5).
                     HAV critical, SRQ dominates.
    FULLY_AUTONOMOUS No human in operational loop.  HAV = 0 by absence.
                     Maximum governance risk.
    """
    RPA              = "RPA"
    ASSISTANT        = "ASSISTANT"
    AUTONOMOUS       = "AUTONOMOUS"
    FULLY_AUTONOMOUS = "FULLY_AUTONOMOUS"


@dataclass
class VNetRecord:
    """
    One epoch's observation of the V_net cost model.

    V_net (cumulative) =
        Σ_t[fitness_t × v_d]           — decision value stream
      − Σ_t[c_comp_t × n_ai_t]        — AI compute cost
      − Σ_t[c_gov_t × n_human_t]      — human governance cost
      − |E| × c_probe                  — probe injection cost
      − c_rem × I{stage_2}             — one-time stage-2 remediation

    IT:HR = Σ c_comp / Σ c_gov  (cumulative; effective zone 1.5–3.0)
    """
    epoch:              int
    fitness_t:          float   # macro_fitness at epoch boundary
    n_ai:               int
    n_human:            int
    v_d:                float   # per-decision value (token-equivalent)
    c_comp:             float   # compute cost per AI agent per epoch
    c_gov:              float   # governance cost per human agent per epoch
    c_probe_epoch:      float   # total probe cost this epoch (|E_t| × c_probe)
    c_rem_epoch:        float   # remediation cost this epoch (c_rem × I{s2_onset})
    value_epoch:        float   # fitness_t × v_d
    cost_epoch:         float   # c_comp×n_ai + c_gov×n_human + c_probe + c_rem
    vnet_epoch:         float   # value_epoch - cost_epoch
    vnet_cumulative:    float   # running cumulative V_net
    ithr_ratio:         float   # cumulative IT:HR = Σc_comp / Σc_gov
    topology:           ProcessTopology = ProcessTopology.FLAT
    role_mode:          RoleMode        = RoleMode.ASSISTANT
    gateway_fired:      int     = 0     # times SEQ_GATEWAY fired this epoch
    gateway_caught:     int     = 0     # times gateway caught misalignment
