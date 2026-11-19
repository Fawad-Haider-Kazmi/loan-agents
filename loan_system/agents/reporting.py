from __future__ import annotations

from typing import Optional

from ..config import PolicyConfig
from ..messaging import AuditLogger
from ..ml.feedback_learning import ModelRetrainer, RetrainResult, ThresholdSuggestion, ThresholdTuner
from ..ml.risk_model import ModelRegistry
from ..models import FinalReport
from ..notifier import Notification, Notifier
from ..repository import Repository
from ..state import ApplicationStatus, WorkflowContext
from ..utils import money
from .base import BaseAgent, RetryPolicy


class ReportingFeedbackAgent(BaseAgent):
    """Agent 8 has two modes.

    1. In-workflow (run): close every case with an internal summary and an
       applicant notice. The notice contains only applicant-safe reasons; fraud
       signals and blacklist hits are never disclosed to the customer.
    2. Offline feedback (record_repayment_outcome / propose_*): months later,
       log whether a loan was repaid or defaulted, then PROPOSE a retrained
       model and new thresholds. Proposals go live only after a human promotes
       them, so learning stays inside the human-oversight boundary.
    """

    name = "ReportingFeedbackAgent"
    purpose = "Close cases, notify applicants, and learn from repayment outcomes"

    def __init__(self, notifier: Notifier, audit: AuditLogger, repository: Repository,
                 models: ModelRegistry, policy: PolicyConfig, min_feedback_samples: int = 40,
                 retrainer: Optional[ModelRetrainer] = None, tuner: Optional[ThresholdTuner] = None,
                 retry: RetryPolicy | None = None):
        super().__init__(audit, retry)
        self._notifier, self._repo, self._models, self._policy = notifier, repository, models, policy
        self._min_samples = min_feedback_samples
        self._retrainer = retrainer or ModelRetrainer()
        self._tuner = tuner or ThresholdTuner()

    # ------------------------------------------------------------ feedback mode
    def record_repayment_outcome(self, ctx: WorkflowContext, defaulted: bool) -> None:
        if ctx.receipt is None or ctx.risk is None:
            raise ValueError("Only disbursed loans have a repayment outcome")
        self._repo.add_feedback(ctx.application_id, ctx.risk.features, ctx.risk.default_probability,
                                defaulted, ctx.receipt.amount)
        self._audit.log(ctx.application_id, self.name, "repayment_outcome_recorded",
                        "defaulted" if defaulted else "repaid")

    def propose_retraining(self) -> Optional[RetrainResult]:
        rows = self._repo.get_feedback()
        if len(rows) < self._min_samples:
            return None
        return self._retrainer.retrain(self._models.current, rows)

    def propose_threshold(self) -> Optional[ThresholdSuggestion]:
        return self._tuner.suggest(self._repo.get_feedback(), self._policy.reject_pd)

    def run(self, ctx: WorkflowContext) -> None:
        s, app = ctx.status, ctx.application
        if s == ApplicationStatus.NEEDS_INFO:
            outcome = "needs_info"
            notice = ("We could not complete your loan application because the following documents are "
                      "missing or unreadable: " + ", ".join(ctx.validation.missing_documents) +
                      ". Please resubmit them and we will continue.")
        elif s == ApplicationStatus.REJECTED:
            outcome = "rejected"
            notice = ("Thank you for applying. After review we are unable to offer you this loan. "
                      + self._applicant_safe_reason(ctx) + " You may reapply or request a manual review.")
        elif s in (ApplicationStatus.VERIFIED, ApplicationStatus.RECONCILIATION_REQUIRED):
            outcome = "approved" if s == ApplicationStatus.VERIFIED else "approved_pending_reconciliation"
            notice = (f"Your loan of {money(ctx.receipt.amount)} has been approved and sent to account "
                      f"{app.account_number} (reference {ctx.receipt.transaction_id}).")
        else:  # pragma: no cover
            outcome, notice = s.value, ""

        ctx.final_report = FinalReport(outcome, self._summary(ctx, outcome), notice)
        self._notifier.send(Notification("applicant", f"Your loan application {app.application_id}",
                                         notice, ctx.application_id))
        if s in (ApplicationStatus.REJECTED, ApplicationStatus.VERIFIED):
            ctx.transition(ApplicationStatus.COMPLETED)

    @staticmethod
    def _applicant_safe_reason(ctx: WorkflowContext) -> str:
        if ctx.human_review and not ctx.human_review.approved:
            return "Our credit team reviewed your application and could not approve it at this time."
        d, f = ctx.decision, ctx.fraud
        if f and (f.blacklisted or f.level.value in ("medium", "high")):
            return "We are unable to proceed with your application at this time."
        text = " ".join(d.reasons)
        if "Debt-to-income" in text:
            return "The requested instalment is too high relative to your verified income."
        return "Our credit assessment did not meet the lending criteria for this amount."

    @staticmethod
    def _summary(ctx: WorkflowContext, outcome: str) -> str:
        parts = [f"{ctx.application_id} ({ctx.application.applicant_name}): {outcome.upper()}."]
        if ctx.risk:
            parts.append(f"PD {ctx.risk.default_probability:.1%}, DTI {ctx.risk.dti:.0%}, score {ctx.risk.credit_report.score}.")
        if ctx.decision:
            parts.append(f"Agent recommendation: {ctx.decision.recommendation.value}.")
        if ctx.human_review:
            parts.append(f"Human review by {ctx.human_review.reviewer}: {'approved' if ctx.human_review.approved else 'rejected'}.")
        if ctx.receipt:
            parts.append(f"Disbursed {money(ctx.receipt.amount)} ({ctx.receipt.transaction_id}).")
        return " ".join(parts)

    def summarize(self, ctx: WorkflowContext) -> str:
        return ctx.final_report.outcome
