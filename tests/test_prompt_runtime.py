#!/usr/bin/env python3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from strategies.prompt_runtime import (  # noqa: E402
    evaluate_frozen_strategy_stage,
    resolve_prompt_order_shares,
    score_prompt_selection,
    stage_feature_requests,
)
from strategies.rules import compile_strategy_spec, replay_rule_evaluation_audit  # noqa: E402

from test_prompt_rule_engine import kdj_spec, outside_bar_spec  # noqa: E402


def frozen_version(spec=None):
    plan = compile_strategy_spec(spec or kdj_spec())
    return {
        "version_id": "preset_text-v1-test",
        "plan_sha256": plan["plan_sha256"],
        "execution_plan": plan,
    }


def falling_rows(count=40):
    rows = []
    start = date(2026, 8, 6) - timedelta(days=count - 1)
    for index in range(count):
        close = 20.0 if index < 30 else 20.0 - (index - 29)
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1000 + index,
            "symbol_code": "600000",
            "stock_name": "测试股",
        })
    return rows


class PromptRuntimeTests(unittest.TestCase):
    def test_runtime_evaluates_exact_current_and_previous_bar_values(self):
        version = frozen_version(outside_bar_spec())
        rows = [
            {
                "date": "2026-08-06",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 1000,
            },
            {
                "date": "2026-08-07",
                "open": 10,
                "high": 12,
                "low": 8,
                "close": 10,
                "volume": 1200,
            },
        ]

        result = evaluate_frozen_strategy_stage(
            version,
            "selection",
            rows,
            code="600000",
            name="测试股",
        )

        self.assertEqual(result["evaluation"]["status"], "true")
        by_field_and_offset = {
            (
                item["parameters"]["field"],
                item["offset_bars"],
            ): result["audit"]["replay_context"]["facts"][fact_key]
            for fact_key, item in result["feature_metadata"].items()
        }
        self.assertEqual(by_field_and_offset[("low", 0)], 8.0)
        self.assertEqual(by_field_and_offset[("low", 1)], 9.0)
        self.assertEqual(by_field_and_offset[("high", 0)], 12.0)
        self.assertEqual(by_field_and_offset[("high", 1)], 11.0)

    def test_runtime_materializes_only_stage_dependencies_and_builds_audit(self):
        version = frozen_version()
        result = evaluate_frozen_strategy_stage(
            version,
            "selection",
            falling_rows(),
            code="600000",
            name="测试股",
        )

        self.assertEqual(len(stage_feature_requests(version["execution_plan"], "selection")), 1)
        self.assertEqual(len(result["feature_metadata"]), 1)
        self.assertEqual(result["evaluation"]["status"], "true")
        self.assertEqual(result["action_intent"]["action"], "OBSERVE")
        self.assertTrue(
            replay_rule_evaluation_audit(
                result["audit"],
                plan=version["execution_plan"],
            )["ok"]
        )

    def test_selection_projection_only_marks_true_as_actionable(self):
        version = frozen_version()
        matched = score_prompt_selection(falling_rows(), version)
        missing = score_prompt_selection(falling_rows(5), version)

        self.assertTrue(matched["actionable"])
        self.assertEqual(matched["prompt_rule_status"], "true")
        self.assertFalse(missing["actionable"])
        self.assertEqual(missing["prompt_rule_status"], "unknown")
        self.assertIn("数据不足", missing["hard_blockers"][0])

    def test_frozen_position_policy_determines_order_size(self):
        sized = resolve_prompt_order_shares(
            {"type": "equity_pct", "value": 10, "allow_add": False},
            price=20,
            total_equity=1_000_000,
        )
        blocked_add = resolve_prompt_order_shares(
            {"type": "fixed_shares", "value": 500, "allow_add": False},
            price=20,
            total_equity=1_000_000,
            existing_quantity=500,
        )

        self.assertEqual(sized, {"shares": 5000, "error": ""})
        self.assertEqual(blocked_add["shares"], 0)
        self.assertIn("禁止加仓", blocked_add["error"])

    def test_closed_bar_contract_fails_closed_on_wrong_trading_date(self):
        version = frozen_version()
        result = evaluate_frozen_strategy_stage(
            version,
            "selection",
            falling_rows(),
            code="600000",
            data_context={
                "expected_closed_date": "2026-08-05",
                "evaluated_at": "2026-08-07 14:30:00",
            },
        )

        self.assertEqual(result["data_quality"]["status"], "stale")
        self.assertEqual(result["evaluation"]["status"], "unknown")
        self.assertIn("不等于预期交易日", result["feature_errors"][-1]["error"])

    def test_supplied_data_contract_requires_expected_trading_date(self):
        result = evaluate_frozen_strategy_stage(
            frozen_version(),
            "selection",
            falling_rows(),
            code="600000",
            data_context={"evaluated_at": "2026-08-07 14:30:00"},
        )

        self.assertEqual(result["data_quality"]["status"], "stale")
        self.assertEqual(result["evaluation"]["status"], "unknown")
        self.assertTrue(any(
            "预期交易日" in item["error"]
            for item in result["feature_errors"]
        ))

    def test_live_bar_contract_rejects_expired_quote(self):
        spec = kdj_spec()
        spec["data_contract"] = {
            "timeframe": "1d",
            "bar_status": "live",
            "freshness_seconds": 60,
        }
        plan = compile_strategy_spec(spec)
        version = {
            "version_id": "preset_text-live-test",
            "plan_sha256": plan["plan_sha256"],
            "execution_plan": plan,
        }
        rows = falling_rows()
        rows.append({
            **rows[-1],
            "date": "2026-08-07",
            "bar_status": "live",
            "observed_at": "2026-08-07 09:30:00",
        })
        result = evaluate_frozen_strategy_stage(
            version,
            "selection",
            rows,
            code="600000",
            data_context={
                "expected_live_date": "2026-08-07",
                "evaluated_at": "2026-08-07 10:00:00",
            },
        )

        self.assertEqual(result["data_quality"]["status"], "stale")
        self.assertEqual(result["evaluation"]["status"], "unknown")
        self.assertTrue(any("已过期" in item["error"] for item in result["feature_errors"]))


if __name__ == "__main__":
    unittest.main()
