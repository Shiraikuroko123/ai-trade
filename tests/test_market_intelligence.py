from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.data.market_intelligence import (
    DATASET,
    EASTMONEY_COLUMNS,
    PAGE_SIZE,
    DragonTigerQuery,
    DragonTigerStore,
    refresh_dragon_tiger,
)


DAY = date(2026, 7, 17)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class DragonTigerStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "config", self.root / "config")
        path = self.root / "config" / "default.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["data"]["max_attempts"] = 1
        raw["data"]["eastmoney_max_attempts"] = 1
        raw["data"]["retry_base_seconds"] = 0
        raw["data"]["retry_max_seconds"] = 0
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.config = load_config(path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_configuration_resolves_bounded_market_intelligence_state(self):
        self.assertEqual(
            self.config.market_intelligence_dir,
            (self.root / "state" / "market_intelligence").resolve(),
        )
        path = self.root / "config" / "default.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))
        for value, message in (
            (None, "must be an object"),
            ({"state_dir": ""}, "non-empty path"),
            ({"state_dir": "outside"}, "inside the workspace state"),
            ({"state_dir": "state"}, "must be a child"),
            (
                {"state_dir": "state/a", "root_dir": "state/b"},
                "must match",
            ),
        ):
            with self.subTest(value=value):
                current = dict(baseline)
                current["market_intelligence"] = value
                path.write_text(json.dumps(current), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    load_config(path)

    def test_multi_page_refresh_is_complete_bounded_and_auditable(self):
        rows = [_row(index) for index in range(PAGE_SIZE + 1)]
        responses = [
            _response(rows[:PAGE_SIZE], count=len(rows), pages=2),
            _response(rows[PAGE_SIZE:], count=len(rows), pages=2),
        ]
        with patch(
            "ai_trade.data.market_intelligence._open_request",
            side_effect=responses,
        ) as opened:
            result = refresh_dragon_tiger(self.config, DAY)

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "current")
        self.assertEqual(len(result["records"]), PAGE_SIZE + 1)
        self.assertEqual(result["coverage"]["pages"], 2)
        self.assertEqual(result["coverage"]["declared_count"], PAGE_SIZE + 1)
        self.assertEqual(result["coverage"]["received_count"], PAGE_SIZE + 1)
        self.assertTrue(result["coverage"]["complete"])
        self.assertRegex(result["source"]["response_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["source"]["certification"], "not_exchange_certified")
        self.assertTrue(result["authority"]["research_only"])
        self.assertFalse(result["authority"]["execution_authorized"])
        requested_pages = [
            parse_qs(urlparse(call.args[0].full_url).query)["pageNumber"][0]
            for call in opened.call_args_list
        ]
        self.assertEqual(requested_pages, ["1", "2"])
        self.assertEqual(
            parse_qs(urlparse(opened.call_args_list[0].args[0].full_url).query)[
                "columns"
            ][0],
            ",".join(EASTMONEY_COLUMNS),
        )

    def test_empty_source_result_is_persisted_as_empty_not_unavailable(self):
        empty = {
            "version": None,
            "result": None,
            "success": False,
            "message": "返回数据为空",
            "code": 9201,
        }
        with _patched_responses(_Response(_encoded(empty))):
            result = refresh_dragon_tiger(self.config, DAY)

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["records"], [])
        self.assertEqual(result["coverage"]["pages"], 0)
        self.assertEqual(result["coverage"]["declared_count"], 0)
        self.assertRegex(result["source"]["response_sha256"], r"^[0-9a-f]{64}$")
        listed = DragonTigerStore(self.config).list(
            DragonTigerQuery(trade_date=DAY)
        )
        self.assertEqual(listed["status"], "empty")
        stale = DragonTigerStore(self.config).list(
            DragonTigerQuery(trade_date=DAY),
            completed_session_cutoff=DAY + timedelta(days=3),
        )
        provisional = DragonTigerStore(self.config).list(
            DragonTigerQuery(trade_date=DAY),
            completed_session_cutoff=DAY - timedelta(days=1),
        )
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(provisional["status"], "provisional")
        self.assertEqual(stale["summary"]["record_count"], 0)
        self.assertEqual(provisional["summary"]["record_count"], 0)

    def test_optional_null_turnover_is_preserved_and_disclosed(self):
        row = _row(1)
        row["TURNOVERRATE"] = None
        with _patched_responses(_response([row])):
            result = refresh_dragon_tiger(self.config, DAY)

        self.assertIsNone(result["records"][0]["turnover_rate"])
        quality = result["coverage"]["data_quality"]
        self.assertEqual(quality["missing_optional_numeric_values"]["turnover_rate"], 1)
        self.assertTrue(quality["complete_identity_and_amounts"])
        self.assertTrue(
            any(item["code"] == "optional_metric_missing" for item in result["warnings"])
        )

    def test_invalid_source_evidence_never_publishes(self):
        cases: list[tuple[str, _Response]] = []

        wrong_date = _row(1)
        wrong_date["TRADE_DATE"] = "2026-07-16 00:00:00"
        cases.append(("wrong date", _response([wrong_date])))

        duplicate = _row(1)
        cases.append(("duplicate composite key", _response([duplicate, duplicate])))

        nan = _row(1)
        nan["CHANGE_RATE"] = float("nan")
        cases.append(("non-finite", _response([nan])))

        wrong_type = _row(1)
        wrong_type["TRADE_ID"] = "1001"
        cases.append(("field type", _response([wrong_type])))

        bad_net = _row(1)
        bad_net["BILLBOARD_NET_AMT"] += 1
        cases.append(("amount relationship", _response([bad_net])))

        count_mismatch = _response([_row(1)], count=2, pages=1)
        cases.append(("count/pages mismatch", count_mismatch))

        duplicate_json = _Response(
            b'{"success":true,"success":true,"code":0,"result":null}'
        )
        cases.append(("duplicate JSON key", duplicate_json))

        for label, response in cases:
            with self.subTest(label=label):
                store_root = self.config.market_intelligence_dir
                shutil.rmtree(store_root, ignore_errors=True)
                with _patched_responses(response):
                    result = refresh_dragon_tiger(self.config, DAY)
                self.assertFalse(result["available"])
                self.assertEqual(result["status"], "unavailable")
                self.assertTrue(result["errors"])
                revisions = list(
                    (store_root / DATASET).glob("*/revision_*.json")
                )
                self.assertEqual(revisions, [])

    def test_pagination_metadata_change_and_cross_page_duplicate_fail_closed(self):
        first_rows = [_row(index) for index in range(PAGE_SIZE)]
        final = _row(PAGE_SIZE)
        failures = (
            [
                _response(first_rows, count=PAGE_SIZE + 1, pages=2, version="v1"),
                _response([final], count=PAGE_SIZE + 1, pages=2, version="v2"),
            ],
            [
                _response(first_rows, count=PAGE_SIZE + 1, pages=2),
                _response([first_rows[0]], count=PAGE_SIZE + 1, pages=2),
            ],
        )
        for responses in failures:
            with self.subTest(kind=responses[1]):
                shutil.rmtree(self.config.market_intelligence_dir, ignore_errors=True)
                with patch(
                    "ai_trade.data.market_intelligence._open_request",
                    side_effect=responses,
                ):
                    result = refresh_dragon_tiger(self.config, DAY)
                self.assertFalse(result["available"])
                self.assertEqual(
                    list(
                        (self.config.market_intelligence_dir / DATASET).glob(
                            "*/revision_*.json"
                        )
                    ),
                    [],
                )

    def test_atomic_publish_failure_leaves_no_visible_revision(self):
        publish_call = "os.rename" if os.name == "nt" else "os.link"
        with (
            _patched_responses(_response([_row(1)])),
            patch(
                f"ai_trade.data.market_intelligence.{publish_call}",
                side_effect=OSError("synthetic atomic failure"),
            ),
        ):
            result = refresh_dragon_tiger(self.config, DAY)

        self.assertFalse(result["available"])
        self.assertEqual(
            list(
                (self.config.market_intelligence_dir / DATASET).glob(
                    "*/revision_*.json"
                )
            ),
            [],
        )

    def test_identical_evidence_is_idempotent_and_change_supersedes(self):
        first_row = _row(1)
        with _patched_responses(_response([first_row])):
            first = refresh_dragon_tiger(self.config, DAY)
        with _patched_responses(_response([first_row])):
            reused = refresh_dragon_tiger(self.config, DAY)

        self.assertFalse(first["reused"])
        self.assertTrue(reused["reused"])
        self.assertEqual(first["revision_id"], reused["revision_id"])
        self.assertEqual(first["evidence_fingerprint"], reused["evidence_fingerprint"])

        changed = _row(1)
        changed["BILLBOARD_BUY_AMT"] += 100
        changed["BILLBOARD_DEAL_AMT"] += 100
        changed["BILLBOARD_NET_AMT"] += 100
        with _patched_responses(_response([changed], version="v2")):
            second = refresh_dragon_tiger(self.config, DAY)

        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["supersedes"], first["revision_id"])
        self.assertEqual(second["supersedes_fingerprint"], first["record_fingerprint"])
        chain = DragonTigerStore(self.config).list(
            DragonTigerQuery(trade_date=DAY, include_revisions=True)
        )
        self.assertEqual([item["revision"] for item in chain["revisions"]], [1, 2])

    def test_tampering_is_detected_by_full_read_and_chain_validation(self):
        with _patched_responses(_response([_row(1)])):
            refresh_dragon_tiger(self.config, DAY)
        path = next(
            (self.config.market_intelligence_dir / DATASET).glob(
                "*/revision_*.json"
            )
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["records"][0]["name"] = "tampered"
        path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "invalid"):
            DragonTigerStore(self.config).list(DragonTigerQuery(trade_date=DAY))

    def test_local_list_filters_without_network_and_reports_freshness(self):
        rows = [
            _row(1, symbol="600519", market="SH", name="贵州茅台", reason="买入榜"),
            _row(2, symbol="000001", market="SZ", name="平安银行", reason="卖出榜"),
            _row(3, symbol="600000", market="SH", name="浦发银行", reason="卖出榜"),
        ]
        with _patched_responses(_response(rows)):
            refresh_dragon_tiger(self.config, DAY)
        store = DragonTigerStore(self.config)
        with patch(
            "ai_trade.data.market_intelligence._open_request",
            side_effect=AssertionError("GET/list must not use network"),
        ) as opened:
            by_symbol = store.list(DragonTigerQuery(symbol="600519"))
            by_market = store.list(DragonTigerQuery(market="SZ"))
            by_text = store.list(DragonTigerQuery(q="卖出", limit=1))
            stale = store.list(
                DragonTigerQuery(), completed_session_cutoff=date(2026, 7, 20)
            )

        opened.assert_not_called()
        self.assertEqual([item["symbol"] for item in by_symbol["records"]], ["600519"])
        self.assertEqual([item["symbol"] for item in by_market["records"]], ["000001"])
        self.assertEqual(len(by_text["records"]), 1)
        self.assertEqual(by_text["summary"]["matched_count"], 2)
        self.assertTrue(by_text["summary"]["truncated"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["freshness"]["status"], "stale")

    def test_unfetched_store_returns_explicit_unavailable(self):
        result = DragonTigerStore(self.config).list(
            DragonTigerQuery(trade_date=DAY),
            completed_session_cutoff=DAY,
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["trade_date"], DAY.isoformat())
        self.assertEqual(result["records"], [])
        self.assertEqual(result["authority"]["execution_authorized"], False)
        self.assertEqual(result["freshness"]["completed_session_cutoff"], DAY.isoformat())

    def test_query_is_strictly_bounded(self):
        store = DragonTigerStore(self.config)
        for query in (
            DragonTigerQuery(symbol="60051"),
            DragonTigerQuery(market="sh"),
            DragonTigerQuery(q=""),
            DragonTigerQuery(q="x" * 101),
            DragonTigerQuery(limit=0),
            DragonTigerQuery(limit=501),
            DragonTigerQuery(include_revisions=1),
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                store.list(query)


class _Response:
    def __init__(self, raw: bytes):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, maximum: int) -> bytes:
        return self.raw[:maximum]


def _patched_responses(*responses: _Response):
    return patch(
        "ai_trade.data.market_intelligence._open_request",
        side_effect=responses,
    )


def _response(
    rows: list[dict[str, object]],
    *,
    count: int | None = None,
    pages: int | None = None,
    version: str = "test-version",
) -> _Response:
    declared = len(rows) if count is None else count
    total_pages = max(1, (declared + PAGE_SIZE - 1) // PAGE_SIZE) if pages is None else pages
    return _Response(
        _encoded(
            {
                "version": version,
                "result": {"pages": total_pages, "data": rows, "count": declared},
                "success": True,
                "message": "ok",
                "code": 0,
            }
        )
    )


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=True).encode("utf-8")


def _row(
    index: int,
    *,
    symbol: str | None = None,
    market: str = "SH",
    name: str | None = None,
    reason: str = "日涨幅偏离值达到7%的前5只证券",
) -> dict[str, object]:
    selected_symbol = symbol or f"{600000 + index:06d}"
    buy = float(1_000 + index)
    sell = float(400 + index)
    return {
        "TRADE_DATE": "2026-07-17 00:00:00",
        "SECURITY_CODE": selected_symbol,
        "SECUCODE": f"{selected_symbol}.{market}",
        "SECURITY_NAME_ABBR": name or f"测试证券{index}",
        "CLOSE_PRICE": 10.0 + index / 100,
        "CHANGE_RATE": 1.5,
        "TURNOVERRATE": 2.5,
        "BILLBOARD_DEAL_AMT": buy + sell,
        "BILLBOARD_BUY_AMT": buy,
        "BILLBOARD_SELL_AMT": sell,
        "BILLBOARD_NET_AMT": buy - sell,
        "DEAL_AMOUNT_RATIO": 12.0,
        "DEAL_NET_RATIO": 5.0,
        "EXPLANATION": reason,
        "CHANGE_TYPE": f"137{index:06d}",
        "TRADE_ID": 100_000 + index,
        "TRADE_MARKET": "上交所主板" if market == "SH" else "深交所主板",
        "TRADE_MARKET_CODE": "069001001001" if market == "SH" else "069001002001",
    }


if __name__ == "__main__":
    unittest.main()
