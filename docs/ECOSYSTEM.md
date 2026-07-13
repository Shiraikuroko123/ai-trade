# Investment System Ecosystem

AI Trade does not attempt to merge several large repositories into one process. It uses each project as a reference for a specific layer, then keeps local contracts small, testable, and auditable.

| System | Strongest reference value | AI Trade adoption |
|---|---|---|
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean) | Event engine, universe selection, brokerage abstraction | Point-in-time universe now; explicit order lifecycle and broker adapters later |
| [Microsoft Qlib](https://github.com/microsoft/qlib) | Factor datasets, ML experiments, research workflow | Planned factor registry and point-in-time feature store |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Deterministic event state, execution and reconciliation | Deterministic paper state, append-only ledgers and rejection audit |
| [VeighNa](https://github.com/vnpy/vnpy) | China broker gateways and live operations | Gateway contract reference only; no live gateway is enabled |
| [RQAlpha](https://github.com/ricequant/rqalpha) | China-market simulation rules | Dated stock fees, lot size, suspension and price-limit rules |
| [vectorbt](https://github.com/polakowo/vectorbt) | Fast parameter and signal research | Useful future research backend, not the accounting authority |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | Data-provider abstraction | Planned provider interface and independent data cross-checks |
| [PyPortfolioOpt](https://github.com/PyPortfolio/PyPortfolioOpt) | Efficient frontier, Black-Litterman and HRP | Research candidates after simple risk budgets pass forward tests |
| [cvxportfolio](https://github.com/cvxgrp/cvxportfolio) | Multi-period optimization with costs and constraints | Reference for future institutional portfolio construction |
| [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) | Broad portfolio-risk optimization | Reference for CVaR and hierarchical risk research |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | Reinforcement-learning experiments | Isolated research only; never promoted without strict leakage and forward tests |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | Operational lifecycle, monitoring and strategy deployment | Reference for health checks and promotion stages; crypto execution is out of scope |

## Hosted Platforms Worth Comparing

- [QuantConnect](https://www.quantconnect.com/) combines hosted research, backtesting, datasets, optimization, and brokerage deployment around LEAN.
- [JoinQuant](https://www.joinquant.com/), [Ricequant](https://www.ricequant.com/), [BigQuant](https://bigquant.com/), and [MyQuant](https://www.myquant.cn/) are useful references for China-market data, factor research, simulation, and broker-facing workflows.
- [Interactive Brokers](https://www.interactivebrokers.com/) and [Alpaca](https://alpaca.markets/) are broker/API references rather than substitutes for a research and risk platform.

Hosted results depend on each provider's data license, adjustment policy, fill model, region, and account permissions. AI Trade should use them for independent comparison or future adapters, not assume that two platforms with the same strategy name have the same data semantics.

## Current Layer Decisions

1. Security master is the authority for identity and point-in-time eligibility.
2. Validated market snapshots are immutable inputs to one run.
3. Strategy code emits target weights, not broker orders.
4. Portfolio constraints reduce exposures; they do not manufacture leverage to fill unused cash.
5. Execution applies date-effective fees and market rules, records rejections, and sells before buying.
6. Accounting and audit remain independent from strategy ranking.
7. Historical validation can promote a model only to future paper testing, never directly to live trading.

## What Is Deliberately Not Adopted

- Multi-agent commentary is not treated as a trading signal. A signal must reduce to deterministic data, parameters, and code.
- Reinforcement learning is not a shortcut around limited data. It has a larger overfitting surface than the current baseline.
- A current index constituent list is not used to backtest prior years.
- A fast vectorized result is not used as the final cash and order ledger without event-level reconciliation.
- Broker connectivity is not enabled until independent future sessions, broker sandbox reconciliation, credentials handling, and kill switches are complete.

The objective is a professional evidence chain, not maximum feature count. None of these systems, individually or combined, can guarantee profit.
