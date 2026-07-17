from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_unique_json(path: Path, *, max_bytes: int) -> Any:
    """Read a bounded UTF-8 JSON file and reject ambiguous duplicate keys."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError(f"JSON file exceeds {max_bytes} bytes")
    return loads_unique_json(content.decode("utf-8"))


def loads_unique_json(content: str) -> Any:
    if not isinstance(content, str):
        raise TypeError("JSON content must be a string")
    return json.loads(content, object_pairs_hook=_unique_object)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value
