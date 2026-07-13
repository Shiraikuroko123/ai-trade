from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .features import ALLOWED_CONCLUSIONS


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 512 * 1024


class AssistantProviderError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
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
    ) -> tuple[dict[str, Any], dict[str, int]]:
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
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        previous = ""
        validation_errors: list[str] = []
        for attempt in range(2):
            messages = [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": json.dumps(public_input, ensure_ascii=False, separators=(",", ":")),
                },
            ]
            if attempt:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The previous JSON failed validation. Return a corrected JSON object. "
                            f"Errors: {validation_errors}. Previous public output: {previous[:12000]}"
                        ),
                    }
                )
            try:
                value, call_usage, raw = self._complete(messages)
            except AssistantProviderError as exc:
                if exc.code == "invalid_model_response" and attempt == 0:
                    validation_errors = ["response was not a valid structured JSON object"]
                    continue
                raise
            for name in usage:
                usage[name] += call_usage[name]
            normalized, validation_errors = _validate_enhancement(value, allowed_ids)
            if not validation_errors:
                return normalized, usage
            previous = raw
        raise AssistantProviderError("invalid_model_response")

    def _complete(
        self, messages: list[dict[str, str]]
    ) -> tuple[dict[str, Any], dict[str, int], str]:
        body = json.dumps(
            {
                "model": self.settings.model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1400,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
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
            raise AssistantProviderError("model_http_error") from None
        except (URLError, TimeoutError, socket.timeout, OSError, ValueError):
            raise AssistantProviderError("model_transport_error") from None
        if len(content) > self.settings.max_response_bytes:
            raise AssistantProviderError("model_response_too_large")
        try:
            response_value = json.loads(content.decode("utf-8"))
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
        except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise AssistantProviderError("invalid_model_response") from None
        return value, usage, raw


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
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("model response is not an object")
    return value


def _validate_enhancement(
    value: dict[str, Any], allowed_ids: set[str]
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    diagnosis = value.get("diagnosis")
    assessment = value.get("assessment")
    if not isinstance(diagnosis, dict):
        errors.append("diagnosis must be an object")
        diagnosis = {}
    if not isinstance(assessment, dict):
        errors.append("assessment must be an object")
        assessment = {}

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
        normalized = {}
        for name in ("name", "trigger", "implication"):
            normalized[name] = _bounded_text(
                item.get(name), 500, f"assessment.scenarios.{name}", errors
            )
        result.append(normalized)
    return result


def _safe_token_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _bounded_float(raw: str | None, default: float, minimum: float, maximum: float) -> float:
    value = default if raw is None or not raw.strip() else float(raw)
    if not minimum <= value <= maximum:
        raise ValueError("numeric value outside bounds")
    return value
