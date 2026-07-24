from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .features import ALLOWED_CONCLUSIONS
from ..json_utils import loads_unique_json


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 512 * 1024
MAX_COMPLETION_TOKENS = 1400
PROMPT_TEMPLATE_VERSION = "assistant-enhancement-v2"
DEBATE_PROMPT_TEMPLATE_VERSION = "assistant-debate-v1"
DEBATE_ADVOCATE_ROLES = {"bull", "bear"}
_RETRYABLE_ERRORS = {
    "model_rate_limited",
    "model_server_error",
    "model_transport_error",
}


class AssistantProviderError(RuntimeError):
    def __init__(self, code: str, *, audit: dict[str, Any] | None = None):
        self.code = code
        self.audit = audit
        super().__init__(code)


@dataclass(frozen=True)
class ProviderSettings:
    api_key: str
    model: str
    endpoint: str
    timeout_seconds: float
    max_response_bytes: int

    @classmethod
    def from_environment(cls) -> tuple[ProviderSettings | None, str | None]:
        key = os.environ.get("AI_TRADE_AI_API_KEY", "").strip()
        model = os.environ.get("AI_TRADE_AI_MODEL", "").strip()
        if not key and not model:
            return None, None
        if not key or not model:
            return None, "AI model configuration requires both API key and model"
        if len(key) > 8192 or len(model) > 200 or any(ord(char) < 32 for char in model):
            return None, "AI model configuration contains an invalid key or model name"
        try:
            endpoint = _completion_endpoint(
                os.environ.get("AI_TRADE_AI_BASE_URL", DEFAULT_BASE_URL).strip()
            )
            timeout = _bounded_float(
                os.environ.get("AI_TRADE_AI_TIMEOUT_SECONDS"),
                DEFAULT_TIMEOUT_SECONDS,
                1.0,
                120.0,
            )
            maximum = DEFAULT_MAX_RESPONSE_BYTES
        except ValueError:
            return None, "AI model endpoint or transport limits are invalid"
        return cls(key, model, endpoint, timeout, maximum), None


class OpenAICompatibleProvider:
    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self._opener = build_opener(_SameOriginRedirectHandler(settings.endpoint))

    def enhance(
        self,
        *,
        symbol: str,
        data_date: str,
        diagnosis: dict[str, Any],
        assessment: dict[str, Any],
        max_retries: int = 0,
        audit_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 3
        ):
            raise ValueError("max_retries must be an integer between 0 and 3")
        evidence = diagnosis["evidence"]
        allowed_ids = {str(item["evidence_id"]) for item in evidence}
        public_input = {
            "symbol": symbol,
            "data_date": data_date,
            "authority": "research_only",
            "order_intent": None,
            "diagnosis": {
                key: diagnosis[key]
                for key in ("trend", "regime", "volatility", "score", "gate", "evidence")
            },
            "local_assessment": assessment,
        }
        instruction = (
            "Return one JSON object only. Improve the plain-language diagnosis and risk "
            "assessment using only supplied evidence. Never create an order, target price, "
            "position size, win rate, hidden reasoning, or uncited fact. risk_budget_pct is "
            "a research-review budget, not a portfolio weight; copy or reduce the supplied "
            "local value and never increase it. Required shape: "
            '{"diagnosis":{"summary":string,"evidence_ids":[string]},'
            '"assessment":{"conclusion":"NO_ACTION|WATCH|REVIEW_CANDIDATE|REDUCE_RISK",'
            '"summary":string,"risk_level":"LOW|MEDIUM|HIGH","risk_budget_pct":integer,'
            '"evidence_ids":[string],"invalidation":[string],'
            '"scenarios":[{"name":string,"trigger":string,"implication":string}]}}.'
        )
        return self._structured_call(
            instruction=instruction,
            public_input=public_input,
            validator=lambda value: _validate_enhancement(value, allowed_ids),
            max_retries=max_retries,
            audit_hook=audit_hook,
        )

    def debate_advocate(
        self,
        *,
        role: str,
        symbol: str,
        data_date: str,
        diagnosis: dict[str, Any],
        assessment: dict[str, Any],
        perspectives: list[dict[str, Any]],
        max_retries: int = 0,
        audit_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        if role not in DEBATE_ADVOCATE_ROLES:
            raise ValueError("debate advocate role is invalid")
        evidence = diagnosis.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError("debate evidence is invalid")
        allowed_ids = {
            str(item["evidence_id"])
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
        }
        emphasis = (
            "Construct the strongest evidence-supported case for continued research review."
            if role == "bull"
            else "Construct the strongest evidence-supported adverse or cautionary case."
        )
        instruction = (
            "Return one JSON object only. You are one bounded research advocate, not a "
            "trader or decision maker. "
            + emphasis
            + " Also state material counterevidence against your assigned case. Use only "
            "supplied evidence IDs. Never output a conclusion, vote, confidence, probability, "
            "order, position, quantity, price target, stop, risk budget, or hidden reasoning. "
            "Required exact shape: "
            '{"summary":string,"arguments":[{"claim":string,"evidence_ids":[string]}],'
            '"counterevidence":[{"claim":string,"evidence_ids":[string]}],'
            '"abstained":boolean,"abstention_reason":string|null}.'
        )
        public_input = {
            "role": role,
            "symbol": symbol,
            "data_date": data_date,
            "authority": "research_only",
            "execution_authorized": False,
            "diagnosis": {
                key: diagnosis.get(key)
                for key in (
                    "trend",
                    "regime",
                    "volatility",
                    "score",
                    "gate",
                    "evidence",
                )
            },
            "local_assessment": assessment,
            "perspectives": perspectives,
        }
        return self._structured_call(
            instruction=instruction,
            public_input=public_input,
            validator=lambda value: _validate_advocate(value, allowed_ids),
            max_retries=max_retries,
            audit_hook=audit_hook,
        )

    def debate_judge(
        self,
        *,
        symbol: str,
        data_date: str,
        evidence: list[dict[str, Any]],
        bull: dict[str, Any],
        bear: dict[str, Any],
        conflict_audit: dict[str, Any],
        max_retries: int = 0,
        audit_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        allowed_ids = {
            str(item["evidence_id"])
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
        }
        bull_argument_ids = {
            str(item.get("argument_id"))
            for key in ("arguments", "counterevidence")
            for item in bull.get(key, [])
            if isinstance(item, dict) and isinstance(item.get("argument_id"), str)
        }
        bear_argument_ids = {
            str(item.get("argument_id"))
            for key in ("arguments", "counterevidence")
            for item in bear.get(key, [])
            if isinstance(item, dict) and isinstance(item.get("argument_id"), str)
        }
        instruction = (
            "Return one JSON object only. Organize the supplied validated bull and bear "
            "research records into agreements, conflicts, and unresolved questions. Use only "
            "supplied evidence IDs and argument IDs. You are not allowed to select or change a "
            "research conclusion, vote, rank a side, estimate confidence, or output any order, "
            "position, quantity, price target, stop, risk budget, or hidden reasoning. Required "
            "exact shape: "
            '{"summary":string,"agreements":[{"topic":string,"evidence_ids":[string]}],'
            '"conflicts":[{"topic":string,"bull_argument_ids":[string],'
            '"bear_argument_ids":[string],"evidence_ids":[string]}],'
            '"unresolved_questions":[{"question":string,"evidence_ids":[string]}]}.'
        )
        public_input = {
            "symbol": symbol,
            "data_date": data_date,
            "authority": "research_only",
            "execution_authorized": False,
            "evidence": evidence,
            "bull": bull,
            "bear": bear,
            "deterministic_conflict_audit": conflict_audit,
        }
        return self._structured_call(
            instruction=instruction,
            public_input=public_input,
            validator=lambda value: _validate_judge(
                value,
                allowed_ids,
                bull_argument_ids,
                bear_argument_ids,
            ),
            max_retries=max_retries,
            audit_hook=audit_hook,
        )

    def _structured_call(
        self,
        *,
        instruction: str,
        public_input: dict[str, Any],
        validator: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]],
        max_retries: int,
        audit_hook: Callable[[dict[str, Any]], None] | None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 3
        ):
            raise ValueError("max_retries must be an integer between 0 and 3")
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        validation_errors: list[str] = []
        request_attempt = 0
        for validation_round in range(2):
            messages = [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        public_input,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]
            if validation_round:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The previous JSON failed validation. Return a corrected JSON object. "
                            f"Errors: {validation_errors}."
                        ),
                    }
                )
            retry = 0
            while True:
                request_attempt += 1
                request_sha256 = hashlib.sha256(
                    self._completion_body(messages)
                ).hexdigest()
                started = time.monotonic()
                try:
                    value, call_usage, raw = self._complete(messages)
                except AssistantProviderError as exc:
                    _emit_audit(
                        audit_hook,
                        {
                            "attempt": request_attempt,
                            "validation_round": validation_round + 1,
                            "retry": retry,
                            "request_sha256": request_sha256,
                            "response_sha256": None,
                            "outcome": "error",
                            "error_code": exc.code,
                            "elapsed_ms": _elapsed_ms(started),
                            "usage": _zero_usage(),
                        },
                    )
                    if exc.code in _RETRYABLE_ERRORS and retry < max_retries:
                        time.sleep(min(2.0, 0.25 * (2**retry)))
                        retry += 1
                        continue
                    if exc.code == "invalid_model_response" and validation_round == 0:
                        validation_errors = [
                            "response was not a valid structured JSON object"
                        ]
                        break
                    raise
                for name in usage:
                    usage[name] += call_usage[name]
                normalized, validation_errors = validator(value)
                response_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                if not validation_errors:
                    _emit_audit(
                        audit_hook,
                        {
                            "attempt": request_attempt,
                            "validation_round": validation_round + 1,
                            "retry": retry,
                            "request_sha256": request_sha256,
                            "response_sha256": response_sha256,
                            "outcome": "success",
                            "error_code": None,
                            "elapsed_ms": _elapsed_ms(started),
                            "usage": call_usage,
                        },
                    )
                    return normalized, usage
                _emit_audit(
                    audit_hook,
                    {
                        "attempt": request_attempt,
                        "validation_round": validation_round + 1,
                        "retry": retry,
                        "request_sha256": request_sha256,
                        "response_sha256": response_sha256,
                        "outcome": "invalid",
                        "error_code": "invalid_model_response",
                        "elapsed_ms": _elapsed_ms(started),
                        "usage": call_usage,
                    },
                )
                break
        raise AssistantProviderError("invalid_model_response")

    def _complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[dict[str, Any], dict[str, int], str]:
        body = self._completion_body(messages)
        request = Request(
            self.settings.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AI-Trade-Assistant/1",
            },
        )
        try:
            with self._opener.open(request, timeout=self.settings.timeout_seconds) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > self.settings.max_response_bytes:
                    raise AssistantProviderError("model_response_too_large")
                content = response.read(self.settings.max_response_bytes + 1)
        except AssistantProviderError:
            raise
        except HTTPError as exc:
            if exc.code == 429:
                raise AssistantProviderError("model_rate_limited") from None
            if exc.code in {401, 403}:
                raise AssistantProviderError("model_auth_error") from None
            if exc.code in {400, 404, 409, 422}:
                raise AssistantProviderError("model_request_rejected") from None
            if 500 <= exc.code <= 599:
                raise AssistantProviderError("model_server_error") from None
            raise AssistantProviderError("model_http_error") from None
        except (URLError, TimeoutError, socket.timeout, OSError, ValueError):
            raise AssistantProviderError("model_transport_error") from None
        if len(content) > self.settings.max_response_bytes:
            raise AssistantProviderError("model_response_too_large")
        try:
            response_value = loads_unique_json(content.decode("utf-8"))
            message = response_value["choices"][0]["message"]
            raw = message["content"]
            if not isinstance(raw, str) or len(raw) > self.settings.max_response_bytes:
                raise ValueError
            value = _parse_json_object(raw)
            raw_usage = response_value.get("usage") or {}
            usage = {
                "prompt_tokens": _safe_token_count(raw_usage.get("prompt_tokens")),
                "completion_tokens": _safe_token_count(raw_usage.get("completion_tokens")),
                "total_tokens": _safe_token_count(raw_usage.get("total_tokens")),
            }
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
            raise AssistantProviderError("invalid_model_response") from None
        return value, usage, raw

    def _completion_body(self, messages: list[dict[str, str]]) -> bytes:
        return json.dumps(
            {
                "model": self.settings.model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": MAX_COMPLETION_TOKENS,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, original_url: str):
        super().__init__()
        self._origin = _origin(original_url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        candidate = urljoin(req.full_url, newurl)
        try:
            _validate_url(candidate)
        except ValueError:
            raise AssistantProviderError("unsafe_model_redirect") from None
        if _origin(candidate) != self._origin:
            raise AssistantProviderError("unsafe_model_redirect")
        return super().redirect_request(req, fp, code, msg, headers, candidate)


def _completion_endpoint(base_url: str) -> str:
    _validate_url(base_url)
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions"
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    _validate_url(endpoint)
    return endpoint


def _validate_url(value: str) -> None:
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid URL")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL credentials, query strings, and fragments are forbidden")
    if not parsed.hostname or parsed.scheme not in {"https", "http"}:
        raise ValueError("unsupported URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid port") from exc
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("plain HTTP is restricted to loopback hosts")


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), str(parsed.hostname).rstrip(".").lower(), port


def _parse_json_object(raw: str) -> dict[str, Any]:
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1])
            if candidate.lstrip().lower().startswith("json\n"):
                candidate = candidate.lstrip()[5:]
    value = loads_unique_json(candidate)
    if not isinstance(value, dict):
        raise ValueError("model response is not an object")
    return value


def _validate_enhancement(
    value: dict[str, Any], allowed_ids: set[str]
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    _exact_keys(value, {"diagnosis", "assessment"}, "response", errors)
    diagnosis = value.get("diagnosis")
    assessment = value.get("assessment")
    if not isinstance(diagnosis, dict):
        errors.append("diagnosis must be an object")
        diagnosis = {}
    if not isinstance(assessment, dict):
        errors.append("assessment must be an object")
        assessment = {}
    _exact_keys(diagnosis, {"summary", "evidence_ids"}, "diagnosis", errors)
    _exact_keys(
        assessment,
        {
            "conclusion",
            "summary",
            "risk_level",
            "risk_budget_pct",
            "evidence_ids",
            "invalidation",
            "scenarios",
        },
        "assessment",
        errors,
    )

    diagnosis_summary = _bounded_text(diagnosis.get("summary"), 2000, "diagnosis.summary", errors)
    diagnosis_refs = _evidence_ids(
        diagnosis.get("evidence_ids"), allowed_ids, "diagnosis.evidence_ids", errors
    )
    conclusion = assessment.get("conclusion")
    if conclusion not in ALLOWED_CONCLUSIONS:
        errors.append("assessment.conclusion is invalid")
        conclusion = "NO_ACTION"
    assessment_summary = _bounded_text(
        assessment.get("summary"), 2000, "assessment.summary", errors
    )
    risk_level = assessment.get("risk_level")
    if risk_level not in {"LOW", "MEDIUM", "HIGH"}:
        errors.append("assessment.risk_level is invalid")
        risk_level = "HIGH"
    risk_budget = assessment.get("risk_budget_pct")
    if isinstance(risk_budget, bool) or not isinstance(risk_budget, int) or not 0 <= risk_budget <= 100:
        errors.append("assessment.risk_budget_pct is invalid")
        risk_budget = 0
    assessment_refs = _evidence_ids(
        assessment.get("evidence_ids"), allowed_ids, "assessment.evidence_ids", errors
    )
    invalidation = _text_list(
        assessment.get("invalidation"), 8, 500, "assessment.invalidation", errors
    )
    scenarios = _scenarios(assessment.get("scenarios"), errors)
    return (
        {
            "diagnosis": {
                "summary": diagnosis_summary,
                "evidence_ids": diagnosis_refs,
            },
            "assessment": {
                "conclusion": conclusion,
                "summary": assessment_summary,
                "risk_level": risk_level,
                "risk_budget_pct": risk_budget,
                "evidence_ids": assessment_refs,
                "invalidation": invalidation,
                "scenarios": scenarios,
            },
        },
        errors,
    )


def _validate_advocate(
    value: dict[str, Any], allowed_ids: set[str]
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    expected = {
        "summary",
        "arguments",
        "counterevidence",
        "abstained",
        "abstention_reason",
    }
    _exact_keys(value, expected, "advocate", errors)
    summary = _bounded_text(value.get("summary"), 2_000, "advocate.summary", errors)
    abstained = value.get("abstained")
    if not isinstance(abstained, bool):
        errors.append("advocate.abstained is invalid")
        abstained = True
    arguments = _claim_rows(
        value.get("arguments"),
        allowed_ids,
        "advocate.arguments",
        errors,
        allow_empty=abstained,
    )
    counterevidence = _claim_rows(
        value.get("counterevidence"),
        allowed_ids,
        "advocate.counterevidence",
        errors,
        allow_empty=False,
    )
    reason = value.get("abstention_reason")
    if abstained:
        reason = _bounded_text(
            reason, 500, "advocate.abstention_reason", errors
        )
    elif reason is not None:
        errors.append("advocate.abstention_reason must be null when not abstained")
        reason = None
    return (
        {
            "summary": summary,
            "arguments": arguments,
            "counterevidence": counterevidence,
            "abstained": abstained,
            "abstention_reason": reason,
        },
        errors,
    )


def _validate_judge(
    value: dict[str, Any],
    allowed_ids: set[str],
    allowed_bull_argument_ids: set[str],
    allowed_bear_argument_ids: set[str],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    _exact_keys(
        value,
        {"summary", "agreements", "conflicts", "unresolved_questions"},
        "judge",
        errors,
    )
    summary = _bounded_text(value.get("summary"), 2_000, "judge.summary", errors)
    agreements = _topic_rows(
        value.get("agreements"),
        allowed_ids,
        "judge.agreements",
        errors,
        maximum=6,
    )
    conflicts = _judge_conflicts(
        value.get("conflicts"),
        allowed_ids,
        allowed_bull_argument_ids,
        allowed_bear_argument_ids,
        errors,
    )
    questions = _question_rows(
        value.get("unresolved_questions"),
        allowed_ids,
        errors,
    )
    if not agreements and not conflicts and not questions:
        errors.append("judge must retain at least one agreement, conflict, or question")
    return (
        {
            "summary": summary,
            "agreements": agreements,
            "conflicts": conflicts,
            "unresolved_questions": questions,
        },
        errors,
    )


def _claim_rows(
    value: Any,
    allowed_ids: set[str],
    name: str,
    errors: list[str],
    *,
    allow_empty: bool,
) -> list[dict[str, Any]]:
    minimum = 0 if allow_empty else 1
    if not isinstance(value, list) or not minimum <= len(value) <= 4:
        errors.append(f"{name} is invalid")
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"{name}[{index}] is invalid")
            continue
        _exact_keys(item, {"claim", "evidence_ids"}, f"{name}[{index}]", errors)
        result.append(
            {
                "claim": _bounded_text(
                    item.get("claim"), 1_000, f"{name}[{index}].claim", errors
                ),
                "evidence_ids": _evidence_ids(
                    item.get("evidence_ids"),
                    allowed_ids,
                    f"{name}[{index}].evidence_ids",
                    errors,
                ),
            }
        )
    return result


def _topic_rows(
    value: Any,
    allowed_ids: set[str],
    name: str,
    errors: list[str],
    *,
    maximum: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        errors.append(f"{name} is invalid")
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"{name}[{index}] is invalid")
            continue
        _exact_keys(item, {"topic", "evidence_ids"}, f"{name}[{index}]", errors)
        result.append(
            {
                "topic": _bounded_text(
                    item.get("topic"), 1_000, f"{name}[{index}].topic", errors
                ),
                "evidence_ids": _evidence_ids(
                    item.get("evidence_ids"),
                    allowed_ids,
                    f"{name}[{index}].evidence_ids",
                    errors,
                ),
            }
        )
    return result


def _judge_conflicts(
    value: Any,
    allowed_ids: set[str],
    allowed_bull_argument_ids: set[str],
    allowed_bear_argument_ids: set[str],
    errors: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 8:
        errors.append("judge.conflicts is invalid")
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        name = f"judge.conflicts[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{name} is invalid")
            continue
        _exact_keys(
            item,
            {"topic", "bull_argument_ids", "bear_argument_ids", "evidence_ids"},
            name,
            errors,
        )
        result.append(
            {
                "topic": _bounded_text(
                    item.get("topic"), 1_000, f"{name}.topic", errors
                ),
                "bull_argument_ids": _bounded_identifiers(
                    item.get("bull_argument_ids"),
                    allowed_bull_argument_ids,
                    f"{name}.bull_argument_ids",
                    errors,
                ),
                "bear_argument_ids": _bounded_identifiers(
                    item.get("bear_argument_ids"),
                    allowed_bear_argument_ids,
                    f"{name}.bear_argument_ids",
                    errors,
                ),
                "evidence_ids": _evidence_ids(
                    item.get("evidence_ids"),
                    allowed_ids,
                    f"{name}.evidence_ids",
                    errors,
                ),
            }
        )
    return result


def _question_rows(
    value: Any, allowed_ids: set[str], errors: list[str]
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 8:
        errors.append("judge.unresolved_questions is invalid")
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        name = f"judge.unresolved_questions[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{name} is invalid")
            continue
        _exact_keys(item, {"question", "evidence_ids"}, name, errors)
        result.append(
            {
                "question": _bounded_text(
                    item.get("question"), 1_000, f"{name}.question", errors
                ),
                "evidence_ids": _evidence_ids(
                    item.get("evidence_ids"),
                    allowed_ids,
                    f"{name}.evidence_ids",
                    errors,
                ),
            }
        )
    return result


def _bounded_identifiers(
    value: Any,
    allowed: set[str],
    name: str,
    errors: list[str],
) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= min(8, len(allowed)):
        errors.append(f"{name} is invalid")
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in allowed or item in result:
            errors.append(f"{name} contains an unknown or duplicate identifier")
            continue
        result.append(item)
    return result


def _exact_keys(
    value: Any,
    expected: set[str],
    name: str,
    errors: list[str],
) -> None:
    if isinstance(value, dict) and set(value) != expected:
        errors.append(f"{name} fields are invalid")


def valid_advocate_shape(value: Any, allowed_ids: set[str]) -> bool:
    if not isinstance(value, dict):
        return False
    _, errors = _validate_advocate(value, allowed_ids)
    return not errors


def valid_judge_shape(
    value: Any,
    allowed_ids: set[str],
    allowed_bull_argument_ids: set[str],
    allowed_bear_argument_ids: set[str],
) -> bool:
    if not isinstance(value, dict):
        return False
    _, errors = _validate_judge(
        value,
        allowed_ids,
        allowed_bull_argument_ids,
        allowed_bear_argument_ids,
    )
    return not errors


def _bounded_text(value: Any, maximum: int, name: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        errors.append(f"{name} is invalid")
        return ""
    return value.strip()


def _evidence_ids(
    value: Any, allowed: set[str], name: str, errors: list[str]
) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(allowed):
        errors.append(f"{name} is invalid")
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in allowed or item in result:
            errors.append(f"{name} contains an unknown or duplicate reference")
            continue
        result.append(item)
    return result


def _text_list(
    value: Any, maximum_items: int, maximum_length: int, name: str, errors: list[str]
) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum_items:
        errors.append(f"{name} is invalid")
        return []
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > maximum_length:
            errors.append(f"{name} contains invalid text")
            continue
        result.append(item.strip())
    return result


def _scenarios(value: Any, errors: list[str]) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        errors.append("assessment.scenarios is invalid")
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            errors.append("assessment.scenarios contains a non-object")
            continue
        _exact_keys(
            item,
            {"name", "trigger", "implication"},
            "assessment.scenarios item",
            errors,
        )
        normalized = {}
        for name in ("name", "trigger", "implication"):
            normalized[name] = _bounded_text(
                item.get(name), 500, f"assessment.scenarios.{name}", errors
            )
        result.append(normalized)
    return result


def _safe_token_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _emit_audit(
    hook: Callable[[dict[str, Any]], None] | None,
    value: dict[str, Any],
) -> None:
    if hook is not None:
        hook(value)


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _zero_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _bounded_float(raw: str | None, default: float, minimum: float, maximum: float) -> float:
    value = default if raw is None or not raw.strip() else float(raw)
    if not minimum <= value <= maximum:
        raise ValueError("numeric value outside bounds")
    return value
