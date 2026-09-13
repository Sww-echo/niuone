#!/usr/bin/env python3
"""Regression tests for the NewsNow realtime-news domain."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

from app.monitoring.news import (
    NEWSNOW_SOURCE_REGISTRY_REVISION,
    NewsNowClient,
    NewsNowConfig,
    NewsNowConfigurationError,
    NewsNowRequestError,
    NewsNowResponseError,
    NewsNowService,
    SUPPORTED_SOURCES,
    normalize_endpoint,
    parse_source_ids,
    shared_newsnow_service,
    source_options,
)


class FakeResponse:
    def __init__(self, payload: object, *, content_type: str = "application/json"):
        if isinstance(payload, bytes):
            self.body = payload
        else:
            self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def source_payload(source_id: str, item_id: str, title: str, *, status: str = "success") -> dict:
    return {
        "id": source_id,
        "status": status,
        "updatedTime": 1_786_420_800_000,
        "items": [{
            "id": item_id,
            "title": title,
            "pubDate": 1_786_420_800_000,
            "url": f"https://example.com/{item_id}",
            "extra": {"hover": "补充内容", "info": "✰"},
        }],
    }


class NewsNowClientTests(unittest.TestCase):
    def config(self, **overrides) -> NewsNowConfig:
        values = {
            "enabled": True,
            "endpoint": "https://news.example/api/s",
            "source_ids": ("cls-telegraph", "jin10"),
            "timeout_seconds": 8,
            "max_retries": 1,
            "max_concurrency": 2,
            "refresh_seconds": 60,
        }
        values.update(overrides)
        return NewsNowConfig(**values)

    def test_configuration_normalizes_endpoint_and_allowlisted_sources(self):
        self.assertEqual(normalize_endpoint("http://newsnow:4444"), "http://newsnow:4444/api/s")
        default_config = NewsNowConfig.from_env({})
        self.assertEqual(
            default_config.source_ids,
            ("cls-telegraph", "jin10", "wallstreetcn-quick"),
        )
        self.assertEqual(default_config.max_concurrency, 3)
        self.assertEqual(default_config.max_items, 300)
        self.assertEqual(default_config.max_important_items, 50)
        self.assertEqual(
            normalize_endpoint("https://example.com/news/api/s/"),
            "https://example.com/news/api/s",
        )
        self.assertEqual(
            parse_source_ids("jin10, wallstreetcn-quick，xueqiu-hotstock,jin10"),
            ("jin10", "wallstreetcn-quick", "xueqiu-hotstock"),
        )
        with self.assertRaises(ValueError):
            normalize_endpoint("https://user:secret@example.com")
        with self.assertRaises(ValueError):
            parse_source_ids("jin10,unknown-source")
        configured_limits = NewsNowConfig.from_env({
            "NEWSNOW_MAX_ITEMS": "450",
            "NEWSNOW_MAX_IMPORTANT_ITEMS": "75",
        })
        self.assertEqual(configured_limits.max_items, 450)
        self.assertEqual(configured_limits.max_important_items, 75)
        self.assertNotEqual(default_config.fingerprint, configured_limits.fingerprint)
        with self.assertRaisesRegex(
            NewsNowConfigurationError,
            "NEWSNOW_MAX_IMPORTANT_ITEMS 不能大于 NEWSNOW_MAX_ITEMS",
        ):
            NewsNowConfig.from_env({
                "NEWSNOW_MAX_ITEMS": "49",
                "NEWSNOW_MAX_IMPORTANT_ITEMS": "50",
            })

    def test_dashboard_and_decision_consumers_share_one_refresh_service(self):
        with tempfile.TemporaryDirectory(prefix="niuone-newsnow-shared-") as tmp:
            cache_path = Path(tmp) / "realtime_news_latest.json"
            first = shared_newsnow_service(cache_path)
            second = shared_newsnow_service(cache_path)
        self.assertIs(first, second)

    def test_source_registry_exposes_only_finance_choices_in_settings(self):
        options = source_options()

        self.assertEqual(len(SUPPORTED_SOURCES), 52)
        self.assertEqual(len(options), 12)
        self.assertEqual(len({option["id"] for option in options}), 12)
        self.assertEqual(options[0]["category"], "finance")
        self.assertEqual({option["category"] for option in options}, {"finance"})
        self.assertIn("wallstreetcn-quick", SUPPORTED_SOURCES)
        self.assertIn("bilibili-ranking", SUPPORTED_SOURCES)
        self.assertNotIn("bilibili-ranking", {option["id"] for option in options})
        self.assertNotIn("wallstreetcn", SUPPORTED_SOURCES)
        self.assertEqual(len(NEWSNOW_SOURCE_REGISTRY_REVISION), 40)
        self.assertEqual(
            parse_source_ids(",".join(option["id"] for option in options)),
            tuple(option["id"] for option in options),
        )

    def test_fetch_normalizes_news_items_without_exposing_upstream_markup(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            payload = source_payload("jin10", "flash-1", "【央行】<b>公开市场操作</b>", status="cache")
            return FakeResponse(payload)

        result = NewsNowClient(
            self.config(),
            opener=opener,
            semaphore=threading.BoundedSemaphore(1),
        ).fetch("jin10")

        self.assertEqual(result["status"], "cache")
        self.assertEqual(result["items"][0]["id"], "jin10:flash-1")
        self.assertEqual(result["items"][0]["title"], "【央行】公开市场操作")
        self.assertEqual(result["items"][0]["summary"], "补充内容")
        self.assertTrue(result["items"][0]["important"])
        request, timeout = calls[0]
        self.assertEqual(timeout, 8)
        self.assertEqual(
            request.full_url,
            "https://news.example/api/s?id=jin10&latest=true",
        )

    def test_fetch_uses_public_service_compatible_identifiable_user_agent(self):
        requests = []

        def opener(request, timeout):
            del timeout
            requests.append(request)
            return FakeResponse(source_payload("jin10", "flash-1", "金十快讯"))

        NewsNowClient(
            self.config(),
            opener=opener,
            semaphore=threading.BoundedSemaphore(1),
        ).fetch("jin10")

        user_agent = requests[0].get_header("User-agent")
        self.assertTrue(user_agent.startswith("Mozilla/5.0"))
        self.assertIn("NiuOne", user_agent)
        self.assertIn("https://github.com/kunkundi/niuone", user_agent)
        self.assertNotEqual(user_agent, "NiuOne/newsnow-client")

    def test_fetch_uses_extra_date_when_source_omits_pub_date(self):
        published_ms = 1_786_461_578_000

        def opener(_request, timeout):
            del timeout
            payload = source_payload("wallstreetcn-quick", "3148148", "华尔街见闻快讯")
            item = payload["items"][0]
            item.pop("pubDate")
            item["extra"]["date"] = published_ms
            return FakeResponse(payload)

        result = NewsNowClient(
            self.config(source_ids=("wallstreetcn-quick",)),
            opener=opener,
            semaphore=threading.BoundedSemaphore(1),
        ).fetch("wallstreetcn-quick")

        item = result["items"][0]
        self.assertEqual(item["published_at_ms"], published_ms)
        self.assertTrue(item["published_at"])

    def test_all_finance_source_formats_normalize_without_false_important_flags(self):
        published_ms = 1_786_461_658_000
        cases = {
            "mktnews-flash": ({
                "id": "019ff169-9f34-7aab-8ff5-60de8e7b525a",
                "title": "MKTNews 快讯",
                "pubDate": "2026-08-11T15:20:58.000Z",
                "url": "https://mktnews.net/flashDetail.html?id=1",
                "extra": {"hover": "补充内容", "info": "Important"},
            }, published_ms, True),
            "wallstreetcn-quick": ({
                "id": 3148148,
                "title": "华尔街见闻快讯",
                "url": "https://wallstreetcn.com/livenews/3148148",
                "extra": {"date": published_ms},
            }, published_ms, False),
            "wallstreetcn-news": ({
                "id": 3779203,
                "title": "华尔街见闻最新",
                "url": "https://wallstreetcn.com/articles/3779203",
                "extra": {"date": published_ms},
            }, published_ms, False),
            "wallstreetcn-hot": ({
                "id": 3779052,
                "title": "华尔街见闻最热",
                "url": "https://wallstreetcn.com/articles/3779052",
            }, None, False),
            "gelonghui": ({
                "id": "/news/5283801",
                "title": "格隆汇事件",
                "url": "https://www.gelonghui.com/news/5283801",
                "extra": {"date": published_ms, "info": "美股异动"},
            }, published_ms, False),
            "fastbull-news": ({
                "id": "/cn/news-detail/4385973_1",
                "title": "法布财经头条",
                "pubDate": published_ms,
                "url": "https://www.fastbull.com/cn/news-detail/4385973_1",
            }, published_ms, False),
            "fastbull-express": ({
                "id": "/cn/fastshort/4221622_212_1",
                "title": "法布财经快讯",
                "pubDate": published_ms,
                "url": "https://www.fastbull.com/cn/fastshort/4221622_212_1",
            }, published_ms, False),
            "cls-depth": ({
                "id": 2451658,
                "title": "财联社深度",
                "pubDate": published_ms,
                "url": "https://www.cls.cn/detail/2451658",
            }, published_ms, False),
            "cls-hot": ({
                "id": 2451324,
                "title": "财联社热门",
                "url": "https://www.cls.cn/detail/2451324",
            }, None, False),
            "cls-telegraph": ({
                "id": 2451675,
                "title": "财联社电报",
                "pubDate": published_ms,
                "url": "",
                "mobileUrl": "https://www.cls.cn/detail/2451675",
            }, published_ms, False),
            "jin10": ({
                "id": "20260811232045206800",
                "title": "金十数据快讯",
                "pubDate": published_ms,
                "url": "https://flash.jin10.com/detail/20260811232045206800",
                "extra": {"hover": "补充内容", "info": "✰"},
            }, published_ms, True),
            "xueqiu-hotstock": ({
                "id": "NVDA",
                "title": "英伟达",
                "url": "https://xueqiu.com/s/NVDA",
                "extra": {"info": "0.6941% NASDAQ"},
            }, None, False),
        }
        finance_source_ids = {option["id"] for option in source_options()}
        self.assertEqual(set(cases), finance_source_ids)

        for source_id, (raw_item, expected_time, expected_important) in cases.items():
            with self.subTest(source_id=source_id):
                payload = {
                    "id": source_id,
                    "status": "success",
                    "updatedTime": published_ms,
                    "items": [raw_item],
                }
                result = NewsNowClient._parse_response(
                    source_id,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )
                self.assertEqual(len(result["items"]), 1)
                item = result["items"][0]
                self.assertEqual(item["published_at_ms"], expected_time)
                self.assertEqual(item["important"], expected_important)
                self.assertTrue(item["title"])
                self.assertTrue(item["url"])

    def test_retry_is_bounded_and_cloudflare_html_is_rejected(self):
        sleeps = []
        calls = []

        def opener(request, timeout):
            del request, timeout
            calls.append(True)
            if len(calls) == 1:
                raise urllib.error.URLError("temporary")
            return FakeResponse(source_payload("jin10", "2", "重试成功"))

        result = NewsNowClient(
            self.config(),
            opener=opener,
            sleep=sleeps.append,
            semaphore=threading.BoundedSemaphore(1),
        ).fetch("jin10")
        self.assertEqual(result["items"][0]["title"], "重试成功")
        self.assertEqual(sleeps, [0.25])

        with self.assertRaises(NewsNowResponseError) as caught:
            NewsNowClient(
                self.config(max_retries=0),
                opener=lambda *_args, **_kwargs: FakeResponse(
                    b"<html>Attention Required</html>",
                    content_type="text/html",
                ),
                semaphore=threading.BoundedSemaphore(1),
            ).fetch("jin10")
        self.assertEqual(caught.exception.code, "invalid_content_type")
        self.assertNotIn("Attention Required", str(caught.exception))

    def test_response_normalization_obeys_configured_total_limit(self):
        payload = source_payload("jin10", "item-0", "快讯 0")
        payload["items"] = [
            {
                "id": f"item-{index}",
                "title": f"快讯 {index}",
                "pubDate": 1_786_420_800_000 + index,
                "url": f"https://example.com/item-{index}",
            }
            for index in range(5)
        ]

        result = NewsNowClient._parse_response(
            "jin10",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            item_limit=3,
        )

        self.assertEqual([item["external_id"] for item in result["items"]], [
            "item-0",
            "item-1",
            "item-2",
        ])


class ControlledClient:
    def __init__(self, responses):
        self.responses = responses

    def fetch(self, source_id):
        value = self.responses[source_id]
        if isinstance(value, Exception):
            raise value
        return value


class NewsNowServiceTests(unittest.TestCase):
    def config(self, **overrides) -> NewsNowConfig:
        values = {
            "enabled": True,
            "endpoint": "https://news.example/api/s",
            "source_ids": ("cls-telegraph", "jin10"),
            "timeout_seconds": 8,
            "max_retries": 0,
            "max_concurrency": 2,
            "refresh_seconds": 15,
        }
        values.update(overrides)
        return NewsNowConfig(**values)

    @staticmethod
    def normalized_source(source_id: str, item_id: str, title: str) -> dict:
        return NewsNowClient._parse_response(
            source_id,
            json.dumps(source_payload(source_id, item_id, title), ensure_ascii=False).encode("utf-8"),
        )

    @staticmethod
    def normalized_source_items(source_id: str, entries: list[tuple[str, int, bool]]) -> dict:
        payload = {
            "id": source_id,
            "status": "success",
            "updatedTime": max(published_at for _, published_at, _ in entries),
            "items": [
                {
                    "id": item_id,
                    "title": item_id,
                    "pubDate": published_at,
                    "url": f"https://example.com/{item_id}",
                    "extra": {"info": "✰" if important else ""},
                }
                for item_id, published_at, important in entries
            ],
        }
        return NewsNowClient._parse_response(
            source_id,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def test_partial_failure_reuses_failed_source_and_preserves_success_history(self):
        clock = [1000.0]
        responses = {
            "cls-telegraph": self.normalized_source("cls-telegraph", "cls-1", "财联社旧消息"),
            "jin10": self.normalized_source("jin10", "jin-1", "金十旧消息"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.json"
            service = NewsNowService(
                path,
                client_factory=lambda _config: ControlledClient(responses),
                now=lambda: clock[0],
            )
            first = service.get_news(self.config())
            self.assertEqual(first["status"], "success")
            self.assertEqual(len(first["items"]), 2)
            self.assertTrue(path.exists())

            clock[0] += 20
            responses["cls-telegraph"] = self.normalized_source(
                "cls-telegraph", "cls-2", "财联社新消息"
            )
            responses["jin10"] = NewsNowRequestError("http_403", "blocked", status_code=403)
            partial = service.get_news(self.config())

            self.assertEqual(partial["status"], "partial")
            self.assertTrue(partial["stale"])
            self.assertEqual(
                {item["title"] for item in partial["items"]},
                {"财联社新消息", "财联社旧消息", "金十旧消息"},
            )
            jin10 = next(source for source in partial["sources"] if source["id"] == "jin10")
            self.assertEqual(jin10["status"], "cache")
            self.assertEqual(jin10["error"], "http_403")

            persisted_attempt = json.loads(path.read_text(encoding="utf-8"))["attempted_at_ms"]
            clock[0] += 20
            responses["cls-telegraph"] = NewsNowRequestError("network_error", "offline")
            cached = service.get_news(self.config())
            self.assertEqual(cached["status"], "cache")
            self.assertEqual(len(cached["items"]), 3)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["attempted_at_ms"],
                persisted_attempt,
            )

    def test_successful_refresh_keeps_bounded_history_and_prioritizes_important_items(self):
        clock = [1000.0]
        base = 1_786_420_800_000
        responses = {
            "jin10": self.normalized_source_items("jin10", [
                ("old-important", base + 10, True),
                ("old-ordinary", base + 20, False),
            ]),
        }
        config = self.config(
            source_ids=("jin10",),
            max_concurrency=1,
            max_items=3,
            max_important_items=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.json"
            service = NewsNowService(
                path,
                client_factory=lambda _config: ControlledClient(responses),
                now=lambda: clock[0],
            )
            first = service.get_news(config)
            self.assertEqual(len(first["items"]), 2)

            clock[0] += 20
            responses["jin10"] = self.normalized_source_items("jin10", [
                ("new-ordinary-1", base + 40, False),
                ("new-ordinary-2", base + 30, False),
            ])
            second = service.get_news(config)

            self.assertEqual(
                {item["external_id"] for item in second["items"]},
                {"old-important", "new-ordinary-1", "new-ordinary-2"},
            )
            self.assertEqual(sum(item["important"] for item in second["items"]), 1)
            self.assertEqual(second["sources"][0]["count"], 3)

            clock[0] += 20
            responses["jin10"] = self.normalized_source_items("jin10", [
                ("new-important-1", base + 60, True),
                ("new-important-2", base + 50, True),
            ])
            third = service.get_news(config)
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(third["items"]), 3)
        self.assertEqual(
            [item["external_id"] for item in third["items"] if item["important"]],
            ["new-important-1"],
        )
        self.assertNotIn("old-important", {item["external_id"] for item in third["items"]})
        self.assertEqual(persisted["max_items"], 3)
        self.assertEqual(persisted["max_important_items"], 1)

    def test_refresh_window_suppresses_duplicate_request_storms(self):
        calls = []
        clock = [1000.0]

        class CountingClient:
            def fetch(self, source_id):
                calls.append(source_id)
                return NewsNowServiceTests.normalized_source(source_id, source_id, source_id)

        with tempfile.TemporaryDirectory() as tmp:
            service = NewsNowService(
                Path(tmp) / "news.json",
                client_factory=lambda _config: CountingClient(),
                now=lambda: clock[0],
            )
            first = service.get_news(self.config())
            second = service.get_news(self.config())

        self.assertFalse(first["served_from_local_cache"])
        self.assertTrue(second["served_from_local_cache"])
        self.assertCountEqual(calls, ["cls-telegraph", "jin10"])


if __name__ == "__main__":
    unittest.main()
