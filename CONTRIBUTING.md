# Contributing

AI Trade `v1.0.0` is the current public release baseline. The project prioritizes timing correctness, reproducibility, provenance, and loss controls over higher backtest returns or a larger feature count. Unreleased research work on `main` must keep factor, model, hypothesis, and broker-sandbox evidence explicitly separated from release claims and execution authority.

## Development Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip install build==1.2.2.post1
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On Linux or macOS, replace the interpreter path with `.venv/bin/python`.

## Pull Requests

- Explain the behavioral change and financial-risk implications.
- Add tests for time ordering, costs, position sizing, persistence, and failure behavior as applicable.
- Report both full-history and walk-forward effects; do not select changes solely by the best in-sample return.
- Preserve the read-only market-chart contract: GET requests must not refresh, recover, or rewrite market data or account state.
- Keep third-party browser assets version-pinned with licenses, provenance, fixed hashes, and distribution tests.
- Do not weaken the live-trading guard or commit credentials, caches, state, reports, or logs.
- Update the README, relevant documents under `docs/`, local-only tutorials when applicable, and the changelog for user-visible behavior.

## Quality Gates

The GitHub Actions `quality` job runs independently from the operating-system test matrix. It enforces:

- Ruff over `src`, `tests`, `scripts`, and the optional QMT adapter.
- Mypy over the research core declared in `pyproject.toml`; adding a new factor/model/hypothesis/pipeline module requires adding it to that scope or documenting why it is excluded.
- Branch coverage over `ai_trade`, with a `75.0%` floor. Do not lower the floor to merge a change; add focused tests or explain a deliberate exclusion in the PR.
- The full unittest suite on Ubuntu and Windows with Python 3.10 and 3.12, plus Windows PowerShell/bootstrap and package-install smoke tests.

The integrated local acceptance snapshot updated on 2026-07-29 has 957 passing unittest cases, 76.5% branch coverage, a clean Ruff run, and zero Mypy errors across 52 research-core files. Compileall and `git diff --check` also pass. The preceding refresh-optimization commit `941392c` passed [CI run 30450549082](https://github.com/Shiraikuroko123/ai-trade/actions/runs/30450549082), including Ubuntu and Windows on Python 3.10/3.12, the quality job, PowerShell/bootstrap, and package smoke tests. CI for later commits remains the authoritative cross-platform result; older local counts and earlier failed runs must not be cited as current evidence.

## Market Data And Research Evidence

The configured `core_etf` universe and the dated local evidence snapshot published on 2026-07-27 contain 47 instruments and complete through 2026-07-24. That snapshot passed file/hash/date and `universe-verify` checks, but its independent Yahoo audit is a warning because all 47 reference requests returned HTTP 403. Do not describe it as independently confirmed, and do not present an unpublished refresh candidate, a partial cache, or old 8-instrument metrics as current 47-instrument evidence.

- Keep durable refresh candidates outside the active manifest. Resume only when the candidate identity, CSV hashes, row counts, providers, adjustment mode, date bounds, and security master still match.
- Never treat an unadjusted provider response as forward-adjusted merely to fill a missing symbol. A provider that cannot prove the configured adjustment contract must fail closed.
- A 47-instrument refresh is accepted only when the active manifest has exactly the configured symbols, every CSV and SHA-256 validates, `latest_common_session` is justified, cross-source status is disclosed, and `universe-verify` passes.
- Rebuild backtest, walk-forward, robustness, factor, model, and hypothesis evidence from the newly published snapshot. Every derived artifact must bind the same market/security/configuration fingerprints; stale evidence remains historical only.
- Feature or model changes must test time ordering explicitly: training statistics and factor values may use only information available by the feature date, while labels must mature before an observation enters training.
- Keep materialized features and future labels in separate create-once stores. `feature-build` defaults to a genuine capture of the latest common completed session; older dates require explicit `--historical-reconstruction`, remain marked as such, and must never train an artifact. Appending future bars must not alter an older feature snapshot identity, and any content tampering must fail closed.
- Snapshot-backed Factor/Model evaluation must use one exact ordered feature set, verify feature/label fingerprints and source identities, persist every source id in an immutable dataset manifest, and use each label's real target session for walk-forward maturity. Its CLI path must not construct `MarketData` or refresh a provider.
- An inference-complete model artifact requires a qualified v2 evaluation, complete inference state, an exact ordered feature-set binding, genuinely captured PIT inputs created before the evidence cutoff, and mature labels available by the training cutoff. A `ModelArtifact`, `PredictionSnapshot`, or `PortfolioPlan` remains `research_only` and must not write paper or broker state.
- Prediction validity must begin on the first trading session after its feature snapshot and end within the artifact horizon. Portfolio plans must bind the verified market manifest, market-snapshot fingerprint, instrument metadata, and per-symbol input fingerprints used for liquidity and volatility constraints.
- Keep the research-pipeline CLI free of approval, activation, execution, and trading flags. Its adapters may publish immutable research evidence only; a failed statistical deployment gate must stop before artifact fitting or downstream prediction.
- Keep the research loop on an exact, versioned tool allowlist. Tool budgets must be checked before execution; duplicate proposals and cross-loop model/hypothesis IDs must fail closed; planner, tool, failure, rejection, and stop outcomes must remain in the per-owner append-only hash chain. Model mode must use the existing call-budget, cache, concurrency, and audit boundary.
- Portfolio changes must test the actual fee schedule, minimum commission, cash, concentration, group, volatility, turnover, and capacity constraints. It is valid and preferable to emit no trade when expected improvement does not cover cost.
- Keep the new Shadow event ledger separate from legacy fill review and formal broker reconciliation. Reconciliation input has the exact fields `cash` and `positions`; callers must not supply a tolerance that can hide a difference. Its projections are not qualifying sandbox evidence until a reviewed adapter supplies scoped account snapshots and a continuous future-session record.
- Statistical records must bind deterministic seeds and disclose observations, block size, resamples, effect size, uncertainty, raw and adjusted p-values, correction family, and chronological subperiod stability. Changing a comparison family requires a schema/version change and focused backward-compatibility tests.
- Do not describe Hypothesis Lab threshold judgments as p-value tests. Factor/Model evaluation v2 performs moving-block bootstrap and Holm correction; the hypothesis runner currently records deterministic pre-registered threshold outcomes.
- Factor and model evidence remains `research_only`. It must not create target weights, orders, approval, activation, or live-trading authority without a separately reviewed conversion and promotion contract.

Run before opening a pull request:

```powershell
python -m compileall -q src tests
python -m unittest discover -s tests -v
python -m ruff check src tests scripts adapters/qmt/src
python -m mypy
python -m coverage run -m unittest discover -s tests -q
python -m coverage report
node --check src/ai_trade/web/assets/app.js
python -m build --outdir dist/release-1.0.0
python -m build adapters/qmt --outdir qmt-dist
python scripts/verify_distribution.py dist/release-1.0.0
git diff --check
```

For a real 47-instrument evidence rebuild, also report the active manifest fingerprint, file count, common completed session, source-route distribution, cross-source result, and `universe-verify` output in the PR. Do not make a network refresh part of the unit-test suite.
