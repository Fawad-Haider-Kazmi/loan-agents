"""Default-probability model: logistic regression implemented from scratch
(pure Python, no heavy dependency) so every step is explainable.

Features (all computed by CreditRiskAgent):
    dti                    (existing obligations + new EMI) / verified income
    credit_score_norm      (bureau score - 600) / 150
    loan_to_annual_income  requested amount / (12 * verified income)
    delinquencies          missed payments on the bureau file
    utilization            revolving credit utilisation, 0..1
"""
from __future__ import annotations

import json
import math
import os
import random
from typing import Optional, Sequence

FEATURES = ["dti", "credit_score_norm", "loan_to_annual_income", "delinquencies", "utilization"]

# The "real world" the synthetic data is drawn from. Used for bootstrap training
# data and, in the feedback demo, to sample which loans actually default.
_TRUE_COEF = {"dti": 3.2, "credit_score_norm": -1.6, "loan_to_annual_income": 0.5,
              "delinquencies": 0.45, "utilization": 0.9}
_TRUE_BIAS = -3.4


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def ground_truth_pd(features: dict[str, float]) -> float:
    return _sigmoid(_TRUE_BIAS + sum(_TRUE_COEF[k] * features[k] for k in FEATURES))


# A shifted "real world" used by the feedback demo to show learning from outcomes:
# utilisation and delinquencies matter more than the bootstrap model believes.
_DRIFT_COEF = dict(_TRUE_COEF, utilization=2.5, delinquencies=0.8)


def drifted_ground_truth_pd(features: dict[str, float]) -> float:
    return _sigmoid(_TRUE_BIAS - 0.3 + sum(_DRIFT_COEF[k] * features[k] for k in FEATURES))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def synthetic_features(rng: random.Random) -> dict[str, float]:
    score = _clamp(rng.gauss(650, 90), 300, 850)
    return {
        "dti": _clamp(rng.gauss(0.35, 0.18), 0.02, 1.2),
        "credit_score_norm": (score - 600) / 150,
        "loan_to_annual_income": _clamp(rng.gauss(0.8, 0.5), 0.02, 3.0),
        "delinquencies": float(min(6, int(rng.expovariate(1.2)))),
        "utilization": _clamp(rng.gauss(0.4, 0.22), 0.0, 1.0),
    }


def synthetic_dataset(n: int = 3000, seed: int = 7) -> tuple[list[list[float]], list[int]]:
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        f = synthetic_features(rng)
        X.append([f[k] for k in FEATURES])
        y.append(1 if rng.random() < ground_truth_pd(f) else 0)
    return X, y


def auc(y: Sequence[int], p: Sequence[float]) -> float:
    pairs = sorted(zip(p, y))
    pos = sum(y)
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):                       # average ranks over ties
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2
        rank_sum += avg_rank * sum(1 for k in range(i, j) if pairs[k][1] == 1)
        i = j
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


class LogisticRiskModel:
    def __init__(self, version: str = "v1"):
        self.version = version
        self.weights = [0.0] * len(FEATURES)
        self.bias = 0.0
        self.means = [0.0] * len(FEATURES)
        self.stds = [1.0] * len(FEATURES)
        self.trained_on = 0

    # ------------------------------------------------------------------ training
    def fit(self, X: Sequence[Sequence[float]], y: Sequence[int],
            sample_weights: Optional[Sequence[float]] = None,
            epochs: int = 300, lr: float = 0.5, l2: float = 1e-3) -> "LogisticRiskModel":
        n, d = len(X), len(FEATURES)
        w_s = list(sample_weights) if sample_weights is not None else [1.0] * n
        total_w = sum(w_s)
        self.means = [sum(row[j] for row in X) / n for j in range(d)]
        self.stds = [max(1e-9, math.sqrt(sum((row[j] - self.means[j]) ** 2 for row in X) / n))
                     for j in range(d)]
        Z = [[(row[j] - self.means[j]) / self.stds[j] for j in range(d)] for row in X]
        w, b = [0.0] * d, 0.0
        for _ in range(epochs):
            grad_w, grad_b = [0.0] * d, 0.0
            for zi, yi, si in zip(Z, y, w_s):
                err = (_sigmoid(b + sum(wj * zj for wj, zj in zip(w, zi))) - yi) * si
                grad_b += err
                for j in range(d):
                    grad_w[j] += err * zi[j]
            b -= lr * grad_b / total_w
            for j in range(d):
                w[j] -= lr * (grad_w[j] / total_w + l2 * w[j])
        self.weights, self.bias, self.trained_on = w, b, n
        return self

    # ----------------------------------------------------------------- inference
    def predict_proba(self, features: dict[str, float]) -> float:
        z = self.bias
        for j, name in enumerate(FEATURES):
            z += self.weights[j] * (features[name] - self.means[j]) / self.stds[j]
        return _sigmoid(z)

    def predict_rows(self, X: Sequence[Sequence[float]]) -> list[float]:
        return [self.predict_proba(dict(zip(FEATURES, row))) for row in X]

    def evaluate(self, X: Sequence[Sequence[float]], y: Sequence[int]) -> dict[str, float]:
        p = self.predict_rows(X)
        eps = 1e-12
        ll = -sum(yi * math.log(max(pi, eps)) + (1 - yi) * math.log(max(1 - pi, eps))
                  for yi, pi in zip(y, p)) / len(y)
        return {"log_loss": ll, "auc": auc(y, p), "n": float(len(y))}

    # --------------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        return {"version": self.version, "weights": self.weights, "bias": self.bias,
                "means": self.means, "stds": self.stds, "trained_on": self.trained_on,
                "features": FEATURES}

    @classmethod
    def from_dict(cls, data: dict) -> "LogisticRiskModel":
        m = cls(data["version"])
        m.weights, m.bias = data["weights"], data["bias"]
        m.means, m.stds, m.trained_on = data["means"], data["stds"], data["trained_on"]
        return m

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)

    @classmethod
    def load_or_train(cls, path: str, n: int = 3000) -> "LogisticRiskModel":
        if os.path.exists(path):
            with open(path) as fh:
                return cls.from_dict(json.load(fh))
        X, y = synthetic_dataset(n)
        model = cls("v1").fit(X, y)
        model.save(path)
        return model


class ModelRegistry:
    """Holds the *live* model. Agents read `.current` on every call, so promoting
    a retrained model takes effect immediately without rebuilding any agent."""

    def __init__(self, model: LogisticRiskModel):
        self.current = model
        self.history: list[str] = [model.version]

    def promote(self, candidate: LogisticRiskModel) -> None:
        self.current = candidate
        self.history.append(candidate.version)
