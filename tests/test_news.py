from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_trade.data.news import (
    NewsProviderError,
    NewsQuery,
    NewsStore,
    _request_json,
    refresh_news,
)
from ai_trade.models import Instrument


def _config(root: Path):
    return SimpleNamespace(
        raw={
            "data": {
                "market_close_time": "15:30",
                "timeout_seconds": 2,
                "max_attempts": 1,
                "proxy_mode": "direct",
            }
        },
        instruments=[Instrument("600000", "Test", "SH", "equity")],
        news_dir=root / "news",
    )


def _news_payload(url: str = "https://finance.eastmoney.com/a/1.html"):
    return {
        "rc": 1,
        "LivesList": [
            {
                "id": "202607171",
                "newsid": "202607171",
                "title": "Company profit growth upgraded",
                "digest": "profit growth upgraded",
                "showtime": "2026-07-17 14:30:00",
                "url_unique": url,
            }
        ],
    }


def _announcement_payload():
    return {
        "data": {
            "list": [
                {
                    "art_code": "AN202607170001",
                    "codes": [{"stock_code": "600000"}],
                    "columns": [{"column_name": "Risk warning"}],
                    "display_time": "2026-07-17 15:10:00:123",
                    "notice_date": "2026-07-18 00:00:00",
                    "title": "Company risk warning",
                    "title_ch": "Company risk warning",
                }
            ],
            "page_index": 1,
            "page_size": 50,
            "total_hits": 1,
        }
    }


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _maximum: int) -> bytes:
        return self.content


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_refresh_merges_news_and_announcements_with_revision_evidence(self):
        news = (_news_payload(), "a" * 64, 100)
        announcements = (_announcement_payload(), "b" * 64, 100)
        as_of = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        with patch("ai_trade.data.news._download_news", return_value=news), patch(
            "ai_trade.data.news._download_announcements", return_value=announcements
        ):
            first = refresh_news(
                self.config,
                trade_date=date(2026, 7, 17),
                as_of=as_of,
            )
        self.assertTrue(first["available"])
        self.assertEqual(first["summary"]["news_count"], 1)
        self.assertEqual(first["summary"]["announcement_count"], 1)
        self.assertEqual(first["revision"], 1)
        self.assertFalse(first["authority"]["execution_authorized"])
        self.assertEqual(first["source"]["response_count"], 2)
        self.assertEqual(
            {item["kind"] for item in first["source"]["responses"]},
            {"news", "announcement"},
        )
        self.assertEqual(
            {item["kind"] for item in first["records"]},
            {"news", "announcement"},
        )
        self.assertTrue(
            all(item["sentiment_annotation"]["method"] == "lexicon-v1" for item in first["records"])
        )

        with patch("ai_trade.data.news._download_news", return_value=news), patch(
            "ai_trade.data.news._download_announcements", return_value=announcements
        ):
            second = refresh_news(
                self.config,
                trade_date=date(2026, 7, 17),
                as_of=as_of,
            )
        self.assertTrue(second["reused"])
        self.assertEqual(second["revision"], 1)

        visible = NewsStore(self.config).list(
            NewsQuery(trade_date=date(2026, 7, 17), kind="announcement", symbol="600000")
        )
        self.assertEqual(len(visible["records"]), 1)
        self.assertEqual(visible["records"][0]["kind"], "announcement")

    def test_partial_source_failure_remains_explicit(self):
        with patch(
            "ai_trade.data.news._download_news",
            side_effect=NewsProviderError("news down"),
        ), patch(
            "ai_trade.data.news._download_announcements",
            return_value=(_announcement_payload(), "b" * 64, 100),
        ):
            result = refresh_news(
                self.config,
                trade_date=date(2026, 7, 17),
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["records"][0]["kind"], "announcement")

    def test_jsonp_parser_and_source_url_allowlist_fail_closed(self):
        body = b"var ajaxResult=" + json.dumps(_news_payload()).encode("utf-8") + b";"
        with patch("ai_trade.data.news._open_request", return_value=_Response(body)):
            parsed, digest, size = _request_json(
                self.config,
                "https://newsapi.eastmoney.com/example",
                jsonp=True,
            )
        self.assertEqual(parsed["rc"], 1)
        self.assertEqual(len(digest), 64)
        self.assertEqual(size, len(body))

        with patch(
            "ai_trade.data.news._download_news",
            return_value=(_news_payload("https://example.com/bad"), "a" * 64, 100),
        ), patch(
            "ai_trade.data.news._download_announcements",
            side_effect=NewsProviderError("announcement down"),
        ):
            unavailable = refresh_news(
                self.config,
                trade_date=date(2026, 7, 17),
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertFalse(unavailable["available"])


if __name__ == "__main__":
    unittest.main()
