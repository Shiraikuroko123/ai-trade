from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
LEDGER_VERSION = 1
PLAN_SCHEMA_VERSION = 1

LOOP_ID = re.compile(r"loop_[0-9a-f]{32}\Z")
EVENT_ID = re.compile(r"rle_[0-9a-f]{32}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
FACTOR_NAME = re.compile(r"[a-z][a-z0-9_]{2,30}\Z")
MODEL_EVALUATION_ID = re.compile(r"mdl_[0-9a-f]{32}\Z")
HYPOTHESIS_ID = re.compile(r"hyp_[0-9a-f]{32}\Z")

RESEARCH_TOOLS = (
    "factor-define",
    "factor-evaluate",
    "model-evaluate",
    "hypothesis-from-model",
    "hypothesis-run",
)
PLANNER_TOOLS = frozenset((*RESEARCH_TOOLS, "stop"))
TOOL_COST_UNITS = {
    "factor-define": 1,
    "factor-evaluate": 2,
    "model-evaluate": 4,
    "hypothesis-from-model": 1,
    "hypothesis-run": 6,
}
EVENT_TYPES = frozenset(
    {
        "loop_started",
        "planner_succeeded",
        "planner_failed",
        "tool_started",
        "tool_succeeded",
        "tool_failed",
        "tool_rejected",
        "loop_finished",
    }
)

LOOP_SAFETY = {
    "research_only": True,
    "execution_enabled": False,
    "may_create_candidate": False,
    "may_approve": False,
    "may_activate": False,
    "may_trade": False,
    "may_change_broker_configuration": False,
    "may_weaken_validation_gates": False,
    "append_only_audit": True,
}

EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "ledger_version",
        "loop_id",
        "owner",
        "sequence",
        "event_id",
        "created_at",
        "event_type",
        "payload",
        "previous_record_fingerprint",
        "safety",
        "record_fingerprint",
    }
)


def validate_static_plan(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "actions",
    }:
        raise ValueError("Research plan fields are invalid")
    if value.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("Research plan schema version is invalid")
    actions = value.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 12:
        raise ValueError("Research plan must contain 1 to 12 actions")
    result: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, Mapping):
            raise ValueError("Research plan actions must be objects")
        result.append(_json_clone(action))
    return result


def validate_proposal(
    value: Any,
    *,
    allowed_factors: set[str],
    allowed_models: set[str],
    model_evaluation_ids: set[str],
    hypothesis_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "tool",
        "arguments",
        "rationale",
    }:
        raise ValueError("Research proposal fields are invalid")
    tool = value.get("tool")
    if tool not in PLANNER_TOOLS:
        raise ValueError("Research proposal tool is not allowlisted")
    rationale = _text(value.get("rationale"), "rationale", 500)
    arguments = value.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ValueError("Research proposal arguments must be an object")

    normalized: dict[str, Any]
    if tool == "factor-define":
        _exact(arguments, {"name", "expression", "direction", "label"}, tool)
        name = arguments.get("name")
        if not isinstance(name, str) or FACTOR_NAME.fullmatch(name) is None:
            raise ValueError("Research factor name is invalid")
        expression = _text(arguments.get("expression"), "expression", 500)
        direction = arguments.get("direction")
        if type(direction) is not int or direction not in {-1, 1}:
            raise ValueError("Research factor direction must be -1 or 1")
        label = arguments.get("label")
        if label is not None:
            label = _text(label, "label", 120)
        normalized = {
            "name": name,
            "expression": expression,
            "direction": direction,
            "label": label,
        }
    elif tool == "factor-evaluate":
        _exact(arguments, {"factor_id", "horizons", "step"}, tool)
        factor_id = arguments.get("factor_id")
        if not isinstance(factor_id, str) or factor_id not in allowed_factors:
            raise ValueError("Research factor is unavailable")
        horizons = _integer_list(arguments.get("horizons"), "horizons", 1, 5, 1, 250)
        if horizons != sorted(set(horizons)):
            raise ValueError("Research horizons must be unique and ascending")
        normalized = {
            "factor_id": factor_id,
            "horizons": horizons,
            "step": _integer(arguments.get("step"), "step", 1, 21),
        }
    elif tool == "model-evaluate":
        _exact(arguments, {"model_id", "factor_ids", "horizon", "step"}, tool)
        model_id = arguments.get("model_id")
        if not isinstance(model_id, str) or model_id not in allowed_models:
            raise ValueError("Research model is unavailable")
        factor_ids = arguments.get("factor_ids")
        if (
            not isinstance(factor_ids, list)
            or not 1 <= len(factor_ids) <= 12
            or any(not isinstance(item, str) for item in factor_ids)
            or len(factor_ids) != len(set(factor_ids))
            or any(item not in allowed_factors for item in factor_ids)
        ):
            raise ValueError("Research model factor_ids are invalid")
        normalized = {
            "model_id": model_id,
            "factor_ids": list(factor_ids),
            "horizon": _integer(arguments.get("horizon"), "horizon", 1, 250),
            "step": _integer(arguments.get("step"), "step", 1, 21),
        }
    elif tool == "hypothesis-from-model":
        _exact(arguments, {"evaluation_id"}, tool)
        evaluation_id = arguments.get("evaluation_id")
        if (
            not isinstance(evaluation_id, str)
            or MODEL_EVALUATION_ID.fullmatch(evaluation_id) is None
            or evaluation_id not in model_evaluation_ids
        ):
            raise ValueError(
                "Hypothesis derivation requires a model evaluation produced by this loop"
            )
        normalized = {"evaluation_id": evaluation_id}
    elif tool == "hypothesis-run":
        _exact(arguments, {"hypothesis_id"}, tool)
        hypothesis_id = arguments.get("hypothesis_id")
        if (
            not isinstance(hypothesis_id, str)
            or HYPOTHESIS_ID.fullmatch(hypothesis_id) is None
            or hypothesis_id not in hypothesis_ids
        ):
            raise ValueError(
                "Hypothesis execution requires a hypothesis produced by this loop"
            )
        normalized = {"hypothesis_id": hypothesis_id}
    else:
        _exact(arguments, set(), "stop")
        normalized = {}

    return {"tool": tool, "arguments": normalized, "rationale": rationale}


def proposal_fingerprint(value: Mapping[str, Any]) -> str:
    return json_fingerprint(value)


def finalize_event(
    draft: Mapping[str, Any],
    *,
    sequence: int,
    previous_record_fingerprint: str | None,
) -> dict[str, Any]:
    record = _json_clone(draft)
    if not isinstance(record, dict):
        raise ValueError("Research loop event must be an object")
    record.update(
        {
            "schema_version": SCHEMA_VERSION,
            "ledger_version": LEDGER_VERSION,
            "sequence": sequence,
            "previous_record_fingerprint": previous_record_fingerprint,
            "safety": dict(LOOP_SAFETY),
            "record_fingerprint": None,
        }
    )
    record["record_fingerprint"] = event_record_fingerprint(record)
    validate_event(record)
    return record


def validate_event(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != EVENT_FIELDS:
        raise ValueError("Research loop event fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Research loop schema version is invalid")
    if value.get("ledger_version") != LEDGER_VERSION:
        raise ValueError("Research loop ledger version is invalid")
    _identifier(value.get("loop_id"), LOOP_ID, "loop_id")
    _identifier(value.get("owner"), FINGERPRINT, "owner")
    sequence = value.get("sequence")
    if type(sequence) is not int or not 1 <= sequence <= 100:
        raise ValueError("Research loop event sequence is invalid")
    _identifier(value.get("event_id"), EVENT_ID, "event_id")
    _timestamp(value.get("created_at"))
    if value.get("event_type") not in EVENT_TYPES:
        raise ValueError("Research loop event type is invalid")
    if not isinstance(value.get("payload"), Mapping):
        raise ValueError("Research loop event payload must be an object")
    previous = value.get("previous_record_fingerprint")
    if previous is not None:
        _identifier(previous, FINGERPRINT, "previous_record_fingerprint")
    if value.get("safety") != LOOP_SAFETY:
        raise ValueError("Research loop safety contract is invalid")
    _identifier(value.get("record_fingerprint"), FINGERPRINT, "record_fingerprint")
    if value["record_fingerprint"] != event_record_fingerprint(value):
        raise ValueError("Research loop event fingerprint does not match content")


def event_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    body["record_fingerprint"] = None
    return json_fingerprint(body)


def json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise ValueError(f"Research {label} arguments are invalid")


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Research {label} must be between {minimum} and {maximum}")
    return value


def _integer_list(
    value: Any,
    label: str,
    minimum_items: int,
    maximum_items: int,
    minimum: int,
    maximum: int,
) -> list[int]:
    if not isinstance(value, list) or not minimum_items <= len(value) <= maximum_items:
        raise ValueError(f"Research {label} item count is invalid")
    return [_integer(item, label, minimum, maximum) for item in value]


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Research {label} must contain 1 to {maximum} characters")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ValueError(f"Research {label} contains invalid control characters")
    return value.strip()


def _identifier(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Research loop {label} is invalid")
    return value


def _timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("Research loop timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Research loop timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Research loop timestamp must include a timezone")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "EVENT_ID",
    "EVENT_TYPES",
    "FINGERPRINT",
    "LEDGER_VERSION",
    "LOOP_ID",
    "LOOP_SAFETY",
    "PLAN_SCHEMA_VERSION",
    "PLANNER_TOOLS",
    "RESEARCH_TOOLS",
    "SCHEMA_VERSION",
    "TOOL_COST_UNITS",
    "event_record_fingerprint",
    "finalize_event",
    "json_fingerprint",
    "proposal_fingerprint",
    "validate_event",
    "validate_proposal",
    "validate_static_plan",
]
