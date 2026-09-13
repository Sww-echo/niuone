#!/usr/bin/env python3
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.dashboard.market_breadth_recovery import (
    add_stock_to_aggregates,
    aggregate_recovered_minutes,
    enrich_turnover,
    fetch_turnover_series,
    load_recovery_checkpoint,
    merge_recovered_history,
    parse_stock_minute_bars,
    persist_recovered_history,
    plan_market_breadth_recovery,
    save_recovery_checkpoint,
    validation_is_safe,
    validation_summary,
)


def quote(*, prev=10, upper=11, lower=9, volume=100):
    return {
        "prev_close": prev,
        "upper_limit": upper,
        "lower_limit": lower,
        "volume": volume,
    }


def sample(time_text, *, red=2, green=1, flat=1, broken=0, actual=100):
    return {
        "generated_at": time_text,
        "quote_count": red + green + flat,
        "limit_price_count": red + green + flat,
        "turnover_amount_count": red + green + flat,
        "red": red,
        "green": green,
        "flat": flat,
        "limit_up": 1,
        "limit_down": 1,
        "broken_limit": broken,
        "actual_turnover_yi": actual,
    }


class MarketBreadthRecoveryTests(unittest.TestCase):
    def test_recovery_plan_waits_for_three_real_minutes_then_becomes_ready(self):
        waiting = plan_market_breadth_recovery(
            "2026-08-10",
            [
                sample("2026-08-10 10:43:07"),
                sample("2026-08-10 10:43:37"),
                sample("2026-08-10 10:44:07"),
            ],
        )
        self.assertEqual(waiting["status"], "waiting_validation")

        ready = plan_market_breadth_recovery(
            "2026-08-10",
            [
                sample("2026-08-10 10:43:07"),
                sample("2026-08-10 10:44:07"),
                sample("2026-08-10 10:45:07"),
            ],
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(
            ready["backfill_targets"][0],
            datetime(2026, 8, 10, 9, 31),
        )
        self.assertEqual(
            ready["backfill_targets"][-1],
            datetime(2026, 8, 10, 10, 42),
        )
        self.assertEqual(len(ready["validation_targets"]), 3)

    def test_recovery_plan_fills_internal_downtime_gap(self):
        plan = plan_market_breadth_recovery(
            "2026-08-10",
            [
                sample("2026-08-10 09:31:00"),
                sample("2026-08-10 09:32:00"),
                sample("2026-08-10 10:00:00"),
                sample("2026-08-10 10:01:00"),
                sample("2026-08-10 10:02:00"),
            ],
        )

        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["backfill_targets"][0], datetime(2026, 8, 10, 9, 33))
        self.assertEqual(plan["backfill_targets"][-1], datetime(2026, 8, 10, 9, 59))
        self.assertEqual(len(plan["validation_targets"]), 3)

    def test_recovery_plan_is_idempotently_complete_without_minute_gaps(self):
        plan = plan_market_breadth_recovery(
            "2026-08-10",
            [
                sample("2026-08-10 09:31:00"),
                sample("2026-08-10 09:32:00"),
                sample("2026-08-10 09:33:00"),
            ],
        )

        self.assertEqual(plan["status"], "complete")
        self.assertEqual(plan["backfill_targets"], [])

    def test_recovery_plan_can_fill_a_terminal_gap_after_close(self):
        plan = plan_market_breadth_recovery(
            "2026-08-10",
            [
                sample("2026-08-10 09:31:00"),
                sample("2026-08-10 09:32:00"),
                sample("2026-08-10 09:33:00"),
            ],
            expected_through=datetime(2026, 8, 10, 15, 0),
            allow_pre_gap_validation=True,
        )

        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["backfill_targets"][0], datetime(2026, 8, 10, 9, 34))
        self.assertEqual(plan["backfill_targets"][-1], datetime(2026, 8, 10, 15, 0))
        self.assertEqual(len(plan["validation_targets"]), 3)

    def test_parse_and_aggregate_reconstructs_broken_limit(self):
        body = json.dumps({
            "data": {
                "sh600001": {"m1": [
                    ["202608100931", "10.00", "11.00", "11.00", "10.00", "100", {}, "0.1"],
                    ["202608100932", "11.00", "10.80", "11.00", "10.70", "100", {}, "0.1"],
                ]},
            }
        })
        bars = parse_stock_minute_bars(body, "sh600001", "2026-08-10")
        targets = [datetime(2026, 8, 10, 9, 31), datetime(2026, 8, 10, 9, 32)]
        rebuilt = aggregate_recovered_minutes(
            {
                "sh600001": quote(),
                "sz000001": quote(prev=10, upper=11, lower=9),
            },
            {
                "sh600001": bars,
                "sz000001": [{
                    "minute": 9 * 60 + 31,
                    "close": 9,
                    "high": 10,
                    "low": 9,
                    "amount_yuan": 500,
                    "generated_at": "2026-08-10 09:31:00",
                }],
            },
            targets,
        )
        self.assertEqual(rebuilt[0]["limit_up"], 1)
        self.assertEqual(rebuilt[0]["limit_down"], 1)
        self.assertEqual(rebuilt[0]["broken_limit"], 0)
        self.assertEqual(rebuilt[1]["broken_limit"], 1)
        self.assertEqual(rebuilt[1]["red"], 1)
        self.assertEqual(rebuilt[1]["green"], 1)

    def test_enrich_turnover_validates_recovered_sample(self):
        raw = sample("2026-08-10 09:31:00", actual=0)
        raw.pop("actual_turnover_yi")
        result = enrich_turnover(
            [raw],
            {1.0: 12_345_000_000},
        )
        self.assertEqual(result[0]["actual_turnover_yi"], 123.45)
        self.assertIn("turnover_actual_source", result[0])

    def test_fetch_turnover_series_sums_cumulative_index_amounts(self):
        bodies = {
            "sh000001": json.dumps({"data": {"sh000001": {"data": {"data": [
                "0930 4000 10 100000000",
                "0931 4001 20 300000000",
            ]}}}}),
            "sz399001": json.dumps({"data": {"sz399001": {"data": {"data": [
                "0930 14000 10 200000000",
                "0931 14001 20 500000000",
            ]}}}}),
        }
        series = fetch_turnover_series(
            "2026-08-10",
            downloader=lambda symbol, _day: bodies[symbol],
        )
        self.assertEqual(series[0.0], 300_000_000)
        self.assertEqual(series[1.0], 800_000_000)

    def test_merge_preserves_existing_sample_on_timestamp_collision(self):
        recovered = sample("2026-08-10 09:31:00", red=2)
        existing = sample("2026-08-10 09:31:00", red=3, green=0)
        merged = merge_recovered_history(
            "2026-08-10",
            [recovered],
            {"samples": [existing, sample("2026-08-10 09:32:07")]},
        )
        self.assertEqual(len(merged["samples"]), 2)
        self.assertEqual(merged["samples"][0]["red"], 3)

    def test_validation_requires_three_close_matching_points(self):
        rebuilt = [
            sample(f"2026-08-10 10:0{minute}:00", actual=100 + minute)
            for minute in range(3)
        ]
        existing = [dict(item, generated_at=item["generated_at"][:-2] + "07") for item in rebuilt]
        summary = validation_summary(rebuilt, existing)
        self.assertTrue(validation_is_safe(summary, 4))
        summary["max_broken_limit_difference"] = 11
        self.assertFalse(validation_is_safe(summary, 4))

    def test_persist_backs_up_and_writes_both_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "market_breadth_history.json"
            recovery = root / "market_breadth_history.recovery.json"
            original = {
                "schema_version": 5,
                "date": "2026-08-10",
                "interval_seconds": 30,
                "samples": [sample("2026-08-10 10:00:07")],
            }
            history.write_text(json.dumps(original), encoding="utf-8")
            recovery.write_text(json.dumps(original), encoding="utf-8")
            merged, backup = persist_recovered_history(
                history,
                [sample("2026-08-10 09:31:00")],
                "2026-08-10",
                backup_root=root / "backups",
            )
            self.assertEqual(len(merged["samples"]), 2)
            self.assertTrue((backup / history.name).exists())
            self.assertTrue((backup / recovery.name).exists())
            persisted = json.loads(history.read_text(encoding="utf-8"))
            recovered = json.loads(recovery.read_text(encoding="utf-8"))
            self.assertEqual(persisted, recovered)

    def test_recovery_checkpoint_round_trips_aggregates(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            targets = [datetime(2026, 8, 10, 9, 31)]
            aggregates = [{"generated_at": "2026-08-10 09:31:00", "red": 2}]
            save_recovery_checkpoint(
                path,
                fingerprint="abc",
                targets=targets,
                aggregates=aggregates,
                verified_symbols={"sh600001"},
            )
            loaded, symbols = load_recovery_checkpoint(
                path,
                fingerprint="abc",
                targets=targets,
            )
            self.assertEqual(loaded, aggregates)
            self.assertEqual(symbols, {"sh600001"})


if __name__ == "__main__":
    unittest.main()
