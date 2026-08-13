from __future__ import annotations

import time
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from app.dashboard.technical_analysis_service import (
    SCORE_MODEL,
    STRATEGY_VERSION,
    TechnicalScanManager,
    analyze_symbol,
)
from app.market_data.technical_analysis import (
    load_technical_index_bundle,
    load_technical_index_data,
    load_technical_market_data,
    technical_index_key,
)


def rows(count: int = 180, *, start: date = date(2024, 1, 1)) -> list[dict]:
    result = []
    for index in range(count):
        close = 10.0 + index * 0.02
        result.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "open": close - 0.05,
            "close": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "volume": 1000 + index,
        })
    return result


class TechnicalIndexDataTests(unittest.TestCase):
    def test_growth_and_star_market_extended_prefixes_use_board_benchmarks(self) -> None:
        self.assertEqual(technical_index_key("301001"), "sz399006")
        self.assertEqual(technical_index_key("689009"), "sh000688")

    def test_board_specific_index_is_loaded_from_cache(self) -> None:
        calls = []

        def series_loader(symbols, **kwargs):
            calls.append((symbols, kwargs))
            return {"sz399006": rows(250)}

        result = load_technical_index_data(
            "300750",
            count=250,
            series_loader=series_loader,
            tencent_fetcher=lambda *_args: self.fail("cache should satisfy request"),
            eastmoney_fetcher=lambda *_args: self.fail("cache should satisfy request"),
        )

        self.assertEqual(result["name"], "创业板指")
        self.assertEqual(result["tencent_symbol"], "sz399006")
        self.assertEqual(len(result["klines"]), 250)
        self.assertEqual(result["data_quality"]["index_source"], "niuone_sqlite_cache")
        self.assertEqual(calls[0][0], ["sz399006"])

    def test_index_bundle_loads_each_board_only_once(self) -> None:
        loaded = []

        def index_loader(symbol, **_kwargs):
            loaded.append(technical_index_key(symbol))
            return {"klines": rows(120)}

        bundle = load_technical_index_bundle(
            ["600000", "600519", "000001", "300750", "688001"],
            index_loader=index_loader,
        )

        self.assertEqual(set(bundle), {"sh000001", "sz399001", "sz399006", "sh000688"})
        self.assertCountEqual(loaded, bundle)

    def test_weekly_market_loader_keeps_enough_history_for_analysis(self) -> None:
        history = rows(500, start=date(2025, 1, 1))
        with patch(
            "app.market_data.technical_analysis.load_kline_series_map",
            return_value={"sh600519": history},
        ), patch(
            "app.market_data.technical_analysis.fetch_tencent_daily_klines",
        ) as tencent_fetcher:
            market = load_technical_market_data(
                "600519",
                period="week",
                quote_fetcher=lambda _symbols: {"sh600519": {}},
                flow_fetcher=lambda *_args, **_kwargs: [],
                index_loader=lambda *_args, **_kwargs: {
                    "klines": rows(70),
                    "data_quality": {"index_source": "fixture"},
                },
            )

        tencent_fetcher.assert_not_called()
        self.assertGreaterEqual(len(market["klines"]), 60)
        self.assertEqual(len(market["index_klines"]), 70)
        self.assertTrue(market["data_quality"]["index_available"])

    def test_index_loader_degrades_without_discarding_usable_cache(self) -> None:
        cached = rows(120)
        result = load_technical_index_data(
            "600519",
            count=250,
            series_loader=lambda *_args, **_kwargs: {"sh000001": cached},
            tencent_fetcher=lambda *_args: (_ for _ in ()).throw(OSError("offline")),
            eastmoney_fetcher=lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        self.assertEqual(len(result["klines"]), 120)
        self.assertEqual(result["data_quality"]["index_source"], "niuone_sqlite_cache")

    def test_market_loader_degrades_optional_sources_and_cache_errors(self) -> None:
        history = rows(180)
        with patch(
            "app.market_data.technical_analysis.load_kline_series_map",
            side_effect=OSError("cache unavailable"),
        ), patch(
            "app.market_data.technical_analysis.fetch_tencent_daily_klines",
            return_value=history,
        ):
            market = load_technical_market_data(
                "600519",
                quote_fetcher=lambda *_args: (_ for _ in ()).throw(OSError("offline")),
                flow_fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
                index_loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
            )

        self.assertEqual(len(market["klines"]), 180)
        self.assertEqual(market["flows"], [])
        self.assertEqual(market["index_klines"], [])
        self.assertTrue(market["data_quality"]["degraded"])


class TechnicalCompositionTests(unittest.TestCase):
    def test_single_symbol_injects_index_and_declares_strategy_contract(self) -> None:
        index_rows = rows(120)
        captured = {}

        def market_loader(*_args, **_kwargs):
            return {
                "symbol": "600519",
                "period": "day",
                "quote": {"name": "贵州茅台", "price": 1500},
                "klines": rows(180),
                "flows": [],
                "index_klines": index_rows,
                "data_quality": {"index_available": True},
            }

        def analyzer(_rows, **kwargs):
            captured.update(kwargs)
            return {"action": "观望", "data_quality": {}}

        result = analyze_symbol(
            "600519", market_loader=market_loader, analyzer=analyzer,
        )

        self.assertIs(captured["index_rows"], index_rows)
        self.assertEqual(result["strategy_version"], STRATEGY_VERSION)
        self.assertEqual(result["score_model"], SCORE_MODEL)
        self.assertEqual(result["analysis_mode"], "realtime")
        self.assertEqual(result["signal"]["strategy_version"], STRATEGY_VERSION)

    def test_scan_loads_shared_index_once_and_marks_closed_data_contract(self) -> None:
        benchmark = rows(120)
        index_calls = []
        analyzer_indexes = []

        def index_bundle_loader(symbols, **kwargs):
            index_calls.append((list(symbols), kwargs))
            return {
                "sh000001": {"klines": benchmark},
                "sz399001": {"klines": benchmark},
            }

        def analyzer(_rows, **kwargs):
            analyzer_indexes.append(kwargs.get("index_rows"))
            return {
                "action": "买入",
                "score": 70,
                "confidence": 66,
                "risk_level": "中",
                "module_scores": {},
                "trade_plan": {},
            }

        manager = TechnicalScanManager(
            symbol_loader=lambda **_kwargs: ("sh600000", "sz000001"),
            series_loader=lambda *_args, **_kwargs: {
                "sh600000": rows(180), "sz000001": rows(180),
            },
            index_bundle_loader=index_bundle_loader,
            analyzer=analyzer,
            max_workers=1,
        )
        started = manager.start(period="day", limit=2)
        deadline = time.monotonic() + 2.0
        status = manager.get(started["job_id"])
        while status and status["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.01)
            status = manager.get(started["job_id"])

        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "done")
        self.assertEqual(len(index_calls), 1)
        self.assertEqual(analyzer_indexes, [benchmark, benchmark])
        self.assertEqual(status["analysis_mode"], "eod_scan")
        self.assertEqual(status["price_status"], "closed")
        self.assertEqual(status["strategy_version"], STRATEGY_VERSION)
        self.assertTrue(all(item["index_available"] for item in status["results"]))


if __name__ == "__main__":
    unittest.main()
