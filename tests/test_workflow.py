import unittest

from tests.helpers import make_system, fresh_app
from loan_system.errors import AuthorizationError
from loan_system.models import Recommendation
from loan_system.state import ApplicationStatus as S
from loan_system.system import build_system


class ScenarioOutcomeTests(unittest.TestCase):
    def test_all_eight_scenarios(self):
        system, scenarios, _ = make_system()
        expected = {
            "APP-1001": S.COMPLETED,          # straight-through approval
            "APP-1002": S.AWAITING_APPROVAL,  # large amount
            "APP-1003": S.COMPLETED,          # rejected (DTI)
            "APP-1004": S.NEEDS_INFO,
            "APP-1005": S.COMPLETED,          # rejected (blacklist)
            "APP-1006": S.AWAITING_APPROVAL,  # fraud + validation escalation
            "APP-1007": S.COMPLETED,          # approved after bureau retries
            "APP-1008": S.AWAITING_APPROVAL,  # thin file
        }
        for app_id, status in expected.items():
            ctx = system.submit(fresh_app(scenarios, app_id))
            self.assertEqual(ctx.status, status, app_id)
        paid = {a for a in expected if system.get(a).receipt}
        self.assertEqual(paid, {"APP-1001", "APP-1007"})
        self.assertEqual(system.bank.disburse_calls, 2)      # nobody else was paid

    def test_straight_through_has_no_human_review(self):
        system, scenarios, _ = make_system()
        ctx = system.submit(fresh_app(scenarios, "APP-1001"))
        self.assertIsNone(ctx.human_review)
        self.assertFalse(ctx.decision.requires_human)
        self.assertEqual(ctx.final_report.outcome, "approved")

    def test_agent_conversation_is_recorded(self):
        system, scenarios, _ = make_system()
        system.submit(fresh_app(scenarios, "APP-1001"))
        msgs = system.bus.conversation("APP-1001")
        senders = [m.sender for m in msgs if m.kind == "RESULT"]
        self.assertEqual(senders, ["DocumentIntakeAgent", "DataValidationAgent", "CreditRiskAgent",
                                   "FraudDetectionAgent", "DecisionAgent", "ExecutionAgent",
                                   "VerificationAgent", "ReportingFeedbackAgent"])


class HumanInTheLoopTests(unittest.TestCase):
    def setUp(self):
        self.system, self.scenarios, self.dir = make_system()

    def test_pause_then_approve_completes_and_pays(self):
        ctx = self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        self.assertEqual(ctx.status, S.AWAITING_APPROVAL)
        self.assertEqual(self.system.bank.disburse_calls, 0)             # nothing paid while waiting
        self.assertEqual(len(self.system.notifier.for_audience("officer")), 1)
        ctx = self.system.review("APP-1002", "Officer Rana", True, "income confirmed by phone")
        self.assertEqual(ctx.status, S.COMPLETED)
        self.assertEqual(ctx.receipt.amount, 1_500_000)
        self.assertEqual(ctx.human_review.reviewer, "Officer Rana")

    def test_human_can_reject_agent_recommendation(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        ctx = self.system.review("APP-1002", "Officer Rana", False, "too risky")
        self.assertEqual(ctx.status, S.COMPLETED)
        self.assertIsNone(ctx.receipt)
        self.assertEqual(ctx.final_report.outcome, "rejected")
        self.assertEqual(self.system.bank.disburse_calls, 0)

    def test_human_can_reduce_but_not_raise_amount(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        with self.assertRaises(ValueError):
            self.system.review("APP-1002", "Officer Rana", True, approved_amount=2_000_000)
        ctx = self.system.review("APP-1002", "Officer Rana", True, approved_amount=1_000_000)
        self.assertEqual(ctx.receipt.amount, 1_000_000)

    def test_review_requires_name_and_correct_state(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        with self.assertRaises(ValueError):
            self.system.review("APP-1002", "  ", True)
        self.system.submit(fresh_app(self.scenarios, "APP-1001"))
        with self.assertRaises(ValueError):                              # already completed
            self.system.review("APP-1001", "Officer Rana", True)

    def test_double_review_cannot_double_pay(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        self.system.review("APP-1002", "Officer A", True)
        with self.assertRaises(ValueError):
            self.system.review("APP-1002", "Officer B", True)
        self.assertEqual(self.system.bank.disburse_calls, 1)

    def test_approving_an_escalated_fraud_case_is_audited(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1006"))
        self.system.review("APP-1006", "Officer Rana", False, "income inflated")
        events = [(a["actor"], a["event"]) for a in self.system.repo.get_audit("APP-1006")]
        self.assertIn(("Officer Rana", "human_review"), events)

    def test_pause_survives_restart(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        restarted = build_system(self.system.config, bureau=self.system.bureau, bank=self.system.bank,
                                 notifier=self.system.notifier, sleep=lambda s: None)
        self.assertEqual([c.application_id for c in restarted.pending_approvals()], ["APP-1002"])
        ctx = restarted.review("APP-1002", "Officer Rana", True)
        self.assertEqual(ctx.status, S.COMPLETED)

    def test_execution_refuses_without_authorisation(self):
        ctx = self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        ctx.status = S.APPROVED                      # forge the status, but there is no human review
        with self.assertRaises(AuthorizationError):
            self.system.workflow._agents["execution"].run(ctx)
        self.assertEqual(self.system.bank.disburse_calls, 0)


class FailureHandlingTests(unittest.TestCase):
    def test_transient_bureau_errors_are_retried_automatically(self):
        system, scenarios, _ = make_system()
        ctx = system.submit(fresh_app(scenarios, "APP-1007"))     # bureau fails twice first
        self.assertEqual(ctx.status, S.COMPLETED)
        self.assertEqual(system.bureau.calls, 3)
        errors = [a for a in system.repo.get_audit("APP-1007") if a["event"] == "transient_error"]
        self.assertEqual(len(errors), 2)

    def test_hard_outage_fails_case_alerts_ops_and_retry_recovers(self):
        system, scenarios, _ = make_system()
        system.bureau.outage = True
        ctx = system.submit(fresh_app(scenarios, "APP-1001"))
        self.assertEqual(ctx.status, S.FAILED)
        self.assertEqual(ctx.failed_node, "risk")
        self.assertEqual(len(system.notifier.for_audience("operations")), 1)
        self.assertEqual(system.bank.disburse_calls, 0)

        system.bureau.outage = False                              # service restored
        ctx = system.retry("APP-1001")
        self.assertEqual(ctx.status, S.COMPLETED)                 # resumed at 'risk', not from scratch
        self.assertEqual(system.bank.disburse_calls, 1)

    def test_bank_outage_then_retry_pays_exactly_once(self):
        system, scenarios, _ = make_system(bank_fail_first=3)    # exhausts the 3 attempts
        ctx = system.submit(fresh_app(scenarios, "APP-1001"))
        self.assertEqual(ctx.status, S.FAILED)
        self.assertEqual(ctx.failed_node, "execution")
        ctx = system.retry("APP-1001")
        self.assertEqual(ctx.status, S.COMPLETED)
        self.assertEqual(system.bank.total_paid_out, 300_000)    # idempotent: one payment only

    def test_ledger_mismatch_stops_case_for_reconciliation(self):
        system, scenarios, _ = make_system()
        system.bank.tamper_amount_by = 5_000                      # bank books the wrong amount
        ctx = system.submit(fresh_app(scenarios, "APP-1001"))
        self.assertEqual(ctx.status, S.RECONCILIATION_REQUIRED)
        self.assertFalse(ctx.verification.matched)
        self.assertEqual(len(system.notifier.for_audience("operations")), 1)
        self.assertEqual(ctx.final_report.outcome, "approved_pending_reconciliation")

    def test_unexpected_exception_does_not_crash_workflow(self):
        system, scenarios, _ = make_system()
        system.workflow._agents["fraud"].run = lambda ctx: 1 / 0
        ctx = system.submit(fresh_app(scenarios, "APP-1001"))
        self.assertEqual(ctx.status, S.FAILED)
        self.assertIn("ZeroDivisionError", ctx.error)

    def test_duplicate_application_id_rejected(self):
        system, scenarios, _ = make_system()
        system.submit(fresh_app(scenarios, "APP-1001"))
        with self.assertRaises(ValueError):
            system.submit(fresh_app(scenarios, "APP-1001"))


if __name__ == "__main__":
    unittest.main()
