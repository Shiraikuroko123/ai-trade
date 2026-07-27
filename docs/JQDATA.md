# JQData onboarding and knowledge-time security master

AI Trade treats JQData as a licensed, account-scoped evidence source. It is not
yet a primary or fallback market snapshot provider. The first integration gate
checks the installed SDK, account entitlement, licensed date range, expiry and
actual query quota without requesting any security master or market bars.

Official references:

- [JQData API index](https://www.joinquant.com/help/api/doc)
- [Login and account API](https://www.joinquant.com/help/api/doc?name=logon&id=9821)
- [Security list API](https://www.joinquant.com/help/api/doc?name=JQDatadoc&id=9841)
- [Price API](https://www.joinquant.com/help/api/doc?name=JQDatadoc&id=9874)
- [Data processing rules](https://www.joinquant.com/help/api/doc?name=JQDatadoc&id=10279)
- [JQData user agreement](https://www.joinquant.com/help/api/doc?name=JQDatadoc&id=11188)

## Authorization boundary

The current account is for the account holder's personal research. AI Trade
therefore records every JQData source manifest with
`usage_scope=personal_research_only`. JQData raw data and JQData-derived
records must not be returned to beta users, published through Web/API exports,
committed to Git, uploaded to release artifacts, or used commercially without
separate written permission.

Credentials are accepted only from an interactive prompt or the current process
environment. They are never copied into JSON configuration, evidence records,
logs or command arguments. The probe stores only a masked `mob` value. The
local evidence root is `state/jqdata/`, which is ignored by Git.

## Install the optional SDK

From the repository virtual environment:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[jqdata]"
```

Do not install or authenticate for a scheduled job until the interactive probe
below has confirmed the actual account result. JQData documentation contains
different trial quota examples; `get_account_info()` and
`get_query_count()` for the current account are authoritative.

## Capture the current manual baseline

This creates the first immutable knowledge-time version from the configured v1
security master. It does not contact JQData.

```powershell
.venv\Scripts\python.exe -m ai_trade.cli security-master-capture
.venv\Scripts\python.exe -m ai_trade.cli security-master-versions
```

Each v2 record contains `version_id`, canonical UTC `known_at`, parent
version id and hash, a credential-free source manifest, the canonical v1
business-time payload, and content/record SHA-256 values. Files are create-once
under `state/security_master/versions/`.

Resolve the version that was actually known at a cutoff:

```powershell
.venv\Scripts\python.exe -m ai_trade.cli security-master-resolve 2026-07-27T16:00:00+08:00
```

The store rejects naive timestamps, a timestamp older than the latest version,
duplicate timestamps with different content, broken parent chains, symbolic
members, tampering and credential-like fields in source requests.

## Run the account-only probe

Preferred interactive command:

```powershell
.venv\Scripts\python.exe -m ai_trade.cli jqdata-probe
```

The command prompts for the account and a non-echoed password, calls only
`auth()`, `get_account_info()`, `get_query_count()` and `logout()`, then
writes a sanitized immutable probe under `state/jqdata/probes/`. It does not
call `get_all_securities()`, `get_price()` or any financial-data API.

For a short-lived non-interactive process, both variables are required:

```powershell
$env:AI_TRADE_JQDATA_USERNAME='<account>'
$env:AI_TRADE_JQDATA_PASSWORD='<password>'
.venv\Scripts\python.exe -m ai_trade.cli jqdata-probe --non-interactive
Remove-Item Env:AI_TRADE_JQDATA_USERNAME
Remove-Item Env:AI_TRADE_JQDATA_PASSWORD
```

Environment variables are not a dedicated secret vault. Another process under
the same Windows account may be able to inspect them. Prefer the interactive
prompt for the first probe and never paste credentials into chat, screenshots,
issues or shell history.

## Run the bounded historical reconciliation

The successful account probe currently reports licensed data from
`2025-04-18` through `2026-04-25`. Because 25 April 2026 is a Saturday,
the latest completed licensed trading session for the first audit is
`2026-04-24`. Run exactly:

```powershell
.venv\Scripts\python.exe -m ai_trade.cli jqdata-sample --end-date 2026-04-24
```

The command authenticates once and requests only `510300.XSHG`,
`510500.XSHG` and `159915.XSHE` for 20 sessions. It overrides every
material SDK default: `fq='none'`, `skip_paused=False`,
`fill_paused=False`, `round=False` and `panel=False`. It captures
OHLC, volume, money, factor, price limits, average price, previous close and
paused state, together with query quota before and after the three requests.

Before authentication, the command verifies the active local manifest and all
three cache-file hashes. The comparison then uses `JQData price * factor` as
the adjusted reference, estimates the stable scale to the local forward-
adjusted series, compares adjusted returns, recognizes either equal volume
units or JQData shares versus local 100-share lots, and compares money in CNY.
A passed gate requires all 20 sessions and all checks to pass for all three
symbols.

Licensed and local rows are stored only in the Git-ignored
`state/jqdata/samples/` immutable evidence store. Console output contains
hashes, metrics and pass/fail states, never bar rows, the full mobile number or
credentials. The command does not change the configured provider, overwrite
the market cache, train a model, create a prediction or place an order.

The roughly three-month lag does not remove July data from the existing local
research cache. It does prevent JQData from independently validating May-July
data or supporting a current signal. For a long-history fit, roughly 60 missing
end sessions are a small fraction of observations; for 20/60-session features,
current regime calibration and latest OOS evidence, the same gap is material.
The verified-through date must therefore remain explicit.

## Knowledge-time semantics

Business time answers when a security was listed, delisted, eligible or in a
universe. Knowledge time answers when this installation first observed that
record. They are intentionally separate.

The first JQData-derived version can only use its real capture timestamp as
`known_at`. A current JQData response must not be backdated to a historical
session. Historical bars can support reconstructed research, but they do not
prove that today's corrected master data or adjustment factors were available
to the strategy in the past.

JQData documents that daily bars are provisionally updated after 15:00 and
checked into the database by 24:00. A later integration should therefore retain
the observation time and, where necessary, separate provisional and final
daily observations instead of silently replacing evidence.

## Gate after the bounded sample

If `comparison.summary.gate_passed` is false, retain the evidence and stop.
Investigate the failed field, unit or session before making another licensed
query. Do not loosen tolerances merely to obtain a pass.

Only after all three symbols pass:

1. Request the current ETF security list once and compare only the configured
   47 codes.
2. Publish a new JQData-sourced security-master version using the real capture
   time as `known_at`; never backdate it.
3. Expand the same reconciliation to 47 symbols within the licensed historical
   window.
4. Bind later FeatureSnapshots to the exact master version, source manifest and
   record hashes they consumed.
5. Keep current-session research on its separately identified local source
   until an authorized current source closes the May-July verification gap.

Model training remains research-only. Historical overlap can verify ingestion,
feature formulas, adjustment handling, leakage controls, costs and reproducible
OOS calculations. It cannot turn a model that failed statistical gates into a
valid model, and it cannot establish current point-in-time correctness beyond
the JQData licensed-through date.
