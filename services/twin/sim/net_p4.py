"""
net_p4.py  —  Paper 4: Process Topology and the V_net Cost Model
================================================================
Subclasses MisalignedCPN with:

  _fire_resolve()     — topology guard condition on T_resolve
                        SEQ_GATEWAY: T_gateway fires when ai_frac > θ_gateway
                        OR alignment_gap > θ_gap.  Catches misalignment
                        with prob λ_mis; corrects alignment before resolution.
                        PAR_GATEWAY: fast+slow parallel paths; dominated by
                        SEQ_GATEWAY (Paper 4, Proposition 4.1).

  _epoch_boundary()   — V_net accumulation and IT:HR ratio computation.
                        V_net = Σ[fitness_t×v_d] − Σ[c_comp+c_gov]
                              − |E|×c_probe − c_rem×I{s2}

Papers 1–3 results are exactly reproducible from net_p3.py with
topology=FLAT.

Key Paper 4 findings implemented here:
  - SEQ_GATEWAY + ASSISTANT (λ_mis=0.8) = UNIFIED governance architecture
    with IT:HR 2.32 — best outcome.
  - IT_ONLY (no SEQ_GATEWAY, IT:HR 3.01) suffers LLM self-defeat.
  - HR_ONLY (IT:HR 1.48) has unsustainable governance cost.
  - Probe injection 8:1 return as insurance premium.
  - Third governance ontology: AI agents' problem is architectural mismatch,
    not opportunism (Coase/Williamson) or bounded rationality (March/Simon).
"""
from __future__ import annotations

import numpy as np
from typing import List, Dict, Optional, Tuple

from tokens_p3 import (
    Opportunity, OrgMemory, DecisionMode, OpportunityState,
    ProblemType, DecisionRecordP3,
)
from tokens_p4 import ProcessTopology, RoleMode, VNetRecord
from agents import AgentPool
from macro import MacroAgent
from landscape import NKLandscape
from net_p3 import MisalignedCPN


class TopologyCPN(MisalignedCPN):
    """
    Paper 4 extension.

    Extra params (all in params dict):
        topology          ProcessTopology   default FLAT
        role_mode         RoleMode          default ASSISTANT
        lambda_mis        float [0,1]       default 0.50
            Probability SEQ_GATEWAY catches misalignment when triggered.
        theta_gateway     float [0,1]       default 0.50
            AI fraction threshold that triggers SEQ_GATEWAY.
        theta_gap_gate    float             default 0.07
            Alignment gap threshold that triggers SEQ_GATEWAY.
        v_d               float             default 1000.0
            Per-decision token-equivalent value.
        c_comp            float             default 100.0
            Compute cost per AI agent per epoch.
        c_gov             float             default 66.0
            Governance cost per human agent per epoch.
            (IT:HR effective zone 1.5–3.0; c_comp/c_gov ≈ 1.5 at baseline)
        c_rem             float             default 5000.0
            One-time Stage-2 remediation cost.
    """

    def __init__(self, params: dict, rng: np.random.Generator,
                 landscape: NKLandscape, agent_pool: AgentPool,
                 macro: MacroAgent, org_memory: OrgMemory):
        super().__init__(params, rng, landscape, agent_pool, macro, org_memory)

        self.topology  = ProcessTopology(
            params.get('topology', ProcessTopology.FLAT))
        self.role_mode = RoleMode(
            params.get('role_mode', RoleMode.ASSISTANT))
        self.lambda_mis     = float(params.get('lambda_mis', 0.50))
        self.theta_gateway  = float(params.get('theta_gateway', 0.50))
        self.theta_gap_gate = float(params.get('theta_gap_gate', 0.07))

        # V_net cost model parameters
        self.v_d    = float(params.get('v_d', 1000.0))
        self.c_comp = float(params.get('c_comp', 100.0))
        self.c_gov  = float(params.get('c_gov', 66.0))
        self.c_rem  = float(params.get('c_rem', 5000.0))

        # V_net accumulators
        self._vnet_records:     List[VNetRecord] = []
        self._cum_value:        float = 0.0
        self._cum_comp_cost:    float = 0.0
        self._cum_gov_cost:     float = 0.0
        self._cum_probe_cost:   float = 0.0
        self._cum_rem_cost:     float = 0.0
        self._epoch_probes:     int   = 0   # probes injected this epoch
        self._total_probes:     int   = 0
        self._s2_remediated:    bool  = False

        # Gateway counters (epoch-level)
        self._gate_fired:   int = 0
        self._gate_caught:  int = 0

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _current_ai_frac(self) -> float:
        return self.pool.n_ai / max(1, self.pool.n_human + self.pool.n_ai)

    def _gateway_triggers(self, alignment_gap: float) -> bool:
        """True when SEQ_GATEWAY guard condition is met."""
        return (self._current_ai_frac() > self.theta_gateway
                or alignment_gap > self.theta_gap_gate)

    def _apply_seq_gateway(self, sols) -> Tuple[float, float, bool]:
        """
        Apply SEQ_GATEWAY alignment audit.

        Returns (corrected_quality, corrected_alignment, caught).
        If caught, alignment is partially corrected toward 1.0 (gateway
        intervention surfaces misalignment and requests human re-framing).
        PAR_GATEWAY modelled as SEQ_GATEWAY with λ_mis × 0.5 (only slow path
        audits; dominated per Proposition 4.1).
        """
        if not sols:
            return 0.5, 0.5, False

        bq = max(sols, key=lambda s: s.quality).quality
        ba = max(sols, key=lambda s: s.quality).alignment_score

        effective_lambda = self.lambda_mis
        if self.topology == ProcessTopology.PAR_GATEWAY:
            effective_lambda *= 0.5  # dominated by SEQ_GATEWAY

        caught = self.rng.random() < effective_lambda
        if caught:
            # Partial alignment correction: audit surfaces misalignment,
            # human re-frames → alignment moves 60% toward 1.0
            ba_corrected = ba + (1.0 - ba) * 0.60
            return bq, float(np.clip(ba_corrected, 0.0, 1.0)), True
        return bq, ba, False

    # ── Override: resolution ─────────────────────────────────────────────────

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

        # Compute baseline quality/alignment from Paper 3
        if sols:
            bq = max(sols, key=lambda s: s.quality).quality
            ba = max(sols, key=lambda s: s.quality).alignment_score
        else:
            bq, ba = 0.5, 0.5

        gap = bq - ba
        gateway_caught = False

        # Topology guard on T_resolve
        if self.topology in (ProcessTopology.SEQ_GATEWAY,
                             ProcessTopology.PAR_GATEWAY):
            if self._gateway_triggers(gap):
                self._gate_fired += 1
                bq, ba, gateway_caught = self._apply_seq_gateway(sols)
                if gateway_caught:
                    self._gate_caught += 1
                    gap = bq - ba

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
            alignment_gap=gap,
        )
        self._next_record_id += 1
        self._p3_sols.pop(opp.id, None)
        self._close_opportunity(opp, rec)

    # ── Probe tracking (override T_probe_entry) ──────────────────────────────

    def _T_probe_entry(self):
        self._epoch_probes += 1
        self._total_probes += 1
        super()._T_probe_entry()

    # ── Override: epoch boundary ─────────────────────────────────────────────

    def _epoch_boundary(self):
        super()._epoch_boundary()   # runs Paper 3 epoch logic, appends to self.results

        # V_net computation for this epoch
        latest     = self.results[-1] if self.results else {}
        fitness_t  = float(latest.get('macro_fitness', 0.0))
        n_ai       = int(latest.get('n_ai', self.pool.n_ai))
        n_human    = int(latest.get('n_human', self.pool.n_human))
        epoch_num  = int(latest.get('epoch', self._epoch_number))
        s2_onset   = bool(latest.get('stage2_active', 0)) and not self._s2_remediated

        c_probe_epoch  = self.p.get('c_probe', 100) * self._epoch_probes
        value_epoch    = fitness_t * self.v_d
        comp_epoch     = self.c_comp * n_ai
        gov_epoch      = self.c_gov  * n_human
        rem_epoch      = self._c_rem_this_epoch(s2_onset)

        cost_epoch  = comp_epoch + gov_epoch + c_probe_epoch + rem_epoch
        vnet_epoch  = value_epoch - cost_epoch

        self._cum_value     += value_epoch
        self._cum_comp_cost += comp_epoch
        self._cum_gov_cost  += gov_epoch
        self._cum_probe_cost += c_probe_epoch
        self._cum_rem_cost  += rem_epoch

        vnet_cum   = (self._cum_value
                      - self._cum_comp_cost
                      - self._cum_gov_cost
                      - self._cum_probe_cost
                      - self._cum_rem_cost)

        ithr = (self._cum_comp_cost / self._cum_gov_cost
                if self._cum_gov_cost > 0 else float('inf'))

        rec = VNetRecord(
            epoch=epoch_num,
            fitness_t=fitness_t,
            n_ai=n_ai,
            n_human=n_human,
            v_d=self.v_d,
            c_comp=self.c_comp,
            c_gov=self.c_gov,
            c_probe_epoch=c_probe_epoch,
            c_rem_epoch=rem_epoch,
            value_epoch=value_epoch,
            cost_epoch=cost_epoch,
            vnet_epoch=vnet_epoch,
            vnet_cumulative=vnet_cum,
            ithr_ratio=ithr,
            topology=self.topology,
            role_mode=self.role_mode,
            gateway_fired=self._gate_fired,
            gateway_caught=self._gate_caught,
        )
        self._vnet_records.append(rec)

        # Annotate the epoch result dict with V_net/IT:HR fields
        latest['vnet_epoch']       = round(vnet_epoch, 2)
        latest['vnet_cumulative']  = round(vnet_cum, 2)
        latest['ithr_ratio']       = round(ithr, 4) if ithr != float('inf') else None
        latest['gateway_fired']    = self._gate_fired
        latest['gateway_caught']   = self._gate_caught
        latest['topology']         = self.topology.value
        latest['role_mode']        = self.role_mode.value

        # Reset epoch-level counters
        self._epoch_probes = 0
        self._gate_fired   = 0
        self._gate_caught  = 0

    def _c_rem_this_epoch(self, s2_onset: bool) -> float:
        """One-time remediation cost on first Stage-2 onset."""
        if s2_onset and not self._s2_remediated:
            self._s2_remediated = True
            return self.c_rem
        return 0.0

    # ── V_net query helpers ───────────────────────────────────────────────────

    def vnet_summary(self) -> dict:
        """Current cumulative V_net summary for API response."""
        if not self._vnet_records:
            return {}
        latest = self._vnet_records[-1]
        ithr = latest.ithr_ratio
        ithr_zone = (
            "IT-dominant (>3.0)" if ithr is not None and ithr > 3.0
            else "HR-dominant (<1.5)" if ithr is not None and ithr < 1.5
            else "Effective (1.5–3.0)" if ithr is not None
            else "undefined"
        )
        return {
            "vnet_cumulative":    round(latest.vnet_cumulative, 2),
            "cum_value":          round(self._cum_value, 2),
            "cum_compute_cost":   round(self._cum_comp_cost, 2),
            "cum_gov_cost":       round(self._cum_gov_cost, 2),
            "cum_probe_cost":     round(self._cum_probe_cost, 2),
            "cum_remediation":    round(self._cum_rem_cost, 2),
            "ithr_ratio":         round(ithr, 4) if ithr else None,
            "ithr_zone":          ithr_zone,
            "total_probes":       self._total_probes,
            "topology":           self.topology.value,
            "role_mode":          self.role_mode.value,
            "epochs":             len(self._vnet_records),
        }
