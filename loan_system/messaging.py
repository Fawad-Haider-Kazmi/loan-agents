"""Agent-to-agent communication and the audit trail.

Pattern: orchestrator-mediated blackboard. Agents do not call each other; the
orchestrator hands each one the shared WorkflowContext and every hand-off is
recorded as an AgentMessage. That gives a complete, replayable conversation
for every case (shown in the dashboard timeline).
"""
from __future__ import annotations

from .models import AgentMessage
from .repository import Repository
from .utils import now_iso


class AuditLogger:
    def __init__(self, repository: Repository):
        self._repo = repository

    def log(self, application_id: str, actor: str, event: str, detail: str = "") -> None:
        self._repo.add_audit(application_id, actor, event, detail)


class MessageBus:
    def __init__(self, repository: Repository):
        self._repo = repository

    def publish(self, application_id: str, sender: str, recipient: str,
                kind: str, summary: str) -> AgentMessage:
        msg = AgentMessage(application_id, sender, recipient, kind, summary, now_iso())
        self._repo.add_message(msg)
        return msg

    def conversation(self, application_id: str) -> list[AgentMessage]:
        return self._repo.get_messages(application_id)
