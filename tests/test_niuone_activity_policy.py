from __future__ import annotations

import unittest

from app.strategies.policy import niuone_stock_activity_blocker


class NiuOneActivityPolicyTests(unittest.TestCase):
    def test_all_entry_routes_require_turnover_and_amount_activity(self):
        for strategy in ("niu_reversal_probe", "niu_emerging", "niu_leader", "niu_pullback"):
            valid = {
                "stock_activity_gate_required": True,
                "stock_activity_data_available": True,
                "stock_market_amount_percentile": 60.0,
                "stock_theme_amount_percentile": 50.0,
                "turnover": 3.0,
            }
            with self.subTest(strategy=strategy):
                self.assertIsNone(niuone_stock_activity_blocker(strategy, valid))
                for field, value in (
                    ("turnover", 2.999), ("turnover", None),
                    ("turnover", float("nan")), ("turnover", float("inf")),
                    ("turnover", True),
                    ("stock_market_amount_percentile", 59.999),
                    ("stock_theme_amount_percentile", 49.999),
                    ("stock_market_amount_percentile", float("nan")),
                    ("stock_theme_amount_percentile", float("inf")),
                    ("stock_activity_data_available", False),
                ):
                    with self.subTest(field=field, value=value):
                        self.assertIsNotNone(niuone_stock_activity_blocker(
                            strategy, {**valid, field: value},
                        ))

    def test_legacy_or_explicit_opt_out_cannot_bypass_a_new_buy_gate(self):
        for strategy in ("niu_reversal_probe", "niu_emerging", "niu_leader", "niu_pullback"):
            for context in ({}, {"stock_activity_gate_required": False}):
                with self.subTest(strategy=strategy, context=context):
                    self.assertIsNotNone(niuone_stock_activity_blocker(strategy, context))
        self.assertIsNone(niuone_stock_activity_blocker("shaofu_b1", {}))
