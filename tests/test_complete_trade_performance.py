from __future__ import annotations

import unittest

from app.strategies.performance import track_strategy_performance
from app.trading.lifecycles import reconstruct_trade_lifecycles
from app.trading.niuone_forward import evaluate_niuone_forward


def fill(day, action, shares, amount, before, after, *, code="600000", fee=2, strategy="niu_reversal_probe", **extra):
    return {"time": f"2026-09-{day:02d} 10:00:00", "action": action,
            "code": code, "shares": shares, "amount": amount, "fee": fee,
            "price": amount / shares, "position_before_qty": before,
            "position_after_qty": after, "buy_strategy": strategy, **extra}


class CompleteTradePerformanceTests(unittest.TestCase):
    def test_adds_and_partial_exits_are_one_net_loss_attributed_to_first_entry(self):
        rows = [fill(8, "BUY", 100, 1000, 0, 100),
                fill(9, "BUY", 100, 1100, 100, 200, strategy="niu_leader"),
                fill(10, "SELL", 100, 1200, 200, 100, pnl=146),
                fill(11, "SELL", 100, 800, 100, 0, pnl=-254)]
        state = {"trade_log": rows, "positions": {}}
        perf = track_strategy_performance(state)
        report = evaluate_niuone_forward(rows, cohort_start="2026-09-08", as_of="2026-09-11")
        self.assertEqual(perf["summary"]["closed_trades"], 1)
        self.assertEqual(perf["summary"]["win_rate"], 0)
        self.assertEqual(perf["summary"]["total_pnl"], -108)
        self.assertEqual(perf["summary"]["sell_fill_count"], 2)
        self.assertEqual(perf["summary"]["win_rate"], report["overall"]["win_rate_pct"])
        self.assertEqual(perf["summary"]["total_pnl"], report["overall"]["realized_pnl"])
        self.assertEqual(perf["buy_strategy"]["niu_reversal_probe"]["trades"], 1)
        self.assertNotIn("niu_leader", perf["buy_strategy"])
        self.assertTrue(all("win_rate" not in row for row in perf["exit_rule"].values()))
        self.assertEqual(state["trade_log"], rows)

    def test_open_cycles_and_orphan_sells_do_not_become_completed_wins(self):
        rows = [fill(8, "BUY", 200, 2000, 0, 200),
                fill(9, "SELL", 100, 1200, 200, 100, pnl=197),
                fill(10, "SELL", 100, 1500, 100, 0, code="600001", pnl=500)]
        perf = track_strategy_performance({"trade_log": rows, "positions": {}})
        self.assertEqual(perf["summary"]["closed_trades"], 0)
        self.assertIsNone(perf["summary"]["win_rate"])
        self.assertEqual(perf["coverage"]["orphan_sell_count"], 1)

    def test_reopening_breakeven_duplicates_and_archived_history(self):
        rows = [fill(8, "BUY", 100, 1000, 0, 100),
                fill(9, "SELL", 100, 1100, 100, 0, pnl=96),
                fill(10, "BUY", 100, 1000, 0, 100),
                fill(11, "SELL", 100, 1004, 100, 0, pnl=0)]
        perf = track_strategy_performance({"trade_log": rows[-1:], "positions": {}}, trade_rows=rows+[rows[-1]])
        self.assertEqual(perf["summary"]["closed_trades"], 2)
        self.assertEqual(perf["summary"]["wins"], 1)
        self.assertEqual(perf["summary"]["flats"], 1)
        self.assertEqual(perf["summary"]["win_rate"], 50)
        self.assertEqual(perf["coverage"]["duplicate_trade_count"], 1)

    def test_component_fees_and_inconsistent_quantities_fail_closed(self):
        rows = [fill(8, "BUY", 100, 1000, 0, 100, fee=None, commission=2),
                fill(9, "SELL", 100, 1003, 100, 0, fee=None, commission=2)]
        cycles = reconstruct_trade_lifecycles(rows)
        self.assertEqual(cycles["completed"][0]["realized_pnl"], -1)
        rows[-1]["position_before_qty"] = 200
        self.assertEqual(reconstruct_trade_lifecycles(rows)["completed"], [])

    def test_cent_breakeven_and_rejected_revisions_do_not_create_wins(self):
        rows = [fill(8, "BUY", 100, 1000.1, 0, 100, fee=0),
                fill(9, "SELL", 50, 500.05, 100, 50, fee=0),
                fill(10, "SELL", 50, 500.05, 50, 0, fee=0)]
        rejected = {**rows[-1], "accounting_rejected": True, "accounting_status": "rejected"}
        perf = track_strategy_performance({"trade_log": rows + [rejected]})
        self.assertEqual(perf["summary"]["flats"], 1)
        self.assertEqual(perf["summary"]["wins"], 0)
        self.assertEqual(perf["coverage"]["inactive_accounting_trade_count"], 1)
