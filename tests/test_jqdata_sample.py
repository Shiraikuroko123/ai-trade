from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_trade.cli import build_parser
from ai_trade.data.jqdata import (
    JQDATA_SAMPLE_COUNT,
    JQDATA_SAMPLE_FIELDS,
    JQDATA_SAMPLE_SECURITIES,
    JQDataCredentials,
    JQDataError,
    JQDataSampleStore,
    capture_price_sample,
    summarize_price_sample,
)


USERNAME = "13812345678"
PASSWORD = "private-password"
END_DATE = date(2026, 4, 24)
CREATED_AT = datetime(2026, 7, 27, 10, 30, tzinfo=timezone.utc)


class _FakeFrame:
    def __init__(self, sessions: list[date], rows: list[dict[str, float]]) -> None:
        self.columns = list(JQDATA_SAMPLE_FIELDS)
        self.index = [datetime(item.year, item.month, item.day) for item in sessions]
        self._rows = rows

    def to_dict(self, *, orient: str) -> list[dict[str, float]]:
        if orient != "records":
            raise ValueError("unsupported orientation")
        return [dict(item) for item in self._rows]


class _FakePriceSDK:
    __version__ = "1.9.8"

    def __init__(self, rows: dict[str, list[dict[str, float]]]) -> None:
        self.rows = rows
        self.sessions = _trading_sessions()
        self.calls: list[object] = []
        self.query_count_calls = 0

    def auth(self, username: str, password: str) -> tuple[bool, str]:
        self.calls.append(("auth", username, password))
        return True, "auth success"

    def logout(self) -> None:
        self.calls.append("logout")

    def get_account_info(self) -> dict[str, object]:
        self.calls.append("get_account_info")
        return {
            "license": 1,
            "date_range_start": "2025-04-18 00:00:00",
            "date_range_end": "2026-04-25 00:00:00",
            "query_count_limit": 1_000_000,
            "expire_time": "2026-10-28 00:00:00",
            "mob": USERNAME,
        }

    def get_query_count(self) -> dict[str, int]:
        self.calls.append("get_query_count")
        self.query_count_calls += 1
        spare = 1_000_000 if self.query_count_calls == 1 else 999_280
        return {"spare": spare, "total": 1_000_000}

    def get_price(self, security: str, **kwargs: object) -> _FakeFrame:
        self.calls.append(("get_price", security, kwargs))
        return _FakeFrame(self.sessions, self.rows[security])


def _trading_sessions() -> list[date]:
    sessions: list[date] = []
    current = END_DATE
    while len(sessions) < JQDATA_SAMPLE_COUNT:
        if current.weekday() < 5:
            sessions.append(current)
        current -= timedelta(days=1)
    return list(reversed(sessions))


def _remote_rows(offset: float) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    previous_close = 4.0 + offset
    for index, _session in enumerate(_trading_sessions()):
        base = 4.0 + offset + index * 0.01
        close = base + 0.005
        volume = float((100_000 + index * 100) * 100)
        rows.append(
            {
                "open": base,
                "close": close,
                "high": base + 0.02,
                "low": base - 0.01,
                "volume": volume,
                "money": close * volume,
                "factor": 1.0 if index < 10 else 1.1,
                "high_limit": base + 1.0,
                "low_limit": base - 1.0,
                "avg": base + 0.004,
                "pre_close": previous_close,
                "paused": 0.0,
            }
        )
        previous_close = close
    return rows


def _all_remote_rows() -> dict[str, list[dict[str, float]]]:
    return {
        security: _remote_rows(index * 0.4)
        for index, (_symbol, security) in enumerate(JQDATA_SAMPLE_SECURITIES)
    }


def _write_local_cache(
    root: Path,
    remote: dict[str, list[dict[str, float]]],
    *,
    money_multiplier: float = 1.0,
) -> Path:
    cache = root / "cache"
    cache.mkdir()
    files: dict[str, dict[str, object]] = {}
    sessions = _trading_sessions()
    for symbol, security in JQDATA_SAMPLE_SECURITIES:
        path = cache / f"{symbol}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["date", "open", "close", "high", "low", "volume", "amount"]
            )
            for session, row in zip(sessions, remote[security]):
                factor = row["factor"]
                scale = 0.5
                writer.writerow(
                    [
                        session.isoformat(),
                        row["open"] * factor * scale,
                        row["close"] * factor * scale,
                        row["high"] * factor * scale,
                        row["low"] * factor * scale,
                        row["volume"] / 100.0,
                        row["money"] * money_multiplier,
                    ]
                )
        files[symbol] = {
            "rows": JQDATA_SAMPLE_COUNT,
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "source": "tencent_network_fallback",
        }
    manifest = {
        "provider": "eastmoney",
        "adjustment": "forward",
        "downloaded_at": "2026-07-27T12:54:48+08:00",
        "completed_through": "2026-07-24",
        "latest_common_session": "2026-07-24",
        "files": files,
    }
    (cache / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return cache


class JQDataSampleTests(unittest.TestCase):
    def test_capture_uses_bounded_parameters_and_passes_adjustment_aware_gate(self):
        remote = _all_remote_rows()
        sdk = _FakePriceSDK(remote)
        credentials = JQDataCredentials(USERNAME, PASSWORD)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = _write_local_cache(root, remote)
            record = capture_price_sample(
                credentials,
                end_date=END_DATE,
                local_cache_dir=cache,
                sdk=sdk,
                created_at=CREATED_AT,
            )

            self.assertTrue(record["comparison"]["summary"]["gate_passed"])
            self.assertEqual(record["query_count_consumed"], 720)
            self.assertEqual(len(record["series"]), 3)
            for item in record["comparison"]["symbols"]:
                self.assertEqual(item["status"], "matched")
                self.assertEqual(
                    item["volume_unit"],
                    "jqdata_shares_local_lots_100",
                )
                self.assertTrue(all(item["checks"].values()))

            price_calls = [
                call
                for call in sdk.calls
                if isinstance(call, tuple) and call[0] == "get_price"
            ]
            self.assertEqual(len(price_calls), 3)
            for call, (_symbol, security) in zip(
                price_calls,
                JQDATA_SAMPLE_SECURITIES,
            ):
                self.assertEqual(call[1], security)
                self.assertEqual(
                    call[2],
                    {
                        "start_date": None,
                        "end_date": "2026-04-24 23:59:59",
                        "frequency": "daily",
                        "fields": list(JQDATA_SAMPLE_FIELDS),
                        "skip_paused": False,
                        "fq": "none",
                        "count": JQDATA_SAMPLE_COUNT,
                        "panel": False,
                        "fill_paused": False,
                        "round": False,
                    },
                )
            self.assertEqual(sdk.calls[-1], "logout")

            serialized = json.dumps(record, ensure_ascii=False)
            self.assertNotIn(USERNAME, serialized)
            self.assertNotIn(PASSWORD, serialized)
            store = JQDataSampleStore(root / "jqdata")
            first = store.publish(record)
            second = store.publish(record)
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])

            summary = summarize_price_sample(first)
            summary_text = json.dumps(summary, ensure_ascii=False)
            self.assertNotIn('"rows"', summary_text)
            self.assertNotIn('"local_rows"', summary_text)
            self.assertTrue(summary["comparison"]["summary"]["gate_passed"])

            path = (
                store.samples_root
                / CREATED_AT.date().isoformat()
                / f"{record['sample_id']}.json"
            )
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["series"][0]["rows"][0]["close"] += 1
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "fingerprint|match|OHLC"):
                store.get(CREATED_AT, str(record["sample_id"]))

    def test_material_mismatch_is_recorded_as_a_failed_gate(self):
        remote = _all_remote_rows()
        sdk = _FakePriceSDK(remote)
        with TemporaryDirectory() as temporary:
            cache = _write_local_cache(
                Path(temporary),
                remote,
                money_multiplier=1.1,
            )
            record = capture_price_sample(
                JQDataCredentials(USERNAME, PASSWORD),
                end_date=END_DATE,
                local_cache_dir=cache,
                sdk=sdk,
                created_at=CREATED_AT,
            )

        self.assertFalse(record["comparison"]["summary"]["gate_passed"])
        self.assertEqual(record["comparison"]["summary"]["mismatch"], 3)
        for item in record["comparison"]["symbols"]:
            self.assertFalse(item["checks"]["money_match"])

    def test_out_of_entitlement_date_never_requests_price_data(self):
        remote = _all_remote_rows()
        sdk = _FakePriceSDK(remote)
        with TemporaryDirectory() as temporary:
            cache = _write_local_cache(Path(temporary), remote)
            with self.assertRaisesRegex(JQDataError, "licensed date range"):
                capture_price_sample(
                    JQDataCredentials(USERNAME, PASSWORD),
                    end_date=date(2026, 4, 26),
                    local_cache_dir=cache,
                    sdk=sdk,
                    created_at=CREATED_AT,
                )

        self.assertFalse(
            any(
                isinstance(call, tuple) and call[0] == "get_price" for call in sdk.calls
            )
        )
        self.assertEqual(sdk.calls[-1], "logout")

    def test_cli_requires_an_explicit_end_date(self):
        parsed = build_parser().parse_args(
            ["jqdata-sample", "--end-date", "2026-04-24"]
        )
        self.assertEqual(parsed.command, "jqdata-sample")
        self.assertEqual(parsed.end_date, "2026-04-24")
        self.assertFalse(parsed.non_interactive)


if __name__ == "__main__":
    unittest.main()
