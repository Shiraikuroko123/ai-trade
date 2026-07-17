import csv
import hashlib
import http.client
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import call, patch

from ai_trade.config import load_config
from ai_trade.data.eastmoney import (
    DIRECT_OPENER,
    EASTMONEY_UT,
    _stage_recent_completed_cache,
    completed_session_cutoff,
    download_instrument,
    download_universe,
    load_cached_bars,
)
from ai_trade.data.market import MAX_MARKET_MANIFEST_BYTES, MarketData
from ai_trade.data.tencent import (
    DIRECT_OPENER as TENCENT_DIRECT_OPENER,
    download_instrument as download_tencent_instrument,
)


class DataTests(unittest.TestCase):
    def test_configuration_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = _write_config(Path(temporary))
            content = path.read_text(encoding="utf-8")
            content = content.replace(
                '"provider": "eastmoney"',
                '"provider": "eastmoney", "provider": "tencent"',
                1,
            )
            path.write_text(content, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                load_config(path)

    def test_completed_session_cutoff_excludes_market_day_before_close(self):
        china = timezone(timedelta(hours=8))
        morning = datetime(2024, 1, 3, 10, 0, tzinfo=china)
        evening = datetime(2024, 1, 3, 16, 0, tzinfo=china)
        self.assertEqual(completed_session_cutoff(morning).isoformat(), "2024-01-02")
        self.assertEqual(completed_session_cutoff(evening).isoformat(), "2024-01-03")

    def test_completed_session_cutoff_rolls_weekend_to_friday(self):
        china = timezone(timedelta(hours=8))
        sunday = datetime(2024, 1, 7, 18, 0, tzinfo=china)
        monday_morning = datetime(2024, 1, 8, 10, 0, tzinfo=china)
        self.assertEqual(completed_session_cutoff(sunday).isoformat(), "2024-01-05")
        self.assertEqual(
            completed_session_cutoff(monday_morning).isoformat(), "2024-01-05"
        )

    def test_market_data_filters_unfinished_bar(self):
        china = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _write_config(root)
            _write_bars(root / "data/cache/510300.csv")
            _write_bars(root / "data/cache/510500.csv")
            market = MarketData(
                load_config(config),
                as_of=datetime(2024, 1, 3, 10, 0, tzinfo=china),
            )
            self.assertEqual(market.latest_date().isoformat(), "2024-01-02")
            self.assertEqual(
                [value.isoformat() for value in market.excluded_dates["510300"]],
                ["2024-01-03"],
            )

    def test_snapshot_reports_true_latest_common_session(self):
        china = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = _write_config(root)
            _write_bars(
                root / "data/cache/510300.csv",
                dates=("2024-01-02", "2024-01-03"),
            )
            _write_bars(root / "data/cache/510500.csv", dates=("2024-01-02",))
            market = MarketData(
                load_config(config_path),
                as_of=datetime(2024, 1, 3, 16, 0, tzinfo=china),
            )

            self.assertEqual(market.latest_date().isoformat(), "2024-01-03")
            self.assertEqual(
                market.snapshot_metadata()["latest_common_session"], "2024-01-02"
            )

    def test_latest_common_session_ignores_inactive_instruments(self):
        china = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = _write_config(root)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["universe"].append(
                {
                    "symbol": "510900",
                    "name": "Inactive",
                    "market": "SH",
                    "asset": "inactive",
                    "lot_size": 100,
                    "delisting_date": "2024-01-02",
                }
            )
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            _write_bars(
                root / "data/cache/510300.csv",
                dates=("2024-01-02", "2024-01-03"),
            )
            _write_bars(
                root / "data/cache/510500.csv",
                dates=("2024-01-02", "2024-01-03"),
            )
            _write_bars(root / "data/cache/510900.csv", dates=("2024-01-02",))

            market = MarketData(
                load_config(config_path),
                as_of=datetime(2024, 1, 3, 16, 0, tzinfo=china),
            )

            self.assertEqual(market.latest_common_session.isoformat(), "2024-01-03")

    def test_cache_rejects_non_increasing_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.csv"
            _write_bars(path, dates=("2024-01-02", "2024-01-02"))
            with self.assertRaisesRegex(RuntimeError, "strictly increasing"):
                load_cached_bars(path)

    def test_market_data_rejects_manifest_policy_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _write_config(root)
            cache = root / "data/cache"
            _write_bars(cache / "510300.csv")
            _write_bars(cache / "510500.csv")
            (cache / "manifest.json").write_text(
                json.dumps({"provider": "other", "adjustment": "forward", "files": {}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "provider"):
                MarketData(load_config(config))

    def test_market_data_rejects_ambiguous_or_oversized_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _write_config(root)
            cache = root / "data/cache"
            _write_bars(cache / "510300.csv")
            _write_bars(cache / "510500.csv")
            manifest = cache / "manifest.json"

            manifest.write_text(
                '{"provider":"eastmoney","provider":"tencent"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "duplicate JSON object key"):
                MarketData(load_config(config))

            manifest.write_bytes(b" " * (MAX_MARKET_MANIFEST_BYTES + 1))
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                MarketData(load_config(config))

    def test_download_retries_disconnect_with_browser_request_and_jitter(self):
        payload = {
            "rc": 0,
            "data": {
                "klines": [
                    "2024-01-02,10,10.1,10.2,9.8,100,1000,4",
                    "2024-01-03,10.1,10.2,10.3,10,110,1120,3",
                ]
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "max_attempts": 3,
                        "retry_base_seconds": 2.0,
                        "retry_max_seconds": 10.0,
                        "retry_jitter_seconds": 0.5,
                    },
                )
            )
            requests = []

            def open_request(request, timeout):
                requests.append((request, timeout))
                if len(requests) == 1:
                    raise http.client.RemoteDisconnected(
                        "Remote end closed connection without response"
                    )
                return _FakeResponse(payload)

            errors: list[str] = []
            output = root / "staged.csv"
            with (
                patch(
                    "ai_trade.data.eastmoney.urllib.request.urlopen",
                    side_effect=open_request,
                ),
                patch("ai_trade.data.eastmoney.os.environ.get", return_value=None),
                patch(
                    "ai_trade.data.eastmoney._cache_buster",
                    side_effect=("100000", "100101"),
                ),
                patch(
                    "ai_trade.data.eastmoney.random.uniform", return_value=0.25
                ) as jitter,
                patch("ai_trade.data.eastmoney.time_module.sleep") as sleep,
            ):
                result = download_instrument(
                    config,
                    config.instruments[0],
                    force=True,
                    output_path=output,
                    network_errors=errors,
                )

            self.assertEqual(result, output)
            self.assertEqual(len(load_cached_bars(output)), 2)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0][1], 20)
            self.assertIn("RemoteDisconnected", errors[0])
            sleep.assert_called_once_with(2.25)
            jitter.assert_called_once_with(0.0, 0.5)
            first_request, second_request = requests[0][0], requests[1][0]
            query = parse_qs(urlparse(first_request.full_url).query)
            self.assertEqual(query["ut"], [EASTMONEY_UT])
            self.assertIn("_", query)
            self.assertNotEqual(first_request.full_url, second_request.full_url)
            self.assertIn("Mozilla/5.0", first_request.get_header("User-agent"))
            self.assertEqual(
                first_request.get_header("Referer"), "https://quote.eastmoney.com/"
            )

    def test_universe_freezes_cutoff_and_cools_down_after_recovered_error(self):
        target = datetime(2024, 1, 3).date()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "request_interval_seconds": 0.25,
                        "request_jitter_seconds": 0.5,
                        "failure_cooldown_seconds": 2.0,
                    },
                )
            )
            calls: list[tuple[str, object]] = []

            def refresh(
                config,
                instrument,
                force,
                output_path,
                *,
                network_errors,
                cutoff,
                proxy_mode,
            ):
                calls.append((instrument.symbol, cutoff))
                if len(calls) == 1:
                    network_errors.append(
                        "attempt 1/4: RemoteDisconnected: provider closed connection"
                    )
                _write_bars(
                    output_path,
                    dates=("2024-01-02", cutoff.isoformat()),
                )
                return output_path

            with (
                patch(
                    "ai_trade.data.eastmoney.completed_session_cutoff",
                    return_value=target,
                ) as completed_cutoff,
                patch(
                    "ai_trade.data.eastmoney.download_instrument",
                    side_effect=refresh,
                ),
                patch("ai_trade.data.eastmoney.os.environ.get", return_value=None),
                patch(
                    "ai_trade.data.eastmoney.random.uniform", return_value=0.1
                ) as jitter,
                patch("ai_trade.data.eastmoney.time_module.sleep") as sleep,
            ):
                download_universe(config, force=True)

            completed_cutoff.assert_called_once_with(market_close="15:30")
            self.assertEqual(
                calls,
                [("510300", target), ("510500", target)],
            )
            jitter.assert_called_once_with(0.0, 0.5)
            sleep.assert_called_once()
            self.assertAlmostEqual(sleep.call_args.args[0], 2.35)
            manifest = json.loads(
                (config.cache_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["completed_through"], target.isoformat())
            self.assertEqual(manifest["latest_common_session"], target.isoformat())
            self.assertEqual(manifest["request_policy"]["max_attempts"], 4)
            self.assertEqual(
                manifest["files"]["510300"]["source"], "network"
            )
            self.assertEqual(len(manifest["files"]["510300"]["network_errors"]), 1)

    def test_eastmoney_attempt_limit_is_independent_from_fallback_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "max_attempts": 4,
                        "eastmoney_max_attempts": 2,
                        "retry_base_seconds": 0.0,
                        "retry_max_seconds": 0.0,
                        "retry_jitter_seconds": 0.0,
                    },
                )
            )
            requests = []

            def disconnect(request, timeout):
                requests.append((request, timeout))
                raise http.client.RemoteDisconnected("provider closed connection")

            errors: list[str] = []
            with (
                patch(
                    "ai_trade.data.eastmoney.urllib.request.urlopen",
                    side_effect=disconnect,
                ),
                patch("ai_trade.data.eastmoney.os.environ.get", return_value=None),
                patch("ai_trade.data.eastmoney.time_module.sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "after 2 attempt"):
                    download_instrument(
                        config,
                        config.instruments[0],
                        force=True,
                        output_path=root / "staged.csv",
                        network_errors=errors,
                    )

            self.assertEqual(len(requests), 2)
            self.assertEqual(len(errors), 2)
            self.assertTrue(all("/2:" in item for item in errors))

    def test_universe_refresh_is_serial_and_manifest_exposes_fallback(self):
        cutoff = completed_session_cutoff()
        shared = (cutoff - timedelta(days=2)).isoformat()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "fallback_provider": "none",
                        "request_interval_seconds": 0.25,
                        "request_jitter_seconds": 0.0,
                        "failure_cooldown_seconds": 0.0,
                    },
                )
            )
            first, second = config.instruments
            _write_bars(
                config.cache_dir / f"{first.symbol}.csv",
                dates=("2024-01-02", shared, cutoff.isoformat()),
            )
            _write_bars(
                config.cache_dir / f"{second.symbol}.csv",
                dates=(
                    "2024-01-02",
                    shared,
                    (cutoff - timedelta(days=1)).isoformat(),
                ),
            )
            fallback_bytes = (
                config.cache_dir / f"{second.symbol}.csv"
            ).read_bytes()
            _write_cache_manifest(
                config, config.cache_dir / f"{first.symbol}.csv"
            )
            _write_cache_manifest(
                config, config.cache_dir / f"{second.symbol}.csv"
            )
            calls: list[str] = []
            cutoffs = []

            def refresh(
                config,
                instrument,
                force,
                output_path,
                *,
                network_errors,
                cutoff,
                proxy_mode,
            ):
                calls.append(instrument.symbol)
                cutoffs.append(cutoff)
                if instrument.symbol == second.symbol:
                    network_errors.append(
                        "attempt 1/1: RemoteDisconnected: provider closed connection"
                    )
                    raise RuntimeError("provider closed connection")
                _write_bars(output_path, dates=(shared, cutoff.isoformat()))
                return output_path

            with (
                patch(
                    "ai_trade.data.eastmoney.download_instrument", side_effect=refresh
                ),
                patch("ai_trade.data.eastmoney.time_module.sleep") as sleep,
            ):
                download_universe(config, force=True)

            self.assertEqual(calls, [first.symbol, second.symbol])
            self.assertEqual(len(set(cutoffs)), 1)
            sleep.assert_called_once_with(0.25)
            manifest = json.loads(
                (config.cache_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["latest_common_session"], shared)
            self.assertEqual(manifest["request_policy"]["mode"], "serial")
            self.assertEqual(manifest["files"][first.symbol]["source"], "network")
            fallback = manifest["files"][second.symbol]
            self.assertEqual(fallback["source"], "validated_local_fallback")
            self.assertIn("RemoteDisconnected", fallback["network_errors"][0])
            self.assertIn("provider closed connection", fallback["fallback_reason"])
            self.assertEqual(
                fallback["latest_session"], (cutoff - timedelta(days=1)).isoformat()
            )
            self.assertEqual(
                (config.cache_dir / f"{second.symbol}.csv").read_bytes(),
                fallback_bytes,
            )

    def test_direct_proxy_mode_uses_proxy_free_opener(self):
        payload = {
            "rc": 0,
            "data": {
                "klines": ["2024-01-02,10,10.1,10.2,9.8,100,1000,4"]
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(root, {"proxy_mode": "direct", "max_attempts": 1})
            )
            output = root / "direct.csv"
            with (
                patch.object(
                    DIRECT_OPENER, "open", return_value=_FakeResponse(payload)
                ) as direct,
                patch("ai_trade.data.eastmoney.urllib.request.urlopen") as system,
                patch("ai_trade.data.eastmoney.os.environ.get", return_value=None),
            ):
                download_instrument(
                    config,
                    config.instruments[0],
                    force=True,
                    output_path=output,
                    cutoff=datetime(2024, 1, 3).date(),
                )

            direct.assert_called_once()
            system.assert_not_called()

    def test_environment_proxy_override_selects_system_opener(self):
        payload = {
            "rc": 0,
            "data": {
                "klines": ["2024-01-02,10,10.1,10.2,9.8,100,1000,4"]
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(root, {"proxy_mode": "direct", "max_attempts": 1})
            )
            with (
                patch.dict(
                    os.environ,
                    {"AI_TRADE_EASTMONEY_PROXY_MODE": " SYSTEM "},
                ),
                patch(
                    "ai_trade.data.eastmoney.urllib.request.urlopen",
                    return_value=_FakeResponse(payload),
                ) as system,
                patch.object(DIRECT_OPENER, "open") as direct,
            ):
                download_instrument(
                    config,
                    config.instruments[0],
                    force=True,
                    output_path=root / "system.csv",
                    cutoff=datetime(2024, 1, 3).date(),
                )

            system.assert_called_once()
            direct.assert_not_called()

    def test_invalid_environment_proxy_mode_fails_before_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(_write_config(root, {"max_attempts": 3}))
            with (
                patch.dict(
                    os.environ,
                    {"AI_TRADE_EASTMONEY_PROXY_MODE": "automatic"},
                ),
                patch("ai_trade.data.eastmoney.urllib.request.urlopen") as system,
                patch.object(DIRECT_OPENER, "open") as direct,
            ):
                with self.assertRaisesRegex(
                    ValueError, "AI_TRADE_EASTMONEY_PROXY_MODE"
                ):
                    download_instrument(
                        config,
                        config.instruments[0],
                        force=True,
                        output_path=root / "invalid.csv",
                    )

            system.assert_not_called()
            direct.assert_not_called()

    def test_failed_snapshot_does_not_replace_existing_cache(self):
        cutoff = completed_session_cutoff()
        stale_dates = (
            (cutoff - timedelta(days=11)).isoformat(),
            (cutoff - timedelta(days=10)).isoformat(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "fallback_provider": "none",
                        "request_interval_seconds": 0.0,
                    },
                )
            )
            first, second = config.instruments
            originals = {}
            for instrument in config.instruments:
                path = config.cache_dir / f"{instrument.symbol}.csv"
                _write_bars(path, dates=stale_dates)
                originals[instrument.symbol] = path.read_bytes()
            manifest_path = config.cache_dir / "manifest.json"
            manifest_path.write_text('{"snapshot": "old"}', encoding="utf-8")

            def refresh(
                config,
                instrument,
                force,
                output_path,
                *,
                network_errors,
                cutoff,
                proxy_mode,
            ):
                if instrument.symbol == second.symbol:
                    network_errors.append("attempt 1/1: OSError: disconnected")
                    raise RuntimeError("download exhausted")
                _write_bars(
                    output_path,
                    dates=(
                        (cutoff - timedelta(days=1)).isoformat(),
                        cutoff.isoformat(),
                    ),
                )
                return output_path

            with patch(
                "ai_trade.data.eastmoney.download_instrument", side_effect=refresh
            ):
                with self.assertRaisesRegex(RuntimeError, "too old"):
                    download_universe(config, force=True)

            for instrument in config.instruments:
                path = config.cache_dir / f"{instrument.symbol}.csv"
                self.assertEqual(path.read_bytes(), originals[instrument.symbol])
            self.assertEqual(
                manifest_path.read_text(encoding="utf-8"), '{"snapshot": "old"}'
            )
            self.assertEqual(list(config.cache_dir.glob(".snapshot-*")), [])

    def test_tencent_jsonp_amount_conversion_and_exact_latest_override(self):
        rows = [
            ["2024-01-02", "10", "10.1", "10.2", "9.8", "100", {}, "4", "123.45"],
            ["2024-01-03", "10.1", "10.2", "10.3", "10", "110", {}, "3", "200.01"],
        ]
        payload = _tencent_payload(rows, quote_amount="2000099")
        requests = []

        def open_request(request, timeout):
            requests.append((request, timeout))
            return _tencent_response(request, payload)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(_write_config(root))
            output = root / "tencent.csv"
            metadata = {}
            with (
                patch(
                    "ai_trade.data.tencent.urllib.request.urlopen",
                    side_effect=open_request,
                ),
                patch("ai_trade.data.tencent.os.environ.get", return_value=None),
            ):
                download_tencent_instrument(
                    config,
                    config.instruments[0],
                    output,
                    cutoff=datetime(2024, 1, 3).date(),
                    provider_metadata=metadata,
                )

            bars = load_cached_bars(output)
            self.assertEqual([bar.amount for bar in bars], [1_234_500, 2_000_099])
            self.assertEqual(bars[-1].volume, 110)
            self.assertEqual(requests[0][1], 20)
            query = parse_qs(urlparse(requests[0][0].full_url).query)
            self.assertEqual(
                query["param"][0].split(","),
                ["sh510300", "day", "2024-01-01", "2024-01-03", "320", "qfq"],
            )
            self.assertEqual(metadata["amount_quality"], "provider_reported_rounded")
            self.assertEqual(metadata["amount_resolution_cny"], 100)
            self.assertEqual(metadata["amount_max_rounding_error_cny"], 50)
            self.assertTrue(metadata["latest_amount_exact_override"])
            self.assertEqual(metadata["source_provider"], "tencent_newfqkline")
            self.assertEqual(metadata["source_mode"], "full_history")
            self.assertEqual(metadata["pages"], 1)
            self.assertEqual(metadata["overlap_rows"], 0)
            self.assertEqual(metadata["tencent_proxy_mode"], "system")

    def test_tencent_rejects_jsonp_tail_legacy_rows_and_bad_quote(self):
        valid_row = [
            "2024-01-03",
            "10",
            "10.1",
            "10.2",
            "9.8",
            "100",
            {},
            "4",
            "0.10",
        ]
        cases = []
        payload = _tencent_payload([valid_row])
        cases.append(("tail", payload, "alert(1)", "JSONP envelope"))
        cases.append(
            (
                "legacy",
                _tencent_payload([valid_row[:6]], include_quote=False),
                "",
                "Malformed Tencent kline",
            )
        )
        bad_code = _tencent_payload([valid_row])
        bad_code["data"]["sh510300"]["qt"]["sh510300"][2] = "510500"
        cases.append(("quote-code", bad_code, "", "qt code"))
        bad_ohlcv = _tencent_payload([valid_row])
        bad_ohlcv["data"]["sh510300"]["qt"]["sh510300"][33] = "10.3"
        cases.append(("quote-ohlcv", bad_ohlcv, "", "qt OHLCV"))
        bad_date = _tencent_payload([valid_row], quote_date="20240102")
        cases.append(("quote-date", bad_date, "", "qt date"))
        future_row = list(valid_row)
        future_row[0] = "2024-01-04"
        cases.append(
            (
                "future-kline",
                _tencent_payload([future_row], include_quote=False),
                "",
                "completed cutoff",
            )
        )

        for name, response_payload, tail, message in cases:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(name=name):
                root = Path(temporary)
                config = load_config(_write_config(root, {"max_attempts": 1}))

                def open_request(request, timeout):
                    return _tencent_response(request, response_payload, tail=tail)

                with (
                    patch(
                        "ai_trade.data.tencent.urllib.request.urlopen",
                        side_effect=open_request,
                    ),
                    patch("ai_trade.data.tencent.os.environ.get", return_value=None),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    download_tencent_instrument(
                        config,
                        config.instruments[0],
                        root / f"{name}.csv",
                        cutoff=datetime(2024, 1, 3).date(),
                    )

    def test_tencent_adjustments_and_direct_proxy(self):
        mappings = {
            "forward": ("qfq", "qfqday"),
            "backward": ("hfq", "hfqday"),
            "none": ("", "day"),
        }
        for configured, (request_mode, response_key) in mappings.items():
            with tempfile.TemporaryDirectory() as temporary, self.subTest(
                adjustment=configured
            ):
                root = Path(temporary)
                config = load_config(
                    _write_config(
                        root,
                        {"adjustment": configured, "proxy_mode": "system"},
                    )
                )
                payload = _tencent_payload(
                    [
                        [
                            "2024-01-03",
                            "10",
                            "10.1",
                            "10.2",
                            "9.8",
                            "100",
                            {},
                            "4",
                            "0.10",
                        ]
                    ],
                    response_key=response_key,
                    include_quote=False,
                )

                def open_request(request, timeout):
                    return _tencent_response(request, payload)

                metadata = {}
                with (
                    patch.object(
                        TENCENT_DIRECT_OPENER,
                        "open",
                        side_effect=open_request,
                    ) as direct,
                    patch("ai_trade.data.tencent.urllib.request.urlopen") as system,
                    patch(
                        "ai_trade.data.tencent.os.environ.get",
                        return_value="system",
                    ),
                ):
                    download_tencent_instrument(
                        config,
                        config.instruments[0],
                        root / f"{configured}.csv",
                        cutoff=datetime(2024, 1, 3).date(),
                        proxy_mode="direct",
                        provider_metadata=metadata,
                    )

                direct.assert_called_once()
                system.assert_not_called()
                query = parse_qs(urlparse(direct.call_args.args[0].full_url).query)
                self.assertEqual(query["param"][0].split(",")[-1], request_mode)
                self.assertEqual(metadata["tencent_proxy_mode"], "direct")

    def test_tencent_recent_cache_merges_only_matching_overlap(self):
        rows = [
            ["2024-01-02", "10", "10.1", "10.2", "9.8", "100", {}, "4", "0.099"],
            ["2024-01-03", "11", "11.1", "11.2", "10.8", "120", {}, "4", "0.20"],
        ]
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            return _tencent_response(
                request, _tencent_payload(rows, include_quote=False)
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(_write_config(root))
            cache = config.cache_dir / f"{config.instruments[0].symbol}.csv"
            _write_bars(cache, dates=("2024-01-02",))
            _write_cache_manifest(config, cache)
            output = root / "merged.csv"
            metadata = {}
            with (
                patch(
                    "ai_trade.data.tencent.urllib.request.urlopen",
                    side_effect=open_request,
                ),
                patch("ai_trade.data.tencent.os.environ.get", return_value=None),
            ):
                download_tencent_instrument(
                    config,
                    config.instruments[0],
                    output,
                    cache_path=cache,
                    cutoff=datetime(2024, 1, 3).date(),
                    provider_metadata=metadata,
                )

            bars = load_cached_bars(output)
            self.assertEqual([bar.date.isoformat() for bar in bars], ["2024-01-02", "2024-01-03"])
            self.assertEqual(bars[0].amount, 1000)
            self.assertEqual(bars[1].amount, 2000)
            query = parse_qs(urlparse(requests[0].full_url).query)
            self.assertEqual(query["param"][0].split(",")[2], "2024-01-02")
            self.assertEqual(len(requests), 1)
            self.assertEqual(metadata["source_mode"], "incremental")
            self.assertEqual(metadata["overlap_rows"], 1)

            mismatched = [list(row) for row in rows]
            mismatched[0][2] = "10.15"
            mismatch_calls = []

            def mismatch(request, timeout):
                mismatch_calls.append(request)
                return _tencent_response(
                    request, _tencent_payload(mismatched, include_quote=False)
                )

            rebuild_metadata = {}
            with (
                patch(
                    "ai_trade.data.tencent.urllib.request.urlopen",
                    side_effect=mismatch,
                ),
                patch("ai_trade.data.tencent.os.environ.get", return_value=None),
            ):
                download_tencent_instrument(
                    config,
                    config.instruments[0],
                    root / "mismatch.csv",
                    cache_path=cache,
                    cutoff=datetime(2024, 1, 3).date(),
                    provider_metadata=rebuild_metadata,
                )

            self.assertEqual(len(mismatch_calls), 2)
            rebuilt = load_cached_bars(root / "mismatch.csv")
            self.assertEqual(rebuilt[0].close, 10.15)
            self.assertEqual(
                rebuild_metadata["source_mode"],
                "full_rebuild_after_overlap_mismatch",
            )
            self.assertEqual(rebuild_metadata["overlap_rows"], 0)

    def test_tencent_exact_quote_override_is_retained_for_cached_latest_bar(self):
        row = [
            "2024-01-02",
            "10",
            "10.1",
            "10.2",
            "9.8",
            "100",
            {},
            "4",
            "0.10",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(_write_config(root))
            cache = config.cache_dir / f"{config.instruments[0].symbol}.csv"
            _write_bars(cache, dates=("2024-01-02",))
            _write_cache_manifest(config, cache)
            metadata = {}

            with (
                patch(
                    "ai_trade.data.tencent.urllib.request.urlopen",
                    side_effect=lambda request, timeout: _tencent_response(
                        request,
                        _tencent_payload([row], quote_date="20240102"),
                    ),
                ),
                patch("ai_trade.data.tencent.os.environ.get", return_value=None),
            ):
                output = root / "exact-existing.csv"
                download_tencent_instrument(
                    config,
                    config.instruments[0],
                    output,
                    cache_path=cache,
                    cutoff=datetime(2024, 1, 2).date(),
                    provider_metadata=metadata,
                )

            self.assertTrue(metadata["latest_amount_exact_override"])
            self.assertEqual(load_cached_bars(output)[-1].amount, 1000)
            self.assertEqual(metadata["cached_seed_source"], "network")
            self.assertEqual(metadata["retained_cached_rows"], 0)

    def test_tencent_incremental_rechecks_latest_320_cached_bars(self):
        first_date = datetime(2023, 1, 1).date()
        dates = tuple(
            (first_date + timedelta(days=index)).isoformat() for index in range(321)
        )
        all_rows = []
        for index, value in enumerate(dates):
            price = 10 + index
            all_rows.append(
                [
                    value,
                    str(price),
                    str(price + 0.1),
                    str(price + 0.2),
                    str(price - 0.2),
                    "100",
                    {},
                    "4",
                    "0.10",
                ]
            )
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            parts = parse_qs(urlparse(request.full_url).query)["param"][0].split(",")
            rows = [row for row in all_rows if parts[2] <= row[0] <= parts[3]]
            return _tencent_response(
                request, _tencent_payload(rows, include_quote=False)
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "start": dates[0],
                        "max_attempts": 1,
                        "request_interval_seconds": 0.0,
                    },
                )
            )
            cache = config.cache_dir / f"{config.instruments[0].symbol}.csv"
            _write_bars(cache, dates=dates)
            _write_cache_manifest(config, cache, requested_from=dates[0])
            output = root / "checked-320.csv"
            metadata = {}
            with (
                patch(
                    "ai_trade.data.tencent.urllib.request.urlopen",
                    side_effect=open_request,
                ),
                patch("ai_trade.data.tencent.os.environ.get", return_value=None),
            ):
                download_tencent_instrument(
                    config,
                    config.instruments[0],
                    output,
                    cache_path=cache,
                    cutoff=datetime.fromisoformat(dates[-1]).date(),
                    provider_metadata=metadata,
                )

            self.assertEqual(len(requests), 1)
            query = parse_qs(urlparse(requests[0].full_url).query)
            self.assertEqual(query["param"][0].split(",")[2], dates[-320])
            self.assertEqual(metadata["source_mode"], "incremental")
            self.assertEqual(metadata["overlap_rows"], 320)
            self.assertEqual(metadata["pages"], 1)
            self.assertEqual(len(load_cached_bars(output)), 321)

    def test_tencent_year_pages_retry_and_throttle(self):
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise http.client.RemoteDisconnected("Tencent disconnected")
            query = parse_qs(urlparse(request.full_url).query)
            year = query["param"][0].split(",")[2][:4]
            row_date = "2023-12-29" if year == "2023" else "2024-01-02"
            payload = _tencent_payload(
                [
                    [
                        row_date,
                        "10",
                        "10.1",
                        "10.2",
                        "9.8",
                        "100",
                        {},
                        "4",
                        "0.10",
                    ]
                ],
                include_quote=False,
            )
            return _tencent_response(request, payload)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "start": "2023-01-01",
                        "max_attempts": 3,
                        "retry_base_seconds": 2.0,
                        "retry_max_seconds": 10.0,
                        "retry_jitter_seconds": 0.5,
                        "request_interval_seconds": 0.75,
                    },
                )
            )
            metadata = {}
            with (
                patch(
                    "ai_trade.data.tencent.urllib.request.urlopen",
                    side_effect=open_request,
                ),
                patch("ai_trade.data.tencent.os.environ.get", return_value=None),
                patch(
                    "ai_trade.data.tencent.random.uniform", return_value=0.25
                ) as jitter,
                patch("ai_trade.data.tencent.time_module.sleep") as sleep,
            ):
                download_tencent_instrument(
                    config,
                    config.instruments[0],
                    root / "retried.csv",
                    cutoff=datetime(2024, 1, 3).date(),
                    provider_metadata=metadata,
                )

            self.assertEqual(len(requests), 3)
            self.assertNotEqual(requests[0].full_url, requests[1].full_url)
            self.assertEqual(sleep.call_args_list, [call(2.25), call(0.75)])
            jitter.assert_called_once_with(0.0, 0.5)
            self.assertEqual(metadata["pages"], 2)

    def test_tencent_rejects_unordered_duplicate_and_oversized_pages(self):
        base = ["10", "10.1", "10.2", "9.8", "100", {}, "4", "0.10"]
        duplicate = [["2023-01-02", *base], ["2023-01-02", *base]]
        descending = [["2023-01-03", *base], ["2023-01-02", *base]]
        oversized = [
            [
                (datetime(2023, 1, 1).date() + timedelta(days=index)).isoformat(),
                *base,
            ]
            for index in range(321)
        ]
        cases = (
            ("duplicate", duplicate, "strictly increasing"),
            ("descending", descending, "strictly increasing"),
            ("oversized", oversized, "row limit"),
        )
        for name, rows, message in cases:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(name=name):
                root = Path(temporary)
                config = load_config(
                    _write_config(
                        root,
                        {
                            "start": "2023-01-01",
                            "max_attempts": 1,
                        },
                    )
                )
                payload = _tencent_payload(rows, include_quote=False)

                def open_request(request, timeout):
                    return _tencent_response(request, payload)

                with (
                    patch(
                        "ai_trade.data.tencent.urllib.request.urlopen",
                        side_effect=open_request,
                    ),
                    patch("ai_trade.data.tencent.os.environ.get", return_value=None),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    download_tencent_instrument(
                        config,
                        config.instruments[0],
                        root / f"{name}.csv",
                        cutoff=datetime(2023, 12, 31).date(),
                    )

    def test_tencent_first_download_requests_each_required_year(self):
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            query = parse_qs(urlparse(request.full_url).query)
            year = query["param"][0].split(",")[2][:4]
            row_date = "2023-12-29" if year == "2023" else "2024-01-02"
            payload = _tencent_payload(
                [
                    [
                        row_date,
                        "10",
                        "10.1",
                        "10.2",
                        "9.8",
                        "100",
                        {},
                        "4",
                        "0.10",
                    ]
                ],
                include_quote=False,
            )
            return _tencent_response(request, payload)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_config(root)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["data"]["start"] = "2023-01-01"
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(path)
            output = root / "history.csv"
            metadata = {}
            with (
                patch(
                    "ai_trade.data.tencent.urllib.request.urlopen",
                    side_effect=open_request,
                ),
                patch("ai_trade.data.tencent.os.environ.get", return_value=None),
                patch("ai_trade.data.tencent.time_module.sleep") as sleep,
            ):
                download_tencent_instrument(
                    config,
                    config.instruments[0],
                    output,
                    cutoff=datetime(2024, 1, 3).date(),
                    provider_metadata=metadata,
                )

            self.assertEqual(len(requests), 2)
            self.assertEqual(len(load_cached_bars(output)), 2)
            sleep.assert_called_once_with(2.0)
            self.assertEqual(metadata["pages"], 2)

    def test_eastmoney_transport_circuit_uses_tencent_for_remaining_symbols(self):
        target = datetime(2024, 1, 3).date()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "request_interval_seconds": 0.0,
                        "request_jitter_seconds": 0.0,
                        "failure_cooldown_seconds": 0.0,
                    },
                )
            )
            eastmoney_calls = []
            tencent_calls = []

            def primary(config, instrument, *args, **kwargs):
                eastmoney_calls.append(instrument.symbol)
                try:
                    raise http.client.RemoteDisconnected("provider disconnected")
                except http.client.RemoteDisconnected as cause:
                    raise RuntimeError("Eastmoney attempts exhausted") from cause

            def fallback(
                config,
                instrument,
                output_path,
                *,
                cache_path,
                cutoff,
                proxy_mode,
                provider_metadata,
            ):
                tencent_calls.append(instrument.symbol)
                _write_bars(output_path, dates=("2024-01-02", cutoff.isoformat()))
                provider_metadata.update(
                    {
                        "amount_quality": "provider_reported_rounded",
                        "amount_resolution_cny": 100,
                        "amount_max_rounding_error_cny": 50,
                        "latest_amount_exact_override": True,
                    }
                )
                return output_path

            with (
                patch(
                    "ai_trade.data.eastmoney.completed_session_cutoff",
                    return_value=target,
                ),
                patch(
                    "ai_trade.data.eastmoney.download_instrument",
                    side_effect=primary,
                ),
                patch(
                    "ai_trade.data.eastmoney.tencent.download_instrument",
                    side_effect=fallback,
                ),
                patch("ai_trade.data.eastmoney.time_module.sleep"),
            ):
                download_universe(config, force=True)

            first, second = config.instruments
            self.assertEqual(eastmoney_calls, [first.symbol])
            self.assertEqual(tencent_calls, [first.symbol, second.symbol])
            manifest = json.loads(
                (config.cache_dir / "manifest.json").read_text(encoding="utf-8")
            )
            first_file = manifest["files"][first.symbol]
            second_file = manifest["files"][second.symbol]
            self.assertEqual(first_file["source"], "tencent_network_fallback")
            self.assertIn("attempts exhausted", first_file["fallback_reason"])
            self.assertEqual(first_file["amount_resolution_cny"], 100)
            self.assertEqual(second_file["source"], "tencent_network_fallback")
            self.assertIn("circuit breaker open", second_file["network_errors"][0])
            circuit = manifest["request_policy"]["eastmoney_circuit_breaker"]
            self.assertTrue(circuit["opened"])
            self.assertEqual(circuit["trigger_symbol"], first.symbol)

    def test_both_network_providers_fail_before_validated_local_fallback(self):
        target = datetime(2024, 1, 3).date()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "request_interval_seconds": 0.0,
                        "request_jitter_seconds": 0.0,
                        "failure_cooldown_seconds": 0.0,
                    },
                )
            )
            for instrument in config.instruments:
                cache = config.cache_dir / f"{instrument.symbol}.csv"
                _write_bars(
                    cache,
                    dates=("2024-01-02", "2024-01-03"),
                )
                _write_cache_manifest(config, cache)
            eastmoney_calls = []
            tencent_calls = []

            def primary(config, instrument, *args, **kwargs):
                eastmoney_calls.append(instrument.symbol)
                try:
                    raise http.client.RemoteDisconnected("provider disconnected")
                except http.client.RemoteDisconnected as cause:
                    raise RuntimeError("Eastmoney attempts exhausted") from cause

            def fallback(config, instrument, *args, **kwargs):
                tencent_calls.append(instrument.symbol)
                raise RuntimeError("Tencent unavailable")

            with (
                patch(
                    "ai_trade.data.eastmoney.completed_session_cutoff",
                    return_value=target,
                ),
                patch(
                    "ai_trade.data.eastmoney.download_instrument",
                    side_effect=primary,
                ),
                patch(
                    "ai_trade.data.eastmoney.tencent.download_instrument",
                    side_effect=fallback,
                ),
                patch("ai_trade.data.eastmoney.time_module.sleep"),
            ):
                download_universe(config, force=True)

            self.assertEqual(eastmoney_calls, [config.instruments[0].symbol])
            self.assertEqual(
                tencent_calls, [item.symbol for item in config.instruments]
            )
            manifest = json.loads(
                (config.cache_dir / "manifest.json").read_text(encoding="utf-8")
            )
            for instrument in config.instruments:
                item = manifest["files"][instrument.symbol]
                self.assertEqual(item["source"], "validated_local_fallback")
                self.assertIn("Tencent unavailable", item["fallback_reason"])
                self.assertTrue(
                    any("Tencent fallback failed" in value for value in item["network_errors"])
                )

    def test_local_fallback_rejects_manifest_contract_and_hash_mismatches(self):
        missing = object()
        cases = (
            ("manifest_missing", "document", "", missing, "manifest is missing"),
            ("provider", "manifest", "provider", "other", "provider"),
            ("adjustment", "manifest", "adjustment", "backward", "adjustment"),
            (
                "requested_from",
                "manifest",
                "requested_from",
                "2024-01-02",
                "configured start",
            ),
            (
                "requested_from_missing",
                "manifest",
                "requested_from",
                missing,
                "requested_from is invalid",
            ),
            ("symbol_missing", "files", "", missing, "omits"),
            ("rows_missing", "file", "rows", missing, "row count"),
            ("rows_wrong", "file", "rows", 1, "row count"),
            ("hash", "file", "sha256", "0" * 64, "SHA-256"),
            ("source_missing", "file", "source", missing, "source is invalid"),
            ("source_invalid", "file", "source", "legacy", "source is invalid"),
            (
                "latest_session_missing",
                "file",
                "latest_session",
                missing,
                "latest session is invalid",
            ),
            (
                "latest_session_wrong",
                "file",
                "latest_session",
                "2024-01-02",
                "latest session does not match",
            ),
        )
        for name, scope, field, value, message in cases:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(name=name):
                root = Path(temporary)
                config = load_config(_write_config(root))
                instrument = config.instruments[0]
                source = config.cache_dir / f"{instrument.symbol}.csv"
                destination = root / "staged.csv"
                _write_bars(source, dates=("2024-01-02", "2024-01-03"))
                _write_cache_manifest(config, source)
                manifest_path = config.cache_dir / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if scope == "document":
                    manifest_path.unlink()
                else:
                    if scope == "manifest":
                        target = manifest
                    elif scope == "files":
                        target = manifest["files"]
                        field = instrument.symbol
                    else:
                        target = manifest["files"][instrument.symbol]
                    if value is missing:
                        target.pop(field, None)
                    else:
                        target[field] = value
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaisesRegex(RuntimeError, message):
                    _stage_recent_completed_cache(
                        config,
                        instrument,
                        source,
                        destination,
                        date(2024, 1, 3),
                    )

                self.assertFalse(destination.exists())

    def test_data_transport_configuration_is_bounded(self):
        invalid = (
            ({"fallback_provider": "other"}, "fallback_provider"),
            ({"fallback_provider": []}, "fallback_provider"),
            ({"proxy_mode": "automatic"}, "proxy_mode"),
            ({"proxy_mode": []}, "proxy_mode"),
            ({"timeout_seconds": 0}, "timeout_seconds"),
            ({"timeout_seconds": True}, "timeout_seconds"),
            ({"max_attempts": 100}, "max_attempts"),
            ({"max_attempts": 1.5}, "max_attempts"),
            ({"eastmoney_max_attempts": 0}, "eastmoney_max_attempts"),
            ({"eastmoney_max_attempts": True}, "eastmoney_max_attempts"),
            ({"request_interval_seconds": float("inf")}, "request_interval"),
            ({"request_jitter_seconds": 11.0}, "request_jitter"),
            ({"failure_cooldown_seconds": -1.0}, "failure_cooldown"),
            ({"retry_jitter_seconds": "0.5"}, "retry_jitter"),
            ({"retry_base_seconds": 5.0, "retry_max_seconds": 1.0}, "retry_max"),
            ({"market_close_time": "15:30+08:00"}, "market_close_time"),
        )
        for overrides, message in invalid:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(
                overrides=overrides
            ):
                path = _write_config(Path(temporary), overrides)
                with self.assertRaisesRegex(ValueError, message):
                    load_config(path)


def _write_config(root: Path, data_overrides: dict | None = None) -> Path:
    path = root / "config/default.json"
    path.parent.mkdir(parents=True)
    value = {
        "data": {
            "provider": "eastmoney",
            "fallback_provider": "tencent",
            "start": "2024-01-01",
            "end": "2024-12-31",
            "cache_dir": "data/cache",
            "market_close_time": "15:30",
            "adjustment": "forward",
        },
        "universe": [
            {
                "symbol": "510300",
                "name": "A",
                "market": "SH",
                "asset": "a",
                "lot_size": 100,
            },
            {
                "symbol": "510500",
                "name": "B",
                "market": "SH",
                "asset": "b",
                "lot_size": 100,
            },
        ],
        "strategy": {
            "benchmark": "510300",
            "rebalance_days": 2,
            "lookback_days": 2,
            "skip_days": 0,
            "trend_sma_days": 2,
            "volatility_days": 2,
            "top_n": 1,
            "minimum_momentum": 0,
            "target_annual_volatility": 0.12,
            "minimum_cash_weight": 0.05,
            "max_position_weight": 0.95,
        },
        "risk": {
            "max_portfolio_drawdown": 0.15,
            "max_daily_loss": 0.1,
            "cooldown_days": 2,
        },
        "costs": {"commission_bps": 2, "slippage_bps": 3, "minimum_commission": 5},
        "backtest": {
            "initial_cash": 100000,
            "start": "2024-01-01",
            "end": "2024-12-31",
        },
        "paper": {
            "initial_cash": 100000,
            "state_file": "state/paper_state.json",
            "trades_file": "state/paper_trades.csv",
        },
        "reports_dir": "reports",
        "logs_dir": "logs",
    }
    if data_overrides:
        value["data"].update(data_overrides)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _write_bars(path: Path, dates=("2024-01-02", "2024-01-03")) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
        for index, value in enumerate(dates):
            price = 10 + index
            writer.writerow(
                [value, price, price + 0.1, price + 0.2, price - 0.2, 100, 1000]
            )


def _write_cache_manifest(
    config, cache: Path, *, requested_from: str | None = None
) -> None:
    manifest_path = config.cache_dir / "manifest.json"
    manifest = {
        "provider": config.raw["data"]["provider"],
        "adjustment": config.raw["data"].get("adjustment", "forward"),
        "requested_from": requested_from or config.raw["data"]["start"],
        "files": {},
    }
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bars = load_cached_bars(cache)
    manifest["files"][cache.stem] = {
        "rows": len(bars),
        "sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
        "source": "network",
        "latest_session": bars[-1].date.isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _tencent_payload(
    rows,
    *,
    response_key="qfqday",
    include_quote=True,
    quote_date="20240103",
    quote_amount="1000",
):
    code = "sh510300"
    item = {response_key: rows}
    if include_quote:
        latest = rows[-1]
        quote = [""] * 36
        quote[2] = "510300"
        quote[3] = latest[2]
        quote[5] = latest[1]
        quote[6] = latest[5]
        quote[30] = quote_date
        quote[33] = latest[3]
        quote[34] = latest[4]
        quote[35] = f"{latest[2]}/{latest[5]}/{quote_amount}"
        item["qt"] = {code: quote}
    return {"code": 0, "msg": "", "data": {code: item}}


def _tencent_response(request, payload, *, tail=""):
    callback = parse_qs(urlparse(request.full_url).query)["_var"][0]
    body = f"{callback}={json.dumps(payload)};{tail}".encode("utf-8")
    return _RawResponse(body)


class _RawResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
