# Investment System Ecosystem

AI Trade does not attempt to merge several large repositories into one process. It uses each project as a reference for a specific layer, then keeps local contracts small, testable, and auditable.

| System | Strongest reference value | AI Trade adoption |
|---|---|---|
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean) | Event engine, universe selection, brokerage abstraction | Point-in-time universe now; explicit order lifecycle and broker adapters later |
| [Microsoft Qlib](https://github.com/microsoft/qlib) | Factor datasets, ML experiments, research workflow | Planned factor registry and point-in-time feature store |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Deterministic event state, execution and reconciliation | Deterministic paper state, append-only ledgers and rejection audit |
| [VeighNa](https://github.com/vnpy/vnpy) | China broker gateways and live operations | Gateway contract reference plus an independent QMT read-only probe; no live gateway is enabled |
| [RQAlpha](https://github.com/ricequant/rqalpha) | China-market simulation rules | Dated stock fees, lot size, suspension and price-limit rules |
| [vectorbt](https://github.com/polakowo/vectorbt) | Fast parameter and signal research | Useful future research backend, not the accounting authority |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | Data-provider abstraction | Shared daily-provider boundary, Eastmoney/Tencent snapshot routes, bounded Yahoo or token-gated Tushare reconciliation, and separate minute, fundamentals, valuation, disclosure, news, and Level-1 depth stores are implemented; additional licensed adapters remain planned |
| [kimi-stock-agent](https://github.com/dbbbbm/kimi-stock-agent) | Daily research cadence, historical review, and human operation notes | Immutable per-user research notes, persistent daily/weekly digest revisions, idempotent generation, and Windows scheduled archive runs are implemented; source accounting remains authoritative |
| [PyPortfolioOpt](https://github.com/PyPortfolio/PyPortfolioOpt) | Efficient frontier, Black-Litterman and HRP | Research candidates after simple risk budgets pass forward tests |
| [cvxportfolio](https://github.com/cvxgrp/cvxportfolio) | Multi-period optimization with costs and constraints | Reference for future institutional portfolio construction |
| [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) | Broad portfolio-risk optimization | Reference for CVaR and hierarchical risk research |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | Reinforcement-learning experiments | Isolated research only; never promoted without strict leakage and forward tests |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | Operational lifecycle, monitoring and strategy deployment | Owner-scoped scheduled research scans and alert review are implemented; automated strategy deployment and crypto execution are out of scope |
| [KLineChart](https://github.com/klinecharts/KLineChart) | Browser K-line rendering and interaction | Pinned local 10.0.0 renderer for read-only completed-session charts; AI Trade owns data, aggregation, indicators, provenance, and trading boundaries |

## Hosted Platforms Worth Comparing

- [QuantConnect](https://www.quantconnect.com/) combines hosted research, backtesting, datasets, optimization, and brokerage deployment around LEAN.
- [JoinQuant](https://www.joinquant.com/), [Ricequant](https://www.ricequant.com/), [BigQuant](https://bigquant.com/), and [MyQuant](https://www.myquant.cn/) are useful references for China-market data, factor research, simulation, and broker-facing workflows.
- [Interactive Brokers](https://www.interactivebrokers.com/) and [Alpaca](https://alpaca.markets/) are broker/API references rather than substitutes for a research and risk platform.

Hosted results depend on each provider's data license, adjustment policy, fill model, region, and account permissions. AI Trade should use them for independent comparison or future adapters, not assume that two platforms with the same strategy name have the same data semantics.

## Capability Status Matrix

The comparison below tracks the specific `kimi-stock-agent`-style workflow that is
implemented in AI Trade. “Complete” means the capability has a tested local
contract; it does not mean that it has execution authority or that it replaces the
authoritative paper and broker ledgers.

| Reference capability | Status | AI Trade boundary |
|---|---|---|
| Manual research notes and decision rationale | Complete | The Research page appends immutable, per-user notes with category, symbol, date, decision, confidence, actor, and evidence fingerprints. Corrections append a linked record; the original is retained. |
| Manual holdings, fills, and cash accounting | Separate authoritative layer | Paper and shadow-account ledgers remain the source of positions, fills, cash, and fees. The research journal can describe a review but cannot edit those ledgers. |
| Daily analysis archive | Complete within the local trust boundary | `archive-generate` materializes owner/account-scoped daily digests from validated paper reports, equity rows, and journal evidence. Repeated evidence is reused; changed evidence appends a `supersedes` revision. |
| Weekly analysis/archive | Complete within the local trust boundary | ISO-week digests expose expected versus included sessions, source fingerprints, and immutable revisions. The 18:30 Windows task can process all enabled profiles; there is no R2 digest sync. |
| Historical holdings snapshot | Partial | The Research page exposes recent ledger quantities by date; it deliberately does not reconstruct historical prices, market value, or weights, and does not create a separate snapshot ledger. |

## Research Monitoring Status

| Capability | Status | AI Trade boundary |
|---|---|---|
| Per-owner watchlists and deterministic daily rules | Complete | Configuration revisions are owner-scoped and compare-and-swap protected. Rules use completed daily bars from the validated local snapshot. |
| Multi-profile scheduled sweep | Complete on Windows | The task scans the local owner and enabled beta accounts. One malformed profile is reported as failed without stopping later profiles; an invalid beta-user store produces a warning and non-zero CLI exit. |
| Partial and failed retry | Complete | Only a fully successful owner/configuration/snapshot scan is reusable. Partial and failed attempts remain evidence and are reevaluated under a new attempt ID; staged alert/scan publication has owner-local marker recovery. |
| Historical rule/evidence binding | Complete within the local trust boundary | Historical configuration revisions and persisted snapshot evidence rederive alert rule metadata and evidence fingerprints. The hashes remain unkeyed local values. |
| Alert review lifecycle | Complete within the local trust boundary | Open, acknowledge, snooze, dismiss, reopen, and automatic scan-time unsnooze actions are append-only and state-fingerprint protected. Snooze remains scan-driven rather than a background timer. |
| Owner-scoped local notification inbox and webhook | Partial, external delivery implemented | Local inbox remains authoritative; optional HTTPS HMAC webhook delivery uses idempotency keys, bounded retries, DNS/public-address checks, and immutable outbox/attempt evidence. Remote failure never changes a scan or alert and no execution authority is granted. |
| Capacity and retention | Bounded | Each owner has hard immutable-file caps and no archive/compaction service; reaching a cap stops new writes until a future verified checkpoint format exists. |
| Host-independent tamper evidence | Not implemented | Local SHA-256 and cross-record validation are not signatures or WORM storage. A local administrator can recalculate records or delete a newest chain tail; monitoring state is not included in the R2 market-cache backup. |
| Minute/Tick/Level-2 monitoring and live execution | Partial | Historical minute evidence and observed public Level-1 five-level snapshots are separate research datasets. Real-time minute monitoring, Tick, replayable order events, full-depth/Level-2, and live execution remain unavailable; monitoring rules still use completed daily data only. |

## Assistant Research Synthesis Status

| Reference capability | Status | AI Trade boundary |
|---|---|---|
| Technical, risk, and strategy-gate role separation | Complete for deterministic closing evidence | Each perspective cites the same validated completed-bar evidence; it is a review matrix, not three autonomous agents. |
| Fundamental role | Exact-date stock integration complete | The assistant reads existing local point-in-time fundamentals and valuation evidence for the final K-line date, cites record/evidence fingerprints, excludes provisional valuation, and explicitly abstains when fewer than two directional signals exist or supportive/adverse evidence conflicts. ETFs remain unsupported. |
| Sentiment role | Coverage contract only | No complete multi-source sentiment methodology exists. Official disclosures, third-party news annotations, Dragon-Tiger rows, board breadth, capital flow, and Level-1 depth do not silently make sentiment available. |
| Perspective conflict and coverage-gap audit | Complete within the assistant record | `deterministic-perspective-audit-v1` separates real stance conflicts from missing coverage, records evidence references and manual resolution requirements, and is reconstructed during internal validation. |
| Model conclusion authority guard | Complete for the optional single configured model | A model may preserve or tighten the deterministic research conclusion. An attempted relaxation is blocked and recorded; it cannot change strategy, accounting, positions, orders, or permissions. |
| Model call governance and cache | Complete within the local trust boundary | Per-user immutable call records capture attempt/retry, Token, latency, cache, budget, cost, and failure metadata without raw prompts/responses or credentials. Single-call/UTC-day budgets and concurrency fail closed; validated public enhancements use an immutable per-user cache. |
| Multiple-model parallel analysis, weighting, voting, or judge model | Not implemented | The audit must not be described as MoA or consensus voting. Adding models would first require provider isolation, deterministic aggregation, cost and failure accounting, and conflict evidence that remains `research_only`. |

## Market Intelligence Status

| Reference capability | Status | AI Trade boundary |
|---|---|---|
| Dragon-Tiger List | Complete within one public-source trust boundary | The full Eastmoney daily report is page/count/date/schema validated and stored as immutable local revisions. It is not exchange-certified and remains `research_only`. |
| Sector rankings and breadth | Complete within one public-source trust boundary | All Eastmoney `m:90+t:2` board pages and SH/SZ/BJ benchmark breadth responses are count/date/schema/identity validated and stored as immutable local revisions. The provider-defined board universe is not presented as a licensed pure-industry taxonomy, has no independent cross-source check, and remains `research_only`. |
| Board capital flow | Complete within one public-source trust boundary | Every Eastmoney `m:90+t:2` page is count/date/schema/identity validated and stored as immutable local revisions with CNY-yuan units and explicit missing-value coverage. The board scope can overlap, order-size buckets remain provider methodology, and there is no exchange certification or independent cross-source check. |
| Point-in-time company fundamentals | Partial, stock-only evidence and assistant consumption implemented | EPS, revenue, parent net profit, ROE, growth, book value, operating cash flow per share, and gross margin retain only periods whose notice and update dates pass the completed cutoff. The assistant consumes only an exact-date local revision and abstains on sparse/conflicting signals. The source remains one third-party normalized dataset and ETFs are unsupported. |
| Official disclosures | Partial market coverage | SSE stock and CNINFO-recognized Shenzhen stock/ETF metadata and PDF links use a separate immutable store. Shanghai ETF, Beijing market, missing-master, provider, PDF-body archival, signature, and WORM gaps remain explicit. |
| Third-party news and announcements | Partial | Eastmoney news and individual announcement aggregation retain publication time, original URL, response fingerprints, immutable revisions, and explicit source failures. They are visibly separate from official disclosures; no multi-source correction model or complete hot-list/sentiment engine exists. |
| Valuation temperature | Partial, stock history implemented | Current quote fields remain available for configured instruments. Stock-only PE/PB/cash-flow/sales empirical percentiles require at least 120 positive finite observations and retain sample provenance; ETFs and insufficient histories stay unavailable, and price history is never substituted. |
| Level-1 five-level order book | Partial, observed snapshots implemented | Public five-level bids/asks, lots, normalized shares, spread, observation time, and bounded imbalance are immutable research evidence. It is not Tick, full depth, Level-2, exchange-certified, replayable, real-time monitoring, or execution data. |
| Market sentiment | Not implemented | Dragon-Tiger List records remain event evidence and do not make assistant `sentiment_coverage` available. |
| External push notification delivery | Partial | Webhook is available behind `AI_TRADE_WEBHOOK_*` environment variables with HTTPS/loopback restrictions, HMAC signatures, idempotency, retries, and immutable delivery evidence. Email, Toast, mobile push, and multi-tenant routing remain out of scope. |

## Deployment Status

| Capability | Status | AI Trade boundary |
|---|---|---|
| Windows source/wheel startup | Complete | Native startup and scheduled tasks bind loopback; owner-local bypass remains limited to one trusted machine. |
| Docker/Compose workstation | Complete on `main` | Multi-stage non-root image, read-only root filesystem, dropped capabilities, persistent named volumes with an optional bind override, health check, beta authentication, and host-loopback port publication are included. |
| Internet or multi-tenant hosting | Not implemented | There is no bundled TLS proxy, centralized identity provider, remote session revocation, tenant isolation, or public-service hardening. |

The matrix deliberately keeps human notes, accounting evidence, and strategy
decisions separate. A note saying “hold” or “reduce risk” is a record of what the
operator thought at that time, not a signal, order, or promotion fact.

## Current Layer Decisions

1. Security master is the authority for identity and point-in-time eligibility.
2. Validated market snapshots are immutable inputs to one run.
3. Strategy code emits target weights, not broker orders.
4. Portfolio constraints reduce exposures; they do not manufacture leverage to fill unused cash.
5. Execution applies date-effective fees and market rules, records rejections, and sells before buying.
6. Accounting and audit remain independent from strategy ranking.
7. Historical validation can promote a model only to future paper testing, never directly to live trading.
8. Eastmoney is a bounded primary data route, Tencent Finance is an auditable network fallback, and Yahoo Finance or environment-authorized Tushare Pro can be a short-window independent reference only; no public endpoint is treated as exchange-certified or guaranteed.
9. The market workstation renders only local validated completed snapshots. Indicator and chart controls are observations, not strategy mutations or order signals.
10. Research notes are append-only, owner-scoped evidence. They can explain a decision, but cannot mutate strategy, accounting, broker permissions, or live authority.
11. Closing archives have two layers: `/api/research/archive` is a read-time evidence projection, while `ResearchDigestStore` persists a derivative, append-only daily/weekly revision chain. The paper equity ledger and daily reports remain accounting authority; digest rows cannot promote a strategy or authorize an order.
12. Monitoring alerts are owner-scoped research prompts. Partial and failed scans remain explicit attempts, and no alert or review action changes strategy, paper accounting, broker permissions, or order authority.
13. Market breadth and board rankings are third-party closing evidence. Their provider-defined classification and counts are disclosed, and they cannot create a signal, alter a strategy, or authorize an order.
14. Board capital flow is provider-reported closing evidence. Signed amounts and direction words are retained without treating overlapping board sums as whole-market flow; the dataset cannot create a signal, alter a strategy, or authorize an order.
15. Point-in-time fundamentals require both disclosure and update dates to pass the completed cutoff and remain stock-only. Assistant analysis consumes only an already stored revision on the exact completed K-line date, cites its fingerprint, and abstains on insufficient or conflicting evidence.
16. Official disclosures and third-party news are separate evidence boundaries. A linked PDF is not an archived, signed, or WORM-preserved document body, and neither dataset creates sentiment coverage.
17. Public five-level depth is an observed Level-1 snapshot. It cannot be replayed as orders, used as licensed Level-2 data, or authorize execution.
18. Optional model calls pass immutable audit, bounded retry/concurrency, per-call and UTC-day budget, and user-isolated cache controls. These controls govern wording enhancement only and cannot authorize execution.

## What Is Deliberately Not Adopted

- Multi-agent commentary is not treated as a trading signal. A signal must reduce to deterministic data, parameters, and code.
- Reinforcement learning is not a shortcut around limited data. It has a larger overfitting surface than the current baseline.
- A current index constituent list is not used to backtest prior years.
- A fast vectorized result is not used as the final cash and order ledger without event-level reconciliation.
- Broker connectivity is not enabled until independent future sessions, broker sandbox reconciliation, credentials handling, and kill switches are complete.

The objective is a professional evidence chain, not maximum feature count. None of these systems, individually or combined, can guarantee profit.
