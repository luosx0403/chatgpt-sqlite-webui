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

from .cli import ImportPipelineError, run_import_pipeline
from .db import connect, get_stats, verify_database
from .logging_utils import get_logger, parse_log_level
from .utils import safe_filename_part
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
    error_code: str | None = None
    outcome: str = "queued"
    canonical_commit_succeeded: bool = False
    cleanup_warning: str | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=1000))
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "stage": self.stage,
                "outcome": self.outcome,
                "canonical_commit_succeeded": self.canonical_commit_succeeded,
                "filename": safe_filename_part(self.filename, 120),
                "size": self.size,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_seconds": round((self.finished_at or time.time()) - (self.started_at or self.created_at), 3),
                "summary": dict(self.summary) if self.summary is not None else None,
                "verify": dict(self.verify) if self.verify is not None else None,
                "stats": dict(self.stats) if self.stats is not None else None,
                "web_index": dict(self.web_index) if self.web_index is not None else None,
                "error": self.error,
                "error_code": self.error_code,
                "cleanup_warning": self.cleanup_warning,
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

    def acquire_pending_upload_slot(self) -> bool:
        """Try to reserve a pending upload slot before reading the upload file.

        Returns True when the slot was acquired.  The caller MUST call
        release_pending_upload_slot() exactly once, or hand the slot over to
        start_import() which takes ownership.
        """
        with self._lock:
            if self._running_job_id is not None:
                return False
            self._running_job_id = "__pending_upload__"
            return True

    def release_pending_upload_slot(self) -> None:
        with self._lock:
            if self._running_job_id == "__pending_upload__":
                self._running_job_id = None

    def start_import(self, upload_path: Path, *, filename: str, size: int) -> ImportJob:
        with self._lock:
            self._prune_jobs_locked()
            if self._running_job_id is not None and self._running_job_id != "__pending_upload__":
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
        terminal: list[tuple[ImportJob, float]] = []
        for job in self._jobs.values():
            with job._lock:
                if job.status in {"succeeded", "failed", "postcheck_failed"}:
                    terminal.append((job, job.finished_at or job.created_at))
        terminal.sort(key=lambda item: item[1], reverse=True)
        for job, _finished_at in terminal[self.history_limit :]:
            self._jobs.pop(job.job_id, None)
        for job, finished_at in terminal[: self.history_limit]:
            age = now - finished_at
            if age > self.history_ttl_seconds:
                self._jobs.pop(job.job_id, None)

    def _log(self, job: ImportJob, level: str, message: str) -> None:
        with job._lock:
            if _JOB_LEVELS[level] >= _JOB_LEVELS[self.log_level]:
                job.logs.append(f"{level} {message}")
        getattr(LOGGER, level)("job_id=%s %s", job.job_id, message)

    def _set_stage(self, job: ImportJob, stage: str) -> None:
        with job._lock:
            job.stage = stage
        self._log(job, "info", f"stage={stage}")

    @staticmethod
    def _set_outcome(
        job: ImportJob,
        *,
        status: str,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        with job._lock:
            job.status = status
            job.outcome = outcome
            job.error_code = error_code
            job.error = error_code

    def _run_job(self, job: ImportJob) -> None:
        with job._lock:
            job.status = "running"
            job.outcome = "import_running"
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
            with job._lock:
                job.summary = dict(result["summary"])
                job.canonical_commit_succeeded = True
                job.outcome = "canonical_commit_succeeded"
            if result.get("summary_update_after_commit_failed"):
                with job._lock:
                    job.cleanup_warning = "summary_update_after_commit_failed"
                self._log(job, "warning", f"summary_update_after_commit_failed {result['summary_update_after_commit_failed']}")
            if result.get("import_connection_close_failed"):
                with job._lock:
                    job.cleanup_warning = job.cleanup_warning or "import_connection_close_failed"
                self._log(job, "warning", f"import_connection_close_failed {result['import_connection_close_failed']}")
            if result.get("summary_update_after_close_failed"):
                with job._lock:
                    job.cleanup_warning = job.cleanup_warning or "summary_update_after_close_failed"
                self._log(job, "warning", f"summary_update_after_close_failed {result['summary_update_after_close_failed']}")
            self._set_stage(job, "verify")
            try:
                conn = connect(self.db_path)
                try:
                    verify_result = verify_database(conn)
                finally:
                    conn.close()
                with job._lock:
                    job.verify = verify_result
            except Exception as exc:
                self._set_outcome(job, status="postcheck_failed", outcome="verify_failed", error_code="verify_failed")
                self._log(job, "error", f"verify_failed error_type={type(exc).__name__}")
                self._set_stage(job, "verify_failed")
                return
            if job.verify and not job.verify.get("ok"):
                if job.verify.get("optional_web_index_error"):
                    self._set_stage(job, "web-index-recovery")
                    try:
                        web_index_result = create_web_indexes(self.db_path)
                        web_index_result["recovered_optional_web_index"] = True
                        conn = connect(self.db_path)
                        try:
                            verify_result = verify_database(conn)
                        finally:
                            conn.close()
                        with job._lock:
                            job.web_index = web_index_result
                            job.verify = verify_result
                    except Exception as exc:
                        self._set_outcome(job, status="postcheck_failed", outcome="web_index_failed", error_code="web_index_failed")
                        self._log(job, "error", f"web_index_failed error_type={type(exc).__name__}")
                        self._set_stage(job, "web_index_failed")
                        return
                    if not job.verify.get("ok"):
                        self._set_outcome(job, status="postcheck_failed", outcome="verify_failed", error_code="verify_failed")
                        self._set_stage(job, "verify_failed")
                        return
                else:
                    self._set_outcome(job, status="postcheck_failed", outcome="verify_failed", error_code="verify_failed")
                    self._set_stage(job, "verify_failed")
                    return
            self._set_stage(job, "stats")
            try:
                conn = connect(self.db_path)
                try:
                    stats_result = get_stats(conn)
                finally:
                    conn.close()
                with job._lock:
                    job.stats = stats_result
            except Exception as exc:
                self._set_outcome(job, status="postcheck_failed", outcome="stats_failed", error_code="stats_failed")
                self._log(job, "error", f"stats_failed error_type={type(exc).__name__}")
                self._set_stage(job, "stats_failed")
                return
            self._set_stage(job, "web-index")
            if job.web_index is None:
                try:
                    web_index_result = create_web_indexes(self.db_path)
                    with job._lock:
                        job.web_index = web_index_result
                except Exception as exc:
                    self._set_outcome(job, status="postcheck_failed", outcome="web_index_failed", error_code="web_index_failed")
                    self._log(job, "error", f"web_index_failed error_type={type(exc).__name__}")
                    self._set_stage(job, "web_index_failed")
                    return
            self._set_outcome(job, status="succeeded", outcome="succeeded")
            self._set_stage(job, "succeeded")
        except ImportPipelineError as exc:
            outcome_by_stage = {
                "input_preflight": "input_preflight_failed",
                "source_scan": "source_scan_failed",
                "json_decode": "json_decode_failed",
                "top_level_contract": "top_level_contract_failed",
                "transaction": "import_transaction_failed",
            }
            with job._lock:
                job.summary = dict(exc.summary) if exc.summary is not None else job.summary
            self._set_outcome(
                job,
                status="failed",
                outcome=outcome_by_stage.get(exc.stage, "import_transaction_failed"),
                error_code=exc.code,
            )
            self._set_stage(job, exc.stage)
            self._log(job, "error", f"import_failed code={exc.code} stage={exc.stage}")
        except Exception as exc:
            self._set_outcome(job, status="failed", outcome="import_transaction_failed", error_code="import_transaction_failed")
            self._set_stage(job, "transaction")
            self._log(job, "error", f"import_failed error_type={type(exc).__name__}")
        finally:
            with job._lock:
                job.finished_at = time.time()
            unlink_error: str | None = None
            try:
                job.upload_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                unlink_error = type(exc).__name__
                with job._lock:
                    job.cleanup_warning = "upload_file_unlink_failed"
                self._log(job, "warning", f"upload_file_unlink_failed error_type={unlink_error}")
            cleanup = cleanup_upload_dir(job.upload_path.parent)
            if not cleanup["ok"]:
                with job._lock:
                    job.cleanup_warning = (
                        "upload_directory_cleanup_failed"
                        if cleanup["error_type"]
                        else "upload_directory_cleanup_incomplete"
                    )
                self._log(
                    job,
                    "warning",
                    "upload_directory_cleanup_failed "
                    f"error_type={cleanup['error_type'] or 'path_still_exists'} "
                    f"file_unlink_error_type={unlink_error or 'none'}",
                )
            with self._lock:
                if self._running_job_id == job.job_id:
                    self._running_job_id = None
                self._prune_jobs_locked()

    def _progress(self, job: ImportJob, stage: str, summary: dict[str, Any]) -> None:
        with job._lock:
            job.stage = stage
            job.summary = dict(summary)


def make_upload_path() -> tuple[Path, Path]:
    directory = Path(tempfile.mkdtemp(prefix="chatgpt-archive-upload-"))
    return directory, directory / "upload.zip"


def cleanup_upload_dir(path: Path) -> dict[str, Any]:
    """Remove a temporary upload directory and report every incomplete result."""

    error_type: str | None = None
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        error_type = type(exc).__name__
    try:
        still_exists = path.exists()
    except OSError as exc:
        still_exists = True
        if error_type is None:
            error_type = type(exc).__name__
    return {
        "ok": error_type is None and not still_exists,
        "error_type": error_type,
        "path_still_exists": still_exists,
        "partial_cleanup": still_exists,
    }
