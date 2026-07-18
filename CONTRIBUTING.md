# Contributing

AI Trade `v0.12.1` is the current public release baseline. The project prioritizes timing correctness, reproducibility, provenance, and loss controls over higher backtest returns or a larger feature count.

## Development Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install ruff build
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

Run before opening a pull request:

```powershell
python -m compileall -q src tests
python -m unittest discover -s tests -v
ruff check src tests scripts adapters/qmt/src
node --check src/ai_trade/web/assets/app.js
python -m build
python -m build adapters/qmt --outdir qmt-dist
python scripts/verify_distribution.py dist
```
