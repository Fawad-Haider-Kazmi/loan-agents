"""Natural-language explanation of a decision.

Design rule: the LLM only *phrases* an explanation. The decision itself is made
by deterministic, auditable policy rules in DecisionAgent. If the LLM is
unavailable the template generator is used, so the workflow never blocks on it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..state import WorkflowContext
from ..utils import money


class ExplanationGenerator(ABC):
    @abstractmethod
    def generate(self, ctx: WorkflowContext) -> str: ...


class TemplateExplanationGenerator(ExplanationGenerator):
    def generate(self, ctx: WorkflowContext) -> str:
        d, r, f = ctx.decision, ctx.risk, ctx.fraud
        lines = [
            f"Recommendation: {d.recommendation.value.upper()} for {money(ctx.application.requested_amount)} "
            f"over {ctx.application.tenure_months} months.",
            f"Credit score {r.credit_report.score}, debt-to-income {r.dti:.0%}, "
            f"estimated default probability {r.default_probability:.1%} ({r.risk_band.value} risk).",
            f"Fraud screening: {f.level.value}" + (f" ({', '.join(s.code for s in f.signals)})" if f.signals else "") + ".",
            "Reasons: " + " ".join(d.reasons),
        ]
        return "\n".join(lines)


class LangChainExplanationGenerator(ExplanationGenerator):
    """Optional. Requires `pip install langchain-anthropic` and ANTHROPIC_API_KEY.
    Sends only the structured facts below (no documents, no national ID)."""

    def __init__(self, model: str = "claude-sonnet-4-5", fallback: ExplanationGenerator | None = None):
        self.model = model
        self.fallback = fallback or TemplateExplanationGenerator()

    def generate(self, ctx: WorkflowContext) -> str:  # pragma: no cover - needs network + key
        try:
            from langchain_anthropic import ChatAnthropic
            facts = self.fallback.generate(ctx)
            llm = ChatAnthropic(model=self.model, temperature=0)
            prompt = ("You are assisting a loan officer. Rewrite these facts as a concise, neutral "
                      "3-sentence case summary. Do not add facts or change the recommendation.\n\n" + facts)
            return llm.invoke(prompt).content
        except Exception:
            return self.fallback.generate(ctx)
