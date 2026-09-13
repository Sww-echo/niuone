from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.storage import watchlist_tracker_store as store
from app.trading import watchlist_tracker_service as service


class WatchlistConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="niuone-watchlist-consistency-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "watchlist.db"
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)
        self.a = store.create_group(self.con, name="A组")
        self.b = store.create_group(self.con, name="B组")
        for name, value in (
            ("today_shanghai", "2026-09-14"),
            ("now_shanghai", datetime(2026, 9, 14, 16, tzinfo=ZoneInfo("Asia/Shanghai"))),
        ):
            mock = patch.object(service, name, return_value=value, create=True)
            mock.start()
            self.addCleanup(mock.stop)

    def stock(self, code="600001", group=None, **overrides):
        fields = dict(code=code, name=code, market="sh", group_id=group,
                      note="保留原观察理由", buy_date="2026-08-24",
                      buy_price=10, buy_shares=100, buy_amount=1000)
        fields.update(overrides)
        return store.upsert_stock(self.con, **fields)

    def quote(self, code, day, price):
        store.upsert_quotes(self.con, [{"code": code, "trade_date": day,
                                      "close_price": price, "source": "fixture"}])

    def test_display_window_does_not_change_cost_or_valuation(self):
        self.stock("600001", self.a["id"])
        self.stock("600002", self.b["id"])
        self.quote("600001", "2026-08-24", 12)
        for day in ("2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31"):
            self.quote("600002", day, 11)
        results = [service.build_board(days=days, db_path=self.db) for days in (5, 15, 60)]
        for result in results:
            self.assertEqual(result["summary"]["total_cost"], 2000)
            self.assertEqual(result["summary"]["total_pnl"], 300)
            row = next(row for row in result["stocks"] if row["code"] == "600001")
            self.assertEqual(row["last_quote_date"], "2026-08-24")
            self.assertTrue(row["quote_stale"])

    def test_missing_valuation_retains_cost_and_marks_totals_incomplete(self):
        self.stock()
        board = service.build_board(db_path=self.db)
        self.assertEqual(board["summary"]["total_cost"], 1000)
        self.assertEqual(board["summary"]["filled_count"], 1)
        self.assertFalse(board["summary"]["valuation_complete"])
        self.assertIsNone(board["summary"]["total_market_value"])
        self.assertIsNone(board["summary"]["total_pnl"])

    def test_duplicate_add_preserves_existing_record_without_fetching(self):
        before = self.stock(group=self.a["id"])
        with patch.object(service, "fetch_history_quotes") as history, patch.object(service, "fetch_today_quote") as live:
            result = service.add_stocks(codes="600001", group_id=self.b["id"], buy_date="2026-09-11", db_path=self.db)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["skipped_existing"], ["600001"])
        self.assertEqual(store.get_stock(self.con, "600001"), before)
        history.assert_not_called()
        live.assert_not_called()

    def test_missing_buy_quote_does_not_erase_a_saved_fill(self):
        before = self.stock()
        service.refresh_paper_fill_for_stock(self.con, "600001")
        after = store.get_stock(self.con, "600001")
        for key in ("buy_date", "buy_price", "buy_shares", "buy_amount", "note"):
            self.assertEqual(after[key], before[key])

    def test_revised_history_and_note_edits_preserve_saved_fill(self):
        self.stock()
        self.quote("600001", "2026-08-24", 20)
        service.refresh_paper_fill_for_stock(self.con, "600001")
        updated = service.update_stock("600001", note="新备注", buy_date="2026-08-24", db_path=self.db)
        self.assertEqual(updated["buy_price"], 10)
        self.assertEqual(updated["buy_shares"], 100)

    def test_explicit_date_change_does_not_reuse_an_old_fill(self):
        self.stock()
        updated = service.update_stock("600001", buy_date="2026-09-11", db_path=self.db)
        self.assertEqual(updated["buy_date"], "2026-09-11")
        self.assertEqual(updated["buy_shares"], 0)
        self.assertIsNone(updated["buy_price"])
        self.assertIsNone(updated["pnl"])

    def test_delayed_fill_cannot_overwrite_an_intervening_date_edit(self):
        old = self.stock(buy_price=None, buy_amount=0, buy_shares=0)
        store.update_stock_fields(self.con, "600001", buy_date="2026-09-11")
        result = store.fill_stock_if_unchanged(self.con, old, buy_price=10, buy_shares=100, buy_amount=1000)
        self.assertEqual(result["buy_date"], "2026-09-11")
        self.assertEqual(result["buy_shares"], 0)

    def test_invalid_dates_and_amounts_do_not_change_records(self):
        before = self.stock()
        for fields in ({"buy_date": "2026-09-13"}, {"buy_date": "2026-09-15"},
                       {"buy_date": "bad"}, {"target_amount": 0},
                       {"target_amount": float("nan")}, {"target_amount": float("inf")}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                service.update_stock("600001", db_path=self.db, **fields)
        self.assertEqual(store.get_stock(self.con, "600001"), before)

    def test_quote_before_buy_date_cannot_value_a_holding(self):
        self.stock(buy_date="2026-09-11")
        self.quote("600001", "2026-09-10", 12)
        board = service.build_board(db_path=self.db)
        self.assertEqual(board["summary"]["total_cost"], 1000)
        self.assertIsNone(board["summary"]["total_pnl"])

    def test_invalid_prices_do_not_replace_last_valid_valuation(self):
        self.stock()
        self.quote("600001", "2026-09-10", 12)
        self.quote("600001", "2026-09-11", float("inf"))
        self.assertEqual(service.build_board(db_path=self.db)["stocks"][0]["last_price"], 12)

    def test_selecting_group_does_not_zero_other_group_counts(self):
        self.stock("600001", self.a["id"])
        self.stock("600002", self.b["id"])
        board = service.build_board(group_id=self.a["id"], db_path=self.db)
        self.assertEqual([row["code"] for row in board["stocks"]], ["600001"])
        self.assertEqual([group["stock_count"] for group in board["groups"]], [1, 1])
        self.assertEqual(board["total_all"], 2)

    def test_ungrouped_is_distinct_from_all_and_empty_search(self):
        self.stock("600001", self.a["id"])
        self.stock("600002")
        board = service.build_board(group_id="ungrouped", db_path=self.db)
        self.assertEqual([row["code"] for row in board["stocks"]], ["600002"])
        empty = service.build_board(q="不存在", db_path=self.db)
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["total_all"], 2)
        self.assertEqual(empty["ungrouped_count"], 1)

    def test_legacy_weekend_and_future_quotes_are_ignored_without_deletion(self):
        self.stock()
        for day, price in (("2026-09-11", 12), ("2026-09-13", 99), ("2026-10-01", 100)):
            self.quote("600001", day, price)
        board = service.build_board(db_path=self.db)
        self.assertEqual(board["trade_dates"], ["2026-09-11"])
        self.assertEqual(board["stocks"][0]["last_price"], 12)
        self.assertIsNotNone(store.quote_on_date(self.con, "600001", "2026-09-13"))

    def test_schema_upgrade_keeps_legacy_quotes_and_positions(self):
        before = self.stock()
        self.quote("600001", "2026-09-11", 12)
        self.con.execute("ALTER TABLE watchlist_daily_quotes DROP COLUMN quote_time")
        self.con.execute("ALTER TABLE watchlist_daily_quotes DROP COLUMN is_final")
        self.con.commit()
        store.init_db(self.con)
        store.init_db(self.con)
        self.assertEqual(store.get_stock(self.con, "600001"), before)
        quote = store.quote_on_date(self.con, "600001", "2026-09-11")
        self.assertEqual(quote["close_price"], 12)
        self.assertEqual(quote["quote_time"], "")
        self.assertEqual(quote["is_final"], 1)


if __name__ == "__main__":
    unittest.main()
