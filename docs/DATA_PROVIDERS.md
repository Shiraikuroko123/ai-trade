# Market Data Providers

AI Trade uses one snapshot refresh contract for every network data source. The
provider boundary is implemented in `src/ai_trade/data/providers.py`; the
existing Eastmoney and Tencent parsers remain deliberately separate so their
response validation and provenance rules cannot be mixed accidentally.

## Supported configuration

The current release registers three daily-bar providers. Eastmoney and Tencent
are eligible to supply the strategy-visible snapshot; Yahoo Finance is a
bounded independent reference route only.

| Key | Role | Intraday | Comparable fields | Status |
| --- | --- | --- | --- | --- |
| `eastmoney` | primary or fallback | No | OHLCV + amount | Implemented |
| `tencent` | primary or fallback | No | OHLCV + amount | Implemented |
| `yahoo` | independent cross-check only | No | OHLCV (amount unavailable) | Implemented, reference-only |

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

Provider names are normalized to lowercase during configuration loading. The
primary and fallback cannot be the same provider. `none` disables the network
fallback and leaves the validated local cache as the final route.

`yahoo` cannot be selected as `provider` or `fallback_provider`. Its public
Chart response has no provider-reported CNY turnover amount and is intentionally
limited to a short, completed-session reference window. Yahoo share volume is
normalized to domestic lots (100 shares) and its estimated amount is retained
only in the temporary comparison CSV; `amount` is excluded from the audit and
never enters strategy liquidity calculations. Yahoo supports `none` and
`forward` adjustment for this reference route; `backward` is rejected at
configuration load time.

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

AKShare, Tushare, TDX and WenCai are not registered yet. A configuration that
names one of them fails at startup instead of pretending that the source was
used. Yahoo is the first reference-only adapter: it has an explicit field
mapping, adjustment policy, rate limit, bounded response parser, and
independent deterministic fixtures, but it is not a replacement for an
exchange-certified or licensed primary feed. The public endpoint has no SLA
and its terms and regional availability can change.

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
