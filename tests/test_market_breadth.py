from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from ai_trade.config import load_config
from ai_trade.data.market_breadth import (
    BREADTH_COLUMNS,
    CHINA_TIMEZONE,
    SECTOR_COLUMNS,
    SECTOR_PAGE_SIZE,
    MarketBreadthQuery,
    MarketBreadthStore,
    refresh_market_breadth,
)


DAY = date(2026, 7, 17)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class MarketBreadthStoreTests(unittest.TestCase):
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

    def test_refresh_paginates_both_complete_evidence_sets(self):
        sectors = [_sector_row(index) for index in range(1, SECTOR_PAGE_SIZE + 2)]
        responses = (
            _sector_response(sectors[:SECTOR_PAGE_SIZE], total=len(sectors)),
            _sector_response(sectors[SECTOR_PAGE_SIZE:], total=len(sectors)),
            _breadth_response(_breadth_rows()),
        )
        with _patched_responses(*responses) as opened:
            result = refresh_market_breadth(self.config, DAY)

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["coverage"]["sector_pages"], 2)
        self.assertEqual(result["coverage"]["sector_declared_count"], 101)
        self.assertEqual(result["coverage"]["sector_received_count"], 101)
        self.assertEqual(result["coverage"]["breadth_received_count"], 3)
        self.assertEqual(result["summary"]["exchange_count"], 3)
        self.assertEqual(result["summary"]["sector_count"], 101)
        self.assertEqual(result["summary"]["advancers"], 421)
        self.assertEqual(result["summary"]["decliners"], 5_052)
        self.assertEqual(result["summary"]["unchanged"], 50)
        self.assertRegex(result["source"]["response_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(result["authority"]["research_only"])
        self.assertFalse(result["authority"]["execution_authorized"])
        sector_pages = []
        for call in opened.call_args_list[:2]:
            query = parse_qs(urlparse(call.args[0].full_url).query)
            sector_pages.append(query["pn"][0])
            self.assertEqual(query["fields"][0], ",".join(SECTOR_COLUMNS))
        self.assertEqual(sector_pages, ["1", "2"])
        breadth_query = parse_qs(urlparse(opened.call_args_list[2].args[0].full_url).query)
        self.assertEqual(breadth_query["fields"][0], ",".join(BREADTH_COLUMNS))

    def test_optional_sector_nulls_are_preserved_and_disclosed(self):
        row = _sector_row(1)
        for field in ("f4", "f8", "f10", "f20"):
            row[field] = "-"
        with _patched_responses(
            _sector_response([row]), _breadth_response(_breadth_rows())
        ):
            result = refresh_market_breadth(self.config, DAY)

        sector = result["sectors"][0]
        self.assertIsNone(sector["change_amount"])
        self.assertIsNone(sector["turnover_rate"])
        self.assertIsNone(sector["volume_ratio"])
        self.assertIsNone(sector["market_cap"])
        quality = result["coverage"]["data_quality"]
        self.assertEqual(quality["sector_rows_with_missing_optional_values"], 1)
        self.assertTrue(
            any(item["code"] == "optional_metric_missing" for item in result["warnings"])
        )

    def test_invalid_source_evidence_never_publishes(self):
        wrong_date = _sector_row(1)
        wrong_date["f124"] = _quote_timestamp(DAY - timedelta(days=1))

        duplicate = _sector_row(1)

        empty_counts = _sector_row(1)
        empty_counts["f104"] = 0
        empty_counts["f105"] = 0
        empty_counts["f106"] = 0

        wrong_breadth = _breadth_rows()[:2]

        cases = (
            (
                "wrong quote date",
                (_sector_response([wrong_date]), _breadth_response(_breadth_rows())),
            ),
            (
                "duplicate sector",
                (
                    _sector_response([duplicate, duplicate]),
                    _breadth_response(_breadth_rows()),
                ),
            ),
            (
                "empty sector count",
                (_sector_response([empty_counts]), _breadth_response(_breadth_rows())),
            ),
            (
                "missing exchange",
                (_sector_response([_sector_row(1)]), _breadth_response(wrong_breadth)),
            ),
            (
                "wrong benchmark name",
                (
                    _sector_response([_sector_row(1)]),
                    _breadth_response(
                        [
                            {**_breadth_rows()[0], "f14": "错误指数"},
                            *_breadth_rows()[1:],
                        ]
                    ),
                ),
            ),
            (
                "duplicate json key",
                (_Response(b'{"rc":0,"rc":0,"data":null}'),),
            ),
        )
        for label, responses in cases:
            with self.subTest(label=label), _patched_responses(*responses):
                result = refresh_market_breadth(self.config, DAY)
                self.assertFalse(result["available"])
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["errors"][0]["code"], "market_breadth_refresh_failed")

        dataset = self.config.market_intelligence_dir / "sector_breadth"
        self.assertFalse(dataset.exists())

    def test_pagination_metadata_drift_is_rejected(self):
        first = [_sector_row(index) for index in range(1, SECTOR_PAGE_SIZE + 1)]
        second = [_sector_row(SECTOR_PAGE_SIZE + 1), _sector_row(SECTOR_PAGE_SIZE + 2)]
        with _patched_responses(
            _sector_response(first, total=101),
            _sector_response(second, total=102),
        ):
            result = refresh_market_breadth(self.config, DAY)

        self.assertFalse(result["available"])
        self.assertIn("changed during pagination", result["errors"][0]["message"])

    def test_repeated_evidence_reuses_and_changed_records_append_revision(self):
        rows = [_sector_row(1), _sector_row(2)]
        with _patched_responses(
            _sector_response(rows), _breadth_response(_breadth_rows())
        ):
            first = refresh_market_breadth(self.config, DAY)
        with _patched_responses(
            _sector_response(rows), _breadth_response(_breadth_rows())
        ):
            reused = refresh_market_breadth(self.config, DAY)
        changed = [_sector_row(1), _sector_row(2)]
        changed[0]["f3"] = 9.5
        with _patched_responses(
            _sector_response(changed), _breadth_response(_breadth_rows())
        ):
            second = refresh_market_breadth(self.config, DAY)

        self.assertEqual(first["revision"], 1)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["revision_id"], first["revision_id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["supersedes"], first["revision_id"])
        listed = MarketBreadthStore(self.config).list(
            MarketBreadthQuery(trade_date=DAY, include_revisions=True)
        )
        self.assertEqual(len(listed["revisions"]), 2)
        self.assertEqual(listed["revisions"][0]["revision"], 1)
        self.assertEqual(listed["revisions"][1]["revision"], 2)

    def test_filters_sort_limit_and_valid_empty_view_keep_full_summary(self):
        rows = [
            _sector_row(1, name="Alpha", change=-2.0, turnover=2.0),
            _sector_row(2, name="Beta", change=3.0, turnover="-"),
            _sector_row(3, name="Gamma", change=1.0, turnover=5.0),
        ]
        with _patched_responses(
            _sector_response(rows), _breadth_response(_breadth_rows())
        ):
            refresh_market_breadth(self.config, DAY)

        store = MarketBreadthStore(self.config)
        sorted_view = store.list(
            MarketBreadthQuery(
                trade_date=DAY,
                sort="turnover_rate",
                direction="desc",
                limit=3,
            )
        )
        self.assertEqual([item["name"] for item in sorted_view["sectors"]], ["Gamma", "Alpha", "Beta"])
        empty_view = store.list(
            MarketBreadthQuery(trade_date=DAY, q="not-present", limit=1)
        )
        self.assertTrue(empty_view["available"])
        self.assertEqual(empty_view["sectors"], [])
        self.assertEqual(empty_view["summary"]["sector_count"], 3)
        self.assertEqual(empty_view["summary"]["matched_sector_count"], 0)
        self.assertEqual(empty_view["summary"]["returned_sector_count"], 0)

    def test_freshness_states_are_explicit(self):
        with _patched_responses(
            _sector_response([_sector_row(1)]), _breadth_response(_breadth_rows())
        ):
            refresh_market_breadth(self.config, DAY)
        store = MarketBreadthStore(self.config)

        current = store.list(completed_session_cutoff=DAY)
        stale = store.list(completed_session_cutoff=DAY + timedelta(days=3))
        provisional = store.list(
            MarketBreadthQuery(trade_date=DAY),
            completed_session_cutoff=DAY - timedelta(days=1),
        )

        self.assertEqual(current["status"], "current")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(provisional["status"], "provisional")
        self.assertEqual(stale["freshness"]["lag_calendar_days"], 3)

    def test_failed_refresh_preserves_previous_revision(self):
        with _patched_responses(
            _sector_response([_sector_row(1)]), _breadth_response(_breadth_rows())
        ):
            first = refresh_market_breadth(self.config, DAY)
        with _patched_responses(_Response(b"not-json")):
            failed = refresh_market_breadth(self.config, DAY)

        self.assertFalse(failed["available"])
        listed = MarketBreadthStore(self.config).list(
            MarketBreadthQuery(trade_date=DAY, include_revisions=True)
        )
        self.assertEqual(listed["revision_id"], first["revision_id"])
        self.assertEqual(len(listed["revisions"]), 1)

    def test_tampered_revision_fails_closed(self):
        with _patched_responses(
            _sector_response([_sector_row(1)]), _breadth_response(_breadth_rows())
        ):
            refresh_market_breadth(self.config, DAY)
        path = (
            self.config.market_intelligence_dir
            / "sector_breadth"
            / DAY.isoformat()
            / "revision_00000001.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["summary"]["advancers"] += 1
        path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "summary field advancers"):
            MarketBreadthStore(self.config).list(MarketBreadthQuery(trade_date=DAY))

    def test_unavailable_read_and_invalid_queries_are_bounded(self):
        result = MarketBreadthStore(self.config).list(
            MarketBreadthQuery(trade_date=DAY)
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["summary"]["advance_share"], None)
        self.assertEqual(result["authority"], {"research_only": True, "execution_authorized": False})
        for query in (
            MarketBreadthQuery(q=""),
            MarketBreadthQuery(sort="unknown"),
            MarketBreadthQuery(direction="sideways"),
            MarketBreadthQuery(limit=0),
            MarketBreadthQuery(limit=501),
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                MarketBreadthStore(self.config).list(query)


def _sector_row(
    index: int,
    *,
    name: str | None = None,
    change: float = 1.5,
    turnover: float | str = 2.5,
) -> dict[str, object]:
    return {
        "f12": f"BK{index:04d}",
        "f13": 90,
        "f14": name or f"Sector {index}",
        "f2": 1_000.0 + index,
        "f3": change,
        "f4": 15.0,
        "f8": turnover,
        "f10": 1.2,
        "f20": 100_000_000.0 + index,
        "f104": 10,
        "f105": 5,
        "f106": 1,
        "f124": _quote_timestamp(DAY),
    }


def _breadth_rows() -> list[dict[str, object]]:
    return [
        _breadth_row("000001", 1, "上证指数", 202, 2_119, 26),
        _breadth_row("399001", 0, "深证成指", 142, 2_690, 17),
        _breadth_row("899050", 0, "北证50", 77, 243, 7),
    ]


def _breadth_row(
    code: str,
    market: int,
    name: str,
    advancers: int,
    decliners: int,
    unchanged: int,
) -> dict[str, object]:
    return {
        "f12": code,
        "f13": market,
        "f14": name,
        "f2": 3_500.0,
        "f3": -1.25,
        "f104": advancers,
        "f105": decliners,
        "f106": unchanged,
        "f124": _quote_timestamp(DAY),
    }


def _quote_timestamp(on_date: date) -> int:
    return int(
        datetime.combine(on_date, time(15, 40), tzinfo=CHINA_TIMEZONE).timestamp()
    )


def _sector_response(
    rows: list[dict[str, object]], *, total: int | None = None
) -> "_Response":
    return _Response(
        _encoded(
            {
                "rc": 0,
                "rt": 6,
                "svr": 1,
                "lt": 1,
                "full": 1,
                "dlmkts": "",
                "dsc": "0",
                "data": {"total": len(rows) if total is None else total, "diff": rows},
            }
        )
    )


def _breadth_response(rows: list[dict[str, object]]) -> "_Response":
    return _Response(
        _encoded(
            {
                "rc": 0,
                "rt": 11,
                "svr": 2,
                "lt": 1,
                "full": 1,
                "dlmkts": "",
                "dsc": "0",
                "data": {"total": len(rows), "diff": rows},
            }
        )
    )


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class _Response:
    def __init__(self, raw: bytes):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, amount: int = -1) -> bytes:
        return self.raw if amount < 0 else self.raw[:amount]


@contextmanager
def _patched_responses(*responses: _Response):
    with patch(
        "ai_trade.data.market_breadth._open_request", side_effect=list(responses)
    ) as opened:
        yield opened


if __name__ == "__main__":
    unittest.main()
