"""
net_p5.py  —  Paper 5: Human Alignment Value (HAVCPN)
=====================================================
Subclasses TopologyCPN with:

  _fire_resolve()        — tracks per-contribution HAV signals
  _apply_measurement()   — measurement regime at every epoch boundary
  _epoch_boundary()      — HAV composite per human; Track 2 nudge in MAN_HOURS;
                           HAV crossover theorem detection

Core finding (Rajendra 2026e, Theorem 4.1 — HAV Crossover):
  F_HAV(φ) > F_MH(φ) for all φ > φ*
  φ*(K=6) = 0.25,  φ*(K=4) = 0.32,  φ*(K≤2) = 0.44

  Man-hours governance above φ* ACTIVELY ACCELERATES Track 2 pathology
  via Track 2 nudge: belief shift toward AI centroid at rate (1-NPF)×φ.
  HAV governance prevents this by rewarding Mode 3 governance activity.

Compensation extension (Paper 5, §8.1):
  Total Compensation = Salary + Token Budget + Alignment Premium
  AP(h,T) = r_AP × HAV(h,T) × Salary
  r_AP = 0.05 at φ < 0.25;  0.25 at φ > 0.75  (interpolated)

Papers 1–4 results reproducible from net_p4.py with regime=HAV.
"""
from __future__ import annotations

import numpy as np
from typing import List, Dict, Optional, Tuple

from tokens_p3 import (
    Opportunity, OrgMemory, DecisionMode, OpportunityState, DecisionRecordP3,
)
from tokens_p4 import ProcessTopology, RoleMode
from tokens_p5 import MeasurementRegime, HumanContributionToken, HAVRecord
from agents import AgentPool
from macro import MacroAgent
from landscape import NKLandscape
from net_p4 import TopologyCPN


# HAV composite weights (Rajendra 2026e, §3.4)
_W1 = 0.50   # NPF weight
_W2 = 0.30   # SRQ weight
_W3 = 0.20   # OC weight

# HAV crossover thresholds (Theorem 4.1, empirically identified)
# Rises monotonically with landscape ruggedness K
def _phi_star(K: int) -> float:
    if K <= 2:
        return 0.44
    if K == 4:
        return 0.32
    return 0.25   # K >= 6


def _alignment_premium_rate(phi: float) -> float:
    """r_AP rises with φ — correct signal: more alignment contribution rewarded
    when AI agents handle more execution (Paper 5, §8.1)."""
    if phi < 0.25:
        return 0.05
    if phi > 0.75:
        return 0.25
    return 0.05 + (phi - 0.25) / 0.50 * 0.20   # linear interpolation


class HAVCPN(TopologyCPN):
    """
    Paper 5 extension — full 5-paper CPN stack.

    Extra params (all in params dict):
        measurement_regime  MeasurementRegime   default HAV
        theta_novel         float [0,1]         default 0.30
            Origination Capacity novelty threshold.  A human contribution
            counts as OC when their belief-org_memory overlap < θ_novel
            (below this = framing novel enough to exceed the indexed corpus).
        org_type            str ('profit'|'nonprofit')  default 'profit'
            Profit orgs have market correction mechanism; nonprofits don't.
            φ*_nonprofit < φ*_profit: nonprofits reach crossover sooner.
    """

    def __init__(self, params: dict, rng: np.random.Generator,
                 landscape: NKLandscape, agent_pool: AgentPool,
                 macro: MacroAgent, org_memory: OrgMemory):
        super().__init__(params, rng, landscape, agent_pool, macro, org_memory)

        self.regime      = MeasurementRegime(
            params.get('measurement_regime', MeasurementRegime.HAV))
        self.theta_novel = float(params.get('theta_novel', 0.30))
        self.org_type    = str(params.get('org_type', 'profit'))

        self._phi_star   = _phi_star(params.get('K', 4))
        # Nonprofits reach crossover sooner: lower effective φ*
        if self.org_type == 'nonprofit':
            self._phi_star = max(0.10, self._phi_star * 0.70)

        # Per-human contribution buckets (reset each epoch)
        self._h_contributions: Dict[int, List[HumanContributionToken]] = {}
        self._slo_breach_epoch = False   # did a fitness < 0.4 event occur?
        self._slo_breach_delta: Dict[int, float] = {}   # human_id → recovery delta

        self._hav_records: List[HAVRecord] = []    # historical (all epochs)
        self._hav_epoch:   List[HAVRecord] = []    # this epoch only
        self._phi_history: List[float] = []
        self._track2_nudge_active = False

    # ── Contribution tracking ────────────────────────────────────────────────

    def _record_human_contribution(self, opp: Opportunity, fitness: float,
                                   framing_novel: bool):
        """Record one contribution per participating human."""
        prev_fitness = (self.results[-1]['macro_fitness']
                        if self.results else 0.5)
        is_recovery  = fitness < 0.4   # SLO breach recovery
        delta        = max(0.0, fitness - prev_fitness)

        for h_id in opp.registered_humans:
            abm = self.pool.humans.get(h_id)
            if abm is None:
                continue
            # π(r) = 0 (non-procedural) when:
            #   • The human's belief diverges from AI centroid (external ref capacity), or
            #   • The opportunity required SLO recovery, or
            #   • The framing is novel (OC)
            ai_beliefs = [a.token.belief for a in self.pool.ais.values()]
            if ai_beliefs:
                ai_centroid = np.mean(ai_beliefs, axis=0)
                divergence = float(np.linalg.norm(
                    abm.token.belief - ai_centroid)) / (len(ai_centroid) ** 0.5)
            else:
                divergence = 1.0

            is_non_proc = (divergence > 0.15 or is_recovery or framing_novel)
            pi_ind = 0 if is_non_proc else 1

            token = HumanContributionToken(
                human_id=h_id,
                opportunity_id=opp.id,
                pi_indicator=pi_ind,
                fitness_delta=delta if is_recovery else 0.0,
                is_recovery=is_recovery,
                is_origination=framing_novel,
            )
            self._h_contributions.setdefault(h_id, []).append(token)
            if is_recovery:
                self._slo_breach_epoch = True
                self._slo_breach_delta[h_id] = (
                    self._slo_breach_delta.get(h_id, 0.0) + delta)

    def _framing_novelty(self, opp: Opportunity) -> bool:
        """True when human belief-org_memory overlap is below θ_novel."""
        if not self.org_memory.entries:
            return True
        human_beliefs = []
        for h_id in opp.registered_humans:
            abm = self.pool.humans.get(h_id)
            if abm:
                human_beliefs.append(abm.token.belief)
        if not human_beliefs:
            return False
        mean_h = np.mean(human_beliefs, axis=0)
        # Compare to macro template (proxy for indexed org memory)
        t = self.macro.template
        norm = np.linalg.norm(mean_h) * np.linalg.norm(t)
        if norm < 1e-9:
            return True
        overlap = float(np.dot(mean_h, t) / norm)
        return overlap < self.theta_novel

    # ── Override: resolution (adds contribution tracking) ────────────────────

    def _fire_resolve(self, opp: Opportunity):
        framing_novel = self._framing_novelty(opp)
        super()._fire_resolve(opp)   # Paper 4 topology logic
        latest_fitness = self._eq[-1] if self._eq else 0.5
        self._record_human_contribution(opp, latest_fitness, framing_novel)

    # ── HAV computation ──────────────────────────────────────────────────────

    def _compute_hav(self, h_id: int, phi: float) -> HAVRecord:
        """Compute HAV composite for human h_id over this epoch."""
        abm = self.pool.humans.get(h_id)
        mode = self.role_mode

        contribs = self._h_contributions.get(h_id, [])
        n_total  = len(contribs)

        if n_total == 0:
            # Human was present but had no contributions this epoch
            # In AUTONOMOUS mode, SLA guardians may have zero events — still count
            if mode == RoleMode.AUTONOMOUS:
                # Mode 3: standing availability for SLO recovery is itself HAV
                npf = 0.80   # default SLA guardian NPF
            elif mode == RoleMode.FULLY_AUTONOMOUS:
                npf = 0.0
            else:
                npf = 0.30   # ASSISTANT default when no contributions
            srq, oc = 0.0, 0.0
        else:
            n_non_proc  = sum(1 for c in contribs if c.pi_indicator == 0)
            npf         = n_non_proc / n_total

            # SRQ: total fitness recovery delta (normalized to [0,1])
            srq = float(np.clip(
                self._slo_breach_delta.get(h_id, 0.0), 0.0, 1.0))

            # OC: fraction of contributions that are novel originations
            n_orig = sum(1 for c in contribs if c.is_origination)
            oc     = n_orig / n_total

        hav = float(np.clip(_W1 * npf + _W2 * srq + _W3 * oc, 0.0, 1.0))
        r_ap = _alignment_premium_rate(phi)
        ap   = r_ap * hav   # salary-normalized (salary = 1.0)

        return HAVRecord(
            human_id=h_id,
            epoch=self._epoch_number,
            n_contributions=n_total,
            n_non_procedural=sum(1 for c in contribs if c.pi_indicator == 0),
            npf=round(npf, 4),
            srq=round(srq, 4),
            oc=round(oc, 4),
            hav_composite=round(hav, 4),
            alignment_premium_rate=round(r_ap, 4),
            alignment_premium=round(ap, 4),
            mode=mode,
        )

    # ── Track 2 nudge (MAN_HOURS regime) ─────────────────────────────────────

    def _apply_track2_nudge(self, phi: float):
        """
        MAN_HOURS measurement regime above φ*.

        Shifts each human agent's belief toward the AI centroid at rate
        (1 − NPF) × φ per epoch.  This is the computational grounding of
        Theorem 4.1: man-hours governance does not merely fail to prevent
        Track 2 — it actively accelerates belief domain convergence by
        rewarding Mode 2 consumption behaviour.
        """
        ai_beliefs = [a.token.belief.copy() for a in self.pool.ais.values()]
        if not ai_beliefs:
            return
        ai_centroid = np.mean(ai_beliefs, axis=0)

        for h_id, abm in self.pool.humans.items():
            hav_rec = next(
                (r for r in self._hav_epoch if r.human_id == h_id), None)
            npf = hav_rec.npf if hav_rec else 0.30
            nudge_rate = (1.0 - npf) * phi
            abm.token.belief = np.clip(
                (1.0 - nudge_rate) * abm.token.belief
                + nudge_rate * ai_centroid,
                0.0, 1.0,
            )

    # ── HAV replacement (HAV regime) vs MAN_HOURS replacement ───────────────

    def _hav_replacement_check(self):
        """
        HAV regime: replace humans whose HAV composite falls below θ_replace_hav.
        Does NOT apply Track 2 nudge.

        Under MAN_HOURS, T_replace fires on hours-weighted contribution.
        Under HAV, T_replace fires on HAV composite.

        The key structural difference: HAV-governed replacement never replaces
        SLA guardians (Mode 3) based on low event count — they are protected
        by their standing SRQ capacity.
        """
        theta = self.p.get('theta_replace_hav', 0.10)   # HAV-based threshold
        to_deactivate = []
        for h_id, abm in list(self.pool.humans.items()):
            hav_rec = next(
                (r for r in self._hav_epoch if r.human_id == h_id), None)
            if hav_rec is None:
                continue
            # Protect Mode 3 SLA guardians from low-event replacement
            if hav_rec.mode == RoleMode.AUTONOMOUS:
                continue
            if hav_rec.hav_composite < theta:
                to_deactivate.append(abm)

        for abm in to_deactivate[:1]:   # gradual: at most 1 per epoch
            self.pool.deactivate_human(abm)

    # ── Override: epoch boundary ─────────────────────────────────────────────

    def _epoch_boundary(self):
        super()._epoch_boundary()   # Paper 4 logic + V_net

        phi = self._current_ai_frac()
        self._phi_history.append(phi)
        phi_star = self._phi_star

        # Compute HAV for every active human
        self._hav_epoch = []
        for h_id in list(self.pool.humans.keys()):
            rec = self._compute_hav(h_id, phi)
            self._hav_epoch.append(rec)
            self._hav_records.append(rec)

        mean_hav = (float(np.mean([r.hav_composite for r in self._hav_epoch]))
                    if self._hav_epoch else 0.0)
        mean_npf = (float(np.mean([r.npf for r in self._hav_epoch]))
                    if self._hav_epoch else 0.0)
        mean_srq = (float(np.mean([r.srq for r in self._hav_epoch]))
                    if self._hav_epoch else 0.0)
        mean_oc  = (float(np.mean([r.oc  for r in self._hav_epoch]))
                    if self._hav_epoch else 0.0)
        total_ap = (sum(r.alignment_premium for r in self._hav_epoch)
                    if self._hav_epoch else 0.0)

        crossover_alert = phi > phi_star
        nudge_active    = False

        # Apply measurement regime
        if self.regime == MeasurementRegime.MAN_HOURS and phi > phi_star:
            self._apply_track2_nudge(phi)
            self._track2_nudge_active = True
            nudge_active = True
        elif self.regime == MeasurementRegime.HAV:
            # HAV replacement check protects SLA guardians
            self._hav_replacement_check()
            self._track2_nudge_active = False

        # Annotate the epoch result dict with HAV fields
        if self.results:
            latest = self.results[-1]
            latest['measurement_regime']    = self.regime.value
            latest['phi']                   = round(phi, 4)
            latest['phi_star']              = round(phi_star, 4)
            latest['crossover_alert']       = crossover_alert
            latest['track2_nudge_active']   = nudge_active
            latest['mean_hav']              = round(mean_hav, 4)
            latest['mean_npf']              = round(mean_npf, 4)
            latest['mean_srq']              = round(mean_srq, 4)
            latest['mean_oc']               = round(mean_oc, 4)
            latest['total_alignment_premium'] = round(total_ap, 4)
            latest['org_type']              = self.org_type
            latest['n_hav_records_epoch']   = len(self._hav_epoch)

        # Reset epoch buckets
        self._h_contributions    = {}
        self._slo_breach_epoch   = False
        self._slo_breach_delta   = {}
        self._hav_epoch          = []

    # ── HAV query helpers ─────────────────────────────────────────────────────

    def hav_dashboard(self) -> dict:
        """HAV summary for API response."""
        phi = self._current_ai_frac()
        phi_star = self._phi_star

        by_epoch: Dict[int, List[HAVRecord]] = {}
        for r in self._hav_records:
            by_epoch.setdefault(r.epoch, []).append(r)

        epoch_summaries = []
        for ep, recs in sorted(by_epoch.items()):
            epoch_summaries.append({
                'epoch':        ep,
                'n_humans':     len(recs),
                'mean_hav':     round(float(np.mean([r.hav_composite for r in recs])), 4),
                'mean_npf':     round(float(np.mean([r.npf for r in recs])), 4),
                'mean_srq':     round(float(np.mean([r.srq for r in recs])), 4),
                'mean_oc':      round(float(np.mean([r.oc  for r in recs])), 4),
                'total_ap':     round(sum(r.alignment_premium for r in recs), 4),
            })

        return {
            'measurement_regime':    self.regime.value,
            'org_type':              self.org_type,
            'phi_current':           round(phi, 4),
            'phi_star':              round(phi_star, 4),
            'crossover_alert':       phi > phi_star,
            'track2_nudge_active':   self._track2_nudge_active,
            'r_ap_current':          round(_alignment_premium_rate(phi), 4),
            'total_hav_records':     len(self._hav_records),
            'epoch_summaries':       epoch_summaries[-20:],   # last 20 epochs
            'crossover_interpretation': (
                "MAN_HOURS governance above φ* ACTIVELY INCENTIVISES Track 2 "
                "via belief nudge. Switch to HAV measurement immediately."
                if phi > phi_star and self.regime == MeasurementRegime.MAN_HOURS
                else "HAV governance active — alignment premium correctly incentivises "
                "Mode 3 SLA guardianship and origination."
                if phi > phi_star
                else f"φ={phi:.2f} below φ*={phi_star:.2f}. "
                "Both measurement regimes equivalent. Monitor as φ rises."
            ),
        }
