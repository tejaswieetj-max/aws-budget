"""Margin policies. Ratios are actual cost / modeled cost for observed runtime."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from math import ceil

FIXED = 1.10


def quantile(values: Iterable[float], level: float) -> float:
    xs = sorted(values)
    if not xs:
        return FIXED
    # Nearest-rank empirical quantile, rank clamped to [1,n].
    return xs[max(0, min(len(xs) - 1, ceil(level * len(xs)) - 1))]


@dataclass
class Sample:
    settlement_id: str
    ratio: float
    reserved_micro: int
    actual_micro: int
    modeled_runtime_micro: int
    settled_order: int


@dataclass
class AdaptiveState:
    alpha: float = 0.05
    samples: list[Sample] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)


class MarginPolicies:
    def __init__(self, gamma: float = 0.02, target_alpha: float = 0.05):
        if not 0.01 <= gamma <= 0.05:
            raise ValueError("gamma must be in [0.01, 0.05]")
        if target_alpha != 0.05:
            raise ValueError("v1 target alpha is fixed at 0.05")
        self.gamma, self.target_alpha = gamma, target_alpha
        self.samples: dict[str, list[Sample]] = {}
        self.adaptive: dict[str, AdaptiveState] = {}
        self.split_calibration: dict[str, list[float]] = {}

    def set_split_calibration(self, resource_class: str, ratios: Iterable[float]) -> None:
        """Freeze an independent calibration partition for split conformal."""
        xs = sorted(float(x) for x in ratios if float(x) > 0)
        self.split_calibration[resource_class] = xs

    def add_settlement(self, resource_class: str, sample: Sample) -> bool:
        """Deduplicate by settlement id; stable-sort by billing order then id.

        Delayed arrivals are inserted in deterministic order. We replay the full
        alpha sequence from the initial state, so arrival order cannot change the
        result. A correction must use a new settlement version/id.
        """
        state = self.adaptive.setdefault(resource_class, AdaptiveState())
        if sample.settlement_id in state.seen:
            return False
        state.seen.add(sample.settlement_id)
        values = self.samples.setdefault(resource_class, [])
        values.append(sample)
        values.sort(key=lambda s: (s.settled_order, s.settlement_id))
        self._replay(resource_class)
        return True

    def _replay(self, resource_class: str) -> None:
        state = self.adaptive[resource_class]
        alpha = self.target_alpha
        for s in self.samples[resource_class]:
            miss = 1 if s.actual_micro > s.reserved_micro else 0
            alpha = min(0.5, max(0.001, alpha + self.gamma * (self.target_alpha - miss)))
        state.alpha = alpha

    def get(self, resource_class: str, policy: str = "fixed") -> float:
        xs = [s.ratio for s in self.samples.get(resource_class, []) if s.ratio > 0]
        if policy == "fixed":
            return FIXED
        if policy == "rolling_quantile":
            return min(2.0, max(1.0, quantile(xs[-50:], 0.95))) if len(xs) >= 10 else FIXED
        if policy == "adaptive":
            alpha = self.adaptive.setdefault(resource_class, AdaptiveState()).alpha
            return min(2.0, max(1.0, quantile(xs, 1 - alpha))) if len(xs) >= 10 else FIXED
        if policy == "split_conformal":
            # One-sided split-conformal order statistic. If k=n+1, no finite
            # empirical bound is available, so report the fixed cold-start rule.
            cal = self.split_calibration.get(resource_class, [])
            k = ceil((len(cal) + 1) * (1 - self.target_alpha))
            if not cal or k > len(cal):
                return FIXED
            return min(2.0, max(1.0, cal[k - 1]))
        raise ValueError(f"unknown policy {policy}")

    def summary(self, resource_class: str, policy: str = "adaptive") -> dict:
        xs = sorted(s.ratio for s in self.samples.get(resource_class, []))
        from statistics import mean

        return {
            "resource_class": resource_class,
            "sample_count": len(xs),
            "mean_ratio": mean(xs) if xs else None,
            "p95_ratio": quantile(xs, 0.95) if xs else None,
            "observed_coverage": sum(x <= self.get(resource_class, policy) for x in xs) / len(xs)
            if xs
            else None,
            "active_multiplier": self.get(resource_class, policy),
            "alpha": self.adaptive.setdefault(resource_class, AdaptiveState()).alpha,
            "limitations": [
                "synthetic/local sample status must be reported",
                "adaptive heuristic has no unconditional coverage guarantee",
            ],
        }
