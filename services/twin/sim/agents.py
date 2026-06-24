"""
agents.py
---------
Mesa-equivalent ABM agent layer.
Implements HumanAgentModel and AIAgentModel with belief update rules,
learning regime dynamics, and fitness tracking.

No external Mesa dependency — implements the same scheduler/step
interface using plain Python, swappable for real Mesa when network
is available.
"""
from __future__ import annotations
import numpy as np
from typing import List, Dict, Optional, TYPE_CHECKING
from tokens import HumanAgent, AIAgent, DecisionRecord, DecisionMode

if TYPE_CHECKING:
    from landscape import NKLandscape


# ── Minimal Mesa-equivalent scheduler ─────────────────────────────────────────

class RandomActivationScheduler:
    """
    Equivalent to Mesa's RandomActivation scheduler.
    Agents are stepped in random order each tick.
    """
    def __init__(self, rng: np.random.Generator):
        self._agents: Dict[int, object] = {}
        self.rng = rng
        self.time: int = 0

    def add(self, agent):
        self._agents[agent.unique_id] = agent

    def remove(self, agent_id: int):
        self._agents.pop(agent_id, None)

    def step(self):
        ids = list(self._agents.keys())
        self.rng.shuffle(ids)
        for aid in ids:
            if aid in self._agents:
                self._agents[aid].step()
        self.time += 1

    @property
    def agents(self):
        return list(self._agents.values())


# ── Human agent ABM model ──────────────────────────────────────────────────────

class HumanAgentABM:
    """
    ABM representation of a human micro-agent.
    Wraps a HumanAgent token and manages its belief update dynamics
    across Regime A (HCM) and Regime B (self-directed) learning.
    """

    def __init__(self, token: HumanAgent, landscape: 'NKLandscape',
                 org_template: np.ndarray, rng: np.random.Generator,
                 socialization_rate: float = 0.3):
        self.unique_id = token.id
        self.token = token
        self.landscape = landscape
        self.org_template = org_template   # reference; updated externally by macro-agent
        self.rng = rng
        self.socialization_rate = socialization_rate
        self._decision_outcomes: List[float] = []

    def step(self):
        """
        Called each simulation tick by the scheduler.
        Applies Regime B (self-directed) belief update: small random
        perturbation in the direction of personally observed high-fitness
        configurations, weighted by selfdir_rate.
        """
        if self.rng.random() < self.token.selfdir_rate:
            self._selfdir_update()

    def _selfdir_update(self):
        """
        Regime B: individual self-directed learning.
        Agent probes a random neighbour configuration and updates
        belief toward it if fitness is higher (gradient-free local search).
        """
        probe = self.token.belief.copy()
        flip_idx = self.rng.integers(0, len(probe))
        probe[flip_idx] = 1.0 - probe[flip_idx]
        if self.landscape.fitness(probe) > self.landscape.fitness(self.token.belief):
            alpha = self.token.selfdir_rate
            self.token.belief = (1 - alpha) * self.token.belief + alpha * probe

    def apply_hcm_update(self, hcm_rate: float):
        """
        Regime A: HCM-driven organizational learning applied at epoch boundary.
        Belief is socialized toward the org_template at a rate proportional
        to hcm_rate, weighted by 1 - current hcm_level (diminishing returns).
        Also increments hcm_level.
        """
        effective_rate = hcm_rate * (1.0 - self.token.hcm_level * 0.5)
        self.token.belief = ((1 - effective_rate) * self.token.belief
                             + effective_rate * self.org_template)
        self.token.belief = np.clip(self.token.belief, 0.0, 1.0)
        self.token.hcm_level = min(1.0, self.token.hcm_level + hcm_rate * 0.1)

    def record_outcome(self, fitness: float):
        """Record a decision outcome; used for fitness tracking."""
        self._decision_outcomes.append(fitness)

    def update_fitness(self):
        """Update rolling fitness at epoch boundary from recent outcomes."""
        if self._decision_outcomes:
            self.token.fitness = (0.7 * self.token.fitness
                                  + 0.3 * float(np.mean(self._decision_outcomes)))
            self._decision_outcomes.clear()


# ── AI agent ABM model ─────────────────────────────────────────────────────────

class AIAgentABM:
    """
    ABM representation of an AI micro-agent.
    Wraps an AIAgent token and manages Regime C (operational) learning.
    Belief updates occur via the RAG interface (external call) and via
    a continuous online update rule applied each tick.
    """

    def __init__(self, token: AIAgent, landscape: 'NKLandscape',
                 rng: np.random.Generator,
                 online_lr: float = 0.05):
        self.unique_id = token.id
        self.token = token
        self.landscape = landscape
        self.rng = rng
        self.online_lr = online_lr     # learning rate for online belief update
        self._decision_outcomes: List[float] = []
        self._failure_count: int = 0

    def step(self):
        """
        Called each tick. AI agents do not require active triggering
        for belief updates — those happen via RAG calls during opportunities.
        This tick handler manages token balance replenishment signals
        and monitors for correlated failure conditions.
        """
        pass  # main update logic in apply_rag_update and record_outcome

    def apply_rag_update(self, retrieved_entries, current_time: int):
        """
        Regime C: RAG-driven online belief update within an active opportunity.
        Updates belief as a weighted average of retrieved entry belief snapshots,
        where weights are proportional to fitness outcomes.
        This is the operationalisation of T_RAG in the ABM layer.
        """
        if not retrieved_entries:
            return
        weights = np.array([max(0.01, e.fitness_outcome) for e in retrieved_entries])
        weights = weights / weights.sum()
        target = np.zeros_like(self.token.belief)
        for w, e in zip(weights, retrieved_entries):
            # align entry snapshot dimensionality if needed
            snap = e.belief_snapshot[:len(self.token.belief)]
            if len(snap) < len(self.token.belief):
                snap = np.pad(snap, (0, len(self.token.belief) - len(snap)))
            target += w * snap
        # online gradient-style update: move belief toward retrieved target
        self.token.belief = ((1 - self.online_lr) * self.token.belief
                             + self.online_lr * target)
        self.token.belief = np.clip(self.token.belief, 0.0, 1.0)

    def belief_uncertainty(self) -> float:
        """
        Entropy of the belief vector as a proxy for retrieval need.
        High entropy → agent is uncertain → T_RAG should fire.
        Values near 0.5 in each dimension maximise entropy.
        """
        b = np.clip(self.token.belief, 1e-9, 1 - 1e-9)
        ent = -b * np.log2(b) - (1 - b) * np.log2(1 - b)
        return float(np.mean(ent))

    def record_outcome(self, fitness: float, correlated: bool = False):
        """Record decision outcome; flag correlated failure if indicated."""
        self._decision_outcomes.append(fitness)
        if correlated:
            self.token.failure_flag = True
            self._failure_count += 1

    def update_fitness(self):
        """Update rolling fitness at epoch boundary."""
        if self._decision_outcomes:
            self.token.fitness = (0.7 * self.token.fitness
                                  + 0.3 * float(np.mean(self._decision_outcomes)))
            self._decision_outcomes.clear()

    def reset_failure_flag(self):
        self.token.failure_flag = False
        self._failure_count = 0


# ── Agent pool manager ─────────────────────────────────────────────────────────

class AgentPool:
    """
    Manages the combined population of human and AI agent ABM instances.
    Wraps the scheduler and provides type-filtered access.
    Handles seeding of new agents from org_memory or template.
    """

    def __init__(self, rng: np.random.Generator, landscape: 'NKLandscape',
                 N: int, org_template: np.ndarray,
                 n_human: int = 20, n_ai: int = 10,
                 hcm_rate: float = 0.02,
                 token_balance_init: int = 500,
                 rag_depth: int = 2):
        self.rng = rng
        self.landscape = landscape
        self.N = N
        self.hcm_rate = hcm_rate
        self.token_balance_init = token_balance_init
        self.rag_depth = rag_depth
        self._next_id = 0
        self.org_template = org_template  # shared reference; macro-agent updates it

        self.scheduler = RandomActivationScheduler(rng)
        self.humans: Dict[int, HumanAgentABM] = {}
        self.ais: Dict[int, AIAgentABM] = {}

        # Initialise populations
        for _ in range(n_human):
            self._spawn_human()
        for _ in range(n_ai):
            self._spawn_ai()

    def _next(self) -> int:
        aid = self._next_id
        self._next_id += 1
        return aid

    def _spawn_human(self, belief: Optional[np.ndarray] = None,
                     role: str = "knowledge_worker") -> HumanAgentABM:
        if belief is None:
            # Human agents: sparse binary-biased beliefs
            # Most dimensions near 0 or 1 (domain specialisation)
            # creates structural diversity from AI agents
            raw = self.rng.beta(0.4, 0.4, self.N)   # U-shaped: mass near 0 and 1
            belief = raw
        token = HumanAgent(
            id=self._next(),
            belief=belief.copy(),
            hcm_level=float(self.rng.uniform(0.1, 0.5)),
            selfdir_rate=float(self.rng.uniform(0.0, 0.05)),
            role=role
        )
        abm = HumanAgentABM(token, self.landscape, self.org_template, self.rng)
        self.scheduler.add(abm)
        self.humans[token.id] = abm
        return abm

    def _spawn_ai(self, belief: Optional[np.ndarray] = None,
                  model_version: str = "v1",
                  role: str = "knowledge_worker") -> AIAgentABM:
        if belief is None:
            # AI agents: dense continuous beliefs near 0.5 (high entropy)
            # Represents broad coverage without deep specialisation
            belief = self.rng.beta(3, 3, self.N)   # bell-shaped: mass near 0.5
        token = AIAgent(
            id=self._next(),
            belief=belief.copy(),
            token_balance=self.token_balance_init,
            rag_depth=self.rag_depth,
            model_version=model_version,
            role=role
        )
        abm = AIAgentABM(token, self.landscape, self.rng)
        self.scheduler.add(abm)
        self.ais[token.id] = abm
        return abm

    def step(self):
        self.scheduler.step()

    # ── Mutation transitions ───────────────────────────────────────────────────

    def replace_human_with_ai(self, human_id: int,
                               seed_belief: Optional[np.ndarray] = None,
                               token_cost: int = 0) -> Optional[AIAgentABM]:
        """
        T_replace_h2a: remove a human agent, spawn an AI agent.
        seed_belief is typically drawn from org_memory retrieval.
        Returns new AIAgentABM, or None if human_id not found.
        """
        if human_id not in self.humans:
            return None
        abm = self.humans.pop(human_id)
        self.scheduler.remove(human_id)
        belief = seed_belief if seed_belief is not None else self.rng.uniform(0, 1, self.N)
        new_ai = self._spawn_ai(belief=belief)
        return new_ai

    def replace_ai_with_human(self, ai_id: int,
                               seed_belief: Optional[np.ndarray] = None) -> Optional[HumanAgentABM]:
        """
        T_replace_a2h: remove an AI agent, spawn a human agent.
        seed_belief is typically sampled from the current org_template.
        Returns new HumanAgentABM, or None if ai_id not found.
        """
        if ai_id not in self.ais:
            return None
        abm = self.ais.pop(ai_id)
        self.scheduler.remove(ai_id)
        belief = seed_belief if seed_belief is not None else self.org_template.copy()
        new_human = self._spawn_human(belief=belief)
        return new_human

    def inject_probe(self, external_dist_mean: Optional[np.ndarray] = None) -> HumanAgentABM:
        """
        T_probe_entry: inject a new-grad outsider human agent.
        Belief drawn from external distribution (deliberately NOT org_memory),
        centred at external_dist_mean with high variance.
        """
        if external_dist_mean is not None:
            noise = self.rng.normal(0, 0.3, self.N)
            belief = np.clip(external_dist_mean + noise, 0, 1)
        else:
            belief = self.rng.uniform(0, 1, self.N)
        return self._spawn_human(belief=belief, role="new_grad_probe")

    # ── Epoch-boundary updates ─────────────────────────────────────────────────

    def apply_epoch_updates(self):
        """Apply HCM updates to all human agents and fitness updates to all."""
        for abm in self.humans.values():
            abm.apply_hcm_update(self.hcm_rate)
            abm.update_fitness()
        for abm in self.ais.values():
            abm.update_fitness()

    # ── Mutation trigger checks ────────────────────────────────────────────────

    def humans_below_threshold(self, theta: float) -> List[int]:
        return [aid for aid, abm in self.humans.items()
                if abm.token.fitness < theta]

    def ais_with_failure_flag(self) -> List[int]:
        return [aid for aid, abm in self.ais.items()
                if abm.token.failure_flag]

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def available_humans(self) -> List[HumanAgentABM]:
        return list(self.humans.values())

    @property
    def available_ais(self) -> List[AIAgentABM]:
        return list(self.ais.values())

    @property
    def n_human(self) -> int:
        return len(self.humans)

    @property
    def n_ai(self) -> int:
        return len(self.ais)
