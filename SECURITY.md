# Security Policy

## Supported Version

Only the latest release on `main` is supported.

## Reporting a Vulnerability

Do not open a public issue for credential exposure, command execution, data-integrity bypasses, or trading-safety defects. Use the repository's private GitHub Security Advisory and include reproduction steps, impact, and affected versions.

## Trading Boundary

AI Trade is a research and local paper-trading tool. It has no working live-broker adapter. The `live-check` command requires an explicit environment confirmation and still fails because no broker is configured.

Never commit or paste broker passwords, fund passwords, API secrets, session cookies, private keys, or recovery phrases. The project never requires a crypto-wallet signature, token purchase, or deposit to unlock features.

## Local Data

Market caches, account state, trade journals, reports, logs, `.env`, and virtual environments are excluded from Git. Review `git status` before every push. Treat generated reports as potentially sensitive because they can reveal capital and positions.

## Generated and External Code

Review third-party strategies and patches before execution. The bundled Vibe-Trading checkout used during development is a read-only external reference and is not part of this repository.
