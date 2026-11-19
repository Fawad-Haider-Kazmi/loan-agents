"""Core-banking API interface + a mock with idempotency and failure injection."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..errors import BankingUnavailable
from ..models import DisbursementReceipt
from ..utils import now_iso


class CoreBankingAPI(ABC):
    @abstractmethod
    def disburse(self, account_number: str, amount: float, reference: str) -> DisbursementReceipt:
        """Send money. MUST be idempotent on `reference`."""

    @abstractmethod
    def get_transaction(self, transaction_id: str) -> Optional[DisbursementReceipt]:
        """Look a transaction up independently of the disburse call (for verification)."""


class MockCoreBanking(CoreBankingAPI):
    def __init__(self, fail_first: int = 0):
        self._by_reference: dict[str, DisbursementReceipt] = {}
        self._by_txn: dict[str, DisbursementReceipt] = {}
        self._fail_first = fail_first
        self.disburse_calls = 0
        self.tamper_amount_by: float = 0.0   # tests: simulate a ledger discrepancy

    def disburse(self, account_number: str, amount: float, reference: str) -> DisbursementReceipt:
        self.disburse_calls += 1
        if self._fail_first > 0:
            self._fail_first -= 1
            raise BankingUnavailable("core banking gateway timeout (simulated)")
        if reference in self._by_reference:          # idempotency: never pay twice
            return self._by_reference[reference]
        txn_id = f"TXN-{len(self._by_txn) + 1:06d}"
        booked = amount + self.tamper_amount_by      # what the bank's ledger actually holds
        ledger_entry = DisbursementReceipt(txn_id, reference, account_number, booked, "SETTLED", now_iso())
        self._by_reference[reference] = ledger_entry
        self._by_txn[txn_id] = ledger_entry
        # the caller only sees what it asked for; verification compares against the ledger
        return DisbursementReceipt(txn_id, reference, account_number, amount, "SETTLED", ledger_entry.timestamp)

    def get_transaction(self, transaction_id: str) -> Optional[DisbursementReceipt]:
        return self._by_txn.get(transaction_id)

    @property
    def total_paid_out(self) -> float:
        return sum(r.amount for r in self._by_txn.values())
