from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.trading.realized_returns import annotate_realized_returns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app/compat"))
_temporary_home = tempfile.TemporaryDirectory(prefix="niuone-realized-")
os.environ.setdefault("DASHBOARD_HOME", _temporary_home.name)


def fill(day, action, shares, price, before, after, **extra):
    return {
        "time": f"2026-09-{day:02d} 10:00:00", "code": "600000",
        "action": action, "shares": shares, "price": price,
        "amount": shares * price, "fee": 0,
        "position_before_qty": before, "position_after_qty": after, **extra,
    }


class RealizedReturnTests(unittest.TestCase):
    def test_partial_sells_use_total_buy_cost_and_accumulate_across_dates(self):
        rows = [fill(1, "BUY", 1000, 10, 0, 1000),
                fill(2, "SELL", 500, 11, 1000, 500, pnl=500),
                fill(3, "SELL", 500, 12, 500, 0, pnl=1000)]
        original = copy.deepcopy(rows)
        result = annotate_realized_returns(rows)
        self.assertEqual(result[1]["cumulative_realized_pnl"], 500)
        self.assertEqual(result[1]["realized_return_pct"], 5)
        self.assertEqual(result[2]["cumulative_realized_pnl"], 1500)
        self.assertEqual(result[2]["realized_return_pct"], 15)
        self.assertEqual(result[2]["pnl"], 1000)
        self.assertEqual(rows, original)
        self.assertEqual(annotate_realized_returns(result + [result[-1]]), result)

    def test_adds_fees_and_future_buys_do_not_change_earlier_returns(self):
        rows = [fill(1, "BUY", 1000, 10, 0, 1000, fee=1),
                fill(2, "SELL", 500, 11, 1000, 500, fee=3, pnl=496.5),
                fill(3, "BUY", 500, 12, 500, 1000, fee=1),
                fill(4, "SELL", 1000, 13, 1000, 0, fee=8, pnl=1990.5)]
        result = annotate_realized_returns(rows)
        self.assertEqual(result[1]["realized_buy_cost"], 10001)
        self.assertEqual(result[1]["realized_return_pct"], 4.96)
        self.assertEqual(result[-1]["realized_buy_cost"], 16002)
        self.assertEqual(result[-1]["cumulative_realized_pnl"], 2487)
        self.assertEqual(result[-1]["realized_return_pct"], 15.54)

    def test_reopening_resets_cycle_and_keeps_zero_and_negative_returns(self):
        rows = [fill(1, "BUY", 100, 10, 0, 100),
                fill(2, "SELL", 100, 11, 100, 0, pnl=100),
                fill(3, "BUY", 200, 10, 0, 200),
                fill(4, "SELL", 100, 10, 200, 100, pnl=0),
                fill(5, "SELL", 100, 9, 100, 0, pnl=-100)]
        result = annotate_realized_returns(rows)
        self.assertEqual(result[3]["realized_return_pct"], 0)
        self.assertEqual(result[4]["cumulative_realized_pnl"], -100)
        self.assertEqual(result[4]["realized_return_pct"], -5)
        self.assertNotEqual(result[1]["realized_cycle_key"], result[3]["realized_cycle_key"])

    def test_missing_opening_quantity_gap_and_cycle_mismatch_are_unavailable(self):
        buy = fill(1, "BUY", 1000, 10, 0, 1000, position_lifecycle_id="one")
        sell = fill(2, "SELL", 500, 11, 1000, 500, pnl=500)
        for rows in ([sell], [{**buy, "position_before_qty": None}, sell],
                     [buy, {**sell, "position_before_qty": 800}],
                     [buy, {**sell, "position_lifecycle_id": "two"}],
                     [buy, {**sell, "shares": 1500}],
                     [buy, {**sell, "time": "invalid"}]):
            with self.subTest(rows=rows):
                result = annotate_realized_returns(rows)[-1]
                self.assertIsNone(result["cumulative_realized_pnl"])
                self.assertIsNone(result["realized_return_pct"])

    def test_rejected_duplicate_does_not_add_profit(self):
        buy = fill(1, "BUY", 1000, 10, 0, 1000)
        rejected = fill(2, "SELL", 500, 11, 1000, 500, pnl=500)
        sell = fill(3, "SELL", 500, 12, 1000, 500, pnl=1000)
        result = annotate_realized_returns([
            buy, rejected, sell, {**rejected, "accounting_status": "rejected"},
        ])
        self.assertEqual(result[-1]["cumulative_realized_pnl"], 1000)
        self.assertEqual(result[-1]["realized_return_pct"], 10)

    def test_legacy_pnl_uses_verified_cost_chain(self):
        result = annotate_realized_returns([
            fill(1, "BUY", 1000, 10, 0, 1000, fee=1),
            fill(2, "SELL", 500, 11, 1000, 500, fee=3),
        ])
        self.assertEqual(result[-1]["cumulative_realized_pnl"], 496.5)

    def test_today_card_counts_each_cycle_once_including_same_day_reopening(self):
        import niuniu_practice_trader as trader

        rows = [fill(1, "BUY", 1000, 10, 0, 1000),
                fill(2, "SELL", 500, 11, 1000, 500, pnl=500),
                fill(3, "SELL", 200, 12, 500, 300, pnl=400),
                fill(3, "SELL", 300, 12, 300, 0, pnl=600,
                     time="2026-09-03 11:00:00"),
                fill(3, "BUY", 100, 10, 0, 100, time="2026-09-03 12:00:00"),
                fill(3, "SELL", 100, 9, 100, 0, pnl=-100,
                     time="2026-09-03 13:00:00")]
        state = {"trade_log": rows + [rows[-1]]}
        result = trader.build_today_sold_stocks(state, today="2026-09-03", quote_map={})[0]
        self.assertEqual(result["shares"], 600)
        self.assertEqual(result["realized_pnl"], 1400)
        self.assertEqual(result["realized_pnl_pct"], 12.73)

    def test_display_recovers_archived_opening_and_notification_uses_same_values(self):
        import niuniu_practice_trader as trader
        import niuniu_db as practice_db
        from app.messaging.trades import _trade_notification

        rows = [fill(1, "BUY", 1000, 10, 0, 1000),
                fill(2, "SELL", 500, 11, 1000, 500, pnl=500),
                fill(3, "SELL", 500, 12, 500, 0, pnl=1000)]
        with tempfile.TemporaryDirectory(prefix="niuone-realized-") as directory:
            with patch.object(practice_db, "DB_PATH", Path(directory) / "trades.db"):
                practice_db.init_db()
                for row in rows:
                    self.assertTrue(practice_db.record_trade(row))
                history = trader._realized_display_trade_history(rows[-1:])
                sold = trader.build_today_sold_stocks(
                    {"trade_log": rows[-1:]}, today="2026-09-03",
                    quote_map={}, trade_rows=history,
                )[0]
                self.assertEqual(sold["realized_pnl"], 1500)
                self.assertEqual(sold["realized_pnl_pct"], 15)
                self.assertEqual(sold["shares"], 500)
                notification = _trade_notification(history[-1:])
                self.assertIn("已实现盈亏 / 收益率：+¥1,500.00（+15.00%）", notification.plain_text())
                self.assertIn("已实现盈亏 / 收益率", str(notification.card_sections))

            with patch.object(practice_db, "DB_PATH", Path(directory) / "missing.db"):
                fallback = trader._realized_display_trade_history(rows[-1:])[-1]
                self.assertIsNone(fallback["realized_return_pct"])
                self.assertEqual(fallback["realized_return_status"], "history_unavailable:FileNotFoundError")
                self.assertIn("暂不可用", _trade_notification([fallback]).plain_text())
                self.assertEqual(trader._realized_display_trade_history(rows)[-1]["realized_return_pct"], 15)

    def test_public_markers_and_operation_logs_use_cumulative_fields(self):
        from app.dashboard.practice_payload import compact_trade_markers
        from app.dashboard.public_projection import build_public_sections

        sell = {**fill(3, "SELL", 500, 12, 500, 0, pnl=1000),
                "cumulative_realized_pnl": 1500, "realized_return_pct": 15,
                "realized_return_status": "verified", "realized_cycle_key": "private-key"}
        markers = compact_trade_markers([sell])
        projected = build_public_sections({"trade_log": [sell], "trade_markers": markers})
        self.assertEqual(projected["activity"]["trades"][0]["realized_return_pct"], 15)
        self.assertNotIn("realized_cycle_key", projected["activity"]["trades"][0])
        self.assertEqual(projected["history"]["trade_markers"][0]["cumulative_realized_pnl"], 1500)
        root = Path(__file__).resolve().parents[1]
        script = f"""
import {{ normalizePracticeTradeMarkers }} from {json.dumps((root / 'web/src/utils/practiceChart.js').as_uri())};
import {{ normalizePracticeOperationLogs }} from {json.dumps((root / 'web/src/utils/practiceLogs.js').as_uri())};
const row = {json.dumps(sell)};
const unknown = {{ ...row, cumulative_realized_pnl: null, realized_return_pct: null }};
console.log(JSON.stringify({{
  marker: normalizePracticeTradeMarkers({{ trade_log: [row] }})[0],
  log: normalizePracticeOperationLogs({{ generated_at: row.time, trade_log: [row] }})[0],
  unknown: normalizePracticeOperationLogs({{ generated_at: row.time, trade_log: [unknown] }})[0],
}}));
"""
        output = json.loads(subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True,
            capture_output=True, text=True, timeout=20,
        ).stdout)
        self.assertEqual(output["marker"]["pnl"], 1500)
        self.assertEqual(output["marker"]["pnlPct"], 15)
        self.assertIn("已实现盈亏", output["log"]["detail"])
        self.assertIn("+15%", output["log"]["detail"])
        self.assertIn("暂不可用", output["unknown"]["detail"])

    def test_responsive_labels_and_chart_profit_can_wrap(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "frontend/dashboard.css").read_text(encoding="utf-8")
        self.assertIn(".realized-return-metric .position-label { white-space:normal; }", css)
        self.assertIn(".practice-trade-marker-line { display:flex; flex-wrap:wrap;", css)
        self.assertIn(".practice-trade-marker-pnl { flex-basis:100%; white-space:normal; overflow-wrap:anywhere;", css)
        self.assertIn("translateX(var(--tooltip-offset, -50%))", css)
        component = (root / "web/src/components/practice/PracticeEquityChart.vue").read_text(encoding="utf-8")
        self.assertIn('@focus="positionTradeTooltip"', component)
        self.assertIn('@touchstart.passive="positionTradeTooltip"', component)


if __name__ == "__main__":
    unittest.main()
