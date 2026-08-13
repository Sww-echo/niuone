from __future__ import annotations

import unittest

from app.market_data.technical_analysis import (
    TechnicalMarketDataError,
    fetch_minute_series,
)


class TechnicalMinuteDataTests(unittest.TestCase):
    def test_short_nonempty_series_is_returned_for_rich_insufficient_analysis(self) -> None:
        trends = [
            f"2026-08-13 09:{30 + index:02d},10,{10 + index / 100},10,10,{100 + index},0,{10 + index / 100}"
            for index in range(9)
        ]

        result = fetch_minute_series(
            "600000",
            downloader=lambda *_args: {
                "data": {"name": "样例", "preClose": 10, "trends": trends},
            },
        )

        self.assertEqual(len(result["prices"]), 9)
        self.assertEqual(result["data_quality"]["point_count"], 9)
        self.assertTrue(result["data_quality"]["aggregation_required"])

    def test_source_cleaning_counts_invalid_prices_rows_and_volumes(self) -> None:
        result = fetch_minute_series(
            "600000",
            downloader=lambda *_args: {
                "data": {
                    "name": "样例",
                    "preClose": 10,
                    "trends": [
                        "bad,row",
                        "2026-08-13 09:30,10,0,10,10,100,0,10",
                        "2026-08-13 09:31,10,10.1,10,10,,0,10.1",
                        "2026-08-13 09:32,10,10.2,10,10,-1,0,10.2",
                        "2026-08-13 09:33,10,10.3,10,10,0,0,10.3",
                    ],
                },
            },
        )

        self.assertEqual(result["volumes"], [0.0, 0.0, 0.0])
        self.assertEqual(result["data_quality"]["source_row_count"], 5)
        self.assertEqual(result["data_quality"]["source_invalid_row_count"], 1)
        self.assertEqual(result["data_quality"]["source_invalid_price_count"], 1)
        self.assertEqual(result["data_quality"]["source_missing_volume_count"], 2)

    def test_empty_valid_series_remains_unavailable(self) -> None:
        with self.assertRaisesRegex(TechnicalMarketDataError, "minute_insufficient"):
            fetch_minute_series(
                "600000",
                downloader=lambda *_args: {"data": {"trends": ["bad,row"]}},
            )


if __name__ == "__main__":
    unittest.main()
