#!/usr/bin/env python3
import unittest
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "compat"))

from screening import holding_cycle  # noqa: E402


class HoldingFastCycleTests(unittest.TestCase):
    def test_merges_complete_stock_profiles_with_fresh_operational_context(self):
        complete = {
            "market": {"state": "defensive"},
            "themes": {"旧题材": {"state": "candidate", "score": 55}},
            "stocks": {
                "600001": {
                    "industry": "新题材",
                    "strong_score": 88.0,
                    "strong": True,
                    "theme_profiles": [{
                        "industry": "新题材",
                        "strong_score": 88.0,
                        "attribution_score": 70.0,
                    }],
                    "theme_attributions": [{
                        "theme": "新题材",
                        "attribution_score": 70.0,
                    }],
                },
            },
        }
        operational = {
            "market": {"state": "offensive"},
            "themes": {
                "新题材": {"state": "mainline", "score": 82.0},
            },
            "stocks": {
                "600001": {
                    "theme_attributions": [{
                        "theme": "新题材",
                        "attribution_score": 91.0,
                        "attribution_weight": 0.8,
                    }],
                },
                "600002": {
                    "theme_attributions": [{
                        "theme": "新题材",
                        "attribution_score": 85.0,
                    }],
                },
            },
        }

        merged = holding_cycle.merge_niuone_holding_cycle_context(
            complete,
            operational,
        )

        self.assertEqual(merged["market"]["state"], "offensive")
        self.assertEqual(merged["themes"]["新题材"]["state"], "mainline")
        self.assertEqual(merged["stocks"]["600001"]["strong_score"], 88.0)
        self.assertEqual(
            merged["stocks"]["600001"]["theme_profiles"][0][
                "attribution_score"
            ],
            91.0,
        )
        self.assertNotIn("600002", merged["stocks"])

    def test_rescores_only_supplied_holdings_with_active_scorers(self):
        calls = {"symbols": [], "analysis": []}

        def quote_fetcher(symbols, **kwargs):
            calls["symbols"] = list(symbols)
            self.assertEqual(kwargs["timeout_seconds"], 5.0)
            self.assertEqual(kwargs["max_attempts"], 2)
            return {
                "sh600001": {
                    "name": "持仓甲",
                    "price": 10.5,
                    "change_pct": 2.0,
                    "amount": 2e8,
                    "turnover": 3.0,
                    "quote_time": "20260827100500",
                },
                "sz000002": {
                    "name": "持仓乙",
                    "price": 8.5,
                    "change_pct": 1.0,
                    "amount": 1e8,
                    "turnover": 2.0,
                    "quote_time": "20260827100500",
                },
                "sh600999": {
                    "name": "非持仓",
                    "price": 12.0,
                    "quote_time": "20260827100500",
                },
            }

        def history_loader(symbols, **_kwargs):
            return {symbol: [{"date": "2026-08-26"}] for symbol in symbols}

        def analyze(code, symbol, **kwargs):
            calls["analysis"].append((code, symbol, sorted(kwargs["scorers"])))
            return {
                "best_strategy": "test_strategy",
                "best_score": 9.0,
                "best_decision_score": 9.0,
                "best_verdict": "满足",
                "consensus_count": 1,
                "strategies": {
                    "test_strategy": {
                        "score": 9.0,
                        "score_total": 10,
                        "entry_threshold": 8.0,
                        "actionable": True,
                        "hard_blockers": [],
                        "verdict": "满足",
                    },
                },
            }

        with (
            patch.object(
                holding_cycle,
                "_active_scorers",
                return_value=({"test_strategy": object()}, None, None, 1),
            ),
            patch.object(
                holding_cycle,
                "prepare_strategy_rows",
                return_value=[{"date": "2026-08-26"}],
            ),
            patch.object(
                holding_cycle,
                "analyze_all_strategies",
                side_effect=analyze,
            ),
            patch.object(
                holding_cycle,
                "resolve_quote_trading_dates",
                return_value=("2026-08-27", "2026-08-26"),
            ),
        ):
            payload = holding_cycle.build_holding_cycle_payload(
                [
                    {"code": "600001", "name": "持仓甲", "qty": 100},
                    {"code": "000002", "name": "持仓乙", "qty": 200},
                ],
                {"market_summary": {"summary": "平衡"}},
                now=datetime(2026, 8, 27, 10, 5, 0),
                quote_fetcher=quote_fetcher,
                history_loader=history_loader,
                board_loader=lambda _path: {},
            )

        self.assertEqual(calls["symbols"], ["sz000002", "sh600001"])
        self.assertEqual(
            calls["analysis"],
            [
                ("000002", "sz000002", ["test_strategy"]),
                ("600001", "sh600001", ["test_strategy"]),
            ],
        )
        self.assertTrue(payload["holding_cycle_only"])
        self.assertEqual(payload["holding_cycle_codes"], ["000002", "600001"])
        self.assertEqual(payload["decision_cycle_kind"], "holding_fast")
        self.assertEqual(payload["holding_cycle_data_status"], "ready")
        self.assertEqual(
            {item["code"] for item in payload["observed_items"]},
            {"000002", "600001"},
        )
        self.assertEqual(
            {item["code"] for item in payload["trade_items"]},
            {"000002", "600001"},
        )
        self.assertNotIn("600999", str(payload))

    def test_stale_quotes_fail_closed_before_history_or_scoring(self):
        history_calls = []

        with patch.object(
            holding_cycle,
            "_active_scorers",
            return_value=({"test_strategy": object()}, None, None, 1),
        ):
            payload = holding_cycle.build_holding_cycle_payload(
                [{"code": "600001", "name": "持仓甲", "qty": 100}],
                {},
                now=datetime(2026, 8, 27, 10, 5, 0),
                quote_fetcher=lambda _symbols, **_kwargs: {
                    "sh600001": {
                        "name": "持仓甲",
                        "price": 10.5,
                        "quote_time": "20260826150000",
                    },
                },
                history_loader=lambda *_args, **_kwargs: history_calls.append(True),
                board_loader=lambda _path: {},
            )

        self.assertEqual(payload["holding_cycle_data_status"], "stale_or_missing_quotes")
        self.assertEqual(payload["trade_items"], [])
        self.assertEqual(history_calls, [])

    def test_quote_failure_is_bounded_and_does_not_raise(self):
        def failing_quotes(_symbols, **_kwargs):
            raise TimeoutError("upstream timeout")

        with patch.object(
            holding_cycle,
            "_active_scorers",
            return_value=({"test_strategy": object()}, None, None, 1),
        ):
            payload = holding_cycle.build_holding_cycle_payload(
                [{"code": "600001", "name": "持仓甲", "qty": 100}],
                {},
                now=datetime(2026, 8, 27, 10, 5, 0),
                quote_fetcher=failing_quotes,
            )

        self.assertEqual(payload["holding_cycle_data_status"], "quote_unavailable")
        self.assertEqual(payload["holding_cycle_error"], "TimeoutError")
        self.assertEqual(payload["trade_items"], [])

    def test_history_failure_returns_empty_buy_pool_for_normal_exit_checks(self):
        with (
            patch.object(
                holding_cycle,
                "_active_scorers",
                return_value=({"test_strategy": object()}, None, None, 1),
            ),
            patch.object(
                holding_cycle,
                "resolve_quote_trading_dates",
                return_value=("2026-08-27", "2026-08-26"),
            ),
        ):
            payload = holding_cycle.build_holding_cycle_payload(
                [{"code": "600001", "name": "持仓甲", "qty": 100}],
                {},
                now=datetime(2026, 8, 27, 10, 5, 0),
                quote_fetcher=lambda _symbols, **_kwargs: {
                    "sh600001": {
                        "name": "持仓甲",
                        "price": 10.5,
                        "quote_time": "20260827100500",
                    },
                },
                history_loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError("cache unavailable")
                ),
            )

        self.assertEqual(payload["holding_cycle_data_status"], "history_unavailable")
        self.assertEqual(payload["holding_cycle_error"], "OSError")
        self.assertEqual(payload["trade_items"], [])
        self.assertTrue(payload["holding_cycle_only"])

    def test_scoring_exception_is_reported_without_exposing_exception_text(self):
        with (
            patch.object(
                holding_cycle,
                "_active_scorers",
                return_value=({"test_strategy": object()}, None, None, 1),
            ),
            patch.object(
                holding_cycle,
                "prepare_strategy_rows",
                return_value=[{"date": "2026-08-26"}],
            ),
            patch.object(
                holding_cycle,
                "analyze_all_strategies",
                side_effect=KeyError("private scorer detail"),
            ),
            patch.object(
                holding_cycle,
                "resolve_quote_trading_dates",
                return_value=("2026-08-27", "2026-08-26"),
            ),
        ):
            payload = holding_cycle.build_holding_cycle_payload(
                [{"code": "600001", "name": "持仓甲", "qty": 100}],
                {},
                now=datetime(2026, 8, 27, 10, 5, 0),
                quote_fetcher=lambda _symbols, **_kwargs: {
                    "sh600001": {
                        "name": "持仓甲",
                        "price": 10.5,
                        "quote_time": "20260827100500",
                    },
                },
                history_loader=lambda *_args, **_kwargs: {
                    "sh600001": [{"date": "2026-08-26"}],
                },
                board_loader=lambda _path: {},
            )

        self.assertEqual(payload["holding_cycle_data_status"], "scoring_error")
        self.assertEqual(
            payload["holding_cycle_scoring_errors"],
            [{"error_type": "KeyError", "count": 1}],
        )
        self.assertNotIn("private scorer detail", str(payload))


if __name__ == "__main__":
    unittest.main()
