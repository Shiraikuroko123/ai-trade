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

## Post-activation Monitoring

An activated strategy-lab candidate is not assumed to remain valid indefinitely. On explicit user request, the workstation reruns the active candidate and its recorded parent baseline over a bounded recent completed-session window, then compares the active result with both the same-window parent and the candidate's activation-time holdout evidence. The record includes the exact market snapshot, period, metrics, thresholds, failed checks, and a SHA-256 evidence fingerprint.

`MONITORING_OK` means no configured decay threshold fired; it does not prove future performance. `REVIEW_REQUIRED` means at least one Sharpe or drawdown threshold needs human review; it is not an instruction to trade or an automatic suspension. `INSUFFICIENT_DATA` is kept distinct from failure. Only a state-bound, explicitly confirmed human action can suspend, resume, retire, or roll back a simulated lab baseline. These research lifecycle states do not modify an external paper process, paper ledger, broker mandate, kill switch, or live-trading gate.

## Charts And Indicators

The `v0.12.1` market workstation computes MA, EMA, BOLL, MACD, KDJ, RSI, and Wilder ATR from the same validated completed OHLCV snapshot used for review. Daily bars may be aggregated deterministically into calendar weeks or months; no intraday, minute, order-book, or synthetic session data is implied.

Chart overlays, oscillator selections, zoom, crosshair inspection, and paper trade markers are descriptive observations. They do not enter the default strategy unless a separately defined parameter or rule is implemented, validated against the same baseline snapshot, approved by a human, and then observed in an isolated paper profile. An indicator crossing is not itself evidence of causality, robustness, or live readiness.
