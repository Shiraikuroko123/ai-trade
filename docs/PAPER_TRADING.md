# Paper Trading Operations

## Daily Flow

```powershell
python -m ai_trade.cli paper-run
python -m ai_trade.cli paper-audit
```

`paper-run` refreshes the complete market snapshot, processes every missing benchmark session in order, simulates pending orders, applies portfolio risk controls, and writes state plus append-only trade/equity ledgers. Repeating a completed date is idempotent.

`paper-audit` checks schema, unique session IDs, strict date ordering, state-to-ledger reconciliation, configuration fingerprints, forward metrics, and promotion gates.

## Configuration Changes

Changing the strategy, risk, costs, universe, provider, adjustment policy, or market close invalidates the active account fingerprint. Review the change, then explicitly archive the epoch:

```powershell
python -m ai_trade.cli paper-init --overwrite
```

## Promotion

The first gate requires at least 60 independent future sessions, a clean ledger, drawdown within the configured limit, positive forward Sharpe, and nonnegative benchmark-relative return. Passing only permits broker-sandbox review. It never enables live trading.
