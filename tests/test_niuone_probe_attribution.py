"""Probe membership eligibility uses existing attribution boundaries."""
import copy
import unittest

from app.strategies.policy import (
    candidate_buy_blockers,
    niu_reversal_theme_attribution_blocker,
)
from app.strategies.scoring.common import with_strategy_profile


class NiuOneProbeAttributionTests(unittest.TestCase):
    def candidate(self, *, score=70.0, weight=0.15):
        return {
            "best_strategy": "niu_reversal_probe",
            "signal_theme": "主题甲",
            "signal_theme_attribution_score": score,
            "signal_theme_attribution_weight": weight,
            "stock_strong": False,
            "stock_leader_tier": False,
        }

    def test_breadth_boundary_does_not_require_stock_leadership(self):
        for weight in (0.15, 0.5, 1.0):
            self.assertIsNone(niu_reversal_theme_attribution_blocker(self.candidate(weight=weight)))
        self.assertIsNotNone(niu_reversal_theme_attribution_blocker(self.candidate(weight=0.149999)))

    def test_diluted_primary_exception_requires_matching_numerical_evidence(self):
        candidate = self.candidate(score=60.0, weight=0.08)
        candidate["theme_attributions"] = [
            {"theme": "主题甲", "attribution_score": 60.0, "attribution_weight": 0.08},
            {"theme": "主题乙", "attribution_score": 55.0, "attribution_weight": 0.07},
        ]
        original = copy.deepcopy(candidate)
        for _ in range(2):
            self.assertIsNone(niu_reversal_theme_attribution_blocker(candidate))
        self.assertEqual(candidate, original)
        for field, value in (("signal_theme", "主题乙"),
                             ("signal_theme_attribution_score", 60.01),
                             ("signal_theme_attribution_weight", 0.09)):
            with self.subTest(field=field):
                self.assertIsNotNone(niu_reversal_theme_attribution_blocker({**candidate, field: value}))
        candidate["theme_attributions"][1]["attribution_score"] = 75.0
        self.assertIsNotNone(niu_reversal_theme_attribution_blocker(candidate))

    def test_tied_primary_preserves_scorer_order(self):
        candidate = self.candidate(weight=0.08)
        candidate["signal_theme"] = "主题乙"
        candidate["theme_attributions"] = [
            {"theme": "主题乙", "attribution_score": 70.0, "attribution_weight": 0.08},
            {"theme": "主题甲", "attribution_score": 70.0, "attribution_weight": 0.08},
        ]
        self.assertIsNone(niu_reversal_theme_attribution_blocker(candidate))
        candidate["signal_theme"] = "主题甲"
        self.assertIsNotNone(niu_reversal_theme_attribution_blocker(candidate))

    def test_invalid_or_missing_evidence_fails_closed(self):
        for field in ("signal_theme_attribution_score", "signal_theme_attribution_weight"):
            for invalid in (None, "bad", True, float("nan"), float("inf"), -1, 101):
                with self.subTest(field=field, invalid=invalid):
                    self.assertIsNotNone(niu_reversal_theme_attribution_blocker({
                        **self.candidate(), field: invalid,
                    }))
        for attrs in (None, [], [{}], ["invalid"]):
            self.assertIsNotNone(niu_reversal_theme_attribution_blocker({
                **self.candidate(weight=0.08), "theme_attributions": attrs,
                "theme_attribution_confident": True,
            }))
        self.assertIsNotNone(niu_reversal_theme_attribution_blocker({}))

    def test_scorer_and_execution_share_the_same_blocker(self):
        candidate = self.candidate(score=22, weight=0.008)
        candidate.update(score=9.9, actionable=True, hard_blockers=[])
        expected = niu_reversal_theme_attribution_blocker(candidate)
        result = with_strategy_profile("niu_reversal_probe", dict(candidate))
        self.assertFalse(result["actionable"])
        self.assertIn(expected, result["hard_blockers"])
        self.assertIn(expected, candidate_buy_blockers(candidate))
        for strategy in ("niu_emerging", "niu_leader", "niu_pullback", "shaofu_b1"):
            self.assertNotIn(expected, candidate_buy_blockers({**candidate, "best_strategy": strategy}))
