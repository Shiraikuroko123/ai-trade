from __future__ import annotations

import io
import json
from unittest import TestCase
from unittest.mock import MagicMock, patch
from contextlib import redirect_stdout

from ai_trade.cli import build_parser, main


class HypothesisCliTests(TestCase):
    def test_parser_exposes_generation_listing_and_show_commands(self):
        generated = build_parser().parse_args(
            ["hypothesis-generate", "--objective", "drawdown", "--title", "Test"]
        )
        self.assertEqual(generated.objective, "drawdown")
        self.assertEqual(generated.title, "Test")
        listed = build_parser().parse_args(["hypothesis-list", "--limit", "7"])
        self.assertEqual(listed.limit, 7)
        shown = build_parser().parse_args(["hypothesis-show", "hyp_" + "a" * 32])
        self.assertEqual(shown.hypothesis_id, "hyp_" + "a" * 32)
        materialized = build_parser().parse_args(
            ["hypothesis-materialize", "hyp_" + "b" * 32, "--yes"]
        )
        self.assertTrue(materialized.yes)

    def test_generate_reads_existing_cache_without_refreshing_provider(self):
        config = object()
        market = object()
        engine = MagicMock()
        engine.generate_local.return_value = {
            "hypothesis_id": "hyp_" + "a" * 32,
            "reused": False,
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData", return_value=market) as market_data,
            patch("ai_trade.cli.HypothesisLabEngine", return_value=engine),
            patch("ai_trade.cli._ensure_cache") as ensure_cache,
            redirect_stdout(output),
        ):
            status = main(
                [
                    "hypothesis-generate",
                    "--objective",
                    "drawdown",
                    "--title",
                    "Test",
                ]
            )

        self.assertEqual(status, 0)
        market_data.assert_called_once_with(config, recover_snapshot=False)
        ensure_cache.assert_not_called()
        engine.generate_local.assert_called_once_with(
            "local-owner", market, objective="drawdown", title="Test"
        )
        self.assertEqual(
            json.loads(output.getvalue())["hypothesis_id"], "hyp_" + "a" * 32
        )

    def test_list_and_show_do_not_open_market_data(self):
        config = object()
        engine = MagicMock()
        engine.list.return_value = {"hypotheses": [], "summary": {"total": 0}}
        engine.get.return_value = {"hypothesis_id": "hyp_" + "b" * 32}
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.HypothesisLabEngine", return_value=engine),
            patch("ai_trade.cli.MarketData") as market_data,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["hypothesis-list", "--limit", "9"]), 0)
            self.assertEqual(main(["hypothesis-show", "hyp_" + "b" * 32]), 0)

        market_data.assert_not_called()
        engine.list.assert_called_once_with("local-owner", limit=9)
        engine.get.assert_called_once_with("local-owner", "hyp_" + "b" * 32)

    def test_materialization_requires_confirmation_and_dispatches_human_actor(self):
        config = object()
        engine = MagicMock()
        engine.materialize_candidate.return_value = {
            "hypothesis_id": "hyp_" + "c" * 32,
            "candidate": {"status": "DRAFT"},
        }
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.HypothesisLabEngine", return_value=engine),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["hypothesis-materialize", "hyp_" + "c" * 32]), 1
            )
            self.assertEqual(
                main(
                    [
                        "hypothesis-materialize",
                        "hyp_" + "c" * 32,
                        "--yes",
                    ]
                ),
                0,
            )

        engine.materialize_candidate.assert_called_once_with(
            "local-owner",
            "hyp_" + "c" * 32,
            confirmed_by="local-cli-user",
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
