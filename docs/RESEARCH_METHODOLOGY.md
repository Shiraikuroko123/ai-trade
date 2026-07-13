# Research Methodology

## Timing Contract

A signal may use only completed daily bars through date `t`. Orders generated from that signal are simulated at date `t+1` open. The benchmark enters on the same tradable session. Intraday partial daily bars are excluded before 15:30 China Standard Time.

## Strategy

The default strategy combines medium-term momentum, a long-term moving-average filter, inverse-volatility weights, conservative volatility aggregation, liquidity and capacity screening, position/asset-class/risk-group caps, and a cash reserve. Candidate eligibility is evaluated at the signal date from the security master. Covariance and risk-parity modes are available for research but did not outperform the simpler default in the current development walk-forward comparison.

## Validation Layers

1. Full-history backtest with explicit costs and lot sizes.
2. Continuous rolling walk-forward selection without resetting positions or risk state at segment boundaries.
3. Moving-block bootstrap to retain short-horizon dependence.
4. One-, two-, and three-times cost stress.
5. Nearby-parameter sensitivity.
6. Historical stress regimes.
7. Independent future paper sessions.

Execution diagnostics include rejected orders and separately report commission, stamp duty, transfer fees, and slippage. The current ETF data uses forward-adjusted bars for both research and simulated fills; this remains an approximation until raw prices and corporate actions are modeled independently.

The existing historical windows were used to select the current liquidity threshold. They are development evidence, not a pristine final holdout. Future paper data is the next independent test.
