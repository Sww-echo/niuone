#!/usr/bin/env python3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.trading.news_decision_context import (  # noqa: E402
    build_important_news_decision_context,
    decision_target_trading_day,
    format_important_realtime_news_for_prompt,
    load_important_realtime_news_decision_context,
    news_effective_trading_day,
)


CN_TZ = ZoneInfo("Asia/Shanghai")
TRADING_DAYS = {
    date(2026, 7, 3),
    date(2026, 7, 6),
    date(2026, 7, 7),
}


def fake_trading_day_status(value, allow_refresh=False):
    target = value.date() if isinstance(value, datetime) else value
    ordered = sorted(TRADING_DAYS)
    return {
        "date": target.isoformat(),
        "is_trading_day": target in TRADING_DAYS,
        "previous_trading_day": max((day for day in ordered if day < target), default="").isoformat()
        if any(day < target for day in ordered)
        else "",
        "next_trading_day": min((day for day in ordered if day > target), default="").isoformat()
        if any(day > target for day in ordered)
        else "",
    }


def news_item(item_id, published_at, *, important=True, title=None):
    published = datetime.fromisoformat(published_at).replace(tzinfo=CN_TZ)
    return {
        "id": item_id,
        "source_id": "cls-telegraph",
        "source_name": "财联社电报",
        "title": title or item_id,
        "summary": f"{item_id}摘要",
        "important": important,
        "published_at": published.isoformat(timespec="seconds"),
        "published_at_ms": int(published.timestamp() * 1000),
    }


class FakeService:
    def __init__(self, payload):
        self.payload = payload
        self.configs = []

    def get_news(self, config):
        self.configs.append(config)
        return self.payload


class RealtimeNewsDecisionContextTests(unittest.TestCase):
    def test_intraday_and_after_close_items_follow_a_share_session(self):
        payload = {
            "status": "success",
            "generated_at": "2026-07-06T10:00:00+08:00",
            "items": [
                news_item("friday-intraday", "2026-07-03T10:00:00"),
                news_item("friday-close", "2026-07-03T15:00:00"),
                news_item("friday-after-close", "2026-07-03T15:10:00"),
                news_item("weekend", "2026-07-04T11:00:00"),
                news_item("monday-premarket", "2026-07-06T08:30:00"),
                news_item("future", "2026-07-06T10:30:00"),
                news_item("ordinary", "2026-07-06T09:00:00", important=False),
            ],
        }

        monday = build_important_news_decision_context(
            payload,
            as_of=datetime(2026, 7, 6, 10, 0, tzinfo=CN_TZ),
            trading_day_lookup=fake_trading_day_status,
        )

        self.assertEqual(monday["target_trading_date"], "2026-07-06")
        self.assertEqual(
            [item["id"] for item in monday["items"]],
            ["monday-premarket", "weekend", "friday-after-close", "friday-close"],
        )
        self.assertEqual(monday["items"][0]["decision_role"], "intraday")
        self.assertTrue(
            all(
                item["decision_role"] == "next_trading_day_auxiliary"
                for item in monday["items"][1:]
            )
        )

        friday = build_important_news_decision_context(
            payload,
            as_of=datetime(2026, 7, 3, 14, 0, tzinfo=CN_TZ),
            trading_day_lookup=fake_trading_day_status,
        )
        self.assertEqual([item["id"] for item in friday["items"]], ["friday-intraday"])

    def test_after_close_decision_targets_next_trading_day(self):
        self.assertEqual(
            decision_target_trading_day(
                datetime(2026, 7, 3, 15, 1, tzinfo=CN_TZ),
                trading_day_lookup=fake_trading_day_status,
            ),
            date(2026, 7, 6),
        )
        effective_day, role = news_effective_trading_day(
            datetime(2026, 7, 3, 15, 0, tzinfo=CN_TZ),
            trading_day_lookup=fake_trading_day_status,
        )
        self.assertEqual(effective_day, date(2026, 7, 6))
        self.assertEqual(role, "next_trading_day_auxiliary")

    def test_loader_refreshes_once_and_formats_auditable_prompt(self):
        payload = {
            "status": "partial",
            "stale": True,
            "generated_at": "2026-07-06T09:30:00+08:00",
            "items": [news_item("policy", "2026-07-06T09:20:00", title="重要政策发布")],
        }
        service = FakeService(payload)
        with tempfile.TemporaryDirectory(prefix="niuone-news-decision-") as tmp:
            context = load_important_realtime_news_decision_context(
                Path(tmp) / "news.json",
                enabled=True,
                as_of=datetime(2026, 7, 6, 10, 0, tzinfo=CN_TZ),
                env={
                    "NEWSNOW_ENABLED": "1",
                    "NEWSNOW_BASE_URL": "http://newsnow:4444/api/s",
                    "NEWSNOW_SOURCES": "cls-telegraph",
                },
                service=service,
                trading_day_lookup=fake_trading_day_status,
            )

        prompt = format_important_realtime_news_for_prompt(context)
        self.assertEqual(len(service.configs), 1)
        self.assertTrue(context["available"])
        self.assertTrue(context["stale"])
        self.assertIn("【财经快讯重要信息】", prompt)
        self.assertIn("[当日盘中][09:20] 财联社电报：重要政策发布", prompt)
        self.assertIn("不能自行新增候选", prompt)
        self.assertIn("陈旧缓存", prompt)

    def test_missing_publish_time_is_never_used_for_decision(self):
        context = build_important_news_decision_context(
            {
                "status": "success",
                "items": [
                    {
                        "id": "missing-time",
                        "important": True,
                        "title": "无法安全归属的旧新闻",
                        "collected_at_ms": 1_783_296_000_000,
                    }
                ],
            },
            as_of=datetime(2026, 7, 6, 10, 0, tzinfo=CN_TZ),
            trading_day_lookup=fake_trading_day_status,
        )
        self.assertFalse(context["available"])
        self.assertEqual(context["skipped_without_published_at"], 1)

    def test_disabled_loader_does_not_touch_service(self):
        service = FakeService({"items": [news_item("unused", "2026-07-06T09:20:00")]})
        context = load_important_realtime_news_decision_context(
            Path("unused.json"),
            enabled=False,
            as_of=datetime(2026, 7, 6, 10, 0, tzinfo=CN_TZ),
            service=service,
            trading_day_lookup=fake_trading_day_status,
        )
        self.assertFalse(context["enabled"])
        self.assertEqual(service.configs, [])


if __name__ == "__main__":
    unittest.main()
