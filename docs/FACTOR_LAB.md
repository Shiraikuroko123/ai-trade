# Factor Lab

The factor lab is the second unreleased `v2.0.0` research component. It is a
deterministic, zero-dependency factor registry and evaluation engine designed
for ordinary personal computers: no model call, no provider refresh, and no
GPU. It turns the verified local market cache into immutable, owner-isolated
factor evidence — rank information coefficients, decay across horizons, and
half-split spread — without creating a signal, weight, candidate, or order.

## Local workflow

```powershell
ai-trade --config config/default.json factor-list
ai-trade --config config/default.json factor-define --name gap_rev --expression "delay(close,1)/open-1" --direction -1 --label "隔夜跳空反转"
ai-trade --config config/default.json factor-evaluate --factor momentum_120_5
ai-trade --config config/default.json factor-evaluate --factor gap_rev
ai-trade --config config/default.json factor-evaluate --factor volatility_60 --horizons 5,20,60 --step 5
ai-trade --config config/default.json factor-evaluations --limit 20
ai-trade --config config/default.json factor-show eval_<32-lowercase-hex>
```

`factor-evaluate` opens the existing verified cache without refreshing any
provider. The configured cache must already exist and pass `MarketData`
validation; `factor-list`, `factor-evaluations`, and `factor-show` never open
market data at all.

## Registry contract

The registry is code-defined and versioned. Each factor declares an
identifier, version, family, human label, exact formula text, minimum bar
history, and a registered `direction`: +1 when the mechanism expects higher
values to precede higher forward returns, -1 for the opposite. The initial
library covers momentum (two windows and distance-from-high), trend gap,
annualized volatility, short-term reversal, and amount surge. The direction is
a research hypothesis under test, not an instruction; evaluations report raw
rank correlations next to it so a wrong registered direction is visible
instead of silently absorbed.

## Custom expression factors

`factor-define` extends the registry with owner-isolated custom factors
written in a deliberately small expression language instead of arbitrary
code. The compiler is a recursive-descent parser over an allowlist — the bar
series `open/high/low/close/volume/amount`, arithmetic, and the windowed
functions `sma`, `std`, `ts_max`, `ts_min`, `delay`, `ret`, `ts_sum`,
`ts_rank`, `ts_argmax`, `ts_argmin`, and `delta` — with hard caps on source
length (200 characters), token count (80), nesting depth (12), and window
size (500). The five later names are an additive allowlist extension in the
style of the public Alpha101/Qlib expression sets: `ts_sum` is the rolling
sum, `ts_rank` is the midrank-based percentile of the latest value inside
its window (`[0, 1]`, ties averaged), `ts_argmax`/`ts_argmin` count sessions
since the window's most recent extreme (`0` = today), and
`delta(x, n) = x[t] - x[t-n]`. They keep the single-series
`name(series, integer_window)` shape, so the grammar, the canonical stored
form, and every previously stored expression are unchanged. There is no `eval`, no attribute access, no name
lookup outside the allowlist, and no way to reach the filesystem, network, or
configuration from an expression. The compiler derives the factor's minimum
bar history from its windows, so point-in-time evaluation applies the same
warm-up honesty as built-in factors.

Definitions are create-once records under
`state/factor_lab/users/<owner-sha256>/custom/`: the stored source is the
canonical whitespace-free form, redefining the same name with the same
canonical expression and direction is idempotent (`reused`), and redefining
it with different content fails closed with an explicit immutability error —
rename instead of mutate, so old evaluations always point at exactly the
definition that produced them. Custom names must not collide with built-in
factor identifiers. `factor-evaluate` resolves built-ins first and then the
owner's custom store, and evaluation records for custom factors carry the
full expression in their factor definition block.

## Point-in-time evaluation

Evaluation samples the configured backtest window every `--step` sessions.
On each sampled date the dynamic universe comes from listing/delisting-aware
`active_symbols`, a symbol must have completed that session, and factor values
are computed only from bars up to that date. Forward returns require an exact
completed bar at the sampled date plus the exact bar `h` sessions later on the
shared calendar; missing either drops the symbol for that observation rather
than substituting a nearby bar. A date with fewer than the minimum
cross-section (4) is skipped and counted, never padded.

Per horizon the record reports Spearman rank IC per date (average ranks on
ties), mean IC, IC standard deviation, ICIR, the share of positive-IC dates,
the direction hit rate against the registered direction, and the equal-weight
top-half minus bottom-half forward-return spread with its direction-adjusted
value. Multiple horizons in one record form the decay curve. Small-universe
honesty is built in: with a handful of ETFs the cross-section is tiny, halves
replace quantiles, and an evaluation fails closed when fewer than 24 valid
cross-section dates exist rather than reporting an impressive-looking average
over three points.

## Storage and integrity

Evaluations are create-once JSON files under
`state/factor_lab/users/<owner-sha256>/evaluations/`, capped at 256 KiB per
record, 500 records per owner, and 50 per factor. Every record binds the
market snapshot fingerprint, universe name, security-master fingerprint, and a
configuration-context fingerprint, and carries a deterministic evaluation
fingerprint over factor definition, parameters, snapshot, and engine versions:
repeating the same evaluation returns the stored record as `reused` instead of
appending a duplicate, while changed parameters or a refreshed cache append a
new record. Fingerprints are recomputed on every read, so a tampered file
fails closed. Hashes remain unkeyed local integrity values, not signatures.

## Authority boundary

The factor lab is `research_only`. Its safety contract fixes candidate
creation, approval, activation, and trading to false, and adds
`creates_no_signal`: an attractive IC cannot create a Strategy Lab candidate,
change the active strategy, touch paper accounting, or advance any
live-trading gate. Turning factor evidence into a strategy change still goes
through the existing human chain — hypothesis registration, deterministic
experiment execution, explicit materialization, validation, and approval.
