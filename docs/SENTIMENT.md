# Sentiment Tilt Evidence

The sentiment tilt engine composes one bounded, explainable market-tilt
number per trading day from evidence stores that already exist locally. It is
a deterministic aggregation, not a model: no network call, no provider
refresh, no LLM, and no change to the assistant's coverage contract.

## Local workflow

```powershell
ai-trade --config config/default.json sentiment-compose
ai-trade --config config/default.json sentiment-compose --date 2026-07-24
ai-trade --config config/default.json sentiment-show
ai-trade --config config/default.json sentiment-list --limit 30
```

`sentiment-compose` reads the already-validated local market-breadth,
capital-flow, and news-lexicon stores; it never refreshes a provider. Refresh
those stores first through their own audited commands if the underlying
evidence is stale.

## Composition contract

Each component is normalized into `[-1, +1]` with a disclosed formula:

- **breadth** — `2 * advancers / (advancers + decliners) - 1` from the
  market-breadth projection.
- **capital_flow** — `2 * positive_main_share - 1` from the capital-flow
  summary.
- **news_lexicon** — the mean deterministic lexicon score over the day's
  annotated news items, requiring at least 5 annotated items.

A component whose store reports a different trade date than the composition
date is excluded with an explicit `剔除` reason rather than silently blended
across days. The tilt score is the plain mean of the available components.
Two floors keep the number honest: fewer than 2 available components fails
closed (`单一来源不合成` — one source is a reading, not a composition), and
the label bands are wide (`RISK_ON_TILT` at ≥ +0.2, `RISK_OFF_TILT` at
≤ −0.2, `NEUTRAL` between) so small noise never changes the label.

## Storage, revisions, and integrity

Records are create-once JSON files
`state/sentiment/tilt_<date>_r<seq>.json`. Re-composing the same date with
identical component evidence returns the stored record as `reused`; changed
evidence appends a superseding revision whose `supersedes` field carries the
previous record's fingerprint, so the full history of what was believed and
when survives. Fingerprints are recomputed on read and a tampered record
fails closed.

## Authority boundary

The record's safety block fixes `research_only` and marks
`assistant_coverage_unchanged`: the tilt is display-and-journal evidence for
a human. It feeds no strategy input, creates no signal or candidate, changes
no account, and grants no authority anywhere else in the system.
