# Hypothesis Lab

The hypothesis lab is the first unreleased `v2.0.0` research component. It is
designed for ordinary personal computers and does not require or call a large
model. It turns the current local Strategy Lab baseline and verified market
cache into an immutable, falsifiable experiment registration. It does not run
the candidate experiment or change strategy state.

## Local workflow

```powershell
ai-trade --config config/default.json hypothesis-generate --objective auto
ai-trade --config config/default.json hypothesis-list --limit 20
ai-trade --config config/default.json hypothesis-show hyp_<32-lowercase-hex>
ai-trade --config config/default.json hypothesis-materialize hyp_<32-lowercase-hex> --yes
```

`hypothesis-generate` never refreshes a provider. The configured cache must
already exist and pass `MarketData` validation. `auto` uses two predeclared
local rules: drawdown is selected when baseline drawdown consumes at least 75%
of the configured drawdown limit; otherwise turnover is selected at notional
turnover of at least 4.0 times average equity; otherwise the balanced template
is selected. The operator can pre-register one of those objectives explicitly.

## Record contract

Every record contains:

- the active Strategy Lab parent, settings, candidate-settings, and complete
  configuration-context fingerprints;
- the market snapshot, per-symbol daily-cache, manifest, and security-master
  fingerprints, without copying raw datasets into the record;
- an observation, mechanism, scope, assumptions, and bounded allowlisted
  parameter changes;
- quantitative predictions and an exact opposite criterion that falsifies
  every prediction;
- three distinguishable competing explanations and explicit confound controls;
- same-snapshot baseline comparison, holdout, rolling out-of-sample, doubled
  cost, parameter sensitivity, and later-snapshot replication plans; and
- a three-hypothesis snapshot-family budget with Holm correction at alpha 0.05.

The owner directory is a SHA-256 identity, each file is published once, and
every read recomputes the design and whole-record fingerprints. A repeated
design on the same evidence is returned as reused even when its display title
changes. Records are capped at 512 KiB, each owner at 500 records, and each
snapshot family at three distinct designs.

## Deterministic experiment execution

`hypothesis-run <hypothesis-id>` executes the pre-registered plan on the
already verified local cache without refreshing a provider or calling a model:

```powershell
ai-trade --config config/default.json hypothesis-run hyp_<32-lowercase-hex>
ai-trade --config config/default.json hypothesis-runs --limit 20
ai-trade --config config/default.json hypothesis-run-show run_<32-lowercase-hex>
```

The runner first re-verifies the registration: the configuration-context
fingerprint must equal the registered one, the stored baseline settings must
re-fingerprint exactly, and the proposed changes must reproduce the registered
candidate-settings fingerprint through the same allowlisted parameter rules.
Any drift fails closed with an instruction to register a new hypothesis; the
runner never rewrites, migrates, or "repairs" a registration.

When the current cache fingerprint equals the registered snapshot, the run
executes in `same_snapshot` mode: baseline-versus-candidate backtests on the
full window, the registered holdout fraction (at least 20 sessions), every
registered cost multiplier above 1.0, the registered contiguous rolling folds,
and plus/minus sensitivity perturbations of each changed numeric parameter.
When the cache has advanced to a later completed session, the same
computations run in `independent_replication` mode against the newer snapshot
and the record discloses how many sessions postdate the registration. A cache
that neither matches nor postdates the registration is refused.

Every pre-registered prediction is judged against its exact falsification
criterion with a disclosed formula; because each criterion is the strict
negation of its prediction, each judgment is `SUPPORTED` or `FALSIFIED` with
no discretionary middle state. The verdict is `SUPPORTED`/`FALSIFIED` in
same-snapshot mode and `REPLICATED`/`NOT_REPLICATED` in replication mode. A
falsified run is published exactly like a supported one: falsification is
evidence, not an error. Judgments are deterministic threshold comparisons; no
p-value is computed, and the record says so next to the registered Holm plan
and the run's position inside its three-hypothesis snapshot family.

Run records are immutable create-once files under the same owner directory
(`runs/run_<hex>.json`, 256 KiB cap, 500 per owner, 20 per hypothesis). Each
record binds the hypothesis record and design fingerprints, the executed
snapshot fingerprint, and the configuration context; repeating an identical
execution returns the existing record as reused instead of appending a
duplicate. Tampered records fail fingerprint verification on read.

## Exploratory parameter sweep

`parameter-sweep` runs a bounded one-at-a-time neighborhood scan around the
active Strategy Lab baseline and stores every variant's full backtest metrics
as one immutable record:

```powershell
ai-trade --config config/default.json parameter-sweep --objective sharpe --points 4
ai-trade --config config/default.json parameter-sweeps --limit 20
ai-trade --config config/default.json parameter-sweep-show sweep_<32-lowercase-hex>
```

The sweep covers every allowlisted numeric parameter by default (or an
explicit `--parameters strategy.lookback_days,...` subset), evaluates at most
200 variants, and ranks them by the declared objective delta (`sharpe`,
`max_drawdown`, or `turnover`). It is deliberately labeled exploration, not
confirmation: the record's fixed disclosure states that rankings are inflated
by multiple comparisons and ignore parameter interactions, and its safety
contract adds `exploratory_not_confirmatory` and `may_register_hypothesis:
false`. A promising direction still has to be pre-registered as a hypothesis
and survive the experiment runner's holdout, cost-stress, sensitivity, and
later-snapshot replication before any human materialization. Sweep records
are create-once, owner-isolated (`sweeps/sweep_<hex>.json`, 100 per owner),
idempotent per sweep fingerprint, and fingerprint-verified on read.

## Authority boundary

The hypothesis schema fixes all of these values to false: candidate creation,
approval, activation, trading, broker-configuration changes, and
validation-gate weakening. Run records additionally fix
`verdict_grants_no_authority` to true: a `SUPPORTED` or `REPLICATED` verdict
is research evidence only. It does not create, validate, approve, or activate
a Strategy Lab candidate, does not touch paper accounting or broker
configuration, and does not count toward any live-trading gate.
`hypothesis-materialize --yes` is a separate human
action. It first verifies that the active parent, configuration, proposed
changes, and candidate fingerprint still equal the registration, then creates
one deterministic Strategy Lab `DRAFT`. Repeating or concurrently issuing the
same confirmation resolves to the same candidate ID. Validation and approval
remain separate Strategy Lab actions.

The lab stores no API key, raw prompt, model response, hidden reasoning, order,
position, or target allocation. A future model generator must enter through
the existing model-call governance layer and produce the same strict record
contract.
