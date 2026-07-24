import re
import unittest
from pathlib import Path


ASSET_ROOT = Path(__file__).resolve().parents[1] / "src" / "ai_trade" / "web" / "assets"


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
        self.assertIn("region.tabIndex = 0;", self.javascript)
        self.assertIn('region.setAttribute("role", "region")', self.javascript)
        self.assertIn(
            'region.setAttribute("aria-describedby", "table-scroll-help")',
            self.javascript,
        )
        self.assertIn("tableRegion.scrollLeft + 96", self.javascript)
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
        self.assertIn("descendant.tabIndex = -1;", self.javascript)
        self.assertIn('aria-describedby="${escapeHtml(id)}-summary"', self.javascript)
        self.assertIn('class="chart-caption"', self.javascript)

    def test_cross_source_provider_fields_are_disclosed(self):
        self.assertIn('yahoo_chart: "Yahoo Finance Chart"', self.javascript)
        self.assertIn("comparisonFields = new Set", self.javascript)
        self.assertIn('amountDeviation = comparisonFields.has("amount")', self.javascript)
        self.assertIn("未核对", self.javascript)
        self.assertIn(
            'reference_provider_has_no_comparable_fields: "参考源没有可比较字段"',
            self.javascript,
        )

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

    def test_overview_and_portfolio_surface_freshness_and_unavailable_valuation(self):
        self.assertIn("app.css?v=0.18.1-call-evidence-binding", self.html)
        self.assertIn("app.js?v=0.18.1-call-evidence-binding", self.html)
        self.assertIn("data.market?.freshness", self.javascript)
        self.assertIn("共同最新", self.javascript)
        self.assertIn("行情估值暂不可用", self.javascript)
        self.assertIn("部分持仓暂未估值", self.javascript)
        self.assertIn("估值口径", self.javascript)
        self.assertIn('aria-label="估值不可用"', self.javascript)
        self.assertIn("账本权益（最近记录）", self.javascript)
        self.assertIn("valuation_available", self.javascript)
        self.assertIn("共同最新行情", self.javascript)
        self.assertIn("marketDecisionDate", self.javascript)

    def test_page_read_time_and_common_date_are_explicit(self):
        self.assertIn('id="view-read-at"', self.html)
        self.assertIn("function updateViewReadAt", self.javascript)
        self.assertIn("页面读取", self.javascript)
        self.assertIn('value: marketDecisionDate || "不可用"', self.javascript)
        self.assertIn('status: !data.signal?.date ? "尚无信号"', self.javascript)

    def test_job_actions_lock_immediately_and_have_specific_names(self):
        self.assertIn("pendingActions: new Set()", self.javascript)
        self.assertIn("state.pendingActions.add(action)", self.javascript)
        self.assertIn("任务日志", self.javascript)
        self.assertIn("summary:focus-visible", self.css)

    def test_market_pulse_is_compact_auditable_and_keyboard_scannable(self):
        self.assertIn('id="market-pulse"', self.html)
        self.assertIn('id="market-pulse-track"', self.html)
        self.assertIn('aria-describedby="market-pulse-help"', self.html)
        self.assertIn("function updateMarketPulse", self.javascript)
        self.assertIn('event.target.closest(".market-pulse-track")', self.javascript)
        self.assertIn("pulseRegion.scrollLeft + step", self.javascript)
        self.assertIn("真实交易锁定", self.javascript)
        self.assertIn(".market-pulse-track:focus-visible", self.css)
        self.assertIn(
            'window.scrollTo({ top: 0, left: 0, behavior: "auto" })', self.javascript
        )

    def test_monitoring_is_persistent_auditable_and_actionable(self):
        self.assertIn('data-route="monitoring"', self.html)
        self.assertIn('monitoring: { title: "监控"', self.javascript)
        self.assertIn('return "/api/monitoring";', self.javascript)
        for endpoint in (
            '"/api/monitoring/watchlist"',
            '"/api/monitoring/rules"',
            '"/api/monitoring/scan"',
            "/api/monitoring/alerts/${encodeURIComponent(alertId)}/actions",
            "/api/monitoring/notifications/${encodeURIComponent(notificationId)}/actions",
        ):
            self.assertIn(endpoint, self.javascript)
        for state_label in (
            "尚未建立监控列表",
            "已有标的但尚未建立规则",
            "尚未运行扫描",
            "扫描部分完成",
            "已扫描，当前没有触发告警",
            "本次监控扫描失败",
        ):
            self.assertIn(state_label, self.javascript)
        for action_label in ("标记已阅", "暂缓处理", "关闭并备注", "重新打开"):
            self.assertIn(action_label, self.javascript)
        self.assertIn('aria-busy="true"', self.javascript)
        self.assertIn('pulseItem("监控"', self.javascript)
        self.assertIn("state.monitoringActionBusy", self.javascript)
        self.assertIn("expected_revision", self.javascript)
        self.assertIn("expected_state_fingerprint", self.javascript)
        self.assertIn(
            "scan.config_revision ?? data.configuration?.revision", self.javascript
        )
        self.assertIn("scan.snapshot_evidence_fingerprint", self.javascript)
        self.assertIn("scan.manifest_sha256", self.javascript)
        self.assertIn('aria-label="本地通知"', self.javascript)
        self.assertIn('id="monitoring-notification-filter-form"', self.javascript)
        self.assertIn('id="monitoring-notifications-region"', self.javascript)
        self.assertIn("本地收件箱只汇总规则告警和扫描失败", self.javascript)
        self.assertIn("标为已读", self.javascript)
        self.assertIn("设为未读", self.javascript)
        self.assertIn("归档通知", self.javascript)
        self.assertIn(
            'restoreFocusAfterRender("#monitoring-notifications-region", "monitoring")',
            self.javascript,
        )

    def test_monitoring_tables_scroll_and_forms_reflow_on_mobile(self):
        self.assertIn(".monitoring-alert-table table", self.css)
        self.assertIn("min-width: 1320px", self.css)
        self.assertIn(".monitoring-notification-table table", self.css)
        self.assertIn("min-width: 1120px", self.css)
        self.assertIn(".monitoring-watchlist-table table", self.css)
        self.assertIn("min-width: 920px", self.css)
        self.assertIn(".monitoring-rule-table table", self.css)
        self.assertIn("min-width: 1120px", self.css)
        mobile_start = self.css.index("@media (max-width: 820px)")
        narrow_start = self.css.index("@media (max-width: 560px)")
        reduced_motion_start = self.css.index("@media (prefers-reduced-motion: reduce)")
        mobile_css = self.css[mobile_start:narrow_start]
        narrow_css = self.css[narrow_start:reduced_motion_start]
        self.assertIn(".monitoring-config-layout", mobile_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", mobile_css)
        self.assertIn(".monitoring-filter-form", narrow_css)
        self.assertIn(".monitoring-notification-filter-form", narrow_css)
        self.assertIn("repeat(6, minmax(156px, 1fr))", narrow_css)

    def test_risk_and_order_semantics_do_not_depend_on_color(self):
        self.assertIn('class="trade-side-code" aria-hidden="true"', self.javascript)
        self.assertIn("BUY", self.javascript)
        self.assertIn("SELL", self.javascript)
        self.assertIn("metric-strip-priority portfolio-risk-strip", self.javascript)
        self.assertIn("metric-strip-priority risk-metric-strip", self.javascript)
        self.assertIn("现金缓冲", self.javascript)

    def test_research_perspectives_show_coverage_and_evidence_boundaries(self):
        self.assertIn("function assistantPerspectivesMarkup", self.javascript)
        self.assertIn("研究视角", self.javascript)
        self.assertIn("缺失数据不会被模型补写", self.javascript)
        self.assertIn("perspective-ledger", self.css)
        self.assertIn("perspective-unavailable", self.css)

    def test_assistant_surfaces_governance_and_call_audit(self):
        self.assertIn("function assistantGovernanceLimits", self.javascript)
        self.assertIn("function assistantModelCallPanel", self.javascript)
        self.assertIn("模型调用审计", self.javascript)
        self.assertIn("Token 上限：单次", self.javascript)
        self.assertIn("每日剩余", self.javascript)
        self.assertIn("命中 · 未重复计费", self.javascript)
        self.assertIn("function assistantCallBindingMarkup", self.javascript)
        self.assertIn("已核验 ${formatInteger(binding.call_count)} 条不可变调用记录", self.javascript)
        self.assertIn("fundamental.weighted_roe_pct", self.javascript)
        self.assertIn("valuation.percentile.", self.javascript)
        self.assertIn("assistant-call-grid", self.css)
        self.assertIn("assistant-call-binding", self.css)

    def test_auditable_debate_is_role_scoped_read_only_and_responsive(self):
        self.assertIn("function assistantDebateAvailable", self.javascript)
        self.assertIn("function assistantAdvocateMarkup", self.javascript)
        self.assertIn("function assistantJudgeMarkup", self.javascript)
        self.assertIn('debate.authority === "research_only"', self.javascript)
        self.assertIn("debate.execution_authorized === false", self.javascript)
        self.assertIn("debate.conclusion_mutation_allowed === false", self.javascript)
        self.assertIn("多空裁判账本", self.javascript)
        self.assertIn("仅整理冲突 · 无结论与订单权限", self.javascript)
        self.assertIn("论点", self.javascript)
        self.assertIn("反证", self.javascript)
        self.assertIn("未决问题", self.javascript)
        self.assertIn("assistant-debate-grid", self.css)
        mobile_start = self.css.index("@media (max-width: 820px)")
        narrow_start = self.css.index("@media (max-width: 560px)")
        reduced_motion_start = self.css.index("@media (prefers-reduced-motion: reduce)")
        self.assertIn(".assistant-debate-grid", self.css[mobile_start:narrow_start])
        self.assertIn(
            ".assistant-debate-authority",
            self.css[narrow_start:reduced_motion_start],
        )

    def test_perspective_conflict_audit_is_textual_auditable_and_responsive(self):
        self.assertIn("function assistantConflictAuditMarkup", self.javascript)
        self.assertIn("function assistantAuditAvailable", self.javascript)
        self.assertIn('audit.authority === "research_only"', self.javascript)
        self.assertIn("audit.execution_authorized === false", self.javascript)
        self.assertIn("视角冲突审计", self.javascript)
        self.assertIn("冲突审计不可用", self.javascript)
        self.assertIn("重新运行一次分析", self.javascript)
        self.assertIn("视角冲突", self.javascript)
        self.assertIn("数据覆盖缺口", self.javascript)
        self.assertIn("模型权限守卫", self.javascript)
        self.assertIn("不是多模型投票", self.javascript)
        self.assertIn("仅研究 · 无执行权限", self.javascript)
        self.assertIn('<ul class="assistant-audit-list">', self.javascript)
        self.assertIn('<dl class="assistant-model-review">', self.javascript)
        self.assertIn("assistant-history-table", self.css)
        self.assertIn("min-width: 980px", self.css)
        mobile_start = self.css.index("@media (max-width: 820px)")
        narrow_start = self.css.index("@media (max-width: 560px)")
        reduced_motion_start = self.css.index("@media (prefers-reduced-motion: reduce)")
        self.assertIn(".assistant-audit-columns", self.css[mobile_start:narrow_start])
        self.assertIn(
            "grid-template-columns: minmax(0, 1fr)", self.css[mobile_start:narrow_start]
        )
        self.assertIn(
            ".assistant-model-review", self.css[narrow_start:reduced_motion_start]
        )

    def test_universe_screen_has_bounded_filters_and_auditable_states(self):
        self.assertIn("/api/universe/screen", self.javascript)
        self.assertIn("universe-filter-form", self.javascript)
        self.assertIn("universe-filter-help", self.javascript)
        self.assertIn("data-universe-reset", self.javascript)
        self.assertIn("data-universe-submit", self.javascript)
        self.assertIn("applyUniverseFilterForm(form)", self.javascript)
        self.assertIn('form.addEventListener("keydown"', self.javascript)
        self.assertIn("screenDataStatusLabel", self.javascript)
        self.assertIn("snapshot_id", self.javascript)
        self.assertIn("filter_fingerprint", self.javascript)
        self.assertIn('"状态未加载"', self.javascript)
        self.assertIn("universe-screen-table", self.css)
        self.assertIn("min-width: 1180px", self.css)

    def test_market_intelligence_is_read_only_auditable_and_responsive(self):
        self.assertIn('data-route="intelligence"', self.html)
        self.assertIn('intelligence: { title: "市场情报"', self.javascript)
        self.assertIn("/api/market-intelligence", self.javascript)
        self.assertIn('"refresh-market-intelligence": "刷新龙虎榜"', self.javascript)
        self.assertIn('id="market-intelligence-filter-form"', self.javascript)
        self.assertIn('aria-label="龙虎榜明细宽表"', self.javascript)
        self.assertIn("尚未固化龙虎榜快照", self.javascript)
        self.assertIn("龙虎榜正在刷新", self.javascript)
        self.assertIn("最近一次刷新失败", self.javascript)
        self.assertIn("该交易日没有龙虎榜记录", self.javascript)
        self.assertIn("非交易所认证", self.javascript)
        self.assertIn("不等同于市场情绪", self.javascript)
        self.assertIn(
            "不能修改策略、持仓、订单、风控门禁或真实交易授权", self.javascript
        )
        self.assertIn("filters.trade_date ?? filters.date", self.javascript)
        self.assertIn("summary.returned_count ?? records.length", self.javascript)
        self.assertIn(
            "coverage.declared_count ?? summary.record_count", self.javascript
        )
        self.assertIn("freshness.completed_session_cutoff", self.javascript)
        self.assertIn("coverage.received_count", self.javascript)
        self.assertIn("summary.buy_amount", self.javascript)
        self.assertIn('status === "provisional"', self.javascript)
        self.assertIn(
            "const orderedRevisions = [...revisions].reverse()", self.javascript
        )
        self.assertNotIn("orderedRevisions.slice", self.javascript)
        self.assertIn("规范化证据相同则复用，记录变化才追加", self.javascript)
        self.assertIn(
            'value === null || value === undefined || value === ""', self.javascript
        )
        self.assertIn("async function applyIntelligenceFilterForm", self.javascript)
        self.assertIn("async function clearIntelligenceFilters", self.javascript)
        self.assertIn(
            "restoreFocusAfterRender('#market-intelligence-filter-form", self.javascript
        )
        self.assertIn(
            'restoreFocusAfterRender("[data-intelligence-filter-clear]"',
            self.javascript,
        )
        self.assertIn('contentStatus === "empty"', self.javascript)
        self.assertIn("该日来源经完整校验返回 0 条记录", self.javascript)
        self.assertIn("尚无已发布证据，不能解释为零值", self.javascript)
        self.assertIn("来源计数不可用", self.javascript)
        self.assertIn("/api/market-breadth", self.javascript)
        self.assertIn('"refresh-market-breadth": "刷新市场宽度"', self.javascript)
        self.assertIn('id="market-breadth-filter-form"', self.javascript)
        self.assertIn('aria-describedby="market-breadth-filter-help"', self.javascript)
        self.assertIn('aria-label="市场宽度摘要"', self.javascript)
        self.assertIn('aria-label="交易所宽度表"', self.javascript)
        self.assertIn('aria-label="板块排名宽表"', self.javascript)
        self.assertIn("市场宽度正在刷新", self.javascript)
        self.assertIn("尚未固化市场宽度快照", self.javascript)
        self.assertIn("当前筛选没有匹配板块", self.javascript)
        self.assertIn("东方财富三基准来源统计", self.javascript)
        self.assertIn("MARKET_BREADTH_SORT_LABELS", self.javascript)
        self.assertIn("Array.isArray(source.breadth_secids)", self.javascript)
        self.assertIn("不代表经许可的纯行业分类", self.javascript)
        self.assertIn("breadthChangeText", self.javascript)
        self.assertIn("上涨", self.javascript)
        self.assertIn("下跌", self.javascript)
        self.assertIn("平盘", self.javascript)
        self.assertIn("async function applyBreadthFilterForm", self.javascript)
        self.assertIn("async function clearBreadthFilters", self.javascript)
        self.assertIn(
            "restoreFocusAfterRender('#market-breadth-filter-form", self.javascript
        )
        self.assertIn(
            'restoreFocusAfterRender("[data-market-breadth-filter-clear]"',
            self.javascript,
        )
        self.assertIn("market-breadth-table", self.css)
        self.assertIn("min-width: 1220px", self.css)
        self.assertIn("market-breadth-dataset > .metric-strip", self.css)
        self.assertIn("/api/capital-flow", self.javascript)
        self.assertIn('"refresh-capital-flow": "刷新资金流"', self.javascript)
        self.assertIn('id="capital-flow-filter-form"', self.javascript)
        self.assertIn('aria-describedby="capital-flow-filter-help"', self.javascript)
        self.assertIn('aria-label="板块资金流摘要"', self.javascript)
        self.assertIn('aria-label="板块资金流宽表"', self.javascript)
        self.assertIn('data-intelligence-jump="capital-flow-evidence"', self.javascript)
        self.assertIn('href="#intelligence" data-intelligence-jump', self.javascript)
        self.assertIn("function jumpToIntelligenceDataset", self.javascript)
        self.assertIn(
            'event.target.closest("[data-intelligence-jump]")', self.javascript
        )
        self.assertIn("target.focus({ preventScroll: true })", self.javascript)
        self.assertIn(
            'target.scrollIntoView({ behavior: "auto", block: "start" })',
            self.javascript,
        )
        self.assertIn('document.querySelector(".topbar")', self.javascript)
        self.assertIn("targetTop - coveredThrough - clearance", self.javascript)
        self.assertIn(".intelligence-dataset:focus-visible", self.css)
        self.assertIn("资金流正在刷新", self.javascript)
        self.assertIn("最近一次资金流刷新失败", self.javascript)
        self.assertIn("尚未固化资金流快照", self.javascript)
        self.assertIn("当前筛选没有匹配板块", self.javascript)
        self.assertIn("板块行求和不能解释为全市场净流入", self.javascript)
        self.assertIn("没有交易所定义或独立跨源校验", self.javascript)
        self.assertIn("资金净额不可用", self.javascript)
        self.assertIn("function capitalFlowErrorText", self.javascript)
        self.assertIn("本机尚未固化资金流快照", self.javascript)
        self.assertIn("已有完整快照不会被覆盖", self.javascript)
        self.assertIn("/api/intraday", self.javascript)
        self.assertIn("/api/valuation", self.javascript)
        self.assertIn("/api/news", self.javascript)
        self.assertIn('id="intraday-filter-form"', self.javascript)
        self.assertIn('id="valuation-filter-form"', self.javascript)
        self.assertIn('id="news-filter-form"', self.javascript)
        self.assertIn("历史 PE 分位", self.javascript)
        self.assertIn("lexicon-v1", self.javascript)
        self.assertIn("provider_reported_f52", self.javascript)
        self.assertIn("本地确定性聚合", self.javascript)
        self.assertIn("monitoringDeliveryStatusLabel", self.javascript)
        self.assertIn("投递失败", self.javascript)
        self.assertIn("intraday-table", self.css)
        self.assertIn("valuation-table", self.css)
        self.assertIn("news-table", self.css)
        for direction_label in ("净流入", "净流出", "净额持平"):
            self.assertIn(direction_label, self.javascript)
        self.assertIn("async function applyCapitalFlowFilterForm", self.javascript)
        self.assertIn("async function clearCapitalFlowFilters", self.javascript)
        self.assertIn(
            "restoreFocusAfterRender('#capital-flow-filter-form", self.javascript
        )
        self.assertIn(
            'restoreFocusAfterRender("[data-capital-flow-filter-clear]"',
            self.javascript,
        )
        self.assertIn("capital-flow-table", self.css)
        self.assertIn("min-width: 1480px", self.css)
        self.assertIn("capital-flow-dataset > .metric-strip", self.css)
        intelligence_dataset_css = self.css.split(".intelligence-dataset {", 1)[
            1
        ].split("}", 1)[0]
        self.assertIn("max-width: 100%", intelligence_dataset_css)
        self.assertIn("scroll-margin-top: 6.5rem", intelligence_dataset_css)
        self.assertIn("scroll-margin-top: 10rem", self.css)
        self.assertIn("min-width: 0", intelligence_dataset_css)
        self.assertNotIn("coverage.reported_count", self.javascript)
        self.assertNotIn("summary.total_buy_amount", self.javascript)
        self.assertIn(".intelligence-table", self.css)
        self.assertIn("min-width: 1380px", self.css)
        mobile_start = self.css.index("@media (max-width: 820px)")
        narrow_start = self.css.index("@media (max-width: 560px)")
        reduced_motion_start = self.css.index("@media (prefers-reduced-motion: reduce)")
        self.assertIn(".intelligence-filter-form", self.css[mobile_start:narrow_start])
        self.assertIn(
            ".intelligence-filter-form", self.css[narrow_start:reduced_motion_start]
        )
        self.assertIn(".breadth-filter-form", self.css[mobile_start:narrow_start])
        self.assertIn(
            ".breadth-filter-form", self.css[narrow_start:reduced_motion_start]
        )
        self.assertIn(".capital-flow-filter-form", self.css[mobile_start:narrow_start])
        self.assertIn(
            ".capital-flow-filter-form", self.css[narrow_start:reduced_motion_start]
        )

    def test_shadow_review_is_a_read_only_auditable_workflow(self):
        self.assertIn('["shadow", "影子复盘"]', self.javascript)
        self.assertIn('id="shadow-import-form"', self.javascript)
        self.assertIn('type="file" accept=".csv,text/csv"', self.javascript)
        self.assertIn("canonical_columns", self.javascript)
        self.assertIn("影子账户只比较导入成交与当前本地模拟账本", self.javascript)
        self.assertIn("不读取券商、不提交或撤销订单", self.javascript)
        self.assertIn("state.shadowImportBusy", self.javascript)
        self.assertIn("arrayBufferToBase64(await file.arrayBuffer())", self.javascript)
        self.assertIn(".shadow-review-table table", self.css)
        self.assertIn("min-width: 920px", self.css)

    def test_broker_lifecycle_exposes_recovery_and_integrity_without_authority(self):
        self.assertIn("function brokerLifecycleMarkup", self.javascript)
        self.assertIn('aria-label="券商订单账本摘要"', self.javascript)
        self.assertIn('id="broker-order-state-title"', self.javascript)
        self.assertIn('id="broker-fill-ledger-title"', self.javascript)
        self.assertIn("撤单期间发生成交", self.javascript)
        self.assertIn("已按券商时间归并且未回退当前状态", self.javascript)
        self.assertIn("BROKER_LEDGER_SCOPE_STATUS_LABELS", self.javascript)
        self.assertIn("证据作用域", self.javascript)
        self.assertIn("按成交号与内容指纹校验", self.javascript)
        self.assertIn("旧格式对账不计入", self.javascript)
        self.assertIn("晚于已完成行情日，暂不计入", self.javascript)
        self.assertIn("新记录校验完整 SHA-256", self.javascript)
        self.assertIn("submission_unconfirmed", self.javascript)
        self.assertIn("提交结果未确认", self.javascript)
        self.assertIn("不要重复提交，也不要删除或改写账本行", self.javascript)
        self.assertIn("账户 ${scope.account_reference", self.javascript)
        self.assertIn("不同适配器、账户、环境或配置意外混用", self.javascript)
        self.assertIn(
            "不会写入沙箱晋级证据、改变策略或解除真实下单门禁", self.javascript
        )
        self.assertIn(".broker-order-table", self.css)
        self.assertIn("min-width: 1120px", self.css)
        self.assertIn("repeat(5, minmax(0, 1fr))", self.css)

    def test_mobile_navigation_and_dense_surfaces_reflow_without_page_overflow(self):
        self.assertRegex(self.css, r"html\s*\{[^}]*min-width:\s*0;[\s\S]*?\}")
        mobile_start = self.css.index("@media (max-width: 820px)")
        narrow_start = self.css.index("@media (max-width: 560px)")
        reduced_motion_start = self.css.index("@media (prefers-reduced-motion: reduce)")
        mobile_css = self.css[mobile_start:narrow_start]
        self.assertIn("bottom: 0", mobile_css)
        self.assertIn("overflow-x: auto", mobile_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", mobile_css)
        narrow_css = self.css[narrow_start:reduced_motion_start]
        self.assertIn(".market-pulse-track", narrow_css)
        self.assertIn(".topbar-tools #job-indicator", narrow_css)

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
