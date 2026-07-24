# Paper Trading Operations

This document starts from the public `v0.18.1` paper-account baseline. Paper execution remains isolated from the read-only market chart, AI assistant, strategy-lab candidate editing, cloud backup, and the unavailable live-broker route. The persistent research-digest, market-intelligence, and webhook task sections are included in the `v0.18.1` wheel and remain derivative, research-only evidence.

## Daily Flow

```powershell
python -m ai_trade.cli paper-run
python -m ai_trade.cli paper-audit
python -m ai_trade.cli archive-generate --kind all
```

`paper-run` refreshes the complete market snapshot, processes every missing benchmark session in order, simulates pending orders, applies portfolio risk controls, and writes state plus append-only trade, rejection, and equity ledgers. A blocked required sell cancels that session's buy phase. Repeating a completed date is idempotent.

`paper-audit` checks schema, unique session IDs, strict date ordering, state-to-ledger reconciliation, configuration fingerprints, forward metrics, and promotion gates. `state/paper_rejections.csv` remains available for execution-quality review even when an order never became a trade.

`archive-generate` is a separate research-only step. It reads the completed
paper ledger, matching `reports/paper_YYYYMMDD.json` files, and the current
owner's research journal, then appends immutable daily and weekly digest
revisions. It does not refresh market data, run the strategy, write paper
accounting, call a broker, or alter promotion gates. Repeating the command with
unchanged evidence is idempotent; changed evidence creates a linked
`supersedes` revision. See [Persistent Research Digests](RESEARCH_DIGESTS.md)
for the query contract, storage tree, limits, and recovery procedure.

Without a period filter, one `archive-generate --kind all` invocation writes or
reuses at most the newest 52 daily and newest 52 weekly periods. It is not a
full-history migration. Backfill an older period with one explicit
`--kind daily --date YYYY-MM-DD` or `--kind weekly --week ISO-MONDAY` command.

The active `paper_state.json` is a bounded, versioned accounting record. Duplicate
JSON keys, unknown fields, invalid dates, non-finite or negative balances,
malformed positions or target weights, and configuration drift fail closed before
the simulator, auditor, or browser can use the state. Do not hand-edit it; start a
new archived epoch when the frozen configuration changes.

State and report updates are written to a same-directory temporary file, flushed
to disk, and atomically replaced. A replacement failure leaves the previous
complete file in place; a leftover temporary file is not an active account state.
When a date has already been processed, the cached report is checked against the
account state before it is returned. A malformed or inconsistent report produces
an explicit fallback summary rather than overriding the ledger's cash or position
values.

The same operations are available in the loopback-only workstation:

```powershell
python -m ai_trade.cli serve
```

Use the Portfolio view for account state and pending targets, Trading for fills/rejections and promotion checks, Risk for independent gates, and System for serialized jobs and logs. The one-time `paper-init` job never passes `--overwrite`; an existing account therefore cannot be replaced from the browser.

## Configuration Changes

Changing the strategy, risk, dated fee tables, security-master contents, selected universe, provider, adjustment policy, or market close invalidates the active account fingerprint. Review the change, then explicitly archive the epoch:

```powershell
python -m ai_trade.cli paper-init --overwrite
```

A strategy-lab approval or activation does not modify this default account. Its exported configuration forces `broker.mode=disabled` and uses candidate-specific state and report paths; the operator must initialize and advance that separate paper profile explicitly.

Each paper account epoch has its own digest namespace. `paper-init --overwrite`
moves the previous paper evidence to `state/archive/` and creates a new
`account_id`; new digest writes cannot attach to the old account's revision
chain. Existing digest files remain under the old account-epoch hash rather than
being moved by `paper-init`; keep that directory with the archived reports as
offline retention evidence. The current page, HTTP API, and CLI always bind to
the active account and cannot select the old epoch. Do not replace or hand-edit
active paper state to make old digests visible; a supported old-epoch viewer or
export verifier has not been implemented.

## Promotion

The first gate requires at least 60 independent future sessions, a clean ledger, drawdown within the configured limit, positive forward Sharpe, and nonnegative benchmark-relative return. Passing only permits broker-sandbox review. It never enables live trading.

Research monitoring is a separate read-only workflow. To install its 18:20
one-shot scan, see [监控与告警运维](MONITORING.md); it does not advance or
authorize this paper account.

## Staggered Close Tasks

The supported Windows schedule separates accounting, monitoring, and archive
work. These are independent wall-clock tasks, not a dependency chain; a later
task may start while an earlier task is still running or retrying:

| Time | Task | Scope and effect |
|---|---|---|
| 18:10 | `AI-Trade Paper Daily` | Refreshes data, advances the local paper account, audits it, and then attempts a local-owner digest generation. |
| 18:20 | `AI-Trade Research Monitor Daily` | Runs one completed-snapshot research scan; it has no strategy, accounting, broker, or order authority. |
| 18:30 | `AI-Trade Research Archive Daily` | Runs `archive-generate --all-profiles --trigger scheduled` and appends owner-isolated daily/weekly digests without provider refresh or broker access. |

Each task must be checked through its own result and log. Monitoring follows its
own refresh/fallback contract, while the archive runner uses the latest completed
local evidence it can safely read; neither start time proves that the 18:10 task
completed. The `scheduled` value is an operator-supplied CLI audit label used by
the bundled runner, not authenticated Task Scheduler provenance.

Install the independent archive task with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_archive_task.ps1
Get-ScheduledTask -TaskName 'AI-Trade Research Archive Daily'
```

Its output is appended to `logs/scheduled_archive.log`. A non-zero exit means
one or more profiles could not be processed; inspect the log and retry. Existing
revisions are never overwritten. Unregister it with:

```powershell
Unregister-ScheduledTask -TaskName 'AI-Trade Research Archive Daily' -Confirm:$false
```

The archive task processes the local owner and enabled beta profiles only. It
does not synchronize digest files to Cloudflare R2; the cloud exporter remains
limited to validated market-cache objects.
