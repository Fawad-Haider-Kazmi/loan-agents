"""Central configuration. Everything tunable lives here, not inside agents."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PolicyConfig:
    """Bank credit policy. Shared (by reference) with the DecisionAgent so the
    threshold tuner can update it at run time, but only after human approval."""

    version: str = "policy-1"
    auto_approve_limit: float = 500_000      # above this a human must approve
    max_dti: float = 0.50                    # debt-to-income ceiling
    reject_pd: float = 0.35                  # default probability: auto-reject
    approve_pd: float = 0.25                 # below this: eligible to approve
    band_low_pd: float = 0.10                # risk band boundaries
    band_high_pd: float = 0.25


@dataclass
class SystemConfig:
    db_path: str = "data/loan_system.db"
    model_path: str = "data/risk_model.json"
    annual_interest_rate: float = 0.18
    fraud_window_days: int = 30
    income_tolerance: float = 0.10           # stated vs verified income
    retry_attempts: int = 3
    retry_base_delay: float = 0.2            # seconds, doubled each attempt
    min_feedback_samples: int = 40           # before retraining is allowed
    policy: PolicyConfig = field(default_factory=PolicyConfig)
