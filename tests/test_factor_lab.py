from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ai_trade.cli import build_parser, main
from ai_trade.config import load_config
from ai_trade.factor_lab import (
    FactorLabEngine,
    FactorLabStore,
    factor_definition,
    factor_registry,
)
from ai_trade.factor_lab.engine import MINIMUM_EVALUATED_DATES
from ai_trade.factor_lab.schema import evaluation_record_fingerprint
from ai_trade.factor_lab.store import FactorLabCapacityError
from ai_trade.models import Bar


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _weekday_calendar(end: date, sessions: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < sessions:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


class _Market:
    """Synthetic point-in-time market with per-symbol geometric growth.

    Symbol growth rates are ordered, so momentum ranks and forward-return
    ranks are identical on every date: rank IC must be exactly +1.
    """

    def __init__(
        self,
        latest: date = date(2026, 7, 24),
        sessions: int = 420,
        rates: dict[str, float] | None = None,
        invert_after: date | None = None,
    ) -> None:
        self.calendar = _weekday_calendar(latest, sessions)
        self.rates = rates or {
            "510300": 0.0002,
            "510500": 0.0006,
            "159915": 0.0010,
            "513100": 0.0014,
            "518880": 0.0018,
            "511010": 0.0022,
        }
        self._bars: dict[str, dict[date, Bar]] = {}
        for symbol, rate in self.rates.items():
            price = 1.0
            bars: dict[date, Bar] = {}
            for index, on_date in enumerate(self.calendar):
                daily = rate
                if invert_after is not None and on_date > invert_after:
                    daily = -rate
                price *= 1.0 + daily
                bars[on_date] = Bar(
                    date=on_date,
                    open=price,
                    close=price,
                    high=price,
                    low=price,
                    volume=1_000_000.0,
                    amount=100_000_000.0 * (1.0 + rate * index),
                )
            self._bars[symbol] = bars

    def active_symbols(self, on_date: date) -> tuple[str, ...]:
        return tuple(sorted(self.rates))

    def bar(self, symbol: str, on_date: date) -> Bar | None:
        return self._bars[symbol].get(on_date)

    def history(self, symbol: str, on_date: date, count: int) -> list[Bar]:
        dates = [value for value in self.calendar if value <= on_date]
        return [self._bars[symbol][value] for value in dates[-count:]]

    def latest_date(self) -> date:
        return self.calendar[-1]

    def snapshot_metadata(self) -> dict[str, object]:
        return {
            "provider": "test-cache",
            "latest_common_session": self.calendar[-1].isoformat(),
            "latest_benchmark_session": self.calendar[-1].isoformat(),
            "manifest": {"snapshot_id": f"factor-{self.calendar[-1].isoformat()}"},
            "symbols": {
                symbol: {"last": self.calendar[-1].isoformat(), "sha256": "d" * 64}
                for symbol in sorted(self.rates)
            },
        }


class FactorLibraryTests(TestCase):
    def test_registry_exposes_versioned_directional_factors(self):
        registry = factor_registry()
        identifiers = [item["factor_id"] for item in registry["factors"]]
        self.assertIn("momentum_120_5", identifiers)
        self.assertIn("volatility_60", identifiers)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for item in registry["factors"]:
            self.assertIn(item["direction"], (-1, 1))
            self.assertGreaterEqual(item["minimum_history"], 2)
            self.assertTrue(item["formula"])
        self.assertTrue(registry["safety"]["research_only"])

    def test_momentum_factor_computes_the_documented_ratio(self):
        market = _Market()
        symbol = "511010"
        definition = factor_definition("momentum_60_5")
        history = market.history(symbol, market.latest_date(), 200)
        value = definition.compute(history)
        closes = [bar.close for bar in history]
        expected = closes[-6] / closes[-66] - 1.0
        self.assertAlmostEqual(value, expected)

    def test_unknown_factor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown factor"):
            factor_definition("does_not_exist")


class FactorLabEngineTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.store = FactorLabStore(root / "factor_lab")
        self.engine = FactorLabEngine(self.config, self.store)
        self.market = _Market()

    def test_perfectly_ordered_market_scores_rank_ic_one(self):
        record = self.engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(5, 20), step=5
        )

        self.assertFalse(record["reused"])
        self.assertEqual(record["factor"]["factor_id"], "momentum_60_5")
        self.assertEqual(
            [row["horizon"] for row in record["results"]], [5, 20]
        )
        for row in record["results"]:
            self.assertAlmostEqual(row["mean_ic"], 1.0)
            self.assertAlmostEqual(row["ic_std"], 0.0)
            self.assertAlmostEqual(row["positive_share"], 1.0)
            self.assertAlmostEqual(row["direction_hit_rate"], 1.0)
            self.assertGreater(row["mean_spread"], 0.0)
            self.assertGreaterEqual(row["dates"], MINIMUM_EVALUATED_DATES)
            validation = row["statistical_validation"]
            self.assertAlmostEqual(validation["effect_size"], 1.0)
            self.assertAlmostEqual(validation["ci_low"], 1.0)
            self.assertAlmostEqual(validation["ci_high"], 1.0)
            self.assertEqual(validation["p_value"], 0.001)
            self.assertLessEqual(validation["adjusted_p_value"], 0.05)
            self.assertTrue(validation["reject_null"])
            self.assertEqual(validation["family_size"], 2)
            self.assertEqual(validation["subperiods"], 3)
            self.assertEqual(validation["positive_subperiods"], 3)
            self.assertEqual(
                validation["block_size"],
                max(1, (row["horizon"] + 4) // 5),
            )
        coverage = record["coverage"]
        self.assertEqual(coverage["evaluated_dates"] + coverage["skipped_dates"], coverage["sampled_dates"])
        self.assertEqual(len(coverage["symbols"]), 6)
        self.assertEqual(
            record["safety"],
            {
                "research_only": True,
                "creates_no_signal": True,
                "may_create_candidate": False,
                "may_approve": False,
                "may_activate": False,
                "may_trade": False,
            },
        )

    def test_negative_direction_factor_reports_direction_hit_rate(self):
        record = self.engine.evaluate(
            "alice", self.market, "reversal_5", horizons=(5,), step=5
        )
        row = record["results"][0]
        # In a monotone market short-term winners keep winning, so the
        # registered reversal direction (-1) must score a zero hit rate.
        self.assertAlmostEqual(row["mean_ic"], 1.0)
        self.assertAlmostEqual(row["direction_hit_rate"], 0.0)
        self.assertAlmostEqual(
            row["direction_adjusted_mean_spread"], -row["mean_spread"]
        )
        validation = row["statistical_validation"]
        self.assertAlmostEqual(validation["effect_size"], -1.0)
        self.assertEqual(validation["p_value"], 1.0)
        self.assertEqual(validation["positive_subperiods"], 0)
        self.assertFalse(validation["reject_null"])

    def test_repeated_evaluation_is_idempotent(self):
        first = self.engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(5,), step=5
        )
        second = self.engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(5,), step=5
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.assertEqual(self.store.list("alice")["summary"]["total"], 1)
        self.assertEqual(self.store.list("bob")["evaluations"], [])

    def test_changed_parameters_produce_a_new_record(self):
        first = self.engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(5,), step=5
        )
        second = self.engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(20,), step=5
        )
        self.assertNotEqual(first["evaluation_id"], second["evaluation_id"])
        listing = self.store.list("alice", factor_id="momentum_60_5")
        self.assertEqual(listing["summary"]["total"], 2)

    def test_insufficient_history_fails_closed(self):
        short = _Market(sessions=80)
        with self.assertRaisesRegex(RuntimeError, "too short"):
            self.engine.evaluate(
                "alice", short, "momentum_120_5", horizons=(60,), step=5
            )
        self.assertEqual(self.store.list("alice")["summary"]["total"], 0)

    def test_invalid_horizons_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "horizons"):
            self.engine.evaluate(
                "alice", self.market, "momentum_60_5", horizons=(20, 5), step=5
            )
        with self.assertRaisesRegex(ValueError, "horizons"):
            self.engine.evaluate(
                "alice", self.market, "momentum_60_5", horizons=(), step=5
            )
        with self.assertRaisesRegex(ValueError, "step"):
            self.engine.evaluate(
                "alice", self.market, "momentum_60_5", horizons=(5,), step=0
            )

    def test_tampered_record_is_rejected_on_read(self):
        record = self.engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(5, 20), step=5
        )
        path = (
            self.store.owner_directory("alice")
            / "evaluations"
            / f"{record['evaluation_id']}.json"
        )
        original = json.loads(path.read_text(encoding="utf-8"))
        value = json.loads(json.dumps(original))
        value["results"][0]["statistical_validation"]["effect_size"] = 0.0
        value["record_fingerprint"] = evaluation_record_fingerprint(value)
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            RuntimeError, "Invalid factor evaluation record"
        ):
            self.store.get("alice", record["evaluation_id"])

        value = original
        validation = value["results"][0]["statistical_validation"]
        validation["adjusted_p_value"] = validation["p_value"]
        value["record_fingerprint"] = evaluation_record_fingerprint(value)
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Holm correction"):
            self.store.get("alice", record["evaluation_id"])

    def test_v1_evaluation_remains_readable(self):
        record = self.engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(5,), step=5
        )
        path = (
            self.store.owner_directory("alice")
            / "evaluations"
            / f"{record['evaluation_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["schema_version"] = 1
        value["engine_version"] = 1
        for result in value["results"]:
            result.pop("statistical_validation")
        value["record_fingerprint"] = evaluation_record_fingerprint(value)
        path.write_text(json.dumps(value), encoding="utf-8")

        stored = self.store.get("alice", record["evaluation_id"])
        self.assertEqual(stored["schema_version"], 1)
        self.assertNotIn("statistical_validation", stored["results"][0])

    def test_per_factor_capacity_is_enforced(self):
        self.engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(5,), step=5
        )
        with patch(
            "ai_trade.factor_lab.store.MAX_EVALUATIONS_PER_FACTOR", 1
        ):
            with self.assertRaisesRegex(
                FactorLabCapacityError, "capacity reached for this factor"
            ):
                self.engine.evaluate(
                    "alice", self.market, "momentum_60_5", horizons=(10,), step=5
                )


class FactorCliTests(TestCase):
    def test_parser_exposes_factor_commands(self):
        listed = build_parser().parse_args(["factor-list"])
        self.assertEqual(listed.command, "factor-list")
        evaluated = build_parser().parse_args(
            [
                "factor-evaluate",
                "--factor",
                "momentum_60_5",
                "--horizons",
                "5,20",
                "--step",
                "3",
            ]
        )
        self.assertEqual(evaluated.factor, "momentum_60_5")
        self.assertEqual(evaluated.horizons, "5,20")
        self.assertEqual(evaluated.step, 3)
        evaluations = build_parser().parse_args(
            ["factor-evaluations", "--factor", "momentum_60_5", "--limit", "7"]
        )
        self.assertEqual(evaluations.limit, 7)
        shown = build_parser().parse_args(["factor-show", "eval_" + "a" * 32])
        self.assertEqual(shown.evaluation_id, "eval_" + "a" * 32)

    def test_evaluate_reads_existing_cache_without_refreshing_provider(self):
        config = object()
        market = object()
        engine = MagicMock()
        engine.evaluate.return_value = {
            "evaluation_id": "eval_" + "a" * 32,
            "reused": False,
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData", return_value=market) as market_data,
            patch("ai_trade.cli.FactorLabEngine", return_value=engine),
            patch("ai_trade.cli._ensure_cache") as ensure_cache,
            redirect_stdout(output),
        ):
            status = main(
                [
                    "factor-evaluate",
                    "--factor",
                    "momentum_60_5",
                    "--horizons",
                    "5,20,60",
                ]
            )

        self.assertEqual(status, 0)
        market_data.assert_called_once_with(config, recover_snapshot=False)
        ensure_cache.assert_not_called()
        engine.evaluate.assert_called_once_with(
            "local-owner",
            market,
            "momentum_60_5",
            horizons=(5, 20, 60),
            step=5,
        )
        self.assertEqual(
            json.loads(output.getvalue())["evaluation_id"], "eval_" + "a" * 32
        )

    def test_listing_and_show_do_not_open_market_data(self):
        config = object()
        engine = MagicMock()
        engine.registry.return_value = {"factors": []}
        engine.list.return_value = {"evaluations": [], "summary": {"total": 0}}
        engine.get.return_value = {"evaluation_id": "eval_" + "b" * 32}
        custom = MagicMock()
        custom.list.return_value = []
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.FactorLabEngine", return_value=engine),
            patch("ai_trade.cli.CustomFactorStore", return_value=custom),
            patch("ai_trade.cli.MarketData") as market_data,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["factor-list"]), 0)
            self.assertEqual(
                main(
                    [
                        "factor-evaluations",
                        "--factor",
                        "momentum_60_5",
                        "--limit",
                        "9",
                    ]
                ),
                0,
            )
            self.assertEqual(main(["factor-show", "eval_" + "b" * 32]), 0)

        market_data.assert_not_called()
        engine.list.assert_called_once_with(
            "local-owner", limit=9, factor_id="momentum_60_5"
        )
        engine.get.assert_called_once_with("local-owner", "eval_" + "b" * 32)


if __name__ == "__main__":
    import unittest

    unittest.main()


class ExpressionFactorTests(TestCase):
    def setUp(self) -> None:
        from ai_trade.factor_lab import CustomFactorStore

        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "config").mkdir()
        import shutil

        for name in ("default.json", "security_master.json"):
            shutil.copy(
                REPOSITORY_ROOT / "config" / name, root / "config" / name
            )
        from ai_trade.config import load_config as _load

        self.config = _load(root / "config" / "default.json")
        self.custom = CustomFactorStore(self.config)
        self.market = _Market()

    def test_expression_math_matches_hand_computation(self):
        from ai_trade.factor_lab import compile_expression

        compiled = compile_expression("sma(close, 3) / delay(close, 2) - 1")
        history = self.market.history("511010", self.market.latest_date(), 10)
        closes = [bar.close for bar in history]
        expected = (sum(closes[-3:]) / 3) / closes[-3] - 1
        self.assertAlmostEqual(compiled.compute(history), expected)
        # sma(close,3) needs 3 bars and delay(close,2) needs 1+2 bars.
        self.assertEqual(compiled.minimum_history, 3)
        self.assertEqual(compiled.source, "sma(close,3)/delay(close,2)-1")

    def test_expression_guardrails_reject_unsafe_input(self):
        from ai_trade.factor_lab import ExpressionError, compile_expression

        for bad in (
            "__import__('os')",
            "close.__class__",
            "open(close)",
            "unknown_series + 1",
            "sma(close)",
            "sma(close, 0)",
            "sma(close, 9999)",
            "close + " * 60 + "1",
            "((((((((((((((close))))))))))))))",
        ):
            with self.assertRaises(ExpressionError):
                compile_expression(bad)

    def test_custom_definition_is_immutable_and_idempotent(self):
        first = self.custom.define(
            "alice", "my_momo", "delay(close,5)/delay(close,65)-1", 1
        )
        second = self.custom.define(
            "alice", "my_momo", "delay(close, 5) / delay(close, 65) - 1", 1
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["minimum_history"], 66)
        with self.assertRaisesRegex(ValueError, "不可变"):
            self.custom.define("alice", "my_momo", "ret(close, 10)", 1)
        with self.assertRaisesRegex(ValueError, "内置因子冲突"):
            self.custom.define("alice", "momentum_60_5", "ret(close, 5)", 1)
        self.assertEqual(self.custom.list("bob"), [])

    def test_custom_factor_evaluates_identically_to_builtin_equivalent(self):
        from ai_trade.factor_lab import FactorLabEngine, FactorLabStore

        self.custom.define(
            "alice", "my_momo", "delay(close,5)/delay(close,65)-1", 1
        )
        store = FactorLabStore(
            Path(self.temporary.name) / "state" / "factor_lab"
        )
        engine = FactorLabEngine(self.config, store)
        builtin = engine.evaluate(
            "alice", self.market, "momentum_60_5", horizons=(5,), step=5
        )
        custom = engine.evaluate(
            "alice", self.market, "my_momo", horizons=(5,), step=5
        )
        self.assertEqual(custom["factor"]["family"], "custom")
        self.assertAlmostEqual(
            custom["results"][0]["mean_ic"], builtin["results"][0]["mean_ic"]
        )

    def test_model_lab_accepts_custom_factors(self):
        from ai_trade.model_lab import ModelLabEngine, ModelLabStore

        self.custom.define(
            "alice", "my_momo", "delay(close,5)/delay(close,65)-1", 1
        )
        engine = ModelLabEngine(
            self.config,
            ModelLabStore(Path(self.temporary.name) / "state" / "model_lab"),
        )
        record = engine.evaluate(
            "alice",
            _Market(sessions=520),
            "factor_mean_v1",
            factor_ids=["my_momo", "momentum_120_5"],
            horizon=10,
            step=5,
        )
        bound = {item["factor_id"] for item in record["factors"]}
        self.assertEqual(bound, {"my_momo", "momentum_120_5"})
        self.assertGreater(record["results"]["model"]["mean_ic"], 0.9)
