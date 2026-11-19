# Architecture and Diagrams

All diagrams are Mermaid. They render on GitHub, in VS Code (Markdown Preview Mermaid
Support extension), and in `docs/diagrams.html` (open it in a browser; it needs internet
to load the Mermaid library).

The diagrams match the code: the state diagram mirrors the transition table enforced in
`loan_system/state.py`, and the agent order matches `loan_system/orchestrator.py`.

---

## 1. Existing process (manual)

```mermaid
flowchart TD
    A["Customer submits application<br/>and paper or PDF documents"] --> B["Loan officer opens each document<br/>and reads it by eye"]
    B --> C["Re-types data into the loan system"]
    C --> D{"All documents present?"}
    D -- "No" --> D1["Phone or email the customer<br/>and wait days"]
    D1 --> B
    D -- "Yes" --> E["Log in to the credit bureau portal<br/>and download the report"]
    E --> F["Calculate instalment and debt-to-income<br/>in a spreadsheet"]
    F --> G["Search blacklist and past applications<br/>by hand"]
    G --> H["Apply credit policy from memory<br/>or a PDF manual"]
    H --> I{"Officer's decision"}
    I -- "Reject" --> N["Write and send rejection letter"]
    I -- "Unsure or large" --> J["Forward to senior officer<br/>by email"]
    J --> K{"Senior decision"}
    K -- "Reject" --> N
    K -- "Approve" --> L
    I -- "Approve" --> L["Key the transfer into<br/>the core banking portal"]
    L --> M["Check the ledger later<br/>to confirm the amount"]
    M --> O["Write and send approval letter"]

    classDef pain fill:#fde2e1,stroke:#a11d17,color:#000;
    class B,C,E,F,G,L,M pain;
```

Red boxes are the repetitive, error-prone steps (re-typing, spreadsheet maths, manual
look-ups, manual payment keying). The judgement steps (`Officer's decision`,
`Senior decision`) stay with humans in the new design.

---

## 2. Proposed multi-agent architecture

```mermaid
flowchart LR
    subgraph IN["Input"]
        APP["Loan application<br/>and documents"]
    end

    ORCH{{"Orchestrator<br/>LoanWorkflow graph runner<br/>state machine and persistence"}}

    subgraph AGENTS["Specialised agents"]
        A1["1 DocumentIntakeAgent"]
        A2["2 DataValidationAgent"]
        A3["3 CreditRiskAgent"]
        A4["4 FraudDetectionAgent"]
        A5["5 DecisionAgent"]
        A6["6 ExecutionAgent"]
        A7["7 VerificationAgent"]
        A8["8 ReportingFeedbackAgent"]
    end

    HUMAN(["Loan officer<br/>approves, rejects, or reduces amount"])

    APP --> ORCH
    ORCH --> A1 --> A2 --> A3 --> A4 --> A5
    A5 -->|"policy: human needed"| HUMAN
    HUMAN -->|"decision"| ORCH
    A5 -->|"policy: straight-through"| A6
    ORCH --> A6 --> A7 --> A8
    A8 -.->|"repayment outcomes: proposals need human approval"| A3
    A8 -.-> A5
```

Agents never call each other. Each reads the shared case, writes only its own section,
and returns control to the orchestrator.

---

## 3. Agent communication flow (one case, human-approved path)

```mermaid
sequenceDiagram
    autonumber
    actor Cust as Customer
    participant O as Orchestrator
    participant IN as DocumentIntakeAgent
    participant VA as DataValidationAgent
    participant CR as CreditRiskAgent
    participant FR as FraudDetectionAgent
    participant DE as DecisionAgent
    actor LO as Loan Officer
    participant EX as ExecutionAgent
    participant VE as VerificationAgent
    participant RP as ReportingFeedbackAgent

    Cust->>O: submit application and documents
    O->>IN: TASK parse documents
    IN-->>O: RESULT structured data
    O->>VA: TASK cross-check
    VA-->>O: RESULT missing documents and issues
    alt documents missing
        O->>RP: TASK notify applicant
        RP-->>Cust: request missing documents
    else complete
        O->>CR: TASK assess risk
        Note over CR: calls credit bureau API and runs the ML model, retries on timeout
        CR-->>O: RESULT DTI and default probability
        O->>FR: TASK screen fraud
        FR-->>O: RESULT level and signals
        O->>DE: TASK recommend
        DE-->>O: RESULT recommendation and reasons
        alt requires human
            O->>LO: HUMAN_REQUEST case summary
            LO-->>O: HUMAN_RESPONSE approve or reject
        end
        opt approved
            O->>EX: TASK disburse
            Note over EX: idempotent bank API call
            EX-->>O: RESULT transaction id
            O->>VE: TASK verify
            VE-->>O: RESULT ledger matches
        end
        O->>RP: TASK close case
        RP-->>Cust: decision notice
    end
```

Every TASK, RESULT, HUMAN_REQUEST and HUMAN_RESPONSE is stored as an `AgentMessage` and
shown in the dashboard under "Agent conversation".

---

## 4. Data flow diagram

```mermaid
flowchart LR
    CUST(["Customer"])
    OFFICER(["Loan officer"])
    OPS(["Operations team"])
    BUREAU[["Credit bureau API"]]
    BANK[["Core banking API"]]

    P1["P1 Parse documents"]
    P2["P2 Validate data"]
    P3["P3 Assess risk"]
    P4["P4 Screen fraud"]
    P5["P5 Decide"]
    P6["P6 Disburse"]
    P7["P7 Verify"]
    P8["P8 Report and learn"]

    D1[("Case store")]
    D2[("Audit log and messages")]
    D3[("Blacklist")]
    D4[("Risk model weights")]
    D5[("Feedback outcomes")]

    CUST -->|"application, documents"| P1
    P1 -->|"extracted fields"| D1
    D1 --> P2
    P2 -->|"validation report"| D1
    D1 --> P3
    BUREAU -->|"credit report"| P3
    D4 -->|"weights"| P3
    P3 -->|"risk assessment"| D1
    D1 --> P4
    D3 -->|"matches"| P4
    D1 -->|"past applications"| P4
    P4 -->|"fraud report"| D1
    D1 --> P5
    P5 -->|"recommendation"| D1
    P5 -->|"case summary"| OFFICER
    OFFICER -->|"approval"| D1
    D1 -->|"authorised amount"| P6
    P6 -->|"payment instruction"| BANK
    BANK -->|"receipt"| P6
    P6 --> D1
    BANK -->|"ledger entry"| P7
    D1 --> P7
    P7 -->|"mismatch alert"| OPS
    P7 --> D1
    D1 --> P8
    P8 -->|"decision notice"| CUST
    OFFICER -->|"repayment outcome"| P8
    P8 --> D5
    D5 -->|"retrain proposal"| D4
    P1 -.-> D2
    P3 -.-> D2
    P5 -.-> D2
    P6 -.-> D2
```

---

## 5. State diagram

```mermaid
stateDiagram-v2
    [*] --> submitted
    submitted --> documents_parsed
    documents_parsed --> validated
    documents_parsed --> needs_info : missing or unreadable documents
    validated --> risk_assessed
    risk_assessed --> fraud_screened
    fraud_screened --> recommended
    recommended --> awaiting_approval : human required
    recommended --> approved : straight-through
    recommended --> rejected : policy rejection
    awaiting_approval --> approved : officer approves
    awaiting_approval --> rejected : officer rejects
    approved --> disbursed
    disbursed --> verified : ledger matches
    disbursed --> reconciliation_required : ledger mismatch
    verified --> completed
    rejected --> completed
    needs_info --> [*]
    reconciliation_required --> [*]
    completed --> [*]

    submitted --> failed
    documents_parsed --> failed
    validated --> failed
    risk_assessed --> failed
    fraud_screened --> failed
    recommended --> failed
    awaiting_approval --> failed
    approved --> failed
    disbursed --> failed
    verified --> failed
    rejected --> failed
    needs_info --> failed
    reconciliation_required --> failed
    failed --> [*] : retry resumes at the failed node
```

Two rules the code enforces: no state can be skipped (`IllegalTransition`), and money only
moves from `approved`, which is reachable only through a human review or a policy-approved
straight-through case. Any non-final state can move to `failed` (for example if the closing
report step itself breaks), and a retry resumes at the node that failed.

---

## 6. System architecture (layers)

```mermaid
flowchart TB
    subgraph UI["Interface layer"]
        DASH["Flask dashboard<br/>officer review, retry, outcomes"]
        CLI["CLI: demo, dashboard, feedback-demo"]
        API["REST API (extension point)"]
    end

    subgraph APPL["Application layer"]
        FACADE["LoanProcessingSystem facade"]
        WF["LoanWorkflow<br/>WorkflowGraph, routing, retry, pause and resume"]
        STATE["WorkflowContext<br/>state machine"]
        BUS["MessageBus and AuditLogger"]
    end

    subgraph AG["Agent layer (all extend BaseAgent)"]
        AGS["Intake, Validation, CreditRisk, FraudDetection,<br/>Decision, Execution, Verification, ReportingFeedback"]
    end

    subgraph SVC["Service layer (abstract interfaces)"]
        S1["DocumentParser"]
        S2["CreditBureauAPI"]
        S3["CoreBankingAPI"]
        S4["FraudDatabase"]
        S5["ExplanationGenerator"]
        S6["Notifier"]
        ML["LogisticRiskModel<br/>and ModelRegistry"]
    end

    subgraph INFRA["Infrastructure (swappable implementations)"]
        I1["KeyValueDocumentParser<br/>(OCR or vision LLM in production)"]
        I2["MockCreditBureau<br/>(real bureau REST API)"]
        I3["MockCoreBanking<br/>(real banking gateway)"]
        I4[("SQLite Repository<br/>PostgreSQL in production")]
        I5["Template explanation<br/>or LangChain LLM"]
        I6["Console or in-memory notifier<br/>(email or SMS)"]
    end

    UI --> FACADE --> WF
    WF --> STATE
    WF --> BUS
    WF --> AGS
    AGS --> S1 & S2 & S3 & S4 & S5 & S6 & ML
    S1 -.-> I1
    S2 -.-> I2
    S3 -.-> I3
    S4 -.-> I4
    S5 -.-> I5
    S6 -.-> I6
    BUS --> I4
    STATE --> I4
```

---

## 7. Human-in-the-loop workflow

```mermaid
flowchart TD
    R["DecisionAgent recommendation"] --> Q1{"Recommendation"}
    Q1 -- "REJECT" --> RJ["Auto-reject: DTI over limit,<br/>PD over threshold, or blacklisted.<br/>Applicant notice has safe reasons only"]
    Q1 -- "ESCALATE" --> H
    Q1 -- "APPROVE" --> Q2{"Straight-through eligible?<br/>amount at or under limit,<br/>risk band low,<br/>no warnings or fraud signals"}
    Q2 -- "Yes" --> EX
    Q2 -- "No" --> H

    H["PAUSE: case saved as awaiting_approval.<br/>Officer notified with summary,<br/>reasons, risk and fraud evidence"] --> HD{"Loan officer decides<br/>name recorded in audit"}
    HD -- "Reject" --> RJ
    HD -- "Approve, optionally reduce amount<br/>(never raise)" --> EX["ExecutionAgent disburses<br/>idempotent bank call"]
    EX --> V["VerificationAgent re-reads the bank ledger"]
    V --> Q3{"Ledger matches<br/>authorisation?"}
    Q3 -- "Yes" --> DONE["Completed: applicant notified"]
    Q3 -- "No" --> OPS["Operations alerted:<br/>human reconciliation"]

    DONE --> FB["Months later: repayment outcome<br/>recorded by a human or system"]
    FB --> LEARN["Agent proposes retrained model<br/>and new threshold"]
    LEARN --> G{"Risk manager<br/>approves?"}
    G -- "Yes" --> LIVE["Promoted live,<br/>audit entry written"]
    G -- "No" --> KEEP["Current model and policy unchanged"]
```

Humans appear at four points: the approval gate, reconciliation of ledger mismatches,
retrying failed cases, and approving any model or policy change.

---

## 8. Class diagram (OOP design)

```mermaid
classDiagram
    class BaseAgent {
        <<abstract>>
        +name
        +execute(ctx)
        +run(ctx)
        +summarize(ctx)
    }
    BaseAgent <|-- DocumentIntakeAgent
    BaseAgent <|-- DataValidationAgent
    BaseAgent <|-- CreditRiskAgent
    BaseAgent <|-- FraudDetectionAgent
    BaseAgent <|-- DecisionAgent
    BaseAgent <|-- ExecutionAgent
    BaseAgent <|-- VerificationAgent
    BaseAgent <|-- ReportingFeedbackAgent

    class RetryPolicy {
        +max_attempts
        +delay_for(attempt)
    }
    BaseAgent o-- RetryPolicy

    class CreditBureauAPI {
        <<interface>>
        +get_report(id)
    }
    class CoreBankingAPI {
        <<interface>>
        +disburse()
        +get_transaction()
    }
    class DocumentParser {
        <<interface>>
        +parse(doc)
    }
    class FraudDatabase {
        <<interface>>
    }
    class ExplanationGenerator {
        <<interface>>
        +generate(ctx)
    }
    class Notifier {
        <<interface>>
        +send(n)
    }
    CreditBureauAPI <|.. MockCreditBureau
    CoreBankingAPI <|.. MockCoreBanking
    DocumentParser <|.. KeyValueDocumentParser
    FraudDatabase <|.. RepositoryFraudDatabase
    ExplanationGenerator <|.. TemplateExplanationGenerator
    ExplanationGenerator <|.. LangChainExplanationGenerator
    Notifier <|.. ConsoleNotifier
    Notifier <|.. InMemoryNotifier

    CreditRiskAgent --> CreditBureauAPI
    CreditRiskAgent --> ModelRegistry
    ExecutionAgent --> CoreBankingAPI
    VerificationAgent --> CoreBankingAPI
    DocumentIntakeAgent --> DocumentParser
    FraudDetectionAgent --> FraudDatabase
    DecisionAgent --> ExplanationGenerator
    ReportingFeedbackAgent --> Notifier

    class WorkflowContext {
        +status
        +transition(new)
        +fail(node, error)
        +recover()
        +authorized_for_execution
    }
    class LoanWorkflow {
        +start(app)
        +apply_human_review()
        +retry_failed()
    }
    class WorkflowGraph {
        +add_node()
        +add_edge()
        +add_conditional_edges()
    }
    class LoanProcessingSystem {
        +submit()
        +review()
        +retry()
        +record_outcome()
    }
    class ModelRegistry {
        +current
        +promote()
    }
    class LogisticRiskModel {
        +fit()
        +predict_proba()
    }
    LoanProcessingSystem --> LoanWorkflow
    LoanWorkflow --> WorkflowGraph
    LoanWorkflow --> WorkflowContext
    ModelRegistry --> LogisticRiskModel
```
