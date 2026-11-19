"""Outbound notifications (loan officer, applicant, operations)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Notification:
    audience: str      # "officer" | "applicant" | "operations"
    subject: str
    body: str
    application_id: str


class Notifier(ABC):
    @abstractmethod
    def send(self, notification: Notification) -> None: ...


class ConsoleNotifier(Notifier):
    def send(self, n: Notification) -> None:
        print(f"    [notify:{n.audience}] {n.subject}")


class InMemoryNotifier(Notifier):
    """Collects notifications; used by tests and the dashboard."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, n: Notification) -> None:
        self.sent.append(n)

    def for_audience(self, audience: str) -> list[Notification]:
        return [n for n in self.sent if n.audience == audience]


class EmailNotifier(Notifier):
    """Placeholder showing where SMTP / SendGrid would plug in."""

    def __init__(self, smtp_host: str, sender: str):
        self.smtp_host, self.sender = smtp_host, sender

    def send(self, n: Notification) -> None:  # pragma: no cover
        raise NotImplementedError("Wire up smtplib or an email API here")
