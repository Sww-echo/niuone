from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER_PATH = ROOT / "web" / "src" / "router.js"
TABS_PATH = ROOT / "web" / "src" / "composables" / "useDashboardTabs.js"
DASHBOARD_PATH = ROOT / "web" / "src" / "components" / "DashboardPage.vue"
PANEL_PATH = ROOT / "web" / "src" / "components" / "TechnicalAnalysisPanel.vue"
COMPOSABLE_PATH = ROOT / "web" / "src" / "composables" / "useTechnicalAnalysis.js"
CHART_PATH = ROOT / "web" / "src" / "components" / "technical-analysis" / "CandlestickChart.vue"


class TechnicalAnalysisFrontendTests(unittest.TestCase):
    def normalize_minute(self, payload: object) -> dict[str, object]:
        scenario = f"""
import {{ normalizeMinutePayload }} from {json.dumps(COMPOSABLE_PATH.as_uri())};
process.stdout.write(JSON.stringify(normalizeMinutePayload({json.dumps(payload, ensure_ascii=False)})));
"""
        output = subprocess.check_output(
            ["node", "--input-type=module", "-e", scenario],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
        )
        return json.loads(output)

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

        for key in ("\u8d8b\u52bf", "\u91cf\u4ef7", "\u5f62\u6001", "\u7a81\u7834", "CAN_SLIM", "\u7f20\u8bba"):
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", composable)

    def test_panel_presents_six_dimensions_and_discloses_minute_ohlc_derivation(self) -> None:
        panel = PANEL_PATH.read_text(encoding="utf-8")
        composable = COMPOSABLE_PATH.read_text(encoding="utf-8")

        self.assertIn("\u516d\u7ef4\u6280\u672f\u8bc4\u5206", panel)
        self.assertIn("{ key: 'chanlun', label: '\u7f20\u8bba' }", composable)
        self.assertIn("/api/technical-analysis/minute", composable)
        self.assertIn("\u8fde\u7eed 1 \u5206\u949f\u4ee3\u8868\u4ef7\u805a\u5408", panel)
        self.assertIn("\u5e76\u975e\u4e0a\u6e38\u539f\u751f 5 \u5206\u949f OHLC K \u7ebf", panel)

    def test_chart_declares_chanlun_structure_overlays(self) -> None:
        chart = CHART_PATH.read_text(encoding="utf-8")
        panel = PANEL_PATH.read_text(encoding="utf-8")

        self.assertIn("technical-chanlun-strokes", chart)
        self.assertIn("technical-chanlun-fractals", chart)
        self.assertIn("technical-chanlun-signals", chart)
        self.assertIn(':chanlun="analysis.signal.chanlun || {}"', panel)

    def test_minute_normalizer_preserves_backend_counts_and_unavailable_state(self) -> None:
        normalized = self.normalize_minute({
            "symbol": "600000",
            "pre_close": 0,
            "times": ["09:30"],
            "prices": [10],
            "avg_prices": [10],
            "volumes": [0],
            "data_quality": {
                "source": "eastmoney_one_minute_points",
                "source_interval": "1m",
                "point_count": 1,
            },
            "analysis": {
                "available": False,
                "reason": "minute_insufficient",
                "source_interval": "1m",
                "output_interval": "5m",
                "point_count": 1,
                "derived_bar_count": 1,
                "fractal_count": 0,
                "stroke_count": 0,
                "zhongshu_count": 0,
                "counts": {"signals": 0},
                "fractals": [],
                "strokes": [],
                "zhongshus": [],
                "signals": [],
                "ohlc_note": "派生 OHLC 口径",
                "data_quality": {"degraded": True, "real_ohlc_available": False},
            },
        })

        analysis = normalized["analysis"]
        self.assertIsInstance(analysis, dict)
        self.assertFalse(analysis["available"])
        self.assertEqual(analysis["reason"], "minute_insufficient")
        self.assertEqual(analysis["sourceInterval"], "1m")
        self.assertEqual(analysis["outputInterval"], "5m")
        self.assertEqual(
            [
                analysis["pointCount"], analysis["derivedBarCount"],
                analysis["fractalCount"], analysis["strokeCount"],
                analysis["zhongshuCount"], analysis["signalCount"],
            ],
            [1, 1, 0, 0, 0, 0],
        )
        self.assertEqual(analysis["ohlcNote"], "派生 OHLC 口径")
        self.assertEqual(normalized["preClose"], 0)
        self.assertEqual(normalized["volumes"], [0])

    def test_minute_normalizer_does_not_turn_an_empty_response_into_available_data(self) -> None:
        normalized = self.normalize_minute({})

        self.assertIsNone(normalized["analysis"])

    def test_minute_normalizer_falls_back_to_arrays_and_market_point_count(self) -> None:
        normalized = self.normalize_minute({
            "data_quality": {"point_count": 7, "source_interval": "1m"},
            "analysis": {
                "available": False,
                "reason": "minute_analysis_not_available",
                "summary": "分时缠论模块暂不可用",
                "fractals": [{"type": "top"}],
                "strokes": [{"direction": "up"}],
                "zhongshus": [{"zg": 10.2}],
                "counts": {"signals": 3},
            },
        })

        analysis = normalized["analysis"]
        self.assertEqual(analysis["pointCount"], 7)
        self.assertIsNone(analysis["derivedBarCount"])
        self.assertEqual(
            [
                analysis["fractalCount"], analysis["strokeCount"],
                analysis["zhongshuCount"], analysis["signalCount"],
            ],
            [1, 1, 1, 3],
        )

    def test_minute_panel_shows_insufficient_summary_and_quality_warnings(self) -> None:
        panel = PANEL_PATH.read_text(encoding="utf-8")

        self.assertIn("minuteResult.summary || minuteResult.currentState", panel)
        self.assertIn("minuteWarnings.length", panel)
        self.assertIn("activeMode.value = 'analysis'", panel)

    def test_scan_ui_declares_delete_cancellation_contract(self) -> None:
        panel = PANEL_PATH.read_text(encoding="utf-8")
        composable = COMPOSABLE_PATH.read_text(encoding="utf-8")

        self.assertIn("async function cancelScan", composable)
        self.assertIn("method: 'DELETE'", composable)
        self.assertIn("scan.cancelling", composable)
        self.assertIn("cancelScan(period)", panel)
        self.assertIn("取消扫描", panel)

    def test_scan_cancellation_calls_delete_and_applies_terminal_state(self) -> None:
        job_id = "a" * 32
        scenario = f"""
console.warn = () => {{}};
globalThis.window = {{ setTimeout: () => 1, clearTimeout: () => {{}} }};
const calls = [];
globalThis.fetch = async (url, options = {{}}) => {{
  calls.push({{ url: String(url), method: options.method || 'GET' }});
  const payload = options.method === 'DELETE'
    ? {{ job_id: {json.dumps(job_id)}, status: 'cancelled', stage: '扫描已取消', progress: 12, scanned: 3, total: 25, results: [] }}
    : {{ job_id: {json.dumps(job_id)}, status: 'queued', stage: '等待扫描', progress: 0, scanned: 0, total: 25, results: [] }};
  return {{ ok: true, status: 200, text: async () => JSON.stringify(payload) }};
}};
const {{ useTechnicalAnalysis }} = await import({json.dumps(COMPOSABLE_PATH.as_uri())});
const technical = useTechnicalAnalysis();
await technical.startScan('day');
const activeBeforeCancel = technical.scanActive.value;
await technical.cancelScan('day');
process.stdout.write(JSON.stringify({{
  calls,
  activeBeforeCancel,
  activeAfterCancel: technical.scanActive.value,
  status: technical.scan.status,
  stage: technical.scan.stage,
  error: technical.scan.error,
  cancelling: technical.scan.cancelling,
}}));
"""
        output = subprocess.check_output(
            ["node", "--input-type=module", "-e", scenario],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(
            json.loads(output),
            {
                "calls": [
                    {"url": "/api/technical-analysis/scans", "method": "POST"},
                    {
                        "url": f"/api/technical-analysis/scans/{job_id}",
                        "method": "DELETE",
                    },
                ],
                "activeBeforeCancel": True,
                "activeAfterCancel": False,
                "status": "cancelled",
                "stage": "扫描已取消",
                "error": "本次扫描已取消",
                "cancelling": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
