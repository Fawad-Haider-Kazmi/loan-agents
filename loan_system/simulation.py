"""Traffic and outcome simulation, shared by the CLI feedback demo and the dashboard."""
from __future__ import annotations

import random

from .ml.risk_model import drifted_ground_truth_pd
from .samples import random_case
from .state import ApplicationStatus as S
from .system import LoanProcessingSystem


def simulate_applications(system: LoanProcessingSystem, n: int, rng: random.Random,
                          start: int = 0) -> dict[str, int]:
    """Push n random applications through the full workflow. A simulated reviewer
    approves escalated cases only when the predicted default probability is under 25%."""
    if not hasattr(system.bureau, "register"):
        raise RuntimeError("Simulation needs a bureau that supports register() (the mock)")
    for i in range(start, start + n):
        app, credit = random_case(rng, i)
        system.bureau.register(app.national_id, credit)
        ctx = system.submit(app)
        if ctx.status == S.AWAITING_APPROVAL:
            system.review(app.application_id, "Sim Reviewer", ctx.risk.default_probability < 0.25)
    cases = [c for c in system.cases() if c.application_id.startswith("SIM-")]
    return {"disbursed": sum(1 for c in cases if c.receipt),
            "rejected": sum(1 for c in cases if c.status == S.COMPLETED and not c.receipt)}


def simulate_outcomes(system: LoanProcessingSystem, rng: random.Random) -> tuple[int, int]:
    """Months later: decide which disbursed simulated loans defaulted, in a 'real world'
    that has drifted from what the bootstrap model learned. Returns (recorded, defaults)."""
    done = {f["application_id"] for f in system.repo.get_feedback()}
    recorded = defaults = 0
    for c in system.cases():
        if c.receipt and c.application_id.startswith("SIM-") and c.application_id not in done:
            defaulted = rng.random() < drifted_ground_truth_pd(c.risk.features)
            system.record_outcome(c.application_id, defaulted)
            recorded += 1
            defaults += defaulted
    return recorded, defaults


def next_sim_index(system: LoanProcessingSystem) -> int:
    return sum(1 for c in system.cases() if c.application_id.startswith("SIM-"))
