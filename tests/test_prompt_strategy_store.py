#!/usr/bin/env python3
import sqlite3
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from storage.prompt_strategies import PromptStrategyStore  # noqa: E402
from strategies.rules import (  # noqa: E402
    EvaluationContext,
    build_action_intent,
    build_rule_evaluation_audit,
    evaluate_plan_stage,
)

from test_prompt_rule_engine import kdj_spec  # noqa: E402


class PromptStrategyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="niuone-prompt-store-")
        self.db_path = Path(self.temp_dir.name) / "prompt-strategies.db"
        self.store = PromptStrategyStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_version(self, prompt="KDJ J值低于0买入，高于15卖出"):
        draft = self.store.create_draft(prompt)
        refined = self.store.save_refinement(
            draft["draft_id"],
            kdj_spec(),
            model="test-model",
            provider="test-provider",
            refinement_prompt_sha256="prompt-sha",
        )
        self.assertEqual(refined["status"], "pending_confirmation")
        return self.store.activate_draft(draft["draft_id"])

    def test_draft_refinement_activation_and_version_retirement(self):
        first = self.create_version()
        second = self.create_version("第二版KDJ规则")

        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.store.get_version(first["version_id"])["status"], "retired")
        self.assertEqual(self.store.active_version()["version_id"], second["version_id"])
        self.assertEqual(len(self.store.list_versions()), 2)
        self.assertEqual(len(self.store.list_drafts()), 2)

    def test_two_phase_activation_keeps_previous_version_until_commit(self):
        first = self.create_version()
        draft = self.store.create_draft("待激活策略")
        self.store.save_refinement(
            draft["draft_id"],
            kdj_spec(),
            model="test-model",
            provider="test-provider",
        )

        pending = self.store.prepare_activation(draft["draft_id"])
        self.assertEqual(pending["status"], "pending_activation")
        resumed = self.store.prepare_activation(draft["draft_id"])
        self.assertEqual(resumed["version_id"], pending["version_id"])
        self.assertEqual(self.store.active_version()["version_id"], first["version_id"])

        self.assertTrue(self.store.fail_activation(pending["version_id"]))
        self.assertEqual(self.store.active_version()["version_id"], first["version_id"])
        self.assertEqual(
            self.store.get_draft(draft["draft_id"])["status"],
            "pending_confirmation",
        )

    def test_activated_version_content_is_immutable(self):
        version = self.create_version()
        with sqlite3.connect(self.db_path) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                conn.execute(
                    "UPDATE prompt_strategy_versions SET raw_prompt = ? WHERE version_id = ?",
                    ("tampered", version["version_id"]),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                conn.execute(
                    "DELETE FROM prompt_strategy_versions WHERE version_id = ?",
                    (version["version_id"],),
                )

    def test_version_reader_fails_closed_on_corrupt_plan_fingerprint(self):
        version = self.create_version()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TRIGGER prompt_version_content_immutable")
            conn.execute(
                "UPDATE prompt_strategy_versions SET plan_sha256 = ? WHERE version_id = ?",
                ("0" * 64, version["version_id"]),
            )
            conn.commit()
        with self.assertRaisesRegex(ValueError, "完整性校验失败"):
            self.store.get_version(version["version_id"])

    def test_invalid_refinement_cannot_activate(self):
        draft = self.store.create_draft("未知指标大于1就买入")
        invalid = kdj_spec()
        invalid["rules"]["entry"]["left"]["feature_id"] = "unknown.indicator"
        refined = self.store.save_refinement(
            draft["draft_id"],
            invalid,
            model="test-model",
            provider="test-provider",
        )

        self.assertEqual(refined["status"], "validation_failed")
        self.assertTrue(refined["validation_errors"])
        with self.assertRaisesRegex(ValueError, "待确认"):
            self.store.activate_draft(draft["draft_id"])

    def test_draft_can_only_be_refined_once(self):
        draft = self.store.create_draft("KDJ策略")
        self.store.save_refinement(
            draft["draft_id"],
            kdj_spec(),
            model="test-model",
            provider="test-provider",
        )
        with self.assertRaisesRegex(ValueError, "已经细化"):
            self.store.save_refinement(
                draft["draft_id"],
                kdj_spec(),
                model="test-model",
                provider="test-provider",
            )

    def test_refinement_claim_prevents_duplicate_model_requests(self):
        draft = self.store.create_draft("KDJ J值低于0买入")
        claimed = self.store.claim_refinement(draft["draft_id"])
        self.assertEqual(claimed["status"], "refining")
        with self.assertRaisesRegex(ValueError, "已在细化"):
            self.store.claim_refinement(draft["draft_id"])
        self.assertTrue(self.store.release_refinement_claim(draft["draft_id"]))
        self.assertEqual(
            self.store.claim_refinement(draft["draft_id"])["status"],
            "refining",
        )

    def test_audit_is_validated_append_only_and_queryable(self):
        version = self.create_version()
        plan = version["execution_plan"]
        fact_key = plan["required_features"]["entry"][0]["fact_key"]
        context = EvaluationContext(
            facts={fact_key: -1.0},
            as_of="2026-08-07 15:00:00",
        )
        evaluation = evaluate_plan_stage(plan, "entry", context)
        audit = build_rule_evaluation_audit(
            strategy_version_id=version["version_id"],
            plan=plan,
            stage="entry",
            code="600000",
            fact_snapshot=context.facts,
            evaluation=evaluation,
            action_intent=build_action_intent(plan, evaluation, code="600000"),
            evaluated_at=context.as_of,
        )
        recorded = self.store.record_evaluation(version["version_id"], audit)
        evaluations = self.store.list_evaluations(version["version_id"])

        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations[0]["evaluation_id"], recorded["evaluation_id"])
        self.assertEqual(evaluations[0]["audit"]["audit_sha256"], audit["audit_sha256"])
        duplicate = self.store.record_evaluation(version["version_id"], audit)
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["evaluation_id"], recorded["evaluation_id"])
        self.assertEqual(len(self.store.list_evaluations(version["version_id"])), 1)
        with sqlite3.connect(self.db_path) as conn:
            encoding, raw_json, compressed_size = conn.execute(
                "SELECT audit_encoding, audit_json, LENGTH(audit_zlib) "
                "FROM prompt_strategy_evaluations WHERE evaluation_id = ?",
                (recorded["evaluation_id"],),
            ).fetchone()
            self.assertEqual(encoding, "zlib-json")
            self.assertEqual(raw_json, "")
            self.assertGreater(compressed_size, 0)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "UPDATE prompt_strategy_evaluations SET status = 'false' WHERE evaluation_id = ?",
                    (recorded["evaluation_id"],),
                )

    def test_audit_reader_fails_closed_on_corrupt_payload(self):
        version = self.create_version()
        plan = version["execution_plan"]
        fact_key = plan["required_features"]["entry"][0]["fact_key"]
        context = EvaluationContext(facts={fact_key: -1.0}, as_of="2026-08-07")
        evaluation = evaluate_plan_stage(plan, "entry", context)
        audit = build_rule_evaluation_audit(
            strategy_version_id=version["version_id"],
            plan=plan,
            stage="entry",
            code="600000",
            fact_snapshot=context.facts,
            evaluation=evaluation,
            action_intent=build_action_intent(plan, evaluation, code="600000"),
        )
        recorded = self.store.record_evaluation(version["version_id"], audit)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TRIGGER prompt_evaluation_no_update")
            conn.execute(
                "UPDATE prompt_strategy_evaluations SET audit_zlib = ? WHERE evaluation_id = ?",
                (zlib.compress(b"{}"), recorded["evaluation_id"]),
            )
            conn.commit()

        with self.assertRaisesRegex(ValueError, "审计指纹无效"):
            self.store.list_evaluations(version["version_id"])

    def test_position_binding_keeps_entry_version_until_release(self):
        first = self.create_version()
        binding = self.store.bind_position(
            code="600000",
            strategy_version_id=first["version_id"],
            entry_evaluation_id="evaluation-1",
            entry_trade_key="trade-1",
        )
        self.create_version("新版本不应替换已持仓绑定")

        active = self.store.active_position_binding("600000")
        self.assertEqual(active["binding_id"], binding["binding_id"])
        self.assertEqual(active["strategy_version_id"], first["version_id"])
        self.assertTrue(self.store.release_position("600000"))
        self.assertIsNone(self.store.active_position_binding("600000"))


if __name__ == "__main__":
    unittest.main()
