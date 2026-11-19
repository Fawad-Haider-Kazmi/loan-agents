from __future__ import annotations

from datetime import date
from difflib import SequenceMatcher
from typing import Callable

from ..messaging import AuditLogger
from ..models import Severity, ValidationIssue, ValidationReport
from ..state import ApplicationStatus, WorkflowContext
from ..utils import normalize_id, today
from .base import BaseAgent, RetryPolicy

REQUIRED = {"national_id": "identity", "salary_slip": "salary", "bank_statement": "bank_statement"}


class DataValidationAgent(BaseAgent):
    """Cross-checks extracted data against the application and against itself.

    Input : ctx.application, ctx.extracted
    Output: ctx.validation (ValidationReport: missing documents + issues)
    Decides: BLOCKING vs WARNING issue severity; missing documents end the
             flow with NEEDS_INFO (no point scoring an incomplete file).
    """

    name = "DataValidationAgent"
    purpose = "Detect missing documents and inconsistencies between documents and application"

    def __init__(self, audit: AuditLogger, income_tolerance: float = 0.10,
                 name_similarity: float = 0.85, today_fn: Callable[[], date] = today,
                 retry: RetryPolicy | None = None):
        super().__init__(audit, retry)
        self._tol, self._sim, self._today = income_tolerance, name_similarity, today_fn

    def run(self, ctx: WorkflowContext) -> None:
        app, ex = ctx.application, ctx.extracted
        report = ValidationReport()

        for doc_type, attr in REQUIRED.items():
            if getattr(ex, attr) is None:
                why = ex.parse_errors.get(doc_type)
                report.missing_documents.append(doc_type + (f" (unreadable: {why})" if why else ""))

        def add(code: str, msg: str, sev: Severity) -> None:
            report.issues.append(ValidationIssue(code, msg, sev))

        if ex.identity:
            if not self._similar(ex.identity.name, app.applicant_name):
                add("NAME_MISMATCH", f"ID name '{ex.identity.name}' differs from application '{app.applicant_name}'", Severity.BLOCKING)
            if normalize_id(ex.identity.id_number) != normalize_id(app.national_id):
                add("ID_MISMATCH", "National ID number on document differs from application", Severity.BLOCKING)
            if date.fromisoformat(ex.identity.expiry_date) < self._today():
                add("ID_EXPIRED", f"National ID expired on {ex.identity.expiry_date}", Severity.BLOCKING)
        if ex.salary:
            if not self._similar(ex.salary.employee_name, app.applicant_name):
                add("NAME_MISMATCH", f"Salary slip name '{ex.salary.employee_name}' differs from application", Severity.BLOCKING)
            if app.stated_monthly_income > ex.salary.monthly_salary * (1 + self._tol):
                add("INCOME_MISMATCH",
                    f"Stated income {app.stated_monthly_income:,.0f} exceeds salary slip {ex.salary.monthly_salary:,.0f} by more than {self._tol:.0%}",
                    Severity.BLOCKING)
            if ex.salary.employer.strip().lower() != app.employer.strip().lower():
                add("EMPLOYER_MISMATCH", f"Salary slip employer '{ex.salary.employer}' differs from application", Severity.WARNING)
        if ex.bank_statement:
            if not self._similar(ex.bank_statement.account_holder, app.applicant_name):
                add("NAME_MISMATCH", "Bank statement holder differs from application", Severity.BLOCKING)
            if ex.bank_statement.account_number != app.account_number:
                add("ACCOUNT_MISMATCH", "Bank statement account differs from the disbursement account", Severity.BLOCKING)
            if ex.salary and ex.bank_statement.avg_monthly_credit < 0.7 * ex.salary.monthly_salary:
                add("LOW_BANK_CREDITS", "Average monthly credits are well below the declared salary", Severity.WARNING)

        ctx.validation = report
        if report.missing_documents:
            ctx.transition(ApplicationStatus.NEEDS_INFO, "missing: " + ", ".join(report.missing_documents))
        else:
            ctx.transition(ApplicationStatus.VALIDATED)

    def _similar(self, a: str, b: str) -> bool:
        return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio() >= self._sim

    def summarize(self, ctx: WorkflowContext) -> str:
        v = ctx.validation
        return f"missing={v.missing_documents or 'none'}; issues={[i.code for i in v.issues] or 'none'}"
