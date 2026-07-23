"""Validated Eastmoney news and announcement evidence.

The provider exposes two public JSON/JSONP feeds: a closing-news stream and a
per-security announcement list.  This module normalizes both into one
immutable, locally queryable evidence dataset.  A small versioned lexical
classifier is included as a transparent *research annotation*; it is not an
assistant sentiment view and never authorizes a strategy or order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import time as time_module
from typing import Any, Mapping, Sequence
import unicodedata
import urllib.parse
import urllib.request
from uuid import uuid4

from ..config import AppConfig
from ..json_utils import load_unique_json, loads_unique_json
from ..models import Instrument
from .eastmoney import REQUEST_HEADERS, _open_request, _proxy_mode, completed_session_cutoff
from .evidence_io import atomic_create_json, evidence_store_lock
from .tushare import ENDPOINT as TUSHARE_ENDPOINT
from .tushare_reference import fetch_news_reference, token_configured


SCHEMA_VERSION = 1
DATASET = "news"
NEWS_ENDPOINT = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_{page_size}_{page}.html"
ANNOUNCEMENT_ENDPOINT = "https://np-anotice-stock.eastmoney.com/api/security/ann"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REFERENCE_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_RECORDS = 2_000
MAX_PAGE_SIZE = 100
MAX_REVISIONS_PER_DATE = 100
MAX_PERIODS = 5_000
MAX_REVISION_BYTES = 16 * 1024 * 1024
MAX_TEXT = 2_000
MAX_QUERY_TEXT = 100
_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_REVISION_FILE = re.compile(r"revision_(\d{8})\.json\Z")
_REVISION_ID = re.compile(r"news_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_ITEM_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
_SYMBOL = re.compile(r"\d{6}\Z")
_AUTHORITY = {"research_only": True, "execution_authorized": False}
DEDUPLICATION_METHOD = "normalized-title-cluster-v1"
TIME_CALIBRATION_METHOD = "provider-time-to-asia-shanghai-v1"
HEAT_METHOD = "freshness-source-breadth-v1"
ITEM_REVISION_METHOD = "source-identity-content-sha256-v1"
TUSHARE_NEWS_SOURCES = ("sina", "wallstreetcn", "10jqka")
_POSITIVE_TERMS = tuple(
    value
    for value in (
        "\u589e\u957f",
        "\u76c8\u5229",
        "\u4e0a\u8c03",
        "\u56de\u8d2d",
        "\u4e2d\u6807",
        "\u7a81\u7834",
        "\u6536\u76ca",
        "\u63d0\u5347",
        "\u7a33\u6b65",
    )
)
_NEGATIVE_TERMS = tuple(
    value
    for value in (
        "\u4e0b\u6ed1",
        "\u4e8f\u635f",
        "\u51cf\u6301",
        "\u76d1\u7ba1\u8b66\u793a",
        "\u8fdd\u89c4",
        "\u98ce\u9669",
        "\u505c\u724c",
        "\u4e0b\u8c03",
        "\u8bc9\u8bbc",
        "\u8b66\u793a",
    )
)


@dataclass(frozen=True)
class NewsQuery:
    trade_date: date | None = None
    symbol: str | None = None
    kind: str = "all"
    q: str | None = None
    limit: int = 200
    include_revisions: bool = False


class NewsProviderError(RuntimeError):
    """Raised when a news/announcement response fails validation."""


def refresh_news(
    config: AppConfig,
    *,
    trade_date: date | None = None,
    symbols: Sequence[str] | None = None,
    as_of: datetime | None = None,
    limit_per_source: int = 50,
) -> dict[str, Any]:
    """Fetch bounded news and announcement evidence for one local snapshot date."""

    if isinstance(limit_per_source, bool) or not isinstance(limit_per_source, int) or not 1 <= limit_per_source <= MAX_PAGE_SIZE:
        raise ValueError(f"limit_per_source must be between 1 and {MAX_PAGE_SIZE}")
    instruments = {item.symbol: item for item in config.instruments}
    selected_symbols = list(symbols) if symbols is not None else list(instruments)
    if len(selected_symbols) > 50:
        raise ValueError("news refresh accepts at most 50 symbols")
    for symbol in selected_symbols:
        if symbol not in instruments or _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("symbol must be in the configured security master")
    market_close = str(config.raw.get("data", {}).get("market_close_time", "15:30"))
    cutoff = completed_session_cutoff(as_of, market_close)
    china_now = _china_now(as_of)
    snapshot_date = trade_date or cutoff
    if snapshot_date > cutoff:
        raise ValueError("news trade_date must not be after the completed-session cutoff")
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    source_responses: list[dict[str, Any]] = []

    try:
        payload, digest, response_bytes = _download_news(config, limit_per_source)
        source_responses.append(
            {
                "provider": "eastmoney",
                "kind": "news",
                "symbol": None,
                "editorial_source": "eastmoney_feed",
                "response_sha256": digest,
                "response_bytes": response_bytes,
            }
        )
        records.extend(_parse_news_payload(payload, snapshot_date))
    except (NewsProviderError, OSError, ValueError) as exc:
        errors.append({"source": "news", "code": "news_provider_error", "message": str(exc)[:300]})

    for symbol in selected_symbols:
        try:
            payload, digest, response_bytes = _download_announcements(
                config, instruments[symbol], limit_per_source
            )
            source_responses.append(
                {
                    "provider": "eastmoney",
                    "kind": "announcement",
                    "symbol": symbol,
                    "editorial_source": "eastmoney_announcement",
                    "response_sha256": digest,
                    "response_bytes": response_bytes,
                }
            )
            records.extend(
                _parse_announcement_payload(
                    payload, instruments[symbol], snapshot_date
                )
            )
        except (NewsProviderError, OSError, ValueError) as exc:
            errors.append({"source": f"announcement:{symbol}", "code": "announcement_provider_error", "message": str(exc)[:300]})

    use_tushare = token_configured()
    if use_tushare:
        reference_records, reference_responses, reference_errors = (
            fetch_news_reference(
                config,
                snapshot_date=snapshot_date,
                sources=TUSHARE_NEWS_SOURCES,
                limit_per_source=limit_per_source,
            )
        )
        errors.extend(reference_errors)
        for response in reference_responses:
            source_responses.append(
                {
                    **response,
                    "kind": "news",
                    "symbol": None,
                }
            )
        for item in reference_records:
            records.append(
                _normalized_item(
                    item["source_id"],
                    "news",
                    None,
                    item["title"],
                    item["summary"],
                    _parse_timestamp(item["published_at"], "Tushare news time"),
                    "https://tushare.pro/document/2?doc_id=143",
                    item["editorial_source"],
                    transport_provider="tushare",
                    channels=str(item.get("channels") or ""),
                )
            )

    clustered = _deduplicate_records(records, snapshot_date)
    ordered = sorted(
        clustered,
        key=lambda item: (str(item.get("published_at", "")), str(item["item_id"])),
        reverse=True,
    )[:MAX_TOTAL_RECORDS]
    if not ordered:
        return _unavailable(NewsQuery(trade_date=snapshot_date), errors)
    provisional = china_now.date() > cutoff
    record = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": True,
        "status": "provisional" if provisional else ("partial" if errors else "current"),
        "trade_date": snapshot_date.isoformat(),
        "retrieved_at": _now(),
        "source": {
            "provider": "multi_source_news",
            "transport_providers": [
                "eastmoney",
                *(["tushare"] if use_tushare else []),
            ],
            "news_endpoint": NEWS_ENDPOINT,
            "announcement_endpoint": ANNOUNCEMENT_ENDPOINT,
            "tushare_endpoint": TUSHARE_ENDPOINT if use_tushare else None,
            "tushare_editorial_sources": (
                list(TUSHARE_NEWS_SOURCES) if use_tushare else []
            ),
            "responses": sorted(
                source_responses,
                key=_response_sort_key,
            ),
            "response_sha256": _fingerprint(
                sorted(source_responses, key=_response_sort_key)
            ),
            "response_count": len(source_responses),
            "certification": "not_exchange_certified",
            "deduplication_method": DEDUPLICATION_METHOD,
            "time_calibration_method": TIME_CALIBRATION_METHOD,
            "heat_method": HEAT_METHOD,
            "item_revision_method": ITEM_REVISION_METHOD,
        },
        "records": ordered,
        "summary": _summary(ordered),
        "authority": dict(_AUTHORITY),
        "errors": errors,
        "warnings": [
            {
                "code": "not_exchange_certified",
                "message": "Eastmoney news and announcements are third-party evidence and are not exchange-certified disclosures.",
            },
            {
                "code": "lexicon_annotation_only",
                "message": "sentiment_annotation uses transparent lexicon-v1 counts; it is not a market-sentiment model and does not change assistant coverage.",
            },
            {
                "code": (
                    "tushare_news_reference_enabled"
                    if use_tushare
                    else "tushare_news_reference_not_configured"
                ),
                "message": (
                    "Configured Tushare editorial feeds were included through one additional transport boundary."
                    if use_tushare
                    else "Set AI_TRADE_TUSHARE_TOKEN to add bounded Tushare editorial feeds to cross-source clustering."
                ),
            },
        ],
    }
    if provisional:
        record["warnings"].append(
            {
                "code": "before_completed_cutoff",
                "message": "The local clock is before the completed-session cutoff; the snapshot is provisional.",
            }
        )
    return NewsStore(config).publish(record)


class NewsStore:
    """Immutable per-date evidence store; reads perform no network I/O."""

    def __init__(self, config_or_root: AppConfig | str | Path):
        if isinstance(config_or_root, AppConfig) or hasattr(config_or_root, "news_dir"):
            raw_root = getattr(config_or_root, "news_dir", None)
            if raw_root is None:
                raw_root = config_or_root.resolve("state/news")
        else:
            raw_root = Path(config_or_root)
        self.root = Path(raw_root).resolve()

    def list(self, query: NewsQuery | None = None) -> dict[str, Any]:
        selected = query or NewsQuery()
        _validate_query(selected)
        periods = self._periods()
        target = selected.trade_date or (periods[-1] if periods else None)
        if target is None or target not in periods:
            return _unavailable(selected, [])
        chain = self._load_chain(target)
        latest = _clone(chain[-1])
        filtered = [item for item in latest["records"] if _matches(item, selected)]
        latest["records"] = filtered[: selected.limit]
        latest["summary"] = {
            **dict(latest.get("summary") or {}),
            "matched_count": len(filtered),
            "returned_count": len(latest["records"]),
            "truncated": len(filtered) > len(latest["records"]),
        }
        latest["filters"] = _query_payload(selected)
        latest["revisions"] = [_revision_summary(item) for item in chain]
        if not selected.include_revisions:
            latest["revisions"] = latest["revisions"][-1:]
        return latest

    def publish(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        with evidence_store_lock(self.root, "News"):
            return self._publish_unlocked(draft)

    def _publish_unlocked(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        record = _clone(draft)
        if not isinstance(record, dict):
            raise ValueError("news record must be an object")
        _validate_draft(record)
        target = date.fromisoformat(str(record["trade_date"]))
        chain = self._load_chain(target, missing_ok=True)
        previous = chain[-1] if chain else None
        record["records"] = _bind_item_revisions(record["records"], previous)
        record["summary"] = _summary(record["records"])
        _validate_draft(record)
        evidence = _fingerprint(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "trade_date": record["trade_date"],
                "source": record["source"],
                "records": record["records"],
                "errors": record["errors"],
            }
        )
        if chain and chain[-1].get("evidence_fingerprint") == evidence:
            result = _clone(chain[-1])
            result["reused"] = True
            result["revisions"] = [_revision_summary(item) for item in chain]
            return result
        if len(chain) >= MAX_REVISIONS_PER_DATE:
            raise RuntimeError("news revision capacity reached")
        record.update(
            {
                "revision_id": f"news_{uuid4().hex}",
                "revision": len(chain) + 1,
                "reused": False,
                "evidence_fingerprint": evidence,
                "supersedes": previous.get("revision_id") if previous else None,
                "supersedes_fingerprint": previous.get("record_fingerprint") if previous else None,
                "record_fingerprint": None,
            }
        )
        record["revisions"] = [*[_revision_summary(item) for item in chain], _revision_summary(record)]
        record["record_fingerprint"] = _record_fingerprint(record)
        record["revisions"][-1]["record_fingerprint"] = record["record_fingerprint"]
        _validate_record(record, expected_date=target, expected_revision=record["revision"])
        self._atomic_create(record)
        committed = self._load_chain(target)
        result = _clone(committed[-1])
        result["revisions"] = [_revision_summary(item) for item in committed]
        return result

    def _periods(self) -> list[date]:
        if not self.root.exists():
            return []
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError("news root is invalid")
        periods: list[date] = []
        for path in self.root.iterdir():
            if path.is_symlink() or not path.is_dir() or not _DATE_DIRECTORY.fullmatch(path.name):
                raise RuntimeError("news period directory is invalid")
            periods.append(date.fromisoformat(path.name))
            if len(periods) > MAX_PERIODS:
                raise RuntimeError("news store contains too many periods")
        return sorted(periods)

    def _load_chain(self, target: date, *, missing_ok: bool = False) -> list[dict[str, Any]]:
        directory = self.root / target.isoformat()
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("news period is missing")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("news period is invalid")
        paths: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("news revision must be a regular file")
            match = _REVISION_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError("unexpected news revision file")
            paths.append((int(match.group(1)), path))
        paths.sort()
        if len(paths) > MAX_REVISIONS_PER_DATE:
            raise RuntimeError("news period contains too many revisions")
        if [number for number, _ in paths] != list(range(1, len(paths) + 1)):
            raise RuntimeError("news revision sequence is not contiguous")
        chain: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for revision, path in paths:
            value = load_unique_json(path, max_bytes=16 * 1024 * 1024)
            if not isinstance(value, dict):
                raise RuntimeError("news revision must be an object")
            _validate_record(value, expected_date=target, expected_revision=revision)
            if previous is None:
                if value.get("supersedes") is not None or value.get(
                    "supersedes_fingerprint"
                ) is not None:
                    raise RuntimeError("first news revision has a parent")
            elif (
                value.get("supersedes") != previous.get("revision_id")
                or value.get("supersedes_fingerprint") != previous.get("record_fingerprint")
            ):
                raise RuntimeError("news supersedes chain is invalid")
            expected_history = [*[_revision_summary(item) for item in chain], _revision_summary(value)]
            if value.get("revisions") != expected_history:
                raise RuntimeError("news embedded revision history is invalid")
            chain.append(value)
            previous = value
        return chain

    def _atomic_create(self, record: Mapping[str, Any]) -> None:
        target = date.fromisoformat(str(record["trade_date"]))
        directory = self.root / target.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"revision_{int(record['revision']):08d}.json"
        if path.exists():
            existing = load_unique_json(path, max_bytes=16 * 1024 * 1024)
            if existing != record:
                raise RuntimeError("news revision already exists with different content")
            return
        atomic_create_json(
            self.root,
            path,
            record,
            label="news",
            maximum_bytes=MAX_REVISION_BYTES,
        )


def _download_news(config: AppConfig, page_size: int) -> tuple[dict[str, Any], str, int]:
    url = NEWS_ENDPOINT.format(page_size=page_size, page=1)
    return _request_json(config, url, jsonp=True)


def _download_announcements(
    config: AppConfig, instrument: Instrument, page_size: int
) -> tuple[dict[str, Any], str, int]:
    params = {
        "sr": "-1",
        "page_size": str(page_size),
        "page_index": "1",
        "ann_type": "A",
        "client_source": "web",
        "stock_list": instrument.symbol,
    }
    url = f"{ANNOUNCEMENT_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return _request_json(config, url, jsonp=False)


def _request_json(config: AppConfig, url: str, *, jsonp: bool) -> tuple[dict[str, Any], str, int]:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS, method="GET")
    data_config = config.raw.get("data", {})
    timeout = int(data_config.get("timeout_seconds", 20))
    attempts = min(3, max(1, int(data_config.get("max_attempts", 3))))
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            with _open_request(request, timeout, _proxy_mode(config)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise NewsProviderError("news response is too large")
            text = raw.decode("utf-8")
            if jsonp:
                match = re.fullmatch(r"\s*var\s+ajaxResult\s*=\s*(\{.*\})\s*;?\s*", text, re.DOTALL)
                if match is None:
                    raise NewsProviderError("news JSONP envelope is invalid")
                text = match.group(1)
            value = loads_unique_json(text)
            if not isinstance(value, dict):
                raise NewsProviderError("news response is not an object")
            return value, sha256(raw).hexdigest(), len(raw)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                time_module.sleep(min(2.0, 0.25 * (2**attempt)))
    raise NewsProviderError("news request failed: " + " | ".join(errors))


def _parse_news_payload(payload: Mapping[str, Any], snapshot_date: date) -> list[dict[str, Any]]:
    if payload.get("rc") not in (None, 1):
        raise NewsProviderError(f"news returned rc={payload.get('rc')}")
    rows = payload.get("LivesList")
    if not isinstance(rows, list):
        raise NewsProviderError("news LivesList is missing")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise NewsProviderError(f"news row {index} is invalid")
        item_id = _text(row.get("newsid") or row.get("id"), "news id", 100)
        title = _text(row.get("title"), "news title", MAX_TEXT)
        summary = _text(row.get("digest") or row.get("simdigest") or "", "news summary", MAX_TEXT)
        published = _parse_timestamp(row.get("showtime") or row.get("ordertime"), "news time")
        if published.date() > snapshot_date:
            continue
        url = _source_url(row.get("url_unique") or row.get("url_w") or row.get("url_m"))
        source_name = str(
            row.get("mediaName")
            or row.get("source")
            or row.get("source_name")
            or "Eastmoney feed"
        ).strip()[:100]
        result.append(
            _normalized_item(
                f"eastmoney:news:{item_id}",
                "news",
                None,
                title,
                summary,
                published,
                url,
                source_name,
                transport_provider="eastmoney",
            )
        )
    return result


def _parse_announcement_payload(
    payload: Mapping[str, Any], instrument: Instrument, snapshot_date: date
) -> list[dict[str, Any]]:
    if payload.get("data") is None:
        raise NewsProviderError("announcement data is missing")
    data = payload["data"]
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise NewsProviderError("announcement list is invalid")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(data["list"]):
        if not isinstance(row, dict):
            raise NewsProviderError(f"announcement row {index} is invalid")
        item_id = _text(row.get("art_code"), "announcement id", 100)
        codes = row.get("codes")
        if not isinstance(codes, list):
            raise NewsProviderError("announcement codes are invalid")
        symbols = [
            str(item.get("stock_code"))
            for item in codes
            if isinstance(item, dict) and _SYMBOL.fullmatch(str(item.get("stock_code", "")))
        ]
        if instrument.symbol not in symbols:
            raise NewsProviderError("announcement symbol binding is invalid")
        published = _parse_timestamp(row.get("display_time") or row.get("notice_date"), "announcement time")
        if published.date() > snapshot_date:
            continue
        title = _text(row.get("title_ch") or row.get("title"), "announcement title", MAX_TEXT)
        columns = row.get("columns")
        category = ""
        if isinstance(columns, list) and columns and isinstance(columns[0], dict):
            category = str(columns[0].get("column_name") or "")[:100]
        url = _source_url(
            f"https://data.eastmoney.com/notices/detail/{instrument.symbol}/{item_id}.html"
        )
        item = _normalized_item(
            f"eastmoney:announcement:{instrument.symbol}:{item_id}",
            "announcement",
            instrument.symbol,
            title,
            category,
            published,
            url,
            "Eastmoney announcement",
            transport_provider="eastmoney",
        )
        item["category"] = category
        result.append(item)
    return result


def _normalized_item(
    source_id: str,
    kind: str,
    symbol: str | None,
    title: str,
    summary: str,
    published: datetime,
    url: str,
    source_name: str,
    *,
    transport_provider: str,
    channels: str = "",
) -> dict[str, Any]:
    annotation = _sentiment_annotation(f"{title} {summary}")
    return {
        "item_id": "item_" + sha256(source_id.encode("utf-8")).hexdigest()[:32],
        "source_id": source_id,
        "kind": kind,
        "symbol": symbol,
        "title": title,
        "summary": summary,
        "published_at": published.isoformat(),
        "raw_published_at": published.isoformat(),
        "url": url,
        "source": source_name,
        "editorial_source": source_name,
        "transport_provider": transport_provider,
        "channels": channels,
        "sentiment_annotation": annotation,
    }


def _deduplicate_records(
    records: Sequence[Mapping[str, Any]], snapshot_date: date
) -> list[dict[str, Any]]:
    clusters: dict[str, list[dict[str, Any]]] = {}
    for source in records:
        item = _clone(source)
        if not isinstance(item, dict):
            raise NewsProviderError("normalized news item is invalid")
        title_key = _canonical_title(str(item.get("title") or ""))
        if not title_key:
            raise NewsProviderError("news title has no canonical content")
        if item.get("kind") == "announcement":
            cluster_key = f"announcement:{item.get('source_id')}"
        else:
            cluster_key = f"news:{title_key}"
        clusters.setdefault(cluster_key, []).append(item)

    result: list[dict[str, Any]] = []
    reference_time = datetime.combine(
        snapshot_date,
        datetime.max.time().replace(microsecond=0),
        tzinfo=timezone(timedelta(hours=8)),
    )
    for cluster_key, members in clusters.items():
        members.sort(
            key=lambda item: (
                0 if item.get("transport_provider") == "eastmoney" else 1,
                str(item.get("source_id") or ""),
            )
        )
        canonical = dict(members[0])
        timestamps = [
            _parse_timestamp(item.get("published_at"), "published_at")
            for item in members
        ]
        earliest = min(timestamps)
        latest = max(timestamps)
        sources = sorted(
            {
                (
                    str(item.get("transport_provider") or "unknown"),
                    str(item.get("editorial_source") or item.get("source") or "unknown"),
                )
                for item in members
            }
        )
        transport_providers = sorted({provider for provider, _ in sources})
        source_ids = sorted({str(item.get("source_id") or "") for item in members})
        canonical["item_id"] = (
            "item_" + sha256(cluster_key.encode("utf-8")).hexdigest()[:32]
        )
        canonical["source_item_ids"] = source_ids
        canonical["sources"] = [
            {"transport_provider": provider, "editorial_source": editorial}
            for provider, editorial in sources
        ]
        canonical["source_count"] = len(sources)
        canonical["transport_provider_count"] = len(transport_providers)
        canonical["duplicate_count"] = len(members) - 1
        canonical["source"] = ", ".join(editorial for _, editorial in sources)
        canonical["published_at"] = earliest.isoformat()
        canonical["deduplication"] = {
            "method": DEDUPLICATION_METHOD,
            "cluster_key_sha256": sha256(cluster_key.encode("utf-8")).hexdigest(),
            "member_count": len(members),
            "exact_normalized_title": True,
        }
        spread_seconds = int((latest - earliest).total_seconds())
        canonical["time_calibration"] = {
            "method": TIME_CALIBRATION_METHOD,
            "timezone": "Asia/Shanghai",
            "earliest_published_at": earliest.isoformat(),
            "latest_published_at": latest.isoformat(),
            "source_spread_seconds": spread_seconds,
            "status": "aligned" if spread_seconds <= 21_600 else "source_conflict",
        }
        age_hours = max(0.0, (reference_time - latest).total_seconds() / 3_600)
        freshness = 0.5 ** (age_hours / 24.0)
        source_breadth = min(1.0, len(transport_providers) / 3.0)
        score = round(100.0 * (0.85 * freshness + 0.15 * source_breadth), 2)
        canonical["heat"] = {
            "method": HEAT_METHOD,
            "score": score,
            "components": {
                "age_hours": round(age_hours, 4),
                "freshness_decay": round(freshness, 8),
                "source_breadth": round(source_breadth, 8),
            },
            "formula": "100 * (0.85 * 0.5^(age_hours/24) + 0.15 * min(transport_provider_count/3, 1))",
            "uses_market_direction": False,
            "sentiment_coverage": "UNAVAILABLE",
        }
        canonical["sentiment_annotation"] = _sentiment_annotation(
            f"{canonical['title']} {canonical['summary']}"
        )
        canonical["item_revision"] = 1
        canonical["revision_status"] = "original"
        canonical["supersedes_content_sha256"] = None
        canonical["content_sha256"] = _item_content_fingerprint(canonical)
        result.append(canonical)
    return result


def _canonical_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized if character.isalnum()
    )


def _item_content_fingerprint(value: Mapping[str, Any]) -> str:
    payload = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "content_sha256",
            "item_revision",
            "revision_status",
            "supersedes_content_sha256",
        }
    }
    return _fingerprint(payload)


def _bind_item_revisions(
    records: Sequence[Mapping[str, Any]], previous: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    previous_by_id = {
        str(item.get("item_id")): item
        for item in (previous.get("records", []) if isinstance(previous, Mapping) else [])
        if isinstance(item, dict)
    }
    result: list[dict[str, Any]] = []
    for source in records:
        item = dict(source)
        prior = previous_by_id.get(str(item.get("item_id")))
        if prior is None:
            item["item_revision"] = 1
            item["revision_status"] = "original"
            item["supersedes_content_sha256"] = None
        elif not isinstance(prior.get("content_sha256"), str):
            # Item-level lineage starts with the enriched v0.17 evidence contract.
            item["item_revision"] = 1
            item["revision_status"] = "original"
            item["supersedes_content_sha256"] = None
        elif prior.get("content_sha256") == item.get("content_sha256"):
            item["item_revision"] = prior.get("item_revision", 1)
            item["revision_status"] = prior.get("revision_status", "original")
            item["supersedes_content_sha256"] = prior.get(
                "supersedes_content_sha256"
            )
        else:
            item["item_revision"] = int(prior.get("item_revision", 1)) + 1
            item["revision_status"] = "revised"
            item["supersedes_content_sha256"] = prior.get("content_sha256")
        result.append(item)
    return result


def _response_sort_key(item: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(item.get("provider") or "eastmoney"),
        str(item.get("kind") or ""),
        str(item.get("symbol") or ""),
        str(item.get("editorial_source") or ""),
        str(item.get("api_name") or ""),
    )


def _sentiment_annotation(text: str) -> dict[str, Any]:
    positive = sum(text.count(term) for term in _POSITIVE_TERMS)
    negative = sum(text.count(term) for term in _NEGATIVE_TERMS)
    total = positive + negative
    raw = (positive - negative) / total if total else 0.0
    score = max(-1.0, min(1.0, raw))
    label = "positive" if score > 0.2 else "negative" if score < -0.2 else "neutral"
    return {
        "method": "lexicon-v1",
        "label": label,
        "score": round(score, 6),
        "positive_hits": positive,
        "negative_hits": negative,
        "confidence": "low" if total else "none",
    }


def _matches(item: Mapping[str, Any], query: NewsQuery) -> bool:
    if query.kind != "all" and item.get("kind") != query.kind:
        return False
    if query.symbol is not None and item.get("symbol") not in {None, query.symbol}:
        return False
    if query.q:
        needle = query.q.casefold()
        if needle not in str(item.get("title", "")).casefold() and needle not in str(item.get("summary", "")).casefold():
            return False
    return True


def _validate_draft(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("dataset") != DATASET:
        raise RuntimeError("news record schema is invalid")
    try:
        date.fromisoformat(str(value["trade_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("news trade_date is invalid") from exc
    source = value.get("source")
    if not isinstance(source, dict) or source.get("provider") not in {
        "eastmoney",
        "multi_source_news",
    }:
        raise RuntimeError("news source metadata is invalid")
    responses = source.get("responses")
    if not isinstance(responses, list) or len(responses) > 60:
        raise RuntimeError("news response metadata is invalid")
    normalized_responses: list[dict[str, Any]] = []
    response_keys: set[tuple[str, ...]] = set()
    for item in responses:
        if not isinstance(item, dict):
            raise RuntimeError("news response metadata is invalid")
        kind = item.get("kind")
        if kind not in {"news", "announcement"}:
            raise RuntimeError("news response kind is invalid")
        symbol = item.get("symbol")
        if symbol is not None and _SYMBOL.fullmatch(str(symbol)) is None:
            raise RuntimeError("news response symbol is invalid")
        digest = str(item.get("response_sha256", ""))
        if _FINGERPRINT.fullmatch(digest) is None:
            raise RuntimeError("news response fingerprint is invalid")
        response_bytes = item.get("response_bytes")
        response_provider = str(item.get("provider") or "eastmoney")
        maximum_bytes = (
            MAX_REFERENCE_RESPONSE_BYTES
            if response_provider == "tushare"
            else MAX_RESPONSE_BYTES
        )
        if (
            isinstance(response_bytes, bool)
            or not isinstance(response_bytes, int)
            or not 0 <= response_bytes <= maximum_bytes
        ):
            raise RuntimeError("news response size is invalid")
        key = _response_sort_key(item)
        if key in response_keys:
            raise RuntimeError("news response metadata is duplicated")
        response_keys.add(key)
        normalized_responses.append(dict(item))
    normalized_responses.sort(key=_response_sort_key)
    if source.get("response_count") != len(normalized_responses):
        raise RuntimeError("news response count is invalid")
    if source.get("response_sha256") != _fingerprint(normalized_responses):
        raise RuntimeError("news response aggregate fingerprint is invalid")
    for endpoint_key in ("news_endpoint", "announcement_endpoint"):
        _source_url(source.get(endpoint_key))
    if source.get("tushare_endpoint") is not None:
        _source_url(source.get("tushare_endpoint"))
    records = value.get("records")
    if not isinstance(records, list) or len(records) > MAX_TOTAL_RECORDS:
        raise RuntimeError("news records are invalid")
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise RuntimeError("news item is invalid")
        item_id = str(item.get("item_id", ""))
        if _ITEM_ID.fullmatch(item_id) is None or item_id in seen:
            raise RuntimeError("news item id is invalid or duplicated")
        seen.add(item_id)
        if item.get("kind") not in {"news", "announcement"}:
            raise RuntimeError("news item kind is invalid")
        _text(item.get("title"), "news title", MAX_TEXT)
        _text(item.get("summary"), "news summary", MAX_TEXT)
        _parse_timestamp(item.get("published_at"), "published_at")
        _source_url(item.get("url"))
        if item.get("symbol") is not None and _SYMBOL.fullmatch(str(item["symbol"])) is None:
            raise RuntimeError("news item symbol is invalid")
        if source.get("provider") == "multi_source_news":
            _validate_enriched_item(
                item, snapshot_date=date.fromisoformat(str(value["trade_date"]))
            )


def _validate_record(value: Mapping[str, Any], *, expected_date: date, expected_revision: int) -> None:
    _validate_draft(value)
    if value.get("trade_date") != expected_date.isoformat() or value.get("revision") != expected_revision:
        raise RuntimeError("news revision identity is invalid")
    if _REVISION_ID.fullmatch(str(value.get("revision_id", ""))) is None:
        raise RuntimeError("news revision id is invalid")
    for key in ("evidence_fingerprint", "record_fingerprint"):
        if _FINGERPRINT.fullmatch(str(value.get(key, ""))) is None:
            raise RuntimeError(f"news {key} is invalid")
    if value.get("record_fingerprint") != _record_fingerprint(value):
        raise RuntimeError("news record fingerprint does not match content")


def _validate_enriched_item(item: Mapping[str, Any], *, snapshot_date: date) -> None:
    source_id = item.get("source_id")
    if (
        not isinstance(source_id, str)
        or not 1 <= len(source_id) <= 200
        or any(ord(character) < 32 for character in source_id)
    ):
        raise RuntimeError("news source identity is invalid")
    source_ids = item.get("source_item_ids")
    sources = item.get("sources")
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or len(source_ids) > 100
        or source_ids != sorted(set(source_ids))
        or source_id not in source_ids
        or not isinstance(sources, list)
        or not sources
        or len(sources) > 100
    ):
        raise RuntimeError("news source cluster is invalid")
    normalized_sources: set[tuple[str, str]] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise RuntimeError("news clustered source is invalid")
        provider = source.get("transport_provider")
        editorial = source.get("editorial_source")
        if provider not in {"eastmoney", "tushare"} or not isinstance(
            editorial, str
        ) or not editorial:
            raise RuntimeError("news clustered source is invalid")
        normalized_sources.add((provider, editorial))
    if len(normalized_sources) != len(sources) or item.get("source_count") != len(
        sources
    ):
        raise RuntimeError("news source count is invalid")
    transport_provider_count = item.get("transport_provider_count")
    if (
        isinstance(transport_provider_count, bool)
        or not isinstance(transport_provider_count, int)
        or transport_provider_count
        != len({provider for provider, _editorial in normalized_sources})
    ):
        raise RuntimeError("news transport provider count is invalid")
    duplicate_count = item.get("duplicate_count")
    if duplicate_count != len(source_ids) - 1:
        raise RuntimeError("news duplicate count is invalid")
    deduplication = item.get("deduplication")
    cluster_key = (
        f"announcement:{source_id}"
        if item.get("kind") == "announcement"
        else f"news:{_canonical_title(str(item.get('title') or ''))}"
    )
    if (
        not isinstance(deduplication, dict)
        or deduplication.get("method") != DEDUPLICATION_METHOD
        or deduplication.get("member_count") != len(source_ids)
        or deduplication.get("exact_normalized_title") is not True
        or deduplication.get("cluster_key_sha256")
        != sha256(cluster_key.encode("utf-8")).hexdigest()
    ):
        raise RuntimeError("news deduplication evidence is invalid")
    if item.get("item_id") != (
        "item_" + sha256(cluster_key.encode("utf-8")).hexdigest()[:32]
    ):
        raise RuntimeError("news cluster identity is invalid")
    published = _parse_timestamp(item.get("published_at"), "published_at")
    raw_published = _parse_timestamp(
        item.get("raw_published_at"), "raw_published_at"
    )
    calibration = item.get("time_calibration")
    if (
        not isinstance(calibration, dict)
        or calibration.get("method") != TIME_CALIBRATION_METHOD
        or calibration.get("timezone") != "Asia/Shanghai"
        or calibration.get("status") not in {"aligned", "source_conflict"}
    ):
        raise RuntimeError("news time calibration is invalid")
    earliest = _parse_timestamp(
        calibration.get("earliest_published_at"), "earliest_published_at"
    )
    latest = _parse_timestamp(
        calibration.get("latest_published_at"), "latest_published_at"
    )
    spread = calibration.get("source_spread_seconds")
    if (
        published != earliest
        or not earliest <= raw_published <= latest
        or latest < earliest
        or isinstance(spread, bool)
        or not isinstance(spread, int)
        or spread != int((latest - earliest).total_seconds())
        or calibration.get("status")
        != ("aligned" if spread <= 21_600 else "source_conflict")
        or latest.date() > snapshot_date
    ):
        raise RuntimeError("news calibrated timestamp evidence is invalid")
    heat = item.get("heat")
    components = heat.get("components") if isinstance(heat, dict) else None
    if (
        not isinstance(heat, dict)
        or heat.get("method") != HEAT_METHOD
        or heat.get("uses_market_direction") is not False
        or heat.get("sentiment_coverage") != "UNAVAILABLE"
        or not isinstance(components, dict)
    ):
        raise RuntimeError("news heat evidence is invalid")
    reference_time = datetime.combine(
        snapshot_date,
        datetime.max.time().replace(microsecond=0),
        tzinfo=timezone(timedelta(hours=8)),
    )
    expected_age = max(0.0, (reference_time - latest).total_seconds() / 3_600)
    expected_freshness = 0.5 ** (expected_age / 24.0)
    expected_breadth = min(1.0, transport_provider_count / 3.0)
    expected_score = round(
        100.0 * (0.85 * expected_freshness + 0.15 * expected_breadth), 2
    )
    if (
        heat.get("formula")
        != "100 * (0.85 * 0.5^(age_hours/24) + 0.15 * min(transport_provider_count/3, 1))"
        or heat.get("score") != expected_score
        or components.get("age_hours") != round(expected_age, 4)
        or components.get("freshness_decay") != round(expected_freshness, 8)
        or components.get("source_breadth") != round(expected_breadth, 8)
    ):
        raise RuntimeError("news heat calculation is invalid")
    item_revision = item.get("item_revision")
    revision_status = item.get("revision_status")
    supersedes = item.get("supersedes_content_sha256")
    if (
        isinstance(item_revision, bool)
        or not isinstance(item_revision, int)
        or item_revision < 1
        or revision_status not in {"original", "revised"}
        or (item_revision == 1) is not (revision_status == "original")
        or (item_revision == 1 and supersedes is not None)
        or (
            item_revision > 1
            and _FINGERPRINT.fullmatch(str(supersedes or "")) is None
        )
    ):
        raise RuntimeError("news item revision evidence is invalid")
    if _FINGERPRINT.fullmatch(str(item.get("content_sha256") or "")) is None or item.get(
        "content_sha256"
    ) != _item_content_fingerprint(item):
        raise RuntimeError("news item content fingerprint is invalid")


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise NewsProviderError(f"{label} is not text")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise NewsProviderError(f"{label} is invalid")
    return text


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise NewsProviderError(f"{label} is missing")
    text = value.strip().replace("Z", "+00:00")
    # Eastmoney announcements use a millisecond suffix separated by a colon.
    text = re.sub(r":(\d{3})(?=$|\+|Z$)", r".\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(value[:19], pattern)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            raise NewsProviderError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone(timedelta(hours=8)))


def _source_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        raise NewsProviderError("source URL is invalid")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NewsProviderError("source URL scheme is invalid")
    host = parsed.hostname.lower().rstrip(".")
    if not (
        host == "eastmoney.com"
        or host.endswith(".eastmoney.com")
        or host == "tushare.pro"
        or host.endswith(".tushare.pro")
    ):
        raise NewsProviderError("source URL host is not allowlisted")
    return value


def _validate_query(query: NewsQuery) -> None:
    if query.trade_date is not None and (
        not isinstance(query.trade_date, date) or isinstance(query.trade_date, datetime)
    ):
        raise ValueError("trade_date must be a date")
    if query.symbol is not None and _SYMBOL.fullmatch(query.symbol) is None:
        raise ValueError("symbol must be a six-digit security code")
    if query.kind not in {"all", "news", "announcement"}:
        raise ValueError("kind must be all, news, or announcement")
    if query.q is not None and (not isinstance(query.q, str) or len(query.q) > MAX_QUERY_TEXT):
        raise ValueError("q is too long")
    if isinstance(query.limit, bool) or not isinstance(query.limit, int) or not 1 <= query.limit <= MAX_TOTAL_RECORDS:
        raise ValueError(f"limit must be between 1 and {MAX_TOTAL_RECORDS}")


def _query_payload(query: NewsQuery) -> dict[str, Any]:
    return {
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "symbol": query.symbol,
        "kind": query.kind,
        "q": query.q,
        "limit": query.limit,
        "include_revisions": query.include_revisions,
    }


def _summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "record_count": len(records),
        "raw_record_count": sum(
            int(item.get("duplicate_count", 0)) + 1 for item in records
        ),
        "duplicate_count": sum(int(item.get("duplicate_count", 0)) for item in records),
        "multi_transport_cluster_count": sum(
            int(item.get("transport_provider_count", 1)) > 1 for item in records
        ),
        "editorial_source_count": len(
            {
                source.get("editorial_source")
                for item in records
                for source in (
                    item.get("sources", [])
                    if isinstance(item.get("sources"), list)
                    else []
                )
                if isinstance(source, dict) and source.get("editorial_source")
            }
        ),
        "revised_item_count": sum(
            item.get("revision_status") == "revised" for item in records
        ),
        "news_count": sum(1 for item in records if item.get("kind") == "news"),
        "announcement_count": sum(1 for item in records if item.get("kind") == "announcement"),
        "symbol_count": len({item.get("symbol") for item in records if item.get("symbol")}),
        "sentiment_annotation": {
            "positive": sum(1 for item in records if item.get("sentiment_annotation", {}).get("label") == "positive"),
            "negative": sum(1 for item in records if item.get("sentiment_annotation", {}).get("label") == "negative"),
            "neutral": sum(1 for item in records if item.get("sentiment_annotation", {}).get("label") == "neutral"),
        },
    }


def _revision_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision_id": value.get("revision_id"),
        "revision": value.get("revision"),
        "trade_date": value.get("trade_date"),
        "retrieved_at": value.get("retrieved_at"),
        "status": value.get("status"),
        "record_count": len(value.get("records", [])),
        "evidence_fingerprint": value.get("evidence_fingerprint"),
        "record_fingerprint": value.get("record_fingerprint"),
        "supersedes": value.get("supersedes"),
    }


def _unavailable(query: NewsQuery, errors: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": False,
        "status": "unavailable",
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "records": [],
        "summary": {"record_count": 0, "returned_count": 0},
        "filters": _query_payload(query),
        "authority": dict(_AUTHORITY),
        "errors": list(errors) or [{"code": "news_not_refreshed", "message": "No validated local news snapshot is available."}],
        "warnings": [],
        "revisions": [],
    }


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    payload = _clone(value)
    if isinstance(payload, dict):
        payload.pop("record_fingerprint", None)
        history = payload.get("revisions")
        if isinstance(history, list) and history and isinstance(history[-1], dict):
            history[-1]["record_fingerprint"] = None
    return _fingerprint(payload)


def _fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")).hexdigest()


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True))


def _china_now(value: datetime | None) -> datetime:
    zone = timezone(timedelta(hours=8))
    if value is None:
        return datetime.now(zone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
