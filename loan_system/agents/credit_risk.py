from __future__ import annotations

from ..config import PolicyConfig
from ..messaging import AuditLogger
from ..ml.risk_model import ModelRegistry
from ..models import RiskAssessment, RiskBand
from ..finance import monthly_payment
from ..services.credit_bureau import CreditBureauAPI
from ..state import ApplicationStatus, WorkflowContext
from .base import BaseAgent, RetryPolicy


class CreditRiskAgent(BaseAgent):
    """Quantifies how likely the applicant is to default.

    Input : verified income (from the salary slip, NOT the stated income),
            requested amount/tenure, credit bureau report
    Output: ctx.risk (credit report, EMI, DTI, default probability, risk band)
    Tools : CreditBureauAPI, ML model (via ModelRegistry)
    Decides: risk band only. It never approves or rejects; that is the
             DecisionAgent's job, so risk modelling and policy stay separate.
    """

    name = "CreditRiskAgent"
    purpose = "Compute DTI and default probability from bureau data and the ML model"

    def __init__(self, bureau: CreditBureauAPI, models: ModelRegistry, policy: PolicyConfig,
                 annual_rate: float, audit: AuditLogger, retry: RetryPolicy | None = None):
        super().__init__(audit, retry)
        self._bureau, self._models, self._policy, self._rate = bureau, models, policy, annual_rate

    def run(self, ctx: WorkflowContext) -> None:
        app = ctx.application
        report = self._bureau.get_report(app.national_id)        # external I/O first
        income = ctx.extracted.salary.monthly_salary
        emi = monthly_payment(app.requested_amount, self._rate, app.tenure_months)
        dti = (report.monthly_obligations + emi) / income
        features = {
            "dti": dti,
            "credit_score_norm": (report.score - 600) / 150,
            "loan_to_annual_income": app.requested_amount / (12 * income),
            "delinquencies": float(report.delinquencies),
            "utilization": report.utilization,
        }
        model = self._models.current
        pd_ = model.predict_proba(features)
        if pd_ < self._policy.band_low_pd:
            band = RiskBand.LOW
        elif pd_ < self._policy.band_high_pd:
            band = RiskBand.MEDIUM
        else:
            band = RiskBand.HIGH
        ctx.risk = RiskAssessment(report, income, emi, dti, pd_, band, features, model.version)
        ctx.transition(ApplicationStatus.RISK_ASSESSED)

    def summarize(self, ctx: WorkflowContext) -> str:
        r = ctx.risk
        return f"score={r.credit_report.score} dti={r.dti:.0%} pd={r.default_probability:.1%} band={r.risk_band.value}"
