from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .utils import classify_file


SHARD_RE = re.compile(r"(^|.*/)conversations-(\d+)\.json$")


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
    path = path.resolve()
    if path.is_file():
        kind = "zip" if path.suffix.lower() == ".zip" else "directory"
        return InputSource(path=path, kind=kind, size=path.stat().st_size, delete_target=path)
    zips = sorted(path.glob("*.zip"))
    if not zips:
        raise ValueError("no_zip_file_found")
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
                if not info.is_dir()
            ]
    else:
        base = input_source.path
        entries = []
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(base).as_posix()
                entries.append(
                    SourceEntry(
                        source_path=rel,
                        file_type=classify_file(rel),
                        size=p.stat().st_size,
                        extension=p.suffix.lower(),
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
    shards = sorted([e for e in conv if is_shard_conversation_source(e.source_path)], key=_shard_sort_key)
    if shards:
        return shards
    legacy = sorted([e for e in conv if is_legacy_conversations_source(e.source_path)], key=lambda e: e.source_path)
    return legacy


def load_json_from_source(input_source: InputSource, source_path: str) -> Any:
    if input_source.kind == "zip":
        with zipfile.ZipFile(input_source.path) as zf:
            with zf.open(source_path) as f:
                return json.load(f)
    with (input_source.path / source_path).open("r", encoding="utf-8") as f:
        return json.load(f)
