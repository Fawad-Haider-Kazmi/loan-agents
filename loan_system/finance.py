from __future__ import annotations


def monthly_payment(principal: float, annual_rate: float, months: int) -> float:
    """Standard amortised instalment (EMI)."""
    if months <= 0:
        raise ValueError("months must be positive")
    r = annual_rate / 12
    if r == 0:
        return principal / months
    growth = (1 + r) ** months
    return principal * r * growth / (growth - 1)
