# Research Monitoring Operations

AI Trade monitoring is a one-shot, research-only scan. It evaluates the
persisted watchlists and enabled rules against one validated completed-market
snapshot, writes immutable scan and alert evidence under `state/monitoring/`,
and exits. It does not run a web server, modify a strategy, write a paper
ledger, call a broker, or create an order.

## Manual Scan

From the project root, after `scripts/bootstrap.ps1` has completed:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli --config config/default.json monitor-scan --all-profiles
```

`--all-profiles` scans the local owner and enabled beta accounts that have at
least one enabled rule. Disabled or deleted beta accounts are not scanned. If
the beta-user store is invalid, the local-owner scan is still attempted, but
the CLI includes `profile_warning` and exits non-zero instead of silently
skipping beta users. One unavailable or malformed monitoring profile produces a
failed result without stopping later eligible profiles.

The command refreshes only when the configured cache is not current, then
reuses one validated snapshot for all profiles. It is safe to repeat only after
a complete result: the same profile, snapshot, and configuration return the
existing successful scan with `reused: true` rather than duplicate alert
records. `partial` and `failed` attempts are never reused.

Watchlists and rules are created from the authenticated workstation. The
scheduled sweep can see only profiles in this workspace; it does not sync
monitoring state to Cloudflare R2. Monitoring is limited to symbols present in
the configured security master and to completed daily bars.

When the current user has selected hybrid storage, the cache refresh step may
run the existing verified market-cache backup. A backup failure remains a
warning and does not rewrite a completed local scan. Watchlists, rules, alerts,
actions and scan records are outside the R2 allowlist.

## Windows Scheduled Task

Install a daily task at 18:20, ten minutes after the default paper task:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_monitor_task.ps1
Get-ScheduledTask -TaskName 'AI-Trade Research Monitor Daily'
Get-ScheduledTaskInfo -TaskName 'AI-Trade Research Monitor Daily'
```

The task is bound to the current Windows user, runs with limited privileges,
starts when a missed run becomes available, ignores a second instance while a
scan is active, and has a 20-minute execution limit with three 10-minute
retries. Like the paper task, the default `Interactive` logon profile requires
the Windows user to be logged in after a reboot. The task is not a permanent
PowerShell process or a background trading service.

The runner writes UTF-8 output to `logs/scheduled_monitor.log`. It rotates the
file at 5 MiB and retains the five newest archives. A non-zero CLI exit code is
passed to Task Scheduler, so invalid configuration, unavailable or inconsistent
market evidence, and failed writes remain visible as failed runs. A successful
scan with no triggered rules is still exit code 0. Cloud backup is independent
of this local research record.

Remove the task when it is no longer needed:

```powershell
Unregister-ScheduledTask -TaskName 'AI-Trade Research Monitor Daily' -Confirm:$false
```

Use the workstation's monitoring page to review the data date, source and
evidence fingerprint before acting on an alert. A monitoring alert is a research
prompt only; human review remains required and no alert unlocks paper or live
trading.

## Scan Result Semantics

- `succeeded`: every enabled rule was evaluated against the bound snapshot. A
  repeated request for the same owner, configuration, and snapshot may reuse
  this immutable result.
- `partial`: the scan completed but one or more rules were explicitly excluded,
  for example because a symbol had no usable completed bar. Valid rule evidence
  and alerts remain visible. Retrying creates a new attempt ID and reevaluates
  the exclusions.
- `failed`: the snapshot, rule evaluation, alert publication, or scan
  publication could not complete. A failed record is persisted whenever the
  store itself remains writable. Retrying creates a new attempt ID; a failed
  attempt is not treated as a cache hit and does not reset the last valid rule
  state used for transition or cooldown checks.

Alert files are published under an owner-local transaction marker. If an alert
write fails, alerts created by that attempt are removed before the failed scan
is recorded. If the final scan publication itself fails, the new alerts are
also rolled back and the CLI exits non-zero. Existing evidence from earlier
scans is never rewritten by this rollback. The marker is atomically staged in
`.staging/` and records the exact scan and alert fingerprints. If the process is
hard-terminated after alert publication, the next integrity-checked read
verifies the pair and either commits the complete scan or removes only the
uncommitted alerts. If the scan is present but one expected alert is missing,
the marker's newest scan and any remaining transaction alerts are rolled back
as one uncommitted unit, provided no alert action has been appended; a non-tail
scan or an action-bearing marker fails closed. A malformed marker or mismatched
file also fails closed. The private staging directory accepts only generated
temporary names and is cleaned on every owner-locked entry; residue above 64
files or 16 MiB is removed and reported as a capacity error before retry.
Windows publication uses same-volume write-through moves where available. This
recovery still is not a cross-platform guarantee against sudden power loss,
disk/controller loss, or privileged deletion. Restore a known-good versioned
backup rather than guessing which evidence file to delete when the marker also
contains committed actions.

## Alert Review States

The browser can acknowledge, snooze, dismiss, reopen, or unsnooze an alert.
Each operation appends an action record and requires the current alert-state
fingerprint, so a stale browser write receives a conflict instead of overwriting
a newer review. Configuration writes use the same compare-and-swap principle
with `expected_revision`.

`snooze_until` is a review date rather than a background timer. At the next scan
whose validated completed-session cutoff is on or after that date, the scanner
appends an automatic `unsnooze` action before rule evaluation. Loading the page,
leaving the workstation open, or reaching the date while the PC is off does not
run a timer. The alert remains snoozed until a later scan or a manual unsnooze.

## Local Notification Inbox

The monitoring response also exposes a small owner-scoped local inbox. It is a
delivery/read projection over immutable alert records and failed scan records;
it does not become a second source of truth for rule state. A new notification
is deterministically identified by its source type and source ID, so refreshing
the page or repeating a scan does not duplicate it. Each record retains the
source fingerprint, optional snapshot-evidence fingerprint, severity, message,
symbol, and data date. A changed or missing source binding fails closed.

`GET /api/monitoring` returns `notifications`, `notification_summary`, and
`notification_delivery`. The delivery mode is currently `local_inbox`; the
response deliberately reports that no external delivery channel is configured.
The workstation supports
`POST /api/monitoring/notifications/<notification-id>/actions` with
`mark_read`, `mark_unread`, or `dismiss`. Every transition is a new immutable
action and requires the current notification state fingerprint. Dismissing a
notification only archives the inbox entry; it does not acknowledge, close, or
reopen the underlying monitoring alert.

The page defaults to unread notifications and can filter by reading state,
severity, and source. The table retains the source ID, data date, generation
time, and evidence fingerprint, and remains a keyboard-focusable horizontally
scrollable region on narrow screens. Loading the inbox never refreshes market
data, changes a strategy, edits an accounting ledger, or calls a broker.

## Storage and Trust Boundary

Monitoring state is stored below
`state/monitoring/users/<sha256-owner>/` as bounded strict JSON configuration
revisions, scans, alerts, alert actions, notifications, and notification actions.
The repository ignores `state/`, release
verification rejects it, and the Cloudflare R2 exporter can read only the market
cache allowlist. Monitoring state therefore does not consume R2 quota and is not
restored by an R2 market-cache restore.

The store uses per-owner process and operating-system file locks, create-once
records, strict schemas, owner binding, SHA-256 content fingerprints,
configuration parent links, contiguous scan and alert-action sequences, and
scan/alert cross-reference checks. Historical configuration revisions are
loaded to rederive rule fingerprints and alert metadata, while persisted
snapshot evidence fields rederive alert evidence fingerprints. These controls
detect accidental corruption and many inconsistent edits, but all hashes remain
unkeyed local values; they do not provide a keyed signature, remote attestation,
or filesystem-enforced WORM storage.

A process with write access to `state/monitoring/`, especially a local Windows
administrator, can rewrite a JSON file and recalculate its SHA-256 or delete
records. The current store has no external durable collection head, so deletion
of the newest configuration or alert action, or deletion of a consistent tail
of scans and their alerts, can appear as a valid rollback. A lone alert deletion
is rejected while its parent scan still references it, but a privileged
operator can rewrite the related tail together. Do not present these local
fingerprints as proof against a privileged host operator. For stronger
retention, restrict directory ACLs and maintain an independent versioned backup
or signed/WORM export; that facility is not part of this release.

## Capacity and Retention

Monitoring uses bounded immutable files rather than an archive or compaction
service. One owner may have at most 50 watchlists, 500 symbols per watchlist
(2,000 symbols total), 500 rules, 1,000 configuration revisions, 2,000 scans,
5,000 alerts, 10,000 alert actions, 7,000 notification records, and 15,000
notification actions. These are lifetime record caps, not free-space estimates:
dismissing an alert or archiving a notification does not free action slots, and
deleting old files is not a supported retention operation because it can break
the evidence chain. At a cap, the API fails closed with a capacity conflict and
the scheduled CLI reports a non-zero result. A future verified checkpoint and
archive format is required before increasing or recycling these limits.
