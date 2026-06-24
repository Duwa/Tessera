"""
tokens_p3.py
------------
Minimal token extensions for Paper 3: Belief-Misalignment Pathology.

Adds two fields that are absent from Paper 2's token spec:
  - Solution.alignment_score : float  — how well the solution addresses the
        framing of the opportunity it was generated in.  Distinct from quality,
        which measures the solution's intrinsic merit.  The pathology arises
        when quality is high and alignment_score is low.
  - DecisionRecord.mean_solution_quality : float  — epoch-level mean quality.
  - DecisionRecord.mean_alignment_score  : float  — epoch-level mean alignment.
        The divergence between these two series is the primary diagnostic signal.

All Paper 2 token classes are imported and re-exported unchanged.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Tuple, List
import numpy as np

# Re-export everything from Paper 2
from tokens import (
    Problem, HumanAgent, AIAgent, Opportunity, TokenReservoir,
    OrgMemory, MemEntry, DecisionRecord,
    ProblemType, SolutionType, OpportunityState, DecisionMode, AllocPolicy
)


@dataclass
class SolutionP3:
    """
    Extended Solution token for Paper 3.

    alignment_score: float in [0,1].
        Measures how well this solution addresses the problem type(s) active
        in the opportunity framing at generation time.  Computed as the
        generating agent's belief-framing overlap score (the same float that
        _belief_overlaps_framing now returns) at the moment of generation.

        - Human-generated solutions: alignment_score = generating agent's
          domain activation in the framing dimensions (domain-specialist
          humans have high alignment in their domain, low elsewhere).
        - AI-generated solutions: alignment_score = AI's belief-framing
          overlap score, which is high-entropy and moderate across all
          framings — but can systematically miss when the agent's RAG
          retrieval has converged on a different region of the belief space.

    quality: float in [0,1] — intrinsic solution quality, independent of
        whether it addresses the right problem.  AI agents produce higher
        mean quality (Beta(3,2)) than human agents (Beta(2,2)) because
        their retrieval-augmented generation draws on the best historical
        records.  The pathology: quality rises as AI composition increases;
        alignment_score silently degrades.
    """
    id: int
    domain: SolutionType
    quality: float          # Beta(α,β) — intrinsic merit
    alignment_score: float  # [0,1] — framing relevance at generation time
    cost_tokens: int = 0    # 0 for human; c_gen for AI-generated


@dataclass
class DecisionRecordP3:
    """
    Extended DecisionRecord for Paper 3.

    Adds mean_solution_quality and mean_alignment_score so the macro-agent's
    consolidation can track both dimensions separately.  The pathology is
    visible only in the divergence between these two series — neither series
    alone reveals it.
    """
    id: int
    mode: DecisionMode
    fitness_outcome: float
    agent_mix: Tuple[int, int]
    opportunity_id: int
    timestamp: int
    belief_snapshot: np.ndarray
    opportunity_framing: Tuple[ProblemType, ...] = field(default_factory=tuple)
    mean_solution_quality: float = 0.0   # NEW
    mean_alignment_score: float = 0.0    # NEW
    alignment_gap: float = 0.0           # NEW: quality - alignment (the pathology signal)
