# Market Data Providers

AI Trade uses one snapshot refresh contract for every network data source. The
provider boundary is implemented in `src/ai_trade/data/providers.py`; the
existing Eastmoney and Tencent parsers remain deliberately separate so their
response validation and provenance rules cannot be mixed accidentally.

The `v1.0.0` release gate was rechecked on 2026-07-24 against local and remote
`main`, the latest public release, and the tracked upstream projects. No new
provider implementation or license change justified a source port. In
particular, adding a dependency wrapper around the same Eastmoney endpoint does
not create an independent source, and repositories without a clear compatible
license remain design references only.

## Supported configuration

The current release registers five daily-bar providers. Eastmoney and Tencent
are eligible to supply the strategy-visible snapshot; Yahoo Finance, Tushare
Pro, and BaoStock are bounded reference routes only. BaoStock has an additional
evidence restriction: its upstream provenance and redistribution authorization
have not been verified, so even a complete numerical match remains a warning.

| Key | Role | Intraday | Comparable fields | Status |
| --- | --- | --- | --- | --- |
| `eastmoney` | primary or fallback | Yes (separate research feed) | OHLCV + amount | Implemented |
| `tencent` | primary or fallback | No | OHLCV + amount | Implemented |
| `yahoo` | independent cross-check only | No | OHLCV (amount unavailable) | Implemented, reference-only |
| `tushare` | independent cross-check only | No | OHLCV + amount | Implemented, reference-only, token required |
| `baostock` | provisional recent-data cross-check only | No | OHLCV + amount | Implemented, optional client, authorization unverified |

JQData is a separate optional licensed pre-integration. It is not registered
as a snapshot or cross-check provider yet; the current command only probes
entitlement and quota without requesting market data. See
[JQDATA.md](JQDATA.md) for its personal-research boundary and staged gate.

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

BaoStock can be installed and selected without credentials:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[baostock]"
```

```json
{
  "data": {
    "provider": "eastmoney",
    "fallback_provider": "tencent",
    "cross_check": {
      "enabled": true,
      "reference_provider": "baostock"
    }
  }
}
```

This route is useful for detecting a stale tail or an obvious value conflict.
It is not a substitute for licensed evidence and cannot produce
`independent_confirmed` even when every compared value matches.

Provider names are normalized to lowercase during configuration loading. The
primary and fallback cannot be the same provider. `none` disables the network
fallback and leaves the validated local cache as the final route.

`yahoo`, `tushare`, and `baostock` cannot be selected as `provider` or
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

BaoStock uses its anonymous optional Python client and a direct TCP connection.
The client does not apply the application's HTTP proxy mode. The adapter limits
each request to 101 calendar days and 80 rows, maps provider `adjustflag=2` to
forward adjustment and `adjustflag=3` to unadjusted bars, converts share volume
to 100-share lots, and preserves provider-reported CNY amount. A live probe on
2026-07-27 returned 63 sessions for `510300`, `510500`, and `159915` from
2026-04-24 through 2026-07-27; the overlapping 2026-07-24 OHLC and amount values
matched the installed cache, with volume matching after the share-to-lot
conversion. A later long-window retry failed with BaoStock transport code
`10002007`, so older forward-adjustment equivalence remains unverified.

## Recent-data source decision

The immediate public route and the formal evidence route are deliberately
different:

| Candidate | Recent ETF coverage | Authorization/evidence role | Decision |
| --- | --- | --- | --- |
| RQData | Documented daily ETF bars with readiness, quota, and license APIs | Account-scoped `TRIAL`/`FULL` service | Defer the adapter until an account/license is already available; it is not required for research-only forward capture |
| Tushare Pro `fund_daily` | Post-close ETF daily bars; account points and endpoint permissions apply | Token-authenticated service | Already implemented as reference-only; do not purchase access merely to bypass a failed model gate |
| JQData | Licensed account-scoped market data | Current trial evidence ends at 2026-04-24 | Keep the passed historical sample; a broader entitlement is still required for May-July |
| BaoStock | Anonymous recent and historical daily bars | No verified SLA, upstream provenance, or redistribution authorization | Implemented only for provisional monitoring and conflict detection |
| AKShare ETF history | Current public wrapper around Eastmoney for this route | Same upstream as the primary route | Do not count as an independent source |
| pytdx | Public client project archived in 2020 | Project restricts use to personal learning | Do not use as formal or production evidence |

Official capability references used for the formal candidates are the
[RQData Python manual](https://www.ricequant.com/doc/rqdata/python/manual),
[RQData generic API](https://www.ricequant.com/doc/rqdata/python/generic-api),
and [Tushare `fund_daily`](https://tushare.pro/document/2?doc_id=127). Provider
terms and account entitlements must be checked again before enabling any
strategy-visible or redistributable use.

When no licensed account is available, the supported zero-cost path is to keep
the validated Eastmoney/Tencent cache as research input, use BaoStock only for
bounded anomaly detection, and accumulate genuine forward Feature/Label
snapshots. This does not make the public feeds licensed or independent and does
not change `live_ready=false`. A broker-supplied market-data and sandbox route
can be assessed later; broker eligibility and fees vary and must not be inferred
from a GitHub client library.

Run the two stages explicitly after a completed market session:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli download --force
.\.venv\Scripts\python.exe -m ai_trade.cli feature-forward-run
```

`feature-forward-run` never refreshes or contacts a provider. It fails closed
unless the feature date, the cache's latest common session, and the completed
session cutoff at capture time are identical. Repeating it with identical
inputs reuses the same immutable FeatureSnapshot. It creates LabelSnapshots
only after a genuine snapshot has accumulated the requested 5, 20, or 60
future market sessions; otherwise the label remains pending. Historical
reconstruction and a late capture of a stale cache do not qualify for model
deployment, even when their numerical values later match. This workflow creates
research evidence only: it creates no signal, order, or trading permission.

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

AKShare, TDX and WenCai are not registered. A configuration that
names one of them fails at startup instead of pretending that the source was
used. Every registered reference-only adapter has explicit field mappings,
adjustment policies, bounded response parsers, and deterministic fixtures, but
none replaces an exchange-certified or licensed primary feed. Their
availability, terms, permissions, and regional access can change. BaoStock's
descriptor additionally prevents a numerical match from being promoted to
independent confirmation.

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
Feature and label snapshots therefore derive their `source.provider` from the
per-file routes: one actual provider is named directly, multiple providers are
recorded as a sorted `mixed:...` set, and an unidentifiable route fails closed.
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

The `v1.0.0` assistant does not register another market-data provider and never
fetches network data during analysis. The workstation remains fully usable
without a model. For a configured `STOCK`, it queries the existing
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

The bull, bear, and judge records consume only this already validated assistant
evidence. Their OpenAI-compatible endpoint is a wording/research service, not a
market-data source or independent confirmation. Three role calls against one
endpoint are three isolated audits, not three independent data providers.

The unreleased `v2.0.0` hypothesis lab preserves the same rule. Its first local
generator binds pre-registered experiments to the complete market snapshot,
per-symbol cache hashes, manifest hash, and security-master hash, and performs
no provider or model call. A future model may propose a transform or experiment,
but it cannot invent observations, relabel one transport as multiple sources,
or turn model agreement into data confirmation. Every experiment remains bound
to versioned point-in-time inputs and subject to explicit human promotion.

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
