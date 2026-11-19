import random
import unittest

from tests.helpers import make_system, fresh_app
from loan_system.ml.feedback_learning import ThresholdTuner
from loan_system.ml.risk_model import (
    FEATURES, LogisticRiskModel, auc, drifted_ground_truth_pd, synthetic_dataset, synthetic_features,
)
from loan_system.state import ApplicationStatus as S


class RiskModelTests(unittest.TestCase):
    def test_model_beats_chance_on_unseen_data(self):
        X, y = synthetic_dataset(1500, seed=1)
        Xt, yt = synthetic_dataset(800, seed=2)
        m = LogisticRiskModel().fit(X, y)
        self.assertGreater(m.evaluate(Xt, yt)["auc"], 0.70)

    def test_worse_profile_gets_higher_pd(self):
        X, y = synthetic_dataset(1500, seed=1)
        m = LogisticRiskModel().fit(X, y)
        good = dict(dti=0.1, credit_score_norm=1.0, loan_to_annual_income=0.1, delinquencies=0, utilization=0.1)
        bad = dict(dti=0.8, credit_score_norm=-1.0, loan_to_annual_income=1.5, delinquencies=4, utilization=0.9)
        self.assertLess(m.predict_proba(good), 0.05)
        self.assertGreater(m.predict_proba(bad), 0.5)

    def test_auc_perfect_and_random(self):
        self.assertEqual(auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]), 1.0)
        self.assertEqual(auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]), 0.5)

    def test_model_json_round_trip(self):
        X, y = synthetic_dataset(300, seed=3)
        m = LogisticRiskModel("vX").fit(X, y, epochs=50)
        m2 = LogisticRiskModel.from_dict(m.to_dict())
        f = dict(zip(FEATURES, X[0]))
        self.assertAlmostEqual(m.predict_proba(f), m2.predict_proba(f))


def _seed_feedback(system, n, seed=5):
    """Pretend n loans were repaid/defaulted in a world that has drifted from the bootstrap model."""
    rng = random.Random(seed)
    for i in range(n):
        f = synthetic_features(rng)
        f["delinquencies"] = float(f["delinquencies"])
        pd_model = system.models.current.predict_proba(f)
        defaulted = rng.random() < drifted_ground_truth_pd(f)
        system.repo.add_feedback(f"FB-{i}", f, pd_model, defaulted, 300_000)


class FeedbackLoopTests(unittest.TestCase):
    def test_outcome_recorded_only_for_disbursed_loans(self):
        system, scenarios, _ = make_system()
        system.submit(fresh_app(scenarios, "APP-1001"))
        system.submit(fresh_app(scenarios, "APP-1003"))
        system.record_outcome("APP-1001", defaulted=False)
        self.assertEqual(len(system.repo.get_feedback()), 1)
        with self.assertRaises(ValueError):
            system.record_outcome("APP-1003", defaulted=True)      # rejected, never paid

    def test_no_retraining_before_enough_data(self):
        system, _, _ = make_system(min_feedback_samples=40)
        _seed_feedback(system, 10)
        self.assertIsNone(system.propose_retraining())

    def test_retrained_model_beats_current_on_holdout_and_needs_human_promotion(self):
        system, scenarios, _ = make_system(min_feedback_samples=40)
        _seed_feedback(system, 400)
        before = system.models.current.version
        result = system.propose_retraining()
        self.assertTrue(result.improved, (result.current_metrics, result.candidate_metrics))
        self.assertEqual(system.models.current.version, before)    # proposal alone changes nothing
        with self.assertRaises(ValueError):
            system.promote_model(result, approved_by="")
        system.promote_model(result, approved_by="Risk Manager")
        self.assertEqual(system.models.current.version, result.candidate.version)
        # the live agent immediately uses the promoted model
        ctx = system.submit(fresh_app(scenarios, "APP-1001"))
        self.assertEqual(ctx.risk.model_version, result.candidate.version)

    def test_threshold_suggestion_is_capped_and_needs_approval(self):
        system, _, _ = make_system()
        # loans scored 0.30 keep defaulting -> a stricter threshold would have avoided the losses
        rows = [{"predicted_pd": 0.30, "defaulted": True, "approved_amount": 100_000}] * 20
        s = system.reporting._tuner.suggest(rows, current_threshold=0.35)
        self.assertLessEqual(s.suggested, 0.30)                    # tightened enough to avoid those losses
        self.assertGreaterEqual(s.suggested, 0.25)                 # but never beyond the +/-0.10 cap
        self.assertLess(s.cost_at_suggested, s.cost_at_current)
        self.assertEqual(s.suggested, 0.30)                        # ties -> smallest change from current
        self.assertEqual(system.config.policy.reject_pd, 0.35)     # not applied yet
        with self.assertRaises(ValueError):
            system.apply_threshold(s, approved_by=" ")
        system.apply_threshold(s, approved_by="Risk Manager")
        self.assertEqual(system.config.policy.reject_pd, s.suggested)

    def test_tuner_cost_function(self):
        t = ThresholdTuner(loss_given_default=0.6, margin=0.1)
        rows = [{"predicted_pd": 0.2, "defaulted": True, "approved_amount": 100},
                {"predicted_pd": 0.2, "defaulted": False, "approved_amount": 100}]
        self.assertAlmostEqual(t._cost(rows, 0.3), 60.0)           # approve both: lose 0.6*100
        self.assertAlmostEqual(t._cost(rows, 0.1), 10.0)           # reject both: lose margin on the good one


if __name__ == "__main__":
    unittest.main()
