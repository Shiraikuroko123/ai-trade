# AI K-line Assistant

AI Trade `v0.12.1` includes an optional K-line assistant for reviewing completed market bars. It is always `research_only`: it cannot produce an order, change a target portfolio, approve a strategy candidate, unlock a broker gate, or promise a return.

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
| `fundamental_coverage` | `UNAVAILABLE` until a validated financial-data provider is configured | Explicit coverage evidence; no financial conclusion is inferred |
| `sentiment_coverage` | `UNAVAILABLE` until traceable news, flow, or sentiment data is configured | Explicit coverage evidence; model prose cannot fill the gap |
| `strategy_gate` | Available | Deterministic conclusion, research gate, and assessment evidence |

The Unreleased Dragon-Tiger List market-intelligence dataset is a single-source
closing event ledger. It is not a validated sentiment methodology and therefore
does not change `sentiment_coverage` from `UNAVAILABLE`.

Each view contains `status`, `stance`, `summary`, `limitation`, and
`evidence_ids`. The engine rejects a result with an unknown view, duplicate
view, missing evidence reference, or incomplete coverage. Model-enhanced mode
can change wording and can only tighten the local conclusion; it cannot change
the coverage status or introduce uncited facts.

Enforcement outside the model must keep `authority="research_only"` and reject any result that tries to create an order intent, target position, quantity, entry price, stop, take-profit instruction, portfolio mutation, paper-promotion fact, sandbox reconciliation, live authorization, or changed kill-switch state. A timeout, stale snapshot, malformed response, unknown conclusion, or provider failure fails closed.

## Windows Model Configuration

Model-enhanced mode reads exactly these current-user environment variables:

| Variable | Purpose | Rule |
|---|---|---|
| `AI_TRADE_AI_BASE_URL` | OpenAI-compatible API base URL | HTTPS, or HTTP on a loopback host only |
| `AI_TRADE_AI_MODEL` | Model ID supported by that endpoint | Required for model-enhanced mode |
| `AI_TRADE_AI_API_KEY` | Provider credential | Required; never stored in assistant history |
| `AI_TRADE_AI_TIMEOUT_SECONDS` | Request timeout | Integer from 1 through 120; default 30 |

Configure the values interactively:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_ai.ps1
```

The Base URL defaults to `https://api.openai.com/v1`. The script reads the API key with `SecureString`, does not echo it, and does not create a repository `.env`, JSON, or credential file. It rejects URL credentials, query strings, fragments, non-HTTPS remote endpoints, and unsupported timeouts. Restart the workstation after configuration so the running process inherits the user environment.

Remove all four variables and continue with zero-key local mode:

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

Per-user results are stored under `state/assistant/`. The repository's `state/*` rule excludes them from Git. The R2 exporter reads only a market-cache allowlist and cannot include assistant history, while release verification rejects every `state/` member. The API key is not written to assistant records, reports, cloud snapshots, browser payloads, or release artifacts.

## Clean-room Reference

The assistant was independently designed after a clean-room review of the public, observable research workflow of `rosemarycox5334-debug/PA_Agent`. No PA_Agent AGPL source code, prompts, schemas, UI, assets, or documentation text is included, adapted, or used as a runtime dependency.
