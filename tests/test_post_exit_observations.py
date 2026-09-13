#!/usr/bin/env python3
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
COMPAT = APP / "compat"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(COMPAT))

from storage import practice_db  # noqa: E402
from trading.post_exit_observations import (  # noqa: E402
    build_post_exit_observations,
    build_post_exit_reentry_observations,
)


def bars(symbol_price: float, *, future_closes: list[float], future_lows=None, future_highs=None):
    lows = future_lows or future_closes
    highs = future_highs or future_closes
    rows = [{"date": "2026-08-03", "close": symbol_price, "high": symbol_price, "low": symbol_price}]
    for index, close in enumerate(future_closes, start=4):
        rows.append({
            "date": f"2026-08-{index:02d}",
            "close": close,
            "high": highs[index - 4],
            "low": lows[index - 4],
        })
    return rows


class PostExitObservationTests(unittest.TestCase):
    def test_returns_are_anchored_to_actual_sell_execution_price(self):
        rows = build_post_exit_observations(
            [{
                "time": "2026-08-03 10:00:00",
                "action": "SELL",
                "code": "600000",
                "shares": 100,
                "price": 9.5,
                "reason": "test",
                "exit_rule": "no_progress",
            }],
            {
                "sh600000": bars(10.0, future_closes=[10.0] * 10),
                "sh000001": bars(100.0, future_closes=[100.0] * 10),
            },
            updated_at="2026-08-18 15:15:00",
        )

        five_day = next(row for row in rows if row["horizon"] == 5)
        self.assertEqual(five_day["price_basis"], "actual_execution")
        self.assertEqual(five_day["close_return_pct"], 5.2632)
        self.assertEqual(five_day["sell_notional"], 950.0)

    def test_five_day_labels_and_replacement_regret_use_forward_bars(self):
        trade = {
            "time": "2026-08-03 14:45:00",
            "action": "SELL",
            "code": "600000",
            "shares": 1000,
            "price": 10.0,
            "reason": "测试软退出",
            "exit_rule": "no_progress",
            "exit_signal": "no_progress",
            "buy_strategy": "niu_emerging",
            "position_fully_closed": True,
            "replacement_target_code": "000001",
            "replacement_execution_time": "2026-08-03 14:46:00",
            "replacement_execution_price": 20.0,
            "replacement_execution_shares": 500,
            "replacement_execution_fee": 0.0,
            "entry_atr20": 0.4,
            "exit_feedback_policy_version": 7,
        }
        stock = bars(
            10.0,
            future_closes=[10.2, 10.4, 10.8, 11.2, 12.0, 12.1, 12.2, 12.3, 12.4, 12.5],
            future_lows=[9.4, 9.8, 10.0, 10.4, 10.8, 11.0, 11.2, 11.4, 11.6, 11.8],
            future_highs=[10.6, 10.8, 11.0, 11.5, 12.5, 12.6, 12.7, 12.8, 12.9, 13.0],
        )
        replacement = bars(
            20.0,
            future_closes=[20.2, 20.4, 20.6, 20.8, 21.0, 21.1, 21.2, 21.3, 21.4, 21.5],
        )
        benchmark = bars(
            100.0,
            future_closes=[100.2, 100.4, 100.6, 100.8, 101.0, 101.1, 101.2, 101.3, 101.4, 101.5],
        )

        rows = build_post_exit_observations(
            [trade],
            {"sh600000": stock, "sz000001": replacement, "sh000001": benchmark},
            updated_at="2026-08-18 15:15:00",
        )
        five_day = next(row for row in rows if row["horizon"] == 5)

        self.assertEqual(five_day["completed"], 1)
        self.assertEqual(five_day["sell_fly"], 1)
        self.assertEqual(five_day["avoided_loss"], 1)
        self.assertEqual(five_day["close_return_pct"], 20.0)
        self.assertEqual(five_day["benchmark_return_pct"], 1.0)
        self.assertEqual(five_day["excess_return_pct"], 19.0)
        self.assertEqual(five_day["replacement_return_pct"], 5.0)
        self.assertEqual(five_day["replacement_counterfactual_return_pct"], 5.0)
        self.assertEqual(five_day["replacement_executed"], 1)
        self.assertEqual(five_day["replacement_regret_pct"], 15.0)
        self.assertEqual(five_day["replacement_regret"], 1)
        self.assertEqual(five_day["sell_fly_threshold_pct"], 5.0)
        self.assertEqual(five_day["feedback_policy_version"], 7)

    def test_missing_sell_date_bar_is_persistable_as_pending(self):
        rows = build_post_exit_observations(
            [{
                "time": "2026-08-03 14:45:00",
                "action": "SELL",
                "code": "600000",
                "shares": 100,
                "price": 10.0,
                "reason": "test",
            }],
            {"sh600000": [{"date": "2026-08-04", "close": 10.2}]},
            updated_at="2026-08-04 15:15:00",
        )

        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["completed"] == 0 for row in rows))
        self.assertTrue(all(row["quality_status"] == "missing_sell_date_bar" for row in rows))

    def test_reentry_shadow_observation_uses_candidate_execution_price(self):
        rows = build_post_exit_reentry_observations(
            [{
                "audit_key": "audit-1",
                "observed_at": "2026-08-03 10:00:00",
                "code": "600000",
                "candidate_price": 9.5,
                "exit_date": "2026-08-01",
                "feedback_policy_version": 2,
                "eligible": 0,
                "executed": 0,
                "reclaim_passed": 1,
                "volume_supportive": 0,
                "thesis_valid": 1,
            }],
            {"sh600000": bars(10.0, future_closes=[10.0] * 10)},
            updated_at="2026-08-18 15:15:00",
        )

        self.assertEqual(rows[0]["completed"], 1)
        self.assertEqual(rows[0]["price_basis"], "actual_execution")
        self.assertEqual(rows[0]["future_return_pct"], 5.2632)

    def test_unexecuted_replacement_remains_counterfactual_only(self):
        rows = build_post_exit_observations(
            [{
                "time": "2026-08-03 14:45:00",
                "action": "SELL",
                "code": "600000",
                "shares": 100,
                "price": 10.0,
                "reason": "replace candidate only",
                "exit_rule": "position_adjust",
                "replacement_target_code": "000001",
            }],
            {
                "sh600000": bars(10.0, future_closes=[11.0] * 10),
                "sz000001": bars(20.0, future_closes=[21.0] * 10),
                "sh000001": bars(100.0, future_closes=[100.0] * 10),
            },
            updated_at="2026-08-18 15:15:00",
        )

        five_day = next(row for row in rows if row["horizon"] == 5)
        self.assertEqual(five_day["replacement_executed"], 0)
        self.assertIsNone(five_day["replacement_return_pct"])
        self.assertEqual(five_day["replacement_counterfactual_return_pct"], 5.0)
        self.assertIsNone(five_day["replacement_regret"])

    def test_observation_upsert_is_idempotent_and_summary_uses_completed_5d(self):
        original_path = practice_db.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            practice_db.DB_PATH = Path(temp_dir) / "practice.db"
            try:
                practice_db.init_db()
                row = {
                    "trade_key": "trade-1",
                    "horizon": 5,
                    "sell_time": "2026-08-03 14:45:00",
                    "code": "600000",
                    "sell_price": 10.0,
                    "sell_notional": 1000.0,
                    "price_basis": "actual_execution",
                    "shares": 100,
                    "full_exit": 1,
                    "exit_rule": "no_progress",
                    "exit_signal": "no_progress",
                    "buy_strategy": "niu_emerging",
                    "replacement_target_code": "",
                    "sessions_observed": 5,
                    "observation_date": "2026-08-10",
                    "close_return_pct": 3.0,
                    "mfe_pct": 6.0,
                    "mae_pct": -5.5,
                    "benchmark_return_pct": 1.0,
                    "excess_return_pct": 2.0,
                    "replacement_return_pct": None,
                    "replacement_counterfactual_return_pct": None,
                    "replacement_regret_pct": None,
                    "replacement_regret": None,
                    "replacement_executed": 0,
                    "replacement_buy_time": "",
                    "replacement_buy_price": None,
                    "replacement_buy_shares": 0,
                    "replacement_buy_fee": 0.0,
                    "sell_fly_threshold_pct": 5.0,
                    "sell_fly": 1,
                    "avoided_loss": 1,
                    "completed": 1,
                    "quality_status": "complete",
                    "updated_at": "2026-08-10 15:15:00",
                }
                practice_db.upsert_post_exit_observations([row])
                row["close_return_pct"] = 3.5
                practice_db.upsert_post_exit_observations([row])

                summary = practice_db.query_post_exit_observation_summary()
                with sqlite3.connect(practice_db.DB_PATH) as connection:
                    count = connection.execute(
                        "SELECT count(*) FROM post_exit_observations"
                    ).fetchone()[0]
                self.assertEqual(count, 1)
                self.assertEqual(summary["completed_5d_count"], 1)
                self.assertEqual(summary["sell_fly_5d_count"], 1)
                self.assertEqual(summary["avg_close_return_5d_pct"], 3.0)

                pending = {
                    **row,
                    "sessions_observed": 0,
                    "observation_date": "",
                    "close_return_pct": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "sell_fly": None,
                    "avoided_loss": None,
                    "completed": 0,
                    "quality_status": "missing_sell_date_bar",
                }
                practice_db.upsert_post_exit_observations([pending])
                with sqlite3.connect(practice_db.DB_PATH) as connection:
                    preserved = connection.execute(
                        "SELECT completed,quality_status,close_return_pct "
                        "FROM post_exit_observations WHERE trade_key='trade-1'"
                    ).fetchone()
                self.assertEqual(preserved, (1, "complete", 3.0))
            finally:
                practice_db.DB_PATH = original_path

    def test_feedback_policy_activation_is_versioned_and_idempotent(self):
        original_path = practice_db.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            practice_db.DB_PATH = Path(temp_dir) / "practice.db"
            try:
                practice_db.init_db()
                base = {
                    "algorithm_version": "test-v1",
                    "created_at": "2026-08-10 15:15:00",
                    "effective_date": "2026-08-10",
                    "status": "active",
                    "action": "hold",
                    "reason": "test",
                    "observation_count": 30,
                    "new_observation_count": 30,
                    "observation_span_months": 3,
                    "source_fingerprint": "fingerprint-1",
                    "parameters": {"soft_exit_confirmations": 2},
                    "metrics": {},
                    "baseline_metrics": {},
                    "previous_parameters": {},
                }
                first = practice_db.record_exit_feedback_policy(base)
                duplicate = practice_db.record_exit_feedback_policy(base)
                second = practice_db.record_exit_feedback_policy({
                    **base,
                    "source_fingerprint": "fingerprint-2",
                    "parameters": {"soft_exit_confirmations": 3},
                })

                active = practice_db.query_active_exit_feedback_policy()
                with sqlite3.connect(practice_db.DB_PATH) as connection:
                    count = connection.execute(
                        "SELECT count(*) FROM exit_feedback_policies"
                    ).fetchone()[0]
                    active_count = connection.execute(
                        "SELECT count(*) FROM exit_feedback_policies WHERE active=1"
                    ).fetchone()[0]
                self.assertEqual(first["version"], duplicate["version"])
                self.assertEqual(count, 2)
                self.assertEqual(active_count, 1)
                self.assertEqual(active["version"], second["version"])
                self.assertEqual(active["parameters"]["soft_exit_confirmations"], 3)
                with self.assertRaisesRegex(RuntimeError, "inactive policy"):
                    practice_db.record_exit_feedback_policy(base)

                evaluation = {
                    "evaluated_at": "2026-08-10 15:16:00",
                    "algorithm_version": "test-v2",
                    "source_fingerprint": "evaluation-1",
                    "status": "hold",
                    "action": "hold",
                    "reason": "test",
                    "observation_count": 40,
                    "observation_span_months": 3,
                    "policy_version": second["version"],
                    "parameters": second["parameters"],
                    "metrics": {"objective_regret_score": 0.0},
                }
                practice_db.record_exit_feedback_evaluation(evaluation)
                practice_db.record_exit_feedback_evaluation(evaluation)
                latest = practice_db.query_latest_exit_feedback_evaluation()
                self.assertEqual(latest["observation_count"], 40)
                with sqlite3.connect(practice_db.DB_PATH) as connection:
                    evaluation_count = connection.execute(
                        "SELECT count(*) FROM exit_feedback_evaluations"
                    ).fetchone()[0]
                self.assertEqual(evaluation_count, 1)
            finally:
                practice_db.DB_PATH = original_path

    def test_durable_trades_and_decisions_link_actual_feedback_events(self):
        original_path = practice_db.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            practice_db.DB_PATH = Path(temp_dir) / "practice.db"
            try:
                practice_db.init_db()
                sell = {
                    "time": "2026-08-03 14:45:00", "action": "SELL",
                    "code": "600000", "name": "old", "shares": 100,
                    "price": 10.0, "amount": 1000.0, "reason": "replace",
                    "replacement_target_code": "000001",
                }
                buy = {
                    "time": "2026-08-03 14:46:00", "action": "BUY",
                    "code": "000001", "name": "new", "shares": 100,
                    "price": 20.0, "amount": 2000.0, "reason": "replace",
                    "replacement_source_code": "600000", "fee": 5.0,
                }
                self.assertTrue(practice_db.record_trade(sell))
                self.assertTrue(practice_db.record_trade(buy))
                linked = practice_db.query_post_exit_sell_trades()
                self.assertEqual(linked[0]["replacement_execution_price"], 20.0)
                self.assertEqual(linked[0]["replacement_execution_shares"], 100)

                audit = {
                    "exit_date": "2026-08-01",
                    "execution_price": 20.0,
                    "exit_feedback_policy_version": 2,
                    "eligible": True,
                    "reclaim_passed": True,
                    "volume_supportive": True,
                    "thesis_valid": True,
                    "volume_ratio": 1.1,
                    "amount_percentile": 65.0,
                    "required_volume_ratio": 1.0,
                    "required_amount_percentile": 60.0,
                }
                self.assertTrue(practice_db.record_decision({
                    "time": "2026-08-04 10:00:00",
                    "decision": {
                        "actions": [{
                            "action": "BUY",
                            "code": "000001",
                            "post_exit_reentry_audit": audit,
                        }],
                    },
                    "executed": [{
                        "action": "BUY",
                        "code": "000001",
                        "post_exit_reentry_audit": audit,
                    }],
                }))
                audits = practice_db.query_post_exit_reentry_audits()
                self.assertEqual(len(audits), 1)
                self.assertEqual(audits[0]["eligible"], 1)
                self.assertEqual(audits[0]["executed"], 1)
            finally:
                practice_db.DB_PATH = original_path

    def test_replacement_buy_outside_execution_window_is_not_linked(self):
        original_path = practice_db.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            practice_db.DB_PATH = Path(temp_dir) / "practice.db"
            try:
                practice_db.init_db()
                self.assertTrue(practice_db.record_trade({
                    "time": "2026-08-03 14:00:00",
                    "action": "SELL",
                    "code": "600000",
                    "name": "old",
                    "shares": 100,
                    "price": 10.0,
                    "amount": 1000.0,
                    "reason": "replace",
                    "replacement_target_code": "000001",
                }))
                self.assertTrue(practice_db.record_trade({
                    "time": "2026-08-03 15:00:01",
                    "action": "BUY",
                    "code": "000001",
                    "name": "new",
                    "shares": 100,
                    "price": 20.0,
                    "amount": 2000.0,
                    "reason": "replace",
                    "replacement_source_code": "600000",
                }))

                linked = practice_db.query_post_exit_sell_trades()

                self.assertNotIn("replacement_execution_price", linked[0])
            finally:
                practice_db.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
