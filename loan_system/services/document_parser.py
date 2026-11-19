"""Document parsing. In production this wraps OCR (Tesseract / AWS Textract /
a vision LLM). Here documents arrive as text, in "Key: Value" lines, so the
rest of the system is exercised end to end without any OCR dependency."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..errors import DocumentParseError
from ..models import (
    BankStatementData, Document, DocumentType, IdentityData, SalaryData,
)


class DocumentParser(ABC):
    @abstractmethod
    def parse(self, document: Document):
        """Return IdentityData | SalaryData | BankStatementData or raise DocumentParseError."""


class KeyValueDocumentParser(DocumentParser):
    def parse(self, document: Document):
        fields = self._read_fields(document.raw_text)
        try:
            if document.doc_type == DocumentType.NATIONAL_ID:
                data = IdentityData(
                    name=self._get(fields, "name"),
                    id_number=self._get(fields, "id number"),
                    date_of_birth=self._date(fields, "date of birth"),
                    expiry_date=self._date(fields, "expiry"))
            elif document.doc_type == DocumentType.SALARY_SLIP:
                data = SalaryData(
                    employer=self._get(fields, "employer"),
                    employee_name=self._get(fields, "employee"),
                    monthly_salary=self._number(fields, "monthly salary"),
                    month=self._get(fields, "month"))
            elif document.doc_type == DocumentType.BANK_STATEMENT:
                data = BankStatementData(
                    account_holder=self._get(fields, "account holder"),
                    account_number=self._get(fields, "account number"),
                    avg_monthly_balance=self._number(fields, "average monthly balance"),
                    avg_monthly_credit=self._number(fields, "average monthly credit"))
            else:  # pragma: no cover
                raise DocumentParseError(f"Unsupported document type {document.doc_type}")
        except KeyError as exc:
            raise DocumentParseError(f"{document.filename}: missing field {exc.args[0]!r}") from exc
        return data

    @staticmethod
    def _read_fields(text: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in text.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()
        return fields

    @staticmethod
    def _get(fields: dict[str, str], key: str) -> str:
        value = fields.get(key, "")
        if not value:
            raise KeyError(key)
        return value

    def _number(self, fields: dict[str, str], key: str) -> float:
        raw = self._get(fields, key)
        try:
            return float(raw.replace(",", ""))
        except ValueError as exc:
            raise DocumentParseError(f"Field {key!r} is not a number: {raw!r}") from exc

    def _date(self, fields: dict[str, str], key: str) -> str:
        raw = self._get(fields, key)
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError as exc:
            raise DocumentParseError(f"Field {key!r} is not an ISO date: {raw!r}") from exc
