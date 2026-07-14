import csv
import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from ai_trade.config import load_config
from ai_trade.models import Bar, Instrument
from ai_trade.web.auth import Session
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_market_chart_query,
)
from ai_trade.web.service import DashboardService


class MarketChartServiceTests(unittest.TestCase):
    def test_week_and_month_aggregation_summary_and_trade_markers(self):
        bars = [
            _bar("2024-01-29", 10, 11, 12, 9, 100, 1_000),
            _bar("2024-01-30", 11, 12, 13, 10, 200, 2_000),
            _bar("2024-02-01", 12, 11, 14, 10, 300, 3_000),
            _bar("2024-02-02", 11, 13, 15, 9, 400, 4_000),
            _bar("2024-02-05", 13, 14, 16, 12, 500, 5_000),
            _bar("2024-02-09", 14, 15, 17, 13, 600, 6_000),
            _bar("2024-02-29", 15, 16, 18, 14, 700, 7_000),
            _bar("2024-03-01", 16, 17, 19, 15, 800, 8_000),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _service_config(root)
            _write_trade_ledger(config.paper_trades_file)
            service = DashboardService(config)
            service.market = MagicMock(return_value=_fake_market(bars))
            service._paper_state = lambda: {"account_id": "paper-account"}

            weekly = service.market_chart(symbol="510300", period="week", limit=60)
            monthly = service.market_chart(symbol="510300", period="month", limit=60)

        self.assertTrue(weekly["available"])
        self.assertEqual(len(weekly["bars"]), 3)
        self.assertEqual(
            weekly["bars"][0],
            {
                "date": "2024-02-02",
                "open": 10.0,
                "high": 15.0,
                "low": 9.0,
                "close": 13.0,
                "volume": 1_000.0,
                "amount": 10_000.0,
            },
        )
        self.assertEqual(weekly["bars"][-1]["date"], "2024-03-01")
        self.assertEqual(weekly["summary"]["latest_close"], 17.0)
        self.assertEqual(weekly["summary"]["previous_close"], 15.0)
        self.assertEqual(weekly["summary"]["change"], 2.0)
        self.assertAlmostEqual(weekly["summary"]["change_percent"], 2 / 15)
        self.assertEqual(weekly["summary"]["bar_count"], 3)
        self.assertEqual(weekly["trade_markers"][0]["date"], "2024-01-30")
        self.assertEqual(weekly["trade_markers"][0]["bar_date"], "2024-02-02")
        self.assertTrue(weekly["diagnostics"]["stale"])
        self.assertEqual(weekly["diagnostics"]["status"], "stale")
        self.assertEqual(weekly["snapshot"]["manifest_sha256"], "b" * 64)
        json.dumps(weekly, allow_nan=False)

        self.assertEqual(
            [value["date"] for value in monthly["bars"]],
            ["2024-01-30", "2024-02-29", "2024-03-01"],
        )
        self.assertEqual(monthly["bars"][1]["open"], 12.0)
        self.assertEqual(monthly["bars"][1]["close"], 16.0)
        self.assertEqual(monthly["bars"][1]["volume"], 2_500.0)

    def test_missing_snapshot_is_explicit_and_unknown_symbol_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = DashboardService(_service_config(Path(temporary)))
            service.market = MagicMock(side_effect=FileNotFoundError("missing cache"))
            result = service.market_chart(symbol="510300", period="day", limit=60)

        self.assertFalse(result["available"])
        self.assertEqual(result["bars"], [])
        self.assertIsNone(result["summary"])
        self.assertTrue(result["diagnostics"]["missing"])
        self.assertEqual(result["diagnostics"]["status"], "missing")
        self.assertEqual(result["diagnostics"]["code"], "market_data_unavailable")
        with self.assertRaisesRegex(ValueError, "configured universe"):
            service.market_chart(symbol="UNKNOWN", period="day", limit=60)

    def test_limit_bounds_response(self):
        start = date(2018, 1, 1)
        bars = [
            Bar(
                start + timedelta(days=index),
                10.0,
                10.5,
                11.0,
                9.5,
                100.0,
                1_000.0,
            )
            for index in range(1_600)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            service = DashboardService(_service_config(Path(temporary)))
            service.market = MagicMock(return_value=_fake_market(bars))
            service._paper_state = lambda: None
            result = service.market_chart(symbol="510300", period="day", limit=60)

        self.assertEqual(len(result["bars"]), 60)
        self.assertEqual(result["summary"]["bar_count"], 60)
        for invalid in (True, 59, 1501, "60"):
            with self.subTest(limit=invalid), self.assertRaises(ValueError):
                service.market_chart(symbol="510300", period="day", limit=invalid)

    def test_validated_cache_request_is_read_only_and_excludes_future_bar(self):
        source = load_config(
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(source, project_root=root)
            _write_validated_cache(config)
            before = _tree_snapshot(root)
            service = DashboardService(config)
            service._paper_state = lambda: None

            result = service.market_chart(
                symbol=config.strategy.benchmark,
                period="day",
                limit=60,
            )
            after = _tree_snapshot(root)

        self.assertEqual(before, after)
        self.assertFalse((config.cache_dir / ".cache-transaction.lock").exists())
        self.assertTrue(result["available"])
        self.assertEqual(result["data_date"], "2024-01-03")
        self.assertEqual(len(result["bars"]), 2)
        self.assertEqual(
            result["diagnostics"]["excluded_incomplete_dates"], ["2099-01-01"]
        )
        self.assertRegex(result["snapshot"]["manifest_sha256"], r"^[0-9a-f]{64}$")


class MarketChartHttpTests(unittest.TestCase):
    def test_vendored_chart_bundle_is_served_locally(self):
        with _RunningServer(_HttpService()) as port:
            status, body = _request(port, "GET", "/vendor/klinecharts.min.js")

        self.assertEqual(status, 200)
        self.assertIn(b"klinecharts", body[:1000])
        self.assertGreater(len(body), 200_000)

    def test_query_parser_is_strict(self):
        self.assertEqual(
            _parse_market_chart_query("symbol=510300"),
            ("510300", "day", 240),
        )
        self.assertEqual(
            _parse_market_chart_query("symbol=510300&period=month&limit=1500"),
            ("510300", "month", 1500),
        )
        for query in (
            "",
            "symbol=",
            "symbol=510300&symbol=510500",
            "symbol=510300&period=day&period=week",
            "symbol=510300&limit=60&limit=61",
            "symbol=510300&period=hour",
            "symbol=510300&limit=59",
            "symbol=510300&limit=1501",
            "symbol=510300&limit=60.0",
            "symbol=510300&extra=true",
            "symbol=510300&",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _parse_market_chart_query(query)

    def test_non_object_manifest_returns_recoverable_unavailable_response(self):
        source = load_config(
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        session_token = "s" * 32
        sessions = {
            session_token: _session("alice", "alice-csrf", "acct_" + "1" * 32)
        }
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(source, project_root=Path(temporary))
            _write_validated_cache(config)
            (config.cache_dir / "manifest.json").write_text("[]", encoding="utf-8")
            service = DashboardService(config)
            service._paper_state = lambda: None
            with _RunningServer(service, auth=_Auth(sessions)) as port:
                status, body = _request(
                    port,
                    "GET",
                    f"/api/market-chart?symbol={config.strategy.benchmark}",
                    headers={"Cookie": f"ai_trade_session={session_token}"},
                )

        payload = json.loads(body)
        self.assertEqual(status, 200, body)
        self.assertFalse(payload["available"])
        self.assertEqual(
            payload["diagnostics"]["code"], "market_data_unavailable"
        )
        self.assertIn("top-level JSON must be an object", payload["diagnostics"]["detail"])

    def test_endpoint_requires_authentication_and_passes_validated_query(self):
        service = _HttpService()
        session_token = "s" * 32
        sessions = {
            session_token: _session("alice", "alice-csrf", "acct_" + "1" * 32)
        }
        with _RunningServer(service, auth=_Auth(sessions)) as port:
            status, _ = _request(
                port,
                "GET",
                "/api/market-chart?symbol=510300&period=week&limit=60",
            )
            self.assertEqual(status, 401)

            status, body = _request(
                port,
                "GET",
                "/api/market-chart?symbol=510300&period=week&limit=60",
                headers={"Cookie": f"ai_trade_session={session_token}"},
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["symbol"], "510300")

            status, _ = _request(
                port,
                "GET",
                "/api/market-chart?symbol=510300&limit=59",
                headers={"Cookie": f"ai_trade_session={session_token}"},
            )
            self.assertEqual(status, 400)

        self.assertEqual(
            service.calls,
            [{"symbol": "510300", "period": "week", "limit": 60}],
        )


class _HttpService:
    config = SimpleNamespace(reports_dir=Path("unused"))

    def __init__(self):
        self.calls = []

    def market_chart(self, **values):
        self.calls.append(values)
        return {"available": True, "symbol": values["symbol"], "bars": []}


class _Jobs:
    def close(self):
        pass

    def list(self):
        return []

    def get(self, _job_id):
        return None


class _Users:
    @staticmethod
    def has_users():
        return True


class _Auth:
    users = _Users()

    def __init__(self, sessions):
        self._sessions = sessions

    def authenticate_session(self, token):
        return self._sessions.get(token)


class _RunningServer:
    def __init__(self, service, *, auth=None):
        self.jobs = _Jobs()
        handler = _handler_factory(service, self.jobs, "local-token", auth, 3600)
        self.server = DashboardServer(("127.0.0.1", 0), handler, self.jobs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server.server_port

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _service_config(root):
    instrument = Instrument(
        "510300",
        "CSI 300 ETF",
        "SH",
        "equity",
        instrument_type="ETF",
        asset_class="equity",
        currency="CNY",
    )
    return SimpleNamespace(
        instruments=(instrument,),
        raw={
            "data": {
                "provider": "eastmoney",
                "adjustment": "forward",
                "market_close_time": "15:30",
            }
        },
        security_master=SimpleNamespace(fingerprint=lambda: "a" * 64),
        paper_trades_file=root / "paper_trades.csv",
    )


def _fake_market(bars):
    return SimpleNamespace(
        symbols={"510300": SimpleNamespace(bars=bars)},
        completed_through=date(2024, 3, 4),
        latest_common_session=bars[-1].date,
        manifest={
            "downloaded_at": "2024-03-04T16:00:00+08:00",
            "files": {
                "510300": {
                    "source": "network",
                    "source_provider": "eastmoney",
                    "source_mode": "full",
                    "amount_quality": "provider_reported",
                }
            },
        },
        manifest_sha256="b" * 64,
        file_hashes={"510300": "c" * 64},
        excluded_dates={"510300": []},
    )


def _bar(raw_date, opening, close, high, low, volume, amount):
    return Bar(
        date.fromisoformat(raw_date),
        float(opening),
        float(close),
        float(high),
        float(low),
        float(volume),
        float(amount),
    )


def _write_trade_ledger(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "account_id",
                "trade_id",
                "date",
                "symbol",
                "side",
                "quantity",
                "price",
            ]
        )
        writer.writerow(
            [
                "paper-account",
                "1" * 24,
                "2024-01-30",
                "510300",
                "BUY",
                100,
                12,
            ]
        )
        writer.writerow(
            ["other-account", "trade-2", "2024-02-09", "510300", "SELL", 100, 15]
        )


def _write_validated_cache(config):
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    rows = [
        ["2024-01-02", 10, 10.5, 11, 9.5, 100, 1_000],
        ["2024-01-03", 10.5, 11, 11.5, 10, 200, 2_000],
        ["2099-01-01", 11, 12, 12.5, 10.5, 300, 3_000],
    ]
    for instrument in config.instruments:
        path = config.cache_dir / f"{instrument.symbol}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
            writer.writerows(rows)
        files[instrument.symbol] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source": "network",
            "latest_session": rows[-1][0],
        }
    manifest = {
        "provider": config.raw["data"]["provider"],
        "adjustment": config.raw["data"].get("adjustment", "none"),
        "downloaded_at": "2024-01-03T16:00:00+08:00",
        "latest_common_session": "2024-01-03",
        "files": files,
    }
    (config.cache_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _tree_snapshot(root):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _session(username, csrf, account_id):
    now = time.time()
    return Session(username, now, now + 3600, csrf, "a" * 64, account_id)


def _request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


if __name__ == "__main__":
    unittest.main()
