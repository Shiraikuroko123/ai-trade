"use strict";

const ROUTES = {
  overview: { title: "总览", context: "每日收盘复盘" },
  market: { title: "行情", context: "只读 K 线与行情证据" },
  intelligence: { title: "市场情报", context: "可追溯的收盘事件证据" },
  monitoring: { title: "监控", context: "自选列表与收盘告警" },
  research: { title: "研究", context: "历史证据与稳健性" },
  assistant: { title: "AI 分析", context: "收盘 K 线诊断与风险复核" },
  "strategy-lab": { title: "策略实验室", context: "候选版本、验证与模拟晋级" },
  portfolio: { title: "组合", context: "模拟账户与目标权重" },
  trading: { title: "交易", context: "执行记录与权限晋级" },
  risk: { title: "风险", context: "约束、尾部与实盘门禁" },
  universe: { title: "数据", context: "证券主数据与覆盖" },
  storage: { title: "存储", context: "本地缓存与云端预算" },
  system: { title: "系统", context: "任务、报告与本地诊断" },
};

const INSTRUMENT_NAMES = {
  "159915": "创业板ETF",
  "510300": "沪深300ETF",
  "510500": "中证500ETF",
  "511010": "国债ETF",
  "512100": "中证1000ETF",
  "513100": "纳指ETF",
  "513500": "标普500ETF",
  "518880": "黄金ETF",
};

const MARKET_PERIODS = {
  day: { label: "日线", chart: { type: "day", span: 1 } },
  week: { label: "周线", chart: { type: "week", span: 1 } },
  month: { label: "月线", chart: { type: "month", span: 1 } },
};

const MARKET_OVERLAYS = {
  MA: "MA",
  EMA: "EMA",
  BOLL: "BOLL",
  none: "不显示",
};

const MARKET_OSCILLATORS = {
  MACD: "MACD",
  KDJ: "KDJ",
  RSI: "RSI",
  ATR: "ATR",
};

const JOB_LABELS = {
  "refresh-data": "刷新行情",
  backtest: "运行回测",
  "walk-forward": "滚动验证",
  validate: "稳健性验证",
  "paper-init": "初始化模拟账户",
  "paper-run": "运行模拟日",
  "paper-audit": "审计模拟账户",
  "cloud-backup": "备份行情",
  "refresh-market-intelligence": "刷新龙虎榜",
  "refresh-market-breadth": "刷新市场宽度",
  "monitoring-scan": "运行收盘监控",
};

const MONITORING_SEVERITY_LABELS = {
  info: "提示",
  warning: "警告",
  critical: "严重",
};

const MONITORING_STATUS_LABELS = {
  open: "待处理",
  acknowledged: "已阅",
  snoozed: "已暂缓",
  dismissed: "已关闭",
  not_run: "尚未扫描",
  no_rules: "尚无规则",
  succeeded: "扫描完成",
  partial: "部分完成",
  failed: "扫描失败",
};

const MONITORING_ACTION_LABELS = {
  acknowledge: "标记已阅",
  dismiss: "关闭并备注",
  reopen: "重新打开",
  snooze: "暂缓处理",
  unsnooze: "取消暂缓",
};

const CHECK_LABELS = {
  broker_mode_live: "配置明确选择实盘模式",
  adapter_configured: "已选择券商适配器",
  adapter_installed: "券商适配器已安装",
  adapter_live_capable: "适配器声明完整且已验证的实盘能力",
  account_configured: "已绑定券商账户",
  paper_gate_passed: "前向模拟门禁通过",
  paper_configuration_current: "模拟账户配置指纹与当前配置一致",
  sandbox_reconciled: "券商沙箱连续对账通过",
  kill_switch_clear: "紧急停止开关未触发",
  authorization_valid: "人工授权有效且未过期",
  mandate_valid: "授权范围明确且要求逐批人工批准",
  environment_confirmed: "本次进程确认实盘风险",
  ledger_integrity: "模拟账本完整",
  minimum_forward_sessions: "前向交易日达到门槛",
  drawdown_within_limit: "模拟最大回撤未越线",
  positive_forward_sharpe: "前向 Sharpe 为正",
  nonnegative_excess_return: "前向收益不低于基准",
};

const STATUS_LABELS = {
  queued: "等待中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  FILLED: "已成交",
  PARTIALLY_FILLED: "部分成交",
  PENDING_SUBMIT: "待确认提交",
  CANCEL_PENDING: "撤单处理中",
  REJECTED: "已拒绝",
  CANCELLED: "已取消",
  SUBMITTED: "已提交",
  EXPIRED: "已过期",
  normal: "正常",
  paper_evidence: "收集模拟证据",
  sandbox_review: "待沙箱复核",
  sandbox_reconciled: "沙箱已对账",
  live_authorized: "实盘已授权",
  collecting_independent_forward_evidence: "收集独立前向证据",
  eligible_for_broker_sandbox_review: "可进入券商沙箱复核",
};

const BROKER_LIFECYCLE_STATUS_LABELS = {
  EMPTY: "尚无记录",
  VERIFIED: "账本已核验",
  RECOVERED: "已恢复，需复核",
  INTEGRITY_ERROR: "完整性错误",
};

const BROKER_LEDGER_SCOPE_STATUS_LABELS = {
  EMPTY: "尚未建立",
  BOUND: "作用域已绑定",
  UNSCOPED: "旧账本未绑定",
  MISMATCH: "与当前配置不符",
  INVALID: "作用域文件无效",
};

const BROKER_LIFECYCLE_ISSUE_LABELS = {
  order_ledger_invalid: "订单事件账本无法校验；修复或归档损坏文件后重新读取。",
  fill_ledger_invalid: "成交账本无法校验；修复或归档损坏文件后重新读取。",
  lifecycle_invalid: "订单状态序列不合法；需要核对券商回报顺序和不可变字段。",
  duplicate_fill_id: "成交号重复，账本不能据此累计成交数量。",
  conflicting_fill_id: "同一成交号出现不同内容，需要核对券商导出或适配器映射。",
  orphan_fill: "存在找不到对应订单的成交记录。",
  fill_identity_mismatch: "成交记录与订单的券商编号、证券或方向不一致。",
  fill_quantity_mismatch: "成交明细合计与订单最新累计成交量不一致。",
  average_fill_price_mismatch: "成交明细无法复算订单最新平均成交价。",
  history_started_mid_lifecycle: "本地记录从订单中途开始，早期事件不可用。",
  out_of_order_events_recovered: "检测到延迟回报，已按券商时间归并且未回退当前状态。",
  submission_unconfirmed: "提交意图已持久化但尚未收到券商确认；先查询券商订单，再人工收敛，禁止盲目重试。",
  ledger_scope_missing: "旧账本未绑定适配器、账户、环境和配置；只可复核，不可继续写入或作为权限证据。",
  ledger_scope_invalid: "账本作用域损坏或与当前券商配置不一致；继续前必须归档并核对本地证据。",
};

const SHADOW_VERDICT_LABELS = {
  INSUFFICIENT_DATA: "证据不足",
  CONSISTENT_WITH_MODEL: "与模拟执行一致",
  REVIEW_REQUIRED: "需要人工复核",
  INTEGRITY_ERROR: "账本完整性错误",
};

const SHADOW_REASON_LABELS = {
  paper_comparison_unavailable: "当前导入窗口缺少模拟成交基准",
  direction_mismatch: "实际方向与模拟方向相反",
  unexpected_fill: "存在模拟账本未预期的成交",
  missed_model_fill: "存在模拟成交但影子账户未成交",
  quantity_deviation_above_5pct: "成交数量偏差超过 5%",
  adverse_price_deviation_above_25bps: "不利价格偏差超过 25 bp",
  trade_allocation_deviation_above_10pct: "成交分配偏差超过 10%",
};

const state = {
  token: "",
  user: null,
  authEnabled: true,
  actions: [],
  route: validRoute(location.hash.slice(1)) || "overview",
  controller: null,
  data: new Map(),
  charts: new Map(),
  jobs: [],
  jobStates: new Map(),
  pendingActions: new Set(),
  cloudBackupWarning: null,
  tradingTab: "paper",
  shadowImportBusy: false,
  universeDate: "",
  universeFilters: {
    asset_class: "",
    sector: "",
    trend: "any",
    coverage: "all",
    min_average_amount: "",
    max_annual_volatility: "",
    active_only: false,
    sort: "momentum",
    direction: "desc",
    limit: "200",
  },
  assistantResult: null,
  assistantBusy: false,
  strategyLabMode: "manual",
  strategyCandidateId: "",
  strategyActionBusy: false,
  journalFilters: {
    category: "",
    symbol: "",
    query: "",
    limit: "100",
  },
  journalBusy: false,
  journalCorrectionOf: "",
  researchDigestBusy: false,
  researchDigestStatus: "",
  researchDigestStatusKind: "",
  intelligenceFilters: {
    date: "",
    symbol: "",
    market: "",
    query: "",
    limit: "200",
  },
  breadthFilters: {
    date: "",
    query: "",
    sort: "change_pct",
    direction: "desc",
    limit: "200",
  },
  marketSymbol: "510300",
  marketPeriod: "day",
  marketLimit: 240,
  marketOverlay: "MA",
  marketOscillator: "MACD",
  marketChart: null,
  marketIndicatorIds: {},
  marketResizeObserver: null,
  monitoringFilters: {
    watchlist_id: "",
    symbol: "",
    severity: "",
    status: "unresolved",
    limit: "100",
  },
  monitoringBusy: false,
  monitoringRefreshBusy: false,
  monitoringScanError: "",
  monitoringActionBusy: new Set(),
  monitoringActionStatus: "",
  monitoringActionTarget: null,
  resizeTimer: 0,
};

const main = document.getElementById("main-content");
const routeTitle = document.getElementById("route-title");
const routeContext = document.getElementById("route-context");
const marketDate = document.getElementById("market-date");
const viewReadAt = document.getElementById("view-read-at");
const versionLabel = document.getElementById("version-label");
const connectionDot = document.getElementById("connection-dot");
const connectionLabel = document.getElementById("connection-label");
const jobIndicator = document.getElementById("job-indicator");
const signedInUser = document.getElementById("signed-in-user");
const logoutButton = document.getElementById("logout");
const marketPulse = document.getElementById("market-pulse");
const marketPulseTrack = document.getElementById("market-pulse-track");

const moneyFormatter = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  maximumFractionDigits: 2,
});
const numberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 2,
});
const integerFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 0,
});
const compactFormatter = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

function validRoute(value) {
  return Object.prototype.hasOwnProperty.call(ROUTES, value) ? value : "";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function finite(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatMoney(value) {
  const parsed = finite(value);
  return parsed === null ? "—" : moneyFormatter.format(parsed);
}

function formatCompactMoney(value) {
  const parsed = finite(value);
  return parsed === null ? "—" : `¥${compactFormatter.format(parsed)}`;
}

function formatNumber(value, digits = 2) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(parsed);
}

function formatInteger(value) {
  const parsed = finite(value);
  return parsed === null ? "—" : integerFormatter.format(parsed);
}

function formatPercent(value, signed = false) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  const sign = signed && parsed > 0 ? "+" : "";
  return `${sign}${(parsed * 100).toFixed(2)}%`;
}

function formatDate(value, includeTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function updateViewReadAt(payload = null, failure = "") {
  if (!viewReadAt) return;
  if (failure) {
    viewReadAt.textContent = "页面读取失败";
    viewReadAt.removeAttribute("title");
    return;
  }
  const generatedAt = payload?.generated_at;
  if (!generatedAt) {
    viewReadAt.textContent = "页面读取待确认";
    viewReadAt.removeAttribute("title");
    return;
  }
  const formatted = formatDate(generatedAt, true);
  viewReadAt.textContent = `页面读取 ${formatted}`;
  viewReadAt.title = `服务生成时间：${formatted}`;
}

function tone(value, inverse = false) {
  const parsed = finite(value);
  if (parsed === null || parsed === 0) return "";
  const positive = inverse ? parsed < 0 : parsed > 0;
  return positive ? "tone-positive" : "tone-negative";
}

function statusChip(label, kind = "neutral") {
  return `<span class="status-chip ${kind}">${escapeHtml(label)}</span>`;
}

const VALUATION_STATUS_LABELS = {
  current: "估值已校验",
  stale: "快照滞后",
  needs_review: "需要复核",
  partial: "部分估值",
  unavailable: "估值不可用",
  uninitialized: "尚未建立",
};

function valuationStatusLabel(value) {
  return VALUATION_STATUS_LABELS[String(value || "").toLowerCase()] || "估值状态未知";
}

function valuationStatusKind(value) {
  return {
    current: "success",
    stale: "warning",
    needs_review: "warning",
    partial: "warning",
    unavailable: "danger",
    uninitialized: "neutral",
  }[String(value || "").toLowerCase()] || "neutral";
}

function valuationCell(value, formatter, available = true) {
  if (available) return formatter(value);
  return '<span class="value-unavailable" aria-label="估值不可用">—</span>';
}

function booleanChip(value, passed = "通过", failed = "未通过") {
  return statusChip(value ? passed : failed, value ? "success" : "danger");
}

function metric(label, value, note = "", valueTone = "") {
  return `
    <div class="metric">
      <span class="metric-label">${escapeHtml(label)}</span>
      <strong class="metric-value ${valueTone}">${escapeHtml(value)}</strong>
      <span class="metric-note">${escapeHtml(note)}</span>
    </div>`;
}

function contextBand(items, label) {
  return `
    <dl class="context-band" aria-label="${escapeHtml(label)}">
      ${items.map((item) => `
        <div class="context-item">
          <dt>${escapeHtml(item.label)}</dt>
          <dd>
            <span class="context-value"><strong>${escapeHtml(item.value)}</strong>${statusChip(item.status, item.kind)}</span>
            <span class="context-note">${escapeHtml(item.note)}</span>
          </dd>
        </div>`).join("")}
    </dl>`;
}

function pulseItem(label, value, stateLabel, kind = "neutral") {
  return `
    <div class="market-pulse-item" data-tone="${escapeHtml(kind)}">
      <dt>${escapeHtml(label)}</dt>
      <dd><strong>${escapeHtml(value)}</strong><span>${escapeHtml(stateLabel)}</span></dd>
    </div>`;
}

function pulseOverview(payload) {
  if (state.route === "overview") return payload || {};
  if (state.route === "risk") return payload?.overview || {};
  return state.data.get("overview") || {};
}

function pulsePortfolio(payload) {
  if (state.route === "portfolio") return payload || {};
  return state.data.get("portfolio") || {};
}

function pulseTrading(payload) {
  if (state.route === "trading") return payload || {};
  return state.data.get("trading") || {};
}

function pulseMonitoring(payload) {
  if (state.route === "monitoring") return payload || {};
  return state.data.get("monitoring")
    || state.data.get("overview")?.monitoring
    || {};
}

function updateMarketPulse(payload = null, failure = "") {
  if (!marketPulse || !marketPulseTrack) return;
  const overview = pulseOverview(payload);
  const portfolio = pulsePortfolio(payload);
  const trading = pulseTrading(payload);
  const monitoring = pulseMonitoring(payload);
  const routeChart = state.route === "market" ? payload?.chart || {} : {};
  const routeUniverse = state.route === "universe" ? payload || {} : {};
  const diagnostics = routeChart.diagnostics || {};
  const market = overview.market || {};
  const marketFreshness = market.freshness || {};
  const marketWarnings = Array.isArray(market.warnings) ? market.warnings : [];
  const universeFallbackCount = finite(routeUniverse.screen?.source_summary?.fallback_count) || 0;
  const fallbackUsed = marketWarnings.some((item) => String(item).includes("network fallback"))
    || universeFallbackCount > 0;
  const marketEvidenceLoaded = Object.keys(market).length > 0
    || Object.prototype.hasOwnProperty.call(routeChart, "available")
    || Object.prototype.hasOwnProperty.call(routeUniverse, "market_available");
  const marketDateValue = routeChart.data_date
    || marketFreshness.latest_common_market_date
    || market.date
    || routeUniverse.screen?.data_date
    || routeUniverse.date
    || "不可用";
  const marketMissing = marketEvidenceLoaded && (
    routeChart.available === false
    || diagnostics.missing
    || market.available === false
    || routeUniverse.market_available === false
    || routeUniverse.screen?.status === "unavailable"
  );
  const marketStale = marketEvidenceLoaded && (
    diagnostics.stale === true
    || marketFreshness.current === false
    || routeUniverse.screen?.status === "partial"
    || finite(routeUniverse.screen?.data_quality?.lag_days?.maximum) > 0
  );
  const marketState = !marketEvidenceLoaded
    ? "状态未加载"
    : marketMissing
      ? "数据缺失"
      : marketStale
        ? "快照滞后"
        : fallbackUsed
          ? "备用源"
          : "完整收盘";
  const marketKind = !marketEvidenceLoaded
    ? "neutral"
    : marketMissing
      ? "danger"
      : marketStale || fallbackUsed
        ? "warning"
        : "success";

  const signal = overview.signal || {};
  const targetCount = Object.keys(signal.target_weights || {}).length;
  const signalDate = signal.date || "尚无信号";
  const signalAligned = Boolean(signal.date && marketDateValue !== "不可用" && signal.date === marketDateValue);
  const signalState = signal.date
    ? `${signalAligned ? "同日" : "日期异常"} · ${targetCount} 目标`
    : "保持现金";
  const signalKind = !signal.date ? "neutral" : signalAligned ? "success" : "warning";

  const audit = overview.paper?.audit || trading.paper_audit || {};
  const equity = finite(portfolio.equity) ?? finite(overview.paper?.equity);
  const drawdown = finite(portfolio.drawdown) ?? finite(audit.metrics?.max_drawdown);
  const accountDate = portfolio.date || audit.period?.[1] || marketDateValue;
  const accountValue = equity === null ? "账户未加载" : `¥${integerFormatter.format(equity)}`;
  const accountState = equity === null
    ? "等待账本"
    : `${String(accountDate || "日期未知").slice(5)} · 回撤 ${formatPercent(drawdown)}`;
  const accountKind = drawdown !== null && drawdown < 0 ? "warning" : "neutral";

  const live = overview.live || trading.live || {};
  const sessions = audit.sessions ?? 0;
  const minimumSessions = audit.minimum_promotion_sessions ?? 60;
  const liveReady = Boolean(live.live_ready);
  const riskValue = audit.status || overview.paper
    ? `${sessions} / ${minimumSessions} 日`
    : "门禁未加载";
  const riskState = liveReady ? "待限时人工授权" : "真实交易锁定";
  const riskKind = liveReady ? "warning" : "danger";

  const monitoringSummary = monitoring.summary || {};
  const monitoringScan = monitoring.scan || {};
  const monitoringSnapshot = monitoring.snapshot || {};
  const monitoringLoaded = Object.keys(monitoring).length > 0;
  const unresolvedAlerts = finite(monitoringSummary.unresolved_count);
  const criticalAlerts = finite(monitoringSummary.severity_counts?.critical) || 0;
  const monitoringDate = monitoringScan.data_date
    || monitoringSnapshot.data_date
    || "不可用";
  const monitoringStatus = String(monitoringScan.status || monitoring.empty_state?.code || "").toLowerCase();
  const monitoringValue = !monitoringLoaded
    ? "监控未加载"
    : monitoringSummary.watchlist_count === 0
      ? "未配置列表"
      : `${integerFormatter.format(unresolvedAlerts ?? 0)} 条待处理`;
  const monitoringState = state.monitoringBusy
    ? "扫描中 · 旧证据保留"
    : state.monitoringScanError
      ? "最近扫描失败"
      : !monitoringLoaded
        ? "状态未加载"
        : monitoringStatus === "partial"
          ? `部分完成 · ${monitoringDate}`
          : monitoringStatus === "failed"
            ? "最近扫描失败"
            : monitoringStatus === "not_run" || monitoring.empty_state?.code === "not_scanned"
              ? "尚未扫描"
              : monitoringStatus === "no_rules" || monitoring.empty_state?.code === "no_rules"
                ? "尚无启用规则"
                : unresolvedAlerts
                  ? `扫描至 ${monitoringDate}`
                  : "已扫描 · 无触发";
  const monitoringKind = state.monitoringBusy
    ? "warning"
    : state.monitoringScanError
      ? "danger"
      : !monitoringLoaded
        ? "neutral"
        : monitoringStatus === "failed"
          ? "danger"
          : monitoringStatus === "partial" || criticalAlerts > 0 || finite(monitoringSummary.stale_count) > 0
            ? "warning"
            : monitoringSummary.watchlist_count === 0 || monitoringStatus === "not_run" || monitoringStatus === "no_rules"
              ? "neutral"
              : unresolvedAlerts
                ? "info"
                : "success";

  const activeJob = state.jobs.find((job) => ["queued", "running"].includes(job.status));
  const jobValue = activeJob ? JOB_LABELS[activeJob.action] || activeJob.action : "本机就绪";
  const jobState = activeJob
    ? `${STATUS_LABELS[activeJob.status] || activeJob.status} · ${jobDuration(activeJob)}`
    : "无任务";

  const items = failure
    ? [
        pulseItem("连接", "本机服务不可用", "本页数据未更新", "danger"),
        pulseItem("行情", marketDateValue, marketState, marketKind),
        pulseItem("策略", signalDate, signalState, signalKind),
        pulseItem("监控", monitoringValue, monitoringState, monitoringKind),
        pulseItem("组合", accountValue, accountState, accountKind),
        pulseItem("风险权限", riskValue, riskState, riskKind),
      ]
    : [
        pulseItem("行情", marketDateValue, marketState, marketKind),
        pulseItem("策略", signalDate, signalState, signalKind),
        pulseItem("监控", monitoringValue, monitoringState, monitoringKind),
        pulseItem("组合", accountValue, accountState, accountKind),
        pulseItem("风险权限", riskValue, riskState, riskKind),
        pulseItem("运行", jobValue, jobState, activeJob ? "warning" : "neutral"),
      ];
  marketPulseTrack.innerHTML = items.join("");
  marketPulse.classList.remove("is-updating");
  marketPulse.setAttribute("aria-busy", "false");
}

function setMarketPulseBusy() {
  if (!marketPulse) return;
  marketPulse.classList.add("is-updating");
  marketPulse.setAttribute("aria-busy", "true");
  if (viewReadAt) {
    viewReadAt.textContent = "页面读取中";
    viewReadAt.removeAttribute("title");
  }
}

function pageIntro(title, description, actions = "") {
  return `
    <section class="page-intro">
      <div>
        <h2>${escapeHtml(title)}</h2>
        <p>${escapeHtml(description)}</p>
      </div>
      ${actions ? `<div class="action-row">${actions}</div>` : ""}
    </section>`;
}

function panelHeader(title, note = "", actions = "") {
  return `
    <header class="panel-header">
      <div>
        <h2>${escapeHtml(title)}</h2>
        ${note ? `<p>${escapeHtml(note)}</p>` : ""}
      </div>
      ${actions}
    </header>`;
}

function emptyRow(colspan, message) {
  return `<tr><td class="empty-row" colspan="${colspan}"><strong>暂无记录</strong><span>${escapeHtml(message)}</span></td></tr>`;
}

function instrumentName(symbol, fallback) {
  return INSTRUMENT_NAMES[symbol] || fallback || symbol || "未知证券";
}

function actionButton(action, style = "secondary") {
  if (!state.actions.includes(action)) return "";
  const pending = state.pendingActions.has(action);
  const label = JOB_LABELS[action] || action;
  return `<button class="button ${style}" type="button" data-job-action="${escapeHtml(action)}"${pending ? ' disabled aria-busy="true"' : ""}>${escapeHtml(pending ? `${label}进行中` : label)}</button>`;
}

function skeletonPage() {
  return `
    <div class="skeleton-stack" role="status" aria-label="正在加载">
      <div class="skeleton-heading"><div class="skeleton-line"></div><div class="skeleton-line short"></div></div>
      <div class="skeleton-metrics" aria-hidden="true">
        <div class="skeleton-metric"></div><div class="skeleton-metric"></div><div class="skeleton-metric"></div><div class="skeleton-metric"></div>
      </div>
      <div class="skeleton-workspace" aria-hidden="true"><div class="skeleton-block"></div><div class="skeleton-block"></div></div>
      <span class="sr-only">正在加载当前视图</span>
    </div>`;
}

function friendlyError(message) {
  const value = String(message || "请求失败");
  if (/failed to fetch|networkerror|network request failed|load failed/i.test(value)) {
    return "无法连接本机服务。请确认 AI Trade 服务仍在运行，然后重新加载。";
  }
  if (value.includes("Market data is unavailable")) {
    return "本地行情缓存不可用。刷新行情后，系统会重新校验已完成交易日和快照指纹。";
  }
  if (value.includes("Missing cache") || value.includes("run download")) {
    return "本机尚无完整行情缓存。刷新行情后，系统会重新校验数据覆盖。";
  }
  if (value.includes("Paper account is not initialized")) {
    return "模拟账户尚未初始化。先建立独立模拟账户，再开始累计前向交易日。";
  }
  if (value.includes("configuration changed")) {
    return "模拟账户建立后配置已变化。旧账户必须归档，再建立新的证据周期。";
  }
  return value;
}

function renderError(error) {
  const message = friendlyError(error?.message || error);
  updateViewReadAt(null, message);
  updateMarketPulse(state.data.get(state.route) || null, message);
  main.innerHTML = `
    <section class="error-state" role="alert">
      <h2>当前视图无法完成加载</h2>
      <p>${escapeHtml(message)}</p>
      <p class="error-impact"><strong>影响范围：</strong>本页没有更新；已经写入的本地账本、报告和策略记录不会因此改变。</p>
      <div class="action-row">
        <button class="button secondary" type="button" data-retry>重新加载</button>
        ${actionButton("refresh-data", "primary")}
      </div>
    </section>`;
}

function renderFileProtocolNotice() {
  setRouteChrome(state.route);
  setConnection(false);
  marketDate.textContent = "未连接工作台服务";
  updateViewReadAt(null, "file-protocol");
  jobIndicator.textContent = "服务未启动";
  jobIndicator.className = "status-chip danger";
  updateMarketPulse(null, "服务未启动");
  main.setAttribute("aria-busy", "false");
  main.innerHTML = `
    <div class="page-stack">
      ${pageIntro("工作台未通过本地服务打开", "当前地址是磁盘上的 HTML 文件，因此无法读取行情、账户和报告 API")}
      <section class="error-state" role="alert">
        <h2>这个文件不是工作台入口</h2>
        <p>请启动 <code>ai-trade serve</code>，然后打开命令行显示的 <code>http://127.0.0.1:端口/</code> 地址。不要直接双击 <code>index.html</code>。</p>
      </section>
    </div>`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const raw = await response.text();
  let payload = {};
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = { error: raw };
    }
  }
  if (!response.ok) {
    if (response.status === 401) {
      location.replace("/login");
    }
    const error = new Error(payload.error || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function bootstrap() {
  main.innerHTML = skeletonPage();
  try {
    const payload = await api("/api/bootstrap");
    state.token = payload.token;
    state.actions = Array.isArray(payload.actions) ? payload.actions : [];
    state.user = payload.user || null;
    state.authEnabled = payload.auth_enabled !== false;
    if (payload.version) versionLabel.textContent = `AI Trade v${payload.version}`;
    signedInUser.textContent = state.authEnabled
      ? `内测账号 ${state.user?.username || "已登录"}`
      : "本地所有者";
    logoutButton.hidden = !state.authEnabled;
    setConnection(true);
    await loadRoute();
    await pollJobs();
    window.setInterval(pollJobs, 4000);
  } catch (error) {
    setConnection(false);
    renderError(error);
  }
}

function setConnection(connected) {
  connectionDot.classList.toggle("connected", connected);
  connectionDot.classList.toggle("failed", !connected);
  connectionLabel.textContent = connected ? "仅连接本机" : "本机服务不可用";
}

function setRouteChrome(route) {
  const meta = ROUTES[route];
  let activeLink = null;
  routeTitle.textContent = meta.title;
  routeContext.textContent = meta.context;
  document.title = `${meta.title} | AI Trade`;
  for (const link of document.querySelectorAll("[data-route]")) {
    if (link.dataset.route === route) {
      link.setAttribute("aria-current", "page");
      activeLink = link;
    } else {
      link.removeAttribute("aria-current");
    }
  }
  if (activeLink && window.matchMedia("(max-width: 820px)").matches) {
    window.requestAnimationFrame(() => {
      const navigation = activeLink.closest(".sidebar");
      if (!navigation) return;
      navigation.scrollLeft = Math.max(
        0,
        activeLink.offsetLeft - (navigation.clientWidth - activeLink.clientWidth) / 2,
      );
    });
  }
}

function universeScreenPath() {
  const params = new URLSearchParams();
  if (state.universeDate) params.set("date", state.universeDate);
  const filters = state.universeFilters || {};
  for (const name of [
    "asset_class",
    "sector",
    "trend",
    "coverage",
    "min_average_amount",
    "max_annual_volatility",
    "sort",
    "direction",
    "limit",
  ]) {
    const value = String(filters[name] ?? "");
    if (value && !((name === "trend" || name === "coverage") && value === "any")) {
      params.set(name, value);
    }
  }
  if (filters.active_only) params.set("active_only", "true");
  const query = params.toString();
  return `/api/universe/screen${query ? `?${query}` : ""}`;
}

function researchPath() {
  const params = new URLSearchParams();
  const filters = state.journalFilters || {};
  if (filters.category) params.set("category", filters.category);
  if (filters.symbol) params.set("symbol", filters.symbol);
  if (filters.query) params.set("q", filters.query);
  if (filters.limit && filters.limit !== "100") params.set("limit", filters.limit);
  const query = params.toString();
  return `/api/research${query ? `?${query}` : ""}`;
}

function monitoringPath() {
  // The monitoring endpoint returns one owner-scoped evidence snapshot. Filters
  // stay client-side so an older server cannot silently change scan semantics.
  return "/api/monitoring";
}

function marketIntelligencePath() {
  const params = new URLSearchParams();
  const filters = state.intelligenceFilters || {};
  if (filters.date) params.set("date", filters.date);
  if (filters.symbol) params.set("symbol", filters.symbol);
  if (filters.market) params.set("market", filters.market);
  if (filters.query) params.set("q", filters.query);
  if (filters.limit && filters.limit !== "200") params.set("limit", filters.limit);
  const query = params.toString();
  return `/api/market-intelligence${query ? `?${query}` : ""}`;
}

function marketBreadthPath() {
  const params = new URLSearchParams();
  const filters = state.breadthFilters || {};
  if (filters.date) params.set("date", filters.date);
  if (filters.query) params.set("q", filters.query);
  if (filters.sort && filters.sort !== "change_pct") params.set("sort", filters.sort);
  if (filters.direction && filters.direction !== "desc") params.set("direction", filters.direction);
  if (filters.limit && filters.limit !== "200") params.set("limit", filters.limit);
  const query = params.toString();
  return `/api/market-breadth${query ? `?${query}` : ""}`;
}

async function loadRoute() {
  state.controller?.abort();
  destroyMarketChart();
  const controller = new AbortController();
  const requestedRoute = state.route;
  state.controller = controller;
  const signal = controller.signal;
  const isCurrentRequest = () => (
    state.controller === controller
    && state.route === requestedRoute
    && !signal.aborted
  );
  setRouteChrome(requestedRoute);
  setMarketPulseBusy();
  main.setAttribute("aria-busy", "true");
  main.innerHTML = skeletonPage();
  try {
    let payload;
    if (requestedRoute === "market") {
      const chartPath = `/api/market-chart?symbol=${encodeURIComponent(state.marketSymbol)}&period=${encodeURIComponent(state.marketPeriod)}&limit=${encodeURIComponent(state.marketLimit)}`;
      const [chartResult, universeResult] = await Promise.allSettled([
        api(chartPath, { signal }),
        api("/api/universe", { signal }),
      ]);
      if (chartResult.status === "rejected" && universeResult.status === "rejected") {
        throw chartResult.reason;
      }
      payload = {
        chart: chartResult.status === "fulfilled" ? chartResult.value : null,
        chart_error: chartResult.status === "rejected" ? friendlyError(chartResult.reason?.message) : "",
        universe: universeResult.status === "fulfilled" ? universeResult.value : { instruments: [] },
        universe_error: universeResult.status === "rejected" ? friendlyError(universeResult.reason?.message) : "",
      };
    } else if (requestedRoute === "intelligence") {
      const [dragonTigerResult, breadthResult] = await Promise.allSettled([
        api(marketIntelligencePath(), { signal }),
        api(marketBreadthPath(), { signal }),
      ]);
      if (dragonTigerResult.status === "rejected" && breadthResult.status === "rejected") {
        throw dragonTigerResult.reason;
      }
      payload = {
        dragon_tiger: dragonTigerResult.status === "fulfilled" ? dragonTigerResult.value : null,
        dragon_tiger_error: dragonTigerResult.status === "rejected" ? friendlyError(dragonTigerResult.reason?.message) : "",
        breadth: breadthResult.status === "fulfilled" ? breadthResult.value : null,
        breadth_error: breadthResult.status === "rejected" ? friendlyError(breadthResult.reason?.message) : "",
        generated_at: breadthResult.status === "fulfilled"
          ? breadthResult.value?.generated_at
          : dragonTigerResult.status === "fulfilled"
            ? dragonTigerResult.value?.generated_at
            : null,
      };
    } else if (requestedRoute === "risk") {
      const [overview, research] = await Promise.all([
        api("/api/overview", { signal }),
        api("/api/research", { signal }),
      ]);
      payload = { overview, research };
    } else if (requestedRoute === "system") {
      const [system, jobs] = await Promise.all([
        api("/api/system", { signal }),
        api("/api/jobs", { signal }),
      ]);
      payload = { system, jobs: jobs.jobs || [] };
    } else {
      const endpoints = {
        overview: "/api/overview",
        research: researchPath(),
        monitoring: monitoringPath(),
        assistant: "/api/assistant",
        "strategy-lab": "/api/strategy-lab",
        portfolio: "/api/portfolio",
        trading: "/api/trading",
        universe: universeScreenPath(),
        storage: "/api/storage",
      };
      payload = await api(endpoints[requestedRoute], { signal });
    }
    if (!isCurrentRequest()) return;
    state.data.set(requestedRoute, payload);
    renderRoute(payload);
    setConnection(true);
  } catch (error) {
    if (error.name !== "AbortError" && isCurrentRequest()) {
      setConnection(false);
      renderError(error);
    }
  } finally {
    if (state.controller === controller && state.route === requestedRoute) {
      main.setAttribute("aria-busy", "false");
    }
  }
}

function renderRoute(payload) {
  destroyMarketChart();
  state.charts.clear();
  const renderers = {
    overview: renderOverview,
    market: renderMarket,
    intelligence: renderMarketIntelligence,
    monitoring: renderMonitoring,
    research: renderResearch,
    assistant: renderAssistant,
    "strategy-lab": renderStrategyLab,
    portfolio: renderPortfolio,
    trading: renderTrading,
    risk: renderRisk,
    universe: renderUniverseEnhanced,
    storage: renderStorage,
    system: renderSystem,
  };
  main.innerHTML = renderers[state.route](payload);
  enhanceRenderedUi();
  updateRouteDate(payload);
  updateViewReadAt(payload);
  updateMarketPulse(payload);
  updateJobButtons();
  requestAnimationFrame(() => {
    drawCharts();
    if (state.route === "market") initMarketChart(payload);
  });
}

function restoreFocusAfterRender(selector, route = state.route) {
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      if (state.route !== route) return;
      document.querySelector(selector)?.focus({ preventScroll: true });
    });
  });
}

function updateRouteDate(payload) {
  let value = "";
  let label = "数据日期";
  if (state.route === "overview") {
    value = payload.market?.freshness?.latest_common_market_date || payload.market?.date;
    label = "共同最新行情";
    if (payload.version) versionLabel.textContent = `AI Trade v${payload.version}`;
  } else if (state.route === "market") {
    value = payload.chart?.data_date;
    label = "K 线截止";
  } else if (state.route === "intelligence") {
    value = payload.breadth?.trade_date || payload.dragon_tiger?.trade_date;
    label = "情报日期";
  } else if (state.route === "monitoring") {
    value = payload.scan?.data_date || payload.snapshot?.data_date;
    label = "监控截止";
  } else if (state.route === "research") {
    value = payload.backtest?.metadata?.end;
    label = "研究截止";
  } else if (state.route === "assistant") {
    value = (state.assistantResult || payload.history?.[0])?.data_date;
    label = "分析快照";
  } else if (state.route === "strategy-lab") {
    const selected = strategyLabSelectedCandidate(payload);
    if (!selected?.validation?.market_snapshot?.date) {
      marketDate.textContent = selected ? "候选尚未验证" : "尚无候选验证";
      return;
    }
    value = selected?.validation?.market_snapshot?.date;
    label = "验证快照";
  } else if (state.route === "portfolio") {
    value = payload.date;
    label = "账本日期";
  } else if (state.route === "trading") {
    value = payload.paper_audit?.period?.[1];
    label = "审计截止";
  } else if (state.route === "risk") {
    value = payload.overview?.market?.freshness?.latest_common_market_date
      || payload.overview?.market?.date;
    label = "共同最新行情";
  } else if (state.route === "universe") {
    value = payload.date;
    label = "截面日期";
  } else if (state.route === "storage") {
    const scanned = payload.usage?.scanned_at;
    marketDate.textContent = scanned
      ? `云端清点 ${formatDate(scanned, true)}`
      : "云端尚未清点";
    return;
  } else if (state.route === "system") {
    value = payload.system?.diagnosis?.latest_market_date;
    label = "行情截止";
  }
  marketDate.textContent = value ? `${label} ${value}` : `${label}不可用`;
}

function enhanceRenderedUi() {
  let tableIndex = 0;
  for (const region of main.querySelectorAll(".table-wrap")) {
    region.tabIndex = 0;
    region.setAttribute("role", "region");
    region.setAttribute("aria-describedby", "table-scroll-help");
    const heading = region.closest(".panel, article, section")?.querySelector("h2, h3");
    if (region.hasAttribute("aria-label")) {
      region.removeAttribute("aria-labelledby");
    } else if (heading) {
      if (!heading.id) heading.id = `${state.route}-table-heading-${tableIndex}`;
      region.setAttribute("aria-labelledby", heading.id);
    } else {
      region.setAttribute("aria-label", "数据表格");
    }
    tableIndex += 1;
  }
  bindUniverseFilterForm();
  syncJournalDecisionControl();
  syncMonitoringRuleControl();
}

function syncMonitoringRuleControl() {
  const form = document.getElementById("monitoring-rule-form");
  if (!form) return;
  const type = form.querySelector("[name='rule_type']");
  const threshold = form.querySelector("[name='threshold']");
  const windowInput = form.querySelector("[name='window']");
  const comparison = form.querySelector("[name='comparison_window']");
  const help = document.getElementById("monitoring-rule-help");
  if (!type || !threshold) return;
  const metadata = monitoringRuleMetadata(state.data.get("monitoring") || {})[type.value] || {};
  const thresholdRequired = Boolean(metadata.threshold_required);
  threshold.required = thresholdRequired;
  threshold.disabled = !type.value || !thresholdRequired;
  if (!thresholdRequired) threshold.value = "";
  if (windowInput) {
    windowInput.placeholder = metadata.window_default ? `默认 ${metadata.window_default}` : "使用默认";
  }
  if (comparison) {
    const isCross = String(type.value || "").startsWith("ema_cross");
    comparison.disabled = !isCross;
    comparison.placeholder = isCross && metadata.comparison_window_default
      ? `默认 ${metadata.comparison_window_default}`
      : "仅 EMA 交叉";
    if (!isCross) comparison.value = "";
  }
  if (help) {
    const formula = metadata.formula || "服务器固定口径";
    const unit = metadata.unit ? ` · 单位 ${metadata.unit}` : "";
    help.textContent = type.value
      ? `${formula}${unit}${thresholdRequired ? "；需要填写阈值" : "；该规则不使用阈值"}。服务器会再次校验窗口和证券归属。`
      : "选择规则后，服务器会校验阈值、窗口和证券是否属于列表。";
  }
}

function applyUniverseFilterForm(form) {
  if (!form) return;
  const values = new FormData(form);
  state.universeDate = String(values.get("date") || "");
  state.universeFilters = {
    asset_class: String(values.get("asset_class") || ""),
    sector: String(values.get("sector") || "").trim(),
    trend: String(values.get("trend") || "any"),
    coverage: String(values.get("coverage") || "all"),
    min_average_amount: String(values.get("min_average_amount") || "").trim(),
    max_annual_volatility: String(values.get("max_annual_volatility") || "").trim(),
    active_only: form.querySelector("[name='active_only']")?.checked === true,
    sort: String(values.get("sort") || "momentum"),
    direction: String(values.get("direction") || "desc"),
    limit: String(values.get("limit") || "200"),
  };
  const submit = form.querySelector("[data-universe-submit]");
  if (submit) {
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
  }
  loadRoute();
}

function bindUniverseFilterForm() {
  const form = document.getElementById("universe-date-form");
  if (!form || form.dataset.bound === "true") return;
  form.dataset.bound = "true";
  form.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing || event.target.matches("textarea")) return;
    event.preventDefault();
    applyUniverseFilterForm(form);
  });
}

async function applyIntelligenceFilterForm(form) {
  if (!form) return;
  const values = new FormData(form);
  state.intelligenceFilters = {
    date: String(values.get("date") || ""),
    symbol: String(values.get("symbol") || "").trim(),
    market: String(values.get("market") || ""),
    query: String(values.get("q") || "").trim(),
    limit: String(values.get("limit") || "200"),
  };
  await loadRoute();
  restoreFocusAfterRender('#market-intelligence-filter-form button[type="submit"]', "intelligence");
}

async function clearIntelligenceFilters() {
  state.intelligenceFilters = {
    date: "",
    symbol: "",
    market: "",
    query: "",
    limit: "200",
  };
  await loadRoute();
  restoreFocusAfterRender("[data-intelligence-filter-clear]", "intelligence");
}

async function applyBreadthFilterForm(form) {
  if (!form) return;
  const values = new FormData(form);
  state.breadthFilters = {
    date: String(values.get("date") || ""),
    query: String(values.get("q") || "").trim(),
    sort: String(values.get("sort") || "change_pct"),
    direction: String(values.get("direction") || "desc"),
    limit: String(values.get("limit") || "200"),
  };
  await loadRoute();
  restoreFocusAfterRender('#market-breadth-filter-form button[type="submit"]', "intelligence");
}

async function clearBreadthFilters() {
  state.breadthFilters = {
    date: "",
    query: "",
    sort: "change_pct",
    direction: "desc",
    limit: "200",
  };
  await loadRoute();
  restoreFocusAfterRender("[data-market-breadth-filter-clear]", "intelligence");
}

function syncJournalDecisionControl() {
  const form = document.getElementById("research-journal-form");
  if (!form) return;
  const decision = form.querySelector("[data-journal-decision]");
  const confidence = form.querySelector("[data-journal-confidence]");
  if (!decision || !confidence) return;
  const enabled = decision.value !== "not_recorded";
  confidence.disabled = !enabled;
  confidence.required = enabled;
  if (!enabled) confidence.value = "";
}

function applyJournalFilterForm(form) {
  if (!form) return;
  const values = new FormData(form);
  state.journalFilters = {
    category: String(values.get("category") || ""),
    symbol: String(values.get("symbol") || ""),
    query: String(values.get("query") || "").trim(),
    limit: String(values.get("limit") || "100"),
  };
  loadRoute();
}

function clearJournalFilters() {
  state.journalFilters = {
    category: "",
    symbol: "",
    query: "",
    limit: "100",
  };
  loadRoute();
}

function selectJournalCorrection(entryId) {
  state.journalCorrectionOf = entryId;
  const form = document.getElementById("research-journal-form");
  if (!form) return;
  const hidden = form.querySelector("[name='correction_of']");
  if (hidden) hidden.value = entryId;
  const context = document.getElementById("research-journal-correction");
  if (context) {
    context.hidden = false;
    const code = context.querySelector("code");
    if (code) code.textContent = shortCandidateId(entryId);
  }
  const title = form.querySelector("[name='title']");
  title?.focus({ preventScroll: true });
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

function cancelJournalCorrection() {
  state.journalCorrectionOf = "";
  const form = document.getElementById("research-journal-form");
  if (!form) return;
  const hidden = form.querySelector("[name='correction_of']");
  if (hidden) hidden.value = "";
  const context = document.getElementById("research-journal-correction");
  if (context) context.hidden = true;
}

async function appendResearchJournal(form) {
  if (state.journalBusy || !form.reportValidity()) return;
  const values = new FormData(form);
  const decision = String(values.get("decision") || "not_recorded");
  const button = form.querySelector("button[type='submit']");
  const status = document.getElementById("research-journal-status");
  state.journalBusy = true;
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "正在写入";
  }
  if (status) {
    status.className = "form-status";
    status.textContent = "正在固化研究证据与权限边界…";
  }
  try {
    const payload = await api("/api/research/journal", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify({
        research_date: String(values.get("research_date") || ""),
        category: String(values.get("category") || ""),
        symbol: String(values.get("symbol") || "") || null,
        title: String(values.get("title") || ""),
        note: String(values.get("note") || ""),
        decision,
        confidence: decision === "not_recorded" ? null : Number(values.get("confidence")),
        correction_of: String(values.get("correction_of") || "") || null,
      }),
    });
    state.journalCorrectionOf = "";
    state.journalBusy = false;
    form.reset();
    cancelJournalCorrection();
    syncJournalDecisionControl();
    try {
      await reloadResearch();
      document.querySelector("[data-research-digest-generate]")?.focus({
        preventScroll: true,
      });
    } catch {
      const message = "研究日志已写入，但列表刷新失败；请刷新视图确认，避免重复提交。";
      if (status?.isConnected) {
        status.className = "form-status error";
        status.textContent = message;
      }
      notify(message, true);
      return;
    }
    notify(payload.entry_id ? "研究日志已写入；旧记录保持不变" : "研究日志已写入");
  } catch (error) {
    const message = friendlyError(error.message);
    if (status) {
      status.className = "form-status error";
      status.textContent = message;
    }
    notify(message, true);
  } finally {
    state.journalBusy = false;
    if (button?.isConnected) {
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
      button.textContent = "写入研究日志";
    }
  }
}

async function reloadResearch() {
  const payload = await api(researchPath());
  state.data.set("research", payload);
  if (state.route === "research") renderRoute(payload);
}

async function generateResearchDigests(button) {
  if (state.researchDigestBusy) return;
  state.researchDigestBusy = true;
  state.researchDigestStatus = "正在从本地日报、账本和研究日志建立不可变版本";
  state.researchDigestStatusKind = "pending";
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "正在写入归档";
  }
  const status = document.getElementById("research-digest-status");
  if (status) {
    status.textContent = state.researchDigestStatus;
    status.className = "form-status pending";
  }
  try {
    const result = await api("/api/research/digests/generate", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify({ kind: "all" }),
    });
    const written = Number(result.summary?.written || 0);
    const reused = Number(result.summary?.reused || 0);
    const partial = result.available === false
      || (Array.isArray(result.errors) && result.errors.length > 0);
    const evidencePartial = !partial && result.status === "partial";
    const detail = result.errors?.[0]?.message || "部分证据未能写入";
    const message = partial
      ? `归档仅部分完成：写入 ${formatInteger(written)} 个新版本，复用 ${formatInteger(reused)} 个已有版本；${detail}`
      : evidencePartial
      ? `归档写入完成：新增 ${formatInteger(written)} 个版本，复用 ${formatInteger(reused)} 个已有版本；其中包含不完整证据，请复核状态`
      : written
      ? `已写入 ${formatInteger(written)} 个新版本，复用 ${formatInteger(reused)} 个未变化版本`
      : `证据没有变化，复用 ${formatInteger(reused)} 个已有版本`;
    state.researchDigestStatus = message;
    state.researchDigestStatusKind = partial
      ? "error"
      : evidencePartial
      ? "warning"
      : "success";
    if (status) {
      status.textContent = message;
      status.className = `form-status ${state.researchDigestStatusKind}`;
    }
    // The refreshed view must not inherit the transient disabled state.
    state.researchDigestBusy = false;
    try {
      await reloadResearch();
    } catch {
      const refreshMessage = `${message}；列表刷新失败，请重新读取页面确认，避免重复操作`;
      state.researchDigestStatus = refreshMessage;
      state.researchDigestStatusKind = "error";
      if (status?.isConnected) {
        status.textContent = refreshMessage;
        status.className = "form-status error";
      }
      notify(refreshMessage, true);
      return;
    }
    notify(message, partial);
  } catch (error) {
    const message = friendlyError(error.message);
    state.researchDigestStatus = message;
    state.researchDigestStatusKind = "error";
    if (status) {
      status.textContent = message;
      status.className = "form-status error";
    }
    notify(message, true);
  } finally {
    state.researchDigestBusy = false;
    const currentButton = button?.isConnected
      ? button
      : document.querySelector("[data-research-digest-generate]");
    if (currentButton) {
      currentButton.disabled = false;
      currentButton.setAttribute("aria-busy", "false");
      currentButton.textContent = "生成收盘归档";
    }
    restoreFocusAfterRender("[data-research-digest-generate]", "research");
  }
}

function marketProviderLabel(value) {
  return {
    eastmoney: "东方财富",
    tencent: "腾讯行情",
    tencent_network_fallback: "腾讯网络回退",
    tencent_newfqkline: "腾讯前复权行情",
    eastmoney_network: "东方财富网络行情",
    network: "网络行情",
    validated_local_fallback: "已验证本地备用缓存",
  }[String(value || "").toLowerCase()] || value || "未说明";
}

function marketAdjustmentLabel(value) {
  return {
    forward: "前复权",
    backward: "后复权",
    none: "不复权",
    raw: "不复权",
  }[String(value || "").toLowerCase()] || value || "未说明";
}

function formatMarketAmount(value) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  if (Math.abs(parsed) >= 100000000) return `${formatNumber(parsed / 100000000)} 亿元`;
  if (Math.abs(parsed) >= 10000) return `${formatNumber(parsed / 10000)} 万元`;
  return `${formatNumber(parsed)} 元`;
}

function marketDirection(change) {
  const parsed = finite(change);
  if (parsed === null || parsed === 0) {
    return { label: "平盘", className: "market-flat", sign: "" };
  }
  return parsed > 0
    ? { label: "上涨", className: "market-up", sign: "+" }
    : { label: "下跌", className: "market-down", sign: "" };
}

function marketInstruments(data) {
  const chartInstrument = data.chart?.instrument || {};
  const rows = Array.isArray(data.universe?.instruments)
    ? data.universe.instruments.map((item) => ({ ...item }))
    : [];
  if (chartInstrument.symbol && !rows.some((item) => item.symbol === chartInstrument.symbol)) {
    rows.push({ ...chartInstrument, active: true, tradable: true });
  }
  if (!rows.some((item) => item.symbol === state.marketSymbol)) {
    rows.push({
      symbol: state.marketSymbol,
      name: instrumentName(state.marketSymbol),
      active: false,
      tradable: false,
    });
  }
  return rows
    .filter((item) => item.symbol)
    .sort((left, right) => {
      const leftRank = Number(Boolean(left.active)) + Number(Boolean(left.tradable));
      const rightRank = Number(Boolean(right.active)) + Number(Boolean(right.tradable));
      return rightRank - leftRank || String(left.symbol).localeCompare(String(right.symbol));
    });
}

function marketEvidenceValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.map(marketEvidenceValue).join(" · ");
  if (typeof value === "object") {
    return Object.entries(value)
      .filter(([, item]) => item !== null && item !== undefined && item !== "")
      .map(([key, item]) => `${key}: ${marketEvidenceValue(item)}`)
      .join(" · ") || "—";
  }
  return String(value);
}

function marketProvenanceSummary(provenance) {
  if (!provenance || typeof provenance !== "object") return "未提供";
  const values = [
    provenance.manifest_available ? "清单已校验" : "缺少清单",
    provenance.downloaded_at ? `下载于 ${formatDate(provenance.downloaded_at, true)}` : "下载时间未知",
    {
      full: "全量更新",
      full_history: "全量历史更新",
      full_rebuild_after_overlap_mismatch: "重叠校验失败后全量重建",
      incremental: "增量更新",
      fallback: "网络回退",
    }[provenance.source_mode] || provenance.source_mode,
    {
      provider_reported: "提供方原始成交额",
      provider_reported_rounded: "提供方四舍五入成交额",
      calculated: "本地估算成交额",
    }[provenance.amount_quality] || provenance.amount_quality,
  ];
  return values.filter(Boolean).join(" · ");
}

function marketDiagnosticMessages(diagnostics) {
  if (Array.isArray(diagnostics)) {
    return diagnostics.map((item) => marketEvidenceValue(item)).filter((item) => item !== "—");
  }
  if (!diagnostics || typeof diagnostics !== "object") return [];
  const messages = [];
  for (const key of ["warnings", "messages", "data_warnings"]) {
    const values = diagnostics[key];
    if (Array.isArray(values)) {
      messages.push(...values.map((item) => friendlyError(marketEvidenceValue(item))));
    } else if (typeof values === "string" && values) {
      messages.push(values);
    }
  }
  if (diagnostics.missing) {
    messages.push("本地没有可验证的行情缓存，请刷新行情后重试。");
  }
  if (diagnostics.stale) {
    messages.push(`数据截至 ${diagnostics.latest_completed_bar || "未知日期"}，早于已完成交易日 ${diagnostics.completed_session_cutoff || "未知日期"}。`);
  }
  if (finite(diagnostics.excluded_incomplete_count) > 0) {
    messages.push(`已排除 ${formatInteger(diagnostics.excluded_incomplete_count)} 个尚未完成的未来日期。`);
  }
  if (diagnostics.trade_markers_truncated) {
    messages.push("模拟成交标记数量超过显示上限，仅保留最近记录。");
  }
  if (diagnostics.stale_reason) messages.push(String(diagnostics.stale_reason));
  return [...new Set(messages.filter(Boolean))];
}

function marketSnapshotFingerprint(snapshot, key) {
  return snapshot?.[key]
    || snapshot?.manifest?.[key]
    || snapshot?.file?.[key]
    || "—";
}

function marketUnavailableMessage(data, diagnostics) {
  if (data.chart_error) return data.chart_error;
  if (diagnostics.code === "market_data_unavailable") {
    return "本地行情缓存不可用。刷新行情后，系统会重新校验已完成交易日和快照指纹。";
  }
  if (diagnostics.message) return friendlyError(diagnostics.message);
  return "至少需要两根有效 K 线才能建立主图和指标窗格。";
}

function renderMarket(data) {
  const chart = data.chart || {};
  const bars = Array.isArray(chart.bars) ? chart.bars : [];
  const latest = bars[bars.length - 1] || null;
  const previous = bars[bars.length - 2] || null;
  const change = latest && previous ? finite(latest.close) - finite(previous.close) : null;
  const changeRatio = change !== null && finite(previous?.close)
    ? change / finite(previous.close)
    : null;
  const direction = marketDirection(change);
  const instrument = chart.instrument
    || marketInstruments(data).find((item) => item.symbol === state.marketSymbol)
    || { symbol: state.marketSymbol, name: instrumentName(state.marketSymbol) };
  const periodLabel = MARKET_PERIODS[state.marketPeriod]?.label || state.marketPeriod;
  const diagnostics = chart.diagnostics || {};
  const diagnosticMessages = marketDiagnosticMessages(diagnostics);
  const stale = diagnostics.stale === true || diagnostics.is_stale === true;
  const chartSummary = latest
    ? `${instrumentName(instrument.symbol, instrument.name)} ${periodLabel}最新一根为 ${latest.date}，开盘 ${formatNumber(latest.open, 3)}，最高 ${formatNumber(latest.high, 3)}，最低 ${formatNumber(latest.low, 3)}，收盘 ${formatNumber(latest.close, 3)}；较上一根${direction.label} ${direction.sign}${formatNumber(change, 3)}，${direction.sign}${formatPercent(changeRatio)}。`
    : `${instrumentName(instrument.symbol, instrument.name)} 暂无可绘制的 ${periodLabel} 数据。`;
  const options = marketInstruments(data).map((item) => {
    const availability = item.active ? "" : " · 非当前有效";
    return `<option value="${escapeHtml(item.symbol)}"${item.symbol === state.marketSymbol ? " selected" : ""}>${escapeHtml(item.symbol)} · ${escapeHtml(instrumentName(item.symbol, item.name))}${availability}</option>`;
  }).join("");
  const periodButtons = Object.entries(MARKET_PERIODS).map(([value, meta]) => `
    <button type="button" data-market-period="${value}" aria-pressed="${value === state.marketPeriod}">${meta.label}</button>`).join("");
  const overlayOptions = Object.entries(MARKET_OVERLAYS).map(([value, label]) =>
    `<option value="${value}"${value === state.marketOverlay ? " selected" : ""}>${label}</option>`
  ).join("");
  const oscillatorOptions = Object.entries(MARKET_OSCILLATORS).map(([value, label]) =>
    `<option value="${value}"${value === state.marketOscillator ? " selected" : ""}>${label}</option>`
  ).join("");
  const actualSource = chart.provenance?.source_provider || chart.provenance?.source;
  const source = actualSource ? marketProviderLabel(actualSource) : "未验证";
  const configuredProvider = marketProviderLabel(chart.provider);
  const adjustment = marketAdjustmentLabel(chart.adjustment);
  const snapshot = chart.snapshot || {};
  const chartBody = bars.length >= 2
    ? `<figure class="market-chart-figure">
        <div id="market-kline-chart" class="market-kline-chart" role="img" tabindex="0" aria-label="${escapeHtml(chartSummary)}" aria-describedby="market-chart-summary">
          <span class="market-chart-loading" role="status">正在准备 K 线</span>
        </div>
        <figcaption id="market-chart-summary">${escapeHtml(chartSummary)}</figcaption>
      </figure>`
    : `<section class="empty-state market-empty" role="status">
        <h2>当前标的没有足够的行情数据</h2>
        <p>${escapeHtml(marketUnavailableMessage(data, diagnostics))}</p>
        <div class="action-row"><button class="button secondary" type="button" data-retry>重新加载</button>${actionButton("refresh-data", "primary")}</div>
      </section>`;
  let dataStatus = statusChip("收盘快照", "info");
  if (data.chart_error || chart.available === false || diagnostics.missing) {
    dataStatus = statusChip("行情不可用", "danger");
  } else if (stale) {
    dataStatus = statusChip("快照已陈旧", "warning");
  } else if (diagnostics.status === "warning") {
    dataStatus = statusChip("证据有警告", "warning");
  }

  return `
    <div class="market-page">
      ${pageIntro(
        `${instrumentName(instrument.symbol, instrument.name)} · ${instrument.symbol}`,
        `${periodLabel} · ${source} · ${adjustment} · ${bars.length} 根有效 K 线`,
        `<div class="market-status-line">${statusChip("只读", "neutral")}${dataStatus}</div>`,
      )}

      <section class="market-command-band" aria-label="行情筛选">
        <form id="market-controls-form" class="market-controls">
          <div class="field market-symbol-field">
            <label for="market-symbol">证券</label>
            <select id="market-symbol" name="symbol">${options}</select>
          </div>
          <fieldset class="market-period-field">
            <legend>周期</legend>
            <div class="segmented market-periods">${periodButtons}</div>
          </fieldset>
          <div class="field market-range-field">
            <label for="market-limit">数据范围</label>
            <select id="market-limit" name="limit">
              ${[120, 240, 500, 1000, 1500].map((value) => `<option value="${value}"${value === state.marketLimit ? " selected" : ""}>近 ${value} 根</option>`).join("")}
            </select>
          </div>
          <button class="button secondary market-apply" type="submit">应用</button>
        </form>
      </section>

      <section class="market-quote-strip" aria-label="最新行情摘要">
        <div class="market-last-price">
          <span>最新收盘</span>
          <strong class="${direction.className}">${formatNumber(latest?.close, 3)}</strong>
          <span class="market-change ${direction.className}">${direction.label} ${direction.sign}${formatNumber(change, 3)} · ${direction.sign}${formatPercent(changeRatio)}</span>
        </div>
        <dl>
          <div><dt>开盘</dt><dd>${formatNumber(latest?.open, 3)}</dd></div>
          <div><dt>最高</dt><dd>${formatNumber(latest?.high, 3)}</dd></div>
          <div><dt>最低</dt><dd>${formatNumber(latest?.low, 3)}</dd></div>
          <div><dt>成交量</dt><dd>${finite(latest?.volume) === null ? "—" : compactFormatter.format(finite(latest.volume))}</dd></div>
          <div><dt>成交额</dt><dd>${formatMarketAmount(latest?.amount)}</dd></div>
          <div><dt>交易日</dt><dd>${escapeHtml(latest?.date || chart.data_date || "—")}</dd></div>
        </dl>
      </section>

      <section class="market-chart-panel" aria-labelledby="market-chart-title">
        <header class="market-chart-header">
          <div>
            <h2 id="market-chart-title">价格与成交</h2>
            <span>${escapeHtml(periodLabel)} · OHLCV</span>
            <strong class="market-mobile-quote ${direction.className}" aria-hidden="true">${formatNumber(latest?.close, 3)} · ${direction.label} ${direction.sign}${formatPercent(changeRatio)}</strong>
          </div>
          <div class="market-indicator-controls">
            <div class="field">
              <label for="market-overlay">主图指标</label>
              <select id="market-overlay" data-market-overlay>${overlayOptions}</select>
            </div>
            <div class="field">
              <label for="market-oscillator">副图指标</label>
              <select id="market-oscillator" data-market-oscillator>${oscillatorOptions}</select>
            </div>
          </div>
        </header>
        ${chartBody}
      </section>

      <section class="market-evidence-layout" aria-label="行情证据">
        <article class="panel">
          ${panelHeader("数据证据", "当前图表对应的只读缓存快照")}
          <div class="path-list">
            <div class="path-row"><span>实际来源</span><code>${escapeHtml(source)}</code></div>
            <div class="path-row"><span>配置数据源</span><code>${escapeHtml(configuredProvider)}</code></div>
            <div class="path-row"><span>复权口径</span><code>${escapeHtml(adjustment)}</code></div>
            <div class="path-row"><span>数据截止</span><code>${escapeHtml(chart.data_date || latest?.date || "—")}</code></div>
            <div class="path-row"><span>完成截止</span><code>${escapeHtml(snapshot.completed_session_cutoff || diagnostics.completed_session_cutoff || "—")}</code></div>
            <div class="path-row"><span>快照标识</span><code>${escapeHtml(snapshot.id || snapshot.snapshot_id || chart.snapshot_id || "—")}</code></div>
            <div class="path-row"><span>清单指纹</span><code>${escapeHtml(marketSnapshotFingerprint(snapshot, "manifest_sha256"))}</code></div>
            <div class="path-row"><span>行情指纹</span><code>${escapeHtml(marketSnapshotFingerprint(snapshot, "file_sha256"))}</code></div>
          </div>
        </article>
        <article class="panel">
          ${panelHeader("完整性", stale ? "该快照需要刷新" : "聚合未制造不存在的交易日")}
          <div class="market-integrity-list">
            <div><span>请求周期</span><strong>${escapeHtml(periodLabel)}</strong></div>
            <div><span>返回根数</span><strong>${formatInteger(bars.length)}</strong></div>
            <div><span>模拟成交标记</span><strong>${formatInteger(chart.trade_markers?.length || 0)}</strong></div>
            <div><span>来源说明</span><strong>${escapeHtml(marketProvenanceSummary(chart.provenance))}</strong></div>
          </div>
          ${diagnosticMessages.length ? `<details class="market-diagnostics"><summary>查看数据诊断（${diagnosticMessages.length}）</summary><ul>${diagnosticMessages.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></details>` : ""}
          ${data.universe_error ? `<div class="callout warning"><strong>证券主数据未完整加载</strong><p>${escapeHtml(data.universe_error)}</p></div>` : ""}
        </article>
      </section>
    </div>`;
}

function marketPricePrecision(bars) {
  let precision = 2;
  for (const bar of bars) {
    for (const key of ["open", "high", "low", "close"]) {
      const value = finite(bar[key]);
      if (value === null) continue;
      const fraction = String(value).split(".")[1] || "";
      precision = Math.max(precision, Math.min(fraction.length, 4));
    }
  }
  return precision;
}

function marketChartBars(bars) {
  return bars.map((bar) => ({
    timestamp: Date.parse(`${bar.date}T00:00:00+08:00`),
    open: finite(bar.open),
    high: finite(bar.high),
    low: finite(bar.low),
    close: finite(bar.close),
    volume: finite(bar.volume) ?? 0,
    turnover: finite(bar.amount) ?? 0,
  })).filter((bar) => Number.isFinite(bar.timestamp)
    && [bar.open, bar.high, bar.low, bar.close].every((value) => value !== null))
    .sort((left, right) => left.timestamp - right.timestamp);
}

function marketIndicatorDefinition(name, paneId) {
  const calcParams = {
    MA: [5, 10, 20, 60],
    EMA: [5, 10, 20, 60],
    BOLL: [20, 2],
  }[name];
  return { name, paneId, ...(calcParams ? { calcParams } : {}) };
}

function sizeMarketPanes(chart, container) {
  const height = Math.max(container.clientHeight, 440);
  const candleHeight = Math.max(240, Math.floor(height * 0.57));
  const volumeHeight = Math.max(84, Math.floor(height * 0.17));
  const oscillatorHeight = Math.max(104, height - candleHeight - volumeHeight - 28);
  chart.setPaneOptions({ id: "candle_pane", height: candleHeight, minHeight: 220, order: 0 });
  chart.setPaneOptions({ id: "volume_pane", height: volumeHeight, minHeight: 72, order: 1 });
  chart.setPaneOptions({ id: "oscillator_pane", height: oscillatorHeight, minHeight: 88, order: 2 });
}

function ensureMarketIndicators(library) {
  const supported = typeof library.getSupportedIndicators === "function"
    ? library.getSupportedIndicators()
    : [];
  if (supported.includes("ATR")) return;
  library.registerIndicator({
    name: "ATR",
    shortName: "ATR",
    precision: 4,
    calcParams: [14],
    figures: [{ key: "atr", title: "ATR: ", type: "line" }],
    calc(dataList, indicator) {
      const requested = Number(indicator.calcParams?.[0]);
      const period = Number.isFinite(requested) ? Math.max(1, Math.floor(requested)) : 14;
      let trueRangeSum = 0;
      let previousAtr = null;
      return dataList.map((bar, index) => {
        const previousClose = index > 0 ? Number(dataList[index - 1].close) : Number(bar.close);
        const trueRange = Math.max(
          Number(bar.high) - Number(bar.low),
          Math.abs(Number(bar.high) - previousClose),
          Math.abs(Number(bar.low) - previousClose),
        );
        if (index < period) trueRangeSum += trueRange;
        if (index < period - 1) return {};
        previousAtr = index === period - 1
          ? trueRangeSum / period
          : ((previousAtr * (period - 1)) + trueRange) / period;
        return { atr: previousAtr };
      });
    },
  });
}

function themeColor(token) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return value || "transparent";
}

function marketChartColors() {
  return {
    grid: themeColor("--chart-grid"),
    gridSoft: themeColor("--chart-grid-soft"),
    axis: themeColor("--chart-axis"),
    crosshair: themeColor("--chart-crosshair"),
    separator: themeColor("--chart-separator"),
    separatorActive: themeColor("--chart-separator-active"),
    up: themeColor("--chart-candle-up"),
    down: themeColor("--chart-candle-down"),
    flat: themeColor("--chart-candle-flat"),
    series1: themeColor("--chart-series-1"),
    series2: themeColor("--chart-series-2"),
    series3: themeColor("--chart-series-3"),
    series4: themeColor("--chart-series-4"),
  };
}

function addMarketTradeMarkers(chart, markers) {
  if (!Array.isArray(markers)) return;
  for (const marker of markers.slice(-120)) {
    const timestamp = Date.parse(`${marker.bar_date}T00:00:00+08:00`);
    const price = finite(marker.price);
    const quantity = finite(marker.quantity);
    if (!Number.isFinite(timestamp) || price === null || quantity === null) continue;
    const buying = marker.side === "BUY";
    const color = buying ? themeColor("--market-up") : themeColor("--market-down");
    chart.createOverlay({
      name: "simpleAnnotation",
      paneId: "candle_pane",
      lock: true,
      zLevel: 20,
      points: [{ timestamp, value: price }],
      extendData: `${buying ? "模买" : "模卖"} ${formatInteger(quantity)}`,
      styles: {
        line: { color, style: "dashed", size: 1, dashedValue: [3, 3] },
        polygon: { color, borderColor: color },
        text: { color, size: 11 },
      },
    });
  }
}

function keepMarketChartFocusOnSummary(container) {
  for (const descendant of container.querySelectorAll("[tabindex]")) {
    descendant.tabIndex = -1;
  }
}

function initMarketChart(data) {
  const container = document.getElementById("market-kline-chart");
  const bars = marketChartBars(data.chart?.bars || []);
  if (!container || bars.length < 2) return;
  const library = window.klinecharts;
  if (!library || typeof library.init !== "function") {
    container.innerHTML = '<div class="market-chart-failure" role="alert"><strong>K 线组件未加载</strong><span>请重新启动当前版本的本地服务。</span></div>';
    return;
  }
  container.replaceChildren();
  try {
    ensureMarketIndicators(library);
    const colors = marketChartColors();
    const compactChart = container.clientWidth < 480;
    const chart = library.init(container, {
      locale: "zh-CN",
      timezone: "Asia/Shanghai",
      styles: {
        grid: {
          show: true,
          horizontal: { show: true, color: colors.grid, size: 1, style: "dashed", dashedValue: [3, 3] },
          vertical: { show: true, color: colors.gridSoft, size: 1, style: "dashed", dashedValue: [3, 3] },
        },
        candle: {
          type: "candle_solid",
          bar: {
            compareRule: "previous_close",
            upColor: colors.up,
            downColor: colors.down,
            noChangeColor: colors.flat,
            upBorderColor: colors.up,
            downBorderColor: colors.down,
            noChangeBorderColor: colors.flat,
            upWickColor: colors.up,
            downWickColor: colors.down,
            noChangeWickColor: colors.flat,
          },
          priceMark: {
            high: { color: colors.axis },
            low: { color: colors.axis },
            last: {
              compareRule: "previous_close",
              upColor: colors.up,
              downColor: colors.down,
              noChangeColor: colors.flat,
            },
          },
          tooltip: { showRule: compactChart ? "follow_cross" : "always" },
        },
        indicator: {
          ohlc: {
            compareRule: "previous_close",
            upColor: colors.up,
            downColor: colors.down,
            noChangeColor: colors.flat,
          },
          lines: [
            { color: colors.series1 },
            { color: colors.series2 },
            { color: colors.series3 },
            { color: colors.series4 },
          ],
          bars: [{
            upColor: colors.up,
            downColor: colors.down,
            noChangeColor: colors.flat,
          }],
          tooltip: { showRule: compactChart ? "follow_cross" : "always" },
        },
        xAxis: {
          axisLine: { color: colors.separator },
          tickLine: { color: colors.separator },
          tickText: { color: colors.axis, family: "Cascadia Mono, Consolas, monospace" },
        },
        yAxis: {
          axisLine: { color: colors.separator },
          tickLine: { color: colors.separator },
          tickText: { color: colors.axis, family: "Cascadia Mono, Consolas, monospace" },
        },
        separator: { color: colors.separator, activeBackgroundColor: colors.separatorActive },
        crosshair: {
          horizontal: { line: { color: colors.crosshair } },
          vertical: { line: { color: colors.crosshair } },
        },
      },
    });
    if (!chart) throw new Error("KLineChart init returned no chart instance");
    state.marketChart = chart;
    chart.setDataLoader({
      getBars({ type, callback }) {
        callback(type === "init" ? bars : [], { backward: false, forward: false });
      },
    });
    const instrument = data.chart?.instrument || {};
    chart.setSymbol({
      ticker: instrument.symbol || state.marketSymbol,
      name: instrumentName(instrument.symbol || state.marketSymbol, instrument.name),
      pricePrecision: marketPricePrecision(bars),
      volumePrecision: 0,
    });
    state.marketIndicatorIds.volume = chart.createIndicator({ name: "VOL", paneId: "volume_pane" }, false);
    if (state.marketOverlay !== "none") {
      state.marketIndicatorIds.overlay = chart.createIndicator(
        marketIndicatorDefinition(state.marketOverlay, "candle_pane"),
        true,
      );
    }
    state.marketIndicatorIds.oscillator = chart.createIndicator(
      marketIndicatorDefinition(state.marketOscillator, "oscillator_pane"),
      false,
    );
    sizeMarketPanes(chart, container);
    chart.setOffsetRightDistance(36);
    chart.setPeriod(MARKET_PERIODS[state.marketPeriod]?.chart || MARKET_PERIODS.day.chart);
    addMarketTradeMarkers(chart, data.chart?.trade_markers || []);
    keepMarketChartFocusOnSummary(container);
    state.marketResizeObserver = new ResizeObserver(() => {
      if (state.marketChart !== chart || !container.isConnected) return;
      chart.resize();
      sizeMarketPanes(chart, container);
      keepMarketChartFocusOnSummary(container);
    });
    state.marketResizeObserver.observe(container);
  } catch (error) {
    destroyMarketChart();
    container.setAttribute("role", "alert");
    container.removeAttribute("aria-label");
    container.removeAttribute("aria-describedby");
    container.tabIndex = -1;
    container.innerHTML = `<div class="market-chart-failure"><strong>K 线绘制失败</strong><span>${escapeHtml(error.message || error)}</span><span>行情数据仍保持只读，刷新视图后可重试绘制。</span></div>`;
  }
}

function destroyMarketChart() {
  state.marketResizeObserver?.disconnect();
  state.marketResizeObserver = null;
  if (state.marketChart && window.klinecharts?.dispose) {
    try {
      window.klinecharts.dispose(state.marketChart);
    } catch {
      // The chart DOM may already have been replaced during route navigation.
    }
  }
  state.marketChart = null;
  state.marketIndicatorIds = {};
}

function setMarketIndicator(kind, value) {
  const chart = state.marketChart;
  if (kind === "overlay") {
    state.marketOverlay = value;
  } else {
    state.marketOscillator = value;
  }
  if (!chart) {
    const payload = state.data.get("market");
    if (payload) renderRoute(payload);
    return;
  }
  const currentId = state.marketIndicatorIds[kind];
  if (currentId) chart.removeIndicator({ id: currentId });
  state.marketIndicatorIds[kind] = null;
  if (kind === "overlay" && value === "none") return;
  const paneId = kind === "overlay" ? "candle_pane" : "oscillator_pane";
  const indicatorId = chart.createIndicator(
    marketIndicatorDefinition(value, paneId),
    kind === "overlay",
  );
  if (!indicatorId) {
    notify(`${value} 指标不可用`, true);
    return;
  }
  state.marketIndicatorIds[kind] = indicatorId;
  const container = document.getElementById("market-kline-chart");
  if (container) sizeMarketPanes(chart, container);
}

function renderOverview(data) {
  const backtest = data.research?.backtest || {};
  const walk = data.research?.walk_forward || {};
  const audit = data.paper?.audit || {};
  const paperMetrics = audit.metrics || {};
  const targets = Object.entries(data.signal?.target_weights || {});
  const ranking = data.signal?.ranking || [];
  const warnings = data.market?.warnings || [];
  const fallbackUsed = warnings.some((item) => String(item).includes("Tencent network fallback"));
  const reportWarnings = warnings.filter((item) => /\.json /.test(String(item)));
  const operationalWarnings = warnings.filter((item) => (
    String(item).includes("network fallback") || /\.json /.test(String(item))
  ));
  const methodologyWarnings = warnings.filter((item) => !operationalWarnings.includes(item));
  const freshness = data.market?.freshness || {};
  const marketDecisionDate = freshness.latest_common_market_date || data.market?.date;
  const signalMatchesMarket = Boolean(data.signal?.date) && data.signal?.date === marketDecisionDate;
  const freshnessLag = finite(freshness.lag_calendar_days);
  const marketCurrent = freshness.current !== false;
  const marketStatus = data.market?.available === false
    ? "不可用"
    : !marketCurrent
      ? `滞后 ${formatInteger(freshnessLag)} 日`
      : fallbackUsed
        ? "备用源补齐"
        : "已校验";
  const marketStatusKind = data.market?.available === false
    ? "danger"
    : !marketCurrent || fallbackUsed
      ? "warning"
      : "success";
  const freshnessNote = `共同最新 ${freshness.latest_common_market_date || data.market?.date || "—"} · 完成截止 ${freshness.completed_session_cutoff || "—"}`;
  const grossExposure = targets.reduce((total, [, weight]) => total + (finite(weight) || 0), 0);
  const researchEnd = data.research?.period?.[1] || "—";
  const actions = [
    actionButton("paper-run", "primary"),
    actionButton("refresh-data", "secondary"),
  ].join("");
  return `
    <div class="page-stack">
      ${pageIntro("收盘决策快照", `信号 ${data.signal?.date || "—"} · ${data.market?.universe?.active_count ?? 0} 支当日有效证券`, actions)}

      ${contextBand([
        {
          label: "行情快照",
          value: marketDecisionDate || "不可用",
          status: marketStatus,
          kind: marketStatusKind,
          note: `${freshnessNote} · ${marketProviderLabel(data.market?.provider)} · ${data.market?.universe?.active_count ?? 0} 支有效`,
        },
        {
          label: "策略信号",
          value: data.signal?.date || "不可用",
          status: !data.signal?.date ? "尚无信号" : signalMatchesMarket ? "与行情同日" : "日期不一致",
          kind: !data.signal?.date ? "neutral" : signalMatchesMarket ? "success" : "danger",
          note: !data.signal?.date
            ? "当前保持现金，等待完整行情快照"
            : `${targets.length} 个目标 · 风险仓位 ${formatPercent(grossExposure)}`,
        },
        {
          label: "历史研究",
          value: `截至 ${researchEnd}`,
          status: reportWarnings.length ? `${reportWarnings.length} 份待更新` : "报告当前",
          kind: reportWarnings.length ? "warning" : "success",
          note: "历史指标不参与权限解锁",
        },
        {
          label: "账户权限",
          value: "前向模拟",
          status: "真实交易锁定",
          kind: "danger",
          note: `${audit.sessions ?? 0} / ${audit.minimum_promotion_sessions ?? 60} 个独立交易日`,
        },
      ], "决策日期与可信度")}

      ${overviewQualityNotice(operationalWarnings, reportWarnings.length)}

      <section class="metric-strip metric-strip-priority" aria-label="当前模拟账户指标">
        ${metric("模拟权益", formatMoney(data.paper?.equity), `账本截至 ${audit.period?.[1] || marketDecisionDate || "—"}`)}
        ${metric("前向累计收益", formatPercent(paperMetrics.total_return), `${audit.sessions ?? 0} 个独立交易日`, tone(paperMetrics.total_return))}
        ${metric("前向最大回撤", formatPercent(paperMetrics.max_drawdown), "相对模拟账户高水位", tone(paperMetrics.max_drawdown))}
        ${metric("证据进度", `${audit.sessions ?? 0} / ${audit.minimum_promotion_sessions ?? 60}`, `距沙箱复核尚需 ${audit.remaining_sessions ?? audit.minimum_promotion_sessions ?? 60} 日`, audit.eligible_for_broker_sandbox ? "tone-positive" : "tone-warning")}
      </section>

      <section class="split-layout">
        <article class="panel">
          ${panelHeader("最新目标权重", translateSignalReason(data.signal?.reason))}
          <div class="table-wrap">
            <table class="data-table compact">
              <thead><tr><th>证券</th><th class="numeric">目标权重</th><th class="numeric">动量</th><th>趋势</th></tr></thead>
              <tbody>
                ${targets.length ? targets.map(([symbol, weight]) => {
                  const ranked = ranking.find((item) => item.symbol === symbol) || {};
                  return `<tr>
                    <td class="symbol-cell"><strong>${escapeHtml(symbol)}</strong><span>${escapeHtml(instrumentName(symbol, ranked.name))}</span></td>
                    <td class="numeric">${formatPercent(weight)}</td>
                    <td class="numeric ${tone(ranked.momentum)}">${formatPercent(ranked.momentum, true)}</td>
                    <td>${booleanChip(Boolean(ranked.above_trend), "趋势上方", "趋势下方")}</td>
                  </tr>`;
                }).join("") : emptyRow(4, "当前信号保持现金，未生成持仓目标")}
              </tbody>
            </table>
          </div>
        </article>

        <article class="panel">
          ${panelHeader("权限晋级", "历史结果不会自动解锁真实交易")}
          ${authorityRail(data.live || {}, audit, data.research?.gates || {})}
        </article>
      </section>

      <section class="panel">
        ${panelHeader(
          "历史权益曲线",
          `${data.research?.period?.[0] || "—"} 至 ${researchEnd} · 与当前行情快照分开审阅`,
          reportWarnings.length ? statusChip("报告待更新", "warning") : statusChip("报告当前", "success"),
        )}
        <section class="metric-strip metric-strip-secondary" aria-label="历史研究指标">
          ${metric("历史年化收益", formatPercent(backtest.cagr), `报告截止 ${researchEnd}`, tone(backtest.cagr))}
          ${metric("历史最大回撤", formatPercent(backtest.max_drawdown), "回测观测值", tone(backtest.max_drawdown))}
          ${metric("样本外 Sharpe", formatNumber(walk.oos_sharpe), `${walk.positive_segments ?? 0} / ${walk.segments ?? 0} 段收益为正`, tone(walk.oos_sharpe))}
          ${metric("研究报告", reportWarnings.length ? `${reportWarnings.length} 份待更新` : "全部当前", `行情快照 ${marketDecisionDate || "—"}`, reportWarnings.length ? "tone-warning" : "tone-positive")}
        </section>
        ${chartMarkup(
          "overview-equity",
          data.equity_curve || [],
          [
            { key: "strategy_equity", label: "策略", color: "--chart-primary" },
            { key: "benchmark_equity", label: "沪深300基准", color: "--chart-secondary" },
          ],
          `策略累计收益 ${formatPercent(backtest.total_return)}，最大回撤 ${formatPercent(backtest.max_drawdown)}；基准累计收益 ${formatPercent(data.research?.benchmark?.total_return)}。`
        )}
      </section>

      <section class="panel">
        ${panelHeader("信号排序明细", "排序不是订单；成交仍受流动性、整手、成本和风险约束")}
        ${rankingTable(ranking)}
      </section>

      ${methodologyWarnings.length ? `<aside class="callout warning"><strong>研究边界</strong><ul>${methodologyWarnings.map((item) => `<li>${escapeHtml(translateWarning(item))}</li>`).join("")}</ul></aside>` : ""}
    </div>`;
}

function overviewQualityNotice(warnings, reportCount) {
  if (!warnings.length) return "";
  const title = reportCount
    ? `当前快照有 ${reportCount} 份研究报告需要更新`
    : "当前快照存在需要复核的数据状态";
  return `<details class="exception-panel warning">
    <summary>
      <span class="exception-severity">数据待复核</span>
      <strong>${escapeHtml(title)}</strong>
      <span class="exception-count">${warnings.length} 项</span>
    </summary>
    <div class="exception-body">
      <p>先区分当前行情、历史报告与权限证据，再决定是否运行模拟日。</p>
      <ul>${warnings.map((item) => `<li>${escapeHtml(translateWarning(item))}</li>`).join("")}</ul>
      <div class="action-row"><a class="button secondary" href="#research">查看研究证据</a></div>
    </div>
  </details>`;
}

function rankingTable(ranking) {
  return `
    <div class="table-wrap">
      <table class="data-table">
        <thead><tr><th>排名</th><th>证券</th><th class="numeric">动量</th><th class="numeric">年化波动</th><th class="numeric">日均成交额</th><th class="numeric">建议权重</th></tr></thead>
        <tbody>
          ${ranking.length ? ranking.map((item, index) => `<tr>
            <td class="numeric">${index + 1}</td>
            <td class="symbol-cell"><strong>${escapeHtml(item.symbol)}</strong><span>${escapeHtml(instrumentName(item.symbol, item.name))}</span></td>
            <td class="numeric ${tone(item.momentum)}">${formatPercent(item.momentum, true)}</td>
            <td class="numeric">${formatPercent(item.annual_volatility)}</td>
            <td class="numeric">${finite(item.average_amount) === null ? "—" : compactFormatter.format(Number(item.average_amount))}</td>
            <td class="numeric">${formatPercent(item.weight)}</td>
          </tr>`).join("") : emptyRow(6, "暂无排序结果")}
        </tbody>
      </table>
    </div>`;
}

function reconciliationExclusionNote(reconciliation) {
  const legacy = Number(reconciliation.ignored_legacy_sessions || 0);
  const incomplete = Number(reconciliation.ignored_incomplete_sessions || 0);
  const completedThrough = reconciliation.completed_through || "";
  return [
    legacy > 0 ? `${legacy} 次旧格式对账不计入` : "",
    incomplete > 0 ? `${incomplete} 次晚于已完成行情日，暂不计入${completedThrough ? `（截止 ${completedThrough}）` : ""}` : "",
  ].filter(Boolean).join("；");
}

function authorityRail(live, audit, researchGates) {
  const reconciliation = live.reconciliation || {};
  const exclusionNote = reconciliationExclusionNote(reconciliation);
  const researchKnown = Boolean(researchGates.total);
  const researchPassed = researchKnown && researchGates.passed === researchGates.total;
  const paperPassed = Boolean(audit.eligible_for_broker_sandbox);
  const sandboxPassed = Boolean(reconciliation.eligible);
  const liveReady = Boolean(live.live_ready);
  const steps = [
    {
      label: "历史研究",
      note: !researchKnown ? "当前页面未返回研究门禁，请到研究页复核" : researchPassed ? "稳健性门禁通过，但结果已参与开发" : "仍有研究门禁未通过",
      complete: researchPassed,
      current: researchKnown && !researchPassed,
      unknown: !researchKnown,
    },
    {
      label: "前向模拟",
      note: `${audit.sessions ?? 0} / ${audit.minimum_promotion_sessions ?? 60} 个独立交易日`,
      complete: paperPassed,
      current: (researchPassed || !researchKnown) && !paperPassed,
    },
    {
      label: "券商沙箱",
      note: `${reconciliation.clean_sessions ?? 0} / ${reconciliation.minimum_sessions ?? 20} 次连续干净对账${exclusionNote ? `；${exclusionNote}` : ""}`,
      complete: sandboxPassed,
      current: paperPassed && !sandboxPassed,
    },
    {
      label: "真实交易",
      note: liveReady ? "本次授权门禁全部通过" : "适配器、账户、授权和紧急停止仍受控",
      complete: liveReady,
      current: false,
      locked: !liveReady,
    },
  ];
  return `<ol class="authority-rail">${steps.map((step) => `
    <li class="authority-step ${step.complete ? "complete" : ""} ${step.current ? "current" : ""}"${step.current ? ' aria-current="step"' : ""}>
      <span class="step-marker" aria-hidden="true">${step.complete ? "✓" : step.locked ? "×" : step.unknown ? "?" : "·"}</span>
      <div><strong>${escapeHtml(step.label)}</strong><span>${escapeHtml(step.note)}</span></div>
      ${step.complete ? statusChip("已通过", "success") : step.current ? statusChip("当前阶段", "warning") : step.unknown ? statusChip("待复核", "neutral") : statusChip(step.locked ? "已锁定" : "未开始", step.locked ? "danger" : "neutral")}
    </li>`).join("")}</ol>`;
}

function translateWarning(value) {
  if (String(value).includes("curated_static")) {
    return "当前投资池为静态人工筛选，不能消除幸存者偏差。";
  }
  if (String(value).includes("Adjusted bars")) {
    return "模拟成交仍使用复权日线；真实成交前必须切换为可对账的原始行情与公司行动口径。";
  }
  if (String(value).includes("validated local fallback")) {
    return "最近一次刷新有部分证券使用了已验证的本地备用缓存，请检查数据提供方是否可用。";
  }
  const tencentFallback = String(value).match(
    /used Tencent network fallback data for (\d+) instrument\(s\)/
  );
  if (tencentFallback) {
    return `最近一次刷新在东方财富不可用后，已通过腾讯行情备用源更新 ${tencentFallback[1]} 支证券；数据已补齐，但主数据源连接仍需检查。`;
  }
  const report = String(value).match(/^(.*?\.json) (?:was generated from a different data snapshot|is missing|is not valid JSON|does not contain a verifiable data snapshot)/);
  if (report) {
    return `${report[1]} 与当前行情快照不一致或不可用，请重新运行对应研究任务。`;
  }
  return value;
}

function translateSignalReason(value) {
  if (!value) return "暂无信号原因";
  const selected = String(value).match(
    /^Selected (.*?); gross exposure ([\d.]+%); estimated volatility ([\d.]+%); weighting (.*?); risk model (.*?); portfolio constraints applied$/
  );
  if (selected) {
    const weighting = {
      inverse_volatility: "逆波动率",
      risk_parity: "风险平价",
    }[selected[4]] || selected[4];
    const riskModel = {
      conservative_sum: "保守波动上界",
      covariance: "协方差模型",
    }[selected[5]] || selected[5];
    return `已选择 ${selected[1]}；总风险仓位 ${selected[2]}；估计年化波动 ${selected[3]}；采用${weighting}权重与${riskModel}，并已应用组合约束。`;
  }
  if (String(value).includes("No eligible assets")) {
    return "当前没有同时通过动量、趋势、流动性和容量约束的证券，目标保持现金。";
  }
  return String(value)
    .replace("Paper risk stop", "模拟账户风险止损")
    .replace("Paper risk cooldown", "模拟账户风险冷却")
    .replace("Hold; next scheduled rebalance in", "继续持有；距离下一次计划调仓还有")
    .replace("sessions", "个交易日");
}

function translateDisclosure(value) {
  if (!value) return "";
  if (String(value).startsWith("Current defaults were compared")) {
    return "当前默认参数已经在这些历史窗口中参与比较，因此这是一份开发期滚动验证，而不是未触碰的最终留出集。";
  }
  if (String(value).startsWith("The current liquidity threshold")) {
    return "当前流动性阈值和风险模型已经参考历史与滚动结果；这些结果属于开发证据，下一份独立证据只能来自未来模拟盘。";
  }
  return value;
}

const JOURNAL_CATEGORY_LABELS = {
  observation: "观察",
  decision: "研究决定",
  trade_review: "操作复盘",
  risk: "风险",
  strategy: "策略",
  weekly_review: "周度复盘",
};

const JOURNAL_DECISION_LABELS = {
  not_recorded: "未记录观点",
  watch: "继续观察",
  consider_increase: "研究增配",
  hold: "维持研究判断",
  consider_reduce: "研究减配",
  avoid: "暂不纳入",
};

function journalCategoryKind(value) {
  return {
    decision: "info",
    risk: "warning",
    strategy: "info",
    trade_review: "neutral",
    weekly_review: "neutral",
  }[value] || "neutral";
}

function journalDecisionKind(value) {
  return {
    watch: "info",
    consider_increase: "success",
    hold: "neutral",
    consider_reduce: "warning",
    avoid: "danger",
  }[value] || "neutral";
}

function localCalendarDate() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function journalOption(value, label, selected) {
  return `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`;
}

function researchJournal(data) {
  if (!data?.available) {
    const message = data?.errors?.[0]?.message || "研究日志存储暂时不可用。";
    return `
      <section class="journal-panel" aria-labelledby="research-journal-heading">
        <header class="journal-heading">
          <div><h2 id="research-journal-heading">研究日志</h2><p>人工判断与当时证据的不可变记录</p></div>
          ${statusChip("写入已暂停", "danger")}
        </header>
        <div class="empty-state journal-error" role="alert">
          <h3>无法安全读取研究日志</h3>
          <p>${escapeHtml(message)}</p>
          <button class="button secondary" type="button" data-retry>重新读取</button>
        </div>
      </section>`;
  }
  const entries = Array.isArray(data.entries) ? data.entries : [];
  const summary = data.summary || {};
  const filters = data.filters || {};
  const symbols = Array.isArray(data.options?.symbols) ? data.options.symbols : [];
  const categories = Array.isArray(data.options?.categories)
    ? data.options.categories
    : Object.keys(JOURNAL_CATEGORY_LABELS);
  const decisions = Array.isArray(data.options?.decisions)
    ? data.options.decisions
    : Object.keys(JOURNAL_DECISION_LABELS);
  const correctionId = state.journalCorrectionOf;
  const correctionEntry = entries.find((entry) => entry.entry_id === correctionId);
  const defaultDate = localCalendarDate();
  const totalLabel = `${formatInteger(summary.total || 0)} 条不可变记录`;
  return `
    <section class="journal-panel" aria-labelledby="research-journal-heading">
      <header class="journal-heading">
        <div>
          <h2 id="research-journal-heading">研究日志</h2>
          <p>把收盘判断、风险复核与操作理由绑定到当时的行情和策略指纹</p>
        </div>
        <div class="journal-heading-status">
          ${statusChip("研究记录", "info")}
          <span>${escapeHtml(totalLabel)}</span>
        </div>
      </header>
      <div class="journal-layout">
        <div class="journal-compose">
          <div class="journal-section-title">
            <h3>新增记录</h3>
            <span>提交后不可覆盖</span>
          </div>
          <form id="research-journal-form" class="journal-form" aria-describedby="research-journal-boundary research-journal-status">
            <input type="hidden" name="correction_of" value="${escapeHtml(correctionId)}">
            <div id="research-journal-correction" class="journal-correction"${correctionId ? "" : " hidden"}>
              <span>正在追加修正：<code>${escapeHtml(correctionId ? shortCandidateId(correctionId) : "")}</code>${correctionEntry ? ` · ${escapeHtml(correctionEntry.title)}` : ""}</span>
              <button class="button ghost compact" type="button" data-journal-correction-cancel>取消修正</button>
            </div>
            <div class="journal-field-grid">
              <label class="field"><span>研究日期</span><input name="research_date" type="date" value="${escapeHtml(defaultDate)}" required></label>
              <label class="field"><span>记录类型</span><select name="category" required>${categories.map((value) => journalOption(value, JOURNAL_CATEGORY_LABELS[value] || value, "observation")).join("")}</select></label>
              <label class="field"><span>关联证券</span><select name="symbol"><option value="">组合 / 全市场</option>${symbols.map((symbol) => journalOption(symbol, `${symbol} · ${instrumentName(symbol)}`, "")).join("")}</select></label>
              <label class="field"><span>研究观点</span><select name="decision" data-journal-decision>${decisions.map((value) => journalOption(value, JOURNAL_DECISION_LABELS[value] || value, "not_recorded")).join("")}</select></label>
              <label class="field"><span>确信度</span><div class="unit-input"><input name="confidence" data-journal-confidence type="number" min="0" max="100" step="5" inputmode="numeric" value="" disabled><span>%</span></div></label>
            </div>
            <label class="field"><span>标题</span><input name="title" maxlength="80" autocomplete="off" required></label>
            <label class="field"><span>证据、理由与待验证项</span><textarea name="note" rows="5" maxlength="4000" required></textarea></label>
            <p id="research-journal-boundary" class="journal-boundary"><strong>权限边界：</strong>日志只保存研究证据，不会改变策略、模拟账户、订单或券商权限。</p>
            <div class="form-footer">
              <span>如需更正，请在原记录上选择“追加修正”；旧记录会继续保留。</span>
              <button class="button primary" type="submit"${state.journalBusy ? ' disabled aria-busy="true"' : ""}>${state.journalBusy ? "正在写入" : "写入研究日志"}</button>
            </div>
            <p id="research-journal-status" class="form-status" role="status" aria-live="polite"></p>
          </form>
        </div>
        <div class="journal-ledger">
          <div class="journal-section-title">
            <h3>审计时间线</h3>
            <span>显示 ${formatInteger(summary.returned || 0)} / 匹配 ${formatInteger(summary.matched || 0)}</span>
          </div>
          <form id="research-journal-filter-form" class="journal-filter" role="search" aria-label="筛选研究日志">
            <label class="field"><span>类型</span><select name="category"><option value="">全部类型</option>${categories.map((value) => journalOption(value, JOURNAL_CATEGORY_LABELS[value] || value, filters.category || "")).join("")}</select></label>
            <label class="field"><span>证券</span><select name="symbol"><option value="">全部证券</option>${symbols.map((symbol) => journalOption(symbol, `${symbol} · ${instrumentName(symbol)}`, filters.symbol || "")).join("")}</select></label>
            <label class="field journal-search"><span>关键词</span><input name="query" type="search" maxlength="80" value="${escapeHtml(filters.query || "")}" placeholder="标题、理由、记录人或编号"></label>
            <label class="field"><span>最多显示</span><select name="limit">${[50, 100, 200].map((value) => journalOption(String(value), `${value} 条`, String(filters.limit || 100))).join("")}</select></label>
            <div class="journal-filter-actions">
              <button class="button secondary" type="submit">应用筛选</button>
              <button class="button ghost" type="button" data-journal-filter-clear>清除</button>
            </div>
          </form>
          ${journalTimeline(entries, summary)}
        </div>
      </div>
    </section>`;
}

function journalTimeline(entries, summary) {
  if (!entries.length) {
    const filtered = Number(summary?.total || 0) > 0;
    return `<div class="empty-state journal-empty" role="status">
      <h3>${filtered ? "筛选条件下没有记录" : "尚无研究记录"}</h3>
      <p>${filtered ? "调整类型、证券或关键词后重新筛选；原始记录没有被删除。" : "左侧写入第一条收盘观察，系统会同时固化行情与策略证据。"}</p>
      ${filtered ? '<button class="button secondary" type="button" data-journal-filter-clear>清除筛选</button>' : ""}
    </div>`;
  }
  const groups = new Map();
  entries.forEach((entry) => {
    const week = entry.week_start || entry.research_date || "日期未知";
    if (!groups.has(week)) groups.set(week, []);
    groups.get(week).push(entry);
  });
  return `<div class="journal-timeline">
    ${[...groups.entries()].map(([week, rows]) => `
      <section class="journal-week" aria-labelledby="journal-week-${escapeHtml(week)}">
        <header><h4 id="journal-week-${escapeHtml(week)}">${escapeHtml(week)} 起</h4><span>${formatInteger(rows.length)} 条</span></header>
        ${rows.map(journalEntry).join("")}
      </section>`).join("")}
    ${summary?.truncated ? `<p class="journal-truncated" role="status">当前只显示最近 ${formatInteger(summary.returned)} 条匹配记录；收窄筛选条件可继续查找。</p>` : ""}
  </div>`;
}

function journalEntry(entry) {
  const market = entry.evidence?.market_snapshot || {};
  const strategy = entry.evidence?.strategy || {};
  const decision = JOURNAL_DECISION_LABELS[entry.decision] || entry.decision || "未记录观点";
  const confidence = entry.confidence === null || entry.confidence === undefined
    ? decision
    : `${decision} · ${formatInteger(entry.confidence)}%`;
  const corrected = entry.correction_of
    ? `<span class="journal-reference">修正自 <code>${escapeHtml(shortCandidateId(entry.correction_of))}</code></span>`
    : "";
  return `
    <article class="journal-entry" id="${escapeHtml(entry.entry_id)}">
      <div class="journal-entry-head">
        <div>
          <div class="journal-entry-labels">
            ${statusChip(JOURNAL_CATEGORY_LABELS[entry.category] || entry.category, journalCategoryKind(entry.category))}
            ${statusChip(confidence, journalDecisionKind(entry.decision))}
            ${entry.symbol ? `<span class="journal-symbol">${escapeHtml(entry.symbol)} · ${escapeHtml(instrumentName(entry.symbol))}</span>` : '<span class="journal-symbol">组合 / 全市场</span>'}
          </div>
          <h4>${escapeHtml(entry.title)}</h4>
        </div>
        <time datetime="${escapeHtml(entry.research_date)}">${escapeHtml(entry.research_date)}</time>
      </div>
      <p class="journal-entry-note">${escapeHtml(entry.note).replaceAll("\n", "<br>")}</p>
      <div class="journal-entry-meta">
        <span>记录人 ${escapeHtml(entry.actor || "本地所有者")}</span>
        <span>写入 ${escapeHtml(formatDate(entry.created_at, true))}</span>
        ${corrected}
      </div>
      <details class="journal-evidence">
        <summary>证据指纹与权限边界</summary>
        <dl>
          <div><dt>行情快照</dt><dd>${market.available ? `${escapeHtml(market.date)}<code>${escapeHtml(market.fingerprint)}</code>` : '<span>写入时不可用</span>'}</dd></div>
          <div><dt>策略版本</dt><dd>${strategy.available ? `${escapeHtml(strategy.candidate_id ? shortCandidateId(strategy.candidate_id) : "默认配置")} · ${escapeHtml(strategy.lifecycle_state)}<code>${escapeHtml(strategy.fingerprint)}</code>` : '<span>写入时不可用</span>'}</dd></div>
          <div><dt>日志完整性</dt><dd><code>${escapeHtml(entry.entry_fingerprint)}</code></dd></div>
          <div><dt>执行权限</dt><dd><span>未授权；不改变策略、账户、订单或券商权限</span></dd></div>
        </dl>
      </details>
      <div class="journal-entry-actions">
        <code>${escapeHtml(entry.entry_id)}</code>
        <button class="button ghost compact" type="button" data-journal-correct="${escapeHtml(entry.entry_id)}">追加修正</button>
      </div>
    </article>`;
}

const RESEARCH_ARCHIVE_STATUS = {
  current: ["证据一致", "success"],
  provisional: ["本周未收完", "warning"],
  partial: ["部分可用", "warning"],
  missing_report: ["缺少日报", "warning"],
  unbound_report: ["日报未绑定账本", "danger"],
  evidence_mismatch: ["证据不一致", "danger"],
  journal_only: ["仅研究日志", "neutral"],
  empty: ["暂无归档", "neutral"],
  unavailable: ["归档不可用", "danger"],
};

function researchArchiveStatus(value) {
  return RESEARCH_ARCHIVE_STATUS[String(value || "").toLowerCase()] || ["状态待确认", "neutral"];
}

function researchArchiveStatusChip(value) {
  const [label, kind] = researchArchiveStatus(value);
  return statusChip(label, kind);
}

function researchDigestKindLabel(value) {
  return value === "weekly" ? "周报" : "日报";
}

function researchDigestTriggerLabel(value) {
  return {
    manual: "人工生成",
    scheduled: "标记为定时",
    backfill: "历史补录",
    rebuild: "证据重建",
  }[value] || value || "来源待确认";
}

function researchDigestLedgerStatus(digests) {
  if (!digests.length) return ["尚未生成", "neutral"];
  if (digests.some((item) => ["unavailable", "unbound_report", "evidence_mismatch"].includes(item.status))) {
    return ["含异常证据", "danger"];
  }
  if (digests.some((item) => ["partial", "missing_report"].includes(item.status))) {
    return ["含不完整证据", "warning"];
  }
  if (digests.some((item) => item.status === "provisional")) {
    return ["含周内暂存", "warning"];
  }
  return ["归档可审计", "success"];
}

function researchDigests(data) {
  const available = Boolean(data?.available);
  const digests = Array.isArray(data?.digests) ? data.digests : [];
  const summary = data?.summary || {};
  const error = data?.errors?.[0] || {};
  const busy = state.researchDigestBusy;
  const command = `
    <button class="button primary" type="button" data-research-digest-generate aria-describedby="research-digest-boundary research-digest-status"${busy || !available ? " disabled" : ""} aria-busy="${busy}">
      ${busy ? "正在写入归档" : "生成收盘归档"}
    </button>`;
  if (!available) {
    const recovery = error.recovery_action && state.actions.includes(error.recovery_action)
      ? actionButton(error.recovery_action, "secondary")
      : '<button class="button secondary" type="button" data-retry>重新读取</button>';
    return `
      <section class="digest-panel" aria-labelledby="research-digest-heading">
        <header class="digest-heading">
          <div><h2 id="research-digest-heading">版本化研究归档</h2><p>按用户和模拟账户账期隔离的不可变日报与周报</p></div>
          ${researchArchiveStatusChip("unavailable")}
        </header>
        <div class="empty-state digest-empty" role="alert">
          <h3>暂时无法读取持久化归档</h3>
          <p>${escapeHtml(error.message || "模拟账户尚未初始化，或归档证据无法通过完整性校验。")}</p>
          <div class="action-row">${recovery}</div>
        </div>
      </section>`;
  }
  const latestRevision = digests.reduce(
    (maximum, item) => Math.max(maximum, Number(item.revision || 0)),
    0,
  );
  const [ledgerLabel, ledgerKind] = researchDigestLedgerStatus(digests);
  const statusKind = ["pending", "success", "warning", "error"].includes(state.researchDigestStatusKind)
    ? ` ${state.researchDigestStatusKind}`
    : "";
  return `
    <section class="digest-panel" aria-labelledby="research-digest-heading">
      <header class="digest-heading">
        <div>
          <h2 id="research-digest-heading">版本化研究归档</h2>
          <p>证据未变化时复用原版本；日报、日志或交易日历变化时追加修订并保留旧版</p>
        </div>
        <div class="digest-heading-actions">
          ${statusChip(ledgerLabel, ledgerKind)}
          ${command}
        </div>
      </header>
      <dl class="digest-summary" aria-label="归档摘要">
        <div><dt>归档周期</dt><dd>${formatInteger(summary.total_chains || 0)}</dd></div>
        <div><dt>保留版本</dt><dd>${formatInteger(summary.total_revisions || 0)}</dd></div>
        <div><dt>最高修订</dt><dd>${latestRevision ? `r${formatInteger(latestRevision)}` : "—"}</dd></div>
        <div><dt>账户账期</dt><dd><code>${escapeHtml(shortFingerprint(data.account_fingerprint))}</code></dd></div>
      </dl>
      <p id="research-digest-status" class="form-status${statusKind}" role="status" aria-live="polite">${escapeHtml(state.researchDigestStatus || "")}</p>
      ${digests.length ? `
        <ol class="digest-timeline" aria-label="日报和周报版本时间线">
          ${digests.map(researchDigestEntry).join("")}
        </ol>
        ${summary.truncated ? `<p class="digest-truncated" role="status">当前只显示最近 ${formatInteger(summary.returned || digests.length)} 个版本；可通过归档接口按周期查询完整修订链。</p>` : ""}
      ` : `
        <div class="empty-state digest-empty" role="status">
          <h3>尚无持久化日报或周报</h3>
          <p>生成一次收盘归档后，系统会把当前账本、日报、研究日志和交易日历指纹绑定为第一个版本。</p>
        </div>`}
      <p id="research-digest-boundary" class="digest-boundary"><strong>权限边界：</strong>归档只追加研究证据，不刷新行情、不改策略、不写模拟账本，也不会创建订单或开放实盘权限。</p>
    </section>`;
}

function researchDigestEntry(item) {
  const payload = item.payload || {};
  const isWeekly = item.kind === "weekly";
  const periodLabel = isWeekly
    ? `${item.period_start || "—"} 至 ${item.period_end || "—"}`
    : item.period_start || "—";
  const performance = isWeekly
    ? `周收益 ${formatPercent(payload.period_return, true)} · ${formatInteger(payload.included_sessions || 0)} 个账本交易日`
    : `日收益 ${formatPercent(payload.daily_return, true)} · 权益 ${formatMoney(payload.equity)}`;
  const journalCount = isWeekly
    ? Number(payload.journal_count || 0)
    : Number(payload.journal?.entry_count || 0);
  return `
    <li class="digest-entry">
      <div class="digest-entry-main">
        <div class="digest-entry-title">
          <span class="digest-kind">${researchDigestKindLabel(item.kind)}</span>
          <strong>${escapeHtml(periodLabel)}</strong>
          ${researchArchiveStatusChip(item.status)}
        </div>
        <p>${escapeHtml(performance)} · ${formatInteger(journalCount)} 条研究日志</p>
      </div>
      <dl class="digest-entry-meta">
        <div><dt>修订</dt><dd><strong>r${formatInteger(item.revision)}</strong>${item.supersedes ? "，追加修订" : "，首版"}</dd></div>
        <div><dt>生成</dt><dd>${escapeHtml(formatDate(item.created_at, true))} · ${escapeHtml(researchDigestTriggerLabel(item.trigger))}</dd></div>
      </dl>
      <details class="digest-evidence">
        <summary>来源指纹与修订链</summary>
        <dl>
          <div><dt>归档指纹</dt><dd><code>${escapeHtml(item.digest_fingerprint || "—")}</code></dd></div>
          <div><dt>来源绑定</dt><dd><code>${escapeHtml(item.source_binding_fingerprint || "—")}</code></dd></div>
          <div><dt>日历指纹</dt><dd><code>${escapeHtml(item.source?.calendar_fingerprint || "未绑定")}</code></dd></div>
          <div><dt>上个版本</dt><dd><code>${escapeHtml(item.supersedes || "首版，无上级修订")}</code></dd></div>
        </dl>
      </details>
    </li>`;
}

function researchArchives(data) {
  if (!data?.available) {
    const error = data?.errors?.[0] || {};
    const recovery = error.recovery_action && state.actions.includes(error.recovery_action)
      ? actionButton(error.recovery_action, "secondary")
      : '<button class="button secondary" type="button" data-retry>重新读取</button>';
    return `
      <section class="archive-panel" aria-labelledby="research-archive-heading">
        <header class="archive-heading">
          <div><h2 id="research-archive-heading">收盘归档</h2><p>日报、权益账本与研究日志的只读交叉校验</p></div>
          ${researchArchiveStatusChip("unavailable")}
        </header>
        <div class="empty-state archive-empty" role="alert">
          <h3>无法建立历史快照</h3>
          <p>${escapeHtml(error.message || "模拟账户尚未初始化，或其账本暂时无法安全读取。")}</p>
          ${recovery}
        </div>
      </section>`;
  }
  const daily = Array.isArray(data.daily) ? data.daily : [];
  const weekly = Array.isArray(data.weekly) ? data.weekly : [];
  const snapshots = Array.isArray(data.snapshots) ? data.snapshots : [];
  const latestSnapshot = snapshots[0];
  const errors = Array.isArray(data.errors) ? data.errors : [];
  return `
    <section class="archive-panel" aria-labelledby="research-archive-heading">
      <header class="archive-heading">
        <div>
          <h2 id="research-archive-heading">收盘归档</h2>
          <p>逐日持仓快照和 ISO 周汇总只读取已落盘证据，不刷新行情或改变账户</p>
        </div>
        <div class="archive-heading-status">
          ${researchArchiveStatusChip(data.status)}
          <span>${formatInteger(data.summary?.source_equity_sessions || 0)} 个账本交易日</span>
        </div>
      </header>
      ${errors.length ? `
        <aside class="archive-warning" role="alert">
          <strong>部分来源需要复核</strong>
          <ul>${errors.slice(0, 5).map((error) => `<li>${escapeHtml(error.message || error.code)}</li>`).join("")}</ul>
        </aside>` : ""}
      <div class="archive-layout">
        <section class="archive-section" aria-labelledby="archive-daily-heading">
          <div class="archive-section-title"><h3 id="archive-daily-heading">逐日摘要</h3><span>最近 ${formatInteger(daily.length)} 个记录日</span></div>
          <div class="table-wrap" tabindex="0" role="region" aria-label="逐日收盘归档表，可横向滚动">
            <table class="dense-table archive-table">
              <thead><tr><th>记录日</th><th>校验</th><th class="numeric">权益</th><th class="numeric">日收益</th><th class="numeric">回撤</th><th class="numeric">持仓</th><th class="numeric">日志</th></tr></thead>
              <tbody>${daily.length ? daily.map(archiveDailyRow).join("") : emptyRow(7, "尚无模拟日报或研究日志")}</tbody>
            </table>
          </div>
        </section>
        <section class="archive-section" aria-labelledby="archive-weekly-heading">
          <div class="archive-section-title"><h3 id="archive-weekly-heading">周度复盘</h3><span>按周一至周日归组</span></div>
          <div class="table-wrap" tabindex="0" role="region" aria-label="周度研究归档表，可横向滚动">
            <table class="dense-table archive-table">
              <thead><tr><th>周起始</th><th>覆盖</th><th class="numeric">交易日</th><th class="numeric">周收益</th><th class="numeric">最低回撤</th><th class="numeric">成交 / 拒单</th><th class="numeric">日志</th></tr></thead>
              <tbody>${weekly.length ? weekly.map(archiveWeeklyRow).join("") : emptyRow(7, "尚无可聚合的周度证据")}</tbody>
            </table>
          </div>
        </section>
      </div>
      ${latestSnapshot ? archivePositionSnapshot(latestSnapshot) : ""}
      <p class="archive-boundary"><strong>权限边界：</strong>归档是账本和日志的只读投影；缺失值不会补零，归档结论不会生成订单、改变策略或授予实盘权限。</p>
    </section>`;
}

function archiveDailyRow(item) {
  const positions = Array.isArray(item.positions) ? item.positions.length : 0;
  const journalCount = Number(item.journal?.entry_count || 0);
  return `<tr>
    <td class="mono">${escapeHtml(item.as_of_date || "—")}</td>
    <td>${researchArchiveStatusChip(item.status)}${item.status_detail ? `<span class="table-subtext">${escapeHtml(item.status_detail)}</span>` : ""}</td>
    <td class="numeric">${formatMoney(item.equity)}</td>
    <td class="numeric ${tone(item.daily_return)}">${formatPercent(item.daily_return, true)}</td>
    <td class="numeric ${tone(item.drawdown)}">${formatPercent(item.drawdown)}</td>
    <td class="numeric">${formatInteger(positions)}</td>
    <td class="numeric">${formatInteger(journalCount)}</td>
  </tr>`;
}

function archiveWeeklyRow(item) {
  const expected = item.expected_sessions === null || item.expected_sessions === undefined
    ? `${formatInteger(item.included_sessions)} / 待确认`
    : `${formatInteger(item.included_sessions)} / ${formatInteger(item.expected_sessions)}`;
  const coverageIssues = [
    item.missing_sessions?.length ? `缺 ${formatInteger(item.missing_sessions.length)} 个已知交易日` : "",
    item.unexpected_sessions?.length ? `多 ${formatInteger(item.unexpected_sessions.length)} 个非交易日记录` : "",
  ].filter(Boolean).join("；");
  return `<tr>
    <td class="mono"><strong>${escapeHtml(item.week_start || "—")}</strong><span class="table-subtext">至 ${escapeHtml(item.week_end || "—")}</span></td>
    <td>${researchArchiveStatusChip(item.status)}${coverageIssues ? `<span class="table-subtext">${coverageIssues}</span>` : ""}</td>
    <td class="numeric">${escapeHtml(expected)}</td>
    <td class="numeric ${tone(item.period_return)}">${formatPercent(item.period_return, true)}</td>
    <td class="numeric ${tone(item.max_drawdown)}">${formatPercent(item.max_drawdown)}</td>
    <td class="numeric">${formatInteger(item.trades_count)} / ${formatInteger(item.rejections_count)}</td>
    <td class="numeric">${formatInteger(item.journal_count)}</td>
  </tr>`;
}

function archivePositionSnapshot(snapshot) {
  const positions = Array.isArray(snapshot.positions) ? snapshot.positions : [];
  return `
    <details class="archive-snapshot">
      <summary>最近持仓快照 · ${escapeHtml(snapshot.as_of_date || "日期待确认")} · ${formatInteger(positions.length)} 项</summary>
      <div class="archive-snapshot-meta">
        <span>账本权益 <strong>${formatMoney(snapshot.equity)}</strong></span>
        <span>现金 <strong>${formatMoney(snapshot.cash)}</strong></span>
        <span>估值口径 <strong>账本数量；无历史价格推算</strong></span>
      </div>
      <div class="table-wrap" tabindex="0" role="region" aria-label="最近持仓数量快照，可横向滚动">
        <table class="dense-table archive-table">
          <thead><tr><th>证券</th><th class="numeric">数量</th><th>口径</th></tr></thead>
          <tbody>${positions.length ? positions.map((position) => `<tr><td><strong>${escapeHtml(position.symbol)}</strong><span class="table-subtext">${escapeHtml(instrumentName(position.symbol))}</span></td><td class="numeric">${formatInteger(position.quantity)}</td><td>收盘后账本数量；价格与市值不回填</td></tr>`).join("") : emptyRow(3, "该记录日没有持仓")}</tbody>
        </table>
      </div>
    </details>`;
}

function renderResearch(data) {
  const metrics = data.backtest?.metrics || {};
  const benchmark = data.backtest?.benchmark || {};
  const walk = data.walk_forward?.aggregate || {};
  const validation = data.validation || {};
  const bootstrapData = validation.bootstrap || {};
  return `
    <div class="page-stack">
      ${pageIntro("研究证据", "回测、滚动样本外、区块自助法与压力测试使用同一份可审计报告", [actionButton("backtest", "secondary"), actionButton("walk-forward", "secondary"), actionButton("validate", "primary")].join(""))}

      ${researchFreshness(data.reports)}

      ${researchArchives(data.archives)}

      ${researchDigests(data.digests)}

      ${researchJournal(data.journal)}

      <section class="metric-strip" aria-label="研究指标">
        ${metric("策略年化收益", formatPercent(metrics.cagr), `基准 ${formatPercent(benchmark.cagr)}`, tone(metrics.cagr))}
        ${metric("策略 Sharpe", formatNumber(metrics.sharpe), `基准 ${formatNumber(benchmark.sharpe)}`, tone(metrics.sharpe))}
        ${metric("策略最大回撤", formatPercent(metrics.max_drawdown), `基准 ${formatPercent(benchmark.max_drawdown)}`, tone(metrics.max_drawdown))}
        ${metric("样本外年化收益", formatPercent(walk.oos_cagr), `${walk.positive_segments ?? 0} / ${walk.segments ?? 0} 段为正`, tone(walk.oos_cagr))}
      </section>

      <section class="panel">
        ${panelHeader("回测权益对比", `${data.backtest?.metadata?.start || "—"} 至 ${data.backtest?.metadata?.end || "—"}`)}
        ${chartMarkup(
          "research-equity",
          data.backtest?.equity_curve || [],
          [
            { key: "strategy_equity", label: "策略", color: "--chart-primary" },
            { key: "benchmark_equity", label: "基准", color: "--chart-secondary" },
          ],
          `策略年化收益 ${formatPercent(metrics.cagr)}、Sharpe ${formatNumber(metrics.sharpe)}、最大回撤 ${formatPercent(metrics.max_drawdown)}。`
        )}
      </section>

      <section class="equal-layout">
        <article class="panel">
          ${panelHeader("研究门禁", validation.research_gates?.status || "尚无验证报告")}
          ${checksList(validation.research_gates?.checks || {}, "research")}
        </article>
        <article class="panel">
          ${panelHeader("区块自助法", `${bootstrapData.samples ?? 0} 次抽样 · ${bootstrapData.block_days ?? 0} 日区块`)}
          <div class="check-list">
            ${detailRow("Sharpe 95% 区间", interval(bootstrapData.sharpe_ci_95, formatNumber))}
            ${detailRow("年化收益 95% 区间", interval(bootstrapData.cagr_ci_95, formatPercent))}
            ${detailRow("较差 5% 路径最大回撤", formatPercent(bootstrapData.max_drawdown_5pct_worst), "danger")}
            ${detailRow("年化收益为正概率", formatPercent(bootstrapData.probability_cagr_positive), "info")}
          </div>
        </article>
      </section>

      <section class="panel">
          ${panelHeader("滚动样本外分段", translateDisclosure(data.walk_forward?.selection_disclosure))}
        ${walkForwardTable(data.walk_forward?.segments || [])}
      </section>

      <section class="equal-layout">
        <article class="panel">
          ${panelHeader("交易成本压力", "成本倍数同时作用于佣金、滑点与最低佣金")}
          ${costStressTable(validation.cost_stress || [])}
        </article>
        <article class="panel">
          ${panelHeader("参数邻域", `${validation.parameter_sensitivity?.variants ?? 0} 个相邻参数组合`)}
          <div class="check-list">
            ${detailRow("正年化收益组合比例", formatPercent(validation.parameter_sensitivity?.positive_cagr_ratio), "success")}
            ${detailRow("最低年化收益", formatPercent(validation.parameter_sensitivity?.min_cagr), toneKind(validation.parameter_sensitivity?.min_cagr))}
            ${detailRow("年化收益中位数", formatPercent(validation.parameter_sensitivity?.median_cagr), "info")}
            ${detailRow("Sharpe 中位数", formatNumber(validation.parameter_sensitivity?.median_sharpe), "info")}
          </div>
        </article>
      </section>

      ${strategyConfiguration(data.configuration)}

      <aside class="callout info"><strong>证据边界</strong><p>${escapeHtml(translateDisclosure(validation.selection_disclosure) || "历史结果用于研究判断，不构成收益保证，也不会自动授予真实交易权限。")}</p></aside>
    </div>`;
}

function researchFreshness(reports) {
  const entries = Object.entries(reports || {});
  const outdated = entries.filter(([, report]) => report.state !== "current");
  if (!outdated.length) return "";
  const labels = {
    backtest: "历史回测",
    walk_forward: "滚动样本外",
    validation: "稳健性验证",
  };
  const states = {
    stale: "快照已变化",
    missing: "尚未生成",
    invalid: "报告损坏",
    unverifiable: "无法验证",
  };
  return `<aside class="callout warning">
    <strong>研究报告需要更新</strong>
    <ul>${outdated.map(([name, report]) => `<li>${escapeHtml(labels[name] || name)}：${escapeHtml(states[report.state] || report.state)}</li>`).join("")}</ul>
    <div class="action-row">${[...new Set(outdated.map(([, report]) => report.recovery_action))].map((action) => actionButton(action, "secondary")).join("")}</div>
  </aside>`;
}

function strategyConfiguration(configuration) {
  const strategy = configuration?.strategy;
  if (!strategy) return "";
  const weighting = {
    inverse_volatility: "逆波动率",
    risk_parity: "风险平价",
  }[strategy.weighting_method] || strategy.weighting_method;
  const riskModel = {
    conservative_sum: "保守波动上界",
    covariance: "协方差模型",
  }[strategy.risk_model] || strategy.risk_model;
  return `
    <section class="equal-layout">
      <article class="panel">
        ${panelHeader("当前信号参数", "配置变化会使现有模拟账户指纹失效")}
        <div class="check-list">
          ${detailRow("调仓间隔", `${formatInteger(strategy.rebalance_days)} 个交易日`)}
          ${detailRow("动量回看 / 跳过最近", `${formatInteger(strategy.lookback_days)} / ${formatInteger(strategy.skip_days)} 日`)}
          ${detailRow("趋势均线", `${formatInteger(strategy.trend_sma_days)} 日`)}
          ${detailRow("最多选择", `${formatInteger(strategy.top_n)} 支证券`)}
          ${detailRow("权重方法", weighting, "info")}
          ${detailRow("风险模型", riskModel, "info")}
        </div>
      </article>
      <article class="panel">
        ${panelHeader("当前组合约束", "约束在信号权重进入成交前统一应用")}
        <div class="check-list">
          ${detailRow("目标年化波动", formatPercent(strategy.target_annual_volatility), "warning")}
          ${detailRow("单一证券上限", formatPercent(strategy.max_position_weight), "warning")}
          ${detailRow("最低现金权重", formatPercent(strategy.minimum_cash_weight), "warning")}
          ${detailRow("资产类别上限", formatPercent(strategy.max_asset_class_weight), "warning")}
          ${detailRow("风险分组上限", formatPercent(strategy.max_sector_weight), "warning")}
          ${detailRow("最小调仓偏差", formatPercent(strategy.minimum_rebalance_weight), "warning")}
        </div>
      </article>
    </section>`;
}

function interval(values, formatter) {
  return Array.isArray(values) && values.length === 2
    ? `${formatter(values[0])} 至 ${formatter(values[1])}`
    : "—";
}

function toneKind(value) {
  const parsed = finite(value);
  if (parsed === null) return "neutral";
  return parsed >= 0 ? "success" : "danger";
}

function detailRow(label, value, kind = "neutral") {
  return `<div class="check-row"><span>${escapeHtml(label)}</span>${statusChip(value, kind)}</div>`;
}

function checksList(checks, namespace = "") {
  const entries = Object.entries(checks || {});
  return entries.length ? `<ul class="check-list">${entries.map(([key, passed], index) => `
    <li class="check-row">
      <span>${escapeHtml(checkLabel(key, index, namespace))}</span>
      ${booleanChip(Boolean(passed))}
    </li>`).join("")}</ul>` : `<div class="empty-state compact-empty" role="status"><h3>尚无门禁结果</h3><p>完成对应验证或审计后，这里会显示逐项通过状态。</p></div>`;
}

function checkLabel(key, index, namespace) {
  if (CHECK_LABELS[key]) return CHECK_LABELS[key];
  if (namespace === "research") {
    return [
      "Bootstrap Sharpe 下界大于 0",
      "三倍成本下年化收益大于 0",
      "至少 75% 参数邻域年化收益为正",
      "至少一半压力区间取得正超额",
    ][index] || key;
  }
  return key.replaceAll("_", " ");
}

function walkForwardTable(segments) {
  return `<div class="table-wrap"><table class="data-table">
    <thead><tr><th>测试区间</th><th>选中参数</th><th class="numeric">样本外年化</th><th class="numeric">Sharpe</th><th class="numeric">最大回撤</th><th class="numeric">换手</th></tr></thead>
    <tbody>${segments.length ? segments.map((row) => `<tr>
      <td>${escapeHtml(row.test_start)} 至 ${escapeHtml(row.test_end)}</td>
      <td class="mono">L${escapeHtml(row.selected?.lookback_days)} / S${escapeHtml(row.selected?.trend_sma_days)} / N${escapeHtml(row.selected?.top_n)}</td>
      <td class="numeric ${tone(row.test_metrics?.cagr)}">${formatPercent(row.test_metrics?.cagr)}</td>
      <td class="numeric ${tone(row.test_metrics?.sharpe)}">${formatNumber(row.test_metrics?.sharpe)}</td>
      <td class="numeric ${tone(row.test_metrics?.max_drawdown)}">${formatPercent(row.test_metrics?.max_drawdown)}</td>
      <td class="numeric">${formatNumber(row.test_metrics?.turnover)}</td>
    </tr>`).join("") : emptyRow(6, "尚无滚动样本外报告")}</tbody>
  </table></div>`;
}

function costStressTable(rows) {
  return `<div class="table-wrap"><table class="data-table compact">
    <thead><tr><th class="numeric">成本倍数</th><th class="numeric">年化收益</th><th class="numeric">Sharpe</th><th class="numeric">最大回撤</th></tr></thead>
    <tbody>${rows.length ? rows.map((row) => `<tr>
      <td class="numeric">${formatNumber(row.multiplier, 0)}x</td>
      <td class="numeric ${tone(row.cagr)}">${formatPercent(row.cagr)}</td>
      <td class="numeric ${tone(row.sharpe)}">${formatNumber(row.sharpe)}</td>
      <td class="numeric ${tone(row.max_drawdown)}">${formatPercent(row.max_drawdown)}</td>
    </tr>`).join("") : emptyRow(4, "尚无成本压力报告")}</tbody>
  </table></div>`;
}

const ASSISTANT_CONCLUSIONS = {
  NO_ACTION: "证据不足",
  WATCH: "继续观察",
  REVIEW_CANDIDATE: "研究候选",
  REDUCE_RISK: "降低风险",
};

function renderAssistant(data) {
  const status = data.status || {};
  const history = Array.isArray(data.history) ? data.history : [];
  const result = state.assistantResult || history[0] || null;
  const defaults = data.defaults || {};
  const instruments = Array.isArray(data.instruments) ? data.instruments : [];
  const selectedSymbol = result?.symbol || defaults.symbol || instruments[0]?.symbol || "";
  const selectedLookback = result?.lookback || defaults.lookback || 180;
  const selectedMode = result?.mode || defaults.mode || "local";
  const modelConfigured = Boolean(
    status.model_configured ?? status.configured ?? status.model?.configured
  );
  const modelName = status.model_name || status.model?.name || status.model || "未配置";

  return `
    <div class="page-stack">
      ${pageIntro("收盘 K 线分析", "基于同一份已校验行情快照完成市场诊断与风险复核")}

      <section class="assistant-control-band" aria-label="分析条件">
        <form id="assistant-analysis-form" class="assistant-form">
          <label class="field">
            <span>分析标的</span>
            <select name="symbol" required>
              ${instruments.map((item) => `<option value="${escapeHtml(item.symbol)}"${item.symbol === selectedSymbol ? " selected" : ""}>${escapeHtml(item.symbol)} · ${escapeHtml(item.name || instrumentName(item.symbol))}</option>`).join("")}
            </select>
          </label>
          <label class="field">
            <span>回看交易日</span>
            <input name="lookback" type="number" min="60" max="500" step="10" value="${escapeHtml(selectedLookback)}" required>
          </label>
          <fieldset class="mode-fieldset assistant-mode">
            <legend>分析模式</legend>
            <div class="mode-segmented">
              <label><input type="radio" name="mode" value="local"${selectedMode !== "model" ? " checked" : ""}><span>本地规则</span></label>
              <label><input type="radio" name="mode" value="model"${selectedMode === "model" ? " checked" : ""}${modelConfigured ? "" : " disabled"}><span>模型增强</span></label>
            </div>
          </fieldset>
          <button id="assistant-analyze-button" class="button primary" type="submit"${state.assistantBusy || !instruments.length ? " disabled" : ""}>${state.assistantBusy ? "分析中" : "开始分析"}</button>
        </form>
        <div class="assistant-control-meta">
          ${statusChip(modelConfigured ? `模型已就绪 · ${modelName}` : "本地模式可用", modelConfigured ? "success" : "neutral")}
          <span>只读研究权限 · 不生成订单</span>
        </div>
      </section>

      ${status.configuration_error ? `<aside class="callout warning"><strong>模型配置未生效</strong><p>当前用户的模型环境变量不完整或端点不符合安全规则。本地模式仍可使用；重新运行配置脚本并重启工作台后再检查。</p></aside>` : ""}

      ${result ? assistantResultMarkup(result) : `
        <section class="empty-state">
          <h2>尚无分析记录</h2>
          <p>选择标的与回看期后建立第一份收盘诊断。</p>
        </section>`}

      ${assistantHistoryMarkup(history, result?.analysis_id)}

      <aside class="callout warning">
        <strong>研究边界</strong>
        <p>AI 结论不会提交或生成真实订单，也不会改变模拟盘、券商适配器或实盘授权门禁。历史表现与模型文字都不保证未来收益。</p>
      </aside>
    </div>`;
}

function assistantResultMarkup(result) {
  const diagnosis = result.diagnosis || {};
  const assessment = result.assessment || {};
  const features = result.features || {};
  const validation = result.validation || {};
  const conclusion = assistantConclusionLabel(assessment.conclusion);
  const evidence = Array.isArray(diagnosis.evidence)
    ? diagnosis.evidence
    : Array.isArray(result.evidence) ? result.evidence : [];
  const chart = Array.isArray(result.chart) ? { points: result.chart } : (result.chart || {});
  const points = (Array.isArray(chart.points) ? chart.points : []).map((point) => ({
    ...point,
    ema20: point.ema20 ?? point.ema_20,
    ema50: point.ema50 ?? point.ema_50,
  }));
  const modeLabel = result.mode === "model"
    ? (validation.model_enhanced ? "模型增强" : "模型未完成 · 本地回退")
    : "本地规则";
  const trend = assistantTerm(diagnosis.trend || features.trend);
  const volatility = finite(
    features.annualized_volatility_20d ?? features.annual_volatility ?? features.volatility_annualized
  );
  const score = finite(diagnosis.score);

  const warningMarkup = Array.isArray(validation.warnings) && validation.warnings.length
    ? `<aside class="callout warning" role="status"><strong>模型增强未完成</strong><p>${escapeHtml(validation.warnings.map(assistantWarning).join("；"))}</p></aside>`
    : "";

  return `
    ${warningMarkup}
    <section class="metric-strip" aria-label="分析摘要">
      ${metric("数据日期", result.data_date || "—", `${result.symbol || "—"} · ${result.name || instrumentName(result.symbol)}`)}
      ${metric("市场趋势", trend, score === null ? "阶段一诊断" : `诊断分数 ${formatNumber(score, 0)}`, assistantTone(diagnosis.trend))}
      ${metric("年化波动", volatility === null ? "—" : formatPercent(volatility), `ATR ${formatPercent(features.atr14_pct ?? features.atr_pct ?? features.atr_percent)}`, volatility !== null && volatility > 0.3 ? "tone-warning" : "")}
      ${metric("研究结论", conclusion, `${modeLabel} · ${validation.valid === false ? "校验失败" : "结构已校验"}`, assistantConclusionTone(assessment.conclusion))}
    </section>

    <section class="assistant-provenance" aria-label="分析来源">
      <span>行情源 <strong>${escapeHtml(result.snapshot?.provider || "—")}</strong></span>
      <span>复权 <strong>${escapeHtml(result.snapshot?.adjustment || "—")}</strong></span>
      <span>窗口指纹 <code>${escapeHtml(String(result.snapshot?.window_sha256 || result.snapshot?.snapshot_id || "—").slice(0, 16))}</code></span>
      <span>生成时间 <strong>${formatDate(result.created_at, true)}</strong></span>
    </section>

    <section class="panel assistant-perspectives-panel" aria-label="研究视角">
      ${panelHeader("研究视角", assistantPerspectiveSummary(result.perspectives))}
      ${assistantPerspectivesMarkup(result.perspectives)}
    </section>

    <section class="panel">
      ${panelHeader("价格结构", `${result.lookback || points.length} 个交易日 · ${escapeHtml(modeLabel)}`)}
      ${chartMarkup(
        "assistant-price",
        points,
        [
          { key: "close", label: "收盘", color: "--chart-primary" },
          { key: "ema20", label: "EMA20", color: "--chart-secondary" },
          { key: "ema50", label: "EMA50", color: "--chart-paper" },
        ],
        `${result.symbol || "标的"} 收盘价与 EMA20、EMA50；数据截止 ${result.data_date || "未知"}。`
      )}
    </section>

    <section class="equal-layout assistant-stages">
      <article class="panel">
        ${panelHeader("阶段一 · 市场诊断", `${assistantTerm(diagnosis.regime)} · ${assistantTerm(diagnosis.volatility)}`)}
        <p class="assistant-summary">${escapeHtml(diagnosis.summary || "本地特征已完成计算，等待结构化诊断摘要。")}</p>
        <div class="check-list">
          ${detailRow("趋势", trend, assistantKind(diagnosis.trend))}
          ${detailRow("市场状态", assistantTerm(diagnosis.regime), "info")}
          ${detailRow("波动状态", assistantTerm(diagnosis.volatility), "neutral")}
          ${detailRow("诊断闸门", assistantTerm(diagnosis.gate), String(diagnosis.gate || "").toLowerCase() === "proceed" ? "success" : "warning")}
        </div>
      </article>
      <article class="panel">
        ${panelHeader("阶段二 · 风险复核", `${conclusion} · ${result.model || "local"}`)}
        <p class="assistant-summary">${escapeHtml(assessment.summary || "当前结论仅用于研究复核。")}</p>
        <div class="check-list">
          ${detailRow("结论", conclusion, assistantConclusionKind(assessment.conclusion))}
          ${detailRow("风险等级", assistantTerm(assessment.risk_level), assistantKind(assessment.risk_level))}
          ${detailRow("研究风险预算", assistantBudget(assessment.risk_budget_pct), "warning")}
          ${detailRow("引用证据", `${Array.isArray(assessment.evidence_ids) ? assessment.evidence_ids.length : 0} 项`, "info")}
          ${result.mode === "model" ? detailRow("模型 Token", formatInteger(validation.usage?.total_tokens || 0), "neutral") : ""}
        </div>
      </article>
    </section>

    <section class="equal-layout">
      <article class="panel">
        ${panelHeader("证据账本", `${evidence.length} 项可追溯证据`)}
        ${assistantEvidenceMarkup(evidence)}
      </article>
      <article class="panel">
        ${panelHeader("失效条件", "任一条件出现时重新分析")}
        ${assistantStringList(assessment.invalidation || assessment.invalidation_conditions, "当前没有额外失效条件")}
      </article>
    </section>

    <section class="equal-layout">
      <article class="panel">
        ${panelHeader("决策路径", `${Array.isArray(result.decision_path) ? result.decision_path.length : 0} 个检查节点`)}
        ${assistantDecisionPath(result.decision_path)}
      </article>
      <article class="panel">
        ${panelHeader("情景复核", "条件触发，不是模型胜率")}
        ${assistantScenarios(assessment.scenarios)}
      </article>
    </section>

    ${assistantComparison(result.comparison)}
  `;
}

function assistantEvidenceMarkup(items) {
  if (!items.length) return `<div class="empty-state compact-empty"><p>当前记录没有可展示的证据项。</p></div>`;
  return `<div class="evidence-ledger">${items.map((item, index) => {
    const evidenceId = item.evidence_id || item.id || `E${index + 1}`;
    const label = item.label || item.name || "分析证据";
    const value = assistantEvidenceValue(evidenceId, item.display_value ?? item.value);
    return `<div class="evidence-row">
      <code>${escapeHtml(evidenceId)}</code>
      <div><strong>${escapeHtml(label)}</strong><span>${escapeHtml(item.interpretation || item.description || "")}</span></div>
      <span class="mono">${escapeHtml(value)}</span>
    </div>`;
  }).join("")}</div>`;
}

function assistantPerspectiveSummary(items) {
  const values = Array.isArray(items) ? items : [];
  if (!values.length) return "旧记录未保存视角结构";
  const available = values.filter((item) => item?.status === "AVAILABLE").length;
  return `${available}/${values.length} 个视角有可用数据 · 缺失数据不会被模型补写`;
}

function assistantPerspectivesMarkup(items) {
  const values = Array.isArray(items) ? items : [];
  if (!values.length) {
    return `<div class="empty-state compact-empty" role="status"><strong>视角数据不可用</strong><p>这是旧版分析记录；重新运行一次分析后会生成可追溯的研究视角。</p></div>`;
  }
  return `<div class="perspective-ledger">${values.map((item) => {
    const available = item?.status === "AVAILABLE";
    const stance = assistantPerspectiveStance(item?.stance);
    const kind = assistantPerspectiveKind(item?.stance, available);
    const evidenceIds = Array.isArray(item?.evidence_ids) ? item.evidence_ids.join(" · ") : "—";
    return `<article class="perspective-row${available ? "" : " perspective-unavailable"}">
      <div class="perspective-row-head">
        <strong>${escapeHtml(item?.label || "研究视角")}</strong>
        ${statusChip(available ? "数据已覆盖" : "暂不可评估", available ? "success" : "warning")}
        ${statusChip(stance, kind)}
      </div>
      <p>${escapeHtml(item?.summary || "当前没有视角摘要。")}</p>
      <span class="perspective-limit">边界：${escapeHtml(item?.limitation || "未提供")}</span>
      <code>证据：${escapeHtml(evidenceIds)}</code>
    </article>`;
  }).join("")}</div>`;
}

function assistantPerspectiveStance(value) {
  return {
    SUPPORTIVE: "偏支持",
    CAUTION: "谨慎",
    ADVERSE: "偏不利",
    MIXED: "信号混合",
    REVIEW: "需要复核",
    NOT_AVAILABLE: "不可评估",
  }[String(value || "").toUpperCase()] || "未知";
}

function assistantPerspectiveKind(value, available) {
  if (!available) return "warning";
  return {
    SUPPORTIVE: "success",
    ADVERSE: "danger",
    CAUTION: "warning",
    REVIEW: "info",
    MIXED: "neutral",
  }[String(value || "").toUpperCase()] || "neutral";
}

function assistantDecisionPath(items) {
  if (!Array.isArray(items) || !items.length) return `<p class="section-note">暂无决策路径。</p>`;
  return `<ol class="decision-path">${items.map((item) => {
    if (typeof item === "string") return `<li><span>${escapeHtml(item)}</span></li>`;
    const evidenceIds = Array.isArray(item.evidence_ids) ? item.evidence_ids.join(" · ") : "";
    return `<li><div><strong>${escapeHtml(item.label || item.step || "检查")}</strong><span>${escapeHtml(assistantPathOutcome(item.outcome || item.result))}</span></div>${evidenceIds ? `<code>${escapeHtml(evidenceIds)}</code>` : ""}</li>`;
  }).join("")}</ol>`;
}

function assistantEvidenceValue(evidenceId, value) {
  if (value === null || value === undefined) return "—";
  if (["momentum.return20", "risk.volatility20", "risk.atr14_pct"].includes(evidenceId)) {
    return formatPercent(value, evidenceId === "momentum.return20");
  }
  if (evidenceId === "momentum.rsi14") return formatNumber(value, 1);
  if (evidenceId === "structure.last_candle") {
    return { BULLISH: "阳线", BEARISH: "阴线", DOJI: "十字", FLAT: "平盘" }[value] || assistantTerm(value);
  }
  if (evidenceId.startsWith("price.") || evidenceId.startsWith("trend.") || evidenceId.startsWith("structure.support") || evidenceId.startsWith("structure.resistance")) {
    return typeof value === "number" ? formatNumber(value, 3) : assistantTerm(value);
  }
  return String(value);
}

function assistantPathOutcome(value) {
  if (Object.prototype.hasOwnProperty.call(ASSISTANT_CONCLUSIONS, value)) {
    return assistantConclusionLabel(value);
  }
  return { PASS: "通过", STOP: "终止", REVIEW: "需要复核" }[value] || value || "—";
}

function assistantScenarios(items) {
  if (!Array.isArray(items) || !items.length) return `<p class="section-note">暂无条件情景。</p>`;
  return `<div class="scenario-list">${items.map((item) => `<div class="scenario-row">
    <strong>${escapeHtml(item.name || item.label || "情景")}</strong>
    <span>${escapeHtml(item.trigger || "条件未定义")}</span>
    <p>${escapeHtml(item.implication || item.outcome || "重新评估")}</p>
  </div>`).join("")}</div>`;
}

function assistantStringList(items, emptyMessage) {
  const values = Array.isArray(items) ? items : [];
  if (!values.length) return `<p class="section-note">${escapeHtml(emptyMessage)}</p>`;
  return `<ul class="assistant-list">${values.map((item) => `<li>${escapeHtml(typeof item === "string" ? item : item.condition || item.label || JSON.stringify(item))}</li>`).join("")}</ul>`;
}

function assistantComparison(comparison) {
  if (!comparison || comparison.available === false) return `<aside class="callout info"><strong>首次分析</strong><p>后续同标的分析会显示与上一份已保存记录的变化。</p></aside>`;
  const changes = Array.isArray(comparison.changes) ? comparison.changes : [];
  let fallback = "与上一份记录相比，主要状态未变化。";
  if (comparison.conclusion_changed) {
    fallback = `研究结论由 ${assistantConclusionLabel(comparison.previous_conclusion)} 变为 ${assistantConclusionLabel(comparison.current_conclusion)}。`;
  } else if (comparison.data_advanced) {
    fallback = "行情已推进到新的完整交易日，研究结论暂未变化。";
  }
  const summary = comparison.summary || (changes.length ? changes.join("；") : fallback);
  return `<aside class="callout info"><strong>与上一份记录对比</strong><p>${escapeHtml(summary)}</p></aside>`;
}

function assistantHistoryMarkup(history, activeId) {
  return `
    <section class="panel">
      ${panelHeader("分析历史", "按当前登录账户隔离保存在本机")}
      <div class="table-wrap">
        <table class="data-table compact">
          <thead><tr><th>生成时间</th><th>标的</th><th>数据日期</th><th>模式</th><th>结论</th><th>查看</th></tr></thead>
          <tbody>${history.length ? history.map((item) => `<tr${item.analysis_id === activeId ? ` class="active-history-row"` : ""}>
            <td>${formatDate(item.created_at, true)}</td>
            <td class="symbol-cell"><strong>${escapeHtml(item.symbol || "—")}</strong><span>${escapeHtml(item.name || instrumentName(item.symbol))}</span></td>
            <td class="mono">${escapeHtml(item.data_date || "—")}</td>
            <td>${escapeHtml(item.mode === "model" ? (item.validation?.model_enhanced ? "模型增强" : "模型回退") : "本地规则")}</td>
            <td>${statusChip(assistantConclusionLabel(item.assessment?.conclusion || item.conclusion), assistantConclusionKind(item.assessment?.conclusion || item.conclusion))}</td>
            <td><button class="button secondary history-button" type="button" data-assistant-history="${escapeHtml(item.analysis_id)}" aria-label="查看${escapeHtml(item.symbol || "该标的")}的分析记录">查看</button></td>
          </tr>`).join("") : emptyRow(6, "尚无分析记录")}</tbody>
        </table>
      </div>
    </section>`;
}

function assistantConclusionLabel(value) {
  return ASSISTANT_CONCLUSIONS[value] || value || "证据不足";
}

function assistantConclusionKind(value) {
  return { REVIEW_CANDIDATE: "warning", WATCH: "info", REDUCE_RISK: "danger", NO_ACTION: "neutral" }[value] || "neutral";
}

function assistantConclusionTone(value) {
  return { REVIEW_CANDIDATE: "tone-warning", REDUCE_RISK: "tone-negative", WATCH: "tone-info" }[value] || "";
}

function assistantTerm(value) {
  const terms = {
    bullish: "偏强", bearish: "偏弱", neutral: "中性", up: "上行", down: "下行",
    sideways: "震荡", trend: "趋势", range: "区间", expansion: "波动扩张",
    contraction: "波动收缩", high: "高", medium: "中", low: "低",
    mixed: "方向混合", normal: "常态", unknown: "未知", insufficient: "数据不足",
    proceed: "进入风险复核", watch: "继续观察", stop: "证据不足", insufficient_data: "数据不足",
  };
  const normalized = String(value || "").toLowerCase();
  return terms[normalized] || value || "—";
}

function assistantKind(value) {
  const normalized = String(value || "").toLowerCase();
  if (["bullish", "up", "low"].includes(normalized)) return "success";
  if (["bearish", "down", "high", "reduce_risk"].includes(normalized)) return "danger";
  if (["medium", "sideways", "contraction", "expansion"].includes(normalized)) return "warning";
  return "neutral";
}

function assistantTone(value) {
  const normalized = String(value || "").toLowerCase();
  if (["bullish", "up"].includes(normalized)) return "tone-positive";
  if (["bearish", "down"].includes(normalized)) return "tone-negative";
  return "";
}

function assistantBudget(value) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  return formatPercent(parsed > 1 ? parsed / 100 : parsed);
}

function assistantWarning(value) {
  const text = String(value || "");
  if (text.includes("model_rate_limited")) return "模型服务触发限流，本次已明确回退到本地规则。";
  if (text.includes("model_response_too_large")) return "模型响应超过安全上限，本次已明确回退到本地规则。";
  if (text.includes("invalid_model_response")) return "模型返回内容未通过结构校验，本次已明确回退到本地规则。";
  if (text.includes("unsafe_model_redirect")) return "模型端点发生不安全跳转，本次请求已阻断并回退到本地规则。";
  return "模型服务当前不可用，本次已明确回退到本地规则。";
}

async function runAssistantAnalysis(form) {
  if (state.assistantBusy) return;
  const values = new FormData(form);
  state.assistantBusy = true;
  const button = form.querySelector("button[type='submit']");
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "分析中";
  }
  try {
    const result = await api("/api/assistant/analyze", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify({
        symbol: String(values.get("symbol") || ""),
        lookback: Number(values.get("lookback")),
        mode: String(values.get("mode") || "local"),
      }),
    });
    state.assistantResult = result;
    const payload = state.data.get("assistant") || {};
    payload.history = [result, ...(payload.history || []).filter((item) => item.analysis_id !== result.analysis_id)];
    state.data.set("assistant", payload);
    if (state.route === "assistant") renderRoute(payload);
    notify("分析已完成并保存到本机历史");
  } catch (error) {
    notify(error.message || "分析失败", true);
  } finally {
    state.assistantBusy = false;
    if (state.route === "assistant") {
      const current = state.data.get("assistant");
      if (current) renderRoute(current);
    }
  }
}

const STRATEGY_PARAMETER_LABELS = {
  rebalance_days: "调仓间隔",
  lookback_days: "动量回看",
  skip_days: "跳过最近",
  trend_sma_days: "趋势均线",
  volatility_days: "波动窗口",
  top_n: "最多选择",
  minimum_momentum: "最低动量",
  target_annual_volatility: "目标年化波动",
  minimum_cash_weight: "最低现金权重",
  max_position_weight: "单一证券上限",
  covariance_days: "协方差窗口",
  covariance_shrinkage: "协方差收缩",
  minimum_average_amount: "最低日均成交额",
  minimum_rebalance_weight: "最小调仓偏差",
  weighting_method: "权重方法",
  risk_model: "风险模型",
  max_asset_class_weight: "资产类别上限",
  max_sector_weight: "风险分组上限",
  capacity_reference_cash: "容量参考资金",
  max_average_amount_participation: "成交额参与上限",
  capacity_days: "容量执行窗口",
  max_portfolio_drawdown: "组合回撤止损",
  max_daily_loss: "单日亏损止损",
  cooldown_days: "风险冷却期",
};

const STRATEGY_CHOICE_LABELS = {
  inverse_volatility: "逆波动率",
  risk_parity: "风险平价",
  conservative_sum: "保守波动上界",
  covariance: "协方差模型",
};

function renderStrategyLab(data) {
  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  const active = data.active || null;
  const activeCandidate = candidates.find((item) => item.candidate_id === active?.candidate_id);
  if (!state.strategyCandidateId || !candidates.some((item) => item.candidate_id === state.strategyCandidateId)) {
    state.strategyCandidateId = activeCandidate?.candidate_id || candidates[0]?.candidate_id || "";
  }
  const selected = strategyLabSelectedCandidate(data);
  const hasActiveCandidate = Boolean(active?.candidate_id);
  const safety = data.safety || {};
  const rollbackAvailable = Boolean(active?.can_rollback);
  return `
    <div class="page-stack strategy-lab-page">
      ${pageIntro(
        "策略实验室",
        "人工调参与本地 AI 建议都只生成候选；通过确定性验证并由你批准后，才能导出独立模拟配置",
        hasActiveCandidate && rollbackAvailable
          ? `<button class="button secondary" type="button" data-strategy-rollback${state.strategyActionBusy ? " disabled" : ""}>回滚模拟版本</button>`
          : "",
      )}

      <section class="strategy-authority-band" aria-label="策略权限边界">
        <div><span>当前基线</span><strong class="mono">${escapeHtml(shortFingerprint(data.baseline?.fingerprint))}</strong></div>
        <div><span>选中候选</span><strong>${selected ? escapeHtml(strategyCandidateTitle(selected)) : "尚无候选"}</strong><span class="strategy-authority-status">${selected ? strategyStatusChip(selected.status) : statusChip("未创建", "neutral")}</span></div>
        <div><span>活动模拟版本</span><strong>${hasActiveCandidate ? escapeHtml(activeCandidate ? strategyCandidateTitle(activeCandidate) : shortCandidateId(active.candidate_id)) : "尚未激活"}</strong><span class="strategy-authority-status">${hasActiveCandidate ? statusChip("模拟活动", "info") : statusChip("使用基线", "neutral")}</span></div>
        <div><span>执行权限</span><strong>${safety.live_trading_enabled === false ? "仅限研究与独立模拟" : "权限状态待确认"}</strong><span class="strategy-authority-status">${statusChip("真实下单锁定", "danger")}</span></div>
      </section>

      ${strategyLifecyclePanel(data)}

      ${strategyLabComposer(data)}

      <section class="strategy-workspace">
        <aside class="strategy-candidate-rail" aria-label="策略候选历史">
          ${panelHeader("候选版本", strategyCandidateCountLabel(data, candidates))}
          ${strategyCandidateList(candidates, active)}
        </aside>
        <div class="strategy-candidate-detail">
          ${selected ? strategyCandidateDetail(selected, data) : strategyLabEmptyCandidate()}
        </div>
      </section>

      ${strategyLabHistory(data.history || [])}
      <aside class="callout info"><strong>权限边界</strong><p>策略实验室不会修改默认配置、当前模拟账本、券商授权或紧急停止开关。导出的版本拥有独立账本路径，历史验证也不能授予真实交易权限。</p></aside>
    </div>`;
}

function strategyLifecyclePanel(data) {
  const active = data.active || {};
  const monitoring = data.monitoring || {};
  const latest = monitoring.latest || null;
  if (!active.candidate_id) {
    return `<section class="strategy-lifecycle-band" aria-labelledby="strategy-lifecycle-title">
      <div class="strategy-lifecycle-head"><div><h2 id="strategy-lifecycle-title">上线后观察</h2><p>激活一个已批准的模拟版本后，才能建立近期表现与衰减证据。</p></div>${statusChip("尚未激活", "neutral")}</div>
    </section>`;
  }
  const evidence = latest?.evidence || null;
  const latestMonitorId = latest?.monitor_id || "";
  const failed = Array.isArray(evidence?.failed_checks) ? evidence.failed_checks.length : 0;
  const period = latest?.period || {};
  const lifecycleState = active.lifecycle_state || "ACTIVE";
  const operationForms = [
    active.can_suspend
      ? strategyLifecycleForm("suspend", active, latestMonitorId, "暂停模拟版本", "确认暂停当前实验室活动版本；不会提交订单或修改券商配置。", "监控证据需要人工复核，暂时停止继续观察", "danger")
      : "",
    active.can_resume
      ? strategyLifecycleForm("resume", active, latestMonitorId, "恢复模拟观察", "确认恢复当前版本的实验室观察状态；不会自动晋级任何权限。", "已完成人工复核，恢复模拟观察", "secondary")
      : "",
    active.can_retire
      ? strategyLifecycleForm("retire", active, latestMonitorId, "退役并恢复上一基线", "确认将当前候选永久标记为已退役，并恢复上一实验室基线。", "人工决定退役当前模拟版本", "danger")
      : "",
  ].filter(Boolean).join("");
  return `<section class="strategy-lifecycle-band" aria-labelledby="strategy-lifecycle-title">
    <div class="strategy-lifecycle-head">
      <div><h2 id="strategy-lifecycle-title">上线后观察</h2><p>监控只生成不可变证据；暂停、恢复和退役始终由当前用户确认。</p></div>
      <div class="action-row">${strategyLifecycleStateChip(lifecycleState)}<button class="button secondary" type="button" data-strategy-monitor${state.strategyActionBusy ? " disabled" : ""}>运行衰减检查</button></div>
    </div>
    <dl class="strategy-lifecycle-metrics">
      <div><dt>活动版本</dt><dd><code>${escapeHtml(shortCandidateId(active.candidate_id))}</code></dd></div>
      <div><dt>最近证据</dt><dd>${latest ? formatDate(latest.created_at, true) : "尚未运行"}</dd></div>
      <div><dt>观察窗口</dt><dd>${latest ? `${escapeHtml(period.start || "—")} 至 ${escapeHtml(period.end || "—")} · ${formatInteger(period.sessions)} 日` : `至少 ${formatInteger(monitoring.policy?.minimum_sessions)} 日`}</dd></div>
      <div><dt>衰减结论</dt><dd>${evidence ? strategyMonitorVerdictChip(evidence.verdict) : statusChip("缺少证据", "warning")}${evidence ? `<span>${failed ? `${failed} 项需复核` : "未触发复核阈值"}</span>` : ""}</dd></div>
    </dl>
    ${latest ? strategyMonitorEvidence(latest) : `<p class="strategy-lifecycle-empty" role="status">尚无监控记录。运行检查会在当前行情快照上比较活动版本、父基线和激活时留出集，不会改变策略状态。</p>`}
    <details class="strategy-lifecycle-actions">
      <summary>人工生命周期操作</summary>
      <div class="strategy-lifecycle-action-list">${operationForms}</div>
    </details>
  </section>`;
}

function strategyMonitorEvidence(monitor) {
  const checks = monitor?.evidence?.checks || [];
  return `<div class="table-wrap strategy-monitor-table"><table class="data-table compact">
    <thead><tr><th>衰减检查</th><th>证据</th><th>结论</th></tr></thead>
    <tbody>${checks.length ? checks.map((check) => `<tr><td>${escapeHtml(check.label || check.id)}</td><td>${escapeHtml(check.detail || "—")}</td><td>${booleanChip(Boolean(check.passed))}</td></tr>`).join("") : emptyRow(3, "当前记录没有可显示的检查项")}</tbody>
  </table></div>`;
}

function strategyLifecycleForm(action, active, monitorId, title, confirmation, note, kind) {
  return `<form class="strategy-lifecycle-form strategy-confirmation-form" data-strategy-lifecycle-form data-lifecycle-action="${escapeHtml(action)}">
    <input type="hidden" name="candidate_id" value="${escapeHtml(active.candidate_id)}">
    <input type="hidden" name="fingerprint" value="${escapeHtml(active.fingerprint)}">
    <input type="hidden" name="monitor_id" value="${escapeHtml(monitorId)}">
    <div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(confirmation)}</span></div>
    <label class="confirmation-check"><input type="checkbox" name="confirmed" required><span>我已核对活动版本、指纹和最近监控证据</span></label>
    <label class="field"><span>人工决定依据</span><input name="note" maxlength="500" required value="${escapeHtml(note)}"></label>
    <button class="button ${escapeHtml(kind)}" type="submit" disabled>${escapeHtml(title)}</button>
  </form>`;
}

function strategyLifecycleStateChip(value) {
  const labels = { ACTIVE: "观察中", SUSPENDED: "已暂停", CONFIGURED: "配置基线" };
  const kinds = { ACTIVE: "info", SUSPENDED: "warning", CONFIGURED: "neutral" };
  return statusChip(labels[value] || value || "未知状态", kinds[value] || "neutral");
}

function strategyMonitorVerdictChip(value) {
  const labels = { MONITORING_OK: "观察正常", REVIEW_REQUIRED: "需人工复核", INSUFFICIENT_DATA: "样本不足" };
  const kinds = { MONITORING_OK: "success", REVIEW_REQUIRED: "danger", INSUFFICIENT_DATA: "warning" };
  return statusChip(labels[value] || value || "未知结论", kinds[value] || "neutral");
}

function strategyCandidateCountLabel(data, candidates) {
  const summary = data?.candidate_summary || {};
  const count = Number.isInteger(summary.count) ? summary.count : candidates.length;
  const total = Number.isInteger(summary.total) ? summary.total : candidates.length;
  return summary.truncated
    ? `最近 ${count} / 共 ${total} 个不可变记录`
    : `共 ${total} 个不可变记录`;
}

function strategyLabComposer(data) {
  const manual = state.strategyLabMode === "manual";
  return `
    <section class="strategy-composer">
      <header class="strategy-composer-head">
        <div>
          <h2>创建策略候选</h2>
          <p>保存后参数不可原地修改；继续调整时创建下一个候选。</p>
        </div>
        <div class="segmented" role="tablist" aria-label="候选来源">
          <button id="strategy-tab-manual" type="button" role="tab" data-strategy-mode="manual" aria-controls="strategy-manual-form" aria-selected="${manual}" tabindex="${manual ? 0 : -1}">手动调参</button>
          <button id="strategy-tab-ai" type="button" role="tab" data-strategy-mode="ai" aria-controls="strategy-proposal-form" aria-selected="${!manual}" tabindex="${manual ? -1 : 0}">本地 AI 建议</button>
        </div>
      </header>
      ${strategyManualForm(data, !manual)}
      ${strategyProposalForm(manual)}
    </section>`;
}

function strategyManualForm(data, hidden = false) {
  const schema = data.parameter_schema?.parameters || [];
  const strategy = schema.filter((item) => item.scope === "strategy");
  const risk = schema.filter((item) => item.scope === "risk");
  return `
    <form id="strategy-manual-form" class="strategy-form" role="tabpanel" aria-labelledby="strategy-tab-manual"${hidden ? " hidden" : ""}>
      <div class="strategy-hypothesis-grid">
        <label class="field"><span>候选名称</span><input name="title" maxlength="80" required value="手动策略候选"></label>
        <label class="field strategy-hypothesis"><span>研究假设</span><textarea name="hypothesis" maxlength="1000" required>调整后的参数可能改善风险收益特征，需要使用同一市场快照验证。</textarea></label>
        <label class="field strategy-hypothesis"><span>调整理由</span><textarea name="reason" maxlength="1000" required>由用户在策略实验室手动调整。</textarea></label>
      </div>
      <fieldset class="strategy-parameter-group">
        <legend>信号与组合参数</legend>
        <div class="strategy-parameter-grid">${strategy.map((item) => strategyParameterField(item, data.baseline)).join("")}</div>
      </fieldset>
      <fieldset class="strategy-parameter-group risk-parameters">
        <legend>账户级风险参数</legend>
        <div class="strategy-parameter-grid">${risk.map((item) => strategyParameterField(item, data.baseline)).join("")}</div>
      </fieldset>
      <footer class="form-footer">
        <span>只有与当前基线不同的字段会写入候选差异。</span>
        <button class="button primary" type="submit"${state.strategyActionBusy ? " disabled" : ""}>保存手动候选</button>
      </footer>
    </form>`;
}

function strategyProposalForm(hidden = false) {
  return `
    <form id="strategy-proposal-form" class="strategy-form strategy-proposal-form" role="tabpanel" aria-labelledby="strategy-tab-ai"${hidden ? " hidden" : ""}>
      <div class="strategy-hypothesis-grid">
        <label class="field"><span>候选名称</span><input name="title" maxlength="80" required value="本地 AI 策略候选"></label>
        <label class="field strategy-hypothesis"><span>研究假设</span><textarea name="hypothesis" maxlength="1000" required>在不扩大交易权限的前提下，寻找更稳定的参数邻域。</textarea></label>
        <fieldset class="mode-fieldset strategy-objective">
          <legend>优化侧重</legend>
          <div class="mode-segmented three-options">
            <label><input type="radio" name="objective" value="balanced" checked><span>平衡</span></label>
            <label><input type="radio" name="objective" value="drawdown"><span>回撤</span></label>
            <label><input type="radio" name="objective" value="turnover"><span>换手</span></label>
          </div>
        </fieldset>
      </div>
      <aside class="callout info"><strong>本地确定性建议</strong><p>该模式不需要 API Key，只能在参数白名单和安全范围内提出差异；它不能生成代码、订单、目标仓位、批准记录或发布决定。</p></aside>
      <footer class="form-footer">
        <span>建议仅创建草稿，仍需同快照验证与人工批准。</span>
        <button class="button primary" type="submit"${state.strategyActionBusy ? " disabled" : ""}>生成候选</button>
      </footer>
    </form>`;
}

function strategyParameterField(spec, baseline) {
  const raw = baseline?.[spec.scope]?.[spec.name];
  const ratio = spec.unit === "ratio";
  const scale = ratio ? 0.01 : 1;
  const shown = finite(raw) === null ? raw : strategyInputNumber(Number(raw) / scale);
  const label = STRATEGY_PARAMETER_LABELS[spec.name] || spec.label || spec.name;
  const unit = strategyUnitLabel(spec.unit);
  const common = `data-strategy-parameter data-scope="${escapeHtml(spec.scope)}" data-parameter="${escapeHtml(spec.name)}" data-value-type="${escapeHtml(spec.type)}" data-original="${escapeHtml(raw)}" data-scale="${scale}"`;
  let control;
  if (spec.type === "choice") {
    control = `<select name="${escapeHtml(spec.scope)}.${escapeHtml(spec.name)}" ${common}>${(spec.options || []).map((option) => `<option value="${escapeHtml(option)}"${option === raw ? " selected" : ""}>${escapeHtml(STRATEGY_CHOICE_LABELS[option] || option)}</option>`).join("")}</select>`;
  } else {
    const minimum = finite(spec.min) === null ? "" : ` min="${strategyInputNumber(Number(spec.min) / scale)}"`;
    const maximum = finite(spec.max) === null ? "" : ` max="${strategyInputNumber(Number(spec.max) / scale)}"`;
    const step = spec.type === "integer" ? 1 : "any";
    control = `<div class="unit-input"><input type="number" name="${escapeHtml(spec.scope)}.${escapeHtml(spec.name)}" value="${escapeHtml(shown)}" step="${escapeHtml(step)}"${minimum}${maximum} ${common} required>${unit ? `<span>${escapeHtml(unit)}</span>` : ""}</div>`;
  }
  return `<label class="field strategy-parameter"><span>${escapeHtml(label)}</span>${control}</label>`;
}

function strategyUnitLabel(unit) {
  return { ratio: "%", sessions: "日", instruments: "支", CNY: "元" }[unit] || "";
}

function strategyInputNumber(value) {
  return Number(value.toFixed(8)).toString();
}

function strategyCandidateList(candidates, active) {
  if (!candidates.length) {
    return `<div class="compact-empty"><strong>尚无候选</strong><p>在上方手动调整参数，或让本地规则提出第一个候选。</p></div>`;
  }
  return `<div class="strategy-candidate-list">${candidates.map((candidate) => {
    const selected = candidate.candidate_id === state.strategyCandidateId;
    return `<button class="strategy-candidate-item" type="button" data-strategy-candidate="${escapeHtml(candidate.candidate_id)}" aria-pressed="${selected}">
      <span class="strategy-candidate-title"><strong>${escapeHtml(strategyCandidateTitle(candidate))}</strong>${candidate.candidate_id === active?.candidate_id ? strategyLifecycleStateChip(active.lifecycle_state) : candidate.lifecycle?.state === "RETIRED" ? statusChip("已退役", "neutral") : ""}</span>
      <span>${escapeHtml(strategySourceLabel(candidate.source))} · ${formatDate(candidate.created_at, true)}</span>
      <span class="strategy-candidate-foot"><code>${escapeHtml(shortCandidateId(candidate.candidate_id))}</code>${strategyStatusChip(candidate.status)}</span>
    </button>`;
  }).join("")}</div>`;
}

function strategyCandidateDetail(candidate, data) {
  const validation = candidate.validation || null;
  const approved = candidate.status === "APPROVED";
  const eligible = candidate.status === "ELIGIBLE";
  const draft = candidate.status === "DRAFT";
  const active = data.active?.candidate_id === candidate.candidate_id;
  return `
    <article class="panel strategy-candidate-record">
      ${panelHeader(
        strategyCandidateTitle(candidate),
        `${strategySourceLabel(candidate.source)} · ${candidate.candidate_id}`,
        `<div class="action-row">${active ? strategyLifecycleStateChip(data.active?.lifecycle_state) : candidate.lifecycle?.state === "RETIRED" ? statusChip("已退役", "neutral") : ""}${strategyStatusChip(candidate.status)}</div>`,
      )}
      <div class="strategy-record-provenance">
        <span>建立时间 <strong>${formatDate(candidate.created_at, true)}</strong></span>
        <span>父版本 <code>${escapeHtml(shortFingerprint(candidate.parent_fingerprint))}</code></span>
        <span>候选指纹 <code>${escapeHtml(shortFingerprint(candidate.candidate_fingerprint))}</code></span>
      </div>
      <p class="strategy-hypothesis-copy"><strong>研究假设</strong>${escapeHtml(candidate.hypothesis || "未记录")}</p>
      ${candidate.reason ? `<p class="strategy-reason-copy"><strong>来源说明</strong>${escapeHtml(candidate.reason)}</p>` : ""}
      ${strategyCandidateDiff(candidate, data)}
      ${validation ? strategyValidationComparison(validation) : `<aside class="callout warning"><strong>尚未验证</strong><p>候选仍是草稿。验证会在当前不可变行情快照上同时运行基线与候选，并检查留出区间、交易成本、回撤和参数稳定性。</p></aside>`}
      ${strategyCandidateActions(candidate, { draft, eligible, approved, active })}
      ${candidate.export ? `<div class="path-row strategy-export-path"><span>模拟配置</span><code>${escapeHtml(candidate.export.path || "已导出")}</code></div>` : ""}
    </article>`;
}

function strategyCandidateDiff(candidate, data) {
  const changes = candidate.effective_changes || candidate.changes || {};
  const rows = [];
  for (const [scope, values] of Object.entries(changes)) {
    if (!values || typeof values !== "object") continue;
    for (const [name, candidateValue] of Object.entries(values)) {
      const spec = (data.parameter_schema?.parameters || []).find((item) => item.scope === scope && item.name === name) || { scope, name };
      const baselineValue = candidate.baseline?.[scope]?.[name] ?? data.baseline?.[scope]?.[name];
      rows.push(`<tr><td>${escapeHtml(STRATEGY_PARAMETER_LABELS[name] || spec.label || name)}</td><td class="numeric">${escapeHtml(strategyParameterValue(baselineValue, spec))}</td><td class="numeric">${escapeHtml(strategyParameterValue(candidateValue, spec))}</td></tr>`);
    }
  }
  return `<section class="strategy-diff-block">
    ${panelHeader("参数差异", `${rows.length} 项白名单变更`)}
    <div class="table-wrap"><table class="data-table compact strategy-diff-table"><thead><tr><th>参数</th><th>基线</th><th>候选</th></tr></thead><tbody>${rows.length ? rows.join("") : emptyRow(3, "未识别到有效参数差异")}</tbody></table></div>
  </section>`;
}

function strategyParameterValue(value, spec) {
  if (spec.type === "choice") return STRATEGY_CHOICE_LABELS[value] || value || "—";
  if (spec.unit === "ratio") return formatPercent(value);
  if (spec.unit === "CNY") return formatMoney(value);
  if (spec.unit === "sessions") return `${formatInteger(value)} 日`;
  if (spec.unit === "instruments") return `${formatInteger(value)} 支`;
  return Number.isInteger(value) ? formatInteger(value) : formatNumber(value, 3);
}

function strategyValidationComparison(validation) {
  const baseline = validation.baseline_metrics || {};
  const candidate = validation.candidate_metrics || {};
  const gates = validation.gates || {};
  const snapshot = validation.market_snapshot || {};
  return `
    <section class="strategy-validation-block">
      ${panelHeader("同快照验证", `${snapshot.date || "—"} · ${shortFingerprint(snapshot.id)}`)}
      <div class="table-wrap"><table class="data-table compact strategy-metric-table">
        <thead><tr><th>指标</th><th>当前基线</th><th>候选版本</th><th>变化</th></tr></thead>
        <tbody>
          ${strategyMetricRow("年化收益", baseline.cagr, candidate.cagr, formatPercent)}
          ${strategyMetricRow("Sharpe", baseline.sharpe, candidate.sharpe, formatNumber)}
          ${strategyMetricRow("最大回撤", baseline.max_drawdown, candidate.max_drawdown, formatPercent)}
          ${strategyMetricRow("年化波动", baseline.annual_volatility, candidate.annual_volatility, formatPercent, true)}
          ${strategyMetricRow("换手倍数", baseline.turnover, candidate.turnover, formatMultiple, true)}
        </tbody>
      </table></div>
      <div class="equal-layout strategy-validation-evidence">
        <section>
          ${panelHeader("确定性闸门", `${gates.passed ?? 0} / ${gates.total ?? 0} 通过`)}
          ${strategyGateList(gates.checks || [])}
        </section>
        <section>
          ${panelHeader("验证切片", "留出集、成本与邻域稳定性")}
          ${strategyValidationSlices(validation)}
        </section>
      </div>
    </section>`;
}

function strategyMetricRow(label, baseline, candidate, formatter, inverse = false) {
  const base = finite(baseline);
  const current = finite(candidate);
  const delta = base === null || current === null ? null : current - base;
  const kind = delta === null || Math.abs(delta) < 1e-12 ? "" : tone(delta, inverse);
  const change = delta === null ? "—" : `${delta > 0 ? "+" : ""}${formatter(delta)}`;
  return `<tr><td>${escapeHtml(label)}</td><td class="numeric">${escapeHtml(formatter(baseline))}</td><td class="numeric">${escapeHtml(formatter(candidate))}</td><td class="numeric ${kind}">${escapeHtml(change)}</td></tr>`;
}

function formatMultiple(value) {
  const parsed = finite(value);
  return parsed === null ? "—" : `${formatNumber(parsed)}x`;
}

function strategyGateList(checks) {
  if (!checks.length) return `<div class="compact-empty"><p>没有可显示的验证闸门。</p></div>`;
  return `<div class="strategy-gate-list">${checks.map((check) => `<div class="strategy-gate-row"><div><strong>${escapeHtml(check.label || check.id)}</strong><span>${escapeHtml(check.detail || "")}</span></div>${booleanChip(Boolean(check.passed))}</div>`).join("")}</div>`;
}

function strategyValidationSlices(validation) {
  const holdout = validation.holdout || {};
  const cost = validation.cost_stress || {};
  const stability = validation.stability || {};
  const holdoutPeriod = validation.period?.holdout_start && validation.period?.end
    ? `${validation.period.holdout_start} 至 ${validation.period.end}`
    : "已执行";
  const variants = Array.isArray(stability.variants)
    ? stability.variants.length
    : stability.total_variants ?? stability.variants;
  return `<div class="check-list">
    ${detailRow("留出区间", holdoutPeriod, "info")}
    ${detailRow("留出集 Sharpe", formatNumber(holdout.candidate_metrics?.sharpe ?? holdout.candidate_sharpe), toneKind(holdout.candidate_metrics?.sharpe ?? holdout.candidate_sharpe))}
    ${detailRow("成本压力", cost.multiplier ? `${formatNumber(cost.multiplier, 0)} 倍成本` : "已执行", cost.passed === false ? "danger" : "warning")}
    ${detailRow("压力后年化收益", formatPercent(cost.candidate_metrics?.cagr ?? cost.candidate_cagr), toneKind(cost.candidate_metrics?.cagr ?? cost.candidate_cagr))}
    ${detailRow("参数邻域", `${formatInteger(variants)} 个变体`, "info")}
    ${detailRow("邻域最低 Sharpe", formatNumber(stability.minimum_sharpe), toneKind(stability.minimum_sharpe))}
  </div>`;
}

function strategyCandidateActions(candidate, states) {
  if (states.draft) {
    return `<div class="strategy-action-band"><div><strong>下一步：验证候选</strong><span>验证期间不会修改任何活动策略或账户。</span></div><button class="button primary" type="button" data-strategy-validate="${escapeHtml(candidate.candidate_id)}"${state.strategyActionBusy ? " disabled" : ""}>运行同快照验证</button></div>`;
  }
  if (candidate.status === "REJECTED") {
    return `<aside class="callout danger"><strong>候选未通过闸门</strong><p>该不可变记录保留为反证。调整参数时请创建新候选，不能覆盖本次结果。</p></aside>`;
  }
  if (states.eligible) {
    return `<form id="strategy-approval-form" class="strategy-action-band strategy-confirmation-form">
      <input type="hidden" name="candidate_id" value="${escapeHtml(candidate.candidate_id)}">
      <label class="confirmation-check"><input type="checkbox" name="confirmed" required><span>我已复核参数差异、验证指标和全部闸门</span></label>
      <label class="field"><span>审批备注</span><input name="note" maxlength="500" required value="已完成策略实验室人工复核"></label>
      <button class="button primary" type="submit" disabled>批准候选</button>
    </form>`;
  }
  if (states.approved) {
    return `<div class="strategy-approved-actions">
      <div class="strategy-action-band"><div><strong>候选已由人工批准</strong><span>可导出为凭据隔离、账本隔离的模拟配置。</span></div><button class="button secondary" type="button" data-strategy-export="${escapeHtml(candidate.candidate_id)}"${state.strategyActionBusy ? " disabled" : ""}>导出模拟配置</button></div>
      <form id="strategy-activation-form" class="strategy-action-band strategy-confirmation-form">
        <input type="hidden" name="candidate_id" value="${escapeHtml(candidate.candidate_id)}">
        <label class="confirmation-check"><input type="checkbox" name="confirmed" required${states.active ? " disabled" : ""}><span>仅设为实验室活动模拟版本，不修改默认配置或开启实盘</span></label>
        <label class="field"><span>激活备注</span><input name="note" maxlength="500" required value="进入独立模拟观察"${states.active ? " disabled" : ""}></label>
        <button class="button primary" type="submit" disabled>${states.active ? "已是活动版本" : "设为模拟版本"}</button>
      </form>
    </div>`;
  }
  return "";
}

function strategyLabEmptyCandidate() {
  return `<section class="empty-state strategy-empty-state" role="status"><h2>创建第一个候选</h2><p>手动调参和本地 AI 建议具有相同权限：只能保存候选，不能直接改写活动策略。</p></section>`;
}

function strategyLabHistory(history) {
  if (!history.length) return "";
  const ordered = [...history].reverse().slice(0, 20);
  return `<section class="panel">
    ${panelHeader("版本审计", "批准、激活与回滚事件按时间保留")}
    <div class="table-wrap"><table class="data-table compact"><thead><tr><th>时间</th><th>事件</th><th>候选</th><th>操作者</th></tr></thead><tbody>${ordered.map((event) => `<tr><td>${formatDate(event.created_at, true)}</td><td>${escapeHtml(strategyEventLabel(event.action || event.type))}</td><td><code>${escapeHtml(shortCandidateId(event.candidate_id ?? event.to_candidate_id))}</code></td><td>${escapeHtml(event.actor || event.approved_by || event.activated_by || "本地所有者")}</td></tr>`).join("")}</tbody></table></div>
  </section>`;
}

function strategyLabSelectedCandidate(data) {
  return (data?.candidates || []).find((item) => item.candidate_id === state.strategyCandidateId) || null;
}

function strategyCandidateTitle(candidate) {
  return candidate?.title || "未命名候选";
}

function strategySourceLabel(source) {
  return { manual: "人工创建", local_ai: "本地 AI 建议", ai_local: "本地 AI 建议", rollback: "回滚" }[source] || source || "未知来源";
}

function strategyStatusChip(status) {
  const labels = { DRAFT: "草稿", ELIGIBLE: "验证通过", REJECTED: "验证未通过", APPROVED: "已批准" };
  const kinds = { DRAFT: "neutral", ELIGIBLE: "success", REJECTED: "danger", APPROVED: "info" };
  return statusChip(labels[status] || status || "未知", kinds[status] || "neutral");
}

function strategyEventLabel(value) {
  return { create: "创建候选", validate: "完成验证", approve: "人工批准", export: "导出模拟配置", activate: "激活模拟版本", monitor: "运行衰减检查", suspend: "暂停模拟版本", resume: "恢复模拟观察", retire: "退役模拟版本", rollback: "回滚模拟版本" }[value] || value || "版本事件";
}

function shortFingerprint(value) {
  const text = String(value || "");
  return text ? `${text.slice(0, 12)}${text.length > 12 ? "…" : ""}` : "—";
}

function shortCandidateId(value) {
  const text = String(value || "");
  return text ? `${text.slice(0, 13)}${text.length > 13 ? "…" : ""}` : "—";
}

async function createManualStrategyCandidate(form) {
  const values = new FormData(form);
  const changes = {};
  for (const input of form.querySelectorAll("[data-strategy-parameter]")) {
    const scale = Number(input.dataset.scale || 1);
    const original = input.dataset.valueType === "choice" ? input.dataset.original : Number(input.dataset.original);
    let current = input.dataset.valueType === "choice" ? input.value : Number(input.value) * scale;
    if (input.dataset.valueType === "integer") current = Math.round(current);
    const changed = typeof current === "number" ? Math.abs(current - original) > 1e-12 : current !== original;
    if (!changed) continue;
    const scope = input.dataset.scope;
    changes[scope] ||= {};
    changes[scope][input.dataset.parameter] = current;
  }
  if (!Object.keys(changes).length) {
    notify("请至少调整一个白名单参数", true);
    return;
  }
  await runStrategyLabMutation(
    "/api/strategy-lab/candidates",
    {
      changes,
      title: String(values.get("title") || ""),
      hypothesis: String(values.get("hypothesis") || ""),
      reason: String(values.get("reason") || ""),
    },
    "手动候选已保存",
  );
}

async function createProposedStrategyCandidate(form) {
  const values = new FormData(form);
  await runStrategyLabMutation(
    "/api/strategy-lab/propose",
    {
      title: String(values.get("title") || ""),
      hypothesis: String(values.get("hypothesis") || ""),
      objective: String(values.get("objective") || "balanced"),
    },
    "本地建议已保存为候选",
  );
}

async function runStrategyLabMutation(path, payload, successMessage, button = null) {
  if (state.strategyActionBusy) return;
  state.strategyActionBusy = true;
  let strategyLabReloaded = false;
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.dataset.originalLabel = button.textContent;
    button.textContent = path.endsWith("/validate") ? "正在验证" : "正在处理";
  }
  try {
    const result = await api(path, {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify(payload),
    });
    if (result.candidate_id) state.strategyCandidateId = result.candidate_id;
    await reloadStrategyLab();
    strategyLabReloaded = true;
    notify(successMessage);
  } catch (error) {
    let message = friendlyError(error.message);
    if (error.status === 409) {
      try {
        await reloadStrategyLab();
        strategyLabReloaded = true;
        message = `${message}；已刷新当前策略状态`;
      } catch {
        message = `${message}；请刷新策略实验室后重试`;
      }
    }
    notify(message, true);
  } finally {
    state.strategyActionBusy = false;
    if (strategyLabReloaded && state.route === "strategy-lab") {
      const current = state.data.get("strategy-lab");
      if (current) renderRoute(current);
    }
    if (button?.isConnected) {
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
      button.textContent = button.dataset.originalLabel || "重试";
    }
  }
}

async function reloadStrategyLab() {
  const payload = await api("/api/strategy-lab");
  state.data.set("strategy-lab", payload);
  if (state.route === "strategy-lab") renderRoute(payload);
}

function renderPortfolio(data) {
  if (!data.initialized) {
    return `<div class="page-stack">
      ${pageIntro("模拟组合", "账户状态、持仓与待执行目标共享同一份本地账本")}
      <section class="empty-state" role="status">
        <h2>模拟账户尚未建立</h2>
        <p>建立账户后，系统会从首个完整交易日开始累计独立前向证据；已有账户不会被覆盖。</p>
        <div class="action-row">${actionButton("paper-init", "primary")}${actionButton("refresh-data", "secondary")}</div>
      </section>
    </div>`;
  }
  const valuationStatus = data.valuation_status
    || (data.valuation_available === false ? "unavailable" : "current");
  const valuationUnavailable = valuationStatus === "unavailable";
  const valuationPartial = valuationStatus === "partial";
  const freshness = data.market_freshness || {};
  const valuationErrors = Array.isArray(data.errors)
    ? data.errors.filter((item) => item?.code && item?.symbol)
    : [];
  const valuationErrorSymbols = [...new Set(valuationErrors.map((item) => item.symbol))];
  const freshnessReviewText = freshness.current === false
    ? `共同最新交易日 ${freshness.date || "—"}，完成交易日截止 ${freshness.completed_session_cutoff || "—"}；滞后 ${formatInteger(freshness.lag_calendar_days)} 个自然日。`
    : `共同最新交易日 ${freshness.date || "—"} 与完成交易日截止 ${freshness.completed_session_cutoff || "—"} 一致；来源或清单状态仍需复核。`;
  const valuationNotice = valuationUnavailable
    ? `<aside class="callout warning" role="status">
        <strong>行情估值暂不可用</strong>
        <p>下方数量、现金和最近账本权益仍来自本地账本；价格、市值、权重和目标差额不会在缺少已校验行情时猜测。运行“刷新行情”后再复核组合。</p>
        <div class="action-row">${actionButton("refresh-data", "primary")}</div>
      </aside>`
    : valuationPartial
      ? `<aside class="callout warning" role="status">
          <strong>部分持仓暂未估值</strong>
          <p>${escapeHtml(valuationErrorSymbols.join("、") || "部分证券")} 缺少已校验的完整收盘价；其数量仍保留，价格、市值、权重和相关目标差额显示为不可用。运行“刷新行情”后再复核。</p>
          <div class="action-row">${actionButton("refresh-data", "primary")}</div>
        </aside>`
    : freshness.status && freshness.status !== "OK"
      ? `<aside class="callout warning" role="status">
          <strong>组合行情快照需要复核</strong>
          <p>${escapeHtml(freshnessReviewText)}</p>
        </aside>`
      : "";
  const equityLabel = valuationStatus === "current" ? "账户权益" : "账本权益（最近记录）";
  const equityNote = valuationStatus === "current"
    ? "唯一模拟记账口径"
    : "行情状态复核后重新估值";
  return `
    <div class="page-stack">
      ${pageIntro("模拟组合", `账户 ${String(data.account_id || "").slice(0, 8)} · 状态日期 ${data.date || "—"}`, actionButton("paper-run", "primary"))}
      <div class="portfolio-valuation-meta" aria-label="组合估值状态">
        <span>估值口径</span>
        ${statusChip(valuationStatusLabel(valuationStatus), valuationStatusKind(valuationStatus))}
        <span>共同最新 ${escapeHtml(data.valuation_date || freshness.date || "—")}</span>
        <span>快照截止 ${escapeHtml(freshness.completed_session_cutoff || "—")}</span>
      </div>
      ${valuationNotice}
      <section class="metric-strip metric-strip-priority portfolio-risk-strip" aria-label="组合摘要">
        ${metric(equityLabel, formatMoney(data.equity), equityNote)}
        ${metric("现金缓冲", formatPercent(data.cash_weight), `${formatMoney(data.cash)} 可用`)}
        ${metric("当前回撤", formatPercent(data.drawdown), "相对账户高水位", tone(data.drawdown))}
        ${metric("风险冷却", `${formatInteger(data.cooldown_remaining)} 日`, data.cooldown_remaining ? "暂停新增风险仓位" : "未触发冷却", data.cooldown_remaining ? "tone-warning" : "tone-positive")}
      </section>

      <section class="panel">
        ${panelHeader("模拟权益", "仅包含已经写入模拟账本的交易日")}
        ${chartMarkup(
          "paper-equity",
          data.equity_curve || [],
          [
            { key: "equity", label: "权益", color: "--chart-paper" },
            { key: "cash", label: "现金", color: "--chart-secondary" },
          ],
          `当前权益 ${formatMoney(data.equity)}，当前回撤 ${formatPercent(data.drawdown)}，现金权重 ${formatPercent(data.cash_weight)}。`
        )}
      </section>

      <section class="equal-layout">
        <article class="panel">
          ${panelHeader("当前持仓", `${data.positions?.length || 0} 个持仓`) }
          ${positionsTable(data.positions || [])}
        </article>
        <article class="panel">
          ${panelHeader("待执行目标", data.pending_signal_date ? `信号日 ${data.pending_signal_date}，下一完整交易日开盘处理` : "暂无待执行信号")}
          ${pendingTable(data.pending_targets || [])}
        </article>
      </section>
    </div>`;
}

function positionsTable(rows) {
  return `<div class="table-wrap"><table class="data-table compact">
    <thead><tr><th>证券</th><th class="numeric">数量</th><th class="numeric">价格</th><th class="numeric">市值</th><th class="numeric">权重</th></tr></thead>
    <tbody>${rows.length ? rows.map((row) => `<tr>
      <td class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong><span>${escapeHtml(instrumentName(row.symbol, row.name))}</span>${row.valuation_status === "unavailable" ? '<span class="table-status">估值不可用</span>' : ""}</td>
      <td class="numeric">${formatInteger(row.quantity)}</td>
      <td class="numeric">${valuationCell(row.price, (value) => formatNumber(value, 3), row.valuation_status !== "unavailable")}</td>
      <td class="numeric">${valuationCell(row.market_value, formatMoney, row.valuation_status !== "unavailable")}</td>
      <td class="numeric">${valuationCell(row.weight, formatPercent, row.valuation_status !== "unavailable")}</td>
    </tr>`).join("") : emptyRow(5, "当前账户全部为现金")}</tbody>
  </table></div>`;
}

function pendingTable(rows) {
  return `<div class="table-wrap"><table class="data-table compact">
    <thead><tr><th>证券</th><th class="numeric">当前</th><th class="numeric">目标</th><th class="numeric">差额</th></tr></thead>
    <tbody>${rows.length ? rows.map((row) => `<tr>
      <td class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong><span>${escapeHtml(instrumentName(row.symbol, row.name))}</span>${row.current_weight === null ? '<span class="table-status">当前估值不可用</span>' : ""}</td>
      <td class="numeric">${valuationCell(row.current_weight, formatPercent, row.current_weight !== null)}</td>
      <td class="numeric">${formatPercent(row.target_weight)}</td>
      <td class="numeric ${tone(row.difference)}">${valuationCell(row.difference, (value) => formatPercent(value, true), row.difference !== null)}</td>
    </tr>`).join("") : emptyRow(4, "暂无待执行目标")}</tbody>
  </table></div>`;
}

function tradeSideLabel(value) {
  return { BUY: "买入", SELL: "卖出" }[String(value || "").toUpperCase()] || value || "—";
}

function tradeSideMarkup(value) {
  const normalized = String(value || "").toUpperCase();
  const label = tradeSideLabel(value);
  if (!["BUY", "SELL"].includes(normalized)) return escapeHtml(label);
  return `<span class="trade-side" data-side="${normalized.toLowerCase()}"><span class="trade-side-code" aria-hidden="true">${normalized}</span><span>${escapeHtml(label)}</span></span>`;
}

function tradeRejectionReason(value) {
  const text = String(value || "");
  if (["No valid opening bar", "No opening bar"].includes(text)) return "缺少可执行的开盘价";
  if (text.includes("required sell could not execute")) return "必要卖出未能执行，已阻止后续买入";
  if (text.includes("suspended or has no executable volume")) return "证券停牌或缺少可执行成交量";
  const status = text.match(/^Security status is (.+)$/);
  if (status) return `证券交易状态：${status[1]}`;
  const limit = text.match(/^Opening price is at the ([\d.]+%) (upper|lower) price limit$/);
  if (limit) return `开盘价触及 ${limit[1]} ${limit[2] === "upper" ? "涨停" : "跌停"}限制`;
  return text || "未记录拒绝原因";
}

function renderTrading(data) {
  const audit = data.paper_audit || {};
  const live = data.live || {};
  const reconciliation = live.reconciliation || {};
  const exclusionNote = reconciliationExclusionNote(reconciliation);
  const tabs = `
    <div class="segmented" role="tablist" aria-label="执行记录">
      ${[
        ["paper", "模拟成交"],
        ["rejections", "拒单"],
        ["broker", "券商账本"],
        ["shadow", "影子复盘"],
      ].map(([key, label]) => `<button id="trading-tab-${key}" type="button" role="tab" data-trading-tab="${key}" aria-controls="trading-ledger-panel" aria-selected="${state.tradingTab === key}" tabindex="${state.tradingTab === key ? 0 : -1}">${label}</button>`).join("")}
    </div>`;
  return `
    <div class="page-stack">
      ${pageIntro("交易与晋级", "订单、拒单、成交和权限检查均保留可追溯记录", [actionButton("paper-run", "primary"), actionButton("paper-audit", "secondary")].join(""))}

      ${contextBand([
        {
          label: "当前阶段",
          value: STATUS_LABELS[audit.status] || audit.status || "尚无审计",
          status: audit.eligible_for_broker_sandbox ? "可申请沙箱复核" : "继续收集证据",
          kind: audit.eligible_for_broker_sandbox ? "success" : "warning",
          note: `${audit.sessions || 0} / ${audit.minimum_promotion_sessions || 60} 个独立交易日`,
        },
        {
          label: "模拟账本",
          value: audit.integrity_errors?.length ? "存在完整性错误" : "完整性已校验",
          status: audit.integrity_errors?.length ? "需处理" : "通过",
          kind: audit.integrity_errors?.length ? "danger" : "success",
          note: audit.period?.length ? `${audit.period[0]} 至 ${audit.period[1]}` : "尚无审计区间",
        },
        {
          label: "券商沙箱",
          value: `${reconciliation.clean_sessions || 0} / ${reconciliation.minimum_sessions || 20} 次对账`,
          status: reconciliation.eligible ? "已具备资格" : "未开始",
          kind: reconciliation.eligible ? "success" : "neutral",
          note: exclusionNote || "只在前向模拟通过后开放复核",
        },
        {
          label: "真实交易",
          value: live.live_ready ? "门禁已通过" : "提交路径锁定",
          status: live.live_ready ? "仍需人工授权" : "不可下单",
          kind: live.live_ready ? "warning" : "danger",
          note: "历史与模拟收益都不能单独解锁",
        },
      ], "交易权限状态")}

      <section class="split-layout">
        <article class="panel">
          ${panelHeader("前向模拟进度", audit.status ? STATUS_LABELS[audit.status] || audit.status : "尚无审计")}
          <div class="progress-block">
            <progress max="${audit.minimum_promotion_sessions || 60}" value="${audit.sessions || 0}" aria-label="前向模拟进度：${audit.sessions || 0} / ${audit.minimum_promotion_sessions || 60} 个交易日">${audit.sessions || 0}</progress>
            <span class="section-note">${audit.sessions || 0} / ${audit.minimum_promotion_sessions || 60} 个交易日，尚需 ${audit.remaining_sessions ?? audit.minimum_promotion_sessions ?? 60} 日</span>
          </div>
          ${checksList(audit.promotion_checks || {})}
        </article>
        <article class="panel">
          ${panelHeader("权限路径", "每一级只授予下一阶段复核资格")}
          ${authorityRail(live, audit, {})}
        </article>
      </section>

      <section class="panel">
        ${panelHeader("执行账本", state.tradingTab === "shadow" ? "只读导入与偏差复盘" : "最近 200 条记录", tabs)}
        <div id="trading-ledger-panel" role="tabpanel" aria-labelledby="trading-tab-${escapeHtml(state.tradingTab)}">
          ${tradingLedger(data)}
        </div>
      </section>

      <section class="split-layout">
        <article class="panel">
          ${panelHeader("实盘提交门禁", live.adapter ? `适配器 ${live.adapter}` : "尚未选择券商适配器")}
          ${checksList(live.checks || {})}
        </article>
        <article class="panel">
          ${panelHeader("真实交易控制", "提交路径在所有门禁通过前保持不可用")}
          <div class="callout danger">
            <strong>真实下单已锁定</strong>
            <p id="live-order-lock-reason">当前阶段不会创建、预览或发送真实订单。历史收益和模拟收益都不能单独解除此锁。</p>
          </div>
          <div class="action-row"><button class="button primary" type="button" aria-describedby="live-order-lock-reason" disabled>提交真实订单</button></div>
        </article>
      </section>
    </div>`;
}

function tradingLedger(data) {
  if (state.tradingTab === "shadow") {
    return shadowAccountMarkup(data.shadow_account || {});
  }
  if (state.tradingTab === "rejections") {
    const rows = data.paper_rejections || [];
    return `<div class="table-wrap"><table class="data-table">
      <thead><tr><th>日期</th><th>证券</th><th>方向</th><th>拒绝原因</th></tr></thead>
      <tbody>${rows.length ? rows.map((row) => `<tr><td>${escapeHtml(row.date)}</td><td>${escapeHtml(row.symbol)}</td><td>${tradeSideMarkup(row.side)}</td><td>${escapeHtml(tradeRejectionReason(row.reason))}</td></tr>`).join("") : emptyRow(4, "模拟账本中没有拒单记录")}</tbody>
    </table></div>`;
  }
  if (state.tradingTab === "broker") {
    return brokerLifecycleMarkup(data);
  }
  const rows = data.paper_trades || [];
  return `<div class="table-wrap"><table class="data-table">
    <thead><tr><th>日期</th><th>证券</th><th>方向</th><th class="numeric">数量</th><th class="numeric">价格</th><th class="numeric">名义金额</th><th class="numeric">交易成本</th></tr></thead>
    <tbody>${rows.length ? rows.map((row) => `<tr>
      <td>${escapeHtml(row.date)}</td><td class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong><span>${escapeHtml(instrumentName(row.symbol))}</span></td><td>${tradeSideMarkup(row.side)}</td><td class="numeric">${formatInteger(row.quantity)}</td><td class="numeric">${formatNumber(row.price, 3)}</td><td class="numeric">${formatMoney(row.notional)}</td><td class="numeric">${formatMoney((finite(row.commission) || 0) + (finite(row.stamp_duty) || 0) + (finite(row.transfer_fee) || 0) + (finite(row.slippage_cost) || 0))}</td>
    </tr>`).join("") : emptyRow(7, "尚无模拟成交；目标会在下一完整交易日处理")}</tbody>
  </table></div>`;
}

function brokerLifecycleMarkup(data) {
  const lifecycle = data.broker_lifecycle || {};
  const scope = lifecycle.scope || {};
  const orders = lifecycle.orders || [];
  const fills = data.broker_fills || [];
  const errors = lifecycle.integrity_errors || [];
  const warnings = lifecycle.recovery_warnings || [];
  const lifecycleStatus = lifecycle.status || "EMPTY";
  const statusKind = lifecycleStatus === "VERIFIED"
    ? "success"
    : lifecycleStatus === "INTEGRITY_ERROR"
      ? "danger"
      : lifecycleStatus === "RECOVERED"
        ? "warning"
        : "neutral";
  const latestUpdate = orders[0]?.updated_at ? formatDate(orders[0].updated_at, true) : "尚无更新时间";
  const submissionUnconfirmedCount = Number(lifecycle.submission_unconfirmed_count) || 0;
  const scopeStatus = scope.status || "EMPTY";
  const scopeKind = scopeStatus === "BOUND"
    ? "success"
    : scopeStatus === "MISMATCH" || scopeStatus === "INVALID"
      ? "danger"
      : scopeStatus === "UNSCOPED"
        ? "warning"
        : "neutral";
  const scopeEnvironment = scope.environment === "live" ? "实盘" : scope.environment === "sandbox" ? "沙箱" : "环境未绑定";
  const scopeDetail = scope.adapter
    ? `${scope.adapter} · ${scopeEnvironment} · 账户 ${scope.account_reference || "未绑定"}`
    : scopeStatus === "UNSCOPED"
      ? "缺少账户、环境和配置绑定"
      : "尚无券商账本作用域";

  return `
    <div class="broker-lifecycle-view">
      <dl class="broker-lifecycle-band" aria-label="券商订单账本摘要">
        <div><dt>恢复状态</dt><dd>${statusChip(BROKER_LIFECYCLE_STATUS_LABELS[lifecycleStatus] || lifecycleStatus, statusKind)}<span>依据本地订单与成交账本重建</span></dd></div>
        <div><dt>订单</dt><dd><strong class="numeric">${formatInteger(lifecycle.open_order_count || 0)} / ${formatInteger(lifecycle.order_count || 0)}</strong><span>未终结 / 全部，最近 ${escapeHtml(latestUpdate)}${submissionUnconfirmedCount ? `；${formatInteger(submissionUnconfirmedCount)} 笔提交未确认` : ""}</span></dd></div>
        <div><dt>执行中状态</dt><dd><strong class="numeric">${formatInteger(lifecycle.partial_order_count || 0)} / ${formatInteger(lifecycle.cancel_pending_count || 0)}</strong><span>部分成交 / 撤单处理中</span></dd></div>
        <div><dt>成交明细</dt><dd><strong class="numeric">${formatInteger(lifecycle.fill_count || 0)}</strong><span>按成交号与内容指纹校验</span></dd></div>
        <div><dt>证据作用域</dt><dd>${statusChip(BROKER_LEDGER_SCOPE_STATUS_LABELS[scopeStatus] || scopeStatus, scopeKind)}<span>${escapeHtml(scopeDetail)}</span></dd></div>
      </dl>

      ${errors.length ? `
        <aside class="callout danger broker-lifecycle-alert" role="alert">
          <strong>订单生命周期未通过完整性校验</strong>
          <p>当前推导状态不能作为现金、持仓或权限依据。先核对本地账本，再继续沙箱复核。</p>
          <ul>${errors.map((issue) => `<li>${escapeHtml(brokerLifecycleIssueText(issue))}</li>`).join("")}</ul>
        </aside>` : ""}
      ${submissionUnconfirmedCount ? `
        <aside class="callout danger broker-lifecycle-alert" role="alert">
          <strong>存在 ${formatInteger(submissionUnconfirmedCount)} 笔提交结果未确认</strong>
          <p>本地只证明提交意图已经落盘，不证明券商是否接收。请先在券商端按客户端订单号查询；确认前不要重复提交，也不要删除或改写账本行。</p>
        </aside>` : ""}
      ${warnings.length ? `
        <aside class="callout warning broker-lifecycle-alert">
          <strong>已恢复可用状态，但历史需要人工复核</strong>
          <ul>${warnings.map((issue) => `<li>${escapeHtml(brokerLifecycleIssueText(issue))}</li>`).join("")}</ul>
        </aside>` : ""}
      ${lifecycleStatus === "EMPTY" ? `
        <aside class="callout info broker-lifecycle-alert">
          <strong>尚无券商订单生命周期记录</strong>
          <p>当前 QMT 连接仅执行只读探测且不会写入晋级账本；未来沙箱适配器轮询订单和成交后，这里才会形成可恢复状态。</p>
        </aside>` : ""}

      <section class="broker-ledger-section" aria-labelledby="broker-order-state-title">
        <div class="broker-ledger-heading">
          <div><h3 id="broker-order-state-title">当前订单状态</h3><p>每笔订单只显示按券商时间归并后的最新状态；事件数量和乱序信息保留在恢复说明中。</p></div>
        </div>
        <div class="table-wrap"><table class="data-table broker-order-table">
          <thead><tr><th>更新时间</th><th>客户端 / 券商订单</th><th>证券</th><th>方向</th><th class="numeric">成交 / 委托</th><th class="numeric">剩余</th><th class="numeric">限价 / 均价</th><th>状态</th><th>恢复说明</th></tr></thead>
          <tbody>${orders.length ? orders.map((row) => `<tr>
            <td>${escapeHtml(formatDate(row.updated_at, true))}</td>
            <td class="broker-order-identifiers"><code>${escapeHtml(row.client_order_id)}</code><code>${escapeHtml(row.broker_order_id || "待券商确认")}</code></td>
            <td class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong><span>${escapeHtml(instrumentName(row.symbol))}</span></td>
            <td>${tradeSideMarkup(row.side)}</td>
            <td class="numeric">${formatInteger(row.filled_quantity)} / ${formatInteger(row.quantity)}</td>
            <td class="numeric">${formatInteger(row.remaining_quantity)}</td>
            <td class="numeric">${formatNumber(row.limit_price, 4)} / ${row.average_fill_price === null || row.average_fill_price === undefined ? "—" : formatNumber(row.average_fill_price, 4)}</td>
            <td>${statusChip(STATUS_LABELS[row.status] || row.status || "—", brokerOrderStatusKind(row.status))}</td>
            <td class="broker-recovery-note">${brokerRecoveryNote(row)}</td>
          </tr>`).join("") : emptyRow(9, errors.length ? "账本校验失败，修复前不推导当前订单状态" : "尚无券商订单；真实交易路径未配置")}</tbody>
        </table></div>
      </section>

      <section class="broker-ledger-section" aria-labelledby="broker-fill-ledger-title">
        <div class="broker-ledger-heading">
          <div><h3 id="broker-fill-ledger-title">成交明细账本</h3><p>最近 ${formatInteger(fills.length)} 条原始标准化记录；新记录校验完整 SHA-256，数量和均价还必须能与上方最新订单快照复算一致。</p></div>
        </div>
        <div class="table-wrap"><table class="data-table">
          <thead><tr><th>成交时间</th><th>成交号</th><th>客户端订单</th><th>证券</th><th>方向</th><th class="numeric">数量</th><th class="numeric">价格</th><th class="numeric">费用</th></tr></thead>
          <tbody>${fills.length ? fills.map((row) => `<tr>
            <td>${escapeHtml(formatDate(row.filled_at, true))}</td><td><code>${escapeHtml(row.fill_id)}</code></td><td><code>${escapeHtml(row.client_order_id)}</code></td><td>${escapeHtml(row.symbol)}</td><td>${tradeSideMarkup(row.side)}</td><td class="numeric">${formatInteger(row.quantity)}</td><td class="numeric">${formatNumber(row.price, 4)}</td><td class="numeric">${formatMoney((finite(row.commission) || 0) + (finite(row.tax) || 0))}</td>
          </tr>`).join("") : emptyRow(8, "尚无券商成交明细")}</tbody>
        </table></div>
      </section>

      <aside class="callout info broker-lifecycle-boundary">
        <strong>审计边界</strong>
        <p>生命周期恢复只证明本地订单事件与成交明细能否自洽；作用域绑定只防止不同适配器、账户、环境或配置意外混用。两者都不等同于现金和持仓对账，也不会写入沙箱晋级证据、改变策略或解除真实下单门禁。</p>
      </aside>
    </div>`;
}

function brokerLifecycleIssueText(issue) {
  const label = BROKER_LIFECYCLE_ISSUE_LABELS[issue?.code] || issue?.message || "未分类的账本问题";
  return issue?.client_order_id ? `订单 ${issue.client_order_id}：${label}` : label;
}

function brokerOrderStatusKind(status) {
  if (status === "FILLED") return "success";
  if (status === "REJECTED") return "danger";
  if (["PARTIALLY_FILLED", "CANCEL_PENDING", "EXPIRED"].includes(status)) return "warning";
  if (["PENDING_SUBMIT", "SUBMITTED"].includes(status)) return "info";
  return "neutral";
}

function brokerRecoveryNote(row) {
  const notes = [`${formatInteger(row.event_count || 0)} 个事件`];
  if (row.submission_unconfirmed) notes.unshift("提交结果未确认；禁止盲目重试");
  if (row.out_of_order_events) notes.push(`已归并 ${formatInteger(row.out_of_order_events)} 个延迟事件`);
  if (row.cancel_race_observed) notes.push("撤单期间发生成交");
  if (row.history_complete === false) notes.push("早期历史不完整");
  if (notes.length === 1) notes.push("顺序完整");
  return notes.map((note) => `<span>${escapeHtml(note)}</span>`).join("");
}

function shadowAccountMarkup(shadow) {
  const review = shadow.review || {};
  const errors = shadow.integrity_errors || [];
  const groups = review.groups || [];
  const imports = shadow.imports || [];
  const recentFills = shadow.recent_fills || [];
  const verdict = shadow.status || review.verdict || "INSUFFICIENT_DATA";
  const verdictKind = verdict === "CONSISTENT_WITH_MODEL"
    ? "success"
    : verdict === "INTEGRITY_ERROR"
      ? "danger"
      : verdict === "REVIEW_REQUIRED"
        ? "warning"
        : "neutral";
  const adverseBps = review.weighted_adverse_price_bps === null
    || review.weighted_adverse_price_bps === undefined
    ? null
    : finite(review.weighted_adverse_price_bps);
  const reasons = review.review_reasons || [];
  const maximumMb = Math.max(0.01, (finite(shadow.max_import_bytes) || 1000000) / 1000000);
  return `
    <div class="shadow-account-view">
      <dl class="shadow-metric-band" aria-label="影子账户复盘摘要">
        <div><dt>复盘结论</dt><dd>${statusChip(SHADOW_VERDICT_LABELS[verdict] || verdict, verdictKind)}<span>${escapeHtml(review.account_alias || "尚未导入账户")}</span></dd></div>
        <div><dt>行为覆盖</dt><dd><strong class="numeric">${review.match_rate === null || review.match_rate === undefined ? "—" : formatPercent(review.match_rate)}</strong><span>${formatInteger(review.matched_groups || 0)} / ${formatInteger(review.expected_groups || 0)} 组模拟成交</span></dd></div>
        <div><dt>不利价格偏差</dt><dd><strong class="numeric ${adverseBps !== null && adverseBps > 25 ? "negative" : ""}">${adverseBps === null ? "—" : `${adverseBps > 0 ? "+" : ""}${formatNumber(adverseBps, 2)} bp`}</strong><span>相对本地模拟成交价，正值更差</span></dd></div>
        <div><dt>成交分配偏差</dt><dd><strong class="numeric">${review.trade_allocation_deviation === null || review.trade_allocation_deviation === undefined ? "—" : formatPercent(review.trade_allocation_deviation)}</strong><span>按导入窗口内各证券成交额比较</span></dd></div>
      </dl>

      ${errors.length ? `<div class="callout danger shadow-integrity-alert" role="alert"><strong>影子账本完整性检查失败</strong><ul>${errors.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul><p>停止使用当前复盘结论；账本不会自动修复或覆盖。</p></div>` : ""}

      <section class="shadow-import-section" aria-labelledby="shadow-import-title">
        <div class="shadow-section-heading">
          <div><h3 id="shadow-import-title">导入券商成交 CSV</h3><p>使用账户别名，不填写真实账号；原始文件校验后即释放，仅保留标准化成交和 SHA-256。</p></div>
          <button class="button secondary" type="button" data-shadow-template>下载空白模板</button>
        </div>
        <form id="shadow-import-form" class="shadow-import-form" aria-describedby="shadow-import-help">
          <label class="field"><span>来源标签</span><input name="source_label" value="broker-export" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9._ -]{0,63}" autocomplete="off" spellcheck="false" required></label>
          <label class="field"><span>账户别名</span><input name="account_alias" maxlength="64" placeholder="例如：模拟账户 A" autocomplete="off" required></label>
          <label class="field shadow-file-field"><span>标准成交文件</span><input name="csv_file" type="file" accept=".csv,text/csv" required></label>
          <button class="button primary" type="submit" ${state.shadowImportBusy ? "disabled aria-busy=\"true\"" : ""}>${state.shadowImportBusy ? "正在校验" : "导入并复盘"}</button>
        </form>
        <p id="shadow-import-help" class="shadow-import-help">表头必须严格等于 ${escapeHtml((shadow.canonical_columns || []).join(","))}；单次不超过 ${formatNumber(maximumMb, 2)} MB、${formatInteger(shadow.max_rows_per_import || 5000)} 行，时间必须包含时区。</p>
      </section>

      <aside class="callout info shadow-boundary"><strong>只读证据边界</strong><p>影子账户只比较导入成交与当前本地模拟账本，不读取券商、不提交或撤销订单，也不参与沙箱或实盘权限晋级。</p></aside>

      <section class="shadow-review-section" aria-labelledby="shadow-review-title">
        <div class="shadow-section-heading">
          <div><h3 id="shadow-review-title">行为与执行偏差</h3><p>${review.period?.[0] ? `${escapeHtml(review.period[0])} 至 ${escapeHtml(review.period[1])}` : "导入成交后形成可审计窗口"}</p></div>
          ${statusChip(SHADOW_VERDICT_LABELS[review.verdict] || "证据不足", verdictKind)}
        </div>
        ${reasons.length ? `<ul class="shadow-reason-list">${reasons.map((reason) => `<li>${escapeHtml(SHADOW_REASON_LABELS[reason] || reason)}</li>`).join("")}</ul>` : review.verdict === "CONSISTENT_WITH_MODEL" ? `<p class="shadow-review-note">在当前导入窗口内，方向、数量、价格和成交分配未越过复盘阈值；这不是收益或实盘资格证明。</p>` : `<p class="shadow-review-note">尚无可与当前模拟成交一一比较的重叠记录。</p>`}
        <div class="table-wrap shadow-review-table"><table class="data-table">
          <thead><tr><th>日期</th><th>证券</th><th>方向</th><th class="numeric">实际 / 模拟数量</th><th class="numeric">实际 / 模拟价格</th><th class="numeric">不利偏差</th><th>结果</th></tr></thead>
          <tbody>${groups.length ? groups.map((row) => `<tr>
            <td>${escapeHtml(row.date)}</td><td class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong><span>${escapeHtml(instrumentName(row.symbol))}</span></td><td>${tradeSideMarkup(row.side)}</td><td class="numeric">${formatInteger(row.actual_quantity)} / ${formatInteger(row.expected_quantity)}</td><td class="numeric">${row.actual_price === null ? "—" : formatNumber(row.actual_price, 4)} / ${row.expected_price === null ? "—" : formatNumber(row.expected_price, 4)}</td><td class="numeric">${row.adverse_price_bps === null ? "—" : `${row.adverse_price_bps > 0 ? "+" : ""}${formatNumber(row.adverse_price_bps, 2)} bp`}</td><td>${statusChip(row.outcome === "MATCHED" ? "已匹配" : row.outcome === "UNEXPECTED" ? "未预期成交" : "模拟成交缺失", row.outcome === "MATCHED" ? "success" : "warning")}</td>
          </tr>`).join("") : emptyRow(7, "尚无可比较的影子成交组")}</tbody>
        </table></div>
      </section>

      <section class="shadow-ledger-section" aria-labelledby="shadow-fill-title">
        <div class="shadow-section-heading"><div><h3 id="shadow-fill-title">标准化影子成交</h3><p>${formatInteger(shadow.fill_count || 0)} 条不可变记录，页面显示最近 ${formatInteger(recentFills.length)} 条</p></div></div>
        <div class="table-wrap shadow-fill-table"><table class="data-table">
          <thead><tr><th>成交时间</th><th>账户别名</th><th>来源 / 成交号</th><th>证券</th><th>方向</th><th class="numeric">数量</th><th class="numeric">价格</th><th class="numeric">费用</th></tr></thead>
          <tbody>${recentFills.length ? recentFills.map((row) => `<tr><td>${escapeHtml(formatDate(row.filled_at, true))}</td><td>${escapeHtml(row.account_alias)}</td><td><span>${escapeHtml(row.source_label)}</span><code>${escapeHtml(row.source_fill_id)}</code></td><td>${escapeHtml(row.symbol)}</td><td>${tradeSideMarkup(row.side)}</td><td class="numeric">${formatInteger(row.quantity)}</td><td class="numeric">${formatNumber(row.price, 4)}</td><td class="numeric">${formatMoney((finite(row.commission) || 0) + (finite(row.tax) || 0))}</td></tr>`).join("") : emptyRow(8, "尚未导入标准成交文件")}</tbody>
        </table></div>
      </section>

      <details class="shadow-import-history">
        <summary>导入审计记录 · ${formatInteger(shadow.import_count || 0)} 次</summary>
        <div class="table-wrap"><table class="data-table">
          <thead><tr><th>导入时间</th><th>来源</th><th>账户别名</th><th class="numeric">接收 / 重复</th><th>文件 SHA-256</th></tr></thead>
          <tbody>${imports.length ? imports.map((row) => `<tr><td>${escapeHtml(formatDate(row.imported_at, true))}</td><td>${escapeHtml(row.source_label)}</td><td>${escapeHtml(row.account_alias)}</td><td class="numeric">${formatInteger(row.accepted_count)} / ${formatInteger(row.duplicate_count)}</td><td><code class="shadow-hash">${escapeHtml(row.source_sha256)}</code></td></tr>`).join("") : emptyRow(5, "尚无导入审计记录")}</tbody>
        </table></div>
      </details>
    </div>`;
}

function renderRisk(data) {
  const overview = data.overview || {};
  const research = data.research || {};
  const historical = overview.research?.backtest || {};
  const audit = overview.paper?.audit || {};
  const paperMetrics = audit.metrics || {};
  const bootstrapData = research.validation?.bootstrap || {};
  const riskConfig = research.configuration?.risk || {};
  const reports = Object.values(research.reports || {});
  const staleReports = reports.filter((report) => report.state !== "current").length;
  const liveChecks = overview.live?.checks || {};
  const livePassed = Object.values(liveChecks).filter(Boolean).length;
  const liveTotal = Object.keys(liveChecks).length;
  const marketDecisionDate = overview.market?.freshness?.latest_common_market_date
    || overview.market?.date;
  return `
    <div class="page-stack">
      ${pageIntro("风险控制", "将已观察风险、前向门禁和真实交易权限分层审阅")}
      ${contextBand([
        {
          label: "行情快照",
          value: marketDecisionDate || "不可用",
          status: overview.market?.available === false ? "不可用" : "已加载",
          kind: overview.market?.available === false ? "danger" : "success",
          note: `${overview.market?.universe?.active_count ?? 0} 支当日有效证券 · 完成截止 ${overview.market?.freshness?.completed_session_cutoff || "—"}`,
        },
        {
          label: "前向证据",
          value: `${audit.sessions || 0} / ${audit.minimum_promotion_sessions || 60} 日`,
          status: audit.eligible_for_broker_sandbox ? "门禁通过" : "尚未通过",
          kind: audit.eligible_for_broker_sandbox ? "success" : "warning",
          note: `当前回撤 ${formatPercent(paperMetrics.max_drawdown)}`,
        },
        {
          label: "研究可信度",
          value: staleReports ? `${staleReports} 份报告待更新` : "报告当前",
          status: staleReports ? "不可视为当前证据" : "可复核",
          kind: staleReports ? "warning" : "success",
          note: `历史区间截至 ${research.backtest?.metadata?.end || "—"}`,
        },
        {
          label: "实盘门禁",
          value: `${livePassed} / ${liveTotal} 项通过`,
          status: overview.live?.live_ready ? "待人工授权" : "真实交易锁定",
          kind: overview.live?.live_ready ? "warning" : "danger",
          note: "额度只限制风险，不代表可交易",
        },
      ], "风险证据与权限状态")}
      <section class="metric-strip metric-strip-priority risk-metric-strip" aria-label="风险摘要">
        ${metric("前向最大回撤", formatPercent(paperMetrics.max_drawdown), `${audit.sessions || 0} 个交易日`, tone(paperMetrics.max_drawdown))}
        ${metric("历史最大回撤", formatPercent(historical.max_drawdown), "回测观测值", tone(historical.max_drawdown))}
        ${metric("历史 95% 预期损失", formatPercent(historical.expected_shortfall_95), "单日尾部均值", tone(historical.expected_shortfall_95))}
        ${metric("Bootstrap 尾部回撤", formatPercent(bootstrapData.max_drawdown_5pct_worst), "较差 5% 路径", tone(bootstrapData.max_drawdown_5pct_worst))}
      </section>

      <section class="equal-layout">
        <article class="panel">
          ${panelHeader("研究门禁", research.validation?.research_gates?.status || "暂无")}
          ${checksList(research.validation?.research_gates?.checks || {}, "research")}
        </article>
        <article class="panel">
          ${panelHeader("前向模拟门禁", audit.status ? STATUS_LABELS[audit.status] || audit.status : "暂无")}
          ${checksList(audit.promotion_checks || {})}
        </article>
      </section>

      <section class="split-layout">
        <article class="panel">
          ${panelHeader("真实交易门禁", `${Object.values(overview.live?.checks || {}).filter(Boolean).length} / ${Object.keys(overview.live?.checks || {}).length} 项通过`)}
          ${checksList(overview.live?.checks || {})}
        </article>
        <article class="panel">
          ${panelHeader("紧急停止与额度", "额度只限制风险，不代表可交易")}
          <div class="check-list">
            ${detailRow("单笔名义金额上限", formatMoney(overview.live?.limits?.max_order_notional), "warning")}
            ${detailRow("单日累计名义金额上限", formatMoney(overview.live?.limits?.max_daily_notional), "warning")}
            ${detailRow("组合回撤止损线", formatPercent(riskConfig.max_portfolio_drawdown), "warning")}
            ${detailRow("单日亏损止损线", formatPercent(riskConfig.max_daily_loss), "warning")}
            ${detailRow("触发后冷却期", `${formatInteger(riskConfig.cooldown_days)} 个交易日`, "warning")}
            ${detailRow("紧急停止文件", overview.live?.checks?.kill_switch_clear ? "未触发" : "已触发", overview.live?.checks?.kill_switch_clear ? "success" : "danger")}
            ${detailRow("人工授权", authorizationReasonLabel(overview.live?.authorization?.reason), overview.live?.authorization?.valid ? "success" : "danger")}
          </div>
        </article>
      </section>

      <aside class="callout warning"><strong>风险不是一个分数</strong><p>回撤、尾部损失、参数敏感性、数据偏差、成交可实现性和账户权限分别审计。任何单项历史优势都不能替代真实券商沙箱对账。</p></aside>
    </div>`;
}

function authorizationReasonLabel(value) {
  const text = String(value || "");
  if (!text) return "未配置";
  if (text.includes("missing or invalid")) return "未找到有效的限时人工授权";
  if (text.includes("expired")) return "人工授权已过期";
  return text;
}

function formatPercentPoints(value, signed = false) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  const sign = signed && parsed > 0 ? "+" : "";
  return `${sign}${formatNumber(parsed, 2)}%`;
}

function intelligenceStatusLabel(value) {
  return {
    complete: "完整收盘快照",
    current: "完整收盘快照",
    empty: "合法空集",
    stale: "快照滞后",
    provisional: "晚于完成截止",
    unavailable: "尚无可用快照",
    failed: "刷新失败",
  }[String(value || "").toLowerCase()] || "状态待确认";
}

function intelligenceStatusKind(value) {
  const normalized = String(value || "").toLowerCase();
  if (["complete", "current"].includes(normalized)) return "success";
  if (["empty", "stale", "provisional"].includes(normalized)) return "warning";
  if (["failed", "unavailable"].includes(normalized)) return "danger";
  return "neutral";
}

function intelligenceWarningText(item, coverage) {
  const code = String(item?.code || "");
  if (code === "not_exchange_certified") {
    return "东方财富是第三方公开研究来源，不是交易所认证披露源。";
  }
  if (code === "optional_metric_missing") {
    const missing = coverage?.data_quality?.missing_optional_numeric_values?.turnover_rate;
    return `${formatInteger(missing)} 条记录的换手率缺失；页面保留“—”，没有填充为 0。`;
  }
  if (code === "dragon_tiger_stale") {
    return "本地龙虎榜快照早于当前已完成收盘日。";
  }
  if (code === "after_completed_session_cutoff") {
    return "所选快照晚于当前已完成收盘日，只能作为待复核证据。";
  }
  return String(item?.message || item || "证据边界待复核");
}

function intelligenceUnavailableText(error) {
  if (error?.code === "dragon_tiger_not_refreshed") {
    return "尚无已验证的本地龙虎榜快照。请运行“刷新龙虎榜”，系统不会用示例数据填充。";
  }
  if (error?.code === "dragon_tiger_refresh_failed") {
    return "最近一次刷新未发布新快照。请到系统任务查看错误记录后重试。";
  }
  return String(error?.message || error || "先刷新当前已验证行情，再运行龙虎榜刷新；系统不会用示例数据填充空页面。");
}

function breadthStatusLabel(value) {
  return {
    current: "完整收盘快照",
    stale: "快照滞后",
    provisional: "晚于完成截止",
    unavailable: "尚无可用快照",
  }[String(value || "").toLowerCase()] || "状态待确认";
}

function breadthStatusKind(value) {
  const normalized = String(value || "").toLowerCase();
  if (normalized === "current") return "success";
  if (["stale", "provisional"].includes(normalized)) return "warning";
  if (normalized === "unavailable") return "danger";
  return "neutral";
}

const MARKET_BREADTH_SORT_LABELS = {
  change_pct: "涨跌幅",
  advance_share: "板块上涨占比",
  turnover_rate: "换手率",
  volume_ratio: "量比",
  market_cap: "总市值",
  constituent_count: "成分数量",
  name: "板块名称",
};

function breadthWarningText(item, coverage) {
  const code = String(item?.code || "");
  if (code === "not_exchange_certified") {
    return "东方财富是第三方公开研究来源，不是交易所认证行情。";
  }
  if (code === "provider_breadth_scope") {
    return "上涨、下跌和平盘家数来自上证、深证、北证三条基准响应，是提供方定义的收盘统计口径。";
  }
  if (code === "optional_metric_missing") {
    const missing = coverage?.data_quality?.sector_rows_with_missing_optional_values;
    return `${formatInteger(missing)} 条板块记录至少缺少一个可选指标；页面保留“—”，没有填充为 0。`;
  }
  if (code === "market_breadth_stale") {
    return "本地市场宽度快照早于当前已完成收盘日。";
  }
  if (code === "after_completed_session_cutoff") {
    return "所选市场宽度快照晚于当前已完成收盘日，只能作为待复核证据。";
  }
  return String(item?.message || item || "证据边界待复核");
}

function breadthChangeText(value) {
  const parsed = finite(value);
  if (parsed === null) return "涨跌不可用";
  if (parsed > 0) return `上涨 ${formatPercentPoints(parsed, true)}`;
  if (parsed < 0) return `下跌 ${formatPercentPoints(parsed, true)}`;
  return `平盘 ${formatPercentPoints(parsed)}`;
}

function breadthCountText(value) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  return `${parsed > 0 ? "+" : ""}${formatInteger(parsed)}`;
}

function renderMarketBreadth(data, requestError = "") {
  const snapshot = data || {};
  const summary = snapshot.summary || {};
  const coverage = snapshot.coverage || {};
  const source = snapshot.source || {};
  const quality = coverage.data_quality || {};
  const breadth = Array.isArray(snapshot.breadth) ? snapshot.breadth : [];
  const sectors = Array.isArray(snapshot.sectors) ? snapshot.sectors : [];
  const revisions = Array.isArray(snapshot.revisions) ? snapshot.revisions : [];
  const filters = snapshot.filters || {};
  const status = String(snapshot.status || (snapshot.available === true ? "current" : "unavailable")).toLowerCase();
  const statusKind = breadthStatusKind(status);
  const running = state.jobs.some((job) => job.action === "refresh-market-breadth" && ["queued", "running"].includes(job.status));
  const latestJob = state.jobs.find((job) => job.action === "refresh-market-breadth");
  const warnings = Array.isArray(snapshot.warnings) ? snapshot.warnings : [];
  const errors = Array.isArray(snapshot.errors) ? snapshot.errors : [];
  const available = snapshot.available === true;
  const returnedSectorCount = summary.returned_sector_count ?? sectors.length;
  const matchedSectorCount = summary.matched_sector_count ?? sectors.length;
  const breadthSecids = Array.isArray(source.breadth_secids)
    ? source.breadth_secids.map((item) => String(item))
    : [];
  const sortKey = String(filters.sort || "change_pct");
  const sortLabel = MARKET_BREADTH_SORT_LABELS[sortKey] || "排序指标待确认";
  const form = `<form class="filter-form breadth-filter-form" id="market-breadth-filter-form" aria-describedby="market-breadth-filter-help">
    <div class="field"><label for="market-breadth-date">交易日期</label><input id="market-breadth-date" name="date" type="date" value="${escapeHtml(filters.trade_date ?? filters.date ?? "")}"></div>
    <div class="field breadth-query"><label for="market-breadth-query">板块名称或代码</label><input id="market-breadth-query" name="q" type="search" maxlength="100" value="${escapeHtml(filters.q ?? filters.query ?? "")}" placeholder="输入板块关键词"></div>
    <div class="field"><label for="market-breadth-sort">排名指标</label><select id="market-breadth-sort" name="sort">
      ${[["change_pct", "涨跌幅"], ["advance_share", "板块上涨占比"], ["turnover_rate", "换手率"], ["volume_ratio", "量比"], ["market_cap", "总市值"], ["constituent_count", "成分数量"], ["name", "板块名称"]].map(([value, label]) => `<option value="${value}"${String(filters.sort || "change_pct") === value ? " selected" : ""}>${label}</option>`).join("")}
    </select></div>
    <div class="field"><label for="market-breadth-direction">排序方向</label><select id="market-breadth-direction" name="direction"><option value="desc"${String(filters.direction || "desc") === "desc" ? " selected" : ""}>从高到低</option><option value="asc"${String(filters.direction || "") === "asc" ? " selected" : ""}>从低到高</option></select></div>
    <div class="field"><label for="market-breadth-limit">最多返回</label><input id="market-breadth-limit" name="limit" type="number" min="1" max="500" step="1" inputmode="numeric" value="${escapeHtml(filters.limit || "200")}"></div>
    <div class="filter-actions"><button class="button secondary" type="submit">应用筛选</button><button class="button secondary" type="button" data-market-breadth-filter-clear>清除条件</button></div>
  </form><p id="market-breadth-filter-help" class="section-note">筛选只读取已固化的完整板块快照，不会重新请求网络。板块排名覆盖东方财富 m:90+t:2 定义的板块集合，不代表经许可的纯行业分类；宽度统计也保留提供方口径。</p>`;
  const statusCallout = running
    ? `<aside class="callout warning" role="status" aria-live="polite"><strong>市场宽度正在刷新</strong><p>后台任务正在抓取并校验全部板块分页和三市场宽度；发布完成前继续显示上一份完整快照。</p></aside>`
    : latestJob?.status === "failed"
      ? `<aside class="callout danger" role="alert"><strong>最近一次市场宽度刷新失败</strong><p>已有快照没有被覆盖。请在<a href="#system">系统任务</a>中查看错误日志，再重新运行刷新。</p></aside>`
      : requestError
        ? `<aside class="callout danger" role="alert"><strong>市场宽度接口读取失败</strong><p>${escapeHtml(requestError)}</p></aside>`
        : !available
          ? `<aside class="callout warning" role="status"><strong>尚未固化市场宽度快照</strong><p>${escapeHtml(errors[0]?.message || "先运行刷新市场宽度，再查看行业排名和来源统计。")}</p></aside>`
          : status === "stale"
            ? `<aside class="callout warning" role="status"><strong>市场宽度快照早于当前行情</strong><p>当前显示 ${escapeHtml(snapshot.trade_date || "未知日期")} 的已固化证据，刷新前不要把它当作最新收盘宽度。</p></aside>`
            : status === "provisional"
              ? `<aside class="callout warning" role="status"><strong>所选市场宽度晚于完成截止</strong><p>当前快照为 ${escapeHtml(snapshot.trade_date || "未知日期")}，只能作为待复核证据。</p></aside>`
              : available && sectors.length === 0
                ? `<aside class="callout warning" role="status"><strong>当前筛选没有匹配板块</strong><p>来源快照仍然完整；请清除关键词或放宽筛选条件。</p></aside>`
                : "";
  const warningMarkup = warnings.length
    ? `<aside class="callout warning" role="status"><strong>证据边界</strong><ul>${warnings.map((item) => `<li>${escapeHtml(breadthWarningText(item, coverage))}</li>`).join("")}</ul></aside>`
    : "";
  const exchangeRows = breadth.length
    ? breadth.map((item) => `<tr>
        <td><strong>${escapeHtml(item.exchange || "—")}</strong><span class="table-subtext">${escapeHtml(item.benchmark_name || item.benchmark_code || "—")}</span></td>
        <td class="numeric ${tone(item.change_pct)}"><strong>${escapeHtml(breadthChangeText(item.change_pct))}</strong></td>
        <td class="numeric"><strong>${formatInteger(item.advancers)}</strong><span class="table-subtext">上涨</span></td>
        <td class="numeric"><strong>${formatInteger(item.decliners)}</strong><span class="table-subtext">下跌</span></td>
        <td class="numeric"><strong>${formatInteger(item.unchanged)}</strong><span class="table-subtext">平盘</span></td>
        <td class="numeric"><strong>${formatPercent(item.advance_share)}</strong><span class="table-subtext">上涨占比</span></td>
        <td class="numeric ${tone(item.net_advances)}"><strong>${escapeHtml(breadthCountText(item.net_advances))}</strong><span class="table-subtext">涨跌差</span></td>
      </tr>`).join("")
    : emptyRow(7, available ? "来源未返回可用的交易所宽度记录" : "尚无已发布的市场宽度证据");
  const sectorRows = sectors.length
    ? sectors.map((item, index) => `<tr>
        <td class="numeric">${formatInteger(index + 1)}</td>
        <td><strong>${escapeHtml(item.name || "—")}</strong><span class="table-subtext mono">${escapeHtml(item.code || "—")}</span></td>
        <td class="numeric ${tone(item.change_pct)}"><strong>${escapeHtml(breadthChangeText(item.change_pct))}</strong></td>
        <td class="numeric"><strong>${formatPercent(item.advance_share)}</strong><span class="table-subtext">${formatInteger(item.constituent_count)} 成分</span></td>
        <td class="numeric"><strong>${formatInteger(item.advancers)} / ${formatInteger(item.decliners)} / ${formatInteger(item.unchanged)}</strong><span class="table-subtext">涨 / 跌 / 平</span></td>
        <td class="numeric">${formatPercentPoints(item.turnover_rate)}</td>
        <td class="numeric">${formatNumber(item.volume_ratio)}</td>
        <td class="numeric">${formatCompactMoney(item.market_cap)}</td>
        <td class="numeric">${escapeHtml(item.quote_date || "—")}</td>
      </tr>`).join("")
    : emptyRow(9, available ? "没有记录符合当前筛选条件" : "尚无已发布的板块证据");
  const bestWorst = available
    ? `<div class="path-list">
        <div class="path-row"><span>涨幅最高</span><code>${escapeHtml(summary.best_sector_name || "—")} ${escapeHtml(formatPercentPoints(summary.best_sector_change_pct, true))}</code></div>
        <div class="path-row"><span>跌幅最大</span><code>${escapeHtml(summary.worst_sector_name || "—")} ${escapeHtml(formatPercentPoints(summary.worst_sector_change_pct, true))}</code></div>
        <div class="path-row"><span>板块中位数</span><code>${escapeHtml(formatPercentPoints(summary.median_sector_change_pct, true))}</code></div>
      </div>`
    : `<p class="section-note">来源不可用时不显示极值，不把缺失解释为零。</p>`;
  const orderedRevisions = [...revisions].reverse();
  const revisionRows = orderedRevisions.length
    ? orderedRevisions.map((item) => `<tr><td>${item.revision_id === snapshot.revision_id ? statusChip("当前", "info") : statusChip("历史", "neutral")}</td><td class="mono">${escapeHtml(item.revision_id || "—")}</td><td>${escapeHtml(item.trade_date || snapshot.trade_date || "—")}</td><td>${formatDate(item.retrieved_at, true)}</td><td class="mono">${escapeHtml(shortFingerprint(item.evidence_fingerprint))}</td></tr>`).join("")
    : emptyRow(5, "尚无修订历史");
  return `<section id="market-breadth-evidence" class="intelligence-dataset market-breadth-dataset">
    ${pageIntro("市场宽度与板块排名", "用同一收盘日的来源计数观察上涨/下跌广度，再比较东方财富定义的板块集合；不自动生成交易信号。", actionButton("refresh-market-breadth", "primary"))}
    <section class="intelligence-filter-band" aria-label="市场宽度筛选">${form}</section>
    <section class="metric-strip" aria-label="市场宽度摘要">
      ${metric("证据状态", breadthStatusLabel(status), `${snapshot.trade_date || "日期不可用"} · ${coverage.sector_complete === true && coverage.breadth_complete === true ? "分页与宽度已核对" : "覆盖不可确认"}`, statusKind === "success" ? "tone-positive" : statusKind === "warning" ? "tone-warning" : "tone-negative")}
      ${metric("上涨 / 下跌 / 平盘", available ? `${formatInteger(summary.advancers)} / ${formatInteger(summary.decliners)} / ${formatInteger(summary.unchanged)}` : "不可用", available ? "东方财富三基准来源统计" : "尚无已发布证据，不能解释为零")}
      ${metric("上涨占比", available ? formatPercent(summary.advance_share) : "不可用", available ? `涨跌比 ${formatNumber(summary.advance_decline_ratio)} · 涨跌差 ${breadthCountText(summary.net_advances)}` : "来源宽度不可用", available ? tone(summary.advance_share - 0.5) : "tone-negative")}
      ${metric("板块上涨 / 下跌", available ? `${formatInteger(summary.positive_sector_count)} / ${formatInteger(summary.negative_sector_count)}` : "不可用", available ? `平盘 ${formatInteger(summary.flat_sector_count)} · 显示 ${formatInteger(returnedSectorCount)} / ${formatInteger(matchedSectorCount)}` : "尚无板块证据")}
      ${metric("板块涨跌中位数", available ? formatPercentPoints(summary.median_sector_change_pct, true) : "不可用", available ? `来源 ${formatInteger(summary.sector_count)} 个板块` : "来源计数不可用", available ? tone(summary.median_sector_change_pct) : "tone-negative")}
    </section>
    ${statusCallout}${warningMarkup}
    <section class="equal-layout">
      <article class="panel">
        ${panelHeader("交易所宽度", "三条基准响应的上涨、下跌和平盘家数")}
        <div class="table-wrap" aria-label="交易所宽度表"><table class="data-table compact exchange-breadth-table"><thead><tr><th>市场</th><th>指数涨跌</th><th class="numeric">上涨</th><th class="numeric">下跌</th><th class="numeric">平盘</th><th class="numeric">上涨占比</th><th class="numeric">涨跌差</th></tr></thead><tbody>${exchangeRows}</tbody></table></div>
      </article>
      <article class="panel">
        ${panelHeader("板块极值", "只从当前完整板块快照计算")}
        ${bestWorst}
        <div class="path-list"><div class="path-row"><span>板块数据质量</span><code>${available ? `${formatInteger(coverage.sector_pages)} 页 · ${formatInteger(coverage.sector_received_count)} / ${formatInteger(coverage.sector_declared_count)} 条` : "不可用"}</code></div><div class="path-row"><span>可选指标缺失</span><code>${available ? formatInteger(quality.sector_rows_with_missing_optional_values) : "不可用"}</code></div></div>
      </article>
    </section>
    <section class="panel">
      ${panelHeader("板块排名", `${snapshot.trade_date || "日期不可用"} · ${formatInteger(returnedSectorCount)} 条显示 · ${sortLabel} ${String(filters.direction || "desc") === "desc" ? "降序" : "升序"}`)}
      <div class="table-wrap" aria-label="板块排名宽表"><table class="data-table market-breadth-table"><thead><tr><th class="numeric">排名</th><th>板块</th><th>涨跌</th><th class="numeric">上涨占比</th><th class="numeric">涨 / 跌 / 平</th><th class="numeric">换手率</th><th class="numeric">量比</th><th class="numeric">总市值</th><th class="numeric">报价日期</th></tr></thead><tbody>${sectorRows}</tbody></table></div>
    </section>
    <section class="equal-layout">
      <article class="panel">
        ${panelHeader("来源与完整性", "复核日期、分页、统计口径和指纹")}
        <div class="path-list"><div class="path-row"><span>交易日期</span><code>${escapeHtml(snapshot.trade_date || "—")}</code></div><div class="path-row"><span>完成截止</span><code>${escapeHtml(snapshot.freshness?.completed_session_cutoff || "—")}</code></div><div class="path-row"><span>板块来源</span><code>${escapeHtml(source.sector_endpoint || "—")} · ${escapeHtml(source.sector_filter || "—")}</code></div><div class="path-row"><span>宽度来源</span><code>${escapeHtml(source.breadth_endpoint || "—")} · ${escapeHtml(breadthSecids.join(", ") || "—")}</code></div><div class="path-row"><span>响应指纹</span><code>${escapeHtml(source.response_sha256 || "—")}</code></div><div class="path-row"><span>证据指纹</span><code>${escapeHtml(snapshot.evidence_fingerprint || "—")}</code></div></div>
      </article>
      <article class="panel">
        ${panelHeader("不可变修订", "规范化证据相同则复用，记录变化才追加")}
        <div class="table-wrap" aria-label="市场宽度修订历史"><table class="data-table compact"><thead><tr><th>状态</th><th>快照 ID</th><th>交易日</th><th>抓取时间</th><th>证据指纹</th></tr></thead><tbody>${revisionRows}</tbody></table></div>
      </article>
    </section>
    <aside class="callout info intelligence-boundary"><strong>研究权限边界</strong><p>市场宽度与板块排名是第三方收盘证据，不是交易所认证行情，也不是市场情绪结论。当前页面固定为 ${snapshot.authority?.research_only === true && snapshot.authority?.execution_authorized === false ? "research_only" : "权限异常，需停止使用"}；它不能修改策略、持仓、订单、风控门禁或真实交易授权。</p></aside>
  </section>`;
}

function renderMarketIntelligence(payload) {
  const dragonTiger = payload?.dragon_tiger || payload || {};
  const breadth = payload?.breadth || {};
  return `<div class="page-stack intelligence-page">
    ${pageIntro("收盘市场情报", "先看市场宽度与板块分化，再复核龙虎榜事件；两个数据集各自披露日期、来源和完整性。")}
    <nav class="intelligence-jump-nav" aria-label="市场情报数据集"><a href="#market-breadth-evidence">市场宽度与板块排名</a><a href="#dragon-tiger-evidence">龙虎榜收盘证据</a></nav>
    ${renderMarketBreadth(breadth, payload?.breadth_error || "")}
    ${renderDragonTigerIntelligence(dragonTiger, payload?.dragon_tiger_error || "")}
  </div>`;
}

function renderDragonTigerIntelligence(data, requestError = "") {
  data = data || {};
  const records = Array.isArray(data.records) ? data.records : [];
  const coverage = data.coverage || {};
  const dataQuality = coverage.data_quality || {};
  const freshness = data.freshness || {};
  const summary = data.summary || {};
  const source = data.source || {};
  const authority = data.authority || {};
  const revisions = Array.isArray(data.revisions) ? data.revisions : [];
  const filters = data.filters || state.intelligenceFilters || {};
  const status = String(data.status || "unavailable").toLowerCase();
  const contentStatus = String(data.content_status || (
    data.available === true && coverage.complete === true && summary.record_count === 0
      ? "empty"
      : data.available === true ? "records" : "unavailable"
  )).toLowerCase();
  const statusKind = intelligenceStatusKind(status);
  const running = state.jobs.some((job) => job.action === "refresh-market-intelligence" && ["queued", "running"].includes(job.status));
  const latestJob = state.jobs.find((job) => job.action === "refresh-market-intelligence");
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const errors = Array.isArray(data.errors) ? data.errors : [];
  const returnedCount = summary.returned_count ?? records.length;
  const matchedCount = summary.matched_count ?? records.length;
  const reportedCount = coverage.declared_count ?? summary.record_count ?? records.length;
  const sourceSymbols = summary.security_count ?? new Set(records.map((item) => item.symbol).filter(Boolean)).size;
  const orderedRevisions = [...revisions].reverse();
  const marketOptions = [
    ["", "全部市场"],
    ["SH", "上海证券交易所"],
    ["SZ", "深圳证券交易所"],
    ["BJ", "北京证券交易所"],
  ];
  const filter = `<form class="filter-form intelligence-filter-form" id="market-intelligence-filter-form" aria-describedby="market-intelligence-filter-help">
    <div class="field"><label for="intelligence-date">交易日期</label><input id="intelligence-date" name="date" type="date" value="${escapeHtml(filters.trade_date ?? filters.date ?? "")}"></div>
    <div class="field"><label for="intelligence-symbol">证券代码</label><input id="intelligence-symbol" name="symbol" type="text" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" value="${escapeHtml(filters.symbol || "")}" placeholder="六位代码"></div>
    <div class="field"><label for="intelligence-market">交易市场</label><select id="intelligence-market" name="market">${marketOptions.map(([value, label]) => `<option value="${value}"${String(filters.market || "") === value ? " selected" : ""}>${label}</option>`).join("")}</select></div>
    <div class="field intelligence-query"><label for="intelligence-query">名称或上榜原因</label><input id="intelligence-query" name="q" type="search" maxlength="100" value="${escapeHtml(filters.q ?? filters.query ?? "")}" placeholder="输入关键词"></div>
    <div class="field"><label for="intelligence-limit">最多返回</label><input id="intelligence-limit" name="limit" type="number" min="1" max="500" step="1" inputmode="numeric" value="${escapeHtml(filters.limit || "200")}"></div>
    <div class="filter-actions"><button class="button secondary" type="submit">应用筛选</button><button class="button secondary" type="button" data-intelligence-filter-clear>清除条件</button></div>
  </form><p id="market-intelligence-filter-help" class="section-note">筛选只读取已固化的完整快照，不会重新请求网络。留空日期时读取最近一次成功发布的交易日。</p>`;
  const statusCallout = running
    ? `<aside class="callout warning" role="status" aria-live="polite"><strong>龙虎榜正在刷新</strong><p>后台任务正在抓取并校验全部分页；发布完成前继续显示上一份完整快照，不会展示半份结果。</p></aside>`
    : latestJob?.status === "failed"
      ? `<aside class="callout danger" role="alert"><strong>最近一次刷新失败</strong><p>已有快照没有被覆盖。请在<a href="#system">系统任务</a>中查看错误日志，再重新运行刷新。</p></aside>`
      : requestError
        ? `<aside class="callout danger" role="alert"><strong>龙虎榜接口读取失败</strong><p>${escapeHtml(requestError)}</p></aside>`
        : status === "unavailable"
        ? `<aside class="callout warning" role="status"><strong>尚未固化龙虎榜快照</strong><p>${escapeHtml(intelligenceUnavailableText(errors[0] || data.empty_state))}</p></aside>`
        : contentStatus === "empty" && !["stale", "provisional"].includes(status)
          ? `<aside class="callout warning" role="status"><strong>该交易日没有龙虎榜记录</strong><p>来源返回计数为 0 且分页校验完整；这与尚未刷新或网络失败是不同状态。</p></aside>`
          : status === "stale" || data.stale === true
            ? `<aside class="callout warning" role="status"><strong>龙虎榜快照早于当前行情</strong><p>${contentStatus === "empty" ? "该日来源经完整校验返回 0 条记录；" : ""}当前显示 ${escapeHtml(data.trade_date || "未知日期")} 的已固化证据，刷新前不要把它当作最新收盘事件。</p></aside>`
            : status === "provisional"
              ? `<aside class="callout warning" role="status"><strong>所选快照晚于完成截止</strong><p>${contentStatus === "empty" ? "该日来源经完整校验返回 0 条记录；" : ""}当前快照为 ${escapeHtml(data.trade_date || "未知日期")}，完成截止为 ${escapeHtml(freshness.completed_session_cutoff || "不可用")}；它只能作为待复核证据。</p></aside>`
              : data.available === true && freshness.status === "unknown"
                ? `<aside class="callout warning" role="status"><strong>无法核对快照新鲜度</strong><p>本地行情缓存不可用，当前只能确认龙虎榜快照自身完整，不能确认它是否为最近完成收盘日。</p></aside>`
                : "";
  const warningMarkup = warnings.length
    ? `<aside class="callout warning" role="status"><strong>证据边界</strong><ul>${warnings.map((item) => `<li>${escapeHtml(intelligenceWarningText(item, coverage))}</li>`).join("")}</ul></aside>`
    : "";
  const rows = records.length
    ? records.map((item) => {
      const symbol = item.symbol || item.security_code || "—";
      const change = item.change_pct ?? item.change_rate;
      const net = item.net_amount ?? item.billboard_net_amount;
      const reason = item.reason_text || item.reason || item.explanation || "原因未提供";
      return `<tr>
        <td class="symbol-cell"><strong>${escapeHtml(symbol)}</strong><span>${escapeHtml(item.name || item.security_name || "名称未提供")}</span></td>
        <td>${escapeHtml(item.trade_market || item.market_name || item.market || "—")}</td>
        <td class="numeric">${formatNumber(item.close ?? item.close_price, 2)}</td>
        <td class="numeric ${tone(change)}">${formatPercentPoints(change, true)}</td>
        <td class="numeric">${formatPercentPoints(item.turnover_pct ?? item.turnover_rate)}</td>
        <td class="numeric">${formatMoney(item.buy_amount ?? item.billboard_buy_amount)}</td>
        <td class="numeric">${formatMoney(item.sell_amount ?? item.billboard_sell_amount)}</td>
        <td class="numeric ${tone(net)}">${formatMoney(net)}</td>
        <td class="numeric ${tone(item.net_ratio ?? item.deal_net_ratio)}">${formatPercentPoints(item.net_ratio ?? item.deal_net_ratio, true)}</td>
        <td class="intelligence-reason"><strong>${escapeHtml(reason)}</strong><span class="table-subtext mono">${escapeHtml(item.reason_code || item.change_type || item.trade_id || "—")}</span></td>
      </tr>`;
    }).join("")
    : emptyRow(10, status === "unavailable" ? "尚无已发布的龙虎榜证据" : contentStatus === "empty" ? "该交易日来源返回 0 条记录" : "没有记录符合当前筛选条件");
  const revisionRows = orderedRevisions.length
    ? orderedRevisions.map((item) => `<tr><td>${item.revision_id === data.revision_id ? statusChip("当前", "info") : statusChip("历史", "neutral")}</td><td class="mono">${escapeHtml(item.revision_id || "—")}</td><td>${escapeHtml(item.trade_date || data.trade_date || "—")}</td><td>${formatDate(item.retrieved_at, true)}</td><td class="mono">${escapeHtml(shortFingerprint(item.evidence_fingerprint))}</td></tr>`).join("")
    : emptyRow(5, "尚无修订历史");
  return `<section id="dragon-tiger-evidence" class="intelligence-dataset dragon-tiger-dataset">
    ${pageIntro("龙虎榜收盘证据", "逐页校验东方财富日频龙虎榜，并将完整结果固化为只读修订链。", actionButton("refresh-market-intelligence", "primary"))}
    <section class="intelligence-filter-band" aria-label="龙虎榜筛选">${filter}</section>
    <section class="metric-strip" aria-label="龙虎榜摘要">
      ${metric("证据状态", intelligenceStatusLabel(status), `${data.trade_date || "日期不可用"} · ${coverage.complete === true ? "分页已核对" : "覆盖不可确认"}${contentStatus === "empty" && status !== "empty" ? " · 合法空集" : ""}`, statusKind === "success" ? "tone-positive" : statusKind === "warning" ? "tone-warning" : "tone-negative")}
      ${metric("显示记录 / 来源证券", `${formatInteger(returnedCount)} / ${data.available === true ? formatInteger(sourceSymbols) : "不可用"}`, data.available === true ? `来源 ${formatInteger(reportedCount)} 条 · 匹配 ${formatInteger(matchedCount)} 条 · 显示 ${formatInteger(returnedCount)} 条` : `来源计数不可用 · 显示 ${formatInteger(returnedCount)} 条`)}
      ${metric("全量龙虎榜净额", data.available === true ? formatMoney(summary.net_amount) : "不可用", data.available === true ? `来源全量：买入 ${formatMoney(summary.buy_amount)} · 卖出 ${formatMoney(summary.sell_amount)}` : "尚无已发布证据，不能解释为零值", data.available === true ? tone(summary.net_amount) : "tone-negative")}
      ${metric("来源 / 修订", source.provider === "eastmoney" ? "东方财富" : source.provider || "未提供", `${formatInteger(revisions.length)} 个可读修订 · 非交易所认证`, "tone-info")}
    </section>
    ${statusCallout}${warningMarkup}
    <section class="panel">
      ${panelHeader("龙虎榜明细", `${data.trade_date || "日期不可用"} · ${escapeHtml(source.report_name || "RPT_DAILYBILLBOARD_DETAILSNEW")} · ${formatInteger(returnedCount)} 条`)}
      <div class="table-wrap" aria-label="龙虎榜明细宽表"><table class="data-table intelligence-table">
        <thead><tr><th>证券</th><th>市场</th><th class="numeric">收盘价</th><th class="numeric">涨跌幅</th><th class="numeric">换手率</th><th class="numeric">买入额</th><th class="numeric">卖出额</th><th class="numeric">净额</th><th class="numeric">净额占比</th><th>上榜原因</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </section>
    <section class="equal-layout">
      <article class="panel">
        ${panelHeader("来源与完整性", "复核日期、分页、指纹与来源边界")}
        <div class="path-list">
          <div class="path-row"><span>交易日期</span><code>${escapeHtml(data.trade_date || "—")}</code></div>
          <div class="path-row"><span>完成截止</span><code>${escapeHtml(freshness.completed_session_cutoff || "—")}</code></div>
          <div class="path-row"><span>来源报告</span><code>${escapeHtml(source.report_name || "—")}</code></div>
          <div class="path-row"><span>分页 / 计数</span><code>${formatInteger(coverage.pages)} 页 · ${formatInteger(coverage.received_count)} / ${formatInteger(coverage.declared_count)} 条</code></div>
          <div class="path-row"><span>字段完整性</span><code>${dataQuality.rows_with_missing_optional_values ? `${formatInteger(dataQuality.rows_with_missing_optional_values)} 条可选指标缺失；身份与金额字段完整` : data.available === true ? "身份、金额与可选指标完整" : "不可确认"}</code></div>
          <div class="path-row"><span>响应指纹</span><code>${escapeHtml(source.response_sha256 || "—")}</code></div>
          <div class="path-row"><span>证据指纹</span><code>${escapeHtml(data.evidence_fingerprint || data.fingerprint || "—")}</code></div>
        </div>
      </article>
      <article class="panel">
        ${panelHeader("不可变修订", "规范化证据相同则复用，记录变化才追加")}
        <div class="table-wrap" aria-label="龙虎榜修订历史"><table class="data-table compact"><thead><tr><th>状态</th><th>快照 ID</th><th>交易日</th><th>抓取时间</th><th>证据指纹</th></tr></thead><tbody>${revisionRows}</tbody></table></div>
      </article>
    </section>
    <aside class="callout info intelligence-boundary"><strong>研究权限边界</strong><p>龙虎榜是单一公开来源的收盘事件证据，不是交易所认证行情，也不等同于市场情绪。当前页面固定为 ${authority.research_only === true && authority.execution_authorized === false ? "research_only" : "权限异常，需停止使用"}；它不能修改策略、持仓、订单、风控门禁或真实交易授权。</p></aside>
  </section>`;
}

function renderUniverse(data) {
  const instruments = Array.isArray(data.instruments) ? data.instruments : [];
  const screen = data.screen || {};
  const filters = screen.filters || state.universeFilters || {};
  const active = instruments.filter((item) => item.active).length;
  const complete = instruments.filter((item) => item.data_status === "complete").length;
  const ready = instruments.filter((item) => item.history_ready).length;
  const counts = screen.counts || {};
  const assetClasses = [...new Set([
    "equity",
    "fixed_income",
    "commodity",
    ...instruments.map((item) => item.asset_class).filter(Boolean),
  ])].sort();
  const sectors = [...new Set(instruments.map((item) => item.sector).filter(Boolean))].sort();
  const filter = `
    <form class="filter-form universe-filter-form" id="universe-date-form" aria-describedby="universe-filter-help">
      <div class="field"><label for="universe-date">截面日期</label><input id="universe-date" name="date" type="date" value="${escapeHtml(data.date || "")}"></div>
      <div class="field"><label for="universe-asset-class">资产类别</label><select id="universe-asset-class" name="asset_class"><option value="">全部</option>${assetClasses.map((value) => `<option value="${escapeHtml(value)}"${filters.asset_class === value ? " selected" : ""}>${escapeHtml(assetClassLabel(value))}</option>`).join("")}</select></div>
      <div class="field"><label for="universe-sector">板块/分组</label><input id="universe-sector" name="sector" list="universe-sector-options" value="${escapeHtml(filters.sector || "")}" placeholder="例如 china_large_cap"><datalist id="universe-sector-options">${sectors.map((value) => `<option value="${escapeHtml(value)}"></option>`).join("")}</datalist></div>
      <div class="field"><label for="universe-trend">趋势</label><select id="universe-trend" name="trend"><option value="any"${filters.trend === "any" ? " selected" : ""}>全部</option><option value="up"${filters.trend === "up" ? " selected" : ""}>上行</option><option value="mixed"${filters.trend === "mixed" ? " selected" : ""}>混合</option><option value="down"${filters.trend === "down" ? " selected" : ""}>下行</option><option value="not_down"${filters.trend === "not_down" ? " selected" : ""}>排除下行</option></select></div>
      <div class="field"><label for="universe-coverage">数据要求</label><select id="universe-coverage" name="coverage"><option value="all"${filters.coverage === "all" ? " selected" : ""}>全部</option><option value="ready"${filters.coverage === "ready" ? " selected" : ""}>历史长度达标</option><option value="complete"${filters.coverage === "complete" ? " selected" : ""}>当日完整</option></select></div>
      <div class="field"><label for="universe-min-amount">最低 20 日成交额</label><input id="universe-min-amount" name="min_average_amount" type="number" min="0" step="100000" inputmode="decimal" value="${escapeHtml(filters.min_average_amount ?? "")}" placeholder="不限制"></div>
      <div class="field"><label for="universe-max-volatility">最高年化波动</label><input id="universe-max-volatility" name="max_annual_volatility" type="number" min="0" max="10" step="0.01" inputmode="decimal" value="${escapeHtml(filters.max_annual_volatility ?? "")}" placeholder="例如 0.35"></div>
      <div class="field"><label for="universe-sort">排序指标</label><select id="universe-sort" name="sort"><option value="momentum"${filters.sort === "momentum" ? " selected" : ""}>动量</option><option value="average_amount"${filters.sort === "average_amount" ? " selected" : ""}>成交额</option><option value="annual_volatility"${filters.sort === "annual_volatility" ? " selected" : ""}>年化波动</option><option value="latest_close"${filters.sort === "latest_close" ? " selected" : ""}>最新收盘</option><option value="coverage"${filters.sort === "coverage" ? " selected" : ""}>数据覆盖</option><option value="symbol"${filters.sort === "symbol" ? " selected" : ""}>代码</option></select></div>
      <div class="field"><label for="universe-direction">顺序</label><select id="universe-direction" name="direction"><option value="desc"${filters.direction !== "asc" ? " selected" : ""}>从高到低</option><option value="asc"${filters.direction === "asc" ? " selected" : ""}>从低到高</option></select></div>
      <label class="check-field" for="universe-active-only"><input id="universe-active-only" name="active_only" type="checkbox"${filters.active_only ? " checked" : ""}><span>仅显示当日有效</span></label>
      <div class="filter-actions"><button class="button primary" type="button" data-universe-submit>应用筛选</button><button class="button secondary" type="button" data-universe-reset>清除条件</button></div>
    </form>
    <p id="universe-filter-help" class="section-note">指标只来自同一份已校验的收盘快照；空值表示历史不足或数据不可用，不会用零填充。</p>`;
  const status = screenStatusLabel(screen.status, data.market_available);
  const statusKind = screen.status === "ok" ? "success" : screen.status === "partial" ? "warning" : screen.status === "empty" ? "warning" : "danger";
  return `
    <div class="page-stack">
      ${pageIntro("证券池筛选", "按时间点、资产、流动性和风险特征比较全部候选证券", filter)}
      <section class="metric-strip" aria-label="筛选摘要">
        ${metric("返回证券", `${formatInteger(counts.returned ?? instruments.length)} / ${formatInteger(counts.input ?? data.candidate_records)}`, `匹配 ${formatInteger(counts.matched ?? instruments.length)}，排除 ${formatInteger(counts.excluded ?? 0)}`)}
        ${metric("数据状态", status, `${complete} 条当日完整，${ready} 条历史长度达标`, statusKind === "success" ? "tone-positive" : "tone-negative")}
        ${metric("最低研究历史", `${formatInteger(screen.minimum_history_bars)} 日`, `截面 ${data.date || "—"}`)}
        ${metric("排序口径", screenSortLabel(filters.sort, filters.direction), `快照 ${screen.snapshot_id ? String(screen.snapshot_id).slice(-16) : "—"}`)}
      </section>

      ${screen.empty_reason ? `<aside class="callout warning"><strong>${screen.empty_reason === "market_data_unavailable" ? "行情快照不可用" : "没有证券符合当前条件"}</strong><p>${screen.empty_reason === "market_data_unavailable" ? "先刷新已完成收盘数据，再重新运行筛选。" : "放宽资产、趋势、覆盖或数值条件；筛选不会改变投资池或策略配置。"}</p></aside>` : ""}
      ${Array.isArray(screen.warnings) && screen.warnings.length ? `<aside class="callout warning"><strong>数据边界</strong><ul>${screen.warnings.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul></aside>` : ""}

      <section class="panel">
        ${panelHeader("横向研究结果", `${data.date || "—"} 的同一快照 · ${screenSortLabel(filters.sort, filters.direction)}`)}
        <div class="table-wrap"><table class="data-table universe-screen-table">
          <thead><tr><th>证券</th><th>资产/分组</th><th class="numeric">最新收盘</th><th class="numeric">动量</th><th class="numeric">年化波动</th><th class="numeric">20 日成交额</th><th>趋势</th><th>历史</th><th>数据状态</th><th>资格</th><th>覆盖</th></tr></thead>
          <tbody>${instruments.length ? instruments.map((item) => `<tr>
            <td class="symbol-cell"><strong>${escapeHtml(item.symbol)}</strong><span>${escapeHtml(instrumentName(item.symbol, item.name))}</span></td>
            <td><span>${escapeHtml(assetClassLabel(item.asset_class))}</span><span class="table-subtext">${escapeHtml(item.sector || "—")}</span></td>
            <td class="numeric">${formatNumber(item.latest_close, 3)}</td>
            <td class="numeric ${tone(item.momentum)}">${formatPercent(item.momentum, true)}</td>
            <td class="numeric">${formatPercent(item.annual_volatility)}</td>
            <td class="numeric">${item.average_amount == null ? "—" : formatMoney(item.average_amount)}</td>
            <td>${statusChip(trendLabel(item.trend), trendKind(item.trend))}</td>
            <td>${statusChip(item.history_ready ? `达标 ${formatInteger(item.history_bars)} 日` : `${formatInteger(item.history_bars)} / ${formatInteger(screen.minimum_history_bars)} 日`, item.history_ready ? "success" : "warning")}</td>
            <td>${statusChip(screenDataStatusLabel(item.data_status), screenDataStatusKind(item.data_status))}</td>
            <td>${booleanChip(Boolean(item.active), "有效", eligibilityLabel(item.eligibility_reasons))}</td>
            <td class="mono">${escapeHtml(item.latest_bar_date || item.coverage?.last || "—")}${item.data_lag_days ? ` · 滞后 ${formatInteger(item.data_lag_days)} 日` : ""}</td>
          </tr>`).join("") : emptyRow(11, screen.empty_reason === "market_data_unavailable" ? "行情快照不可用，暂无可计算指标" : "没有证券符合当前筛选条件")}</tbody>
        </table></div>
      </section>

      <section class="equal-layout">
        <article class="panel">
          ${panelHeader("筛选证据", "条件与数据身份")}
          <div class="path-list">
            <div class="path-row"><span>筛选条件</span><code>${escapeHtml(JSON.stringify(screen.filters || {}))}</code></div>
            <div class="path-row"><span>条件指纹</span><code>${escapeHtml(screen.filter_fingerprint || "—")}</code></div>
            <div class="path-row"><span>筛选快照</span><code>${escapeHtml(screen.snapshot_id || "—")}</code></div>
            <div class="path-row"><span>主数据指纹</span><code>${escapeHtml(data.master_sha256 || "—")}</code></div>
          </div>
        </article>
        <aside class="callout warning"><strong>研究边界</strong><p>筛选结果只是候选集合，不会自动生成订单、改变策略参数或绕过风险门禁。基本面和情绪数据仍需单独接入并标记覆盖范围。</p></aside>
      </section>
    </div>`;
}

function renderUniverseEnhanced(data) {
  const instruments = Array.isArray(data.instruments) ? data.instruments : [];
  const screen = data.screen || {};
  const filters = screen.filters || state.universeFilters || {};
  const counts = screen.counts || {};
  const quality = screen.data_quality || {};
  const sourceSummary = screen.source_summary || {};
  const returnedSourceSummary = screen.returned_source_summary || {};
  const primarySource = returnedSourceSummary.providers?.[0] || sourceSummary.providers?.[0];
  const sourceValue = primarySource
    ? `${screenSourceLabel(primarySource.provider)} ${formatInteger(primarySource.count)} / ${formatInteger(returnedSourceSummary.instrument_count ?? sourceSummary.instrument_count ?? 0)}`
    : "未提供";
  const maxLag = finite(quality.lag_days?.maximum);
  const coverageMedian = finite(quality.coverage_percent?.median);
  const fallbackUsed = Number(sourceSummary.fallback_count || 0) > 0;
  const assetClasses = [...new Set([
    "equity",
    "fixed_income",
    "commodity",
    ...instruments.map((item) => item.asset_class).filter(Boolean),
  ])].sort();
  const sectors = [...new Set(instruments.map((item) => item.sector).filter(Boolean))].sort();
  const option = (value, label, selected) => `<option value="${escapeHtml(value)}"${selected ? " selected" : ""}>${escapeHtml(label)}</option>`;
  const filter = `
    <form class="filter-form universe-filter-form" id="universe-date-form" aria-describedby="universe-filter-help">
      <div class="field"><label for="universe-date">截面日期</label><input id="universe-date" name="date" type="date" value="${escapeHtml(data.date || "")}"></div>
      <div class="field"><label for="universe-asset-class">资产类别</label><select id="universe-asset-class" name="asset_class">${option("", "全部", !filters.asset_class)}${assetClasses.map((value) => option(value, assetClassLabel(value), filters.asset_class === value)).join("")}</select></div>
      <div class="field"><label for="universe-sector">板块/分组</label><input id="universe-sector" name="sector" list="universe-sector-options" value="${escapeHtml(filters.sector || "")}" placeholder="例如 china_large_cap"><datalist id="universe-sector-options">${sectors.map((value) => `<option value="${escapeHtml(value)}"></option>`).join("")}</datalist></div>
      <div class="field"><label for="universe-trend">趋势</label><select id="universe-trend" name="trend">${option("any", "全部", filters.trend === "any")}${option("up", "上行", filters.trend === "up")}${option("mixed", "混合", filters.trend === "mixed")}${option("down", "下行", filters.trend === "down")}${option("not_down", "排除下行", filters.trend === "not_down")}</select></div>
      <div class="field"><label for="universe-coverage">数据要求</label><select id="universe-coverage" name="coverage">${option("all", "全部", filters.coverage === "all")}${option("ready", "历史长度达标", filters.coverage === "ready")}${option("complete", "当日完整", filters.coverage === "complete")}</select></div>
      <div class="field"><label for="universe-min-amount">最低 20 日成交额</label><input id="universe-min-amount" name="min_average_amount" type="number" min="0" step="100000" inputmode="decimal" value="${escapeHtml(filters.min_average_amount ?? "")}" placeholder="不限制"></div>
      <div class="field"><label for="universe-max-volatility">最高年化波动</label><input id="universe-max-volatility" name="max_annual_volatility" type="number" min="0" max="10" step="0.01" inputmode="decimal" value="${escapeHtml(filters.max_annual_volatility ?? "")}" placeholder="例如 0.35"></div>
      <div class="field"><label for="universe-sort">排序指标</label><select id="universe-sort" name="sort">${option("momentum", "动量", filters.sort === "momentum")}${option("average_amount", "成交额", filters.sort === "average_amount")}${option("annual_volatility", "年化波动", filters.sort === "annual_volatility")}${option("latest_close", "最新收盘", filters.sort === "latest_close")}${option("coverage", "数据覆盖", filters.sort === "coverage")}${option("symbol", "代码", filters.sort === "symbol")}</select></div>
      <div class="field"><label for="universe-direction">顺序</label><select id="universe-direction" name="direction">${option("desc", "从高到低", filters.direction !== "asc")}${option("asc", "从低到高", filters.direction === "asc")}</select></div>
      <div class="field"><label for="universe-limit">最多返回</label><input id="universe-limit" name="limit" type="number" min="1" max="500" step="1" inputmode="numeric" value="${escapeHtml(filters.limit ?? "200")}"></div>
      <label class="check-field" for="universe-active-only"><input id="universe-active-only" name="active_only" type="checkbox"${filters.active_only ? " checked" : ""}><span>仅显示当日有效</span></label>
       <div class="filter-actions"><button class="button primary" type="button" data-universe-submit>应用筛选</button><button class="button secondary" type="button" data-universe-reset>清除条件</button></div>
    </form>
    <p id="universe-filter-help" class="section-note">指标只来自同一份已校验的收盘快照；空值表示历史不足或数据不可用，不会用零填充。最多返回 500 条。</p>`;
  const status = screenStatusLabel(screen.status, data.market_available);
  const statusKind = screen.status === "ok" ? "success" : screen.status === "partial" || screen.status === "empty" ? "warning" : "danger";
  const qualityStatus = Array.isArray(screen.warnings) && screen.warnings.length ? "tone-warning" : "tone-info";
  const warningMarkup = Array.isArray(screen.warnings) && screen.warnings.length
    ? `<aside class="callout warning" role="status" aria-live="polite"><strong>数据边界</strong><ul>${screen.warnings.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul></aside>`
    : "";
  const emptyMarkup = screen.empty_reason
    ? `<aside class="callout warning" role="status" aria-live="polite"><strong>${screen.empty_reason === "market_data_unavailable" ? "行情快照不可用" : "没有证券符合当前条件"}</strong><p>${screen.empty_reason === "market_data_unavailable" ? "先刷新已完成收盘数据，再重新运行筛选；本次响应没有用零值代替缺失指标。" : "放宽资产、趋势、覆盖或数值条件；筛选不会改变投资池或策略配置。"}</p></aside>`
    : "";
  const rows = instruments.length
    ? instruments.map((item) => `<tr>
        <td class="symbol-cell"><strong>${escapeHtml(item.symbol)}</strong><span>${escapeHtml(instrumentName(item.symbol, item.name))}</span></td>
        <td><span>${escapeHtml(assetClassLabel(item.asset_class))}</span><span class="table-subtext">${escapeHtml(item.sector || "—")}</span></td>
        <td class="numeric">${formatNumber(item.latest_close, 3)}</td>
        <td class="numeric ${tone(item.momentum)}">${formatPercent(item.momentum, true)}</td>
        <td class="numeric">${formatPercent(item.annual_volatility)}</td>
        <td class="numeric">${item.average_amount == null ? "—" : formatMoney(item.average_amount)}</td>
        <td>${statusChip(trendLabel(item.trend), trendKind(item.trend))}</td>
        <td>${statusChip(item.history_ready ? `达标 ${formatInteger(item.history_bars)} 日` : `${formatInteger(item.history_bars)} / ${formatInteger(screen.minimum_history_bars)} 日`, item.history_ready ? "success" : "warning")}</td>
        <td>${statusChip(screenDataStatusLabel(item.data_status), screenDataStatusKind(item.data_status))}</td>
        <td>${booleanChip(Boolean(item.active), "有效", eligibilityLabel(item.eligibility_reasons))}</td>
        <td class="coverage-cell"><strong class="mono">${item.coverage_percent == null ? "—" : `${formatNumber(item.coverage_percent, 1)}%`}</strong><span class="table-subtext">${escapeHtml(item.latest_bar_date || item.coverage?.last || "—")}${item.data_lag_days ? ` · 滞后 ${formatInteger(item.data_lag_days)} 日` : ""}</span></td>
        <td>${statusChip(screenSourceLabel(item.source_provider || item.source), screenSourceKind(item.source_provider || item.source))}<span class="table-subtext mono">${escapeHtml(item.source || "—")}</span></td>
      </tr>`).join("")
    : emptyRow(12, screen.empty_reason === "market_data_unavailable" ? "行情快照不可用，暂无可计算指标" : "没有证券符合当前筛选条件");
  const sourceRows = (sourceSummary.providers || []).map((entry) => `<span><strong>${escapeHtml(screenSourceLabel(entry.provider))}</strong> ${formatInteger(entry.count)} 条 · ${formatNumber(entry.percent, 1)}%</span>`).join("") || "<span>没有来源元数据</span>";
  const definitions = Object.values(screen.metric_definitions || {}).map((definition) => `<div><dt>${escapeHtml(definition.label || "指标")}</dt><dd><code>${escapeHtml(definition.formula || "—")}</code><span>${escapeHtml(definition.window || "")}</span></dd></div>`).join("") || "<div><dd>当前响应未提供指标定义。</dd></div>";
  return `<div class="page-stack">
    ${pageIntro("证券池筛选", "按时间点、资产、流动性和风险特征比较全部候选证券", filter)}
    <section class="metric-strip" aria-label="筛选摘要">
      ${metric("返回证券", `${formatInteger(counts.returned ?? instruments.length)} / ${formatInteger(counts.input ?? data.candidate_records)}`, `匹配 ${formatInteger(counts.matched ?? instruments.length)}，排除 ${formatInteger(counts.excluded ?? 0)}`)}
      ${metric("数据状态", status, `${formatInteger(screen.quality_counts?.complete ?? 0)} 条当日完整，${formatInteger(screen.quality_counts?.insufficient_history ?? 0)} 条历史不足`, statusKind === "success" ? "tone-positive" : statusKind === "warning" ? "tone-warning" : "tone-negative")}
      ${metric("来源提供方", sourceValue, fallbackUsed ? "包含回退来源，需核对数据边界" : "返回结果的主要来源", fallbackUsed ? "tone-warning" : "tone-info")}
      ${metric("覆盖 / 滞后", coverageMedian == null ? "—" : `${formatNumber(coverageMedian, 1)}%`, maxLag == null || maxLag === 0 ? "未发现日期滞后" : `最大滞后 ${formatInteger(maxLag)} 日`, maxLag > 0 ? "tone-warning" : qualityStatus)}
    </section>
    ${emptyMarkup}${warningMarkup}
    <section class="panel">
      ${panelHeader("横向研究结果", `${data.date || "—"} 的同一快照 · ${screenSortLabel(filters.sort, filters.direction)} · ${screen.completed_session_cutoff ? `完成截止 ${screen.completed_session_cutoff}` : "完成截止待确认"}`)}
      <div class="table-wrap"><table class="data-table universe-screen-table">
        <thead><tr><th>证券</th><th>资产/分组</th><th class="numeric">最新收盘</th><th class="numeric">动量</th><th class="numeric">年化波动</th><th class="numeric">20 日成交额</th><th>趋势</th><th>历史</th><th>数据状态</th><th>资格</th><th>覆盖 / 截止</th><th>来源提供方</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </section>
    <section class="screen-quality-band" aria-label="筛选质量摘要">
      <div class="screen-quality-heading"><strong>质量摘要</strong><span>对所有候选记录统计，非仅当前返回行</span></div>
      <div class="screen-quality-grid"><div><span>当日完整</span><strong>${formatNumber(quality.complete_percent, 1)}%</strong></div><div><span>历史达标</span><strong>${formatNumber(quality.history_ready_percent, 1)}%</strong></div><div><span>覆盖中位</span><strong>${formatNumber(quality.coverage_percent?.median, 1)}%</strong></div><div><span>最大滞后</span><strong>${maxLag == null ? "—" : `${formatInteger(maxLag)} 日`}</strong></div></div>
      <div class="screen-source-list" aria-label="来源分布">${sourceRows}</div>
    </section>
    <section class="equal-layout">
      <article class="panel">${panelHeader("筛选证据", "条件、数据身份与口径")}<div class="path-list"><div class="path-row"><span>筛选条件</span><code>${escapeHtml(JSON.stringify(screen.filters || {}))}</code></div><div class="path-row"><span>条件指纹</span><code>${escapeHtml(screen.filter_fingerprint || "—")}</code></div><div class="path-row"><span>筛选快照</span><code>${escapeHtml(screen.snapshot_id || "—")}</code></div><div class="path-row"><span>契约版本</span><code>screen-v${escapeHtml(screen.schema_version ?? "—")}</code></div><div class="path-row"><span>完成截止</span><code>${escapeHtml(screen.completed_session_cutoff || "—")}</code></div><div class="path-row"><span>主数据指纹</span><code>${escapeHtml(data.master_sha256 || "—")}</code></div></div></article>
      <article class="panel screen-methodology">${panelHeader("指标口径", "只读计算，空值保持空值")}<details><summary>查看公式与窗口</summary><dl>${definitions}</dl></details><p class="section-note">筛选结果只是候选集合，不会自动生成订单、改变策略参数或绕过风险门禁。基本面和情绪数据仍需单独接入并标记覆盖范围。</p></article>
    </section>
  </div>`;
}

function screenSortLabel(value, direction) {
  const labels = { momentum: "动量", average_amount: "成交额", annual_volatility: "年化波动", latest_close: "最新收盘", coverage: "数据覆盖", symbol: "代码" };
  return `${labels[value] || "代码"} · ${direction === "asc" ? "升序" : "降序"}`;
}

function screenStatusLabel(value, marketAvailable) {
  if (!marketAvailable || value === "unavailable") return "行情不可用";
  return { ok: "完整", partial: "部分可用", empty: "无匹配", loading: "加载中" }[value] || "待确认";
}

function screenSourceLabel(value) {
  return {
    eastmoney: "东方财富",
    eastmoney_network: "东方财富网络",
    tencent: "腾讯行情",
    tencent_newfqkline: "腾讯前复权",
    tencent_network_fallback: "腾讯网络回退",
    validated_local_fallback: "已验证本地回退",
    network: "网络行情",
    unknown: "来源未说明",
  }[String(value || "unknown").toLowerCase()] || value || "来源未说明";
}

function screenSourceKind(value) {
  const normalized = String(value || "unknown").toLowerCase();
  if (normalized === "unknown") return "neutral";
  if (normalized.includes("fallback")) return "warning";
  return "info";
}

function screenDataStatusLabel(value) {
  return { complete: "当日完整", stale: "快照滞后", insufficient_history: "历史不足", missing: "缺少数据" }[value] || "待确认";
}

function screenDataStatusKind(value) {
  return { complete: "success", stale: "warning", insufficient_history: "warning", missing: "danger" }[value] || "neutral";
}

function trendLabel(value) {
  return { UP: "上行", DOWN: "下行", MIXED: "混合" }[value] || "待确认";
}

function trendKind(value) {
  return { UP: "success", DOWN: "danger", MIXED: "neutral" }[value] || "neutral";
}

function translateSelection(value) {
  return value === "curated_static" ? "静态人工筛选" : value || "未说明";
}

function assetClassLabel(value) {
  return {
    equity: "权益",
    commodity: "商品",
    fixed_income: "固定收益",
  }[value] || value || "其他";
}

function eligibilityLabel(reasons) {
  if (!Array.isArray(reasons) || !reasons.length) return "无效";
  return reasons.map((reason) => ({
    not_yet_listed: "尚未上市",
    delisted: "已退市",
    outside_universe_membership: "不在投资池",
    listing_seasoning: "上市时间不足",
  }[reason] || reason)).join("、");
}

function monitoringSeverityLabel(value) {
  return MONITORING_SEVERITY_LABELS[String(value || "").toLowerCase()] || "未分级";
}

function monitoringSeverityKind(value) {
  return { critical: "danger", warning: "warning", info: "info" }[String(value || "").toLowerCase()] || "neutral";
}

function monitoringStatusLabel(value) {
  return MONITORING_STATUS_LABELS[String(value || "").toLowerCase()] || String(value || "待确认");
}

function monitoringStatusKind(value) {
  return {
    open: "warning",
    acknowledged: "success",
    snoozed: "info",
    dismissed: "neutral",
    partial: "warning",
    failed: "danger",
    succeeded: "success",
    no_rules: "warning",
    not_run: "neutral",
  }[String(value || "").toLowerCase()] || "neutral";
}

function monitoringRuleMetadata(data) {
  return Object.fromEntries((Array.isArray(data?.rule_types) ? data.rule_types : []).map((item) => [item.rule_type, item]));
}

function monitoringUnitValue(value, unit) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  if (unit === "ratio") return formatPercent(parsed);
  if (unit === "CNY") return `¥${formatNumber(parsed, 4)}`;
  if (unit === "days") return `${formatInteger(parsed)} 日`;
  if (unit === "boolean") return "条件交叉";
  return formatNumber(parsed, 2);
}

function monitoringRuleSummary(rule, metadata = {}) {
  const meta = metadata[rule?.rule_type] || {};
  const label = rule?.rule_label || meta.label || rule?.rule_type || "监控规则";
  const threshold = rule?.threshold === null || rule?.threshold === undefined
    ? ""
    : ` ${rule?.operator_label || meta.operator_label || ""} ${monitoringUnitValue(rule.threshold, meta.unit)}`;
  const window = rule?.window ? ` · 窗口 ${formatInteger(rule.window)}` : "";
  const comparison = rule?.comparison_window ? `/${formatInteger(rule.comparison_window)}` : "";
  return `${label}${threshold}${window}${comparison}`;
}

function monitoringRuleTargetValue(watchlistId, symbol) {
  return `${watchlistId}~${symbol}`;
}

function parseMonitoringRuleTarget(value) {
  const text = String(value || "");
  const index = text.indexOf("~");
  return index < 1 ? { watchlist_id: "", symbol: "" } : {
    watchlist_id: text.slice(0, index),
    symbol: text.slice(index + 1),
  };
}

function monitoringExpectedRevision(data) {
  const value = Number(data?.configuration?.revision);
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function monitoringActionBusy(key) {
  return state.monitoringActionBusy.has(String(key));
}

function monitoringActionStatusMarkup(id = "monitoring-action-status") {
  const value = state.monitoringActionStatus;
  if (!value) return `<p id="${escapeHtml(id)}" class="form-status" role="status" aria-live="polite"></p>`;
  const status = typeof value === "string" ? { message: value, kind: "" } : value;
  const role = status.kind === "error" ? "alert" : "status";
  return `<p id="${escapeHtml(id)}" class="form-status${status.kind === "error" ? " error" : ""}" role="${role}" aria-live="polite">${escapeHtml(status.message || "")}</p>`;
}

function monitoringFilteredAlerts(data) {
  const filters = state.monitoringFilters || {};
  const watchlistId = String(filters.watchlist_id || "");
  const symbol = String(filters.symbol || "").trim().toUpperCase();
  const severity = String(filters.severity || "");
  const status = String(filters.status || "unresolved");
  const alerts = Array.isArray(data?.alerts) ? data.alerts : [];
  const filtered = alerts.filter((item) => {
    if (watchlistId && item.watchlist_id !== watchlistId) return false;
    if (symbol && !String(item.symbol || "").toUpperCase().includes(symbol)) return false;
    if (severity && item.severity !== severity) return false;
    if (status === "unresolved" && !["open", "snoozed"].includes(item.status)) return false;
    if (status && status !== "unresolved" && status !== "all" && item.status !== status) return false;
    return true;
  });
  const limit = Math.max(1, Math.min(200, Number(filters.limit) || 100));
  return filtered.slice(0, limit);
}

function monitoringEmptyStateMarkup(data, filteredAlerts) {
  const empty = data?.empty_state || {};
  const code = String(empty.code || "");
  const scan = data?.scan || {};
  const snapshot = data?.snapshot || {};
  if (state.monitoringBusy) {
    return `<aside class="callout info monitoring-state" role="status" aria-busy="true"><strong>正在运行收盘监控</strong><p>保留上一次告警记录，等待新的行情快照和规则评估完成。</p></aside>`;
  }
  if (state.monitoringScanError) {
    return `<aside class="callout danger monitoring-state" role="alert"><strong>本次监控扫描失败</strong><p>${escapeHtml(state.monitoringScanError)}</p><p>已有告警和上一次扫描证据保持不变；修复数据后可以再次运行。</p></aside>`;
  }
  if (scan.status === "failed" || scan.error) {
    return `<aside class="callout danger monitoring-state" role="alert"><strong>最近监控扫描失败</strong><p>${escapeHtml(scan.error?.message || scan.error || "扫描没有生成新的告警证据。")}</p><p>已有告警保持不变；修复数据后可重新运行。</p></aside>`;
  }
  if (snapshot.available === false) {
    return `<aside class="callout warning monitoring-state" role="status"><strong>行情快照不可用</strong><p>${escapeHtml(snapshot.error?.message || "当前没有可校验的收盘快照；监控不会用零值代替缺失指标。")}</p><p>先刷新已完成行情，再重新运行收盘监控。</p></aside>`;
  }
  if (code === "no_watchlists") {
    return `<aside class="callout info monitoring-state" role="status"><strong>尚未建立监控列表</strong><p>先建立一个列表并加入证券，系统才会有可扫描的范围。监控只生成研究证据，不会改变策略或生成订单。</p></aside>`;
  }
  if (code === "no_rules" || scan.status === "no_rules") {
    return `<aside class="callout warning monitoring-state" role="status"><strong>已有标的但尚未建立规则</strong><p>为列表中的证券创建至少一条启用规则，再运行收盘监控。当前不会触发告警。</p></aside>`;
  }
  if (code === "not_scanned" || scan.status === "not_run") {
    return `<aside class="callout info monitoring-state" role="status"><strong>尚未运行扫描</strong><p>当前列表和规则已经保存，但没有可复核的扫描结果。运行一次收盘监控后，系统会记录数据日期、来源和规则指纹。</p></aside>`;
  }
  if (code === "partial" || scan.status === "partial") {
    return `<aside class="callout warning monitoring-state" role="status"><strong>扫描部分完成</strong><p>部分证券或规则因数据不可用被排除；请先查看扫描边界，再判断剩余告警。</p></aside>`;
  }
  if (Array.isArray(data?.alerts) && data.alerts.length && !filteredAlerts.length) {
    return `<aside class="callout info monitoring-state" role="status"><strong>当前筛选没有匹配告警</strong><p>清除状态、严重级别或证券条件后查看其他记录。扫描证据仍保留在本机。</p><div class="action-row"><button class="button secondary" type="button" data-monitoring-filter-clear>清除筛选</button></div></aside>`;
  }
  if (code === "no_alerts") {
    return `<aside class="callout info monitoring-state" role="status"><strong>已扫描，当前没有触发告警</strong><p>这是一次成功的零触发结果，不等同于“尚未扫描”。可在下方核对规则和数据截止日期。</p></aside>`;
  }
  return "";
}

function monitoringScanStatusMarkup(data) {
  const scan = data?.scan || {};
  const status = String(scan.status || "not_run").toLowerCase();
  const exclusions = Array.isArray(scan.exclusions) ? scan.exclusions : [];
  if (status === "partial" && exclusions.length) {
    return `<aside class="callout warning monitoring-boundary" role="status"><strong>扫描边界：${formatInteger(exclusions.length)} 条被排除</strong><ul>${exclusions.slice(0, 8).map((item) => `<li><code>${escapeHtml(item.symbol || "—")}</code>：${escapeHtml(item.message || item.code || "数据不可用")}</li>`).join("")}</ul>${exclusions.length > 8 ? `<p>其余 ${formatInteger(exclusions.length - 8)} 条请查看扫描记录。</p>` : ""}</aside>`;
  }
  if (status === "succeeded" && Array.isArray(scan.suppressed) && scan.suppressed.length) {
    return `<aside class="callout info monitoring-boundary" role="status"><strong>重复条件已抑制</strong><p>${formatInteger(scan.suppressed.length)} 条条件仍然成立，但受规则冷却或连续触发去重约束，没有重复生成告警。</p></aside>`;
  }
  return "";
}

function monitoringAlertActionButtons(alert) {
  const id = escapeHtml(alert.alert_id);
  const symbol = escapeHtml(alert.symbol || "该证券");
  const status = String(alert.status || "open");
  const buttons = [];
  const direct = (action, label, style = "secondary") => `<button class="button ${style} compact" type="button" data-monitoring-alert-action="${action}" data-monitoring-alert-id="${id}" aria-label="${label}${symbol}"${monitoringActionBusy(`${alert.alert_id}:${action}`) ? ' disabled aria-busy="true"' : ""}>${monitoringActionBusy(`${alert.alert_id}:${action}`) ? "处理中" : label}</button>`;
  const compose = (action, label, style = "secondary") => `<button class="button ${style} compact" type="button" data-monitoring-alert-compose="${action}" data-monitoring-alert-id="${id}" aria-label="${label}${symbol}"${monitoringActionBusy(`${alert.alert_id}:${action}`) ? ' disabled aria-busy="true"' : ""}>${label}</button>`;
  if (status === "open") {
    buttons.push(direct("acknowledge", "标记已阅 · "));
    buttons.push(compose("snooze", "暂缓处理 · "));
    buttons.push(compose("dismiss", "关闭并备注 · ", "danger"));
  } else if (status === "snoozed") {
    buttons.push(direct("unsnooze", "取消暂缓 · "));
    buttons.push(direct("acknowledge", "标记已阅 · "));
    buttons.push(compose("dismiss", "关闭并备注 · ", "danger"));
  } else {
    buttons.push(direct("reopen", "重新打开 · "));
  }
  return `<div class="action-row monitoring-alert-actions">${buttons.join("")}</div>`;
}

function monitoringActionComposer(data) {
  const target = state.monitoringActionTarget;
  if (!target) return "";
  const alerts = Array.isArray(data?.alerts) ? data.alerts : [];
  const alert = alerts.find((item) => item.alert_id === target.alertId);
  if (!alert) return "";
  const snooze = target.action === "snooze";
  const today = new Date().toLocaleDateString("en-CA");
  const defaultUntil = alert.snooze_until || today;
  return `<section class="panel monitoring-action-composer" aria-labelledby="monitoring-action-heading">
    ${panelHeader("处理告警", `${escapeHtml(alert.symbol || "—")} · ${escapeHtml(alert.rule_label || alert.rule_type || "规则")}`)}
    <form id="monitoring-alert-action-form" class="monitoring-action-form" aria-describedby="monitoring-action-help">
      <input type="hidden" name="alert_id" value="${escapeHtml(alert.alert_id)}">
      <input type="hidden" name="action" value="${escapeHtml(target.action)}">
      <p id="monitoring-action-help" class="section-note">${snooze ? "暂缓只改变告警处理状态，不改变规则；扫描到达所选日期后会自动重新打开。" : "关闭会保留不可变告警记录，并追加一条带备注的处理事件。"}</p>
      ${snooze ? `<div class="field"><label for="monitoring-snooze-until">重新检查日期</label><input id="monitoring-snooze-until" name="snooze_until" type="date" min="${escapeHtml(today)}" value="${escapeHtml(defaultUntil)}" required></div>` : ""}
      <div class="field"><label for="monitoring-action-note">处理备注${snooze ? "（可选）" : ""}</label><textarea id="monitoring-action-note" name="note" rows="3" maxlength="1000"${snooze ? "" : " required"} placeholder="记录人工判断依据"></textarea></div>
      <div class="action-row"><button class="button ${snooze ? "secondary" : "danger"}" type="submit"${monitoringActionBusy(`${alert.alert_id}:${target.action}`) ? ' disabled aria-busy="true"' : ""}>${monitoringActionBusy(`${alert.alert_id}:${target.action}`) ? "正在保存" : MONITORING_ACTION_LABELS[target.action]}</button><button class="button ghost" type="button" data-monitoring-alert-compose-cancel>取消</button></div>
      ${monitoringActionStatusMarkup("monitoring-composer-status")}
    </form>
  </section>`;
}

function renderMonitoring(data) {
  const watchlists = Array.isArray(data?.watchlists) ? data.watchlists : [];
  const rules = Array.isArray(data?.rules) ? data.rules : [];
  const alerts = Array.isArray(data?.alerts) ? data.alerts : [];
  const instruments = Array.isArray(data?.instruments) ? data.instruments : [];
  const summary = data?.summary || {};
  const snapshot = data?.snapshot || {};
  const scan = data?.scan || {};
  const metadata = monitoringRuleMetadata(data);
  const watchlistMap = Object.fromEntries(watchlists.map((item) => [item.watchlist_id, item]));
  const filteredAlerts = monitoringFilteredAlerts(data);
  const unresolved = finite(summary.unresolved_count) || 0;
  const critical = finite(summary.severity_counts?.critical) || 0;
  const scanStatus = String(scan.status || "not_run").toLowerCase();
  const snapshotDate = snapshot.data_date || scan.data_date || "不可用";
  const scanLabel = monitoringStatusLabel(scanStatus);
  const scanKind = monitoringStatusKind(scanStatus);
  const authority = data?.authority || {};
  const actions = `<div class="action-row"><button class="button primary" type="button" data-monitoring-scan${state.monitoringBusy ? ' disabled aria-busy="true"' : state.monitoringRefreshBusy ? " disabled" : ""}>${state.monitoringBusy ? "扫描中" : "运行收盘监控"}</button><button class="button secondary" type="button" data-monitoring-refresh${state.monitoringRefreshBusy ? ' disabled aria-busy="true"' : state.monitoringBusy ? " disabled" : ""}>${state.monitoringRefreshBusy ? "刷新中" : "刷新监控"}</button></div>`;
  const watchlistOptions = [`<option value="">全部列表</option>`, ...watchlists.map((item) => `<option value="${escapeHtml(item.watchlist_id)}"${state.monitoringFilters.watchlist_id === item.watchlist_id ? " selected" : ""}>${escapeHtml(item.name)}</option>`)].join("");
  const filter = `<form id="monitoring-filter-form" class="filter-form monitoring-filter-form" aria-describedby="monitoring-filter-help">
    <div class="field"><label for="monitoring-filter-watchlist">监控列表</label><select id="monitoring-filter-watchlist" name="watchlist_id">${watchlistOptions}</select></div>
    <div class="field"><label for="monitoring-filter-symbol">证券</label><input id="monitoring-filter-symbol" name="symbol" value="${escapeHtml(state.monitoringFilters.symbol || "")}" list="monitoring-symbol-options" placeholder="代码或名称"></div>
    <div class="field"><label for="monitoring-filter-severity">严重级别</label><select id="monitoring-filter-severity" name="severity"><option value="">全部级别</option>${Object.entries(MONITORING_SEVERITY_LABELS).map(([value, label]) => `<option value="${value}"${state.monitoringFilters.severity === value ? " selected" : ""}>${label}</option>`).join("")}</select></div>
    <div class="field"><label for="monitoring-filter-status">处理状态</label><select id="monitoring-filter-status" name="status"><option value="unresolved"${state.monitoringFilters.status === "unresolved" ? " selected" : ""}>待处理</option><option value="all"${state.monitoringFilters.status === "all" ? " selected" : ""}>全部</option>${Object.entries(MONITORING_STATUS_LABELS).filter(([value]) => ["open", "acknowledged", "snoozed", "dismissed"].includes(value)).map(([value, label]) => `<option value="${value}"${state.monitoringFilters.status === value ? " selected" : ""}>${label}</option>`).join("")}</select></div>
    <div class="field"><label for="monitoring-filter-limit">最多显示</label><input id="monitoring-filter-limit" name="limit" type="number" min="1" max="200" step="1" inputmode="numeric" value="${escapeHtml(state.monitoringFilters.limit || "100")}"></div>
    <div class="filter-actions"><button class="button secondary" type="submit">应用筛选</button><button class="button ghost" type="button" data-monitoring-filter-clear>清除</button></div>
  </form><p id="monitoring-filter-help" class="section-note">筛选只改变当前显示，不改变已经固化的扫描和告警记录。</p>`;
  const statusRegion = state.monitoringActionTarget ? "" : monitoringActionStatusMarkup();
  const ruleTargetOptions = watchlists.flatMap((list) => list.symbols.map((symbol) => `<option value="${escapeHtml(monitoringRuleTargetValue(list.watchlist_id, symbol))}">${escapeHtml(list.name)} · ${escapeHtml(symbol)} · ${escapeHtml(instrumentName(symbol))}</option>`)).join("");
  const ruleTypeOptions = (Array.isArray(data?.rule_types) ? data.rule_types : []).map((item) => `<option value="${escapeHtml(item.rule_type)}">${escapeHtml(item.label || item.rule_type)}</option>`).join("");
  const symbolOptions = instruments.map((item) => `<option value="${escapeHtml(item.symbol)}">${escapeHtml(item.name || instrumentName(item.symbol))}</option>`).join("");
  const symbolOptionsForWatchlist = (watchlist) => {
    const existing = new Set(Array.isArray(watchlist.symbols) ? watchlist.symbols : []);
    const available = instruments.filter((item) => !existing.has(item.symbol));
    const options = available.map((item) => `<option value="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)} · ${escapeHtml(item.name || instrumentName(item.symbol))}</option>`).join("");
    const id = `monitoring-symbol-${watchlist.watchlist_id}`;
    return `<form class="monitoring-symbol-form" data-monitoring-symbol-form data-watchlist-id="${escapeHtml(watchlist.watchlist_id)}" aria-label="向${escapeHtml(watchlist.name)}加入证券">
      <label class="sr-only" for="${escapeHtml(id)}">选择要加入的证券</label>
      <select id="${escapeHtml(id)}" name="symbol"${available.length ? " required" : " disabled"}><option value="">${available.length ? "加入证券" : "证券已全部加入"}</option>${options}</select>
      <button class="button secondary compact" type="submit"${available.length ? "" : " disabled"}>加入</button>
    </form>`;
  };

  const alertRows = filteredAlerts.length ? filteredAlerts.map((alert) => {
    const rule = rules.find((item) => item.rule_id === alert.rule_id) || alert;
    const list = watchlistMap[alert.watchlist_id];
    const evidence = monitoringRuleSummary({ ...rule, rule_label: alert.rule_label, operator_label: alert.operator_label, threshold: alert.threshold }, metadata);
    const source = screenSourceLabel(alert.source);
    return `<tr>
      <td><div class="monitoring-alert-state">${statusChip(monitoringStatusLabel(alert.status), monitoringStatusKind(alert.status))}${statusChip(monitoringSeverityLabel(alert.severity), monitoringSeverityKind(alert.severity))}</div></td>
      <td class="symbol-cell"><strong>${escapeHtml(alert.symbol || "—")}</strong><span>${escapeHtml(instrumentName(alert.symbol))}</span><span class="table-subtext">${escapeHtml(list?.name || "未分组")}</span></td>
      <td class="monitoring-alert-action-cell">${monitoringAlertActionButtons(alert)}</td>
      <td><strong>${escapeHtml(evidence)}</strong><span class="table-subtext">观测 ${escapeHtml(alert.observed_text || monitoringUnitValue(alert.observed_value, metadata[alert.rule_type]?.unit))}</span></td>
      <td><span class="mono">${escapeHtml(alert.data_date || "—")}</span><span class="table-subtext">${escapeHtml(source)}${alert.completed_session_cutoff ? ` · 截止 ${escapeHtml(alert.completed_session_cutoff)}` : ""}</span></td>
      <td><span class="mono">${escapeHtml(formatDate(alert.triggered_at, true))}</span>${alert.snooze_until ? `<span class="table-subtext">暂缓至 ${escapeHtml(alert.snooze_until)}</span>` : ""}</td>
      <td><code class="truncate-hash" title="${escapeHtml(alert.evidence_fingerprint || "—")}">${escapeHtml(alert.evidence_fingerprint || "—")}</code></td>
    </tr>`;
  }).join("") : emptyRow(7, alerts.length ? "当前筛选没有匹配记录" : "尚无触发告警");

  const watchlistRows = watchlists.length ? watchlists.map((item) => {
    const busy = monitoringActionBusy(`watchlist:${item.watchlist_id}`);
    const symbols = Array.isArray(item.symbols) ? item.symbols : [];
    const symbolMarkup = symbols.length
      ? `<ul class="monitoring-symbol-list" aria-label="${escapeHtml(item.name)}中的证券">${symbols.map((symbol) => `<li><code>${escapeHtml(symbol)}</code><button class="button ghost compact" type="button" data-monitoring-watchlist-action="remove_symbol" data-monitoring-watchlist-id="${escapeHtml(item.watchlist_id)}" data-monitoring-symbol="${escapeHtml(symbol)}" aria-label="从${escapeHtml(item.name)}移出${escapeHtml(symbol)}"${busy ? " disabled" : ""}>移出</button></li>`).join("")}</ul>`
      : `<span class="value-unavailable">尚无证券</span>`;
    return `<tr><td><strong>${escapeHtml(item.name)}</strong><span class="table-subtext mono">${escapeHtml(item.watchlist_id)}</span></td><td>${statusChip(item.enabled ? "已启用" : "已停用", item.enabled ? "success" : "neutral")}</td><td class="numeric">${formatInteger(symbols.length)}</td><td><div class="monitoring-symbols-cell">${symbolMarkup}${symbolOptionsForWatchlist(item)}</div></td><td><div class="action-row"><button class="button secondary compact" type="button" data-monitoring-watchlist-action="set_enabled" data-monitoring-watchlist-id="${escapeHtml(item.watchlist_id)}" data-monitoring-enabled="${item.enabled ? "false" : "true"}"${busy ? " disabled" : ""}>${item.enabled ? "停用" : "启用"}</button><button class="button secondary compact" type="button" data-monitoring-watchlist-action="rename" data-monitoring-watchlist-id="${escapeHtml(item.watchlist_id)}" data-monitoring-watchlist-name="${escapeHtml(item.name)}"${busy ? " disabled" : ""}>重命名</button><button class="button danger compact" type="button" data-monitoring-watchlist-action="delete" data-monitoring-watchlist-id="${escapeHtml(item.watchlist_id)}"${busy ? " disabled" : ""}>删除</button></div></td></tr>`;
  }).join("") : emptyRow(5, "尚未建立监控列表");

  const ruleRows = rules.length ? rules.map((rule) => {
    const list = watchlistMap[rule.watchlist_id];
    const meta = metadata[rule.rule_type] || {};
    const busy = monitoringActionBusy(`rule:${rule.rule_id}`);
    return `<tr><td>${statusChip(rule.enabled ? "启用" : "停用", rule.enabled ? "success" : "neutral")}<span class="table-subtext">${escapeHtml(monitoringSeverityLabel(rule.severity))}</span></td><td class="symbol-cell"><strong>${escapeHtml(rule.symbol)}</strong><span>${escapeHtml(instrumentName(rule.symbol))}</span><span class="table-subtext">${escapeHtml(list?.name || "未分组")}</span></td><td>${escapeHtml(meta.label || rule.rule_type)}<span class="table-subtext">${escapeHtml(meta.formula || "服务器固定口径")}</span></td><td class="numeric">${escapeHtml(rule.threshold === null || rule.threshold === undefined ? "—" : monitoringUnitValue(rule.threshold, meta.unit))}</td><td class="numeric">${formatInteger(rule.cooldown_sessions)} 日</td><td><div class="action-row"><button class="button secondary compact" type="button" data-monitoring-rule-action="update" data-monitoring-rule-id="${escapeHtml(rule.rule_id)}" data-monitoring-rule-enabled="${rule.enabled ? "false" : "true"}"${busy ? " disabled" : ""}>${rule.enabled ? "停用" : "启用"}</button><button class="button danger compact" type="button" data-monitoring-rule-action="delete" data-monitoring-rule-id="${escapeHtml(rule.rule_id)}"${busy ? " disabled" : ""}>删除</button></div></td></tr>`;
  }).join("") : emptyRow(6, "尚未建立规则");

  return `<div class="page-stack monitoring-page">
    ${pageIntro("收盘监控", "持久化自选列表、确定性规则和可追溯告警；只用于研究复核，不生成订单", actions)}
    ${contextBand([
      { label: "行情快照", value: snapshotDate, status: snapshot.available === false ? "不可用" : snapshot.data_date ? "已加载" : "待确认", kind: snapshot.available === false ? "danger" : snapshot.data_date ? "success" : "neutral", note: `来源 ${marketProviderLabel(snapshot.source || snapshot.providers?.[0]?.provider || scan.source_summary?.providers?.[0]?.provider || "未说明")} · 截止 ${snapshot.completed_session_cutoff || scan.completed_session_cutoff || "—"}` },
      { label: "监控列表", value: `${formatInteger(summary.watchlist_count || 0)} 个`, status: `${formatInteger(summary.symbol_count || 0)} 支证券`, kind: summary.watchlist_count ? "info" : "neutral", note: `${formatInteger(summary.enabled_rule_count || 0)} / ${formatInteger(summary.rule_count || 0)} 条规则启用` },
      { label: "最近扫描", value: scanLabel, status: scan.data_date || scan.status === "succeeded" ? "有记录" : "无记录", kind: scanKind, note: scan.finished_at ? `完成于 ${formatDate(scan.finished_at, true)}` : "尚未生成扫描记录" },
      { label: "待处理告警", value: `${formatInteger(unresolved)} 条`, status: critical ? `${formatInteger(critical)} 条严重` : unresolved ? "需要复核" : "当前为空", kind: critical ? "danger" : unresolved ? "warning" : "success", note: `滞后告警 ${formatInteger(summary.stale_count || 0)} 条` },
      { label: "权限边界", value: authority.research_only === false ? "状态待确认" : "仅研究", status: authority.execution_authorized ? "需复核" : "不会下单", kind: authority.execution_authorized ? "warning" : "info", note: "不会修改策略、账本或券商权限" },
    ], "监控日期、范围与权限")}
    <section class="metric-strip monitoring-metric-strip" aria-label="监控摘要">
      ${metric("监控证券", formatInteger(summary.symbol_count || 0), `${formatInteger(summary.watchlist_count || 0)} 个列表`)}
      ${metric("启用规则", `${formatInteger(summary.enabled_rule_count || 0)} / ${formatInteger(summary.rule_count || 0)}`, "规则口径由服务器固定", summary.enabled_rule_count ? "tone-info" : "tone-warning")}
      ${metric("待处理", `${formatInteger(unresolved)} 条`, critical ? `${formatInteger(critical)} 条严重` : "无严重告警", critical ? "tone-negative" : unresolved ? "tone-warning" : "tone-positive")}
      ${metric("扫描证据", scan.data_date || "—", scanStatus === "failed" ? "本次失败，旧证据保留" : scanStatus === "partial" ? "部分完成" : scanStatus === "succeeded" ? "完整记录" : "尚未记录", scanStatus === "failed" ? "tone-negative" : scanStatus === "partial" ? "tone-warning" : "tone-info")}
    </section>
    ${monitoringEmptyStateMarkup(data, filteredAlerts)}
    ${monitoringScanStatusMarkup(data)}
    ${monitoringActionComposer(data)}
    <section class="panel monitoring-alert-panel" aria-labelledby="monitoring-alerts-title">
      ${panelHeader("告警队列", `${formatInteger(filteredAlerts.length)} / ${formatInteger(alerts.length)} 条显示 · 记录保留在当前账户`, statusChip(alerts.length ? "证据已加载" : "暂无告警", alerts.length ? "info" : "neutral"))}
      ${filter}
      ${statusRegion}
      <div id="monitoring-alerts-region" class="table-wrap monitoring-alert-table"${state.monitoringBusy ? ' aria-busy="true"' : ""}><table class="data-table"><thead><tr><th>状态 / 级别</th><th>证券 / 列表</th><th>处理</th><th>触发规则与观测</th><th>数据日期 / 来源</th><th>触发时间</th><th>证据指纹</th></tr></thead><tbody>${alertRows}</tbody></table></div>
    </section>
    <section class="monitoring-config-layout" aria-label="监控配置">
      <article class="panel monitoring-config-panel">
        ${panelHeader("监控列表", "列表修改会生成新的配置修订；旧修订不覆盖")}
        <form id="monitoring-watchlist-form" class="monitoring-create-form"><div class="field"><label for="monitoring-watchlist-name">新列表名称</label><input id="monitoring-watchlist-name" name="name" maxlength="80" required placeholder="例如 收盘核心 ETF"></div><button class="button primary" type="submit">建立列表</button></form>
        <div class="table-wrap monitoring-watchlist-table"><table class="data-table compact"><thead><tr><th>列表</th><th>状态</th><th class="numeric">证券数</th><th>证券</th><th>操作</th></tr></thead><tbody>${watchlistRows}</tbody></table></div>
      </article>
      <article class="panel monitoring-config-panel">
        ${panelHeader("规则配置", "只允许服务器已登记的规则类型；调整后重新运行扫描")}
        <form id="monitoring-rule-form" class="monitoring-rule-form" aria-describedby="monitoring-rule-help">
          <div class="field"><label for="monitoring-rule-target">列表 / 证券</label><select id="monitoring-rule-target" name="target" required${ruleTargetOptions ? "" : " disabled"}><option value="">${ruleTargetOptions ? "选择列表和证券" : "先加入证券"}</option>${ruleTargetOptions}</select></div>
          <div class="field"><label for="monitoring-rule-type">规则类型</label><select id="monitoring-rule-type" name="rule_type" required${ruleTypeOptions ? "" : " disabled"}>${ruleTypeOptions || "<option value=\"\">尚无规则类型</option>"}</select></div>
          <div class="field"><label for="monitoring-rule-threshold">阈值</label><input id="monitoring-rule-threshold" name="threshold" type="number" step="any" inputmode="decimal" placeholder="按规则需要"></div>
          <div class="field"><label for="monitoring-rule-window">窗口</label><input id="monitoring-rule-window" name="window" type="number" min="2" max="1000" step="1" inputmode="numeric" placeholder="使用默认"></div>
          <div class="field"><label for="monitoring-rule-comparison">比较窗口</label><input id="monitoring-rule-comparison" name="comparison_window" type="number" min="2" max="1000" step="1" inputmode="numeric" placeholder="仅 EMA 交叉"></div>
          <div class="field"><label for="monitoring-rule-cooldown">冷却交易日</label><input id="monitoring-rule-cooldown" name="cooldown_sessions" type="number" min="0" max="250" step="1" inputmode="numeric" value="1" required></div>
          <div class="field"><label for="monitoring-rule-severity">严重级别</label><select id="monitoring-rule-severity" name="severity"><option value="info">提示</option><option value="warning" selected>警告</option><option value="critical">严重</option></select></div>
          <div class="action-row"><button class="button primary" type="submit"${ruleTargetOptions && ruleTypeOptions ? "" : " disabled"}>建立规则</button></div>
          <p id="monitoring-rule-help" class="section-note">选择规则后，服务器会校验阈值、窗口和证券是否属于列表。</p>
        </form>
        <div class="table-wrap monitoring-rule-table"><table class="data-table compact"><thead><tr><th>状态 / 级别</th><th>证券 / 列表</th><th>规则口径</th><th class="numeric">阈值</th><th class="numeric">冷却</th><th>操作</th></tr></thead><tbody>${ruleRows}</tbody></table></div>
      </article>
    </section>
    <section class="panel monitoring-evidence-panel">
      ${panelHeader("扫描证据", "最近扫描记录持久化的配置与行情身份")}
      <div class="path-list"><div class="path-row"><span>扫描 ID</span><code>${escapeHtml(scan.scan_id || "—")}</code></div><div class="path-row"><span>配置修订</span><code>${escapeHtml(String(scan.config_revision ?? data.configuration?.revision ?? "—"))} · ${escapeHtml(scan.config_fingerprint || data.configuration?.fingerprint || "—")}</code></div><div class="path-row"><span>快照 ID</span><code>${escapeHtml(scan.snapshot_id || snapshot.snapshot_id || "—")}</code></div><div class="path-row"><span>行情指纹</span><code>${escapeHtml(scan.snapshot_evidence_fingerprint || snapshot.evidence_fingerprint || "—")}</code></div><div class="path-row"><span>清单 SHA-256</span><code>${escapeHtml(scan.manifest_sha256 || snapshot.manifest_sha256 || "—")}</code></div><div class="path-row"><span>来源分布</span><code>${escapeHtml((scan.source_summary?.providers || snapshot.providers || []).map((item) => `${item.provider || "unknown"} ${item.rule_count ?? item.instrument_count ?? 0}`).join(" · ") || "—")}</code></div></div>
      <p class="section-note">扫描结果仅作为研究证据。告警处理不会改变策略参数、模拟账本、风险门禁或券商权限。</p>
    </section>
    <datalist id="monitoring-symbol-options">${symbolOptions}</datalist>
  </div>`;
}

function monitoringRenderCurrent() {
  const payload = state.data.get("monitoring");
  if (state.route === "monitoring" && payload) renderRoute(payload);
}

function monitoringSetStatus(message, kind = "") {
  state.monitoringActionStatus = message
    ? { message: String(message), kind }
    : "";
}

function monitoringNumber(value) {
  if (value === null || value === undefined || String(value).trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function monitoringInteger(value) {
  const parsed = monitoringNumber(value);
  return parsed !== null && Number.isInteger(parsed) ? parsed : null;
}

function monitoringPayload(result) {
  return result && typeof result === "object" && Array.isArray(result.watchlists)
    ? result
    : null;
}

async function reloadMonitoring(silent = false) {
  if (state.monitoringRefreshBusy && !silent) return null;
  if (!silent) {
    state.monitoringRefreshBusy = true;
    monitoringSetStatus("正在刷新监控快照…", "info");
    monitoringRenderCurrent();
  }
  try {
    const payload = await api(monitoringPath());
    state.data.set("monitoring", payload);
    state.monitoringScanError = "";
    if (!silent) monitoringSetStatus("监控快照已刷新", "success");
    monitoringRenderCurrent();
    return payload;
  } catch (error) {
    if (!silent) {
      const message = friendlyError(error.message);
      monitoringSetStatus(message, "error");
      notify(message, true);
      monitoringRenderCurrent();
    }
    throw error;
  } finally {
    if (!silent) {
      state.monitoringRefreshBusy = false;
      monitoringRenderCurrent();
    }
  }
}

async function runMonitoringMutation(path, payload, successMessage, key, button = null, onSuccess = null) {
  const busyKey = String(key || path);
  if (monitoringActionBusy(busyKey)) return false;
  state.monitoringActionBusy.add(busyKey);
  monitoringSetStatus("正在保存监控配置…", "info");
  monitoringRenderCurrent();
  try {
    const result = await api(path, {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify(payload),
    });
    const next = monitoringPayload(result);
    if (!next) throw new Error("监控服务返回了无法校验的快照");
    state.data.set("monitoring", next);
    state.monitoringScanError = "";
    if (typeof onSuccess === "function") onSuccess(next);
    monitoringSetStatus(successMessage, "success");
    notify(successMessage);
    return true;
  } catch (error) {
    let message = friendlyError(error.message);
    if (error.status === 409) {
      try {
        await reloadMonitoring(true);
        message = `${message}；已刷新当前监控配置`;
      } catch {
        message = `${message}；请刷新监控后重试`;
      }
    }
    monitoringSetStatus(message, "error");
    notify(message, true);
    return false;
  } finally {
    state.monitoringActionBusy.delete(busyKey);
    monitoringRenderCurrent();
    if (button?.isConnected) {
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
    }
  }
}

async function runMonitoringScan(button = null) {
  if (state.monitoringBusy) return false;
  state.monitoringBusy = true;
  state.monitoringScanError = "";
  monitoringSetStatus("正在运行收盘监控…", "info");
  monitoringRenderCurrent();
  try {
    const result = await api("/api/monitoring/scan", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify({}),
    });
    const next = monitoringPayload(result);
    if (!next) throw new Error("监控服务返回了无法校验的扫描快照");
    const scanResult = result.scan_result;
    if (scanResult && scanResult.status === "no_rules" && next.scan?.status === "not_run") {
      next.scan = scanResult;
    }
    state.data.set("monitoring", next);
    state.monitoringScanError = "";
    const scan = next.scan_result || next.scan || {};
    const resultLabel = monitoringStatusLabel(scan.status || "succeeded");
    monitoringSetStatus(`收盘监控${resultLabel}`, scan.status === "failed" ? "error" : "success");
    notify(`收盘监控${resultLabel}`, scan.status === "failed");
    return true;
  } catch (error) {
    const message = friendlyError(error.message);
    state.monitoringScanError = message;
    monitoringSetStatus(message, "error");
    notify(message, true);
    return false;
  } finally {
    state.monitoringBusy = false;
    monitoringRenderCurrent();
    if (button?.isConnected) {
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
    }
  }
}

async function createMonitoringWatchlist(form) {
  const name = String(new FormData(form).get("name") || "").trim();
  if (!name) {
    form.reportValidity();
    return;
  }
  const data = state.data.get("monitoring") || {};
  const button = form.querySelector("button[type='submit']");
  await runMonitoringMutation(
    "/api/monitoring/watchlist",
    { action: "create", name, expected_revision: monitoringExpectedRevision(data) },
    "监控列表已建立",
    "watchlist:create",
    button,
    () => form.reset(),
  );
}

async function mutateMonitoringWatchlist(action, watchlistId, extras = {}, button = null) {
  const data = state.data.get("monitoring") || {};
  const payload = {
    action,
    watchlist_id: watchlistId,
    expected_revision: monitoringExpectedRevision(data),
    ...extras,
  };
  return runMonitoringMutation(
    "/api/monitoring/watchlist",
    payload,
    action === "add_symbol" ? "证券已加入监控列表" : action === "remove_symbol" ? "证券已移出监控列表" : action === "rename" ? "监控列表已重命名" : action === "delete" ? "监控列表已删除" : "监控列表状态已更新",
    `watchlist:${watchlistId}`,
    button,
  );
}

async function createMonitoringRule(form) {
  const values = new FormData(form);
  const target = parseMonitoringRuleTarget(values.get("target"));
  const ruleType = String(values.get("rule_type") || "");
  const data = state.data.get("monitoring") || {};
  const metadata = monitoringRuleMetadata(data)[ruleType] || {};
  const threshold = monitoringNumber(values.get("threshold"));
  const window = monitoringInteger(values.get("window"));
  const comparisonWindow = monitoringInteger(values.get("comparison_window"));
  if (!target.watchlist_id || !target.symbol || !ruleType || (metadata.threshold_required && threshold === null)) {
    form.reportValidity();
    return;
  }
  const payload = {
    action: "create",
    expected_revision: monitoringExpectedRevision(data),
    watchlist_id: target.watchlist_id,
    symbol: target.symbol,
    rule_type: ruleType,
    threshold,
    cooldown_sessions: monitoringInteger(values.get("cooldown_sessions")) ?? 1,
    severity: String(values.get("severity") || "warning"),
  };
  if (window !== null) payload.window = window;
  if (comparisonWindow !== null) payload.comparison_window = comparisonWindow;
  const button = form.querySelector("button[type='submit']");
  await runMonitoringMutation(
    "/api/monitoring/rules",
    payload,
    "监控规则已建立",
    "rule:create",
    button,
    () => form.reset(),
  );
}

async function mutateMonitoringRule(action, ruleId, enabled, button = null) {
  const data = state.data.get("monitoring") || {};
  const payload = {
    action,
    rule_id: ruleId,
    expected_revision: monitoringExpectedRevision(data),
  };
  if (action === "update") payload.enabled = Boolean(enabled);
  return runMonitoringMutation(
    "/api/monitoring/rules",
    payload,
    action === "delete" ? "监控规则已删除" : "监控规则状态已更新",
    `rule:${ruleId}`,
    button,
  );
}

async function runMonitoringAlertAction(alertId, action, extras = {}, button = null) {
  const current = (state.data.get("monitoring")?.alerts || []).find((item) => item.alert_id === alertId);
  const expected = current?.state_fingerprint;
  return runMonitoringMutation(
    `/api/monitoring/alerts/${encodeURIComponent(alertId)}/actions`,
    { action, ...extras, ...(expected ? { expected_state_fingerprint: expected } : {}) },
    `告警${MONITORING_ACTION_LABELS[action] || "状态已更新"}`,
    `${alertId}:${action}`,
    button,
    () => { state.monitoringActionTarget = null; },
  );
}

function renderStorage(data) {
  const preferences = data.preferences || {};
  const usage = data.usage || {};
  const operational = Boolean(data.operational);
  const storageMode = data.effective_storage_mode || "local";
  const snapshots = Array.isArray(usage.snapshots) ? usage.snapshots : [];
  const actions = [
    `<button class="button secondary" type="button" data-storage-refresh${operational ? "" : " disabled"}>清点云端</button>`,
    operational ? actionButton("cloud-backup", "primary") : "",
  ].join("");
  const missing = Array.isArray(data.missing_configuration) ? data.missing_configuration : [];
  return `
    <div class="page-stack">
      ${pageIntro("数据存储", "活动行情保留在本地，R2 保存可校验的独立快照", actions)}

      <section class="metric-strip" aria-label="存储状态">
        ${metric("当前策略", storageMode === "hybrid" ? "本地 + R2" : "仅本地", `本地缓存 ${formatStorageBytes(data.local?.bytes || 0)}`)}
        ${metric("云端容量剩余", usage.scanned_at ? formatStorageBytes(usage.storage_remaining_bytes) : "待清点", usage.scanned_at ? `已用 ${formatStorageBytes(usage.storage_bytes)} / ${formatStorageBytes(usage.storage_limit_bytes)}` : "点击清点云端读取当前安装空间")}
        ${metric("A 类操作剩余", formatInteger(usage.class_a_remaining), `预算周期已记录 ${formatInteger(usage.class_a)} / ${formatInteger(usage.class_a_limit)}`)}
        ${metric("B 类操作剩余", formatInteger(usage.class_b_remaining), `预算周期已记录 ${formatInteger(usage.class_b)} / ${formatInteger(usage.class_b_limit)}`)}
      </section>

      ${!data.credentials_configured ? `<aside class="callout warning"><strong>尚未配置 Cloudflare R2</strong><p>先安装云端支持组件，再运行 <code>powershell -ExecutionPolicy Bypass -File .\\scripts\\configure_cloud.ps1</code>，然后重启工作台。${missing.length ? ` 缺少：${escapeHtml(missing.join("、"))}。` : ""}</p></aside>` : ""}
      ${data.credentials_configured && !data.enabled ? `<aside class="callout warning"><strong>R2 凭据已保存，但云备份未启用</strong><p>重新运行云配置脚本以启用当前 Windows 用户的配置，然后重启工作台。</p></aside>` : ""}
      ${data.credentials_configured && !data.dependency_available ? `<aside class="callout warning"><strong>尚未安装云端支持组件</strong><p>在仓库根目录运行 <code>.\\.venv\\Scripts\\python.exe -m pip install -e '.[cloud]'</code>，然后重启工作台。</p></aside>` : ""}
      ${data.configuration_error ? `<aside class="callout danger"><strong>云配置无法读取</strong><p>${escapeHtml(data.configuration_error)}</p></aside>` : ""}
      ${data.inventory_error ? `<aside class="callout danger"><strong>云端清点未完成</strong><p>${escapeHtml(data.inventory_error)}</p></aside>` : ""}
      <div id="cloud-backup-warning-region">${cloudBackupWarningMarkup(state.cloudBackupWarning)}</div>

      <section class="split-layout storage-layout">
        <article class="panel">
          ${panelHeader("用量与预算", `${usage.scanned_at ? `容量清点于 ${formatDate(usage.scanned_at, true)}，共 ${formatInteger(usage.object_count)} 个对象` : "容量尚未清点"}；操作追踪始于 ${formatDate(usage.tracking_started_at, true)}`)}
          <div class="usage-list">
            ${storageUsageMeter("R2 容量", usage.storage_bytes, usage.storage_limit_bytes, usage.storage_remaining_bytes, usage.storage_percent, "当前安装命名空间")}
            ${storageUsageMeter("A 类操作", usage.class_a, usage.class_a_limit, usage.class_a_remaining, usage.class_a_percent, `${usage.period_start || "—"} 至 ${usage.period_end || "—"}`)}
            ${storageUsageMeter("B 类操作", usage.class_b, usage.class_b_limit, usage.class_b_remaining, usage.class_b_percent, `${usage.period_start || "—"} 至 ${usage.period_end || "—"}`)}
          </div>
        </article>

        <article class="panel">
          ${panelHeader("存储偏好", "设置保存在当前工作区，不包含 R2 密钥")}
          <form id="storage-preferences-form" class="storage-form">
            <fieldset class="mode-fieldset">
              <legend>自动保存策略</legend>
              <div class="mode-segmented">
                <label><input type="radio" name="storage_mode" value="local"${storageMode === "local" ? " checked" : ""}><span>仅本地</span></label>
                <label><input type="radio" name="storage_mode" value="hybrid"${storageMode === "hybrid" ? " checked" : ""}${operational ? "" : " disabled"}><span>本地 + R2</span></label>
              </div>
            </fieldset>
            <div class="storage-fields">
              ${numberField("storage_limit_gb", "容量预算", preferences.storage_limit_gb ?? 10, "GB", "0.1", "1000000", "0.1")}
              ${numberField("class_a_limit", "A 类操作额度", preferences.class_a_limit ?? 1000000, "次", "1", "1000000000000", "1")}
              ${numberField("class_b_limit", "B 类操作额度", preferences.class_b_limit ?? 10000000, "次", "1", "1000000000000", "1")}
              ${numberField("billing_cycle_day", "预算周期起始日", preferences.billing_cycle_day ?? 1, "日", "1", "28", "1")}
            </div>
            <div class="form-footer">
              <span>手动“备份行情”会执行一次上传；自动策略影响后续刷新和模拟任务。</span>
              <button class="button primary" type="submit">保存设置</button>
            </div>
          </form>
        </article>
      </section>

      <section class="panel">
        ${panelHeader("云端快照", usage.scanned_at ? `${snapshots.length} 个行情快照出现在最近清点结果中` : "清点后显示当前安装的快照")}
        <div class="table-wrap"><table class="data-table compact">
          <thead><tr><th>快照 ID</th><th class="numeric">大小</th><th>更新时间</th></tr></thead>
          <tbody>${snapshots.length ? snapshots.map((snapshot) => `<tr><td class="mono">${escapeHtml(snapshot.snapshot_id)}</td><td class="numeric">${formatStorageBytes(snapshot.size)}</td><td>${formatDate(snapshot.last_modified, true)}</td></tr>`).join("") : emptyRow(3, data.credentials_configured ? "尚无已清点的云端快照" : "配置 R2 后可保存行情快照")}</tbody>
        </table></div>
      </section>

      <aside class="callout info"><strong>统计口径</strong><p>容量来自当前安装命名空间的 R2 对象清点。A/B 类操作只统计 AI Trade 在本机观测到的高层请求，不包含升级前记录、其他应用、其他设备及 SDK 内部重试。页面额度是你设置的预算，不是 Cloudflare 官方账单余额。</p></aside>
    </div>`;
}

function storageUsageMeter(label, used, limit, remaining, percent, note) {
  const actualPercent = Math.max(0, finite(percent) || 0);
  const progressPercent = Math.min(100, actualPercent);
  const operation = label.includes("操作");
  const renderValue = (value) => operation ? formatInteger(value) : formatStorageBytes(value);
  const overage = Math.max(0, (finite(used) || 0) - (finite(limit) || 0));
  return `<div class="usage-row">
    <div class="usage-row-head"><div><strong>${escapeHtml(label)}</strong><span>${escapeHtml(note)}</span></div><span class="mono">${renderValue(used)} / ${renderValue(limit)}</span></div>
    <progress max="100" value="${progressPercent}" aria-label="${escapeHtml(label)}已使用 ${actualPercent.toFixed(2)}%"></progress>
    <div class="usage-row-foot"><span>已使用 ${actualPercent.toFixed(3)}%</span><strong>${overage > 0 ? `超出 ${renderValue(overage)}` : `剩余 ${renderValue(remaining)}`}</strong></div>
  </div>`;
}

function numberField(name, label, value, unit, minimum, maximum, step) {
  return `<label class="field"><span>${escapeHtml(label)}</span><div class="unit-input"><input name="${escapeHtml(name)}" type="number" value="${escapeHtml(value)}" min="${minimum}" max="${maximum}" step="${step}" required><span>${escapeHtml(unit)}</span></div></label>`;
}

function renderSystem(payload) {
  const data = payload.system || {};
  const diagnosis = data.diagnosis || {};
  const actionButtons = state.actions.filter((action) => action !== "cloud-backup").map((action) => actionButton(action, action === "refresh-data" ? "primary" : "secondary")).join("");
  return `
    <div class="page-stack">
      ${pageIntro("本地系统", "所有任务仅在当前电脑运行，结果写入可检查的报告和账本", actionButtons)}

      <section class="metric-strip" aria-label="系统状态">
        ${metric("诊断状态", diagnosis.status || "不可用", diagnosis.universe_latest_dates_aligned === false ? "证券最新日期未对齐" : "证券日期已对齐", diagnosis.status === "OK" ? "tone-positive" : "tone-warning")}
        ${metric("行情截止", diagnosis.latest_market_date || "—", `完整会话截止 ${diagnosis.completed_session_cutoff || "—"}`)}
        ${metric("有效证券", formatInteger(diagnosis.point_in_time_universe?.active_count), `共加载 ${diagnosis.point_in_time_universe?.loaded_instrument_count ?? 0} 支`)}
        ${metric("券商模式", brokerModeLabel(data.broker?.mode), data.broker?.adapter ? `适配器 ${data.broker.adapter}` : "未安装真实交易适配器")}
      </section>

      <section class="split-layout">
        <article class="panel">
          ${panelHeader("后台任务", "同类任务会自动去重，任一时刻串行执行")}
          <div id="jobs-table-region">${jobsTable(payload.jobs || [])}</div>
          <div id="job-detail"></div>
        </article>
        <article class="panel">
          ${panelHeader("本地路径", "凭据不得写入仓库或报告目录")}
          <div class="path-list">
            ${Object.entries(data.paths || {}).map(([key, value]) => `<div class="path-row"><span>${escapeHtml(pathLabel(key))}</span><code>${escapeHtml(value)}</code></div>`).join("")}
          </div>
        </article>
      </section>

      <section class="panel">
        ${panelHeader("报告清单", `${data.reports?.length || 0} 个本地文件`)}
        <div class="table-wrap"><table class="data-table compact">
          <thead><tr><th>文件</th><th class="numeric">大小</th><th>更新时间</th><th>操作</th></tr></thead>
          <tbody>${data.reports?.length ? data.reports.map((row) => `<tr><td class="mono">${escapeHtml(row.name)}</td><td class="numeric">${formatBytes(row.size)}</td><td>${formatDate(row.updated_at, true)}</td><td><a class="button secondary" href="/reports/${encodeURIComponent(row.name)}" download>下载</a></td></tr>`).join("") : emptyRow(4, "尚未生成研究报告")}</tbody>
        </table></div>
      </section>

      ${diagnosis.research_warnings?.length ? `<aside class="callout warning"><strong>诊断提示</strong><ul>${diagnosis.research_warnings.map((item) => `<li>${escapeHtml(translateWarning(item))}</li>`).join("")}</ul></aside>` : ""}
    </div>`;
}

function brokerModeLabel(value) {
  return { disabled: "禁用", sandbox: "沙箱", live: "实盘" }[value] || value || "禁用";
}

function pathLabel(value) {
  return {
    project: "工程",
    config: "配置",
    cache: "行情缓存",
    reports: "报告",
    logs: "日志",
  }[value] || value;
}

function formatBytes(value) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  if (parsed < 1024) return `${parsed} B`;
  if (parsed < 1024 * 1024) return `${(parsed / 1024).toFixed(1)} KB`;
  if (parsed < 1024 * 1024 * 1024) return `${(parsed / 1024 / 1024).toFixed(1)} MB`;
  if (parsed < 1024 * 1024 * 1024 * 1024) return `${(parsed / 1024 / 1024 / 1024).toFixed(2)} GB`;
  return `${(parsed / 1024 / 1024 / 1024 / 1024).toFixed(2)} TB`;
}

function formatStorageBytes(value) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  if (parsed < 1000) return `${parsed} B`;
  if (parsed < 1000 ** 2) return `${(parsed / 1000).toFixed(1)} KB`;
  if (parsed < 1000 ** 3) return `${(parsed / 1000 ** 2).toFixed(1)} MB`;
  if (parsed < 1000 ** 4) return `${(parsed / 1000 ** 3).toFixed(2)} GB`;
  return `${(parsed / 1000 ** 4).toFixed(2)} TB`;
}

function jobsTable(jobs) {
  return `<div class="table-wrap"><table class="data-table compact">
    <thead><tr><th>任务</th><th>状态</th><th>开始时间</th><th>耗时</th><th>操作</th></tr></thead>
    <tbody>${jobs.length ? jobs.map((job) => `<tr>
      <td>${escapeHtml(JOB_LABELS[job.action] || job.action)}</td>
      <td><div class="action-row">${jobStatusChip(job.status)}${cloudBackupStatusChip(job)}</div></td>
      <td>${formatDate(job.started_at || job.created_at, true)}</td>
      <td class="mono">${jobDuration(job)}</td>
      <td><div class="action-row"><button class="button secondary" type="button" data-job-view="${escapeHtml(job.id)}" aria-label="查看${escapeHtml(JOB_LABELS[job.action] || job.action)}任务日志">查看</button>${["queued", "running"].includes(job.status) ? `<button class="button danger" type="button" data-job-cancel="${escapeHtml(job.id)}" aria-label="取消${escapeHtml(JOB_LABELS[job.action] || job.action)}任务">取消</button>` : ""}</div></td>
    </tr>`).join("") : emptyRow(5, "本次启动后尚无后台任务")}</tbody>
  </table></div>`;
}

function jobStatusChip(status) {
  const kind = {
    queued: "neutral",
    running: "warning",
    succeeded: "success",
    failed: "danger",
    cancelled: "neutral",
  }[status] || "neutral";
  return statusChip(STATUS_LABELS[status] || status || "—", kind);
}

function cloudBackupStatusChip(job) {
  const backup = job?.cloud_backup;
  if (!backup || !["succeeded", "failed", "cancelled"].includes(backup.status)) return "";
  const label = backup.automatic ? "自动云备份" : "云备份";
  const suffix = {
    succeeded: "完成",
    failed: "失败",
    cancelled: "已取消",
  }[backup.status];
  const kind = backup.status === "succeeded" ? "success" : backup.status === "failed" ? "danger" : "warning";
  return statusChip(`${label}${suffix}`, kind);
}

function cloudBackupWarningMarkup(event) {
  const backup = event?.cloud_backup;
  if (!backup || !["failed", "cancelled"].includes(backup.status)) return "";
  const automatic = Boolean(backup.automatic);
  const title = backup.status === "failed"
    ? (automatic ? "自动云备份失败" : "云备份失败")
    : (automatic ? "自动云备份已取消" : "云备份已取消");
  const message = backup.status === "failed" && automatic
    ? "本地任务结果仍有效。A/B 操作计数已在本机更新；请检查云端配置或稍后手动备份。"
    : "本地行情仍有效，已经发生的 A/B 操作也会保留在本机计数中；云端可能没有形成完整快照。";
  return `<aside class="callout warning" role="status"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p></aside>`;
}

function syncCloudBackupWarning(jobs) {
  const latest = jobs.find((job) => ["succeeded", "failed", "cancelled"].includes(job?.cloud_backup?.status));
  if (!latest) return;
  state.cloudBackupWarning = ["failed", "cancelled"].includes(latest.cloud_backup.status) ? latest : null;
}

function updateCloudBackupWarningUi() {
  const region = document.getElementById("cloud-backup-warning-region");
  if (region) region.innerHTML = cloudBackupWarningMarkup(state.cloudBackupWarning);
}

function jobDuration(job) {
  if (!job.started_at) return "—";
  const start = new Date(job.started_at).getTime();
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function chartMarkup(id, points, series, summary) {
  state.charts.set(id, { points, series });
  return `
    <figure class="chart-frame">
      <canvas id="${escapeHtml(id)}" role="img" tabindex="0" aria-label="${escapeHtml(summary)}" aria-describedby="${escapeHtml(id)}-summary"></canvas>
      <figcaption id="${escapeHtml(id)}-summary" class="chart-caption">${escapeHtml(summary)}</figcaption>
    </figure>`;
}

function drawCharts() {
  for (const [id, spec] of state.charts) {
    const canvas = document.getElementById(id);
    if (canvas) drawLineChart(canvas, spec.points, spec.series);
  }
}

function drawLineChart(canvas, points, series) {
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 20 || rect.height < 20) return;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, rect.width, rect.height);
  const style = getComputedStyle(document.documentElement);
  const inkSoft = style.getPropertyValue("--ink-soft").trim();
  const rule = style.getPropertyValue("--rule").trim();
  const validSeries = series.map((line) => ({
    ...line,
    values: points.map((point) => finite(point[line.key])),
  })).filter((line) => line.values.some((value) => value !== null));
  const values = validSeries.flatMap((line) => line.values.filter((value) => value !== null));
  if (points.length < 2 || values.length < 2) {
    context.fillStyle = inkSoft;
    context.font = '13px "Segoe UI", sans-serif';
    context.fillText("数据点不足，曲线将在后续交易日形成", 18, rect.height / 2);
    return;
  }
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  if (minimum === maximum) {
    const padding = Math.max(Math.abs(minimum) * 0.02, 1);
    minimum -= padding;
    maximum += padding;
  }
  const rangePadding = (maximum - minimum) * 0.08;
  minimum -= rangePadding;
  maximum += rangePadding;
  const plot = { left: 62, top: 30, right: rect.width - 16, bottom: rect.height - 30 };
  const plotWidth = Math.max(1, plot.right - plot.left);
  const plotHeight = Math.max(1, plot.bottom - plot.top);
  context.lineWidth = 1;
  context.strokeStyle = rule;
  context.fillStyle = inkSoft;
  context.font = '11px "Segoe UI", sans-serif';
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let index = 0; index <= 4; index += 1) {
    const y = plot.top + (plotHeight * index) / 4;
    context.beginPath();
    context.moveTo(plot.left, y);
    context.lineTo(plot.right, y);
    context.stroke();
    const value = maximum - ((maximum - minimum) * index) / 4;
    context.fillText(compactFormatter.format(value), plot.left - 8, y);
  }
  context.textBaseline = "top";
  const indexes = [0, Math.floor((points.length - 1) / 2), points.length - 1];
  indexes.forEach((pointIndex, labelIndex) => {
    const x = plot.left + (plotWidth * pointIndex) / (points.length - 1);
    context.textAlign = labelIndex === 0 ? "left" : labelIndex === 2 ? "right" : "center";
    context.fillText(String(points[pointIndex]?.date || ""), x, plot.bottom + 8);
  });
  validSeries.forEach((line) => {
    context.beginPath();
    context.strokeStyle = style.getPropertyValue(line.color).trim();
    context.lineWidth = 2;
    let started = false;
    line.values.forEach((value, index) => {
      if (value === null) return;
      const x = plot.left + (plotWidth * index) / (points.length - 1);
      const y = plot.bottom - ((value - minimum) / (maximum - minimum)) * plotHeight;
      if (!started) {
        context.moveTo(x, y);
        started = true;
      } else {
        context.lineTo(x, y);
      }
    });
    context.stroke();
  });
  let legendX = plot.left;
  context.textAlign = "left";
  context.textBaseline = "middle";
  validSeries.forEach((line) => {
    context.fillStyle = style.getPropertyValue(line.color).trim();
    context.fillRect(legendX, 10, 14, 3);
    context.fillStyle = inkSoft;
    context.fillText(line.label, legendX + 20, 12);
    legendX += context.measureText(line.label).width + 48;
  });
}

async function refreshStorageInventory(silent = false) {
  const button = document.querySelector("[data-storage-refresh]");
  if (button) {
    button.disabled = true;
    button.textContent = "正在清点";
  }
  try {
    const payload = await api("/api/storage/refresh", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
    });
    state.data.set("storage", payload);
    if (state.route === "storage") renderRoute(payload);
    if (!silent) notify(payload.inventory_error ? "云端清点未完成" : "云端用量已更新", Boolean(payload.inventory_error));
  } catch (error) {
    notify(friendlyError(error.message), true);
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = "清点云端";
    }
  }
}

async function reloadStorageStatus(silent = false) {
  try {
    const payload = await api("/api/storage");
    state.data.set("storage", payload);
    if (state.route === "storage") renderRoute(payload);
    if (!silent) notify("本地存储用量已更新");
  } catch (error) {
    if (!silent) notify(friendlyError(error.message), true);
  }
}

async function saveStoragePreferences(form) {
  const button = form.querySelector('button[type="submit"]');
  const values = new FormData(form);
  button.disabled = true;
  button.textContent = "正在保存";
  try {
    const payload = await api("/api/storage/preferences", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify({
        storage_mode: String(values.get("storage_mode") || "local"),
        storage_limit_gb: Number(values.get("storage_limit_gb")),
        class_a_limit: Number(values.get("class_a_limit")),
        class_b_limit: Number(values.get("class_b_limit")),
        billing_cycle_day: Number(values.get("billing_cycle_day")),
      }),
    });
    state.data.set("storage", payload);
    if (state.route === "storage") renderRoute(payload);
    notify("存储设置已保存");
  } catch (error) {
    notify(friendlyError(error.message), true);
    button.disabled = false;
    button.textContent = "保存设置";
  }
}

function downloadShadowTemplate() {
  const shadow = state.data.get("trading")?.shadow_account || {};
  const columns = shadow.canonical_columns || [
    "fill_id", "order_id", "symbol", "side", "quantity", "price",
    "commission", "tax", "filled_at",
  ];
  const blob = new Blob([`${columns.join(",")}\r\n`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "ai-trade-shadow-fills-template.csv";
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  notify("影子成交空白模板已生成");
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunks = [];
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 32768)));
  }
  return btoa(chunks.join(""));
}

async function importShadowAccount(form) {
  if (state.shadowImportBusy) return;
  const values = new FormData(form);
  const file = values.get("csv_file");
  const maximum = finite(state.data.get("trading")?.shadow_account?.max_import_bytes) || 1000000;
  if (!(file instanceof File) || !file.size) {
    notify("请选择非空的 CSV 成交文件", true);
    return;
  }
  if (file.size > maximum) {
    notify(`CSV 文件超过 ${formatNumber(maximum / 1000000, 2)} MB 上限`, true);
    return;
  }
  state.shadowImportBusy = true;
  form.setAttribute("aria-busy", "true");
  const controls = [...form.querySelectorAll("input, button")];
  controls.forEach((control) => { control.disabled = true; });
  const button = form.querySelector('button[type="submit"]');
  if (button) button.textContent = "正在校验";
  try {
    const csvBase64 = arrayBufferToBase64(await file.arrayBuffer());
    const payload = await api("/api/shadow-account/import", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify({
        source_label: String(values.get("source_label") || ""),
        account_alias: String(values.get("account_alias") || ""),
        csv_base64: csvBase64,
      }),
    });
    const trading = state.data.get("trading") || {};
    state.data.set("trading", {
      ...trading,
      generated_at: payload.generated_at,
      shadow_account: payload.shadow_account,
    });
    if (state.route === "trading") renderRoute(state.data.get("trading"));
    const result = payload.import_result || {};
    notify(
      result.already_imported
        ? "该文件已导入，账本未重复写入"
        : `影子成交已校验：接收 ${formatInteger(result.accepted_count)} 条，识别重复 ${formatInteger(result.duplicate_count)} 条`,
    );
  } catch (error) {
    notify(friendlyError(error.message), true);
    if (form.isConnected) {
      form.setAttribute("aria-busy", "false");
      controls.forEach((control) => { control.disabled = false; });
      if (button) button.textContent = "导入并复盘";
    }
  } finally {
    state.shadowImportBusy = false;
  }
}

async function startJob(action) {
  if (state.pendingActions.has(action)) return;
  state.pendingActions.add(action);
  updateJobButtons();
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify({ action }),
    });
    mergeJob(job);
    notify(`${JOB_LABELS[action] || action}已进入任务队列`);
    updateJobsUi();
    if (["refresh-market-intelligence", "refresh-market-breadth"].includes(action) && state.route === "intelligence") {
      await loadRoute();
      restoreFocusAfterRender(`[data-job-action="${action}"]`, "intelligence");
    }
  } catch (error) {
    notify(friendlyError(error.message), true);
  } finally {
    state.pendingActions.delete(action);
    updateJobButtons();
  }
}

async function cancelJob(jobId) {
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
      headers: { "X-AI-Trade-Token": state.token },
    });
    mergeJob(job);
    notify("已请求取消任务");
    updateJobsUi();
  } catch (error) {
    notify(friendlyError(error.message), true);
  }
}

async function showJob(jobId) {
  const detail = document.getElementById("job-detail");
  if (!detail) return;
  detail.setAttribute("aria-busy", "true");
  detail.setAttribute("role", "status");
  detail.innerHTML = `<div class="skeleton-line" aria-label="正在读取任务日志"></div>`;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    detail.innerHTML = `
      <section class="panel" aria-live="polite">
        ${panelHeader(`${JOB_LABELS[job.action] || job.action} · ${STATUS_LABELS[job.status] || job.status}`, job.return_code === null ? "进程尚未结束" : `退出码 ${job.return_code}`, cloudBackupStatusChip(job))}
        ${cloudBackupWarningMarkup(job)}
        <pre class="job-output">${escapeHtml(job.output || "任务尚无输出")}</pre>
      </section>`;
  } catch (error) {
    detail.innerHTML = `<div class="callout danger"><strong>无法读取任务日志</strong><p>${escapeHtml(friendlyError(error.message))}</p></div>`;
  } finally {
    detail.setAttribute("aria-busy", "false");
  }
}

async function pollJobs() {
  try {
    const payload = await api("/api/jobs");
    const jobs = payload.jobs || [];
    let storageRefreshMode = "";
    let intelligenceRefreshCompleted = "";
    for (const job of jobs) {
      const previous = state.jobStates.get(job.id);
      if (previous && ["queued", "running"].includes(previous) && ["succeeded", "failed", "cancelled"].includes(job.status)) {
        const backupStatus = job.cloud_backup?.status;
        const automaticBackupFailed = job.cloud_backup?.automatic && backupStatus === "failed";
        notify(
          automaticBackupFailed && job.status === "succeeded"
            ? `${JOB_LABELS[job.action] || job.action}已完成；自动云备份失败，本地任务结果仍有效`
            : `${JOB_LABELS[job.action] || job.action}${job.status === "succeeded" ? "已完成" : job.status === "failed" ? "失败" : "已取消"}`,
          job.status === "failed" || automaticBackupFailed,
        );
        if (!storageRefreshMode && backupStatus === "succeeded") {
          storageRefreshMode = "inventory";
        } else if (!storageRefreshMode && ["failed", "cancelled"].includes(backupStatus)) {
          storageRefreshMode = "local";
        } else if (
          !storageRefreshMode
          && job.status === "cancelled"
          && ["refresh-data", "paper-run"].includes(job.action)
          && state.data.get("storage")?.effective_storage_mode === "hybrid"
        ) {
          storageRefreshMode = "local";
        }
        if (["refresh-market-intelligence", "refresh-market-breadth"].includes(job.action)) {
          intelligenceRefreshCompleted = job.action;
        }
      }
      state.jobStates.set(job.id, job.status);
    }
    state.jobs = jobs;
    syncCloudBackupWarning(jobs);
    updateJobsUi();
    updateCloudBackupWarningUi();
    setConnection(true);
    if (state.route === "storage" && storageRefreshMode === "inventory") {
      await refreshStorageInventory(true);
    } else if (state.route === "storage" && storageRefreshMode === "local") {
      await reloadStorageStatus(true);
    } else if (state.route === "intelligence" && intelligenceRefreshCompleted) {
      await loadRoute();
      restoreFocusAfterRender(`[data-job-action="${intelligenceRefreshCompleted}"]`, "intelligence");
    }
  } catch {
    setConnection(false);
  }
}

function mergeJob(job) {
  state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
  state.jobStates.set(job.id, job.status);
  syncCloudBackupWarning(state.jobs);
}

function updateJobsUi() {
  const active = state.jobs.filter((job) => ["queued", "running"].includes(job.status));
  const activeActions = new Set(active.map((job) => job.action));
  const pending = [...state.pendingActions].filter((action) => !activeActions.has(action)).length;
  const activeCount = active.length + pending;
  jobIndicator.textContent = activeCount
    ? pending && !active.length ? `${pending} 个任务提交中` : `${activeCount} 个任务运行中`
    : "无运行任务";
  jobIndicator.className = `status-chip ${activeCount ? "warning" : "neutral"}`;
  jobIndicator.setAttribute("aria-busy", String(Boolean(activeCount)));
  const region = document.getElementById("jobs-table-region");
  if (region) {
    region.innerHTML = jobsTable(state.jobs);
    enhanceRenderedUi();
  }
  updateMarketPulse(state.data.get(state.route) || null);
  updateJobButtons();
}

function updateJobButtons() {
  const runningActions = new Set(
    state.jobs.filter((job) => ["queued", "running"].includes(job.status)).map((job) => job.action)
  );
  for (const button of document.querySelectorAll("[data-job-action]")) {
    const busy = runningActions.has(button.dataset.jobAction) || state.pendingActions.has(button.dataset.jobAction);
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
    button.textContent = busy
      ? `${JOB_LABELS[button.dataset.jobAction] || button.dataset.jobAction}进行中`
      : JOB_LABELS[button.dataset.jobAction] || button.dataset.jobAction;
  }
}

function notify(message, error = false) {
  const region = document.getElementById("toast-region");
  const toast = document.createElement("div");
  toast.className = `toast${error ? " error" : ""}`;
  toast.setAttribute("role", error ? "alert" : "status");
  toast.textContent = message;
  region.append(toast);
  window.setTimeout(() => toast.remove(), 4200);
}

async function logout() {
  logoutButton.disabled = true;
  logoutButton.textContent = "正在退出";
  try {
    await api("/api/auth/logout", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
    });
  } catch (error) {
    if (!location.pathname.startsWith("/login")) {
      notify(error.message || "退出失败", true);
    }
  } finally {
    location.replace("/login");
  }
}

document.addEventListener("click", (event) => {
  const breadthFilterClear = event.target.closest("[data-market-breadth-filter-clear]");
  if (breadthFilterClear) {
    clearBreadthFilters();
    return;
  }
  const intelligenceFilterClear = event.target.closest("[data-intelligence-filter-clear]");
  if (intelligenceFilterClear) {
    clearIntelligenceFilters();
    return;
  }
  const researchDigestGenerate = event.target.closest("[data-research-digest-generate]");
  if (researchDigestGenerate) {
    generateResearchDigests(researchDigestGenerate);
    return;
  }
  const journalCorrect = event.target.closest("[data-journal-correct]");
  if (journalCorrect) {
    selectJournalCorrection(journalCorrect.dataset.journalCorrect);
    return;
  }
  const journalCorrectionCancel = event.target.closest("[data-journal-correction-cancel]");
  if (journalCorrectionCancel) {
    cancelJournalCorrection();
    return;
  }
  const journalFilterClear = event.target.closest("[data-journal-filter-clear]");
  if (journalFilterClear) {
    clearJournalFilters();
    return;
  }
  const universeSubmit = event.target.closest("[data-universe-submit]");
  if (universeSubmit) {
    const form = universeSubmit.closest("#universe-date-form");
    if (form && !universeSubmit.disabled) applyUniverseFilterForm(form);
    return;
  }
  const universeReset = event.target.closest("[data-universe-reset]");
  if (universeReset) {
    state.universeDate = "";
    state.universeFilters = {
      asset_class: "",
      sector: "",
      trend: "any",
      coverage: "all",
      min_average_amount: "",
      max_annual_volatility: "",
      active_only: false,
      sort: "momentum",
      direction: "desc",
      limit: "200",
    };
    loadRoute();
    return;
  }
  const monitoringScan = event.target.closest("[data-monitoring-scan]");
  if (monitoringScan) {
    runMonitoringScan(monitoringScan);
    return;
  }
  const monitoringRefresh = event.target.closest("[data-monitoring-refresh]");
  if (monitoringRefresh) {
    reloadMonitoring();
    return;
  }
  const monitoringFilterClear = event.target.closest("[data-monitoring-filter-clear]");
  if (monitoringFilterClear) {
    state.monitoringFilters = {
      watchlist_id: "",
      symbol: "",
      severity: "",
      status: "unresolved",
      limit: "100",
    };
    monitoringSetStatus("筛选已清除", "success");
    monitoringRenderCurrent();
    return;
  }
  const monitoringComposeCancel = event.target.closest("[data-monitoring-alert-compose-cancel]");
  if (monitoringComposeCancel) {
    state.monitoringActionTarget = null;
    monitoringSetStatus("", "");
    monitoringRenderCurrent();
    return;
  }
  const monitoringCompose = event.target.closest("[data-monitoring-alert-compose]");
  if (monitoringCompose) {
    state.monitoringActionTarget = {
      alertId: monitoringCompose.dataset.monitoringAlertId,
      action: monitoringCompose.dataset.monitoringAlertCompose,
    };
    monitoringSetStatus("", "");
    monitoringRenderCurrent();
    window.requestAnimationFrame(() => document.getElementById("monitoring-action-note")?.focus({ preventScroll: true }));
    return;
  }
  const monitoringAlertAction = event.target.closest("[data-monitoring-alert-action]");
  if (monitoringAlertAction) {
    runMonitoringAlertAction(
      monitoringAlertAction.dataset.monitoringAlertId,
      monitoringAlertAction.dataset.monitoringAlertAction,
      {},
      monitoringAlertAction,
    );
    return;
  }
  const monitoringWatchlistAction = event.target.closest("[data-monitoring-watchlist-action]");
  if (monitoringWatchlistAction) {
    const actionName = monitoringWatchlistAction.dataset.monitoringWatchlistAction;
    const watchlistId = monitoringWatchlistAction.dataset.monitoringWatchlistId;
    if (actionName === "delete" && !window.confirm("确认删除这个监控列表？其中的规则也将无法继续扫描。")) return;
    if (actionName === "rename") {
      const currentName = monitoringWatchlistAction.dataset.monitoringWatchlistName || "";
      const name = window.prompt("输入新的监控列表名称", currentName);
      if (name === null || !name.trim()) return;
      mutateMonitoringWatchlist("rename", watchlistId, { name: name.trim() }, monitoringWatchlistAction);
      return;
    }
    if (actionName === "set_enabled") {
      mutateMonitoringWatchlist("set_enabled", watchlistId, { enabled: monitoringWatchlistAction.dataset.monitoringEnabled === "true" }, monitoringWatchlistAction);
      return;
    }
    mutateMonitoringWatchlist(
      actionName,
      watchlistId,
      actionName === "remove_symbol" || actionName === "add_symbol"
        ? { symbol: monitoringWatchlistAction.dataset.monitoringSymbol }
        : {},
      monitoringWatchlistAction,
    );
    return;
  }
  const monitoringRuleAction = event.target.closest("[data-monitoring-rule-action]");
  if (monitoringRuleAction) {
    const actionName = monitoringRuleAction.dataset.monitoringRuleAction;
    if (actionName === "delete" && !window.confirm("确认删除这条监控规则？历史告警证据不会被改写。")) return;
    mutateMonitoringRule(
      actionName,
      monitoringRuleAction.dataset.monitoringRuleId,
      monitoringRuleAction.dataset.monitoringRuleEnabled === "true",
      monitoringRuleAction,
    );
    return;
  }
  const shadowTemplate = event.target.closest("[data-shadow-template]");
  if (shadowTemplate) {
    downloadShadowTemplate();
    return;
  }
  const marketPeriod = event.target.closest("[data-market-period]");
  if (marketPeriod) {
    const period = marketPeriod.dataset.marketPeriod;
    if (MARKET_PERIODS[period] && period !== state.marketPeriod) {
      state.marketPeriod = period;
      loadRoute();
    }
    return;
  }
  const action = event.target.closest("[data-job-action]");
  if (action) {
    startJob(action.dataset.jobAction);
    return;
  }
  const assistantHistory = event.target.closest("[data-assistant-history]");
  if (assistantHistory) {
    const payload = state.data.get("assistant") || {};
    const selected = (payload.history || []).find(
      (item) => item.analysis_id === assistantHistory.dataset.assistantHistory
    );
    if (selected) {
      state.assistantResult = selected;
      renderRoute(payload);
      main.focus({ preventScroll: true });
    }
    return;
  }
  const strategyMode = event.target.closest("[data-strategy-mode]");
  if (strategyMode) {
    state.strategyLabMode = strategyMode.dataset.strategyMode;
    const payload = state.data.get("strategy-lab");
    if (payload) renderRoute(payload);
    return;
  }
  const strategyCandidate = event.target.closest("[data-strategy-candidate]");
  if (strategyCandidate) {
    state.strategyCandidateId = strategyCandidate.dataset.strategyCandidate;
    const payload = state.data.get("strategy-lab");
    if (payload) {
      renderRoute(payload);
      window.requestAnimationFrame(() => {
        const selected = [...document.querySelectorAll("[data-strategy-candidate]")]
          .find((item) => item.dataset.strategyCandidate === state.strategyCandidateId);
        selected?.focus({ preventScroll: true });
      });
    }
    return;
  }
  const strategyValidate = event.target.closest("[data-strategy-validate]");
  if (strategyValidate) {
    runStrategyLabMutation(
      `/api/strategy-lab/candidates/${encodeURIComponent(strategyValidate.dataset.strategyValidate)}/validate`,
      {},
      "候选验证已完成",
      strategyValidate,
    );
    return;
  }
  const strategyExport = event.target.closest("[data-strategy-export]");
  if (strategyExport) {
    runStrategyLabMutation(
      `/api/strategy-lab/candidates/${encodeURIComponent(strategyExport.dataset.strategyExport)}/export`,
      { confirmed: true },
      "独立模拟配置已导出",
      strategyExport,
    );
    return;
  }
  const strategyMonitor = event.target.closest("[data-strategy-monitor]");
  if (strategyMonitor) {
    runStrategyLabMutation(
      "/api/strategy-lab/monitor",
      {},
      "策略衰减证据已记录",
      strategyMonitor,
    );
    return;
  }
  const strategyRollback = event.target.closest("[data-strategy-rollback]");
  if (strategyRollback) {
    const active = state.data.get("strategy-lab")?.active;
    if (!active?.candidate_id || !active?.fingerprint) {
      notify("当前活动策略状态不完整，请刷新策略实验室后重试", true);
      return;
    }
    if (window.confirm("确认回滚到上一个已激活的模拟策略版本？默认配置和真实交易权限不会改变。")) {
      runStrategyLabMutation(
        "/api/strategy-lab/rollback",
        {
          confirmed: true,
          note: "用户从策略实验室执行回滚",
          expected_active_candidate_id: active.candidate_id,
          expected_active_fingerprint: active.fingerprint,
        },
        "模拟策略版本已回滚",
        strategyRollback,
      );
    }
    return;
  }
  const retry = event.target.closest("[data-retry]");
  if (retry) {
    loadRoute();
    return;
  }
  const storageRefresh = event.target.closest("[data-storage-refresh]");
  if (storageRefresh) {
    refreshStorageInventory();
    return;
  }
  const view = event.target.closest("[data-job-view]");
  if (view) {
    showJob(view.dataset.jobView);
    return;
  }
  const cancel = event.target.closest("[data-job-cancel]");
  if (cancel) {
    cancelJob(cancel.dataset.jobCancel);
    return;
  }
  const tab = event.target.closest("[data-trading-tab]");
  if (tab) {
    state.tradingTab = tab.dataset.tradingTab;
    const data = state.data.get("trading");
    if (data) renderRoute(data);
  }
});

document.addEventListener("keydown", (event) => {
  const pulseRegion = event.target.closest(".market-pulse-track");
  if (pulseRegion && event.target === pulseRegion && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    if (pulseRegion.scrollWidth <= pulseRegion.clientWidth) return;
    event.preventDefault();
    const maximum = pulseRegion.scrollWidth - pulseRegion.clientWidth;
    const step = Math.max(150, Math.round(pulseRegion.clientWidth * 0.6));
    if (event.key === "Home") pulseRegion.scrollLeft = 0;
    if (event.key === "End") pulseRegion.scrollLeft = maximum;
    if (event.key === "ArrowLeft") pulseRegion.scrollLeft = Math.max(0, pulseRegion.scrollLeft - step);
    if (event.key === "ArrowRight") pulseRegion.scrollLeft = Math.min(maximum, pulseRegion.scrollLeft + step);
    return;
  }
  const tableRegion = event.target.closest(".table-wrap");
  if (tableRegion && event.target === tableRegion && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    if (tableRegion.scrollWidth <= tableRegion.clientWidth) return;
    event.preventDefault();
    const maximum = tableRegion.scrollWidth - tableRegion.clientWidth;
    if (event.key === "Home") tableRegion.scrollLeft = 0;
    if (event.key === "End") tableRegion.scrollLeft = maximum;
    if (event.key === "ArrowLeft") tableRegion.scrollLeft = Math.max(0, tableRegion.scrollLeft - 96);
    if (event.key === "ArrowRight") tableRegion.scrollLeft = Math.min(maximum, tableRegion.scrollLeft + 96);
    return;
  }
  const current = event.target.closest('[role="tab"]');
  const tablist = current?.closest('[role="tablist"]');
  if (!current || !tablist || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...tablist.querySelectorAll('[role="tab"]')].filter((item) => !item.disabled);
  const index = tabs.indexOf(current);
  if (index < 0 || !tabs.length) return;
  event.preventDefault();
  let nextIndex = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : index;
  if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
  if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
  const next = tabs[nextIndex];
  const nextId = next.id;
  next.click();
  window.requestAnimationFrame(() => document.getElementById(nextId)?.focus({ preventScroll: true }));
});

document.addEventListener("change", (event) => {
  const marketOverlay = event.target.closest("[data-market-overlay]");
  if (marketOverlay && Object.prototype.hasOwnProperty.call(MARKET_OVERLAYS, marketOverlay.value)) {
    setMarketIndicator("overlay", marketOverlay.value);
    return;
  }
  const marketOscillator = event.target.closest("[data-market-oscillator]");
  if (marketOscillator && Object.prototype.hasOwnProperty.call(MARKET_OSCILLATORS, marketOscillator.value)) {
    setMarketIndicator("oscillator", marketOscillator.value);
    return;
  }
  const confirmation = event.target.closest(".strategy-confirmation-form input[name='confirmed']");
  if (confirmation) {
    const form = confirmation.closest("form");
    const submit = form?.querySelector("button[type='submit']");
    if (submit) submit.disabled = !confirmation.checked || state.strategyActionBusy;
    return;
  }
  if (event.target.closest("[data-journal-decision]")) {
    syncJournalDecisionControl();
    return;
  }
  if (event.target.closest("#monitoring-rule-type")) {
    syncMonitoringRuleControl();
  }
});

document.addEventListener("submit", (event) => {
  if (event.target.id === "market-breadth-filter-form") {
    event.preventDefault();
    if (event.target.reportValidity()) applyBreadthFilterForm(event.target);
  } else if (event.target.id === "market-intelligence-filter-form") {
    event.preventDefault();
    if (event.target.reportValidity()) applyIntelligenceFilterForm(event.target);
  } else if (event.target.id === "market-controls-form") {
    event.preventDefault();
    const values = new FormData(event.target);
    const symbol = String(values.get("symbol") || "");
    const limit = Number(values.get("limit"));
    if (symbol) state.marketSymbol = symbol;
    if ([120, 240, 500, 1000, 1500].includes(limit)) state.marketLimit = limit;
    loadRoute();
  } else if (event.target.id === "monitoring-filter-form") {
    event.preventDefault();
    const values = new FormData(event.target);
    state.monitoringFilters = {
      watchlist_id: String(values.get("watchlist_id") || ""),
      symbol: String(values.get("symbol") || "").trim(),
      severity: String(values.get("severity") || ""),
      status: String(values.get("status") || "unresolved"),
      limit: String(values.get("limit") || "100"),
    };
    monitoringSetStatus("筛选已应用", "success");
    monitoringRenderCurrent();
  } else if (event.target.id === "monitoring-watchlist-form") {
    event.preventDefault();
    createMonitoringWatchlist(event.target);
  } else if (event.target.matches("[data-monitoring-symbol-form]")) {
    event.preventDefault();
    const form = event.target;
    const values = new FormData(form);
    const symbol = String(values.get("symbol") || "");
    if (!symbol) {
      form.reportValidity();
      return;
    }
    const button = form.querySelector("button[type='submit']");
    mutateMonitoringWatchlist(
      "add_symbol",
      form.dataset.watchlistId,
      { symbol },
      button,
    );
  } else if (event.target.id === "monitoring-rule-form") {
    event.preventDefault();
    createMonitoringRule(event.target);
  } else if (event.target.id === "monitoring-alert-action-form") {
    event.preventDefault();
    const values = new FormData(event.target);
    const action = String(values.get("action") || "");
    const alertId = String(values.get("alert_id") || "");
    const extras = { note: String(values.get("note") || "") };
    if (action === "snooze") extras.snooze_until = String(values.get("snooze_until") || "");
    runMonitoringAlertAction(alertId, action, extras, event.target.querySelector("button[type='submit']"));
  } else if (event.target.id === "assistant-analysis-form") {
    event.preventDefault();
    runAssistantAnalysis(event.target);
  } else if (event.target.id === "strategy-manual-form") {
    event.preventDefault();
    createManualStrategyCandidate(event.target);
  } else if (event.target.id === "strategy-proposal-form") {
    event.preventDefault();
    createProposedStrategyCandidate(event.target);
  } else if (event.target.matches("[data-strategy-lifecycle-form]")) {
    event.preventDefault();
    const values = new FormData(event.target);
    const action = event.target.dataset.lifecycleAction;
    const labels = {
      suspend: "模拟版本已暂停",
      resume: "模拟观察已恢复",
      retire: "模拟版本已退役并恢复上一基线",
    };
    runStrategyLabMutation(
      `/api/strategy-lab/lifecycle/${encodeURIComponent(action)}`,
      {
        confirmed: values.get("confirmed") === "on",
        note: String(values.get("note") || ""),
        expected_active_candidate_id: String(values.get("candidate_id") || ""),
        expected_active_fingerprint: String(values.get("fingerprint") || ""),
        monitor_id: String(values.get("monitor_id") || "") || null,
      },
      labels[action] || "策略生命周期已更新",
      event.target.querySelector("button[type='submit']"),
    );
  } else if (event.target.id === "shadow-import-form") {
    event.preventDefault();
    importShadowAccount(event.target);
  } else if (event.target.id === "research-journal-form") {
    event.preventDefault();
    appendResearchJournal(event.target);
  } else if (event.target.id === "research-journal-filter-form") {
    event.preventDefault();
    applyJournalFilterForm(event.target);
  } else if (event.target.id === "strategy-approval-form") {
    event.preventDefault();
    const values = new FormData(event.target);
    const candidateId = String(values.get("candidate_id") || "");
    runStrategyLabMutation(
      `/api/strategy-lab/candidates/${encodeURIComponent(candidateId)}/approve`,
      { confirmed: values.get("confirmed") === "on", note: String(values.get("note") || "") },
      "候选已由当前用户批准",
      event.target.querySelector("button[type='submit']"),
    );
  } else if (event.target.id === "strategy-activation-form") {
    event.preventDefault();
    const values = new FormData(event.target);
    const candidateId = String(values.get("candidate_id") || "");
    runStrategyLabMutation(
      `/api/strategy-lab/candidates/${encodeURIComponent(candidateId)}/activate`,
      { confirmed: values.get("confirmed") === "on", note: String(values.get("note") || "") },
      "候选已设为实验室活动模拟版本",
      event.target.querySelector("button[type='submit']"),
    );
  } else if (event.target.id === "storage-preferences-form") {
    event.preventDefault();
    saveStoragePreferences(event.target);
  }
});

document.getElementById("refresh-view").addEventListener("click", loadRoute);
logoutButton.addEventListener("click", logout);

window.addEventListener("hashchange", async () => {
  const next = validRoute(location.hash.slice(1)) || "overview";
  if (next === state.route) return;
  state.route = next;
  await loadRoute();
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  main.focus({ preventScroll: true });
});

window.addEventListener("resize", () => {
  window.clearTimeout(state.resizeTimer);
  state.resizeTimer = window.setTimeout(drawCharts, 120);
});

window.addEventListener("pagehide", destroyMarketChart);

if (!location.hash) {
  history.replaceState(null, "", "#overview");
}
if (location.protocol === "file:") {
  renderFileProtocolNotice();
} else {
  bootstrap();
}
