# Paper Trading Operations

This document describes the `v0.12.1` paper-account baseline. Paper execution remains isolated from the read-only market chart, AI assistant, strategy-lab candidate editing, cloud backup, and the unavailable live-broker route.

## Daily Flow

```powershell
python -m ai_trade.cli paper-run
python -m ai_trade.cli paper-audit
```

`paper-run` refreshes the complete market snapshot, processes every missing benchmark session in order, simulates pending orders, applies portfolio risk controls, and writes state plus append-only trade, rejection, and equity ledgers. A blocked required sell cancels that session's buy phase. Repeating a completed date is idempotent.

`paper-audit` checks schema, unique session IDs, strict date ordering, state-to-ledger reconciliation, configuration fingerprints, forward metrics, and promotion gates. `state/paper_rejections.csv` remains available for execution-quality review even when an order never became a trade.

The active `paper_state.json` is a bounded, versioned accounting record. Duplicate
JSON keys, unknown fields, invalid dates, non-finite or negative balances,
malformed positions or target weights, and configuration drift fail closed before
the simulator, auditor, or browser can use the state. Do not hand-edit it; start a
new archived epoch when the frozen configuration changes.

State and report updates are written to a same-directory temporary file, flushed
to disk, and atomically replaced. A replacement failure leaves the previous
complete file in place; a leftover temporary file is not an active account state.
When a date has already been processed, the cached report is checked against the
account state before it is returned. A malformed or inconsistent report produces
an explicit fallback summary rather than overriding the ledger's cash or position
values.

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

Research monitoring is a separate read-only workflow. To install its 18:20
one-shot scan, see [监控与告警运维](MONITORING.md); it does not advance or
authorize this paper account.
