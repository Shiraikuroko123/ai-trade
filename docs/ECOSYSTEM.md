# Investment System Ecosystem

AI Trade does not attempt to merge several large repositories into one process. It uses each project as a reference for a specific layer, then keeps local contracts small, testable, and auditable.

## Product Version Boundary

`v1.0.0` is the complete personal research workstation and has no LLM runtime
requirement. Existing model calls are optional, budgeted wording/research
enhancements with deterministic fallback and permanent `research_only`
authority. The unreleased `v2.0.0` line has started with deterministic,
evidence-bound hypothesis pre-registration and reproducible experiment plans.
Large-model-assisted discovery and Champion/Challenger materialization remain
future work. The complete line must remain practical on ordinary personal
hardware and cannot activate a strategy, change positions, or submit orders
without explicit human review.

| System | Strongest reference value | AI Trade adoption |
|---|---|---|
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean) | Event engine, universe selection, brokerage abstraction | Point-in-time universe now; explicit order lifecycle and broker adapters later |
| [Microsoft Qlib](https://github.com/microsoft/qlib) | Factor datasets, ML experiments, research workflow | Planned factor registry and point-in-time feature store |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Deterministic event state, execution and reconciliation | Deterministic paper state, append-only ledgers and rejection audit |
| [VeighNa](https://github.com/vnpy/vnpy) | China broker gateways and live operations | Gateway contract reference plus an independent QMT read-only probe; no live gateway is enabled |
| [RQAlpha](https://github.com/ricequant/rqalpha) | China-market simulation rules | Dated stock fees, lot size, suspension and price-limit rules |
| [vectorbt](https://github.com/polakowo/vectorbt) | Fast parameter and signal research | Useful future research backend, not the accounting authority |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | Data-provider abstraction | Shared daily-provider boundary, Eastmoney/Tencent snapshot routes, bounded Yahoo or token-gated Tushare reconciliation, and separate minute, fundamentals, valuation, disclosure, news, and Level-1 depth stores are implemented; additional licensed adapters remain planned |
| [kimi-stock-agent](https://github.com/dbbbbm/kimi-stock-agent) | Daily research cadence, historical review, and human operation notes | Immutable per-user notes, daily/weekly digest revisions, monthly projections, archived-epoch review, optional R2 digest staging, and Windows scheduled archive runs are implemented; source accounting remains authoritative |
| [PyPortfolioOpt](https://github.com/PyPortfolio/PyPortfolioOpt) | Efficient frontier, Black-Litterman and HRP | Research candidates after simple risk budgets pass forward tests |
| [cvxportfolio](https://github.com/cvxgrp/cvxportfolio) | Multi-period optimization with costs and constraints | Reference for future institutional portfolio construction |
| [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) | Broad portfolio-risk optimization | Reference for CVaR and hierarchical risk research |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | Reinforcement-learning experiments | Isolated research only; never promoted without strict leakage and forward tests |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | Operational lifecycle, monitoring and strategy deployment | Owner-scoped scheduled research scans and alert review are implemented; automated strategy deployment and crypto execution are out of scope |
| [KLineChart](https://github.com/klinecharts/KLineChart) | Browser K-line rendering and interaction | Pinned local 10.0.0 renderer for read-only completed-session charts; AI Trade owns data, aggregation, indicators, provenance, and trading boundaries |

## Upstream Release Gate

The `v1.0.0` gate was rechecked on 2026-07-24 before release. Local `HEAD` and
`origin/main` were both `c9407d7ec5b470c0c38fef4d94392cf666b241a4`, and the
latest public release remained `v0.18.1`; no newer project commit had to be
integrated first.

| Upstream | Checked release/commit state | License and portability decision |
|---|---|---|
| `simonlin1212/TradingAgents-astock` | `v0.2.21`; post-release `d55820c` removes executable price/stop/size/target fields | Apache-2.0. Windowing and OpenAI-compatible provider ideas were already represented. The new removal matches AI Trade's existing prohibition on price, position, and order outputs, so no source port is required; free-text Chinese ratings do not replace strict local enums. |
| `TauricResearch/TradingAgents` | `v0.3.1` | Apache-2.0. Retry-budget and run-identity ideas informed the already shipped call-governance boundary; future-data and UTC cutoffs remain independently enforced here. |
| `virattt/ai-hedge-fund` | `v2.0.1` | MIT. The new historical `run_cycle` workflow and reduced backtest interface do not change AI Trade's research-only role contract and are not copied as a trading cycle. Protocol and caching ideas remain portable only through immutable local call/cache evidence, not overwrite-style JSON state. |
| `KylinMountain/TradingAgents-AShare` | `v0.8.1` / `fef942f`; GitHub license detection reports `NOASSERTION` | Repository terms were previously identified as PolyForm Noncommercial. Design may be studied, but source is not copied into a potentially commercial distribution unless the effective license is independently cleared. |
| `zhaoboy9692/Q-Limit` | No release; latest functional change remained Docker-related | GPL-3.0. Debate UX may be studied; frontend credential relay, free-text decisions, and weak audit patterns are not adopted. |
| `a-stock-az` / `dbbbbm/kimi-stock-agent` | Functional activity remained at 2026-01 / 2026-05 | No clear license. Requirements and workflow may be observed; source is not copied. |

The first `v2.0.0` development gate was rechecked on 2026-07-25. Microsoft
RD-Agent (`v0.8.0`, MIT) and Qlib (`v0.9.7`, MIT), ai-hedge-fund (`v2.0.1`,
MIT), and TradingAgents (`v0.3.1`, Apache-2.0) were reviewed as workflow
references only. No runtime or source was imported. RD-Agent's iterative
proposal/evaluation separation and Qlib's versioned experiment concepts are
represented through native immutable records and existing Strategy Lab gates;
ordinary installations do not acquire either project as a dependency.

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
| Weekly analysis/archive | Complete within the local trust boundary | ISO-week digests expose expected versus included sessions, source fingerprints, and immutable revisions. The 18:30 Windows task can process all enabled profiles, and the active local owner/account digest namespace can be backed up to private R2 and restored only into a new staging directory. |
| Monthly analysis view | Complete as a read-time projection | Natural-month grouping reports return, drawdown, coverage, trades, rejections, journals, latest quantities, and evidence fingerprints. It does not create a third persistent digest kind. |
| Historical holdings and account epochs | Complete for ledger quantities and read-only epoch review | The Research page exposes ledger quantities by date and validates archived paper epochs without reactivation. It deliberately does not reconstruct historical prices, market value, or weights. Archived views exclude journal entries because those records do not carry a paper-account epoch binding. |

## Research Monitoring Status

| Capability | Status | AI Trade boundary |
|---|---|---|
| Per-owner watchlists and deterministic daily rules | Complete | Configuration revisions are owner-scoped and compare-and-swap protected. Rules use completed daily bars from the validated local snapshot. |
| Multi-profile scheduled sweep | Complete on Windows | The task scans the local owner and enabled beta accounts. One malformed profile is reported as failed without stopping later profiles; an invalid beta-user store produces a warning and non-zero CLI exit. |
| Partial and failed retry | Complete | Only a fully successful owner/configuration/snapshot scan is reusable. Partial and failed attempts remain evidence and are reevaluated under a new attempt ID; staged alert/scan publication has owner-local marker recovery. |
| Historical rule/evidence binding | Complete within the local trust boundary | Historical configuration revisions and persisted snapshot evidence rederive alert rule metadata and evidence fingerprints. The hashes remain unkeyed local values. |
| Alert review lifecycle | Complete within the local trust boundary | Open, acknowledge, snooze, dismiss, reopen, and automatic scan-time unsnooze actions are append-only and state-fingerprint protected. Snooze remains scan-driven rather than a background timer. |
| Owner-scoped local inbox and external channels | Complete for Webhook, email, and host Windows Toast | The local inbox remains authoritative. HTTPS HMAC Webhook, SMTP SSL/STARTTLS email, and interactive Windows Toast have bounded delivery, immutable attempt evidence, target fingerprints, and isolated failures. Mobile push and multi-tenant routing remain unavailable. |
| Capacity and retention | Bounded | Each owner has hard immutable-file caps and no archive/compaction service; reaching a cap stops new writes until a future verified checkpoint format exists. |
| Host-independent tamper evidence | Not implemented | Local SHA-256 and cross-record validation are not signatures or WORM storage. A local administrator can recalculate records or delete a newest chain tail; monitoring state is not included in the R2 market-cache backup. |
| Minute/Tick/Level-2 monitoring and live execution | Partial | Historical minute evidence and observed public Level-1 five-level snapshots are separate research datasets. Real-time minute monitoring, Tick, replayable order events, full-depth/Level-2, and live execution remain unavailable; monitoring rules still use completed daily data only. |

## Assistant Research Synthesis Status

| Reference capability | Status | AI Trade boundary |
|---|---|---|
| Technical, risk, and strategy-gate role separation | Complete for deterministic closing evidence | Each perspective cites the same validated completed-bar evidence; it is a review matrix, not three autonomous agents. |
| Fundamental role | Exact-date stock integration complete | The assistant reads existing local point-in-time fundamentals and valuation evidence for the final K-line date, cites record/evidence fingerprints, excludes provisional valuation, and explicitly abstains when evidence is sparse, supportive/adverse evidence conflicts, or an optional Tushare field check conflicts. ETFs remain unsupported. |
| Sentiment role | Coverage contract only | No complete multi-source sentiment methodology exists. Official disclosures, third-party news annotations, Dragon-Tiger rows, board breadth, capital flow, and Level-1 depth do not silently make sentiment available. |
| Perspective conflict and coverage-gap audit | Complete within the assistant record | `deterministic-perspective-audit-v1` separates real stance conflicts from missing coverage, records evidence references and manual resolution requirements, and is reconstructed during internal validation. |
| Model conclusion authority guard | Complete for the optional single configured model | A model may preserve or tighten the deterministic research conclusion. An attempted relaxation is blocked and recorded; it cannot change strategy, accounting, positions, orders, or permissions. |
| Model call governance and cache | Complete within the local trust boundary | Per-user immutable call records capture attempt/retry, Token, latency, cache, budget, cost, and failure metadata without raw prompts/responses or credentials. Single-call/UTC-day budgets and concurrency fail closed; validated public enhancements use an immutable per-user cache. |
| Autonomous factor research and model iteration | Deterministic hypothesis core in development | The unreleased line can pre-register three bounded parameter-neighborhood hypotheses per immutable snapshot with explicit predictions, falsification, alternatives, confounds, holdout, rolling out-of-sample, cost, sensitivity, replication, and Holm-correction plans. An explicit human CLI action can materialize one fingerprint-bound Strategy Lab draft. It does not yet run the plan or call a model, and it may never approve itself, weaken gates, place orders, or require a high-performance local model. |

## Market Intelligence Status

| Reference capability | Status | AI Trade boundary |
|---|---|---|
| Dragon-Tiger List | Complete within one public-source trust boundary | The full Eastmoney daily report is page/count/date/schema validated and stored as immutable local revisions. It is not exchange-certified and remains `research_only`. |
| Sector rankings and breadth | Complete within one public-source trust boundary | All Eastmoney `m:90+t:2` board pages and SH/SZ/BJ benchmark breadth responses are count/date/schema/identity validated and stored as immutable local revisions. The provider-defined board universe is not presented as a licensed pure-industry taxonomy, has no independent cross-source check, and remains `research_only`. |
| Board capital flow | Complete within one public-source trust boundary | Every Eastmoney `m:90+t:2` page is count/date/schema/identity validated and stored as immutable local revisions with CNY-yuan units and explicit missing-value coverage. The board scope can overlap, order-size buckets remain provider methodology, and there is no exchange certification or independent cross-source check. |
| Point-in-time company fundamentals | Partial, stock-only evidence and assistant consumption implemented | Eastmoney fields retain point-in-time notice/update cutoffs. Optional Tushare common-period field checks are fingerprinted and reference-only; they never fill primary data, and conflicts force assistant abstention. ETFs are unsupported. |
| Official disclosures | Partial market coverage | SSE/CNINFO metadata, deterministic A-share event categories, PDF links, and optional bounded PDF response hashes use a separate immutable store. PDF bodies are not retained; hashes are not signatures, archives, or WORM evidence. Shanghai ETF, Beijing, and missing-master gaps remain explicit. |
| Third-party news and announcements | Partial, auditable aggregation implemented | Eastmoney and optional Tushare editorial rows retain transport/editorial identity, title clustering, calibrated time, response/content hashes, heat inputs, and revision lineage. Same-transport feeds are not independent confirmation; no complete hot-list/sentiment model exists. |
| Valuation temperature | Partial, stock history and reference checks implemented | Eastmoney stock PE/PB/cash-flow/sales percentiles require at least 120 positive finite observations. Optional exact-session Tushare PE/PB/PS checks never fill or replace primary values; ETFs and insufficient histories stay unavailable. |
| Level-1 five-level order book | Partial, observed snapshots implemented | Public five-level bids/asks, lots, normalized shares, spread, observation time, and bounded imbalance are immutable research evidence. It is not Tick, full depth, Level-2, exchange-certified, replayable, real-time monitoring, or execution data. |
| Market sentiment | Not implemented | News heat, lexicon annotations, official events, and Dragon-Tiger rows remain research evidence and do not make assistant `sentiment_coverage` available. |
| External push notification delivery | Complete for personal-computer channels | Webhook uses HTTPS/loopback restrictions, HMAC signatures, idempotency, and immutable evidence. SMTP email supports SSL/STARTTLS; Windows Toast uses an encoded host command and requires an interactive user session. Mobile push and multi-tenant routing remain out of scope. |

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
11. Closing archives have three bounded surfaces: `/api/research/archive` projects daily/weekly/monthly evidence at read time, `ResearchDigestStore` persists derivative daily/weekly revision chains, and the archived-epoch browser validates old paper namespaces without reactivation. Optional R2 digest backup restores only to staging. The paper equity ledger and daily reports remain accounting authority; no archive or digest can promote a strategy or authorize an order.
12. Monitoring alerts are owner-scoped research prompts. Partial and failed scans remain explicit attempts, and no alert or review action changes strategy, paper accounting, broker permissions, or order authority.
13. Market breadth and board rankings are third-party closing evidence. Their provider-defined classification and counts are disclosed, and they cannot create a signal, alter a strategy, or authorize an order.
14. Board capital flow is provider-reported closing evidence. Signed amounts and direction words are retained without treating overlapping board sums as whole-market flow; the dataset cannot create a signal, alter a strategy, or authorize an order.
15. Point-in-time fundamentals require both disclosure and update dates to pass the completed cutoff and remain stock-only. Optional Tushare fundamental/valuation checks are field-level reference evidence only; they never fill primary values, and a conflict forces assistant abstention.
16. Official disclosures and third-party news are separate evidence boundaries. A PDF response hash is not an archived, signed, or WORM-preserved body. News heat counts distinct transport Providers rather than same-transport editorial feeds, and neither dataset creates sentiment coverage.
17. Public five-level depth is an observed Level-1 snapshot. It cannot be replayed as orders, used as licensed Level-2 data, or authorize execution.
18. Optional model calls pass immutable audit, bounded retry/concurrency, per-call and UTC-day budget, and user-isolated cache controls. These controls govern wording enhancement only and cannot authorize execution.

## What Is Deliberately Not Adopted

- Multi-agent commentary is not treated as a trading signal. A signal must reduce to deterministic data, parameters, and code.
- Reinforcement learning is not a shortcut around limited data. It has a larger overfitting surface than the current baseline.
- A current index constituent list is not used to backtest prior years.
- A fast vectorized result is not used as the final cash and order ledger without event-level reconciliation.
- Broker connectivity is not enabled until independent future sessions, broker sandbox reconciliation, credentials handling, and kill switches are complete.

The objective is a professional evidence chain, not maximum feature count. None of these systems, individually or combined, can guarantee profit.
