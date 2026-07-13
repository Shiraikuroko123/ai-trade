# Architecture

```text
security master + dated memberships + trading status
                         |
                         v
Eastmoney daily data -> validated atomic snapshot + manifest
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

The workstation has two explicit local access profiles. Beta mode protects every data API, job, and report with a password-authenticated in-memory session and a session-bound CSRF token. Owner-local mode deliberately bypasses that login for one trusted loopback-only process; it is a convenience profile, not a remotely enforceable license.

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
- `web/auth.py`: atomic PBKDF2 user records, portable whitelist validation, login throttling, and in-memory sessions.
- `web/`: loopback-only authenticated HTTP server, background job manager, dashboard service, and packaged static application.

No broker adapter ships with the project. The live route exists so its safety contract can be tested before credentials or broker-specific code are introduced; with the default configuration it cannot submit an order.

The security-master schema removes a fixed instrument-count assumption, but the default master remains a curated ETF universe. A professional stock universe additionally requires licensed or independently verified point-in-time constituent and corporate-action data; the architecture does not treat a current constituent list as historical truth.
