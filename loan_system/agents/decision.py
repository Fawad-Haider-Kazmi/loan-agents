from __future__ import annotations

from ..config import PolicyConfig
from ..messaging import AuditLogger
from ..models import DecisionRecord, FraudLevel, Recommendation, RiskBand
from ..services.explanation import ExplanationGenerator, TemplateExplanationGenerator
from ..state import ApplicationStatus, WorkflowContext
from ..utils import money
from .base import BaseAgent, RetryPolicy


class DecisionAgent(BaseAgent):
    """Combines validation, risk and fraud findings into one recommendation.

    Deterministic policy rules (evaluated top to bottom, first match wins) so
    every decision is reproducible and auditable. An LLM may only *phrase* the
    explanation afterwards.

    Input : ctx.validation, ctx.risk, ctx.fraud, PolicyConfig
    Output: ctx.decision (recommendation, requires_human, reasons, explanation)
    Decides: APPROVE / REJECT / ESCALATE, and whether a human must sign off.
    It does NOT move money: it can only recommend.
    """

    name = "DecisionAgent"
    purpose = "Apply credit policy and produce an explainable recommendation"

    def __init__(self, policy: PolicyConfig, audit: AuditLogger,
                 explainer: ExplanationGenerator | None = None, retry: RetryPolicy | None = None):
        super().__init__(audit, retry)
        self.policy = policy
        self._explainer = explainer or TemplateExplanationGenerator()

    def run(self, ctx: WorkflowContext) -> None:
        p, v, r, f = self.policy, ctx.validation, ctx.risk, ctx.fraud
        amount = ctx.application.requested_amount
        reasons: list[str] = []

        if f.blacklisted:
            rec = Recommendation.REJECT
            reasons.append("Applicant is on the internal blacklist.")
        elif f.level in (FraudLevel.MEDIUM, FraudLevel.HIGH):
            rec = Recommendation.ESCALATE
            reasons.append(f"Fraud screening returned {f.level.value}: {', '.join(s.code for s in f.signals)}.")
        elif v.has_blocking:
            rec = Recommendation.ESCALATE
            reasons.append("Document checks found blocking issues: " + ", ".join(i.code for i in v.issues if i.severity.value == "blocking") + ".")
        elif r.credit_report.thin_file:
            rec = Recommendation.ESCALATE
            reasons.append("Credit bureau has no meaningful history for this applicant (thin file).")
        elif r.dti > p.max_dti:
            rec = Recommendation.REJECT
            reasons.append(f"Debt-to-income {r.dti:.0%} exceeds the {p.max_dti:.0%} limit.")
        elif r.default_probability >= p.reject_pd:
            rec = Recommendation.REJECT
            reasons.append(f"Estimated default probability {r.default_probability:.1%} is at or above the {p.reject_pd:.0%} reject threshold.")
        elif r.default_probability >= p.approve_pd:
            rec = Recommendation.ESCALATE
            reasons.append(f"Borderline default probability {r.default_probability:.1%} needs a human judgement.")
        else:
            rec = Recommendation.APPROVE
            reasons.append(f"Default probability {r.default_probability:.1%} and DTI {r.dti:.0%} are within policy.")

        requires_human = rec == Recommendation.ESCALATE
        if rec == Recommendation.APPROVE:
            if amount > p.auto_approve_limit:
                requires_human = True
                reasons.append(f"Amount {money(amount)} exceeds the {money(p.auto_approve_limit)} straight-through limit.")
            if r.risk_band != RiskBand.LOW:
                requires_human = True
                reasons.append(f"Risk band is {r.risk_band.value}, not low.")
            if v.has_warnings or f.level != FraudLevel.NONE:
                requires_human = True
                reasons.append("Non-blocking warnings are present.")

        ctx.decision = DecisionRecord(rec, requires_human, amount if rec != Recommendation.REJECT else 0.0,
                                      reasons, "", p.version)
        ctx.decision.explanation = self._explainer.generate(ctx)
        ctx.transition(ApplicationStatus.RECOMMENDED)

    def summarize(self, ctx: WorkflowContext) -> str:
        d = ctx.decision
        return f"{d.recommendation.value.upper()} (human required: {d.requires_human})"
