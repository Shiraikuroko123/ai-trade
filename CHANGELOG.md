# Changelog

All notable changes are documented here. The project follows semantic versioning while it remains experimental.

## 0.6.0 - 2026-07-13

- Added a packaged, zero-runtime-dependency local workstation with overview, research, portfolio, trading, risk, universe, and system views.
- Added loopback-only HTTP serving with strict Host validation, per-process write tokens, cross-origin write denial, security headers, and scoped local report downloads.
- Added cancellable serialized background jobs for data refresh, research validation, paper initialization, paper execution, and paper audit.
- Added broker plugin contracts, sandbox/live environments, account/position/order/fill models, append-only intent/order/fill ledgers, and configuration-bound reconciliation evidence.
- Added fail-closed live readiness gates for current paper evidence, installed adapter/account identity, consecutive sandbox reconciliation, kill switch, expiring authorization, live configuration fingerprint, and explicit process confirmation.
- Added pre-trade checks for point-in-time universe membership, trading status, lots, ticks, daily price limits, available cash/positions, cumulative sells, order limits, and atomically reserved daily notional.
- Added market freshness and report-snapshot diagnostics, first-use recovery states, visible strategy/risk configuration, and responsive browser QA across desktop and mobile layouts.
- Hardened clean bootstrap, wheel/sdist content verification, fresh-environment installation smoke tests, and CI release checks.

## 0.5.0 - 2026-07-13

- Added a versioned security master with listing/delisting dates, dated universe memberships, trading-status periods, provenance, and a point-in-time inspection command.
- Removed the common-latest-date universe bottleneck and verified that the engine supports more than eight instruments.
- Added asset-class, risk-group, and average-amount capacity constraints to portfolio construction.
- Added date-effective ETF/stock fee schedules with separately audited commission, stamp duty, transfer fees, and slippage.
- Added suspension and price-limit order checks, sell-first risk handling, and append-only paper rejection records.
- Added an ecosystem comparison and explicit data gates for expanding from ETFs to a professional stock universe.

## 0.4.0 - 2026-07-13

- Added an append-only daily paper-equity ledger with account, configuration, and market-snapshot identifiers.
- Added `paper-audit` with ledger integrity checks and a 60-session broker-sandbox promotion gate.
- Added GitHub Actions CI, public repository documentation, security policy, issue templates, and MIT packaging metadata.
- Expanded the paper configuration fingerprint to include the universe and market-data adjustment policy.

## 0.3.0 - 2026-07-13

- Added optional covariance shrinkage and risk-parity allocation.
- Added liquidity filtering and a rebalance no-trade band.
- Added moving-block bootstrap, cost stress, parameter sensitivity, and historical-regime validation.
- Added tail-risk metrics and explicit model-selection disclosure.

## 0.2.0 - 2026-07-13

- Prevented unfinished intraday bars from entering signals and backtests.
- Added atomic validated data snapshots and SHA-256 manifests.
- Made walk-forward validation a continuous account across segment boundaries.
- Added idempotent, locked, catch-up-capable paper trading.

## 0.1.0 - 2026-07-13

- Initial ETF rotation strategy, backtest, reports, CLI, and local paper account.
