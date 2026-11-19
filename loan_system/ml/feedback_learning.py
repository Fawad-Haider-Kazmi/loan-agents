"""Closing the loop: learn from real repayment outcomes.

Both tools only *propose* changes. Nothing goes live until a human calls
LoanProcessingSystem.promote_model(...) / apply_threshold(...). That keeps the
learning loop inside the same human-oversight boundary as the rest of the system.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .risk_model import FEATURES, LogisticRiskModel, synthetic_dataset


@dataclass
class RetrainResult:
    candidate: LogisticRiskModel
    current_metrics: dict
    candidate_metrics: dict
    feedback_used: int
    holdout_size: int

    @property
    def improved(self) -> bool:
        return self.candidate_metrics["log_loss"] < self.current_metrics["log_loss"]


class ModelRetrainer:
    def __init__(self, base_samples: int = 2000, feedback_weight: float = 5.0, epochs: int = 300):
        self.base_samples = base_samples
        self.feedback_weight = feedback_weight   # real outcomes count more than synthetic rows
        self.epochs = epochs

    def retrain(self, current: LogisticRiskModel, feedback: list[dict]) -> RetrainResult:
        X_fb = [[row["features"][k] for k in FEATURES] for row in feedback]
        y_fb = [1 if row["defaulted"] else 0 for row in feedback]
        # deterministic ~30% hold-out so we can compare old vs new on unseen real outcomes
        hold = [i for i in range(len(feedback)) if i % 10 < 3]
        train = [i for i in range(len(feedback)) if i % 10 >= 3]
        X_base, y_base = synthetic_dataset(self.base_samples, seed=11)
        X = X_base + [X_fb[i] for i in train]
        y = y_base + [y_fb[i] for i in train]
        w = [1.0] * len(X_base) + [self.feedback_weight] * len(train)
        candidate = LogisticRiskModel(f"{current.version.split('+')[0]}+fb{len(feedback)}").fit(
            X, y, sample_weights=w, epochs=self.epochs)
        X_h, y_h = [X_fb[i] for i in hold], [y_fb[i] for i in hold]
        return RetrainResult(candidate, current.evaluate(X_h, y_h), candidate.evaluate(X_h, y_h),
                             len(train), len(hold))


@dataclass
class ThresholdSuggestion:
    current: float
    suggested: float
    cost_at_current: float
    cost_at_suggested: float
    samples: int
    note: str = ""


class ThresholdTuner:
    """Picks the auto-reject PD threshold that minimises expected cost on
    observed loans: losses on approved defaults vs. margin lost on rejected good loans.

    Caveat (also in the README): we only observe outcomes for loans we approved,
    so this is biased toward being *more* conservative, never more lenient. The
    per-cycle change is capped for the same reason."""

    def __init__(self, loss_given_default: float = 0.6, margin: float = 0.12, max_step: float = 0.10):
        self.lgd, self.margin, self.max_step = loss_given_default, margin, max_step

    def _cost(self, rows: list[dict], threshold: float) -> float:
        cost = 0.0
        for r in rows:
            approved = r["predicted_pd"] < threshold
            if approved and r["defaulted"]:
                cost += r["approved_amount"] * self.lgd
            elif not approved and not r["defaulted"]:
                cost += r["approved_amount"] * self.margin
        return cost

    def suggest(self, rows: list[dict], current_threshold: float) -> Optional[ThresholdSuggestion]:
        if not rows:
            return None
        grid = [round(0.10 + 0.05 * i, 2) for i in range(11)]  # 0.10 .. 0.60
        lo, hi = current_threshold - self.max_step, current_threshold + self.max_step
        candidates = [t for t in grid if lo - 1e-9 <= t <= hi + 1e-9] or [current_threshold]
        best = min(candidates, key=lambda t: (self._cost(rows, t), abs(t - current_threshold)))
        return ThresholdSuggestion(
            current_threshold, best, self._cost(rows, current_threshold), self._cost(rows, best),
            len(rows), note=f"change capped at +/-{self.max_step:.2f} per cycle")
