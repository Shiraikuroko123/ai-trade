# AI Trade

[![CI](https://github.com/Shiraikuroko123/ai-trade/actions/workflows/ci.yml/badge.svg)](https://github.com/Shiraikuroko123/ai-trade/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Shiraikuroko123/ai-trade)](https://github.com/Shiraikuroko123/ai-trade/releases)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-2f6f68)](LICENSE)

**[中文完整文档 / Full Chinese README](README.md)** · [Architecture](docs/ARCHITECTURE.md) · [Ecosystem comparison](docs/ECOSYSTEM.md) · [Changelog](CHANGELOG.md)

AI Trade is a local, auditable, systematic-investment workstation for China
A-share ETFs and stocks, built for one individual investor on one trusted
Windows computer. The complete research → backtest → paper-trading →
monitoring → archive loop runs **without any LLM, GPU, or paid service**, on
a plain zero-dependency Python standard library.

## What makes it different

Most open-source "AI quant" projects optimize for features; AI Trade
optimizes for **evidence**. Every dataset, strategy decision, research note,
and account movement is an immutable, fingerprint-verified record:

- **Point-in-time correctness.** A dated security master drives a dynamic
  universe (listing/delisting aware), snapshots are content-hashed, and
  cross-source audits (Eastmoney primary, Tencent fallback, Yahoo/Tushare
  reference) are recorded rather than assumed.
- **China market realism.** Round lots, stamp duty, transfer fees, slippage,
  suspensions, and price limits are modeled with date-effective schedules.
- **Progressive authority.** Research evidence can never trade. Strategy
  changes pass validation gates and explicit human approval; live trading is
  structurally disabled until broker-adapter, sandbox-reconciliation, and
  kill-switch gates exist — none of which current evidence can unlock.
- **Deterministic AI research line (`v2.0.0`, in development).**
  - *Factor lab*: a versioned factor registry with point-in-time rank-IC /
    decay / spread evaluation evidence.
  - *Model lab*: pure-stdlib walk-forward ridge and equal-weight baselines
    with strict leakage guards and same-protocol single-factor comparison —
    the record states plainly whether the model beat its best input.
  - *Hypothesis lab*: pre-registered, falsifiable hypotheses (predictions,
    exact falsification criteria, confounds, Holm-corrected three-per-snapshot
    family budget) plus a deterministic experiment runner whose
    SUPPORTED/FALSIFIED verdicts grant no authority.
  - *Exploratory parameter sweeps* clearly labeled as exploration, and a
    deterministic consolidated research report.
- **Optional, governed LLM assistant.** Bull/bear/judge research review with
  immutable call audits, budgets, caches, and deterministic fallback — the
  model can tighten a conclusion but never loosen one, and never outputs an
  order, position, or price target.

## Quick start (Windows, PowerShell)

```powershell
git clone https://github.com/Shiraikuroko123/ai-trade.git
cd ai-trade
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
.\.venv\Scripts\python.exe -m ai_trade.cli download --force
.\.venv\Scripts\python.exe -m ai_trade.cli serve --owner-local
```

Then open the printed loopback URL. A constrained Docker/Compose deployment
is also provided (`docs/DOCKER_DEPLOYMENT.md`). Release wheels and SHA-256
manifests are on the [Releases](https://github.com/Shiraikuroko123/ai-trade/releases)
page.

```powershell
# Research CLI highlights
ai-trade backtest ; ai-trade walk-forward ; ai-trade validate
ai-trade factor-evaluate --factor momentum_120_5
ai-trade model-evaluate --model ridge_v1 --horizon 20
ai-trade parameter-sweep --objective sharpe
ai-trade hypothesis-generate --objective auto
ai-trade hypothesis-run hyp_<id>
ai-trade research-report
```

## Boundaries, stated plainly

No live-broker adapter ships in this repository and real-order controls stay
disabled. No Tick/Level-2 data, no complete sentiment model, no profit
promises. Historical performance does not predict future results; this is a
research and paper-trading tool, not investment advice.

License: MIT. The full documentation, including operations guides for
monitoring, archives, cloud snapshots, and broker-adapter contracts, is in
Chinese — see [README.md](README.md).
