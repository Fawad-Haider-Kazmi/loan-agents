"""Workflow state machine + the shared WorkflowContext ("blackboard").

Agents never call each other. They read the context, write their own section
of it, and ask the context to move to the next status. The context refuses
illegal moves, so a bug in one agent cannot silently skip a stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .errors import IllegalTransition
from .models import (
    DecisionRecord, DisbursementReceipt, ExtractedData, FinalReport, FraudReport,
    HumanReview, LoanApplication, Recommendation, RiskAssessment,
    ValidationReport, VerificationReport,
)
from .utils import now_iso


class ApplicationStatus(str, Enum):
    SUBMITTED = "submitted"
    DOCUMENTS_PARSED = "documents_parsed"
    VALIDATED = "validated"
    NEEDS_INFO = "needs_info"                     # terminal: applicant must resubmit
    RISK_ASSESSED = "risk_assessed"
    FRAUD_SCREENED = "fraud_screened"
    RECOMMENDED = "recommended"
    AWAITING_APPROVAL = "awaiting_approval"       # paused: human in the loop
    APPROVED = "approved"
    REJECTED = "rejected"
    DISBURSED = "disbursed"
    VERIFIED = "verified"
    RECONCILIATION_REQUIRED = "reconciliation_required"  # terminal: ops must act
    COMPLETED = "completed"
    FAILED = "failed"                             # recoverable via retry


S = ApplicationStatus
ALLOWED_TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    S.SUBMITTED: {S.DOCUMENTS_PARSED, S.FAILED},
    S.DOCUMENTS_PARSED: {S.VALIDATED, S.NEEDS_INFO, S.FAILED},
    S.VALIDATED: {S.RISK_ASSESSED, S.FAILED},
    S.NEEDS_INFO: {S.FAILED},
    S.RISK_ASSESSED: {S.FRAUD_SCREENED, S.FAILED},
    S.FRAUD_SCREENED: {S.RECOMMENDED, S.FAILED},
    S.RECOMMENDED: {S.AWAITING_APPROVAL, S.APPROVED, S.REJECTED, S.FAILED},
    S.AWAITING_APPROVAL: {S.APPROVED, S.REJECTED, S.FAILED},
    S.APPROVED: {S.DISBURSED, S.FAILED},
    S.REJECTED: {S.COMPLETED, S.FAILED},
    S.DISBURSED: {S.VERIFIED, S.RECONCILIATION_REQUIRED, S.FAILED},
    S.VERIFIED: {S.COMPLETED, S.FAILED},
    S.RECONCILIATION_REQUIRED: {S.FAILED},
    S.COMPLETED: set(),
    S.FAILED: set(),   # leaves FAILED only through WorkflowContext.recover()
}

TERMINAL = {S.COMPLETED, S.NEEDS_INFO, S.RECONCILIATION_REQUIRED, S.FAILED}


@dataclass
class StatusChange:
    from_status: str
    to_status: str
    at: str
    note: str = ""


@dataclass
class WorkflowContext:
    application: LoanApplication
    status: ApplicationStatus = ApplicationStatus.SUBMITTED
    extracted: Optional[ExtractedData] = None
    validation: Optional[ValidationReport] = None
    risk: Optional[RiskAssessment] = None
    fraud: Optional[FraudReport] = None
    decision: Optional[DecisionRecord] = None
    human_review: Optional[HumanReview] = None
    receipt: Optional[DisbursementReceipt] = None
    verification: Optional[VerificationReport] = None
    final_report: Optional[FinalReport] = None
    failed_node: Optional[str] = None
    status_before_failure: Optional[str] = None
    error: Optional[str] = None
    history: list[StatusChange] = field(default_factory=list)

    # ---------------------------------------------------------------- transitions
    def transition(self, new_status: ApplicationStatus, note: str = "") -> None:
        if new_status not in ALLOWED_TRANSITIONS[self.status]:
            raise IllegalTransition(
                f"{self.application.application_id}: {self.status.value} -> {new_status.value} is not allowed"
            )
        self.history.append(StatusChange(self.status.value, new_status.value, now_iso(), note))
        self.status = new_status

    def fail(self, node: str, error: str) -> None:
        self.failed_node = node
        self.status_before_failure = self.status.value
        self.error = error
        self.transition(ApplicationStatus.FAILED, f"{node}: {error}")

    def recover(self) -> str:
        """Leave FAILED and return the node to re-run (used by retry)."""
        if self.status != ApplicationStatus.FAILED or not self.failed_node:
            raise IllegalTransition("Only a FAILED case can be recovered")
        node = self.failed_node
        self.history.append(StatusChange(
            ApplicationStatus.FAILED.value, self.status_before_failure or "", now_iso(),
            f"manual retry of {node}"))
        self.status = ApplicationStatus(self.status_before_failure)
        self.failed_node = self.status_before_failure = self.error = None
        return node

    # ------------------------------------------------------------------ helpers
    @property
    def application_id(self) -> str:
        return self.application.application_id

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def authorized_for_execution(self) -> bool:
        """Money may move only after an explicit human approval, or when policy
        allows straight-through approval (low risk, small amount, clean checks)."""
        if self.human_review is not None:
            return self.human_review.approved
        d = self.decision
        return bool(d and d.recommendation == Recommendation.APPROVE and not d.requires_human)

    @property
    def authorized_amount(self) -> float:
        if self.human_review is not None:
            return self.human_review.approved_amount
        return self.decision.approved_amount if self.decision else 0.0
