from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.dashboard.watchlist_jobs import WatchlistJobManager
from app.storage import watchlist_tracker_store as store
from app.trading import watchlist_tracker_service as service


class WatchlistJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="niuone-watchlist-jobs-"))
        self.db = Path(self.temp) / "watchlist.db"
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)
        self.codes = ["600001", "600002", "600003"]
        for code in self.codes:
            store.upsert_stock(self.con, code=code, name="测试", buy_date="2026-09-11",
                               buy_price=10, buy_shares=100, buy_amount=1000)
        self.enterContext(patch.object(service, "now_shanghai", return_value=
            datetime(2026, 9, 14, 16, tzinfo=ZoneInfo("Asia/Shanghai"))))
        self.manager = WatchlistJobManager(self.db)
        self.addCleanup(self.manager.shutdown)

    def quote(self, code):
        return {"code": code, "trade_date": "2026-09-14", "close_price": 12}

    def finish(self, manager=None):
        manager = manager or self.manager
        manager._thread.join(timeout=3)
        self.assertFalse(manager._thread.is_alive(), "test worker did not finish")
        return manager.get()

    def test_submit_returns_before_network_and_rejects_duplicate_work(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def fetch(code):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.quote(code)

        with patch.object(service, "fetch_today_quote", side_effect=fetch):
            job = self.manager.start("update-today")
            self.assertEqual(job["total"], 3)
            self.assertTrue(entered.wait(1))
            running = self.manager.get(job["id"])
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["processed"], 0)
            self.assertEqual(running["current_code"], "600001")
            with self.assertRaises(service.JobConflictError):
                self.manager.start("backfill")
            with self.assertRaises(service.JobConflictError):
                service.update_today_quotes(db_path=self.db)
            release.set()
            finished = self.finish()
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["processed"], 3)
        self.assertEqual(finished["succeeded"], 3)

    def test_partial_failure_is_saved_and_only_failed_codes_are_retried(self):
        def fetch(code):
            if code != "600001":
                raise TimeoutError("upstream private details must not be exposed")
            return self.quote(code)

        with patch.object(service, "fetch_today_quote", side_effect=fetch):
            first = self.manager.start("update-today")
            finished = self.finish()
        self.assertEqual(finished["status"], "partial")
        self.assertEqual(finished["processed"], 3)
        self.assertEqual(finished["retry_count"], 2)
        self.assertEqual([item["code"] for item in finished["failed"]], self.codes[1:])
        self.assertNotIn("private", str(finished))
        self.assertNotIn("codes", finished)
        with patch.object(service, "fetch_today_quote", side_effect=self.quote) as fetch:
            retry = self.manager.retry(first["id"])
            self.assertEqual(retry["total"], 2)
            finished = self.finish()
        self.assertEqual([call.args[0] for call in fetch.call_args_list], self.codes[1:])
        self.assertEqual(finished["retry_count"], 0)
        self.assertEqual(store.get_stock(self.con, "600001")["buy_price"], 10)

    def test_restart_marks_incomplete_job_and_retries_failure_plus_unfinished(self):
        store.save_job(self.con, {
            "id": "old", "kind": "update-today", "params": {"force": False},
            "codes": self.codes, "processed_codes": self.codes[:2],
            "status": "running", "total": 3, "processed": 2, "succeeded": 1,
            "skipped": 0, "failed": [{"code": "600002", "error": "超时"}],
            "quotes": 0, "current_code": "600003", "error": "",
            "created_at": "2026-09-14T16:00:00+08:00", "updated_at": "", "finished_at": "",
        })
        recovered = self.manager.get()
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["retry_count"], 2)
        self.assertEqual(recovered["processed"], 2)
        with patch.object(service, "fetch_today_quote", side_effect=self.quote) as fetch:
            self.manager.retry("old")
            self.finish()
        self.assertEqual([call.args[0] for call in fetch.call_args_list], self.codes[1:])

    def test_history_retry_is_idempotent_and_empty_history_is_a_failure(self):
        def history(code, days):
            return [] if code == "600003" else [self.quote(code)]

        with patch.object(service, "fetch_history_quotes", side_effect=history):
            self.manager.start("backfill", days=60)
            result = self.finish()
            self.assertEqual(result["quotes"], 2)
            self.assertEqual(result["succeeded"], 2)
            self.manager.start("backfill", days=60)
            result = self.finish()
        self.assertEqual(result["quotes"], 0)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["failed"][0]["code"], "600003")
        self.assertEqual(store.get_stock(self.con, "600001")["buy_amount"], 1000)

    def test_stop_during_download_leaves_unwritten_codes_retryable(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def fetch(code):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.quote(code)

        with patch.object(service, "fetch_today_quote", side_effect=fetch):
            self.manager.start("update-today")
            self.assertTrue(entered.wait(1))
            self.manager._stop.set()
            release.set()
            result = self.finish()
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["retry_count"], 3)
        self.assertEqual(store.stock_quote_dates(self.con, "600001"), [])

    def test_thread_start_failure_releases_execution_slot(self):
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                self.manager.start("update-today")
        result = self.manager.get()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["retry_count"], 3)
        with patch.object(service, "fetch_today_quote", side_effect=self.quote):
            self.manager.retry(result["id"])
            self.assertEqual(self.finish()["status"], "completed")

    def test_invalid_scope_never_expands_to_all_stocks(self):
        for codes in ([], "600001", ["bad"], [None]):
            with self.subTest(codes=codes), self.assertRaises(ValueError):
                self.manager.start("update-today", codes=codes)
        self.assertIsNone(self.manager.get())
