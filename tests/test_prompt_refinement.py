#!/usr/bin/env python3
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from strategies.prompt_refinement import (  # noqa: E402
    PromptRefinementContractError,
    PromptRefinementCoverageError,
    PromptRefinementParseError,
    build_refinement_messages,
    finalize_prompt_refinement,
    parse_refinement_json,
    refine_prompt_once,
)
from strategies.rules import compile_strategy_spec  # noqa: E402

from test_prompt_rule_engine import kdj_spec, outside_bar_spec  # noqa: E402


class PromptRefinementTests(unittest.TestCase):
    def test_messages_ground_model_in_registered_capabilities(self):
        messages = build_refinement_messages("kdj<0买入，kdj>15卖出")

        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertIn('"feature_id":"technical.kdj"', messages[1]["content"])
        self.assertIn('"offset_bars":{"default":0', messages[1]["content"])
        self.assertIn('"max":499', messages[1]["content"])
        self.assertIn("今日最低价低于昨日最低价", messages[0]["content"])
        self.assertIn('{"type":"equity_pct","value":10', messages[0]["content"])
        self.assertIn("禁止从规则中删除条件", messages[0]["content"])
        self.assertIn("禁止使用 conditions", messages[0]["content"])
        self.assertIn("激活后规则会冻结", messages[0]["content"])
        self.assertIn("禁止生成代码", messages[0]["content"])

    def test_refinement_accepts_wrapped_json_and_is_called_once(self):
        calls = []

        def requester(messages):
            calls.append(messages)
            return {"strategy_spec": kdj_spec()}

        result = refine_prompt_once("kdj<0买入，kdj>15卖出", requester)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.refined_spec["strategy_id"], "kdj-rebound")
        self.assertEqual(len(result.refinement_prompt_sha256), 64)

    def test_parser_accepts_fenced_json_but_rejects_non_object(self):
        parsed = parse_refinement_json(
            "```json\n" + '{"strategy_spec":{"schema_version":1}}' + "\n```"
        )
        self.assertEqual(parsed, {"schema_version": 1})
        with self.assertRaisesRegex(ValueError, "JSON 对象"):
            parse_refinement_json("[1, 2, 3]")

    def test_parser_reports_truncated_streamed_json(self):
        with self.assertRaisesRegex(
            PromptRefinementParseError,
            "流式传输中被截断",
        ):
            parse_refinement_json(
                '{"strategy_spec":{"schema_version":1,"assumptions":["unfinished'
            )

    def test_refinement_completes_omitted_supported_today_yesterday_conditions(self):
        messages = build_refinement_messages(
            "今日最低价低于昨日最低价且今日最高价高于昨日最高价时买入"
        )
        omitted = kdj_spec()
        omitted["description"] = (
            "Price range conditions are omitted due to system limitations."
        )

        result = finalize_prompt_refinement(
            messages,
            json.dumps({"strategy_spec": omitted}, ensure_ascii=False),
        )

        rendered = json.dumps(result.refined_spec, ensure_ascii=False)
        self.assertIn("selection-required-low-current-vs-previous", rendered)
        self.assertIn("entry-required-high-current-vs-previous", rendered)
        self.assertIn("本地完整性补全", rendered)
        self.assertNotIn("system limitations", rendered)

    def test_local_completion_replaces_malformed_model_temporal_nodes(self):
        messages = build_refinement_messages(
            "今日成交量大于昨日成交量，今日最低价低于昨日最低价且"
            "今日最高价高于昨日最高价时买入"
        )
        malformed = kdj_spec()
        bad_feature = {
            "type": "feature",
            "feature_id": "volume.ratio",
            "field": "value",
            "parameters": {"short_period": 1, "long_period": 1},
            "left": {"unexpected": True},
            "right": {"unexpected": True},
        }
        bad_compare = {
            "type": "compare",
            "rule_id": "bad-volume",
            "left": bad_feature,
            "operator": "gt",
            "right": 1,
        }
        malformed["rules"]["selection"] = bad_compare
        malformed["rules"]["entry"] = bad_compare

        result = finalize_prompt_refinement(
            messages,
            json.dumps({"strategy_spec": malformed}, ensure_ascii=False),
        )

        rendered_rules = json.dumps(result.refined_spec["rules"], ensure_ascii=False)
        self.assertNotIn("volume.ratio", rendered_rules)
        self.assertIn("selection-required-volume-current-vs-previous", rendered_rules)
        self.assertIn("entry-required-high-current-vs-previous", rendered_rules)
        self.assertTrue(compile_strategy_spec(result.refined_spec)["plan_sha256"])

    def test_refinement_accepts_exact_offset_coverage_for_requested_range(self):
        messages = build_refinement_messages(
            "今日最低价低于昨日最低价且今日最高价高于昨日最高价时买入"
        )

        result = finalize_prompt_refinement(
            messages,
            json.dumps({"strategy_spec": outside_bar_spec()}, ensure_ascii=False),
        )

        self.assertEqual(result.refined_spec["strategy_id"], "outside-bar")

    def test_refinement_rejects_fraction_style_position_object(self):
        messages = build_refinement_messages("kdj<0买入，kdj>15卖出")
        invalid = kdj_spec()
        invalid["position"] = {"equity_pct": 0.1, "allow_add": False}

        with self.assertRaisesRegex(
            PromptRefinementContractError,
            "type/value/allow_add",
        ):
            finalize_prompt_refinement(
                messages,
                json.dumps({"strategy_spec": invalid}, ensure_ascii=False),
            )

    def test_refinement_normalizes_all_conditions_alias_to_children(self):
        messages = build_refinement_messages("kdj<0买入，kdj>15卖出")
        aliased = kdj_spec()
        aliased["rules"]["selection"] = {
            "type": "all",
            "rule_id": "selection-all",
            "conditions": [aliased["rules"]["selection"]],
        }

        result = finalize_prompt_refinement(
            messages,
            json.dumps({"strategy_spec": aliased}, ensure_ascii=False),
        )

        selection = result.refined_spec["rules"]["selection"]
        self.assertIn("children", selection)
        self.assertNotIn("conditions", selection)
        self.assertTrue(any(
            "本地结构规范化" in item
            for item in result.refined_spec["assumptions"]
        ))

    def test_coverage_completes_implied_current_field_before_yesterday(self):
        messages = build_refinement_messages(
            "最低价低于昨日最低价且最高价高于昨日最高价时买入"
        )

        result = finalize_prompt_refinement(
            messages,
            json.dumps({"strategy_spec": kdj_spec()}, ensure_ascii=False),
        )

        rendered = json.dumps(result.refined_spec, ensure_ascii=False)
        self.assertIn("selection-required-low-current-vs-previous", rendered)
        self.assertIn("entry-required-high-current-vs-previous", rendered)

    def test_coverage_rejects_temporal_condition_with_ambiguous_direction(self):
        messages = build_refinement_messages("今日最低价参考昨日最低价时买入")

        with self.assertRaisesRegex(
            PromptRefinementCoverageError,
            "selection.最低价缺少",
        ):
            finalize_prompt_refinement(
                messages,
                json.dumps({"strategy_spec": kdj_spec()}, ensure_ascii=False),
            )

    def test_streamed_parts_can_be_finalized_with_the_original_prompt_hash(self):
        messages = build_refinement_messages("kdj<0买入，kdj>15卖出")
        response = json.dumps(
            {"strategy_spec": kdj_spec()},
            ensure_ascii=False,
        )

        result = finalize_prompt_refinement(messages, response)

        self.assertEqual(result.refined_spec["strategy_id"], "kdj-rebound")
        self.assertEqual(len(result.refinement_prompt_sha256), 64)


if __name__ == "__main__":
    unittest.main()
