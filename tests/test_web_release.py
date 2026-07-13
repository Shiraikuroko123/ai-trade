import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.diagnostics import diagnose
from ai_trade.models import Bar
from ai_trade.web.service import DashboardService


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class WebReleaseTests(unittest.TestCase):
    def test_diagnosis_distinguishes_alignment_from_freshness(self):
        config = load_config(REPOSITORY_ROOT / "config/default.json")
        instruments = config.instruments[:2]
        stale_date = date(2024, 1, 2)
        market = _DiagnosticMarket(
            instruments,
            latest=stale_date,
            completed_through=date(2024, 1, 5),
            sources=("network", "validated_local_fallback"),
        )

        result = diagnose(config, market)

        self.assertTrue(result["universe_latest_dates_aligned"])
        self.assertFalse(result["market_data_current"])
        self.assertEqual(result["market_data_lag_days"], 3)
        self.assertEqual(result["status"], "WARNING")
        self.assertEqual(
            result["cache_manifest"]["source_counts"],
            {"network": 1, "validated_local_fallback": 1},
        )
        self.assertTrue(
            any("refresh-data" in warning for warning in result["research_warnings"])
        )

    def test_diagnosis_warns_when_manifest_is_missing(self):
        config = load_config(REPOSITORY_ROOT / "config/default.json")
        current = date(2024, 1, 5)
        market = _DiagnosticMarket(
            config.instruments[:2],
            latest=current,
            completed_through=current,
            sources=("network", "network"),
        )
        market.manifest = None

        result = diagnose(config, market)

        self.assertTrue(result["market_data_current"])
        self.assertEqual(result["status"], "WARNING")
        self.assertFalse(result["cache_manifest"]["available"])

    def test_diagnosis_warns_after_recovered_provider_error(self):
        config = load_config(REPOSITORY_ROOT / "config/default.json")
        current = date(2024, 1, 5)
        market = _DiagnosticMarket(
            config.instruments[:2],
            latest=current,
            completed_through=current,
            sources=("network", "network"),
            network_errors=(
                (
                    "attempt 1/4: RemoteDisconnected: provider closed connection",
                ),
                (),
            ),
        )

        result = diagnose(config, market)

        self.assertTrue(result["market_data_current"])
        self.assertEqual(result["status"], "WARNING")
        self.assertEqual(
            result["cache_manifest"]["refresh_failures"],
            [
                {
                    "symbol": config.instruments[0].symbol,
                    "source": "network",
                    "attempts": 1,
                    "error_types": ["RemoteDisconnected"],
                }
            ],
        )
        self.assertTrue(
            any("connectivity was unstable" in item for item in result["research_warnings"])
        )

    def test_diagnosis_explains_tencent_network_fallback(self):
        config = load_config(REPOSITORY_ROOT / "config/default.json")
        current = date(2024, 1, 5)
        market = _DiagnosticMarket(
            config.instruments[:2],
            latest=current,
            completed_through=current,
            sources=("tencent_network_fallback", "tencent_network_fallback"),
        )

        result = diagnose(config, market)

        self.assertTrue(result["market_data_current"])
        self.assertEqual(result["status"], "WARNING")
        self.assertEqual(
            result["cache_manifest"]["source_counts"],
            {"tencent_network_fallback": 2},
        )
        self.assertTrue(
            any(
                "Tencent network fallback data for 2 instrument(s) after Eastmoney"
                in item
                for item in result["research_warnings"]
            )
        )

    def test_diagnosis_does_not_count_circuit_skip_as_provider_attempt(self):
        config = load_config(REPOSITORY_ROOT / "config/default.json")
        current = date(2024, 1, 5)
        market = _DiagnosticMarket(
            config.instruments[:2],
            latest=current,
            completed_through=current,
            sources=("tencent_network_fallback", "tencent_network_fallback"),
            network_errors=(
                (
                    "attempt 1/2: RemoteDisconnected: provider closed connection",
                    "attempt 2/2: RemoteDisconnected: provider closed connection",
                ),
                (
                    "Eastmoney circuit breaker open; skipped after transport failure "
                    "on 159915: EastmoneyDownloadError: attempts exhausted",
                ),
            ),
        )

        failures = diagnose(config, market)["cache_manifest"]["refresh_failures"]

        self.assertEqual(failures[0]["attempts"], 2)
        self.assertEqual(failures[1]["attempts"], 0)

    def test_research_reports_snapshot_state_and_configuration(self):
        source = load_config(REPOSITORY_ROOT / "config/default.json")
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(source, project_root=Path(temporary))
            config.reports_dir.mkdir(parents=True)
            snapshot = {
                "provider": config.raw["data"]["provider"],
                "adjustment": config.raw["data"]["adjustment"],
                "universe": {
                    "name": config.universe_name,
                    "security_master_sha256": config.security_master.fingerprint(),
                },
                "symbols": {"510300": {"sha256": "old-digest"}},
            }
            (config.reports_dir / "backtest_summary.json").write_text(
                json.dumps({"metadata": {"data_snapshot": snapshot}}),
                encoding="utf-8",
            )
            (config.reports_dir / "validation_report.json").write_text(
                "not-json", encoding="utf-8"
            )
            service = DashboardService(config)
            service.market = lambda: SimpleNamespace(
                file_hashes={"510300": "new-digest"}
            )

            result = service.research()

            self.assertEqual(result["reports"]["backtest"]["state"], "stale")
            self.assertEqual(result["reports"]["walk_forward"]["state"], "missing")
            self.assertEqual(result["reports"]["validation"]["state"], "invalid")
            self.assertEqual(
                result["configuration"]["strategy"], asdict(config.strategy)
            )
            self.assertEqual(result["configuration"]["risk"], asdict(config.risk))
            self.assertTrue(
                any("different data snapshot" in item for item in result["warnings"])
            )

    def test_universe_api_is_dynamic_and_works_without_market_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = _write_nine_instrument_config(root)
            config = load_config(config_path)

            result = DashboardService(config).universe(date(2026, 7, 10))

            self.assertFalse(result["market_available"])
            self.assertEqual(result["candidate_records"], 9)
            self.assertEqual(len(result["instruments"]), 9)
            self.assertEqual(
                {item["symbol"] for item in result["instruments"]},
                {item.symbol for item in config.instruments},
            )
            self.assertTrue(
                all(item["latest_bar_date"] is None for item in result["instruments"])
            )
            self.assertEqual(result["errors"][0]["recovery_action"], "refresh-data")

            trading = DashboardService(config).trading()
            self.assertEqual(
                trading["paper_audit"]["status"], "market_data_unavailable"
            )
            self.assertEqual(trading["paper_trades"], [])
            self.assertEqual(trading["errors"][0]["recovery_action"], "refresh-data")

    def test_historical_universe_coverage_is_capped_at_selected_date(self):
        config = load_config(REPOSITORY_ROOT / "config/default.json")
        selected = date(2020, 1, 2)
        market = SimpleNamespace(
            symbols={item.symbol: object() for item in config.instruments},
            latest_bar_on_or_before=lambda symbol, on_date: Bar(
                selected, 10, 10, 10, 10, 100, 1000
            ),
        )
        coverage = {
            item.symbol: {
                "name": item.name,
                "first": "2013-01-04",
                "last": "2026-07-10",
            }
            for item in config.instruments
        }
        service = DashboardService(config)
        service.market = lambda: market

        with patch(
            "ai_trade.web.service.diagnose", return_value={"coverage": coverage}
        ):
            result = service.universe(selected)

        self.assertTrue(result["market_available"])
        self.assertTrue(
            all(
                item["coverage"]["last"] == selected.isoformat()
                and item["coverage"]["cache_last"] == "2026-07-10"
                for item in result["instruments"]
            )
        )


class _DiagnosticMarket:
    def __init__(
        self,
        instruments,
        latest,
        completed_through,
        sources,
        network_errors=None,
    ):
        self.completed_through = completed_through
        self._latest = latest
        self.symbols = {}
        self.file_hashes = {}
        self.excluded_dates = {}
        files = {}
        errors_by_instrument = network_errors or (() for _ in instruments)
        for instrument, source, errors in zip(
            instruments, sources, errors_by_instrument
        ):
            bar = Bar(latest, 10, 10, 10, 10, 100, 1000)
            self.symbols[instrument.symbol] = SimpleNamespace(
                instrument=instrument, bars=[bar]
            )
            self.file_hashes[instrument.symbol] = f"digest-{instrument.symbol}"
            self.excluded_dates[instrument.symbol] = []
            files[instrument.symbol] = {
                "sha256": self.file_hashes[instrument.symbol],
                "source": source,
                "network_errors": list(errors),
            }
        self.manifest = {
            "downloaded_at": "2024-01-05T16:00:00+08:00",
            "completed_through": completed_through.isoformat(),
            "files": files,
        }

    def active_symbols(self, on_date):
        return tuple(self.symbols)

    def latest_date(self):
        return self._latest


def _write_nine_instrument_config(root: Path) -> Path:
    config = json.loads(
        (REPOSITORY_ROOT / "config/default.json").read_text(encoding="utf-8")
    )
    master = json.loads(
        (REPOSITORY_ROOT / "config/security_master.json").read_text(encoding="utf-8")
    )
    extra = dict(master["instruments"][0])
    extra.update(
        {
            "symbol": "588000",
            "name": "科创50ETF",
            "asset": "China technology",
            "sector": "china_technology",
            "listing_date": "2020-11-16",
        }
    )
    master["instruments"].append(extra)
    master["universes"]["core_etf"].append(
        {"symbol": extra["symbol"], "start": extra["listing_date"], "end": None}
    )
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "security_master.json").write_text(
        json.dumps(master, ensure_ascii=False), encoding="utf-8"
    )
    path = config_dir / "default.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
