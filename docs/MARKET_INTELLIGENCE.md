# Market Intelligence Evidence

The market-intelligence layer on `main` is an **Unreleased**, read-only research
surface. Its first dataset is the daily Dragon-Tiger List published by
Eastmoney. It does not provide intraday quotes, exchange-certified records,
news sentiment, a strategy signal, or any order authority.

## Current Dataset

| Dataset | Provider | Frequency | Current boundary |
|---|---|---|---|
| `dragon_tiger_daily` | Eastmoney `RPT_DAILYBILLBOARD_DETAILSNEW` | One completed trading date | Implemented as a local immutable revision chain |
| Sector rankings and breadth | None | - | Not implemented; the security-master `sector` field remains a risk group, not a whole-market industry feed |
| Capital flow | None | - | Not implemented |
| Announcements and news | None | - | Not implemented |
| Valuation percentiles | None | - | Not implemented |
| Sentiment | None | - | Not implemented; Dragon-Tiger List evidence does not make `sentiment_coverage` available |

The system identifies the default refresh date from the latest locally
validated market snapshot. It does not guess a trading day from the wall clock
and a GET request never contacts Eastmoney.

## Refresh

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli market-intelligence-refresh
```

An explicit historical date can be requested for controlled backfill:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli market-intelligence-refresh --date 2026-07-17
```

The **市场情报** page exposes the same fixed background job as **刷新龙虎榜**.
The job downloads every reported page, validates the complete response, and
publishes only after the whole dataset passes. A failed or cancelled refresh
leaves the previous complete snapshot untouched. The page distinguishes a
running refresh, a failed job, a valid empty result, a stale snapshot, and a
workspace that has never refreshed this dataset.

## Validation Contract

The provider accepts only the documented daily report envelope and a bounded
set of fields. Validation includes:

- HTTP response size, page count, row count, and total reported count;
- one requested trade date on every row;
- unique `TRADE_ID + SECURITY_CODE + CHANGE_TYPE` source identity;
- six-digit symbols, known SH/SZ/BJ market codes, and bounded text;
- finite prices, percentages, and amounts;
- nonnegative buy, sell, and deal amounts; and
- internal buy/sell/net-amount consistency within the declared currency
  precision.

Duplicate JSON keys, malformed pages, date leakage, missing fields, non-finite
numbers, count mismatches, and inconsistent amounts reject the entire refresh.
The public endpoint is not an exchange feed and its availability and terms may
change; successful structural validation does not turn it into certified data.

## Immutable Revisions

Validated records are normalized, deterministically ordered, and stored below:

```text
state/market_intelligence/dragon_tiger/YYYY-MM-DD/
```

Each revision carries the source report, retrieval time, response and evidence
fingerprints, coverage totals, summary values, authority declaration, and the
normalized records. Repeating the same normalized record set reuses the existing
revision. If those normalized records change for the same trade date, a new
revision is appended with a `supersedes` link; an old file is never overwritten.

These SHA-256 values detect accidental changes and inconsistent local edits;
they are not signatures, remote attestation, or WORM storage. A local
administrator can rewrite or delete files. The directory is ignored by Git,
excluded from release artifacts, and outside the Cloudflare R2 market-cache
allowlist. It therefore does not consume the configured R2 snapshot budget and
is not restored by `cloud-restore`.

## Read API

```text
GET /api/market-intelligence
GET /api/market-intelligence?date=2026-07-17&market=SZ&symbol=000722&q=涨幅&limit=100
```

Supported filters are `date`, `market`, `symbol`, `q`, and `limit`. Parameters
are unique and bounded; unknown or repeated parameters fail with HTTP 400.
Filtering changes only the returned view, not the immutable source snapshot.
The response keeps source, coverage, fingerprints, revision history, status,
warnings, and fixed authority alongside the filtered rows.

## Authority Boundary

Every snapshot and response fixes:

```json
{
  "research_only": true,
  "execution_authorized": false
}
```

Dragon-Tiger List rows may support a human research review. They cannot modify
a strategy candidate, mark fundamental or sentiment coverage as available,
write a paper or broker ledger, create an order, satisfy a promotion gate, or
unlock live trading. Later sector, flow, news, valuation, and sentiment
adapters require their own provider, date, methodology, licensing, completeness,
staleness, and cross-source contracts before entering this layer.
