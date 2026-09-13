from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.dashboard.routers.watchlist_tracker import create_watchlist_tracker_router
from app.dashboard.watchlist_jobs import WatchlistJobManager
from app.storage import watchlist_tracker_store as store
from app.trading import watchlist_tracker_service as service


class WatchlistRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="niuone-watchlist-api-"))
        self.db = Path(self.temp) / "watchlist.db"
        self.enterContext(patch.object(store, "DEFAULT_DB_PATH", self.db))
        self.enterContext(patch.object(service, "now_shanghai", return_value=
            datetime(2026, 9, 14, 16, tzinfo=ZoneInfo("Asia/Shanghai"))))
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)
        group = store.create_group(self.con, name="A组")
        store.upsert_stock(self.con, code="600001", group_id=group["id"])
        store.upsert_stock(self.con, code="600002")
        self.allowed = True

        async def guard(request):
            return None if self.allowed else JSONResponse({"error": "admin_required"}, status_code=403)

        async def limits(request):
            return None

        self.manager = WatchlistJobManager(self.db)
        self.addCleanup(self.manager.shutdown)
        app = FastAPI()
        app.include_router(create_watchlist_tracker_router(
            enforce_api_limits=limits, require_admin_action=guard, job_manager=self.manager,
            json_response=lambda request, payload, **kw: JSONResponse(
                payload, status_code=kw.get("status_code", 200), headers={"Cache-Control": kw["cache_control"]},
            ),
        ))
        self.client = self.enterContext(TestClient(app))

    def test_ungrouped_and_all_are_distinct_api_filters(self):
        board = self.client.get("/api/watchlist/board?group_id=ungrouped&days=60").json()
        self.assertEqual([row["code"] for row in board["stocks"]], ["600002"])
        self.assertEqual(board["days"], 60)
        self.assertEqual(board["total_all"], 2)
        self.assertEqual(board["groups"][0]["stock_count"], 1)

    def test_bad_parameters_are_400_without_modifying_records(self):
        before = store.get_stock(self.con, "600001")
        cases = [
            ("post", "/api/watchlist/stocks", {"codes": "600003", "group_id": "bad"}),
            ("post", "/api/watchlist/stocks", {"codes": "600003", "target_amount": "bad"}),
            ("patch", "/api/watchlist/stocks/600001", {"target_amount": "nan"}),
            ("patch", "/api/watchlist/stocks/600001", {"target_amount": None}),
            ("patch", "/api/watchlist/stocks/600001", {"buy_date": "2026-09-13"}),
            ("post", "/api/watchlist/quotes/backfill", {"days": []}),
            ("post", "/api/watchlist/quotes/update-today", {"codes": "600001"}),
            ("post", "/api/watchlist/jobs", {"kind": "backfill", "days": "bad"}),
            ("post", "/api/watchlist/jobs", {"kind": "update-today", "codes": []}),
            ("post", "/api/watchlist/jobs", {"kind": "backfill", "missing_only": "false"}),
        ]
        for method, url, payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(getattr(self.client, method)(url, json=payload).status_code, 400)
        for query in ("group_id=bad", "group_id=-1", "days=bad"):
            self.assertEqual(self.client.get(f"/api/watchlist/board?{query}").status_code, 400)
        self.assertEqual(store.get_stock(self.con, "600001"), before)
        self.assertEqual(len(store.list_stocks(self.con)), 2)

    def test_mutating_jobs_require_admin_before_any_work(self):
        self.allowed = False
        with patch.object(self.manager, "start") as start, patch.object(self.manager, "retry") as retry:
            self.assertEqual(self.client.post("/api/watchlist/jobs", json={"kind": "update-today"}).status_code, 403)
            self.assertEqual(self.client.post("/api/watchlist/jobs/old/retry").status_code, 403)
        start.assert_not_called()
        retry.assert_not_called()
        self.assertEqual(self.client.get("/api/watchlist/jobs/latest").json(), {"job": None})

    def test_job_api_returns_accepted_and_progress_without_waiting_for_provider(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def quote(code):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"code": code, "trade_date": "2026-09-14", "close_price": 12}

        with patch.object(service, "fetch_today_quote", side_effect=quote):
            response = self.client.post("/api/watchlist/jobs", json={"kind": "update-today"})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(entered.wait(1))
            job_id = response.json()["job"]["id"]
            status = self.client.get(f"/api/watchlist/jobs/{job_id}")
            self.assertEqual(status.json()["job"]["status"], "running")
            self.assertEqual(status.headers["Cache-Control"], "no-store")
            self.assertEqual(self.client.post("/api/watchlist/jobs", json={"kind": "backfill"}).status_code, 409)
            release.set()
            self.manager._thread.join(3)
        self.assertEqual(self.client.get("/api/watchlist/jobs/latest").json()["job"]["status"], "completed")
        self.assertEqual(self.client.get("/api/watchlist/jobs/missing").status_code, 404)
        self.assertEqual(self.client.head("/api/watchlist/jobs/latest").status_code, 200)
