from __future__ import annotations

from typing import Any

from .features import ALLOWED_CONCLUSIONS
from .provider import (
    AssistantProviderError,
    DEBATE_PROMPT_TEMPLATE_VERSION,
    valid_advocate_shape,
    valid_judge_shape,
)


DEBATE_METHOD = "auditable-bull-bear-judge-v1"
ADVOCATE_ROLES = ("bull", "bear")
ROLE_CALL_NAMES = {
    "bull": "research_debate_bull",
    "bear": "research_debate_bear",
    "judge": "research_debate_judge",
}
ROLE_STATUSES = {"LOCAL", "MODEL_APPLIED", "MODEL_CACHE_HIT", "FALLBACK_LOCAL"}
ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def build_auditable_debate(
    *,
    mode: str,
    user_id: str,
    symbol: str,
    data_date: str,
    diagnosis: dict[str, Any],
    assessment: dict[str, Any],
    perspectives: list[dict[str, Any]],
    conflict_audit: dict[str, Any],
    deterministic_conclusion: str,
    provider: Any,
    governance: Any,
) -> dict[str, Any]:
    allowed_evidence_ids = _allowed_evidence_ids(diagnosis)
    local_advocates = {
        role: _local_advocate(
            role,
            perspectives,
            assessment,
            allowed_evidence_ids,
        )
        for role in ADVOCATE_ROLES
    }
    roles: dict[str, dict[str, Any]] = {}
    for role in ADVOCATE_ROLES:
        roles[role] = _run_advocate(
            role=role,
            fallback=local_advocates[role],
            mode=mode,
            user_id=user_id,
            symbol=symbol,
            data_date=data_date,
            diagnosis=diagnosis,
            assessment=assessment,
            perspectives=perspectives,
            allowed_evidence_ids=allowed_evidence_ids,
            provider=provider,
            governance=governance,
        )

    local_judge = _local_judge(
        roles["bull"],
        roles["bear"],
        conflict_audit,
        assessment,
        allowed_evidence_ids,
    )
    roles["judge"] = _run_judge(
        fallback=local_judge,
        mode=mode,
        user_id=user_id,
        symbol=symbol,
        data_date=data_date,
        evidence=diagnosis.get("evidence", []),
        bull=roles["bull"],
        bear=roles["bear"],
        conflict_audit=conflict_audit,
        allowed_evidence_ids=allowed_evidence_ids,
        provider=provider,
        governance=governance,
    )

    role_values = list(roles.values())
    attempted = sum(item["model_attempted"] is True for item in role_values)
    applied = sum(
        item["status"] in {"MODEL_APPLIED", "MODEL_CACHE_HIT"}
        for item in role_values
    )
    fallback_count = sum(item["status"] == "FALLBACK_LOCAL" for item in role_values)
    usage = _aggregate_usage(role_values)
    costs = [
        item["call"].get("estimated_cost_usd")
        for item in role_values
        if isinstance(item.get("call"), dict)
        and isinstance(item["call"].get("estimated_cost_usd"), (int, float))
        and not isinstance(item["call"].get("estimated_cost_usd"), bool)
    ]
    return {
        "method": DEBATE_METHOD,
        "mode": mode,
        "status": (
            "LOCAL_ONLY"
            if mode == "local"
            else "COMPLETE"
            if applied == 3
            else "PARTIAL"
        ),
        "authority": "research_only",
        "execution_authorized": False,
        "conclusion_mutation_allowed": False,
        "deterministic_conclusion": deterministic_conclusion,
        "effective_conclusion": assessment.get("conclusion"),
        "roles": roles,
        "summary": {
            "model_attempted_count": attempted,
            "model_applied_count": applied,
            "fallback_count": fallback_count,
            "usage": usage,
            "estimated_cost_usd": round(sum(costs), 8) if costs else None,
        },
    }


def validate_debate(
    debate: Any,
    *,
    allowed_evidence_ids: set[str],
    mode: Any,
    deterministic_conclusion: Any,
    effective_conclusion: Any,
) -> list[str]:
    errors: list[str] = []
    expected_top = {
        "method",
        "mode",
        "status",
        "authority",
        "execution_authorized",
        "conclusion_mutation_allowed",
        "deterministic_conclusion",
        "effective_conclusion",
        "roles",
        "summary",
    }
    if not isinstance(debate, dict) or set(debate) != expected_top:
        return ["auditable debate structure is invalid"]
    if (
        debate.get("method") != DEBATE_METHOD
        or debate.get("mode") != mode
        or debate.get("authority") != "research_only"
        or debate.get("execution_authorized") is not False
        or debate.get("conclusion_mutation_allowed") is not False
    ):
        errors.append("auditable debate authority boundary is invalid")
    if (
        deterministic_conclusion not in ALLOWED_CONCLUSIONS
        or debate.get("deterministic_conclusion") != deterministic_conclusion
    ):
        errors.append("auditable debate deterministic conclusion is invalid")
    if debate.get("effective_conclusion") != effective_conclusion:
        errors.append("auditable debate effective conclusion is invalid")

    roles = debate.get("roles")
    if not isinstance(roles, dict) or set(roles) != {"bull", "bear", "judge"}:
        errors.append("auditable debate roles are invalid")
        return errors
    argument_ids: dict[str, set[str]] = {"bull": set(), "bear": set()}
    for role in ADVOCATE_ROLES:
        _validate_advocate_record(
            roles.get(role),
            role,
            allowed_evidence_ids,
            argument_ids[role],
            errors,
        )
    _validate_judge_record(
        roles.get("judge"),
        allowed_evidence_ids,
        argument_ids,
        errors,
    )
    _validate_debate_summary(debate, roles, errors)
    return errors


def _run_advocate(
    *,
    role: str,
    fallback: dict[str, Any],
    mode: str,
    user_id: str,
    symbol: str,
    data_date: str,
    diagnosis: dict[str, Any],
    assessment: dict[str, Any],
    perspectives: list[dict[str, Any]],
    allowed_evidence_ids: set[str],
    provider: Any,
    governance: Any,
) -> dict[str, Any]:
    if mode != "model":
        return _role_record(role, fallback, status="LOCAL", source="local_deterministic")
    method = getattr(provider, "debate_advocate", None)
    if not callable(method) or governance is None:
        return _role_record(
            role,
            fallback,
            status="FALLBACK_LOCAL",
            source="local_deterministic",
            error_code="model_role_unavailable",
        )

    call: dict[str, Any] | None = None
    try:
        value, _usage, call = governance.run_structured(
            user_id=user_id,
            role=ROLE_CALL_NAMES[role],
            template_version=DEBATE_PROMPT_TEMPLATE_VERSION,
            request_payload={
                "symbol": symbol,
                "data_date": data_date,
                "diagnosis": diagnosis,
                "assessment": assessment,
                "perspectives": perspectives,
            },
            evidence=diagnosis.get("evidence"),
            provider_call=lambda max_retries, audit_hook: method(
                role=role,
                symbol=symbol,
                data_date=data_date,
                diagnosis=diagnosis,
                assessment=assessment,
                perspectives=perspectives,
                max_retries=max_retries,
                audit_hook=audit_hook,
            ),
            result_validator=lambda result: valid_advocate_shape(
                result, allowed_evidence_ids
            ),
        )
        content = _with_argument_ids(role, value)
        return _role_record(
            role,
            content,
            status="MODEL_CACHE_HIT" if call.get("cache_hit") else "MODEL_APPLIED",
            source="model",
            call=call,
            model_attempted=True,
        )
    except AssistantProviderError as exc:
        call = exc.audit
        error_code = exc.code
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        error_code = "model_governance_unavailable"
    return _role_record(
        role,
        fallback,
        status="FALLBACK_LOCAL",
        source="local_deterministic",
        call=call,
        error_code=error_code,
        model_attempted=True,
    )


def _run_judge(
    *,
    fallback: dict[str, Any],
    mode: str,
    user_id: str,
    symbol: str,
    data_date: str,
    evidence: Any,
    bull: dict[str, Any],
    bear: dict[str, Any],
    conflict_audit: dict[str, Any],
    allowed_evidence_ids: set[str],
    provider: Any,
    governance: Any,
) -> dict[str, Any]:
    if mode != "model":
        return _role_record("judge", fallback, status="LOCAL", source="local_deterministic")
    method = getattr(provider, "debate_judge", None)
    if not callable(method) or governance is None:
        return _role_record(
            "judge",
            fallback,
            status="FALLBACK_LOCAL",
            source="local_deterministic",
            error_code="model_role_unavailable",
        )

    bull_content = _advocate_public_content(bull)
    bear_content = _advocate_public_content(bear)
    bull_ids = _argument_ids(bull_content)
    bear_ids = _argument_ids(bear_content)
    call: dict[str, Any] | None = None
    try:
        value, _usage, call = governance.run_structured(
            user_id=user_id,
            role=ROLE_CALL_NAMES["judge"],
            template_version=DEBATE_PROMPT_TEMPLATE_VERSION,
            request_payload={
                "symbol": symbol,
                "data_date": data_date,
                "evidence": evidence,
                "bull": bull_content,
                "bear": bear_content,
                "conflict_audit": conflict_audit,
            },
            evidence={
                "market_evidence": evidence,
                "bull": bull_content,
                "bear": bear_content,
            },
            provider_call=lambda max_retries, audit_hook: method(
                symbol=symbol,
                data_date=data_date,
                evidence=evidence,
                bull=bull_content,
                bear=bear_content,
                conflict_audit=conflict_audit,
                max_retries=max_retries,
                audit_hook=audit_hook,
            ),
            result_validator=lambda result: valid_judge_shape(
                result,
                allowed_evidence_ids,
                bull_ids,
                bear_ids,
            ),
        )
        return _role_record(
            "judge",
            value,
            status="MODEL_CACHE_HIT" if call.get("cache_hit") else "MODEL_APPLIED",
            source="model",
            call=call,
            model_attempted=True,
        )
    except AssistantProviderError as exc:
        call = exc.audit
        error_code = exc.code
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        error_code = "model_governance_unavailable"
    return _role_record(
        "judge",
        fallback,
        status="FALLBACK_LOCAL",
        source="local_deterministic",
        call=call,
        error_code=error_code,
        model_attempted=True,
    )


def _local_advocate(
    role: str,
    perspectives: list[dict[str, Any]],
    assessment: dict[str, Any],
    allowed_evidence_ids: set[str],
) -> dict[str, Any]:
    fallback_refs = _known_references(
        assessment.get("evidence_ids"), allowed_evidence_ids
    ) or sorted(allowed_evidence_ids)[:1]
    if role == "bull":
        argument_stances = {"SUPPORTIVE", "REVIEW"}
        counter_stances = {"CAUTION", "ADVERSE", "MIXED", "NOT_AVAILABLE"}
        empty_summary = "当前快照没有形成可引用的支持性论点，多头角色明确弃权。"
    else:
        argument_stances = {"CAUTION", "ADVERSE", "MIXED", "NOT_AVAILABLE"}
        counter_stances = {"SUPPORTIVE", "REVIEW"}
        empty_summary = "当前快照没有形成可引用的不利论点，空头角色明确弃权。"
    arguments = _perspective_claims(
        perspectives,
        argument_stances,
        allowed_evidence_ids,
        maximum=4,
    )
    counterevidence = _perspective_claims(
        perspectives,
        counter_stances,
        allowed_evidence_ids,
        maximum=4,
    )
    if not counterevidence:
        counterevidence = [
            {
                "claim": "同一已完成快照仍包含可能削弱本角色观点的反向证据，需由人工复核。",
                "evidence_ids": fallback_refs,
            }
        ]
    abstained = not arguments
    content = {
        "summary": (
            empty_summary
            if abstained
            else (
                "支持性证据与反证已分别登记，不代表买入或扩大风险权限。"
                if role == "bull"
                else "不利证据与反证已分别登记，不代表卖出、减仓或止损指令。"
            )
        ),
        "arguments": arguments,
        "counterevidence": counterevidence,
        "abstained": abstained,
        "abstention_reason": "insufficient_role_evidence" if abstained else None,
    }
    return _with_argument_ids(role, content)


def _local_judge(
    bull: dict[str, Any],
    bear: dict[str, Any],
    conflict_audit: dict[str, Any],
    assessment: dict[str, Any],
    allowed_evidence_ids: set[str],
) -> dict[str, Any]:
    fallback_refs = _known_references(
        assessment.get("evidence_ids"), allowed_evidence_ids
    ) or sorted(allowed_evidence_ids)[:1]
    bull_ids = _argument_ids(bull)
    bear_ids = _argument_ids(bear)
    agreements: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    for item in conflict_audit.get("conflicts", []):
        refs = _known_references(item.get("evidence_ids"), allowed_evidence_ids)
        if bull_ids and bear_ids:
            conflicts.append(
                {
                    "topic": str(item.get("title") or "已登记视角分歧"),
                    "bull_argument_ids": [sorted(bull_ids)[0]],
                    "bear_argument_ids": [sorted(bear_ids)[0]],
                    "evidence_ids": refs or fallback_refs,
                }
            )
        else:
            questions.append(
                {
                    "question": str(item.get("resolution") or "如何核对该视角分歧？"),
                    "evidence_ids": refs or fallback_refs,
                }
            )
    for item in conflict_audit.get("coverage_gaps", []):
        questions.append(
            {
                "question": str(item.get("resolution") or "如何补齐该数据覆盖缺口？"),
                "evidence_ids": _known_references(
                    item.get("evidence_ids"), allowed_evidence_ids
                )
                or fallback_refs,
            }
        )
    if not conflicts:
        agreements.append(
            {
                "topic": "双方材料均受同一已完成快照和 research_only 权限约束。",
                "evidence_ids": fallback_refs,
            }
        )
    if not questions and conflict_audit.get("status") != "ALIGNED":
        questions.append(
            {
                "question": "哪些新增的已完成交易日证据可以消除当前未决分歧？",
                "evidence_ids": fallback_refs,
            }
        )
    return {
        "summary": (
            f"本地裁判整理了 {len(agreements)} 项一致点、{len(conflicts)} 项冲突和"
            f" {len(questions)} 项未决问题；不拥有研究结论或订单权限。"
        ),
        "agreements": agreements,
        "conflicts": conflicts,
        "unresolved_questions": questions,
    }


def _perspective_claims(
    perspectives: list[dict[str, Any]],
    stances: set[str],
    allowed_evidence_ids: set[str],
    *,
    maximum: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in perspectives:
        refs = _known_references(item.get("evidence_ids"), allowed_evidence_ids)
        if item.get("stance") not in stances or not refs:
            continue
        result.append(
            {
                "claim": f"{item.get('label') or '研究视角'}：{item.get('summary')}",
                "evidence_ids": refs,
            }
        )
        if len(result) >= maximum:
            break
    return result


def _with_argument_ids(role: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": value["summary"],
        "arguments": [
            {"argument_id": f"{role}_argument_{index}", **item}
            for index, item in enumerate(value["arguments"], 1)
        ],
        "counterevidence": [
            {"argument_id": f"{role}_counter_{index}", **item}
            for index, item in enumerate(value["counterevidence"], 1)
        ],
        "abstained": value["abstained"],
        "abstention_reason": value["abstention_reason"],
    }


def _role_record(
    role: str,
    content: dict[str, Any],
    *,
    status: str,
    source: str,
    call: dict[str, Any] | None = None,
    error_code: str | None = None,
    model_attempted: bool = False,
) -> dict[str, Any]:
    return {
        "role": role,
        "status": status,
        "source": source,
        "model_attempted": model_attempted,
        **content,
        "call": call,
        "error_code": error_code,
    }


def _advocate_public_content(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (
            "summary",
            "arguments",
            "counterevidence",
            "abstained",
            "abstention_reason",
        )
    }


def _argument_ids(value: dict[str, Any]) -> set[str]:
    return {
        str(item["argument_id"])
        for key in ("arguments", "counterevidence")
        for item in value.get(key, [])
        if isinstance(item, dict) and isinstance(item.get("argument_id"), str)
    }


def _allowed_evidence_ids(diagnosis: dict[str, Any]) -> set[str]:
    return {
        str(item["evidence_id"])
        for item in diagnosis.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }


def _known_references(value: Any, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item in allowed))


def _aggregate_usage(roles: list[dict[str, Any]]) -> dict[str, int]:
    result = dict(ZERO_USAGE)
    for role in roles:
        call = role.get("call")
        usage = call.get("usage") if isinstance(call, dict) else None
        if not isinstance(usage, dict):
            continue
        for key in result:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] += value
    return result


def _validate_advocate_record(
    value: Any,
    role: str,
    allowed_evidence_ids: set[str],
    argument_ids: set[str],
    errors: list[str],
) -> None:
    expected = {
        "role",
        "status",
        "source",
        "model_attempted",
        "summary",
        "arguments",
        "counterevidence",
        "abstained",
        "abstention_reason",
        "call",
        "error_code",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("role") != role:
        errors.append(f"auditable debate {role} record is invalid")
        return
    _validate_role_metadata(value, role, errors)
    if (
        not isinstance(value.get("summary"), str)
        or not value["summary"].strip()
        or len(value["summary"]) > 2_000
    ):
        errors.append(f"auditable debate {role} summary is invalid")
    abstained = value.get("abstained")
    if not isinstance(abstained, bool):
        errors.append(f"auditable debate {role} abstention state is invalid")
    elif abstained is not (not value.get("arguments")):
        errors.append(f"auditable debate {role} abstention does not match its arguments")
    reason = value.get("abstention_reason")
    if (abstained and (not isinstance(reason, str) or not reason)) or (
        abstained is False and reason is not None
    ):
        errors.append(f"auditable debate {role} abstention reason is invalid")
    for key, minimum in (("arguments", 0 if abstained else 1), ("counterevidence", 1)):
        rows = value.get(key)
        if not isinstance(rows, list) or not minimum <= len(rows) <= 4:
            errors.append(f"auditable debate {role} {key} are invalid")
            continue
        prefix = f"{role}_{'argument' if key == 'arguments' else 'counter'}_"
        for index, item in enumerate(rows, 1):
            if not isinstance(item, dict) or set(item) != {
                "argument_id",
                "claim",
                "evidence_ids",
            }:
                errors.append(f"auditable debate {role} {key} item is invalid")
                continue
            argument_id = item.get("argument_id")
            if argument_id != f"{prefix}{index}" or argument_id in argument_ids:
                errors.append(f"auditable debate {role} argument identifier is invalid")
            else:
                argument_ids.add(argument_id)
            if (
                not isinstance(item.get("claim"), str)
                or not item["claim"].strip()
                or len(item["claim"]) > 1_000
            ):
                errors.append(f"auditable debate {role} claim is invalid")
            _validate_references(
                item.get("evidence_ids"),
                allowed_evidence_ids,
                f"auditable debate {role} claim",
                errors,
            )


def _validate_judge_record(
    value: Any,
    allowed_evidence_ids: set[str],
    argument_ids: dict[str, set[str]],
    errors: list[str],
) -> None:
    expected = {
        "role",
        "status",
        "source",
        "model_attempted",
        "summary",
        "agreements",
        "conflicts",
        "unresolved_questions",
        "call",
        "error_code",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("role") != "judge":
        errors.append("auditable debate judge record is invalid")
        return
    _validate_role_metadata(value, "judge", errors)
    if (
        not isinstance(value.get("summary"), str)
        or not value["summary"].strip()
        or len(value["summary"]) > 2_000
    ):
        errors.append("auditable debate judge summary is invalid")
    for key, text_key in (("agreements", "topic"), ("unresolved_questions", "question")):
        rows = value.get(key)
        if not isinstance(rows, list) or len(rows) > 8:
            errors.append(f"auditable debate judge {key} are invalid")
            continue
        for item in rows:
            if not isinstance(item, dict) or set(item) != {text_key, "evidence_ids"}:
                errors.append(f"auditable debate judge {key} item is invalid")
                continue
            if (
                not isinstance(item.get(text_key), str)
                or not item[text_key].strip()
                or len(item[text_key]) > 1_000
            ):
                errors.append(f"auditable debate judge {key} text is invalid")
            _validate_references(
                item.get("evidence_ids"),
                allowed_evidence_ids,
                f"auditable debate judge {key}",
                errors,
            )
    conflicts = value.get("conflicts")
    if not isinstance(conflicts, list) or len(conflicts) > 8:
        errors.append("auditable debate judge conflicts are invalid")
    else:
        for item in conflicts:
            if not isinstance(item, dict) or set(item) != {
                "topic",
                "bull_argument_ids",
                "bear_argument_ids",
                "evidence_ids",
            }:
                errors.append("auditable debate judge conflict item is invalid")
                continue
            if (
                not isinstance(item.get("topic"), str)
                or not item["topic"].strip()
                or len(item["topic"]) > 1_000
            ):
                errors.append("auditable debate judge conflict topic is invalid")
            for role in ADVOCATE_ROLES:
                refs = item.get(f"{role}_argument_ids")
                if (
                    not isinstance(refs, list)
                    or not refs
                    or len(refs) != len(set(refs))
                    or any(ref not in argument_ids[role] for ref in refs)
                ):
                    errors.append(
                        f"auditable debate judge references an invalid {role} argument"
                    )
            _validate_references(
                item.get("evidence_ids"),
                allowed_evidence_ids,
                "auditable debate judge conflict",
                errors,
            )
    if not any(value.get(key) for key in ("agreements", "conflicts", "unresolved_questions")):
        errors.append("auditable debate judge retained no research issue")


def _validate_role_metadata(value: dict[str, Any], role: str, errors: list[str]) -> None:
    status = value.get("status")
    source = value.get("source")
    attempted = value.get("model_attempted")
    call = value.get("call")
    error_code = value.get("error_code")
    if status not in ROLE_STATUSES or not isinstance(attempted, bool):
        errors.append(f"auditable debate {role} status is invalid")
    if status == "LOCAL" and (
        source != "local_deterministic" or attempted or call is not None or error_code is not None
    ):
        errors.append(f"auditable debate {role} local metadata is invalid")
    elif status in {"MODEL_APPLIED", "MODEL_CACHE_HIT"} and (
        source != "model" or not attempted or not isinstance(call, dict) or error_code is not None
    ):
        errors.append(f"auditable debate {role} model metadata is invalid")
    elif status == "FALLBACK_LOCAL" and (
        source != "local_deterministic" or not isinstance(error_code, str) or not error_code
    ):
        errors.append(f"auditable debate {role} fallback metadata is invalid")
    if isinstance(call, dict):
        if call.get("role") != ROLE_CALL_NAMES[role]:
            errors.append(f"auditable debate {role} call role is invalid")
        if call.get("template_version") != DEBATE_PROMPT_TEMPLATE_VERSION:
            errors.append(f"auditable debate {role} call template is invalid")
        expected_status = {
            "MODEL_APPLIED": "success",
            "MODEL_CACHE_HIT": "cache_hit",
        }.get(status)
        if expected_status is not None and call.get("status") != expected_status:
            errors.append(f"auditable debate {role} call status is invalid")
        if status == "FALLBACK_LOCAL" and (
            call.get("status") not in {"denied", "failed"}
            or call.get("error_code") != error_code
        ):
            errors.append(f"auditable debate {role} failure audit is invalid")


def _validate_references(
    value: Any,
    allowed: set[str],
    name: str,
    errors: list[str],
) -> None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or item not in allowed for item in value)
    ):
        errors.append(f"{name} evidence references are invalid")


def _validate_debate_summary(
    debate: dict[str, Any], roles: dict[str, dict[str, Any]], errors: list[str]
) -> None:
    summary = debate.get("summary")
    expected = {
        "model_attempted_count",
        "model_applied_count",
        "fallback_count",
        "usage",
        "estimated_cost_usd",
    }
    if not isinstance(summary, dict) or set(summary) != expected:
        errors.append("auditable debate summary is invalid")
        return
    values = list(roles.values())
    attempted = sum(item.get("model_attempted") is True for item in values)
    applied = sum(
        item.get("status") in {"MODEL_APPLIED", "MODEL_CACHE_HIT"} for item in values
    )
    fallback = sum(item.get("status") == "FALLBACK_LOCAL" for item in values)
    if (
        summary.get("model_attempted_count") != attempted
        or summary.get("model_applied_count") != applied
        or summary.get("fallback_count") != fallback
        or summary.get("usage") != _aggregate_usage(values)
    ):
        errors.append("auditable debate summary does not match its role records")
    expected_status = (
        "LOCAL_ONLY"
        if debate.get("mode") == "local"
        else "COMPLETE"
        if applied == 3
        else "PARTIAL"
    )
    if debate.get("status") != expected_status:
        errors.append("auditable debate status is invalid")
    if debate.get("mode") == "local" and summary.get("usage") != ZERO_USAGE:
        errors.append("auditable debate local usage is invalid")
    costs = [
        item["call"].get("estimated_cost_usd")
        for item in values
        if isinstance(item.get("call"), dict)
        and isinstance(item["call"].get("estimated_cost_usd"), (int, float))
        and not isinstance(item["call"].get("estimated_cost_usd"), bool)
    ]
    expected_cost = round(sum(costs), 8) if costs else None
    if summary.get("estimated_cost_usd") != expected_cost:
        errors.append("auditable debate estimated cost is invalid")
