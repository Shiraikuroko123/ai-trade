# Market Data Providers

AI Trade uses one snapshot refresh contract for every network data source. The
provider boundary is implemented in `src/ai_trade/data/providers.py`; the
existing Eastmoney and Tencent parsers remain deliberately separate so their
response validation and provenance rules cannot be mixed accidentally.

## Supported configuration

The current release registers four daily-bar providers. Eastmoney and Tencent
are eligible to supply the strategy-visible snapshot; Yahoo Finance and
Tushare Pro are bounded independent reference routes only.

| Key | Role | Intraday | Comparable fields | Status |
| --- | --- | --- | --- | --- |
| `eastmoney` | primary or fallback | Yes (separate research feed) | OHLCV + amount | Implemented |
| `tencent` | primary or fallback | No | OHLCV + amount | Implemented |
| `yahoo` | independent cross-check only | No | OHLCV (amount unavailable) | Implemented, reference-only |
| `tushare` | independent cross-check only | No | OHLCV + amount | Implemented, reference-only, token required |

Example:

```json
{
  "data": {
    "provider": "eastmoney",
    "fallback_provider": "tencent",
    "cross_check": {
      "enabled": true,
      "reference_provider": "yahoo"
    }
  }
}
```

To select Tushare instead, keep the token outside configuration and change
only the reference key:

```powershell
$env:AI_TRADE_TUSHARE_TOKEN='<tushare-token>'
```

```json
{
  "data": {
    "provider": "eastmoney",
    "fallback_provider": "tencent",
    "cross_check": {
      "enabled": true,
      "reference_provider": "tushare"
    }
  }
}
```

Provider names are normalized to lowercase during configuration loading. The
primary and fallback cannot be the same provider. `none` disables the network
fallback and leaves the validated local cache as the final route.

`yahoo` and `tushare` cannot be selected as `provider` or
`fallback_provider`. Yahoo's public
Chart response has no provider-reported CNY turnover amount and is intentionally
limited to a short, completed-session reference window. Yahoo share volume is
normalized to domestic lots (100 shares) and its estimated amount is retained
only in the temporary comparison CSV; `amount` is excluded from the audit and
never enters strategy liquidity calculations. Yahoo supports `none` and
`forward` adjustment for this reference route; `backward` is rejected at
configuration load time.

Tushare uses the authenticated Pro API for configured `STOCK` and `ETF`
instruments. `AI_TRADE_TUSHARE_TOKEN` is read at request time, never copied into
configuration, manifests, logs, evidence metadata, or release artifacts, and
is also passed explicitly by the optional Compose setup. Tushare requests a
maximum 62-calendar-day completed-session window, validates a maximum of 64
rows, normalizes share volume to domestic lots and amount from thousands of
CNY to CNY, and supports only `none` and `forward` adjustment. A missing token
or provider error makes the independent audit unavailable; it never replaces
the primary snapshot or changes strategy output.

## Independent cross-check

The optional `data.cross_check` block runs a bounded recent-session audit after
the snapshot is published. It uses a different registered provider, compares
the fields declared by the reference provider with explicit tolerances, and
stores the result under
`manifest.json -> cross_source_check`. A file supplied by the fallback is
never compared with that same provider; the auditor tries the configured
primary instead and records an unavailable/warning result if it cannot be
reached. See [CROSS_SOURCE_AUDIT.md](CROSS_SOURCE_AUDIT.md) for the status
semantics and command examples.

AKShare, TDX and WenCai are not registered yet. A configuration that
names one of them fails at startup instead of pretending that the source was
used. Both reference-only adapters have explicit field mappings, adjustment
policies, bounded response parsers, and independent deterministic fixtures,
but neither replaces an exchange-certified or licensed primary feed. Their
availability, terms, permissions, and regional access can change.

The Dragon-Tiger List, market-breadth, and board-capital-flow
adapters documented in `MARKET_INTELLIGENCE.md`, `MARKET_BREADTH.md`, and
`CAPITAL_FLOW.md` are separate evidence boundaries. They do not implement
`MarketDataProvider`, cannot supply or replace an OHLCV file, and are not
counted as independent daily-bar sources. Market breadth uses a provider-defined
board universe and three benchmark quote responses; capital flow uses the same
provider-defined board scope with Eastmoney's order-size methodology. Neither
is a licensed industry taxonomy, exchange-certified statistic, or independent
cross-source validation route, and overlapping board-flow rows cannot be summed
as whole-market flow.

## Manifest evidence

Each refresh records the normalized provider chain in
`data/cache/manifest.json`:

- `request_policy.primary_provider`
- `request_policy.fallback_provider`
- `request_policy.provider_chain`
- `request_policy.primary_provider_circuit_breaker`
- per-file `source`, `network_errors`, `fallback_reason`, and provider metadata
- `cross_source_check` status, provider pair, date overlap, deviation summary,
  provider-declared comparison fields, unavailable fields, and an audit digest
  bound to the active CSV hashes

The top-level `provider` remains the configured primary provider. A file may
still have a fallback source; that distinction is preserved so a report never
confuses the configured route with the route that actually supplied a bar.
All files are subject to the existing completed-session cutoff, schema checks,
hash checks and atomic snapshot publication.

## Adding a provider

An adapter must implement the normalized per-instrument contract exposed by
`MarketDataProvider` and then be registered in `_PROVIDERS`. Before enabling it
in a release, add deterministic fixtures for malformed payloads, retries,
partial history, adjustment semantics, amount precision and transport failure.
The adapter must not write strategy, paper-account or broker state. It may only
stage validated bars and metadata for the snapshot transaction.

Daily public endpoints do not provide real-time or exchange-certified data.
Adding a provider to this registry does not authorize live trading or remove
the requirement for a licensed intraday/quote feed.

The Eastmoney `trends2` minute endpoint is intentionally kept in a separate
`intraday` evidence store rather than exposed as a strategy snapshot provider. It records
the response fingerprint, requested interval, completed-session cutoff and the
`f52-f55` OHLC mapping. Wider intervals are deterministic local aggregations
of a validated one-minute revision.

Public five-level depth is stored in its own `order_book` evidence chain with
lot/share units and observation time.
These third-party feeds cannot replace licensed Tick, full-depth, Level-2, or
execution data.

## Assistant consumption boundary

The `v0.17.0` assistant does not register another provider and never fetches
network data during analysis. For a configured `STOCK`, it queries the existing
fundamental and valuation stores using the exact final completed K-line date.
Only `current` or `partial` evidence is eligible; a `provisional` valuation is
excluded to prevent pre-close observations from entering a completed-bar
review. ETFs and other non-company instruments remain unsupported.

Eligible financial fields, PE/PB values, and PE/PB/cash-flow/PS empirical
percentiles are copied into the analysis evidence ledger with stable evidence
IDs and their immutable record fingerprints are bound into the assistant
snapshot. Missing, sparse, or conflicting evidence produces an explicit
abstention. A recorded conflict from either optional Tushare field-level check
also forces the fundamental perspective to abstain. It is never filled from
model prose and never changes execution authority.

Both stores keep Eastmoney as the primary normalized data. Consuming them
together does not make them independent sources and must not be presented as
cross-source confirmation. When `AI_TRADE_TUSHARE_TOKEN` is configured, the
fundamental refresh compares the newest common disclosed report period against
Tushare `fina_indicator` and consolidated `income` fields, while valuation
compares the exact completed session against `daily_basic`. These checks are
reference-only: they preserve their own response fingerprints, never fill a
missing primary value, never replace a primary record, and do not create a
strategy signal or execution authority.

The news store may also request the Tushare `sina`, `wallstreetcn`, and
`10jqka` editorial feeds. Those names identify editorial sources delivered
through one Tushare transport Provider; they are not three independent
transport confirmations. News heat uses freshness plus the count of distinct
transport Providers, retains `sentiment_coverage=UNAVAILABLE`, and cannot be
used as an independent daily-bar or fundamental cross-check.
