from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
WINDOWS_RESERVED_BASENAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
WINDOWS_RESERVED_DIGIT_TRANSLATION = str.maketrans({"¹": "1", "²": "2", "³": "3"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_bytes_if_changed(path: Path, data: bytes, force: bool = False) -> bool:
    """Atomically replace path only when bytes differ.

    Default exports must be deterministic and idempotent. This helper compares
    final UTF-8 bytes, preserves mtimes for unchanged files, and avoids partial
    files if a write is interrupted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force and path.exists() and _file_matches_bytes(path, data):
        return False
    fd: int | None = None
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        return True
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def write_chunks_if_changed(
    path: Path,
    chunks: Iterable[str | bytes],
    *,
    force: bool = False,
    max_bytes: int,
) -> tuple[bool, str, int]:
    """Atomically publish bounded streamed bytes while preserving unchanged mtimes.

    The candidate is written and hashed once.  When an old regular file exists,
    it is compared incrementally during that write; identical output discards the
    temporary candidate instead of replacing the old inode.
    """

    if max_bytes < 0:
        raise ValueError("invalid_stream_byte_budget")
    path.parent.mkdir(parents=True, exist_ok=True)
    old = None
    old_matches = not force
    try:
        if old_matches:
            old = path.open("rb")
    except (FileNotFoundError, IsADirectoryError, OSError):
        old = None
        old_matches = False
    fd: int | None = None
    tmp_name: str | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as candidate:
            fd = None
            for chunk in chunks:
                payload = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                total += len(payload)
                if total > max_bytes:
                    raise ValueError("export_output_byte_limit_exceeded")
                candidate.write(payload)
                digest.update(payload)
                if old_matches and old is not None:
                    if old.read(len(payload)) != payload:
                        old_matches = False
            if old_matches and old is not None and old.read(1) != b"":
                old_matches = False
            candidate.flush()
            os.fsync(candidate.fileno())
        output_hash = digest.hexdigest()
        if old_matches:
            os.unlink(tmp_name)
            tmp_name = None
            return False, output_hash, total
        os.replace(tmp_name, path)
        tmp_name = None
        return True, output_hash, total
    finally:
        if old is not None:
            old.close()
        if fd is not None:
            os.close(fd)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def _file_matches_bytes(path: Path, data: bytes, chunk_size: int = 1024 * 1024) -> bool:
    """Compare an existing file without reading the old payload all at once."""

    if path.stat().st_size != len(data):
        return False
    view = memoryview(data)
    offset = 0
    with path.open("rb") as existing:
        while offset < len(data):
            chunk = existing.read(min(chunk_size, len(data) - offset))
            if not chunk or chunk != view[offset : offset + len(chunk)]:
                return False
            offset += len(chunk)
        return existing.read(1) == b""


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def compact_json(value: Any, max_chars: int | None = None) -> str:
    # ASCII escaping preserves raw JSON semantics while ensuring isolated
    # surrogates never reach SQLite's UTF-8 binder.
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def classify_file(path: str | Path) -> str:
    suffix = Path(str(path)).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return "other"


def safe_filename_part(text: str | None, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFC", (text or "untitled")).strip() or "untitled"
    text = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._ ")
    if not text:
        text = "untitled"
    text = _avoid_windows_reserved_filename(text)
    text = text[:max_len].rstrip("._ ") or "untitled"
    return _avoid_windows_reserved_filename(text)


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate at a UTF-8 code-point boundary without emitting invalid text."""

    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def finite_float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _avoid_windows_reserved_filename(text: str) -> str:
    candidate = text.rstrip(" .") or "untitled"
    stem = candidate.split(".", 1)[0].rstrip(" .").casefold().translate(WINDOWS_RESERVED_DIGIT_TRANSLATION)
    if stem in WINDOWS_RESERVED_BASENAMES:
        candidate = f"_{candidate}"
    return candidate.rstrip(" .") or "untitled"


def epoch_to_display(value: float | int | str | None) -> str:
    if value in (None, ""):
        return ""
    number = finite_float_or_none(value)
    if number is None:
        return ""
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(value)


def epoch_to_date_part(value: float | int | str | None) -> str:
    display = epoch_to_display(value)
    if display:
        return display[:10]
    return "undated"


def parse_date_boundary(value: str | None, end_of_day: bool = False) -> float | None:
    if not value:
        return None
    dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt + timedelta(days=1)
    return dt.timestamp()
