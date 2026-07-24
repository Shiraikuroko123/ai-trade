from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .provider import AssistantProviderError


SCHEMA_VERSION = 1
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_CONCURRENT_CALLS = 1
DEFAULT_MAX_TOKENS_PER_CALL = 50_000
DEFAULT_DAILY_TOKEN_BUDGET = 100_000
MAX_AUDIT_RECORD_BYTES = 1024 * 1024
MAX_CACHE_RECORD_BYTES = 1024 * 1024
MAX_AUDIT_RECORDS_PER_DAY = 10_000
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_CALL_FILE = re.compile(r"call_[0-9a-f]{32}\.json\Z")
_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_CALL_ROLES = {
    "research_assistant_wording",
    "research_debate_bull",
    "research_debate_bear",
    "research_debate_judge",
}
MAX_REQUEST_BYTES = 1024 * 1024


@dataclass(frozen=True)
class GovernanceSettings:
    max_retries: int = DEFAULT_MAX_RETRIES
    max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS
    max_tokens_per_call: int = DEFAULT_MAX_TOKENS_PER_CALL
    daily_token_budget: int = DEFAULT_DAILY_TOKEN_BUDGET
    input_cost_per_million_usd: float | None = None
    output_cost_per_million_usd: float | None = None
    daily_cost_budget_usd: float | None = None

    @classmethod
    def from_environment(cls) -> tuple[GovernanceSettings | None, str | None]:
        try:
            retries = _bounded_int(
                os.environ.get("AI_TRADE_AI_MAX_RETRIES"),
                DEFAULT_MAX_RETRIES,
                0,
                3,
            )
            concurrent = _bounded_int(
                os.environ.get("AI_TRADE_AI_MAX_CONCURRENT_CALLS"),
                DEFAULT_MAX_CONCURRENT_CALLS,
                1,
                8,
            )
            call_token_budget = _bounded_int(
                os.environ.get("AI_TRADE_AI_MAX_TOKENS_PER_CALL"),
                DEFAULT_MAX_TOKENS_PER_CALL,
                2_000,
                10_000_000,
            )
            token_budget = _bounded_int(
                os.environ.get("AI_TRADE_AI_DAILY_TOKEN_BUDGET"),
                DEFAULT_DAILY_TOKEN_BUDGET,
                1_000,
                100_000_000,
            )
            input_cost = _optional_bounded_float(
                os.environ.get("AI_TRADE_AI_INPUT_COST_PER_MILLION_USD"),
                0.0,
                100_000.0,
            )
            output_cost = _optional_bounded_float(
                os.environ.get("AI_TRADE_AI_OUTPUT_COST_PER_MILLION_USD"),
                0.0,
                100_000.0,
            )
            cost_budget = _optional_bounded_float(
                os.environ.get("AI_TRADE_AI_DAILY_COST_BUDGET_USD"),
                0.0,
                1_000_000.0,
            )
            if (input_cost is None) != (output_cost is None):
                raise ValueError("both model price rates are required")
            if cost_budget is not None and input_cost is None:
                raise ValueError("cost budget requires model price rates")
        except (TypeError, ValueError):
            return None, "AI model governance limits are invalid"
        return (
            cls(
                max_retries=retries,
                max_concurrent_calls=concurrent,
                max_tokens_per_call=call_token_budget,
                daily_token_budget=token_budget,
                input_cost_per_million_usd=input_cost,
                output_cost_per_million_usd=output_cost,
                daily_cost_budget_usd=cost_budget,
            ),
            None,
        )

    def public_status(self) -> dict[str, Any]:
        prices_available = self.input_cost_per_million_usd is not None
        return {
            "schema_version": SCHEMA_VERSION,
            "max_retries": self.max_retries,
            "max_concurrent_calls": self.max_concurrent_calls,
            "max_tokens_per_call": self.max_tokens_per_call,
            "daily_token_budget": self.daily_token_budget,
            "cost_accounting_available": prices_available,
            "daily_cost_budget_usd": self.daily_cost_budget_usd,
            "cache_enabled": True,
            "immutable_audit_enabled": True,
            "user_isolated": True,
            "stores_raw_prompt": False,
            "stores_raw_response": False,
            "stores_hidden_reasoning": False,
        }


class ModelCallGovernance:
    """Fail-closed model budget, cache, concurrency, and audit boundary."""

    def __init__(
        self,
        project_root: Path,
        settings: GovernanceSettings,
        *,
        model: str,
        endpoint: str,
        template_version: str,
        maximum_completion_tokens: int,
    ):
        self.settings = settings
        self.model = model
        self.endpoint_sha256 = _sha256_text(endpoint)
        self.template_version = template_version
        self.maximum_completion_tokens = maximum_completion_tokens
        state = Path(project_root) / "state"
        self.audit_root = state / "assistant_calls"
        self.cache_root = state / "assistant_model_cache"
        self._budget_lock = threading.RLock()
        self._reservations: dict[str, tuple[int, float]] = {}
        self._storage_failed = False
        semaphore_key = (
            os.path.normcase(str(Path(project_root).resolve())),
            settings.max_concurrent_calls,
        )
        with _SEMAPHORE_LOCK:
            self._semaphore = _SEMAPHORES.setdefault(
                semaphore_key,
                threading.BoundedSemaphore(settings.max_concurrent_calls),
            )

    def status(self) -> dict[str, Any]:
        return self.settings.public_status()

    def enhance(
        self,
        *,
        user_id: str,
        symbol: str,
        data_date: str,
        diagnosis: dict[str, Any],
        assessment: dict[str, Any],
        provider: Any,
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        return self.run_structured(
            user_id=user_id,
            role="research_assistant_wording",
            template_version=self.template_version,
            request_payload={
                "symbol": symbol,
                "data_date": data_date,
                "diagnosis": diagnosis,
                "assessment": assessment,
            },
            evidence=diagnosis.get("evidence"),
            provider_call=lambda max_retries, audit_hook: provider.enhance(
                symbol=symbol,
                data_date=data_date,
                diagnosis=diagnosis,
                assessment=assessment,
                max_retries=max_retries,
                audit_hook=audit_hook,
            ),
            result_validator=_valid_enhancement_shape,
        )

    def run_structured(
        self,
        *,
        user_id: str,
        role: str,
        template_version: str,
        request_payload: dict[str, Any],
        evidence: Any,
        provider_call: Callable[
            [int, Callable[[dict[str, Any]], None]],
            tuple[dict[str, Any], dict[str, int]],
        ],
        result_validator: Callable[[Any], bool],
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        if self._storage_failed:
            raise AssistantProviderError("model_audit_unavailable")
        if role not in _CALL_ROLES:
            raise ValueError("model call role is invalid")
        if (
            not isinstance(template_version, str)
            or not 1 <= len(template_version) <= 100
            or any(ord(character) < 32 for character in template_version)
        ):
            raise ValueError("model call template version is invalid")
        if not isinstance(request_payload, dict) or not callable(provider_call):
            raise TypeError("model structured request is invalid")
        if not callable(result_validator):
            raise TypeError("model structured validator is invalid")
        user_hash = _user_hash(user_id)
        try:
            with evidence_store_lock(
                self.audit_root / user_hash,
                "Assistant model budget",
            ):
                return self._call_user_locked(
                    user_hash=user_hash,
                    role=role,
                    template_version=template_version,
                    request_payload=request_payload,
                    evidence=evidence,
                    provider_call=provider_call,
                    result_validator=result_validator,
                )
        except AssistantProviderError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._storage_failed = True
            raise AssistantProviderError("model_audit_unavailable") from exc

    def _call_user_locked(
        self,
        *,
        user_hash: str,
        role: str,
        template_version: str,
        request_payload: dict[str, Any],
        evidence: Any,
        provider_call: Callable[
            [int, Callable[[dict[str, Any]], None]],
            tuple[dict[str, Any], dict[str, int]],
        ],
        result_validator: Callable[[Any], bool],
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        governed_request = {
            "role": role,
            "input": request_payload,
            "model": self.model,
            "endpoint_sha256": self.endpoint_sha256,
            "template_version": template_version,
        }
        request_bytes = _canonical_bytes(governed_request)
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise AssistantProviderError("model_request_too_large")
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        evidence_sha256 = _sha256_json(evidence)
        cache_key = request_sha256
        estimated_prompt_tokens = max(1, math.ceil(len(request_bytes) / 4) + 500)
        per_attempt_reserved_tokens = (
            estimated_prompt_tokens + self.maximum_completion_tokens
        )
        maximum_attempts = 2 * (self.settings.max_retries + 1)
        reserved_tokens = per_attempt_reserved_tokens * maximum_attempts
        reserved_cost = self._cost(
            estimated_prompt_tokens * maximum_attempts,
            self.maximum_completion_tokens * maximum_attempts,
        )
        call_id = uuid4().hex
        started_at = _now()
        budget_date_utc = _today()

        try:
            cached = self._load_cache(
                user_hash,
                cache_key,
                role=role,
                template_version=template_version,
                result_validator=result_validator,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            try:
                budget = self._budget_snapshot(user_hash, budget_date_utc)
                record = self._record(
                    call_id=call_id,
                    user_hash=user_hash,
                    role=role,
                    template_version=template_version,
                    started_at=started_at,
                    status="failed",
                    request_sha256=request_sha256,
                    evidence_sha256=evidence_sha256,
                    response_sha256=None,
                    cache_hit=False,
                    budget={**budget, "decision": "model_cache_integrity_error"},
                    attempts=[],
                    usage=_zero_usage(),
                    accounted_tokens=0,
                    estimated_cost_usd=0.0 if reserved_cost is not None else None,
                    error_code="model_cache_integrity_error",
                )
                summary = self._commit_audit(user_hash, record)
            except AssistantProviderError:
                self._storage_failed = True
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as audit_exc:
                self._storage_failed = True
                raise AssistantProviderError(
                    "model_audit_integrity_error"
                ) from audit_exc
            self._storage_failed = True
            raise AssistantProviderError(
                "model_cache_integrity_error", audit=summary
            ) from exc
        if cached is not None:
            try:
                budget = self._budget_snapshot(user_hash, budget_date_utc)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._storage_failed = True
                raise AssistantProviderError("model_audit_integrity_error") from exc
            usage = _zero_usage()
            record = self._record(
                call_id=call_id,
                user_hash=user_hash,
                role=role,
                template_version=template_version,
                started_at=started_at,
                status="cache_hit",
                request_sha256=request_sha256,
                evidence_sha256=evidence_sha256,
                response_sha256=cached["response_sha256"],
                cache_hit=True,
                budget={**budget, "decision": "cache_not_charged"},
                attempts=[],
                usage=usage,
                accounted_tokens=0,
                estimated_cost_usd=0.0 if reserved_cost is not None else None,
                error_code=None,
                cached_usage=cached["usage"],
            )
            summary = self._commit_audit(user_hash, record)
            return cached["enhancement"], usage, summary

        try:
            reservation, budget = self._reserve(
                user_hash,
                budget_date_utc=budget_date_utc,
                reserved_tokens=reserved_tokens,
                reserved_cost=reserved_cost,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._storage_failed = True
            raise AssistantProviderError("model_audit_integrity_error") from exc
        if reservation is None:
            error_code = str(budget["decision"])
            record = self._record(
                call_id=call_id,
                user_hash=user_hash,
                role=role,
                template_version=template_version,
                started_at=started_at,
                status="denied",
                request_sha256=request_sha256,
                evidence_sha256=evidence_sha256,
                response_sha256=None,
                cache_hit=False,
                budget=budget,
                attempts=[],
                usage=_zero_usage(),
                accounted_tokens=0,
                estimated_cost_usd=0.0 if reserved_cost is not None else None,
                error_code=error_code,
            )
            summary = self._commit_audit(user_hash, record)
            raise AssistantProviderError(error_code, audit=summary)

        acquired = self._semaphore.acquire(blocking=False)
        if not acquired:
            self._release(user_hash, reservation)
            budget["decision"] = "model_concurrency_limited"
            record = self._record(
                call_id=call_id,
                user_hash=user_hash,
                role=role,
                template_version=template_version,
                started_at=started_at,
                status="denied",
                request_sha256=request_sha256,
                evidence_sha256=evidence_sha256,
                response_sha256=None,
                cache_hit=False,
                budget=budget,
                attempts=[],
                usage=_zero_usage(),
                accounted_tokens=0,
                estimated_cost_usd=0.0 if reserved_cost is not None else None,
                error_code="model_concurrency_limited",
            )
            summary = self._commit_audit(user_hash, record)
            raise AssistantProviderError("model_concurrency_limited", audit=summary)

        attempts: list[dict[str, Any]] = []

        def audit_hook(value: dict[str, Any]) -> None:
            try:
                attempts.append(_sanitize_attempt(value, len(attempts) + 1))
            except (TypeError, ValueError) as exc:
                raise AssistantProviderError("model_attempt_audit_invalid") from exc

        try:
            enhancement, usage = provider_call(
                self.settings.max_retries,
                audit_hook,
            )
            if not result_validator(enhancement):
                raise AssistantProviderError("invalid_model_response")
            usage = _normalize_usage(usage)
            if not attempts:
                attempts.append(
                    {
                        "attempt": 1,
                        "validation_round": 1,
                        "retry": 0,
                        "request_sha256": request_sha256,
                        "response_sha256": _sha256_json(enhancement),
                        "outcome": "success",
                        "error_code": None,
                        "elapsed_ms": 0,
                        "usage": usage,
                    }
                )
            if _sum_attempt_usage(attempts) != usage:
                raise AssistantProviderError("model_usage_audit_mismatch")
            response_sha256 = _sha256_json(enhancement)
            accounted_tokens, accounted_cost = self._account_attempts(
                attempts,
                estimated_prompt_tokens=estimated_prompt_tokens,
                per_attempt_reserved_tokens=per_attempt_reserved_tokens,
            )
            final_budget = self._complete_budget(
                budget,
                accounted_tokens=accounted_tokens,
                accounted_cost=accounted_cost,
            )
            record = self._record(
                call_id=call_id,
                user_hash=user_hash,
                role=role,
                template_version=template_version,
                started_at=started_at,
                status="success",
                request_sha256=request_sha256,
                evidence_sha256=evidence_sha256,
                response_sha256=response_sha256,
                cache_hit=False,
                budget=final_budget,
                attempts=attempts,
                usage=usage,
                accounted_tokens=accounted_tokens,
                estimated_cost_usd=accounted_cost,
                error_code=None,
            )
            self._store_cache(
                user_hash,
                cache_key,
                role=role,
                template_version=template_version,
                result_validator=result_validator,
                request_sha256=request_sha256,
                evidence_sha256=evidence_sha256,
                response_sha256=response_sha256,
                enhancement=enhancement,
                usage=usage,
                source_record=record,
            )
            summary = self._commit_audit(user_hash, record)
            return enhancement, usage, summary
        except AssistantProviderError as exc:
            if exc.code == "model_audit_unavailable" and self._storage_failed:
                raise
            usage = _sum_attempt_usage(attempts)
            accounted_tokens, accounted_cost = self._account_attempts(
                attempts,
                estimated_prompt_tokens=estimated_prompt_tokens,
                per_attempt_reserved_tokens=per_attempt_reserved_tokens,
            )
            final_budget = self._complete_budget(
                budget,
                accounted_tokens=accounted_tokens,
                accounted_cost=accounted_cost,
            )
            record = self._record(
                call_id=call_id,
                user_hash=user_hash,
                role=role,
                template_version=template_version,
                started_at=started_at,
                status="failed",
                request_sha256=request_sha256,
                evidence_sha256=evidence_sha256,
                response_sha256=None,
                cache_hit=False,
                budget=final_budget,
                attempts=attempts,
                usage=usage,
                accounted_tokens=accounted_tokens,
                estimated_cost_usd=accounted_cost,
                error_code=exc.code,
            )
            summary = self._commit_audit(user_hash, record)
            if exc.code.startswith("model_cache_"):
                self._storage_failed = True
            raise AssistantProviderError(exc.code, audit=summary) from None
        finally:
            self._release(user_hash, reservation)
            self._semaphore.release()

    def _reserve(
        self,
        user_hash: str,
        *,
        budget_date_utc: str,
        reserved_tokens: int,
        reserved_cost: float | None,
    ) -> tuple[tuple[int, float] | None, dict[str, Any]]:
        with self._budget_lock:
            snapshot = self._budget_snapshot(user_hash, budget_date_utc)
            inflight_tokens, inflight_cost = self._reservations.get(user_hash, (0, 0.0))
            snapshot.update(
                {
                    "reserved_tokens": reserved_tokens,
                    "reserved_cost_usd": reserved_cost,
                    "inflight_tokens": inflight_tokens,
                    "inflight_cost_usd": round(inflight_cost, 8),
                }
            )
            if reserved_tokens > self.settings.max_tokens_per_call:
                snapshot["decision"] = "model_call_token_budget_exceeded"
                return None, snapshot
            if (
                snapshot["tokens_used_before"]
                + inflight_tokens
                + reserved_tokens
                > self.settings.daily_token_budget
            ):
                snapshot["decision"] = "model_daily_token_budget_exceeded"
                return None, snapshot
            cost_limit = self.settings.daily_cost_budget_usd
            if (
                cost_limit is not None
                and reserved_cost is not None
                and snapshot["cost_used_before_usd"] + inflight_cost + reserved_cost
                > cost_limit
            ):
                snapshot["decision"] = "model_daily_cost_budget_exceeded"
                return None, snapshot
            self._reservations[user_hash] = (
                inflight_tokens + reserved_tokens,
                inflight_cost + (reserved_cost or 0.0),
            )
            snapshot["decision"] = "allowed"
            return (reserved_tokens, reserved_cost or 0.0), snapshot

    def _release(self, user_hash: str, reservation: tuple[int, float]) -> None:
        with self._budget_lock:
            current_tokens, current_cost = self._reservations.get(user_hash, (0, 0.0))
            remaining = (
                max(0, current_tokens - reservation[0]),
                max(0.0, current_cost - reservation[1]),
            )
            if remaining == (0, 0.0):
                self._reservations.pop(user_hash, None)
            else:
                self._reservations[user_hash] = remaining

    def _complete_budget(
        self,
        budget: dict[str, Any],
        *,
        accounted_tokens: int,
        accounted_cost: float | None,
    ) -> dict[str, Any]:
        result = dict(budget)
        result["accounted_tokens"] = accounted_tokens
        result["accounted_cost_usd"] = accounted_cost
        result["tokens_used_after"] = result["tokens_used_before"] + accounted_tokens
        result["cost_used_after_usd"] = round(
            result["cost_used_before_usd"] + (accounted_cost or 0.0), 8
        )
        return result

    def _budget_snapshot(
        self, user_hash: str, budget_date_utc: str
    ) -> dict[str, Any]:
        tokens = 0
        cost = 0.0
        if not _valid_date_text(budget_date_utc):
            raise RuntimeError("Assistant audit budget date is invalid")
        directory = self.audit_root / user_hash / budget_date_utc
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("Assistant audit day directory is invalid")
            paths = list(directory.iterdir())
            if len(paths) > MAX_AUDIT_RECORDS_PER_DAY:
                raise RuntimeError("Assistant audit day exceeds its record limit")
            for path in paths:
                if path.is_symlink() or not path.is_file() or not _CALL_FILE.fullmatch(path.name):
                    raise RuntimeError("Assistant audit directory contains an invalid entry")
                value = load_unique_json(path, max_bytes=MAX_AUDIT_RECORD_BYTES)
                if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
                    raise RuntimeError("Assistant audit record is invalid")
                if value.get("record_sha256") != _audit_record_fingerprint(value):
                    raise RuntimeError("Assistant audit record fingerprint is invalid")
                if value.get("user_scope_sha256") != user_hash:
                    raise RuntimeError("Assistant audit user scope is invalid")
                accounted = value.get("accounted_tokens", 0)
                if isinstance(accounted, bool) or not isinstance(accounted, int) or accounted < 0:
                    raise RuntimeError("Assistant audit token accounting is invalid")
                amount = value.get("estimated_cost_usd")
                if amount is not None and (
                    isinstance(amount, bool)
                    or not isinstance(amount, (int, float))
                    or not math.isfinite(float(amount))
                    or float(amount) < 0
                ):
                    raise RuntimeError("Assistant audit cost accounting is invalid")
                tokens += accounted
                cost += float(amount or 0.0)
        return {
            "decision": "pending",
            "date_utc": budget_date_utc,
            "daily_token_limit": self.settings.daily_token_budget,
            "tokens_used_before": tokens,
            "daily_cost_limit_usd": self.settings.daily_cost_budget_usd,
            "cost_used_before_usd": round(cost, 8),
            "cost_accounting_available": (
                self.settings.input_cost_per_million_usd is not None
            ),
        }

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> float | None:
        if self.settings.input_cost_per_million_usd is None:
            return None
        value = (
            prompt_tokens * self.settings.input_cost_per_million_usd
            + completion_tokens * float(self.settings.output_cost_per_million_usd)
        ) / 1_000_000
        return round(value, 8)

    def _account_attempts(
        self,
        attempts: list[dict[str, Any]],
        *,
        estimated_prompt_tokens: int,
        per_attempt_reserved_tokens: int,
    ) -> tuple[int, float | None]:
        selected = attempts or [{"usage": _zero_usage()}]
        tokens = 0
        cost = 0.0 if self.settings.input_cost_per_million_usd is not None else None
        for attempt in selected:
            usage = _normalize_usage(attempt.get("usage"))
            if usage["total_tokens"]:
                tokens += usage["total_tokens"]
                attempt_cost = self._cost(
                    usage["prompt_tokens"], usage["completion_tokens"]
                )
            else:
                tokens += per_attempt_reserved_tokens
                attempt_cost = self._cost(
                    estimated_prompt_tokens, self.maximum_completion_tokens
                )
            if cost is not None:
                cost += float(attempt_cost or 0.0)
        return tokens, round(cost, 8) if cost is not None else None

    def _record(
        self,
        *,
        call_id: str,
        user_hash: str,
        role: str,
        template_version: str,
        started_at: str,
        status: str,
        request_sha256: str,
        evidence_sha256: str,
        response_sha256: str | None,
        cache_hit: bool,
        budget: dict[str, Any],
        attempts: list[dict[str, Any]],
        usage: dict[str, int],
        accounted_tokens: int,
        estimated_cost_usd: float | None,
        error_code: str | None,
        cached_usage: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        record = {
            "schema_version": SCHEMA_VERSION,
            "call_id": call_id,
            "created_at": started_at,
            "completed_at": _now(),
            "user_scope_sha256": user_hash,
            "provider": "openai-compatible",
            "role": role,
            "model": self.model,
            "endpoint_sha256": self.endpoint_sha256,
            "template_version": template_version,
            "request_sha256": request_sha256,
            "evidence_sha256": evidence_sha256,
            "response_sha256": response_sha256,
            "status": status,
            "error_code": error_code,
            "cache": {"hit": cache_hit, "key_sha256": request_sha256},
            "budget": budget,
            "attempts": attempts,
            "usage": usage,
            "cached_usage": cached_usage,
            "accounted_tokens": accounted_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "authority": "research_only",
            "execution_authorized": False,
            "record_sha256": None,
            "content_retention": {
                "raw_prompt": False,
                "raw_response": False,
                "hidden_reasoning": False,
            },
        }
        record["record_sha256"] = _audit_record_fingerprint(record)
        return record

    def _commit_audit(
        self, user_hash: str, record: dict[str, Any]
    ) -> dict[str, Any]:
        content = _canonical_bytes(record) + b"\n"
        if len(content) > MAX_AUDIT_RECORD_BYTES:
            raise AssistantProviderError("model_audit_record_too_large")
        budget_date_utc = str(record.get("budget", {}).get("date_utc") or "")
        if not _valid_date_text(budget_date_utc):
            raise AssistantProviderError("model_audit_record_invalid")
        directory = self.audit_root / user_hash / budget_date_utc
        target = directory / f"call_{record['call_id']}.json"
        try:
            _secure_directory(self.audit_root, self.audit_root)
            _secure_directory(self.audit_root, self.audit_root / user_hash)
            _secure_directory(self.audit_root, directory)
            if len(list(directory.iterdir())) >= MAX_AUDIT_RECORDS_PER_DAY:
                raise RuntimeError("Assistant audit day reached its record limit")
            atomic_create_json(
                self.audit_root,
                target,
                record,
                label="assistant audit",
                maximum_bytes=MAX_AUDIT_RECORD_BYTES,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._storage_failed = True
            raise AssistantProviderError("model_audit_unavailable") from exc
        return _audit_summary(record)


    def _load_cache(
        self,
        user_hash: str,
        cache_key: str,
        *,
        role: str,
        template_version: str,
        result_validator: Callable[[Any], bool],
    ) -> dict[str, Any] | None:
        path = self._cache_path(user_hash, cache_key)
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Assistant model cache path is invalid")
        value = load_unique_json(path, max_bytes=MAX_CACHE_RECORD_BYTES)
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("Assistant model cache record is invalid")
        if (
            value.get("cache_key") != cache_key
            or value.get("request_sha256") != cache_key
            or value.get("role") != role
            or value.get("model") != self.model
            or value.get("endpoint_sha256") != self.endpoint_sha256
            or value.get("template_version") != template_version
        ):
            raise RuntimeError("Assistant model cache identity is invalid")
        if _FINGERPRINT.fullmatch(str(value.get("evidence_sha256", ""))) is None:
            raise RuntimeError("Assistant model cache evidence hash is invalid")
        if value.get("record_sha256") != _cache_record_fingerprint(value):
            raise RuntimeError("Assistant model cache fingerprint is invalid")
        enhancement = value.get("enhancement")
        usage = _normalize_usage(value.get("usage"))
        if not result_validator(enhancement):
            raise RuntimeError("Assistant model cache enhancement is invalid")
        if value.get("response_sha256") != _sha256_json(enhancement):
            raise RuntimeError("Assistant model cache response hash is invalid")
        self._validate_cache_source(
            user_hash,
            value,
            role=role,
            template_version=template_version,
        )
        return {
            "enhancement": enhancement,
            "usage": usage,
            "response_sha256": value["response_sha256"],
        }

    def _store_cache(
        self,
        user_hash: str,
        cache_key: str,
        *,
        role: str,
        template_version: str,
        result_validator: Callable[[Any], bool],
        request_sha256: str,
        evidence_sha256: str,
        response_sha256: str,
        enhancement: dict[str, Any],
        usage: dict[str, int],
        source_record: dict[str, Any],
    ) -> None:
        source_content_sha256 = hashlib.sha256(
            _canonical_bytes(source_record) + b"\n"
        ).hexdigest()
        value = {
            "schema_version": SCHEMA_VERSION,
            "cache_key": cache_key,
            "created_at": _now(),
            "role": role,
            "model": self.model,
            "endpoint_sha256": self.endpoint_sha256,
            "template_version": template_version,
            "request_sha256": request_sha256,
            "evidence_sha256": evidence_sha256,
            "response_sha256": response_sha256,
            "enhancement": enhancement,
            "usage": usage,
            "source_call_id": source_record["call_id"],
            "source_audit_date_utc": source_record["budget"]["date_utc"],
            "source_audit_record_sha256": source_content_sha256,
            "authority": "research_only",
            "record_sha256": None,
        }
        value["record_sha256"] = _cache_record_fingerprint(value)
        content = _canonical_bytes(value) + b"\n"
        if len(content) > MAX_CACHE_RECORD_BYTES:
            raise AssistantProviderError("model_cache_record_too_large")
        path = self._cache_path(user_hash, cache_key)
        try:
            _secure_directory(self.cache_root, self.cache_root)
            _secure_directory(self.cache_root, self.cache_root / user_hash)
            _secure_directory(self.cache_root, path.parent)
            try:
                atomic_create_json(
                    self.cache_root,
                    path,
                    value,
                    label="assistant model cache",
                    maximum_bytes=MAX_CACHE_RECORD_BYTES,
                )
            except FileExistsError:
                if self._load_cache(
                    user_hash,
                    cache_key,
                    role=role,
                    template_version=template_version,
                    result_validator=result_validator,
                ) is None:
                    raise RuntimeError("Existing model cache record is invalid")
        except (OSError, RuntimeError, ValueError) as exc:
            raise AssistantProviderError("model_cache_unavailable") from exc

    def _cache_path(self, user_hash: str, cache_key: str) -> Path:
        return self.cache_root / user_hash / cache_key[:2] / f"{cache_key}.json"

    def _validate_cache_source(
        self,
        user_hash: str,
        cache_record: dict[str, Any],
        *,
        role: str,
        template_version: str,
    ) -> None:
        call_id = str(cache_record.get("source_call_id") or "")
        date_utc = str(cache_record.get("source_audit_date_utc") or "")
        expected_content_hash = str(
            cache_record.get("source_audit_record_sha256") or ""
        )
        if (
            re.fullmatch(r"[0-9a-f]{32}", call_id) is None
            or not _valid_date_text(date_utc)
            or _FINGERPRINT.fullmatch(expected_content_hash) is None
        ):
            raise RuntimeError("Assistant model cache source reference is invalid")
        path = self.audit_root / user_hash / date_utc / f"call_{call_id}.json"
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Assistant model cache source audit is unavailable")
        source = load_unique_json(path, max_bytes=MAX_AUDIT_RECORD_BYTES)
        if (
            not isinstance(source, dict)
            or source.get("record_sha256") != _audit_record_fingerprint(source)
            or source.get("user_scope_sha256") != user_hash
            or source.get("status") != "success"
            or source.get("request_sha256") != cache_record.get("request_sha256")
            or source.get("evidence_sha256") != cache_record.get("evidence_sha256")
            or source.get("response_sha256") != cache_record.get("response_sha256")
            or source.get("role") != role
            or source.get("model") != self.model
            or source.get("endpoint_sha256") != self.endpoint_sha256
            or source.get("template_version") != template_version
        ):
            raise RuntimeError("Assistant model cache source audit is invalid")
        actual_content_hash = hashlib.sha256(
            _canonical_bytes(source) + b"\n"
        ).hexdigest()
        if actual_content_hash != expected_content_hash:
            raise RuntimeError("Assistant model cache source audit hash is invalid")


def verify_call_audit_summary(
    project_root: Path,
    user_id: str,
    summary: Any,
) -> bool:
    """Match one public call summary to its immutable per-user audit record."""

    if not isinstance(summary, dict) or set(summary) != _AUDIT_SUMMARY_FIELDS:
        return False
    call_id = summary.get("call_id")
    record_sha256 = summary.get("audit_record_sha256")
    budget = summary.get("budget")
    date_utc = budget.get("date_utc") if isinstance(budget, dict) else None
    if (
        not isinstance(call_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", call_id) is None
        or not isinstance(record_sha256, str)
        or _FINGERPRINT.fullmatch(record_sha256) is None
        or not isinstance(date_utc, str)
        or not _valid_date_text(date_utc)
    ):
        return False
    try:
        user_hash = _user_hash(user_id)
        audit_root = Path(project_root) / "state" / "assistant_calls"
        user_root = audit_root / user_hash
        date_root = user_root / date_utc
        path = date_root / f"call_{call_id}.json"
        for directory in (audit_root, user_root, date_root):
            if directory.is_symlink() or not directory.is_dir():
                return False
        if path.is_symlink() or not path.is_file():
            return False
        value = load_unique_json(path, max_bytes=MAX_AUDIT_RECORD_BYTES)
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != SCHEMA_VERSION
            or value.get("call_id") != call_id
            or value.get("user_scope_sha256") != user_hash
            or value.get("record_sha256") != _audit_record_fingerprint(value)
        ):
            return False
        return _audit_summary(value) == summary
    except (AttributeError, KeyError, OSError, UnicodeError, TypeError, ValueError):
        return False


_AUDIT_SUMMARY_FIELDS = {
    "schema_version",
    "call_id",
    "role",
    "model",
    "template_version",
    "status",
    "error_code",
    "cache_hit",
    "audit_record_sha256",
    "attempt_count",
    "retry_count",
    "latency_ms",
    "usage",
    "cached_usage",
    "estimated_cost_usd",
    "budget",
}


def _audit_summary(record: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(_canonical_bytes(record) + b"\n").hexdigest()
    attempts = record["attempts"]
    budget = record["budget"]
    return {
        "schema_version": SCHEMA_VERSION,
        "call_id": record["call_id"],
        "role": record["role"],
        "model": record["model"],
        "template_version": record["template_version"],
        "status": record["status"],
        "error_code": record["error_code"],
        "cache_hit": record["cache"]["hit"],
        "audit_record_sha256": digest,
        "attempt_count": len(attempts),
        "retry_count": sum(int(item.get("retry", 0) > 0) for item in attempts),
        "latency_ms": sum(int(item.get("elapsed_ms", 0)) for item in attempts),
        "usage": record["usage"],
        "cached_usage": record["cached_usage"],
        "estimated_cost_usd": record["estimated_cost_usd"],
        "budget": {
            "decision": budget.get("decision"),
            "date_utc": budget.get("date_utc"),
            "daily_token_limit": budget.get("daily_token_limit"),
            "tokens_used_before": budget.get("tokens_used_before"),
            "accounted_tokens": budget.get("accounted_tokens", 0),
            "tokens_used_after": budget.get(
                "tokens_used_after", budget.get("tokens_used_before")
            ),
            "cost_accounting_available": budget.get(
                "cost_accounting_available", False
            ),
            "daily_cost_limit_usd": budget.get("daily_cost_limit_usd"),
            "cost_used_before_usd": budget.get("cost_used_before_usd"),
            "accounted_cost_usd": budget.get("accounted_cost_usd"),
            "cost_used_after_usd": budget.get(
                "cost_used_after_usd", budget.get("cost_used_before_usd")
            ),
        },
    }


def _sanitize_attempt(value: dict[str, Any], fallback_attempt: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("Model attempt audit must be an object")
    request_hash = str(value.get("request_sha256") or "")
    response_hash = value.get("response_sha256")
    if _FINGERPRINT.fullmatch(request_hash) is None:
        raise ValueError("Model attempt request hash is invalid")
    if response_hash is not None and _FINGERPRINT.fullmatch(str(response_hash)) is None:
        raise ValueError("Model attempt response hash is invalid")
    outcome = str(value.get("outcome") or "error")
    if outcome not in {"success", "error", "invalid"}:
        raise ValueError("Model attempt outcome is invalid")
    return {
        "attempt": _positive_int(value.get("attempt"), fallback_attempt),
        "validation_round": _positive_int(value.get("validation_round"), 1),
        "retry": _nonnegative_int(value.get("retry"), 0),
        "request_sha256": request_hash,
        "response_sha256": response_hash,
        "outcome": outcome,
        "error_code": (
            str(value.get("error_code"))[:100] if value.get("error_code") else None
        ),
        "elapsed_ms": min(3_600_000, _nonnegative_int(value.get("elapsed_ms"), 0)),
        "usage": _normalize_usage(value.get("usage")),
    }


def _valid_enhancement_shape(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"diagnosis", "assessment"}:
        return False
    diagnosis = value.get("diagnosis")
    assessment = value.get("assessment")
    return (
        isinstance(diagnosis, dict)
        and isinstance(diagnosis.get("summary"), str)
        and isinstance(diagnosis.get("evidence_ids"), list)
        and isinstance(assessment, dict)
        and isinstance(assessment.get("summary"), str)
        and isinstance(assessment.get("evidence_ids"), list)
        and isinstance(assessment.get("invalidation"), list)
        and isinstance(assessment.get("scenarios"), list)
        and isinstance(assessment.get("conclusion"), str)
    )


def _sum_attempt_usage(attempts: list[dict[str, Any]]) -> dict[str, int]:
    result = _zero_usage()
    for attempt in attempts:
        usage = _normalize_usage(attempt.get("usage"))
        for key in result:
            result[key] += usage[key]
    return result


def _normalize_usage(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    prompt = _nonnegative_int(source.get("prompt_tokens"), 0)
    completion = _nonnegative_int(source.get("completion_tokens"), 0)
    total = max(
        prompt + completion,
        _nonnegative_int(source.get("total_tokens"), prompt + completion),
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _zero_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _secure_directory(root: Path, directory: Path) -> None:
    root = Path(root)
    directory = Path(directory)
    try:
        directory.relative_to(root)
    except ValueError as exc:
        if directory != root:
            raise ValueError("Assistant storage path escapes its root") from exc
    current = root.parent
    if current.is_symlink():
        raise RuntimeError("Assistant storage parent must not be symbolic")
    current.mkdir(parents=True, exist_ok=True)
    if not current.is_dir():
        raise RuntimeError("Assistant storage parent must be a directory")
    for part in directory.relative_to(root.parent).parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError("Assistant storage directory must not be symbolic")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise RuntimeError("Assistant storage path must be a directory")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _audit_record_fingerprint(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload["record_sha256"] = None
    return _sha256_json(payload)


def _cache_record_fingerprint(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload["record_sha256"] = None
    return _sha256_json(payload)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user_hash(user_id: str) -> str:
    if not isinstance(user_id, str):
        raise TypeError("user_id must be a string")
    normalized = user_id.strip()
    if not normalized or len(normalized.encode("utf-8")) > 256 or "\x00" in normalized:
        raise ValueError("user_id must contain between 1 and 256 UTF-8 bytes")
    return _sha256_text(normalized)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _valid_date_text(value: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
    except (TypeError, ValueError):
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_int(raw: str | None, default: int, minimum: int, maximum: int) -> int:
    value = default if raw is None or not raw.strip() else int(raw)
    if not minimum <= value <= maximum:
        raise ValueError("integer outside bounds")
    return value


def _optional_bounded_float(
    raw: str | None, minimum: float, maximum: float
) -> float | None:
    if raw is None or not raw.strip():
        return None
    value = float(raw)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("number outside bounds")
    return value


def _positive_int(value: Any, default: int) -> int:
    parsed = _nonnegative_int(value, default)
    return parsed if parsed > 0 else default


def _nonnegative_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


__all__ = [
    "GovernanceSettings",
    "ModelCallGovernance",
    "verify_call_audit_summary",
]
