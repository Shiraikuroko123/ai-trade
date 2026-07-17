# Changelog

AI Trade follows semantic versioning while the project remains experimental. `v0.12.0` is the first public release; earlier development tags and release notes were consolidated into this baseline.

## Unreleased

- The workstation now separates the common completed market date from the
  server-generated page-read time, treats a missing signal as an explicit cash
  state instead of a date mismatch, and locks background-job controls while a
  submission is in flight. Repeated log controls have specific accessible
  names and disclosure widgets keep visible keyboard focus.
- Portfolio responses now retain validated paper-ledger quantities, cash, and
  recorded equity when the market cache is unavailable. Price, market-value,
  weight, and target-difference fields stay explicitly unavailable until a
  fresh snapshot is restored, instead of failing the whole portfolio view.
- Overview and portfolio payloads now expose the completed-session cutoff,
  common latest market date, calendar-day lag, manifest availability, and
  source provenance so data age and trust can be audited without opening raw
  diagnostics. The market pulse also reflects stale snapshots.
- Portfolio valuation now distinguishes current, stale, review-required, and
  partial snapshots. Missing position bars remain explicit unavailable values
  with a recovery action instead of becoming zero-price estimates, and the UI
  uses the common latest session date in its top-level date label.
- The local HTTP handler treats browser aborts and reset sockets as normal
  disconnects, preventing harmless reloads from producing worker tracebacks.
- Strategy-lab candidate, validation, approval, monitoring, active-state, and
  transition records now use bounded duplicate-key-rejecting JSON with explicit
  top-level field allowlists. Atomic writes enforce the same limits before
  publication, so malformed or oversized research evidence fails closed.
- Market cache manifests and cache-transaction markers now use bounded,
  duplicate-key-rejecting JSON in the reader, chart, and Eastmoney/Tencent
  fallback paths. Ambiguous or oversized snapshot metadata cannot select or
  recover an active data set.
- Cloud preferences and inventory, assistant history, dashboard reports, cloud
  snapshot manifests, and model responses now use bounded duplicate-key-
  rejecting JSON at their local or remote input boundaries. Eastmoney responses
  are size-limited before decoding, keeping refresh and cloud restore failures
  explicit instead of partially accepting ambiguous metadata.
- Cloud-backup job events and assistant model JSON fragments now reject duplicate
  keys before status or research evidence is accepted.
- Paper account state now uses bounded duplicate-key-rejecting JSON and validates
  an exact versioned schema, canonical identity and dates, finite balances,
  positions, targets, and counters before simulation, audit, or rendering.
- Paper state and daily reports now use fsynced same-directory temporary files
  and atomic replacement, preserving the prior complete file if replacement fails.
- Repeated paper runs now validate the cached daily report against the authoritative
  account state and fall back to a safe summary when the report is malformed,
  oversized, ambiguous, or tampered.
- Sandbox reconciliation rejects future dates and counts position-bound rows
  only through the verified completed market date. Later valid rows remain
  visible as pending evidence instead of prematurely advancing live readiness.
- Position and issue JSON embedded in formal reconciliation CSV rows now rejects
  duplicate object keys, preventing ambiguous nested evidence from qualifying
  even when a last-key-wins parser would reproduce the stored content hash.
- Distribution verification now requires the strict JSON parser and broker
  runtime-validation modules in both wheel and source artifacts, so packaging
  regressions cannot silently omit these fail-closed boundaries.
- Live authorization now rejects future approval timestamps and expiry windows
  that do not follow their approval time, preventing internally inconsistent
  authorization records from satisfying the live-readiness gate.
- Active beta-user stores, portable user exports, and browser JSON writes now
  reject duplicate object keys. Versioned user/password records require exact
  fields, so unknown or accidentally embedded plaintext fields fail closed.
- Main configuration and point-in-time security-master JSON now reject duplicate
  keys and use explicit 5 MiB/32 MiB bounds, preventing ambiguous broker, risk,
  universe, or data-source settings without limiting a professional universe.
- Live routing now refreshes the configured account identity, available cash,
  sellable positions, and trading-session health after approval/ledger I/O. The
  authorization and kill switch are then checked last before broker submission.
- Live authorization, one-time batch approval, and broker ledger-scope JSON
  files now use a bounded strict UTF-8 parser. Oversized files, invalid encoding,
  and duplicate object keys fail closed instead of creating ambiguous controls.
- Trading no longer returns raw broker order CSV rows. Its order table comes
  from the lifecycle reducer and its fill table now comes from a validated fill
  projection; an invalid order/fill schema or fingerprint cannot be rendered as
  broker evidence.
- Overview and Trading browser payloads no longer expose the configured broker
  account ID or absolute kill-switch and batch-approval paths. Internal live
  gates retain exact account/path binding while the UI receives only status,
  capability, limit, fingerprint, and reconciliation projections.
- Broker adapter/account identities and runtime account, position, order, fill,
  currency, and message text now share explicit length and canonical-text
  boundaries; leading/trailing whitespace, non-printing Unicode, control
  characters, DEL, and oversized adapter output fail closed before persistence
  or rendering.
- Live routing now validates the complete broker submission collection and its
  exact request identities before appending any returned snapshot. A malformed
  or changed response leaves only the pre-I/O reservation for manual recovery.
- New qualifying `v3_` reconciliation rows bind canonical expected and broker
  position maps as well as non-negative cash, and recompute the complete issue
  list at write and audit time. Identity-only and cash-only `v2_` rows remain
  readable and idempotent but no longer count toward promotion.
- Read-only broker probes now share strict runtime validation with live routing
  and additionally reject malformed or duplicate order/fill collections and
  truthy non-boolean fee-completeness flags before rendering observations.
- Broker discovery now rejects duplicate entry-point names, malformed typed
  capability declarations, and factory identity or environment drift at the
  registry boundary.
- Live routing now validates broker account, position, and health objects at
  runtime with strict booleans, numeric types, identities, and bounds before
  consuming approval or reserving an intent.
- Live `PENDING_SUBMIT` reservations now embed a canonical approval ID and exact
  batch fingerprint inside the content-bound order event, linking durable
  intent evidence to the consumed one-time approval without exposing an account.
- Live validation and one-time batch fingerprints now reject arbitrary order
  metadata until a bounded adapter-independent schema exists, preventing
  unapproved routing semantics from bypassing exact-batch review.
- Broker control and evidence paths are now compared after resolution against
  the configuration root, preventing relative or absolute path aliases from
  bypassing file-isolation checks.
- Added full canonical SHA-256 fingerprints to new broker fill rows, with
  strict header validation and read-time content verification. Legacy fill
  ledgers remain readable and retain their original schema during exact or
  incremental retries; they are never silently migrated into stronger evidence.
- Added an atomic broker-ledger scope manifest binding lifecycle evidence to the
  adapter, a non-plaintext account reference, runtime environment, live
  configuration fingerprint, and exact ledger paths. Scoped writes reject
  legacy or mismatched ledgers before broker I/O; legacy ledgers remain readable
  but visibly non-authoritative, and the Trading view exposes the scope state.
- Established the reconciliation-ledger foundation with canonical content
  fingerprints, strict whole-ledger validation, logical-session conflict
  detection, in-process and operating-system writer serialization, and atomic
  CSV publication; the newer position-bound format above is now authoritative.
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
