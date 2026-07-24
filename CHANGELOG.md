# Changelog

AI Trade follows semantic versioning. `v1.0.0` is the current public release;
`v0.12.0` remains the first-public-release historical baseline.

## 1.0.0 - 2026-07-24

- Promoted the completed personal workstation roadmap to the first stable
  release. Core installation and operation remain fully usable without an LLM;
  the existing optional assistant stays `research_only` and cannot alter
  strategies, accounts, orders, positions, risk gates, or broker authority.
- Added natural-month research projections with return, drawdown, session
  coverage, trades, rejections, journal counts, positions, and source
  fingerprints. Daily and ISO-week views remain available from the same
  validated paper reports and equity ledger.
- Added a read-only archived paper-epoch browser. It validates archived account
  state and matching digest namespaces without copying, merging, reactivating,
  or overwriting active state. Because journal entries do not contain a paper
  epoch binding, archived views explicitly exclude them instead of showing a
  same-date note from another epoch.
- Added optional Cloudflare R2 backup, listing, and verified staging restore for
  immutable daily/weekly research digest chains. Snapshots contain only hashed
  owner/account identities and validated revisions; restore verifies archive
  checksums, record schemas, bindings, revision chains, and fingerprints, and
  never changes active state.
- Added optional SMTP email and interactive Windows Toast delivery for unread
  monitoring notifications. Credentials remain environment-only, attempts are
  bounded and immutable, concurrent delivery is serialized, failures do not
  change the local inbox or scan result, and Docker supports email but not host
  desktop Toast.
- Expanded release verification so the new archive, epoch, cloud-digest,
  notification modules, configuration script, UI contracts, and tamper tests
  are mandatory in wheel/source artifacts.
- Defined `v2.0.0` as the future large-model research line: factor-hypothesis
  generation, reproducible experiments, leakage controls, model comparison,
  and Champion/Challenger proposals may be researched there, but no autonomous
  strategy activation or trading authority is part of `v1.0.0`.

## 0.18.1 - 2026-07-24

- Bound every new assistant result to all available wording, bull, bear, and
  judge call summaries. Saving and history reads now cross-check each summary
  against its immutable per-user call record, including role, template,
  status, cache, usage, cost, budget, content fingerprint, and UTC date.
- Exclude newly bound model records from history and next-analysis comparison
  when a referenced call record is missing, altered, malformed, cross-user,
  or replaced through a symbolic path. Legacy schema-v1 history without the
  binding remains readable, and local mode records an explicit zero-call state.
- Added a responsive workstation status for verified call bindings and legacy
  history, plus deletion, tampering, cache-hit, local-mode, and compatibility
  regression coverage. Research-only authority and all execution gates remain
  unchanged.

## 0.18.0 - 2026-07-24

- Added `auditable-bull-bear-judge-v1`: bull, bear, and judge run as three
  independently governed research roles with strict JSON contracts, stable
  argument IDs, evidence-only citations, isolated failures, and deterministic
  local fallback. Local mode performs no model I/O.
- Restricted advocates to bounded arguments and counterevidence. The judge can
  only organize agreements, conflicts, and unresolved questions; separate
  validation rejects unknown or cross-role argument references and gives the
  judge no conclusion, vote, confidence, order, position, target-price, stop,
  risk-budget, or permission fields.
- Extended the model-call governance request identity and immutable cache with
  role and prompt-template version. Each debate role receives an independent
  budget, concurrency, retry, cache, usage, cost, latency, and public failure
  audit; a failed role does not erase successful sibling roles.
- Added a responsive multi-role ledger to the AI workstation with source,
  status, arguments, counterevidence, evidence IDs, Token/cost summaries,
  failure codes, and an explicit research-only authority band.
- Added recomputable `record_sha256` integrity checks to new assistant history
  records and immutable no-replace publication. Tampered fingerprinted records
  are excluded from history and comparison; legacy schema-v1 records without a
  fingerprint remain readable.
- Rechecked the upstream release gate through 2026-07-24. The new
  `ai-hedge-fund v2.0.1` historical `run_cycle` workflow does not change this
  release's research-only role protocol and was not copied into the runtime.

## 0.17.0 - 2026-07-23

- Added optional Tushare Pro field-level reconciliation for stock fundamentals
  and exact-session PE/PB/PS valuation. Eastmoney remains the primary
  normalized record; the reference route never fills a missing field or
  replaces primary evidence, and any recorded conflict forces the assistant's
  fundamental perspective to abstain.
- Added deterministic official-disclosure title classification for lockup
  expiration, shareholder increase/decrease/change, and share pledge events.
  Bounded official PDF responses can now be SHA-256 hashed with byte counts;
  document bodies are not stored, and the hash is not an archive, signature,
  remote attestation, or WORM record.
- Extended third-party news evidence with normalized-title clustering,
  Asia/Shanghai publication-time calibration, transparent freshness and
  independent-transport breadth heat, content fingerprints, and item-level
  revision lineage. Multiple editorial feeds delivered through one Tushare
  transport are not counted as multiple independent providers.
- Preserved `sentiment_coverage=UNAVAILABLE`: heat and low-confidence
  `lexicon-v1` annotations remain auditable research metadata, not a sentiment
  model, strategy signal, order, position decision, or authority change.
- Added responsive audit columns and status summaries for independent checks,
  official events, PDF response hashes, news clustering, source identity,
  timing, heat, and revisions; added a CLI switch to skip PDF hashing when only
  official metadata is required.
- Added adapter-level response-contract tests, legacy news-chain upgrade
  coverage, distribution requirements for the new Tushare reference module,
  and release documentation for every new evidence boundary.

## 0.16.0 - 2026-07-23

- Added an AI-call governance boundary around the optional OpenAI-compatible
  wording layer. Every request now passes pre-I/O per-call and UTC-day Token
  budgets, a process concurrency cap, bounded retry policy, and per-user
  cross-process serialization before network access.
- Added immutable per-call audit records with role, model, prompt-template
  version, endpoint/evidence/request/response fingerprints, attempt-level
  latency and failure classes, retries, provider usage, conservative accounted
  Token usage, optional estimated USD cost, cache outcome, and budget state.
  API keys, endpoint URLs, raw prompts, raw model responses, and hidden
  reasoning are never stored in these records.
- Added a per-user immutable cache for schema-validated public enhancement
  output. Cache hits are independently audited and are not charged again;
  audit/cache corruption or publication failure disables model enhancement and
  preserves the deterministic local result.
- Wired stock-only point-in-time fundamentals and historical valuation
  percentiles into the assistant on the exact final K-line date. Analysis reads
  only existing local immutable records, accepts `current`/`partial` evidence,
  excludes provisional valuation, binds source fingerprints to the analysis
  snapshot, and cites stable evidence IDs.
- Added explicit fundamental-role abstention for sparse or conflicting
  directional evidence. ETFs remain unsupported, sentiment coverage remains
  `UNAVAILABLE`, and neither fundamental evidence nor model text can relax the
  deterministic conclusion or gain execution authority.
- Added governance limits and call-audit summaries to the AI workstation,
  exposed the limits through Windows and Compose configuration, and extended
  unit, UI, Docker, package-manifest, and distribution verification coverage.

## 0.15.0 - 2026-07-20

- Registered a credentialed Tushare Pro daily-bar adapter as an independent
  reference-only provider. It supports configured A-shares and ETFs, `none`
  and forward adjustment, completed-session bounds, lot volume, CNY amount
  normalization, strict response schemas, and bounded retries. The token is
  read only from `AI_TRADE_TUSHARE_TOKEN`, is never persisted, and Tushare
  cannot supply a strategy-visible snapshot or network fallback.
- Added stock-only point-in-time company fundamentals under
  `state/fundamentals`. EPS, revenue, parent net profit, weighted ROE, growth,
  book value, operating cash flow per share, and gross margin are retained
  only when both disclosure and update dates are within the completed cutoff.
  ETFs and other non-company instruments are explicitly unsupported.
- Extended valuation evidence with stock-only historical `PE_TTM`, `PE_LAR`,
  `PB_MRQ`, `PCF_OCF_TTM`, and `PS_TTM` series. Empirical percentiles require
  at least 120 positive finite completed-session observations, retain sample
  counts/date ranges and source-response fingerprints, and remain null for
  ETFs or insufficient histories. Price history is never substituted.
- Added a separate official-disclosure store under `state/disclosures`.
  Shanghai-listed stocks use SSE metadata; Shenzhen stocks and CNINFO-master
  recognized Shenzhen ETFs use the designated CNINFO platform. Official PDF
  links, publication time, per-security coverage, response fingerprints, and
  immutable revisions are retained. Shanghai ETF, Beijing, missing-master,
  and provider gaps stay explicit; PDF bodies are not WORM-archived and no
  sentiment score is inferred.
- Added immutable public Level-1 five-level order-book snapshots under
  `state/order_book`. Buy/sell ranks, CNY prices, provider lots, normalized
  shares, spread, observed time, and bounded depth imbalance are validated.
  The surface is explicitly not Tick, full-depth, Level-2, exchange-certified,
  replayable, or execution-authorized.
- Added fixed CLI/background refresh actions, bounded local-only read APIs,
  safe state-directory configuration, distribution requirements, and distinct
  responsive Market Intelligence sections for fundamentals, official
  disclosures, historical valuation percentiles, and five-level depth.
  Third-party news remains visibly separate from official disclosure evidence.
- Hardened the shared immutable evidence store so retrieval timestamps and
  revision-chain metadata do not create false revisions, while the complete
  committed record remains protected by its content fingerprint.
- Remaining gaps include AKShare/TDX/WenCai adapters, licensed minute/Tick and
  Level-2 data, full official coverage for every ETF/market, PDF body archival,
  a complete multi-source sentiment/hot-list model, isolated multi-model MoA,
  email/Toast/mobile push, R2 digest sync, signatures/WORM storage, and live
  broker execution.

## 0.14.0 - 2026-07-20

- Added a bounded Eastmoney historical minute-evidence feed with validated
  `f52`-`f55` OHLC, volume and amount fields, 1/5/15/30/60-minute local
  aggregation, completed-session/date checks, response fingerprints, and an
  immutable `state/intraday` revision chain. Wider intervals can be derived
  deterministically from a validated one-minute revision when a separately
  published interval is not present. The feed is research-only and is not a
  tick, Level-2, order-book, or real-time service.
- Added current Eastmoney valuation evidence for price, PE/PB, change, and
  market-cap fields. Requests retain raw provider scaling provenance and
  missing values remain `null`; historical PE/PB/cash-flow percentiles remain
  explicitly unavailable. Fixed the request contract so raw fields are not
  double-scaled by the formatted quote option.
- Added Eastmoney快讯 and per-security announcement evidence with publication
  timestamps, titles, summaries, security binding, allowlisted source URLs,
  per-response SHA-256 fingerprints, immutable revisions, and bounded source
  failure reporting. `lexicon-v1` labels are low-confidence research
  annotations only and do not make sentiment coverage available.
- Added optional owner-scoped HTTPS Webhook delivery for unread monitoring
  notifications. HMAC-SHA256 signatures, deterministic idempotency keys,
  redirect rejection, DNS/public-address checks, bounded retries, response
  caps, and immutable outbox/attempt evidence are enforced. Secrets never
  enter local state, logs, or release artifacts; remote failure cannot change
  alerts, scans, accounting, strategies, or execution permissions.
- Added `intraday-refresh`, `valuation-refresh`, `news-refresh`, their fixed
  background jobs, bounded read-only APIs, and responsive Market Intelligence
  sections with source, cutoff, status, and authority disclosures. Monitoring
  now reports external-delivery status, counts, attempts, and the last error
  without hiding the authoritative local inbox.
- Added cross-process locking and no-replace atomic publication for the new
  evidence stores, plus distribution checks and package-manifest entries for
  every new module. Expanded unit, API, UI, webhook, compilation, and release
  surface coverage.
- Retained the following deliberate gaps: AKShare/TDX/Tushare/WenCai provider
  registrations, tick/Level-2/order-book data, historical valuation
  percentiles, a complete multi-source sentiment or hot-list model, parallel
  multi-model MoA voting/judging, email/Toast/mobile push, R2 digest sync,
  WORM/signature archival, and live broker execution.

## 0.13.0 - 2026-07-20

- Added a multi-stage non-root Docker image and Compose deployment with a
  read-only root filesystem, dropped capabilities, health checks, explicit
  persistence mounts, and host-loopback-only port publication. The new
  container bind is accepted only when beta authentication is active;
  `--owner-local` plus container binding fails closed.
- Added `docs/DOCKER_DEPLOYMENT.md`, a credential-free environment template,
  Docker source-distribution checks, and documented rebuild/restart, Linux
  UID/GID, optional AI/R2, and non-public-hosting boundaries.

- Added a bounded independent daily-bar reconciliation for the registered
  Eastmoney/Tencent providers, then added a validated Yahoo Finance Chart
  reference adapter. The audit binds actual per-file source routes,
  overlapping completed sessions, explicit OHLCV tolerances, deviations, and
  an integrity digest to `manifest.json`; provider-declared fields keep Yahoo's
  estimated turnover out of the comparison. A fallback file is never compared
  with itself, and a reference outage never replaces the primary snapshot.
- Added `cross-check-data`, the `cross-check-data` workstation job, Data/System
  view status and per-symbol evidence, plus `docs/CROSS_SOURCE_AUDIT.md`.

- Fixed weekly research-digest finalization when the market calendar is
  unavailable: the result remains explicitly `provisional` instead of being
  marked complete from the natural calendar alone.

- Added `deterministic-perspective-audit-v1` to every new AI analysis. It
  separates technical/risk/strategy stance conflicts from unavailable
  fundamental and sentiment coverage, cites the affected evidence, records
  manual resolution requirements, and exposes an explicit `research_only`
  authority boundary.
- Added model-review invariants for deterministic, proposed, and effective
  conclusions. A model attempt to relax the local result is blocked and logged
  as an authority-guard conflict; the validator reconstructs the complete audit
  before saving. This is not multi-model voting and has no execution authority.
- Added a dense, responsive conflict-audit ledger to AI Analysis, including
  textual conflict and gap counts, model-guard state, a clear legacy-record
  rerun state, and audit status in the local history table.
- Added a third read-only closing-market intelligence dataset for Eastmoney's
  provider-defined `m:90+t:2` board capital flow. The bounded refresh validates
  all pages, stable counts, exact fields, unique board identities, quote dates,
  finite present values, and at least one main-flow value before publication;
  source nulls remain unavailable instead of becoming zero.
- Added immutable capital-flow revisions, response and evidence fingerprints,
  `capital-flow-refresh`, a fixed background job, and bounded local-only
  `GET /api/capital-flow` filters. The workstation exposes unavailable, stale,
  provisional, running, failed, and valid empty-filter states. Signed values
  are paired with direction words, and warnings state that overlapping board
  rows are not whole-market flow and order-size buckets remain uncertified,
  single-source provider methodology with no execution authority.
- Added a second read-only closing-market intelligence dataset for Eastmoney's
  provider-defined `m:90+t:2` board universe and the SH/SZ/BJ benchmark breadth
  responses. The bounded refresh validates all pages, counts, quote dates,
  benchmark identities, finite values, count relationships, and deterministic
  ordering before publishing any evidence.
- Added immutable sector/breadth revisions, response and evidence fingerprints,
  explicit third-party scope warnings, `market-breadth-refresh`, a fixed
  background job, and bounded local-only `GET /api/market-breadth` filters.
  The workstation distinguishes unavailable, stale, provisional, running,
  failed, and valid empty-filter states, preserves wide-table keyboard access,
  and uses words plus signed values instead of color-only market semantics.
- Added the first read-only closing-market intelligence dataset: Eastmoney's
  daily Dragon-Tiger List. The bounded provider validates every page, requested
  trade date, source identity, finite value, amount relationship, page count,
  and total row count before publishing any local evidence.
- Added immutable Dragon-Tiger List revisions with idempotent normalized-record
  reuse, `supersedes` links for changed same-date normalized records, strict integrity
  reads, and atomic publication below Git-ignored local state. A failed refresh
  retains the previous complete snapshot.
- Added `market-intelligence-refresh`, a fixed background job, bounded read-only
  `GET /api/market-intelligence` filters, and a dedicated dense workstation
  view with explicit unavailable, empty, stale, running, and failed states.
  This single public source is not exchange-certified, does not make sentiment
  coverage available, and has no strategy, accounting, order, promotion, or
  live-trading authority.
- Added owner-scoped research monitoring with versioned watchlists and rules,
  one-snapshot scans, immutable alert evidence, append-only review actions, and
  a Windows scheduled-task runner. Monitoring remains `research_only` and has
  no strategy, paper-ledger, broker, approval, or order authority.
- Added an owner-scoped local notification inbox that materializes alert and
  failed-scan sources idempotently with source/evidence fingerprints. Read,
  unread, and archive transitions are append-only and state-fingerprint
  protected; external email, webhook, toast, and mobile delivery remain out of
  scope.
- Made scan outcomes explicit and retryable: a fully successful scan may be
  reused for the same owner/configuration/snapshot tuple, while `partial` and
  `failed` attempts remain immutable evidence and are reevaluated under a new
  attempt ID. Alert-write failures roll back alerts created by that attempt
  before a failed scan is recorded.
- Added optimistic configuration revisions and alert-state fingerprints,
  process and file locks, bounded strict JSON records, owner binding, and
  scan/alert/action integrity checks. These local SHA-256 records detect
  accidental changes and many inconsistent edits. Historical configuration and
  persisted snapshot-evidence fields are rechecked against alert rule and
  evidence fingerprints. The hashes are not signatures or WORM storage, and a
  local administrator can rewrite or delete state. In particular, deleting the
  newest configuration/action tail (or a consistent scan/alert tail) is not
  detectable without an external durable head or backup.
- Added an atomically staged owner-local scan transaction marker and recovery.
  Hard termination between alert and parent-scan publication is reconciled on
  the next integrity-checked read by either committing the exact complete pair
  or rolling back the exact uncommitted alerts; an incomplete newest scan is
  rolled back as a unit when no review action exists. Malformed markers,
  non-tail transactions, and action-bearing recovery states fail closed.
  Writes now validate the full record envelope before publication, clean only
  reserved staging names within bounded residue limits, and use Windows
  write-through same-volume publication where available.
- Monitoring record budgets are explicit hard caps (1,000 configuration
  revisions, 2,000 scans, 5,000 alerts, 10,000 alert actions, 7,000
  notifications, and 15,000 notification actions per owner, alongside bounded
  watchlists, symbols, and rules). This release has no verified archive or
  compaction path, so reaching a cap stops new writes until a future
  checkpointed retention format exists.
- Added open, acknowledged, snoozed, and dismissed alert review states. A
  reached `snooze_until` date appends an auditable automatic unsnooze action at
  the next snapshot-backed scan; it does not create a separate timer or
  background wake-up.
- Added an immutable, per-owner research journal for manual observations,
  decision rationale, trade reviews, risk notes, strategy notes, and corrections.
  Each record binds the authenticated actor to the market snapshot and active
  strategy evidence available at write time, carries a content fingerprint, and
  remains `research_only`; it cannot mutate strategies, paper ledgers, orders,
  broker permissions, or live gates.
- Added the Research view journal workflow with category/symbol/text filters,
  bounded results, ISO-week timeline grouping, explicit empty/unavailable/running
  states, and an append-linked correction flow. Grouping alone remains distinct
  from the persistent daily/weekly digest ledger described below.
- Documented the journal storage and trust boundary in
  `docs/RESEARCH_JOURNAL.md`, including owner-hashed Git-ignored state and the
  explicit exclusion from the Cloudflare R2 market-cache backup allowlist.
- Added `ResearchArchiveProjection` and the read-only `GET /api/research/archive`
  contract. It strictly joins the active paper equity ledger, matching daily
  reports, and the authenticated owner's journal into daily summaries, ISO-week
  review rows, and ledger-quantity position snapshots. Account/date/schema,
  duplicate-key, fingerprint, and cross-source mismatches fail closed or remain
  explicit `partial` statuses; missing values are never zero-filled and historical
  prices are never inferred for snapshots.
- Added the Research-page closing-archive view with coverage dates, explicit
  evidence-status text, bounded table scrolling, and a fixed `research_only`
  authority declaration; the API retains source fingerprints for audit tooling.
  The projection is computed on demand and does not refresh providers, write
  ledgers, call a broker, upload journal data, or change execution gates.
- Added owner- and paper-account-epoch-scoped persistent daily/weekly research
  digests. Unchanged canonical evidence is idempotently reused; changed evidence
  appends an immutable revision with `supersedes` ID/fingerprint links and
  independently validated source, payload, content, account, and configuration
  fingerprints.
- Added bounded `GET /api/research/digests` and CSRF-protected
  `POST /api/research/digests/generate` routes, explicit empty/running/failure
  states, revision history, source details, and HTTP 409 capacity handling. The
  server binds owner and actor from the authenticated session; request bodies
  cannot select another owner or obtain execution authority.
- Added `archive-generate` for the local owner, one enabled beta user, or all
  enabled profiles, including daily/weekly period filters and audited
  `manual` and `scheduled` CLI labels. Browser generation rejects trigger
  injection and is always recorded as `manual`; CLI `scheduled` is an
  operator-supplied label used by the bundled runner, not authenticated scheduler
  provenance. Unfiltered generation materializes at most the newest 52 daily and
  52 weekly periods; older periods require explicit single-period `--date` or
  `--week` backfill. The Windows tasks stagger paper/audit plus local-owner digest
  at 18:10, research monitoring at 18:20, and all-profile digest generation at
  18:30, but remain independent and do not guarantee predecessor completion.
- Retained old paper-epoch digest namespaces after `paper-init --overwrite` without
  joining them to the new account. Current browser, HTTP, and CLI queries bind to
  the active paper epoch and provide no old-epoch selector, so those records are
  offline retention evidence rather than a directly browsable archive.
- Added digest release-surface checks for the wheel and source distribution and
  documented the distinction between authoritative paper evidence, the read-only
  projection, and derivative persistent digests in the architecture, ecosystem,
  README, paper operations, and research guides.
- Hardened digest publication with ledger-external same-volume staging, atomic
  first-chain publication, and explicit committed-prefix reporting when a
  post-publication verification or durability barrier fails. A pre-publication
  interruption no longer leaves an empty or temporary member that locks the
  account ledger. Top-level query status now aggregates the newest period
  revisions, and `date`/`week` queries reject conflicting kind semantics.
- Isolated expected capacity, I/O, integrity, and input failures per profile in
  `archive-generate --all-profiles`; one failed account is reported and makes
  the command non-zero without preventing later enabled profiles from running.
- Hardened archive edge cases: post-run journal notes remain visible, invalid journal
  input preserves valid paper evidence with a partial state, unbound reports cannot
  become holdings or weekly accounting, weekly returns include the first session,
  the active configuration fingerprint and trading calendar are cross-checked,
  zero-quantity holdings fail validation, and explicit table labels remain available
  to keyboard and screen-reader users.
- Added a shared daily market-data provider boundary with explicit Eastmoney
  and Tencent registrations. The configured primary and fallback can now be
  selected independently, while unsupported providers fail at startup instead
  of silently using stale local data.
- Added provider-chain and primary-circuit-breaker evidence to every market
  snapshot manifest, plus a provider capability reference in
  `docs/DATA_PROVIDERS.md`.
- The Data view now provides a bounded, read-only cross-sectional universe
  screen for every configured instrument. Momentum, annualized volatility,
  20-session average amount, trend, history readiness, freshness, provenance,
  filter fingerprints, and snapshot identity remain explicit; missing metrics
  sort last and never become zero-filled evidence.
- Persisted `PENDING_SUBMIT` intents are now surfaced as explicit
  `submission_unconfirmed` warnings with a per-order flag and aggregate count.
  The Trading view explains the manual broker-side lookup required before any
  retry; no timeout is inferred as rejection and no automatic retry is added.
- Scoped broker order events and fill fingerprints now carry the bound ledger
  scope ID. A copied or replaced scope manifest cannot silently reassign old
  order evidence, and unscoped writers cannot append into a scoped ledger.
- Reconciliation audits now expose a deterministic fingerprint and validated
  row counts for the complete ledger. Formal evidence keeps one fixed CNY 0.01
  cash tolerance so diagnostic comparisons cannot silently disagree with the
  promotion ledger.
- Broker lifecycle recovery now rejects contradictory snapshots that share an
  order timestamp and rejects any post-terminal change to filled quantity or
  average fill price. Concurrent polling therefore fails closed instead of
  selecting a state from file order.
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

## 0.12.1 - 2026-07-18

- Promoted the universe screen contract to schema version 2 with explicit metric
  definitions, completed-session cutoff, source-provider distributions, fallback
  disclosure, coverage summaries, and lag summaries.
- Published the screen read timestamp after all derived metrics finish, keeping
  page-read time distinct from the market data date.
- Added source-provider, coverage-percent, and cutoff columns to the Data view,
  plus quality summaries and an expandable formula/measurement section.
- Added bounded result-count control, actionable unavailable/empty states, and
  live warning announcements for stale, missing, insufficient, or fallback data.
- Routed K-line and market-direction colors through the shared CSS semantic token
  bridge so chart, table, and status colors remain auditable and theme-consistent.
- Rechecked the dense table, mobile reflow, keyboard scrolling, and chart text
  summaries without changing trading, backtest, risk, or broker execution logic.
- Removed the page-level minimum width so the workstation remains readable at
  200% zoom; wide evidence tables and the market pulse retain their own
  intentional internal scrolling.

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
