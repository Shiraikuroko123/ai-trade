# Market Intelligence Evidence

The market-intelligence layer in `v0.16.0` is a read-only research surface.
It contains nine separately stored evidence datasets. Each dataset has its own
source, cutoff, response fingerprint, coverage declaration, immutable revision
chain, and explicit authority boundary. No dataset creates a strategy signal,
changes accounting, or authorizes an order.

## Current Datasets

| Dataset | Provider | Frequency | Current boundary |
|---|---|---|---|
| `dragon_tiger_daily` | Eastmoney `RPT_DAILYBILLBOARD_DETAILSNEW` | One completed trading date | Full reported pagination is validated and stored as immutable revisions. It is third-party event evidence, not sentiment. |
| `sector_breadth` | Eastmoney board pages plus SH/SZ/BJ benchmark quotes | One completed trading date | Provider-defined, potentially overlapping boards and market-width counts; see `MARKET_BREADTH.md`. |
| `capital_flow` | Eastmoney provider-defined order-size buckets | One completed quote date | Signed CNY amounts and percentages are retained; board rows must not be summed as whole-market flow. |
| `intraday` | Eastmoney `trends2` | One completed date and selected interval | Historical minute evidence with retained `f52-f55` OHLC mapping; not a real-time or Tick feed. |
| `valuation` | Eastmoney quote and `RPT_VALUEANALYSIS_DET` | Current quote plus bounded history | Current fields are available for configured instruments. Historical PE/PB/cash-flow/sales percentiles are stock-only and require at least 120 valid observations. |
| `fundamentals` | Eastmoney `RPT_LICO_FN_CPD` | Completed-session cutoff | Stock-only disclosed financial periods. Both notice and update dates must be no later than the cutoff. |
| `official_disclosures` | SSE and CNINFO | Bounded completed-date window | Official metadata and PDF links only. Provider and security coverage gaps remain explicit; PDF bodies are not archived. |
| `news` | Eastmoney news and individual announcement aggregation | One completed cutoff | Third-party evidence kept separate from official disclosures. `lexicon-v1` is not a sentiment model. |
| `order_book` | Eastmoney public quote | One observed snapshot | Level-1 five-level bids/asks, lot/share units, spread, and bounded depth imbalance; not Tick, full depth, or Level-2. |

The default closing date for daily datasets comes from the latest locally
validated market snapshot. The system does not guess a trading date from the
wall clock, and every GET endpoint reads local evidence without contacting a
provider.

## Refresh Commands

Closing market datasets have independent commands so failure in one source
cannot change another dataset's availability:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli market-intelligence-refresh --date 2026-07-17
.\.venv\Scripts\python.exe -m ai_trade.cli market-breadth-refresh --date 2026-07-17
.\.venv\Scripts\python.exe -m ai_trade.cli capital-flow-refresh --date 2026-07-17
```

Instrument evidence also uses separate bounded refreshes:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli intraday-refresh --symbol 510300 --interval 5
.\.venv\Scripts\python.exe -m ai_trade.cli valuation-refresh --symbol 600519
.\.venv\Scripts\python.exe -m ai_trade.cli fundamentals-refresh --symbol 600519 --periods 8
.\.venv\Scripts\python.exe -m ai_trade.cli disclosures-refresh --symbol 600519 --lookback-days 30 --limit 50
.\.venv\Scripts\python.exe -m ai_trade.cli news-refresh --symbol 600519
.\.venv\Scripts\python.exe -m ai_trade.cli order-book-refresh --symbol 510300
```

The Market Intelligence page exposes matching fixed background actions. A
failed or cancelled action leaves the previous complete revision untouched.
The UI distinguishes a running refresh, provider failure, valid empty filter,
partial coverage, stale snapshot, and a dataset that has never been refreshed.

## Point-in-time and Coverage Rules

Fundamental records are accepted only for configured `STOCK` instruments.
For each report period, `NOTICE_DATE` and `UPDATE_DATE` must both be on or
before the completed-session cutoff; the newest eligible update wins when the
provider returns multiple versions for one period. EPS, revenue, parent net
profit, weighted ROE, revenue and profit growth, book value per share,
operating cash flow per share, and gross margin remain nullable provider
fields. ETFs are reported as unsupported instead of receiving inferred company
metrics. This evidence is not yet consumed by the AI assistant's fundamental
role, so that role remains `UNAVAILABLE`.

Historical valuation percentiles are calculated only for configured stocks.
The store retains the source field, observation count, first and last date,
and provider response fingerprint for `PE_TTM`, `PE_LAR`, `PB_MRQ`,
`PCF_OCF_TTM`, and `PS_TTM`. Only positive finite completed-session values are
eligible. A percentile remains null when the current value is invalid or fewer
than 120 observations exist. ETF history is explicitly unsupported, and price
history is never substituted for valuation history.

Official-disclosure routing is deliberately narrow:

| Instrument | Official metadata route |
|---|---|
| Shanghai stock | SSE |
| Shenzhen stock | CNINFO designated platform |
| Shenzhen ETF present in the CNINFO fund master | CNINFO designated platform |
| Shanghai ETF, Beijing market, or missing CNINFO master entry | Explicit coverage gap |

Only official metadata and allowlisted PDF URLs are archived. The system does
not download, sign, or WORM-store PDF bodies, and it does not infer sentiment
from disclosure titles. Eastmoney news and announcement aggregation remains a
separate third-party dataset with its own provenance.

The order-book store validates one observed public quote per instrument. It
retains five bid and ask ranks, CNY prices, provider volumes in lots, normalized
share volumes (`lots * 100`), best bid/ask, spread, observation timestamp, and
bounded imbalance. Missing levels remain a visible partial snapshot. This is
ephemeral Level-1 research evidence, not a replayable order-event stream or an
execution-quality quote.

## Immutable Revisions

Validated records are normalized and stored below:

```text
state/market_intelligence/dragon_tiger/YYYY-MM-DD/
state/market_intelligence/sector_breadth/YYYY-MM-DD/
state/market_intelligence/capital_flow/YYYY-MM-DD/
state/intraday/<symbol>/YYYY-MM-DD/<interval>/
state/valuation/YYYY-MM-DD/
state/fundamentals/YYYY-MM-DD/
state/disclosures/YYYY-MM-DD/
state/news/YYYY-MM-DD/
state/order_book/YYYY-MM-DD/
```

Each committed revision retains normalized records, coverage, source response
fingerprints, and a full content fingerprint. Retrieval time and revision-chain
metadata are excluded only from the normalized evidence identity, so repeating
the same evidence reuses the existing revision. Changed evidence appends a new
revision with a `supersedes` link; an old file is never overwritten.

These SHA-256 values detect accidental changes and inconsistent local edits.
They are not signatures, remote attestation, or WORM storage. A local
administrator can rewrite or delete files. The directories are ignored by Git,
excluded from release artifacts, and outside the Cloudflare R2 market-cache
allowlist.

## Read APIs

```text
GET /api/market-intelligence
GET /api/market-breadth
GET /api/capital-flow
GET /api/intraday?symbol=510300&date=2026-07-17&interval=5&limit=120
GET /api/valuation?date=2026-07-17&symbol=600519&limit=100
GET /api/fundamentals?date=2026-07-17&symbol=600519&limit=100
GET /api/disclosures?date=2026-07-17&symbol=600519&provider=sse&limit=100
GET /api/news?date=2026-07-17&symbol=600519&kind=announcement&limit=100
GET /api/order-book?date=2026-07-17&symbol=510300&limit=100
```

Supported filters vary by dataset and are strictly allowlisted and bounded.
Unknown, repeated, malformed, or oversized parameters fail with HTTP 400.
Filtering changes only the returned local view, not the source revision. Every
response retains status, source, coverage, warnings, fingerprints, and fixed
authority metadata.

## Authority Boundary

Every snapshot and API response fixes:

```json
{
  "research_only": true,
  "execution_authorized": false
}
```

These rows may support human research review. They cannot modify a strategy
candidate, write a paper or broker ledger, create an order, satisfy a promotion
gate, or unlock live trading. Fundamental evidence does not automatically make
the assistant's fundamental role available, and official disclosures, news,
Dragon-Tiger rows, breadth, flow, or depth do not make sentiment coverage
available. Remaining work includes licensed real-time minute/Tick and Level-2
feeds, broader official-market coverage and PDF body archival, independent
fundamental/valuation reconciliation, and a complete multi-source hot-list and
sentiment methodology.
