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

## Authority boundary

The hypothesis schema fixes all of these values to false: candidate creation,
approval, activation, trading, broker-configuration changes, and
validation-gate weakening. `hypothesis-materialize --yes` is a separate human
action. It first verifies that the active parent, configuration, proposed
changes, and candidate fingerprint still equal the registration, then creates
one deterministic Strategy Lab `DRAFT`. Repeating or concurrently issuing the
same confirmation resolves to the same candidate ID. Validation and approval
remain separate Strategy Lab actions.

The lab stores no API key, raw prompt, model response, hidden reasoning, order,
position, or target allocation. A future model generator must enter through
the existing model-call governance layer and produce the same strict record
contract.
