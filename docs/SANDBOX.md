# Sandbox Broker Drill

`sandbox-cycle` runs the entire broker order-lifecycle machinery — scope
binding, mandate enforcement, intent reservation, and observation
reconciliation — against a deterministic in-process broker, so the riskiest
code paths in the system can be rehearsed and audited without a broker
account, without a network, and without touching any promotion-countable
evidence.

## Local workflow

```powershell
ai-trade --config config/default.json sandbox-cycle --symbol 510300
ai-trade --config config/default.json sandbox-cycle --symbol 510300 --limit-price 3.00
ai-trade --config config/default.json sandbox-cycle --symbol 510300 --side SELL --date 2026-07-23
ai-trade --config config/default.json sandbox-status
ai-trade --config config/default.json sandbox-drills --limit 20
```

The cycle needs cached bars for the symbol (`refresh-data` first). Every
drill exercises, in order: capability declaration
(`local-sandbox`, sandbox environment only, no cancel operation), a bounded
`BrokerMandate` enforced against the sandbox ledger's own daily totals, batch
fingerprint computation, durable `PENDING_SUBMIT` intent reservation, a
`SUBMITTED` acknowledgement observation, and a terminal settlement
observation that must reconcile fills against order state or the whole
append is rejected.

## Deterministic fills

The sandbox broker replays one cached session bar. A resting BUY fills iff
its limit price is at or above the session low; a resting SELL fills iff its
limit is at or below the session high; fills execute at the limit price and
charge the configured commission, transfer fee, and (for sells) stamp duty.
Everything else expires at the close. The same inputs always produce the same
outcome, which is what makes the drill a known-answer test of the lifecycle
plumbing rather than a simulation of edge.

## Isolation and the untouched-evidence attestation

Sandbox ledgers live under `state/sandbox/` (`orders.csv`, `fills.csv`,
`ledger_scope.json`) and are bound to their own scope manifest with adapter
`local-sandbox`, a sandbox environment, and a sandbox-specific configuration
fingerprint — the same fail-closed scope machinery the live ledgers use, so a
sandbox row can never be read into a live lifecycle or vice versa. The engine
refuses to construct at all if a sandbox path aliases a live broker path.

Each drill records a `protected_evidence` attestation: SHA-256 digests of
`broker_orders.csv`, `broker_fills.csv`, `broker_ledger_scope.json`, and
`broker_reconciliation.csv` taken before and after the cycle. If any digest
changes, the drill raises instead of publishing. A sandbox run therefore
proves, in the record itself, that it wrote no promotion-countable
reconciliation or live-ledger evidence.

## What the sandbox deliberately does not do

The one-time batch approval file is a live-only human authority gate; the
drill computes and records the batch fingerprint but never creates or
consumes an approval, and says so in its disclosure. Cancel flows are not
declared (deterministic sessions settle every order at the close). Drill
records are create-once, fingerprint-verified JSON under
`state/sandbox/drills/` (capacity 500) with a safety block fixing
`qualifying_evidence`, `promotion_countable`, and `execution_enabled` to
false: a thousand green drills bring live trading zero steps closer, by
design.
