#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPLAY_UTILS_PATH = ROOT / "web" / "src" / "utils" / "todayCandidatesDisplay.js"
PANEL_PATH = ROOT / "web" / "src" / "components" / "TodayCandidatesPanel.vue"
CHART_PATH = ROOT / "web" / "src" / "components" / "candidates" / "CandidateIntradayChart.vue"
DATA_PATH = ROOT / "web" / "src" / "composables" / "useTodayCandidatesData.js"
SPARKLINE_PATH = ROOT / "web" / "src" / "components" / "indices" / "IndexSparkline.vue"


class TodayCandidatesFrontendTests(unittest.TestCase):
    def test_panel_uses_a_compact_theme_aware_information_toolbar(self) -> None:
        source = PANEL_PATH.read_text(encoding="utf-8")

        self.assertIn('class="today-candidates-page mainline-page"', source)
        self.assertIn('class="today-candidates-toolbar theme-ranking-panel"', source)
        self.assertIn('class="today-candidates-context"', source)
        self.assertIn('当前 {{ currentCandidateCount }}只达标', source)
        self.assertIn('今日累计 {{ candidateCount }}只曾达标', source)
        self.assertIn('class="today-candidates-controls"', source)
        self.assertIn('id="todayCandidatesTitle" class="visually-hidden"', source)
        self.assertIn(':global(html[data-theme="dark"] .today-candidates-toolbar)', source)
        self.assertIn(':global(html[data-theme="tongdaxin"] .today-candidates-page)', source)
        self.assertNotIn('>候选列表<', source)
        self.assertNotIn('>达标轨迹<', source)
        self.assertNotIn('type="search"', source)
        self.assertNotIn('v-model="query"', source)
        self.assertNotIn('today-candidate-history', source)
        self.assertNotIn('class="today-candidates-summary mainline-summary-grid"', source)
        self.assertNotIn('radial-gradient', source)

        data = DATA_PATH.read_text(encoding="utf-8")
        self.assertIn("niuniu-dashboard-today-candidates-v2", data)
        self.assertIn("currentCount: 0", data)
        self.assertIn("payload?.current_count", data)

    def test_each_candidate_loads_a_theme_aware_intraday_chart_from_one_batch_endpoint(self) -> None:
        panel = PANEL_PATH.read_text(encoding="utf-8")
        chart = CHART_PATH.read_text(encoding="utf-8")
        data = DATA_PATH.read_text(encoding="utf-8")
        sparkline = SPARKLINE_PATH.read_text(encoding="utf-8")

        self.assertIn('<CandidateIntradayChart', panel)
        self.assertIn(':series="state.intradayByCode[item.code]"', panel)
        self.assertIn("fetch('/api/today_candidates/intraday'", data)
        self.assertIn("intradayByCode", data)
        self.assertIn("var(--red)", chart)
        self.assertIn("var(--green)", chart)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", chart)
        for label in ("最佳评分", "策略综合评价", "预期收益", "预期损失", "首次止盈", "含跳空与费用缓冲"):
            self.assertIn(label, chart)
        for obsolete_label in ("成交额", "全市分位", "距EMA20", "距BBI", "个股强度", "主线强度"):
            self.assertNotIn(obsolete_label, chart)
        self.assertIn("props.item.best_score ?? props.item.score", chart)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", chart)
        self.assertIn("todayCandidateExpectedOutcome", chart)
        self.assertIn("qualification_transitions", chart)
        self.assertIn("const qualificationGuides = computed", chart)
        self.assertIn("qualificationProgress", chart)
        self.assertIn("markers: []", chart)
        self.assertIn('class="sparkline-marker-dot"', sparkline)
        self.assertIn('class="sparkline-marker-halo"', sparkline)
        self.assertIn('rx="1.7"', sparkline)
        self.assertIn('ry="4.2"', sparkline)
        self.assertIn("markerY - 7", sparkline)
        self.assertIn("markerY + 7", sparkline)
        self.assertIn("marker?.kind === 'missed'", sparkline)
        self.assertIn('class="candidate-intraday-guides"', chart)
        self.assertIn('class="candidate-intraday-guide"', chart)
        self.assertIn("shortLabel: qualified ? '达标' : '未达标'", chart)
        self.assertIn("金黄色竖线标注达标、蓝紫色竖线标注未达标", chart)
        self.assertIn(".candidate-intraday-guide::before", chart)
        self.assertIn("left: -.5px", chart)
        self.assertRegex(chart, r"(?s)\.candidate-intraday-guide::before\s*\{[^}]*opacity:\s*\.72[^}]*width:\s*1px")
        self.assertNotIn(".candidate-intraday-guide::after", chart)
        self.assertIn(".candidate-intraday-guide.missed span", chart)
        self.assertIn("color-mix(in srgb, var(--yellow) 22%, var(--panel))", chart)
        self.assertIn("guide.shortLabel", chart)
        self.assertNotIn('class="candidate-intraday-marker-key"', chart)
        self.assertIn("var(--sparkline-qualified-marker-stroke, var(--text))", sparkline)
        self.assertIn("var(--sparkline-missed-marker-stroke, currentColor)", sparkline)
        self.assertIn("重新达标", chart)
        self.assertIn("转为未达标", chart)
        self.assertNotIn('<strong>今日分时</strong>', chart)
        self.assertIn('html[data-theme="tongdaxin"] .candidate-intraday', chart)
        self.assertIn("marketType === 'a_index' || marketType === 'a_stock'", sparkline)
        self.assertIn("!['a_index', 'a_stock', 'us_index'].includes(marketType)", sparkline)

    def test_mobile_layout_keeps_controls_and_candidate_data_compact(self) -> None:
        panel = PANEL_PATH.read_text(encoding="utf-8")
        chart = CHART_PATH.read_text(encoding="utf-8")

        self.assertIn("@media (max-width: 760px)", panel)
        self.assertIn("grid-template-areas: 'strategies sort'", panel)
        self.assertIn("'strategies'\n      'sort'", panel)
        self.assertIn(".today-candidates-strategy-count", panel)
        self.assertIn(".today-candidates-context-primary", panel)
        self.assertIn(".today-candidates-context-secondary", panel)
        self.assertIn("today-candidates-strategy-option-label-compact", panel)
        self.assertIn("flex-direction: row", panel)
        self.assertIn("overflow-x: auto", panel)
        self.assertIn("grid-template-areas: 'primary industry'", panel)
        self.assertIn("'primary'\n      'industry'", panel)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto", panel)
        self.assertIn("justify-self: end", panel)
        self.assertIn("max-width: 52vw", panel)
        self.assertIn("@media (max-width: 360px)", panel)
        self.assertIn("candidate-strategy-label-compact", panel)
        self.assertIn("candidate-context-label-compact", panel)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", chart)
        self.assertIn("grid-template-columns: minmax(90px, 1fr) 58px", chart)

    def test_filters_searches_and_sorts_today_candidates(self) -> None:
        scenario = f"""
const module = await import({json.dumps(DISPLAY_UTILS_PATH.as_uri())});
const items = [
  {{code: '000001', name: '平安银行', best_strategy: 'leader', best_score: 8.8, qualified_count: 2, signal_theme: '金融科技', industry: '银行', last_qualified_at: '2026-08-28 14:30:00'}},
  {{code: '300001', name: '特锐德', best_strategy: 'probe', best_score: 9.2, qualified_count: 1, signal_theme: '充电桩', industry: '电力设备', last_qualified_at: '2026-08-28 14:00:00'}},
  {{code: '600001', name: '邯郸钢铁', best_strategy: 'leader', best_score: 8.5, qualified_count: 4, signal_theme: '钢铁', industry: '钢铁', last_qualified_at: '2026-08-28 15:00:00'}},
];
const meta = {{leader: {{label: '领涨'}}, probe: {{label: '试仓'}}}};
const codes = (options) => module.filterAndSortTodayCandidates(items, options, meta).map(item => item.code);
console.log(JSON.stringify({{
  options: module.todayCandidateStrategyOptions(items, meta),
  searched: codes({{query: '金融科技'}}),
  strategy: codes({{strategy: 'leader'}}),
  score: codes({{sortBy: 'score'}}),
  recent: codes({{sortBy: 'recent'}}),
  frequency: codes({{sortBy: 'frequency'}}),
}}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", scenario],
            cwd=ROOT / "web",
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(
            json.loads(result.stdout),
            {
                "options": [
                    {"key": "all", "count": 3, "label": "全部"},
                    {"key": "leader", "count": 2, "label": "领涨"},
                    {"key": "probe", "count": 1, "label": "试仓"},
                ],
                "searched": ["000001"],
                "strategy": ["000001", "600001"],
                "score": ["300001", "000001", "600001"],
                "recent": ["600001", "000001", "300001"],
                "frequency": ["600001", "000001", "300001"],
            },
        )

    def test_expected_outcome_uses_strategy_target_and_effective_loss(self) -> None:
        scenario = f"""
const module = await import({json.dumps(DISPLAY_UTILS_PATH.as_uri())});
console.log(JSON.stringify({{
  earlyProbe: module.todayCandidateExpectedOutcome({{
    best_strategy: 'niu_reversal_probe', market_regime: 'offensive',
    price: 10, stop_price: 9, stop_distance_pct: 10,
    effective_loss_distance_pct: 12,
  }}),
  matureNiu: module.todayCandidateExpectedOutcome({{
    best_strategy: 'niu_leader', price: 10, stop_price: 9,
  }}),
  tide: module.todayCandidateExpectedOutcome({{
    best_strategy: 'tide_leader', price: 10, stop_price: 9,
  }}),
}}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", scenario],
            cwd=ROOT / "web",
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["earlyProbe"], {
            "expectedReturnPct": 7.5,
            "expectedLossPct": 12,
            "targetPrice": 10.75,
            "targetR": 0.75,
        })
        self.assertEqual(payload["matureNiu"], {
            "expectedReturnPct": 10,
            "expectedLossPct": 10,
            "targetPrice": 11,
            "targetR": 1,
        })
        self.assertEqual(payload["tide"], {
            "expectedReturnPct": 20,
            "expectedLossPct": 10,
            "targetPrice": 12,
            "targetR": 2,
        })


if __name__ == "__main__":
    unittest.main()
