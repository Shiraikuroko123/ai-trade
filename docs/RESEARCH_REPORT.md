# Consolidated Research Report

`research-report` projects evidence that already exists in the workspace into
one deterministic Markdown document. It is a template over local files — no
model call, no provider refresh, no state mutation beyond the single output
file:

```powershell
ai-trade --config config/default.json research-report
ai-trade --config config/default.json research-report --output reports\weekly_review.md
```

The report has eight sections: market snapshot (from the verified local
cache), strategy backtest versus benchmark (`reports/backtest_summary.json`),
rolling out-of-sample validation (`reports/walk_forward.json`), robustness
gates with their status text and `live_ready` flag
(`reports/validation_report.json`), the paper account
(`state/paper_state.json`), and the latest factor, model, and
hypothesis/experiment records from the owner-isolated research stores.

Every section fails soft and explicitly: missing or malformed evidence is
rendered as “（不可用）” with a bounded reason instead of being padded,
estimated, or invented. The header carries the generation time, application
version, universe name, and a content fingerprint computed over the section
bodies (excluding the timestamp), so two reports over identical evidence are
verifiably identical. The output path must stay inside the workspace.

The report is `research_only` and says so in its own text: it is a review
document, not investment advice, and it cannot create orders, change
strategies, or unlock any permission. When an LLM layer arrives in the
`v2.0.0` line it may add optional commentary alongside this deterministic
projection, never replace it.
