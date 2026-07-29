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

Provider refresh latency is deliberately excluded. It is dominated by remote
response time, retry policy, throttling, and cross-source verification rather
than local compute. Use the scheduled runner timestamps and `logs/ai_trade.log`
to assess that path separately.

Absolute time thresholds are not CI gates because shared runners and laptops
vary materially. Numerical tests, result fingerprints, Ruff, mypy, and the
full unit suite remain the correctness gates; benchmark comparisons provide
the performance evidence.
