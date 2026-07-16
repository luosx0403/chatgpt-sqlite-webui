from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import zipfile
import codecs
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Iterator

from .utils import classify_file


SHARD_RE = re.compile(r"(^|.*/)conversations-(\d+)\.json$")
MAX_JSON_INTEGER_DIGITS = 1000
JSON_STREAM_CHUNK_BYTES = 64 * 1024
MAX_JSON_ELEMENT_CHARS = 128 * 1024 * 1024


class NonFiniteJsonNumberError(ValueError):
    """Raised when a JSON source contains non-standard NaN/Infinity tokens."""


class JsonIntegerTooLargeError(ValueError):
    """Raised when a JSON integer exceeds the project-stable digit budget."""


class InvalidConversationEncodingError(ValueError):
    """Raised for invalid UTF-8 or a BOM outside the single allowed prefix."""


class EncryptedZipMemberError(ValueError):
    pass


class ZipMemberNotFoundError(ValueError):
    pass


class SourceChangedDuringReadError(ValueError):
    pass


class ZipMemberCrcError(ValueError):
    pass


class ZipMemberReadError(ValueError):
    pass


class ConversationJsonTopLevelError(ValueError):
    def __init__(self, top_level_type: str):
        super().__init__("conversation_json_top_level_not_list")
        self.top_level_type = top_level_type


class ConversationJsonElementTooLargeError(ValueError):
    """Raised before one top-level array element exceeds its scalar budget."""


def _reject_non_finite_json_number(value: str) -> None:
    raise NonFiniteJsonNumberError(f"non_finite_json_number:{value.casefold()}")


def _parse_finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _reject_non_finite_json_number(value)
    return number


def _parse_bounded_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise JsonIntegerTooLargeError("json_integer_too_large")
    return int(value, 10)


@dataclass(frozen=True)
class SourceEntry:
    source_path: str
    file_type: str
    size: int
    extension: str
    is_conversation_json: bool = False
    is_selected_conversation_source: bool = False


@dataclass(frozen=True)
class FileIdentity:
    file_type: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    is_symlink: bool
    is_reparse_point: bool
    link_target_hash: str | None = None


def _entry_identity(path: Path, *, follow_symlinks: bool) -> FileIdentity:
    info = path.stat() if follow_symlinks else path.lstat()
    is_link = stat.S_ISLNK(info.st_mode) if not follow_symlinks else False
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    target_hash = None
    if is_link:
        target_hash = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
    return FileIdentity(
        file_type=stat.S_IFMT(info.st_mode),
        device=int(info.st_dev),
        inode=int(info.st_ino),
        size=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
        ctime_ns=int(info.st_ctime_ns),
        nlink=int(info.st_nlink),
        is_symlink=is_link,
        is_reparse_point=bool(attributes & reparse_flag),
        link_target_hash=target_hash,
    )


@dataclass(frozen=True)
class InputSource:
    path: Path
    kind: str
    size: int
    delete_target: Path | None = None
    identity: tuple[int, int, int, int] | None = None
    delete_entry_identity: FileIdentity | None = None
    delete_target_identity: FileIdentity | None = None
    delete_parent_identity: FileIdentity | None = None

    def __post_init__(self) -> None:
        if self.identity is None and self.kind in {"zip", "json"}:
            object.__setattr__(self, "identity", _file_identity(self.path))
        if self.delete_target is not None and self.delete_entry_identity is None:
            object.__setattr__(self, "delete_entry_identity", _entry_identity(self.delete_target, follow_symlinks=False))
            object.__setattr__(self, "delete_target_identity", _entry_identity(self.delete_target, follow_symlinks=True))
            object.__setattr__(self, "delete_parent_identity", _entry_identity(self.delete_target.parent, follow_symlinks=True))


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))


def delete_input_identity_is_current(input_source: InputSource) -> bool:
    """Conservatively prove that the original directory entry is unchanged."""

    path = input_source.delete_target
    expected_entry = input_source.delete_entry_identity
    expected_target = input_source.delete_target_identity
    expected_parent = input_source.delete_parent_identity
    if path is None or expected_entry is None or expected_target is None or expected_parent is None:
        return False
    # Portable stat fields cannot prove pathname continuity for hard links.
    if expected_entry.nlink != 1 or expected_target.nlink != 1:
        return False
    try:
        current_parent = _entry_identity(path.parent, follow_symlinks=True)
        current_entry = _entry_identity(path, follow_symlinks=False)
        current_target = _entry_identity(path, follow_symlinks=True)
    except OSError:
        return False
    same_parent = (
        current_parent.file_type,
        current_parent.device,
        current_parent.inode,
        current_parent.is_reparse_point,
    ) == (
        expected_parent.file_type,
        expected_parent.device,
        expected_parent.inode,
        expected_parent.is_reparse_point,
    )
    return same_parent and current_entry == expected_entry and current_target == expected_target


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
    return list(iter_json_array_from_source(input_source, source_path))


def iter_json_array_from_source(input_source: InputSource, source_path: str) -> Iterator[Any]:
    """Stream one top-level array element at a time through one UTF-8 contract."""

    with _open_source_binary(input_source, source_path) as stream:
        yield from _iter_json_array(_iter_utf8_chunks(stream))


@contextmanager
def _open_source_binary(input_source: InputSource, source_path: str) -> Iterator[BinaryIO]:
    if input_source.kind in {"zip", "json"}:
        try:
            current_identity = _file_identity(input_source.path)
        except OSError as exc:
            raise SourceChangedDuringReadError("source_changed_during_read") from exc
        if input_source.identity is not None and current_identity != input_source.identity:
            raise SourceChangedDuringReadError("source_changed_during_read")
    if input_source.kind == "zip":
        try:
            zf = zipfile.ZipFile(input_source.path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise SourceChangedDuringReadError("source_changed_during_read") from exc
        try:
            try:
                info = zf.getinfo(source_path)
            except KeyError as exc:
                raise ZipMemberNotFoundError("zip_member_not_found") from exc
            if info.flag_bits & 0x1:
                raise EncryptedZipMemberError("encrypted_zip_member_not_supported")
            try:
                member = zf.open(info, "r")
            except RuntimeError as exc:
                raise EncryptedZipMemberError("encrypted_zip_member_not_supported") from exc
            except KeyError as exc:
                raise ZipMemberNotFoundError("zip_member_not_found") from exc
            try:
                yield member
            except zipfile.BadZipFile as exc:
                if "CRC" in str(exc).upper():
                    raise ZipMemberCrcError("zip_member_crc_failed") from exc
                raise ZipMemberReadError("zip_member_read_failed") from exc
            except (OSError, RuntimeError) as exc:
                raise ZipMemberReadError("zip_member_read_failed") from exc
            finally:
                member.close()
        finally:
            zf.close()
        return
    if input_source.kind == "json":
        if source_path != input_source.path.name:
            raise SourceChangedDuringReadError("source_changed_during_read")
        try:
            with input_source.path.open("rb") as stream:
                yield stream
        except FileNotFoundError as exc:
            raise SourceChangedDuringReadError("source_changed_during_read") from exc
        return
    try:
        with _open_directory_source(input_source.path, source_path, binary=True) as stream:
            yield stream
    except ValueError:
        raise
    except FileNotFoundError as exc:
        raise SourceChangedDuringReadError("source_changed_during_read") from exc


def _iter_utf8_chunks(stream: BinaryIO) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    first = True
    try:
        while True:
            data = stream.read(JSON_STREAM_CHUNK_BYTES)
            if not data:
                break
            if first:
                first = False
                if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
                    raise InvalidConversationEncodingError("invalid_conversation_encoding")
                if data.startswith(codecs.BOM_UTF8):
                    data = data[len(codecs.BOM_UTF8) :]
                    if data.startswith(codecs.BOM_UTF8):
                        raise InvalidConversationEncodingError("invalid_conversation_encoding")
            text = decoder.decode(data, final=False)
            if text:
                yield text
        tail = decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise InvalidConversationEncodingError("invalid_conversation_encoding") from exc
    if tail:
        yield tail


def _iter_json_array(chunks: Iterable[str]) -> Iterator[Any]:
    decoder = json.JSONDecoder(
        parse_constant=_reject_non_finite_json_number,
        parse_float=_parse_finite_json_float,
        parse_int=_parse_bounded_json_int,
    )
    phase = "start"
    parts: list[str] = []
    element_chars = 0
    kind = ""
    depth = 0
    in_string = False
    escaped = False
    saw_element = False

    def append_part(value: str) -> None:
        nonlocal element_chars
        if not value:
            return
        element_chars += len(value)
        if element_chars > MAX_JSON_ELEMENT_CHARS:
            raise ConversationJsonElementTooLargeError("conversation_json_element_too_large")
        parts.append(value)

    def finish_element() -> Any:
        nonlocal parts, element_chars
        value = decoder.decode("".join(parts))
        parts = []
        element_chars = 0
        return value

    for chunk in chunks:
        i = 0
        while i < len(chunk):
            if phase == "nonlist":
                append_part(chunk[i:])
                i = len(chunk)
                continue
            if phase != "element":
                while i < len(chunk) and chunk[i] in " \t\r\n":
                    i += 1
                if i >= len(chunk):
                    continue
                char = chunk[i]
                if char == "\ufeff":
                    raise InvalidConversationEncodingError("invalid_conversation_encoding")
                if phase == "start":
                    if char != "[":
                        phase = "nonlist"
                        append_part(chunk[i:])
                        i = len(chunk)
                        continue
                    phase = "between"
                    i += 1
                    continue
                if phase == "between":
                    if char == "]" and not saw_element:
                        phase = "done"
                        i += 1
                        continue
                    if char == "]":
                        raise json.JSONDecodeError("Expecting value", chunk, i)
                    kind = "composite" if char in "[{" else "string" if char == '"' else "scalar"
                    depth = 0
                    in_string = False
                    escaped = False
                    phase = "element"
                elif phase == "after":
                    if char == ",":
                        phase = "between"
                        i += 1
                        continue
                    if char == "]":
                        phase = "done"
                        i += 1
                        continue
                    raise json.JSONDecodeError("Expecting ',' delimiter", chunk, i)
                else:
                    raise json.JSONDecodeError("Extra data", chunk, i)

            start = i
            completed_at: int | None = None
            scalar_delimiter = False
            while i < len(chunk):
                char = chunk[i]
                if char == "\ufeff" and not in_string:
                    raise InvalidConversationEncodingError("invalid_conversation_encoding")
                if kind == "scalar":
                    if char in ",]":
                        completed_at = i
                        scalar_delimiter = True
                        break
                elif in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                        if kind == "string":
                            completed_at = i + 1
                            break
                else:
                    if char == '"':
                        in_string = True
                    elif char in "[{":
                        depth += 1
                    elif char in "]}":
                        depth -= 1
                        if depth == 0:
                            completed_at = i + 1
                            break
                i += 1
            if completed_at is None:
                append_part(chunk[start:])
                continue
            append_part(chunk[start:completed_at])
            yield finish_element()
            saw_element = True
            phase = "after"
            if not scalar_delimiter:
                i = completed_at
            # Scalar delimiter is reprocessed by the surrounding-array state.

    if phase == "nonlist":
        scalar = decoder.decode("".join(parts))
        raise ConversationJsonTopLevelError(type(scalar).__name__)
    if phase != "done":
        raise json.JSONDecodeError("Expecting value", "", 0)


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


def _open_directory_source(base: Path, source_path: str, *, binary: bool = False):
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
            return os.fdopen(file_fd, "rb") if binary else os.fdopen(file_fd, "r", encoding="utf-8")
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
    return candidate.open("rb") if binary else candidate.open("r", encoding="utf-8")
