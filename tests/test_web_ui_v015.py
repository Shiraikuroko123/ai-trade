from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WebUiV015Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = (ROOT / "src/ai_trade/web/assets/app.js").read_text(
            encoding="utf-8"
        )
        cls.css = (ROOT / "src/ai_trade/web/assets/app.css").read_text(
            encoding="utf-8"
        )

    def test_new_evidence_surfaces_are_separate_and_read_only(self):
        for endpoint in (
            "/api/fundamentals",
            "/api/disclosures",
            "/api/order-book",
        ):
            self.assertIn(endpoint, self.javascript)
        for form_id in (
            "fundamentals-filter-form",
            "disclosures-filter-form",
            "order-book-filter-form",
        ):
            self.assertIn(f'id="{form_id}"', self.javascript)
        for target in (
            "disclosures-evidence",
            "fundamentals-evidence",
            "order-book-evidence",
        ):
            self.assertIn(f'data-intelligence-jump="{target}"', self.javascript)
        self.assertIn("官方披露证据", self.javascript)
        self.assertIn("第三方新闻与公告", self.javascript)
        self.assertIn("不属于官方披露证据", self.javascript)
        self.assertIn("不自动生成情绪分数", self.javascript)

    def test_valuation_and_depth_labels_do_not_overclaim(self):
        self.assertIn("当前估值与历史分位", self.javascript)
        self.assertIn("历史 PE 分位", self.javascript)
        self.assertIn("现金流估值分位", self.javascript)
        self.assertNotIn("PE / PB / 现金流分位均未接入", self.javascript)
        self.assertIn("至少 120 个正值有限样本", self.javascript)
        self.assertIn("Level-1 五档盘口", self.javascript)
        self.assertIn("不宣称 Tick 或 Level-2", self.javascript)
        self.assertIn("原始手数按 100 股换算", self.javascript)
        self.assertIn("交易授权", self.javascript)

    def test_new_tables_are_stable_and_responsive(self):
        for selector in (
            ".fundamentals-table",
            ".disclosures-table",
            ".order-book-table",
            ".fundamentals-filter-form",
            ".disclosures-filter-form",
            ".order-book-filter-form",
        ):
            self.assertIn(selector, self.css)
        mobile = self.css[self.css.index("@media (max-width: 820px)") :]
        for selector in (
            ".fundamentals-filter-form",
            ".disclosures-filter-form",
            ".order-book-filter-form",
        ):
            self.assertIn(selector, mobile)


if __name__ == "__main__":
    unittest.main()
