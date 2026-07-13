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

Live broker submission is deliberately absent.

The security-master schema removes a fixed instrument-count assumption, but the default master remains a curated ETF universe. A professional stock universe additionally requires licensed or independently verified point-in-time constituent and corporate-action data; the architecture does not treat a current constituent list as historical truth.
