"""Fraud data access: blacklist and cross-application look-ups."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..repository import Repository


class FraudDatabase(ABC):
    @abstractmethod
    def is_blacklisted(self, national_id: str) -> bool: ...

    @abstractmethod
    def recent_application_count(self, national_id: str, days: int, exclude_id: str) -> int: ...

    @abstractmethod
    def other_applicants_on_account(self, account_number: str, national_id: str) -> int: ...


class RepositoryFraudDatabase(FraudDatabase):
    """Backed by the same SQLite store as the cases themselves."""

    def __init__(self, repository: Repository):
        self._repo = repository

    def is_blacklisted(self, national_id: str) -> bool:
        return self._repo.is_blacklisted(national_id)

    def recent_application_count(self, national_id: str, days: int, exclude_id: str) -> int:
        return self._repo.recent_application_count(national_id, days, exclude_id)

    def other_applicants_on_account(self, account_number: str, national_id: str) -> int:
        return self._repo.other_applicants_on_account(account_number, national_id)
