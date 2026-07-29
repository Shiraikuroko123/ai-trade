# Model Lab

The model lab is the third unreleased `v2.0.0` research component: a
pure-standard-library, walk-forward machine-learning baseline over the factor
registry. It needs no GPU, no third-party package, and no model API. Like the
factor lab, it produces immutable, owner-isolated research evidence — never a
signal, weight, candidate, or order.

## Local workflow

```powershell
ai-trade --config config/default.json model-list
ai-trade --config config/default.json model-evaluate --model ridge_v1 --horizon 20
ai-trade --config config/default.json model-evaluate --model factor_mean_v1 --factors momentum_120_5,volatility_60
ai-trade --config config/default.json model-evaluate --model gbdt_v1 --horizon 20
ai-trade --config config/default.json model-evaluate --model ridge_v1 --horizon 20 --step 1 --snapshot-input
ai-trade --config config/default.json model-evaluations --limit 20
ai-trade --config config/default.json model-show mdl_<32-lowercase-hex>
ai-trade --config config/default.json feature-dataset-show fds_<32-lowercase-hex>
```

`model-evaluate` opens the existing verified cache without refreshing any
provider; the other commands never open market data.

`--snapshot-input` switches to the common immutable FeatureSnapshot dataset
boundary and never constructs or refreshes `MarketData`. The full ordered
factor set must match the selected FeatureSnapshot revision; labels must bind
the exact feature fingerprint and source symbols. An immutable dataset
manifest under `state/feature_store/datasets/` records every FeatureSnapshot
and LabelSnapshot id and is referenced by the model evaluation. Historical
reconstruction, stale capture, changed universe/adjustment identity, and
conflicting label revisions are rejected. Use `--feature-set-id fset_...` to
pin a revision when more than one exists.
Use `--step 1` for daily forward evidence; larger values deliberately
downsample the available genuine snapshot dates and require a longer calendar
history before the 24-date gate can pass.

## Registry contract

Models are code-defined with fixed, pre-registered hyperparameters so an
evaluation cannot quietly tune itself on the data it reports. `ridge_v1`
solves the ridge normal equations (fixed lambda 0.1 on correlation scale)
over train-window z-scored factors against per-date demeaned forward returns.
`factor_mean_v1` is the untrained honest baseline: the equal-weight mean of
direction-adjusted standardized factor scores.

`gbdt_v1` is the optional non-linear extension, still pure standard library:
least-squares gradient boosting over depth-2 regression trees (24 trees,
learning rate 0.12, minimum leaf 20, 8 boundary-snapped quantile split
candidates per feature). Because tree fitting is the most expensive step, it
adds two pre-registered honesty-preserving cost controls — `refit_interval`
(refit from scratch every 8 evaluated dates; predictions between refits use
the last fitted model, whose training data is still strictly older than the
evaluated date) and `max_train_rows` (train on the most recent 2000
observations). Instead of coefficients it discloses normalized split-gain
feature importance. All hyperparameters are fixed in the registry; there is
no tuning loop.

## Walk-forward protocol and leakage guards

Evaluation samples the configured backtest window every `--step` sessions and
builds complete-case observations: a symbol enters a date only with an exact
completed bar, an exact bar `horizon` sessions later, and a finite value for
every selected factor. At each evaluated date the model is refit from scratch
using only observations whose forward-return windows have fully completed
(`feature_index + horizon <= evaluation_index`), with at least 12 training
dates and 48 training observations; earlier dates are reported as warm-up,
never silently backfilled. Standardization statistics come from the training
window only, and a constant training feature contributes zero.

For snapshot input, the leakage guard uses the label's actual
`target_session <= evaluation FeatureSnapshot session` and
`realized_at <= evaluation knowledge_cutoff` relations. It does not infer
maturity from file order or ordinary calendar days. Missing or pending labels
are counted as skipped/warm-up evidence and cannot enter training.

Out-of-sample Spearman rank IC is recorded per evaluated date, and every
input factor is evaluated on exactly the same dates and symbols under the
identical protocol. The record therefore always answers the uncomfortable
question directly: `model_minus_best_factor_ic` states whether the trained
model actually beat its best single input. On a small ETF cross-section it
frequently does not — that negative delta is the point of the evidence, not a
failure to hide. Mean/mean-absolute/final coefficients per factor are
disclosed for interpretability.

## Storage and integrity

Evaluations are create-once JSON files under
`state/model_lab/users/<owner-sha256>/evaluations/` (256 KiB per record, 500
per owner, 50 per model), bound to the market snapshot fingerprint, universe,
security master, and a configuration-context fingerprint. A deterministic
evaluation fingerprint over model, factors, parameters, snapshot, and engine
versions makes repeats idempotent (`reused`), while any parameter or snapshot
change appends a new record. Fingerprints are recomputed on read; tampered
files fail closed.

## Authority boundary

The safety contract fixes candidate creation, approval, activation, and
trading to false and adds `creates_no_signal`. A strong out-of-sample IC is
research evidence only: turning it into a strategy change still requires the
existing human chain — hypothesis registration, deterministic experiment
execution, explicit materialization, Strategy Lab validation, and approval.
`gbdt_v1` is that optional non-linear extra, implemented without any
third-party dependency; the deterministic core keeps running on an ordinary
personal computer, and skipping the tree model changes nothing else.
