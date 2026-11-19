import unittest

from tests.helpers import make_system, fresh_app
from loan_system import serialization
from loan_system.errors import DocumentParseError, IllegalTransition
from loan_system.finance import monthly_payment
from loan_system.models import Document, DocumentType, Severity
from loan_system.services.document_parser import KeyValueDocumentParser
from loan_system.state import ApplicationStatus, WorkflowContext


class FinanceTests(unittest.TestCase):
    def test_emi_known_value(self):
        # 100,000 at 12% p.a. over 12 months -> 8,884.88 (standard amortisation table)
        self.assertAlmostEqual(monthly_payment(100_000, 0.12, 12), 8884.88, places=2)

    def test_zero_rate(self):
        self.assertAlmostEqual(monthly_payment(1200, 0, 12), 100.0)


class ParserTests(unittest.TestCase):
    parser = KeyValueDocumentParser()

    def test_parses_salary_slip_with_commas(self):
        doc = Document(DocumentType.SALARY_SLIP, "s", "Employer: X\nEmployee: Y\nMonthly Salary: 150,000\nMonth: 2026-09")
        self.assertEqual(self.parser.parse(doc).monthly_salary, 150_000)

    def test_missing_field_raises(self):
        with self.assertRaises(DocumentParseError):
            self.parser.parse(Document(DocumentType.NATIONAL_ID, "id", "Name: X"))

    def test_bad_number_raises(self):
        doc = Document(DocumentType.SALARY_SLIP, "s", "Employer: X\nEmployee: Y\nMonthly Salary: lots\nMonth: m")
        with self.assertRaises(DocumentParseError):
            self.parser.parse(doc)


class StateTests(unittest.TestCase):
    def test_illegal_transition_rejected(self):
        system, scenarios, _ = make_system()
        ctx = WorkflowContext(application=fresh_app(scenarios, "APP-1001"))
        with self.assertRaises(IllegalTransition):
            ctx.transition(ApplicationStatus.DISBURSED)     # cannot skip straight to money

    def test_fail_and_recover(self):
        system, scenarios, _ = make_system()
        ctx = WorkflowContext(application=fresh_app(scenarios, "APP-1001"))
        ctx.transition(ApplicationStatus.DOCUMENTS_PARSED)
        ctx.fail("validation", "boom")
        self.assertEqual(ctx.status, ApplicationStatus.FAILED)
        self.assertEqual(ctx.recover(), "validation")
        self.assertEqual(ctx.status, ApplicationStatus.DOCUMENTS_PARSED)

    def test_full_context_survives_db_round_trip(self):
        system, scenarios, _ = make_system()
        system.submit(fresh_app(scenarios, "APP-1001"))
        loaded = system.get("APP-1001")
        again = serialization.from_json(WorkflowContext, serialization.to_json(loaded))
        self.assertEqual(again.status, ApplicationStatus.COMPLETED)
        self.assertEqual(again.receipt.transaction_id, loaded.receipt.transaction_id)
        self.assertEqual(again.risk.risk_band, loaded.risk.risk_band)


class AgentUnitTests(unittest.TestCase):
    def test_validation_flags_inflated_income_as_blocking(self):
        system, scenarios, _ = make_system()
        ctx = system.submit(fresh_app(scenarios, "APP-1006"))
        codes = {i.code: i.severity for i in ctx.validation.issues}
        self.assertEqual(codes["INCOME_MISMATCH"], Severity.BLOCKING)

    def test_missing_document_stops_flow_with_needs_info(self):
        system, scenarios, _ = make_system()
        ctx = system.submit(fresh_app(scenarios, "APP-1004"))
        self.assertEqual(ctx.status, ApplicationStatus.NEEDS_INFO)
        self.assertIn("bank_statement", ctx.validation.missing_documents)
        self.assertIsNone(ctx.risk)                          # never scored an incomplete file

    def test_unreadable_document_treated_as_missing(self):
        system, scenarios, _ = make_system()
        app = fresh_app(scenarios, "APP-1001")
        app.application_id = "X-GARBLED"
        app.documents[1].raw_text = "### illegible ###"
        ctx = system.submit(app)
        self.assertEqual(ctx.status, ApplicationStatus.NEEDS_INFO)
        self.assertTrue(any("unreadable" in m for m in ctx.validation.missing_documents))

    def test_risk_uses_verified_not_stated_income(self):
        system, scenarios, _ = make_system()
        ctx = system.submit(fresh_app(scenarios, "APP-1006"))
        self.assertEqual(ctx.risk.verified_income, 120_000)

    def test_duplicate_applications_raise_fraud_signal(self):
        system, scenarios, _ = make_system()
        system.submit(fresh_app(scenarios, "APP-1001"))
        again = fresh_app(scenarios, "APP-1001")
        again.application_id = "APP-1001-B"
        ctx = system.submit(again)
        self.assertIn("DUPLICATE_APPLICATION", [s.code for s in ctx.fraud.signals])
        self.assertTrue(ctx.decision.requires_human)         # no longer straight-through

    def test_shared_disbursement_account_flagged(self):
        system, scenarios, _ = make_system()
        system.submit(fresh_app(scenarios, "APP-1001"))
        other = fresh_app(scenarios, "APP-1007")
        other.application_id = "APP-MULE"
        other.account_number = "PK01-1001"                   # same account as Ayesha
        for d in other.documents:
            d.raw_text = d.raw_text.replace("PK01-1007", "PK01-1001")
        ctx = system.submit(other)
        self.assertIn("SHARED_ACCOUNT", [s.code for s in ctx.fraud.signals])

    def test_blacklist_is_rejected_with_generic_notice(self):
        system, scenarios, _ = make_system()
        ctx = system.submit(fresh_app(scenarios, "APP-1005"))
        self.assertEqual(ctx.status, ApplicationStatus.COMPLETED)
        self.assertIsNone(ctx.receipt)
        notice = ctx.final_report.applicant_notice.lower()
        self.assertNotIn("blacklist", notice)
        self.assertNotIn("fraud", notice)

    def test_thin_file_escalates(self):
        system, scenarios, _ = make_system()
        ctx = system.submit(fresh_app(scenarios, "APP-1008"))
        self.assertEqual(ctx.status, ApplicationStatus.AWAITING_APPROVAL)
        self.assertIn("thin file", " ".join(ctx.decision.reasons))


if __name__ == "__main__":
    unittest.main()
