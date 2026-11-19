# Design Decisions

## What "agent" means in this project
Each agent is an autonomous component with a defined job, tools it may use, inputs,
outputs, and decision rights. Most agents here are deterministic code or a small ML model
rather than LLM calls, on purpose: credit decisions must be reproducible and auditable. An
LLM is used only where language adds value and risk is low (`ExplanationGenerator`, optional),
and the design marks other places an LLM would fit (reading messy document scans in
`DocumentParser`, drafting longer case narratives).

## Why multiple agents instead of one
| Reason | How it shows up in the design |
|---|---|
| **Separation of duties / least privilege** | Only `ExecutionAgent` can write to the bank API. The agent that recommends (`DecisionAgent`) cannot move money, so a bug or bad input in analysis cannot cause a payment |
| **Independent verification** | `VerificationAgent` re-reads the bank ledger instead of trusting the executor's receipt. One agent checking its own work would miss its own blind spots |
| **Fraud is not credit risk** | A strong credit score can hide a fraudster. Separate agents with separate data and outputs stop one score masking the other |
| **Different tools, data and failure modes** | Bureau timeouts, bank timeouts and unreadable scans each need different handling. Per-agent retry and audit keeps failures local and visible |
| **Replaceability** | Swap the OCR vendor, the bureau or the risk model without touching other agents (interfaces in `services/`) |
| **Testability** | Every agent is tested alone with mocks (63 tests including failure injection) |
| **Scoped context for LLMs** | If agents use LLMs, each gets a small focused prompt and a narrow tool set instead of one giant prompt with every tool |

## How agents communicate
Orchestrator-mediated blackboard. Agents never call each other. The orchestrator gives each
the shared `WorkflowContext`; each writes its own section; every hand-off is stored as an
`AgentMessage` (TASK, RESULT, HUMAN_REQUEST, HUMAN_RESPONSE, ALERT). This gives one
authority over ordering, a replayable conversation per case, and no hidden agent-to-agent coupling.

## Where human approval is required
1. **Approval gate**: every ESCALATE, and APPROVE above PKR 500,000, outside the low risk band, or with any warning/fraud signal.
2. **Reconciliation**: any ledger mismatch.
3. **Failed cases**: an operator triggers the retry.
4. **Learning**: any model promotion or policy threshold change needs a named approver, recorded in the audit log.

Reviewers can reduce an approved amount but never raise it; a reviewer name is mandatory.

## Failure handling
| Failure | Behaviour |
|---|---|
| Transient (timeouts) in bureau or bank | Automatic retry, 3 attempts, exponential back-off, each attempt audited |
| Still failing after retries, or any permanent error or unexpected exception | Case moves to `failed`, operations is alerted, state is saved. `retry` resumes at the failed node, not from scratch |
| Bank call times out after paying | Idempotency key (application id) and receipt check mean a retry cannot pay twice |
| Unreadable or missing document | Not a crash: recorded, case ends in `needs_info`, applicant told what to resubmit |
| Bureau has no history | Treated as a thin file and escalated to a human, not an error |
| Bank ledger differs from authorisation | `reconciliation_required`, operations alerted, no automatic correction |
| Attempt to skip a stage or pay without authorisation | `IllegalTransition` / `AuthorizationError` |
| Process restarts while waiting for a human | Case state is in SQLite; the pending approval is still there |

## Feedback loop
Repayment outcomes are stored with the feature vector and the PD the model predicted. The
agent can propose (a) a retrained model, evaluated against the current one on held-out real
outcomes, and (b) a changed reject threshold, chosen by expected cost. Both are proposals only,
and the threshold change is capped at +/-0.10 per cycle. Open the **Learning** tab (or run `python main.py feedback-demo`)
to see it: at 250 simulated cases the guard correctly declined to promote a candidate that was
not better; at 800 it improved held-out log-loss from 0.499 to 0.479 and was promoted after
the (simulated) human approval.

## Known limitations (read before presenting)
* **External systems are mocked**: credit bureau, core banking and OCR. Interfaces are real; swap implementations to connect actual services.
* **Risk model is trained on synthetic data**, not real credit data. It demonstrates the pipeline, not real-world accuracy.
* **Selection bias in feedback**: outcomes exist only for loans we approved, so threshold tuning can learn to be stricter but not more lenient. Production systems need reject inference or a controlled exploration policy.
* **LangGraph**: the workflow runs on its own small graph runner (zero extra installs). `langgraph_adapter.py` mirrors the graph for LangGraph but was not run in the offline environment this was built in.
* **LLM explanation** (`LangChainExplanationGenerator`) is optional and untested offline; the template generator is the default.
* **Dashboard has no authentication or CSRF protection**, and SQLite does not coordinate two reviewers acting at the exact same instant. Both are fine for a demo, not for production.
* **Agents run sequentially**. Risk and fraud are independent and could run in parallel; the graph structure allows it.
