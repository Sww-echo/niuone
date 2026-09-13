from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "compat"))

from app.trading.probe_chase import (
    PROBE_CHASE_VERSION, build_probe_chase_outcomes, collect_probe_chase_observations,
    load_probe_chase_outcomes, make_probe_chase_observation, probe_chase_protocol, summarize_probe_chase,
)


def observation(price=10.3, stamp="2026-09-08 10:00:00"):
    return make_probe_chase_observation("600000", {"price":price,"prev_close":10,"turnover":4},
                                       {"industry":"测试题材"}, observed_at=stamp)


def decision(*rows):
    return {"_forward_payload_available":True, "decision":{"actions":[],"probe_chase_observations":list(rows)}}


def series():
    dates = ("2026-09-07","2026-09-08","2026-09-09","2026-09-10","2026-09-11","2026-09-14","2026-09-15","2026-09-16")
    rows = [{"date":day,"close":10.0,"volume":10000} for day in dates]
    return {"sh000001":[dict(row) for row in rows],"sh600000":[dict(row) for row in rows]}


class ProbeChaseForwardTests(unittest.TestCase):
    def test_preregistered_boundary_and_first_observation_cannot_be_reassigned(self):
        self.assertIsNone(observation(stamp="2026-09-07 10:00:00"))
        first=observation(10.3)
        later=observation(10.2, "2026-09-08 10:30:00")
        self.assertFalse(first["filtered_accepts"])
        self.assertTrue(later["filtered_accepts"])
        rows=collect_probe_chase_observations([decision(later),decision(first),decision(first)],as_of="2026-09-08")
        self.assertEqual(rows,[first])
        self.assertEqual(collect_probe_chase_observations([decision(first)],as_of="2026-09-07"),[])
        self.assertEqual(collect_probe_chase_observations([{**decision(first),"_forward_payload_available":False}],as_of="2026-09-08"),[])
        self.assertEqual(probe_chase_protocol()["horizon_sessions"],5)
        self.assertEqual(probe_chase_protocol()["minimum_mature_samples_each_price_group"],30)

    def test_fixed_horizon_missing_days_future_data_and_costs(self):
        obs=observation(10.0)
        data=series()
        pending=build_probe_chase_outcomes([obs],data,as_of="2026-09-14")[0]
        self.assertFalse(pending["completed"])
        complete=build_probe_chase_outcomes([obs],data,as_of="2026-09-15")[0]
        self.assertTrue(complete["completed"])
        self.assertEqual(complete["exit_date"],"2026-09-15")
        self.assertLess(complete["net_return_pct"],0)  # flat price still pays both-side costs/slippage
        data["sh600000"]=[row for row in data["sh600000"] if row["date"]!="2026-09-10"]
        gap=build_probe_chase_outcomes([obs],data,as_of="2026-09-16")[0]
        self.assertFalse(gap["completed"])
        self.assertEqual(gap["status"],"stock_bar_gap")

    def test_adjusted_price_scale_cancels_and_baseline_keeps_blocked_samples(self):
        obs=observation(10.3)
        data=series()
        original=build_probe_chase_outcomes([obs],data,as_of="2026-09-15")[0]
        for row in data["sh600000"]: row["close"]*=0.5
        scaled=build_probe_chase_outcomes([obs],data,as_of="2026-09-15")[0]
        self.assertAlmostEqual(original["net_return_pct"],scaled["net_return_pct"])
        result=summarize_probe_chase([obs],[scaled],as_of="2026-09-15")
        self.assertEqual(result["baseline"]["completed_trade_count"],1)
        self.assertEqual(result["filtered"]["completed_trade_count"],0)
        self.assertIsNone(result["filtered"]["win_rate_pct"])
        self.assertGreater(result["paired_mean_return_delta_pct"],0)
        self.assertFalse(result["causal_or_live_performance_claim_supported"])
        self.assertEqual(result["status"],"collecting")

    def test_missing_market_session_never_extends_the_exit_horizon(self):
        data = series()
        data["sh000001"] = [row for row in data["sh000001"] if row["date"] != "2026-09-10"]
        result = build_probe_chase_outcomes([observation()], data, as_of="2026-09-16")[0]
        self.assertFalse(result["completed"])
        self.assertEqual(result["status"], "market_calendar_or_bar_gap")
        data = series()
        data["sh600000"][-2]["volume"] = 0
        result = build_probe_chase_outcomes([observation()], data, as_of="2026-09-16")[0]
        self.assertFalse(result["completed"])
        self.assertEqual(result["status"], "terminal_not_tradable")

    def test_completed_outcome_storage_is_immutable_and_pending_never_overwrites(self):
        with tempfile.TemporaryDirectory(prefix="niuone-probe-chase-") as directory:
            path=Path(directory)/"practice.db"
            with patch.dict(os.environ,{"DASHBOARD_HOME":directory,"DASHBOARD_NIUNIU_DB":str(path)}):
                storage=importlib.import_module("app.storage.practice_db")
            with patch.object(storage,"DB_PATH",path):
                storage.init_db()
                completed=build_probe_chase_outcomes([observation()],series(),as_of="2026-09-15")[0]
                self.assertEqual(storage.record_probe_chase_outcomes([completed]),1)
                self.assertEqual(storage.record_probe_chase_outcomes([{**completed,"net_return_pct":999}]),0)
                self.assertEqual(storage.record_probe_chase_outcomes([{**completed,"completed":False}]),0)
                self.assertEqual(load_probe_chase_outcomes(path),[completed])
