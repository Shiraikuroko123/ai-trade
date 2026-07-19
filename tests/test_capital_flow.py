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
from ai_trade.data.capital_flow import (
    BOARD_FILTER,
    CHINA_TIMEZONE,
    FLOW_COLUMNS,
    PAGE_SIZE,
    CapitalFlowQuery,
    CapitalFlowStore,
    refresh_capital_flow,
)


DAY = date(2026, 7, 17)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class CapitalFlowStoreTests(unittest.TestCase):
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

    def test_refresh_paginates_and_publishes_provider_scope(self):
        rows = [_flow_row(index) for index in range(1, PAGE_SIZE + 2)]
        with _patched_responses(
            _flow_response(rows[:PAGE_SIZE], total=len(rows)),
            _flow_response(rows[PAGE_SIZE:], total=len(rows)),
        ) as opened:
            result = refresh_capital_flow(self.config, DAY)

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["coverage"]["pages"], 2)
        self.assertEqual(result["coverage"]["declared_count"], 101)
        self.assertEqual(result["coverage"]["received_count"], 101)
        self.assertEqual(result["summary"]["positive_main_count"], 101)
        self.assertEqual(result["summary"]["negative_main_count"], 0)
        self.assertTrue(result["authority"]["research_only"])
        self.assertFalse(result["authority"]["execution_authorized"])
        self.assertRegex(result["source"]["response_sha256"], r"^[0-9a-f]{64}$")
        queries = [parse_qs(urlparse(call.args[0].full_url).query) for call in opened.call_args_list]
        self.assertEqual([item["pn"][0] for item in queries], ["1", "2"])
        self.assertEqual(queries[0]["fs"], [BOARD_FILTER])
        self.assertEqual(queries[0]["fid"], ["f62"])
        self.assertEqual(queries[0]["fields"], [",".join(FLOW_COLUMNS)])
        warning_codes = {item["code"] for item in result["warnings"]}
        self.assertIn("provider_flow_scope", warning_codes)
        self.assertIn("provider_flow_methodology", warning_codes)

    def test_optional_values_are_preserved_and_reported(self):
        row = _flow_row(1)
        for field in ("f2", "f3", "f62", "f184", "f78"):
            row[field] = "-"
        with _patched_responses(_flow_response([row, _flow_row(2)])):
            result = refresh_capital_flow(self.config, DAY)

        self.assertTrue(result["available"])
        self.assertIsNone(result["flows"][0]["close"])
        self.assertIsNone(result["flows"][0]["main_net_inflow"])
        self.assertIsNone(result["flows"][0]["medium_net_inflow"])
        self.assertEqual(result["summary"]["missing_main_count"], 1)
        self.assertEqual(
            result["coverage"]["data_quality"]["main_metric_available_rows"], 1
        )
        self.assertTrue(
            any(item["code"] == "optional_metric_missing" for item in result["warnings"])
        )

        all_missing = _flow_row(3)
        all_missing["f62"] = "-"
        all_missing["f124"] = _quote_timestamp(DAY + timedelta(days=1))
        with _patched_responses(_flow_response([all_missing])):
            failed = refresh_capital_flow(self.config, DAY + timedelta(days=1))
        self.assertFalse(failed["available"])
        self.assertEqual(failed["errors"][0]["code"], "capital_flow_refresh_failed")
        self.assertIn("no main-flow values", failed["errors"][0]["message"])

    def test_invalid_date_duplicate_and_schema_never_publish(self):
        wrong_date = _flow_row(1)
        wrong_date["f124"] = _quote_timestamp(DAY - timedelta(days=1))
        duplicate = _flow_row(1)
        malformed = _flow_row(1)
        malformed.pop("f87")
        cases = (
            ("wrong date", _flow_response([wrong_date])),
            ("duplicate", _flow_response([duplicate, duplicate])),
            ("schema", _Response(json.dumps({"rc": 0, "data": {"total": 1, "diff": [malformed]}}).encode())),
            ("duplicate key", _Response(b'{"rc":0,"rc":0,"data":null}')),
        )
        for label, response in cases:
            with self.subTest(label=label), _patched_responses(response):
                result = refresh_capital_flow(self.config, DAY)
                self.assertFalse(result["available"])
                self.assertEqual(result["status"], "unavailable")
        self.assertFalse((self.config.market_intelligence_dir / "capital_flow").exists())

    def test_repeated_evidence_reuses_and_changed_rows_append_revision(self):
        rows = [_flow_row(1), _flow_row(2)]
        with _patched_responses(_flow_response(rows)):
            first = refresh_capital_flow(self.config, DAY)
        with _patched_responses(_flow_response(rows)):
            reused = refresh_capital_flow(self.config, DAY)
        changed = [_flow_row(1), _flow_row(2)]
        changed[0]["f62"] = 9_999.0
        with _patched_responses(_flow_response(changed)):
            second = refresh_capital_flow(self.config, DAY)

        self.assertEqual(first["revision"], 1)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["revision_id"], first["revision_id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["supersedes"], first["revision_id"])
        listed = CapitalFlowStore(self.config).list(
            CapitalFlowQuery(trade_date=DAY, include_revisions=True)
        )
        self.assertEqual(len(listed["revisions"]), 2)

    def test_filters_sort_nulls_and_freshness_are_bounded(self):
        rows = [
            _flow_row(1, name="Alpha", main=-20.0),
            _flow_row(2, name="Beta", main=30.0),
            _flow_row(3, name="Gamma", main="-"),
        ]
        with _patched_responses(_flow_response(rows)):
            refresh_capital_flow(self.config, DAY)
        store = CapitalFlowStore(self.config)
        selected = store.list(
            CapitalFlowQuery(sort="main_net_inflow", direction="desc", limit=3)
        )
        self.assertEqual([item["name"] for item in selected["flows"]], ["Beta", "Alpha", "Gamma"])
        empty = store.list(CapitalFlowQuery(q="not-present", limit=1))
        self.assertTrue(empty["available"])
        self.assertEqual(empty["flows"], [])
        self.assertEqual(empty["summary"]["matched_flow_count"], 0)
        current = store.list(completed_session_cutoff=DAY)
        stale = store.list(completed_session_cutoff=DAY + timedelta(days=3))
        provisional = store.list(
            CapitalFlowQuery(trade_date=DAY), completed_session_cutoff=DAY - timedelta(days=1)
        )
        self.assertEqual(current["status"], "current")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(provisional["status"], "provisional")

    def test_tampered_revision_fails_closed(self):
        with _patched_responses(_flow_response([_flow_row(1)])):
            refresh_capital_flow(self.config, DAY)
        revision_path = (
            self.config.market_intelligence_dir
            / "capital_flow"
            / DAY.isoformat()
            / "revision_00000001.json"
        )
        value = json.loads(revision_path.read_text(encoding="utf-8"))
        value["summary"]["positive_main_count"] += 1
        revision_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "summary field positive_main_count"):
            CapitalFlowStore(self.config).list(CapitalFlowQuery(trade_date=DAY))

    def test_failed_refresh_keeps_previous_complete_revision(self):
        with _patched_responses(_flow_response([_flow_row(1)])):
            first = refresh_capital_flow(self.config, DAY)
        with _patched_responses(_Response(b"not-json")):
            failed = refresh_capital_flow(self.config, DAY)
        self.assertFalse(failed["available"])
        listed = CapitalFlowStore(self.config).list(
            CapitalFlowQuery(trade_date=DAY, include_revisions=True)
        )
        self.assertEqual(listed["revision_id"], first["revision_id"])
        self.assertEqual(len(listed["revisions"]), 1)

    def test_unavailable_reads_and_invalid_queries_are_explicit(self):
        result = CapitalFlowStore(self.config).list(CapitalFlowQuery())
        self.assertFalse(result["available"])
        self.assertEqual(result["summary"]["positive_main_share"], None)
        for query in (
            CapitalFlowQuery(q=""),
            CapitalFlowQuery(sort="unknown"),
            CapitalFlowQuery(direction="sideways"),
            CapitalFlowQuery(limit=0),
            CapitalFlowQuery(limit=501),
            CapitalFlowQuery(include_revisions=1),
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                CapitalFlowStore(self.config).list(query)


def _flow_row(
    index: int,
    *,
    name: str | None = None,
    main: float | str = 100.0,
) -> dict[str, object]:
    return {
        "f12": f"BK{index:04d}",
        "f13": 90,
        "f14": name or f"Board {index}",
        "f2": 1_000.0 + index,
        "f3": 1.5 if index % 2 else -1.5,
        "f62": main,
        "f184": 3.0,
        "f66": 60.0,
        "f69": 1.8,
        "f72": 40.0,
        "f75": 1.2,
        "f78": -30.0,
        "f81": -0.9,
        "f84": -70.0,
        "f87": -2.1,
        "f124": _quote_timestamp(DAY),
    }


def _flow_response(rows: list[dict[str, object]], *, total: int | None = None) -> _Response:
    return _Response(
        json.dumps(
            {
                "rc": 0,
                "data": {"total": len(rows) if total is None else total, "diff": rows},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _quote_timestamp(on_date: date) -> int:
    return int(
        datetime.combine(on_date, time(15, 40), tzinfo=CHINA_TIMEZONE).timestamp()
    )


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
        "ai_trade.data.capital_flow._open_request", side_effect=list(responses)
    ) as opened:
        yield opened


if __name__ == "__main__":
    unittest.main()
