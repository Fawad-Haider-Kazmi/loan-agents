"""The orchestrator: a small explicit graph runner (LangGraph-style).

    intake -> validation -> risk -> fraud -> decision -> approval_gate
                                                  |-- PAUSE (human) --> resume
                                                  |-- execution -> verification -> reporting -> END
                                                  `-- reporting (rejected)

* Nodes are agents (or the non-agent `approval_gate`).
* Edges are fixed or conditional (a router function reads the context).
* After every node the context is persisted, so a crash, a pause for human
  approval, or a failure can be resumed from exactly where it stopped.
"""
from __future__ import annotations

from typing import Callable, Optional

from .agents.base import BaseAgent
from .errors import IllegalTransition
from .messaging import AuditLogger, MessageBus
from .models import HumanReview, LoanApplication, Recommendation
from .notifier import Notification, Notifier
from .repository import Repository
from .state import ApplicationStatus, WorkflowContext
from .utils import now_iso

END = "__end__"
PAUSE = "__pause__"
ORCH = "Orchestrator"
OFFICER = "LoanOfficer"

Handler = Callable[[WorkflowContext], None]
Router = Callable[[WorkflowContext], str]


class WorkflowGraph:
    """Directed graph of named nodes with fixed or conditional edges."""

    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self.routers: dict[str, Router] = {}
        self.entry: Optional[str] = None

    def add_node(self, name: str, handler: Handler) -> "WorkflowGraph":
        self.handlers[name] = handler
        self.entry = self.entry or name
        return self

    def add_edge(self, src: str, dst: str) -> "WorkflowGraph":
        self.routers[src] = lambda ctx, d=dst: d
        return self

    def add_conditional_edges(self, src: str, router: Router) -> "WorkflowGraph":
        self.routers[src] = router
        return self

    def run_node(self, name: str, ctx: WorkflowContext) -> None:
        self.handlers[name](ctx)

    def next_node(self, name: str, ctx: WorkflowContext) -> str:
        return self.routers[name](ctx)


class LoanWorkflow:
    def __init__(self, repo: Repository, bus: MessageBus, audit: AuditLogger, notifier: Notifier,
                 agents: dict[str, BaseAgent]):
        self._repo, self._bus, self._audit, self._notifier = repo, bus, audit, notifier
        self._agents = agents
        self.graph = self._build_graph()

    @property
    def agents(self) -> dict[str, BaseAgent]:
        return dict(self._agents)

    # -------------------------------------------------------------------- graph
    def _build_graph(self) -> WorkflowGraph:
        g = WorkflowGraph()
        for node in ("intake", "validation", "risk", "fraud", "decision"):
            g.add_node(node, self._agent_handler(node))
        g.add_node("approval_gate", self._approval_gate)
        for node in ("execution", "verification", "reporting"):
            g.add_node(node, self._agent_handler(node))

        g.add_edge("intake", "validation")
        g.add_conditional_edges(
            "validation", lambda c: "reporting" if c.status == ApplicationStatus.NEEDS_INFO else "risk")
        g.add_edge("risk", "fraud")
        g.add_edge("fraud", "decision")
        g.add_edge("decision", "approval_gate")
        g.add_conditional_edges("approval_gate", self._route_after_gate)
        g.add_edge("execution", "verification")
        g.add_edge("verification", "reporting")
        g.add_edge("reporting", END)
        return g

    def _agent_handler(self, node: str) -> Handler:
        def handler(ctx: WorkflowContext) -> None:
            agent = self._agents[node]
            self._bus.publish(ctx.application_id, ORCH, agent.name, "TASK", f"run {node}")
            agent.execute(ctx)
            self._bus.publish(ctx.application_id, agent.name, ORCH, "RESULT", agent.summarize(ctx))
        return handler

    def _approval_gate(self, ctx: WorkflowContext) -> None:
        d = ctx.decision
        if d.recommendation == Recommendation.REJECT:
            ctx.transition(ApplicationStatus.REJECTED, "policy rejection")
        elif d.requires_human:
            ctx.transition(ApplicationStatus.AWAITING_APPROVAL, "human review required")
            self._bus.publish(ctx.application_id, ORCH, OFFICER, "HUMAN_REQUEST",
                              f"{d.recommendation.value.upper()}: " + " ".join(d.reasons))
            self._notifier.send(Notification(
                "officer", f"Approval needed: {ctx.application_id}", d.explanation, ctx.application_id))
        else:
            ctx.transition(ApplicationStatus.APPROVED, "straight-through approval by policy")

    @staticmethod
    def _route_after_gate(ctx: WorkflowContext) -> str:
        return {ApplicationStatus.AWAITING_APPROVAL: PAUSE,
                ApplicationStatus.APPROVED: "execution",
                ApplicationStatus.REJECTED: "reporting"}[ctx.status]

    # ---------------------------------------------------------------- public API
    def start(self, application: LoanApplication) -> WorkflowContext:
        if self._repo.exists(application.application_id):
            raise ValueError(f"Application {application.application_id} already exists")
        application.submitted_at = application.submitted_at or now_iso()
        ctx = WorkflowContext(application=application)
        self._repo.save_context(ctx)
        self._audit.log(ctx.application_id, "System", "case_submitted", application.applicant_name)
        return self._run_from(ctx, "intake")

    def apply_human_review(self, application_id: str, reviewer: str, approved: bool,
                           notes: str = "", approved_amount: Optional[float] = None) -> WorkflowContext:
        ctx = self._repo.load_context(application_id)
        if ctx.status != ApplicationStatus.AWAITING_APPROVAL:
            raise ValueError(f"{application_id} is {ctx.status.value}, not awaiting approval")
        if not reviewer.strip():
            raise ValueError("A reviewer name is required for the audit trail")
        requested = ctx.application.requested_amount
        amount = ctx.decision.approved_amount if approved_amount is None else approved_amount
        if approved and not (0 < amount <= requested):
            raise ValueError(f"Approved amount must be between 0 and the requested {requested:,.0f}")
        ctx.human_review = HumanReview(reviewer.strip(), approved, notes, amount if approved else 0.0, now_iso())
        ctx.transition(ApplicationStatus.APPROVED if approved else ApplicationStatus.REJECTED,
                       f"human review by {reviewer.strip()}")
        self._audit.log(application_id, reviewer.strip(), "human_review",
                        f"{'approved' if approved else 'rejected'} {amount:,.0f}; {notes}")
        self._bus.publish(application_id, OFFICER, ORCH, "HUMAN_RESPONSE",
                          f"{'APPROVED' if approved else 'REJECTED'} by {reviewer.strip()}")
        self._repo.save_context(ctx)
        return self._run_from(ctx, self.graph.next_node("approval_gate", ctx))

    def retry_failed(self, application_id: str) -> WorkflowContext:
        ctx = self._repo.load_context(application_id)
        node = ctx.recover()
        self._audit.log(application_id, "Operator", "retry", f"re-running {node}")
        self._repo.save_context(ctx)
        return self._run_from(ctx, node)

    # ------------------------------------------------------------------- runner
    def _run_from(self, ctx: WorkflowContext, node: str) -> WorkflowContext:
        while node not in (END, PAUSE):
            try:
                self.graph.run_node(node, ctx)
            except Exception as exc:                      # noqa: BLE001 - last line of defence
                self._handle_failure(ctx, node, exc)
                return ctx
            self._repo.save_context(ctx)
            node = self.graph.next_node(node, ctx)
        self._repo.save_context(ctx)
        return ctx

    def _handle_failure(self, ctx: WorkflowContext, node: str, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        try:
            ctx.fail(node, message)
        except IllegalTransition:                           # pragma: no cover - defensive
            ctx.error, ctx.failed_node = message, node
        self._audit.log(ctx.application_id, ORCH, "case_failed", f"{node}: {message}")
        self._bus.publish(ctx.application_id, ORCH, "Operations", "ALERT", f"{node} failed: {message}")
        self._notifier.send(Notification("operations", f"Case failed: {ctx.application_id}",
                                         f"Node '{node}' failed: {message}. Retry once the cause is fixed.",
                                         ctx.application_id))
        self._repo.save_context(ctx)
