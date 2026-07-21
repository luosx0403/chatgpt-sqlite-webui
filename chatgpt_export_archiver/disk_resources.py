from __future__ import annotations

import errno
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


DISK_RESERVE_BYTES = 256 * 1024 * 1024
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
    return max(0, int(content_length)) + DISK_RESERVE_BYTES


def import_required_bytes(selected_json_bytes: int) -> int:
    # Canonical tables, indexes, rollback journal/WAL and temporary pages can
    # coexist. This is capacity planning, not a guarantee; runtime ENOSPC is
    # still handled independently.
    return max(512 * 1024 * 1024, max(0, int(selected_json_bytes)) * 5) + DISK_RESERVE_BYTES


def web_index_required_bytes(database_bytes: int) -> int:
    # Old live objects, private staging, FTS shadow objects and WAL may coexist
    # until atomic publication.
    return max(512 * 1024 * 1024, max(0, int(database_bytes)) * 2) + DISK_RESERVE_BYTES


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
