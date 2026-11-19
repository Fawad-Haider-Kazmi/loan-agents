from __future__ import annotations

from ..errors import AuthorizationError
from ..messaging import AuditLogger
from ..services.banking import CoreBankingAPI
from ..state import ApplicationStatus, WorkflowContext
from .base import BaseAgent, RetryPolicy


class ExecutionAgent(BaseAgent):
    """Moves the money. The only agent with write access to the core banking API.

    Safety design:
      * refuses to run unless the context is authorised (human approval, or a
        policy-approved straight-through case),
      * pays exactly the authorised amount (a reviewer may reduce it, never raise it),
      * uses the application id as an idempotency key, so a retry after a timeout
        cannot pay twice.
    """

    name = "ExecutionAgent"
    purpose = "Disburse the approved loan through the core banking API"

    def __init__(self, bank: CoreBankingAPI, audit: AuditLogger, retry: RetryPolicy | None = None):
        super().__init__(audit, retry)
        self._bank = bank

    def run(self, ctx: WorkflowContext) -> None:
        if ctx.status != ApplicationStatus.APPROVED or not ctx.authorized_for_execution:
            raise AuthorizationError(f"{ctx.application_id} is not authorised for disbursement")
        amount = ctx.authorized_amount
        if amount <= 0 or amount > ctx.application.requested_amount:
            raise AuthorizationError(f"Invalid authorised amount {amount}")
        if ctx.receipt is None:   # already paid (e.g. failure happened after payment) -> skip
            ctx.receipt = self._bank.disburse(ctx.application.account_number, amount, ctx.application_id)
        ctx.transition(ApplicationStatus.DISBURSED, f"transaction {ctx.receipt.transaction_id}")

    def summarize(self, ctx: WorkflowContext) -> str:
        return f"{ctx.receipt.transaction_id} amount={ctx.receipt.amount:,.0f}"
