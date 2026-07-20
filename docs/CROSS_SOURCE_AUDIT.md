# Independent Daily-Bar Cross-Check

AI Trade can compare the most recent completed daily bars in the active cache
with a different registered provider. This is an evidence audit only. It never
replaces the primary CSV, changes a strategy, writes a paper ledger, or grants
broker authority.

## Configuration

The bundled configuration enables a five-session check against Yahoo Finance
when Eastmoney is configured as the primary source. Tencent remains the
network fallback:

```json
{
  "data": {
    "provider": "eastmoney",
    "fallback_provider": "tencent",
    "cross_check": {
      "enabled": true,
      "reference_provider": "yahoo",
      "lookback_sessions": 5,
      "minimum_overlap_sessions": 3
    }
  }
}
```

Custom configurations without `data.cross_check` keep the previous offline
behavior. The reference provider must be registered and different from the
configured primary provider. `yahoo` is reference-only and can be selected
there, but cannot supply the primary or fallback snapshot.

## Running It

An enabled `download --force` refresh runs the audit after the CSV snapshot is
published. It can also be run explicitly, including for a configuration where
the automatic switch is disabled:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli cross-check-data
.\.venv\Scripts\python.exe -m ai_trade.cli cross-check-data --symbol 510300
```

The web **数据** and **系统** views expose the same result. The **跨源核对**
button starts the background job and reloads the current view when it finishes.

## Reading the Result

The result is stored under `cross_source_check` in
`data/cache/manifest.json`. Each symbol records its actual file provider,
reference provider, requested/overlapping sessions, the provider-declared
comparison fields, maximum relative deviation, and bounded breach details.
Yahoo records `amount` in `unavailable_fields`; its locally estimated amount is
never compared or used as liquidity evidence.

| Status | Meaning | Operating response |
| --- | --- | --- |
| `passed` | Every checked value is within the disclosed tolerance and dates fully overlap | Independent public-source confirmation for this snapshot; still not exchange certification |
| `failed` | At least one OHLCV value exceeds tolerance | Treat the snapshot as requiring review; the primary CSV is not silently replaced |
| `warning` | Missing sessions, transport failure, or an incomplete set of symbols | Primary data remains readable, but it is not independently confirmed |
| `unavailable` | No usable different reference provider | Use the source/fallback evidence and resolve the provider configuration |
| `not_independent` | The file's actual source is unknown or is the same as the candidate reference | No self-comparison is counted as evidence |
| `invalid` | The audit digest or its file binding no longer matches the active manifest | Refresh and rerun; do not rely on the old audit |

When a fallback provider supplied a file, the auditor does not compare that
file with itself. It tries the configured primary as the independent source;
if that source is unavailable, the row is recorded as unavailable or warning.
This distinction is important for the common Eastmoney-blocked/Tencent-fallback
case.

Price tolerance is `max(CNY 0.02, 0.5%)`; volume tolerance is 10% and amount
tolerance is 15% when both providers declare an amount field. These bounds
account for public-provider rounding and do not claim an exchange or
licensed-feed guarantee. A pass is therefore evidence of recent consistency,
not proof that every historical revision or corporate action is identical.

Yahoo maps Shanghai symbols to `.SS` and Shenzhen symbols to `.SZ`, converts
share volume to 100-share lots, and uses the response's adjusted close to
scale OHLC for forward adjustment. Yahoo cash-dividend adjustment is a
multiplicative total-return convention; it can differ from the domestic
provider's additive forward-adjustment convention on older sessions. Such a
difference remains a visible audit mismatch rather than being silently
reconciled. The adapter is limited to 63 calendar days per request and only
completed rows are accepted.

The audit digest is bound to the manifest's CSV row counts and SHA-256 values.
Changing a CSV or editing the audit summary makes the projection `invalid` on
the next read. Local SHA-256 is tamper evidence, not a host-independent digital
signature or WORM archive.
