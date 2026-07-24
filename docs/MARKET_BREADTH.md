# Market Breadth and Board Rankings

This document describes the `v0.18.1` `sector_breadth` evidence dataset. It is a
closing research view, not a real-time quote service, industry
classification license, sentiment score, strategy signal, or order source.

## Source Scope

One refresh reads two Eastmoney public endpoints for one explicit China trading
date:

| Evidence | Endpoint | Fixed request scope |
|---|---|---|
| Board ranking | `https://push2.eastmoney.com/api/qt/clist/get` | `fs=m:90+t:2`, 100 rows per page, every declared page |
| Market breadth | `https://push2.eastmoney.com/api/qt/ulist.np/get` | `1.000001`, `0.399001`, `0.899050` |

The three breadth identities are fixed to 上证指数, 深证成指, and 北证50.
Advance, decline, and unchanged counts are the values exposed in those three
responses. They are a provider-defined closing scope, not exchange-certified
market statistics.

The `m:90+t:2` result is described as a **provider-defined board universe**.
It can contain different board concepts and must not be relabelled as a
licensed pure-industry taxonomy. The security master's `sector` field remains a
separate point-in-time portfolio risk group and is not used to manufacture this
whole-market ranking.

## Refresh

Use the latest date in the validated local market cache:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli market-breadth-refresh
```

Or request one controlled historical date:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli market-breadth-refresh --date 2026-07-17
```

The **市场情报** page exposes the same fixed command as **刷新市场宽度**. A
browser GET never contacts Eastmoney. Refresh runs as an explicit background
job, and the previous complete revision stays readable while it is running or
if it fails.

## Validation Contract

No revision is published until both response families pass all checks:

- duplicate JSON keys and oversized responses are rejected before parsing;
- the board result must contain every declared page, with stable totals and no
  duplicate board codes;
- each row must use the exact allowlisted schema and contain bounded identity
  text, finite required numbers, and nonnegative counts;
- all source quote timestamps must resolve to the requested date in China
  Standard Time;
- the three benchmark codes, market identifiers, names, and exchange coverage
  must match the fixed allowlist;
- advance, decline, unchanged, total, share, and net-advance relationships must
  be internally consistent; and
- normalized boards and exchanges must use deterministic ordering.

Optional board metrics such as turnover, volume ratio, market capitalization,
or change amount preserve a source null as unavailable. They are never replaced
with zero. Response, normalized evidence, and full-record SHA-256 fingerprints
are retained for local review.

## Immutable Storage

Validated evidence is stored below the Git-ignored workspace state:

```text
state/market_intelligence/sector_breadth/YYYY-MM-DD/revision_NNNNNNNN.json
```

Repeating the same normalized breadth and board rows reuses the existing
revision. A changed same-date record set appends a new revision linked through
`supersedes`; it does not overwrite the old file. Atomic staging and strict
reads make an incomplete or inconsistent local chain fail closed.

The fingerprints detect accidental changes and many inconsistent edits. They
are not signatures, remote attestation, or WORM storage; a local administrator
can rewrite or delete state. This dataset is excluded from Git, release
artifacts, and the Cloudflare R2 market-cache allowlist.

## Read API

```text
GET /api/market-breadth
GET /api/market-breadth?date=2026-07-17&q=电力&sort=change_pct&direction=desc&limit=100
```

Supported parameters are:

| Parameter | Contract |
|---|---|
| `date` | Canonical `YYYY-MM-DD`; omitted selects the latest revision not later than the completed-session cutoff |
| `q` | Non-empty board code/name substring, at most 100 characters |
| `sort` | `change_pct`, `advance_share`, `turnover_rate`, `volume_ratio`, `market_cap`, `constituent_count`, or `name` |
| `direction` | `asc` or `desc` |
| `limit` | Integer from 1 through 500 |

Unknown, repeated, empty, malformed, or unbounded parameters fail with HTTP
400. Filtering and sorting operate only on the normalized local revision. The
unfiltered summary, source scope, coverage, freshness, fingerprints, warnings,
and complete revision history remain in the response. A filter with no matching
boards is a valid empty view, not a missing-source state.

## Workstation States

The page keeps the source date separate from the page-read time and the current
validated market-cache cutoff. It displays explicit states for:

- no published local snapshot;
- current, stale, or later-than-cutoff evidence;
- background refresh running;
- last refresh failed while an older snapshot remains intact;
- valid filters returning no boards; and
- incomplete provenance or optional source values.

Wide tables scroll inside their own region. Market direction uses signed values
and the words 上涨, 下跌, and 平盘; color is supplemental. Filter controls,
jump links, actions, and table scroll regions remain keyboard reachable.

## Authority Boundary

Every response fixes:

```json
{
  "research_only": true,
  "execution_authorized": false
}
```

Breadth and board evidence cannot modify a strategy candidate, set a target
weight, update a paper or shadow ledger, create an order, satisfy a promotion
gate, change broker capability, or unlock live trading. This single public
source also cannot make assistant sentiment coverage available. A future
professional deployment still needs licensed data, source terms review,
independent cross-checks, and an explicit correction policy.
