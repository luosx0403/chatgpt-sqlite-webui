from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cli import run_import_pipeline
from .db import connect, get_stats, verify_database
from .logging_utils import get_logger, parse_log_level
from .web_db import create_web_indexes

LOGGER = get_logger("web_jobs")

_JOB_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "none": 100}
DEFAULT_JOB_HISTORY_LIMIT = 50
DEFAULT_JOB_HISTORY_TTL_SECONDS = 24 * 60 * 60
JOB_HISTORY_LIMIT_ENV = "CHATGPT_ARCHIVE_WEB_JOB_HISTORY_LIMIT"
JOB_HISTORY_TTL_ENV = "CHATGPT_ARCHIVE_WEB_JOB_HISTORY_TTL_SECONDS"


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        LOGGER.warning("invalid_web_job_history_setting env=%s error_type=invalid_integer", name)
        return default
    if value <= 0:
        LOGGER.warning("invalid_web_job_history_setting env=%s error_type=non_positive", name)
        return default
    return value


@dataclass
class ImportJob:
    job_id: str
    db_path: Path
    upload_path: Path
    filename: str
    size: int
    status: str = "queued"
    stage: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    summary: dict[str, Any] | None = None
    verify: dict[str, Any] | None = None
    stats: dict[str, Any] | None = None
    web_index: dict[str, Any] | None = None
    error: str | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=1000))

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "filename": "zip",
            "size": self.size,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round((self.finished_at or time.time()) - (self.started_at or self.created_at), 3),
            "summary": self.summary,
            "verify": self.verify,
            "stats": self.stats,
            "web_index": self.web_index,
            "error": self.error,
            "log_tail": list(self.logs)[-100:],
        }


class ImportJobManager:
    def __init__(self, db_path: Path, *, log_level: str = "warning", history_limit: int | None = None, history_ttl_seconds: int | None = None) -> None:
        self.db_path = db_path
        self.log_level = parse_log_level(log_level)
        self.history_limit = history_limit if history_limit is not None and history_limit > 0 else _positive_int_env(JOB_HISTORY_LIMIT_ENV, DEFAULT_JOB_HISTORY_LIMIT)
        self.history_ttl_seconds = history_ttl_seconds if history_ttl_seconds is not None and history_ttl_seconds > 0 else _positive_int_env(JOB_HISTORY_TTL_ENV, DEFAULT_JOB_HISTORY_TTL_SECONDS)
        self._lock = threading.Lock()
        self._jobs: dict[str, ImportJob] = {}
        self._running_job_id: str | None = None

    def has_running_job(self) -> bool:
        with self._lock:
            return self._running_job_id is not None

    def start_import(self, upload_path: Path, *, filename: str, size: int) -> ImportJob:
        with self._lock:
            self._prune_jobs_locked()
            if self._running_job_id is not None:
                raise RuntimeError("an import job is already running")
            job_id = uuid.uuid4().hex
            job = ImportJob(job_id=job_id, db_path=self.db_path, upload_path=upload_path, filename=filename, size=size)
            self._jobs[job_id] = job
            self._running_job_id = job_id
        thread = threading.Thread(target=self._run_job, args=(job,), name=f"chatgpt-import-{job_id[:8]}", daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> ImportJob | None:
        with self._lock:
            self._prune_jobs_locked()
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[ImportJob]:
        with self._lock:
            self._prune_jobs_locked()
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)[:20]

    def _prune_jobs_locked(self) -> None:
        now = time.time()
        terminal = [
            job for job in self._jobs.values()
            if job.status in {"succeeded", "failed", "postcheck_failed"}
        ]
        terminal.sort(key=lambda job: job.finished_at or job.created_at, reverse=True)
        for job in terminal[self.history_limit :]:
            self._jobs.pop(job.job_id, None)
        for job in terminal[: self.history_limit]:
            age = now - (job.finished_at or job.created_at)
            if age > self.history_ttl_seconds:
                self._jobs.pop(job.job_id, None)

    def _log(self, job: ImportJob, level: str, message: str) -> None:
        if _JOB_LEVELS[level] >= _JOB_LEVELS[self.log_level]:
            job.logs.append(f"{level} {message}")
        getattr(LOGGER, level)("job_id=%s %s", job.job_id, message)

    def _set_stage(self, job: ImportJob, stage: str) -> None:
        job.stage = stage
        self._log(job, "info", f"stage={stage}")

    def _run_job(self, job: ImportJob) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._set_stage(job, "import")
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            result = run_import_pipeline(
                self.db_path,
                str(job.upload_path),
                cwd=Path.cwd(),
                no_input_sha256=True,
                rebuild_fts=True,
                optimize_after_import_flag=False,
                optimize_fts_after_import=False,
                delete_input_on_success=False,
                progress_callback=lambda stage, summary: self._progress(job, stage, summary),
            )
            job.summary = result["summary"]
            if result.get("summary_update_after_commit_failed"):
                self._log(job, "warning", f"summary_update_after_commit_failed {result['summary_update_after_commit_failed']}")
            if result.get("import_connection_close_failed"):
                self._log(job, "warning", f"import_connection_close_failed {result['import_connection_close_failed']}")
            if result.get("summary_update_after_close_failed"):
                self._log(job, "warning", f"summary_update_after_close_failed {result['summary_update_after_close_failed']}")
            self._set_stage(job, "verify")
            conn = connect(self.db_path)
            try:
                job.verify = verify_database(conn)
                if job.verify.get("ok"):
                    job.stats = get_stats(conn)
            finally:
                conn.close()
            if job.verify and not job.verify.get("ok"):
                if job.verify.get("optional_web_index_error"):
                    self._set_stage(job, "web-index-recovery")
                    job.web_index = create_web_indexes(self.db_path)
                    job.web_index["recovered_optional_web_index"] = True
                    conn = connect(self.db_path)
                    try:
                        job.verify = verify_database(conn)
                        if job.verify.get("ok"):
                            job.stats = get_stats(conn)
                    finally:
                        conn.close()
                    if not job.verify.get("ok"):
                        job.status = "postcheck_failed"
                        job.error = "postcheck_failed"
                        self._set_stage(job, "postcheck_failed")
                        return
                else:
                    job.status = "postcheck_failed"
                    job.error = "postcheck_failed"
                    self._set_stage(job, "postcheck_failed")
                    return
            self._set_stage(job, "web-index")
            if job.web_index is None:
                job.web_index = create_web_indexes(self.db_path)
            job.status = "succeeded"
            self._set_stage(job, "succeeded")
        except Exception as exc:
            job.status = "failed"
            job.error = f"error_type={type(exc).__name__}"
            self._log(job, "error", f"import_failed error_type={type(exc).__name__}")
        finally:
            job.finished_at = time.time()
            try:
                job.upload_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._log(job, "warning", f"temporary_upload_cleanup_failed {type(exc).__name__}")
            cleanup_upload_dir(job.upload_path.parent)
            with self._lock:
                if self._running_job_id == job.job_id:
                    self._running_job_id = None
                self._prune_jobs_locked()

    def _progress(self, job: ImportJob, stage: str, summary: dict[str, Any]) -> None:
        job.stage = stage
        job.summary = summary


def make_upload_path() -> tuple[Path, Path]:
    directory = Path(tempfile.mkdtemp(prefix="chatgpt-archive-upload-"))
    return directory, directory / "upload.zip"


def cleanup_upload_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
