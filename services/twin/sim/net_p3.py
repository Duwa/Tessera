"""
net_p3.py  —  Paper 3: Belief-Misalignment Pathology
=====================================================
Subclasses HybridCPN with targeted overrides:

  _attempt_coupling()      — graded AI assignment with misalignment probe
  _propose_solutions_p3()  — solutions carry independent quality + alignment
  _fire_resolve()          — fitness penalised by alignment gap
  _epoch_boundary()        — tracks quality/alignment series + stage-2 drift

Papers 1 & 2 results are exactly reproducible from net.py.
"""
from __future__ import annotations

import numpy as np
from typing import List, Dict, Optional, Tuple

from tokens import (
    Opportunity, OrgMemory, DecisionRecord,
    ProblemType, SolutionType, OpportunityState, DecisionMode,
)
from tokens_p3 import SolutionP3, DecisionRecordP3
from agents import AgentPool
from macro import MacroAgent
from landscape import NKLandscape
from net import HybridCPN, _FRAMING_DIMS


class MisalignedCPN(HybridCPN):
    """
    Paper 3 extension.

    Extra params (all in the params dict, with defaults):
        alignment_coupling_prob : float  [0,1]  default 0.45
        alignment_weight        : float  [0,1]  default 0.6
        template_drift_rate     : float         default 0.15
    """

    def __init__(self, params: dict, rng: np.random.Generator,
                 landscape: NKLandscape, agent_pool: AgentPool,
                 macro: MacroAgent, org_memory: OrgMemory):
        super().__init__(params, rng, landscape, agent_pool, macro, org_memory)
        self.acp = params.get('alignment_coupling_prob', 0.45)
        self.aw  = params.get('alignment_weight', 0.6)
        self.tdr = params.get('template_drift_rate', 0.15)

        self._p3_sols: Dict[int, List[SolutionP3]] = {}
        self._eq: List[float] = []
        self._ea: List[float] = []
        self._gap_history: List[float] = []
        self._s2_active = False
        self._s2_epoch: Optional[int] = None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _overlap(self, belief: np.ndarray,
                 framing: Tuple) -> float:
        N = len(belief)
        scores = []
        for pt in framing:
            dims = [d for d in _FRAMING_DIMS.get(pt, []) if d < N]
            scores.append(float(np.mean(belief[dims])) if dims
                          else float(np.mean(belief)))
        return float(np.mean(scores)) if scores else 0.5

    def _ai_overlap(self, belief: np.ndarray, framing: Tuple) -> float:
        """AI overlap compressed toward centre: broad but shallow."""
        return self._overlap(belief, framing) * 0.70 + 0.15

    # ── override: coupling ────────────────────────────────────────────────────

    def _attempt_coupling(self, opp_id: int):
        opp = self.opportunities_open.get(opp_id)
        if opp is None or opp.state != OpportunityState.OPEN:
            return

        coupled_any = False

        # Human coupling — domain threshold 0.40
        for abm in self.pool.available_humans:
            if abm.unique_id in opp.registered_humans:
                continue
            if (self._overlap(abm.token.belief, opp.framing) > 0.40
                    and abm.token.hcm_level >= opp.access_min_hcm):
                opp.registered_humans.append(abm.unique_id)
                coupled_any = True

        # AI coupling — misalignment assignment mechanism
        c_part = self.p['c_part']
        for abm in self.pool.available_ais:
            if abm.unique_id in opp.registered_ais:
                continue
            ov  = self._ai_overlap(abm.token.belief, opp.framing)
            mis = max(0.0, 0.65 - ov) / 0.65   # normalised misalignment [0,1]

            by_overlap = ov >= 0.55
            by_assign  = (not by_overlap
                          and self.rng.random() < self.acp * mis)

            if by_overlap or by_assign:
                if self.token_reservoir.debit(c_part):
                    opp.registered_ais.append(abm.unique_id)
                    abm.token.token_balance -= min(c_part,
                                                   abm.token.token_balance)
                    if not hasattr(abm, '_opp_ov'):
                        abm._opp_ov = {}
                    abm._opp_ov[opp_id] = ov
                    coupled_any = True

        if opp.quorum_met and opp.state == OpportunityState.OPEN:
            opp.state = OpportunityState.ACTIVE
            self.opportunities_open.pop(opp_id)
            self.opportunities_active[opp_id] = opp
            self._attach_problems(opp)
            self._propose_solutions_p3(opp)
            if opp.registered_ais:
                self._push(self.time + 1, 'attempt_rag', opp_id)
            else:
                self._push(self.time + 2, 'attempt_resolution', opp_id)
        elif coupled_any:
            self._push(self.time + 2, 'attempt_coupling', opp_id)

    # ── override: solution generation ────────────────────────────────────────

    def _propose_solutions_p3(self, opp: Opportunity):
        self._p3_sols[opp.id] = []

        for sol in self.solution_stream[:2]:
            ov = 0.5
            if opp.registered_humans:
                h = self.pool.humans.get(opp.registered_humans[0])
                if h:
                    ov = self._overlap(h.token.belief, opp.framing)
            p3 = SolutionP3(id=sol.id, domain=sol.domain,
                            quality=sol.quality, alignment_score=ov,
                            cost_tokens=0)
            self._p3_sols[opp.id].append(p3)
            opp.proposed_solutions.append((sol.id, -1))
        self.solution_stream = self.solution_stream[2:]

        c_gen = self.p.get('c_gen', 20)
        for ai_id in opp.registered_ais:
            abm = self.pool.ais.get(ai_id)
            if abm is None or not self.token_reservoir.debit(c_gen):
                continue
            ov = getattr(abm, '_opp_ov', {}).get(opp.id, 0.5)
            quality = float(self.rng.beta(3, 2))
            p3 = SolutionP3(id=self._next_solution_id,
                            domain=self.rng.choice(list(SolutionType)),
                            quality=quality, alignment_score=ov,
                            cost_tokens=c_gen)
            self._next_solution_id += 1
            self._p3_sols[opp.id].append(p3)
            opp.proposed_solutions.append((p3.id, ai_id))

    # ── override: resolution ─────────────────────────────────────────────────

    def _fire_resolve(self, opp: Opportunity):
        beliefs = []
        for h_id in opp.registered_humans:
            abm = self.pool.humans.get(h_id)
            if abm:
                beliefs.append(abm.token.belief.copy())
        for a_id in opp.registered_ais:
            abm = self.pool.ais.get(a_id)
            if abm:
                beliefs.append(abm.token.belief.copy())

        mean_b    = np.mean(beliefs, axis=0) if beliefs else self.macro.template
        agent_fit = self.landscape.fitness(mean_b)
        tmpl_fit  = self.landscape.fitness(self.macro.template)

        sols = self._p3_sols.get(opp.id, [])
        bq, ba = (max(sols, key=lambda s: s.quality).quality,
                  max(sols, key=lambda s: s.quality).alignment_score) \
                  if sols else (0.5, 0.5)

        base    = 0.6 * agent_fit + 0.4 * tmpl_fit
        af      = (1.0 - self.aw) + self.aw * ba
        fitness = float(np.clip(base * (0.5 + 0.5 * bq) * af, 0.0, 1.0))

        self._eq.append(bq)
        self._ea.append(ba)

        snap = (np.mean(beliefs, axis=0) if beliefs
                else self.macro.template.copy())
        rec = DecisionRecordP3(
            id=self._next_record_id,
            mode=DecisionMode.RESOLUTION,
            fitness_outcome=fitness,
            agent_mix=(len(opp.registered_humans), len(opp.registered_ais)),
            opportunity_id=opp.id,
            timestamp=self.time,
            belief_snapshot=snap,
            opportunity_framing=opp.framing,
            mean_solution_quality=bq,
            mean_alignment_score=ba,
            alignment_gap=bq - ba,
        )
        self._next_record_id += 1
        self._p3_sols.pop(opp.id, None)
        self._close_opportunity(opp, rec)

    def _make_record(self, opp, mode, fitness):
        beliefs = []
        for h_id in opp.registered_humans:
            abm = self.pool.humans.get(h_id)
            if abm:
                beliefs.append(abm.token.belief.copy())
        for a_id in opp.registered_ais:
            abm = self.pool.ais.get(a_id)
            if abm:
                beliefs.append(abm.token.belief.copy())
        snap = (np.mean(beliefs, axis=0) if beliefs
                else self.macro.template.copy())
        rec = DecisionRecordP3(
            id=self._next_record_id, mode=mode,
            fitness_outcome=fitness,
            agent_mix=(len(opp.registered_humans), len(opp.registered_ais)),
            opportunity_id=opp.id, timestamp=self.time,
            belief_snapshot=snap, opportunity_framing=opp.framing,
            mean_solution_quality=0.0, mean_alignment_score=0.0,
            alignment_gap=0.0,
        )
        self._next_record_id += 1
        return rec

    # ── override: epoch boundary ──────────────────────────────────────────────

    def _epoch_boundary(self):
        self._epoch_number += 1

        eq  = float(np.mean(self._eq)) if self._eq else 0.0
        ea  = float(np.mean(self._ea)) if self._ea else 0.0
        gap = eq - ea
        self._gap_history.append(gap)

        ai_frac    = self.pool.n_ai / max(1, self.pool.n_human + self.pool.n_ai)
        # Stage-2: sustained positive alignment gap (≥3 of last 4 epochs > 0.03)
        # Independent of AI fraction — the gap IS the signal
        gap_streak = sum(1 for g in self._gap_history[-4:] if g > 0.03)
        if not self._s2_active and gap_streak >= 3:
            self._s2_active = True
            self._s2_epoch  = self._epoch_number

        base_lr = self.macro.consolidation_lr
        if self._s2_active:
            self.macro.consolidation_lr = min(0.7, base_lr + self.tdr)

        epoch_stats = self.macro.consolidate(
            records=self._epoch_records,
            org_memory=self.org_memory,
            current_time=self.time,
        )

        if self._s2_active:
            self.macro.consolidation_lr = base_lr

        obs_dep = self.macro.all_reference_rates(self._epoch_beliefs)
        self.pool.apply_epoch_updates()

        t, t0 = self.macro.template, self.macro.initial_template
        norm  = np.linalg.norm(t) * np.linalg.norm(t0)
        drift = float(1.0 - np.dot(t, t0) / norm) if norm > 1e-9 else 0.0

        self.results.append({
            'epoch':                 self._epoch_number,
            'time':                  self.time,
            'n_human':               self.pool.n_human,
            'n_ai':                  self.pool.n_ai,
            'token_balance':         self.token_reservoir.balance,
            'macro_fitness':         self.macro.current_fitness,
            'template_revised':      self.macro.template_revised_this_epoch(),
            'mean_solution_quality': eq,
            'mean_alignment_score':  ea,
            'alignment_gap':         gap,
            'stage2_active':         int(self._s2_active),
            'stage2_onset_epoch':    self._s2_epoch if self._s2_epoch else -1,
            'template_drift':        drift,
            'ai_fraction':           ai_frac,
            **epoch_stats,
            **{f'explore_{k}': v for k, v in obs_dep.items()},
        })

        self._epoch_records = []
        self._epoch_beliefs = []
        self._eq = []
        self._ea = []
        self._correlated_failure_count_epoch = 0
