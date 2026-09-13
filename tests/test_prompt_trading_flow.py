#!/usr/bin/env python3
import os
import json
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
from storage.prompt_strategies import PromptStrategyStore  # noqa: E402
from strategies.rules import build_action_intent, evaluate_plan_stage, EvaluationContext  # noqa: E402

from test_prompt_rule_engine import kdj_spec  # noqa: E402


def market_context():
    return {
        "tone_label": "中性",
        "max_open_positions": trader.MAX_OPEN_POSITIONS,
        "max_new_buys_per_decision": 2,
        "max_total_position_pct": trader.MAX_TOTAL_POSITION_PCT,
        "min_cash_reserve_pct": trader.MIN_CASH_RESERVE_PCT,
        "allow_new_buys": True,
    }


class PromptTradingFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="niuone-prompt-trading-")
        self.old_db = os.environ.get("DASHBOARD_PROMPT_STRATEGY_DB")
        self.old_active = os.environ.get(trader.ACTIVE_STRATEGY_ENV)
        os.environ["DASHBOARD_PROMPT_STRATEGY_DB"] = str(
            Path(self.temp_dir.name) / "prompt.db"
        )
        os.environ[trader.ACTIVE_STRATEGY_ENV] = "preset_text"
        self.store = PromptStrategyStore()
        draft = self.store.create_draft("KDJ J值低于0买入，高于15卖出")
        self.store.save_refinement(
            draft["draft_id"],
            kdj_spec(),
            model="test",
            provider="test",
        )
        self.version = self.store.activate_draft(draft["draft_id"])

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

    def test_local_decision_uses_frozen_plan_without_runtime_model(self):
        decision = trader.build_local_prompt_decision(
            [{"code": "600000", "name": "测试股"}],
            {"positions": {}},
            self.version,
            market_context(),
        )

        self.assertEqual(decision["model"], "LOCAL_PROMPT_RULE_ENGINE")
        self.assertEqual(decision["provider"], "local_rule")
        self.assertEqual(decision["actions"][0]["action"], "BUY")
        self.assertEqual(
            decision["actions"][0]["prompt_strategy_version_id"],
            self.version["version_id"],
        )

    def test_recommend_only_version_never_emits_executable_actions(self):
        version = dict(self.version)
        plan = dict(version["execution_plan"])
        strategy = dict(plan["strategy"])
        strategy["execution_mode"] = "recommend_only"
        plan["strategy"] = strategy
        version["execution_plan"] = plan

        decision = trader.build_local_prompt_decision(
            [{"code": "600000", "name": "测试股"}],
            {"positions": {}},
            version,
            market_context(),
        )

        self.assertEqual(decision["actions"], [])
        self.assertEqual(decision["recommendations"][0]["code"], "600000")
        self.assertEqual(decision["execution_mode"], "recommend_only")

    def test_versioned_buy_rechecks_entry_and_uses_frozen_position_size(self):
        plan = self.version["execution_plan"]
        fact_key = plan["required_features"]["entry"][0]["fact_key"]
        evaluation = evaluate_plan_stage(
            plan,
            "entry",
            EvaluationContext(facts={fact_key: -1.0}, as_of="2026-08-06"),
        )
        entry_result = {
            "evaluation_id": "evaluation-entry",
            "evaluation": evaluation,
            "action_intent": build_action_intent(
                plan,
                evaluation,
                code="600000",
                name="测试股",
            ),
            "audit": {"audit_sha256": "a" * 64},
        }
        candidate = {
            "code": "600000",
            "name": "测试股",
            "best_strategy": "preset_text",
            "best_score": 10.0,
            "entry_threshold": 10.0,
            "actionable": True,
            "hard_blockers": [],
            "prompt_strategy_version_id": self.version["version_id"],
        }
        state = {"cash": 100_000.0, "positions": {}, "trade_log": []}
        decision = {
            "actions": [{
                "action": "BUY",
                "code": "600000",
                "name": "测试股",
                "shares": 100,
                "reason": "本地规则买入",
            }]
        }
        original_check = trader.is_a_share_execution_time
        original_quote = trader.execution_quote
        original_entry = trader.evaluate_prompt_entry_before_buy
        try:
            trader.is_a_share_execution_time = lambda dt=None: (True, "连续竞价")
            trader.execution_quote = lambda code: {
                "price": 10.0,
                "name": "测试股",
                "source": "test",
            }
            trader.evaluate_prompt_entry_before_buy = lambda *args, **kwargs: (
                entry_result,
                self.version,
                "",
            )
            executed = trader.execute_actions(
                state,
                decision,
                [candidate],
                True,
                "连续竞价",
                market_context(),
            )
        finally:
            trader.is_a_share_execution_time = original_check
            trader.execution_quote = original_quote
            trader.evaluate_prompt_entry_before_buy = original_entry

        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["shares"], 1000)
        position = state["positions"]["600000"]
        self.assertEqual(position["prompt_strategy_version_id"], self.version["version_id"])
        self.assertTrue(position["prompt_strategy_binding_id"])
        self.assertEqual(
            self.store.active_position_binding("600000")["strategy_version_id"],
            self.version["version_id"],
        )

    def test_versioned_buy_cannot_exceed_system_position_ceiling(self):
        plan = self.version["execution_plan"]
        fact_key = plan["required_features"]["entry"][0]["fact_key"]
        evaluation = evaluate_plan_stage(
            plan,
            "entry",
            EvaluationContext(facts={fact_key: -1.0}, as_of="2026-08-06"),
        )
        action_intent = build_action_intent(
            plan,
            evaluation,
            code="600000",
            name="测试股",
        )
        action_intent["quantity_policy"] = {
            "type": "equity_pct",
            "value": 50,
            "allow_add": False,
        }
        entry_result = {
            "evaluation_id": "evaluation-entry-risk",
            "evaluation": evaluation,
            "action_intent": action_intent,
            "audit": {"audit_sha256": "b" * 64},
        }
        candidate = {
            "code": "600000",
            "name": "测试股",
            "best_strategy": "preset_text",
            "best_score": 10.0,
            "entry_threshold": 10.0,
            "actionable": True,
            "hard_blockers": [],
            "prompt_strategy_version_id": self.version["version_id"],
        }
        state = {"cash": 100_000.0, "positions": {}, "trade_log": []}
        decision = {
            "actions": [{
                "action": "BUY",
                "code": "600000",
                "name": "测试股",
                "shares": 100,
                "reason": "本地规则买入",
            }]
        }
        original_check = trader.is_a_share_execution_time
        original_quote = trader.execution_quote
        original_entry = trader.evaluate_prompt_entry_before_buy
        try:
            trader.is_a_share_execution_time = lambda dt=None: (True, "连续竞价")
            trader.execution_quote = lambda code: {
                "price": 10.0,
                "name": "测试股",
                "source": "test",
            }
            trader.evaluate_prompt_entry_before_buy = lambda *args, **kwargs: (
                entry_result,
                self.version,
                "",
            )
            executed = trader.execute_actions(
                state,
                decision,
                [candidate],
                True,
                "连续竞价",
                market_context(),
            )
        finally:
            trader.is_a_share_execution_time = original_check
            trader.execution_quote = original_quote
            trader.evaluate_prompt_entry_before_buy = original_entry

        self.assertEqual(executed, [])
        self.assertEqual(state["positions"], {})
        self.assertTrue(any(
            item.get("category") == "risk_ceiling"
            and "单票仓位" in str(item.get("reason") or "")
            for item in decision.get("execution_blocks") or []
        ))

    def test_versioned_position_exit_uses_entry_version_and_local_rule(self):
        rows = []
        start = date(2026, 8, 6) - timedelta(days=39)
        for index in range(40):
            close = 10.0 + index * 0.2
            rows.append({
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1000,
            })
        position = {
            "code": "600000",
            "name": "测试股",
            "qty": 1000,
            "avg_cost": 10.0,
            "last_price": 17.8,
            "buy_date_lots": {"2026-07-01": 1000},
            "buy_strategy": "preset_text",
            "prompt_strategy_version_id": self.version["version_id"],
            "prompt_strategy_plan_sha256": self.version["plan_sha256"],
        }
        result = trader.evaluate_prompt_position_exit(
            "600000",
            position,
            rows,
            state={"cash": 50_000},
            dt=datetime(2026, 8, 7, 14, 30),
            store=self.store,
        )
        position["prompt_strategy_exit_status"] = result["evaluation"]["status"]
        position["prompt_strategy_exit_evaluation"] = result["evaluation"]
        position["prompt_strategy_exit_evaluation_id"] = result["evaluation_id"]
        position["prompt_strategy_exit_audit_sha256"] = result["audit"]["audit_sha256"]

        signal = trader.evaluate_sell_signal(
            "600000",
            position,
            "2026-08-07",
        )

        self.assertEqual(result["evaluation"]["status"], "true")
        self.assertEqual(
            trader.validate_versioned_prompt_exit_evidence(
                "600000",
                position,
                store=self.store,
            ),
            "",
        )
        self.assertEqual(signal["signal"], "prompt_strategy_exit")
        self.assertIn("冻结文字策略退出", signal["reason"])

        position["prompt_strategy_exit_audit_sha256"] = "0" * 64
        self.assertIn(
            "不一致",
            trader.validate_versioned_prompt_exit_evidence(
                "600000",
                position,
                store=self.store,
            ),
        )

    def test_position_monitor_fetches_only_exit_dependency_budget(self):
        state = {
            "cash": 50_000,
            "positions": {
                "600000": {
                    "code": "600000",
                    "name": "测试股",
                    "qty": 1000,
                    "avg_cost": 10.0,
                    "last_price": 11.0,
                    "buy_date_lots": {"2026-07-01": 1000},
                    "buy_strategy": "preset_text",
                    "prompt_strategy_version_id": self.version["version_id"],
                    "prompt_strategy_plan_sha256": self.version["plan_sha256"],
                }
            },
        }
        start = date(2026, 8, 6) - timedelta(days=39)
        rows = [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "open": 10 + index * 0.1,
                "high": 10.2 + index * 0.1,
                "low": 9.8 + index * 0.1,
                "close": 10 + index * 0.1,
                "volume": 1000,
            }
            for index in range(40)
        ]
        commands = []
        original_run = trader.subprocess.run
        try:
            trader.subprocess.run = lambda command, **_kwargs: (
                commands.append(command)
                or types.SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"rows": rows}),
                )
            )
            trader._refresh_position_bbi(
                state,
                datetime(2026, 8, 7, 14, 30),
                evaluate_prompt_exits=True,
            )
        finally:
            trader.subprocess.run = original_run

        self.assertEqual(commands[0][-1], "40")
        self.assertNotIn("bbi", state["positions"]["600000"])

    def test_t1_pending_exit_is_sticky_until_shares_are_sellable(self):
        plan = self.version["execution_plan"]
        fact_key = plan["required_features"]["exit"][0]["fact_key"]
        evaluation = evaluate_plan_stage(
            plan,
            "exit",
            EvaluationContext(facts={fact_key: 20.0}, as_of="2026-08-07"),
        )
        position = {
            "code": "600000",
            "name": "测试股",
            "qty": 1000,
            "avg_cost": 10.0,
            "last_price": 11.0,
            "buy_date_lots": {"2026-08-07": 1000},
            "buy_strategy": "preset_text",
            "prompt_strategy_version_id": self.version["version_id"],
            "prompt_strategy_plan_sha256": self.version["plan_sha256"],
            "prompt_strategy_exit_status": "true",
            "prompt_strategy_exit_evaluation": evaluation,
            "prompt_strategy_exit_evaluation_id": "evaluation-exit",
            "prompt_strategy_pending_exit": True,
            "prompt_strategy_pending_exit_reason": "T+1",
        }
        state = {"cash": 50_000, "positions": {"600000": position}}
        calls = []
        original_run = trader.subprocess.run
        try:
            trader.subprocess.run = lambda *_args, **_kwargs: calls.append(True)
            trader._refresh_position_bbi(
                state,
                datetime(2026, 8, 8, 9, 35),
                evaluate_prompt_exits=True,
            )
        finally:
            trader.subprocess.run = original_run

        self.assertEqual(calls, [])
        self.assertTrue(position["prompt_strategy_pending_exit"])
        self.assertTrue(position["prompt_strategy_pending_exit_ready"])
        self.assertEqual(position["prompt_strategy_exit_status"], "true")


if __name__ == "__main__":
    unittest.main()
