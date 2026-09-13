#!/usr/bin/env python3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from market_data.news_precheck import (  # noqa: E402
    IWENCAI_NEWS_SOURCE_VERSION,
    NewsPrecheckConfig,
    build_iwencai_candidate_news_query,
    cached_news_record_matches_source,
    fetch_candidate_news_records,
    format_cached_news_records,
    judge_iwencai_news_with_decision_model,
    parse_iwencai_candidate_news_record,
)
import market_data.news_precheck as news_precheck  # noqa: E402


class NewsPrecheckServiceTests(unittest.TestCase):
    def test_config_is_disabled_by_default_and_ignores_legacy_model_values(self):
        self.assertIsNone(NewsPrecheckConfig.from_mapping({}))
        self.assertIsNone(NewsPrecheckConfig.from_mapping({
            "DASHBOARD_NEWS_SOURCE": "model",
            "DASHBOARD_NEWS_BASE_URL": "https://news.example/v1",
            "DASHBOARD_NEWS_API_KEY": "legacy-secret",
            "DASHBOARD_NEWS_MODEL": "legacy-model",
        }))

    def test_enabled_config_reuses_iwencai_and_decision_model_settings(self):
        config = NewsPrecheckConfig.from_mapping({
            "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
            "IWENCAI_ENABLED": "1",
            "IWENCAI_BASE_URL": "https://openapi.iwencai.com",
            "IWENCAI_API_KEY": "iwencai-secret",
            "IWENCAI_TIMEOUT_SECONDS": "18",
            "IWENCAI_MAX_RETRIES": "2",
            "IWENCAI_MAX_CONCURRENCY": "3",
            "DASHBOARD_DECISION_MODEL": "decision-test-model",
            "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
            "DASHBOARD_DECISION_API_KEY": "decision-secret",
            "DASHBOARD_DECISION_STREAM_MODE": "non_stream",
            "DASHBOARD_DECISION_REASONING_EFFORT": "high",
            "DASHBOARD_DECISION_TIMEOUT": "90",
        })

        self.assertEqual(config.source_mode, "iwencai")
        self.assertEqual(config.model, "decision-test-model")
        self.assertEqual(config.provider_label, "同花顺问财 + decision-test-model")
        self.assertEqual(config.decision_base_url, "https://model.example/v1")
        self.assertEqual(config.decision_api_key, "decision-secret")
        self.assertEqual(config.decision_stream_mode, "non_stream")
        self.assertEqual(config.decision_reasoning_effort, "high")
        self.assertEqual(config.decision_timeout_seconds, 90)
        self.assertEqual(config.timeout_seconds, 18)
        self.assertEqual(config.max_requests, 3)
        self.assertEqual(config.concurrency, 3)

    def test_enabled_config_requires_iwencai_source_and_key(self):
        with self.assertRaisesRegex(ValueError, "source_disabled"):
            NewsPrecheckConfig.from_mapping({
                "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
                "IWENCAI_API_KEY": "iwencai-secret",
            })
        with self.assertRaisesRegex(ValueError, "IWENCAI_API_KEY"):
            NewsPrecheckConfig.from_mapping({
                "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
                "IWENCAI_ENABLED": "1",
            })
        with self.assertRaisesRegex(ValueError, "decision_model_not_configured"):
            NewsPrecheckConfig.from_mapping({
                "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
                "IWENCAI_ENABLED": "1",
                "IWENCAI_API_KEY": "iwencai-secret",
                "DASHBOARD_CONFIG": "/tmp/niuone-missing-model-config.yaml",
            })

    def test_fetch_uses_iwencai_and_decision_model_judgment(self):
        config = NewsPrecheckConfig.from_mapping({
            "IWENCAI_NEWS_PRECHECK_ENABLED": "1",
            "IWENCAI_ENABLED": "1",
            "IWENCAI_API_KEY": "iwencai-secret",
            "DASHBOARD_DECISION_MODEL": "decision-test-model",
            "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
            "DASHBOARD_DECISION_API_KEY": "decision-secret",
        })
        calls = []
        model_calls = []

        def model_requester(request, api_key, **kwargs):
            model_calls.append((request, api_key, kwargs))
            return SimpleNamespace(content=(
                '{"tone_label":"利好","event":"公司签订重大合同",'
                '"impact":"合同有望增厚后续收入","reason":"订单已正式落地"}'
            ))

        class FakeIwencaiClient:
            def __init__(self, iwencai_config):
                self.config = iwencai_config

            def query(self, query, **kwargs):
                calls.append((query, kwargs, self.config))
                return {"datas": [], "code_count": 0}

            def comprehensive_search(self, query, **kwargs):
                calls.append((query, kwargs, self.config))
                channel = kwargs["channel"]
                if channel == "announcement":
                    return {"status_code": 0, "data": [{
                        "title": "测试公司签订重大合同公告",
                        "summary": "600000 测试签订重大合同",
                        "publish_date": "2026-07-17 09:00:00",
                        "url": "https://example.test/announcement",
                    }]}
                return {"status_code": 0, "data": []}

        original_client = news_precheck.IwencaiClient
        try:
            news_precheck.IwencaiClient = FakeIwencaiClient
            records = fetch_candidate_news_records(
                [{"code": "600000", "name": "测试"}],
                config,
                now=datetime(2026, 7, 17, 10, 0, 0),
                model_requester=model_requester,
            )
        finally:
            news_precheck.IwencaiClient = original_client

        self.assertEqual(len(calls), 3)
        self.assertEqual(
            {call[1].get("channel") for call in calls if "channel" in call[1]},
            {"announcement", "news"},
        )
        event_call = next(call for call in calls if call[1].get("skill_id"))
        self.assertEqual(event_call[1]["skill_id"], "hithink-event-query")
        self.assertFalse(event_call[1]["is_cache"])
        self.assertEqual(records[0]["tone"], "positive")
        self.assertEqual(records[0]["provider"], "同花顺问财 + decision-test-model")
        self.assertEqual(records[0]["judgment_provider"], "decision_model")
        self.assertEqual(records[0]["judgment_model"], "decision-test-model")
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(model_calls[0][1], "decision-secret")
        self.assertNotIn("关键词", records[0]["summary"])
        self.assertEqual(records[0]["source_version"], IWENCAI_NEWS_SOURCE_VERSION)
        self.assertEqual(records[0]["evidence_count"], 1)
        self.assertEqual(
            set(records[0]["source_scope"]),
            {"announcement-search", "news-search", "hithink-event-query"},
        )

    def test_market_quotes_are_not_event_evidence(self):
        candidate = {"code": "600000", "name": "测试"}
        record = parse_iwencai_candidate_news_record(
            candidate,
            fetched_at="2026-07-17T10:00:00+08:00",
            source_payloads={
                "announcement-search": {"status_code": 0, "data": []},
                "news-search": {"status_code": 0, "data": []},
                "hithink-event-query": {
                    "datas": [{"股票代码": "600000.SH", "股票简称": "测试"}],
                },
            },
        )

        self.assertTrue(record["available"])
        self.assertEqual(record["tone_label"], "中性")
        self.assertEqual(record["evidence_count"], 0)
        self.assertEqual(record["error"], "")
        self.assertIn("600000 测试", build_iwencai_candidate_news_query(candidate))

    def test_news_search_finds_recent_clarification_and_filters_old_rows(self):
        candidate = {"code": "000887.SZ", "name": "中鼎股份"}
        record = parse_iwencai_candidate_news_record(
            candidate,
            fetched_at="2026-08-13T10:00:00+08:00",
            source_payloads={
                "announcement-search": {"status_code": 0, "data": []},
                "news-search": {"status_code": 0, "data": [
                    {
                        "title": "中鼎股份：关于市场传闻的澄清及风险提示",
                        "summary": "框架协议尚无实质落地内容，暂未产生收入，存在不确定性。",
                        "publish_date": "2026-08-12 18:14:08",
                        "url": "https://example.test/current",
                    },
                    {
                        "title": "中鼎股份历史新闻",
                        "summary": "中鼎股份历史事项。",
                        "publish_date": "2026-07-01 12:00:00",
                        "url": "https://example.test/old",
                    },
                    {
                        "title": "中鼎股份8月12日主力资金净买入1000万元",
                        "summary": "000887中鼎股份最新主力资金净流入数据。",
                        "publish_date": "2026-08-12 15:30:00",
                        "url": "https://example.test/flow",
                    },
                    {
                        "title": "[风险]中鼎股份(000887)：市场传闻的澄清及风险提示",
                        "summary": "中鼎股份发布澄清公告和风险提示。",
                        "publish_date": "2026-08-12 18:30:00",
                        "url": "https://example.test/duplicate",
                    },
                ]},
                "hithink-event-query": {"datas": []},
            },
        )

        self.assertFalse(record["available"])
        self.assertFalse(record["checked"])
        self.assertEqual(record["tone_label"], "待判断")
        self.assertEqual(record["evidence_count"], 1)
        self.assertEqual(record["evidence"][0]["source"], "news-search")
        self.assertNotIn("主力资金", str(record["evidence"]))

    def test_event_query_keeps_only_recent_dated_event_fields(self):
        record = parse_iwencai_candidate_news_record(
            {"code": "600000.SH", "name": "测试"},
            fetched_at="2026-08-13T10:00:00+08:00",
            source_payloads={
                "announcement-search": {"status_code": 0, "data": []},
                "news-search": {"status_code": 0, "data": []},
                "hithink-event-query": {"datas": [{
                    "股票代码": "600000.SH",
                    "股票简称": "测试",
                    "监管函件类型[20260812]": "关注函",
                    "监管函件类型[20260701]": "历史问询函",
                    "最新价": "10.00",
                }]},
            },
        )

        self.assertFalse(record["available"])
        self.assertFalse(record["checked"])
        self.assertEqual(record["evidence_count"], 1)
        self.assertIn("20260812", record["evidence"][0]["title"])
        self.assertNotIn("20260701", record["evidence"][0]["summary"])
        self.assertEqual(
            record["evidence"][0]["published_at"],
            "2026-08-12T00:00:00+08:00",
        )

    def test_partial_source_failure_keeps_evidence_and_reports_degradation(self):
        record = parse_iwencai_candidate_news_record(
            {"code": "600000.SH", "name": "测试"},
            fetched_at="2026-08-13T10:00:00+08:00",
            source_payloads={
                "news-search": {"status_code": 0, "data": [{
                    "title": "测试公司回购股份",
                    "summary": "600000 测试公司回购股份。",
                    "publish_date": "2026-08-13 09:00:00",
                }]},
                "hithink-event-query": {"datas": []},
            },
            source_errors={"announcement-search": "http_error"},
        )

        self.assertFalse(record["available"])
        self.assertTrue(record["partial"])
        self.assertEqual(record["tone_label"], "待判断")
        announcement = next(
            item for item in record["source_results"]
            if item["skill"] == "announcement-search"
        )
        self.assertFalse(announcement["ok"])
        self.assertEqual(announcement["error"], "http_error")

    def test_all_failed_sources_are_unavailable(self):
        record = parse_iwencai_candidate_news_record(
            {"code": "600000.SH", "name": "测试"},
            fetched_at="2026-08-13T10:00:00+08:00",
            source_errors={skill: "network_error" for skill in (
                "announcement-search", "news-search", "hithink-event-query",
            )},
        )

        self.assertFalse(record["available"])
        self.assertTrue(record["partial"])
        self.assertEqual(record["error"], "iwencai_news_precheck_partial_no_evidence")

    def test_legacy_model_cache_is_never_reused(self):
        legacy_model_record = {
            "checked": True,
            "provider": "消息面预检模型",
            "source_mode": "model",
        }
        iwencai_record = {
            "checked": True,
            "provider": "同花顺问财",
            "source_mode": "iwencai",
            "source_version": IWENCAI_NEWS_SOURCE_VERSION,
            "judgment_provider": "decision_model",
            "judgment_model": "decision-test-model",
        }
        legacy_iwencai_record = {
            "checked": True,
            "provider": "同花顺问财",
            "source_mode": "iwencai",
        }

        self.assertFalse(cached_news_record_matches_source(legacy_model_record, "iwencai"))
        self.assertTrue(cached_news_record_matches_source(iwencai_record, "iwencai"))
        self.assertTrue(cached_news_record_matches_source(
            iwencai_record, "iwencai", "decision-test-model"
        ))
        self.assertFalse(cached_news_record_matches_source(
            iwencai_record, "iwencai", "another-model"
        ))
        self.assertFalse(cached_news_record_matches_source(legacy_iwencai_record, "iwencai"))
        self.assertFalse(cached_news_record_matches_source(iwencai_record, "model"))

    def test_decision_model_invalid_output_never_falls_back_to_keywords(self):
        config = NewsPrecheckConfig(
            base_url="https://openapi.iwencai.com",
            api_key="iwencai-secret",
            model="decision-test-model",
            decision_base_url="https://model.example/v1",
            decision_api_key="decision-secret",
        )
        collected = parse_iwencai_candidate_news_record(
            {"code": "600000", "name": "测试"},
            fetched_at="2026-08-13T10:00:00+08:00",
            source_payloads={
                "announcement-search": {"status_code": 0, "data": [{
                    "title": "测试公司增持回购公告",
                    "summary": "600000 测试公司增持并回购。",
                    "publish_date": "2026-08-13 09:00:00",
                }]},
                "news-search": {"status_code": 0, "data": []},
                "hithink-event-query": {"datas": []},
            },
        )

        result = judge_iwencai_news_with_decision_model(
            collected,
            config,
            requester=lambda *_args, **_kwargs: SimpleNamespace(content="利好"),
        )

        self.assertFalse(result["checked"])
        self.assertFalse(result["available"])
        self.assertEqual(result["tone_label"], "判断不可用")
        self.assertEqual(result["error"], "decision_model_invalid_json")
        self.assertNotIn("关键词", result.get("summary", ""))

    def test_formatter_keeps_iwencai_stock_identity(self):
        formatted = format_cached_news_records([{
            "code": "600000.SH",
            "name": "测试",
            "available": True,
            "summary": "公司签订重大合同（利好）",
        }])

        self.assertIn("600000.SH 测试：公司签订重大合同（利好）", formatted)


if __name__ == "__main__":
    unittest.main()
