# Persistent Research Digests

> **Release status:** the `v1.0.0` wheel contains the research-digest surface,
> `archive-generate`, monthly read-time projection, archived-epoch browser,
> optional R2 digest staging, these HTTP routes, and the Windows archive-task scripts.
> Digests remain derivative, owner-scoped research evidence rather than an
> accounting or execution authority.

Research digests are the durable, owner-scoped close-of-day record for the
research workflow. They materialize the read-only closing-archive projection
from already-authoritative local evidence. A digest is a research artifact, not
a new accounting ledger and not a trading instruction.

The source evidence remains authoritative:

- `state/paper_equity.csv` is authoritative for the paper account's equity and
  session quantities;
- `reports/paper_YYYYMMDD.json` is authoritative for the immutable paper daily
  report; and
- the authenticated owner's append-only research journal is authoritative for
  human notes and review context.

The generator only reads those sources and appends a digest record. It does not
refresh a provider, run a strategy, write a paper or shadow ledger, call a
broker, change a strategy, or grant live authority.

## What Is Stored

Each daily digest represents one `as_of_date`; each weekly digest represents an
ISO Monday and its Sunday period end. The record includes the projection
payload, source evidence fingerprints, the active paper configuration
fingerprint, an account-epoch fingerprint, actor, trigger, status, and a fixed
authority declaration:

```text
research_only=true
execution_authorized=false
strategy_changed=false
paper_account_changed=false
broker_permissions_changed=false
```

The Research page also projects natural-month summaries from daily evidence.
Monthly rows are read-time views, not a third persistent digest kind; they do
not change the daily/weekly store schema or generate additional revisions.

Records are immutable. Repeating a generation with the same canonical payload
and source evidence reuses the newest revision (idempotent). If evidence or the
payload changes, a new revision is appended with `supersedes` and
`supersedes_fingerprint` pointing to the previous revision. Volatile fields
such as `created_at`, `actor`, and `trigger` do not create a revision when the
evidence is otherwise identical.

The store validates canonical JSON, owner/account/kind/date bindings, the
revision sequence, parent links, and SHA-256 fingerprints on every read. These
hashes provide local tamper detection; they are not digital signatures, WORM
storage, or protection from a local administrator who can rewrite or remove an
entire tail. Keep a separate controlled backup if the records are needed for
long-term audit.

## Local Storage and Isolation

The default configuration is:

```json
{
  "research_digest": {
    "root_dir": "state/research_digests"
  }
}
```

The path must remain a child of the workspace `state/` directory. The store
rejects the workspace root, an empty path, paths outside the project, symbolic
links, unexpected members, and ambiguous revision files. A typical layout is:

```text
state/research_digests/
├── .staging/                         # transient, outside verified chains
└── users/<owner-sha256>/
    └── accounts/<account-epoch-sha256>/
        ├── .account.lock
        └── digests/
            ├── daily/<YYYY-MM-DD>/revision_00000001.json
            └── weekly/<ISO-MONDAY>/revision_00000001.json
```

The username is not placed in the path. The account epoch is derived from the
paper `account_id`, so `paper-init --overwrite` starts a new digest namespace;
records from an old paper epoch are retained but can never be silently joined
to the new account. Each owner and account is locked independently, and writes
are staged outside the verified chain and atomically published. A crash before
publication can leave only an ignored staging residue, not an empty formal
chain. A failure after publication is reported with the visible revision in the
committed prefix, so operators are not told to repeat a write that already
became immutable.

Current digest-generation and active digest-list APIs always derive the account
epoch from `paper_state.json`. The Research page has a separate read-only
browser for validated `state/archive/YYYYMMDD_HHMMSS/` epochs and their matching
digest namespaces. It never copies, merges, restores, reactivates, or replaces
active state and does not expose raw account IDs. Because journal records do not
carry a paper-account epoch binding, archived projections exclude journals and
show that limitation explicitly instead of attaching a same-date note to the
wrong account.

The store is bounded: at most 2,000 period chains per account and 2,000
revisions per chain. There is no automatic deletion or compaction. A capacity
error stops the attempted write and is reported as HTTP 409 by the workstation.

## Command Line

Run `paper-init` once before generation. The default scope is the loopback
owner (`local-owner`):

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli paper-init
.\.venv\Scripts\python.exe -m ai_trade.cli archive-generate
```

Without `--date` or `--week`, one invocation materializes at most the newest 52
daily periods and the newest 52 weekly periods available in the validated
projection. It is not an all-history backfill. Generate an older period
explicitly, one period at a time:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli archive-generate --kind daily --date 2026-06-30
.\.venv\Scripts\python.exe -m ai_trade.cli archive-generate --kind weekly --week 2026-06-22
```

Useful bounded variants:

```powershell
# Only one kind, or one period.
.\.venv\Scripts\python.exe -m ai_trade.cli archive-generate --kind daily --date 2026-07-17
.\.venv\Scripts\python.exe -m ai_trade.cli archive-generate --kind weekly --week 2026-07-13

# Generate for the local owner and every enabled beta profile.
.\.venv\Scripts\python.exe -m ai_trade.cli archive-generate --all-profiles `
  --trigger scheduled

# Generate one enabled beta account by username.
.\.venv\Scripts\python.exe -m ai_trade.cli archive-generate `
  --username example-user --kind all
```

`--date` and `--week` are mutually exclusive. A week must be an ISO Monday.
The CLI's `--trigger` is `manual` or `scheduled`; it is an audit label, not an
authority grant or authenticated scheduler identity. Any local operator can
pass either value; `scheduled` is the convention used by the bundled task
runner. The digest stores creation metadata, not a trusted invocation log. A
successful command prints per-profile counts for `written` and `reused`. It
exits non-zero when a profile cannot be read safely or no valid paper account
is available. A `partial` evidence status with an empty `errors` list means the
incomplete source evidence was archived successfully and remains visible for
review. A batch write interruption also reports `partial`, but includes an
error and exits non-zero after disclosing the committed prefix.

The scheduled Windows flow uses three deliberately separate, staggered tasks:

```powershell
# 18:10 paper refresh, audit, and local-owner digest (run_daily_paper.ps1)
powershell -ExecutionPolicy Bypass -File .\scripts\install_paper_task.ps1

# 18:20 owner-scoped research monitor
powershell -ExecutionPolicy Bypass -File .\scripts\install_monitor_task.ps1

# 18:30 all-profile persistent digest generation
powershell -ExecutionPolicy Bypass -File .\scripts\install_archive_task.ps1
Get-ScheduledTask -TaskName 'AI-Trade Research Archive Daily'
```

The archive runner uses the installed virtual environment, invokes
`archive-generate --all-profiles --trigger scheduled`, and appends to
`logs/scheduled_archive.log`. It does not start a resident service. If a task
fails, inspect the log and rerun the same command after fixing the underlying
evidence; an already committed revision is never overwritten. Remove it with:

```powershell
Unregister-ScheduledTask -TaskName 'AI-Trade Research Archive Daily' -Confirm:$false
```

The successful `run_daily_paper.ps1` flow also performs a local-owner digest
generation after `paper-run` and `paper-audit`. The separate 18:30 task is what
ensures enabled beta profiles are processed and safely retries current evidence
after an earlier failure.

The 18:10, 18:20, and 18:30 times are offsets, not dependencies. Windows Task
Scheduler may still be running or retrying an earlier task when a later task
starts. Each runner executes its own contract; the archive runner uses the
latest completed evidence it can safely read. Operators should inspect each
task result and log rather than infer that the earlier stage completed from the
wall-clock order.

## HTTP Contract

The Research page reads the latest revision of each chain:

```text
GET /api/research/digests
    ?kind=all|daily|weekly
    &date=YYYY-MM-DD
    &week=YYYY-MM-DD       # ISO Monday
    &limit=1..200
    &revisions=0|1
```

`date` and `week` cannot be combined. Supplying `date` selects `daily`; supplying
`week` selects `weekly`. If `kind` is also supplied it must match that period
field, so `kind=all&date=...` and `kind=daily&week=...` return HTTP 400 instead
of mixing chains. `revisions=1` returns the immutable revision history instead
of only the newest record per period. Responses expose
status (`current`, `provisional`, `partial`, `empty`, or `unavailable`), source
and account fingerprints, truncation/limit information, and the fixed authority
declaration. An unfinished ISO week is `provisional` and receives a finalized
revision after the week closes. Missing or corrupt input is reported as
unavailable or partial evidence; it is never converted to zero values.

Generation is a CSRF-protected, authenticated write route:

```text
POST /api/research/digests/generate
{
  "kind": "all|daily|weekly", // optional; must match date/week when present
  "date": "YYYY-MM-DD",       // optional; daily only
  "week": "YYYY-MM-DD"        // optional; weekly only, ISO Monday
}
```

Omitting `kind` while sending `date` or `week` infers `daily` or `weekly`.
Explicit `all` cannot be combined with a single-period field.

The server supplies the owner and actor from the session. Request fields cannot
select another account, inject an owner, set `trigger`, or add execution
permissions. HTTP generation is always recorded as `manual`; scheduled
labeling is available only through the local CLI and is not authenticated
scheduler provenance. The loopback `--owner-local` mode is intended only for a
trusted local workstation. Generation returns `201`
when at least one revision is newly written, `200` for an idempotent/no-op
result, `409` for a capacity conflict, and an explicit failure status for
unavailable evidence.

## Cloud, Git, and Recovery Boundary

Digest files are local application state. The repository ignores
`state/research_digests/`; release verification rejects `state/` members. The
Cloudflare R2 market-cache exporter remains allowlisted to validated
`data/cache/*.csv` and `data/cache/manifest.json`. A separate digest exporter
can read only validated `daily/` and `weekly/` revision chains for the active
local owner and paper epoch. It never uploads journals, reports, paper ledgers,
broker files, assistant state, credentials, logs, or raw owner/account IDs.
Storage-page capacity does not include digest objects; high-level R2 requests
still enter the same local A/B observation ledger.

After configuring private R2, use:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-digest-backup
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-digest-list --limit 20
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-digest-restore <snapshot-id>
```

Backup verifies the complete local namespace before and after reading, uses
hashed owner/account identities, and deduplicates an unchanged dataset. Restore
checks remote object metadata, ZIP limits, safe paths, manifest and file hashes,
record schemas, owner/account/config bindings, revision numbering,
`supersedes` links, and record fingerprints. It writes only a new
`local/cloud-digest-restore/<snapshot-id>/` staging directory and returns
`active_state_unchanged: true`; it never overwrites `state/research_digests/` or
adopts an archived account. These SHA-256 checks are not signatures or remote
attestation, so bucket access and independent retention still matter.

## Verification Checklist

1. Confirm `paper-init` created an active account and a stable configuration
   fingerprint.
2. Run `archive-generate --kind all` once and record the JSON `written` and
   `reused` counts. This covers at most the newest 52 daily and 52 weekly
   periods; use one explicit `--date` or `--week` command for each older period
   that must be backfilled.
3. Run it again with the same evidence; the second run should reuse the same
   revisions rather than append duplicates.
4. Open **Research** and inspect the digest status, period, source fingerprints,
   revision timeline, and authority declaration.
5. Optionally run `cloud-digest-backup`, list the snapshot, and restore it to a
   new staging directory; confirm active state remains unchanged.
6. If a report or ledger is changed, do not repair it by editing a digest. Fix
   the authoritative source and run generation again; a new `supersedes`
   revision will preserve the previous evidence.

Persistent digests improve reproducibility and review discipline. They do not
make historical performance predictive, constitute investment advice, or enable
real-money trading.
