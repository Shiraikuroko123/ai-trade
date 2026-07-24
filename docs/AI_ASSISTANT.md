# AI K-line Assistant

AI Trade `v0.18.1` includes an optional K-line assistant for reviewing completed market bars. It is always `research_only`: it cannot produce an order, change a target portfolio, approve a strategy candidate, unlock a broker gate, or promise a return.

## Contract

The assistant has two internal modes. The workstation labels them **本地规则** and **模型增强**:

- `local`: available with no API key and uses AI Trade's deterministic local workflow.
- `model`: sends a bounded research request to the model endpoint configured for the current Windows user.

Both modes return one of four closed conclusion values:

| Conclusion | Meaning |
|---|---|
| `NO_ACTION` | The available evidence does not justify another review step. |
| `WATCH` | Wait for more completed data and keep the instrument under research review. |
| `REVIEW_CANDIDATE` | The evidence warrants human review; this is not approval to trade. |
| `REDUCE_RISK` | Review existing risk exposure; this is not an instruction to sell, resize, or submit an order. |

## Research Perspective Matrix

Every new analysis also returns five deterministic, evidence-bound views under
`perspectives`. They are a review layout, not five independent trading agents:

| Key | Status in the current release | Evidence basis |
|---|---|---|
| `technical` | Available | EMA20/EMA50, 20-session momentum, RSI, candle structure, and breakout state |
| `risk` | Available | Annualized volatility, ATR14 percentage, and support reference |
| `fundamental_coverage` | Available for exact-date stock evidence; explicitly abstains when sparse or conflicting | Point-in-time financial fields plus current and historical valuation evidence, each cited by evidence ID |
| `sentiment_coverage` | `UNAVAILABLE`; traceable source records exist but no complete sentiment methodology is wired in | Explicit coverage evidence; model prose cannot fill the gap |
| `strategy_gate` | Available | Deterministic conclusion, research gate, and assessment evidence |

The current release reads stock fundamentals and valuation evidence already
stored for the exact final K-line date. It performs no network fetch during an
analysis, accepts only `current` or `partial` records, and excludes
`provisional` valuation so pre-close observations cannot leak into a completed
bar review. ETFs remain explicitly unsupported. Fewer than two directional
signals, a conflict between supportive and adverse signals, or a field-level
conflict with an already stored Tushare reference check produces a `MIXED`
abstention rather than an inferred conclusion. The reference check never fills
or replaces Eastmoney primary fields. Each available check is cited as
`fundamental.independent_check` or `valuation.independent_check`. Sentiment remains
`UNAVAILABLE`: official disclosures, third-party news, Dragon-Tiger List,
breadth, capital flow, and Level-1 depth do not form a validated sentiment
methodology.

Each view contains `status`, `stance`, `summary`, `limitation`, and
`evidence_ids`. The engine rejects a result with an unknown view, duplicate
view, missing evidence reference, or incomplete coverage. Model-enhanced mode
can change wording and can only tighten the local conclusion; it cannot change
the coverage status or introduce uncited facts.

## Deterministic Perspective Conflict Audit

Every new analysis on `main` includes `conflict_audit`, produced by
`deterministic-perspective-audit-v1`. It compares the registered perspective
stances after the local rules and optional model wording layer have finished.
It is not a multi-model vote, an ensemble score, a confidence probability, or
an execution decision.

The audit keeps two classes of evidence separate:

| Class | Meaning |
|---|---|
| `conflicts` | An available technical, risk, or strategy-gate view materially disagrees with another available view, or a model attempted to relax the deterministic conclusion. |
| `coverage_gaps` | A registered view is `UNAVAILABLE`; absence is not counted as disagreement or silently converted into consensus. |

Its status is `REVIEW_REQUIRED` when at least one conflict exists,
`INCOMPLETE` when there are no conflicts but one or more coverage gaps, and
`ALIGNED` only when every registered view has data and no conflict is found.
Each conflict records a stable conflict ID, affected perspective keys, evidence
references, explanation, and manual resolution requirement. Current conflict
IDs cover technical/risk divergence, strategy/technical divergence,
strategy/risk divergence, a stricter risk override, and the model-authority
guard.

`model_review` records the deterministic conclusion, the model-proposed
conclusion when a valid response was applied, the effective conclusion, and
whether relaxation was blocked or the result was tightened. Internal
validation reconstructs the complete audit from the perspective evidence and
checks these conclusion-order invariants before saving the record. A changed
count, summary, conflict row, coverage row, guard flag, or effective conclusion
fails validation. Historical records created before this field remain readable;
the workstation labels their audit as unavailable and asks the user to rerun
the analysis instead of inferring a result retroactively.

Enforcement outside the model must keep `authority="research_only"` and reject any result that tries to create an order intent, target position, quantity, entry price, stop, take-profit instruction, portfolio mutation, paper-promotion fact, sandbox reconciliation, live authorization, or changed kill-switch state. A timeout, stale snapshot, malformed response, unknown conclusion, or provider failure fails closed.

## Auditable Bull, Bear, and Judge Ledger

Every new analysis also contains `auditable-bull-bear-judge-v1`. This is a
structured research ledger, not agent voting or a trading cycle. The bull and
bear records can contain only a bounded summary, evidence-cited arguments,
evidence-cited counterevidence, and an explicit abstention state. Stable
identifiers such as `bull_argument_1` and `bear_counter_1` let the judge refer
to validated role records without copying free-form hidden reasoning.

The judge can contain only `agreements`, `conflicts`, and
`unresolved_questions`. Conflict rows must cite known bull IDs, known bear IDs,
and known evidence IDs in their respective fields. Cross-role IDs, unknown
evidence, additional conclusion/order/position fields, and empty untraceable
claims fail validation. The ledger fixes `conclusion_mutation_allowed=false`,
`execution_authorized=false`, and requires its effective conclusion to equal
the separately validated assistant assessment.

In `local` mode all three roles are deterministic and report zero model Token
usage and no model cost. In `model` mode bull, bear, and judge are three
separate governed calls. Each receives its own role-bound request fingerprint,
budget decision, concurrency check, retry record, user-isolated cache identity,
usage, cost estimate, and immutable audit summary. A normal provider or schema
failure in one advocate falls back only that role; the other advocate still
runs, and the judge receives the validated model or local record from each
side. Audit-store integrity failure still closes all later model work in the
process, because continuing without an audit would violate the governance
boundary.

## Windows Model Configuration

Model-enhanced mode reads these environment variables. The Windows helper sets
the endpoint, credential, timeout, and core governance limits for the current
user; Compose exposes the same values explicitly:

| Variable | Purpose | Rule |
|---|---|---|
| `AI_TRADE_AI_BASE_URL` | OpenAI-compatible API base URL | HTTPS, or HTTP on a loopback host only |
| `AI_TRADE_AI_MODEL` | Model ID supported by that endpoint | Required for model-enhanced mode |
| `AI_TRADE_AI_API_KEY` | Provider credential | Required; never stored in assistant history |
| `AI_TRADE_AI_TIMEOUT_SECONDS` | Request timeout | Integer from 1 through 120; default 30 |
| `AI_TRADE_AI_MAX_RETRIES` | Retry budget after the first attempt | Integer 0-3; default 1; only rate-limit, server, and transport failures retry |
| `AI_TRADE_AI_MAX_CONCURRENT_CALLS` | Process-level concurrency cap | Integer 1-8; default 1 |
| `AI_TRADE_AI_MAX_TOKENS_PER_CALL` | Conservative accounted Token limit per logical call | Integer 2,000-10,000,000; default 50,000 |
| `AI_TRADE_AI_DAILY_TOKEN_BUDGET` | Per-user accounted Token budget | UTC day; default 100,000 |
| `AI_TRADE_AI_INPUT_COST_PER_MILLION_USD` | Optional input price for estimates | Must be configured together with output price |
| `AI_TRADE_AI_OUTPUT_COST_PER_MILLION_USD` | Optional output price for estimates | Must be configured together with input price |
| `AI_TRADE_AI_DAILY_COST_BUDGET_USD` | Optional per-user daily cost ceiling | Requires both price variables; UTC day |

Configure the values interactively:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_ai.ps1
```

The Base URL defaults to `https://api.openai.com/v1`. The script reads the API key with `SecureString`, does not echo it, and does not create a repository `.env`, JSON, or credential file. It rejects URL credentials, query strings, fragments, non-HTTPS remote endpoints, and unsupported timeouts. Restart the workstation after configuration so the running process inherits the user environment.

Remove all model and governance variables and continue with zero-key local mode:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_ai.ps1 -Disable
```

Current-user environment variables are not a dedicated secret vault: another process running as the same Windows user may read them. Use a restricted provider key, never paste it into logs, screenshots, issues, or shell commands, and rotate it at the provider after suspected exposure. Disabling local variables does not revoke the provider credential.

## Workstation Route

Start the configured workspace through the local server:

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli serve --owner-local
```

Open the printed loopback URL, choose **AI 分析** in the left navigation, select an instrument and **回看交易日**, then choose **本地规则** or **模型增强**. A beta deployment uses `serve` and follows the same route after sign-in. Opening `src/ai_trade/web/assets/index.html` directly is unsupported because the view requires the local API server.

## Data and Storage Boundary

Model-enhanced mode discloses bounded indicators and evidence derived from the selected completed K-line window to the configured provider. Review its retention, training, residency, and account policies before enabling the mode. Never include broker or fund passwords, tokens, private positions, personal identifiers, material non-public information, or third-party confidential text.

Per-user results are stored under `state/assistant/`. New result files contain
a recomputable `record_sha256` and are published without replacing an existing
analysis ID. A fingerprinted record that no longer matches its content is
excluded from history and from the next-analysis comparison. Legacy
`schema_version=1` records without this field remain readable. This local hash
detects accidental or unsophisticated file changes; it is not a signature,
remote attestation, WORM archive, or defense against a privileged actor who can
rewrite both the content and hash. Immutable call records are
stored under `state/assistant_calls/`, and normalized schema-validated public
wording and role output is cached under `state/assistant_model_cache/`. User IDs are
represented by SHA-256 directory keys. A cache hit gets a new call audit and is
not charged again. Failed attempts without provider usage are conservatively
accounted against estimated attempted capacity. Audit/cache corruption or
publication failure disables later model calls in the process and falls back
to the deterministic local result. Records contain hashes and public output
only: no API key, endpoint URL, raw prompt, raw provider response, or hidden
reasoning is saved.

Starting with `v0.18.1`, every newly saved result also contains a
`call_audit_binding`. Before publication and whenever history is read, each
available wording, bull, bear, and judge summary is reconstructed from its
immutable call file in the same hashed user scope. The role, template, status,
cache state, usage, cost, budget, UTC date, record fingerprint, and complete
file digest must match. A newly bound model record with missing or altered call
evidence is excluded from history and cannot become the baseline for the next
comparison. Local results retain an explicit `NO_CALLS` binding. Older
schema-version-1 records without the binding remain readable and are labeled as
legacy history in the workstation.

The repository's `state/*` rule excludes all of these records from Git. The R2
exporter reads only a market-cache allowlist and cannot include them, while
release verification rejects every `state/` member. The API key is not written
to assistant records, reports, cloud snapshots, browser payloads, or release
artifacts.

## Clean-room Reference

The assistant was independently designed after a clean-room review of the public, observable research workflow of `rosemarycox5334-debug/PA_Agent`. No PA_Agent AGPL source code, prompts, schemas, UI, assets, or documentation text is included, adapted, or used as a runtime dependency.
