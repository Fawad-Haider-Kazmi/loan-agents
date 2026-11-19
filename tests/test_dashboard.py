import unittest

from tests.helpers import make_system, fresh_app
from loan_system.dashboard import create_app
from loan_system.samples import sample_scenarios
from loan_system.state import ApplicationStatus as S


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.system, self.scenarios, _ = make_system()
        self.client = create_app(self.system, list(self.scenarios.values())).test_client()

    def test_index_loads_demo_and_lists_pending(self):
        r = self.client.post("/demo/load", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("APP-1002", html)
        self.assertIn("awaiting your approval", html)
        self.assertEqual(len(self.system.pending_approvals()), 3)

    def test_officer_can_approve_through_the_ui(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        page = self.client.get("/case/APP-1002").get_data(as_text=True)
        self.assertIn("Your decision", page)
        self.client.post("/case/APP-1002/review",
                         data={"reviewer": "Officer Rana", "decision": "approve", "amount": "1200000", "notes": "ok"})
        ctx = self.system.get("APP-1002")
        self.assertEqual(ctx.status, S.COMPLETED)
        self.assertEqual(ctx.receipt.amount, 1_200_000)

    def test_ui_refuses_raising_the_amount(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1002"))
        r = self.client.post("/case/APP-1002/review",
                             data={"reviewer": "Officer", "decision": "approve", "amount": "9000000"},
                             follow_redirects=True)
        self.assertIn("Could not record decision", r.get_data(as_text=True))
        self.assertEqual(self.system.get("APP-1002").status, S.AWAITING_APPROVAL)

    def test_failed_case_can_be_retried_from_ui(self):
        self.system.bureau.outage = True
        self.system.submit(fresh_app(self.scenarios, "APP-1001"))
        self.system.bureau.outage = False
        self.assertIn("Retry from risk", self.client.get("/case/APP-1001").get_data(as_text=True))
        self.client.post("/case/APP-1001/retry")
        self.assertEqual(self.system.get("APP-1001").status, S.COMPLETED)

    def test_outcome_recording_and_unknown_case(self):
        self.system.submit(fresh_app(self.scenarios, "APP-1001"))
        self.client.post("/case/APP-1001/outcome", data={"result": "repaid"})
        self.assertEqual(len(self.system.repo.get_feedback()), 1)
        self.assertEqual(self.client.get("/case/NOPE").status_code, 404)

    def test_html_is_escaped(self):
        app = fresh_app(self.scenarios, "APP-1001")
        app.application_id = "X-XSS"
        app.applicant_name = "Ayesha Khan"
        ctx = self.system.submit(app)
        self.system.repo.add_audit("X-XSS", "<script>alert(1)</script>", "probe")
        html = self.client.get("/case/X-XSS").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)


if __name__ == "__main__":
    unittest.main()


def _form_for(scenario, **overrides):
    a = scenario.application
    texts = {d.doc_type.value: d.raw_text for d in a.documents}
    cr = scenario.credit
    form = {"name": a.applicant_name, "national_id": a.national_id, "account": a.account_number,
            "employer": a.employer, "income": str(int(a.stated_monthly_income)),
            "amount": str(int(a.requested_amount)), "tenure": str(a.tenure_months),
            "score": str(cr.score if cr else 600), "obligations": str(int(cr.monthly_obligations) if cr else 0),
            "delinquencies": str(cr.delinquencies if cr else 0), "utilization": str(cr.utilization if cr else 0),
            "doc_id": texts.get("national_id", ""), "doc_salary": texts.get("salary_slip", ""),
            "doc_bank": texts.get("bank_statement", "")}
    form.update(overrides)
    return form


class SubmitPageTests(unittest.TestCase):
    def setUp(self):
        self.system, self.scenarios, _ = make_system()
        self.client = create_app(self.system, list(self.scenarios.values())).test_client()

    def test_form_is_prefilled_from_sample(self):
        html = self.client.get("/submit?sample=APP-1002").get_data(as_text=True)
        self.assertIn("Bilal Ahmed", html)
        self.assertIn("human approval required", html)

    def test_submitting_through_the_ui_runs_the_agents(self):
        form = _form_for(self.scenarios["APP-1001"])
        r = self.client.post("/submit", data=form)
        self.assertEqual(r.status_code, 302)
        ctx = self.system.get("WEB-0001")
        self.assertEqual(ctx.status, S.COMPLETED)           # same profile as the straight-through demo
        self.assertEqual(ctx.receipt.amount, 300_000)

    def test_ui_credit_profile_changes_the_outcome(self):
        form = _form_for(self.scenarios["APP-1001"], score="500", delinquencies="4", utilization="0.9",
                         obligations="40000")
        self.client.post("/submit", data=form)
        ctx = self.system.get("WEB-0001")
        self.assertIsNone(ctx.receipt)                      # bad profile is not paid

    def test_empty_document_box_triggers_needs_info(self):
        self.client.post("/submit", data=_form_for(self.scenarios["APP-1001"], doc_bank=""))
        self.assertEqual(self.system.get("WEB-0001").status, S.NEEDS_INFO)

    def test_thin_file_checkbox(self):
        self.client.post("/submit", data=_form_for(self.scenarios["APP-1001"], thin="on"))
        self.assertEqual(self.system.get("WEB-0001").status, S.AWAITING_APPROVAL)

    def test_invalid_input_creates_nothing(self):
        r = self.client.post("/submit", data=_form_for(self.scenarios["APP-1001"], amount="-5"),
                             follow_redirects=True)
        self.assertIn("Could not submit", r.get_data(as_text=True))
        self.assertEqual(self.system.cases(), [])


class WorkflowPageTests(unittest.TestCase):
    def test_lists_live_agents_policy_and_diagrams(self):
        system, scenarios, _ = make_system()
        html = create_app(system, list(scenarios.values())).test_client().get("/workflow").get_data(as_text=True)
        for name in ("DocumentIntakeAgent", "ExecutionAgent", "ReportingFeedbackAgent"):
            self.assertIn(name, html)
        self.assertEqual(html.count('class="mermaid"'), 8)
        self.assertIn("PKR 500,000", html)


class LearningPageTests(unittest.TestCase):
    def setUp(self):
        self.system, self.scenarios, _ = make_system(min_feedback_samples=40)
        self.client = create_app(self.system, list(self.scenarios.values())).test_client()

    def test_page_loads_and_reports_not_enough_data(self):
        self.assertEqual(self.client.get("/learning").status_code, 200)
        self.client.post("/learning/retrain")
        self.assertIn("Not enough data yet", self.client.get("/learning").get_data(as_text=True))

    def test_simulation_creates_cases_and_outcomes(self):
        self.client.post("/learning/simulate", data={"n": "40"})
        sims = [c for c in self.system.cases() if c.application_id.startswith("SIM-")]
        self.assertEqual(len(sims), 40)
        self.assertGreater(len(self.system.repo.get_feedback()), 0)
        self.client.post("/learning/simulate", data={"n": "10"})       # ids continue, no collision
        self.assertEqual(len([c for c in self.system.cases() if c.application_id.startswith("SIM-")]), 50)

    def test_retrain_and_promote_requires_named_approver(self):
        from tests.test_feedback import _seed_feedback
        _seed_feedback(self.system, 400)
        self.client.post("/learning/retrain")
        page = self.client.get("/learning").get_data(as_text=True)
        self.assertIn("The candidate is better", page)
        before = self.system.models.current.version
        self.client.post("/learning/promote", data={"approver": "  "})
        self.assertEqual(self.system.models.current.version, before)    # blank approver refused
        self.client.post("/learning/promote", data={"approver": "Risk Manager"})
        self.assertNotEqual(self.system.models.current.version, before)
        self.assertIn("model promoted", self.client.get("/learning").get_data(as_text=True))

    def test_stale_proposal_is_discarded_when_new_outcomes_arrive(self):
        from tests.test_feedback import _seed_feedback
        _seed_feedback(self.system, 100)
        self.client.post("/learning/retrain")
        self.system.repo.add_feedback("EXTRA", {"dti": .3, "credit_score_norm": 0, "loan_to_annual_income": .5,
                                                "delinquencies": 0, "utilization": .3}, .1, False, 100_000)
        page = self.client.get("/learning").get_data(as_text=True)
        self.assertNotIn("Promote to live", page)

    def test_threshold_proposal_and_apply(self):
        for i in range(20):
            self.system.repo.add_feedback(f"T{i}", {"dti": .3, "credit_score_norm": 0, "loan_to_annual_income": .5,
                                                    "delinquencies": 0, "utilization": .3}, 0.30, True, 100_000)
        self.client.post("/learning/threshold")
        self.client.post("/learning/apply-threshold", data={"approver": "Risk Manager"})
        self.assertEqual(self.system.config.policy.reject_pd, 0.30)
