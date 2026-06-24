"""
macro.py
--------
Macro-agent: the organizational belief template, epochal consolidation,
and the observer-dependence classification procedure from Section 4.5
of the companion paper, extended for the hybrid agent setting.

The macro-agent:
  1. Holds the organizational belief template (N-dimensional).
  2. At each epoch boundary, consumes all pending DecisionRecords and
     updates the template toward high-fitness configurations (consolidation).
  3. Writes the epoch summary to OrgMemory (T_consolidate arc).
  4. Classifies any trajectory as exploratory or exploitative under five
     reference templates (the observer-dependence procedure).
  5. Tracks template snapshots for longitudinal analysis.
"""
from __future__ import annotations
import numpy as np
from typing import List, Tuple, Dict, Optional
from tokens import DecisionRecord, DecisionMode, OrgMemory, MemEntry
from landscape import NKLandscape


# ── Reference template types for observer-dependence classification ───────────

REFERENCE_LABELS = [
    'current',        # organisation's present template
    'one_epoch_ago',  # template from one epoch prior
    'three_epochs_ago',
    'peer',           # a peer organisation's template (injected externally)
    'initial',        # the template at simulation start
]


class MacroAgent:
    """
    Organisational macro-agent.

    Parameters
    ----------
    N : int          Belief vector dimensionality.
    landscape        NKLandscape instance.
    rng              NumPy Generator (seeded per simulation run).
    consolidation_lr Learning rate for template update toward high-fitness configs.
    """

    def __init__(self, N: int, landscape: NKLandscape,
                 rng: np.random.Generator,
                 consolidation_lr: float = 0.2):
        self.N = N
        self.landscape = landscape
        self.rng = rng
        self.consolidation_lr = consolidation_lr

        # Initial template: random draw
        self.template = rng.uniform(0, 1, N)
        self.initial_template = self.template.copy()

        # Snapshot history: list of (epoch, template_copy, fitness)
        self.snapshots: List[Tuple[int, np.ndarray, float]] = []
        self._epoch = 0

        # Peer template: set externally to simulate peer-organisation reference
        self.peer_template: Optional[np.ndarray] = None

    # ── Consolidation (T_consolidate) ──────────────────────────────────────────

    def consolidate(self, records: List[DecisionRecord],
                    org_memory: OrgMemory,
                    current_time: int) -> Dict:
        """
        Epoch-boundary consolidation.
        1. Update template toward mean of high-fitness resolution records.
        2. Take a gradient step on the NK landscape from the updated template.
        3. Append epoch summary to org_memory.
        4. Record snapshot.
        Returns a dict of epoch-level statistics.
        """
        self._epoch += 1

        # Snapshot before update
        pre_template = self.template.copy()
        pre_fitness = self.landscape.fitness(self.template)

        # Filter: only resolution records contribute to template update
        resolution_records = [r for r in records
                               if r.mode == DecisionMode.RESOLUTION]

        if resolution_records:
            # Weight each record by fitness outcome AND agent-mix diversity.
            # Human-dominated records (sparse beliefs) pull template toward
            # exploration-friendly regions; AI-dominated records (dense beliefs)
            # pull toward exploitation-friendly convergence points.
            # This makes composition causally shape the macro-level trajectory.
            weights = []
            for r in resolution_records:
                fitness_w = max(0.01, r.fitness_outcome)
                # Diversity bonus: records with mixed composition get extra weight
                n_h, n_a = r.agent_mix
                total = max(1, n_h + n_a)
                mix_balance = 1.0 - abs(n_h - n_a) / total   # 1=balanced, 0=homogeneous
                diversity_w = 0.7 + 0.3 * mix_balance
                weights.append(fitness_w * diversity_w)
            weights = np.array(weights)
            weights = weights / weights.sum()
            target = np.zeros(self.N)
            for w, r in zip(weights, resolution_records):
                snap = r.belief_snapshot[:self.N]
                if len(snap) < self.N:
                    snap = np.pad(snap, (0, self.N - len(snap)))
                target += w * snap

            # Move template toward agent-mix-weighted target
            self.template = ((1 - self.consolidation_lr) * self.template
                              + self.consolidation_lr * target)
            self.template = np.clip(self.template, 0, 1)

        # NK gradient step: apply with probability proportional to how
        # much the agent-belief signal has already moved the template.
        # High consolidation_lr + strong agent signal → gradient step less dominant.
        revision_so_far = float(np.linalg.norm(self.template - pre_template))
        gradient_prob = max(0.1, 1.0 - revision_so_far * 4.0)
        if self.rng.random() < gradient_prob:
            self.template = self.landscape.gradient_step(self.template, self.rng)

        post_fitness = self.landscape.fitness(self.template)

        # Write epoch summary to org_memory
        for r in records:
            entry = MemEntry(
                id=len(org_memory.entries),
                decision_mode=r.mode,
                fitness_outcome=r.fitness_outcome,
                belief_snapshot=self.template.copy(),
                opportunity_framing=r.opportunity_framing,
                agent_mix=r.agent_mix,
                timestamp=current_time
            )
            org_memory.append(entry)

        # Snapshot after update
        self.snapshots.append((self._epoch, self.template.copy(), post_fitness))

        # Compute template revision magnitude
        revision_magnitude = float(np.linalg.norm(self.template - pre_template))

        # Mode distribution for this epoch
        mode_counts: Dict[str, int] = {m.name: 0 for m in DecisionMode}
        for r in records:
            mode_counts[r.mode.name] += 1

        return {
            'epoch': self._epoch,
            'pre_fitness': pre_fitness,
            'post_fitness': post_fitness,
            'n_records': len(records),
            'n_resolution': len(resolution_records),
            'revision_magnitude': revision_magnitude,
            'mode_counts': mode_counts,
            'agent_mix_mean': self._mean_agent_mix(records),
        }

    def _mean_agent_mix(self, records: List[DecisionRecord]) -> Tuple[float, float]:
        if not records:
            return (0.0, 0.0)
        h_vals = [r.agent_mix[0] for r in records]
        a_vals = [r.agent_mix[1] for r in records]
        return (float(np.mean(h_vals)), float(np.mean(a_vals)))

    # ── Observer-dependence classification ─────────────────────────────────────

    def classify_trajectory_step(self, step_belief: np.ndarray,
                                  reference: str = 'current') -> float:
        """
        Classify a single trajectory step as exploratory (1) or
        exploitative (0) relative to the given reference template.

        A step is classified as exploratory if it moves the belief vector
        away from the reference template (increasing distance), exploitative
        if it moves it toward the reference (decreasing distance).

        Returns a float in [0,1]: 1 = fully exploratory, 0 = fully exploitative.
        The graded measure uses cosine distance normalised to [0,1].
        """
        ref = self._get_reference(reference)
        dist = float(np.linalg.norm(step_belief - ref)) / (np.sqrt(self.N) + 1e-9)
        return float(np.clip(dist, 0, 1))

    def classify_epoch_exploration_rate(self, epoch_beliefs: List[np.ndarray],
                                         reference: str = 'current') -> float:
        """
        Mean exploration rate for a sequence of trajectory steps under
        a given reference template. Replicates Section 4.5 of companion paper.
        """
        if not epoch_beliefs:
            return 0.5
        rates = [self.classify_trajectory_step(b, reference) for b in epoch_beliefs]
        return float(np.mean(rates))

    def _get_reference(self, reference: str) -> np.ndarray:
        if reference == 'current':
            return self.template.copy()
        elif reference == 'initial':
            return self.initial_template.copy()
        elif reference == 'peer':
            return self.peer_template.copy() if self.peer_template is not None \
                   else self.rng.uniform(0, 1, self.N)
        elif reference == 'one_epoch_ago':
            if len(self.snapshots) >= 2:
                return self.snapshots[-2][1].copy()
            return self.template.copy()
        elif reference == 'three_epochs_ago':
            if len(self.snapshots) >= 4:
                return self.snapshots[-4][1].copy()
            return self.initial_template.copy()
        else:
            raise ValueError(f"Unknown reference: {reference}")

    def all_reference_rates(self, epoch_beliefs: List[np.ndarray]) -> Dict[str, float]:
        """Return exploration rates under all five reference templates."""
        return {ref: self.classify_epoch_exploration_rate(epoch_beliefs, ref)
                for ref in REFERENCE_LABELS}

    # ── Template revision detection ────────────────────────────────────────────

    def template_revised_this_epoch(self, threshold: float = 0.05) -> bool:
        """True if the template changed by more than threshold this epoch."""
        if len(self.snapshots) < 2:
            return False
        prev = self.snapshots[-2][1]
        curr = self.snapshots[-1][1]
        return float(np.linalg.norm(curr - prev)) > threshold

    @property
    def current_fitness(self) -> float:
        return self.landscape.fitness(self.template)
