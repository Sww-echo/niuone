import copy
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "app/compat")]
_runtime = tempfile.TemporaryDirectory(prefix="niuone-decision-age-")
os.environ.setdefault("DASHBOARD_HOME", _runtime.name)
import niuniu_practice_trader as trader
from app.trading.decision_freshness import decision_expiry, decision_is_expired
from app.strategies.exits import niuone_stop_levels


class DecisionFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 10)
        self.position = {
            "code": "600000", "qty": 1000, "avg_cost": 10.0,
            "last_price": 9.4, "close": 9.4, "sell_score": 5,
            "buy_strategy": "niu_leader", "entry_stop_price": 9.5,
            "entry_stop_source": "niu_structure_low",
            "mainline_score": 70, "mainline_state": "mainline",
            "buy_date_lots": {"2026-09-08": 1000},
        }
        self.state = {"cash": 0.0, "positions": {"600000": self.position}, "trade_log": []}
        self.decision = {"decision_expires_at": decision_expiry(self.now), "actions": [
            {"action": "SELL", "code": "600000", "shares": 1000, "reason": "卖出"}
        ]}

    def test_expiry_boundary_and_malformed_values(self):
        self.assertFalse(decision_is_expired(self.decision, self.now + timedelta(seconds=179)))
        self.assertTrue(decision_is_expired(self.decision, self.now + timedelta(seconds=180)))
        self.assertTrue(decision_is_expired({"decision_expires_at": "bad"}, self.now))
        self.assertFalse(decision_is_expired({}, self.now))

    def test_late_decision_is_audited_without_quote_or_account_mutation(self):
        before = copy.deepcopy(self.state)
        with patch.object(trader, "execution_quote") as quote:
            result = trader.execute_actions(self.state, self.decision, [], True, "test", {},
                                            evaluated_at=self.now + timedelta(seconds=180))
        self.assertEqual(result, [])
        self.assertEqual(self.state, before)
        self.assertEqual(self.decision["actions"][0]["action"], "SELL")
        self.assertEqual(self.decision["execution_blocks"][0]["category"], "decision_expired")
        quote.assert_not_called()

    def test_deadline_rechecked_after_slow_quote(self):
        clock = [0.0]
        def quote(code):
            clock[0] = 181
            return {"price": 9.4}
        before = copy.deepcopy(self.state)
        with patch.object(trader.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(trader, "execution_quote", side_effect=quote), \
                patch.object(trader, "is_a_share_execution_time", return_value=(True, "test")), \
                patch.object(trader, "check_daily_loss_budget", return_value=(False, 0)):
            result = trader.execute_actions(self.state, self.decision, [], True, "test", {}, evaluated_at=self.now)
        self.assertEqual(result, [])
        self.assertEqual(self.state, before)

    def test_expired_deferred_decision_does_not_call_model(self):
        self.state["pending_decisions"] = [{"status": "pending", "due_at": "2026-09-09 09:30:00", "decision": self.decision}]
        self.decision["decision_expires_at"] = "2026-09-09 09:33:00"
        with ExitStack() as stack:
            for name, value in {"load_state": self.state, "is_a_share_execution_time": (True, "test"),
                                "record_equity": None, "save_state": None,
                                "_sync_committed_account_projections": {}}.items():
                stack.enter_context(patch.object(trader, name, return_value=value))
            model = stack.enter_context(patch.object(trader, "refine_overlimit_buy_actions"))
            trader.execute_due_pending_decisions(self.now)
        model.assert_not_called()
        self.assertEqual(self.state["pending_decisions"][0]["status"], "expired")

    def test_cost_stop_retains_original_structure_and_legacy_unknown(self):
        self.position.update(partial_tp_done=True, last_price=9.99, close=9.99)
        signal = trader.evaluate_sell_signal("600000", self.position, "2026-09-09")
        self.assertEqual(self.position["original_structural_stop_price"], 9.5)
        self.assertEqual(self.position["cost_protection_stop_price"], 10)
        self.assertEqual(signal["niuone_hard_exit_evidence"]["stop_kind"], "cost_protection")
        trader.evaluate_sell_signal("600000", self.position, "2026-09-09")
        self.assertEqual(self.position["original_structural_stop_price"], 9.5)
        legacy = {"entry_stop_price": 10, "entry_stop_source": "niu_breakeven", "partial_tp_done": True}
        self.assertIsNone(niuone_stop_levels(legacy, cost=10, break_even=True)["original_structural_stop_price"])

    def test_hard_exit_runs_during_model_cooldown(self):
        with ExitStack() as stack:
            model = stack.enter_context(patch.object(trader, "call_model_decision", side_effect=trader.ModelAdmissionError("rate_limited", 60)))
            guard = stack.enter_context(patch("core.model_api.shared_model_coordinator", side_effect=AssertionError("hard exits must not acquire model leases")))
            for name, value in {
                "is_a_share_execution_time": (True, "test"), "load_state": self.state,
                "load_latest_sector_tide_payload": {}, "refresh_realtime_prices": None,
                "refresh_position_intraday": None, "_refresh_position_bbi": None,
                "_refresh_frozen_prompt_position_exits": None, "update_zettaranc_volume_context": None,
                "sync_sector_tide_position_context": None, "sync_niuone_position_context": None,
                "sync_zettaranc_position_context": None, "enrich_portfolio": {},
                "_notify_trade_executions_safely": None,
            }.items():
                stack.enter_context(patch.object(trader, name, return_value=value))
            def commit(state, baseline, dt):
                signal = trader.evaluate_sell_signal("600000", state["positions"]["600000"], "2026-09-09")
                self.assertEqual(signal["signal"], "niu_structure_stop")
                return state, [{"action": "SELL", "shares": 1000, "code": "600000"}], {"durable_evidence_persisted": True}
            stack.enter_context(patch.object(trader, "_commit_refreshed_auto_exits", side_effect=commit))
            result = trader.run_auto_exits_once(self.now)
        self.assertEqual(result["executed_count"], 1)
        model.assert_not_called()
        guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
