"""
landscape.py
------------
NK rugged performance landscape.
N = dimensionality of the belief/policy space.
K = epistatic linkage (ruggedness). K=0 → additive/smooth; K=N-1 → maximally rugged.

Each of the N loci contributes a fitness value that depends on its own
state and the states of K randomly chosen other loci (its epistatic
neighbours). The total fitness of a configuration is the mean of the
N locus contributions.

This is the standard Levinthal (1997) NK formulation used in the companion
paper, extended here to accept continuous-valued belief vectors by
binarising them at 0.5 (belief[i] >= 0.5 → 1, else → 0).
"""
from __future__ import annotations
import numpy as np
from typing import Tuple, Dict


class NKLandscape:
    """
    NK fitness landscape.

    Parameters
    ----------
    N : int
        Number of dimensions (loci). Default 12 (Paper 1 / Paper 2 baseline).
    K : int
        Epistatic linkage per locus. Default 4 (baseline); varied in sensitivity.
    seed : int
        RNG seed for landscape construction (fixed per simulation run so that
        all conditions face the same landscape).
    """

    def __init__(self, N: int = 12, K: int = 4, seed: int = 42):
        self.N = N
        self.K = K
        rng = np.random.default_rng(seed)

        # For each locus i, choose K epistatic neighbours from the other N-1 loci
        self.neighbours: Dict[int, Tuple[int, ...]] = {}
        for i in range(N):
            others = [j for j in range(N) if j != i]
            chosen = rng.choice(others, size=min(K, len(others)), replace=False)
            self.neighbours[i] = tuple(int(x) for x in sorted(chosen))

        # For each locus i, build a lookup table over all 2^(K+1) states
        # of (locus_i, neighbours_i).  Values drawn from U[0,1].
        self.tables: Dict[int, Dict[Tuple[int, ...], float]] = {}
        for i in range(N):
            n_states = 2 ** (len(self.neighbours[i]) + 1)
            vals = rng.uniform(0.0, 1.0, size=n_states)
            self.tables[i] = {}
            for state_idx in range(n_states):
                bits = tuple(int(b) for b in format(state_idx, f'0{len(self.neighbours[i])+1}b'))
                self.tables[i][bits] = float(vals[state_idx])

    def _binarise(self, belief: np.ndarray) -> np.ndarray:
        """Convert continuous belief vector to binary configuration."""
        return (belief >= 0.5).astype(int)

    def fitness(self, belief: np.ndarray) -> float:
        """
        Return fitness in [0,1] for a given belief vector.
        Binarises first, then evaluates the NK table.
        """
        config = self._binarise(belief)
        total = 0.0
        for i in range(self.N):
            locus_val = config[i]
            neighbour_vals = tuple(int(config[j]) for j in self.neighbours[i])
            key = (locus_val,) + neighbour_vals
            total += self.tables[i].get(key, 0.0)
        return total / self.N

    def local_optima_count(self, sample: int = 2000, rng_seed: int = 0) -> int:
        """
        Estimate number of local optima via random sampling.
        A configuration is a local optima if flipping any single bit
        reduces fitness. Used for landscape characterisation only.
        """
        rng = np.random.default_rng(rng_seed)
        count = 0
        configs = rng.integers(0, 2, size=(sample, self.N))
        for cfg in configs:
            f0 = self.fitness(cfg.astype(float))
            is_local = True
            for i in range(self.N):
                neighbour = cfg.copy()
                neighbour[i] = 1 - neighbour[i]
                if self.fitness(neighbour.astype(float)) > f0:
                    is_local = False
                    break
            if is_local:
                count += 1
        return count

    def gradient_step(self, belief: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        """
        Take a single hill-climbing step from the current belief.
        Used by the macro-agent's consolidation update rule.
        Flips the single bit that most improves fitness; if none improves,
        returns the current belief unchanged (at a local optimum).
        """
        config = self._binarise(belief)
        best_f = self.fitness(config.astype(float))
        best_config = config.copy()
        for i in range(self.N):
            trial = config.copy()
            trial[i] = 1 - trial[i]
            f = self.fitness(trial.astype(float))
            if f > best_f:
                best_f = f
                best_config = trial.copy()
        return best_config.astype(float)
