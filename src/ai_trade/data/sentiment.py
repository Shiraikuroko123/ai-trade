from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from ..config import AppConfig
from ..json_utils import load_unique_json
from .evidence_io import atomic_create_json, evidence_store_lock


"""Deterministic market-tilt evidence composed from existing local stores.

This module reads only already-validated local evidence — exchange breadth,
board capital flow, and lexicon-annotated news — and composes one bounded,
explainable market-tilt score per completed trade date. It never touches the
network, never invents a component that is missing, requires at least two
independent components from the same trade date, and does not change the
assistant's `sentiment_coverage` contract: the record says so itself. A tilt
is research evidence, not a signal, forecast, or sentiment ground truth.
"""


SCHEMA_VERSION = 1
ENGINE_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024
MAX_REVISIONS_PER_DATE = 20
MINIMUM_COMPONENTS = 2
MINIMUM_ANNOTATED_NEWS = 5
FILE_NAME = re.compile(r"tilt_(\d{4}-\d{2}-\d{2})_r(\d{3})\.json\Z")

SAFETY = {
    "research_only": True,
    "creates_no_signal": True,
    "assistant_coverage_unchanged": True,
    "single_provider_caveat": True,
    "orders_created": False,
}

_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "trade_date",
        "revision",
        "supersedes",
        "created_at",
        "components",
        "available_components",
        "tilt_score",
        "tilt_label",
        "method",
        "content_fingerprint",
        "safety",
        "record_fingerprint",
    }
)

_METHOD = (
    "mean of available component scores; breadth = 2*advancers/(advancers+"
    "decliners)-1 across exchanges; capital_flow = 2*positive_main_share-1; "
    "news_lexicon = mean lexicon-v1 score over annotated items (minimum "
    f"{MINIMUM_ANNOTATED_NEWS}); components must share one trade date and at "
    f"least {MINIMUM_COMPONENTS} must be available"
)


class SentimentTiltEngine:
    """Compose, store, and read immutable market-tilt evidence."""

    def __init__(
        self,
        config: AppConfig,
        readers: Mapping[str, Callable[[], Mapping[str, Any]]] | None = None,
    ) -> None:
        self.config = config
        self.root = (config.project_root / "state" / "sentiment").resolve()
        self._readers = dict(readers) if readers is not None else None

    def compose(self, trade_date: date | None = None) -> dict[str, Any]:
        readers = self._readers or self._default_readers()
        components: list[dict[str, Any]] = []
        for name in ("breadth", "capital_flow", "news_lexicon"):
            reader = readers.get(name)
            if reader is None:
                components.append(_missing(name, "无对应读取器"))
                continue
            try:
                projection = reader()
            except Exception as exc:  # noqa: BLE001 - one source must not kill the rest
                components.append(_missing(name, f"读取失败: {str(exc)[:160]}"))
                continue
            components.append(_component(name, projection))

        dates = {
            item["trade_date"]
            for item in components
            if item["available"] and item["trade_date"]
        }
        if trade_date is not None:
            target = trade_date.isoformat()
        elif dates:
            target = max(dates)
        else:
            raise RuntimeError(
                "没有任何可用组件；请先刷新宽度、资金流或新闻证据"
            )
        for item in components:
            if item["available"] and item["trade_date"] != target:
                item["available"] = False
                item["detail"] += f"；交易日 {item['trade_date']} ≠ {target}，剔除"
                item["score"] = None

        available = [item for item in components if item["available"]]
        if len(available) < MINIMUM_COMPONENTS:
            raise RuntimeError(
                f"交易日 {target} 只有 {len(available)} 个可用组件，"
                f"少于最低 {MINIMUM_COMPONENTS} 个；单一来源不合成倾向证据"
            )
        score = sum(float(item["score"]) for item in available) / len(available)
        score = max(-1.0, min(1.0, score))
        label = (
            "RISK_ON_TILT"
            if score >= 0.2
            else "RISK_OFF_TILT"
            if score <= -0.2
            else "NEUTRAL"
        )
        content = {
            "trade_date": target,
            "components": components,
            "available_components": len(available),
            "tilt_score": score,
            "tilt_label": label,
            "method": _METHOD,
        }
        content_fingerprint = _fingerprint(content)
        with evidence_store_lock(self.root, "Sentiment tilt"):
            chain = self._chain_unlocked(target)
            if chain and chain[-1]["content_fingerprint"] == content_fingerprint:
                latest = dict(chain[-1])
                latest["reused"] = True
                return latest
            if len(chain) >= MAX_REVISIONS_PER_DATE:
                raise RuntimeError(
                    f"交易日 {target} 的修订数达到上限 {MAX_REVISIONS_PER_DATE}"
                )
            record = {
                "schema_version": SCHEMA_VERSION,
                "engine_version": ENGINE_VERSION,
                "revision": len(chain) + 1,
                "supersedes": (
                    chain[-1]["record_fingerprint"] if chain else None
                ),
                "created_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                **content,
                "content_fingerprint": content_fingerprint,
                "safety": dict(SAFETY),
            }
            record["record_fingerprint"] = _record_fingerprint(record)
            _validate(record)
            target_path = (
                self.root / f"tilt_{target}_r{record['revision']:03d}.json"
            )
            atomic_create_json(
                self.root,
                target_path,
                record,
                label="sentiment tilt record",
                maximum_bytes=MAX_RECORD_BYTES,
            )
        stored = self.latest(date.fromisoformat(target))
        stored["reused"] = False
        return stored

    def latest(self, trade_date: date | None = None) -> dict[str, Any]:
        with evidence_store_lock(self.root, "Sentiment tilt"):
            if trade_date is None:
                periods = self._periods_unlocked()
                if not periods:
                    raise KeyError("no sentiment tilt evidence")
                trade_date = periods[-1]
            chain = self._chain_unlocked(trade_date.isoformat())
        if not chain:
            raise KeyError(trade_date.isoformat())
        return dict(chain[-1])

    def list(self, *, limit: int = 30) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Sentiment list limit must be between 1 and 100")
        with evidence_store_lock(self.root, "Sentiment tilt"):
            periods = self._periods_unlocked()
            rows = []
            for value in reversed(periods[-limit:]):
                chain = self._chain_unlocked(value.isoformat())
                latest = chain[-1]
                rows.append(
                    {
                        "trade_date": latest["trade_date"],
                        "revision": latest["revision"],
                        "tilt_score": latest["tilt_score"],
                        "tilt_label": latest["tilt_label"],
                        "available_components": latest["available_components"],
                    }
                )
        return {
            "schema_version": 1,
            "tilts": rows,
            "summary": {"dates": len(periods), "returned": len(rows)},
            "safety": dict(SAFETY),
        }

    def _default_readers(self) -> dict[str, Callable[[], Mapping[str, Any]]]:
        from .capital_flow import CapitalFlowStore
        from .market_breadth import MarketBreadthStore
        from .news import NewsStore

        return {
            "breadth": lambda: MarketBreadthStore(self.config).list(),
            "capital_flow": lambda: CapitalFlowStore(self.config).list(),
            "news_lexicon": lambda: NewsStore(self.config).list(),
        }

    def _periods_unlocked(self) -> list[date]:
        if not self.root.exists():
            return []
        periods: set[date] = set()
        for path in self.root.iterdir():
            match = FILE_NAME.fullmatch(path.name)
            if path.is_symlink() or not path.is_file() or match is None:
                raise RuntimeError("Unexpected sentiment store member")
            periods.add(date.fromisoformat(match.group(1)))
        return sorted(periods)

    def _chain_unlocked(self, trade_date: str) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        chain: list[dict[str, Any]] = []
        for path in sorted(self.root.iterdir()):
            match = FILE_NAME.fullmatch(path.name)
            if match is None or match.group(1) != trade_date:
                continue
            record = _read(path)
            if record["revision"] != int(match.group(2)):
                raise RuntimeError("Sentiment revision does not match file name")
            chain.append(record)
        for index, record in enumerate(chain, start=1):
            if record["revision"] != index:
                raise RuntimeError("Sentiment revision chain has a gap")
            expected = chain[index - 2]["record_fingerprint"] if index > 1 else None
            if record["supersedes"] != expected:
                raise RuntimeError("Sentiment supersedes binding is invalid")
        return chain


def _component(name: str, projection: Mapping[str, Any]) -> dict[str, Any]:
    if name == "breadth":
        rows = projection.get("breadth")
        if not isinstance(rows, list) or not rows:
            return _missing(name, "宽度快照不可用")
        advancers = 0
        decliners = 0
        for row in rows:
            if not isinstance(row, Mapping):
                return _missing(name, "宽度行结构异常")
            advancers += int(row.get("advancers") or 0)
            decliners += int(row.get("decliners") or 0)
        if advancers + decliners <= 0:
            return _missing(name, "宽度计数为零")
        score = 2.0 * advancers / (advancers + decliners) - 1.0
        return {
            "name": name,
            "available": True,
            "trade_date": str(projection.get("trade_date") or ""),
            "score": score,
            "detail": f"上涨 {advancers} / 下跌 {decliners}（跨交易所合计）",
            "inputs_fingerprint": _fingerprint(
                {"advancers": advancers, "decliners": decliners}
            ),
        }
    if name == "capital_flow":
        summary = projection.get("summary")
        share = summary.get("positive_main_share") if isinstance(summary, Mapping) else None
        if not isinstance(share, (int, float)) or isinstance(share, bool):
            return _missing(name, "资金流主力净流入占比不可用")
        score = 2.0 * float(share) - 1.0
        return {
            "name": name,
            "available": True,
            "trade_date": str(projection.get("trade_date") or ""),
            "score": max(-1.0, min(1.0, score)),
            "detail": f"主力净流入为正的板块占比 {float(share):.1%}",
            "inputs_fingerprint": _fingerprint({"positive_main_share": share}),
        }
    if name == "news_lexicon":
        items = projection.get("items")
        if not isinstance(items, list):
            return _missing(name, "新闻快照不可用")
        scores: list[float] = []
        for item in items:
            annotation = (
                item.get("sentiment_annotation") if isinstance(item, Mapping) else None
            )
            value = (
                annotation.get("score") if isinstance(annotation, Mapping) else None
            )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                scores.append(max(-1.0, min(1.0, float(value))))
        if len(scores) < MINIMUM_ANNOTATED_NEWS:
            return _missing(
                name,
                f"词典标注新闻不足（{len(scores)} < {MINIMUM_ANNOTATED_NEWS}）",
            )
        score = sum(scores) / len(scores)
        return {
            "name": name,
            "available": True,
            "trade_date": str(
                projection.get("trade_date") or projection.get("snapshot_date") or ""
            ),
            "score": score,
            "detail": f"{len(scores)} 条 lexicon-v1 标注均值",
            "inputs_fingerprint": _fingerprint(
                {"count": len(scores), "mean": score}
            ),
        }
    return _missing(name, "未知组件")


def _missing(name: str, detail: str) -> dict[str, Any]:
    return {
        "name": name,
        "available": False,
        "trade_date": "",
        "score": None,
        "detail": detail,
        "inputs_fingerprint": None,
    }


def _validate(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) - {"reused"} != _FIELDS:
        raise RuntimeError("Sentiment tilt schema fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Sentiment tilt schema version is invalid")
    if value.get("tilt_label") not in {"RISK_ON_TILT", "RISK_OFF_TILT", "NEUTRAL"}:
        raise RuntimeError("Sentiment tilt label is invalid")
    score = value.get("tilt_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise RuntimeError("Sentiment tilt score is invalid")
    if not -1.0 <= float(score) <= 1.0:
        raise RuntimeError("Sentiment tilt score is out of range")
    if value.get("safety") != SAFETY:
        raise RuntimeError("Sentiment tilt safety contract is invalid")
    components = value.get("components")
    if not isinstance(components, list) or len(components) != 3:
        raise RuntimeError("Sentiment tilt components are invalid")
    available = sum(
        1 for item in components if isinstance(item, Mapping) and item.get("available")
    )
    if value.get("available_components") != available:
        raise RuntimeError("Sentiment tilt availability count is inconsistent")
    if available < MINIMUM_COMPONENTS:
        raise RuntimeError("Sentiment tilt has too few available components")
    if value.get("record_fingerprint") != _record_fingerprint(value):
        raise RuntimeError("Sentiment tilt record fingerprint does not match")


def _read(path: Path) -> dict[str, Any]:
    try:
        value = load_unique_json(path, max_bytes=MAX_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid sentiment record: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Sentiment record must be an object")
    try:
        _validate(value)
    except RuntimeError as exc:
        raise RuntimeError(f"Invalid sentiment record: {path}: {exc}") from exc
    return value


def _fingerprint(value: Any) -> str:
    import json
    from hashlib import sha256

    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    body = {
        key: item
        for key, item in value.items()
        if key not in {"record_fingerprint", "reused"}
    }
    return _fingerprint(body)


__all__ = [
    "MINIMUM_COMPONENTS",
    "SAFETY",
    "SentimentTiltEngine",
]
