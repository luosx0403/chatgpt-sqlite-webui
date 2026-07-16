from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import zipfile
import codecs
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Iterator

from .json_safety import (
    JsonSafetyLimitError,
    MAX_JSON_NESTING_DEPTH,
    MAX_JSON_SCALAR_COUNT,
)
from .utils import classify_file


SHARD_RE = re.compile(r"(^|.*/)conversations-(\d+)\.json$")
MAX_JSON_INTEGER_DIGITS = 1000
JSON_STREAM_CHUNK_BYTES = 64 * 1024
# Both limits are intentional.  The byte limit is the externally documented
# resource contract; the character limit independently bounds the Python
# decoded representation for ASCII-heavy input.
MAX_JSON_ELEMENT_BYTES = 32 * 1024 * 1024
MAX_JSON_ELEMENT_CHARS = 32 * 1024 * 1024
MAX_SOURCE_TOTAL_MEMBERS = 100_000


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
    identity: tuple[int, int, int, int, int] | None = None
    delete_entry_identity: FileIdentity | None = None
    delete_target_identity: FileIdentity | None = None
    delete_parent_identity: FileIdentity | None = None
    directory_identities: dict[str, tuple[int, int, int, int, int]] = field(
        default_factory=dict, compare=False, repr=False
    )
    directory_root_identity: tuple[int, int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if self.identity is None and self.kind in {"zip", "json"}:
            object.__setattr__(self, "identity", _file_identity(self.path))
        if self.delete_target is not None and self.delete_entry_identity is None:
            object.__setattr__(self, "delete_entry_identity", _entry_identity(self.delete_target, follow_symlinks=False))
            object.__setattr__(self, "delete_target_identity", _entry_identity(self.delete_target, follow_symlinks=True))
            object.__setattr__(self, "delete_parent_identity", _entry_identity(self.delete_target.parent, follow_symlinks=True))


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_size),
        int(info.st_mtime_ns), int(info.st_ctime_ns),
    )


def _fstat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_size),
        int(info.st_mtime_ns), int(info.st_ctime_ns),
    )


@contextmanager
def _open_verified_input_file(input_source: InputSource) -> Iterator[BinaryIO]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(input_source.path, flags)
    except OSError as exc:
        raise SourceChangedDuringReadError("source_changed_during_read") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SourceChangedDuringReadError("source_changed_during_read")
        if input_source.identity is not None and _fstat_identity(info) != input_source.identity:
            raise SourceChangedDuringReadError("source_changed_during_read")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def sha256_input_source(input_source: InputSource) -> str:
    digest = hashlib.sha256()
    with _open_verified_input_file(input_source) as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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


def delete_input_if_unchanged(input_source: InputSource) -> bool:
    """Atomically stage, revalidate, and delete only the captured entry.

    The rename closes the check-to-unlink window: a replacement racing before
    the rename is moved aside, detected, and restored rather than unlinked.
    ``False`` means continuity could not be proven and nothing was deleted.
    """

    path = input_source.delete_target
    expected_entry = input_source.delete_entry_identity
    expected_target = input_source.delete_target_identity
    expected_parent = input_source.delete_parent_identity
    if path is None or expected_entry is None or expected_target is None or expected_parent is None:
        return False
    if expected_entry.nlink != 1 or expected_target.nlink != 1:
        return False
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, directory_flags)
    staged_name = f".chatgpt-archive-delete-{os.getpid()}-{secrets.token_hex(16)}"
    staged = False
    try:
        parent_info = os.fstat(parent_fd)
        if (
            stat.S_IFMT(parent_info.st_mode), int(parent_info.st_dev), int(parent_info.st_ino)
        ) != (expected_parent.file_type, expected_parent.device, expected_parent.inode):
            return False
        if not delete_input_identity_is_current(input_source):
            return False
        os.rename(path.name, staged_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        staged = True
        staged_info = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
        staged_identity = (
            stat.S_IFMT(staged_info.st_mode), int(staged_info.st_dev), int(staged_info.st_ino),
            int(staged_info.st_size), int(staged_info.st_mtime_ns),
            int(staged_info.st_nlink), stat.S_ISLNK(staged_info.st_mode),
        )
        expected_identity = (
            expected_entry.file_type, expected_entry.device, expected_entry.inode,
            expected_entry.size, expected_entry.mtime_ns,
            expected_entry.nlink, expected_entry.is_symlink,
        )
        if staged_identity != expected_identity:
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(staged_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                staged = False
            return False
        try:
            os.unlink(staged_name, dir_fd=parent_fd)
        except OSError:
            # A failed unlink must not make the caller's input disappear under
            # our private staging name.  Restore only when no racer has
            # created a new entry at the original name; never overwrite a
            # replacement merely to hide the cleanup failure.
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(staged_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                staged = False
            raise
        staged = False
        return True
    finally:
        # Never delete an unverified staged entry.  A failure is intentionally
        # visible to the caller for a safe cleanup warning.
        os.close(parent_fd)


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
        with _open_verified_input_file(input_source) as archive_stream, zipfile.ZipFile(archive_stream) as zf:
            if len(zf.filelist) > MAX_SOURCE_TOTAL_MEMBERS:
                raise ValueError("source_member_limit_exceeded")
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
        with _open_verified_input_file(input_source) as source_stream:
            source_size = os.fstat(source_stream.fileno()).st_size
        entries = [
            SourceEntry(
                source_path=name,
                file_type=classify_file(name),
                size=source_size,
                extension=input_source.path.suffix.lower(),
                is_conversation_json=is_conversation_json_source(name),
            )
        ]
    else:
        base = input_source.path
        entries = []
        root_identity = _file_identity(base)
        discovered_identities: dict[str, tuple[int, int, int, int, int]] = {}
        for rel, size, identity in _walk_directory_without_links(base):
            discovered_identities[rel] = identity
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
        object.__setattr__(input_source, "directory_identities", discovered_identities)
        object.__setattr__(input_source, "directory_root_identity", root_identity)
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


def _load_json_from_source_for_tests(input_source: InputSource, source_path: str) -> Any:
    """Materialize a complete source only for bounded synthetic tests.

    Production import and inspect paths use ``iter_json_array_from_source``.
    """
    return list(iter_json_array_from_source(input_source, source_path))


def iter_json_array_from_source(input_source: InputSource, source_path: str) -> Iterator[Any]:
    """Stream one top-level array element at a time through one UTF-8 contract."""

    with _open_source_binary(input_source, source_path) as stream:
        yield from _iter_json_array(_iter_utf8_chunks(stream))


@contextmanager
def _open_source_binary(input_source: InputSource, source_path: str) -> Iterator[BinaryIO]:
    if input_source.kind == "zip":
        with _open_verified_input_file(input_source) as archive_stream:
            try:
                zf = zipfile.ZipFile(archive_stream)
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
        with _open_verified_input_file(input_source) as stream:
            yield stream
        return
    try:
        with _open_directory_source(
            input_source.path,
            source_path,
            binary=True,
            expected_identity=input_source.directory_identities.get(source_path),
            expected_root_identity=input_source.directory_root_identity,
        ) as stream:
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
    element_bytes = 0
    kind = ""
    depth = 0
    in_string = False
    escaped = False
    scalar_count = 0
    scalar_token = False
    saw_element = False

    def append_part(value: str) -> None:
        nonlocal element_chars, element_bytes
        if not value:
            return
        element_chars += len(value)
        element_bytes += len(value.encode("utf-8"))
        if (
            element_chars > MAX_JSON_ELEMENT_CHARS
            or element_bytes > MAX_JSON_ELEMENT_BYTES
        ):
            raise ConversationJsonElementTooLargeError("conversation_json_element_too_large")
        parts.append(value)

    def finish_element() -> Any:
        nonlocal parts, element_chars, element_bytes, scalar_count, scalar_token
        value = decoder.decode("".join(parts))
        parts = []
        element_chars = 0
        element_bytes = 0
        scalar_count = 0
        scalar_token = False
        return value

    def finish_scalar_token() -> None:
        nonlocal scalar_count, scalar_token
        if not scalar_token:
            return
        scalar_count += 1
        scalar_token = False
        if scalar_count > MAX_JSON_SCALAR_COUNT:
            raise JsonSafetyLimitError(
                "json_scalar_limit_exceeded", limit=MAX_JSON_SCALAR_COUNT
            )

    def count_string_scalar() -> None:
        nonlocal scalar_count
        scalar_count += 1
        if scalar_count > MAX_JSON_SCALAR_COUNT:
            raise JsonSafetyLimitError(
                "json_scalar_limit_exceeded", limit=MAX_JSON_SCALAR_COUNT
            )

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
                    scalar_count = 0
                    scalar_token = False
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
                        scalar_token = True
                        finish_scalar_token()
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
                        count_string_scalar()
                        if kind == "string":
                            completed_at = i + 1
                            break
                else:
                    if char == '"':
                        finish_scalar_token()
                        in_string = True
                    elif char in "[{":
                        finish_scalar_token()
                        depth += 1
                        if depth > MAX_JSON_NESTING_DEPTH:
                            raise JsonSafetyLimitError(
                                "json_nesting_limit_exceeded",
                                limit=MAX_JSON_NESTING_DEPTH,
                            )
                    elif char in "]}":
                        finish_scalar_token()
                        depth -= 1
                        if depth == 0:
                            completed_at = i + 1
                            break
                    elif char in ",:":
                        finish_scalar_token()
                    elif char in " \t\r\n":
                        finish_scalar_token()
                    else:
                        scalar_token = True
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


def _walk_directory_without_links(
    base: Path,
) -> list[tuple[str, int, tuple[int, int, int, int, int]]]:
    results: list[tuple[str, int, tuple[int, int, int, int, int]]] = []
    member_count = 0

    def visit(directory: Path) -> None:
        nonlocal member_count
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError("input_source_scan_failed") from exc
        for entry in children:
            member_count += 1
            if member_count > MAX_SOURCE_TOTAL_MEMBERS:
                raise ValueError("source_member_limit_exceeded")
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
                results.append((
                    path.relative_to(base).as_posix(),
                    int(info.st_size),
                    _fstat_identity(info),
                ))

    visit(base)
    return results


def _open_directory_source(
    base: Path,
    source_path: str,
    *,
    binary: bool = False,
    expected_identity: tuple[int, int, int, int, int] | None = None,
    expected_root_identity: tuple[int, int, int, int, int] | None = None,
):
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
            root_info = os.fstat(current_fd)
            for component in logical.parts[:-1]:
                current_fd = os.open(
                    component,
                    os.O_RDONLY | directory_flag | nofollow,
                    dir_fd=current_fd,
                )
                descriptors.append(current_fd)
            file_fd = os.open(logical.parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
            info = os.fstat(file_fd)
            if (
                expected_root_identity is not None
                and _fstat_identity(root_info) != expected_root_identity
            ):
                os.close(file_fd)
                raise ValueError("source_changed_during_read")
            if not stat.S_ISREG(info.st_mode):
                os.close(file_fd)
                raise ValueError("input_source_not_regular_file")
            if expected_identity is not None and _fstat_identity(info) != expected_identity:
                os.close(file_fd)
                raise ValueError("source_changed_during_read")
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
    stream = candidate.open("rb") if binary else candidate.open("r", encoding="utf-8")
    try:
        if expected_root_identity is not None and _file_identity(base) != expected_root_identity:
            raise ValueError("source_changed_during_read")
        if expected_identity is not None and _fstat_identity(os.fstat(stream.fileno())) != expected_identity:
            raise ValueError("source_changed_during_read")
        return stream
    except BaseException:
        stream.close()
        raise
