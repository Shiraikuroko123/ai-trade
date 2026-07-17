import re
import unittest
from pathlib import Path


ASSET_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ai_trade"
    / "web"
    / "assets"
)


class WebUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
        cls.css = (ASSET_ROOT / "app.css").read_text(encoding="utf-8")

    def test_status_regions_and_wide_tables_are_keyboard_accessible(self):
        self.assertRegex(
            self.html,
            r'id="job-indicator"[^>]+role="status"[^>]+aria-live="polite"',
        )
        self.assertIn('id="table-scroll-help" class="sr-only"', self.html)
        self.assertIn('region.tabIndex = 0;', self.javascript)
        self.assertIn('region.setAttribute("role", "region")', self.javascript)
        self.assertIn('region.setAttribute("aria-describedby", "table-scroll-help")', self.javascript)
        self.assertIn('tableRegion.scrollLeft + 96', self.javascript)
        self.assertIn(".table-wrap:focus-visible", self.css)

    def test_network_failures_use_actionable_local_copy(self):
        self.assertIn("failed to fetch|networkerror", self.javascript)
        self.assertIn(
            "无法连接本机服务。请确认 AI Trade 服务仍在运行，然后重新加载。",
            self.javascript,
        )
        self.assertIn("已经写入的本地账本、报告和策略记录不会因此改变", self.javascript)

    def test_charts_keep_text_summaries_and_focus_contracts(self):
        self.assertIn('role="img" tabindex="0"', self.javascript)
        self.assertIn('aria-describedby="market-chart-summary"', self.javascript)
        self.assertIn('id="market-chart-summary"', self.javascript)
        self.assertIn('descendant.tabIndex = -1;', self.javascript)
        self.assertIn('aria-describedby="${escapeHtml(id)}-summary"', self.javascript)
        self.assertIn('class="chart-caption"', self.javascript)

    def test_tabs_expose_relationships_and_roving_focus(self):
        for control_id in (
            "strategy-manual-form",
            "strategy-proposal-form",
            "trading-ledger-panel",
        ):
            self.assertIn(f'aria-controls="{control_id}"', self.javascript)
        self.assertIn('role="tabpanel"', self.javascript)
        self.assertIn('["ArrowLeft", "ArrowRight", "Home", "End"]', self.javascript)
        self.assertIn('tabindex="${manual ? 0 : -1}"', self.javascript)

    def test_overview_separates_current_account_from_historical_research(self):
        self.assertIn('"决策日期与可信度"', self.javascript)
        self.assertIn('aria-label="当前模拟账户指标"', self.javascript)
        self.assertIn('aria-label="历史研究指标"', self.javascript)
        self.assertIn("历史指标不参与权限解锁", self.javascript)
        self.assertIn("与当前行情快照分开审阅", self.javascript)

    def test_selected_strategy_state_uses_a_full_boundary(self):
        selected_rule = re.search(
            r'\.strategy-candidate-item\[aria-pressed="true"\]\s*\{([^}]+)\}',
            self.css,
        )
        self.assertIsNotNone(selected_rule)
        self.assertIn("border-color: var(--honey-hover)", selected_rule.group(1))
        self.assertNotIn("box-shadow", selected_rule.group(1))


if __name__ == "__main__":
    unittest.main()
