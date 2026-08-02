from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .cli import ImportPipelineError, run_import_pipeline
from .db import connect, get_stats, verify_database
from .logging_utils import get_logger, parse_log_level
from .utils import safe_filename_part
from .web_db import (
    WebIndexBuildCancelled,
    WebIndexBuildError,
    acquire_writer_process_lock,
    create_web_indexes,
)

LOGGER = get_logger("web_jobs")


class WebIndexBuilder(Protocol):
    """Keyword-only contract shared by the job runner and index builder."""

    def __call__(
        self,
        db_path: Path,
        *,
        batch_size: int = ...,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = ...,
        cancel_check: Callable[[], bool] | None = ...,
        _writer_lock: Any | None = ...,
    ) -> dict[str, Any]: ...


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
    web_index_cancel_requested: bool = False
    web_index_cancelled: bool = False
    error: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    outcome: str = "queued"
    completion_outcome: str = "queued"
    canonical_import_outcome: str = "queued"
    canonical_commit_succeeded: bool = False
    cleanup_warning: str | None = None
    cleanup_warnings: list[dict[str, str]] = field(default_factory=list)
    stage_timings: dict[str, float] = field(default_factory=dict)
    _web_index_cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _writer_lock: Any | None = field(default=None, repr=False, compare=False)
    _terminal_status: str | None = field(default=None, repr=False, compare=False)
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=1000))
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "stage": self.stage,
                "outcome": self.outcome,
                "completion_outcome": self.completion_outcome,
                "canonical_import_outcome": self.canonical_import_outcome,
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
                "web_index_cancel_requested": self.web_index_cancel_requested,
                "web_index_cancelled": self.web_index_cancelled,
                "error": self.error,
                "error_code": self.error_code,
                "error_type": self.error_type,
                "cleanup_warning": self.cleanup_warning,
                "cleanup_warnings": [dict(item) for item in self.cleanup_warnings],
                "stage_timings": dict(self.stage_timings),
                "log_tail": list(self.logs)[-100:],
            }


class ImportJobStartError(RuntimeError):
    """Safe failure raised when a worker thread cannot be constructed/started."""

    def __init__(
        self,
        error_type: str,
        *,
        cleanup_warnings: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__("import_job_start_failed")
        self.code = "import_job_start_failed"
        self.error_type = error_type
        self.cleanup_warnings = [dict(item) for item in cleanup_warnings or []]


@contextmanager
def _timed_job_stage(job: ImportJob, name: str):
    """Accumulate content-free wall timing for one production job stage."""

    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = max(0.0, time.perf_counter() - started)
        with job._lock:
            job.stage_timings[name] = round(
                float(job.stage_timings.get(name, 0.0)) + elapsed,
                6,
            )


def _add_cleanup_warning(
    job: ImportJob,
    code: str,
    *,
    error_type: str = "UnknownError",
    path_kind: str = "import_job",
) -> None:
    """Append every distinct warning while preserving the deprecated scalar alias."""

    with job._lock:
        item = {"code": code, "error_type": error_type, "path_kind": path_kind}
        if item not in job.cleanup_warnings:
            job.cleanup_warnings.append(item)
        if job.cleanup_warning is None:
            job.cleanup_warning = code


def _add_exception_cleanup_warnings(job: ImportJob, exc: BaseException) -> None:
    for item in getattr(exc, "cleanup_warnings", []):
        if not isinstance(item, dict):
            continue
        _add_cleanup_warning(
            job,
            str(item.get("code") or "web_index_staging_cleanup_failed"),
            error_type=str(item.get("error_type") or "UnknownError"),
            path_kind=str(item.get("path_kind") or "web_index_staging"),
        )


class ImportJobManager:
    def __init__(self, db_path: Path, *, log_level: str = "warning", history_limit: int | None = None, history_ttl_seconds: int | None = None) -> None:
        self.db_path = db_path
        self.log_level = parse_log_level(log_level)
        self.history_limit = history_limit if history_limit is not None and history_limit > 0 else _positive_int_env(JOB_HISTORY_LIMIT_ENV, DEFAULT_JOB_HISTORY_LIMIT)
        self.history_ttl_seconds = history_ttl_seconds if history_ttl_seconds is not None and history_ttl_seconds > 0 else _positive_int_env(JOB_HISTORY_TTL_ENV, DEFAULT_JOB_HISTORY_TTL_SECONDS)
        self._lock = threading.Lock()
        self._jobs: dict[str, ImportJob] = {}
        self._running_job_id: str | None = None
        self._pending_writer_lock: Any | None = None

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
        try:
            writer_lock = acquire_writer_process_lock(self.db_path)
        except WebIndexBuildError as exc:
            with self._lock:
                if self._running_job_id == "__pending_upload__":
                    self._running_job_id = None
            if exc.code == "writer_process_lock_busy":
                return False
            raise
        with self._lock:
            if self._running_job_id != "__pending_upload__":
                writer_lock.close()
                return False
            self._pending_writer_lock = writer_lock
        return True

    def release_pending_upload_slot(self) -> list[dict[str, str]]:
        writer_lock = None
        with self._lock:
            if self._running_job_id == "__pending_upload__":
                self._running_job_id = None
                writer_lock, self._pending_writer_lock = (
                    self._pending_writer_lock,
                    None,
                )
        if writer_lock is not None:
            try:
                writer_lock.close()
            except WebIndexBuildError as exc:
                # Admission has already been released.  Return safe secondary
                # diagnostics so the request path can preserve its primary
                # outcome without silently discarding cleanup evidence.
                return [dict(item) for item in exc.cleanup_warnings]
        return []

    def start_import(
        self,
        upload_path: Path,
        *,
        filename: str,
        size: int,
        upload_seconds: float = 0.0,
    ) -> ImportJob:
        needs_lock = False
        with self._lock:
            self._prune_jobs_locked()
            if self._running_job_id is not None and self._running_job_id != "__pending_upload__":
                raise RuntimeError("an import job is already running")
            if self._running_job_id is None:
                self._running_job_id = "__pending_upload__"
                needs_lock = True
        if needs_lock:
            try:
                writer_lock = acquire_writer_process_lock(self.db_path)
            except Exception:
                with self._lock:
                    if self._running_job_id == "__pending_upload__":
                        self._running_job_id = None
                raise
            with self._lock:
                self._pending_writer_lock = writer_lock
        with self._lock:
            if self._running_job_id != "__pending_upload__":
                raise RuntimeError("an import job is already running")
            job_id = uuid.uuid4().hex
            writer_lock, self._pending_writer_lock = self._pending_writer_lock, None
            if writer_lock is None:
                self._running_job_id = None
                raise RuntimeError("writer admission lock missing")
            job = ImportJob(
                job_id=job_id,
                db_path=self.db_path,
                upload_path=upload_path,
                filename=filename,
                size=size,
                _writer_lock=writer_lock,
            )
            job.stage_timings["upload"] = round(max(0.0, upload_seconds), 6)
            self._jobs[job_id] = job
            self._running_job_id = job_id
        try:
            thread = threading.Thread(target=self._run_job, args=(job,), name=f"chatgpt-import-{job_id[:8]}", daemon=True)
            thread.start()
        except Exception as exc:
            writer_lock = None
            cleanup_warnings: list[dict[str, str]] = []
            with self._lock:
                if self._running_job_id == job_id:
                    self._running_job_id = None
                removed = self._jobs.pop(job_id, None)
                if removed is not None:
                    writer_lock, removed._writer_lock = removed._writer_lock, None
            if writer_lock is not None:
                try:
                    writer_lock.close()
                except WebIndexBuildError as cleanup_exc:
                    cleanup_warnings = [
                        dict(item) for item in cleanup_exc.cleanup_warnings
                    ]
            raise ImportJobStartError(
                type(exc).__name__, cleanup_warnings=cleanup_warnings
            ) from exc
        return job

    def get(self, job_id: str) -> ImportJob | None:
        with self._lock:
            self._prune_jobs_locked()
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[ImportJob]:
        with self._lock:
            self._prune_jobs_locked()
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)[:20]

    def request_web_index_cancel(self, job_id: str) -> tuple[ImportJob | None, bool]:
        """Request cancellation only while an import job is rebuilding the optional index."""

        with self._lock:
            self._prune_jobs_locked()
            job = self._jobs.get(job_id)
        if job is None:
            return None, False
        with job._lock:
            if job.status != "running" or job.stage not in {"web-index", "web-index-recovery"}:
                return job, False
            job.web_index_cancel_requested = True
            if job.web_index is None:
                job.web_index = {"status": "cancelling"}
            else:
                job.web_index = {**job.web_index, "status": "cancelling"}
            job._web_index_cancel_event.set()
        return job, True

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
        error_type: str | None = None,
    ) -> None:
        with job._lock:
            if status in {"succeeded", "failed", "postcheck_failed"}:
                job._terminal_status = status
                job.status = "running"
            else:
                job.status = status
            job.outcome = outcome
            job.error_code = error_code
            job.error = error_code
            job.error_type = error_type

    @staticmethod
    def _set_completion_outcome(job: ImportJob, value: str) -> None:
        with job._lock:
            job.completion_outcome = value

    @staticmethod
    def _successful_completion_outcome(job: ImportJob) -> str:
        summary = job.summary or {}
        if int(summary.get("skipped_invalid_elements") or 0) > 0:
            return "partial_success"
        if int(summary.get("warnings") or 0) > 0:
            return "success_with_warnings"
        return "success"

    def _run_job(self, job: ImportJob) -> None:
        """Protect the complete worker entry, including initial state/log setup."""

        try:
            self._run_job_body(job)
        except Exception as exc:
            # This guard covers failures before _run_job_body reaches its own
            # pipeline try/finally (for example a patched stage/log setup).
            with job._lock:
                job.status = "running"
                job._terminal_status = "failed"
                job.stage = "job_setup"
                job.outcome = "import_job_start_failed"
                job.completion_outcome = "failed_before_commit"
                job.canonical_import_outcome = "failed_before_commit"
                job.error_code = "import_job_start_failed"
                job.error = "import_job_start_failed"
                job.error_type = type(exc).__name__
        finally:
            with self._lock:
                still_owned = self._running_job_id == job.job_id
            if still_owned:
                self._finalize_job(job)

    def _run_job_body(self, job: ImportJob) -> None:
        with job._lock:
            job.status = "running"
            job.outcome = "import_running"
            job.completion_outcome = "running"
            job.canonical_import_outcome = "running"
            job.started_at = time.time()
        self._set_stage(job, "import")
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with _timed_job_stage(job, "import"):
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
                    writer_lock=job._writer_lock,
                )
            with job._lock:
                job.summary = dict(result["summary"])
                job.canonical_commit_succeeded = True
                job.outcome = "canonical_commit_succeeded"
                job.canonical_import_outcome = self._successful_completion_outcome(job)
            if result.get("summary_update_after_commit_failed"):
                _add_cleanup_warning(job, "summary_update_after_commit_failed", error_type=str(result["summary_update_after_commit_failed"]), path_kind="import_summary")
                self._log(job, "warning", f"summary_update_after_commit_failed {result['summary_update_after_commit_failed']}")
            if result.get("import_connection_close_failed"):
                _add_cleanup_warning(job, "import_connection_close_failed", error_type=str(result["import_connection_close_failed"]), path_kind="database_connection")
                self._log(job, "warning", f"import_connection_close_failed {result['import_connection_close_failed']}")
            if result.get("summary_update_after_close_failed"):
                _add_cleanup_warning(job, "summary_update_after_close_failed", error_type=str(result["summary_update_after_close_failed"]), path_kind="import_summary")
                self._log(job, "warning", f"summary_update_after_close_failed {result['summary_update_after_close_failed']}")
            self._set_stage(job, "verify")
            try:
                with _timed_job_stage(job, "verify"):
                    conn = connect(self.db_path)
                    try:
                        verify_result = verify_database(conn)
                    finally:
                        conn.close()
                with job._lock:
                    job.verify = verify_result
            except Exception as exc:
                self._set_outcome(
                    job,
                    status="postcheck_failed",
                    outcome="verify_failed",
                    error_code="verify_failed",
                    error_type=type(exc).__name__,
                )
                self._log(job, "error", f"verify_failed error_type={type(exc).__name__}")
                self._set_stage(job, "verify_failed")
                return
            if job.verify and not job.verify.get("ok"):
                if job.verify.get("optional_web_index_error"):
                    self._set_stage(job, "web-index-recovery")
                    try:
                        builder: WebIndexBuilder = create_web_indexes
                        with _timed_job_stage(job, "web_index"):
                            web_index_result = builder(
                                self.db_path,
                                progress_callback=lambda stage, progress: self._web_index_progress(job, stage, progress),
                                cancel_check=job._web_index_cancel_event.is_set,
                                _writer_lock=job._writer_lock,
                            )
                        web_index_result["recovered_optional_web_index"] = True
                        conn = connect(self.db_path)
                        try:
                            verify_result = verify_database(conn)
                        finally:
                            conn.close()
                        with job._lock:
                            job.web_index = web_index_result
                            job.verify = verify_result
                    except WebIndexBuildCancelled as exc:
                        _add_exception_cleanup_warnings(job, exc)
                        self._web_index_cancelled(job, postcheck_failed=True)
                        return
                    except Exception as exc:
                        _add_exception_cleanup_warnings(job, exc)
                        error_code = (
                            exc.code
                            if isinstance(exc, WebIndexBuildError)
                            else "web_index_failed"
                        )
                        self._set_outcome(
                            job,
                            status="postcheck_failed",
                            outcome="web_index_failed",
                            error_code=error_code,
                            error_type=type(exc).__name__,
                        )
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
                with _timed_job_stage(job, "stats"):
                    conn = connect(self.db_path)
                    try:
                        stats_result = get_stats(conn)
                    finally:
                        conn.close()
                with job._lock:
                    job.stats = stats_result
            except Exception as exc:
                self._set_outcome(
                    job,
                    status="postcheck_failed",
                    outcome="stats_failed",
                    error_code="stats_failed",
                    error_type=type(exc).__name__,
                )
                self._log(job, "error", f"stats_failed error_type={type(exc).__name__}")
                self._set_stage(job, "stats_failed")
                return
            self._set_stage(job, "web-index")
            if job.web_index is None:
                try:
                    builder: WebIndexBuilder = create_web_indexes
                    with _timed_job_stage(job, "web_index"):
                        web_index_result = builder(
                            self.db_path,
                            progress_callback=lambda stage, progress: self._web_index_progress(job, stage, progress),
                            cancel_check=job._web_index_cancel_event.is_set,
                            _writer_lock=job._writer_lock,
                        )
                    with job._lock:
                        job.web_index = web_index_result
                except WebIndexBuildCancelled as exc:
                    _add_exception_cleanup_warnings(job, exc)
                    self._web_index_cancelled(job, postcheck_failed=False)
                    return
                except Exception as exc:
                    _add_exception_cleanup_warnings(job, exc)
                    error_code = (
                        exc.code
                        if isinstance(exc, WebIndexBuildError)
                        else "web_index_failed"
                    )
                    self._set_outcome(
                        job,
                        status="postcheck_failed",
                        outcome="web_index_failed",
                        error_code=error_code,
                        error_type=type(exc).__name__,
                    )
                    self._log(job, "error", f"web_index_failed error_type={type(exc).__name__}")
                    self._set_stage(job, "web_index_failed")
                    return
            self._set_outcome(job, status="succeeded", outcome="succeeded")
            self._set_completion_outcome(job, self._successful_completion_outcome(job))
            self._set_stage(job, "succeeded")
        except ImportPipelineError as exc:
            outcome_by_stage = {
                "input_preflight": "input_preflight_failed",
                "source_scan": "source_scan_failed",
                "source_read": "source_read_failed",
                "json_decode": "json_decode_failed",
                "top_level_contract": "top_level_contract_failed",
                "transaction": "import_transaction_failed",
            }
            with job._lock:
                job.summary = dict(exc.summary) if exc.summary is not None else job.summary
                job.error_type = exc.failure_persistence_error_type
            self._set_outcome(
                job,
                status="failed",
                outcome=outcome_by_stage.get(exc.stage, "import_transaction_failed"),
                error_code=exc.code,
            )
            self._set_completion_outcome(job, "failed_before_commit")
            with job._lock:
                job.canonical_import_outcome = "failed_before_commit"
            self._set_stage(job, exc.stage)
            self._log(job, "error", f"import_failed code={exc.code} stage={exc.stage}")
        except Exception as exc:
            self._set_outcome(
                job,
                status="failed",
                outcome="import_transaction_failed",
                error_code="import_transaction_failed",
                error_type=type(exc).__name__,
            )
            self._set_completion_outcome(job, "failed_before_commit")
            with job._lock:
                job.canonical_import_outcome = "failed_before_commit"
            self._set_stage(job, "transaction")
            self._log(job, "error", f"import_failed error_type={type(exc).__name__}")
        finally:
            self._finalize_job(job)

    def _finalize_job(self, job: ImportJob) -> None:
        """Best-effort upload cleanup with unconditional writer-slot release."""

        cleanup_started = time.perf_counter()
        with job._lock:
            terminal_status = job._terminal_status or (
                job.status
                if job.status in {"succeeded", "failed", "postcheck_failed"}
                else "failed"
            )
            terminal_stage = job.stage
            job.status = "running"
            job.stage = "cleanup"
        unlink_error: str | None = None
        try:
            try:
                job.upload_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                unlink_error = type(exc).__name__
                _add_cleanup_warning(job, "upload_file_unlink_failed", error_type=unlink_error, path_kind="upload_file")
                try:
                    self._log(job, "warning", f"upload_file_unlink_failed error_type={unlink_error}")
                except Exception:
                    pass
            cleanup = cleanup_upload_dir(job.upload_path.parent)
            if not cleanup["ok"]:
                _add_cleanup_warning(
                    job,
                    "upload_directory_cleanup_failed"
                    if cleanup["error_type"]
                    else "upload_directory_cleanup_incomplete",
                    error_type=cleanup["error_type"] or "PathStillExists",
                    path_kind="upload_directory",
                )
                try:
                    self._log(
                        job,
                        "warning",
                        "upload_directory_cleanup_failed "
                        f"error_type={cleanup['error_type'] or 'path_still_exists'} "
                        f"file_unlink_error_type={unlink_error or 'none'}",
                    )
                except Exception:
                    pass
        finally:
            writer_lock = None
            with job._lock:
                writer_lock, job._writer_lock = job._writer_lock, None
                if job.cleanup_warnings and terminal_status == "succeeded":
                    job.completion_outcome = "cleanup_warning"
                elif terminal_status == "postcheck_failed" and job.canonical_commit_succeeded:
                    job.completion_outcome = "failed_after_canonical_commit"
            if writer_lock is not None:
                try:
                    writer_lock.close()
                except WebIndexBuildError as exc:
                    _add_exception_cleanup_warnings(job, exc)
                    with job._lock:
                        if terminal_status == "succeeded":
                            job.completion_outcome = "cleanup_warning"
            with job._lock:
                job.stage_timings["cleanup"] = round(
                    float(job.stage_timings.get("cleanup", 0.0))
                    + max(0.0, time.perf_counter() - cleanup_started),
                    6,
                )
                job.status = terminal_status
                job.stage = terminal_stage
                job._terminal_status = None
                job.finished_at = time.time()
            with self._lock:
                if self._running_job_id == job.job_id:
                    self._running_job_id = None
                self._prune_jobs_locked()

    def _progress(self, job: ImportJob, stage: str, summary: dict[str, Any]) -> None:
        with job._lock:
            job.stage = stage
            job.summary = dict(summary)

    def _web_index_progress(self, job: ImportJob, stage: str, progress: dict[str, Any]) -> None:
        with job._lock:
            job.stage = "web-index"
            status = "cancelling" if job.web_index_cancel_requested else "building"
            job.web_index = {"status": status, "build_stage": stage, **progress}

    def _web_index_cancelled(self, job: ImportJob, *, postcheck_failed: bool) -> None:
        with job._lock:
            job.web_index_cancel_requested = True
            job.web_index_cancelled = True
            job.web_index = {"status": "cancelled", "complete": True}
        self._set_outcome(
            job,
            status="postcheck_failed" if postcheck_failed else "succeeded",
            outcome="web_index_cancelled",
            error_code="web_index_cancelled" if postcheck_failed else None,
        )
        self._set_completion_outcome(job, "cancelled")
        self._set_stage(job, "web_index_cancelled")


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
