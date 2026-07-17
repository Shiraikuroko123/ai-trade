# Broker Adapter Boundary

AI Trade `v0.12.0`, the first public release, defines and tests a broker boundary. The core wheel intentionally ships no broker-specific implementation. The source repository now includes an independently installable QMT read-only observation plugin under `adapters/qmt`; it cannot place or cancel orders. Installing any adapter does not unlock live trading.

## Plugin Contract

Adapters register a factory in the Python entry-point group `ai_trade.brokers`:

```toml
[project.entry-points."ai_trade.brokers"]
example = "example_adapter:create_broker"
```

Every adapter must also publish immutable capability metadata before its factory
can be instantiated:

```toml
[project.entry-points."ai_trade.broker_capabilities"]
example = "example_adapter:broker_capabilities"
```

The declaration assigns one access level (`read_only`, `sandbox`, or `live`),
an explicit environment set, and an allowlist drawn from `read_account`,
`read_positions`, `read_orders`, `read_fills`, `submit_orders`, and
`cancel_orders`. A missing declaration, unknown operation, environment mismatch,
or runtime/declaration mismatch is denied before the core calls the adapter.

The factory receives `AppConfig` and a `BrokerEnvironment` (`sandbox` or `live`) and returns a subclass of `ai_trade.broker.base.Broker`. The adapter must implement:

- health and trading-session status;
- account cash, available cash, and equity;
- positions and available sell quantity;
- open orders;
- batch limit-order submission;
- cancellation;
- fills since an optional timestamp.

Credentials stay in the adapter's local secret storage. They must never appear in `config/default.json`, logs, reports, authorization files, Git, or exception messages.

## Optional QMT Read-Only Probe

The `ai-trade-qmt` plugin is the first broker-specific integration milestone. It
connects only to a user-owned, already logged-in QMT/miniQMT process on Windows
and reads account assets, positions, cancelable orders, and today's fills.

Its authority is deliberately narrow:

- the plugin accepts `sandbox` construction only and rejects `live`;
- submission and cancellation fail before an xtquant write method can run;
- QMT does not expose a trustworthy paper/live discriminator, so the configured
  environment is not considered broker-verified;
- `broker-probe` and `broker-compare` never append to
  `state/broker_reconciliation.csv` and cannot contribute to the 20-session gate;
- no QMT binary, DLL, credential, account, or installation path is bundled;
- xtquant trade objects differ by broker/version. Commission is used when
  present, separately reported tax is unavailable, and zero must not be read as
  proof that the broker charged no fee.

Install from a source checkout after `scripts/bootstrap.ps1`:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-deps -e .\adapters\qmt
```

Keep the account identifier out of tracked `config/default.json`. Create a local,
Git-ignored configuration instead:

```powershell
$Directory = Join-Path (Get-Location) 'local\qmt'
$ConfigPath = Join-Path $Directory 'config.json'
New-Item -ItemType Directory -Force $Directory | Out-Null

$Config = Get-Content .\config\default.json -Raw -Encoding UTF8 | ConvertFrom-Json
$Config.broker.mode = 'sandbox'
$Config.broker.adapter = 'qmt-readonly'
$Config.broker.account_id = Read-Host 'QMT account ID'
$Json = $Config | ConvertTo-Json -Depth 100
[System.IO.File]::WriteAllText(
    $ConfigPath,
    $Json,
    (New-Object System.Text.UTF8Encoding($false))
)
```

Start and log in to the broker-provided QMT client. In the same PowerShell used
for the probe, set the `userdata_mini` directory and, only when Python cannot
already import xtquant, the directory that directly contains the `xtquant`
package:

```powershell
$env:AI_TRADE_QMT_USERDATA_PATH = 'D:\BrokerQMT\userdata_mini'
$env:AI_TRADE_QMT_PYTHON_PATH = 'D:\BrokerQMT\bin.x64\Lib\site-packages'
# Optional when another process already uses the generated session ID:
# $env:AI_TRADE_QMT_SESSION_ID = '987654'
```

Paths vary by broker. Use the xtquant build supplied with the installed QMT
client; the project deliberately does not download a third-party mirror.

Run the read-only checks:

```powershell
$Python = '.\.venv\Scripts\python.exe'
& $Python -m ai_trade.cli --config .\local\qmt\config.json broker-list
& $Python -m ai_trade.cli --config .\local\qmt\config.json broker-probe
& $Python -m ai_trade.cli --config .\local\qmt\config.json broker-compare
```

`broker-list` includes the static capability declaration. For QMT it must show
`access_level=read_only`, only the four read operations, `sandbox` as the sole
environment, and `runtime_environment_verified=false`. Treat any other result
as an installation mismatch and do not continue.

`broker-probe` masks the account identifier in console output. Cash, positions,
orders, and fills remain sensitive local information and are not written by the
command. `broker-compare` compares account cash and positions with the local
paper account, reports differences, and explicitly records no promotion
evidence.

## Shadow Account CSV Review

The Trading view accepts a canonical, read-only fill export independently of any
installed broker plugin. This is the fastest way to compare manually exported
broker fills with the current local paper ledger without granting broker access.
The UTF-8 header is exact and versioned by its ordered columns:

```csv
fill_id,order_id,symbol,side,quantity,price,commission,tax,filled_at
```

Sides are uppercase `BUY` or `SELL`; symbols are six digits; quantities are
positive integers; numeric values must be finite; and `filled_at` is an ISO-8601
timestamp with an explicit offset. The UI supplies a source label and a local
account alias. Users must not enter a real account identifier in that alias.

The importer validates the entire file before writing, caps one file at 1 MB and
5,000 rows by default, and never retains the source file. It appends normalized
fills to `state/shadow_fills.csv` and import provenance to
`state/shadow_imports.csv`. Stable fill identities detect overlap between
exports; exact files are idempotent; a changed payload for an existing source
fill ID rejects the whole batch. Every normalized fill and import row carries a
recomputed SHA-256 content fingerprint. Both ledgers are user-scoped local state
and are excluded from Git, R2, reports, and release artifacts.

Review groups actual and paper fills by date, symbol, and side. It reports
behavior coverage, sign-aware price deviation relative to the modeled paper
fill, and total-variation distance between actual and modeled trade-notional
allocations. This is not cash/position reconciliation: an export does not prove
opening holdings, account identity, broker environment, or fee completeness.
Shadow results never append qualifying reconciliation evidence and cannot
promote a strategy, authorize a broker, or submit/cancel an order.

## Order Lifecycle Recovery

The core persists broker order snapshots in `state/broker_orders.csv` and
standard fills in `state/broker_fills.csv`. `append_broker_observation` is the
preferred polling boundary for a future sandbox or live-capable adapter. It
locks both ledgers in deterministic path order, validates the complete
prospective lifecycle, and only then appends new order events followed by fills.
Exact retries are idempotent. If a process stops between those two durable
writes, the next poll can repeat the same observation and repair the missing
fill side without duplicating the already written order event.

Writers are serialized inside one process before taking the operating-system
lock. Each individual CSV update is assembled in a same-directory temporary
file, flushed, and atomically published, so a row-write or replace failure
preserves the prior complete ledger. The order and fill files remain two
separate commits; an interruption between them is repaired by the exact retry
described above. Local submission intents use midnight in China Standard Time,
not UTC midnight, so early-morning broker responses retain the correct event
order for the China trading session.

New order events use a `v2_`-prefixed SHA-256 fingerprint over the canonical
event payload. New fill rows retain the broker `fill_id` as their idempotency
key and add a full canonical `record_sha256`. Both fingerprints are recomputed
on every read, so an accidental order-field, fill-price, commission, or tax
rewrite fails closed. Header-only files are validated against the same exact
schemas before they can be treated as empty ledgers.

Existing 24-hex-character legacy event IDs and fill CSVs without
`record_sha256` remain readable. Exact retries do not migrate or duplicate
those rows, and appending another fill to a legacy CSV preserves its old schema
instead of making the historical file appear content-bound. Numeric ledger
fields compare by parsed integer or floating-point meaning, so an older `10`
and a canonical `10.0` do not create a false conflict during recovery. These
local fingerprints are corruption evidence, not keyed signatures or a
replacement for filesystem permissions, backups, and broker reconciliation.

`state/broker_ledger_scope.json` is a separate, atomically published scope
manifest for the order/fill pair. It binds the adapter, declared sandbox/live
environment, active live-configuration fingerprint, resolved ledger paths, and
a SHA-256 account reference. The plaintext broker account ID is never stored in
the manifest or returned by the lifecycle report; the UI exposes only a
12-character hexadecimal reference for human comparison.

The reconciliation, order, fill, scope, authorization, batch-approval, and
kill-switch paths must also resolve to different files. Configuration loading
compares paths against the configuration root, so an alias such as
`state/a/../broker_orders.csv` or an equivalent absolute path cannot bypass the
isolation check.

`append_broker_observation` can initialize the manifest only when both lifecycle
ledgers are absent. Individual scoped order, intent, and fill writes require an
already matching manifest. The live router consumes the exact batch approval,
then creates or verifies the scope before intent reservation and broker I/O. A
different adapter, account, environment, configuration, or ledger path fails
closed. Existing unscoped CSV files remain readable with an `UNSCOPED` warning,
but scoped writers refuse to adopt or append them; archive them and begin with
empty paths instead of fabricating a migration.

Snapshots use the following state model:

- `PENDING_SUBMIT` may advance to any broker-observed state because submission
  can fail or polling can miss an intermediate acknowledgement;
- `SUBMITTED` may become partially/fully filled, cancel-pending, cancelled,
  rejected, or expired;
- `PARTIALLY_FILLED` may accumulate more fills, become cancel-pending, finish,
  cancel with a remainder, or expire;
- `CANCEL_PENDING` may return to submitted when cancellation is rejected,
  receive partial or complete fills while cancellation races the market, or end
  cancelled/expired;
- `FILLED`, `CANCELLED`, `REJECTED`, and `EXPIRED` are terminal and cannot change
  to a different state.

Filled quantity must never decrease. `PARTIALLY_FILLED` requires a quantity
strictly between zero and the order size; `FILLED` requires the full quantity;
rejected and unfilled submission states cannot contain fills. An average fill
price is required exactly when cumulative filled quantity is positive. Client
order identity, symbol, side, original quantity, limit price, and the first
non-empty broker order ID are immutable. Reusing one broker order ID for two
client orders fails closed.

Persisted events are reduced by timezone-aware broker timestamp rather than CSV
append position. A late event can therefore be inserted into its historical
position without regressing the current state. A later stale regression, a
filled-quantity decrease, or an illegal terminal transition is rejected.
Physical duplicate/conflicting rows, malformed schemas, non-canonical values,
or reused fill IDs also fail validation.

`recover_order_lifecycle` reconstructs current states from disk after every
process start. It cross-checks each fill's client/broker IDs, symbol, side,
aggregate quantity, and quantity-weighted average price against the latest
order snapshot. The report explicitly distinguishes `EMPTY`, `VERIFIED`,
`RECOVERED`, and `INTEGRITY_ERROR`; valid out-of-order recovery and a history
that began mid-order remain visible warnings. Operating-system file locks are
released automatically on process exit, so a leftover `.lock` file is not
itself a blocker after a crash.

Lifecycle verification is local accounting evidence only. It never calls an
adapter, submits or cancels an order, appends a qualifying sandbox
reconciliation, changes a strategy, consumes an approval, or grants live
authority. The current QMT plugin deliberately keeps its probes non-persistent
and therefore does not populate these ledgers.

## Promotion Evidence

An account reaches sandbox review only after the frozen paper epoch passes its independent-forward gate. Sandbox reconciliation compares expected cash and positions against the broker and appends one idempotent record per adapter, account, date, and configuration fingerprint.

The default gate requires 20 consecutive clean reconciliations. A mismatch resets the clean-session run; evidence from another account or configuration does not count.

New reconciliation rows use a `v2_` SHA-256 fingerprint over the canonical
adapter, account, date, configuration, cash values, issue count, and issue
details. Every audit validates the exact schema and every row in the shared
ledger before selecting the configured account. A malformed row, changed
fingerprint, duplicate logical session, or non-increasing date fails closed.
Different content for an existing logical session is atomically retained as a
conflict before the writer raises, so the earlier clean row cannot silently keep
its authority.

Reconciliation writers use the same in-process serialization, operating-system
lock, flushed temporary file, and same-directory atomic replacement as broker
lifecycle ledgers. Exact retries are idempotent and replacement failure preserves
the previous complete file. Existing 24-character identity-only IDs remain
readable and an exact retry does not rewrite them, but those legacy rows are
excluded from the consecutive clean-session count because their cash and issue
content was never fingerprinted. Accumulate fresh `v2_` sessions after upgrade;
do not edit or manually migrate the ledger.

## Live Authorization

Live authorization is a local schema-versioned, expiring JSON record containing:

- `schema_version: 2`;
- `approved: true`;
- a non-empty human approver and timezone-aware approval timestamp;
- adapter name;
- account ID;
- the active live configuration fingerprint;
- a timezone-aware expiry timestamp;
- an exact mandate symbol allowlist and side allowlist;
- mandate order, daily-notional, and daily-order-count ceilings;
- `require_batch_approval: true`.

Unknown authorization or mandate fields are rejected instead of ignored. Mandate
limits may tighten configured hard limits but cannot expand them. An adapter must
separately declare the complete `live` operation surface, verified runtime
environment, and qualifying reconciliation support. The QMT read-only plugin
cannot satisfy these checks.

Authorization is necessary but not sufficient. The runtime also requires
`broker.mode=live`, `AI_TRADE_LIVE_CONFIRMATION=I_ACCEPT_LIVE_TRADING_RISK`, an
installed live-capable adapter, a clear kill-switch file, current paper evidence,
and eligible reconciliation evidence.

Each exact order batch additionally requires a short-lived approval record at
the configured `batch_approval_file`. Its SHA-256 fingerprint binds the order
date, adapter, account, live configuration, client IDs, symbols, sides,
quantities, limit prices, time-in-force values, and ordering. Approval lifetime
is limited to 15 minutes. A valid record is atomically moved to a uniquely named
retained `.consumed.json` audit record before intent reservation or broker submission; it
cannot authorize a retry or a changed batch. Each resulting `PENDING_SUBMIT`
event embeds the approval ID and exact batch fingerprint in a canonical message
that is covered by the event's content fingerprint, providing a durable link
between review and reservation. There is intentionally no live
order UI or approval generator while no verified live adapter exists.

## Pre-Trade Validation

`LiveOrderRouter` fails closed before calling the adapter. It checks:

- unique client order IDs and append-only intent reservations;
- empty order metadata until a bounded cross-adapter schema is defined;
- active point-in-time universe membership and tradable status;
- positive whole-lot quantities and tick-aligned positive limit prices;
- previous-close daily price-limit bounds;
- broker-available positions for sells and cash for buys;
- configured maximum order notional;
- mandate symbol/side, order, daily-notional, and daily-order-count limits;
- a matching short-lived one-time batch approval;
- an exact broker-ledger scope binding for adapter, account, environment,
  configuration, and order/fill paths;
- previously reserved plus new daily notional and order count;
- exact live broker environment and a healthy trading session.

Adapters must still implement broker-specific validation and reconciliation. Core checks do not replace exchange or broker rules.

## Development Sequence

1. Implement sandbox read methods and health reporting. The optional QMT probe now covers this stage without trusting its environment label.
2. Compare account and positions without submitting orders. The QMT diagnostic covers comparison but intentionally does not produce qualifying reconciliation evidence.
3. Implement sandbox limit orders, cancellations, order polling, and fills.
4. Accumulate the configured consecutive clean reconciliations.
5. Test disconnects, partial fills, duplicates, stale quotes, rejects, cancellation races, and restarts.
6. Review credentials, logs, kill switch, mandate, one-time approvals, and audit files.
7. Add live environment support only after a separate human review.
