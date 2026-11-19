# Multi-Agent Loan Application Processing

A backend system where eight specialised agents process a loan application end to end:
read documents, validate them, score risk, screen fraud, recommend a decision, pause for a
human where policy requires, disburse, verify the payment, notify the applicant, and later
learn from repayment outcomes. It is not a chatbot: it runs as an intelligent backend, and
one web dashboard puts everything in a single place.

Currency is PKR. External services (credit bureau, core banking, OCR) are mocked behind
interfaces; see `docs/DESIGN.md` for the limitations.

## Quick start

```bash
pip install flask
python main.py dashboard          # then open http://127.0.0.1:5000
```

That one command is the whole system. The dashboard has four tabs:

| Tab | What you do there |
|---|---|
| **Cases** | See every case, click **Load demo applications**, review and approve/reject the ones the agents escalate, retry failed cases, record repayment outcomes |
| **New application** | Submit your own application (or prefill a demo scenario), set the simulated bureau profile, edit the documents, and watch the agents process it |
| **Workflow & agents** | The eight live agents, current policy and model, and all 8 diagrams rendered (needs internet for the diagram library) |
| **Learning** | Simulate traffic and repayments, propose a retrained model, propose a threshold change, and approve either with a named approver; change history is listed |

Command-line alternatives (same engine, no browser):

```bash
python main.py demo               # 8 scenarios, scripted human reviewer
python main.py demo --interactive # you play the loan officer in the terminal
python main.py feedback-demo      # the learning loop, printed
python -m unittest discover -s tests -t .   # 63 tests
```

Python 3.10+. The core needs only the standard library; Flask is only for the dashboard.

## The workflow

```
intake -> validation -> risk -> fraud -> decision -> approval_gate
                                                      |-- PAUSE for loan officer --> resume
                                                      |-- execution -> verification -> reporting
                                                      `-- reporting (rejected)
```

| # | Agent | One-line job |
|---|---|---|
| 1 | `DocumentIntakeAgent` | Documents to structured data |
| 2 | `DataValidationAgent` | Missing documents, name/ID/income/account mismatches |
| 3 | `CreditRiskAgent` | Bureau API + ML model: DTI, default probability, risk band |
| 4 | `FraudDetectionAgent` | Blacklist, duplicates, shared accounts, income anomalies |
| 5 | `DecisionAgent` | Policy rules to APPROVE / REJECT / ESCALATE with reasons |
| 6 | `ExecutionAgent` | Disburse via banking API (only agent that can move money) |
| 7 | `VerificationAgent` | Re-read the bank ledger and reconcile |
| 8 | `ReportingFeedbackAgent` | Close cases, notify applicant, learn from outcomes |

Full specification of each agent (purpose, input, output, tools, data, communication,
decisions, human intervention): `docs/AGENTS.md`.

## The eight demo scenarios

| Case | What happens |
|---|---|
| APP-1001 | Low risk, small amount: straight-through approval and payment, no human |
| APP-1002 | Eligible but large amount: pauses for officer approval |
| APP-1003 | Debt-to-income 82%: automatic rejection |
| APP-1004 | Bank statement missing: `needs_info`, applicant told what to send |
| APP-1005 | National ID blacklisted: rejected, notice does not reveal why |
| APP-1006 | Stated income 2.5x salary slip: fraud and validation flags, escalated |
| APP-1007 | Credit bureau times out twice: retried automatically, then approved |
| APP-1008 | No bureau history: escalated to a human |

## Project structure

```
main.py                       CLI: demo | dashboard | feedback-demo
loan_system/
  models.py  state.py         domain dataclasses; WorkflowContext + enforced state machine
  orchestrator.py             WorkflowGraph + LoanWorkflow (routing, pause/resume, retry)
  system.py                   composition root (dependency injection) + LoanProcessingSystem facade
  agents/                     BaseAgent + the 8 agents
  services/                   abstract interfaces + mocks: parser, bureau, banking, fraud DB, explanations
  ml/                         logistic-regression risk model, retraining, threshold tuning
  repository.py               SQLite: cases, audit log, messages, feedback, blacklist
  messaging.py notifier.py    agent message bus + audit; notifications
  dashboard.py                Flask UI: cases, new application, workflow & agents, learning
  simulation.py               traffic and repayment simulation (used by the Learning tab and CLI)
  langgraph_adapter.py        optional LangGraph mirror of the same graph
  samples.py                  demo scenarios
tests/                        63 tests (unit, workflow, failure injection, feedback, dashboard pages, docs)
docs/
  ARCHITECTURE.md             8 Mermaid diagrams (open diagrams.html in a browser to see them rendered)
  AGENTS.md                   per-agent specification
  DESIGN.md                   why multi-agent, failure handling, feedback loop, limitations
```

## OOP design in one paragraph

Every agent subclasses abstract `BaseAgent` (template method: `execute()` wraps the agent's
`run()` with audit and retry). Everything an agent touches from outside is an abstract
interface (`CreditBureauAPI`, `CoreBankingAPI`, `DocumentParser`, `FraudDatabase`,
`ExplanationGenerator`, `Notifier`) injected through constructors, with mock implementations
for tests and demos. `WorkflowContext` owns the state machine and rejects illegal transitions.
`build_system()` is the only place concrete classes are wired together.

## Swapping in real services

Implement the interface and pass it to `build_system(...)`:

```python
system = build_system(config, bureau=MyBureauClient(), bank=MyBankGateway(), parser=MyOcrParser())
```

## Where this maps to the assignment

| Assignment item | Where |
|---|---|
| Existing process, repetitive tasks, what is automated vs human | `docs/ARCHITECTURE.md` diagram 1; `docs/DESIGN.md` |
| Agent definitions (purpose, I/O, tools, decisions, human intervention) | `docs/AGENTS.md` |
| The 7 diagrams (+ class diagram) | `docs/ARCHITECTURE.md`, `docs/diagrams.html` |
| Why multiple agents; why not one | `docs/DESIGN.md` |
| Human-in-the-loop | approval gate, `LoanWorkflow.apply_human_review`, dashboard |
| Failure handling | `docs/DESIGN.md` table; `tests/test_workflow.py::FailureHandlingTests` |
| Feedback improving the system | `ml/feedback_learning.py`, `python main.py feedback-demo` |
