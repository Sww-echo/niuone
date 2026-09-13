from __future__ import annotations

import unittest
from unittest.mock import patch

from app.market_data import cn_stock_tools


class CnStockQuoteTurnoverTests(unittest.TestCase):
    def test_single_quote_eastmoney_normalizes_hundredths_of_a_percent(self):
        with patch.object(cn_stock_tools, "http_get_json", return_value={"data": {
            "f43": 1000, "f60": 1000, "f168": 325, "f48": 1000000,
        }}) as request:
            quote = cn_stock_tools.get_quote("600000")
        self.assertEqual(quote["turnover"], 3.25)
        self.assertEqual(quote["price"], 10.0)
        self.assertEqual(quote["turnover_yuan"], 1000000)
        self.assertIn("f168", request.call_args.args[1]["fields"].split(","))
        self.assertNotIn("fltt", request.call_args.args[1])

    def test_single_quote_tencent_keeps_percentage_points(self):
        parts = [""] * 39
        for index, value in {1: "测试", 3: "10", 4: "10", 5: "10", 6: "1000",
                             33: "10", 34: "10", 37: "100", 38: "3.25"}.items():
            parts[index] = value
        for count, expected in ((39, 3.25), (38, None)):
            with self.subTest(count=count), patch.object(
                cn_stock_tools, "http_get_json", side_effect=TimeoutError,
            ), patch.object(cn_stock_tools.urllib.request, "urlopen") as request:
                response = request.return_value.__enter__.return_value
                response.read.return_value = ('v_sh600000="' + "~".join(parts[:count]) + '";').encode("gbk")
                quote = cn_stock_tools.get_quote("600000")
            self.assertEqual(quote["turnover"], expected)
            self.assertEqual(quote["price"], 10.0)
            self.assertEqual(quote["turnover_yuan"], 1000000)

    def test_unavailable_turnover_preserves_price_for_exits(self):
        for value in (None, "-", float("nan"), float("inf"), -1, True):
            with self.subTest(value=value), patch.object(cn_stock_tools, "http_get_json", return_value={
                "data": {"f43": 1000, "f60": 1000, "f168": value},
            }):
                quote = cn_stock_tools.get_quote("600000")
            self.assertIsNone(quote["turnover"])
            self.assertEqual(quote["price"], 10.0)
