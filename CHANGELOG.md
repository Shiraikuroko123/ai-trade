# Changelog

All notable changes are documented here. The project follows semantic versioning while it remains experimental.

## Unreleased

## 0.11.0 - 2026-07-14

- Added a per-user strategy lab where manual edits and deterministic local AI suggestions create immutable, schema-validated candidate versions.
- Added same-snapshot baseline comparisons with deterministic full-period, holdout, transaction-cost, drawdown, and parameter-stability gates.
- Required a separate human approval before a candidate can be exported or activated for an isolated paper profile; AI output cannot approve, activate, export, or unlock broker permissions.
- Added an audit history for candidate creation, validation, approval, paper-profile activation, and rollback, stored under Git-ignored `state/strategy_lab/`.
- Added strict same-origin, CSRF-protected strategy-lab APIs whose owner identity is derived from the authenticated session rather than request data.
- Added a responsive strategy-lab workstation with allowlisted parameter forms, baseline/candidate diffs, validation evidence, explicit confirmations, and rollback controls.
- Bound every candidate, validation, approval, export, and activation to a stable non-reused account identity, the complete configuration context, and the expected parent baseline; user-facing audit records continue to show the login username rather than the internal account ID.
- Added per-owner cross-process locking and a recoverable activation journal so immutable records, the active baseline, and rollback history cannot be silently overwritten by concurrent workstation processes or an interrupted activation.
- Added a per-account candidate ceiling, a fixed activation/rollback event budget, bounded candidate and audit summaries, and one synchronous validation slot per workstation process so repeated requests cannot create unbounded memory, disk, or backtest load.
- Bound rollback confirmations to the active candidate ID and fingerprint shown to the user, so duplicate, stale, or concurrent requests fail as HTTP 409 instead of rolling back more than one version or being reported as a service outage.
- Hardened release packaging to exclude every runtime-data directory and reject market caches, credential databases, private-key files, AI-provider keys, GitHub tokens, and bearer credentials.

## 0.10.0 - 2026-07-14

- Added an optional K-line assistant with a zero-key local mode and an explicitly `research_only` model-enhanced mode.
- Restricted assistant conclusions to `NO_ACTION`, `WATCH`, `REVIEW_CANDIDATE`, and `REDUCE_RISK`; assistant output cannot create orders, unlock broker gates, or promise returns.
- Added a Windows setup script for current-user `AI_TRADE_AI_*` environment variables. API keys are read with `SecureString`, never echoed, and are not written to repository files or assistant history.
- Restricted model endpoints to HTTPS, except for loopback-only HTTP development endpoints, and documented how to disable model configuration safely.
- Kept per-user assistant history under Git-ignored `state/assistant/`, outside Cloudflare R2 snapshots and release artifacts.
- Documented the independent clean-room implementation boundary: PA_Agent's observable research workflow was reviewed, while its AGPL source, prompts, schemas, UI, assets, and documentation text were not copied.

## 0.9.0 - 2026-07-13

- Added a workstation storage page for local-cache size, R2 namespace inventory, safe snapshot summaries, and manual inventory/backup actions.
- Added explicit `local` and `hybrid` storage preferences; hybrid mode keeps the active cache local while attempting verified R2 snapshots after successful refresh and paper workflows.
- Added persistent, cross-process accounting for AI Trade's locally observed high-level R2 Class A and Class B requests, plus configurable capacity and operation budgets with a user-defined UTC budget cycle.
- Isolated preferences, inventory caches, and operation counters by an irreversible local R2-profile fingerprint so changing accounts, buckets, or installation IDs cannot mix usage views.
- Clarified that capacity is scoped to the current installation namespace and that all displayed limits and remaining values are user budgets, not Cloudflare official account usage, billing balances, or enforced quotas.
- Kept R2 credentials and cloud coordinates in the current Windows user's environment and out of browser responses, reports, snapshots, release archives, and Git; ordinary GitHub users can configure their own independent R2 account.
- Preserved the non-secret installation identity when disabling cloud credentials so reconnecting the same R2 profile can still locate its existing snapshots.
- Fixed workstation diagnostics so an Eastmoney circuit-breaker skip is not reported as another provider request attempt.

## 0.8.0 - 2026-07-13

- Added opt-in Cloudflare R2 backups for checksummed market-cache snapshots, isolated by installation namespace, with list and connection diagnostics.
- Added staging-only verified restores that never overwrite the active cache and explicitly exclude reports, logs, account/beta state, broker credentials, and live authorization.
- Added a `cloud` installation extra, current-user setup script, cloud-storage operations guide, and release checks that reject local staging and credential files.
- Hardened Eastmoney refreshes with serial rate limiting, browser-compatible requests, cache-busting, jittered retries, cooldowns, and a refresh-scoped transport circuit breaker.
- Limited Eastmoney-specific retries independently from the Tencent fallback and stopped retrying payload, business-response, and local-validation errors that cannot be repaired by another immediate request.
- Added Tencent Finance as the default network fallback, including paged retries, overlap validation, full rebuilds after overlap mismatches such as adjustment drift, latest-session amount refinement, and explicit proxy routing.
- Extended cache manifests with per-instrument source routes, fallback reasons, provider metadata, actual common-session dates, and explicit Tencent amount-resolution evidence.
- Made multi-file cache publication crash-recoverable with an advisory lock, a durable transaction journal, immutable rollback copies, manifest-last installation, explicit committed-state verification, and fail-closed markerless recovery.
- Hardened cloud export with CSV-derived date/row verification, a sanitized manifest schema, and object-backed duplicate checks; Tencent incremental seeds and local network fallbacks now require matching manifest provenance, requested history, latest session, and hashes.
- Required the Tencent fallback module in both wheel and source-distribution release verification.
- Added explicit workstation diagnostics when current data was completed through Tencent after the Eastmoney historical endpoint degraded.

## 0.7.0 - 2026-07-13

- Added a loopback-only beta login with PBKDF2 password verifiers, in-memory sessions, session-bound CSRF tokens, strict same-origin writes, rate limiting, and explicit logout.
- Added local beta-user management plus portable whitelist export/import without plaintext passwords or sessions.
- Added an explicit `serve --owner-local` mode for a trusted workstation owner while keeping beta authentication enabled for ordinary deployments.
- Made legacy configurations fail closed into beta login and bound sessions to the current credential revision so password replacement, disabling, removal, and changed imports invalidate existing access.
- Hardened report downloads against unsafe filenames, traversal, and symbolic-link escapes.

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
