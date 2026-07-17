# Security Policy

## Supported Version

Only `v0.12.0`, the latest release on `main`, is supported. Earlier prerelease tags and GitHub Release objects are not supported distribution channels.

## Reporting a Vulnerability

Do not open a public issue for credential exposure, command execution, data-integrity bypasses, or trading-safety defects. Use the repository's private GitHub Security Advisory and include reproduction steps, impact, and affected versions.

## Trading Boundary

AI Trade is a research and local paper-trading tool. It has no bundled working live-broker adapter. The local workstation binds only to a loopback address, validates the Host header, and denies cross-origin writes. Beta mode stores only PBKDF2 password verifiers in the Git-ignored `state/` directory, keeps sessions in memory, uses `HttpOnly`/`SameSite=Strict` cookies, and binds every write token to the authenticated session. `serve --owner-local` deliberately bypasses beta login for the owner on a trusted local machine; it must never be treated as remote access control.

The separately installed `adapters/qmt` package is an optional read-only observation plugin, not a live-broker adapter. It rejects live construction, order submission, and cancellation. Because xtquant does not provide a reliable paper/live discriminator, its account/position comparison is always diagnostic and cannot write qualifying sandbox reconciliation evidence. Use only the xtquant package supplied by the selected broker, keep account identifiers in the Git-ignored `local/` directory, and never expose a QMT bridge outside the local machine.

Neither a successful Eastmoney/Tencent refresh nor a cloud backup is a live-trading authorization or a warranty of data fitness. Public provider responses can be unavailable, revised, rounded, or inconsistent with exchange-grade feeds. The manifest records provider routing and declared precision so operators can audit a snapshot, but that evidence does not replace independent data validation. The project provides no investment advice, performance guarantee, or bundled route capable of placing a real order.

The market-chart API is authenticated and read-only. It accepts only configured instruments, bounded bar counts, and daily/weekly/monthly periods; it reads verified completed-session files without downloading data, creating transaction locks, recovering interrupted writes, changing strategies, or mutating account state. A pending or changing snapshot fails closed into an unavailable response. Chart indicators are derived display values, not order instructions.

Future live submission requires every independent gate: current paper-account configuration, forward promotion eligibility, an installed adapter with a complete verified live capability declaration, consecutive clean sandbox reconciliations bound to the active account and configuration, a clear kill switch, a matching unexpired schema-versioned authorization and bounded mandate, explicit live mode, and the exact process-level risk confirmation. Reconciliation dates cannot be in the future and count only through the verified completed market date. Authorization timestamps must be timezone-aware and internally ordered; a future approval or an expiry that does not follow approval fails closed. Each exact batch also needs a human-attributed approval with a maximum 15-minute lifetime; it is consumed before broker I/O and retained as a local audit record. Pre-trade checks enforce the mandate symbol/side allowlists, active universe, lot and tick sizes, daily price limits, broker-available cash/positions, single-order and daily limits, and atomic daily order-count reservations. Account identity, available resources, and session health are refreshed after reservation; authorization and the kill switch are checked last before submission.

Authorization, batch-approval, and broker ledger-scope controls are bounded UTF-8 JSON. Duplicate object keys, invalid encoding, oversized files, unknown fields, stale fingerprints, and malformed values fail closed; do not hand-edit these files with tools that silently preserve duplicate fields.

The main configuration and point-in-time security master also use bounded UTF-8 parsing and reject duplicate object keys at every nesting level. This prevents different JSON consumers from disagreeing about broker mode, limits, control paths, strategy/risk settings, provider routing, or universe membership.

Active beta-user stores, portable user exports, and browser JSON writes use the same bounded, duplicate-key-rejecting parsing rules. Versioned user and password records require exact field sets, so unknown credential fields, ambiguous login names, and accidentally embedded plaintext passwords fail closed before authentication state changes.

Never commit or paste broker passwords, fund passwords, beta password files, exported beta-user bundles, API secrets, session cookies, private keys, or recovery phrases. Portable beta-user bundles contain offline password verifiers: distribute them privately and rotate affected passwords if a bundle leaks. The project never requires a crypto-wallet signature, token purchase, or deposit to unlock features.

## AI Assistant Boundary

The K-line assistant is always `research_only`. Its only valid conclusions are `NO_ACTION`, `WATCH`, `REVIEW_CANDIDATE`, and `REDUCE_RISK`. These values are research labels, not order sides. In particular, `REDUCE_RISK` means that a person should review exposure; it is not permission to sell, resize, cancel, or submit an order.

Assistant output cannot create an order intent, target position, quantity, entry, stop, take-profit value, broker request, or portfolio mutation. It cannot satisfy or alter paper-promotion evidence, adapter installation, sandbox reconciliation, kill-switch state, live authorization, process confirmation, or any other trading gate. Model output remains untrusted data even after schema validation, and no response may be presented as guaranteed, profitable, certain, or suitable for a particular user.

Local assistant mode works without an API key. Model-enhanced mode reads `AI_TRADE_AI_BASE_URL`, `AI_TRADE_AI_MODEL`, `AI_TRADE_AI_API_KEY`, and `AI_TRADE_AI_TIMEOUT_SECONDS` from the current Windows user's environment. Remote endpoints must use HTTPS. Plain HTTP is accepted only for a loopback host; URL credentials, query strings, fragments, and cross-origin redirects are rejected. Configure the values with `scripts/configure_ai.ps1`, which reads the key with `SecureString` and does not echo it or write a repository credential file. Use `scripts/configure_ai.ps1 -Disable` to remove all four user/process variables, then restart the workstation.

The application must not persist an AI API key in assistant history, reports, logs, browser payloads, R2 snapshots, or release artifacts. Current-user environment variables are not a dedicated secret vault: any process running as that user may be able to read them. Use a provider key with the narrowest available privileges, rotate it after suspected exposure, and remember that disabling the local variables does not revoke the provider credential.

Model-enhanced mode sends bounded indicators and evidence derived from the selected completed K-line window to the configured provider. Before enabling it, review the provider's retention, training, residency, and account policies. Never include broker or fund passwords, tokens, private positions, personal identifiers, material non-public information, or third-party confidential text in assistant input. A timeout, stale snapshot, malformed response, unknown conclusion, or provider failure must fail closed and never become approval to trade.

## Local Data

Market caches, assistant history, account state, trade journals, reports, logs, `.env`, and virtual environments are excluded from Git. The active paper state is bounded strict JSON with an exact versioned schema; duplicate keys, unknown fields, and malformed accounting values fail closed. Assistant records live under `state/assistant/`, covered by the existing `state/*` ignore rule. They are also outside the R2 market-cache allowlist, and release verification rejects any `state/` member. Review `git status` before every push. Treat generated reports and assistant history as potentially sensitive because they can reveal research interests, capital, or positions.

## Optional Cloud Storage

Cloudflare R2 backup is disabled and unconfigured by default. Every user must supply a separate private account configuration; the project does not ship or fall back to an author's shared bucket or credentials. It is an object backup for the validated `data/cache` dataset, not a mounted working directory, account-sync service, market-data provider, or trading service. The snapshot builder uses an explicit file allowlist: reports, logs, paper or beta state, broker credentials, live-trading authorization, kill-switch files, sessions, and local environment files are never included.

Cloud credentials come only from the current user's environment. User-level environment variables are convenient but are not a secret vault: programs running as that user may read them. Use a private bucket and a dedicated, least-privilege token, never paste values into issues, logs, screenshots, shell history, or tracked files, and rotate the token after suspected exposure. A token imported from an existing Paper Scout plaintext environment file should be treated as shared legacy material and replaced with a separate AI Trade token after migration; do not publish the source path or values.

Cloud restores are fail-closed and staging-only. Archives must pass size, SHA-256, schema, member allowlist, and path checks before extraction into a new Git-ignored `local/` directory. The command never replaces the active cache. Treat R2 as a backup copy rather than an authoritative market-data provider, and confirm provider licensing and retention requirements before storing downloaded data remotely.

## Generated and External Code

Review third-party strategies and patches before execution. The bundled Vibe-Trading checkout used during development is a read-only external reference and is not part of this repository.

The release vendors KLineChart `10.0.0` under Apache-2.0. Its bundle, notices, license texts, and provenance record are shipped locally and verified by fixed SHA-256 values in both wheel and source distributions. Changing any vendored byte requires an explicit version, license, provenance, hash, and browser review.

The public workflow of `rosemarycox5334-debug/PA_Agent` was reviewed only as a clean-room product reference for the K-line assistant. AI Trade does not include, adapt, or depend on PA_Agent's AGPL source code, prompts, schemas, UI, assets, or documentation text. Report any accidental provenance overlap as a security and release-integrity issue before distribution.
