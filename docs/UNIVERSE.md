# Universe And China-Market Rules

## Why The Default Has Eight ETFs

Eight is a configuration choice, not an engine limit. The initial universe was kept small so timing, transaction costs, risk stops, state persistence, and forward audit could be verified before introducing stock-specific data failure modes. The test suite includes a 12-security point-in-time universe to prevent an accidental eight-symbol limit.

Run the current eligibility audit with:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli universe-status --date 2024-01-02
```

`config/security_master.json` stores instrument identity, listing and delisting dates, asset class, risk group, lot and tick size, base price limit, dated universe memberships, and optional dated trading-status periods. `selection_method` and `provenance` are exposed in diagnostics so a curated list cannot silently present itself as unbiased index history.

The `v1.0.0` market workstation builds its security selector from this configured master and the requested point-in-time eligibility state. It does not contain an eight-symbol frontend list. Adding a valid instrument to the master and universe can make it selectable after the corresponding cache is published, but that mechanical availability does not satisfy the professional-data gates below.

## Rules Implemented

- A security enters the candidate set only while both its listing interval and selected universe-membership interval are effective.
- A configurable listing-seasoning interval applies before selection.
- Missing bars, zero volume/amount, explicit suspension, upper-limit buys, and lower-limit sells are rejected with an audit record.
- Required sells execute before buys. If a sell is blocked, the buy phase is cancelled to avoid silently increasing risk.
- ETF and stock fee schedules differ. Stock stamp duty and transfer fees switch by effective date.
- Target weights are capped by position, asset class, risk group, and average-amount participation constraints.

## Gate Before Adding CSI 300 Stocks

The engine can load hundreds or thousands of records, but a credible stock expansion needs all of the following:

1. Historical constituent additions and removals with source and effective timestamps.
2. Delisted securities and their last tradable sessions, not only surviving tickers.
3. Raw execution prices separated from adjusted research prices.
4. Point-in-time adjustment factors, cash dividends, splits, rights issues, mergers, and symbol changes.
5. Historical ST status, suspensions, board-specific price limits, IPO no-limit periods, and trading calendars.
6. Date-effective exchange, tax, broker, slippage, and capacity models.
7. A broad benchmark and sector-neutral factor evaluation that includes delisting outcomes.
8. Independent provider reconciliation and future broker-sandbox fills.

Until those data contracts exist, adding today's CSI 300 constituents would increase the symbol count while reducing the credibility of the result. The next data milestone is therefore a point-in-time stock dataset, not a larger hard-coded list.

## Verifying master-data dates against provider evidence

`universe-verify` binds the curated dates to the same trust boundary the rest
of the system uses — the verified local cache:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli universe-verify
```

For every configured instrument it reads the first and last cached bars and
reports one row with the configured listing date and membership windows. A
membership window that starts materially before the first cached bar (beyond
the configured data-window start plus a small holiday tolerance) is the
dangerous direction — the instrument would be visible in the historical
universe before any data exists — and is reported as an issue with a non-zero
exit code. Everything else is disclosure: history truncated at the data-window
start, provider history beginning after a lower-bound listing date, or a
listing date later than the first bar. Missing caches are counted separately
so the command is also useful before the first full `download`. The check is
read-only and `research_only`; it never edits the master, refreshes a
provider, or changes any permission.

The 2026-07-26 expansion's provenance: membership start dates are
conservative (at or after the believed listing date, never before), the
twelve highest-risk post-2016 entries were cross-checked against Eastmoney
fund establishment dates (establishment is a strict lower bound on the
exchange listing date; one correction resulted, 512720), and the first cached
bar remains the authoritative first completed session.
