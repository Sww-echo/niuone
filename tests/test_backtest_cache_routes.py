from __future__ import annotations

import unittest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.backtesting.tasks import BacktestTaskError
from app.dashboard.routers.backtesting import create_backtesting_router


class _Access:
    async def session_valid(self, _request: Request) -> bool:
        return True

    async def require_action(self, request: Request):
        if request.headers.get("X-NiuOne-Action") == "1":
            return None
        return JSONResponse({"error": "action_required"}, status_code=403)


class _Manager:
    def __init__(self) -> None:
        self.active = False

    def cache_usage(self):
        return {
            "available": True,
            "entry_count": 2,
            "file_count": 2,
            "temporary_file_count": 0,
            "byte_count": 4096,
        }

    def clear_cache(self):
        if self.active:
            raise BacktestTaskError("回测执行期间不能清理缓存")
        return {
            "available": True,
            "removed_file_count": 2,
            "removed_byte_count": 4096,
            "entry_count": 0,
            "file_count": 0,
            "temporary_file_count": 0,
            "byte_count": 0,
        }


def _json_response(
    _request: Request,
    payload,
    *,
    cache_control: str,
    status_code: int = 200,
):
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": cache_control},
    )


class BacktestCacheRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = _Manager()
        app = FastAPI()
        app.include_router(create_backtesting_router(
            access=_Access(),
            rate_limit=lambda _request: None,
            json_response=_json_response,
            manager=self.manager,
        ))
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def test_cache_status_and_clear_are_protected_admin_routes(self):
        status = self.client.get("/api/admin/backtests/cache")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["entry_count"], 2)
        self.assertEqual(status.headers["cache-control"], "no-store")

        rejected = self.client.post("/api/admin/backtests/cache/clear")
        self.assertEqual(rejected.status_code, 403)

        cleared = self.client.post(
            "/api/admin/backtests/cache/clear",
            headers={"X-NiuOne-Action": "1"},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["removed_file_count"], 2)

    def test_cache_clear_conflict_is_reported_while_backtest_is_active(self):
        self.manager.active = True
        response = self.client.post(
            "/api/admin/backtests/cache/clear",
            headers={"X-NiuOne-Action": "1"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("不能清理缓存", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
