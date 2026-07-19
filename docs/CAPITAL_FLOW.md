# Board Capital-Flow Evidence

This document describes the **Unreleased** `capital_flow` dataset on `main`.
It is a closing research view of provider-reported order-size flow, not a
real-time quote feed, exchange-certified statistic, whole-market flow total,
strategy signal, or order source.

## Source Scope

One refresh reads every declared page from Eastmoney's public board-list
endpoint:

| Evidence | Endpoint | Fixed request scope |
|---|---|---|
| Board capital flow | `https://push2.eastmoney.com/api/qt/clist/get` | `fs=m:90+t:2`, sorted by `f62`, 100 rows per page, every declared page |

The normalized rows retain board identity, close, change percentage, and the
provider-reported net amount and percentage for main, super-large, large,
medium, and small order-size buckets. Amounts are stored as CNY yuan. A source
null remains unavailable; it is never replaced with zero.

`m:90+t:2` is a provider-defined board universe. Boards can overlap, so adding
the rows does not produce a valid whole-market net-flow statistic. The bucket
labels and percentages also follow Eastmoney's methodology. They are not an
exchange definition and have no independent cross-source verification in this
version.

## Refresh

Use the latest date in the validated local market cache:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli capital-flow-refresh
```

Or request one controlled date:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli capital-flow-refresh --date 2026-07-17
```

The endpoint exposes current quote timestamps rather than a licensed historical
archive. A revision is published only when every row's China Standard Time
quote date equals the requested date. Supplying an older date does not make the
provider return historical evidence; a date mismatch fails without publication.

The **市场情报** page exposes the same fixed command as **刷新资金流**. Browser
GET requests never contact Eastmoney. While a refresh is running, or after it
fails, the previous complete revision remains readable.

## Validation Contract

No revision is published until the complete response passes all checks:

- duplicate JSON keys and oversized page or aggregate responses are rejected;
- every declared page must be present, with a stable total and exact page row
  counts;
- board codes must be unique, use the fixed `BK####` identity, and carry market
  code `90`;
- every row must match the exact allowlisted field set and requested quote date;
- present numeric values must be finite and within explicit bounds;
- at least one row must expose the main-flow metric; and
- normalized rows use deterministic board-code ordering.

Coverage records declared and received counts, page count, response bytes,
missing optional values, rows with a main metric, and rows with all five amount
buckets. Response, normalized evidence, and full-record SHA-256 fingerprints
are retained for local review.

Structural validation proves only that the observed response fits this local
contract. It does not certify provider methodology, economic interpretation,
licensing, or accuracy.

## Immutable Storage

Validated evidence is stored below the Git-ignored workspace state:

```text
state/market_intelligence/capital_flow/YYYY-MM-DD/revision_NNNNNNNN.json
```

Repeating the same normalized rows reuses the existing revision. Changed rows
for the same date append a new revision linked through `supersedes`; old files
are not overwritten. Atomic staging and strict reads make an incomplete,
tampered, or inconsistent local chain fail closed.

The hashes are not signatures, remote attestation, or WORM storage. A local
administrator can rewrite or delete state. Capital-flow revisions are excluded
from Git, release artifacts, and the Cloudflare R2 market-cache allowlist.

## Read API

```text
GET /api/capital-flow
GET /api/capital-flow?date=2026-07-17&q=银行&sort=main_net_inflow&direction=desc&limit=100
```

Supported parameters are:

| Parameter | Contract |
|---|---|
| `date` | Canonical `YYYY-MM-DD`; omitted selects the latest revision not later than the completed-session cutoff |
| `q` | Non-empty board code/name substring, at most 100 characters |
| `sort` | `name`, `change_pct`, or any amount/percentage field for main, super-large, large, medium, or small net flow |
| `direction` | `asc` or `desc` |
| `limit` | Integer from 1 through 500 |

Unknown, repeated, empty, malformed, or unbounded parameters fail with HTTP
400. Filtering and sorting read only the local normalized revision. Missing
sort values are placed after present values. A filter with no matching boards
is a valid empty view, not a zero-flow or missing-source claim.

## Workstation States

The page separates provider quote date, completed-session cutoff, and page-read
time. It exposes distinct text states for:

- no published local snapshot;
- current, stale, or later-than-cutoff evidence;
- background refresh running;
- last refresh failed while an older snapshot remains intact;
- valid filters returning no boards; and
- incomplete optional metrics or provenance warnings.

Wide tables scroll inside their own keyboard-focusable region. Flow values use
signed amounts plus the words 净流入, 净流出, 净额持平, or 方向不可用; color is
supplemental. Filters and actions reflow to one column at 390 px while the two
commands remain keyboard reachable.

## Authority Boundary

Every response fixes:

```json
{
  "research_only": true,
  "execution_authorized": false
}
```

Capital-flow evidence cannot modify a strategy candidate, set a target weight,
update a paper or shadow ledger, create an order, satisfy a promotion gate,
change broker capability, or unlock live trading. A professional deployment
still needs licensed data, provider-methodology review, independent validation,
a correction policy, and explicit operational controls.
