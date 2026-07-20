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
import urllib.parse
import urllib.request
from uuid import uuid4

from ..config import AppConfig
from ..json_utils import load_unique_json, loads_unique_json
from ..models import Instrument
from .eastmoney import REQUEST_HEADERS, _open_request, _proxy_mode, completed_session_cutoff
from .evidence_io import atomic_create_json, evidence_store_lock


SCHEMA_VERSION = 1
DATASET = "news"
NEWS_ENDPOINT = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_{page_size}_{page}.html"
ANNOUNCEMENT_ENDPOINT = "https://np-anotice-stock.eastmoney.com/api/security/ann"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
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
    records: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    source_responses: list[dict[str, Any]] = []

    try:
        payload, digest, response_bytes = _download_news(config, limit_per_source)
        source_responses.append(
            {
                "kind": "news",
                "symbol": None,
                "response_sha256": digest,
                "response_bytes": response_bytes,
            }
        )
        for item in _parse_news_payload(payload, snapshot_date):
            records[item["item_id"]] = item
    except (NewsProviderError, OSError, ValueError) as exc:
        errors.append({"source": "news", "code": "news_provider_error", "message": str(exc)[:300]})

    for symbol in selected_symbols:
        try:
            payload, digest, response_bytes = _download_announcements(
                config, instruments[symbol], limit_per_source
            )
            source_responses.append(
                {
                    "kind": "announcement",
                    "symbol": symbol,
                    "response_sha256": digest,
                    "response_bytes": response_bytes,
                }
            )
            for item in _parse_announcement_payload(payload, instruments[symbol], snapshot_date):
                records[item["item_id"]] = item
        except (NewsProviderError, OSError, ValueError) as exc:
            errors.append({"source": f"announcement:{symbol}", "code": "announcement_provider_error", "message": str(exc)[:300]})

    ordered = sorted(
        records.values(),
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
            "provider": "eastmoney",
            "news_endpoint": NEWS_ENDPOINT,
            "announcement_endpoint": ANNOUNCEMENT_ENDPOINT,
            "responses": sorted(
                source_responses,
                key=lambda item: (str(item["kind"]), str(item["symbol"] or "")),
            ),
            "response_sha256": _fingerprint(
                sorted(
                    source_responses,
                    key=lambda item: (str(item["kind"]), str(item["symbol"] or "")),
                )
            ),
            "response_count": len(source_responses),
            "certification": "not_exchange_certified",
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
        previous = chain[-1] if chain else None
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
        result.append(_normalized_item(item_id, "news", None, title, summary, published, url, "Eastmoney快讯"))
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
        item = _normalized_item(item_id, "announcement", instrument.symbol, title, category, published, url, "Eastmoney公告")
        item["category"] = category
        result.append(item)
    return result


def _normalized_item(
    item_id: str,
    kind: str,
    symbol: str | None,
    title: str,
    summary: str,
    published: datetime,
    url: str,
    source_name: str,
) -> dict[str, Any]:
    annotation = _sentiment_annotation(f"{title} {summary}")
    return {
        "item_id": item_id,
        "kind": kind,
        "symbol": symbol,
        "title": title,
        "summary": summary,
        "published_at": published.isoformat(),
        "url": url,
        "source": source_name,
        "sentiment_annotation": annotation,
    }


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
    if not isinstance(source, dict) or source.get("provider") != "eastmoney":
        raise RuntimeError("news source metadata is invalid")
    responses = source.get("responses")
    if not isinstance(responses, list) or len(responses) > 51:
        raise RuntimeError("news response metadata is invalid")
    normalized_responses: list[dict[str, Any]] = []
    response_keys: set[tuple[str, str]] = set()
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
        if (
            isinstance(response_bytes, bool)
            or not isinstance(response_bytes, int)
            or not 0 <= response_bytes <= MAX_RESPONSE_BYTES
        ):
            raise RuntimeError("news response size is invalid")
        key = (str(kind), str(symbol or ""))
        if key in response_keys:
            raise RuntimeError("news response metadata is duplicated")
        response_keys.add(key)
        normalized_responses.append(
            {
                "kind": kind,
                "symbol": symbol,
                "response_sha256": digest,
                "response_bytes": response_bytes,
            }
        )
    normalized_responses.sort(key=lambda item: (str(item["kind"]), str(item["symbol"] or "")))
    if source.get("response_count") != len(normalized_responses):
        raise RuntimeError("news response count is invalid")
    if source.get("response_sha256") != _fingerprint(normalized_responses):
        raise RuntimeError("news response aggregate fingerprint is invalid")
    for endpoint_key in ("news_endpoint", "announcement_endpoint"):
        _source_url(source.get(endpoint_key))
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
    if not (host == "eastmoney.com" or host.endswith(".eastmoney.com")):
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
