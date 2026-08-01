from __future__ import annotations

import errno
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


DISK_RESERVE_BYTES = 256 * 1024 * 1024
DISK_CLEANUP_RESERVE_BYTES = 256 * 1024 * 1024
DISK_RUNTIME_CHECK_BYTES = 256 * 1024 * 1024
DISK_RUNTIME_CHECK_SECONDS = 5.0


class DiskSpaceInsufficientError(OSError):
    """Content-free capacity failure with one stable public code."""

    def __init__(self, code: str, *, required_bytes: int, free_bytes: int) -> None:
        super().__init__(errno.ENOSPC, code)
        self.code = code
        self.required_bytes = max(0, int(required_bytes))
        self.free_bytes = max(0, int(free_bytes))


def is_disk_full_error(exc: BaseException) -> bool:
    if isinstance(exc, DiskSpaceInsufficientError):
        return True
    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
        return True
    text = str(exc).casefold()
    return "database or disk is full" in text or "no space left on device" in text


def _existing_capacity_path(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(_existing_capacity_path(path)).free)


def require_free_space(path: Path, required_bytes: int, code: str) -> dict[str, int]:
    required = max(0, int(required_bytes))
    available = free_bytes(path)
    if available < required:
        raise DiskSpaceInsufficientError(
            code,
            required_bytes=required,
            free_bytes=available,
        )
    return {"required_bytes": required, "free_bytes": available}


def upload_required_bytes(content_length: int) -> int:
    return int(upload_capacity_plan(content_length)["required_free_bytes"])


def upload_capacity_plan(content_length: int) -> dict[str, int]:
    """Model parser spool and pipeline ZIP coexistence during multipart copy."""

    length = max(0, int(content_length))
    return {
        "compressed_http_body_bytes": length,
        "estimated_multipart_parser_spool_bytes": length,
        "estimated_pipeline_owned_zip_bytes": length,
        "cleanup_reserve_bytes": DISK_CLEANUP_RESERVE_BYTES,
        "filesystem_emergency_reserve_bytes": DISK_RESERVE_BYTES,
        "required_free_bytes": (
            length * 2 + DISK_CLEANUP_RESERVE_BYTES + DISK_RESERVE_BYTES
        ),
    }


def import_required_bytes(selected_json_bytes: int) -> int:
    return int(import_capacity_plan(selected_json_bytes)["required_free_bytes"])


def web_index_required_bytes(database_bytes: int) -> int:
    return int(web_index_capacity_plan(database_bytes)["required_free_bytes"])


def import_capacity_plan(
    selected_json_bytes: int,
    *,
    compressed_source_bytes: int = 0,
    pipeline_owned_zip_bytes: int = 0,
    multipart_parser_spool_bytes: int = 0,
    existing_database_bytes: int = 0,
) -> dict[str, int]:
    """Return an explicit conservative free-space model for canonical import.

    The selected JSON is already present in the source/spooled ZIP when this
    check runs, so existing source bytes are reported by higher layers but are
    not double-counted as newly required free space.
    """

    selected = max(0, int(selected_json_bytes))
    canonical_growth = max(256 * 1024 * 1024, selected * 3)
    wal_or_journal = canonical_growth
    core_fts_rebuild = max(128 * 1024 * 1024, selected)
    sqlite_temp = max(128 * 1024 * 1024, selected // 2)
    required = (
        canonical_growth
        + wal_or_journal
        + core_fts_rebuild
        + sqlite_temp
        + DISK_CLEANUP_RESERVE_BYTES
        + DISK_RESERVE_BYTES
    )
    return {
        "existing_compressed_source_bytes": max(0, int(compressed_source_bytes)),
        "existing_multipart_parser_spool_bytes": max(
            0, int(multipart_parser_spool_bytes)
        ),
        "existing_pipeline_owned_zip_bytes": max(
            0, int(pipeline_owned_zip_bytes)
        ),
        "existing_canonical_database_bytes": max(
            0, int(existing_database_bytes)
        ),
        "selected_logical_json_bytes": selected,
        "estimated_canonical_db_growth_bytes": canonical_growth,
        "estimated_wal_or_journal_bytes": wal_or_journal,
        "estimated_core_fts_rebuild_bytes": core_fts_rebuild,
        "estimated_sqlite_temp_bytes": sqlite_temp,
        "cleanup_reserve_bytes": DISK_CLEANUP_RESERVE_BYTES,
        "filesystem_emergency_reserve_bytes": DISK_RESERVE_BYTES,
        "required_free_bytes": required,
    }


def web_index_capacity_plan(database_bytes: int) -> dict[str, int]:
    """Model old live, private staging, publish WAL and TEMP coexistence."""

    database = max(0, int(database_bytes))
    staging = max(256 * 1024 * 1024, database)
    publish_wal = max(128 * 1024 * 1024, database)
    sqlite_temp = max(128 * 1024 * 1024, database // 2)
    required = (
        staging
        + publish_wal
        + sqlite_temp
        + DISK_CLEANUP_RESERVE_BYTES
        + DISK_RESERVE_BYTES
    )
    return {
        "existing_optional_live_within_database_bytes": database,
        "estimated_optional_staging_bytes": staging,
        "estimated_publish_wal_or_journal_bytes": publish_wal,
        "estimated_sqlite_temp_bytes": sqlite_temp,
        "cleanup_reserve_bytes": DISK_CLEANUP_RESERVE_BYTES,
        "filesystem_emergency_reserve_bytes": DISK_RESERVE_BYTES,
        "required_free_bytes": required,
    }


def migration_required_bytes(database_bytes: int, row_count: int = 0) -> int:
    """Conservative coexistence budget for a schema rewrite and its journal.

    The existing database, rollback journal/WAL, rewritten pages, indexes and
    safety reserve may coexist.  The row allowance covers the v4→v5 display
    revision material written for every canonical node.
    """

    database = max(0, int(database_bytes))
    rows = max(0, int(row_count))
    rewritten = max(database, rows * 96)
    return max(512 * 1024 * 1024, database + rewritten) + DISK_RESERVE_BYTES


@dataclass
class DiskSpaceGuard:
    path: Path
    code: str
    reserve_bytes: int = DISK_RESERVE_BYTES
    check_bytes: int = DISK_RUNTIME_CHECK_BYTES
    check_seconds: float = DISK_RUNTIME_CHECK_SECONDS
    _bytes_since_check: int = 0
    _last_check: float = 0.0

    def check(self, *, force: bool = False, advanced_bytes: int = 0) -> None:
        self._bytes_since_check += max(0, int(advanced_bytes))
        now = time.monotonic()
        if not force and self._last_check and (
            self._bytes_since_check < self.check_bytes
            and now - self._last_check < self.check_seconds
        ):
            return
        require_free_space(self.path, self.reserve_bytes, self.code)
        self._bytes_since_check = 0
        self._last_check = now
