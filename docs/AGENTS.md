# Agent Specifications

Each agent extends `BaseAgent` (`loan_system/agents/base.py`), which adds audit logging
and automatic retry on transient errors. Agents never call each other: they read and
write the shared `WorkflowContext`, and the orchestrator hands control from one to the next.

## 1. DocumentIntakeAgent
| | |
|---|---|
| **Purpose** | Turn uploaded documents into structured data |
| **Input** | `application.documents` (OCR text of national ID, salary slip, bank statement) |
| **Output** | `ctx.extracted`: identity, salary and bank statement fields, plus `parse_errors` |
| **Responsibilities** | Parse each document; record unreadable documents instead of crashing |
| **Tools / APIs** | `DocumentParser` (here a key-value text parser; OCR service or vision LLM in production) |
| **Data sources** | The submitted documents only |
| **Talks to** | Orchestrator only (hands data to DataValidationAgent via the context) |
| **Decisions** | Whether a document is readable. It never judges whether the content is correct |
| **Human intervention** | None |

## 2. DataValidationAgent
| | |
|---|---|
| **Purpose** | Catch missing documents and inconsistencies before any money-related analysis |
| **Input** | `ctx.application`, `ctx.extracted` |
| **Output** | `ctx.validation`: missing documents and issues, each `BLOCKING` or `WARNING` |
| **Responsibilities** | Name match (fuzzy, 85%), ID number match, ID expiry, stated vs verified income (10% tolerance), employer match, account match, bank credits vs salary |
| **Tools / APIs** | None (pure logic) |
| **Data sources** | Application form and extracted document data |
| **Talks to** | Orchestrator only |
| **Decisions** | Issue severity. Missing documents end the flow in `needs_info` |
| **Human intervention** | None. Blocking issues are passed to the DecisionAgent, which escalates to a human |

## 3. CreditRiskAgent
| | |
|---|---|
| **Purpose** | Quantify the chance the applicant defaults |
| **Input** | Verified income (from the salary slip, never the stated income), amount, tenure |
| **Output** | `ctx.risk`: credit report, EMI, DTI, default probability, risk band, feature vector, model version |
| **Responsibilities** | Fetch bureau report, compute EMI and DTI, run the ML model, assign band (low under 10%, medium under 25%, else high) |
| **Tools / APIs** | `CreditBureauAPI` (retried on timeout), `ModelRegistry` (logistic regression) |
| **Data sources** | Credit bureau, salary slip, model weights |
| **Talks to** | Orchestrator only |
| **Decisions** | Risk band. It does not approve or reject |
| **Human intervention** | None per case. Model changes need human promotion (see Agent 8) |

## 4. FraudDetectionAgent
| | |
|---|---|
| **Purpose** | Detect fraud indicators independently of creditworthiness |
| **Input** | Application, extracted data |
| **Output** | `ctx.fraud`: score 0..1, level (none/low/medium/high), signals, blacklisted flag |
| **Responsibilities** | Blacklist hit (always high); duplicate applications in 30 days; disbursement account shared with a different applicant; stated income over 1.5x verified; loan over 24x monthly income |
| **Tools / APIs** | `FraudDatabase` |
| **Data sources** | Blacklist table, history of past applications |
| **Talks to** | Orchestrator only |
| **Decisions** | Fraud level |
| **Human intervention** | None directly. Medium/high fraud forces escalation to a human |

## 5. DecisionAgent
| | |
|---|---|
| **Purpose** | Combine validation, risk and fraud into one explainable recommendation |
| **Input** | `ctx.validation`, `ctx.risk`, `ctx.fraud`, `PolicyConfig` |
| **Output** | `ctx.decision`: APPROVE / REJECT / ESCALATE, `requires_human`, reasons, explanation |
| **Responsibilities** | Apply policy rules top-down, first match wins: blacklist, fraud, blocking validation issues, thin bureau file, DTI over 50%, PD at or over 35%, PD at or over 25%, else approve. Decide whether a human must sign off |
| **Tools / APIs** | `ExplanationGenerator` (template, or LLM via LangChain, phrasing only) |
| **Data sources** | Outputs of agents 2 to 4, policy configuration |
| **Talks to** | Orchestrator; its output routes the case to the officer or to execution |
| **Decisions** | Recommendation and whether human review is needed. **It cannot move money** |
| **Human intervention** | Required for every ESCALATE, and for APPROVE when the amount exceeds PKR 500,000, the risk band is not low, or any warning or fraud signal exists |

## 6. ExecutionAgent
| | |
|---|---|
| **Purpose** | Disburse the approved loan |
| **Input** | Approved case (`ctx.authorized_for_execution`, `ctx.authorized_amount`) |
| **Output** | `ctx.receipt` (transaction id, amount, status) |
| **Responsibilities** | Verify authorisation, pay exactly the authorised amount, use the application id as an idempotency key |
| **Tools / APIs** | `CoreBankingAPI.disburse` (the only write access to the bank in the system) |
| **Data sources** | Authorisation fields of the case |
| **Talks to** | Orchestrator only |
| **Decisions** | None. It refuses (`AuthorizationError`) when not authorised or when the amount is invalid |
| **Human intervention** | Its authorisation comes from a human review or from policy-approved straight-through |

## 7. VerificationAgent
| | |
|---|---|
| **Purpose** | Independently confirm the payment matches what was authorised |
| **Input** | `ctx.receipt`, authorisation fields |
| **Output** | `ctx.verification` (matched flag and discrepancies) |
| **Responsibilities** | Re-read the transaction from the bank ledger; compare status, amount, account and reference. It does not trust the ExecutionAgent's receipt |
| **Tools / APIs** | `CoreBankingAPI.get_transaction`, `Notifier` |
| **Data sources** | Bank ledger |
| **Talks to** | Orchestrator; alerts the operations team on mismatch |
| **Decisions** | Matched or reconciliation required |
| **Human intervention** | Operations team handles every mismatch |

## 8. ReportingFeedbackAgent
| | |
|---|---|
| **Purpose** | Close each case and learn from real outcomes |
| **Input (reporting)** | The finished case. **(feedback)** repayment outcomes |
| **Output** | `ctx.final_report` (summary and applicant notice); feedback rows; proposals for a retrained model and a new reject threshold |
| **Responsibilities** | Draft applicant notices with applicant-safe reasons only (fraud details are never disclosed); log repayment outcomes; propose retraining (only with 40+ outcomes) and a threshold change (capped at +/-0.10 per cycle) |
| **Tools / APIs** | `Notifier`, `Repository`, `ModelRetrainer`, `ThresholdTuner` |
| **Data sources** | Closed cases, feedback table |
| **Talks to** | Orchestrator; proposals go to the risk manager |
| **Decisions** | Whether a candidate model beat the current one on held-out outcomes |
| **Human intervention** | A named risk manager must approve before any model or threshold goes live |
