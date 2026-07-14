# Paper Trading Operations

This document describes the `v0.12.0` first-public-release paper-account baseline. Paper execution remains isolated from the read-only market chart, AI assistant, strategy-lab candidate editing, cloud backup, and the unavailable live-broker route.

## Daily Flow

```powershell
python -m ai_trade.cli paper-run
python -m ai_trade.cli paper-audit
```

`paper-run` refreshes the complete market snapshot, processes every missing benchmark session in order, simulates pending orders, applies portfolio risk controls, and writes state plus append-only trade, rejection, and equity ledgers. A blocked required sell cancels that session's buy phase. Repeating a completed date is idempotent.

`paper-audit` checks schema, unique session IDs, strict date ordering, state-to-ledger reconciliation, configuration fingerprints, forward metrics, and promotion gates. `state/paper_rejections.csv` remains available for execution-quality review even when an order never became a trade.

The same operations are available in the loopback-only workstation:

```powershell
python -m ai_trade.cli serve
```

Use the Portfolio view for account state and pending targets, Trading for fills/rejections and promotion checks, Risk for independent gates, and System for serialized jobs and logs. The one-time `paper-init` job never passes `--overwrite`; an existing account therefore cannot be replaced from the browser.

## Configuration Changes

Changing the strategy, risk, dated fee tables, security-master contents, selected universe, provider, adjustment policy, or market close invalidates the active account fingerprint. Review the change, then explicitly archive the epoch:

```powershell
python -m ai_trade.cli paper-init --overwrite
```

A strategy-lab approval or activation does not modify this default account. Its exported configuration forces `broker.mode=disabled` and uses candidate-specific state and report paths; the operator must initialize and advance that separate paper profile explicitly.

## Promotion

The first gate requires at least 60 independent future sessions, a clean ledger, drawdown within the configured limit, positive forward Sharpe, and nonnegative benchmark-relative return. Passing only permits broker-sandbox review. It never enables live trading.
