#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
COMPAT = APP / "compat"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(COMPAT))

import niuniu_practice_trader as trader  # noqa: E402
from screening.multi_strategy import prepare_strategy_rows  # noqa: E402
from storage.prompt_strategies import PromptStrategyStore  # noqa: E402
from strategies.prompt_refinement import refine_prompt_once  # noqa: E402
from strategies.prompt_runtime import score_prompt_selection  # noqa: E402
from strategies.rules import replay_rule_evaluation_audit  # noqa: E402

from test_prompt_rule_engine import kdj_spec  # noqa: E402
from test_prompt_trading_flow import market_context  # noqa: E402


def price_rows(values, *, end_date):
    start = end_date - timedelta(days=len(values) - 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "open": value,
            "high": value + 0.2,
            "low": value - 0.2,
            "close": value,
            "volume": 1000 + index,
        }
        for index, value in enumerate(values)
    ]


class PromptStrategyEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="niuone-prompt-e2e-")
        self.old_db = os.environ.get("DASHBOARD_PROMPT_STRATEGY_DB")
        self.old_active = os.environ.get(trader.ACTIVE_STRATEGY_ENV)
        os.environ["DASHBOARD_PROMPT_STRATEGY_DB"] = str(
            Path(self.temp_dir.name) / "prompt.db"
        )
        os.environ[trader.ACTIVE_STRATEGY_ENV] = "preset_text"

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("DASHBOARD_PROMPT_STRATEGY_DB", None)
        else:
            os.environ["DASHBOARD_PROMPT_STRATEGY_DB"] = self.old_db
        if self.old_active is None:
            os.environ.pop(trader.ACTIVE_STRATEGY_ENV, None)
        else:
            os.environ[trader.ACTIVE_STRATEGY_ENV] = self.old_active
        self.temp_dir.cleanup()

    def test_kdj_prompt_registers_selects_buys_monitors_and_sells(self):
        model_calls = []

        def fake_model(messages):
            model_calls.append(messages)
            return {"strategy_spec": kdj_spec()}

        store = PromptStrategyStore()
        draft = store.create_draft("kdj<0时买入，kdj>15时卖出")
        refinement = refine_prompt_once(draft["raw_prompt"], fake_model)
        refined = store.save_refinement(
            draft["draft_id"],
            refinement.refined_spec,
            model="fake-model",
            provider="test",
            refinement_prompt_sha256=refinement.refinement_prompt_sha256,
        )
        version = store.activate_draft(draft["draft_id"])

        self.assertEqual(len(model_calls), 1)
        self.assertEqual(refined["status"], "pending_confirmation")
        self.assertEqual(version["status"], "active")
        self.assertEqual(
            version["execution_plan"]["required_features"]["selection"][0][
                "feature_version"
            ],
            "cn-kdj-v2",
        )

        buy_history = price_rows(
            [20.0] * 57 + [12.0, 8.0, 5.0],
            end_date=date(2026, 8, 6),
        )
        buy_quote = {
            "price": 5.0,
            "open": 5.0,
            "high": 5.2,
            "low": 4.8,
            "volume": 1200,
            "name": "测试股",
            "quote_time": "2026-08-07 10:00:00",
        }
        requested_rows = (
            version["execution_plan"]["stage_requirements"]["selection"][
                "minimum_bars"
            ]
            + 1
        )
        selection_rows = prepare_strategy_rows(
            "600000",
            "sh600000",
            quote=buy_quote,
            name="测试股",
            historical_rows=buy_history,
            kline_count=requested_rows,
            minimum_rows=requested_rows,
            enrich_legacy_indicators=False,
        )
        self.assertIsNotNone(selection_rows)
        selection = score_prompt_selection(
            selection_rows or [],
            version,
            data_context={
                "expected_closed_date": "2026-08-06",
                "expected_live_date": "2026-08-07",
                "evaluated_at": "2026-08-07 14:30:00",
            },
        )
        self.assertTrue(selection["actionable"])
        selection_record = store.record_evaluation(
            version["version_id"],
            selection["prompt_rule_audit"],
        )

        candidate = {
            **selection,
            "code": "600000",
            "name": "测试股",
            "best_strategy": "preset_text",
            "best_score": selection["score"],
        }
        state = {"cash": 100_000.0, "positions": {}, "trade_log": []}
        decision = trader.build_local_prompt_decision(
            [candidate],
            state,
            version,
            market_context(),
        )

        row_requests = []
        original_check = trader.is_a_share_execution_time
        original_quote = trader.execution_quote
        original_loader = trader.load_prompt_strategy_rows
        original_run = trader.subprocess.run
        original_sync_trades = trader._sync_trades_to_db
        original_sync_positions = trader._sync_positions_to_db
        original_sync_decision = trader._sync_decision_to_db
        try:
            trader.is_a_share_execution_time = lambda dt=None: (True, "连续竞价")
            trader.execution_quote = lambda code: dict(buy_quote)

            def load_entry_rows(code, *, quote, count, timeout=20):
                row_requests.append(("entry", count))
                return list(selection_rows or [])

            trader.load_prompt_strategy_rows = load_entry_rows
            bought = trader.execute_actions(
                state,
                decision,
                [candidate],
                True,
                "连续竞价",
                market_context(),
                evaluated_at=datetime(2026, 8, 7, 14, 30),
            )
            self.assertEqual(len(bought), 1)
            self.assertEqual(bought[0]["action"], "BUY")
            self.assertEqual(bought[0]["shares"], 2000)
            self.assertEqual(row_requests, [("entry", requested_rows)])

            exit_history = price_rows(
                [20.0] * 55 + [12.0, 8.0, 5.0, 8.0, 10.0],
                end_date=date(2026, 8, 7),
            )
            position = state["positions"]["600000"]
            position.update({
                "last_price": 10.0,
                "day_open": 10.0,
                "day_high": 10.2,
                "day_low": 9.8,
                "volume_lots": 1300,
                "quote_time": "2026-08-10 14:30:00",
            })

            def fake_kline(command, **_kwargs):
                row_requests.append(("exit", int(command[-1])))
                return types.SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"rows": exit_history}),
                )

            trader.subprocess.run = fake_kline
            trader._refresh_position_bbi(
                state,
                datetime(2026, 8, 10, 14, 30),
                evaluate_prompt_exits=True,
            )
            self.assertEqual(position["prompt_strategy_exit_status"], "true")
            self.assertEqual(row_requests[-1], ("exit", requested_rows))

            trader._sync_trades_to_db = lambda _items: True
            trader._sync_positions_to_db = lambda _state: None
            trader._sync_decision_to_db = lambda _entry: True
            sold = trader.check_auto_exits(
                state,
                datetime(2026, 8, 10, 14, 31),
            )
        finally:
            trader.is_a_share_execution_time = original_check
            trader.execution_quote = original_quote
            trader.load_prompt_strategy_rows = original_loader
            trader.subprocess.run = original_run
            trader._sync_trades_to_db = original_sync_trades
            trader._sync_positions_to_db = original_sync_positions
            trader._sync_decision_to_db = original_sync_decision

        self.assertEqual(len(sold), 1)
        self.assertEqual(sold[0]["action"], "SELL")
        self.assertEqual(sold[0]["shares"], 2000)
        self.assertEqual(state["positions"], {})
        self.assertGreater(state["cash"], 100_000.0)
        self.assertIsNone(store.active_position_binding("600000"))

        evaluations = store.list_evaluations(
            version["version_id"],
            code="600000",
        )
        self.assertEqual(len(evaluations), 3)
        self.assertEqual(
            {item["stage"] for item in evaluations},
            {"selection", "entry", "exit"},
        )
        self.assertIn(
            selection_record["evaluation_id"],
            {item["evaluation_id"] for item in evaluations},
        )
        for item in evaluations:
            self.assertTrue(replay_rule_evaluation_audit(
                item["audit"],
                plan=version["execution_plan"],
            )["ok"])
        self.assertEqual(len(model_calls), 1)


if __name__ == "__main__":
    unittest.main()
