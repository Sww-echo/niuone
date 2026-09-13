from __future__ import annotations

import threading
import time
import unittest

from app.dashboard.today_candidates import (
    build_today_candidate_intraday_payload,
    build_today_candidates_payload,
)


class TodayCandidatesTests(unittest.TestCase):
    def test_merges_today_trade_ready_rows_by_stock_and_keeps_best_snapshot(self) -> None:
        scans = [
            {
                "generated_at": "2026-08-28 09:45:00",
                "strategy_meta": {
                    "niu_leader": {
                        "label": "牛牛战法 · 领涨",
                        "color": "#8b5cf6",
                        "private_rule": "secret",
                    }
                },
                "trade_items": [{
                    "code": "600001",
                    "name": "测试一",
                    "best_strategy": "niu_leader",
                    "best_score": 8.4,
                    "entry_threshold": 8.0,
                    "actionable": True,
                    "price": 10.2,
                    "private_context": {"token": "secret"},
                }],
            },
            {
                "generated_at": "2026-08-28 10:30:00",
                "trade_items": [
                    {
                        "code": "600001",
                        "name": "测试一",
                        "best_strategy": "niu_leader",
                        "best_score": 9.1,
                        "entry_threshold": 8.0,
                        "actionable": True,
                        "price": 10.8,
                    },
                    {
                        "code": "000002",
                        "name": "测试二",
                        "best_strategy": "trend_pullback",
                        "best_score": 8.6,
                        "entry_threshold": 8.0,
                        "actionable": True,
                    },
                ],
            },
            {
                "generated_at": "2026-08-28 10:30:00",
                "trade_items": [{"code": "duplicate-scan", "best_score": 10}],
            },
            {
                "generated_at": "2026-08-27 14:30:00",
                "trade_items": [{"code": "previous-day", "best_score": 10}],
            },
        ]

        payload = build_today_candidates_payload(scans, current_date="2026-08-28")

        self.assertEqual(payload["scan_count"], 2)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["current_count"], 2)
        self.assertEqual([item["code"] for item in payload["items"]], ["600001", "000002"])
        first = payload["items"][0]
        self.assertEqual(first["best_score"], 9.1)
        self.assertEqual(first["price"], 10.8)
        self.assertEqual(first["first_qualified_at"], "2026-08-28 09:45:00")
        self.assertEqual(first["last_qualified_at"], "2026-08-28 10:30:00")
        self.assertEqual(first["best_qualified_at"], "2026-08-28 10:30:00")
        self.assertEqual(first["qualified_count"], 2)
        self.assertTrue(first["currently_qualified"])
        self.assertEqual(
            first["qualification_transitions"],
            [
                {
                    "at": "2026-08-28 09:45:00",
                    "qualified": True,
                    "score": 8.4,
                    "strategy": "niu_leader",
                },
            ],
        )
        self.assertNotIn("private_context", first)
        self.assertEqual(
            payload["strategy_meta"]["niu_leader"],
            {"label": "牛牛战法 · 领涨", "color": "#8b5cf6"},
        )

    def test_marks_only_the_first_point_of_each_qualification_state(self) -> None:
        scans = [
            {
                "generated_at": "2026-08-28 09:45:00",
                "trade_items": [{
                    "code": "600001",
                    "best_strategy": "niu_leader",
                    "best_score": 8.4,
                }],
            },
            {
                "generated_at": "2026-08-28 10:00:00",
                "trade_items": [{"code": "600001", "best_score": 8.6}],
            },
            {
                "generated_at": "2026-08-28 10:30:00",
                "trade_items": [],
                "items": [{"code": "600001", "best_score": 7.7}],
            },
            {
                "generated_at": "2026-08-28 11:00:00",
                "trade_items": [],
                "items": [{"code": "600001", "best_score": 7.5}],
            },
            {
                "generated_at": "2026-08-28 13:30:00",
                "trade_items": [{"code": "600001", "best_score": 8.8}],
            },
        ]

        payload = build_today_candidates_payload(scans, current_date="2026-08-28")

        self.assertEqual(payload["items"][0]["qualified_count"], 3)
        self.assertEqual(payload["current_count"], 1)
        self.assertTrue(payload["items"][0]["currently_qualified"])
        self.assertEqual(
            payload["items"][0]["qualification_transitions"],
            [
                {"at": "2026-08-28 09:45:00", "qualified": True, "score": 8.4, "strategy": "niu_leader"},
                {"at": "2026-08-28 10:30:00", "qualified": False, "score": 7.7},
                {"at": "2026-08-28 13:30:00", "qualified": True, "score": 8.8},
            ],
        )

    def test_explicit_empty_trade_pool_does_not_treat_display_rows_as_qualified(self) -> None:
        payload = build_today_candidates_payload(
            [{
                "generated_at": "2026-08-28 11:00:00",
                "items": [{
                    "code": "600003",
                    "best_score": 9.5,
                    "entry_threshold": 8,
                    "actionable": True,
                }],
                "trade_items": [],
            }],
            current_date="2026-08-28",
        )

        self.assertEqual(payload["scan_count"], 1)
        self.assertEqual(payload["current_count"], 0)
        self.assertEqual(payload["items"], [])

    def test_legacy_archive_without_trade_items_uses_threshold_fallback(self) -> None:
        payload = build_today_candidates_payload(
            [{
                "generated_at": "2026-08-28 13:30:00",
                "items": [
                    {
                        "code": "600004",
                        "best_score": 8.2,
                        "entry_threshold": 8,
                        "actionable": True,
                    },
                    {
                        "code": "600005",
                        "best_score": 9.2,
                        "entry_threshold": 8,
                        "actionable": True,
                        "hard_blockers": ["停牌"],
                    },
                ],
            }],
            current_date="2026-08-28",
        )

        self.assertEqual([item["code"] for item in payload["items"]], ["600004"])
        self.assertEqual(payload["current_count"], 1)
        self.assertTrue(payload["items"][0]["currently_qualified"])

    def test_distinguishes_current_trade_pool_from_today_cumulative_count(self) -> None:
        payload = build_today_candidates_payload(
            [
                {
                    "generated_at": "2026-08-28 09:45:00",
                    "trade_items": [
                        {"code": "600001", "best_score": 8.4},
                        {"code": "600002", "best_score": 8.2},
                    ],
                },
                {
                    "generated_at": "2026-08-28 10:30:00",
                    "trade_items": [{"code": "600002", "best_score": 8.5}],
                    "items": [
                        {"code": "600001", "best_score": 7.7},
                        {"code": "600002", "best_score": 8.5},
                    ],
                },
            ],
            current_date="2026-08-28",
        )

        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["current_count"], 1)
        current_by_code = {
            item["code"]: item["currently_qualified"]
            for item in payload["items"]
        }
        self.assertEqual(current_by_code, {"600001": False, "600002": True})

    def test_intraday_batch_is_bounded_concurrent_and_keeps_candidate_order(self) -> None:
        lock = threading.Lock()
        active = 0
        peak_active = 0
        seen_previous_close: dict[str, float | None] = {}

        def fetcher(code: str, previous_close: float | None) -> dict[str, object]:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
                seen_previous_close[code] = previous_close
            time.sleep(0.01)
            with lock:
                active -= 1
            return {
                "updated_at": "2026-08-28 10:30:00",
                "prev_close": previous_close,
                "last_price": 10.2,
                "last_pct": 2,
                "points": [
                    {"time": "09:30", "minute": 0, "price": 10, "pct": 0},
                    {"time": "10:30", "minute": 60, "price": 10.2, "pct": 2},
                ],
            }

        candidates = [
            {"code": f"{index:06d}", "price": 10.2, "change_pct": 2}
            for index in range(1, 9)
        ]
        payload = build_today_candidate_intraday_payload(
            candidates,
            fetcher=fetcher,
            generated_at="2026-08-28 10:30:01",
            max_items=6,
            max_workers=3,
        )

        self.assertEqual(payload["requested_count"], 6)
        self.assertEqual(payload["count"], 6)
        self.assertTrue(payload["truncated"])
        self.assertLessEqual(peak_active, 3)
        self.assertEqual(
            [item["code"] for item in payload["items"]],
            [f"{index:06d}" for index in range(1, 7)],
        )
        self.assertAlmostEqual(seen_previous_close["000001"] or 0, 10, places=6)

    def test_intraday_batch_degrades_per_stock_and_only_exposes_chart_fields(self) -> None:
        def fetcher(code: str, previous_close: float | None) -> dict[str, object]:
            if code == "000002":
                raise TimeoutError("provider timeout with private detail")
            return {
                "source": "private-provider-name",
                "secret": "not-public",
                "prev_close": previous_close,
                "last_price": 10.1,
                "points": [
                    {"time": "09:30", "minute": 0, "price": 10, "volume": 100},
                    {"time": "09:31", "minute": 1, "price": 10.1, "volume": 200},
                ],
            }

        payload = build_today_candidate_intraday_payload(
            [
                {"code": "000001", "price": 10, "change_pct": 0},
                {"code": "000002", "price": 12, "change_pct": 1},
                {"code": "000001", "price": 10, "change_pct": 0},
                {"code": "invalid"},
            ],
            fetcher=fetcher,
            generated_at="2026-08-28 09:31:10",
        )

        self.assertEqual(payload["requested_count"], 2)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["timed_out_count"], 0)
        self.assertNotIn("error", payload)
        self.assertEqual(payload["items"][0]["code"], "000001")
        self.assertEqual(
            set(payload["items"][0]),
            {"code", "updated_at", "prev_close", "last_price", "last_pct", "points"},
        )
        self.assertNotIn("volume", payload["items"][0]["points"][0])


if __name__ == "__main__":
    unittest.main()
