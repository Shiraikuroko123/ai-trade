# Contributing

AI Trade prioritizes timing correctness, reproducibility, and loss controls over higher backtest returns.

## Development Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On Linux or macOS, replace the interpreter path with `.venv/bin/python`.

## Pull Requests

- Explain the behavioral change and financial-risk implications.
- Add tests for time ordering, costs, position sizing, persistence, and failure behavior as applicable.
- Report both full-history and walk-forward effects; do not select changes solely by the best in-sample return.
- Do not weaken the live-trading guard or commit credentials, caches, state, reports, or logs.
- Update the README and changelog for user-visible behavior.

Run before opening a pull request:

```powershell
python -m compileall -q src tests
python -m unittest discover -s tests -v
```
