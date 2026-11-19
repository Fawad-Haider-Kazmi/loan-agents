from __future__ import annotations

from ..messaging import AuditLogger
from ..models import VerificationReport
from ..notifier import Notification, Notifier
from ..services.banking import CoreBankingAPI
from ..state import ApplicationStatus, WorkflowContext
from ..utils import now_iso
from .base import BaseAgent, RetryPolicy


class VerificationAgent(BaseAgent):
    """Independently confirms the disbursement happened as intended.

    It does not trust the ExecutionAgent's receipt: it re-reads the transaction
    from the bank and compares amount, account, reference and status against
    what was authorised. Any mismatch stops the case for human reconciliation.
    """

    name = "VerificationAgent"
    purpose = "Reconcile the bank ledger against what was authorised"

    def __init__(self, bank: CoreBankingAPI, notifier: Notifier, audit: AuditLogger,
                 retry: RetryPolicy | None = None):
        super().__init__(audit, retry)
        self._bank, self._notifier = bank, notifier

    def run(self, ctx: WorkflowContext) -> None:
        receipt = ctx.receipt
        booked = self._bank.get_transaction(receipt.transaction_id)
        problems: list[str] = []
        if booked is None:
            problems.append("transaction not found in bank ledger")
        else:
            if booked.status != "SETTLED":
                problems.append(f"status is {booked.status}, expected SETTLED")
            if abs(booked.amount - ctx.authorized_amount) > 0.01:
                problems.append(f"ledger amount {booked.amount:,.2f} differs from authorised {ctx.authorized_amount:,.2f}")
            if booked.account_number != ctx.application.account_number:
                problems.append("ledger account differs from the application account")
            if booked.reference != ctx.application_id:
                problems.append("ledger reference differs from the application id")
        ctx.verification = VerificationReport(not problems, problems, now_iso())
        if problems:
            ctx.transition(ApplicationStatus.RECONCILIATION_REQUIRED, "; ".join(problems))
            self._notifier.send(Notification("operations", f"Reconciliation needed: {ctx.application_id}",
                                             "; ".join(problems), ctx.application_id))
        else:
            ctx.transition(ApplicationStatus.VERIFIED)

    def summarize(self, ctx: WorkflowContext) -> str:
        v = ctx.verification
        return "ledger matches" if v.matched else "MISMATCH: " + "; ".join(v.discrepancies)
