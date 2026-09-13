#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from strategies.rules import (  # noqa: E402
    CompileError,
    DEFAULT_FEATURE_REGISTRY,
    EvaluationContext,
    FeatureDefinition,
    FeatureRegistry,
    FeatureRequest,
    build_action_intent,
    build_rule_evaluation_audit,
    compile_strategy_spec,
    evaluate_plan_stage,
    materialize_features,
    replay_rule_evaluation_audit,
)


def feature(feature_id, output_field="value", *, offset_bars=0, **parameters):
    payload = {
        "type": "feature",
        "feature_id": feature_id,
        "field": output_field,
        "parameters": parameters,
    }
    if offset_bars:
        payload["offset_bars"] = offset_bars
    return payload


def compare(rule_id, left, operator, right):
    return {
        "type": "compare",
        "rule_id": rule_id,
        "left": left,
        "operator": operator,
        "right": right,
    }


def kdj_spec():
    j = feature("technical.kdj", "j", n=9, m1=3, m2=3)
    return {
        "schema_version": 1,
        "strategy_id": "kdj-rebound",
        "name": "KDJ超卖反弹",
        "description": "日线J值低于0买入，高于15卖出",
        "data_contract": {
            "timeframe": "1d",
            "bar_status": "closed",
            "freshness_seconds": 129600,
        },
        "rules": {
            "selection": compare("selection-j-low", j, "lt", 0),
            "entry": compare("entry-j-low", j, "lt", 0),
            "exit": compare("exit-j-high", j, "gt", 15),
        },
        "position": {"type": "equity_pct", "value": 10, "allow_add": False},
        "exit_quantity": "all_available",
        "candidate_limit": 20,
        "max_new_buys_per_cycle": 2,
        "missing_data_policy": "hold",
        "conflict_policy": "exit_first",
        "execution_mode": "simulation",
        "assumptions": ["KDJ指J值"],
        "ambiguities": [],
    }


def outside_bar_spec():
    current_low = feature("market.value", field="low")
    previous_low = feature("market.value", field="low", offset_bars=1)
    current_high = feature("market.value", field="high")
    previous_high = feature("market.value", field="high", offset_bars=1)
    outside_bar = {
        "type": "all",
        "rule_id": "outside-bar",
        "children": [
            compare("lower-low", current_low, "lt", previous_low),
            compare("higher-high", current_high, "gt", previous_high),
        ],
    }
    spec = kdj_spec()
    spec.update({
        "strategy_id": "outside-bar",
        "name": "日线外包络",
        "description": "今日最低价低于昨日最低价且今日最高价高于昨日最高价",
        "rules": {
            "selection": outside_bar,
            "entry": outside_bar,
            "exit": compare(
                "never-exit-in-test",
                feature("market.value", field="close"),
                "gt",
                9999,
            ),
        },
        "assumptions": ["今日和昨日均指相邻已收盘日K"],
    })
    return spec


class PromptRuleEngineTests(unittest.TestCase):
    def test_compiler_builds_stable_versioned_dependency_plan(self):
        first = compile_strategy_spec(kdj_spec())
        second = compile_strategy_spec(kdj_spec())

        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["engine_version"], "prompt-rules-v3")
        self.assertEqual(
            first["required_features"]["selection"][0]["feature_id"],
            "technical.kdj",
        )
        self.assertEqual(
            first["required_features"]["selection"][0]["feature_version"],
            "cn-kdj-v2",
        )
        self.assertEqual(first["strategy"]["position"]["value"], 10.0)

    def test_compiler_rejects_unknown_feature(self):
        spec = kdj_spec()
        spec["rules"]["entry"] = compare(
            "unsupported",
            feature("technical.magic", "value"),
            "gt",
            1,
        )

        with self.assertRaisesRegex(CompileError, "unsupported feature"):
            compile_strategy_spec(spec)

    def test_compiler_rejects_silently_ignored_fields_and_preserves_zero_limit(self):
        unknown = kdj_spec()
        unknown["rules"]["entry"]["model_hint"] = "买入"
        with self.assertRaisesRegex(CompileError, "model_hint"):
            compile_strategy_spec(unknown)

        zero = kdj_spec()
        zero["max_new_buys_per_cycle"] = 0
        plan = compile_strategy_spec(zero)
        self.assertEqual(plan["strategy"]["max_new_buys_per_cycle"], 0)

        invalid_exit = kdj_spec()
        invalid_exit["exit_quantity"] = "half"
        with self.assertRaisesRegex(CompileError, "all_available"):
            compile_strategy_spec(invalid_exit)

    def test_registry_supports_multiple_indicator_families_and_exact_bar_budget(self):
        spec = kdj_spec()
        macd = feature("technical.macd", "hist", fast=12, slow=26, signal=9)
        spec["rules"]["selection"] = compare("macd-positive", macd, "gt", 0)
        plan = compile_strategy_spec(spec)

        request = plan["required_features"]["selection"][0]
        self.assertEqual(request["feature_id"], "technical.macd")
        self.assertEqual(request["min_bars"], 175)
        self.assertEqual(plan["stage_requirements"]["selection"]["minimum_bars"], 175)

        invalid = kdj_spec()
        invalid["rules"]["selection"] = compare(
            "invalid-macd",
            feature("technical.macd", "dif", fast=30, slow=20, signal=9),
            "gt",
            0,
        )
        with self.assertRaisesRegex(CompileError, "fast must be smaller"):
            compile_strategy_spec(invalid)

    def test_feature_offset_compiles_and_evaluates_exact_previous_bar_values(self):
        plan = compile_strategy_spec(outside_bar_spec())
        requests = plan["required_features"]["selection"]

        self.assertEqual(sorted(item["offset_bars"] for item in requests), [0, 0, 1, 1])
        self.assertEqual(plan["stage_requirements"]["selection"]["minimum_bars"], 2)
        self.assertTrue(all(
            item.get("offset_bars", {}).get("max") == 499
            for item in DEFAULT_FEATURE_REGISTRY.capability_catalog()
        ))

        rows = [
            {"date": "2026-08-06", "high": 11, "low": 9, "close": 10},
            {"date": "2026-08-07", "high": 12, "low": 8, "close": 10},
        ]
        payload = materialize_features(
            [FeatureRequest(
                feature_id=item["feature_id"],
                field=item["field"],
                parameters=item["parameters"],
                timeframe=item["timeframe"],
                feature_version=item["feature_version"],
                offset_bars=item["offset_bars"],
            ) for item in requests],
            rows,
        )
        evaluation = evaluate_plan_stage(
            plan,
            "selection",
            EvaluationContext(facts=payload["facts"]),
        )

        self.assertEqual(evaluation["status"], "true")
        previous_metadata = [
            item for item in payload["metadata"].values()
            if item["offset_bars"] == 1
        ]
        self.assertTrue(previous_metadata)
        self.assertTrue(all(item["bar_time"] == "2026-08-06" for item in previous_metadata))

        missing = materialize_features(
            [FeatureRequest(
                feature_id=item["feature_id"],
                field=item["field"],
                parameters=item["parameters"],
                timeframe=item["timeframe"],
                feature_version=item["feature_version"],
                offset_bars=item["offset_bars"],
            ) for item in requests],
            rows[-1:],
        )
        self.assertEqual(
            evaluate_plan_stage(
                plan,
                "selection",
                EvaluationContext(facts=missing["facts"]),
            )["status"],
            "unknown",
        )

    def test_feature_offset_is_generic_and_strictly_bounded(self):
        spec = kdj_spec()
        spec["rules"]["selection"] = compare(
            "previous-kdj",
            feature(
                "technical.kdj",
                "j",
                offset_bars=1,
                n=9,
                m1=3,
                m2=3,
            ),
            "lt",
            0,
        )
        request = compile_strategy_spec(spec)["required_features"]["selection"][0]
        self.assertEqual(request["offset_bars"], 1)
        self.assertEqual(request["min_bars"], 40)

        for invalid_offset in (-1, 500, 1.5, True):
            invalid = kdj_spec()
            node = feature("market.value", field="low")
            node["offset_bars"] = invalid_offset
            invalid["rules"]["selection"] = compare(
                "invalid-offset",
                node,
                "lt",
                10,
            )
            with self.subTest(offset=invalid_offset), self.assertRaises(CompileError):
                compile_strategy_spec(invalid)

    def test_materializer_computes_only_requested_features_and_reuses_one_call(self):
        registry = FeatureRegistry()
        calls = []

        def compute(rows, parameters):
            calls.append((len(rows), dict(parameters)))
            return {"a": 1.5, "b": 2.5}

        registry.register(FeatureDefinition(
            feature_id="test.feature",
            version="v1",
            outputs=("a", "b"),
            min_bars=1,
            compute=compute,
        ))
        payload = materialize_features(
            [
                FeatureRequest("test.feature", "a"),
                FeatureRequest("test.feature", "b"),
            ],
            [{"date": "2026-08-07", "close": 10}],
            registry=registry,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(payload["facts"].values()), [1.5, 2.5])
        self.assertEqual(payload["errors"], [])

    def test_kdj_materializer_uses_registered_parameters(self):
        rows = []
        for index in range(40):
            close = 10 + index * 0.1
            rows.append({
                "date": f"2026-07-{index + 1:02d}",
                "open": close - 0.05,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1000 + index,
            })
        plan = compile_strategy_spec(kdj_spec())
        request = plan["required_features"]["selection"][0]
        payload = materialize_features([
            FeatureRequest(
                feature_id=request["feature_id"],
                field=request["field"],
                parameters=request["parameters"],
                timeframe=request["timeframe"],
                feature_version=request["feature_version"],
            )
        ], rows)

        self.assertEqual(payload["errors"], [])
        self.assertEqual(len(payload["facts"]), 1)
        self.assertIsInstance(next(iter(payload["facts"].values())), float)

    def test_registry_keeps_frozen_recursive_indicator_versions_replayable(self):
        rows = [
            {
                "date": f"2026-07-{index + 1:02d}",
                "open": 10 + index * 0.1,
                "high": 10.2 + index * 0.1,
                "low": 9.8 + index * 0.1,
                "close": 10 + index * 0.1,
                "volume": 1000,
            }
            for index in range(30)
        ]
        legacy = materialize_features(
            [FeatureRequest(
                "technical.kdj",
                "j",
                {"n": 9, "m1": 3, "m2": 3},
                "1d",
                "cn-kdj-v1",
            )],
            rows,
            registry=DEFAULT_FEATURE_REGISTRY,
        )
        current = materialize_features(
            [FeatureRequest(
                "technical.kdj",
                "j",
                {"n": 9, "m1": 3, "m2": 3},
                "1d",
                "cn-kdj-v2",
            )],
            rows,
            registry=DEFAULT_FEATURE_REGISTRY,
        )

        self.assertIsInstance(next(iter(legacy["facts"].values())), float)
        self.assertIsNone(next(iter(current["facts"].values())))

    def test_evaluator_returns_true_false_and_unknown_with_evidence(self):
        plan = compile_strategy_spec(kdj_spec())
        fact_key = plan["required_features"]["entry"][0]["fact_key"]

        matched = evaluate_plan_stage(
            plan,
            "entry",
            EvaluationContext(facts={fact_key: -1.2}, as_of="2026-08-07"),
        )
        blocked = evaluate_plan_stage(
            plan,
            "entry",
            EvaluationContext(facts={fact_key: 1.2}, as_of="2026-08-07"),
        )
        unknown = evaluate_plan_stage(
            plan,
            "entry",
            EvaluationContext(facts={}, as_of="2026-08-07"),
        )

        self.assertEqual(matched["status"], "true")
        self.assertEqual(blocked["status"], "false")
        self.assertEqual(unknown["status"], "unknown")
        self.assertIn(fact_key, matched["root"]["evidence"])

    def test_compiler_rejects_runtime_model_judgment(self):
        spec = kdj_spec()
        spec["rules"]["entry"] = {
            "type": "all",
            "children": [
                spec["rules"]["entry"],
                {
                    "type": "model_judgment",
                    "rule_id": "avoid-chasing",
                    "instruction": "判断是否存在明显追高风险",
                    "required_features": [
                        feature("return.close", "pct", period=5),
                    ],
                    "minimum_confidence": 0.7,
                },
            ],
        }
        with self.assertRaisesRegex(CompileError, "创建阶段调用模型"):
            compile_strategy_spec(spec)

    def test_compiler_accepts_only_materialized_runtime_facts(self):
        spec = kdj_spec()
        spec["rules"]["exit"] = compare(
            "take-profit",
            {"type": "fact", "field": "position.pnl_pct"},
            "gt",
            8,
        )
        plan = compile_strategy_spec(spec)
        self.assertEqual(
            plan["strategy"]["rules"]["exit"]["left"]["field"],
            "position.pnl_pct",
        )

        invalid = kdj_spec()
        invalid["rules"]["exit"] = compare(
            "unsupported-fact",
            {"type": "fact", "field": "market_context.magic_score"},
            "gt",
            1,
        )
        with self.assertRaisesRegex(CompileError, "market_context.magic_score"):
            compile_strategy_spec(invalid)

        invalid_selection = kdj_spec()
        invalid_selection["rules"]["selection"] = compare(
            "account-aware-selection",
            {"type": "fact", "field": "account.cash"},
            "gt",
            10000,
        )
        with self.assertRaisesRegex(CompileError, "selection 阶段"):
            compile_strategy_spec(invalid_selection)

    def test_exit_evaluation_builds_non_executing_sell_intent(self):
        plan = compile_strategy_spec(kdj_spec())
        fact_key = plan["required_features"]["exit"][0]["fact_key"]
        evaluation = evaluate_plan_stage(
            plan,
            "exit",
            EvaluationContext(facts={fact_key: 16.0}),
        )

        intent = build_action_intent(
            plan,
            evaluation,
            code="600000",
            name="测试股",
        )

        self.assertEqual(intent["action"], "SELL")
        self.assertEqual(intent["quantity_policy"]["type"], "all_available")
        self.assertEqual(intent["plan_sha256"], plan["plan_sha256"])

    def test_audit_replays_stateful_context_and_detects_tampering(self):
        spec = kdj_spec()
        spec["rules"]["entry"] = {
            "type": "crosses_above",
            "rule_id": "j-cross-zero",
            "left": feature("technical.kdj", "j", n=9, m1=3, m2=3),
            "right": 0,
        }
        plan = compile_strategy_spec(spec)
        fact_key = plan["required_features"]["entry"][0]["fact_key"]
        context = EvaluationContext(
            facts={fact_key: 1.0},
            previous_facts={fact_key: -1.0},
            runtime_facts={"position.available_shares": 100},
            as_of="2026-08-07 15:00:00",
        )
        evaluation = evaluate_plan_stage(plan, "entry", context)
        audit = build_rule_evaluation_audit(
            strategy_version_id="version-1",
            plan=plan,
            stage="entry",
            code="600000",
            fact_snapshot=context.facts,
            previous_facts=context.previous_facts,
            runtime_facts=context.runtime_facts,
            evaluation=evaluation,
            action_intent=build_action_intent(plan, evaluation, code="600000"),
            evaluated_at=context.as_of,
        )

        replayed = replay_rule_evaluation_audit(audit, plan=plan)
        self.assertTrue(replayed["ok"])

        tampered = dict(audit)
        tampered["code"] = "000001"
        self.assertEqual(
            replay_rule_evaluation_audit(tampered, plan=plan)["error"],
            "audit_fingerprint_mismatch",
        )


if __name__ == "__main__":
    unittest.main()
