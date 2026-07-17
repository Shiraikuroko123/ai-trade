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

## Promotion Evidence

An account reaches sandbox review only after the frozen paper epoch passes its independent-forward gate. Sandbox reconciliation compares expected cash and positions against the broker and appends one idempotent record per adapter, account, date, and configuration fingerprint.

The default gate requires 20 consecutive clean reconciliations. A mismatch resets the clean-session run; evidence from another account or configuration does not count.

## Live Authorization

Live authorization is a local, expiring JSON record containing:

- `approved: true`;
- adapter name;
- account ID;
- the active live configuration fingerprint;
- a timezone-aware expiry timestamp.

Authorization is necessary but not sufficient. The runtime also requires `broker.mode=live`, `AI_TRADE_LIVE_CONFIRMATION=I_ACCEPT_LIVE_TRADING_RISK`, an installed adapter, a clear kill-switch file, current paper evidence, and eligible reconciliation evidence.

## Pre-Trade Validation

`LiveOrderRouter` fails closed before calling the adapter. It checks:

- unique client order IDs and append-only intent reservations;
- active point-in-time universe membership and tradable status;
- positive whole-lot quantities and tick-aligned positive limit prices;
- previous-close daily price-limit bounds;
- broker-available positions for sells and cash for buys;
- configured maximum order notional;
- previously reserved plus new daily notional;
- exact live broker environment and a healthy trading session.

Adapters must still implement broker-specific validation and reconciliation. Core checks do not replace exchange or broker rules.

## Development Sequence

1. Implement sandbox read methods and health reporting. The optional QMT probe now covers this stage without trusting its environment label.
2. Compare account and positions without submitting orders. The QMT diagnostic covers comparison but intentionally does not produce qualifying reconciliation evidence.
3. Implement sandbox limit orders, cancellations, order polling, and fills.
4. Accumulate the configured consecutive clean reconciliations.
5. Test disconnects, partial fills, duplicates, stale quotes, rejects, cancellation races, and restarts.
6. Review credentials, logs, kill switch, order limits, and authorization expiry.
7. Add live environment support only after a separate human review.
