"""BaseAgent: the template every agent follows.

execute() = audit + retry-on-transient-error around the agent's own run().
Because run() may be called more than once, every agent obeys one rule:
**do external I/O first, mutate the context last**, so a retry never sees
half-applied state.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from ..errors import TransientError
from ..messaging import AuditLogger
from ..state import WorkflowContext


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.2
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def delay_for(self, attempt: int) -> float:
        return self.base_delay * (2 ** (attempt - 1))


class BaseAgent(ABC):
    name: str = "agent"
    purpose: str = ""

    def __init__(self, audit: AuditLogger, retry_policy: RetryPolicy | None = None):
        self._audit = audit
        self._retry = retry_policy or RetryPolicy()

    @abstractmethod
    def run(self, ctx: WorkflowContext) -> None:
        """Do the work: read the context, call tools, write this agent's output section."""

    def summarize(self, ctx: WorkflowContext) -> str:
        """One line describing the result; becomes the agent's RESULT message."""
        return "done"

    def execute(self, ctx: WorkflowContext) -> None:
        self._audit.log(ctx.application_id, self.name, "started")
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                self.run(ctx)
                self._audit.log(ctx.application_id, self.name, "completed", self.summarize(ctx))
                return
            except TransientError as exc:
                self._audit.log(ctx.application_id, self.name, "transient_error",
                                f"attempt {attempt}/{self._retry.max_attempts}: {exc}")
                if attempt == self._retry.max_attempts:
                    raise
                self._retry.sleep(self._retry.delay_for(attempt))
