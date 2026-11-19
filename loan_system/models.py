"""Domain model. Plain dataclasses, no behaviour that talks to the outside world.

Every agent reads from and writes to these objects (through WorkflowContext),
which keeps agent interfaces explicit and makes the whole case serialisable
to JSON for persistence, audit and the dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DocumentType(str, Enum):
    NATIONAL_ID = "national_id"
    SALARY_SLIP = "salary_slip"
    BANK_STATEMENT = "bank_statement"


class Severity(str, Enum):
    WARNING = "warning"
    BLOCKING = "blocking"


class RiskBand(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FraudLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Recommendation(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"


# --------------------------------------------------------------------------- input
@dataclass
class Document:
    doc_type: DocumentType
    filename: str
    raw_text: str  # what OCR / text extraction would return


@dataclass
class LoanApplication:
    application_id: str
    applicant_name: str
    national_id: str
    requested_amount: float
    tenure_months: int
    stated_monthly_income: float
    employer: str
    account_number: str
    documents: list[Document] = field(default_factory=list)
    submitted_at: str = ""


# ------------------------------------------------------------- document intake output
@dataclass
class IdentityData:
    name: str
    id_number: str
    date_of_birth: str
    expiry_date: str


@dataclass
class SalaryData:
    employer: str
    employee_name: str
    monthly_salary: float
    month: str


@dataclass
class BankStatementData:
    account_holder: str
    account_number: str
    avg_monthly_balance: float
    avg_monthly_credit: float


@dataclass
class ExtractedData:
    identity: Optional[IdentityData] = None
    salary: Optional[SalaryData] = None
    bank_statement: Optional[BankStatementData] = None
    parse_errors: dict[str, str] = field(default_factory=dict)  # doc_type -> reason


# --------------------------------------------------------------- validation output
@dataclass
class ValidationIssue:
    code: str
    message: str
    severity: Severity


@dataclass
class ValidationReport:
    missing_documents: list[str] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def has_blocking(self) -> bool:
        return any(i.severity == Severity.BLOCKING for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == Severity.WARNING for i in self.issues)

    @property
    def passed(self) -> bool:
        return not self.missing_documents and not self.has_blocking


# ------------------------------------------------------------------- risk output
@dataclass
class CreditReport:
    national_id: str
    score: int
    active_loans: int
    monthly_obligations: float
    delinquencies: int
    utilization: float          # 0..1
    thin_file: bool = False     # bureau has little or no history


@dataclass
class RiskAssessment:
    credit_report: CreditReport
    verified_income: float
    requested_emi: float
    dti: float                  # (existing obligations + new EMI) / verified income
    default_probability: float
    risk_band: RiskBand
    features: dict[str, float] = field(default_factory=dict)
    model_version: str = ""


# ------------------------------------------------------------------ fraud output
@dataclass
class FraudSignal:
    code: str
    message: str
    weight: float


@dataclass
class FraudReport:
    blacklisted: bool = False
    score: float = 0.0
    level: FraudLevel = FraudLevel.NONE
    signals: list[FraudSignal] = field(default_factory=list)


# ---------------------------------------------------------------- decision output
@dataclass
class DecisionRecord:
    recommendation: Recommendation
    requires_human: bool
    approved_amount: float
    reasons: list[str] = field(default_factory=list)
    explanation: str = ""
    policy_version: str = ""


@dataclass
class HumanReview:
    reviewer: str
    approved: bool
    notes: str
    approved_amount: float
    reviewed_at: str


# ------------------------------------------------------- execution / verification
@dataclass
class DisbursementReceipt:
    transaction_id: str
    reference: str
    account_number: str
    amount: float
    status: str                 # PENDING | SETTLED | FAILED
    timestamp: str


@dataclass
class VerificationReport:
    matched: bool
    discrepancies: list[str] = field(default_factory=list)
    checked_at: str = ""


@dataclass
class FinalReport:
    outcome: str
    summary: str
    applicant_notice: str


# -------------------------------------------------------------------- messaging
@dataclass
class AgentMessage:
    application_id: str
    sender: str
    recipient: str
    kind: str                   # TASK | RESULT | ALERT | HUMAN_REQUEST | HUMAN_RESPONSE
    summary: str
    timestamp: str = ""
