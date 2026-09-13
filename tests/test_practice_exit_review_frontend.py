#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OVERVIEW_PATH = (
    ROOT / "web" / "src" / "components" / "practice" / "PracticeAccountOverview.vue"
)


class PracticeExitReviewFrontendTests(unittest.TestCase):
    def test_exit_review_groups_status_metrics_and_feedback(self) -> None:
        source = OVERVIEW_PATH.read_text(encoding="utf-8")

        self.assertIn('aria-labelledby="practiceExitReviewTitle"', source)
        self.assertIn('class="practice-exit-review-status"', source)
        self.assertIn("feedbackPolicy.enabled ? feedbackStatusLabel : '未启用'", source)
        self.assertNotIn("feedbackPolicy.version", source)
        self.assertIn('class="practice-exit-review-metrics"', source)
        self.assertIn('class="practice-exit-review-highlights"', source)
        self.assertIn('class="practice-exit-review-details"', source)
        self.assertIn("<summary>调参详情</summary>", source)
        self.assertIn('class="practice-exit-review-parameters"', source)
        self.assertIn("卖出后第 5 个交易日效果跟踪", source)
        self.assertIn('class="practice-exit-review-error"', source)

    def test_mobile_exit_review_stays_compact_and_keeps_details_collapsed(self) -> None:
        source = OVERVIEW_PATH.read_text(encoding="utf-8")
        mobile_styles = source.split("@media (max-width: 720px) {", 1)[1]

        self.assertIn(
            "grid-template-columns: repeat(4, minmax(0, 1fr));",
            source,
        )
        self.assertIn(".practice-exit-review-title small {\n    display: none;", mobile_styles)
        self.assertIn(".practice-exit-review-metric {\n    padding: 4px 3px;", mobile_styles)
        self.assertIn(".practice-exit-review-footer {\n    flex-wrap: wrap;", mobile_styles)
        self.assertNotIn("open class=\"practice-exit-review-details\"", source)


if __name__ == "__main__":
    unittest.main()
