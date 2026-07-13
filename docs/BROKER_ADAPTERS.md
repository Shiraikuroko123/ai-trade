# Broker Adapter Boundary

AI Trade `v0.8.0` defines and tests a broker boundary but intentionally ships no broker-specific implementation. Installing an adapter does not unlock live trading.

## Plugin Contract

Adapters register a factory in the Python entry-point group `ai_trade.brokers`:

```toml
[project.entry-points."ai_trade.brokers"]
example = "example_adapter:create_broker"
```

The factory receives `AppConfig` and a `BrokerEnvironment` (`sandbox` or `live`) and returns a subclass of `ai_trade.broker.base.Broker`. The adapter must implement:

- health and trading-session status;
- account cash, available cash, and equity;
- positions and available sell quantity;
- open orders;
- batch limit-order submission;
- cancellation;
- fills since an optional timestamp.

Credentials stay in the adapter's local secret storage. They must never appear in `config/default.json`, logs, reports, authorization files, Git, or exception messages.

## Promotion Evidence

An account reaches sandbox review only after the frozen paper epoch passes its independent-forward gate. Sandbox reconciliation compares expected cash and positions against the broker and appends one idempotent record per adapter, account, date, and configuration fingerprint.

The default gate requires 20 consecutive clean reconciliations. A mismatch resets the clean-session run; evidence from another account or configuration does not count.

## Live Authorization

Live authorization is a local, expiring JSON record containing:

- `approved: true`;
- adapter name;
- account ID;
- the active live configuration fingerprint;
- a timezone-aware expiry timestamp.

Authorization is necessary but not sufficient. The runtime also requires `broker.mode=live`, `AI_TRADE_LIVE_CONFIRMATION=I_ACCEPT_LIVE_TRADING_RISK`, an installed adapter, a clear kill-switch file, current paper evidence, and eligible reconciliation evidence.

## Pre-Trade Validation

`LiveOrderRouter` fails closed before calling the adapter. It checks:

- unique client order IDs and append-only intent reservations;
- active point-in-time universe membership and tradable status;
- positive whole-lot quantities and tick-aligned positive limit prices;
- previous-close daily price-limit bounds;
- broker-available positions for sells and cash for buys;
- configured maximum order notional;
- previously reserved plus new daily notional;
- exact live broker environment and a healthy trading session.

Adapters must still implement broker-specific validation and reconciliation. Core checks do not replace exchange or broker rules.

## Development Sequence

1. Implement sandbox read methods and health reporting.
2. Reconcile account and positions without submitting orders.
3. Implement sandbox limit orders, cancellations, order polling, and fills.
4. Accumulate the configured consecutive clean reconciliations.
5. Test disconnects, partial fills, duplicates, stale quotes, rejects, cancellation races, and restarts.
6. Review credentials, logs, kill switch, order limits, and authorization expiry.
7. Add live environment support only after a separate human review.
