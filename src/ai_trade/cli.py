from __future__ import annotations

import argparse
import importlib.resources
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path

from .assistant import AssistantEngine
from .backtest import BacktestEngine
from .broker.live_guard import BrokerNotConfigured, require_live_confirmation
from .broker.paper import initialize_paper, paper_status, run_paper
from .broker.paper_audit import audit_paper, save_paper_audit
from .broker.probe import (
    available_broker_adapters,
    compare_configured_broker,
    probe_configured_broker,
)
from .config import AppConfig, load_config
from .data.eastmoney import download_universe, load_cached_bars
from .data.market import MarketData
from .diagnostics import diagnose
from .factor_lab import CustomFactorStore, FactorLabEngine
from .hypothesis_lab import HypothesisExperimentRunner, HypothesisLabEngine
from .hypothesis_lab.nested import NestedWalkForwardEngine
from .hypothesis_lab.sweep import ParameterSweepEngine
from .model_lab import ModelLabEngine
from .research_report import write_research_report
from .monitoring import MonitoringEngine
from .report import save_backtest_report
from .strategy import MomentumTrendStrategy
from .validation import run_robustness_validation, save_validation_report
from .walk_forward import run_walk_forward, save_walk_forward


_WEB_JOB_PROTOCOL_ENV = "AI_TRADE_WEB_JOB_PROTOCOL"
_CLOUD_BACKUP_EVENT_PREFIX = "@@AI_TRADE_CLOUD_BACKUP@@"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-trade", description="Auditable systematic research and paper trading"
    )
    parser.add_argument(
        "--config", default="config/default.json", help="Path to JSON configuration"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create a standalone AI Trade workspace")
    init.add_argument("--directory", default=".", help="Target workspace directory")

    download = subparsers.add_parser("download", help="Download and cache market data")
    download.add_argument(
        "--force", action="store_true", help="Overwrite existing cache"
    )

    jqdata_probe = subparsers.add_parser(
        "jqdata-probe",
        help="Inspect JQData entitlement and quota without requesting market data",
    )
    jqdata_probe.add_argument(
        "--non-interactive",
        action="store_true",
        help="Require credentials from process environment instead of prompting",
    )
    jqdata_sample = subparsers.add_parser(
        "jqdata-sample",
        help="Capture and reconcile a bounded licensed JQData history sample",
    )
    jqdata_sample.add_argument(
        "--end-date",
        required=True,
        help="Licensed completed trading date in YYYY-MM-DD form",
    )
    jqdata_sample.add_argument(
        "--non-interactive",
        action="store_true",
        help="Require credentials from process environment instead of prompting",
    )


    subparsers.add_parser(
        "security-master-capture",
        help="Capture the configured master as an immutable knowledge-time version",
    )
    subparsers.add_parser(
        "security-master-versions",
        help="List verified knowledge-time security-master versions",
    )
    security_resolve = subparsers.add_parser(
        "security-master-resolve",
        help="Resolve the master version known at a timezone-aware cutoff",
    )
    security_resolve.add_argument("knowledge_cutoff")

    cross_check = subparsers.add_parser(
        "cross-check-data",
        help="Reconcile the installed daily snapshot with an independent provider",
    )
    cross_check.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Configured six-digit symbol; repeat to limit the audit",
    )

    market_intelligence = subparsers.add_parser(
        "market-intelligence-refresh",
        help="Refresh Dragon Tiger List evidence for one completed market date",
    )
    market_intelligence.add_argument(
        "--date",
        help="YYYY-MM-DD; defaults to the latest verified local market date",
    )

    market_breadth = subparsers.add_parser(
        "market-breadth-refresh",
        help="Refresh sector rankings and exchange breadth for one completed date",
    )
    market_breadth.add_argument(
        "--date",
        help="YYYY-MM-DD; defaults to the latest verified local market date",
    )

    capital_flow = subparsers.add_parser(
        "capital-flow-refresh",
        help="Refresh provider-defined board capital flow for one completed date",
    )
    capital_flow.add_argument(
        "--date",
        help="YYYY-MM-DD; defaults to the latest verified local market date",
    )

    intraday = subparsers.add_parser(
        "intraday-refresh",
        help="Refresh bounded historical minute evidence for configured symbols",
    )
    intraday.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Configured six-digit symbol; repeat to refresh a subset",
    )
    intraday.add_argument("--date", help="YYYY-MM-DD; defaults to latest completed session")
    intraday.add_argument(
        "--interval",
        type=int,
        choices=(1, 5, 15, 30, 60),
        default=1,
        help="Aggregation interval in minutes",
    )
    intraday.add_argument("--limit", type=int, default=480)

    valuation = subparsers.add_parser(
        "valuation-refresh",
        help="Refresh current PE/PB and market-cap quote evidence",
    )
    valuation.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Configured six-digit symbol; repeat to refresh a subset",
    )

    fundamentals = subparsers.add_parser(
        "fundamentals-refresh",
        help="Refresh stock-only point-in-time company fundamental evidence",
    )
    fundamentals.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Configured STOCK symbol; repeat to refresh a subset",
    )
    fundamentals.add_argument("--periods", type=int, default=8)

    disclosures = subparsers.add_parser(
        "disclosures-refresh",
        help="Refresh official SSE and CNINFO disclosure metadata",
    )
    disclosures.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Configured symbol; repeat to refresh a subset",
    )
    disclosures.add_argument("--lookback-days", type=int, default=30)
    disclosures.add_argument("--limit", type=int, default=50, dest="limit_per_symbol")
    disclosures.add_argument(
        "--skip-document-hash",
        action="store_true",
        help="Keep official metadata but skip bounded PDF response hashing",
    )

    order_book = subparsers.add_parser(
        "order-book-refresh",
        help="Refresh public Level-1 five-level order-book snapshots",
    )
    order_book.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Configured six-digit symbol; repeat to refresh a subset",
    )

    news = subparsers.add_parser(
        "news-refresh",
        help="Refresh bounded news and announcement evidence",
    )
    news.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Configured symbol for announcement evidence; repeat to limit scope",
    )
    news.add_argument("--date", help="YYYY-MM-DD; defaults to latest completed session")
    news.add_argument("--limit", type=int, default=50, dest="limit_per_source")

    cloud_status = subparsers.add_parser(
        "cloud-status", help="Inspect this user's optional Cloudflare R2 backup"
    )
    cloud_status.add_argument(
        "--check",
        action="store_true",
        help="Verify access to the configured R2 namespace",
    )
    subparsers.add_parser(
        "cloud-backup", help="Upload a verified market-cache snapshot to Cloudflare R2"
    )
    cloud_list = subparsers.add_parser(
        "cloud-list",
        help="List market-cache snapshots in this installation's namespace",
    )
    cloud_list.add_argument("--limit", type=int, default=20)
    cloud_restore = subparsers.add_parser(
        "cloud-restore",
        help="Verify and restore a cloud snapshot into a new staging directory",
    )
    cloud_restore.add_argument("snapshot_id")
    cloud_restore.add_argument(
        "--directory",
        help=(
            "New destination directory (must not already exist); "
            "defaults to local/cloud-restore/<snapshot-id>"
        ),
    )
    subparsers.add_parser(
        "cloud-digest-backup",
        help="Upload the active local-owner research digest namespace to R2",
    )
    cloud_digest_list = subparsers.add_parser(
        "cloud-digest-list",
        help="List research-digest snapshots in this installation's namespace",
    )
    cloud_digest_list.add_argument("--limit", type=int, default=20)
    cloud_digest_restore = subparsers.add_parser(
        "cloud-digest-restore",
        help="Verify a research-digest snapshot into a new staging directory",
    )
    cloud_digest_restore.add_argument("snapshot_id")
    cloud_digest_restore.add_argument(
        "--directory",
        help=(
            "New destination directory (must not already exist); defaults to "
            "local/cloud-digest-restore/<snapshot-id>"
        ),
    )

    backtest = subparsers.add_parser("backtest", help="Run historical backtest")
    backtest.add_argument("--start", help="YYYY-MM-DD")
    backtest.add_argument("--end", help="YYYY-MM-DD")

    walk = subparsers.add_parser(
        "walk-forward", help="Run rolling out-of-sample validation"
    )
    walk.add_argument("--train-days", type=int, default=756)
    walk.add_argument("--test-days", type=int, default=252)

    validate = subparsers.add_parser(
        "validate", help="Run robustness and stress validation"
    )
    validate.add_argument("--bootstrap-samples", type=int, default=1000)
    validate.add_argument("--block-days", type=int, default=20)

    signal = subparsers.add_parser("signal", help="Show the latest target weights")
    signal.add_argument("--refresh", action="store_true")

    assistant_analyze = subparsers.add_parser(
        "assistant-analyze", help="Run one research-only assistant analysis"
    )
    assistant_analyze.add_argument("--symbol", required=True)
    assistant_analyze.add_argument("--lookback", type=int, default=180)
    assistant_analyze.add_argument(
        "--mode", choices=("local", "model"), default="local"
    )

    hypothesis_generate = subparsers.add_parser(
        "hypothesis-generate",
        help="Pre-register one deterministic hypothesis from the verified local cache",
    )
    hypothesis_generate.add_argument(
        "--objective",
        choices=("auto", "balanced", "drawdown", "turnover"),
        default="auto",
    )
    hypothesis_generate.add_argument(
        "--title",
        help="Optional research title; does not alter the pre-registered tests",
    )
    hypothesis_list = subparsers.add_parser(
        "hypothesis-list", help="List owner-isolated pre-registered hypotheses"
    )
    hypothesis_list.add_argument("--limit", type=int, default=50)
    hypothesis_show = subparsers.add_parser(
        "hypothesis-show", help="Show and verify one immutable hypothesis record"
    )
    hypothesis_show.add_argument("hypothesis_id")
    hypothesis_run = subparsers.add_parser(
        "hypothesis-run",
        help=(
            "Execute one pre-registered hypothesis plan on the verified local "
            "cache and append an immutable run record"
        ),
    )
    hypothesis_run.add_argument("hypothesis_id")
    hypothesis_runs = subparsers.add_parser(
        "hypothesis-runs", help="List owner-isolated hypothesis experiment runs"
    )
    hypothesis_runs.add_argument("--hypothesis", default=None)
    hypothesis_runs.add_argument("--limit", type=int, default=50)
    hypothesis_run_show = subparsers.add_parser(
        "hypothesis-run-show",
        help="Show and verify one immutable hypothesis run record",
    )
    hypothesis_run_show.add_argument("run_id")
    subparsers.add_parser(
        "factor-list",
        help="List the deterministic research factor registry",
    )
    factor_evaluate = subparsers.add_parser(
        "factor-evaluate",
        help=(
            "Evaluate one registered factor point-in-time on the verified "
            "local cache and append an immutable evidence record"
        ),
    )
    factor_evaluate.add_argument("--factor", required=True)
    factor_evaluate.add_argument(
        "--horizons",
        default="5,20,60",
        help="Comma-separated forward horizons in sessions (ascending)",
    )
    factor_evaluate.add_argument("--step", type=int, default=5)
    factor_define = subparsers.add_parser(
        "factor-define",
        help=(
            "Register one immutable custom research factor from a safe "
            "allowlisted expression"
        ),
    )
    factor_define.add_argument("--name", required=True)
    factor_define.add_argument("--expression", required=True)
    factor_define.add_argument("--direction", type=int, default=1, choices=[1, -1])
    factor_define.add_argument("--label", default=None)
    factor_evaluations = subparsers.add_parser(
        "factor-evaluations",
        help="List owner-isolated factor evaluation records",
    )
    factor_evaluations.add_argument("--factor", default=None)
    factor_evaluations.add_argument("--limit", type=int, default=50)
    factor_show = subparsers.add_parser(
        "factor-show",
        help="Show and verify one immutable factor evaluation record",
    )
    factor_show.add_argument("evaluation_id")
    subparsers.add_parser(
        "model-list",
        help="List the deterministic research model registry",
    )
    model_evaluate = subparsers.add_parser(
        "model-evaluate",
        help=(
            "Evaluate one registered model walk-forward over the factor "
            "registry on the verified local cache"
        ),
    )
    model_evaluate.add_argument("--model", default="ridge_v1")
    model_evaluate.add_argument(
        "--factors",
        default=None,
        help="Comma-separated factor ids (default: the whole registry)",
    )
    model_evaluate.add_argument("--horizon", type=int, default=20)
    model_evaluate.add_argument("--step", type=int, default=5)
    model_evaluations = subparsers.add_parser(
        "model-evaluations",
        help="List owner-isolated model evaluation records",
    )
    model_evaluations.add_argument("--model", default=None)
    model_evaluations.add_argument("--limit", type=int, default=50)
    model_show = subparsers.add_parser(
        "model-show",
        help="Show and verify one immutable model evaluation record",
    )
    model_show.add_argument("evaluation_id")
    research_loop_run = subparsers.add_parser(
        "research-loop-run",
        help=(
            "Run a bounded research-only loop over an explicit tool allowlist "
            "and append every outcome to an immutable ledger"
        ),
    )
    research_loop_run.add_argument(
        "--mode", choices=("local", "model"), default="local"
    )
    research_loop_run.add_argument(
        "--plan-file",
        default=None,
        help="Bounded JSON action plan required in local mode",
    )
    research_loop_run.add_argument("--max-rounds", type=int, default=6)
    research_loop_run.add_argument("--max-tool-units", type=int, default=16)
    research_loop_list = subparsers.add_parser(
        "research-loop-list", help="List immutable local research-loop ledgers"
    )
    research_loop_list.add_argument("--limit", type=int, default=50)
    research_loop_show = subparsers.add_parser(
        "research-loop-show", help="Show and verify one research-loop hash chain"
    )
    research_loop_show.add_argument("loop_id")
    feature_build = subparsers.add_parser(
        "feature-build",
        help="Materialize one immutable completed-session FeatureSnapshot",
    )
    feature_build.add_argument("--as-of", help="Completed session in YYYY-MM-DD")
    feature_build.add_argument(
        "--factors",
        default=None,
        help="Comma-separated factor ids matching the intended model evaluation",
    )
    capture_mode = feature_build.add_mutually_exclusive_group()
    capture_mode.add_argument(
        "--live-capture",
        dest="live_capture",
        action="store_true",
        help="Capture the latest completed session with the current knowledge cutoff",
    )
    capture_mode.add_argument(
        "--historical-reconstruction",
        dest="live_capture",
        action="store_false",
        help="Build research-only historical evidence that cannot train an artifact",
    )
    feature_build.set_defaults(live_capture=True)
    feature_forward = subparsers.add_parser(
        "feature-forward-run",
        help=(
            "Capture the current genuine FeatureSnapshot and materialize every "
            "mature pending LabelSnapshot without refreshing market data"
        ),
    )
    feature_forward.add_argument(
        "--factors",
        default=None,
        help="Comma-separated factor ids (default: the whole registry)",
    )
    feature_forward.add_argument(
        "--horizons",
        default="5,20,60",
        help="Comma-separated forward horizons in sessions (ascending)",
    )
    feature_show = subparsers.add_parser(
        "feature-show",
        help="Read and verify one immutable FeatureSnapshot",
    )
    feature_show.add_argument("snapshot_id")
    feature_show.add_argument("--date", required=True, help="YYYY-MM-DD")
    feature_label = subparsers.add_parser(
        "feature-label",
        help="Materialize a separate mature LabelSnapshot for one feature snapshot",
    )
    feature_label.add_argument("snapshot_id")
    feature_label.add_argument("--date", required=True, help="YYYY-MM-DD")
    feature_label.add_argument("--horizon", type=int, required=True)
    artifact_fit = subparsers.add_parser(
        "model-artifact-fit",
        help=(
            "Fit an inference-complete linear artifact only from a statistically "
            "qualified v2 evaluation and mature PIT training pairs"
        ),
    )
    artifact_fit.add_argument("evaluation_id")
    artifact_fit.add_argument(
        "--training-cutoff",
        required=True,
        help="Timezone-aware ISO-8601 timestamp",
    )
    model_predict = subparsers.add_parser(
        "model-predict",
        help="Create an immutable out-of-sample PredictionSnapshot",
    )
    model_predict.add_argument("artifact_id")
    model_predict.add_argument("feature_snapshot_id")
    model_predict.add_argument("--feature-date", required=True, help="YYYY-MM-DD")
    model_predict.add_argument("--valid-from", required=True, help="YYYY-MM-DD")
    model_predict.add_argument("--valid-until", required=True, help="YYYY-MM-DD")
    portfolio_plan = subparsers.add_parser(
        "portfolio-plan",
        help="Create a research-only, cost-constrained immutable PortfolioPlan",
    )
    portfolio_plan.add_argument("prediction_id")
    portfolio_plan.add_argument("--feature-date", required=True, help="YYYY-MM-DD")
    portfolio_plan.add_argument("--execution-date", required=True, help="YYYY-MM-DD")
    portfolio_plan.add_argument("--equity", type=float, required=True)
    portfolio_plan.add_argument(
        "--decision-time",
        help="Timezone-aware ISO-8601 timestamp (default: now)",
    )
    portfolio_plan.add_argument(
        "--current-weights-file",
        help="Bounded JSON object mapping symbols to current portfolio weights",
    )
    parameter_sweep = subparsers.add_parser(
        "parameter-sweep",
        help=(
            "Run an exploratory one-at-a-time parameter neighborhood sweep "
            "on the verified local cache"
        ),
    )
    parameter_sweep.add_argument(
        "--objective", default="sharpe", choices=["sharpe", "max_drawdown", "turnover"]
    )
    parameter_sweep.add_argument(
        "--parameters",
        default=None,
        help="Comma-separated scope.name keys (default: every numeric parameter)",
    )
    parameter_sweep.add_argument("--points", type=int, default=4)
    parameter_sweeps = subparsers.add_parser(
        "parameter-sweeps", help="List owner-isolated parameter sweep records"
    )
    parameter_sweeps.add_argument("--limit", type=int, default=50)
    parameter_sweep_show = subparsers.add_parser(
        "parameter-sweep-show",
        help="Show and verify one immutable parameter sweep record",
    )
    parameter_sweep_show.add_argument("sweep_id")
    nested_walk_forward = subparsers.add_parser(
        "nested-walk-forward",
        help=(
            "Run the nested walk-forward confirmatory tuning protocol: "
            "per-fold selection on inner validation windows, out-of-fold "
            "measurement on embargo-separated test folds"
        ),
    )
    nested_walk_forward.add_argument(
        "--objective", default="sharpe", choices=["sharpe", "max_drawdown", "turnover"]
    )
    nested_walk_forward.add_argument(
        "--parameters",
        default=None,
        help="Comma-separated scope.name keys (default: every numeric parameter)",
    )
    nested_walk_forward.add_argument("--points", type=int, default=2)
    nested_walk_forward.add_argument("--outer-folds", type=int, default=4)
    nested_walk_forward.add_argument("--inner-folds", type=int, default=2)
    nested_walk_forward.add_argument("--embargo", type=int, default=5)
    nested_walk_forwards = subparsers.add_parser(
        "nested-walk-forwards",
        help="List owner-isolated nested walk-forward tuning records",
    )
    nested_walk_forwards.add_argument("--limit", type=int, default=50)
    nested_walk_forward_show = subparsers.add_parser(
        "nested-walk-forward-show",
        help="Show and verify one immutable nested walk-forward record",
    )
    nested_walk_forward_show.add_argument("nested_id")
    sentiment_compose = subparsers.add_parser(
        "sentiment-compose",
        help=(
            "Compose one market-tilt evidence revision from already validated "
            "local breadth, capital-flow, and news stores (no network)"
        ),
    )
    sentiment_compose.add_argument("--date", default=None)
    sentiment_show = subparsers.add_parser(
        "sentiment-show", help="Show the latest market-tilt evidence revision"
    )
    sentiment_show.add_argument("--date", default=None)
    sentiment_list = subparsers.add_parser(
        "sentiment-list", help="List market-tilt evidence by trade date"
    )
    sentiment_list.add_argument("--limit", type=int, default=30)
    sandbox_cycle = subparsers.add_parser(
        "sandbox-cycle",
        help=(
            "Run one isolated broker order-lifecycle drill against cached "
            "bars (sandbox scope only; grants no live authority)"
        ),
    )
    sandbox_cycle.add_argument("--symbol", required=True)
    sandbox_cycle.add_argument("--side", default="BUY", choices=["BUY", "SELL"])
    sandbox_cycle.add_argument("--quantity", type=int, default=None)
    sandbox_cycle.add_argument("--date", default=None)
    sandbox_cycle.add_argument("--limit-price", type=float, default=None)
    subparsers.add_parser(
        "sandbox-status",
        help="Verify the sandbox ledger scope and recover its order lifecycle",
    )
    sandbox_drills = subparsers.add_parser(
        "sandbox-drills", help="List immutable sandbox drill records"
    )
    sandbox_drills.add_argument("--limit", type=int, default=50)
    shadow_event = subparsers.add_parser(
        "shadow-event-append",
        help="Append one research-only event to a pseudonymous ShadowAccount ledger",
    )
    shadow_event.add_argument("--account-reference", required=True)
    shadow_event.add_argument("--event-type", required=True)
    shadow_event.add_argument("--occurred-at", required=True)
    shadow_event.add_argument("--trading-session", required=True, help="YYYY-MM-DD")
    shadow_event.add_argument("--source", required=True)
    shadow_event.add_argument("--external-id", required=True)
    shadow_event.add_argument("--payload-file", required=True)
    shadow_project = subparsers.add_parser(
        "shadow-project",
        help="Rebuild cash, positions, costs, and equity from ShadowAccount events",
    )
    shadow_project.add_argument("--account-reference", required=True)
    shadow_reconcile = subparsers.add_parser(
        "shadow-reconcile",
        help="Compare a ShadowAccount projection with an independent JSON snapshot",
    )
    shadow_reconcile.add_argument("--account-reference", required=True)
    shadow_reconcile.add_argument("--broker-snapshot-file", required=True)
    subparsers.add_parser(
        "universe-verify",
        help=(
            "Cross-check configured listing and membership dates against the "
            "first cached bar of every instrument"
        ),
    )
    research_report = subparsers.add_parser(
        "research-report",
        help=(
            "Project existing local evidence into one deterministic Markdown "
            "research report"
        ),
    )
    research_report.add_argument("--output", default=None)
    hypothesis_from_model = subparsers.add_parser(
        "hypothesis-from-model",
        help=(
            "Register one hypothesis draft derived from a fingerprint-verified "
            "model evaluation (evidence-gated; grants no authority)"
        ),
    )
    hypothesis_from_model.add_argument("evaluation_id")
    hypothesis_from_model.add_argument("--title", default=None)
    hypothesis_materialize = subparsers.add_parser(
        "hypothesis-materialize",
        help="Explicitly create one Strategy Lab draft from a registered hypothesis",
    )
    hypothesis_materialize.add_argument("hypothesis_id")
    hypothesis_materialize.add_argument(
        "--yes",
        action="store_true",
        help="Confirm human creation of a draft candidate; grants no approval",
    )

    monitor_scan = subparsers.add_parser(
        "monitor-scan",
        help="Evaluate persisted research alerts on one completed market snapshot",
    )
    monitor_scope = monitor_scan.add_mutually_exclusive_group()
    monitor_scope.add_argument(
        "--all-profiles",
        action="store_true",
        help="Scan every persisted monitoring profile without exposing account names",
    )
    monitor_scope.add_argument(
        "--owner-local",
        action="store_true",
        help="Scan the loopback owner profile (the default interactive scope)",
    )
    monitor_scope.add_argument(
        "--username",
        help="Scan one local beta account by its configured username",
    )
    monitor_scan.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use the existing verified cache without contacting a provider",
    )

    archive_generate = subparsers.add_parser(
        "archive-generate",
        help="Persist daily and weekly research digests from local evidence",
    )
    archive_scope = archive_generate.add_mutually_exclusive_group()
    archive_scope.add_argument(
        "--all-profiles",
        action="store_true",
        help="Generate owner-isolated digests for the local owner and enabled beta accounts",
    )
    archive_scope.add_argument(
        "--owner-local",
        action="store_true",
        help="Generate the loopback owner digest (the default scope)",
    )
    archive_scope.add_argument(
        "--username",
        help="Generate a digest for one enabled local beta account",
    )
    archive_generate.add_argument(
        "--kind",
        choices=("all", "daily", "weekly"),
        default="all",
        help="Digest kinds to materialize",
    )
    archive_generate.add_argument(
        "--trigger",
        choices=("manual", "scheduled"),
        default="manual",
        help="Operator-supplied audit label; bundled runners use scheduled",
    )
    archive_period = archive_generate.add_mutually_exclusive_group()
    archive_period.add_argument("--date", help="Limit daily evidence to YYYY-MM-DD")
    archive_period.add_argument("--week", help="Limit weekly evidence to an ISO Monday")

    paper_init = subparsers.add_parser(
        "paper-init", help="Initialize the paper account"
    )
    paper_init.add_argument("--cash", type=float)
    paper_init.add_argument("--overwrite", action="store_true")

    paper_run = subparsers.add_parser(
        "paper-run", help="Refresh data and process one paper session"
    )
    paper_run.add_argument("--no-refresh", action="store_true")

    subparsers.add_parser("paper-status", help="Show paper account state")
    subparsers.add_parser(
        "paper-audit", help="Audit forward paper performance and promotion gates"
    )
    universe = subparsers.add_parser(
        "universe-status", help="Inspect point-in-time universe eligibility"
    )
    universe.add_argument("--date", help="YYYY-MM-DD; defaults to today")
    subparsers.add_parser(
        "doctor", help="Check configuration, cache, and latest market date"
    )
    subparsers.add_parser(
        "live-check", help="Verify the live-trading guard; does not submit orders"
    )
    subparsers.add_parser(
        "broker-list", help="List installed broker adapter plugins"
    )
    subparsers.add_parser(
        "broker-probe",
        help="Read the configured sandbox broker without changing broker state",
    )
    subparsers.add_parser(
        "broker-compare",
        help="Compare the paper account with a read-only broker observation",
    )
    serve = subparsers.add_parser("serve", help="Start the local AI Trade workstation")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true", help="Do not open a browser")
    serve.add_argument(
        "--owner-local",
        action="store_true",
        help="Bypass beta login for this loopback-only owner session",
    )
    serve.add_argument(
        "--container-bind",
        action="store_true",
        help=(
            "Allow the Docker container interface; requires beta authentication "
            "and should be paired with a loopback-only published port"
        ),
    )
    beta_add = subparsers.add_parser(
        "beta-user-add", help="Add an account to the local beta whitelist"
    )
    beta_add.add_argument("username")
    beta_add.add_argument(
        "--replace", action="store_true", help="Replace an existing password"
    )
    subparsers.add_parser("beta-user-list", help="List local beta accounts")
    for action in ("enable", "disable", "remove"):
        command = subparsers.add_parser(
            f"beta-user-{action}", help=f"{action.title()} a local beta account"
        )
        command.add_argument("username")
        if action == "remove":
            command.add_argument(
                "--yes", action="store_true", help="Confirm permanent removal"
            )
    beta_export = subparsers.add_parser(
        "beta-users-export",
        help="Export a portable beta whitelist without plaintext passwords",
    )
    beta_export.add_argument("output")
    beta_import = subparsers.add_parser(
        "beta-users-import", help="Import a portable beta whitelist"
    )
    beta_import.add_argument("source")
    beta_import.add_argument(
        "--mode", choices=("reject", "merge", "replace"), default="reject"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return _initialize_workspace(Path(args.directory))
        config = load_config(_resolve_config_path(args.config))
        _configure_logging(config)
        if args.command == "jqdata-probe":
            from .data.jqdata import (
                JQDataCredentialError,
                JQDataProbeStore,
                credentials_from_environment,
                probe_account,
                prompt_credentials,
            )

            credentials = credentials_from_environment()
            if credentials is None:
                if args.non_interactive:
                    raise JQDataCredentialError(
                        "JQData credentials are absent from the process environment"
                    )
                credentials = prompt_credentials()
            probe = probe_account(credentials)
            result = JQDataProbeStore(
                config.project_root / "state" / "jqdata"
            ).publish(probe)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "jqdata-sample":
            from .data.jqdata import (
                JQDataCredentialError,
                JQDataSampleStore,
                capture_price_sample,
                credentials_from_environment,
                prompt_credentials,
                summarize_price_sample,
            )

            credentials = credentials_from_environment()
            if credentials is None:
                if args.non_interactive:
                    raise JQDataCredentialError(
                        "JQData credentials are absent from the process environment"
                    )
                credentials = prompt_credentials()
            sample = capture_price_sample(
                credentials,
                end_date=_required_cli_date(args.end_date, "--end-date"),
                local_cache_dir=config.cache_dir,
            )
            stored = JQDataSampleStore(
                config.project_root / "state" / "jqdata"
            ).publish(sample)
            print(
                json.dumps(
                    summarize_price_sample(stored),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.command.startswith("security-master-"):
            from hashlib import sha256

            from .security_store import SecurityMasterVersionStore

            store = SecurityMasterVersionStore(config.security_master_store_dir)
            if args.command == "security-master-capture":
                source_path = config.security_master.source_path
                if source_path is not None and source_path.is_file():
                    response_sha256 = sha256(source_path.read_bytes()).hexdigest()
                    try:
                        source_label = source_path.resolve().relative_to(
                            config.project_root
                        ).as_posix()
                    except ValueError:
                        source_label = "external-configured-master"
                else:
                    encoded = json.dumps(
                        config.security_master.to_dict(),
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    response_sha256 = sha256(encoded).hexdigest()
                    source_label = "legacy-config"
                version = store.publish(
                    config.security_master,
                    known_at=datetime.now(timezone.utc),
                    source_manifest={
                        "provider": "manual",
                        "dataset": "configured_security_master",
                        "request": {
                            "mode": "configured_snapshot",
                            "source_file": source_label,
                        },
                        "rows": len(config.security_master.instruments),
                        "response_sha256": response_sha256,
                        "usage_scope": "internal_research_only",
                    },
                )
                print(
                    json.dumps(
                        version.summary(), ensure_ascii=False, indent=2, default=str
                    )
                )
                return 0
            if args.command == "security-master-versions":
                print(
                    json.dumps(
                        store.versions(), ensure_ascii=False, indent=2, default=str
                    )
                )
                return 0
            version = store.resolve(
                _parse_cli_timestamp(
                    args.knowledge_cutoff, "knowledge_cutoff"
                )
            )
            print(
                json.dumps(
                    version.summary(), ensure_ascii=False, indent=2, default=str
                )
            )
            return 0
        if args.command in {
            "beta-user-add",
            "beta-user-list",
            "beta-user-enable",
            "beta-user-disable",
            "beta-user-remove",
        }:
            from .web.auth import UserStore

            users = UserStore(config.auth_users_file)
            if args.command == "beta-user-add":
                password = getpass("内测密码: ")
                confirmation = getpass("再次输入: ")
                if password != confirmation:
                    raise ValueError("两次输入的密码不一致")
                user = users.add_user(args.username, password, replace=args.replace)
                print(json.dumps(asdict(user), ensure_ascii=False, indent=2))
                return 0
            if args.command == "beta-user-list":
                print(
                    json.dumps(
                        [asdict(user) for user in users.list_users()],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.command == "beta-user-enable":
                user = users.set_enabled(args.username, True)
                print(json.dumps(asdict(user), ensure_ascii=False, indent=2))
                return 0
            if args.command == "beta-user-disable":
                user = users.set_enabled(args.username, False)
                print(json.dumps(asdict(user), ensure_ascii=False, indent=2))
                return 0
            if args.command == "beta-user-remove":
                if not args.yes:
                    raise ValueError("Permanent removal requires --yes")
                if not users.remove_user(args.username):
                    raise ValueError("Beta user does not exist")
                print(json.dumps({"removed": args.username}, ensure_ascii=False))
                return 0
        if args.command in {"beta-users-export", "beta-users-import"}:
            from .web.auth import UserStore

            users = UserStore(config.auth_users_file)
            if args.command == "beta-users-export":
                output = Path(args.output).expanduser().resolve()
                count = users.export_users(output)
                print(
                    json.dumps(
                        {"output": str(output), "users": count},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            imported = users.import_users(args.source, mode=args.mode)
            print(
                json.dumps(
                    {"users": [asdict(user) for user in imported]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "universe-status":
            on_date = date.fromisoformat(args.date) if args.date else date.today()
            print(
                json.dumps(
                    config.security_master.snapshot(
                        config.universe_name, on_date, config.minimum_listing_days
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command.startswith("cloud-"):
            from .cloud import (
                backup_market_cache,
                cloud_dependency_available,
                load_cloud_settings,
                tracked_r2_store,
            )

            settings = load_cloud_settings()
            if args.command == "cloud-status":
                status = settings.public_status()
                status["dependency_available"] = cloud_dependency_available()
                if args.check:
                    store = tracked_r2_store(config, settings)
                    store.check_connection()
                    status["connection"] = "ok"
                else:
                    status["connection"] = "not_checked"
                print(json.dumps(status, ensure_ascii=False, indent=2))
                return 0
            store = tracked_r2_store(config, settings)
            if args.command.startswith("cloud-digest-"):
                from .research_digest import ResearchDigestStore
                from .research_digest_cloud import (
                    backup_research_digests,
                    list_research_digest_snapshots,
                    restore_research_digest_snapshot,
                )

                if args.command == "cloud-digest-list":
                    snapshots = list_research_digest_snapshots(
                        store, limit=args.limit
                    )
                    public = [
                        {
                            key: value
                            for key, value in item.items()
                            if key != "object_key"
                        }
                        for item in snapshots
                    ]
                    print(
                        json.dumps(
                            {"dataset": "research-digests", "snapshots": public},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 0
                if args.command == "cloud-digest-restore":
                    destination = (
                        Path(args.directory)
                        if args.directory
                        else config.project_root
                        / "local"
                        / "cloud-digest-restore"
                        / args.snapshot_id
                    )
                    restored = restore_research_digest_snapshot(
                        store, args.snapshot_id, destination
                    )
                    print(
                        json.dumps(
                            {
                                "dataset": "research-digests",
                                "snapshot_id": args.snapshot_id,
                                "restored_to": str(restored),
                                "active_state_unchanged": True,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 0
                state = paper_status(config)
                result = backup_research_digests(
                    ResearchDigestStore(config.research_digest_dir),
                    "local-owner",
                    str(state["account_id"]),
                    store,
                )
                public_keys = (
                    "dataset",
                    "snapshot_id",
                    "sha256",
                    "dataset_sha256",
                    "size",
                    "created_at",
                    "account_fingerprint",
                    "skipped_duplicate",
                )
                print(
                    json.dumps(
                        {key: result[key] for key in public_keys if key in result},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.command == "cloud-backup":
                _ensure_cache(config)
                result = backup_market_cache(config, store)
                public_keys = (
                    "snapshot_id",
                    "sha256",
                    "dataset_sha256",
                    "size",
                    "created_at",
                    "latest_common_session",
                    "skipped_duplicate",
                )
                print(
                    json.dumps(
                        {key: result[key] for key in public_keys if key in result},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.command == "cloud-list":
                snapshots = store.list_snapshots(limit=args.limit)
                public = [
                    {key: value for key, value in item.items() if key != "object_key"}
                    for item in snapshots
                ]
                print(json.dumps({"snapshots": public}, ensure_ascii=False, indent=2))
                return 0
            destination = (
                Path(args.directory)
                if args.directory
                else config.project_root / "local" / "cloud-restore" / args.snapshot_id
            )
            restored = store.restore_snapshot(config, args.snapshot_id, destination)
            print(
                json.dumps(
                    {
                        "snapshot_id": args.snapshot_id,
                        "restored_to": str(restored),
                        "active_cache_unchanged": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "download":
            paths = download_universe(config, force=args.force)
            _maybe_automatic_cloud_backup(config)
            print(
                json.dumps(
                    {key: str(value) for key, value in paths.items()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "cross-check-data":
            _ensure_cache(config)
            from .data.cross_check import cross_check_market_snapshot

            result = cross_check_market_snapshot(
                config,
                symbols=args.symbols,
                force=True,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result.get("persisted") is True else 1
        if args.command == "market-intelligence-refresh":
            on_date = _parse_cli_iso_date(args.date, "date")
            if on_date is None:
                on_date = MarketData(config).latest_date()

            from .data.market_intelligence import refresh_dragon_tiger

            result = refresh_dragon_tiger(config, on_date)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return (
                0
                if result.get("available") is True
                and result.get("status") in {"current", "empty"}
                and not result.get("errors")
                else 1
            )
        if args.command == "market-breadth-refresh":
            on_date = _parse_cli_iso_date(args.date, "date")
            if on_date is None:
                on_date = MarketData(config).latest_date()

            from .data.market_breadth import refresh_market_breadth

            result = refresh_market_breadth(config, on_date)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return (
                0
                if result.get("available") is True
                and result.get("status") == "current"
                and not result.get("errors")
                else 1
            )
        if args.command == "capital-flow-refresh":
            on_date = _parse_cli_iso_date(args.date, "date")
            if on_date is None:
                on_date = MarketData(config).latest_date()

            from .data.capital_flow import refresh_capital_flow

            result = refresh_capital_flow(config, on_date)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return (
                0
                if result.get("available") is True
                and result.get("status") == "current"
                and not result.get("errors")
                else 1
            )
        if args.command == "intraday-refresh":
            from .data.intraday import refresh_intraday

            on_date = _parse_cli_iso_date(args.date, "date")
            selected = args.symbols or [item.symbol for item in config.instruments]
            results = []
            for symbol in selected:
                results.append(
                    refresh_intraday(
                        config,
                        symbol,
                        trade_date=on_date,
                        interval=args.interval,
                        limit=args.limit,
                    )
                )
            print(json.dumps({"snapshots": results}, ensure_ascii=False, indent=2, default=str))
            return 0 if all(item.get("available") is True for item in results) else 1
        if args.command == "valuation-refresh":
            from .data.valuation import refresh_valuation

            result = refresh_valuation(config, symbols=args.symbols)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result.get("available") is True and not (
                result.get("errors") and not result.get("records")
            ) else 1
        if args.command == "fundamentals-refresh":
            from .data.fundamentals import refresh_fundamentals

            result = refresh_fundamentals(
                config,
                symbols=args.symbols,
                periods_per_symbol=args.periods,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result.get("available") is True else 1
        if args.command == "disclosures-refresh":
            from .data.disclosures import refresh_disclosures

            result = refresh_disclosures(
                config,
                symbols=args.symbols,
                lookback_days=args.lookback_days,
                limit_per_symbol=args.limit_per_symbol,
                hash_documents=not args.skip_document_hash,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result.get("available") is True else 1
        if args.command == "order-book-refresh":
            from .data.order_book import refresh_order_book

            result = refresh_order_book(config, symbols=args.symbols)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result.get("available") is True else 1
        if args.command == "news-refresh":
            from .data.news import refresh_news

            on_date = _parse_cli_iso_date(args.date, "date")
            result = refresh_news(
                config,
                trade_date=on_date,
                symbols=args.symbols,
                limit_per_source=args.limit_per_source,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result.get("available") is True else 1
        if args.command == "backtest":
            _ensure_cache(config)
            market = MarketData(config)
            result = BacktestEngine(config, market).run(
                date.fromisoformat(args.start) if args.start else None,
                date.fromisoformat(args.end) if args.end else None,
            )
            paths = save_backtest_report(result, config.reports_dir)
            print(_backtest_console(result, paths))
            return 0
        if args.command == "walk-forward":
            _ensure_cache(config)
            market = MarketData(config)
            result = run_walk_forward(config, market, args.train_days, args.test_days)
            paths = save_walk_forward(result, config.reports_dir)
            print(
                json.dumps(
                    {
                        "aggregate": result["aggregate"],
                        "files": [str(path) for path in paths],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "validate":
            _ensure_cache(config)
            market = MarketData(config)
            result = run_robustness_validation(
                config,
                market,
                bootstrap_samples=args.bootstrap_samples,
                block_days=args.block_days,
            )
            paths = save_validation_report(result, config.reports_dir)
            print(
                json.dumps(
                    {
                        "research_gates": result["research_gates"],
                        "bootstrap": result["bootstrap"],
                        "files": [str(path) for path in paths],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "signal":
            if args.refresh:
                download_universe(config, force=True)
                _maybe_automatic_cloud_backup(config)
            else:
                _ensure_cache(config)
            market = MarketData(config)
            signal = MomentumTrendStrategy(config.strategy).generate(
                market, market.latest_date()
            )
            print(json.dumps(_signal_payload(signal), ensure_ascii=False, indent=2))
            return 0
        if args.command == "assistant-analyze":
            _ensure_cache(config)
            result = AssistantEngine(config).analyze(
                MarketData(config),
                args.symbol,
                lookback=args.lookback,
                mode=args.mode,
                user_id="local-owner",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "hypothesis-generate":
            market = MarketData(config, recover_snapshot=False)
            result = HypothesisLabEngine(config).generate_local(
                "local-owner",
                market,
                objective=args.objective,
                title=args.title,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "hypothesis-list":
            result = HypothesisLabEngine(config).list(
                "local-owner", limit=args.limit
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "hypothesis-show":
            result = HypothesisLabEngine(config).get(
                "local-owner", args.hypothesis_id
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "hypothesis-run":
            market = MarketData(config, recover_snapshot=False)
            result = HypothesisExperimentRunner(config).execute(
                "local-owner", args.hypothesis_id, market
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "hypothesis-from-model":
            market = MarketData(config, recover_snapshot=False)
            result = HypothesisLabEngine(config).derive_from_model(
                "local-owner",
                market,
                args.evaluation_id,
                title=args.title,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "hypothesis-runs":
            result = HypothesisExperimentRunner(config).list_runs(
                "local-owner", limit=args.limit, hypothesis_id=args.hypothesis
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "hypothesis-run-show":
            result = HypothesisExperimentRunner(config).get_run(
                "local-owner", args.run_id
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "factor-list":
            registry = FactorLabEngine(config).registry()
            registry["custom_factors"] = CustomFactorStore(config).list(
                "local-owner"
            )
            print(json.dumps(registry, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "factor-define":
            result = CustomFactorStore(config).define(
                "local-owner",
                args.name,
                args.expression,
                args.direction,
                label=args.label,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "factor-evaluate":
            market = MarketData(config, recover_snapshot=False)
            result = FactorLabEngine(config).evaluate(
                "local-owner",
                market,
                args.factor,
                horizons=_parse_horizons(args.horizons),
                step=args.step,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "factor-evaluations":
            result = FactorLabEngine(config).list(
                "local-owner", limit=args.limit, factor_id=args.factor
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "factor-show":
            result = FactorLabEngine(config).get(
                "local-owner", args.evaluation_id
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "model-list":
            print(
                json.dumps(
                    ModelLabEngine(config).registry(),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.command == "model-evaluate":
            market = MarketData(config, recover_snapshot=False)
            factor_ids = (
                None
                if args.factors is None
                else [part.strip() for part in str(args.factors).split(",") if part.strip()]
            )
            result = ModelLabEngine(config).evaluate(
                "local-owner",
                market,
                args.model,
                factor_ids=factor_ids,
                horizon=args.horizon,
                step=args.step,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "model-evaluations":
            result = ModelLabEngine(config).list(
                "local-owner", limit=args.limit, model_id=args.model
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "model-show":
            result = ModelLabEngine(config).get(
                "local-owner", args.evaluation_id
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "research-loop-run":
            from .research_loop import (
                ModelResearchPlanner,
                ResearchLoopEngine,
                StaticResearchPlanner,
            )

            if args.mode == "local":
                if args.plan_file is None:
                    raise ValueError(
                        "research-loop-run local mode requires --plan-file"
                    )
                planner = StaticResearchPlanner.from_file(args.plan_file)
            else:
                if args.plan_file is not None:
                    raise ValueError(
                        "research-loop-run model mode does not accept --plan-file"
                    )
                planner = ModelResearchPlanner(config, "local-owner")
            market = MarketData(config, recover_snapshot=False)
            result = ResearchLoopEngine(config).run(
                "local-owner",
                market,
                planner,
                max_rounds=args.max_rounds,
                max_tool_units=args.max_tool_units,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "research-loop-list":
            from .research_loop import ResearchLoopStore

            result = ResearchLoopStore(
                config.project_root / "state" / "research_loop"
            ).list("local-owner", limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "research-loop-show":
            from .research_loop import ResearchLoopStore

            result = ResearchLoopStore(
                config.project_root / "state" / "research_loop"
            ).get("local-owner", args.loop_id)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "feature-build":
            from .pipeline_cli import build_feature_snapshot

            factor_ids = (
                None
                if args.factors is None
                else [
                    part.strip()
                    for part in str(args.factors).split(",")
                    if part.strip()
                ]
            )
            if factor_ids is not None and (
                not factor_ids or len(factor_ids) != len(set(factor_ids))
            ):
                raise ValueError("feature-build --factors must contain unique ids")
            result = build_feature_snapshot(
                config,
                as_of_session=_parse_cli_iso_date(args.as_of, "--as-of"),
                live_capture=bool(args.live_capture),
                factor_ids=factor_ids,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "feature-forward-run":
            from .pipeline_cli import run_forward_evidence

            factor_ids = (
                None
                if args.factors is None
                else [
                    part.strip()
                    for part in str(args.factors).split(",")
                    if part.strip()
                ]
            )
            if factor_ids is not None and (
                not factor_ids or len(factor_ids) != len(set(factor_ids))
            ):
                raise ValueError(
                    "feature-forward-run --factors must contain unique ids"
                )
            result = run_forward_evidence(
                config,
                factor_ids=factor_ids,
                horizons=_parse_horizons(args.horizons),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "feature-show":
            from .pipeline_cli import show_feature_snapshot

            result = show_feature_snapshot(
                config,
                args.snapshot_id,
                on_date=_required_cli_date(args.date, "--date"),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "feature-label":
            from .pipeline_cli import build_label_snapshot

            result = build_label_snapshot(
                config,
                args.snapshot_id,
                on_date=_required_cli_date(args.date, "--date"),
                horizon=args.horizon,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "model-artifact-fit":
            from .pipeline_cli import fit_model_artifact

            result = fit_model_artifact(
                config,
                args.evaluation_id,
                training_cutoff=_parse_cli_timestamp(
                    args.training_cutoff,
                    "--training-cutoff",
                ),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "model-predict":
            from .pipeline_cli import create_prediction_snapshot

            result = create_prediction_snapshot(
                config,
                args.artifact_id,
                args.feature_snapshot_id,
                feature_date=_required_cli_date(
                    args.feature_date,
                    "--feature-date",
                ),
                valid_from_session=_required_cli_date(
                    args.valid_from,
                    "--valid-from",
                ),
                valid_until_session=_required_cli_date(
                    args.valid_until,
                    "--valid-until",
                ),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "portfolio-plan":
            from .pipeline_cli import create_portfolio_plan

            result = create_portfolio_plan(
                config,
                args.prediction_id,
                feature_date=_required_cli_date(
                    args.feature_date,
                    "--feature-date",
                ),
                equity=args.equity,
                execution_session=_required_cli_date(
                    args.execution_date,
                    "--execution-date",
                ),
                decision_time=(
                    datetime.now(timezone.utc)
                    if args.decision_time is None
                    else _parse_cli_timestamp(args.decision_time, "--decision-time")
                ),
                current_weights_file=args.current_weights_file,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "parameter-sweep":
            market = MarketData(config, recover_snapshot=False)
            selected = (
                None
                if args.parameters is None
                else [
                    part.strip()
                    for part in str(args.parameters).split(",")
                    if part.strip()
                ]
            )
            result = ParameterSweepEngine(config).execute(
                "local-owner",
                market,
                objective=args.objective,
                parameters=selected,
                points=args.points,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "parameter-sweeps":
            result = ParameterSweepEngine(config).list(
                "local-owner", limit=args.limit
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "parameter-sweep-show":
            result = ParameterSweepEngine(config).get("local-owner", args.sweep_id)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "nested-walk-forward":
            market = MarketData(config, recover_snapshot=False)
            selected = (
                None
                if args.parameters is None
                else [
                    part.strip()
                    for part in str(args.parameters).split(",")
                    if part.strip()
                ]
            )
            result = NestedWalkForwardEngine(config).execute(
                "local-owner",
                market,
                objective=args.objective,
                parameters=selected,
                points=args.points,
                outer_folds=args.outer_folds,
                inner_folds=args.inner_folds,
                embargo_sessions=args.embargo,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "nested-walk-forwards":
            result = NestedWalkForwardEngine(config).list(
                "local-owner", limit=args.limit
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "nested-walk-forward-show":
            result = NestedWalkForwardEngine(config).get(
                "local-owner", args.nested_id
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "sentiment-compose":
            from .data.sentiment import SentimentTiltEngine

            target = date.fromisoformat(args.date) if args.date else None
            result = SentimentTiltEngine(config).compose(target)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "sentiment-show":
            from .data.sentiment import SentimentTiltEngine

            target = date.fromisoformat(args.date) if args.date else None
            result = SentimentTiltEngine(config).latest(target)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "sentiment-list":
            from .data.sentiment import SentimentTiltEngine

            result = SentimentTiltEngine(config).list(limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "sandbox-cycle":
            from .broker.sandbox import SandboxCycleEngine

            result = SandboxCycleEngine(config).cycle(
                args.symbol,
                side=args.side,
                quantity=args.quantity,
                session=date.fromisoformat(args.date) if args.date else None,
                limit_price=args.limit_price,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "sandbox-status":
            from .broker.sandbox import SandboxCycleEngine

            result = SandboxCycleEngine(config).status()
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "sandbox-drills":
            from .broker.sandbox import SandboxCycleEngine

            result = SandboxCycleEngine(config).list_drills(limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "shadow-event-append":
            from .pipeline_cli import append_shadow_event

            result = append_shadow_event(
                config,
                args.account_reference,
                args.event_type,
                occurred_at=_parse_cli_timestamp(
                    args.occurred_at,
                    "--occurred-at",
                ),
                trading_session=_required_cli_date(
                    args.trading_session,
                    "--trading-session",
                ),
                source=args.source,
                external_id=args.external_id,
                payload_file=args.payload_file,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "shadow-project":
            from .pipeline_cli import shadow_projection

            result = shadow_projection(config, args.account_reference)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "shadow-reconcile":
            from .pipeline_cli import shadow_reconciliation

            result = shadow_reconciliation(
                config,
                args.account_reference,
                broker_snapshot_file=args.broker_snapshot_file,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "universe-verify":
            result = _universe_verify(config)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result["summary"]["dangerous_issues"] == 0 else 1
        if args.command == "research-report":
            result = write_research_report(config, output=args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "hypothesis-materialize":
            if not args.yes:
                raise ValueError(
                    "hypothesis-materialize requires --yes human confirmation"
                )
            result = HypothesisLabEngine(config).materialize_candidate(
                "local-owner",
                args.hypothesis_id,
                confirmed_by="local-cli-user",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "monitor-scan":
            refresh_warning = None
            if args.no_refresh:
                try:
                    _ensure_cache(config)
                except (OSError, RuntimeError, ValueError) as exc:
                    refresh_warning = str(exc)
            else:
                try:
                    download_universe(config, force=False)
                    _maybe_automatic_cloud_backup(config)
                except (OSError, RuntimeError, ValueError) as exc:
                    # A verified local cache may still support a scan. If it
                    # cannot be opened, the engine records a failed ScanRun.
                    refresh_warning = str(exc)
            try:
                market = MarketData(config, recover_snapshot=False)
            except (OSError, RuntimeError, ValueError):
                market = None
            engine = MonitoringEngine(config)
            if args.all_profiles:
                allowed_profiles, profile_warning = _monitor_allowed_profiles(config, engine)
                scans = engine.scan_all_profiles(
                    actor="scheduled-monitor",
                    market=market,
                    allowed_profile_ids=allowed_profiles,
                )
                result = {
                    "schema_version": 1,
                    "scope": "all_profiles",
                    "profiles_scanned": len(scans),
                    "status_counts": _monitor_status_counts(scans),
                    "triggered_alerts": sum(
                        len(item.get("triggered_alert_ids", [])) for item in scans
                    ),
                    "scans": scans,
                    "refresh_warning": refresh_warning,
                    "profile_warning": profile_warning,
                    "authority": {
                        "research_only": True,
                        "execution_authorized": False,
                    },
                }
            else:
                owner = "local-owner"
                actor = "local-owner"
                if args.username:
                    from .web.auth import UserStore, normalize_username

                    actor = normalize_username(args.username)
                    account_id = UserStore(
                        config.auth_users_file
                    ).enabled_account_id_for(actor)
                    if account_id is None:
                        raise ValueError("Beta user does not exist or is disabled")
                    owner = account_id
                scan = engine.scan(owner, actor=actor, market=market)
                result = {
                    "schema_version": 1,
                    "scope": "beta_user" if args.username else "local_owner",
                    "profiles_scanned": 1,
                    "status_counts": _monitor_status_counts([scan]),
                    "triggered_alerts": len(scan.get("triggered_alert_ids", [])),
                    "scans": [scan],
                    "refresh_warning": refresh_warning,
                    "authority": {
                        "research_only": True,
                        "execution_authorized": False,
                    },
                }
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 1 if (
                any(item.get("status") == "failed" for item in result["scans"])
                or bool(result.get("profile_warning"))
            ) else 0
        if args.command == "archive-generate":
            from .research_digest import ResearchDigestCapacityError
            from .web.service import DashboardService

            on_date = _parse_cli_iso_date(args.date, "date")
            week_start = _parse_cli_iso_date(args.week, "week")
            if week_start is not None and week_start.weekday() != 0:
                raise ValueError("week must be an ISO Monday")
            if args.kind == "daily" and week_start is not None:
                raise ValueError("daily generation accepts date, not week")
            if args.kind == "weekly" and on_date is not None:
                raise ValueError("weekly generation accepts week, not date")
            service = DashboardService(config)
            scheduled_actor = (
                "scheduled-archive" if args.trigger == "scheduled" else None
            )
            profile_warning = None
            requests: list[tuple[str, str]] = [
                ("local-owner", scheduled_actor or "local-owner")
            ]
            if args.username:
                from .web.auth import UserStore, normalize_username

                actor = normalize_username(args.username)
                account_id = UserStore(
                    config.auth_users_file
                ).enabled_account_id_for(actor)
                if account_id is None:
                    raise ValueError("Beta user does not exist or is disabled")
                requests = [(account_id, scheduled_actor or actor)]
            elif args.all_profiles:
                from .web.auth import UserStore

                batch_actor = scheduled_actor or "archive-cli"
                requests = [("local-owner", batch_actor)]
                try:
                    requests.extend(
                        (account_id, batch_actor)
                        for account_id in UserStore(
                            config.auth_users_file
                        ).enabled_account_ids()
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    profile_warning = str(exc)
                    logging.getLogger(__name__).warning(
                        "Could not enumerate enabled archive profiles: %s", exc
                    )
            writes: list[dict[str, object]] = []
            for owner, actor in requests:
                try:
                    item = service.generate_research_digests(
                        owner_id=owner,
                        actor=actor,
                        trigger=args.trigger,
                        kind=args.kind,
                        on_date=on_date,
                        week_start=week_start,
                    )
                except (
                    ResearchDigestCapacityError,
                    OSError,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    if not args.all_profiles:
                        raise
                    logging.getLogger(__name__).warning(
                        "Research archive profile generation failed (%s): %s",
                        type(exc).__name__,
                        exc,
                    )
                    writes.append(
                        {
                            "scope": (
                                "local_owner"
                                if owner == "local-owner"
                                else "beta_user"
                            ),
                            "status": "unavailable",
                            "available": False,
                            "summary": {},
                            "errors": [
                                {
                                    "code": (
                                        "research_digest_capacity"
                                        if isinstance(
                                            exc, ResearchDigestCapacityError
                                        )
                                        else "research_digest_generation_failed"
                                    ),
                                    "message": str(exc),
                                }
                            ],
                        }
                    )
                    continue
                writes.append(
                    {
                        "scope": "local_owner" if owner == "local-owner" else "beta_user",
                        "status": item.get("status"),
                        "available": item.get("available", False),
                        "summary": item.get("summary", {}),
                        "errors": item.get("errors", []),
                    }
                )
            result = {
                "schema_version": 1,
                "scope": "all_profiles" if args.all_profiles else writes[0]["scope"],
                "trigger": args.trigger,
                "profiles_processed": len(writes),
                "profiles": writes,
                "profile_warning": profile_warning,
                "authority": {
                    "research_only": True,
                    "execution_authorized": False,
                },
            }
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return (
                0
                if all(
                    item["available"]
                    and item["status"]
                    in {"current", "provisional", "partial", "empty"}
                    and not item["errors"]
                    for item in writes
                )
                and not profile_warning
                else 1
            )
        if args.command == "paper-init":
            state = initialize_paper(config, args.cash, args.overwrite)
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        if args.command == "paper-run":
            if not args.no_refresh:
                download_universe(config, force=True)
            else:
                _ensure_cache(config)
            report = run_paper(config, MarketData(config))
            _maybe_automatic_cloud_backup(config)
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "paper-status":
            print(json.dumps(paper_status(config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "paper-audit":
            _ensure_cache(config)
            report = audit_paper(config, MarketData(config))
            paths = save_paper_audit(report, config.reports_dir)
            print(
                json.dumps(
                    report | {"files": [str(path) for path in paths]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if not report["integrity_errors"] else 1
        if args.command == "doctor":
            _ensure_cache(config)
            market = MarketData(config)
            diagnosis = diagnose(config, market)
            print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
            return 0 if diagnosis["status"] == "OK" else 1
        if args.command == "broker-list":
            print(
                json.dumps(
                    available_broker_adapters(), ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.command == "broker-probe":
            print(
                json.dumps(
                    probe_configured_broker(config), ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.command == "broker-compare":
            print(
                json.dumps(
                    compare_configured_broker(config), ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.command == "live-check":
            require_live_confirmation()
            raise BrokerNotConfigured(
                "No live-capable broker adapter is configured; read-only adapters "
                "cannot unlock live trading."
            )
        if args.command == "serve":
            from .web.server import serve_dashboard

            serve_dashboard(
                config,
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
                auth_enabled=False if args.owner_local else None,
                allow_container_bind=args.container_bind,
            )
            return 0
    except Exception as exc:
        if getattr(args, "command", "").startswith("cloud-"):
            message = _safe_cloud_error(exc)
            logging.getLogger(__name__).error("Cloud command failed: %s", message)
            print(f"ERROR: {message}", file=sys.stderr)
        else:
            logging.getLogger(__name__).exception("Command failed")
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _monitor_status_counts(scans: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for scan in scans:
        status = str(scan.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _parse_cli_iso_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD format") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use YYYY-MM-DD format")
    return parsed


def _required_cli_date(value: str, field: str) -> date:
    parsed = _parse_cli_iso_date(value, field)
    if parsed is None:
        raise ValueError(f"{field} is required")
    return parsed


def _parse_cli_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must use ISO-8601 format") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _monitor_allowed_profiles(
    config: AppConfig, engine: MonitoringEngine
) -> tuple[set[str], str | None]:
    """Limit scheduled sweeps to the local owner and enabled beta accounts."""
    allowed = {engine.store.owner_id("local-owner")}
    warning = None
    try:
        from .web.auth import UserStore

        users = UserStore(config.auth_users_file)
        for account_id in users.enabled_account_ids():
            allowed.add(engine.store.owner_id(account_id))
    except (OSError, RuntimeError, ValueError) as exc:
        # A missing auth store must not prevent the local owner sweep.
        warning = str(exc)
    return allowed, warning


def _maybe_automatic_cloud_backup(config: AppConfig) -> None:
    try:
        from .cloud import (
            automatic_cloud_backup_enabled,
            backup_market_cache,
            load_cloud_settings,
            tracked_r2_store,
        )

        if not automatic_cloud_backup_enabled(config):
            return
        settings = load_cloud_settings()
        result = backup_market_cache(config, tracked_r2_store(config, settings))
        logging.getLogger(__name__).info(
            "Automatic cloud snapshot %s (%s)",
            result.get("snapshot_id", "completed"),
            "deduplicated" if result.get("skipped_duplicate") else "uploaded",
        )
        _emit_cloud_backup_event("succeeded")
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Automatic cloud backup failed; local data remains valid: %s",
            _safe_cloud_error(exc),
        )
        _emit_cloud_backup_event("failed")


def _emit_cloud_backup_event(status: str) -> None:
    if os.environ.get(_WEB_JOB_PROTOCOL_ENV) != "1":
        return
    if status not in {"succeeded", "failed"}:
        raise ValueError("Cloud backup event status is invalid")
    payload = json.dumps(
        {"schema_version": 1, "status": status},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    print(f"{_CLOUD_BACKUP_EVENT_PREFIX}{payload}", file=sys.stderr, flush=True)


def _universe_verify(config: AppConfig) -> dict:
    """Bind configured master-data dates to the cached provider evidence.

    The first cached bar of a symbol is the authoritative first completed
    session from the configured provider. A membership window that starts
    before that bar is the dangerous direction (the instrument would appear
    in the historical universe before any data exists) and is reported as an
    issue; everything else is disclosure. The check is read-only.
    """
    members: list[dict] = []
    dangerous = 0
    missing = 0
    memberships: dict[str, list] = {}
    for name, values in config.security_master.universes.items():
        for membership in values:
            memberships.setdefault(membership.symbol, []).append(
                (name, membership.start, membership.end)
            )
    for instrument in config.instruments:
        symbol = instrument.symbol
        row: dict = {
            "symbol": symbol,
            "name": instrument.name,
            "listing_date": (
                instrument.listing_date.isoformat()
                if instrument.listing_date
                else None
            ),
            "memberships": [
                {
                    "universe": name,
                    "start": start.isoformat(),
                    "end": end.isoformat() if end else None,
                }
                for name, start, end in memberships.get(symbol, [])
            ],
            "first_bar": None,
            "last_bar": None,
            "status": "ok",
            "notes": [],
        }
        path = config.cache_dir / f"{symbol}.csv"
        if not path.is_file():
            row["status"] = "missing_cache"
            row["notes"].append("尚无本地缓存；先运行 download")
            missing += 1
            members.append(row)
            continue
        try:
            bars = load_cached_bars(path)
        except (OSError, ValueError, RuntimeError) as exc:
            row["status"] = "invalid_cache"
            row["notes"].append(f"缓存无法读取: {str(exc)[:160]}")
            dangerous += 1
            members.append(row)
            continue
        if not bars:
            row["status"] = "invalid_cache"
            row["notes"].append("缓存为空")
            dangerous += 1
            members.append(row)
            continue
        first = bars[0].date
        row["first_bar"] = first.isoformat()
        row["last_bar"] = bars[-1].date.isoformat()
        window_start = date.fromisoformat(str(config.raw["data"]["start"]))
        tolerance = timedelta(days=10)
        for name, start, _end in memberships.get(symbol, []):
            expected = max(start, window_start)
            if first > expected + tolerance:
                gap = (first - expected).days
                row["status"] = "membership_before_first_bar"
                row["notes"].append(
                    f"{name} 成分自 {start.isoformat()} 起（数据窗口起点 "
                    f"{window_start.isoformat()}），但首根缓存 K 线是 "
                    f"{first.isoformat()}（晚 {gap} 天）；应把成分起始日推迟到"
                    "不早于首根 K 线"
                )
                dangerous += 1
            elif start < window_start:
                row["notes"].append(
                    f"{name} 成分起始 {start.isoformat()} 早于数据窗口起点，"
                    "历史仅自窗口起点可用（仅披露）"
                )
        listing = instrument.listing_date
        if listing is not None and row["status"] == "ok":
            if listing > first:
                row["notes"].append(
                    "配置上市日晚于首根缓存 K 线；实际上市更早（保守方向，仅披露）"
                )
            elif first > listing and listing >= window_start:
                row["notes"].append(
                    f"提供方历史自 {first.isoformat()} 开始，晚于配置上市日 "
                    f"{(first - listing).days} 天（下界口径或提供方截断，仅披露）"
                )
        members.append(row)
    return {
        "schema_version": 1,
        "master_as_of": str(config.security_master.metadata.get("as_of") or ""),
        "summary": {
            "instruments": len(members),
            "verified": len(members) - missing,
            "missing_cache": missing,
            "dangerous_issues": dangerous,
        },
        "members": members,
        "safety": {
            "research_only": True,
            "read_only": True,
            "strategy_changed": False,
            "orders_created": False,
        },
    }


def _parse_horizons(value: str) -> tuple[int, ...]:
    try:
        items = tuple(int(part.strip()) for part in str(value).split(",") if part.strip())
    except ValueError as exc:
        raise ValueError(
            "--horizons must be comma-separated integers, e.g. 5,20,60"
        ) from exc
    if not items:
        raise ValueError("--horizons must contain at least one horizon")
    return items


def _ensure_cache(config: AppConfig) -> None:
    missing = [
        item.symbol
        for item in config.instruments
        if not (config.cache_dir / f"{item.symbol}.csv").exists()
    ]
    if missing:
        download_universe(config, force=False)


def _resolve_config_path(value: str) -> Path:
    requested = Path(value)
    if requested.exists() or requested.is_absolute() or value != "config/default.json":
        return requested
    project_default = Path(__file__).resolve().parents[2] / "config" / "default.json"
    return project_default if project_default.exists() else requested


def _initialize_workspace(directory: Path) -> int:
    root = directory.expanduser().resolve()
    config_path = root / "config" / "default.json"
    if config_path.exists():
        raise FileExistsError(f"Workspace configuration already exists: {config_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    resource = importlib.resources.files("ai_trade").joinpath("default_config.json")
    config_path.write_text(resource.read_text(encoding="utf-8"), encoding="utf-8")
    master_resource = importlib.resources.files("ai_trade").joinpath(
        "default_security_master.json"
    )
    (root / "config" / "security_master.json").write_text(
        master_resource.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for relative in ("data/cache", "reports", "state", "logs"):
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / ".gitkeep").touch(exist_ok=True)
    print(json.dumps({"workspace": str(root), "config": str(config_path)}, indent=2))
    return 0


def _configure_logging(config: AppConfig) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    # force=True closes and detaches any handlers from a previous invocation
    # in the same process, so repeated CLI calls (and the test suite) bind the
    # log file to the *current* configuration and never leak open handles —
    # on Windows an open ai_trade.log would block temporary-directory cleanup.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(config.logs_dir / "ai_trade.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )


def _safe_cloud_error(exc: Exception) -> str:
    from .cloud import safe_cloud_error

    return safe_cloud_error(exc)


def _backtest_console(result, paths: dict[str, Path]) -> str:
    payload = {
        "period": [result.metadata["start"], result.metadata["end"]],
        "strategy": result.metrics,
        "benchmark": result.benchmark_metrics,
        "latest_signal": _signal_payload(result.latest_signal)
        if result.latest_signal
        else None,
        "report": str(paths["html"]),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _signal_payload(signal) -> dict[str, object]:
    return {
        "date": signal.date.isoformat(),
        "target_weights": signal.target_weights,
        "reason": signal.reason,
        "diagnostics": signal.diagnostics,
        "ranking": [item.__dict__ for item in signal.ranked],
    }


if __name__ == "__main__":
    raise SystemExit(main())
