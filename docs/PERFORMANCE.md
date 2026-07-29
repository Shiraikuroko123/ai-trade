# Performance baseline

AI Trade keeps performance work evidence-driven. The benchmark measures only
verified local computation and does not refresh providers, write reports,
append research evidence, or modify the paper account.

Run the full baseline from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_core.py --bootstrap-samples 1000
```

The JSON output records runtime details, data scope, wall-clock timings, and
deterministic result fingerprints. Compare timings only on the same machine,
Python version, power mode, and market snapshot. Fingerprints should remain
stable when an optimization is intended to preserve numerical results.

The measured paths are:

- verified CSV snapshot parsing and integrity validation;
- the full event-driven historical backtest;
- the 18-candidate rolling walk-forward protocol;
- robustness validation with cost, parameter, regime, and moving-block
  bootstrap checks.

Provider refresh latency is deliberately excluded from the compute benchmark.
It is dominated by remote response time, retry policy, throttling, and
cross-source verification rather than local compute. Use the scheduled runner
timestamps and `logs/ai_trade.log` to assess that path separately.

## Network refresh evidence

Network refresh changes require a separate measurement with an isolated copy
of the active validated cache. Record the source manifest fingerprint, symbol
count, planned provider-page count, start/end timestamps, provider circuit
state, cross-check outcome, and published CSV hashes. Never benchmark by
overwriting the active cache or research evidence. A comparison is meaningful
only when the provider route, cache snapshot, configuration, machine, and
completed-session cutoff are disclosed.

The 2026-07-29 scheduled baseline refreshed 47 symbols from `18:00:04` through
`18:10:18` Asia/Shanghai. Eastmoney's first sustained failure consumed eight
bounded attempts, after which Tencent fallback issued 189 yearly page requests;
13 newer ETFs repeatedly required 6 through 14 pages because their first
provider session followed the configured global start date. With a verified
20-session overlap, an isolated refresh of the same cache completed in 257.283
seconds: Eastmoney supplied one symbol and the other 46 used one Tencent page
each, a reduction from 189 to 46 Tencent pages (75.7%). All 46 incremental
files carried explicit coverage evidence, no full-history rebuild occurred,
and every resulting CSV hash matched the active cache. Cloud upload was
disabled and the active manifest hash was unchanged. This wall-clock result is
observational rather than a strict comparison with the scheduled baseline,
which included its configured cloud attempt; public endpoints can also vary.

Absolute time thresholds are not CI gates because shared runners and laptops
vary materially. Numerical tests, result fingerprints, Ruff, mypy, and the
full unit suite remain the correctness gates; benchmark comparisons provide
the performance evidence.
