import unittest
from datetime import datetime, timedelta

from app.backtesting.entry_risk_study import bounded_cost_confirmation, compare_entry_variants, entry_risk_groups


class EntryRiskStudyTests(unittest.TestCase):
    def test_groups_are_independent_and_missing_values_do_not_pass(self):
        self.assertEqual(entry_risk_groups(stage="divergence", strength=39, change_pct=5.8, stop_distance_pct=6), ["diverging_weak_chase"])
        self.assertEqual(entry_risk_groups(stage="climax", strength=71, change_pct=8.8, stop_distance_pct=9.8), ["climax_wide_stop"])
        self.assertEqual(entry_risk_groups(stage="divergence", strength=None, change_pct=6, stop_distance_pct=9), [])

    def bars(self):
        return [{"date": f"2026-09-{10+i:02}", "open": 10.0, "high": 11.0, "low": 9.8, "close": 10.5} for i in range(5)]

    def test_wait_fills_next_open_and_cannot_use_same_day_stop(self):
        bars = self.bars()
        bars[1].update(open=10.6, low=8, close=10.8)
        result = compare_entry_variants(entry_price=10, structural_stop=9, bars=bars)
        self.assertEqual(result["baseline"]["exit_index"], 1)
        self.assertEqual(result["wait_confirmation"]["entry_index"], 1)
        self.assertEqual(result["wait_confirmation"]["exit_index"], 4)  # T+1.
        self.assertLess(result["wait_confirmation"]["shares"], result["baseline"]["shares"])

    def test_break_before_confirmation_cannot_be_revived(self):
        bars = self.bars()
        bars[0].update(open=8.5, low=8, close=10.5)
        result = compare_entry_variants(entry_price=10, structural_stop=9, bars=bars)
        self.assertEqual(result["baseline"]["reason"], "original_structure_stop")
        self.assertFalse(result["wait_confirmation"]["entered"])
        self.assertGreater(result["half_initial"]["net_pnl"], result["baseline"]["net_pnl"])

    def test_no_lookahead_from_later_bars_to_confirmation(self):
        bars = self.bars()
        for bar in bars[:2]:
            bar.update(close=9.9)
        bars[-1].update(high=30, close=30)
        result = compare_entry_variants(entry_price=10, structural_stop=9, bars=bars)
        self.assertFalse(result["wait_confirmation"]["entered"])

    def test_cost_grace_only_for_small_cost_breach(self):
        now = datetime(2026, 9, 9, 10)
        base = dict(price=9.99, cost_line=10, structural_line=9.5, observed_at=now, now=now)
        first = bounded_cost_confirmation(**base)
        self.assertEqual(first["action"], "wait")
        for updates, reason in (({"structural_line": None}, "unknown_structure"),
                                ({"price": 9.4}, "original_structure_stop"),
                                ({"price": 9.97}, "deep_cost_breach"),
                                ({"hard_failure": True}, "hard_failure")):
            self.assertEqual(bounded_cost_confirmation(**{**base, **updates})["reason"], reason)
        pending = first["pending"]
        repeated = bounded_cost_confirmation(**base, pending=pending)
        self.assertEqual(repeated["action"], "wait")
        self.assertEqual(repeated["pending"], pending)
        later = now + timedelta(seconds=15)
        self.assertEqual(bounded_cost_confirmation(**{**base, "observed_at": later, "now": later}, pending=pending)["reason"], "two_distinct_breaches")
        later = now + timedelta(seconds=60)
        self.assertEqual(bounded_cost_confirmation(**{**base, "observed_at": later, "now": later}, pending=pending)["reason"], "confirmation_timeout")
        self.assertEqual(bounded_cost_confirmation(**{**base, "price": 10.01}, pending=pending)["action"], "clear")
        self.assertEqual(bounded_cost_confirmation(**{**base, "now": later}, pending=pending)["action"], "exit_required")


if __name__ == "__main__":
    unittest.main()
