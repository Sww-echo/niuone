#!/usr/bin/env python3
import importlib.util
import os
import sys
import unittest
from pathlib import Path

from app.market_data.news_precheck import IWENCAI_NEWS_SOURCE_VERSION


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "app"
COMPAT = SRC / "compat"
NEWS_ENV_KEYS = {
    "DASHBOARD_ENV_FILE",
    "IWENCAI_NEWS_PRECHECK_ENABLED",
    "IWENCAI_ENABLED",
    "IWENCAI_BASE_URL",
    "IWENCAI_API_KEY",
    "IWENCAI_TIMEOUT_SECONDS",
    "IWENCAI_MAX_RETRIES",
    "IWENCAI_MAX_CONCURRENCY",
    "DASHBOARD_DECISION_MODEL",
    "DASHBOARD_DECISION_BASE_URL",
    "DASHBOARD_DECISION_API_KEY",
    "DASHBOARD_DECISION_STREAM_MODE",
    "DASHBOARD_DECISION_REASONING_EFFORT",
    "DASHBOARD_DECISION_TIMEOUT",
    "DASHBOARD_DECISION_MAX_TOKENS",
    "CROSSDESK_BASE_URL",
    "CROSSDESK_API_KEY",
    "DASHBOARD_CONFIG",
    # Old values are included only to prove they no longer activate model prechecks.
    "DASHBOARD_NEWS_SOURCE",
    "DASHBOARD_NEWS_MODEL",
    "DASHBOARD_NEWS_BASE_URL",
    "DASHBOARD_NEWS_API_KEY",
}


def import_trader_with_env(updates: dict[str, str]):
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
        sys.path.insert(0, str(COMPAT))
    for key in NEWS_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["DASHBOARD_ENV_FILE"] = str(ROOT / ".missing-dashboard.env")
    os.environ.update(updates)
    spec = importlib.util.spec_from_file_location(
        f"niuniu_practice_trader_under_test_{len(sys.modules)}",
        COMPAT / "niuniu_practice_trader.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class NewsPrecheckConfigTests(unittest.TestCase):
    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in NEWS_ENV_KEYS}

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_switch_off_skips_precheck_and_legacy_model_config(self):
        module = import_trader_with_env({
            "DASHBOARD_NEWS_SOURCE": "model",
            "DASHBOARD_NEWS_BASE_URL": "https://news.example/v1",
            "DASHBOARD_NEWS_API_KEY": "legacy-secret",
            "DASHBOARD_NEWS_MODEL": "legacy-model",
        })

        self.assertIsNone(module.load_news_precheck_config())
        self.assertEqual(
            module.check_candidate_news_precheck([{
                "code": "000001",
                "name": "平安银行",
                "news_precheck": {
                    "checked": True,
                    "available": True,
                    "provider": "消息面预检模型",
                    "source_mode": "model",
                    "summary": "旧模型摘要（利好）",
                },
            }]),
            "",
        )

    def test_enabled_precheck_fetches_through_iwencai_and_uses_decision_config(self):
        module = import_trader_with_env({
            "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
            "IWENCAI_ENABLED": "1",
            "IWENCAI_API_KEY": "iwencai-secret",
            "DASHBOARD_DECISION_MODEL": "decision-test-model",
            "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
            "DASHBOARD_DECISION_API_KEY": "decision-secret",
        })
        captured = {}

        def fake_fetcher(candidates, config, **kwargs):
            captured["candidates"] = candidates
            captured["config"] = config
            captured["kwargs"] = kwargs
            return [{
                "code": candidates[0]["code"],
                "name": candidates[0]["name"],
                "checked": True,
                "available": True,
                "tone": "neutral",
                "tone_label": "中性",
                "summary": "问财结构化结果（中性）",
                "provider": "同花顺问财",
                "source_mode": "iwencai",
                "source_version": IWENCAI_NEWS_SOURCE_VERSION,
                "judgment_provider": "decision_model",
                "judgment_model": "decision-test-model",
            }]

        module.fetch_candidate_news_records = fake_fetcher
        result = module.check_candidate_news_precheck([
            {"code": "000001", "name": "平安银行"},
        ])

        self.assertEqual(captured["config"].source_mode, "iwencai")
        self.assertEqual(captured["config"].model, "decision-test-model")
        self.assertEqual(captured["kwargs"]["max_candidates"], 1)
        self.assertIn("【消息面预检（同花顺问财）】", result)
        self.assertIn("问财结构化结果", result)

    def test_enabled_precheck_replaces_legacy_model_cache(self):
        module = import_trader_with_env({
            "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
            "IWENCAI_ENABLED": "1",
            "IWENCAI_API_KEY": "iwencai-secret",
            "DASHBOARD_DECISION_MODEL": "decision-test-model",
            "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
            "DASHBOARD_DECISION_API_KEY": "decision-secret",
        })
        calls = []

        def fake_fetcher(candidates, _config, **_kwargs):
            calls.extend(candidate["code"] for candidate in candidates)
            return [{
                "code": "000001",
                "name": "平安银行",
                "checked": True,
                "available": True,
                "summary": "问财新结果（中性）",
                "provider": "同花顺问财",
                "source_mode": "iwencai",
                "source_version": IWENCAI_NEWS_SOURCE_VERSION,
                "judgment_provider": "decision_model",
                "judgment_model": "decision-test-model",
            }]

        module.fetch_candidate_news_records = fake_fetcher
        result = module.check_candidate_news_precheck([{
            "code": "000001",
            "name": "平安银行",
            "news_precheck": {
                "checked": True,
                "available": True,
                "summary": "旧模型雪球舆情（利好）",
                "provider": "消息面预检模型",
                "source_mode": "model",
            },
        }])

        self.assertEqual(calls, ["000001"])
        self.assertIn("问财新结果", result)
        self.assertNotIn("雪球", result)

    def test_failed_precheck_has_zero_decision_weight(self):
        module = import_trader_with_env({
            "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
            "IWENCAI_ENABLED": "1",
            "IWENCAI_API_KEY": "iwencai-secret",
            "DASHBOARD_DECISION_MODEL": "decision-test-model",
            "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
            "DASHBOARD_DECISION_API_KEY": "decision-secret",
        })

        failed_record = {
            "code": "000001",
            "name": "平安银行",
            "checked": False,
            "available": False,
            "tone": "neutral",
            "tone_label": "不可用",
            "summary": "",
            "provider": "同花顺问财",
            "source_mode": "iwencai",
            "source_version": IWENCAI_NEWS_SOURCE_VERSION,
            "error": "request_TimeoutError",
        }
        module.fetch_candidate_news_records = (
            lambda _candidates, _config, **_kwargs: [failed_record]
        )
        candidate = {
            "code": "000001",
            "name": "平安银行",
            "news_precheck": failed_record,
            "news_available": False,
            "news_tone_label": "不可用",
        }

        self.assertEqual(module.check_candidate_news_precheck([candidate]), "")
        self.assertFalse(
            module.news_precheck_record_has_decision_weight(failed_record)
        )
        self.assertEqual(module.candidate_news_tone_for_decision(candidate), "中性")

    def test_valid_precheck_keeps_decision_weight(self):
        module = import_trader_with_env({})
        record = {
            "checked": True,
            "available": True,
            "tone": "negative",
            "tone_label": "利空",
            "summary": "监管函提示风险（利空）",
        }
        candidate = {
            "news_precheck": record,
            "news_available": True,
            "news_tone_label": "利空",
        }

        self.assertTrue(module.news_precheck_record_has_decision_weight(record))
        self.assertEqual(module.candidate_news_tone_for_decision(candidate), "利空")

    def test_enabled_precheck_requires_enabled_iwencai_and_key(self):
        module = import_trader_with_env({
            "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
            "IWENCAI_ENABLED": "0",
        })

        with self.assertRaisesRegex(RuntimeError, "source_disabled"):
            module.load_news_precheck_config()


if __name__ == "__main__":
    unittest.main()
