from __future__ import annotations

from ..messaging import AuditLogger
from ..models import FraudLevel, FraudReport, FraudSignal
from ..services.fraud_database import FraudDatabase
from ..state import ApplicationStatus, WorkflowContext
from .base import BaseAgent, RetryPolicy


class FraudDetectionAgent(BaseAgent):
    """Screens for fraud indicators independently of creditworthiness.

    A person can be a good credit risk and still be a fraudster (and vice versa),
    so this runs as its own agent with its own data sources and its own output.

    Input : application, extracted data, FraudDatabase
    Output: ctx.fraud (score 0..1, level, signals, blacklisted flag)
    Decides: fraud level. A blacklisted ID is always HIGH.
    """

    name = "FraudDetectionAgent"
    purpose = "Blacklist, duplicate-application, shared-account and income-anomaly checks"

    def __init__(self, fraud_db: FraudDatabase, audit: AuditLogger, window_days: int = 30,
                 retry: RetryPolicy | None = None):
        super().__init__(audit, retry)
        self._db, self._window = fraud_db, window_days

    def run(self, ctx: WorkflowContext) -> None:
        app, ex = ctx.application, ctx.extracted
        blacklisted = self._db.is_blacklisted(app.national_id)
        duplicates = self._db.recent_application_count(app.national_id, self._window, app.application_id)
        sharing = self._db.other_applicants_on_account(app.account_number, app.national_id)

        signals: list[FraudSignal] = []
        if blacklisted:
            signals.append(FraudSignal("BLACKLISTED", "National ID is on the internal blacklist", 1.0))
        if duplicates >= 3:
            signals.append(FraudSignal("DUPLICATE_APPLICATIONS", f"{duplicates} other applications in {self._window} days", 0.6))
        elif duplicates >= 1:
            signals.append(FraudSignal("DUPLICATE_APPLICATION", f"{duplicates} other application(s) in {self._window} days", 0.3))
        if sharing:
            signals.append(FraudSignal("SHARED_ACCOUNT", "Disbursement account is used by a different applicant", 0.5))
        verified = ex.salary.monthly_salary
        if app.stated_monthly_income > 1.5 * verified:
            signals.append(FraudSignal("INCOME_ANOMALY", "Stated income is more than 1.5x the verified salary", 0.4))
        if app.requested_amount > 24 * verified:
            signals.append(FraudSignal("EXTREME_LOAN_TO_INCOME", "Requested amount exceeds 24 months of income", 0.2))

        score = min(1.0, sum(s.weight for s in signals))
        if blacklisted or score >= 0.7:
            level = FraudLevel.HIGH
        elif score >= 0.35:
            level = FraudLevel.MEDIUM
        elif score > 0:
            level = FraudLevel.LOW
        else:
            level = FraudLevel.NONE
        ctx.fraud = FraudReport(blacklisted, score, level, signals)
        ctx.transition(ApplicationStatus.FRAUD_SCREENED)

    def summarize(self, ctx: WorkflowContext) -> str:
        f = ctx.fraud
        return f"level={f.level.value} score={f.score:.2f} signals={[s.code for s in f.signals] or 'none'}"
