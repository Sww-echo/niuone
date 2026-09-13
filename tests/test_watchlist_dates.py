from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from app.market_data import cn_stock_tools
from app.storage import watchlist_tracker_store as store
from app.trading import watchlist_tracker_service as service


class WatchlistDateTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="niuone-watchlist-dates-"))
        self.db = Path(self.temp) / "watchlist.db"
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)
        store.upsert_stock(self.con, code="600001", name="测试", buy_date="2026-09-11")
        self.enterContext(patch.object(service, "now_shanghai", return_value=
            datetime(2026, 9, 13, 16, tzinfo=ZoneInfo("Asia/Shanghai"))))

    def test_weekend_quote_keeps_provider_date_and_skips_today_write(self):
        tools = Mock()
        tools.get_quote.return_value = {"code": "600001", "price": 12,
                                      "quote_time": "2026-09-11T15:00:00+08:00"}
        with patch.object(service, "_import_cn_stock_tools", return_value=tools):
            self.assertEqual(service.fetch_today_quote("600001")["trade_date"], "2026-09-11")
            result = service.update_today_quotes(db_path=self.db)
            self.assertTrue(result["skipped"])
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(result["updated"], 0)
            service.update_today_quotes(db_path=self.db, force=True)
        self.assertIsNone(store.quote_on_date(self.con, "600001", "2026-09-13"))
        self.assertEqual(store.quote_on_date(self.con, "600001", "2026-09-11")["close_price"], 12)

    def test_unknown_or_invalid_quote_time_preserves_existing_data(self):
        store.upsert_quotes(self.con, [{"code": "600001", "trade_date": "2026-09-11", "close_price": 12}])
        tools = Mock()
        for stamp in (None, "", "2026-09-13T15:00:00+08:00", "2026-09-14T15:00:00+08:00"):
            with self.subTest(stamp=stamp), patch.object(service, "_import_cn_stock_tools", return_value=tools):
                tools.get_quote.return_value = {"code": "600001", "price": 99, "quote_time": stamp}
                result = service.update_today_quotes(db_path=self.db, force=True)
                self.assertEqual(len(result["failed"]), 1)
                self.assertEqual(result["updated"], 0)
        self.assertEqual(store.quote_on_date(self.con, "600001", "2026-09-11")["close_price"], 12)
        self.assertEqual(store.stock_quote_dates(self.con, "600001"), ["2026-09-11"])

    def test_default_buy_date_is_last_completed_session(self):
        self.assertEqual(service.build_board(db_path=self.db)["default_buy_date"], "2026-09-11")
        with patch.object(service, "now_shanghai", return_value=
                   datetime(2026, 9, 14, 11, tzinfo=ZoneInfo("Asia/Shanghai"))):
            self.assertEqual(service.latest_session_date(completed=True), "2026-09-11")
            self.assertEqual(service.latest_session_date(), "2026-09-14")
            service.update_stock("600001", buy_date="2026-09-14", db_path=self.db)
            store.upsert_quotes(self.con, [{"code": "600001", "trade_date": "2026-09-14", "close_price": 12}])
            service.refresh_paper_fill_for_stock(self.con, "600001")
            self.assertEqual(store.get_stock(self.con, "600001")["buy_shares"], 0)
            self.assertEqual(service.build_board(db_path=self.db)["stocks"][0]["fill_status"], "awaiting_close")

    def test_empty_code_selection_never_updates_every_stock(self):
        with patch.object(service, "fetch_today_quote") as fetch:
            self.assertEqual(service.update_today_quotes(codes=[], db_path=self.db)["updated"], 0)
        fetch.assert_not_called()

    def test_missing_only_backfill_finalizes_intraday_bar_before_filling(self):
        store.upsert_quotes(self.con, [{"code": "600001", "trade_date": "2026-09-11",
            "close_price": 10, "is_final": False, "quote_time": "2026-09-11T11:00:00+08:00"}])
        service.refresh_paper_fill_for_stock(self.con, "600001")
        self.assertEqual(store.get_stock(self.con, "600001")["buy_shares"], 0)
        with patch.object(service, "fetch_history_quotes", return_value=[{
            "code": "600001", "trade_date": "2026-09-11", "close_price": 12,
        }]):
            result = service.backfill_quotes(db_path=self.db, missing_only=True)
        self.assertEqual(result["quotes"], 1)
        self.assertEqual(store.get_stock(self.con, "600001")["buy_price"], 12)
        self.assertEqual(store.quote_on_date(self.con, "600001", "2026-09-11")["is_final"], 1)
        store.upsert_quotes(self.con, [{"code": "600001", "trade_date": "2026-09-11",
                                      "close_price": 10, "is_final": False}])
        self.assertEqual(store.quote_on_date(self.con, "600001", "2026-09-11")["close_price"], 12)

    def test_missing_only_repairs_unusable_price_but_keeps_good_history(self):
        store.upsert_quotes(self.con, [
            {"code": "600001", "trade_date": "2026-09-10", "close_price": 11},
            {"code": "600001", "trade_date": "2026-09-11", "close_price": None},
        ])
        with patch.object(service, "fetch_history_quotes", return_value=[
            {"code": "600001", "trade_date": "2026-09-10", "close_price": 20},
            {"code": "600001", "trade_date": "2026-09-11", "close_price": 12},
        ]):
            service.backfill_quotes(db_path=self.db, missing_only=True)
        self.assertEqual(store.quote_on_date(self.con, "600001", "2026-09-10")["close_price"], 11)
        self.assertEqual(store.quote_on_date(self.con, "600001", "2026-09-11")["close_price"], 12)

    def test_preserved_final_quote_counts_as_skipped_not_updated(self):
        store.upsert_quotes(self.con, [{"code": "600001", "trade_date": "2026-09-11",
                                      "close_price": 12, "is_final": True}])
        with patch.object(service, "fetch_today_quote", return_value={
            "code": "600001", "trade_date": "2026-09-11", "close_price": 10, "is_final": False,
        }):
            result = service.update_today_quotes(db_path=self.db, force=True)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(store.quote_on_date(self.con, "600001", "2026-09-11")["close_price"], 12)


class ProviderQuoteTimeTests(unittest.TestCase):
    def test_eastmoney_epoch_is_converted_to_shanghai(self):
        stamp = datetime(2026, 9, 11, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch.object(cn_stock_tools, "http_get_json", return_value={
            "data": {"f43": 1200, "f60": 1000, "f124": int(stamp.timestamp())},
        }) as request:
            quote = cn_stock_tools.get_quote("600001")
        self.assertEqual(quote["quote_time"], "2026-09-11T15:00:00+08:00")
        self.assertIn("f124", request.call_args.args[1]["fields"].split(","))

    def test_tencent_compact_time_is_preserved(self):
        parts = [""] * 39
        for index, value in {1: "测试", 3: "12", 4: "10", 5: "10", 6: "1000",
                             30: "20260911150000", 33: "12", 34: "10", 37: "100"}.items():
            parts[index] = value
        with patch.object(cn_stock_tools, "http_get_json", side_effect=TimeoutError), patch.object(
            cn_stock_tools.urllib.request, "urlopen",
        ) as request:
            request.return_value.__enter__.return_value.read.return_value = (
                'v_sh600001="' + "~".join(parts) + '";'
            ).encode("gbk")
            self.assertEqual(cn_stock_tools.get_quote("600001")["quote_time"], "2026-09-11T15:00:00+08:00")

    def test_unavailable_times_are_not_replaced_with_now(self):
        for value in (None, "", "-", True, float("nan"), float("inf"), "20260230150000", "bad", -1):
            with self.subTest(value=value):
                self.assertEqual(cn_stock_tools.normalize_quote_time(value), "")
