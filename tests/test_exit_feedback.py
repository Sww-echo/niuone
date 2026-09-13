#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
COMPAT = APP / "compat"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(COMPAT))

from strategies.exit_feedback import (  # noqa: E402
    EXIT_FEEDBACK_DEFAULT_PARAMETERS,
    effective_exit_feedback_parameters,
    observation_metrics,
    propose_exit_feedback_policy,
)


def feedback_rows(
    count: int,
    *,
    sell_fly: int = 0,
    avoided_loss: int = 0,
    close_return_pct: float = 0.0,
    exit_rule: str = "no_progress",
    replacement_regret: int | None = None,
    replacement_regret_pct: float | None = None,
    policy_version: int = 0,
):
    rows = []
    for index in range(count):
        month = 1 + index % 3
        day = 1 + index // 3
        rows.append({
            "trade_key": f"trade-{index}",
            "code": f"{600000 + index:06d}",
            "sell_time": f"2026-{month:02d}-{day:02d} 14:45:00",
            "sell_price": 10.0,
            "sell_notional": 10000.0,
            "shares": 1000,
            "price_basis": "actual_execution",
            "exit_rule": exit_rule,
            "close_return_pct": close_return_pct,
            "mae_pct": -5.0 if avoided_loss else -1.0,
            "sell_fly": sell_fly,
            "avoided_loss": avoided_loss,
            "replacement_regret": replacement_regret,
            "replacement_regret_pct": replacement_regret_pct,
            "replacement_executed": int(replacement_regret is not None),
            "feedback_policy_version": policy_version,
            "completed": 1,
        })
    return rows


class ExitFeedbackPolicyTests(unittest.TestCase):
    def test_defaults_fail_closed_when_policy_is_disabled(self):
        parameters = effective_exit_feedback_parameters({
            "enabled": False,
            "parameters": {
                "soft_exit_confirmations": 3,
                "replacement_priority_margin": 5,
            },
        })

        self.assertEqual(parameters, EXIT_FEEDBACK_DEFAULT_PARAMETERS)

    def test_cross_month_sample_gate_blocks_early_tuning(self):
        proposal = propose_exit_feedback_policy(
            feedback_rows(19, sell_fly=1, close_return_pct=5.0),
            None,
            min_samples=20,
            min_months=3,
        )

        self.assertFalse(proposal["persist"])
        self.assertEqual(proposal["status"], "learning")
        self.assertEqual(
            proposal["parameters"]["soft_exit_confirmations"],
            2,
        )

    def test_calendar_month_boundaries_do_not_fake_three_month_span(self):
        rows = feedback_rows(30, sell_fly=1, close_return_pct=5.0)
        for index, row in enumerate(rows):
            row["sell_time"] = (
                "2026-01-31 14:45:00"
                if index < 10
                else "2026-02-15 14:45:00"
                if index < 20
                else "2026-03-01 14:45:00"
            )

        proposal = propose_exit_feedback_policy(
            rows,
            None,
            min_samples=30,
            min_months=3,
        )

        self.assertEqual(proposal["action"], "sample_gate")
        self.assertEqual(proposal["observation_span_months"], 1)

    def test_high_sell_fly_rate_moves_only_one_patience_step(self):
        proposal = propose_exit_feedback_policy(
            feedback_rows(30, sell_fly=1, close_return_pct=5.0),
            None,
            min_samples=30,
            min_months=3,
        )

        self.assertTrue(proposal["persist"])
        self.assertEqual(
            proposal["parameters"]["soft_exit_confirmations"],
            3,
        )
        self.assertEqual(proposal["parameters"]["soft_exit_reduce_ratio"], 0.5)
        self.assertEqual(proposal["action"], "reduce_sell_fly:soft_exit_confirmations")

    def test_replacement_regret_raises_margin_after_component_gate(self):
        proposal = propose_exit_feedback_policy(
            feedback_rows(
                30,
                exit_rule="other_exit",
                replacement_regret=1,
                replacement_regret_pct=3.0,
            ),
            None,
            min_samples=30,
            min_months=3,
        )

        self.assertEqual(proposal["action"], "raise_replacement_margin")
        self.assertEqual(
            proposal["parameters"]["replacement_priority_margin"],
            4.0,
        )

    def test_cooldown_requires_new_completed_observations(self):
        current = {
            "version": 1,
            "observation_count": 30,
            "parameters": EXIT_FEEDBACK_DEFAULT_PARAMETERS,
        }
        proposal = propose_exit_feedback_policy(
            feedback_rows(34, sell_fly=1, close_return_pct=5.0),
            current,
            min_samples=30,
            min_months=3,
            cooldown_samples=5,
        )

        self.assertFalse(proposal["persist"])
        self.assertEqual(proposal["status"], "cooldown")

    def test_materially_worse_version_rolls_back_previous_parameters(self):
        prior = dict(EXIT_FEEDBACK_DEFAULT_PARAMETERS)
        current_parameters = {**prior, "soft_exit_confirmations": 3}
        old_rows = feedback_rows(20)
        new_rows = feedback_rows(
            20,
            sell_fly=1,
            close_return_pct=5.0,
            policy_version=2,
        )
        for index, row in enumerate(new_rows, start=20):
            row["trade_key"] = f"trade-{index}"
            row["code"] = f"{600000 + index:06d}"
        current = {
            "version": 2,
            "observation_count": 20,
            "parameters": current_parameters,
            "previous_parameters": prior,
            "baseline_metrics": {
                "objective_regret_score": 0.0,
                "objective_regret_upper": 0.0,
                "soft_opportunity_return": {
                    "lower": 0.0,
                    "upper": 0.0,
                },
            },
            "action": "reduce_sell_fly:soft_exit_confirmations",
        }

        proposal = propose_exit_feedback_policy(
            [*old_rows, *new_rows],
            current,
            min_samples=30,
            min_months=3,
            cooldown_samples=10,
        )

        self.assertEqual(proposal["action"], "automatic_rollback")
        self.assertEqual(proposal["rollback_of"], 2)
        self.assertEqual(proposal["parameters"], prior)

    def test_hold_records_evaluation_without_creating_parameter_version(self):
        proposal = propose_exit_feedback_policy(
            feedback_rows(30),
            None,
            min_samples=30,
            min_months=3,
        )

        self.assertEqual(proposal["action"], "hold")
        self.assertFalse(proposal["persist"])
        self.assertTrue(proposal["record_evaluation"])

    def test_latest_hold_evaluation_advances_cooldown_checkpoint(self):
        proposal = propose_exit_feedback_policy(
            feedback_rows(40),
            {
                "version": 1,
                "observation_count": 30,
                "parameters": EXIT_FEEDBACK_DEFAULT_PARAMETERS,
            },
            last_evaluation_count=40,
            min_samples=30,
            min_months=3,
            cooldown_samples=10,
        )

        self.assertEqual(proposal["action"], "cooldown")
        self.assertFalse(proposal["record_evaluation"])

    def test_algorithm_upgrade_creates_one_baseline_version_without_grid_move(self):
        proposal = propose_exit_feedback_policy(
            feedback_rows(40),
            {
                "version": 3,
                "algorithm_version": "niuone-exit-feedback-v1",
                "observation_count": 30,
                "parameters": EXIT_FEEDBACK_DEFAULT_PARAMETERS,
            },
            min_samples=30,
            min_months=3,
            cooldown_samples=10,
        )

        self.assertEqual(proposal["action"], "algorithm_upgrade")
        self.assertTrue(proposal["persist"])
        self.assertEqual(proposal["parameters"], EXIT_FEEDBACK_DEFAULT_PARAMETERS)

    def test_next_grid_move_uses_current_version_cohort_not_old_regime(self):
        old_rows = feedback_rows(
            30,
            sell_fly=1,
            close_return_pct=5.0,
            policy_version=1,
        )
        current_rows = feedback_rows(15, policy_version=2)
        for index, row in enumerate(current_rows, start=30):
            row["trade_key"] = f"trade-{index}"
            row["code"] = f"{600000 + index:06d}"

        proposal = propose_exit_feedback_policy(
            [*old_rows, *current_rows],
            {
                "version": 2,
                "algorithm_version": "niuone-exit-feedback-v2",
                "action": "algorithm_upgrade",
                "observation_count": 30,
                "parameters": EXIT_FEEDBACK_DEFAULT_PARAMETERS,
                "previous_parameters": EXIT_FEEDBACK_DEFAULT_PARAMETERS,
                "baseline_metrics": {},
            },
            min_samples=30,
            min_months=3,
            cooldown_samples=10,
        )

        self.assertEqual(proposal["action"], "hold")
        self.assertFalse(proposal["persist"])
        self.assertEqual(proposal["metrics"]["soft_exit_count"], 15)

    def test_capital_weighted_metrics_follow_economic_impact(self):
        rows = feedback_rows(2)
        rows[0].update({"close_return_pct": 10.0, "sell_notional": 100000.0})
        rows[1].update({"close_return_pct": -10.0, "sell_notional": 10000.0})

        metrics = observation_metrics(rows)

        self.assertAlmostEqual(
            metrics["soft_opportunity_return"]["mean"],
            8.181818,
            places=5,
        )

    def test_concentrated_capital_does_not_satisfy_component_sample_gate(self):
        rows = feedback_rows(30, sell_fly=1, close_return_pct=5.0)
        rows[0]["sell_notional"] = 1_000_000.0
        for row in rows[1:]:
            row["sell_notional"] = 1.0

        proposal = propose_exit_feedback_policy(
            rows,
            None,
            min_samples=30,
            min_months=3,
        )

        self.assertEqual(proposal["action"], "hold")
        self.assertFalse(proposal["persist"])
        self.assertLess(
            proposal["metrics"]["soft_opportunity_return"]["effective_count"],
            15,
        )

    def test_reentry_parameters_require_direct_shadow_outcomes(self):
        exit_rows = feedback_rows(30, exit_rule="other_exit")
        reentry_rows = []
        for index in range(15):
            month = 1 + index % 3
            day = 1 + index // 3
            reentry_rows.append({
                "audit_key": f"audit-{index}",
                "observed_at": f"2026-{month:02d}-{day:02d} 10:00:00",
                "code": f"{600100 + index:06d}",
                "price_basis": "actual_execution",
                "feedback_policy_version": 0,
                "eligible": 0,
                "executed": 0,
                "reclaim_passed": 1,
                "volume_supportive": 0,
                "thesis_valid": 1,
                "future_return_pct": 5.0,
                "completed": 1,
            })

        proposal = propose_exit_feedback_policy(
            exit_rows,
            None,
            reentry_rows=reentry_rows,
            min_samples=30,
            min_months=3,
        )

        self.assertEqual(proposal["action"], "loosen_reentry:reentry_volume_ratio")
        self.assertEqual(proposal["parameters"]["reentry_volume_ratio"], 0.9)


if __name__ == "__main__":
    unittest.main()
