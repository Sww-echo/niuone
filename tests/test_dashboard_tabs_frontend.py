#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_TABS_PATH = ROOT / "web" / "src" / "composables" / "useDashboardTabs.js"


class DashboardTabsFrontendTests(unittest.TestCase):
    def test_bootstrap_hydrates_lazy_panel_counts_before_panels_mount(self) -> None:
        scenario = f"""
globalThis.window = {{
  location: {{pathname: '/practice', search: ''}},
  setTimeout,
  clearTimeout,
}};
const fetchCalls = [];
globalThis.fetch = async url => {{
  fetchCalls.push(url);
  return {{
    ok: true,
    async json() {{
      return {{
        message_counts: {{
          market_monitor: 6,
        }},
      }};
    }},
  }};
}};
const module = await import(
  {json.dumps(DASHBOARD_TABS_PATH.as_uri())} + '?bootstrap-counts-test=1'
);
const tabs = module.useDashboardTabs();
const initialMarketCount = tabs.items.value
  .find(item => item.key === 'market_monitor')?.count;
await tabs.initializeDashboardTabs();
await tabs.initializeDashboardTabs();
const counts = Object.fromEntries(
  tabs.items.value.map(item => [item.key, item.count])
);
const order = tabs.items.value.map(item => item.key);
console.log(JSON.stringify({{fetchCalls, initialMarketCount, counts, order}}));
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
                "fetchCalls": ["/api/dashboard/bootstrap"],
                "initialMarketCount": "",
                "order": [
                    "overview",
                    "practice",
                    "candidates",
                    "technical_analysis",
                    "watchlist",
                    "niuone_mainline",
                    "indices",
                    "market_monitor",
                    "realtime_news",
                    "dragon_tiger",
                ],
                "counts": {
                    "overview": "",
                    "candidates": "",
                    "practice": "",
                    "watchlist": "",
                    "technical_analysis": "",
                    "niuone_mainline": "",
                    "indices": "",
                    "market_monitor": " · 6",
                    "realtime_news": "",
                    "dragon_tiger": "",
                },
            },
        )

    def test_root_defaults_to_overview_and_preserves_legacy_query_routes(self) -> None:
        scenario = f"""
globalThis.window = {{
  location: {{pathname: '/', search: ''}},
  setTimeout,
  clearTimeout,
}};
const module = await import(
  {json.dumps(DASHBOARD_TABS_PATH.as_uri())} + '?overview-route-test=1'
);
console.log(JSON.stringify({{
  active: module.useDashboardTabs().activeCategory.value,
  root: module.dashboardCategoryFromLocation('/', ''),
  legacy: module.dashboardCategoryFromLocation('/', 'b1_screen'),
  direct: module.dashboardCategoryFromLocation('/indices', 'practice'),
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
                "active": "overview",
                "root": "overview",
                "legacy": "practice",
                "direct": "indices",
            },
        )

    def test_settings_routes_do_not_activate_a_dashboard_tab(self) -> None:
        scenario = f"""
globalThis.window = {{
  location: {{pathname: '/admin/settings/appearance', search: '?category=practice'}},
  setTimeout,
  clearTimeout,
}};
const module = await import(
  {json.dumps(DASHBOARD_TABS_PATH.as_uri())} + '?settings-route-test=1'
);
const tabs = module.useDashboardTabs();
console.log(JSON.stringify({{
  active: tabs.activeCategory.value,
  activeItems: tabs.items.value.filter(item => item.active).map(item => item.key),
  admin: module.dashboardCategoryFromLocation('/admin', ''),
  settings: module.dashboardCategoryFromLocation('/admin/settings/appearance', 'practice'),
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
                "active": "",
                "activeItems": [],
                "admin": "",
                "settings": "",
            },
        )


if __name__ == "__main__":
    unittest.main()
