# Architecture

```text
security master + dated memberships + trading status
                         |
                         v
Eastmoney primary -> Tencent network fallback -> recent validated cache
                         |
                         v
              validated snapshot + manifest
                         |
                         v
point-in-time universe -> signal factors -> portfolio constraints
                         |                     |
                         +----------+----------+
                                    v
                         next-session order intent
                                    |
                                    v
                 market rules + dated fees + sell-first execution
                                    |
                    +---------------+---------------+
                    |                               |
                    v                               v
           historical backtest              local paper account
                    |                               |
                    v                               v
          walk-forward + stress       equity/trade/rejection ledgers
                    |                               |
                    +---------------+---------------+
                                    v
                         auditable reports
                                    |
                                    v
                    loopback-only local workstation
```

The market refresh route is deterministic and auditable. The configured primary
and fallback are resolved through the shared provider boundary described in
`docs/DATA_PROVIDERS.md`. The release registers Eastmoney and Tencent Finance
for daily bars; a refresh-scoped transport circuit breaker skips repeated
primary-provider attempts only when that provider classifies the failure as
provider-wide. Only when all configured network routes fail may the refresh
reuse a locally validated cache inside the configured freshness window.

Every candidate file is staged and validated before publication. The cache manifest distinguishes the requested completed-session cutoff from the latest trading session shared by the required active instruments, and records each instrument's route, errors, fallback reason, latest session, row count, and SHA-256. Tencent incremental refreshes and local network fallbacks accept a seed only when the active manifest, provider, adjustment, requested history start, latest session, row count, source route, and file hash match; entries record the retained seed source/hash in addition to refresh mode, proxy mode, page and overlap counts, and amount-quality fields.

Publication is a recoverable cross-file transaction. A same-volume transaction directory keeps immutable copies of the previous files and a durable journal, while an atomic root marker mirrors the current phase. All CSV files are replaced before `manifest.json`; after validating the installed files, the journal and marker record an explicit `committed` state with every target hash. `MarketData` holds the transaction lock while loading. Recovery keeps a complete committed snapshot, rolls a mixed install back, reconstructs state from the journal when the root marker is missing, and preserves backups while failing closed if the remaining transaction is ambiguous.

Tencent's currently observed historical kline amount field uses two decimal places in units of CNY 10,000, giving CNY 100 resolution. CNY 50 is only the nominal error bound if that quantization is round-to-nearest; it is not a provider contract. When the latest kline can be reconciled exactly with the separate quote response, the quote amount replaces that one value and the manifest records the override. This provenance supports review; it does not turn a public provider response into exchange-certified data.

The workstation has two explicit local access profiles. Beta mode protects every data API, job, and report with a password-authenticated in-memory session and a session-bound CSRF token. Owner-local mode deliberately bypasses that login for one trusted loopback-only process; it is a convenience profile, not a remotely enforceable license.

## Read-only Market Workstation Boundary

```text
validated manifest + instrument CSV + paper trade ledger
                         |
                         v
             non-mutating authenticated GET
                         |
        bounded day bars + deterministic week/month bars
                         |
                         v
       locally vendored KLineChart 10.0.0 in the browser
                         |
                         X  no refresh, strategy write, ledger write,
                            cloud upload, broker call, or gate mutation
```

The market-chart route accepts only configured instruments, `day`/`week`/`month`, allowlisted indicators, and bounded bar counts. It loads one validated completed snapshot under the cache transaction lock, derives weekly and monthly OHLCV by calendar period using the last real session date, and returns explicit source, adjustment, completion cutoff, manifest hash, file hash, stale state, and missing-data diagnostics. The overview and portfolio projections expose the same completed-session cutoff, common latest date, lag, manifest availability, and source provenance so a dashboard user can audit freshness without opening raw diagnostics. A portfolio response keeps validated paper-ledger quantities and recorded equity visible when market valuation is unavailable, while leaving price-derived fields explicitly unavailable; it also marks stale, review-required, and partial valuations and never turns a missing close into a zero-price estimate. A GET never invokes a provider, publishes cache files, changes strategy-lab state, writes paper accounting, or creates an order intent.

KLineChart 10.0.0 is a pinned local distribution asset, not a CDN dependency. Its minified bundle, license, notices, provenance record, and fixed SHA-256 are checked by release verification. Price, volume, overlays, oscillators, crosshair state, zoom, and paper markers are browser projections of returned evidence; changing them is not a strategy or trading action.

The read-only `/api/universe/screen` projection derives liquidity, momentum, annualized volatility, trend, and history readiness for every configured instrument from the same completed snapshot. Its bounded query contract returns a filter fingerprint, snapshot ID, data-status counts, source route, and explicit empty/unavailable states. It never refreshes providers, changes strategy settings, writes paper ledgers, or grants broker authority. The UI keeps the base security-master eligibility separate from the screen result so a research filter cannot silently redefine the tradable universe.

## Closing Market Intelligence Boundary

```text
explicit completed trade date + bounded Eastmoney report pages
                          |
                          v
       date/schema/count/identity/amount validation
                          |
                          v
        immutable Dragon-Tiger List revision chain
                          |
                          v
              non-mutating filtered GET
                          |
                          X  no sentiment inference, strategy write,
                             ledger write, order, promotion, or live authority
```

`data/market_intelligence.py` owns the first normalized event-data boundary. A
refresh date comes from an explicit CLI argument or the latest locally validated
market snapshot. The Eastmoney provider reads every bounded page of
`RPT_DAILYBILLBOARD_DETAILSNEW` and rejects an incomplete envelope, mismatched
date or count, duplicate source identity, missing field, non-finite value, or
inconsistent buy/sell/net amount before publication. GET routes never invoke
the provider.

An identical normalized record set for the same date is idempotently reused.
Changed normalized records are published as another immutable revision linked
through `supersedes`; atomic
publication preserves the previous complete revision on failure. Source,
response, normalized evidence, and record fingerprints provide local integrity
evidence, not signatures or exchange certification. Records live below
Git-ignored `state/market_intelligence/`, outside the release and R2 market-cache
allowlists. The UI and API retain fixed `research_only` authority, and this
single event report does not change assistant fundamental or sentiment coverage.

## Research Monitoring Boundary

```text
owner-scoped watchlists + versioned deterministic rules
                         |
                         v
             one validated completed snapshot
                         |
                         v
      immutable scan + alert evidence + review-action chain
                         |
                         X  no strategy write, paper-ledger write,
                            broker call, approval, or order authority
```

`monitoring.py` stores each owner below
`state/monitoring/users/<sha256-owner>/`. Configuration changes create numbered
revisions linked to the prior revision fingerprint. Scans bind the owner,
configuration revision and fingerprint, snapshot, completed-session cutoff,
rule observations, exclusions, suppression reasons, and triggered alert IDs.
Alert records retain the deterministic rule fingerprint, source-file hash, and
evidence fingerprint. Review actions append state transitions instead of editing
the original alert. Authenticated HTTP writes derive owner and actor from the
server session, require same-origin CSRF protection, and use configuration or
alert-state compare-and-swap tokens. Per-owner re-entrant process locks and an
operating-system file lock serialize readers and writers. Before publishing a
scan with new alerts, the owner writes an atomically staged
`.scan-transaction.json` marker from the private `.staging/` directory. Recovery
on the next integrity-checked read verifies the exact scan and alert
fingerprints, commits a complete pair, or removes the marker's exact
uncommitted alerts. If a newest scan is present but its alert set is incomplete,
the scan and remaining transaction alerts are rolled back together unless an
action has already been appended. A malformed or mismatched marker, a non-tail
scan, or an action-bearing transaction fails closed.
This protocol covers interrupted processes after a filesystem publish; it is
not a cross-platform guarantee against sudden power loss, storage-controller
loss, or privileged deletion. Windows uses write-through same-volume publication
when available, while local SHA-256 values remain unkeyed tamper evidence.

A successful scan is reusable only for the same owner, configuration, and
snapshot. `partial` means at least one rule was explicitly excluded while other
evidence may still be valid; `failed` means the snapshot, rule evaluation, or
durable publication could not complete. Partial and failed attempts are never
cache hits: they remain immutable and a retry gets a new attempt ID. Alerts
created before an alert-publication failure are rolled back before the failed
scan is written. A failed attempt does not replace the latest valid rule state
used for transition and cooldown decisions. One malformed owner in an
all-profile sweep is reported as failed without stopping later owners. Normal
error paths use compensating rollback, while the transaction marker makes a
hard process termination between alert and parent-scan publication recoverable:
the next integrity-checked read either commits the complete pair or rolls back
the exact uncommitted alerts. Marker tampering or an incomplete/mismatched pair
still fails closed.

`snooze_until` is a review date, not a timer service. When a later scan has a
validated completed-session cutoff on or after that date, the scanner appends an
automatic `unsnooze` action before evaluating rules. Merely loading the page or
leaving the workstation stopped does not wake or reopen an alert.

The monitoring fingerprints are unkeyed local SHA-256 checks, not digital
signatures, remote attestations, or append-only storage enforced outside the
host. Strict schemas, owner binding, content fingerprints, contiguous action
chains, configuration parent links, and scan/alert cross-references detect
accidental corruption and many inconsistent edits. Scan/alert identifiers and
selected configuration/snapshot fields are cross-checked. Historical
configuration revisions are loaded to rederive rule fingerprints and alert
metadata, and persisted snapshot evidence fields are used to rederive each
alert evidence fingerprint. These are still unkeyed local hashes: a process
with write access to the state directory,
especially the local Windows administrator, can still rewrite records and
recalculate fingerprints or delete records. There is no durable external
collection head, so deleting the newest configuration or action, or deleting a
consistent tail of scans and their alerts, can cause an apparently valid
rollback. A lone alert deletion is rejected while its parent scan still
references it, but a privileged operator can rewrite the related tail together.
Monitoring state is excluded from the R2 market-cache allowlist.
Operators who require stronger history must restrict filesystem ACLs and keep
an independent versioned backup or signed/WORM audit export.

Monitoring deliberately uses bounded immutable files rather than archive
compaction: 1,000 configuration revisions, 2,000 scans, 5,000 alerts, and
10,000 alert actions per owner, with additional watchlist, symbol, and rule
limits. Dismissal does not reclaim action capacity, and reaching a cap stops
new writes until a future verified checkpoint/archive format is introduced.

## Research-only Assistant Boundary

```text
validated completed K-line snapshot
                 |
                 v
       bounded assistant input
          |             |
          v             v
    local analysis   configured model API
          |             |
          +------+------+
                 v
        research_only assessment
        NO_ACTION | WATCH
        REVIEW_CANDIDATE | REDUCE_RISK
                 |
                 +------> state/assistant/ local history
                 |
                 X  no order intent, broker call, or gate mutation
```

The assistant is a parallel research projection from the validated market snapshot, not a stage in the order pipeline. Local mode requires no API key. Model-enhanced mode uses only the current Windows user's `AI_TRADE_AI_BASE_URL`, `AI_TRADE_AI_MODEL`, `AI_TRADE_AI_API_KEY`, and `AI_TRADE_AI_TIMEOUT_SECONDS`; a remote Base URL must use HTTPS, while plain HTTP is accepted only on a loopback host. The API key is not part of an assistant request payload, result record, report, cloud snapshot, browser response, or release artifact except where the upstream protocol carries it as an authentication header.

Assistant conclusions are a closed enum. `REVIEW_CANDIDATE` requests human research review, and `REDUCE_RISK` requests human exposure review; neither is an order side or approval. Enforcement outside the model fixes `authority="research_only"` and prevents assistant data from creating an order intent, target position, entry/exit price, paper-promotion fact, sandbox reconciliation, live authorization, or changed kill-switch state. Provider failures, stale inputs, malformed responses, and unsupported conclusions fail closed.

Per-user assistant records are stored under `state/assistant/`. The repository-wide `state/*` ignore rule excludes them from Git, the R2 exporter can read only its market-cache allowlist, and release verification rejects every `state/` member. Assistant history is therefore local operational state rather than a portable report or cloud backup.

## Append-only Research Journal Boundary

```text
completed market snapshot + active strategy metadata
                         |
                         v
                 human journal draft
                         |
                         v
             server-bound owner and actor
                         |
                         v
            immutable local journal record
                         |
                         X  no strategy mutation, paper-account write,
                            order intent, broker call, or live authority
```

The research journal is a parallel evidence layer for human observations,
decision rationale, trade reviews, risk notes, strategy notes, and weekly review
notes. It is not an input stage of the signal or order pipeline. The append route
accepts only an allowlisted draft; the server supplies the authenticated owner
identity and human-readable actor. `authority` is fixed to `research_only`, with
`execution_authorized`, `strategy_changed`, `paper_account_changed`, and
`broker_permissions_changed` all set to `false`. A journal conclusion therefore
cannot create an order, alter a strategy candidate, write a paper or shadow
ledger, consume a mandate approval, or change a kill switch.

Each record captures the research date and ISO week start, a category, optional
configured symbol, bounded title/note, optional closed decision enum, and a
content-bound SHA-256 fingerprint. It also records the market snapshot date and
fingerprint when a completed snapshot is available, and the active strategy
candidate/fingerprint/lifecycle state when strategy evidence is available. An
explicit `available=false` object is stored when either evidence source cannot be
read; missing evidence is never silently represented as a current or zero value.

Records use create-once JSON files under
`state/research_journal/users/<sha256-owner>/entries/`. Per-owner in-process and
operating-system locks serialize writes; the file is staged, flushed, and
atomically created, and duplicate JSON keys, unknown fields, owner mismatches,
or fingerprint changes fail closed on read. There is no edit or delete API. A
correction appends a new record with `correction_of` pointing to the original,
leaving the original evidence intact. The store caps one owner at 2,000 records
and keeps the browser query bounded. The UI groups records by week and can build a
separate read-only closing-archive projection, but that projection is computed on
demand and is not an automated report or a new authoritative ledger.

The journal root is Git-ignored local state and is excluded from release
artifacts. The optional R2 exporter has an explicit market-cache allowlist and
cannot read `state/research_journal/` (or any other `state/` member). Consequently
R2 capacity and operation counters do not include journal records, and a cloud
cache restore cannot restore or mutate a user's journal. See
`docs/RESEARCH_JOURNAL.md` for the user workflow and request contract.

## Read-only Closing Archive Projection

```text
paper_equity.csv  +  paper_YYYYMMDD.json  +  current owner's journal entries
          | strict schema, account/date/fingerprint cross-checks
          v
  daily summaries + ISO weekly review + ledger-quantity position snapshots
          |
          X no provider refresh, strategy write, ledger write, broker call,
            order intent, permission change, or cloud upload
```

`ResearchArchiveProjection` is a read-only evidence join. It accepts only the active
paper `account_id` and its validated configuration fingerprint, validates the bounded
CSV ledger and each matching daily report, and reads journal entries through the
authenticated owner's scope. A daily row is
marked `current` only when the ledger and report agree on the key accounting and
snapshot fields. Missing reports, reports without a bound ledger row, journal-only
dates, malformed input, and cross-source mismatches remain explicit statuses rather
than being converted to zeros or silently dropped. The aggregate response reports
`current`, `partial`, `empty`, or `unavailable` and includes source fingerprints and
recovery actions.

The `/api/research/archive` endpoint bounds the projection to `all`, `daily`, or
`weekly`, an optional ISO date or Monday week start, and at most 52 rows. Weekly
records use the supplied market calendar when available to disclose expected versus
included sessions and unexpected ledger dates. Position snapshots deliberately expose
ledger quantities only;
they do not reconstruct historical prices, market value, or weights from today's
cache. The browser renders the same limitations and keeps wide tables internally
scrollable.

This projection is still computed on demand and is not itself persistence. The
`DashboardService.generate_research_digests` path is the separate materialization
boundary: it reads one validated projection, then appends owner/account-scoped
daily and weekly digest records. It never refreshes providers, invokes strategy,
writes paper or shadow accounting, calls a broker, changes a strategy or permission,
or creates an order intent. The original paper reports, equity ledger, and
owner-scoped journal remain authoritative; digests are derivative, fingerprinted
research evidence.

### Persistent digest ledger

This surface is implemented on `main` under Unreleased. It is not present in
the public `v0.12.1` wheel.

```text
validated archive projection
            |
            v
owner + paper account epoch + config fingerprint
            |
            v
state/research_digests/users/<owner-sha256>/accounts/<account-sha256>/digests/
            |
            +--> daily/<date>/revision_N.json
            +--> weekly/<iso-monday>/revision_N.json
```

`ResearchDigestStore` validates canonical JSON, source and account bindings,
periods, revision numbering, parent `supersedes` links, and SHA-256 fingerprints
on every read. An unchanged canonical payload/source tuple is idempotently reused;
an evidence change creates the next revision and links it to the previous record.
The actor and trigger (`manual` or `scheduled`) are audit metadata and cannot
change authority. HTTP generation is fixed to `manual`; the local CLI accepts
either operator-supplied value, and `scheduled` is only the bundled runner's
convention, not authenticated scheduler provenance. Per-account locks, atomic
create-once files, bounded 2,000-chain/2,000-revision limits, and fail-closed
handling prevent ambiguous concurrent writes. Revision bytes are flushed in a
ledger-external, same-volume `.staging` directory; a first revision publishes
the complete chain directory atomically, while later revisions use create-once
file publication. A pre-publication interruption therefore cannot leave an
empty chain or temporary member in the verified ledger. If publication is
visible but a later verification or directory durability barrier fails, the
batch result includes that revision in its committed prefix and still returns
an explicit partial failure. Local hashes detect many accidental edits but are
not signatures or WORM storage; no built-in cloud sync or compaction exists.

An unfiltered materialization reads at most the newest 52 daily and newest 52
weekly projection rows. Older periods require explicit one-period `--date` or
`--week` generation; the default command is not a full-history backfill.

The default Windows schedule staggers paper refresh/audit and the local-owner
digest at 18:10, monitoring at 18:20, and `archive-generate --all-profiles
--trigger scheduled` at 18:30. These are independent tasks with no completion
dependency; a later task may start while an earlier task is running or retrying.
A missed run can be retried safely because the append operation is idempotent.
Revisions are isolated by the paper `account_id` epoch, so a new
`paper-init --overwrite` cannot inherit or mutate an old account's digest chain.
Current browser, HTTP, and CLI reads always bind to the active epoch and expose
no old-epoch selector. Old namespaces are retained offline with their matching
paper evidence; directly browsing or exporting them requires a future dedicated
verifier, not replacement of the active paper state.

## Strategy Lab Boundary

```text
active baseline + allowlisted schema
              |
       +------+------+
       |             |
 manual parameter   deterministic local AI
       |             |
       +------+
              v
       immutable candidate
              |
       same market snapshot
       baseline vs candidate
              |
 full period + holdout + cost + drawdown + stability gates
              |
       explicit human approval
              |
       isolated paper config ----> active paper baseline
                                      |
                         rolling decay evidence
                                      |
                         human review only
                          /       |       \
                     suspend   resume   retire/rollback
                                      |
                                      X  no broker authorization or live order route
```

`strategy_lab/` owns the editable parameter schema, immutable candidate records, deterministic validation, human approvals, isolated paper-config export, activation history, post-activation monitoring, and rollback pointer. Each beta user receives a stable, non-reused internal account identity that is mapped to a hashed local directory under `state/strategy_lab/`; request payloads cannot choose another owner. Login usernames are used only as human-readable audit actors and the browser never receives the raw internal account identity. Version-1 user files migrate in place so existing per-user data remains reachable, while deleting and recreating the same username produces a new identity and cannot inherit the deleted account's records.

A candidate keeps its parent and candidate fingerprints, complete configuration-context fingerprint, baseline and changed settings, source, hypothesis, snapshot identity, gate results, and approval provenance. Validation, approval, export, and activation recompute and compare those bindings. An active-baseline compare-and-swap rejects a stale sibling after another candidate is activated, and activation additionally requires the exact approved export whose broker mode is forced to `disabled`. A rollback request carries the active candidate ID and fingerprint that the user confirmed; the transition compares both inside the owner lock, so a duplicate or stale request cannot pop a second rollback entry.

All writes for one owner run under both an in-process re-entrant lock and an operating-system file lock. The active pointer and its activation, suspension, resumption, retirement, or rollback event are coordinated through a recoverable transaction marker, so another process sees either the prior committed state or the completed transition after recovery. Immutable writes use create-once semantics inside the same lock. Candidate creation is capped at 100 records, monitoring at 500 records, lifecycle transitions share a 1,000-event budget, browser summaries retain only the newest 50 candidates and 200 events while reporting total counts, and each workstation process admits only one synchronous strategy backtest at a time. Real compare-and-swap and capacity conflicts return HTTP 409.

Strategy-lab lifecycle monitoring reruns the active candidate and its recorded parent over a bounded recent window on one immutable market snapshot. It is separate from the watchlist/alert monitor described above. It compares recent Sharpe and drawdown with both the same-window parent and the candidate's activation-time holdout evidence. The result is an immutable `MONITORING_OK`, `REVIEW_REQUIRED`, or `INSUFFICIENT_DATA` record. No verdict changes the active lifecycle state. Suspension, resumption, and retirement require a human confirmation bound to the exact active candidate ID and fingerprint; optional monitoring evidence is fingerprint-bound as well. Retirement is terminal for that candidate and atomically restores the prior lab baseline. None of these operations starts or stops an external paper process, changes a broker configuration, or grants live authority.

AI suggestions are deterministic bounded parameter diffs. They cannot generate Python, arbitrary rules, orders, target positions, approval records, exports, or deployment decisions. Approval is necessary but does not alter `config/default.json` or the existing paper ledger. Export rewrites paper-state paths into a candidate-specific profile and forces the broker mode to `disabled`; starting that profile remains a separate operator action. Historical evidence never mutates the live-readiness gates.

Future broker integrations stay outside the core runtime and enter through the `ai_trade.brokers` entry-point group:

```text
frozen paper epoch -> promotion gate -> broker sandbox adapter
                                          |
                     order snapshots + fills -> lifecycle recovery
                                          |
                         account / position / order reconciliation
                                          |
                         consecutive clean reconciliation gate
                                          |
                    expiring account-bound human authorization
                                          |
                kill switch + pre-trade limits + live environment check
                                          |
                              live order router
```

## Boundaries

- `data/`: provider download, validation, snapshot publication, and read-only market access.
- `security.py`: point-in-time listing, delisting, universe membership, and trading-status records.
- `strategy.py`: momentum, trend, liquidity, capacity, weighting, volatility, and group-exposure constraints.
- `execution.py`: sell-first lot sizing, dated fees, slippage, price-limit checks, no-trade bands, and rejection audit.
- `backtest.py`: close-to-next-open event loop, portfolio risk stops, and benchmark alignment.
- `walk_forward.py`: train-window parameter selection with a continuous out-of-sample account.
- `validation.py`: moving-block bootstrap, cost stress, sensitivity, and regime diagnostics.
- `broker/paper.py`: locked, idempotent, append-only local paper execution.
- `broker/paper_audit.py`: independent-forward ledger checks and promotion gates.
- `broker/base.py`: broker environments, account/position/order/fill contracts, and plugin discovery.
- `broker/lifecycle.py`: legal order transitions, immutable identity checks, partial-fill and cancellation-race reduction, timestamp-ordered recovery, and order/fill consistency reports.
- `broker/reconciliation.py`: account and position comparison plus content-bound, atomically published sandbox evidence with strict whole-ledger auditing.
- `broker/scope.py`: atomic lifecycle-ledger scope manifests with non-plaintext account references and strict adapter, environment, configuration, and path binding.
- `broker/shadow.py`: strict canonical CSV normalization, per-user immutable fill/import fingerprints, duplicate/conflict detection, and non-qualifying behavior/price/allocation review.
- `broker/ledger.py`: idempotent order intents, broker observations, durable event/fill ledgers, operating-system locks, and restart recovery entry points.
- `broker/mandate.py`: strict authorization scopes, exact batch fingerprints, and one-time human approval consumption with retained local audit records.
- `broker/live_guard.py`: paper, configuration, adapter capability, reconciliation, kill-switch, mandate, authorization, and process-confirmation gates.
- `broker/live.py`: fail-closed pre-trade validation and the only future live submission boundary.
- `assistant/`: local/model K-line review, closed research conclusion schema, and per-user local history; it has no broker capability.
- `research_journal.py`: per-owner immutable human notes, decision rationale, correction links, evidence fingerprints, and fixed research-only authority.
- `research_digest.py`: per-owner/per-paper-epoch immutable daily and weekly digest chains, source bindings, revisions, and fail-closed storage checks.
- `monitoring.py`: owner-scoped watchlists, deterministic rules, immutable scan and alert evidence, append-only review actions, and research-only scheduled scans.
- `strategy_lab/`: allowlisted strategy/risk parameters, immutable per-user candidates and monitoring evidence, same-snapshot validation, human approval, isolated paper export, activation lifecycle, retirement registry, and rollback.
- `web/auth.py`: atomic PBKDF2 user records, portable whitelist validation, login throttling, and in-memory sessions.
- `web/`: loopback-only authenticated HTTP server, non-mutating market-chart projection, background job manager, dashboard service, and packaged static application with pinned local KLineChart assets.

No broker adapter ships inside the core wheel. Adapter factories and immutable capability declarations use separate entry-point groups; the core rejects duplicate registration names, malformed declaration types, missing metadata, undeclared operations, factory identity or environment mismatches, and runtime/declaration drift before broker I/O. Shared runtime validators treat broker account, position, health, order, fill, and completeness-flag annotations as untrusted values before probe rendering or approval consumption. Adapter/account identities and returned currency, symbol, order/fill ID, and message text have explicit length and canonical-text limits; surrounding whitespace, non-printing Unicode, control characters, DEL, and oversized values fail closed before persistence or rendering. Broker control files and evidence ledgers are required to resolve to distinct filesystem paths, so relative or absolute aliases cannot collapse those trust boundaries. The source repository includes an optional, independently installed QMT read-only observation plugin. It rejects live construction and all submission/cancellation calls, cannot verify paper versus live from QMT, and its diagnostic comparison writes no qualifying reconciliation evidence. The live route exists so its safety contract can be tested before a live-capable broker implementation is introduced; with the default configuration it cannot submit an order.

The broker lifecycle reducer is downstream of adapter observations and upstream of any formal order reconciliation. It sorts timezone-aware snapshots into event time, preserves valid late arrivals, enforces monotonic fills and terminal states, and cross-checks standard fills against the latest snapshot. China-session intent timestamps prevent UTC date-boundary inversions. Live order requests reject arbitrary adapter metadata until it has a bounded cross-adapter schema, ensuring the one-time batch fingerprint covers the complete accepted order semantics. Each live intent embeds its approval ID and exact batch fingerprint in the content-bound reservation event. After approval and ledger I/O, the live router refreshes runtime account identity, available cash, sellable positions, and trading-session health, then checks authorization and the kill switch last before broker submission. After broker I/O, the complete returned collection is runtime-validated and compared with every approved request identity before any response snapshot is appended; malformed or changed responses leave only the pre-I/O reservation as uncertain evidence. A persisted `PENDING_SUBMIT` is reported as `submission_unconfirmed` until a broker snapshot resolves it; the router never infers rejection from a timeout or retries it automatically. New order events carry a canonical `v2_` content fingerprint and new fill rows carry a full canonical `record_sha256`; both are recomputed on read. The Trading API does not return raw order CSV rows and derives both current orders and displayed fills from this validated reducer projection, so invalid schemas or fingerprints cannot be rendered as evidence. Legacy event IDs, legacy fill schemas, and numerically equivalent legacy rows remain readable without in-place migration. Appending to a legacy fill file preserves its old schema rather than presenting historical rows as content-bound evidence. Formal clean reconciliation additionally binds both position maps, automatically reproduces every cash and position issue, and requires non-negative cash within the fixed tolerance. In-process serialization plus operating-system locks protect concurrent writers, and each individual ledger is published by flushed same-directory atomic replacement. Two-ledger observation writes remain ordered and retryable: if a process exits after the order side is durable but before fills are appended, repeating the exact observation repairs the incomplete pair.

Before a future sandbox or live writer creates evidence, an atomically published sidecar binds the lifecycle pair to the adapter, a SHA-256 account reference, declared environment, active live-configuration fingerprint, and the resolved order/fill paths. The manifest never stores or returns the plaintext account ID. Overview and Trading web projections additionally remove the configured broker account ID and absolute kill-switch/batch-approval paths from the readiness object; exact values remain inside the server-side gate only. A scoped writer rejects a missing manifest beside existing ledgers and rejects any identity, environment, configuration, or path mismatch before broker I/O. Legacy unscoped ledgers remain readable and are labeled non-authoritative, but cannot be extended through the scoped route. Scope binding prevents accidental evidence mixing; it is not a keyed signature, formal cash/position reconciliation, strategy-promotion evidence, or live authority.

Formal sandbox reconciliation is a separate authority-bearing ledger. New rows bind adapter, account, date, configuration, cash values, complete canonical expected/broker position maps, and automatically reproduced issue details into a `v3_` content fingerprint. The auditor validates the exact schema and every row before selecting the active account; duplicate nested JSON keys, duplicate logical sessions, malformed rows, reordered dates, fingerprint mismatches, and issue/snapshot disagreement fail closed. Updates use the same in-process/operating-system locking and atomic same-directory publication as lifecycle ledgers. Legacy 24-character identity-only and `v2_` cash-only rows remain readable for operational continuity but are excluded from the consecutive clean-session gate because they do not bind the compared position snapshots.

Provider fallback, cloud backup, research-journal append, research monitoring,
shadow CSV review, and broker routing are separate trust boundaries. A
successful data refresh, R2 backup, journal entry, monitoring alert, or shadow
comparison does not satisfy a paper-promotion gate, authorize live trading, or
establish that the data is suitable for an order decision. The journal,
monitoring engine, and shadow review have no broker object and cannot call
submission or cancellation methods.

The security-master schema removes a fixed instrument-count assumption, but the default master remains a curated ETF universe. A professional stock universe additionally requires licensed or independently verified point-in-time constituent and corporate-action data; the architecture does not treat a current constituent list as historical truth.

## Optional Cloud Backup Boundary

```text
validated data/cache allowlist -> ZIP + snapshot manifest + SHA-256 -> private R2 namespace
private R2 namespace -> size/hash/schema/path verification -> local/cloud-restore staging
```

R2 is an optional object-backup adapter and is disabled without each user's own environment configuration. The upload boundary can read only the configured instrument CSV files and `data/cache/manifest.json`; it recomputes CSV date facts and emits a strict, sanitized manifest rather than copying arbitrary fields or exception text. It cannot serialize `reports/`, `state/` (including `state/assistant/`, `state/research_journal/`, and `state/research_digests/`), `logs/`, beta users, broker material, or live-trading controls. Deduplication verifies that the pointed-to object still exists with matching size and hashes. Restore creates a new Git-ignored staging directory and never mutates the active cache, so adopting restored data remains a separate, explicit operator decision.

## Clean-room Reference Boundary

The K-line assistant was independently designed after reviewing only the public, observable research workflow of `rosemarycox5334-debug/PA_Agent`. PA_Agent is not a dependency. No AGPL source code, prompt text, schemas, UI implementation, assets, or documentation text was copied, translated, or adapted into AI Trade. The assistant's module boundaries, contracts, storage format, provider controls, and workstation presentation are native AI Trade designs.
