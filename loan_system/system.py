"""Composition root + facade. This is the only place concrete classes are wired
together (dependency injection): swap any service by passing a different object."""
from __future__ import annotations

import time
from typing import Callable, Optional

from .agents.base import RetryPolicy
from .agents.credit_risk import CreditRiskAgent
from .agents.data_validation import DataValidationAgent
from .agents.decision import DecisionAgent
from .agents.document_intake import DocumentIntakeAgent
from .agents.execution import ExecutionAgent
from .agents.fraud_detection import FraudDetectionAgent
from .agents.reporting import ReportingFeedbackAgent
from .agents.verification import VerificationAgent
from .config import SystemConfig
from .messaging import AuditLogger, MessageBus
from .ml.feedback_learning import RetrainResult, ThresholdSuggestion
from .ml.risk_model import LogisticRiskModel, ModelRegistry
from .models import LoanApplication
from .notifier import InMemoryNotifier, Notifier
from .orchestrator import LoanWorkflow
from .repository import Repository
from .services.banking import CoreBankingAPI, MockCoreBanking
from .services.credit_bureau import CreditBureauAPI, MockCreditBureau
from .services.document_parser import DocumentParser, KeyValueDocumentParser
from .services.explanation import ExplanationGenerator, TemplateExplanationGenerator
from .services.fraud_database import RepositoryFraudDatabase
from .state import ApplicationStatus, WorkflowContext


class LoanProcessingSystem:
    """What a UI, API or CLI talks to."""

    def __init__(self, config: SystemConfig, repo: Repository, workflow: LoanWorkflow,
                 models: ModelRegistry, reporting: ReportingFeedbackAgent, notifier: Notifier,
                 bureau: CreditBureauAPI, bank: CoreBankingAPI, bus: MessageBus):
        self.config, self.repo, self.workflow, self.models = config, repo, workflow, models
        self.reporting, self.notifier, self.bureau, self.bank, self.bus = reporting, notifier, bureau, bank, bus

    # ------------------------------------------------------------------ cases
    def submit(self, application: LoanApplication) -> WorkflowContext:
        return self.workflow.start(application)

    def review(self, application_id: str, reviewer: str, approved: bool, notes: str = "",
               approved_amount: Optional[float] = None) -> WorkflowContext:
        return self.workflow.apply_human_review(application_id, reviewer, approved, notes, approved_amount)

    def retry(self, application_id: str) -> WorkflowContext:
        return self.workflow.retry_failed(application_id)

    def get(self, application_id: str) -> WorkflowContext:
        return self.repo.load_context(application_id)

    def cases(self) -> list[WorkflowContext]:
        return self.repo.list_contexts()

    def pending_approvals(self) -> list[WorkflowContext]:
        return self.repo.list_contexts(ApplicationStatus.AWAITING_APPROVAL.value)

    # --------------------------------------------------------------- feedback
    def record_outcome(self, application_id: str, defaulted: bool) -> None:
        self.reporting.record_repayment_outcome(self.get(application_id), defaulted)

    def propose_retraining(self) -> Optional[RetrainResult]:
        return self.reporting.propose_retraining()

    def promote_model(self, result: RetrainResult, approved_by: str) -> None:
        if not approved_by.strip():
            raise ValueError("Promoting a model requires a named approver")
        self.models.promote(result.candidate)
        self.repo.add_audit("SYSTEM", approved_by, "model_promoted", result.candidate.version)

    def propose_threshold(self) -> Optional[ThresholdSuggestion]:
        return self.reporting.propose_threshold()

    def apply_threshold(self, suggestion: ThresholdSuggestion, approved_by: str) -> None:
        if not approved_by.strip():
            raise ValueError("Changing policy requires a named approver")
        policy = self.config.policy
        policy.reject_pd = suggestion.suggested
        policy.version = f"{policy.version.split('+')[0]}+reject_pd={suggestion.suggested}"
        self.repo.add_audit("SYSTEM", approved_by, "policy_changed", policy.version)


def build_system(config: Optional[SystemConfig] = None, *,
                 bureau: Optional[CreditBureauAPI] = None, bank: Optional[CoreBankingAPI] = None,
                 notifier: Optional[Notifier] = None, parser: Optional[DocumentParser] = None,
                 explainer: Optional[ExplanationGenerator] = None,
                 model: Optional[LogisticRiskModel] = None,
                 sleep: Callable[[float], None] = time.sleep) -> LoanProcessingSystem:
    config = config or SystemConfig()
    repo = Repository(config.db_path)
    audit, bus = AuditLogger(repo), MessageBus(repo)
    notifier = notifier or InMemoryNotifier()
    bureau = bureau or MockCreditBureau()
    bank = bank or MockCoreBanking()
    models = ModelRegistry(model or LogisticRiskModel.load_or_train(config.model_path))
    retry = RetryPolicy(config.retry_attempts, config.retry_base_delay, sleep)

    decision = DecisionAgent(config.policy, audit, explainer or TemplateExplanationGenerator(), retry)
    reporting = ReportingFeedbackAgent(notifier, audit, repo, models, config.policy,
                                       config.min_feedback_samples, retry=retry)
    agents = {
        "intake": DocumentIntakeAgent(parser or KeyValueDocumentParser(), audit, retry),
        "validation": DataValidationAgent(audit, config.income_tolerance, retry=retry),
        "risk": CreditRiskAgent(bureau, models, config.policy, config.annual_interest_rate, audit, retry),
        "fraud": FraudDetectionAgent(RepositoryFraudDatabase(repo), audit, config.fraud_window_days, retry),
        "decision": decision,
        "execution": ExecutionAgent(bank, audit, retry),
        "verification": VerificationAgent(bank, notifier, audit, retry),
        "reporting": reporting,
    }
    workflow = LoanWorkflow(repo, bus, audit, notifier, agents)
    return LoanProcessingSystem(config, repo, workflow, models, reporting, notifier, bureau, bank, bus)
