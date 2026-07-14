# Product

## Release Baseline

`v0.12.0` is the first public release. It provides completed-session market review, research and strategy validation, paper accounting, optional private R2 backup, and broker-readiness controls. It does not provide intraday market data, a working live-broker adapter, or automatic real-money execution.

## Register

product

## Users

The primary user is a Chinese-speaking individual investor working on one trusted Windows computer. They review market data after the close, compare strategy evidence, inspect risk and positions, run controlled research jobs, and advance a paper account over many sessions. The interface should support repeated daily use without requiring command-line knowledge, while retaining enough detail for an experienced user to audit every result.

## Product Purpose

AI Trade is a local, end-to-end systematic investment workstation for China A-shares and exchange-traded funds. It unifies point-in-time security data, strategy signals, portfolio construction, historical validation, paper execution, accounting, risk controls, scheduled operations, and audit evidence behind one interface.

Success means the user can answer six questions from one source of truth: what data the system used, how current and trustworthy that snapshot is, why a position is proposed, what could go wrong, what the account currently owns, and whether the evidence is strong enough to advance to the next operating stage.

Future real-market trading is an explicit product goal, implemented through isolated broker adapters. Historical success never enables live trading. Promotion requires a frozen strategy version, sufficient independent paper sessions, a broker sandbox, order and position reconciliation, kill switches, credential isolation, and a separate human authorization for the selected broker account.

## Brand Personality

Rigorous, calm, transparent. The product should feel like a well-run investment desk: dense enough for real work, quiet enough for sustained attention, and candid about uncertainty. The voice is precise and direct without sounding academic or promotional.

## Anti-references

- Profit-guarantee marketing, countdown urgency, social proof, or language that implies the model cannot lose.
- Flashing red/green gambling-terminal aesthetics, decorative ticker walls, and motion that competes with decisions.
- Opaque AI-agent theatre where commentary replaces deterministic data, parameters, and code.
- Marketing hero layouts, oversized slogans, glassmorphism, purple-blue gradients, and endless decorative card grids.
- Interfaces that hide data age, configuration drift, rejected orders, drawdown, or unavailable live-trading permissions.

## Design Principles

1. Evidence before action. Every recommendation links to its data date, model version, constraints, and validation state.
2. Progressive authority. Research, paper, sandbox, and live modes are visibly distinct; no historical metric silently unlocks the next stage.
3. Quiet operational density. Repeated workflows stay compact, scannable, and keyboard-accessible without turning into a decorative dashboard.
4. Explain the constraint. Empty states, warnings, rejected orders, and disabled controls state what is missing and what resolves it.
5. One accounting truth. Strategy views may propose; only reconciled ledgers define cash, positions, fills, fees, and performance.

## Accessibility & Inclusion

Target WCAG 2.2 AA. All workflows must work by keyboard, maintain visible focus, use semantic landmarks and labels, and avoid relying on red/green or color alone. Tables need readable overflow behavior, charts need text summaries, motion must respect reduced-motion preferences, and Chinese labels must remain legible at 200% zoom and on narrow mobile screens.
