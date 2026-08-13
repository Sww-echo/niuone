#!/usr/bin/env python3
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app.market_data import tencent_kline_cache as cache


def sample_rows(last_day: str = "2026-07-28", count: int = 60) -> list[dict]:
    last_date = date.fromisoformat(last_day)
    first_date = last_date - timedelta(days=max(0, count - 1))
    rows = []
    for index in range(count):
        date_text = (first_date + timedelta(days=index)).isoformat()
        price = 10 + index / 100
        rows.append({
            "date": date_text,
            "open": price,
            "close": price + 0.02,
            "high": price + 0.05,
            "low": price - 0.05,
            "volume": 1000 + index,
        })
    return rows


class TencentKlineCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="niuone-kline-cache-")
        self.path = Path(self.temp.name) / "daily.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_store_and_bulk_load_only_accept_fresh_completed_history(self):
        stored = cache.store_kline_series(
            {"sh600001": sample_rows(), "sz000001": sample_rows("2026-07-25")},
            path=self.path,
            fetched_at="2026-07-29 09:10:00",
        )

        self.assertEqual(stored, 2)
        loaded = cache.load_kline_series_map(
            ["sh600001", "sz000001"],
            path=self.path,
            accepted_last_dates={"2026-07-28"},
            min_rows=55,
        )
        self.assertEqual(list(loaded), ["sh600001"])
        self.assertEqual(loaded["sh600001"][-1]["date"], "2026-07-28")

    def test_failed_refresh_preserves_previous_valid_series(self):
        original = sample_rows()
        cache.store_kline_series({"sh600001": original}, path=self.path)
        cache.record_kline_failures({"sh600001": "timeout"}, path=self.path)

        loaded = cache.load_kline_series_map(
            ["sh600001"],
            path=self.path,
            accepted_last_dates={"2026-07-28"},
            min_rows=55,
        )
        self.assertEqual(len(loaded["sh600001"]), 60)
        self.assertEqual(loaded["sh600001"][-1]["close"], original[-1]["close"])

    def test_lists_cached_symbols_without_loading_history_payloads(self):
        cache.store_kline_series(
            {"sh600001": sample_rows(), "sz000001": sample_rows(count=20)},
            path=self.path,
        )

        self.assertEqual(
            cache.load_cached_kline_symbols(path=self.path, min_rows=30),
            ("sh600001",),
        )

    def test_merge_live_quote_appends_or_replaces_without_mutating_cache(self):
        historical = sample_rows()
        original_close = historical[-1]["close"]
        quote = {
            "quote_time": "20260729100501",
            "open": 11.0,
            "price": 11.5,
            "high": 11.8,
            "low": 10.9,
            "volume": 8888,
        }

        merged = cache.merge_live_quote(historical, quote)
        replaced = cache.merge_live_quote(merged, {**quote, "price": 11.7, "high": 12.0})

        self.assertEqual(historical[-1]["close"], original_close)
        self.assertEqual(merged[-1]["date"], "2026-07-29")
        self.assertEqual(merged[-1]["close"], 11.5)
        self.assertEqual(merged[-1]["bar_status"], "live")
        self.assertTrue(all(row["bar_status"] == "closed" for row in merged[:-1]))
        self.assertEqual(replaced[-1]["close"], 11.7)
        self.assertEqual(replaced[-1]["bar_status"], "live")
        self.assertEqual(len(replaced), len(merged))

        closed = cache.merge_live_quote(historical, {})
        self.assertTrue(closed)
        self.assertTrue(all(row["bar_status"] == "closed" for row in closed))

    def test_merge_live_quote_defaults_to_120_but_preserves_explicit_windows(self):
        historical = sample_rows(count=500)
        original = [dict(row) for row in historical]
        quote = {
            "quote_time": "20260729100501",
            "open": 15.0,
            "price": 15.5,
            "high": 15.8,
            "low": 14.9,
            "volume": 8888,
        }

        default_rows = cache.merge_live_quote(historical, quote)
        rows_250 = cache.merge_live_quote(historical, quote, limit=250)
        rows_500 = cache.merge_live_quote(historical, quote, limit=500)
        replaced_500 = cache.merge_live_quote(
            rows_500,
            {**quote, "price": 15.7, "high": 16.0},
            limit=500,
        )

        self.assertEqual(len(default_rows), 120)
        self.assertEqual(default_rows[-1]["date"], "2026-07-29")
        self.assertEqual(default_rows[-1]["close"], 15.5)
        self.assertEqual(len(rows_250), 250)
        self.assertEqual(len(rows_500), 500)
        self.assertEqual(rows_500[-1]["date"], "2026-07-29")
        self.assertEqual(rows_500[-1]["close"], 15.5)
        self.assertEqual(len(replaced_500), 500)
        self.assertEqual(replaced_500[-1]["close"], 15.7)
        self.assertEqual(historical, original)

    def test_cache_can_store_and_load_500_rows_without_changing_default_store(self):
        rows = sample_rows(count=500)

        cache.store_kline_series({"sh600001": rows}, path=self.path)
        default_stored = cache.load_kline_series_map(
            ["sh600001"], path=self.path, min_rows=1, count=500
        )
        cache.store_kline_series(
            {"sh600001": rows}, path=self.path, limit=500
        )
        full_stored = cache.load_kline_series_map(
            ["sh600001"], path=self.path, min_rows=500, count=500
        )

        self.assertEqual(len(default_stored["sh600001"]), 120)
        self.assertEqual(len(full_stored["sh600001"]), 500)

    def test_short_fallback_refresh_preserves_existing_500_row_capability(self):
        original = sample_rows("2026-07-28", count=500)
        cache.store_kline_series(
            {"sh600001": original}, path=self.path, limit=500,
        )
        refreshed = sample_rows("2026-07-29", count=120)
        refreshed[-2]["close"] = 88.88

        cache.store_kline_series({"sh600001": refreshed}, path=self.path)

        loaded = cache.load_kline_series_map(
            ["sh600001"],
            path=self.path,
            min_rows=490,
            count=500,
            min_requested_count=500,
        )["sh600001"]
        by_date = {row["date"]: row for row in loaded}
        self.assertEqual(len(loaded), 500)
        self.assertEqual(loaded[-1]["date"], "2026-07-29")
        self.assertEqual(by_date[refreshed[-2]["date"]]["close"], 88.88)

    def test_corrupt_long_cache_is_replaced_by_valid_short_refresh(self):
        cache.store_kline_series(
            {"sh600001": sample_rows(count=500)}, path=self.path, limit=500,
        )
        connection = cache._open_database(self.path)
        try:
            with connection:
                connection.execute(
                    "UPDATE kline_series SET rows_json='not-json' WHERE symbol='sh600001'"
                )
        finally:
            connection.close()

        cache.store_kline_series(
            {"sh600001": sample_rows("2026-07-29", count=120)}, path=self.path,
        )

        default_rows = cache.load_kline_series_map(
            ["sh600001"], path=self.path, min_rows=120, count=120,
        )
        long_rows = cache.load_kline_series_map(
            ["sh600001"],
            path=self.path,
            min_rows=120,
            count=500,
            min_requested_count=500,
        )
        self.assertEqual(len(default_rows["sh600001"]), 120)
        self.assertEqual(long_rows, {})

    def test_legacy_schema_marks_existing_rows_as_120_row_requests(self):
        rows = sample_rows(count=120)
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '2');
            CREATE TABLE kline_series (
                symbol TEXT PRIMARY KEY,
                adjustment TEXT NOT NULL,
                first_trade_date TEXT NOT NULL,
                last_trade_date TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                rows_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                updated_ts REAL NOT NULL
            );
            CREATE TABLE kline_attempts (
                symbol TEXT PRIMARY KEY,
                attempted_at TEXT NOT NULL,
                error_code TEXT NOT NULL
            );
            CREATE TABLE prewarm_runs (
                target_date TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                requested_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                duration_seconds REAL NOT NULL DEFAULT 0,
                error_summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        connection.execute(
            "INSERT INTO kline_series VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sh600001",
                "qfq",
                rows[0]["date"],
                rows[-1]["date"],
                len(rows),
                json.dumps(rows),
                "2026-07-28 15:00:00",
                1.0,
            ),
        )
        connection.commit()
        connection.close()

        default_loaded = cache.load_kline_series_map(
            ["sh600001"], path=self.path, min_rows=120, count=120
        )
        extended_loaded = cache.load_kline_series_map(
            ["sh600001"],
            path=self.path,
            min_rows=120,
            count=500,
            min_requested_count=500,
        )

        self.assertEqual(len(default_loaded["sh600001"]), 120)
        self.assertEqual(extended_loaded, {})

    def test_500_row_cache_and_live_bar_produce_at_least_60_weekly_bars(self):
        from app.market_data.technical_analysis import _aggregate_weekly

        historical = sample_rows(count=500)
        cache.store_kline_series(
            {"sh600001": historical}, path=self.path, limit=500
        )
        loaded = cache.load_kline_series_map(
            ["sh600001"], path=self.path, min_rows=500, count=500
        )["sh600001"]
        merged = cache.merge_live_quote(
            loaded,
            {
                "quote_time": "20260729100501",
                "open": 15.0,
                "price": 15.5,
                "high": 15.8,
                "low": 14.9,
                "volume": 8888,
            },
            limit=500,
        )
        weekly = _aggregate_weekly(merged)

        self.assertEqual(len(merged), 500)
        self.assertGreaterEqual(len(weekly), 60)

    def test_prewarm_records_coverage_and_keeps_successes(self):
        requested_counts = []

        def fetcher(symbol, _count):
            requested_counts.append(_count)
            if symbol == "sz000002":
                return []
            return sample_rows()

        result = cache.prewarm_kline_cache(
            ["sh600001", "sz000001", "sz000002"],
            path=self.path,
            target_date="2026-07-29",
            workers=3,
            max_attempts=1,
            fetcher=fetcher,
        )

        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["kline_count"], 500)
        self.assertEqual(requested_counts, [500, 500, 500])
        self.assertFalse(
            cache.prewarm_completed_for_date(
                "2026-07-29", path=self.path, minimum_coverage=0.90
            )
        )
        self.assertTrue(
            cache.prewarm_completed_for_date(
                "2026-07-29", path=self.path, minimum_coverage=0.60
            )
        )

        readiness = cache.kline_cache_readiness(
            accepted_last_dates={"2026-07-28"},
            path=self.path,
            minimum_coverage=0.60,
        )
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["requested_count"], 3)
        self.assertEqual(readiness["completed_count"], 3)
        self.assertEqual(readiness["fresh_count"], 2)
        self.assertEqual(readiness["coverage"], 0.6667)

    def test_resume_fetches_only_missing_or_stale_symbols(self):
        cache.store_kline_series(
            {"sh600001": sample_rows("2026-07-28", count=500)},
            path=self.path,
            limit=500,
        )
        calls = []

        def fetcher(symbol, _count):
            calls.append(symbol)
            return sample_rows("2026-07-28")

        result = cache.prewarm_kline_cache(
            ["sh600001", "sz000001"],
            path=self.path,
            target_date="2026-07-29",
            accepted_last_dates={"2026-07-28"},
            workers=2,
            count=500,
            max_attempts=1,
            fetcher=fetcher,
        )

        self.assertEqual(calls, ["sz000001"])
        self.assertEqual(result["requested_count"], 2)
        self.assertEqual(result["reused_count"], 1)
        self.assertEqual(result["success_count"], 2)

    def test_500_row_resume_upgrades_a_fresh_legacy_120_row_entry(self):
        cache.store_kline_series(
            {"sh600001": sample_rows("2026-07-28", count=120)},
            path=self.path,
        )
        calls = []

        def fetcher(symbol, requested_count):
            calls.append((symbol, requested_count))
            return sample_rows("2026-07-28", count=requested_count)

        result = cache.prewarm_kline_cache(
            ["sh600001"],
            path=self.path,
            target_date="2026-07-29",
            accepted_last_dates={"2026-07-28"},
            workers=1,
            max_attempts=1,
            fetcher=fetcher,
        )
        loaded = cache.load_kline_series_map(
            ["sh600001"], path=self.path, min_rows=500, count=500
        )

        self.assertEqual(calls, [("sh600001", 500)])
        self.assertEqual(result["reused_count"], 0)
        self.assertEqual(len(loaded["sh600001"]), 500)

    def test_legacy_120_row_cache_does_not_satisfy_500_row_readiness(self):
        cache.store_kline_series(
            {"sh600001": sample_rows("2026-07-28", count=120)},
            path=self.path,
        )
        connection = cache._open_database(self.path)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO prewarm_runs("
                    "target_date, started_at, finished_at, requested_count, "
                    "completed_count, success_count, status, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "2026-07-29",
                        "2026-07-29 08:00:00",
                        "2026-07-29 08:01:00",
                        1,
                        1,
                        1,
                        "completed",
                        "2026-07-29 08:01:00",
                    ),
                )
        finally:
            connection.close()

        self.assertFalse(
            cache.prewarm_completed_for_date("2026-07-29", path=self.path)
        )
        readiness = cache.kline_cache_readiness(
            accepted_last_dates={"2026-07-28"}, path=self.path,
        )
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["cached_count"], 1)
        self.assertEqual(readiness["capable_count"], 0)
        self.assertEqual(readiness["fresh_count"], 0)
        self.assertEqual(readiness["min_requested_count"], 500)
        self.assertEqual(readiness["error_code"], "kline_cache_upgrade_required")

    def test_readiness_can_explicitly_accept_legacy_120_row_window(self):
        cache.store_kline_series(
            {"sh600001": sample_rows("2026-07-28", count=120)},
            path=self.path,
        )
        connection = cache._open_database(self.path)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO prewarm_runs("
                    "target_date, started_at, finished_at, requested_count, "
                    "completed_count, success_count, status, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "2026-07-29",
                        "2026-07-29 08:00:00",
                        "2026-07-29 08:01:00",
                        1,
                        1,
                        1,
                        "completed",
                        "2026-07-29 08:01:00",
                    ),
                )
        finally:
            connection.close()

        self.assertTrue(cache.prewarm_completed_for_date(
            "2026-07-29", path=self.path, min_requested_count=120,
        ))
        readiness = cache.kline_cache_readiness(
            accepted_last_dates={"2026-07-28"},
            path=self.path,
            min_requested_count=120,
        )
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["cached_count"], 1)
        self.assertEqual(readiness["capable_count"], 1)
        self.assertEqual(readiness["fresh_count"], 1)
        self.assertEqual(readiness["min_requested_count"], 120)

    def test_aggregate_failure_is_visible_without_deleting_cache(self):
        cache.prewarm_kline_cache(
            ["sh600001"],
            path=self.path,
            target_date="2026-07-29",
            workers=1,
            max_attempts=1,
            fetcher=lambda *_args: sample_rows(),
        )

        cache.mark_prewarm_run_failed(
            "2026-07-29",
            "aggregate_timeout",
            path=self.path,
        )
        readiness = cache.kline_cache_readiness(
            accepted_last_dates={"2026-07-28"},
            path=self.path,
        )

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["status"], "error")
        self.assertEqual(readiness["error_code"], "")
        self.assertEqual(readiness["fresh_count"], 1)

    def test_aggregate_failure_is_recorded_before_any_series_exists(self):
        cache.mark_prewarm_run_failed(
            "2026-07-29",
            "prewarm_process_failed",
            path=self.path,
        )

        readiness = cache.kline_cache_readiness(
            accepted_last_dates={"2026-07-28"},
            path=self.path,
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "error")
        self.assertEqual(readiness["error_code"], "prewarm_process_failed")


if __name__ == "__main__":
    unittest.main()
