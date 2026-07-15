from __future__ import annotations

import json
import math
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .utils import classify_file


SHARD_RE = re.compile(r"(^|.*/)conversations-(\d+)\.json$")


class NonFiniteJsonNumberError(ValueError):
    """Raised when a JSON source contains non-standard NaN/Infinity tokens."""


def _reject_non_finite_json_number(value: str) -> None:
    raise NonFiniteJsonNumberError(f"non_finite_json_number:{value.casefold()}")


def _parse_finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _reject_non_finite_json_number(value)
    return number


@dataclass(frozen=True)
class SourceEntry:
    source_path: str
    file_type: str
    size: int
    extension: str
    is_conversation_json: bool = False
    is_selected_conversation_source: bool = False


@dataclass(frozen=True)
class InputSource:
    path: Path
    kind: str
    size: int
    delete_target: Path | None = None


def find_default_input(path: Path) -> InputSource:
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError("input_not_supported")
    path = path.resolve()
    if path.is_file():
        if path.suffix.lower() == ".zip":
            kind = "zip"
        elif is_conversation_json_source(path.name):
            kind = "json"
        else:
            raise ValueError("input_not_supported")
        return InputSource(path=path, kind=kind, size=path.stat().st_size, delete_target=path)
    zips = sorted(path.glob("*.zip"))
    if any(candidate.is_symlink() for candidate in zips):
        raise ValueError("input_symlink_not_allowed")
    if not zips:
        json_files = sorted([p for p in path.glob("conversations*.json") if is_conversation_json_source(p.name)])
        if any(candidate.is_symlink() for candidate in json_files):
            raise ValueError("input_symlink_not_allowed")
        if not json_files:
            raise ValueError("no_zip_file_found")
        if len(json_files) > 1:
            shard_files = [p for p in json_files if is_shard_conversation_source(p.name)]
            if len(shard_files) == len(json_files):
                return InputSource(path=path, kind="directory", size=0, delete_target=None)
            raise ValueError(f"multiple_conversation_json_files_found count {len(json_files)}")
        resolved_json = json_files[0].resolve()
        return InputSource(path=resolved_json, kind="json", size=json_files[0].stat().st_size, delete_target=resolved_json)
    if len(zips) > 1:
        raise ValueError(f"multiple_zip_files_found count {len(zips)}")
    resolved = zips[0].resolve()
    return InputSource(path=resolved, kind="zip", size=zips[0].stat().st_size, delete_target=resolved)


def resolve_input(value: str | None, cwd: Path) -> InputSource:
    if value:
        delete_target = Path(value).expanduser()
        if not delete_target.is_absolute():
            delete_target = cwd / delete_target
        if not delete_target.exists():
            raise ValueError("input_not_found")
        p = delete_target.resolve()
        if p.is_file() and p.suffix.lower() == ".zip":
            return InputSource(path=p, kind="zip", size=p.stat().st_size, delete_target=delete_target)
        if p.is_file() and is_conversation_json_source(p.name):
            return InputSource(path=p, kind="json", size=p.stat().st_size, delete_target=delete_target)
        if p.is_dir():
            return InputSource(path=p, kind="directory", size=0, delete_target=delete_target)
        raise ValueError("input_not_supported")
    return find_default_input(cwd)


def is_legacy_conversations_source(path: str) -> bool:
    return Path(_logical_zip_path(path)).name == "conversations.json"


def is_shard_conversation_source(path: str) -> bool:
    return bool(SHARD_RE.search(_logical_zip_path(path)))


def is_conversation_json_source(path: str) -> bool:
    return is_legacy_conversations_source(path) or is_shard_conversation_source(path)


def _shard_sort_key(entry: SourceEntry) -> tuple[int, str]:
    logical = _logical_zip_path(entry.source_path)
    match = SHARD_RE.search(logical)
    return (int(match.group(2)) if match else -1, entry.source_path)


def _logical_zip_path(path: str) -> str:
    """Normalize ZIP member separators for detection while preserving source_path."""
    return path.replace("\\", "/")


def is_metadata_path(path: str) -> bool:
    logical = _logical_zip_path(path)
    parts = [part for part in logical.split("/") if part]
    if "__MACOSX" in parts:
        return True
    name = parts[-1] if parts else ""
    return name == ".DS_Store" or name.startswith("._")


def list_source_entries(input_source: InputSource) -> list[SourceEntry]:
    if input_source.kind == "zip":
        with zipfile.ZipFile(input_source.path) as zf:
            entries = [
                SourceEntry(
                    source_path=info.filename,
                    file_type=classify_file(_logical_zip_path(info.filename)),
                    size=info.file_size,
                    extension=Path(_logical_zip_path(info.filename)).suffix.lower(),
                    is_conversation_json=is_conversation_json_source(info.filename),
                )
                for info in zf.infolist()
                if not info.is_dir() and not is_metadata_path(info.filename)
            ]
    elif input_source.kind == "json":
        name = input_source.path.name
        entries = [
            SourceEntry(
                source_path=name,
                file_type=classify_file(name),
                size=input_source.path.stat().st_size,
                extension=input_source.path.suffix.lower(),
                is_conversation_json=is_conversation_json_source(name),
            )
        ]
    else:
        base = input_source.path
        entries = []
        for rel, size in _walk_directory_without_links(base):
            if is_metadata_path(rel):
                continue
            entries.append(
                SourceEntry(
                    source_path=rel,
                    file_type=classify_file(rel),
                    size=size,
                    extension=Path(rel).suffix.lower(),
                    is_conversation_json=is_conversation_json_source(rel),
                )
            )
    selected = set(e.source_path for e in select_conversation_sources(entries))
    return [
        SourceEntry(
            source_path=e.source_path,
            file_type=e.file_type,
            size=e.size,
            extension=e.extension,
            is_conversation_json=e.is_conversation_json,
            is_selected_conversation_source=e.source_path in selected,
        )
        for e in entries
    ]


def select_conversation_sources(entries: Iterable[SourceEntry]) -> list[SourceEntry]:
    conv = [e for e in entries if e.is_conversation_json]
    _reject_duplicate_conversation_sources(conv)
    shards = sorted([e for e in conv if is_shard_conversation_source(e.source_path)], key=_shard_sort_key)
    if shards:
        return shards
    legacy = sorted([e for e in conv if is_legacy_conversations_source(e.source_path)], key=lambda e: e.source_path)
    return legacy


def _reject_duplicate_conversation_sources(entries: list[SourceEntry]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    seen_identities: set[tuple[str, int | None]] = set()
    duplicate_identities: set[tuple[str, int | None]] = set()
    for entry in entries:
        logical = _logical_zip_path(entry.source_path)
        if logical in seen:
            duplicates.add(logical)
        seen.add(logical)
        match = SHARD_RE.search(logical)
        identity = ("shard", int(match.group(2))) if match else ("legacy", None)
        if identity in seen_identities:
            duplicate_identities.add(identity)
        seen_identities.add(identity)
    if duplicates:
        raise ValueError("duplicate_conversation_json_source " + ",".join(sorted(duplicates)))
    if duplicate_identities:
        labels = [
            f"shard:{number}" if kind == "shard" else "legacy"
            for kind, number in sorted(duplicate_identities, key=str)
        ]
        raise ValueError("ambiguous_conversation_source_identity " + ",".join(labels))


def load_json_from_source(input_source: InputSource, source_path: str) -> Any:
    load_options = {
        "parse_constant": _reject_non_finite_json_number,
        "parse_float": _parse_finite_json_float,
    }
    if input_source.kind == "zip":
        with zipfile.ZipFile(input_source.path) as zf:
            with zf.open(source_path) as f:
                return json.load(f, **load_options)
    if input_source.kind == "json":
        if source_path != input_source.path.name:
            raise ValueError("source_not_found")
        with input_source.path.open("r", encoding="utf-8") as f:
            return json.load(f, **load_options)
    with _open_directory_source(input_source.path, source_path) as f:
        return json.load(f, **load_options)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _walk_directory_without_links(base: Path) -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []

    def visit(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError("input_source_scan_failed") from exc
        for entry in children:
            path = Path(entry.path)
            if _is_link_or_reparse(path):
                raise ValueError("input_symlink_not_allowed")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError("input_source_scan_failed") from exc
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                results.append((path.relative_to(base).as_posix(), int(info.st_size)))

    visit(base)
    return results


def _open_directory_source(base: Path, source_path: str):
    """Open a directory member without following a replaced path component."""

    logical = PurePosixPath(source_path.replace("\\", "/"))
    if logical.is_absolute() or not logical.parts or any(part in {"", ".", ".."} for part in logical.parts):
        raise ValueError("input_source_outside_root")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if nofollow and os.open in os.supports_dir_fd:
        descriptors: list[int] = []
        try:
            current_fd = os.open(base, os.O_RDONLY | directory_flag | nofollow)
            descriptors.append(current_fd)
            for component in logical.parts[:-1]:
                current_fd = os.open(
                    component,
                    os.O_RDONLY | directory_flag | nofollow,
                    dir_fd=current_fd,
                )
                descriptors.append(current_fd)
            file_fd = os.open(logical.parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(file_fd)
                raise ValueError("input_source_not_regular_file")
            return os.fdopen(file_fd, "r", encoding="utf-8")
        except OSError as exc:
            if exc.errno in {getattr(os, "ELOOP", 62), 40}:
                raise ValueError("input_symlink_not_allowed") from exc
            raise ValueError("input_source_open_failed") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    # Portable fallback: reject every reparse component immediately before
    # opening and verify the resolved target remains under the root.
    candidate = base.joinpath(*logical.parts)
    current = base
    for component in logical.parts:
        current = current / component
        if _is_link_or_reparse(current):
            raise ValueError("input_symlink_not_allowed")
    try:
        candidate.resolve(strict=True).relative_to(base.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError("input_source_outside_root") from exc
    return candidate.open("r", encoding="utf-8")
