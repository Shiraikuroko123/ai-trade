# Security Policy

## Supported Version

Only the latest release on `main` is supported.

## Reporting a Vulnerability

Do not open a public issue for credential exposure, command execution, data-integrity bypasses, or trading-safety defects. Use the repository's private GitHub Security Advisory and include reproduction steps, impact, and affected versions.

## Trading Boundary

AI Trade is a research and local paper-trading tool. It has no bundled working live-broker adapter. The local workstation binds only to a loopback address, validates the Host header, denies cross-origin writes, and requires a per-process write token for state-changing jobs.

Future live submission requires every independent gate: current paper-account configuration, forward promotion eligibility, an installed adapter and account, consecutive clean sandbox reconciliations bound to the active configuration, a clear kill switch, a matching unexpired authorization file, explicit live mode, and the exact process-level risk confirmation. Pre-trade checks also enforce the active universe, lot and tick sizes, daily price limits, broker-available cash/positions, single-order limits, and reserved daily notional.

Never commit or paste broker passwords, fund passwords, API secrets, session cookies, private keys, or recovery phrases. The project never requires a crypto-wallet signature, token purchase, or deposit to unlock features.

## Local Data

Market caches, account state, trade journals, reports, logs, `.env`, and virtual environments are excluded from Git. Review `git status` before every push. Treat generated reports as potentially sensitive because they can reveal capital and positions.

## Generated and External Code

Review third-party strategies and patches before execution. The bundled Vibe-Trading checkout used during development is a read-only external reference and is not part of this repository.
