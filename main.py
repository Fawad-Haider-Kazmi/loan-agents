"""Command-line entry point.

    python main.py demo               run the 8 scenarios; scripted human reviewer
    python main.py demo --interactive you play the loan officer
    python main.py dashboard          web UI for the human-in-the-loop step
    python main.py feedback-demo      simulate repayments, retrain, propose new thresholds
"""
from __future__ import annotations

import argparse
import os
import random
import tempfile

from loan_system.config import SystemConfig
from loan_system.notifier import ConsoleNotifier
from loan_system.samples import sample_scenarios, seed_services
from loan_system.simulation import simulate_applications, simulate_outcomes
from loan_system.services.credit_bureau import MockCreditBureau
from loan_system.state import ApplicationStatus as S
from loan_system.system import LoanProcessingSystem, build_system
from loan_system.utils import money

BAR = "-" * 78


def demo_system(fresh: bool, db: str = "data/loan_system.db") -> tuple[LoanProcessingSystem, list]:
    if fresh and os.path.exists(db):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
    os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
    bureau = MockCreditBureau()
    system = build_system(SystemConfig(db_path=db, retry_base_delay=0.05), bureau=bureau,
                          notifier=ConsoleNotifier())
    scenarios = sample_scenarios()
    seed_services(scenarios, bureau, system.repo)
    return system, scenarios


def describe(ctx) -> str:
    line = f"status={ctx.status.value}"
    if ctx.decision:
        line += f" | agents recommend {ctx.decision.recommendation.value.upper()}"
    if ctx.risk:
        line += f" | PD {ctx.risk.default_probability:.1%}, DTI {ctx.risk.dti:.0%}"
    if ctx.receipt:
        line += f" | paid {money(ctx.receipt.amount)} ({ctx.receipt.transaction_id})"
    return line


def cmd_demo(args) -> None:
    system, scenarios = demo_system(fresh=True)
    print(f"\n{BAR}\n STEP 1: submit {len(scenarios)} applications; agents process each one\n{BAR}")
    for s in scenarios:
        a = s.application
        print(f"\n{a.application_id}  {a.applicant_name}  {money(a.requested_amount)}")
        print(f"  scenario: {s.expectation}")
        ctx = system.submit(a)
        retries = [e for e in system.repo.get_audit(a.application_id) if e["event"] == "transient_error"]
        if retries:
            print(f"  ({len(retries)} transient failure(s) retried automatically)")
        print(f"  -> {describe(ctx)}")

    pending = system.pending_approvals()
    print(f"\n{BAR}\n STEP 2: human in the loop - {len(pending)} case(s) need a decision\n{BAR}")
    scripted = {"APP-1002": (True, "Officer Rana", "Large loan, income confirmed with employer", None),
                "APP-1006": (False, "Officer Rana", "Stated income does not match salary slip", None),
                "APP-1008": (True, "Officer Rana", "Verified employment by phone; approving a smaller amount", 150_000)}
    for ctx in pending:
        d = ctx.decision
        print(f"\n{ctx.application_id}  {ctx.application.applicant_name}: agents recommend {d.recommendation.value.upper()}")
        for r in d.reasons:
            print(f"   - {r}")
        if args.interactive:
            approved = input("   approve? [y/N] ").strip().lower() == "y"
            reviewer = input("   your name: ").strip() or "Officer"
            amount = None
            if approved:
                raw = input(f"   amount (enter = {ctx.decision.approved_amount:,.0f}): ").strip()
                amount = float(raw) if raw else None
            notes = ""
        else:
            approved, reviewer, notes, amount = scripted.get(ctx.application_id, (False, "Officer Rana", "", None))
            print(f"   [scripted reviewer] {'APPROVE' if approved else 'REJECT'}: {notes}")
        done = system.review(ctx.application_id, reviewer, approved, notes, amount)
        print(f"   -> {describe(done)}")

    print(f"\n{BAR}\n STEP 3: final state\n{BAR}")
    for ctx in system.cases():
        print(f"{ctx.application_id}  {ctx.application.applicant_name:15} {ctx.status.value:20} "
              f"{ctx.final_report.outcome if ctx.final_report else ''}")
    print(f"\nTotal paid out by core banking: {money(system.bank.total_paid_out)}")
    print("Audit trail and agent conversations are stored in data/loan_system.db")
    print("Run `python main.py dashboard` to explore them in the browser.\n")


def cmd_dashboard(args) -> None:
    from loan_system.dashboard import create_app
    system, scenarios = demo_system(fresh=args.fresh)
    print(f"Dashboard on http://127.0.0.1:{args.port}  (click 'Load demo applications')")
    create_app(system, scenarios).run(port=args.port, debug=False)


def cmd_feedback_demo(args) -> None:
    tmp = tempfile.mkdtemp()
    rng = random.Random(2026)
    bureau = MockCreditBureau()
    system = build_system(SystemConfig(db_path=f"{tmp}/fb.db", model_path=f"{tmp}/model.json",
                                       retry_base_delay=0), bureau=bureau)
    n = args.cases
    print(f"\nProcessing {n} simulated applications through the full multi-agent workflow ...")
    counts = simulate_applications(system, n, rng)
    print(f"  {counts['disbursed']} loans disbursed, {counts['rejected']} rejected")

    print("\nMonths later: repayment outcomes arrive. The 'real world' has drifted from what the\n"
          "bootstrap model learned (utilisation and delinquencies matter more).")
    recorded, defaults = simulate_outcomes(system, rng)
    print(f"  recorded {recorded} outcomes, {defaults} defaults ({defaults / max(recorded, 1):.0%})")

    result = system.propose_retraining()
    print(f"\nReporting/Feedback agent proposes retraining ({result.feedback_used} outcomes + synthetic base data)")
    print(f"  hold-out log-loss  current model : {result.current_metrics['log_loss']:.3f}")
    print(f"  hold-out log-loss  candidate     : {result.candidate_metrics['log_loss']:.3f}"
          f"   -> {'IMPROVED' if result.improved else 'not better; keep current'}")
    if result.improved:
        system.promote_model(result, approved_by="Risk Manager (human approval)")
        print(f"  promoted {result.candidate.version} after human approval; live model is now {system.models.current.version}")

    sug = system.propose_threshold()
    if sug.suggested == sug.current:
        print(f"\nThreshold proposal: keep reject_pd at {sug.current:.2f} (no cheaper threshold within the "
              f"+/-0.10 cap on {sug.samples} observed outcomes)")
    else:
        print(f"\nThreshold proposal: reject_pd {sug.current:.2f} -> {sug.suggested:.2f} "
              f"(expected cost {sug.cost_at_current:,.0f} -> {sug.cost_at_suggested:,.0f}; {sug.note})")
    if sug.suggested != sug.current:
        system.apply_threshold(sug, approved_by="Risk Manager (human approval)")
        print(f"  applied after human approval; policy is now {system.config.policy.version}")
    print()


def main() -> None:
    p = argparse.ArgumentParser(description="Multi-agent loan processing system")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run the eight demo scenarios")
    d.add_argument("--interactive", action="store_true", help="you act as the loan officer")
    d.set_defaults(fn=cmd_demo)
    w = sub.add_parser("dashboard", help="launch the officer dashboard")
    w.add_argument("--port", type=int, default=5000)
    w.add_argument("--fresh", action="store_true", help="wipe the demo database first")
    w.set_defaults(fn=cmd_dashboard)
    f = sub.add_parser("feedback-demo", help="simulate outcomes and show the learning loop")
    f.add_argument("--cases", type=int, default=800,
                   help="learning needs volume: at ~250 the guard correctly declines to promote")
    f.set_defaults(fn=cmd_feedback_demo)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
