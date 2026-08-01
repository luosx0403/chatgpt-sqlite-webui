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
import struct
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterable, Iterator

from .json_safety import (
    JsonSafetyLimitError,
    MAX_JSON_NESTING_DEPTH,
)
from .utils import classify_file


SHARD_RE = re.compile(r"(^|.*/)conversations-(\d+)\.json$")
MAX_JSON_INTEGER_DIGITS = 1000
JSON_STREAM_CHUNK_BYTES = 64 * 1024
MAX_JSON_STRING_PRIMITIVE_TOKENS = 2_500_000
MAX_JSON_MAPPING_ENTRIES = 1_250_000
MAX_JSON_ARRAY_ITEMS = 1_000_000
MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES = 384 * 1024 * 1024
# Backward-compatible public name. The profile now independently accounts for
# mapping entries, array items, nesting, decoded heap, bytes, and characters.
MAX_CONVERSATION_JSON_SCALARS = MAX_JSON_STRING_PRIMITIVE_TOKENS
_JSON_OUTSIDE_SPECIAL_RE = re.compile(
    # Possessive repetition is essential here: a near-limit string without a
    # closing quote must not retain one backtracking state per character.
    r'"(?:\\[\s\S]|[^"\\])*+"|["\[\]{}\ufeff]'
)
_JSON_FAST_STRING_PATTERN = r'"(?:\\[\s\S]|[^"\\])*+"'
_JSON_FAST_ATOM_PATTERN = rf"(?:{_JSON_FAST_STRING_PATTERN}|[^\"\[\]{{}}\ufeff]++)"
_JSON_FAST_FLAT_CONTAINER_PATTERN = (
    rf"(?:\{{(?:{_JSON_FAST_ATOM_PATTERN})*+\}}"
    rf"|\[(?:{_JSON_FAST_ATOM_PATTERN})*+\])"
)
_JSON_FAST_ONE_LEVEL_CONTAINER_PATTERN = (
    rf"(?:\{{(?:(?:{_JSON_FAST_ATOM_PATTERN})|"
    rf"(?:{_JSON_FAST_FLAT_CONTAINER_PATTERN}))*+\}}"
    rf"|\[(?:(?:{_JSON_FAST_ATOM_PATTERN})|"
    rf"(?:{_JSON_FAST_FLAT_CONTAINER_PATTERN}))*+\])"
)
_JSON_NEXT_STRUCTURAL_RE = re.compile(
    # Entire flat child containers are balanced and can be skipped in C.
    # Everything else advances to the next structural character. A bare
    # quote is returned only when its string is incomplete in this window.
    rf"(?:(?:{_JSON_FAST_ATOM_PATTERN})|(?:{_JSON_FAST_FLAT_CONTAINER_PATTERN}))*+"
    rf"([\"\[\]{{}}\ufeff])"
)
_JSON_NEXT_STRUCTURAL_LEVEL1_RE = re.compile(
    rf"(?:(?:{_JSON_FAST_ATOM_PATTERN})|"
    rf"(?:{_JSON_FAST_ONE_LEVEL_CONTAINER_PATTERN}))*+"
    rf"([\"\[\]{{}}\ufeff])"
)
_JSON_STRING_SPECIAL_RE = re.compile(r'["\\]')
_JSON_SCALAR_END_RE = re.compile(r'[,\]]')
_JSON_NON_STRING_SCALAR_RE = re.compile(r'[^\s,\]:{}\[\]"]+')
# Both limits are intentional.  The byte limit is the externally documented
# resource contract; the character limit independently bounds the Python
# decoded representation for ASCII-heavy input.
MAX_JSON_ELEMENT_BYTES = 32 * 1024 * 1024
MAX_JSON_ELEMENT_CHARS = 32 * 1024 * 1024
JSON_RAW_DECODE_WINDOW_CHARS = 4 * 1024 * 1024
# The tiny common-case probe avoids Python token callbacks for ordinary compact
# elements.  A failed probe is followed by one extended boundary/depth proof;
# it is never retried for every source chunk.
JSON_HYBRID_FAST_PROBE_CHARS = 256
JSON_HYBRID_MEDIUM_PROBE_CHARS = 256 * 1024
JSON_HYBRID_EXTENDED_PROBE_CHARS = MAX_JSON_ELEMENT_CHARS
# Source decoding remains in 64 KiB byte chunks.  Coalescing already-decoded
# text into a bounded window lets ordinary cross-chunk elements use the safe C
# decoder path without changing source reads or buffering an entire shard.
JSON_HYBRID_COALESCE_CHARS = 32 * 1024 * 1024
MAX_SOURCE_TOTAL_MEMBERS = 100_000
MAX_SOURCE_DIRECTORY_DEPTH = 256
MAX_SOURCE_RELATIVE_PATH_BYTES = 32 * 1024
_ZIP_EOCD_SEARCH_BYTES = 22 + 65_535
DELETE_INPUT_RECOVERY_FORMAT_VERSION = 2
DELETE_INPUT_RECOVERY_PREDECESSOR_VERSIONS = (1,)
DELETE_INPUT_RECOVERY_PREFIX = ".chatgpt-archive-delete-recovery-"
DELETE_INPUT_RECOVERY_MAX_BYTES = 64 * 1024
_DELETE_DIR_FD_SUPPORTED = all(
    function in os.supports_dir_fd for function in (os.open, os.stat, os.rename, os.unlink)
)


class NonFiniteJsonNumberError(ValueError):
    """Raised when a JSON source contains non-standard NaN/Infinity tokens."""


class JsonIntegerTooLargeError(ValueError):
    """Raised when a JSON integer exceeds the project-stable digit budget."""


class InvalidConversationEncodingError(ValueError):
    """Raised for invalid UTF-8 or a BOM outside the single allowed prefix."""


class ConversationJsonObject(dict[str, Any]):
    """Top-level object carrying framer resource metrics without hidden JSON keys."""

    input_utf8_bytes: int
    decoded_chars: int
    json_scalar_count: int
    json_mapping_entries: int
    json_array_items: int
    estimated_decoded_heap_bytes: int


class EncryptedZipMemberError(ValueError):
    pass


class ZipMemberNotFoundError(ValueError):
    pass


class SourceChangedDuringReadError(ValueError):
    pass


class DeleteInputRecoveryRequired(ValueError):
    """A private staged entry could not be safely restored or removed."""

    def __init__(self, code: str, *, recovery_token: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.recovery_token = (
            recovery_token
            if isinstance(recovery_token, str)
            and len(recovery_token) == 32
            and all(character in "0123456789abcdef" for character in recovery_token)
            else None
        )
        self.journal_format_version = DELETE_INPUT_RECOVERY_FORMAT_VERSION


def delete_input_secure_identity_supported() -> bool:
    """Return whether strict irreversible deletion can exclude pre-open writers.

    The supported Python/OS primitives provide pathname identity and advisory
    locks, but no mandatory ownership guarantee against a non-cooperating
    descriptor opened before staging. Strict delete therefore fails closed on
    every currently supported platform before creating a journal or renaming.
    """

    return False


def _delete_identity_record(identity: FileIdentity) -> dict[str, Any]:
    return {
        "file_type": identity.file_type,
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "ctime_ns": identity.ctime_ns,
        "nlink": identity.nlink,
        "is_symlink": identity.is_symlink,
        "is_reparse_point": identity.is_reparse_point,
        "link_target_hash": identity.link_target_hash,
    }


def _write_delete_recovery_record(
    parent_fd: int,
    journal_name: str,
    record: dict[str, Any],
) -> None:
    payload = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > DELETE_INPUT_RECOVERY_MAX_BYTES:
        raise DeleteInputRecoveryRequired("delete_input_recovery_record_too_large")
    temporary = f"{journal_name}.tmp-{secrets.token_hex(8)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short recovery record write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.rename(temporary, journal_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _remove_delete_recovery_record(parent_fd: int, journal_name: str) -> None:
    try:
        os.unlink(journal_name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    os.fsync(parent_fd)


def _identity_from_record(value: Any) -> FileIdentity:
    required = {
        "file_type", "device", "inode", "size", "mtime_ns", "ctime_ns",
        "nlink", "is_symlink", "is_reparse_point", "link_target_hash",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
    integer_fields = ("file_type", "device", "inode", "size", "mtime_ns", "ctime_ns", "nlink")
    if any(not isinstance(value[name], int) or isinstance(value[name], bool) for name in integer_fields):
        raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
    if not isinstance(value["is_symlink"], bool) or not isinstance(value["is_reparse_point"], bool):
        raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
    link_hash = value["link_target_hash"]
    if link_hash is not None and (
        not isinstance(link_hash, str)
        or len(link_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in link_hash)
    ):
        raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
    return FileIdentity(**value)


def _stable_entry_identity_at(parent_fd: int, name: str) -> FileIdentity:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    is_symlink = stat.S_ISLNK(info.st_mode)
    link_target_hash = None
    if is_symlink:
        link_target_hash = hashlib.sha256(
            os.fsencode(os.readlink(name, dir_fd=parent_fd))
        ).hexdigest()
    return FileIdentity(
        file_type=stat.S_IFMT(info.st_mode),
        device=int(info.st_dev),
        inode=int(info.st_ino),
        size=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
        ctime_ns=int(info.st_ctime_ns),
        nlink=int(info.st_nlink),
        is_symlink=is_symlink,
        is_reparse_point=bool(attributes & reparse_flag),
        link_target_hash=link_target_hash,
    )


def _entry_matches_stable(actual: FileIdentity, expected: FileIdentity, *, allow_link_pair: bool = False) -> bool:
    expected_nlink = expected.nlink + 1 if allow_link_pair else expected.nlink
    return _stable_delete_identity(actual) == (
        expected.file_type,
        expected.device,
        expected.inode,
        expected.size,
        expected.mtime_ns,
        expected_nlink,
        expected.is_symlink,
        expected.is_reparse_point,
        expected.link_target_hash,
    )


def _parent_matches_descriptor(parent_fd: int, expected: FileIdentity) -> bool:
    info = os.fstat(parent_fd)
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return (
        stat.S_IFMT(info.st_mode),
        int(info.st_dev),
        int(info.st_ino),
        bool(attributes & reparse_flag),
    ) == (
        expected.file_type,
        expected.device,
        expected.inode,
        expected.is_reparse_point,
    )


def _sha256_verified_dir_entry(
    parent_fd: int,
    name: str,
    *,
    expected_entry: FileIdentity,
    expected_target: FileIdentity,
    allow_link_pair: bool = False,
) -> str:
    actual_entry = _stable_entry_identity_at(parent_fd, name)
    if not _entry_matches_stable(actual_entry, expected_entry, allow_link_pair=allow_link_pair):
        raise SourceChangedDuringReadError("source_changed_during_read")
    flags = os.O_RDONLY
    if not expected_entry.is_symlink:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        before_identity = (
            stat.S_IFMT(before.st_mode), int(before.st_dev), int(before.st_ino),
            int(before.st_size), int(before.st_mtime_ns), int(before.st_nlink),
        )
        expected_nlink = expected_target.nlink + 1 if allow_link_pair and not expected_entry.is_symlink else expected_target.nlink
        if before_identity != (
            expected_target.file_type, expected_target.device, expected_target.inode,
            expected_target.size, expected_target.mtime_ns, expected_nlink,
        ):
            raise SourceChangedDuringReadError("source_changed_during_read")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        after_identity = (
            stat.S_IFMT(after.st_mode), int(after.st_dev), int(after.st_ino),
            int(after.st_size), int(after.st_mtime_ns), int(after.st_nlink),
        )
        if after_identity != before_identity:
            raise SourceChangedDuringReadError("source_changed_during_read")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


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


@dataclass(frozen=True)
class DeleteInputStage:
    original_path: Path
    staged_path: Path
    source: InputSource


def _stable_delete_identity(identity: FileIdentity) -> tuple[Any, ...]:
    # rename changes ctime on common filesystems, so content continuity is
    # instead re-established by parsing the staged InputSource through its new
    # descriptor-bound identity.  Every other captured entry attribute stays
    # part of the staging barrier.
    return (
        identity.file_type,
        identity.device,
        identity.inode,
        identity.size,
        identity.mtime_ns,
        identity.nlink,
        identity.is_symlink,
        identity.is_reparse_point,
        identity.link_target_hash,
    )


def stage_input_for_delete(input_source: InputSource) -> DeleteInputStage:
    """Reject the retired pre-commit rename design.

    Production keeps the original directory entry present through canonical
    commit and uses :func:`delete_input_if_unchanged` afterwards with a durable
    recovery record.  Retaining this callable as an explicit refusal keeps old
    integrations from silently reintroducing crash-unsafe staging.
    """

    raise DeleteInputRecoveryRequired("delete_input_precommit_staging_disabled")


def restore_staged_input(stage: DeleteInputStage) -> None:
    """Atomically restore without overwriting a replacement directory entry."""

    try:
        os.link(stage.staged_path, stage.original_path, follow_symlinks=False)
        os.unlink(stage.staged_path)
    except OSError as exc:
        raise DeleteInputRecoveryRequired("delete_input_recovery_required") from exc


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


def delete_input_if_unchanged(
    input_source: InputSource,
    *,
    expected_source_sha256: str | None = None,
) -> bool:
    """Fail closed before any journal, rename, staging, or unlink operation.

    The historical recovery reader remains available for journals produced by
    older supported releases, but ordinary production code has no capability
    that can activate irreversible deletion.  In particular, monkeypatching
    :func:`delete_input_secure_identity_supported` cannot resurrect the retired
    implementation.
    """

    del input_source, expected_source_sha256
    raise DeleteInputRecoveryRequired("delete_input_secure_identity_unsupported")


def recover_delete_input(directory: Path, owner_token: str) -> str:
    """Restore a crash-left staged input using its durable recovery token."""

    if not _DELETE_DIR_FD_SUPPORTED or os.link not in os.supports_dir_fd:
        raise DeleteInputRecoveryRequired("delete_input_secure_identity_unsupported")
    if len(owner_token) != 32 or any(ch not in "0123456789abcdef" for ch in owner_token):
        raise DeleteInputRecoveryRequired("delete_input_recovery_token_invalid")
    journal_name = f"{DELETE_INPUT_RECOVERY_PREFIX}{owner_token}.json"
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(directory, flags)
    try:
        record_fd = os.open(
            journal_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            info = os.fstat(record_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > DELETE_INPUT_RECOVERY_MAX_BYTES:
                raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
            payload = os.read(record_fd, DELETE_INPUT_RECOVERY_MAX_BYTES + 1)
        finally:
            os.close(record_fd)
        try:
            record = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid") from None
        required = {
            "format_version",
            "owner_token",
            "state",
            "original_name",
            "staged_name",
            "source_sha256",
            "entry_identity",
            "target_identity",
            "parent_identity",
        }
        if (
            not isinstance(record, dict)
            or set(record) != required
            or record["format_version"] not in (
                *DELETE_INPUT_RECOVERY_PREDECESSOR_VERSIONS,
                DELETE_INPUT_RECOVERY_FORMAT_VERSION,
            )
            or not secrets.compare_digest(str(record["owner_token"]), owner_token)
            or record["state"] not in {
                "prepared", "rename_pending", "staged", "delete_pending",
                "deleted", "restore_pending", "restored",
            }
        ):
            raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
        if record["format_version"] == 1 and record["state"] not in {"prepared", "staged"}:
            raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
        original_name = str(record["original_name"])
        staged_name = str(record["staged_name"])
        expected_staged = f".chatgpt-archive-delete-{owner_token}"
        if (
            not original_name
            or original_name in {".", ".."}
            or "/" in original_name
            or os.sep in original_name
            or staged_name != expected_staged
        ):
            raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
        source_sha256 = record["source_sha256"]
        if (
            not isinstance(source_sha256, str)
            or len(source_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in source_sha256)
        ):
            raise DeleteInputRecoveryRequired("delete_input_recovery_record_invalid")
        expected_entry = _identity_from_record(record["entry_identity"])
        expected_target = _identity_from_record(record["target_identity"])
        expected_parent = _identity_from_record(record["parent_identity"])
        if (
            expected_entry.nlink != 1
            or expected_target.nlink != 1
            or not _parent_matches_descriptor(parent_fd, expected_parent)
        ):
            raise DeleteInputRecoveryRequired("delete_input_recovery_identity_mismatch")

        def write_state(state: str) -> None:
            record["format_version"] = DELETE_INPUT_RECOVERY_FORMAT_VERSION
            record["state"] = state
            _write_delete_recovery_record(parent_fd, journal_name, record)

        def verify_entry(name: str, *, allow_link_pair: bool = False) -> None:
            try:
                digest = _sha256_verified_dir_entry(
                    parent_fd,
                    name,
                    expected_entry=expected_entry,
                    expected_target=expected_target,
                    allow_link_pair=allow_link_pair,
                )
            except (OSError, SourceChangedDuringReadError) as exc:
                raise DeleteInputRecoveryRequired(
                    "delete_input_recovery_identity_mismatch"
                ) from exc
            if not secrets.compare_digest(digest, source_sha256):
                raise DeleteInputRecoveryRequired("delete_input_recovery_identity_mismatch")

        def exists(name: str) -> bool:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False

        original_exists = exists(original_name)
        staged_exists = exists(staged_name)
        if original_exists and staged_exists:
            if record["state"] != "restore_pending":
                raise DeleteInputRecoveryRequired("delete_input_recovery_original_occupied")
            original_info = _stable_entry_identity_at(parent_fd, original_name)
            staged_info = _stable_entry_identity_at(parent_fd, staged_name)
            if (
                original_info.device != staged_info.device
                or original_info.inode != staged_info.inode
            ):
                raise DeleteInputRecoveryRequired("delete_input_recovery_original_occupied")
            verify_entry(original_name, allow_link_pair=True)
            os.unlink(staged_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            write_state("restored")
            _remove_delete_recovery_record(parent_fd, journal_name)
            return "restored"
        if staged_exists:
            if record["state"] in {"deleted", "restored"}:
                raise DeleteInputRecoveryRequired("delete_input_recovery_identity_mismatch")
            verify_entry(staged_name)
            write_state("restore_pending")
            os.link(
                staged_name,
                original_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(staged_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            write_state("restored")
            _remove_delete_recovery_record(parent_fd, journal_name)
            return "restored"
        if original_exists:
            if record["state"] == "deleted":
                raise DeleteInputRecoveryRequired("delete_input_recovery_identity_mismatch")
            verify_entry(original_name)
            write_state("restored")
            _remove_delete_recovery_record(parent_fd, journal_name)
            return "already_restored"
        if record["state"] not in {"delete_pending", "deleted"}:
            raise DeleteInputRecoveryRequired("delete_input_recovery_identity_mismatch")
        write_state("deleted")
        _remove_delete_recovery_record(parent_fd, journal_name)
        return "already_deleted"
    except (FileNotFoundError, NotImplementedError) as exc:
        raise DeleteInputRecoveryRequired("delete_input_recovery_record_not_found") from exc
    finally:
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


def preflight_zip_central_directory(stream: BinaryIO, *, max_members: int) -> int:
    """Bound central-directory object creation before ``zipfile.ZipFile``.

    This metadata check complements rather than replaces the stdlib parser and
    its structural/CRC checks.
    """

    stream.seek(0, os.SEEK_END)
    archive_size = stream.tell()
    tail_size = min(archive_size, _ZIP_EOCD_SEARCH_BYTES)
    stream.seek(archive_size - tail_size)
    tail = stream.read(tail_size)
    marker = tail.rfind(b"PK\x05\x06")
    while marker >= 0:
        if len(tail) - marker >= 22:
            candidate_comment_length = struct.unpack_from("<H", tail, marker + 20)[0]
            if marker + 22 + candidate_comment_length == len(tail):
                break
        marker = tail.rfind(b"PK\x05\x06", 0, marker)
    if marker < 0:
        raise ValueError("source_zip_eocd_invalid")
    eocd_offset = archive_size - tail_size + marker
    (
        _signature,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = struct.unpack_from("<4s4H2LH", tail, marker)
    # The matching-candidate scan above intentionally tolerates the EOCD
    # signature bytes inside an otherwise valid ZIP comment.
    if disk_number != 0 or central_disk != 0 or entries_on_disk != total_entries:
        raise ValueError("source_zip_multidisk_not_supported")
    if total_entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        locator_offset = eocd_offset - 20
        if locator_offset < 0:
            raise ValueError("source_zip64_eocd_invalid")
        stream.seek(locator_offset)
        locator = stream.read(20)
        if len(locator) != 20 or locator[:4] != b"PK\x06\x07":
            raise ValueError("source_zip64_eocd_invalid")
        _loc_signature, zip64_disk, zip64_offset, disk_count = struct.unpack("<4sLQL", locator)
        if zip64_disk != 0 or disk_count != 1:
            raise ValueError("source_zip_multidisk_not_supported")
        if zip64_offset < 0 or zip64_offset + 56 > locator_offset:
            raise ValueError("source_zip64_eocd_invalid")
        stream.seek(zip64_offset)
        record = stream.read(56)
        if len(record) != 56 or record[:4] != b"PK\x06\x06":
            raise ValueError("source_zip64_eocd_invalid")
        (
            _signature64,
            record_size,
            _made_by,
            _needed,
            disk_number64,
            central_disk64,
            entries_on_disk64,
            total_entries64,
            central_size64,
            central_offset64,
        ) = struct.unpack("<4sQ2H2L4Q", record)
        if record_size < 44 or disk_number64 != 0 or central_disk64 != 0 or entries_on_disk64 != total_entries64:
            raise ValueError("source_zip64_eocd_invalid")
        total_entries = total_entries64
        central_size = central_size64
        central_offset = central_offset64
    if total_entries > max_members:
        raise ValueError("source_member_limit_exceeded")
    if central_offset > archive_size or central_size > archive_size or central_offset + central_size > eocd_offset:
        raise ValueError("source_zip_central_directory_invalid")
    stream.seek(0)
    return int(total_entries)


def list_source_entries(input_source: InputSource) -> list[SourceEntry]:
    if input_source.kind == "zip":
        with _open_verified_input_file(input_source) as archive_stream:
            preflight_zip_central_directory(archive_stream, max_members=MAX_SOURCE_TOTAL_MEMBERS)
            zf = zipfile.ZipFile(archive_stream)
            try:
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
            finally:
                zf.close()
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
        # Consume the sorted discovery tuples destructively.  At the 100,000
        # member boundary this transfers their path/identity objects into the
        # durable entry and identity collections instead of retaining a third
        # complete helper collection until every SourceEntry has been built.
        discovered = _walk_directory_without_links(base)
        while discovered:
            rel, size, identity = discovered.pop()
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
    selected = {e.source_path for e in select_conversation_sources(entries)}
    # Only the normally tiny selected subset needs replacement.  Rebuilding
    # every frozen SourceEntry used to double the complete member list at the
    # scanner's upper bound.
    for index, entry in enumerate(entries):
        if entry.source_path in selected:
            entries[index] = replace(entry, is_selected_conversation_source=True)
    return entries


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
def open_source_read_session(
    input_source: InputSource,
) -> Iterator[Callable[[str], Iterator[Any]]]:
    """Reuse one verified archive and one ZipFile across every selected shard."""

    if input_source.kind != "zip":
        yield lambda source_path: iter_json_array_from_source(input_source, source_path)
        return
    with _open_verified_input_file(input_source) as archive_stream:
        preflight_zip_central_directory(archive_stream, max_members=MAX_SOURCE_TOTAL_MEMBERS)
        try:
            zf = zipfile.ZipFile(archive_stream)
        except (OSError, zipfile.BadZipFile) as exc:
            raise SourceChangedDuringReadError("source_changed_during_read") from exc

        def iterate(source_path: str) -> Iterator[Any]:
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
                yield from _iter_json_array(_iter_utf8_chunks(member))
            except zipfile.BadZipFile as exc:
                if "CRC" in str(exc).upper():
                    raise ZipMemberCrcError("zip_member_crc_failed") from exc
                raise ZipMemberReadError("zip_member_read_failed") from exc
            except (OSError, RuntimeError) as exc:
                raise ZipMemberReadError("zip_member_read_failed") from exc
            finally:
                member.close()

        try:
            yield iterate
        finally:
            zf.close()


def iter_source_array_sessions(
    input_source: InputSource,
    entries: Iterable[SourceEntry],
) -> Iterator[tuple[int, SourceEntry, Iterator[Any]]]:
    """Yield shard iterators while keeping their shared ZIP session alive."""

    with open_source_read_session(input_source) as iterate:
        for index, entry in enumerate(entries):
            yield index, entry, iter(iterate(entry.source_path))


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


class _MeasuredJsonObject(dict[str, Any]):
    json_scalar_count: int
    json_depth: int
    json_mapping_entries: int
    json_array_items: int


def _measure_decoded_json(value: Any) -> tuple[int, int, int, int]:
    if isinstance(value, _MeasuredJsonObject):
        return (
            value.json_scalar_count,
            value.json_depth,
            value.json_mapping_entries,
            value.json_array_items,
        )
    if isinstance(value, list):
        scalar_count = 0
        mapping_entries = 0
        array_items = len(value)
        child_depth = 0
        for child in value:
            child_scalars, depth, child_mappings, child_arrays = _measure_decoded_json(child)
            scalar_count += child_scalars
            mapping_entries += child_mappings
            array_items += child_arrays
            child_depth = max(child_depth, depth)
            if scalar_count > MAX_CONVERSATION_JSON_SCALARS:
                raise JsonSafetyLimitError(
                    "json_scalar_limit_exceeded", limit=MAX_CONVERSATION_JSON_SCALARS
                )
            if mapping_entries > MAX_JSON_MAPPING_ENTRIES:
                raise JsonSafetyLimitError(
                    "json_mapping_entry_limit_exceeded", limit=MAX_JSON_MAPPING_ENTRIES
                )
            if array_items > MAX_JSON_ARRAY_ITEMS:
                raise JsonSafetyLimitError(
                    "json_array_item_limit_exceeded", limit=MAX_JSON_ARRAY_ITEMS
                )
        depth = child_depth + 1
        if depth > MAX_JSON_NESTING_DEPTH:
            raise JsonSafetyLimitError(
                "json_nesting_limit_exceeded", limit=MAX_JSON_NESTING_DEPTH
            )
        return scalar_count, depth, mapping_entries, array_items
    return 1, 0, 0, 0


def _measured_object_pairs(pairs: list[tuple[str, Any]]) -> _MeasuredJsonObject:
    scalar_count = len(pairs)  # Every object key is a JSON string scalar.
    mapping_entries = len(pairs)
    array_items = 0
    child_depth = 0
    for _key, value in pairs:
        child_scalars, depth, child_mappings, child_arrays = _measure_decoded_json(value)
        scalar_count += child_scalars
        mapping_entries += child_mappings
        array_items += child_arrays
        child_depth = max(child_depth, depth)
        if scalar_count > MAX_CONVERSATION_JSON_SCALARS:
            raise JsonSafetyLimitError(
                "json_scalar_limit_exceeded", limit=MAX_CONVERSATION_JSON_SCALARS
            )
        if mapping_entries > MAX_JSON_MAPPING_ENTRIES:
            raise JsonSafetyLimitError(
                "json_mapping_entry_limit_exceeded", limit=MAX_JSON_MAPPING_ENTRIES
            )
        if array_items > MAX_JSON_ARRAY_ITEMS:
            raise JsonSafetyLimitError(
                "json_array_item_limit_exceeded", limit=MAX_JSON_ARRAY_ITEMS
            )
    depth = child_depth + 1
    if depth > MAX_JSON_NESTING_DEPTH:
        raise JsonSafetyLimitError(
            "json_nesting_limit_exceeded", limit=MAX_JSON_NESTING_DEPTH
        )
    value = _MeasuredJsonObject(pairs)
    value.json_scalar_count = scalar_count
    value.json_depth = depth
    value.json_mapping_entries = mapping_entries
    value.json_array_items = array_items
    return value


def _iter_json_array_retained_tail(chunks: Iterable[str]) -> Iterator[Any]:
    """Decode retained top-level tails with the C JSON decoder.

    Input is still consumed incrementally and only one decoded array element
    is yielded at a time.  A 4 MiB coalescing window avoids rescanning a
    growing tail for every 64 KiB source read; the existing 32 MiB per-element
    byte and character ceilings bound the retained tail.
    """

    decoder = json.JSONDecoder(
        object_pairs_hook=_measured_object_pairs,
        parse_constant=_reject_non_finite_json_number,
        parse_float=_parse_finite_json_float,
        parse_int=_parse_bounded_json_int,
    )
    phase = "start"
    saw_element = False
    tail = ""
    pending: list[str] = []
    pending_chars = 0

    def decode_window(data: str, *, final: bool) -> Iterator[Any]:
        nonlocal phase, saw_element, tail
        position = 0
        length = len(data)
        while True:
            while position < length and data[position] in " \t\r\n":
                position += 1
            if position >= length:
                tail = ""
                return
            character = data[position]
            if character == "\ufeff":
                raise InvalidConversationEncodingError("invalid_conversation_encoding")
            if phase == "start":
                if character != "[":
                    top_level_type = {
                        "{": "dict", '"': "str", "t": "bool", "f": "bool", "n": "NoneType"
                    }.get(character, "number")
                    raise ConversationJsonTopLevelError(top_level_type)
                phase = "between"
                position += 1
                continue
            if phase == "after":
                if character == ",":
                    phase = "between"
                    position += 1
                    continue
                if character == "]":
                    phase = "done"
                    position += 1
                    continue
                raise json.JSONDecodeError("Expecting ',' delimiter", data, position)
            if phase == "done":
                raise json.JSONDecodeError("Extra data", data, position)
            if character == "]":
                if saw_element:
                    raise json.JSONDecodeError("Expecting value", data, position)
                phase = "done"
                position += 1
                continue

            element_start = position
            try:
                value, element_end = decoder.raw_decode(data, position)
            except json.JSONDecodeError:
                tail = data[element_start:]
                tail_chars = len(tail)
                if tail_chars > MAX_JSON_ELEMENT_CHARS or len(tail.encode("utf-8")) > MAX_JSON_ELEMENT_BYTES:
                    raise ConversationJsonElementTooLargeError("conversation_json_element_too_large")
                if final:
                    raise
                return
            element_text = data[element_start:element_end]
            element_chars = len(element_text)
            element_bytes = len(element_text.encode("utf-8"))
            if element_chars > MAX_JSON_ELEMENT_CHARS or element_bytes > MAX_JSON_ELEMENT_BYTES:
                raise ConversationJsonElementTooLargeError("conversation_json_element_too_large")
            _measure_decoded_json(value)
            if isinstance(value, dict):
                measured = ConversationJsonObject(value)
                measured.input_utf8_bytes = element_bytes
                measured.decoded_chars = element_chars
                value = measured
            position = element_end
            phase = "after"
            saw_element = True
            yield value

    for chunk in chunks:
        pending.append(chunk)
        pending_chars += len(chunk)
        if pending_chars < JSON_RAW_DECODE_WINDOW_CHARS:
            continue
        window = tail + "".join(pending)
        tail = ""
        pending.clear()
        pending_chars = 0
        yield from decode_window(window, final=False)
    window = tail + "".join(pending)
    tail = ""
    yield from decode_window(window, final=True)
    if phase != "done":
        raise json.JSONDecodeError("Expecting value", "", 0)


def _iter_json_array_framed(chunks: Iterable[str]) -> Iterator[Any]:
    decoder = json.JSONDecoder(
        object_pairs_hook=_measured_object_pairs,
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
    mapping_entries = 0
    array_items = 0
    container_stack: list[dict[str, Any]] = []
    saw_element = False

    def check_predecode_limits(*, pending_bytes: int = 0) -> None:
        if mapping_entries > MAX_JSON_MAPPING_ENTRIES:
            raise JsonSafetyLimitError(
                "json_mapping_entry_limit_exceeded", limit=MAX_JSON_MAPPING_ENTRIES
            )
        if array_items > MAX_JSON_ARRAY_ITEMS:
            raise JsonSafetyLimitError(
                "json_array_item_limit_exceeded", limit=MAX_JSON_ARRAY_ITEMS
            )
        estimated_heap = (
            element_bytes
            + pending_bytes
            + scalar_count * 48
            + mapping_entries * 72
            + array_items * 16
        )
        if estimated_heap > MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES:
            raise JsonSafetyLimitError(
                "json_estimated_heap_limit_exceeded",
                limit=MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES,
            )

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
        check_predecode_limits()
        parts.append(value)

    def decorate_value(value: Any, *, decoded_chars: int, input_bytes: int) -> Any:
        measured_scalars, _depth, measured_mappings, measured_arrays = _measure_decoded_json(value)
        estimated_heap = (
            input_bytes
            + measured_scalars * 48
            + measured_mappings * 72
            + measured_arrays * 16
        )
        if estimated_heap > MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES:
            raise JsonSafetyLimitError(
                "json_estimated_heap_limit_exceeded",
                limit=MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES,
            )
        if isinstance(value, dict):
            measured = ConversationJsonObject(value)
            measured.input_utf8_bytes = input_bytes
            measured.decoded_chars = decoded_chars
            measured.json_scalar_count = measured_scalars
            measured.json_mapping_entries = measured_mappings
            measured.json_array_items = measured_arrays
            measured.estimated_decoded_heap_bytes = estimated_heap
            value = measured
        return value

    def finish_element() -> Any:
        nonlocal parts, element_chars, element_bytes, scalar_count, scalar_token
        nonlocal mapping_entries, array_items, container_stack
        value = decorate_value(
            decoder.decode("".join(parts)),
            decoded_chars=element_chars,
            input_bytes=element_bytes,
        )
        parts = []
        element_chars = 0
        element_bytes = 0
        scalar_count = 0
        scalar_token = False
        mapping_entries = 0
        array_items = 0
        container_stack = []
        return value

    def mark_array_value() -> None:
        nonlocal array_items
        if container_stack and container_stack[-1]["kind"] == "array":
            array_items += 1
            check_predecode_limits()

    def finish_scalar_token() -> None:
        nonlocal scalar_count, scalar_token
        if not scalar_token:
            return
        scalar_count += 1
        scalar_token = False
        if scalar_count > MAX_CONVERSATION_JSON_SCALARS:
            raise JsonSafetyLimitError(
                "json_scalar_limit_exceeded", limit=MAX_CONVERSATION_JSON_SCALARS
            )
        check_predecode_limits()

    def count_string_scalar() -> None:
        nonlocal scalar_count
        scalar_count += 1
        if scalar_count > MAX_CONVERSATION_JSON_SCALARS:
            raise JsonSafetyLimitError(
                "json_scalar_limit_exceeded", limit=MAX_CONVERSATION_JSON_SCALARS
            )
        check_predecode_limits()

    def scan_outside_segment(value: str) -> None:
        """Count non-string tokens while C regex skips punctuation runs.

        The previous scanner returned to Python for every comma, colon and
        whitespace character.  Large metadata-rich exports contain millions
        of those delimiters.  This keeps the exact lexical accounting while
        returning only for the much rarer scalar token spans.
        """

        nonlocal scalar_token, mapping_entries
        mapping_entries += value.count(":")
        check_predecode_limits(pending_bytes=len(value.encode("utf-8")))
        position = 0
        for match in _JSON_NON_STRING_SCALAR_RE.finditer(value):
            if match.start() > position:
                finish_scalar_token()
            if not scalar_token:
                # A token may continue from the preceding chunk; in that case
                # scalar_token is already true and must be counted only once.
                mark_array_value()
                scalar_token = True
            position = match.end()
        if position < len(value):
            finish_scalar_token()

    def probe_is_resource_safe(probe: str) -> bool:
        """Conservatively clear a bounded C-decoder probe before allocation."""

        # For a probe no longer than every independent count/depth ceiling,
        # its worst possible UTF-8 width and per-character structural cost are
        # already below the heap ceiling. This is a proof from lengths alone
        # and removes four complete scans from the ordinary tiny-element path.
        worst_utf8_bytes = len(probe) * 4
        if (
            len(probe) <= MAX_CONVERSATION_JSON_SCALARS
            and len(probe) <= MAX_JSON_MAPPING_ENTRIES
            and len(probe) <= MAX_JSON_ARRAY_ITEMS
            and len(probe) <= MAX_JSON_NESTING_DEPTH
            and worst_utf8_bytes + len(probe) * (48 + 72 + 16)
            <= MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES
        ):
            return True
        probe_bytes = len(probe.encode("utf-8"))
        conservative_heap = probe_bytes + len(probe) * (48 + 72 + 16)
        return (
            len(probe) <= MAX_CONVERSATION_JSON_SCALARS
            and len(probe) <= MAX_JSON_MAPPING_ENTRIES
            and len(probe) <= MAX_JSON_ARRAY_ITEMS
            and probe.count("[") + probe.count("{") <= MAX_JSON_NESTING_DEPTH
            and conservative_heap <= MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES
        )

    def fast_element_proof(
        data: str,
        start: int,
    ) -> tuple[int, int, int, int] | None:
        """Return boundary plus conservative pre-decode resource upper bounds."""

        limit = min(len(data), start + JSON_HYBRID_EXTENDED_PROBE_CHARS)
        first = data[start]
        if first == '"':
            match = _JSON_OUTSIDE_SPECIAL_RE.match(data, start, limit)
            if match is None or match.end() - match.start() <= 1:
                return None
            candidate = data[start : match.end()]
            quote_upper = candidate.count('"')
            mapping_upper = candidate.count(":")
            array_upper = candidate.count(",")
            return match.end(), quote_upper + mapping_upper + array_upper + 1, mapping_upper, array_upper
        if first not in "[{":
            match = _JSON_SCALAR_END_RE.search(data, start, limit)
            return (match.start(), 1, 0, 0) if match is not None else None

        # Consume the top-level opener explicitly. Allowing the balanced-child
        # regex to swallow it would lose the element boundary and could walk
        # across the surrounding array comma into the next element.
        fast_depth = 1
        position = start + 1
        while position < limit:
            structural_re = (
                _JSON_NEXT_STRUCTURAL_LEVEL1_RE
                if fast_depth <= MAX_JSON_NESTING_DEPTH - 2
                else _JSON_NEXT_STRUCTURAL_RE
            )
            match = structural_re.match(data, position, limit)
            if match is None:
                return None
            skipped_end = match.start(1)
            if (
                fast_depth >= MAX_JSON_NESTING_DEPTH
                and (
                    data.find("{", position, skipped_end) >= 0
                    or data.find("[", position, skipped_end) >= 0
                )
            ):
                raise JsonSafetyLimitError(
                    "json_nesting_limit_exceeded",
                    limit=MAX_JSON_NESTING_DEPTH,
                )
            char = match.group(1)
            if char == '"':
                # The bounded window ended inside a string.
                return None
            if char == "\ufeff":
                raise InvalidConversationEncodingError("invalid_conversation_encoding")
            if char in "[{":
                fast_depth += 1
                if fast_depth > MAX_JSON_NESTING_DEPTH:
                    raise JsonSafetyLimitError(
                        "json_nesting_limit_exceeded",
                        limit=MAX_JSON_NESTING_DEPTH,
                    )
            else:
                fast_depth -= 1
                if fast_depth == 0:
                    candidate_end = match.end()
                    candidate = data[start:candidate_end]
                    quote_upper = candidate.count('"')
                    mapping_upper = candidate.count(":")
                    comma_upper = candidate.count(",")
                    container_upper = (
                        candidate.count("[") + candidate.count("{")
                    )
                    scalar_upper = (
                        quote_upper
                        + mapping_upper
                        + comma_upper
                        + container_upper
                        + 1
                    )
                    array_upper = comma_upper + container_upper
                    return (
                        candidate_end,
                        scalar_upper,
                        mapping_upper,
                        array_upper,
                    )
            position = match.end()
        return None

    chunk_iterator = iter(chunks)
    for chunk in chunk_iterator:
        i = 0
        while i < len(chunk):
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
                        top_level_type = {
                            "{": "dict",
                            '"': "str",
                            "t": "bool",
                            "f": "bool",
                            "n": "NoneType",
                        }.get(char, "number")
                        raise ConversationJsonTopLevelError(top_level_type)
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
                    compact_probe = chunk[
                        i : i + JSON_HYBRID_FAST_PROBE_CHARS
                    ]
                    compact_consumed = False
                    if probe_is_resource_safe(compact_probe):
                        try:
                            compact_value, compact_end = decoder.raw_decode(
                                compact_probe
                            )
                        except (json.JSONDecodeError, RecursionError):
                            pass
                        else:
                            compact_boundary_proven = (
                                compact_end < len(compact_probe)
                                and compact_probe[compact_end] in " \t\r\n,]"
                            ) or (
                                compact_end == len(compact_probe)
                                and char in "[{\""
                            )
                            if compact_boundary_proven:
                                compact_chars = compact_end
                                compact_bytes = len(
                                    compact_probe[:compact_end].encode("utf-8")
                                )
                                if (
                                    compact_chars > MAX_JSON_ELEMENT_CHARS
                                    or compact_bytes > MAX_JSON_ELEMENT_BYTES
                                ):
                                    raise ConversationJsonElementTooLargeError(
                                        "conversation_json_element_too_large"
                                    )
                                yield decorate_value(
                                    compact_value,
                                    decoded_chars=compact_chars,
                                    input_bytes=compact_bytes,
                                )
                                saw_element = True
                                phase = "after"
                                i += compact_end
                                compact_consumed = True
                    if compact_consumed:
                        continue
                    fast_proof = fast_element_proof(chunk, i)
                    if (
                        fast_proof is None
                        and len(chunk) - i < JSON_HYBRID_EXTENDED_PROBE_CHARS
                    ):
                        try:
                            next_window = next(chunk_iterator)
                        except StopIteration:
                            next_window = ""
                        if next_window:
                            # Preserve only the unconsumed suffix.  This
                            # realigns a bounded window around an ordinary
                            # element crossing a coalescing boundary without
                            # retaining the shard or rescanning prior elements.
                            chunk = chunk[i:] + next_window
                            i = 0
                            char = chunk[0]
                            fast_proof = fast_element_proof(chunk, i)
                    fast_candidate_end = fast_proof[0] if fast_proof is not None else None
                    fast_candidate_chars = (
                        fast_candidate_end - i
                        if fast_candidate_end is not None
                        else 0
                    )
                    fast_candidate_text = (
                        chunk[i:fast_candidate_end]
                        if fast_candidate_end is not None
                        else ""
                    )
                    probe_bytes = len(fast_candidate_text.encode("utf-8"))
                    max_candidate_scalars = fast_proof[1] if fast_proof is not None else 0
                    measured_mappings = fast_proof[2] if fast_proof is not None else 0
                    max_candidate_array_items = fast_proof[3] if fast_proof is not None else 0
                    conservative_heap = (
                        probe_bytes
                        + max_candidate_scalars * 48
                        + measured_mappings * 72
                        + max_candidate_array_items * 16
                    )
                    if (
                        fast_candidate_end is not None
                        and max_candidate_scalars <= MAX_CONVERSATION_JSON_SCALARS
                        and measured_mappings <= MAX_JSON_MAPPING_ENTRIES
                        and max_candidate_array_items <= MAX_JSON_ARRAY_ITEMS
                        and conservative_heap <= MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES
                    ):
                        try:
                            fast_value, fast_end = decoder.raw_decode(chunk, i)
                        except (json.JSONDecodeError, RecursionError):
                            pass
                        else:
                            # raw_decode() accepts a valid numeric prefix
                            # (for example ``1`` in ``1e2``).  A scalar at the
                            # end of this bounded probe therefore has no
                            # proven token boundary yet.  Composite/string
                            # closure is self-delimiting; every other success
                            # must expose a surrounding-array delimiter or
                            # whitespace inside the current probe.
                            fast_boundary_proven = (
                                fast_end < len(chunk)
                                and chunk[fast_end] in " \t\r\n,]"
                            ) or (
                                fast_end == len(chunk)
                                and char in "[{\""
                            )
                            fast_boundary_proven = (
                                fast_boundary_proven
                                and fast_end == fast_candidate_end
                            )
                            if not fast_boundary_proven:
                                fast_value = None
                                fast_end = 0
                            if fast_end == 0:
                                kind = (
                                    "composite"
                                    if char in "[{"
                                    else "string"
                                    if char == '"'
                                    else "scalar"
                                )
                                depth = 0
                                in_string = False
                                escaped = False
                                scalar_count = 0
                                scalar_token = False
                                mapping_entries = 0
                                array_items = 0
                                container_stack = []
                                phase = "element"
                                continue
                            fast_chars = fast_end - i
                            fast_bytes = probe_bytes
                            if (
                                fast_chars > MAX_JSON_ELEMENT_CHARS
                                or fast_bytes > MAX_JSON_ELEMENT_BYTES
                            ):
                                raise ConversationJsonElementTooLargeError(
                                    "conversation_json_element_too_large"
                                )
                            yield decorate_value(
                                fast_value,
                                decoded_chars=fast_chars,
                                input_bytes=fast_bytes,
                            )
                            saw_element = True
                            phase = "after"
                            i = fast_end
                            continue
                    kind = "composite" if char in "[{" else "string" if char == '"' else "scalar"
                    depth = 0
                    in_string = False
                    escaped = False
                    scalar_count = 0
                    scalar_token = False
                    mapping_entries = 0
                    array_items = 0
                    container_stack = []
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
                if kind == "scalar":
                    match = _JSON_SCALAR_END_RE.search(chunk, i)
                    if match is None:
                        if i < len(chunk):
                            scalar_token = True
                        i = len(chunk)
                        break
                    i = match.start()
                    scalar_token = True
                    finish_scalar_token()
                    completed_at = i
                    scalar_delimiter = True
                    break
                if in_string:
                    if escaped:
                        # The JSON decoder validates the escape itself.  The
                        # framer only needs to prevent an escaped quote from
                        # terminating this top-level element.
                        escaped = False
                        i += 1
                        continue
                    match = _JSON_STRING_SPECIAL_RE.search(chunk, i)
                    if match is None:
                        i = len(chunk)
                        break
                    i = match.start()
                    char = chunk[i]
                    if char == "\\":
                        escaped = True
                        i += 1
                        continue
                    if char == '"':
                        in_string = False
                        count_string_scalar()
                        if kind == "string":
                            completed_at = i + 1
                            break
                    i += 1
                    continue
                match = _JSON_OUTSIDE_SPECIAL_RE.search(chunk, i)
                if match is None:
                    scan_outside_segment(chunk[i:])
                    i = len(chunk)
                    break
                if match.start() > i:
                    scan_outside_segment(chunk[i : match.start()])
                i = match.start()
                char = chunk[i]
                if char == "\ufeff":
                    raise InvalidConversationEncodingError("invalid_conversation_encoding")
                if char == '"':
                    finish_scalar_token()
                    mark_array_value()
                    if match.end() - match.start() > 1:
                        # A complete JSON string is recognized in the C regex
                        # engine.  Only strings split across chunks fall back
                        # to the incremental escape/quote state machine.
                        count_string_scalar()
                        i = match.end()
                        if kind == "string":
                            completed_at = i
                            break
                        continue
                    in_string = True
                elif char in "[{":
                    finish_scalar_token()
                    mark_array_value()
                    depth += 1
                    if depth > MAX_JSON_NESTING_DEPTH:
                        raise JsonSafetyLimitError(
                            "json_nesting_limit_exceeded",
                            limit=MAX_JSON_NESTING_DEPTH,
                        )
                    container_stack.append(
                        {"kind": "array" if char == "[" else "object"}
                    )
                elif char in "]}":
                    finish_scalar_token()
                    depth -= 1
                    if container_stack:
                        container_stack.pop()
                    if depth == 0:
                        completed_at = i + 1
                        break
                else:
                    finish_scalar_token()
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

    if phase != "done":
        raise json.JSONDecodeError("Expecting value", "", 0)


def _coalesce_json_text_chunks(chunks: Iterable[str]) -> Iterator[str]:
    """Yield bounded decoded-text windows without changing source reads."""

    parts: list[str] = []
    chars = 0
    for chunk in chunks:
        if not chunk:
            continue
        parts.append(chunk)
        chars += len(chunk)
        if chars >= JSON_HYBRID_COALESCE_CHARS:
            yield "".join(parts)
            parts = []
            chars = 0
    if parts:
        yield "".join(parts)


def _iter_json_array(chunks: Iterable[str]) -> Iterator[Any]:
    """Frame each top-level element once, then invoke the C decoder once."""

    yield from _iter_json_array_framed(_coalesce_json_text_chunks(chunks))


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
    pending: list[tuple[Path, int]] = [(base, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_SOURCE_DIRECTORY_DEPTH:
            raise ValueError("source_directory_depth_limit_exceeded")
        try:
            iterator_context = os.scandir(directory)
        except OSError as exc:
            raise ValueError("input_source_scan_failed") from exc
        try:
            with iterator_context as iterator:
                for entry in iterator:
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
                    relative = path.relative_to(base).as_posix()
                    if len(relative.encode("utf-8", errors="surrogatepass")) > MAX_SOURCE_RELATIVE_PATH_BYTES:
                        raise ValueError("source_path_length_limit_exceeded")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((path, depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        results.append((
                            relative,
                            int(info.st_size),
                            _fstat_identity(info),
                        ))
        except OSError as exc:
            raise ValueError("input_source_scan_failed") from exc
    # list_source_entries consumes this with pop(), yielding ascending paths
    # without a second complete path list.
    results.sort(key=lambda item: item[0], reverse=True)
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
