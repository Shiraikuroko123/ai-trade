# Architecture

```text
Eastmoney daily data
        |
        v
validated atomic snapshot + manifest
        |
        +--> strategy signal (completed close only)
        |          |
        |          v
        |    target ETF weights
        |          |
        +----------+--> next-session open execution model
                           |
          +----------------+----------------+
          |                                 |
          v                                 v
 historical backtest                 local paper account
          |                                 |
          v                                 v
 walk-forward + robustness           equity/trade ledgers
          |                                 |
          +----------------+----------------+
                           v
                    auditable reports
```

## Boundaries

- `data/`: provider download, validation, snapshot publication, and read-only market access.
- `strategy.py`: momentum, trend, liquidity, weighting, and risk-budget signals.
- `execution.py`: lot sizing, slippage, commissions, and no-trade bands.
- `backtest.py`: close-to-next-open event loop, portfolio risk stops, and benchmark alignment.
- `walk_forward.py`: train-window parameter selection with a continuous out-of-sample account.
- `validation.py`: moving-block bootstrap, cost stress, sensitivity, and regime diagnostics.
- `broker/paper.py`: locked, idempotent, append-only local paper execution.
- `broker/paper_audit.py`: independent-forward ledger checks and promotion gates.

Live broker submission is deliberately absent.
