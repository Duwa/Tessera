"""
net.py  (calibrated v2)
-----------------------
Extended Colored Petri Net for Paper 2 — all fifteen transitions.

Calibration fixes applied (v2):
  1. T_token_exhaustion guard: fires when reservoir critical AND AI-majority AND
     no human quorum, not when individual agent balances hit zero.
  2. Correlated failure injection: stochastic per-tick signal to AI agents
     sharing a model_version, controlled by corr_failure_rate parameter.
  3. Resolution fitness: computed from mean agent belief evaluated on NK
     landscape, not from macro template alone — composition now shapes fitness.
  4. Belief-framing overlap: sensitive to opportunity framing type via a
     per-agent specialisation score.
  5. Gradient-step suppressed in consolidation when agent belief provides
     a richer signal — allows composition-driven template divergence.
"""
from __future__ import annotations

import heapq
import numpy as np
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field

from tokens import (
    Problem, Solution, HumanAgent, AIAgent, Opportunity, TokenReservoir,
    OrgMemory, DecisionRecord, MemEntry,
    ProblemType, SolutionType, OpportunityState, DecisionMode, AllocPolicy
)
from agents import AgentPool, HumanAgentABM, AIAgentABM
from macro import MacroAgent
from landscape import NKLandscape


# ── Event ─────────────────────────────────────────────────────────────────────

@dataclass(order=True)
class Event:
    time: int
    kind: str = field(compare=False)
    payload: Any = field(compare=False, default=None)


# ── Framing specialisation map ────────────────────────────────────────────────
# Maps ProblemType → which belief dimensions are "active" for that framing.
# Used by _belief_overlaps_framing to make participation type-sensitive.
_FRAMING_DIMS: Dict[ProblemType, List[int]] = {
    ProblemType.OPERATIONAL: [0, 1, 2, 3],
    ProblemType.STRATEGIC:   [4, 5, 6, 7],
    ProblemType.RELATIONAL:  [8, 9, 10, 11],
}


class HybridCPN:
    """Extended CPN — calibrated v2."""

    def __init__(self, params: dict, rng: np.random.Generator,
                 landscape: NKLandscape, agent_pool: AgentPool,
                 macro: MacroAgent, org_memory: OrgMemory):

        self.p = params
        self.rng = rng
        self.landscape = landscape
        self.pool = agent_pool
        self.macro = macro
        self.org_memory = org_memory
        self.time: int = 0

        # ── Places ────────────────────────────────────────────────────────────
        self.problem_stream: List[Problem] = []
        self.solution_stream: List[Solution] = []
        self.opportunities_open: Dict[int, Opportunity] = {}
        self.opportunities_active: Dict[int, Opportunity] = {}
        self.token_reservoir = TokenReservoir(
            balance=params['budget_ceiling'],
            replenish_rate=params['r_replenish'],
            budget_ceiling=params['budget_ceiling'],
            alloc_policy=AllocPolicy.UNIFORM
        )
        self.decision_output: List[DecisionRecord] = []

        # ── Counters and logs ─────────────────────────────────────────────────
        self._next_problem_id = 0
        self._next_solution_id = 0
        self._next_opp_id = 0
        self._next_record_id = 0
        self._correlated_failure_count_epoch: int = 0
        self._epoch_records: List[DecisionRecord] = []
        self._epoch_beliefs: List[np.ndarray] = []
        self._epoch_number: int = 0
        self.results: List[dict] = []
        self.decision_log: List[dict] = []

        # ── Event queue ───────────────────────────────────────────────────────
        self._queue: List[Event] = []
        self._schedule_initial_events()

    # ── Scheduling ────────────────────────────────────────────────────────────

    def _push(self, time: int, kind: str, payload=None):
        heapq.heappush(self._queue, Event(time, kind, payload))

    def _schedule_initial_events(self):
        p = self.p
        self._push(self._next_arrival(p['lambda_P']), 'gen_problem')
        self._push(self._next_arrival(p['lambda_S']), 'gen_solution')
        self._push(self.rng.integers(1, 5), 'open_opportunity')
        self._push(p['tau_epoch'], 'epoch_boundary')
        self._push(p['tau_probe'], 'probe_entry')
        self._push(p['tau_epoch'], 'replenish_tokens')
        # Periodic correlated failure signal for AI agents
        self._push(p.get('corr_failure_interval', 40), 'corr_failure_signal')

    def _next_arrival(self, rate: float) -> int:
        return self.time + max(1, int(self.rng.exponential(1.0 / rate)))

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self, max_time: int = 300) -> List[dict]:
        while self._queue and self.time <= max_time:
            event = heapq.heappop(self._queue)
            self.time = event.time
            if self.time > max_time:
                break
            self._dispatch(event)
            self.pool.step()
            self._check_deadlines()
            self._check_mutation_triggers()
        return self.results

    def _dispatch(self, event: Event):
        k = event.kind
        if k == 'gen_problem':
            self._T_gen_P()
            self._push(self._next_arrival(self.p['lambda_P']), 'gen_problem')
        elif k == 'gen_solution':
            self._T_gen_S()
            self._push(self._next_arrival(self.p['lambda_S']), 'gen_solution')
        elif k == 'open_opportunity':
            self._T_open_opportunity()
            self._push(self.time + max(1, int(self.rng.exponential(3.0))),
                       'open_opportunity')
        elif k == 'epoch_boundary':
            self._epoch_boundary()
            self._push(self.time + self.p['tau_epoch'], 'epoch_boundary')
        elif k == 'replenish_tokens':
            self._T_replenish()
            self._push(self.time + self.p['tau_epoch'], 'replenish_tokens')
        elif k == 'probe_entry':
            self._T_probe_entry()
            self._push(self.time + self.p['tau_probe'], 'probe_entry')
        elif k == 'corr_failure_signal':
            self._inject_correlated_failure_signal()
            interval = self.p.get('corr_failure_interval', 40)
            self._push(self.time + interval, 'corr_failure_signal')
        elif k == 'attempt_coupling':
            opp_id = event.payload
            if opp_id in self.opportunities_open:
                self._attempt_coupling(opp_id)
        elif k == 'attempt_rag':
            opp_id = event.payload
            if opp_id in self.opportunities_active:
                self._T_RAG(opp_id)
            self._push(self.time + 2, 'attempt_resolution', opp_id)
        elif k == 'attempt_resolution':
            opp_id = event.payload
            if opp_id in self.opportunities_active:
                self._attempt_resolution(opp_id)

    # ── T_gen_P ───────────────────────────────────────────────────────────────

    def _T_gen_P(self):
        ptype = self.rng.choice(list(ProblemType))
        self.problem_stream.append(Problem(
            id=self._next_problem_id, type=ptype,
            urgency=float(self.rng.uniform(0, 1))
        ))
        self._next_problem_id += 1

    # ── T_gen_S ───────────────────────────────────────────────────────────────

    def _T_gen_S(self):
        self.solution_stream.append(Solution(
            id=self._next_solution_id,
            domain=self.rng.choice(list(SolutionType)),
            quality=float(self.rng.beta(2, 2)),
            cost_tokens=0
        ))
        self._next_solution_id += 1

    # ── T_open_opportunity ────────────────────────────────────────────────────

    def _T_open_opportunity(self):
        n_types = self.rng.choice([1, 2])
        framing = tuple(self.rng.choice(list(ProblemType), size=n_types, replace=False))
        deadline = self.time + int(self.rng.integers(
            self.p['deadline_min'], self.p['deadline_max']))
        opp = Opportunity(
            id=self._next_opp_id, framing=framing,
            access_min_hcm=float(self.rng.uniform(0.0, 0.3)),
            deadline=deadline, open_time=self.time
        )
        self._next_opp_id += 1
        self.opportunities_open[opp.id] = opp
        self._push(self.time + 1, 'attempt_coupling', opp.id)

    # ── T_couple_H / T_couple_A ───────────────────────────────────────────────

    def _attempt_coupling(self, opp_id: int):
        opp = self.opportunities_open.get(opp_id)
        if opp is None or opp.state != OpportunityState.OPEN:
            return

        coupled_any = False

        for abm in self.pool.available_humans:
            if abm.unique_id in opp.registered_humans:
                continue
            if (self._belief_overlaps_framing(abm.token.belief, opp.framing, 'human')
                    and abm.token.hcm_level >= opp.access_min_hcm):
                opp.registered_humans.append(abm.unique_id)
                coupled_any = True

        c_part = self.p['c_part']
        for abm in self.pool.available_ais:
            if abm.unique_id in opp.registered_ais:
                continue
            if (self._belief_overlaps_framing(abm.token.belief, opp.framing, 'ai')
                    and self.token_reservoir.debit(c_part)):
                opp.registered_ais.append(abm.unique_id)
                abm.token.token_balance -= min(c_part, abm.token.token_balance)
                coupled_any = True

        if opp.quorum_met and opp.state == OpportunityState.OPEN:
            opp.state = OpportunityState.ACTIVE
            self.opportunities_open.pop(opp_id)
            self.opportunities_active[opp_id] = opp
            self._attach_problems(opp)
            self._propose_solutions(opp)
            if opp.registered_ais:
                self._push(self.time + 1, 'attempt_rag', opp_id)
            else:
                self._push(self.time + 2, 'attempt_resolution', opp_id)
        elif coupled_any:
            self._push(self.time + 2, 'attempt_coupling', opp_id)

    def _belief_overlaps_framing(self, belief: np.ndarray,
                                  framing: Tuple[ProblemType, ...],
                                  agent_type: str = 'human') -> bool:
        """
        FIX 4: Framing-sensitive overlap.
        Checks activation in the specific belief dimensions associated with
        the opportunity's problem types. AI agents have wider coverage (dense
        embedding) but less domain depth than specialised human agents.
        """
        N = len(belief)
        scores = []
        for pt in framing:
            dims = [d for d in _FRAMING_DIMS.get(pt, []) if d < N]
            if not dims:
                scores.append(float(np.mean(belief)))
                continue
            domain_activation = float(np.mean(belief[dims]))
            if agent_type == 'ai':
                # AI agents: broad but shallow — score is mean activation, slightly penalised
                scores.append(max(0.15, domain_activation * 0.85 + 0.1))
            else:
                # Human agents: deeper in their specialised domains
                scores.append(domain_activation)
        return float(np.mean(scores)) > 0.25

    def _attach_problems(self, opp: Opportunity):
        remaining = []
        for prob in self.problem_stream:
            if prob.type in opp.framing and len(opp.attached_problems) < 3:
                opp.attached_problems.append(prob.id)
            else:
                remaining.append(prob)
        self.problem_stream = remaining

    def _propose_solutions(self, opp: Opportunity):
        for sol in self.solution_stream[:2]:
            opp.proposed_solutions.append((sol.id, -1))
        self.solution_stream = self.solution_stream[2:]

        c_gen = self.p.get('c_gen', 20)
        for ai_id in opp.registered_ais:
            abm = self.pool.ais.get(ai_id)
            if abm and self.token_reservoir.debit(c_gen):
                sol = Solution(
                    id=self._next_solution_id,
                    domain=self.rng.choice(list(SolutionType)),
                    quality=float(self.rng.beta(3, 2)),
                    cost_tokens=c_gen
                )
                self._next_solution_id += 1
                opp.proposed_solutions.append((sol.id, ai_id))

    # ── T_RAG ─────────────────────────────────────────────────────────────────

    def _T_RAG(self, opp_id: int):
        opp = self.opportunities_active.get(opp_id)
        if not opp:
            return
        c_rag = self.p['c_rag']
        eps_rag = self.p.get('eps_rag', 0.3)
        for ai_id in opp.registered_ais:
            abm = self.pool.ais.get(ai_id)
            if abm is None:
                continue
            depth = abm.token.rag_depth
            total_cost = c_rag * depth
            if (self.token_reservoir.debit(total_cost)
                    and abm.belief_uncertainty() > eps_rag):
                retrieved = self.org_memory.retrieve(
                    query_belief=abm.token.belief,
                    framing=opp.framing,
                    depth=depth,
                    current_time=self.time
                )
                abm.apply_rag_update(retrieved, self.time)

    # ── Correlated failure injection (FIX 2) ──────────────────────────────────

    def _inject_correlated_failure_signal(self):
        """
        FIX 2: Stochastic correlated failure signal.
        With probability corr_failure_rate, flag all AI agents sharing the
        dominant model_version — simulating a shared model/retrieval failure.
        Only fires if there are AI agents in active opportunities.
        """
        rate = self.p.get('corr_failure_rate', 0.15)
        if self.rng.random() > rate:
            return
        # Identify dominant model version
        if not self.pool.ais:
            return
        versions = [abm.token.model_version for abm in self.pool.available_ais]
        if not versions:
            return
        dominant = max(set(versions), key=versions.count)
        flagged_count = 0
        for abm in self.pool.available_ais:
            if abm.token.model_version == dominant:
                abm.token.failure_flag = True
                flagged_count += 1
        if flagged_count > 0:
            self._correlated_failure_count_epoch += flagged_count

    # ── Resolution transitions ────────────────────────────────────────────────

    def _attempt_resolution(self, opp_id: int):
        opp = self.opportunities_active.get(opp_id)
        if not opp or opp.state != OpportunityState.ACTIVE:
            return

        # T_resolve
        if (opp.quorum_met and opp.attached_problems
                and opp.proposed_solutions and self.time <= opp.deadline):
            self._fire_resolve(opp)
            return

        # T_token_exhaustion: reservoir cannot cover another round of AI
        # participation AND opportunity is AI-majority AND no human quorum.
        # Threshold = c_part * n_ai_participants (cost of re-coupling all AIs)
        n_ai_in_opp = len(opp.registered_ais)
        ai_majority = n_ai_in_opp > len(opp.registered_humans)
        exhaustion_threshold = self.p['c_part'] * max(1, n_ai_in_opp)
        reservoir_critical = self.token_reservoir.balance < exhaustion_threshold
        if ai_majority and reservoir_critical and not opp.human_quorum:
            self._fire_token_exhaustion(opp)
            return

        # T_correlated_failure
        flagged = [ai_id for ai_id in opp.registered_ais
                   if ai_id in self.pool.ais
                   and self.pool.ais[ai_id].token.failure_flag]
        if len(flagged) >= self.p['xi_trust']:
            self._fire_correlated_failure(opp, flagged)
            return

        # T_oversight
        time_open = self.time - opp.open_time
        if (opp.quorum_met and not opp.attached_problems
                and time_open > self.p['theta_oversight']
                and self.time <= opp.deadline):
            self._fire_oversight(opp)
            return

        # T_flight
        if (self.problem_stream and self.problem_stream[0].urgency > 0.8
                and opp.attached_problems):
            self._fire_flight(opp)
            return

        # Dissolution
        if self.time > opp.deadline:
            self._fire_dissolution(opp)
            return

        self._push(self.time + 3, 'attempt_resolution', opp_id)

    # ── Record construction ───────────────────────────────────────────────────

    def _make_record(self, opp: Opportunity, mode: DecisionMode,
                     fitness: float) -> DecisionRecord:
        beliefs = []
        for h_id in opp.registered_humans:
            abm = self.pool.humans.get(h_id)
            if abm:
                beliefs.append(abm.token.belief.copy())
        for a_id in opp.registered_ais:
            abm = self.pool.ais.get(a_id)
            if abm:
                beliefs.append(abm.token.belief.copy())
        snap = np.mean(beliefs, axis=0) if beliefs else self.macro.template.copy()

        rec = DecisionRecord(
            id=self._next_record_id, mode=mode,
            fitness_outcome=fitness,
            agent_mix=(len(opp.registered_humans), len(opp.registered_ais)),
            opportunity_id=opp.id, timestamp=self.time,
            belief_snapshot=snap,
            opportunity_framing=opp.framing
        )
        self._next_record_id += 1
        return rec

    def _close_opportunity(self, opp: Opportunity, record: DecisionRecord):
        opp.state = OpportunityState.CLOSED
        self.opportunities_active.pop(opp.id, None)
        self.decision_output.append(record)
        self._epoch_records.append(record)
        self._epoch_beliefs.append(self.macro.template.copy())

        fitness = record.fitness_outcome
        for h_id in opp.registered_humans:
            abm = self.pool.humans.get(h_id)
            if abm:
                abm.record_outcome(fitness)
        correlated = (record.mode == DecisionMode.CORRELATED_FAILURE)
        for a_id in opp.registered_ais:
            abm = self.pool.ais.get(a_id)
            if abm:
                abm.record_outcome(fitness, correlated=correlated)

        self.decision_log.append({
            'time': self.time, 'opp_id': opp.id,
            'mode': record.mode.name, 'fitness': fitness,
            'n_human': record.agent_mix[0], 'n_ai': record.agent_mix[1],
            'epoch': self._epoch_number,
        })

    # ── Resolution firers ─────────────────────────────────────────────────────

    def _fire_resolve(self, opp: Opportunity):
        """
        FIX 3: Resolution fitness = NK fitness of mean agent belief
        (not macro template), weighted by best solution quality.
        This makes composition genuinely shape the fitness signal at consolidation.
        """
        beliefs = []
        for h_id in opp.registered_humans:
            abm = self.pool.humans.get(h_id)
            if abm:
                beliefs.append(abm.token.belief.copy())
        for a_id in opp.registered_ais:
            abm = self.pool.ais.get(a_id)
            if abm:
                beliefs.append(abm.token.belief.copy())

        if beliefs:
            mean_belief = np.mean(beliefs, axis=0)
            agent_fitness = self.landscape.fitness(mean_belief)
        else:
            agent_fitness = self.landscape.fitness(self.macro.template)

        best_q = max((q for q, _ in opp.proposed_solutions), default=0.5)
        # Blend: 60% agent-belief fitness + 40% template fitness, weighted by quality
        template_fitness = self.landscape.fitness(self.macro.template)
        blended = 0.6 * agent_fitness + 0.4 * template_fitness
        fitness = float(np.clip(blended * (0.5 + 0.5 * best_q), 0, 1))
        rec = self._make_record(opp, DecisionMode.RESOLUTION, fitness)
        self._close_opportunity(opp, rec)

    def _fire_oversight(self, opp: Opportunity):
        rec = self._make_record(opp, DecisionMode.OVERSIGHT, 0.0)
        self._close_opportunity(opp, rec)

    def _fire_flight(self, opp: Opportunity):
        self.problem_stream.extend([
            Problem(pid, ProblemType.OPERATIONAL, 0.5)
            for pid in opp.attached_problems
        ])
        rec = self._make_record(opp, DecisionMode.FLIGHT, 0.0)
        self._close_opportunity(opp, rec)

    def _fire_token_exhaustion(self, opp: Opportunity):
        rec = self._make_record(opp, DecisionMode.TOKEN_EXHAUSTION, 0.0)
        self._close_opportunity(opp, rec)

    def _fire_correlated_failure(self, opp: Opportunity, flagged_ai_ids: List[int]):
        self._correlated_failure_count_epoch += 1
        rec = self._make_record(opp, DecisionMode.CORRELATED_FAILURE, 0.0)
        self._close_opportunity(opp, rec)
        for ai_id in flagged_ai_ids:
            abm = self.pool.ais.get(ai_id)
            if abm:
                abm.reset_failure_flag()

    def _fire_dissolution(self, opp: Opportunity):
        rec = self._make_record(opp, DecisionMode.DISSOLUTION, 0.0)
        self._close_opportunity(opp, rec)

    # ── T_replenish ───────────────────────────────────────────────────────────

    def _T_replenish(self):
        self.token_reservoir.replenish()

    # ── T_probe_entry ─────────────────────────────────────────────────────────

    def _T_probe_entry(self):
        self.pool.inject_probe()

    # ── Deadline checker ──────────────────────────────────────────────────────

    def _check_deadlines(self):
        expired = [oid for oid, opp in self.opportunities_active.items()
                   if self.time > opp.deadline]
        for oid in expired:
            opp = self.opportunities_active.get(oid)
            if opp:
                self._fire_dissolution(opp)

        expired_open = [oid for oid, opp in self.opportunities_open.items()
                        if self.time > opp.deadline]
        for oid in expired_open:
            opp = self.opportunities_open.pop(oid, None)
            if opp:
                opp.state = OpportunityState.CLOSED
                rec = self._make_record(opp, DecisionMode.DISSOLUTION, 0.0)
                self.decision_output.append(rec)
                self._epoch_records.append(rec)

    # ── Mutation triggers ─────────────────────────────────────────────────────

    def _check_mutation_triggers(self):
        p = self.p

        # T_replace_h2a
        if self.pool.n_ai < p.get('max_ai', 30):
            for h_id in self.pool.humans_below_threshold(p['theta_replace']):
                if self.token_reservoir.debit(p.get('onboarding_cost', 100)):
                    seed = self._seed_from_memory()
                    self.pool.replace_human_with_ai(h_id, seed_belief=seed)
                    break

        # T_replace_a2h
        if self._correlated_failure_count_epoch >= p['xi_trust']:
            flagged = self.pool.ais_with_failure_flag()
            for a_id in flagged[:1]:
                seed = self.macro.template.copy()
                self.pool.replace_ai_with_human(a_id, seed_belief=seed)
            self._correlated_failure_count_epoch = 0

    def _seed_from_memory(self) -> Optional[np.ndarray]:
        if not self.org_memory.entries:
            return None
        best = max(self.org_memory.entries, key=lambda e: e.fitness_outcome)
        noise = self.rng.normal(0, 0.1, len(best.belief_snapshot))
        return np.clip(best.belief_snapshot + noise, 0, 1)

    # ── Epoch boundary ────────────────────────────────────────────────────────

    def _epoch_boundary(self):
        self._epoch_number += 1

        epoch_stats = self.macro.consolidate(
            records=self._epoch_records,
            org_memory=self.org_memory,
            current_time=self.time
        )
        obs_dep = self.macro.all_reference_rates(self._epoch_beliefs)
        self.pool.apply_epoch_updates()

        result = {
            'epoch': self._epoch_number,
            'time': self.time,
            'n_human': self.pool.n_human,
            'n_ai': self.pool.n_ai,
            'token_balance': self.token_reservoir.balance,
            'macro_fitness': self.macro.current_fitness,
            'template_revised': self.macro.template_revised_this_epoch(),
            **epoch_stats,
            **{f'explore_{k}': v for k, v in obs_dep.items()},
        }
        self.results.append(result)

        self._epoch_records = []
        self._epoch_beliefs = []
        self._correlated_failure_count_epoch = 0
