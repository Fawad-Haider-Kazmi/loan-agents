from __future__ import annotations

from ..errors import DocumentParseError
from ..messaging import AuditLogger
from ..models import BankStatementData, DocumentType, ExtractedData, IdentityData, SalaryData
from ..services.document_parser import DocumentParser
from ..state import ApplicationStatus, WorkflowContext
from .base import BaseAgent, RetryPolicy


class DocumentIntakeAgent(BaseAgent):
    """Turns uploaded documents into structured data.

    Input : application.documents (raw text from OCR)
    Output: ctx.extracted (ExtractedData)
    Tools : DocumentParser (OCR / extraction service)
    Decides: whether a document is readable. Unreadable != fatal: it is recorded
             and the validation agent treats it as missing.
    """

    name = "DocumentIntakeAgent"
    purpose = "Extract structured fields from ID, salary slip and bank statement"

    def __init__(self, parser: DocumentParser, audit: AuditLogger, retry: RetryPolicy | None = None):
        super().__init__(audit, retry)
        self._parser = parser

    def run(self, ctx: WorkflowContext) -> None:
        extracted = ExtractedData()
        for doc in ctx.application.documents:
            try:
                data = self._parser.parse(doc)
            except DocumentParseError as exc:
                extracted.parse_errors[doc.doc_type.value] = str(exc)
                continue
            if doc.doc_type == DocumentType.NATIONAL_ID and isinstance(data, IdentityData):
                extracted.identity = data
            elif doc.doc_type == DocumentType.SALARY_SLIP and isinstance(data, SalaryData):
                extracted.salary = data
            elif doc.doc_type == DocumentType.BANK_STATEMENT and isinstance(data, BankStatementData):
                extracted.bank_statement = data
        ctx.extracted = extracted
        ctx.transition(ApplicationStatus.DOCUMENTS_PARSED)

    def summarize(self, ctx: WorkflowContext) -> str:
        e = ctx.extracted
        got = [n for n, v in (("id", e.identity), ("salary", e.salary), ("bank", e.bank_statement)) if v]
        return f"parsed {', '.join(got) or 'nothing'}; unreadable: {list(e.parse_errors) or 'none'}"
