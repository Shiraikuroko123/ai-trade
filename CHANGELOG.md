# Changelog

AI Trade follows semantic versioning while the project remains experimental. `v0.12.0` is the first public release; earlier development tags and release notes were consolidated into this baseline.

## Unreleased

- Hardened qualifying sandbox reconciliation evidence with canonical `v2_`
  content fingerprints, strict whole-ledger validation, logical-session conflict
  detection, in-process and operating-system writer serialization, and atomic
  CSV publication. Legacy identity-only records remain readable and idempotent
  but no longer count toward the live-promotion gate.
- Hardened broker-ledger durability with serialized in-process writers,
  atomic single-ledger publication, injected-failure recovery tests, and an
  independent transition-matrix contract. Order intents now use China-session
  midnight, preventing pre-UTC-midnight broker responses from sorting before
  their local reservation.
- Added a restart-safe broker order lifecycle reducer with legal transition and
  immutable-identity checks, partial-fill aggregation, cancellation-race
  handling, event-time recovery for late observations, cross-ledger fill
  verification, canonical v2 event fingerprints with legacy-ledger
  compatibility, operating-system locks, and a read-only Trading view.
  Lifecycle health never counts as reconciliation or trading authority.
- Added a per-user shadow-account CSV review with an exact canonical schema,
  immutable row fingerprints, idempotent file imports, overlap/conflict
  detection, and behavior, price, and trade-allocation comparisons against the
  current paper ledger. Source files are not retained and results never count as
  broker reconciliation or trading authority.
- Added post-activation strategy lifecycle governance with immutable rolling
  decay evidence, activation-reference and parent-baseline comparisons,
  human-only suspension/resumption/retirement, exact state-bound requests, and
  no automatic strategy or broker-authority changes.
- Added strict versioned live mandates with explicit symbol/side allowlists,
  order and daily ceilings, atomic daily order-count enforcement, and short-lived
  one-time approvals bound to the exact order batch. Consumed approvals remain
  local audit records and cannot authorize retries.
- Added machine-readable broker capability declarations with explicit access
  levels, environments, and operation allowlists. Missing declarations,
  undeclared operations, and runtime metadata drift now fail closed before
  broker method calls.
- Added an independently packaged QMT/xtquant read-only adapter for local account, position, cancelable-order, and same-day fill observation. It rejects live mode, submission, and cancellation, and does not bundle broker binaries or credentials.
- Added `broker-list`, `broker-probe`, and `broker-compare` CLI diagnostics. Read-only comparisons are masked, non-mutating, and never count toward qualifying sandbox reconciliation evidence.
- Added fail-closed QMT account binding, quantity/status validation, unknown-order preservation, fee-completeness warnings, focused tests, and local configuration guidance.

## 0.12.0 - 2026-07-14

- Added a local, loopback-only investment workstation covering research, backtesting, walk-forward and robustness validation, paper accounting, risk review, report downloads, beta access, storage controls, and broker-readiness diagnostics.
- Added an independent, read-only market workstation with responsive daily, weekly, and monthly OHLCV charts, volume, MA/EMA/BOLL overlays, MACD/KDJ/RSI/Wilder ATR panes, zoom, crosshair inspection, and current-paper-account trade markers.
- Added a strictly validated market-chart API that reads only completed local cache snapshots, performs deterministic calendar aggregation, and returns bounded bars, summaries, provenance, snapshot fingerprints, and explicit stale or missing diagnostics without mutating cache or account state.
- Vendored KLineChart 10.0.0 as a version-pinned local browser asset with upstream notices, licenses, provenance, fixed SHA-256 checks, and wheel/source-distribution coverage; no CDN is required.
- Added a versioned security master and dynamic selector sourced from point-in-time listing, membership, and trading-status records rather than a hard-coded eight-symbol UI limit.
- Added Eastmoney primary data access with bounded retries, a refresh-scoped transport circuit breaker, Tencent Finance network fallback, validated local fallback, per-instrument provenance, and recoverable atomic cache publication.
- Added deterministic event backtesting, continuous walk-forward validation, moving-block bootstrap, cost and parameter stress, historical regimes, dated China-market fees, lot/tick/price-limit rules, liquidity/capacity limits, and append-only rejection evidence.
- Added locked, idempotent paper trading with catch-up processing, configuration-bound account epochs, trade/rejection/equity ledgers, independent audit, and a 60-session promotion gate that can lead only to broker-sandbox review.
- Added a per-user strategy lab where manual edits and deterministic local suggestions create immutable, schema-validated candidates, run same-snapshot baseline comparisons, require explicit human approval, and export only isolated broker-disabled paper profiles with activation and rollback history.
- Added an optional K-line assistant with zero-key local analysis and an explicitly `research_only` model-enhanced mode. Its closed conclusions cannot create orders, mutate portfolios, approve strategies, unlock broker gates, or promise returns.
- Added optional private Cloudflare R2 market-cache snapshots, verified staging-only restore, local/hybrid storage preferences, namespace inventory, and locally observed user-configurable capacity/Class A/Class B budgets. Every installation supplies its own credentials.
- Added a tested broker plugin contract, order intent/order/fill ledgers, reconciliation evidence, pre-trade controls, kill switch, expiring authorization, and fail-closed live routing. No broker-specific adapter ships and real-money execution remains unavailable.
- Added release hardening for wheel and source distributions, including runtime-data and credential rejection, vendored-asset integrity checks, fresh-install smoke tests, CI, security policy, contribution guidance, and local-only beginner tutorials.

Historical returns do not predict future results. This release is for research and paper trading, not investment advice or live-trading authorization.
