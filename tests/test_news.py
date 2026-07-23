from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
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

    def test_cross_source_dedup_heat_and_item_revision_are_auditable(self):
        reference_records = [
            {
                "source_id": "tushare:sina:reference-1",
                "title": "Company profit growth upgraded!",
                "summary": "Independent editorial copy",
                "published_at": "2026-07-17T14:35:00+08:00",
                "editorial_source": "sina",
                "channels": "finance",
            }
        ]
        reference_responses = [
            {
                "provider": "tushare",
                "api_name": "news",
                "editorial_source": "sina",
                "response_sha256": "c" * 64,
                "response_bytes": 120,
                "row_count": 1,
            }
        ]
        first_news = _news_payload()
        second_news = _news_payload()
        second_news["LivesList"][0]["digest"] = "profit growth upgraded and revised"
        common = {
            "trade_date": date(2026, 7, 17),
            "as_of": datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        }
        with (
            patch("ai_trade.data.news.token_configured", return_value=True),
            patch(
                "ai_trade.data.news.fetch_news_reference",
                return_value=(reference_records, reference_responses, []),
            ),
            patch(
                "ai_trade.data.news._download_news",
                return_value=(first_news, "a" * 64, 100),
            ),
            patch(
                "ai_trade.data.news._download_announcements",
                return_value=(_announcement_payload(), "b" * 64, 100),
            ),
        ):
            first = refresh_news(self.config, **common)
        clustered = next(item for item in first["records"] if item["kind"] == "news")
        self.assertEqual(clustered["source_count"], 2)
        self.assertEqual(clustered["transport_provider_count"], 2)
        self.assertEqual(clustered["duplicate_count"], 1)
        self.assertEqual(clustered["heat"]["sentiment_coverage"], "UNAVAILABLE")
        self.assertEqual(clustered["item_revision"], 1)
        self.assertEqual(first["summary"]["multi_transport_cluster_count"], 1)

        with (
            patch("ai_trade.data.news.token_configured", return_value=True),
            patch(
                "ai_trade.data.news.fetch_news_reference",
                return_value=(reference_records, reference_responses, []),
            ),
            patch(
                "ai_trade.data.news._download_news",
                return_value=(second_news, "d" * 64, 110),
            ),
            patch(
                "ai_trade.data.news._download_announcements",
                return_value=(_announcement_payload(), "b" * 64, 100),
            ),
        ):
            second = refresh_news(self.config, **common)
        revised = next(item for item in second["records"] if item["kind"] == "news")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(revised["item_revision"], 2)
        self.assertEqual(revised["revision_status"], "revised")
        self.assertEqual(
            revised["supersedes_content_sha256"], clustered["content_sha256"]
        )

    def test_same_transport_editorial_feeds_do_not_inflate_source_breadth(self):
        reference_records = [
            {
                "source_id": f"tushare:{source}:reference-{index}",
                "title": "Shared provider headline",
                "summary": f"Editorial copy {index}",
                "published_at": f"2026-07-17T14:{30 + index:02d}:00+08:00",
                "editorial_source": source,
                "channels": "finance",
            }
            for index, source in enumerate(("sina", "wallstreetcn", "10jqka"))
        ]
        with (
            patch("ai_trade.data.news.token_configured", return_value=True),
            patch(
                "ai_trade.data.news.fetch_news_reference",
                return_value=(reference_records, [], []),
            ),
            patch(
                "ai_trade.data.news._download_news",
                side_effect=NewsProviderError("news down"),
            ),
            patch(
                "ai_trade.data.news._download_announcements",
                side_effect=NewsProviderError("announcement down"),
            ),
        ):
            result = refresh_news(
                self.config,
                trade_date=date(2026, 7, 17),
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        item = result["records"][0]
        self.assertEqual(item["source_count"], 3)
        self.assertEqual(item["transport_provider_count"], 1)
        self.assertEqual(result["summary"]["multi_transport_cluster_count"], 0)
        self.assertAlmostEqual(
            item["heat"]["components"]["source_breadth"], 1 / 3, places=8
        )

    def test_cluster_identity_survives_transport_source_changes(self):
        common = {
            "trade_date": date(2026, 7, 17),
            "as_of": datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        }
        reference_records = [
            {
                "source_id": "tushare:sina:first",
                "title": "Company profit growth upgraded!",
                "summary": "Tushare-only copy",
                "published_at": "2026-07-17T14:35:00+08:00",
                "editorial_source": "sina",
                "channels": "finance",
            }
        ]
        with (
            patch("ai_trade.data.news.token_configured", return_value=True),
            patch(
                "ai_trade.data.news.fetch_news_reference",
                return_value=(reference_records, [], []),
            ),
            patch(
                "ai_trade.data.news._download_news",
                side_effect=NewsProviderError("news down"),
            ),
            patch(
                "ai_trade.data.news._download_announcements",
                side_effect=NewsProviderError("announcement down"),
            ),
        ):
            first = refresh_news(self.config, **common)

        with (
            patch("ai_trade.data.news.token_configured", return_value=True),
            patch(
                "ai_trade.data.news.fetch_news_reference",
                return_value=(reference_records, [], []),
            ),
            patch(
                "ai_trade.data.news._download_news",
                return_value=(_news_payload(), "a" * 64, 100),
            ),
            patch(
                "ai_trade.data.news._download_announcements",
                side_effect=NewsProviderError("announcement down"),
            ),
        ):
            second = refresh_news(self.config, **common)

        first_item = first["records"][0]
        second_item = second["records"][0]
        self.assertEqual(second_item["item_id"], first_item["item_id"])
        self.assertEqual(second_item["item_revision"], 2)
        self.assertEqual(
            second_item["supersedes_content_sha256"], first_item["content_sha256"]
        )

    def test_enriched_refresh_can_extend_a_legacy_news_chain(self):
        legacy_draft = {
            "schema_version": 1,
            "dataset": "news",
            "available": True,
            "status": "current",
            "trade_date": "2026-07-17",
            "retrieved_at": "2026-07-20T08:00:00+00:00",
            "source": {
                "provider": "eastmoney",
                "news_endpoint": "https://newsapi.eastmoney.com/example",
                "announcement_endpoint": "https://np-anotice-stock.eastmoney.com/example",
                "responses": [],
                "response_count": 0,
                "response_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e5f2d9d33e9f0a5f5b6f9c4",
            },
            "records": [
                {
                    "item_id": "item_"
                    + sha256(b"eastmoney:news:202607171").hexdigest()[:32],
                    "kind": "news",
                    "symbol": None,
                    "title": "Legacy headline",
                    "summary": "Legacy summary",
                    "published_at": "2026-07-17T14:30:00+08:00",
                    "url": "https://finance.eastmoney.com/a/1.html",
                    "sentiment_annotation": {
                        "method": "lexicon-v1",
                        "label": "neutral",
                        "score": 0.0,
                        "positive_hits": 0,
                        "negative_hits": 0,
                        "confidence": "none",
                    },
                }
            ],
            "summary": {},
            "authority": {"research_only": True, "execution_authorized": False},
            "errors": [],
            "warnings": [],
        }
        legacy_draft["source"]["response_sha256"] = sha256(b"[]").hexdigest()
        NewsStore(self.config).publish(legacy_draft)

        with (
            patch("ai_trade.data.news.token_configured", return_value=False),
            patch(
                "ai_trade.data.news._download_news",
                return_value=(_news_payload(), "a" * 64, 100),
            ),
            patch(
                "ai_trade.data.news._download_announcements",
                return_value=(_announcement_payload(), "b" * 64, 100),
            ),
        ):
            result = refresh_news(
                self.config,
                trade_date=date(2026, 7, 17),
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(result["revision"], 2)
        self.assertTrue(all(item["item_revision"] == 1 for item in result["records"]))


if __name__ == "__main__":
    unittest.main()
