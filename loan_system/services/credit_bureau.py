"""Credit bureau API client interface + an in-memory mock with failure injection."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..errors import CreditBureauUnavailable
from ..models import CreditReport
from ..utils import normalize_id


class CreditBureauAPI(ABC):
    @abstractmethod
    def get_report(self, national_id: str) -> CreditReport:
        """Return the applicant's credit report. May raise CreditBureauUnavailable."""


class MockCreditBureau(CreditBureauAPI):
    def __init__(self, reports: Optional[dict[str, CreditReport]] = None,
                 fail_first: Optional[dict[str, int]] = None):
        self._reports = {normalize_id(k): v for k, v in (reports or {}).items()}
        self._fail_first = {normalize_id(k): v for k, v in (fail_first or {}).items()}
        self.calls = 0
        self.outage = False   # tests flip this to simulate a hard outage

    def register(self, national_id: str, report: CreditReport) -> None:
        self._reports[normalize_id(national_id)] = report

    def unregister(self, national_id: str) -> None:
        """Forget this person, so the bureau answers 'no history' (thin file)."""
        self._reports.pop(normalize_id(national_id), None)

    def get_report(self, national_id: str) -> CreditReport:
        self.calls += 1
        key = normalize_id(national_id)
        if self.outage:
            raise CreditBureauUnavailable("credit bureau outage (simulated)")
        remaining = self._fail_first.get(key, 0)
        if remaining > 0:
            self._fail_first[key] = remaining - 1
            raise CreditBureauUnavailable("credit bureau timed out (simulated)")
        if key in self._reports:
            return self._reports[key]
        # "no hit" at the bureau = thin file, not an error
        return CreditReport(national_id=key, score=600, active_loans=0, monthly_obligations=0.0,
                            delinquencies=0, utilization=0.0, thin_file=True)
