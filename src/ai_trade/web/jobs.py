from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from ..config import AppConfig
from ..json_utils import loads_unique_json


COMMANDS: dict[str, tuple[str, ...]] = {
    "refresh-data": ("download", "--force"),
    "refresh-market-intelligence": ("market-intelligence-refresh",),
    "refresh-market-breadth": ("market-breadth-refresh",),
    "backtest": ("backtest",),
    "walk-forward": ("walk-forward",),
    "validate": ("validate",),
    "paper-init": ("paper-init",),
    "paper-run": ("paper-run",),
    "paper-audit": ("paper-audit",),
    "cloud-backup": ("cloud-backup",),
}


_CLOSE_TIMEOUT_SECONDS = 5.0
_WEB_JOB_PROTOCOL_ENV = "AI_TRADE_WEB_JOB_PROTOCOL"
_CLOUD_BACKUP_EVENT_PREFIX = "@@AI_TRADE_CLOUD_BACKUP@@"
_CLOUD_BACKUP_STATUSES = {"succeeded", "failed", "cancelled"}


@dataclass
class Job:
    id: str
    action: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: _now())
    started_at: str | None = None
    finished_at: str | None = None
    return_code: int | None = None
    output: str = ""
    cancel_requested: bool = False
    cloud_backup_status: str | None = None
    cloud_backup_automatic: bool = False

    def payload(self, include_output: bool = True) -> dict[str, object]:
        value = {
            "id": self.id,
            "action": self.action,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
        }
        if include_output:
            value["output"] = self.output[-100_000:]
        if self.cloud_backup_status is not None:
            value["cloud_backup"] = {
                "status": self.cloud_backup_status,
                "automatic": self.cloud_backup_automatic,
            }
        return value


class JobManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.RLock()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_job_id: str | None = None
        self._closed = False
        self._worker = threading.Thread(
            target=self._work, name="ai-trade-jobs", daemon=True
        )
        self._worker.start()

    def submit(self, action: str) -> Job:
        if action not in COMMANDS:
            raise ValueError(f"Unsupported job action: {action}")
        with self._lock:
            if self._closed:
                raise RuntimeError("Job manager is closed")
            for value in self._jobs.values():
                if value.action == action and value.status in {"queued", "running"}:
                    return value
            job = Job(uuid4().hex[:16], action)
            self._jobs[job.id] = job
            self._prune()
            self._queue.put(job.id)
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            values = sorted(
                self._jobs.values(), key=lambda value: value.created_at, reverse=True
            )
            return [value.payload(include_output=False) for value in values[:30]]

    def cancel(self, job_id: str) -> Job:
        process: subprocess.Popen[str] | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("Job manager is closed")
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status == "queued":
                job.cancel_requested = True
                job.status = "cancelled"
                job.finished_at = _now()
                _assign_cloud_backup_status(job, None)
            elif job.status == "running" and self._active_job_id == job_id:
                job.cancel_requested = True
                process = self._active_process
        if process is not None:
            _request_termination(process)
        return job

    def close(self, timeout: float = _CLOSE_TIMEOUT_SECONDS) -> None:
        signal_worker = False
        with self._lock:
            if not self._closed:
                self._closed = True
                signal_worker = True
                finished_at = _now()
                for job in self._jobs.values():
                    if job.status == "queued":
                        job.cancel_requested = True
                        job.status = "cancelled"
                        job.finished_at = finished_at
                        _assign_cloud_backup_status(job, None)
                if self._active_job_id is not None:
                    active = self._jobs.get(self._active_job_id)
                    if active is not None:
                        active.cancel_requested = True
            process = self._active_process
        if process is not None:
            _request_termination(process)
        if signal_worker:
            self._queue.put(None)
        if threading.current_thread() is self._worker:
            return
        self._worker.join(timeout=max(0.0, timeout))
        if not self._worker.is_alive():
            return
        with self._lock:
            process = self._active_process
        if process is not None:
            _force_termination(process)
        self._worker.join(timeout=max(0.0, timeout))

    def _work(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            with self._lock:
                job = self._jobs.get(job_id)
                if self._closed or job is None or job.cancel_requested:
                    continue
                job.status = "running"
                job.started_at = _now()
                self._active_job_id = job.id
            command = [
                sys.executable,
                "-m",
                "ai_trade.cli",
                "--config",
                str(self.config.path),
                *COMMANDS[job.action],
            ]
            environment = os.environ.copy()
            for name in tuple(environment):
                if name.startswith("AI_TRADE_AI_"):
                    environment.pop(name, None)
            environment["PYTHONUTF8"] = "1"
            environment[_WEB_JOB_PROTOCOL_ENV] = "1"
            creation_flags = (
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.config.project_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    creationflags=creation_flags,
                )
                with self._lock:
                    self._active_process = process
                    should_terminate = self._closed or job.cancel_requested
                if should_terminate:
                    _request_termination(process)
                output, _ = process.communicate()
                output, automatic_cloud_status = _extract_cloud_backup_event(output)
                with self._lock:
                    job.output = output
                    job.return_code = process.returncode
                    job.status = (
                        "cancelled"
                        if job.cancel_requested
                        else "succeeded" if process.returncode == 0 else "failed"
                    )
                    _assign_cloud_backup_status(job, automatic_cloud_status)
            except Exception as exc:
                with self._lock:
                    if job.cancel_requested:
                        job.status = "cancelled"
                    else:
                        job.output = f"Unable to start job: {exc}"
                        job.return_code = -1
                        job.status = "failed"
                    _assign_cloud_backup_status(job, None)
            finally:
                with self._lock:
                    job.finished_at = _now()
                    self._active_process = None
                    self._active_job_id = None

    def _prune(self) -> None:
        completed = [
            value
            for value in self._jobs.values()
            if value.status in {"succeeded", "failed", "cancelled"}
        ]
        for value in sorted(completed, key=lambda item: item.created_at)[:-70]:
            self._jobs.pop(value.id, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_cloud_backup_event(output: str) -> tuple[str, str | None]:
    clean: list[str] = []
    status: str | None = None
    for line in output.splitlines(keepends=True):
        candidate = line.rstrip("\r\n")
        if candidate.startswith(_CLOUD_BACKUP_EVENT_PREFIX):
            try:
                payload = loads_unique_json(
                    candidate.removeprefix(_CLOUD_BACKUP_EVENT_PREFIX)
                )
            except (UnicodeError, ValueError):
                clean.append(line)
                continue
            if (
                isinstance(payload, dict)
                and payload.get("schema_version") == 1
                and payload.get("status") in {"succeeded", "failed"}
            ):
                status = str(payload["status"])
                continue
        clean.append(line)
    return "".join(clean), status


def _assign_cloud_backup_status(job: Job, automatic_status: str | None) -> None:
    if automatic_status in _CLOUD_BACKUP_STATUSES:
        job.cloud_backup_status = automatic_status
        job.cloud_backup_automatic = True
    elif job.action == "cloud-backup" and job.status in _CLOUD_BACKUP_STATUSES:
        job.cloud_backup_status = job.status
        job.cloud_backup_automatic = False


def _request_termination(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
    except OSError:
        pass


def _force_termination(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
