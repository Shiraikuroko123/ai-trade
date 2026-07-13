"use strict";

const ROUTES = {
  overview: { title: "总览", context: "每日收盘复盘" },
  research: { title: "研究", context: "历史证据与稳健性" },
  portfolio: { title: "组合", context: "模拟账户与目标权重" },
  trading: { title: "交易", context: "执行记录与权限晋级" },
  risk: { title: "风险", context: "约束、尾部与实盘门禁" },
  universe: { title: "数据", context: "证券主数据与覆盖" },
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

const JOB_LABELS = {
  "refresh-data": "刷新行情",
  backtest: "运行回测",
  "walk-forward": "滚动验证",
  validate: "稳健性验证",
  "paper-init": "初始化模拟账户",
  "paper-run": "运行模拟日",
  "paper-audit": "审计模拟账户",
};

const CHECK_LABELS = {
  broker_mode_live: "配置明确选择实盘模式",
  adapter_configured: "已选择券商适配器",
  adapter_installed: "券商适配器已安装",
  account_configured: "已绑定券商账户",
  paper_gate_passed: "前向模拟门禁通过",
  sandbox_reconciled: "券商沙箱连续对账通过",
  kill_switch_clear: "紧急停止开关未触发",
  authorization_valid: "人工授权有效且未过期",
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
  normal: "正常",
  paper_evidence: "收集模拟证据",
  sandbox_review: "待沙箱复核",
  sandbox_reconciled: "沙箱已对账",
  live_authorized: "实盘已授权",
  collecting_independent_forward_evidence: "收集独立前向证据",
  eligible_for_broker_sandbox_review: "可进入券商沙箱复核",
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
  tradingTab: "paper",
  universeDate: "",
  resizeTimer: 0,
};

const main = document.getElementById("main-content");
const routeTitle = document.getElementById("route-title");
const routeContext = document.getElementById("route-context");
const marketDate = document.getElementById("market-date");
const versionLabel = document.getElementById("version-label");
const connectionDot = document.getElementById("connection-dot");
const connectionLabel = document.getElementById("connection-label");
const jobIndicator = document.getElementById("job-indicator");
const signedInUser = document.getElementById("signed-in-user");
const logoutButton = document.getElementById("logout");

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
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatMoney(value) {
  const parsed = finite(value);
  return parsed === null ? "—" : moneyFormatter.format(parsed);
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

function tone(value, inverse = false) {
  const parsed = finite(value);
  if (parsed === null || parsed === 0) return "";
  const positive = inverse ? parsed < 0 : parsed > 0;
  return positive ? "tone-positive" : "tone-negative";
}

function statusChip(label, kind = "neutral") {
  return `<span class="status-chip ${kind}">${escapeHtml(label)}</span>`;
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
  return `<tr><td class="empty-row" colspan="${colspan}">${escapeHtml(message)}</td></tr>`;
}

function instrumentName(symbol, fallback) {
  return INSTRUMENT_NAMES[symbol] || fallback || symbol || "未知证券";
}

function actionButton(action, style = "secondary") {
  if (!state.actions.includes(action)) return "";
  return `<button class="button ${style}" type="button" data-job-action="${escapeHtml(action)}">${escapeHtml(JOB_LABELS[action] || action)}</button>`;
}

function skeletonPage() {
  return `
    <div class="skeleton-stack" aria-label="正在加载">
      <div class="skeleton-line"></div>
      <div class="skeleton-block"></div>
      <div class="skeleton-block"></div>
      <span class="sr-only">正在加载当前视图</span>
    </div>`;
}

function friendlyError(message) {
  const value = String(message || "请求失败");
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
  main.innerHTML = `
    <section class="error-state" role="alert">
      <h2>当前视图无法完成加载</h2>
      <p>${escapeHtml(message)}</p>
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
  jobIndicator.textContent = "服务未启动";
  jobIndicator.className = "status-chip danger";
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
    throw new Error(payload.error || `请求失败 (${response.status})`);
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
  routeTitle.textContent = meta.title;
  routeContext.textContent = meta.context;
  document.title = `${meta.title} | AI Trade`;
  for (const link of document.querySelectorAll("[data-route]")) {
    if (link.dataset.route === route) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }
}

async function loadRoute() {
  state.controller?.abort();
  state.controller = new AbortController();
  const signal = state.controller.signal;
  setRouteChrome(state.route);
  main.setAttribute("aria-busy", "true");
  main.innerHTML = skeletonPage();
  try {
    let payload;
    if (state.route === "risk") {
      const [overview, research] = await Promise.all([
        api("/api/overview", { signal }),
        api("/api/research", { signal }),
      ]);
      payload = { overview, research };
    } else if (state.route === "system") {
      const [system, jobs] = await Promise.all([
        api("/api/system", { signal }),
        api("/api/jobs", { signal }),
      ]);
      payload = { system, jobs: jobs.jobs || [] };
    } else {
      const endpoints = {
        overview: "/api/overview",
        research: "/api/research",
        portfolio: "/api/portfolio",
        trading: "/api/trading",
        universe: `/api/universe${state.universeDate ? `?date=${encodeURIComponent(state.universeDate)}` : ""}`,
      };
      payload = await api(endpoints[state.route], { signal });
    }
    state.data.set(state.route, payload);
    renderRoute(payload);
    setConnection(true);
  } catch (error) {
    if (error.name !== "AbortError") {
      setConnection(false);
      renderError(error);
    }
  } finally {
    main.setAttribute("aria-busy", "false");
  }
}

function renderRoute(payload) {
  state.charts.clear();
  const renderers = {
    overview: renderOverview,
    research: renderResearch,
    portfolio: renderPortfolio,
    trading: renderTrading,
    risk: renderRisk,
    universe: renderUniverse,
    system: renderSystem,
  };
  main.innerHTML = renderers[state.route](payload);
  updateRouteDate(payload);
  updateJobButtons();
  requestAnimationFrame(drawCharts);
}

function updateRouteDate(payload) {
  let value = "";
  if (state.route === "overview") {
    value = payload.market?.date;
    if (payload.version) versionLabel.textContent = `AI Trade v${payload.version}`;
  } else if (state.route === "research") {
    value = payload.backtest?.metadata?.end;
  } else if (state.route === "portfolio") {
    value = payload.date;
  } else if (state.route === "trading") {
    value = payload.paper_audit?.period?.[1];
  } else if (state.route === "risk") {
    value = payload.overview?.market?.date;
  } else if (state.route === "universe") {
    value = payload.date;
  } else if (state.route === "system") {
    value = payload.system?.diagnosis?.latest_market_date;
  }
  marketDate.textContent = value ? `数据日期 ${value}` : "数据日期不可用";
}

function renderOverview(data) {
  const backtest = data.research?.backtest || {};
  const walk = data.research?.walk_forward || {};
  const audit = data.paper?.audit || {};
  const targets = Object.entries(data.signal?.target_weights || {});
  const ranking = data.signal?.ranking || [];
  const warnings = data.market?.warnings || [];
  const actions = [
    actionButton("paper-run", "primary"),
    actionButton("refresh-data", "secondary"),
  ].join("");
  return `
    <div class="page-stack">
      ${pageIntro("收盘决策快照", `信号 ${data.signal?.date || "—"} · ${data.market?.universe?.active_count ?? 0} 支当日有效证券`, actions)}

      <section class="metric-strip" aria-label="核心指标">
        ${metric("模拟权益", formatMoney(data.paper?.equity), `${audit.sessions ?? 0} / ${audit.minimum_promotion_sessions ?? 60} 个前向交易日`)}
        ${metric("历史年化收益", formatPercent(backtest.cagr), "含当前成本模型", tone(backtest.cagr))}
        ${metric("历史最大回撤", formatPercent(backtest.max_drawdown), "回测观测值", tone(backtest.max_drawdown))}
        ${metric("滚动样本外 Sharpe", formatNumber(walk.oos_sharpe), `${walk.positive_segments ?? 0} / ${walk.segments ?? 0} 段收益为正`, tone(walk.oos_sharpe))}
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
        ${panelHeader("历史权益曲线", `${data.research?.period?.[0] || "—"} 至 ${data.research?.period?.[1] || "—"}`)}
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

      ${warnings.length ? `<aside class="callout warning"><strong>研究口径提示</strong><ul>${warnings.map((item) => `<li>${escapeHtml(translateWarning(item))}</li>`).join("")}</ul></aside>` : ""}
    </div>`;
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

function authorityRail(live, audit, researchGates) {
  const reconciliation = live.reconciliation || {};
  const researchPassed = Boolean(researchGates.total) && researchGates.passed === researchGates.total;
  const paperPassed = Boolean(audit.eligible_for_broker_sandbox);
  const sandboxPassed = Boolean(reconciliation.eligible);
  const liveReady = Boolean(live.live_ready);
  const steps = [
    {
      label: "历史研究",
      note: researchPassed ? "稳健性门禁通过，但结果已参与开发" : "仍有研究门禁未通过",
      complete: researchPassed,
      current: !researchPassed,
    },
    {
      label: "前向模拟",
      note: `${audit.sessions ?? 0} / ${audit.minimum_promotion_sessions ?? 60} 个独立交易日`,
      complete: paperPassed,
      current: researchPassed && !paperPassed,
    },
    {
      label: "券商沙箱",
      note: `${reconciliation.clean_sessions ?? 0} / ${reconciliation.minimum_sessions ?? 20} 次连续干净对账`,
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
  return `<div class="authority-rail">${steps.map((step) => `
    <div class="authority-step ${step.complete ? "complete" : ""} ${step.current ? "current" : ""}">
      <span class="step-marker" aria-hidden="true">${step.complete ? "✓" : step.locked ? "×" : "·"}</span>
      <div><strong>${escapeHtml(step.label)}</strong><span>${escapeHtml(step.note)}</span></div>
      ${step.complete ? statusChip("已通过", "success") : step.current ? statusChip("当前阶段", "warning") : statusChip(step.locked ? "已锁定" : "未开始", step.locked ? "danger" : "neutral")}
    </div>`).join("")}</div>`;
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
  return `<div class="check-list">${entries.length ? entries.map(([key, passed], index) => `
    <div class="check-row">
      <span>${escapeHtml(checkLabel(key, index, namespace))}</span>
      ${booleanChip(Boolean(passed))}
    </div>`).join("") : `<div class="empty-state"><p>尚无门禁结果</p></div>`}</div>`;
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

function renderPortfolio(data) {
  if (!data.initialized) {
    return `<div class="page-stack">
      ${pageIntro("模拟组合", "账户状态、持仓与待执行目标共享同一份本地账本")}
      <section class="empty-state">
        <h2>模拟账户尚未建立</h2>
        <p>建立账户后，系统会从首个完整交易日开始累计独立前向证据；已有账户不会被覆盖。</p>
        <div class="action-row">${actionButton("paper-init", "primary")}${actionButton("refresh-data", "secondary")}</div>
      </section>
    </div>`;
  }
  return `
    <div class="page-stack">
      ${pageIntro("模拟组合", `账户 ${String(data.account_id || "").slice(0, 8)} · 状态日期 ${data.date || "—"}`, actionButton("paper-run", "primary"))}
      <section class="metric-strip" aria-label="组合摘要">
        ${metric("账户权益", formatMoney(data.equity), "唯一模拟记账口径")}
        ${metric("可用现金", formatMoney(data.cash), `现金权重 ${formatPercent(data.cash_weight)}`)}
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
      <td class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong><span>${escapeHtml(instrumentName(row.symbol, row.name))}</span></td>
      <td class="numeric">${formatInteger(row.quantity)}</td>
      <td class="numeric">${formatNumber(row.price, 3)}</td>
      <td class="numeric">${formatMoney(row.market_value)}</td>
      <td class="numeric">${formatPercent(row.weight)}</td>
    </tr>`).join("") : emptyRow(5, "当前账户全部为现金")}</tbody>
  </table></div>`;
}

function pendingTable(rows) {
  return `<div class="table-wrap"><table class="data-table compact">
    <thead><tr><th>证券</th><th class="numeric">当前</th><th class="numeric">目标</th><th class="numeric">差额</th></tr></thead>
    <tbody>${rows.length ? rows.map((row) => `<tr>
      <td class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong><span>${escapeHtml(instrumentName(row.symbol, row.name))}</span></td>
      <td class="numeric">${formatPercent(row.current_weight)}</td>
      <td class="numeric">${formatPercent(row.target_weight)}</td>
      <td class="numeric ${tone(row.difference)}">${formatPercent(row.difference, true)}</td>
    </tr>`).join("") : emptyRow(4, "暂无待执行目标")}</tbody>
  </table></div>`;
}

function renderTrading(data) {
  const audit = data.paper_audit || {};
  const live = data.live || {};
  const tabs = `
    <div class="segmented" role="tablist" aria-label="执行记录">
      ${[
        ["paper", "模拟成交"],
        ["rejections", "拒单"],
        ["broker", "券商账本"],
      ].map(([key, label]) => `<button type="button" role="tab" data-trading-tab="${key}" aria-selected="${state.tradingTab === key}">${label}</button>`).join("")}
    </div>`;
  return `
    <div class="page-stack">
      ${pageIntro("交易与晋级", "订单、拒单、成交和权限检查均保留可追溯记录", [actionButton("paper-run", "primary"), actionButton("paper-audit", "secondary")].join(""))}

      <section class="split-layout">
        <article class="panel">
          ${panelHeader("前向模拟进度", audit.status ? STATUS_LABELS[audit.status] || audit.status : "尚无审计")}
          <div class="progress-block">
            <progress max="${audit.minimum_promotion_sessions || 60}" value="${audit.sessions || 0}">${audit.sessions || 0}</progress>
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
        ${panelHeader("执行账本", "最近 200 条记录", tabs)}
        ${tradingLedger(data)}
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
            <p>当前阶段不会创建、预览或发送真实订单。历史收益和模拟收益都不能单独解除此锁。</p>
          </div>
          <div class="action-row"><button class="button primary" type="button" disabled>提交真实订单</button></div>
        </article>
      </section>
    </div>`;
}

function tradingLedger(data) {
  if (state.tradingTab === "rejections") {
    const rows = data.paper_rejections || [];
    return `<div class="table-wrap"><table class="data-table">
      <thead><tr><th>日期</th><th>证券</th><th>方向</th><th>拒绝原因</th></tr></thead>
      <tbody>${rows.length ? rows.map((row) => `<tr><td>${escapeHtml(row.date)}</td><td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(row.side)}</td><td>${escapeHtml(row.reason)}</td></tr>`).join("") : emptyRow(4, "模拟账本中没有拒单记录")}</tbody>
    </table></div>`;
  }
  if (state.tradingTab === "broker") {
    const rows = data.broker_orders || [];
    return `<div class="table-wrap"><table class="data-table">
      <thead><tr><th>更新时间</th><th>客户端订单</th><th>券商订单</th><th>证券</th><th>方向</th><th class="numeric">数量</th><th>状态</th></tr></thead>
      <tbody>${rows.length ? rows.map((row) => `<tr>
        <td>${escapeHtml(row.updated_at)}</td><td class="mono">${escapeHtml(row.client_order_id)}</td><td class="mono">${escapeHtml(row.broker_order_id)}</td><td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(row.side)}</td><td class="numeric">${formatInteger(row.quantity)}</td><td>${statusChip(STATUS_LABELS[row.status] || row.status || "—", row.status === "FILLED" ? "success" : row.status === "REJECTED" ? "danger" : "neutral")}</td>
      </tr>`).join("") : emptyRow(7, "尚无券商订单；真实交易路径未配置")}</tbody>
    </table></div>`;
  }
  const rows = data.paper_trades || [];
  return `<div class="table-wrap"><table class="data-table">
    <thead><tr><th>日期</th><th>证券</th><th>方向</th><th class="numeric">数量</th><th class="numeric">价格</th><th class="numeric">名义金额</th><th class="numeric">交易成本</th></tr></thead>
    <tbody>${rows.length ? rows.map((row) => `<tr>
      <td>${escapeHtml(row.date)}</td><td class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong><span>${escapeHtml(instrumentName(row.symbol))}</span></td><td>${escapeHtml(row.side)}</td><td class="numeric">${formatInteger(row.quantity)}</td><td class="numeric">${formatNumber(row.price, 3)}</td><td class="numeric">${formatMoney(row.notional)}</td><td class="numeric">${formatMoney((finite(row.commission) || 0) + (finite(row.stamp_duty) || 0) + (finite(row.transfer_fee) || 0) + (finite(row.slippage_cost) || 0))}</td>
    </tr>`).join("") : emptyRow(7, "尚无模拟成交；目标会在下一完整交易日处理")}</tbody>
  </table></div>`;
}

function renderRisk(data) {
  const overview = data.overview || {};
  const research = data.research || {};
  const historical = overview.research?.backtest || {};
  const audit = overview.paper?.audit || {};
  const paperMetrics = audit.metrics || {};
  const bootstrapData = research.validation?.bootstrap || {};
  const riskConfig = research.configuration?.risk || {};
  return `
    <div class="page-stack">
      ${pageIntro("风险控制", "将已观察风险、前向门禁和真实交易权限分层审阅")}
      <section class="metric-strip" aria-label="风险摘要">
        ${metric("历史最大回撤", formatPercent(historical.max_drawdown), "回测观测值", tone(historical.max_drawdown))}
        ${metric("前向最大回撤", formatPercent(paperMetrics.max_drawdown), `${audit.sessions || 0} 个交易日`, tone(paperMetrics.max_drawdown))}
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
            ${detailRow("人工授权", overview.live?.authorization?.reason || "未配置", overview.live?.authorization?.valid ? "success" : "danger")}
          </div>
        </article>
      </section>

      <aside class="callout warning"><strong>风险不是一个分数</strong><p>回撤、尾部损失、参数敏感性、数据偏差、成交可实现性和账户权限分别审计。任何单项历史优势都不能替代真实券商沙箱对账。</p></aside>
    </div>`;
}

function renderUniverse(data) {
  const instruments = data.instruments || [];
  const active = instruments.filter((item) => item.active).length;
  const complete = instruments.filter((item) => item.coverage?.last === data.date).length;
  const filter = `
    <form class="filter-form" id="universe-date-form">
      <div class="field"><label for="universe-date">历史截面日期</label><input id="universe-date" name="date" type="date" value="${escapeHtml(data.date || "")}"></div>
      <button class="button secondary" type="submit">查看截面</button>
    </form>`;
  return `
    <div class="page-stack">
      ${pageIntro("证券与数据覆盖", "投资池资格、交易状态和缓存覆盖按日期复原", filter)}
      <section class="metric-strip" aria-label="数据摘要">
        ${metric("候选记录", formatInteger(data.candidate_records), `投资池 ${data.universe || "—"}`)}
        ${metric("当日有效", formatInteger(active), `最少上市 ${data.minimum_listing_days || 0} 日`)}
        ${metric("覆盖至截面", `${complete} / ${instruments.length}`, "各证券最近日线")}
        ${metric("选择方法", translateSelection(data.selection_method), "主数据指纹已记录")}
      </section>

      <section class="panel">
        ${panelHeader("证券主数据", `${data.date || "—"} 的时间点快照`)}
        <div class="table-wrap"><table class="data-table">
          <thead><tr><th>证券</th><th>资产类别</th><th>分组</th><th>上市日期</th><th>资格</th><th>交易状态</th><th class="numeric">最新收盘</th><th>覆盖区间</th></tr></thead>
          <tbody>${instruments.length ? instruments.map((item) => `<tr>
            <td class="symbol-cell"><strong>${escapeHtml(item.symbol)}</strong><span>${escapeHtml(instrumentName(item.symbol, item.name))}</span></td>
            <td>${escapeHtml(assetClassLabel(item.asset_class))}</td>
            <td>${escapeHtml(item.sector || "—")}</td>
            <td>${escapeHtml(item.listing_date || "—")}</td>
            <td>${booleanChip(Boolean(item.active), "有效", eligibilityLabel(item.eligibility_reasons))}</td>
            <td>${booleanChip(Boolean(item.tradable), item.trading_status || "可交易", item.trading_status || "不可交易")}</td>
            <td class="numeric">${formatNumber(item.latest_close, 3)}</td>
            <td class="mono">${escapeHtml(item.coverage?.first || "—")} → ${escapeHtml(item.latest_bar_date || item.coverage?.last || "—")}</td>
          </tr>`).join("") : emptyRow(8, "该日期没有候选证券")}</tbody>
        </table></div>
      </section>

      <section class="equal-layout">
        <article class="panel">
          ${panelHeader("主数据来源", translateSelection(data.selection_method))}
          <div class="path-list">
            <div class="path-row"><span>来源说明</span><code>${escapeHtml(data.provenance || "—")}</code></div>
            <div class="path-row"><span>主数据指纹</span><code>${escapeHtml(data.master_sha256 || "—")}</code></div>
          </div>
        </article>
        <aside class="callout warning"><strong>幸存者偏差提示</strong><p>静态人工投资池只记录证券何时具备资格，不等同于历史指数成分。研究结论必须保留这一限制。</p></aside>
      </section>
    </div>`;
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

function renderSystem(payload) {
  const data = payload.system || {};
  const diagnosis = data.diagnosis || {};
  const actionButtons = state.actions.map((action) => actionButton(action, action === "refresh-data" ? "primary" : "secondary")).join("");
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
  return `${(parsed / 1024 / 1024).toFixed(1)} MB`;
}

function jobsTable(jobs) {
  return `<div class="table-wrap"><table class="data-table compact">
    <thead><tr><th>任务</th><th>状态</th><th>开始时间</th><th>耗时</th><th>操作</th></tr></thead>
    <tbody>${jobs.length ? jobs.map((job) => `<tr>
      <td>${escapeHtml(JOB_LABELS[job.action] || job.action)}</td>
      <td>${jobStatusChip(job.status)}</td>
      <td>${formatDate(job.started_at || job.created_at, true)}</td>
      <td class="mono">${jobDuration(job)}</td>
      <td><div class="action-row"><button class="button secondary" type="button" data-job-view="${escapeHtml(job.id)}">查看</button>${["queued", "running"].includes(job.status) ? `<button class="button danger" type="button" data-job-cancel="${escapeHtml(job.id)}">取消</button>` : ""}</div></td>
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
      <canvas id="${escapeHtml(id)}" role="img" aria-label="${escapeHtml(summary)}"></canvas>
      <figcaption class="chart-caption">${escapeHtml(summary)}</figcaption>
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

async function startJob(action) {
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "X-AI-Trade-Token": state.token },
      body: JSON.stringify({ action }),
    });
    mergeJob(job);
    notify(`${JOB_LABELS[action] || action}已进入任务队列`);
    updateJobsUi();
  } catch (error) {
    notify(friendlyError(error.message), true);
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
  detail.innerHTML = `<div class="skeleton-line"></div>`;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    detail.innerHTML = `
      <section class="panel">
        ${panelHeader(`${JOB_LABELS[job.action] || job.action} · ${STATUS_LABELS[job.status] || job.status}`, job.return_code === null ? "进程尚未结束" : `退出码 ${job.return_code}`)}
        <pre class="job-output">${escapeHtml(job.output || "任务尚无输出")}</pre>
      </section>`;
  } catch (error) {
    detail.innerHTML = `<div class="callout danger"><strong>无法读取任务日志</strong><p>${escapeHtml(friendlyError(error.message))}</p></div>`;
  }
}

async function pollJobs() {
  try {
    const payload = await api("/api/jobs");
    const jobs = payload.jobs || [];
    for (const job of jobs) {
      const previous = state.jobStates.get(job.id);
      if (previous && ["queued", "running"].includes(previous) && ["succeeded", "failed", "cancelled"].includes(job.status)) {
        notify(`${JOB_LABELS[job.action] || job.action}${job.status === "succeeded" ? "已完成" : job.status === "failed" ? "失败" : "已取消"}`, job.status === "failed");
      }
      state.jobStates.set(job.id, job.status);
    }
    state.jobs = jobs;
    updateJobsUi();
    setConnection(true);
  } catch {
    setConnection(false);
  }
}

function mergeJob(job) {
  state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
  state.jobStates.set(job.id, job.status);
}

function updateJobsUi() {
  const active = state.jobs.filter((job) => ["queued", "running"].includes(job.status));
  jobIndicator.textContent = active.length ? `${active.length} 个任务运行中` : "无运行任务";
  jobIndicator.className = `status-chip ${active.length ? "warning" : "neutral"}`;
  const region = document.getElementById("jobs-table-region");
  if (region) region.innerHTML = jobsTable(state.jobs);
  updateJobButtons();
}

function updateJobButtons() {
  const runningActions = new Set(
    state.jobs.filter((job) => ["queued", "running"].includes(job.status)).map((job) => job.action)
  );
  for (const button of document.querySelectorAll("[data-job-action]")) {
    const busy = runningActions.has(button.dataset.jobAction);
    button.disabled = busy;
    if (busy) button.textContent = `${JOB_LABELS[button.dataset.jobAction] || button.dataset.jobAction}进行中`;
  }
}

function notify(message, error = false) {
  const region = document.getElementById("toast-region");
  const toast = document.createElement("div");
  toast.className = `toast${error ? " error" : ""}`;
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
  const action = event.target.closest("[data-job-action]");
  if (action) {
    startJob(action.dataset.jobAction);
    return;
  }
  const retry = event.target.closest("[data-retry]");
  if (retry) {
    loadRoute();
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

document.addEventListener("submit", (event) => {
  if (event.target.id !== "universe-date-form") return;
  event.preventDefault();
  const form = new FormData(event.target);
  state.universeDate = String(form.get("date") || "");
  loadRoute();
});

document.getElementById("refresh-view").addEventListener("click", loadRoute);
logoutButton.addEventListener("click", logout);

window.addEventListener("hashchange", () => {
  const next = validRoute(location.hash.slice(1)) || "overview";
  if (next === state.route) return;
  state.route = next;
  loadRoute();
});

window.addEventListener("resize", () => {
  window.clearTimeout(state.resizeTimer);
  state.resizeTimer = window.setTimeout(drawCharts, 120);
});

if (!location.hash) {
  history.replaceState(null, "", "#overview");
}
if (location.protocol === "file:") {
  renderFileProtocolNotice();
} else {
  bootstrap();
}
