from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER_PATH = ROOT / "web" / "src" / "router.js"
TABS_PATH = ROOT / "web" / "src" / "composables" / "useDashboardTabs.js"
DASHBOARD_PATH = ROOT / "web" / "src" / "components" / "DashboardPage.vue"
PANEL_PATH = ROOT / "web" / "src" / "components" / "TechnicalAnalysisPanel.vue"
COMPOSABLE_PATH = ROOT / "web" / "src" / "composables" / "useTechnicalAnalysis.js"


class TechnicalAnalysisFrontendTests(unittest.TestCase):
    def test_dashboard_declares_technical_analysis_route_and_tab(self) -> None:
        router = ROUTER_PATH.read_text(encoding="utf-8")
        tabs = TABS_PATH.read_text(encoding="utf-8")
        dashboard = DASHBOARD_PATH.read_text(encoding="utf-8")

        self.assertIn("'/technical-analysis'", router)
        self.assertIn("technical_analysis: '\u6280\u672f\u5206\u6790'", tabs)
        self.assertIn("technical_analysis: '/technical-analysis'", tabs)
        self.assertIn("import('./TechnicalAnalysisPanel.vue')", dashboard)
        self.assertIn("activeCategory === 'technical_analysis'", dashboard)
        self.assertIn("<TechnicalAnalysisPanel />", dashboard)

    def test_panel_declares_single_stock_analysis_contract(self) -> None:
        source = PANEL_PATH.read_text(encoding="utf-8")
        composable = COMPOSABLE_PATH.read_text(encoding="utf-8")
        declarations = f"{source}\n{composable}"

        self.assertIn("/api/technical-analysis/analyze", composable)
        for label in (
            "\u4e2a\u80a1\u6280\u672f\u5206\u6790",
            "\u65e5\u7ebf",
            "\u5468\u7ebf",
            "\u5f00\u59cb\u5206\u6790",
            "\u8d8b\u52bf",
            "\u91cf\u80fd",
            "\u5f62\u6001",
            "\u7a81\u7834",
            "CAN SLIM",
            "\u7f20\u8bba\u7ed3\u6784",
            "\u98ce\u9669\u63d0\u793a",
            "\u4ea4\u6613\u8ba1\u5212",
            "\u6570\u636e\u8d28\u91cf",
        ):
            with self.subTest(label=label):
                self.assertIn(label, declarations)

    def test_panel_declares_background_scan_contract(self) -> None:
        source = PANEL_PATH.read_text(encoding="utf-8")
        composable = COMPOSABLE_PATH.read_text(encoding="utf-8")

        self.assertIn("/api/technical-analysis/scans", composable)
        self.assertIn("/api/technical-analysis/scans/${", composable)
        self.assertIn("\u5168\u5e02\u573a\u626b\u63cf", source)
        self.assertIn("\u5f00\u59cb\u626b\u63cf", source)

    def test_score_cards_accept_the_engine_module_score_keys(self) -> None:
        composable = COMPOSABLE_PATH.read_text(encoding="utf-8")

        for key in ("\u8d8b\u52bf", "\u91cf\u4ef7", "\u5f62\u6001", "\u7a81\u7834", "CAN_SLIM"):
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", composable)


if __name__ == "__main__":
    unittest.main()
