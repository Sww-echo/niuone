"""Model prose cannot grant an unverified NiuOne hard exit."""
import copy
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "app/compat")]
_runtime = tempfile.TemporaryDirectory(prefix="niuone-model-exit-")
os.environ.setdefault("DASHBOARD_HOME", _runtime.name)
import niuniu_practice_trader as trader
from app.trading.niuone_forward import _summarize_niuone_sell_execution


class NiuOneModelExitEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "cash": 0.0,
            "positions": {"600000": {
                "code": "600000", "qty": 1000, "avg_cost": 10.0,
                "last_price": 10.0, "close": 10.0, "sell_score": 3,
                "buy_strategy": "niu_leader", "entry_stop_price": 9.5,
                "entry_stop_source": "niu_structure_low",
                "mainline_score": 70.0, "mainline_state": "mainline",
                "buy_date_lots": {"2026-06-23": 1000},
            }},
            "trade_log": [],
        }

    def execute(self, reason, *, price=10.0, day=24, action="SELL"):
        decision = {"actions": [{
            "action": action, "code": "600000", "shares": 1000,
            "reason": reason,
            # A model-supplied signal is not evidence either.
            "source_signal": "niu_structure_stop", "exit_rule": "stop_loss",
        }]}
        with patch.object(trader, "is_a_share_execution_time", return_value=(True, "test")), \
                patch.object(trader, "execution_quote", return_value={"price": price, "source": "test"}):
            fills = trader.execute_actions(
                self.state, decision, [], True, "test", {},
                evaluated_at=datetime(2026, 6, day, 10, 0),
            )
        return fills, decision

    def test_words_unknown_and_missing_reasons_cannot_bypass_staging(self):
        original = copy.deepcopy(self.state)
        for reason in ("结构尚未破位，降低仓位", "触发止损", "市场硬停止", "主线失活", "逃顶", "暂时卖出", ""):
            with self.subTest(reason=reason):
                self.state = copy.deepcopy(original)
                fills, _ = self.execute(reason)
                self.assertEqual(fills[0]["shares"], 500)
                self.assertEqual(fills[0]["soft_exit_stage"], "reduce")
                self.assertEqual(fills[0]["exit_signal"], "model_soft_exit")
                self.assertFalse(fills[0]["niuone_hard_exit_evidence"]["confirmed"])
                self.assertNotEqual(fills[0]["exit_rule"], "stop_loss")

    def test_quote_below_structural_stop_allows_hard_exit_despite_high_score(self):
        self.state["positions"]["600000"]["sell_score"] = 5
        fills, _ = self.execute("考虑减仓", price=9.4)
        self.assertEqual(fills[0]["shares"], 1000)
        self.assertEqual(fills[0]["exit_signal"], "niu_structure_stop")
        self.assertTrue(fills[0]["niuone_hard_exit_evidence"]["confirmed"])

    def test_inactive_theme_and_compound_market_stop_still_exit_immediately(self):
        original = copy.deepcopy(self.state)
        for updates, signal in (
            ({"mainline_state": "inactive"}, "niu_mainline_faded"),
            ({"market_hard_stop": True, "mainline_score": 50}, "niu_market_hard_stop"),
            ({"partial_tp_done": True}, "niu_structure_stop"),
        ):
            with self.subTest(signal=signal):
                self.state = copy.deepcopy(original)
                self.state["positions"]["600000"].update(updates)
                fills, _ = self.execute("风险控制", price=9.9)
                self.assertEqual(fills[0]["shares"], 1000)
                self.assertEqual(fills[0]["exit_signal"], signal)

    def test_local_stop_uses_current_price_instead_of_stale_daily_close(self):
        pos = self.state["positions"]["600000"]
        pos.update(last_price=9.4, close=10.0)
        signal = trader.evaluate_sell_signal("600000", pos, "2026-06-24")
        self.assertEqual(signal["signal"], "niu_structure_stop")
        pos.update(last_price=10.0, close=9.4)
        self.assertIsNone(trader.evaluate_sell_signal("600000", pos, "2026-06-24"))

    def test_local_check_and_same_day_retry_do_not_erase_or_repeat_model_reduction(self):
        first, _ = self.execute("结构尚未破位，降低仓位")
        self.assertEqual(first[0]["shares"], 500)
        pos = self.state["positions"]["600000"]
        self.assertIsNone(trader.evaluate_sell_signal("600000", pos, "2026-06-24"))
        repeated, _ = self.execute("触发止损")
        self.assertEqual(repeated, [])
        self.assertIsNone(trader.evaluate_sell_signal("600000", pos, "2026-06-25"))
        final, _ = self.execute("仍需降低仓位", day=25)
        self.assertEqual(final[0]["shares"], 500)
        self.assertEqual(final[0]["soft_exit_stage"], "exit")

    def test_explicit_model_hold_clears_model_confirmation(self):
        self.execute("降低仓位")
        self.execute("继续观察", action="HOLD")
        pos = self.state["positions"]["600000"]
        self.assertNotIn("soft_exit_pending_count", pos)
        fills, _ = self.execute("降低仓位", day=25)
        self.assertEqual(fills, [])

    def test_one_lot_and_score_veto_preserve_position_until_confirmation(self):
        pos = self.state["positions"]["600000"]
        pos.update(qty=100, buy_date_lots={"2026-06-23": 100}, sell_score=5)
        fills, decision = self.execute("触发止损")
        self.assertEqual(fills, [])
        self.assertFalse(decision["actions"][0]["niuone_hard_exit_evidence"]["confirmed"])
        pos["sell_score"] = 3
        self.assertEqual(self.execute("触发止损")[0], [])
        self.assertEqual(self.execute("触发止损", day=25)[0][0]["shares"], 100)

    def test_forward_audit_accepts_staged_fills_and_rejects_missing_or_forged_evidence(self):
        pos = self.state["positions"]["600000"]
        pos.update(qty=800, buy_date_lots={"2026-06-23": 800})
        first, _ = self.execute("未破位，降低仓位")
        second, _ = self.execute("仍需降低仓位", day=25)
        self.assertEqual([first[0]["shares"], second[0]["shares"]], [400, 400])
        rows = first + second
        for index, row in enumerate(rows):
            row.update(_forward_payload_available=True, time=f"2026-06-{24 + index} 10:00:00")

        def audit(records):
            return _summarize_niuone_sell_execution(
                records, cohort_start=date(2026, 6, 24), as_of=date(2026, 6, 25),
            )["sell_execution_data_quality_gate_met"]

        self.assertTrue(audit(rows))
        missing = copy.deepcopy(first[0])
        missing.pop("niuone_hard_exit_evidence")
        self.assertFalse(audit([missing]))
        forged = copy.deepcopy(first[0])
        forged["niuone_hard_exit_evidence"]["confirmed"] = True
        forged["shares"] = 800
        self.assertFalse(audit([forged]))
        premature = copy.deepcopy(second[0])
        premature["soft_exit_confirmation_count"] = 1
        self.assertFalse(audit([premature]))

    def test_stop_boundary_and_removed_percentage_fallback_do_not_trigger_hard_exit(self):
        original = copy.deepcopy(self.state)
        for updates, price in (({}, 9.5), ({"shaofu_stop_source": "fallback_pct"}, 9.4)):
            with self.subTest(updates=updates):
                self.state = copy.deepcopy(original)
                self.state["positions"]["600000"].update(updates)
                fills, _ = self.execute("触发止损", price=price)
                self.assertEqual(fills[0]["shares"], 500)
                self.assertFalse(fills[0]["niuone_hard_exit_evidence"]["confirmed"])


if __name__ == "__main__":
    unittest.main()
