#!/usr/bin/env python3
"""Unit tests for watchlist tracker store + paper fill math."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
COMPAT = APP / "compat"
sys.path.insert(0, str(COMPAT))
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT))

from app.storage import watchlist_tracker_store as store  # noqa: E402
from app.trading import watchlist_tracker_service as service  # noqa: E402


class WatchlistPaperMathTests(unittest.TestCase):
    def test_compute_paper_fill_maximizes_whole_shares_under_budget(self) -> None:
        fill = service.compute_paper_fill(12.34, 10_000)
        self.assertTrue(fill["filled"])
        self.assertEqual(fill["buy_shares"], int(10_000 // 12.34))
        self.assertLessEqual(fill["buy_amount"], 10_000)
        self.assertAlmostEqual(fill["buy_amount"], fill["buy_shares"] * 12.34, places=2)

    def test_compute_paper_fill_rejects_price_above_budget(self) -> None:
        fill = service.compute_paper_fill(12_000, 10_000)
        self.assertFalse(fill["filled"])
        self.assertEqual(fill["buy_shares"], 0)
        self.assertEqual(fill["reason"], "price_above_budget")

    def test_compute_paper_pnl(self) -> None:
        pnl = service.compute_paper_pnl(buy_shares=100, buy_amount=1000, last_price=12)
        self.assertEqual(pnl["market_value"], 1200)
        self.assertEqual(pnl["pnl"], 200)
        self.assertEqual(pnl["pnl_percent"], 20.0)

    def test_clamp_board_days(self) -> None:
        self.assertEqual(service.clamp_board_days(5), 5)
        self.assertEqual(service.clamp_board_days(15), 15)
        self.assertEqual(service.clamp_board_days(9), 7)
        self.assertEqual(service.clamp_board_days("60"), 60)


class WatchlistStoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="niuone-watchlist-")
        self.db_path = Path(self.temp.name) / "watchlist.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_group_and_stock_board_with_selectable_buy_date(self) -> None:
        group = service.create_group(name="半导体", note="主线", db_path=self.db_path)
        self.assertEqual(group["name"], "半导体")

        def fake_history(code: str, days: int):
            base = [
                {
                    "date": "2026-07-20",
                    "open": 10.0,
                    "close": 10.5,
                    "high": 10.8,
                    "low": 9.9,
                    "volume": 1000,
                },
                {
                    "date": "2026-07-21",
                    "open": 10.5,
                    "close": 11.0,
                    "high": 11.2,
                    "low": 10.4,
                    "volume": 1100,
                },
                {
                    "date": "2026-07-22",
                    "open": 11.0,
                    "close": 10.8,
                    "high": 11.1,
                    "low": 10.6,
                    "volume": 1200,
                },
            ]
            return [
                service._kline_to_quote(
                    code,
                    {**row, "prev_close": None if index == 0 else base[index - 1]["close"]},
                    source="test",
                )
                for index, row in enumerate(base)
            ][-days:]

        def fake_today(code: str):
            return {
                "code": code,
                "name": "测试股份",
                "trade_date": "2026-07-22",
                "open_price": 11.0,
                "close_price": 10.8,
                "high_price": 11.1,
                "low_price": 10.6,
                "change_amount": -0.2,
                "change_percent": -1.8182,
                "volume": 1200,
                "source": "test",
            }

        with patch.object(service, "fetch_history_quotes", side_effect=fake_history), patch.object(
            service, "fetch_today_quote", side_effect=fake_today
        ), patch.object(service, "normalize_code", side_effect=lambda raw: {
            "code": str(raw).zfill(6)[-6:],
            "market": "sh",
            "display": f"sh{str(raw).zfill(6)[-6:]}",
        }):
            added = service.add_stocks(
                codes="600519",
                group_id=int(group["id"]),
                buy_date="2026-07-20",
                target_amount=10_000,
                backfill_days=5,
                db_path=self.db_path,
            )
        self.assertEqual(added["added"], ["600519"])
        board = service.build_board(days=5, group_id=int(group["id"]), db_path=self.db_path)
        self.assertEqual(board["total"], 1)
        stock = board["stocks"][0]
        self.assertEqual(stock["code"], "600519")
        self.assertEqual(stock["name"], "测试股份")
        self.assertEqual(stock["group_name"], "半导体")
        self.assertEqual(stock["buy_date"], "2026-07-20")
        self.assertEqual(stock["buy_shares"], int(10_000 // 10.5))
        self.assertAlmostEqual(stock["buy_price"], 10.5, places=4)
        self.assertIsNotNone(stock["pnl"])
        # change buy date and recompute
        updated = service.update_stock(
            "600519",
            buy_date="2026-07-21",
            db_path=self.db_path,
        )
        self.assertEqual(updated["buy_date"], "2026-07-21")
        self.assertEqual(updated["buy_shares"], int(10_000 // 11.0))
        self.assertAlmostEqual(float(updated["buy_price"]), 11.0, places=4)

    def test_delete_group_keeps_stocks_ungrouped(self) -> None:
        group = service.create_group(name="临时组", db_path=self.db_path)
        con = store.connect(self.db_path)
        try:
            store.upsert_stock(
                con,
                code="000001",
                name="平安银行",
                market="sz",
                group_id=int(group["id"]),
                buy_date="2026-07-21",
                target_amount=10_000,
            )
        finally:
            con.close()
        service.delete_group(int(group["id"]), db_path=self.db_path)
        board = service.build_board(db_path=self.db_path)
        self.assertEqual(board["total"], 1)
        self.assertIsNone(board["stocks"][0]["group_id"])
        self.assertEqual(board["groups"], [])


if __name__ == "__main__":
    unittest.main()
