#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / "web" / "src" / "utils" / "realtimeNewsDisplay.js"
COMPOSABLE = ROOT / "web" / "src" / "composables" / "useRealtimeNewsData.js"


class RealtimeNewsFrontendTests(unittest.TestCase):
    def test_display_helpers_filter_sources_and_important_items(self):
        scenario = f"""
const module = await import({json.dumps(UTILS.as_uri())} + '?realtime-news-test=1');
const items = [
  {{id:'1', source_id:'jin10', important:true}},
  {{id:'2', source_id:'cls-telegraph', important:false}},
];
const sources = [
  {{id:'cls-telegraph', label:'财联社电报'}},
  {{id:'jin10', label:'金十数据', stale:true}},
];
console.log(JSON.stringify({{
  all: module.filterRealtimeNews(items).map(item => item.id),
  important: module.filterRealtimeNews(items, 'all', true).map(item => item.id),
  jin10: module.filterRealtimeNews(items, 'jin10').map(item => item.id),
  options: module.realtimeNewsSourceOptions(sources, items),
  blocked: module.realtimeNewsErrorText('http_403'),
  partial: module.realtimeNewsStatusText('partial'),
  rankingClock: module.realtimeNewsClock({{rank: 3}}),
  rankingDate: module.realtimeNewsDate({{rank: 3}}),
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
        self.assertEqual(payload["all"], ["1", "2"])
        self.assertEqual(payload["important"], ["1"])
        self.assertEqual(payload["jin10"], ["1"])
        self.assertEqual(payload["options"][0], {"id": "all", "label": "全部", "count": 2})
        self.assertTrue(payload["options"][2]["stale"])
        self.assertEqual(payload["blocked"], "NewsNow 拒绝了当前请求")
        self.assertEqual(payload["partial"], "部分来源可用")
        self.assertEqual(payload["rankingClock"], "榜单")
        self.assertEqual(payload["rankingDate"], "#03")

    def test_dashboard_registers_realtime_news_route_and_lazy_panel(self):
        router = (ROOT / "web" / "src" / "router.js").read_text(encoding="utf-8")
        tabs = (ROOT / "web" / "src" / "composables" / "useDashboardTabs.js").read_text(encoding="utf-8")
        page = (ROOT / "web" / "src" / "components" / "DashboardPage.vue").read_text(encoding="utf-8")
        panel = (ROOT / "web" / "src" / "components" / "RealtimeNewsPanel.vue").read_text(encoding="utf-8")
        dashboard_css = (ROOT / "frontend" / "dashboard.css").read_text(encoding="utf-8")

        self.assertIn("'/realtime-news'", router)
        self.assertIn("realtime_news: '财经快讯'", tabs)
        self.assertIn("import('./RealtimeNewsPanel.vue')", page)
        self.assertIn("聚合设置中选定的 NewsNow 来源", panel)
        self.assertIn("每 30 秒检查一次", panel)
        self.assertIn('<h2>财经快讯</h2>', panel)
        self.assertIn('MARKET FLASH', panel)
        self.assertIn('rel="noopener noreferrer"', panel)
        self.assertIn('class="realtime-news-table-head"', panel)
        self.assertIn('class="realtime-news-meta-cell"', panel)
        self.assertIn('class="realtime-news-source-cell"', panel)
        self.assertIn('class="realtime-news-title-row"', panel)
        self.assertIn('<span>来源 / 时间</span>', panel)
        self.assertIn('grid-template-columns:180px minmax(0,1fr)', dashboard_css)
        self.assertIn(
            'html:not([data-theme="tongdaxin"]) .realtime-news-title-row > a',
            dashboard_css,
        )
        self.assertIn(
            '.realtime-news-item.important .realtime-news-title-row > a',
            dashboard_css,
        )
        self.assertNotIn('.realtime-news-list::before', dashboard_css)
        self.assertNotIn('.realtime-news-marker', dashboard_css)

        admin_input = (ROOT / "web" / "src" / "components" / "AdminEnvInput.vue").read_text(
            encoding="utf-8",
        )
        admin_css = (ROOT / "frontend" / "admin.css").read_text(encoding="utf-8")
        self.assertIn("kind === 'news_sources'", admin_input)
        self.assertIn('boolNoDefault.value ? String(props.item.default', admin_input)
        self.assertIn('data-news-source-picker', admin_input)
        self.assertIn('placeholder="搜索来源名称"', admin_input)
        self.assertNotIn('placeholder="搜索来源名称或 ID"', admin_input)
        self.assertNotIn('{{ option.id }} · 上游约', admin_input)
        self.assertNotIn('--news-source-color', admin_input)
        self.assertIn('上游约 {{ newsSourceIntervalText(option.interval_seconds) }}更新', admin_input)
        self.assertIn('已选择 <b>{{ newsSourceValues.length }}</b>', admin_input)
        self.assertIn('v-model="newsSourceValues"', admin_input)
        self.assertNotIn('<strong>{{ group.label }}</strong>', admin_input)
        self.assertNotIn('{{ group.options.length }} 项', admin_input)
        self.assertIn('.news-source-groups', admin_css)
        self.assertNotIn('scrollbar-gutter:stable', admin_css)
        self.assertIn('.news-source-option:focus-within', admin_css)
        self.assertIn('background:var(--surface)', admin_css)
        self.assertIn('.news-source-option.selected{background:var(--accent-soft)}', admin_css)
        self.assertNotIn(
            '.news-source-option.selected{background:var(--accent-soft);box-shadow',
            admin_css,
        )
        self.assertNotIn('.news-source-option-copy strong::before', admin_css)

        tongdaxin_css = (ROOT / "frontend" / "tongdaxin-theme.css").read_text(encoding="utf-8")
        self.assertIn(
            'html[data-theme="tongdaxin"]:root .news-source-option.selected',
            tongdaxin_css,
        )
        self.assertIn(
            'html[data-theme="tongdaxin"]:root .realtime-news-table-head',
            tongdaxin_css,
        )
        self.assertIn(
            'html[data-theme="tongdaxin"]:root .overview-right-bottom > .overview-news-panel',
            tongdaxin_css,
        )
        self.assertIn('grid-template-columns:112px minmax(0,1fr)', tongdaxin_css)
        self.assertIn(
            '.realtime-news-item.important .realtime-news-title-row > a',
            tongdaxin_css,
        )
        self.assertIn('background:var(--terminal-selection)', tongdaxin_css)
        self.assertNotIn('box-shadow:inset 2px 0 0 #fff200', tongdaxin_css)

    def test_shared_feed_preserves_overview_filter_setting_in_cache(self):
        composable = COMPOSABLE.read_text(encoding="utf-8")

        self.assertIn("overviewImportantOnly: true", composable)
        self.assertIn("overviewImportantOnly: state.overviewImportantOnly", composable)
        self.assertIn("payload?.overview_important_only !== false", composable)
        self.assertIn("payload?.overviewImportantOnly !== false", composable)


if __name__ == "__main__":
    unittest.main()
