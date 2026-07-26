from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import date
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ai_trade.cli import build_parser, main


class SentimentCliTests(TestCase):
    def test_parser_exposes_compose_show_and_list_commands(self):
        composed = build_parser().parse_args(
            ["sentiment-compose", "--date", "2026-07-24"]
        )
        self.assertEqual(composed.date, "2026-07-24")
        shown = build_parser().parse_args(["sentiment-show"])
        self.assertIsNone(shown.date)
        listed = build_parser().parse_args(["sentiment-list", "--limit", "9"])
        self.assertEqual(listed.limit, 9)

    def test_compose_dispatches_without_market_data_or_provider_refresh(self):
        config = object()
        engine = MagicMock()
        engine.compose.return_value = {"tilt_label": "NEUTRAL", "revision": 1}
        engine.latest.return_value = {"tilt_label": "NEUTRAL"}
        engine.list.return_value = {"summary": {"dates": 0}}
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.data.sentiment.SentimentTiltEngine",
                return_value=engine,
            ),
            patch("ai_trade.cli.MarketData") as market_data,
            patch("ai_trade.cli._ensure_cache") as ensure_cache,
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(["sentiment-compose", "--date", "2026-07-24"]), 0
            )
            self.assertEqual(main(["sentiment-show"]), 0)
            self.assertEqual(main(["sentiment-list", "--limit", "5"]), 0)

        market_data.assert_not_called()
        ensure_cache.assert_not_called()
        engine.compose.assert_called_once_with(date(2026, 7, 24))
        engine.latest.assert_called_once_with(None)
        engine.list.assert_called_once_with(limit=5)
        self.assertIn("NEUTRAL", output.getvalue())


class FactorDefineCliTests(TestCase):
    def test_parser_requires_name_and_expression(self):
        parsed = build_parser().parse_args(
            [
                "factor-define",
                "--name",
                "gap_rev",
                "--expression",
                "delay(close,1)/open-1",
                "--direction",
                "-1",
            ]
        )
        self.assertEqual(parsed.name, "gap_rev")
        self.assertEqual(parsed.direction, -1)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["factor-define", "--name", "x"])

    def test_define_dispatches_to_the_custom_store(self):
        config = object()
        store = MagicMock()
        store.define.return_value = {"name": "gap_rev", "reused": False}
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.CustomFactorStore", return_value=store),
            patch("ai_trade.cli.MarketData") as market_data,
            redirect_stdout(io.StringIO()),
        ):
            status = main(
                [
                    "factor-define",
                    "--name",
                    "gap_rev",
                    "--expression",
                    "delay(close,1)/open-1",
                    "--direction",
                    "-1",
                    "--label",
                    "隔夜反转",
                ]
            )
        self.assertEqual(status, 0)
        market_data.assert_not_called()
        store.define.assert_called_once_with(
            "local-owner",
            "gap_rev",
            "delay(close,1)/open-1",
            -1,
            label="隔夜反转",
        )


class SandboxCliTests(TestCase):
    def test_parser_exposes_cycle_status_and_drills_commands(self):
        cycled = build_parser().parse_args(
            [
                "sandbox-cycle",
                "--symbol",
                "510300",
                "--side",
                "SELL",
                "--quantity",
                "200",
                "--date",
                "2026-07-24",
                "--limit-price",
                "3.45",
            ]
        )
        self.assertEqual(cycled.symbol, "510300")
        self.assertEqual(cycled.side, "SELL")
        self.assertEqual(cycled.quantity, 200)
        self.assertEqual(cycled.limit_price, 3.45)
        build_parser().parse_args(["sandbox-status"])
        listed = build_parser().parse_args(["sandbox-drills", "--limit", "3"])
        self.assertEqual(listed.limit, 3)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["sandbox-cycle"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["sandbox-cycle", "--symbol", "510300", "--side", "HOLD"]
            )

    def test_cycle_dispatches_into_the_isolated_sandbox_engine(self):
        config = object()
        engine = MagicMock()
        engine.cycle.return_value = {"outcome": {"status": "FILLED"}}
        engine.status.return_value = {"lifecycle": {"status": "EMPTY"}}
        engine.list_drills.return_value = {"summary": {"total": 0}}
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.broker.sandbox.SandboxCycleEngine",
                return_value=engine,
            ),
            patch("ai_trade.cli.MarketData") as market_data,
            patch("ai_trade.cli._ensure_cache") as ensure_cache,
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "sandbox-cycle",
                        "--symbol",
                        "510300",
                        "--date",
                        "2026-07-24",
                    ]
                ),
                0,
            )
            self.assertEqual(main(["sandbox-status"]), 0)
            self.assertEqual(main(["sandbox-drills", "--limit", "7"]), 0)

        market_data.assert_not_called()
        ensure_cache.assert_not_called()
        engine.cycle.assert_called_once_with(
            "510300",
            side="BUY",
            quantity=None,
            session=date(2026, 7, 24),
            limit_price=None,
        )
        engine.status.assert_called_once_with()
        engine.list_drills.assert_called_once_with(limit=7)
        self.assertIn('"FILLED"', output.getvalue())


if __name__ == "__main__":
    import unittest

    unittest.main()
