from __future__ import annotations

import copy
import json
import math
import unittest
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from app.dashboard.routers.technical_analysis import create_technical_analysis_router
from app.market_data.technical_analysis import TechnicalMarketDataError
from app.strategies.technical_analysis import analyze_technical


MODULE_NAMES = {
    "trend",
    "volume_price",
    "patterns",
    "breakout",
    "canslim",
    "chanlun",
}
ACTION_NAMES = {"\u5f3a\u70c8\u4e70\u5165", "\u4e70\u5165", "\u8c28\u614e\u4e70\u5165", "\u89c2\u671b", "\u5356\u51fa"}


def synthetic_ohlcv(*, count: int = 180, direction: str = "up") -> list[dict[str, Any]]:
    """Build deterministic bars with an unambiguous long-term direction."""

    if direction not in {"up", "down"}:
        raise ValueError("unsupported synthetic direction")
    multiplier = 1.008 if direction == "up" else 0.992
    first_close = 30.0 if direction == "up" else 150.0
    previous_close: float | None = None
    rows: list[dict[str, Any]] = []
    for index in range(count):
        close = first_close * multiplier**index
        # A small deterministic wave prevents the fixture from being an
        # unrealistic perfectly straight series without obscuring its trend.
        close *= 1 + math.sin(index / 5) * 0.0015
        open_price = close * (0.997 if direction == "up" else 1.003)
        high = max(open_price, close) * 1.006
        low = min(open_price, close) * 0.994
        volume = 800_000 + index * 7_500
        pct = 0.0 if previous_close is None else (close / previous_close - 1) * 100
        rows.append(
            {
                "date": (date(2024, 1, 1) + timedelta(days=index)).isoformat(),
                "open": round(open_price, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "volume": volume,
                "amount": round(close * volume, 2),
                "turnover": round(1.5 + index / count, 4),
                "pct": round(pct, 4),
            }
        )
        previous_close = close
    return rows


def synthetic_flows(rows: list[dict[str, Any]], *, positive: bool) -> list[dict[str, Any]]:
    sign = 1 if positive else -1
    return [
        {
            "date": row["date"],
            "main_net": sign * (2_000_000 + index * 25_000),
            "main_pct": sign * (1.0 + index / 50),
        }
        for index, row in enumerate(rows[-30:])
    ]


class TechnicalAnalysisEngineTests(unittest.TestCase):
    def test_analysis_is_deterministic_json_safe_and_does_not_mutate_inputs(self) -> None:
        rows = synthetic_ohlcv()
        flows = synthetic_flows(rows, positive=True)
        quote = {
            "symbol": "sz000001",
            "name": "\u5408\u6210\u6837\u672c",
            "price": rows[-1]["close"],
            "turnover": 3.2,
        }
        original_rows = copy.deepcopy(rows)
        original_flows = copy.deepcopy(flows)
        original_quote = copy.deepcopy(quote)

        first = analyze_technical(rows, quote=quote, flows=flows, period="day")
        second = analyze_technical(rows, quote=quote, flows=flows, period="day")

        self.assertEqual(first, second)
        self.assertEqual(rows, original_rows)
        self.assertEqual(flows, original_flows)
        self.assertEqual(quote, original_quote)
        json.dumps(first, ensure_ascii=False, sort_keys=True, allow_nan=False)

    def test_analysis_exposes_each_module_and_bounded_summary(self) -> None:
        rows = synthetic_ohlcv()
        result = analyze_technical(
            rows,
            quote={"price": rows[-1]["close"], "name": "\u5408\u6210\u6837\u672c"},
            flows=synthetic_flows(rows, positive=True),
            index_rows=synthetic_ohlcv(direction="up"),
            period="day",
        )

        self.assertEqual(result["period"], "day")
        self.assertIn(result["action"], ACTION_NAMES)
        self.assertIsInstance(result["score"], int)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertIsInstance(result["confidence"], int)
        self.assertGreaterEqual(result["confidence"], 0)
        self.assertLessEqual(result["confidence"], 100)

        modules = result["modules"]
        self.assertEqual(set(modules), MODULE_NAMES)
        for name, module in modules.items():
            with self.subTest(module=name):
                self.assertIsInstance(module, dict)
                self.assertTrue(module, f"{name} should contain an explainable result")

        self.assertIsInstance(result["key_levels"], dict)
        self.assertIsInstance(result["trade_plan"], dict)
        self.assertIsInstance(result["data_requirements"], (dict, list))
        self.assertIsInstance(result["data_quality"], dict)

        risk = result["risk"]
        self.assertIsInstance(risk["level"], str)
        self.assertIsInstance(risk["score"], int)
        self.assertGreaterEqual(risk["score"], 0)
        self.assertLessEqual(risk["score"], 100)
        self.assertIsInstance(risk["warnings"], list)

        signals = result["signals"]
        self.assertIsInstance(signals["buy"], list)
        self.assertIsInstance(signals["sell"], list)
        self.assertIsInstance(signals["list"], list)

    def test_persistent_downtrend_produces_sell_action(self) -> None:
        rows = synthetic_ohlcv(direction="down")
        result = analyze_technical(
            rows,
            quote={"price": rows[-1]["close"], "name": "\u4e0b\u8dcc\u6837\u672c"},
            flows=synthetic_flows(rows, positive=False),
            index_rows=synthetic_ohlcv(direction="down"),
            period="day",
        )

        self.assertEqual(result["action"], "\u5356\u51fa")
        self.assertTrue(result["signals"]["sell"])

    def test_optional_enrichment_can_be_omitted(self) -> None:
        result = analyze_technical(synthetic_ohlcv(), period="week")

        self.assertEqual(result["period"], "week")
        self.assertIsInstance(result["data_requirements"], (dict, list))
        self.assertIsInstance(result["data_quality"], dict)

    def test_fewer_than_sixty_bars_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "technical analysis requires at least 60 rows",
        ):
            analyze_technical(synthetic_ohlcv(count=59))


class FakeTechnicalScanManager:
    job_id = "a" * 32

    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    def start(self, *, period: str, limit: int) -> dict[str, Any]:
        self.start_calls.append({"period": period, "limit": limit})
        return {
            "job_id": self.job_id,
            "status": "queued",
            "period": period,
            "limit": limit,
            "progress": 0.0,
        }

    def get(self, job_id: str) -> dict[str, Any] | None:
        self.get_calls.append(job_id)
        if job_id != self.job_id:
            return None
        return {
            "job_id": self.job_id,
            "status": "done",
            "period": "week",
            "progress": 100.0,
            "results": [{"symbol": "600519", "score": 72}],
        }


def technical_router_client(
    analyze_service: Any,
    scan_manager: FakeTechnicalScanManager,
) -> TestClient:
    async def allow_request(_request: Request) -> Response | None:
        return None

    def json_response(
        _request: Request,
        value: dict[str, Any],
        *,
        cache_control: str,
        status_code: int = 200,
    ) -> Response:
        return JSONResponse(
            value,
            status_code=status_code,
            headers={"Cache-Control": cache_control},
        )

    app = FastAPI()
    app.include_router(
        create_technical_analysis_router(
            enforce_api_limits=allow_request,
            json_response=json_response,
            analyze_service=analyze_service,
            scan_manager=scan_manager,
        )
    )
    return TestClient(app)


class TechnicalAnalysisRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyze_calls: list[tuple[str, str]] = []
        self.scan_manager = FakeTechnicalScanManager()

        def analyze_service(symbol: str, period: str) -> dict[str, Any]:
            self.analyze_calls.append((symbol, period))
            return {
                "symbol": symbol,
                "period": period,
                "signal": {
                    "action": "\u4e70\u5165",
                    "score": 70,
                    "modules": {name: {} for name in MODULE_NAMES},
                },
            }

        self.client = technical_router_client(analyze_service, self.scan_manager)

    def tearDown(self) -> None:
        self.client.close()

    def test_analyze_route_uses_injected_service_without_market_access(self) -> None:
        response = self.client.get(
            "/api/technical-analysis/analyze?symbol=600519&period=week"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["symbol"], "600519")
        self.assertEqual(response.json()["period"], "week")
        self.assertEqual(self.analyze_calls, [("600519", "week")])

    def test_analyze_route_validates_before_calling_service(self) -> None:
        invalid_symbol = self.client.get(
            "/api/technical-analysis/analyze?symbol=not-a-stock&period=day"
        )
        invalid_period = self.client.get(
            "/api/technical-analysis/analyze?symbol=600519&period=hour"
        )

        self.assertEqual(invalid_symbol.status_code, 400)
        self.assertEqual(invalid_symbol.json()["error"], "invalid_symbol")
        self.assertEqual(invalid_period.status_code, 400)
        self.assertEqual(invalid_period.json()["error"], "unsupported_period")
        self.assertEqual(self.analyze_calls, [])

    def test_analyze_head_is_network_free_and_has_no_body(self) -> None:
        response = self.client.head(
            "/api/technical-analysis/analyze?symbol=600519&period=day"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.analyze_calls, [])

    def test_market_data_failure_has_stable_unavailable_response(self) -> None:
        def unavailable(_symbol: str, period: str) -> dict[str, Any]:
            del period
            raise TechnicalMarketDataError("kline_unavailable")

        with technical_router_client(unavailable, self.scan_manager) as client:
            response = client.get(
                "/api/technical-analysis/analyze?symbol=600519&period=day"
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "kline_unavailable")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_scan_routes_use_injected_manager(self) -> None:
        started = self.client.post(
            "/api/technical-analysis/scans",
            json={"period": "week", "limit": 25},
        )
        status = self.client.get(
            f"/api/technical-analysis/scans/{self.scan_manager.job_id}"
        )

        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.headers["Cache-Control"], "no-store")
        self.assertEqual(started.json()["job_id"], self.scan_manager.job_id)
        self.assertEqual(
            self.scan_manager.start_calls,
            [{"period": "week", "limit": 25}],
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "done")
        self.assertEqual(status.json()["results"][0]["symbol"], "600519")
        self.assertEqual(self.scan_manager.get_calls, [self.scan_manager.job_id])

    def test_scan_status_rejects_unknown_or_malformed_ids(self) -> None:
        malformed = self.client.get("/api/technical-analysis/scans/not-a-job")
        unknown = self.client.get(f"/api/technical-analysis/scans/{'b' * 32}")

        self.assertEqual(malformed.status_code, 404)
        self.assertEqual(malformed.json()["error"], "scan_not_found")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"], "scan_not_found")


if __name__ == "__main__":
    unittest.main()
