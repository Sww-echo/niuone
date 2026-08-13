from __future__ import annotations

import copy
import json
import unittest

from app.strategies.technical_analysis.minute_chanlun import (
    BAR_KIND,
    INPUT_KIND,
    MINIMUM_FIVE_MINUTE_BARS,
    OHLC_METHOD,
    analyze_minute_technical,
    build_five_minute_bars,
)


def minute_points(count: int = 60) -> tuple[list[str], list[float], list[float]]:
    times: list[str] = []
    prices: list[float] = []
    volumes: list[float] = []
    minute = 9 * 60 + 30
    for index in range(count):
        if minute > 11 * 60 + 30:
            minute = 13 * 60
        times.append(f"{minute // 60:02d}:{minute % 60:02d}")
        prices.append(round(10 + index * 0.01 + ((index % 18) - 9) * 0.03, 3))
        volumes.append(float(100 + index))
        minute += 1
    return times, prices, volumes


class TechnicalMinuteChanlunTests(unittest.TestCase):
    def test_0930_to_0934_builds_one_real_range_and_sums_volume(self) -> None:
        bars, quality = build_five_minute_bars(
            ["09:30", "09:31", "09:32", "09:33", "09:34"],
            [10.0, 10.4, 9.8, 10.2, 10.1],
            [10, 20, 30, 40, 50],
        )

        self.assertEqual(len(bars), 1)
        self.assertEqual(
            {key: bars[0][key] for key in ("start_time", "end_time", "open", "high", "low", "close", "volume", "source_point_count")},
            {
                "start_time": "09:30", "end_time": "09:34", "open": 10.0,
                "high": 10.4, "low": 9.8, "close": 10.1, "volume": 150.0,
                "source_point_count": 5,
            },
        )
        self.assertEqual(quality["source_interval"], "1m")
        self.assertEqual(quality["output_interval"], "5m")
        self.assertTrue(quality["aggregated_from_one_minute"])
        self.assertFalse(quality["real_ohlc_available"])
        self.assertEqual(quality["ohlc_method"], OHLC_METHOD)

    def test_gap_starts_a_new_group_and_tail_shorter_than_five_is_retained(self) -> None:
        bars, quality = build_five_minute_bars(
            ["11:28", "11:29", "11:30", "13:00", "13:01", "13:02"],
            [10.0, 10.1, 10.2, 10.5, 10.3, 10.4],
            [1, 2, 3, 4, 5, 6],
        )

        self.assertEqual(len(bars), 2)
        self.assertEqual([(bar["start_time"], bar["end_time"], bar["source_point_count"]) for bar in bars], [
            ("11:28", "11:30", 3), ("13:00", "13:02", 3),
        ])
        self.assertEqual([bar["volume"] for bar in bars], [6.0, 15.0])
        self.assertEqual(quality["discontinuity_count"], 1)
        self.assertEqual(quality["trailing_partial_point_count"], 3)

    def test_gap_after_a_complete_bar_is_still_reported(self) -> None:
        bars, quality = build_five_minute_bars(
            ["09:30", "09:31", "09:32", "09:33", "09:34", "13:00"],
            [10.0, 10.1, 10.2, 10.3, 10.4, 10.5],
            [1, 2, 3, 4, 5, 6],
        )

        self.assertEqual([
            (bar["start_time"], bar["end_time"], bar["source_point_count"])
            for bar in bars
        ], [("09:30", "09:34", 5), ("13:00", "13:00", 1)])
        self.assertEqual(quality["discontinuity_count"], 1)
        self.assertEqual(quality["trailing_partial_point_count"], 1)

    def test_contract_passes_one_bar_per_five_points_to_structure_engine(self) -> None:
        times, prices, volumes = minute_points(MINIMUM_FIVE_MINUTE_BARS * 5)
        captured: list[list[dict[str, object]]] = []

        def fake_structure(rows: object) -> dict[str, object]:
            captured.append(copy.deepcopy(list(rows)))
            return {
                "counts": {"klines": len(captured[-1]), "fractals": 0, "strokes": 0, "zhongshus": 0, "signals": 0},
                "fractals": [], "strokes": [], "zhongshus": [], "signals": [],
                "current_state": "笔形成中", "summary": "无信号", "description": "结构形成中",
            }

        result = analyze_minute_technical(times, prices, volumes, structure_analyzer=fake_structure)

        self.assertTrue(result["available"])
        self.assertEqual(result["input_kind"], INPUT_KIND)
        self.assertEqual(result["bar_kind"], BAR_KIND)
        self.assertEqual(result["source_interval"], "1m")
        self.assertEqual(result["output_interval"], "5m")
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(captured[0]), MINIMUM_FIVE_MINUTE_BARS)
        self.assertEqual(result["derived_bar_count"], MINIMUM_FIVE_MINUTE_BARS)
        self.assertIn("并非上游原生5分钟 OHLC", result["ohlc_note"])

    def test_full_pipeline_is_json_safe_deterministic_and_discloses_quality(self) -> None:
        times, prices, volumes = minute_points(75)
        original = copy.deepcopy((times, prices, volumes))

        first = analyze_minute_technical(times, prices, volumes)
        second = analyze_minute_technical(times, prices, volumes)

        self.assertEqual(first, second)
        self.assertEqual((times, prices, volumes), original)
        self.assertTrue(first["available"])
        self.assertTrue(first["data_quality"]["degraded"])
        self.assertIn("派生结构", first["summary"])
        self.assertIn("1分钟代表价聚合", first["description"])
        self.assertIsInstance(first["fractals"], list)
        self.assertIsInstance(first["strokes"], list)
        self.assertIsInstance(first["zhongshus"], list)
        self.assertIsInstance(first["signals"], list)
        json.dumps(first, ensure_ascii=False, allow_nan=False)

    def test_insufficient_result_keeps_quality_and_empty_structure_contract(self) -> None:
        result = analyze_minute_technical(
            ["09:30", "bad", "09:32", "09:33"],
            [10.0, 10.2, float("nan"), 0],
            [100.0, -1, 300.0],
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "minute_insufficient")
        self.assertEqual(result["point_count"], 1)
        self.assertEqual(result["data_quality"]["invalid_time_count"], 1)
        self.assertEqual(result["data_quality"]["invalid_price_count"], 2)
        self.assertEqual(result["signals"], [])
        self.assertIn("5分钟派生 bar 不足", result["summary"])

    def test_nine_source_points_return_rich_insufficient_counts(self) -> None:
        times, prices, volumes = minute_points(9)

        result = analyze_minute_technical(times, prices, volumes)

        self.assertFalse(result["available"])
        self.assertEqual(result["point_count"], 9)
        self.assertEqual(result["derived_bar_count"], 2)
        self.assertIn("至少需要10根", result["summary"])


if __name__ == "__main__":
    unittest.main()
