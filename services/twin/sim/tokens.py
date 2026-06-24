"""
tokens.py
---------
Typed token color classes for the extended Colored Petri Net.
Each class corresponds to one token color in Table 1 of Section 7.
All tokens are immutable value objects (frozen dataclasses) except
where noted; mutable state lives in the simulation's place dictionaries.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Tuple
import numpy as np


# ── Enumerations ──────────────────────────────────────────────────────────────

class ProblemType(Enum):
    OPERATIONAL = auto()   # routine, domain-stable problems
    STRATEGIC   = auto()   # high-stakes, framing-sensitive problems
    RELATIONAL  = auto()   # interpersonal / stakeholder problems

class SolutionType(Enum):
    PROCEDURAL  = auto()   # codified, rule-based solutions
    CREATIVE    = auto()   # novel, recombinant solutions
    ADAPTIVE    = auto()   # context-tuned solutions

class OpportunityState(Enum):
    OPEN   = auto()
    ACTIVE = auto()
    CLOSED = auto()

class DecisionMode(Enum):
    RESOLUTION         = auto()
    OVERSIGHT          = auto()
    FLIGHT             = auto()
    TOKEN_EXHAUSTION   = auto()
    CORRELATED_FAILURE = auto()
    DISSOLUTION        = auto()

class AllocPolicy(Enum):
    UNIFORM             = auto()   # equal budget per opportunity
    ROLE_WEIGHTED       = auto()   # high-value opps get more
    PERFORMANCE_WEIGHTED = auto()  # budget follows demonstrated return


# ── Token color classes ───────────────────────────────────────────────────────

@dataclass
class Problem:
    """
    P — problem token.
    type controls which opportunity framings may consume this token.
    urgency in [0,1]; higher urgency causes flight if left unattached.
    age increments each time unit the problem circulates unresolved.
    """
    id: int
    type: ProblemType
    urgency: float          # U[0,1]
    age: int = 0

    def tick(self) -> 'Problem':
        return Problem(self.id, self.type, self.urgency, self.age + 1)


@dataclass
class Solution:
    """
    S — solution token.
    cost_tokens = 0 for human-generated solutions; > 0 for AI-generated.
    quality in [0,1] drawn from Beta(α,β) at generation time.
    """
    id: int
    domain: SolutionType
    quality: float          # Beta(α, β)
    cost_tokens: int = 0    # 0 for human; c_gen for AI-generated


@dataclass
class HumanAgent:
    """
    H — human agent token.
    belief: N-dimensional belief vector over the representation space.
    hcm_level: cumulative HCM investment [0,1]; widens framing overlap.
    selfdir_rate: individual self-directed learning momentum [0, 0.05].
    fitness: rolling fitness score updated each epoch.
    role: string tag (any knowledge-work domain — not restricted to engineering).
    """
    id: int
    belief: np.ndarray      # shape (N,), values in {0,1}
    hcm_level: float        # [0, 1]
    selfdir_rate: float     # [0, 0.05]
    fitness: float = 0.5
    role: str = "knowledge_worker"

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        return isinstance(other, HumanAgent) and self.id == other.id


@dataclass
class AIAgent:
    """
    A — AI agent token.
    belief: N-dimensional embedding belief vector; dense float values.
    token_balance: current compute budget for this agent instance.
    rag_depth: max retrieval hops per T_RAG firing.
    failure_flag: set True when agent participates in a correlated failure.
    model_version: string tag allowing correlated failure grouping.
    role: any knowledge-work domain (legal, finance, HR, ops, etc.).
    """
    id: int
    belief: np.ndarray      # shape (N,), float values
    token_balance: int
    rag_depth: int = 2
    failure_flag: bool = False
    model_version: str = "v1"
    fitness: float = 0.5
    role: str = "knowledge_worker"

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        return isinstance(other, AIAgent) and self.id == other.id


@dataclass
class Opportunity:
    """
    O — choice opportunity token.
    framing: set of admissible ProblemTypes for this opportunity.
    access_min_hcm: minimum hcm_level for human agent coupling.
    deadline: absolute simulation time at which opportunity expires.
    state: lifecycle state.
    registered_humans: list of HumanAgent ids participating.
    registered_ais: list of AIAgent ids participating.
    attached_problems: list of Problem ids attached.
    proposed_solutions: list of (Solution id, agent id) tuples.
    open_time: simulation time at which opportunity opened.
    """
    id: int
    framing: Tuple[ProblemType, ...]
    access_min_hcm: float
    deadline: int
    state: OpportunityState = OpportunityState.OPEN
    registered_humans: List[int] = field(default_factory=list)
    registered_ais: List[int] = field(default_factory=list)
    attached_problems: List[int] = field(default_factory=list)
    proposed_solutions: List[Tuple[int, int]] = field(default_factory=list)
    open_time: int = 0

    @property
    def total_participants(self) -> int:
        return len(self.registered_humans) + len(self.registered_ais)

    @property
    def quorum_met(self) -> bool:
        return self.total_participants >= 2

    @property
    def human_quorum(self) -> bool:
        return len(self.registered_humans) >= 2


@dataclass
class TokenReservoir:
    """
    R — organizational token budget.
    balance: current available tokens.
    replenish_rate: tokens added per epoch.
    budget_ceiling: cap on balance after replenishment.
    alloc_policy: how tokens are distributed across opportunity types.
    """
    balance: int
    replenish_rate: int
    budget_ceiling: int
    alloc_policy: AllocPolicy = AllocPolicy.UNIFORM

    def debit(self, amount: int) -> bool:
        """Attempt to debit amount. Returns True if successful."""
        if self.balance >= amount:
            self.balance -= amount
            return True
        return False

    def replenish(self):
        self.balance = min(self.balance + self.replenish_rate, self.budget_ceiling)


@dataclass
class MemEntry:
    """Single entry in the organizational memory store."""
    id: int
    decision_mode: DecisionMode
    fitness_outcome: float
    belief_snapshot: np.ndarray   # macro belief template at time of decision
    opportunity_framing: Tuple[ProblemType, ...]
    agent_mix: Tuple[int, int]    # (n_humans, n_ais)
    timestamp: int


@dataclass
class OrgMemory:
    """
    M — organizational memory / RAG store.
    entries: list of MemEntry tokens.
    retrieval_cost: tokens debited per retrieval hop.
    staleness_decay: weight applied to older entries (exponential).
    """
    entries: List[MemEntry] = field(default_factory=list)
    retrieval_cost: int = 30
    staleness_decay: float = 0.95  # per-epoch decay on entry weight

    def append(self, entry: MemEntry):
        self.entries.append(entry)

    def retrieve(self, query_belief: np.ndarray, framing: Tuple,
                 depth: int, current_time: int) -> List[MemEntry]:
        """
        Return top-`depth` entries by cosine similarity to query_belief,
        weighted by fitness_outcome and recency.
        Falls back gracefully if store is empty.
        """
        if not self.entries:
            return []
        scores = []
        for e in self.entries:
            # cosine similarity in belief space
            qn = np.linalg.norm(query_belief)
            en = np.linalg.norm(e.belief_snapshot)
            if qn < 1e-9 or en < 1e-9:
                cos_sim = 0.0
            else:
                cos_sim = float(np.dot(query_belief, e.belief_snapshot) / (qn * en))
            # framing overlap bonus
            framing_overlap = len(set(framing) & set(e.opportunity_framing)) / max(len(framing), 1)
            # recency weight
            age_epochs = max(0, (current_time - e.timestamp) // 50)
            recency = self.staleness_decay ** age_epochs
            # fitness weighting (higher-fitness records pulled more strongly)
            score = cos_sim * framing_overlap * recency * (0.5 + 0.5 * e.fitness_outcome)
            scores.append((score, e))
        scores.sort(key=lambda x: -x[0])
        return [e for _, e in scores[:depth]]


@dataclass
class DecisionRecord:
    """
    D — output token consumed by macro-agent at consolidation.
    mode: which resolution transition fired.
    fitness_outcome: NK landscape fitness returned for this decision.
    agent_mix: (n_humans, n_ais) at resolution time.
    opportunity_id: which opportunity produced this record.
    timestamp: simulation time of resolution.
    belief_snapshot: macro-agent template at time of resolution.
    """
    id: int
    mode: DecisionMode
    fitness_outcome: float
    agent_mix: Tuple[int, int]   # (n_humans, n_ais)
    opportunity_id: int
    timestamp: int
    belief_snapshot: np.ndarray
    opportunity_framing: Tuple[ProblemType, ...] = field(default_factory=tuple)
