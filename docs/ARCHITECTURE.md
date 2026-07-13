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

The market refresh route is deterministic and auditable. Eastmoney is the configured primary provider. A failed primary request can fall back to Tencent Finance; a refresh-scoped transport circuit breaker skips repeated Eastmoney attempts after a qualifying connection failure. Only when both permitted network routes fail may the refresh reuse a locally validated cache inside the configured freshness window.

Every candidate file is staged and validated before publication. The cache manifest distinguishes the requested completed-session cutoff from the latest trading session shared by the required active instruments, and records each instrument's route, errors, fallback reason, latest session, row count, and SHA-256. Tencent incremental refreshes and local network fallbacks accept a seed only when the active manifest, provider, adjustment, requested history start, latest session, row count, source route, and file hash match; entries record the retained seed source/hash in addition to refresh mode, proxy mode, page and overlap counts, and amount-quality fields.

Publication is a recoverable cross-file transaction. A same-volume transaction directory keeps immutable copies of the previous files and a durable journal, while an atomic root marker mirrors the current phase. All CSV files are replaced before `manifest.json`; after validating the installed files, the journal and marker record an explicit `committed` state with every target hash. `MarketData` holds the transaction lock while loading. Recovery keeps a complete committed snapshot, rolls a mixed install back, reconstructs state from the journal when the root marker is missing, and preserves backups while failing closed if the remaining transaction is ambiguous.

Tencent's currently observed historical kline amount field uses two decimal places in units of CNY 10,000, giving CNY 100 resolution. CNY 50 is only the nominal error bound if that quantization is round-to-nearest; it is not a provider contract. When the latest kline can be reconciled exactly with the separate quote response, the quote amount replaces that one value and the manifest records the override. This provenance supports review; it does not turn a public provider response into exchange-certified data.

The workstation has two explicit local access profiles. Beta mode protects every data API, job, and report with a password-authenticated in-memory session and a session-bound CSRF token. Owner-local mode deliberately bypasses that login for one trusted loopback-only process; it is a convenience profile, not a remotely enforceable license.

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

Future broker integrations stay outside the core runtime and enter through the `ai_trade.brokers` entry-point group:

```text
frozen paper epoch -> promotion gate -> broker sandbox adapter
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
- `broker/reconciliation.py`: account and position comparison plus append-only sandbox evidence.
- `broker/ledger.py`: idempotent order intents, broker order events, and fills.
- `broker/live_guard.py`: paper, configuration, adapter, reconciliation, kill-switch, authorization, and process-confirmation gates.
- `broker/live.py`: fail-closed pre-trade validation and the only future live submission boundary.
- `assistant/`: local/model K-line review, closed research conclusion schema, and per-user local history; it has no broker capability.
- `web/auth.py`: atomic PBKDF2 user records, portable whitelist validation, login throttling, and in-memory sessions.
- `web/`: loopback-only authenticated HTTP server, background job manager, dashboard service, and packaged static application.

No broker adapter ships with the project. The live route exists so its safety contract can be tested before credentials or broker-specific code are introduced; with the default configuration it cannot submit an order.

Provider fallback, cloud backup, and broker routing are separate trust boundaries. A successful data refresh or R2 backup does not satisfy a paper-promotion gate, authorize live trading, or establish that the data is suitable for an order decision.

The security-master schema removes a fixed instrument-count assumption, but the default master remains a curated ETF universe. A professional stock universe additionally requires licensed or independently verified point-in-time constituent and corporate-action data; the architecture does not treat a current constituent list as historical truth.

## Optional Cloud Backup Boundary

```text
validated data/cache allowlist -> ZIP + snapshot manifest + SHA-256 -> private R2 namespace
private R2 namespace -> size/hash/schema/path verification -> local/cloud-restore staging
```

R2 is an optional object-backup adapter and is disabled without each user's own environment configuration. The upload boundary can read only the configured instrument CSV files and `data/cache/manifest.json`; it recomputes CSV date facts and emits a strict, sanitized manifest rather than copying arbitrary fields or exception text. It cannot serialize `reports/`, `state/` (including `state/assistant/`), `logs/`, beta users, broker material, or live-trading controls. Deduplication verifies that the pointed-to object still exists with matching size and hashes. Restore creates a new Git-ignored staging directory and never mutates the active cache, so adopting restored data remains a separate, explicit operator decision.

## Clean-room Reference Boundary

The K-line assistant was independently designed after reviewing only the public, observable research workflow of `rosemarycox5334-debug/PA_Agent`. PA_Agent is not a dependency. No AGPL source code, prompt text, schemas, UI implementation, assets, or documentation text was copied, translated, or adapted into AI Trade. The assistant's module boundaries, contracts, storage format, provider controls, and workstation presentation are native AI Trade designs.
