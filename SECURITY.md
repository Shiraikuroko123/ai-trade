# Security Policy

## Supported Version

Only the latest release on `main` is supported.

## Reporting a Vulnerability

Do not open a public issue for credential exposure, command execution, data-integrity bypasses, or trading-safety defects. Use the repository's private GitHub Security Advisory and include reproduction steps, impact, and affected versions.

## Trading Boundary

AI Trade is a research and local paper-trading tool. It has no bundled working live-broker adapter. The local workstation binds only to a loopback address, validates the Host header, and denies cross-origin writes. Beta mode stores only PBKDF2 password verifiers in the Git-ignored `state/` directory, keeps sessions in memory, uses `HttpOnly`/`SameSite=Strict` cookies, and binds every write token to the authenticated session. `serve --owner-local` deliberately bypasses beta login for the owner on a trusted local machine; it must never be treated as remote access control.

Neither a successful Eastmoney/Tencent refresh nor a cloud backup is a live-trading authorization or a warranty of data fitness. Public provider responses can be unavailable, revised, rounded, or inconsistent with exchange-grade feeds. The manifest records provider routing and declared precision so operators can audit a snapshot, but that evidence does not replace independent data validation. The project provides no investment advice, performance guarantee, or bundled route capable of placing a real order.

Future live submission requires every independent gate: current paper-account configuration, forward promotion eligibility, an installed adapter and account, consecutive clean sandbox reconciliations bound to the active configuration, a clear kill switch, a matching unexpired authorization file, explicit live mode, and the exact process-level risk confirmation. Pre-trade checks also enforce the active universe, lot and tick sizes, daily price limits, broker-available cash/positions, single-order limits, and reserved daily notional.

Never commit or paste broker passwords, fund passwords, beta password files, exported beta-user bundles, API secrets, session cookies, private keys, or recovery phrases. Portable beta-user bundles contain offline password verifiers: distribute them privately and rotate affected passwords if a bundle leaks. The project never requires a crypto-wallet signature, token purchase, or deposit to unlock features.

## Local Data

Market caches, account state, trade journals, reports, logs, `.env`, and virtual environments are excluded from Git. Review `git status` before every push. Treat generated reports as potentially sensitive because they can reveal capital and positions.

## Optional Cloud Storage

Cloudflare R2 backup is disabled and unconfigured by default. Every user must supply a separate private account configuration; the project does not ship or fall back to an author's shared bucket or credentials. It is an object backup for the validated `data/cache` dataset, not a mounted working directory, account-sync service, market-data provider, or trading service. The snapshot builder uses an explicit file allowlist: reports, logs, paper or beta state, broker credentials, live-trading authorization, kill-switch files, sessions, and local environment files are never included.

Cloud credentials come only from the current user's environment. User-level environment variables are convenient but are not a secret vault: programs running as that user may read them. Use a private bucket and a dedicated, least-privilege token, never paste values into issues, logs, screenshots, shell history, or tracked files, and rotate the token after suspected exposure. A token imported from an existing Paper Scout plaintext environment file should be treated as shared legacy material and replaced with a separate AI Trade token after migration; do not publish the source path or values.

Cloud restores are fail-closed and staging-only. Archives must pass size, SHA-256, schema, member allowlist, and path checks before extraction into a new Git-ignored `local/` directory. The command never replaces the active cache. Treat R2 as a backup copy rather than an authoritative market-data provider, and confirm provider licensing and retention requirements before storing downloaded data remotely.

## Generated and External Code

Review third-party strategies and patches before execution. The bundled Vibe-Trading checkout used during development is a read-only external reference and is not part of this repository.
