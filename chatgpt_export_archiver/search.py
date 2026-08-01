from __future__ import annotations

import re
import json
import base64
import hashlib
import hmac
import codecs
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from array import array
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .json_safety import (
    JsonSafetyLimitError,
    MAX_RAW_PREVIEW_BYTES,
    sanitize_json_value,
    validate_json_lexical_limits,
)
from .current_path import (
    effective_current_metadata,
    ensure_effective_current_views,
    resolve_effective_current_collection,
)
from .display_resolver import PlaceholderStreamClassifier, placeholder_prefix_may_match
from .parser import RAW_MESSAGE_NOT_PARSED, extract_message_content, is_generated_non_text_placeholder, normalize_display_text, recover_message_display_text
from .schema_contract import (
    DISPLAY_TEXT_RESOLVER_VERSION,
    NORMALIZATION_INDEX_FORMAT_VERSION,
    OPTIONAL_WEB_INDEX_FORMAT_VERSION,
    STABLE_OPTIONAL_ADDRESS_VERSION,
    parse_nonnegative_integer,
)
from .sqlite_errors import is_optional_search_capability_missing
from .utils import compact_json


MAX_QUERY_LENGTH = 500
DISPLAY_REVISION_SAMPLE_BYTES = 256
MAX_API_LIMIT = 100
MAX_MESSAGE_LIMIT = 300
MAX_AROUND_NODE_ROWS = 8000
HIGHLIGHT_TERM_LIMIT = 10
HIGHLIGHT_RANGE_LIMIT = 50
HIGHLIGHT_MESSAGE_SCAN_CHARS = 100_000
READER_MIN_TEXT_HYDRATION_CHARS = 4096
MAX_API_TITLE_CHARS = 4096
MAX_API_SOURCE_CHARS = 4096
SEARCH_CANDIDATE_SCAN_CHARS = 200_000
# Exact search is intentionally bounded by the same independent decoded-
# character and UTF-8 byte ceilings as one imported JSON element.
SEARCH_EXACT_VERIFY_CHARS = 32 * 1024 * 1024
SEARCH_EXACT_VERIFY_BYTES = 32 * 1024 * 1024
SEARCH_EXACT_VERIFY_MAX_OPT_IN_CHARS = 100 * 1024 * 1024
SEARCH_EXACT_VERIFY_ENV = "CHATGPT_ARCHIVE_SEARCH_EXACT_VERIFY_CHARS"
SEARCH_HIT_PREVIEW_CHARS = 8_192
SEARCH_SNIPPET_SCAN_CHARS = 16_384
SEARCH_PAGE_ESTIMATED_BYTES = 2 * 1024 * 1024
SEARCH_PAGE_RESOLVED_CHARS = 64 * 1024 * 1024
SEARCH_REQUEST_VERIFY_BYTES = 128 * 1024 * 1024
SEARCH_REQUEST_VERIFY_CHARS = 128 * 1024 * 1024
SEARCH_STREAM_CHUNK_BYTES = 64 * 1024
SEARCH_STREAM_OVERLAP_CHARS = 2048
SEARCH_CANDIDATE_LIMIT = 100_000
SEARCH_VM_PROGRESS_INTERVAL = 1_000
SEARCH_WALL_DEADLINE_SECONDS = 30.0
SEARCH_CONTINUATION_VERSION = 2
SEARCH_BUDGET_CONTRACT_VERSION = 2
MAX_SEARCH_CONTINUATION_LENGTH = 4096
MAX_SEARCH_CONTINUATION_ID_CHARS = 16 * 1024
SEARCH_CONTINUATION_SESSION_TTL_SECONDS = 15 * 60
SEARCH_CONTINUATION_SESSION_LIMIT = 128
SEARCH_RAW_EXACT_MAX_BYTES = 1024 * 1024
SEARCH_RAW_EXACT_MAX_CHARS = 800_000
SEARCH_RAW_CONTINUATION_TIERS = (5 * 1024 * 1024, 20 * 1024 * 1024)
SEARCH_ENRICHMENT_MATCH_LIMIT = 10_000
MAX_API_ROLE_CHARS = 256
MAX_API_AUTHOR_CHARS = 4096
MAX_API_CONTENT_TYPE_CHARS = 256
MAX_DISPLAY_CURSOR_LENGTH = 1024
DISPLAY_CURSOR_VERSION = 3
MAX_LEGACY_DISPLAY_OFFSET = 1_048_576
MAX_SQLITE_CURSOR_OFFSET = 9_223_372_036_854_775_807
_DISPLAY_REVISION_CACHE_LIMIT = 128
_DISPLAY_REVISION_CACHE: OrderedDict[tuple[Any, ...], str] = OrderedDict()
_DISPLAY_REVISION_CACHE_LOCK = threading.Lock()
_SEARCH_CONTINUATION_SECRET = secrets.token_bytes(32)
_SEARCH_CONTINUATION_SESSIONS: OrderedDict[
    str, tuple[float, dict[str, Any]]
] = OrderedDict()
_SEARCH_CONTINUATION_SESSIONS_LOCK = threading.Lock()


def _bounded_scalar_projection(expression: str, alias: str, limit: int) -> str:
    return (
        f"substr(CAST(COALESCE({expression}, '') AS BLOB), 1, {limit * 4 + 4}) "
        f"AS {alias}"
    )


def _conversation_api_columns(alias: str = "c") -> str:
    return ", ".join((
        f"{alias}.conversation_id",
        _bounded_scalar_projection(f"{alias}.title", "title", MAX_API_TITLE_CHARS),
        f"{alias}.create_time",
        f"{alias}.update_time",
        f"{alias}.current_node",
        _bounded_scalar_projection(f"{alias}.source_file", "source_file", MAX_API_SOURCE_CHARS),
    ))


class DisplayCursorError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SearchResourceLimitError(ValueError):
    def __init__(self, code: str = "search_candidate_exact_verify_limit") -> None:
        super().__init__(code)
        self.code = code


class SearchContinuationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _display_cursor_identity(conversation_id: str, node_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(conversation_id.encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0")
    digest.update(node_id.encode("utf-8", errors="surrogatepass"))
    return digest.hexdigest()


def _database_token_identity(conn: sqlite3.Connection) -> str:
    return str(_search_database_contract(conn)["database_identity"])


def _encode_display_cursor(
    database_identity: str,
    identity: str,
    revision: str,
    byte_offset: int,
    char_offset: int,
    *,
    source: str = "canonical",
) -> str:
    payload = json.dumps(
        [
            DISPLAY_CURSOR_VERSION,
            database_identity,
            identity,
            revision,
            source,
            byte_offset,
            char_offset,
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(_SEARCH_CONTINUATION_SECRET, b"display\0" + payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")


def _decode_display_cursor(
    database_identity_expected: str,
    value: str,
) -> tuple[str, str, str, int, int]:
    if not value or len(value) > MAX_DISPLAY_CURSOR_LENGTH:
        raise DisplayCursorError("invalid_display_cursor")
    try:
        packed = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        if (
            base64.urlsafe_b64encode(packed).rstrip(b"=").decode("ascii")
            != value
        ):
            raise ValueError("non-canonical cursor encoding")
        try:
            legacy = json.loads(packed)
        except (UnicodeDecodeError, json.JSONDecodeError):
            legacy = None
        if isinstance(legacy, list) and len(legacy) == 4:
            # Unsigned v1/v2 cursors are never accepted, but they are a
            # recognizable predecessor rather than malformed input.
            raise DisplayCursorError("display_cursor_stale")
        if len(packed) <= 32:
            raise ValueError("short cursor")
        raw, signature = packed[:-32], packed[-32:]
        expected = hmac.new(
            _SEARCH_CONTINUATION_SECRET, b"display\0" + raw, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad cursor signature")
        payload = json.loads(raw)
        if (
            not isinstance(payload, list)
            or len(payload) != 7
            or payload[0] != DISPLAY_CURSOR_VERSION
        ):
            raise ValueError("invalid cursor shape")
        _version, database_identity, identity, revision, source, byte_offset, char_offset = payload
    except DisplayCursorError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DisplayCursorError("invalid_display_cursor") from exc
    if (
        not isinstance(database_identity, str) or len(database_identity) != 64
        or
        not isinstance(identity, str) or len(identity) > 128
        or not isinstance(revision, str) or len(revision) > 256
        or source not in {"canonical"}
        or isinstance(byte_offset, bool) or not isinstance(byte_offset, int)
        or byte_offset < 0 or byte_offset > MAX_SQLITE_CURSOR_OFFSET
        or isinstance(char_offset, bool) or not isinstance(char_offset, int)
        or char_offset < 0 or char_offset > MAX_SQLITE_CURSOR_OFFSET
    ):
        raise DisplayCursorError("invalid_display_cursor")
    if database_identity != database_identity_expected:
        raise DisplayCursorError("display_cursor_stale")
    return identity, revision, source, byte_offset, char_offset


def _read_utf8_blob_chunk(blob: sqlite3.Blob, byte_offset: int, char_limit: int) -> tuple[str, int, bool, bool]:
    blob.seek(byte_offset)
    data = blob.read(min(len(blob) - byte_offset, char_limit * 4 + 4))
    try:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        decoded = decoder.decode(data, final=byte_offset + len(data) >= len(blob))
        invalid_utf8 = False
    except UnicodeDecodeError:
        decoded = data.decode("utf-8", errors="replace")
        invalid_utf8 = True
    chunk = normalize_display_text(decoded[:char_limit])
    if invalid_utf8:
        consumed = len(data)
    else:
        consumed = len(decoded[:char_limit].encode("utf-8"))
    next_byte = byte_offset + consumed
    return chunk, next_byte, next_byte < len(blob), invalid_utf8


@dataclass(frozen=True)
class ReaderBudget:
    message_display_chars: int = 65_536
    page_display_chars: int = 524_288
    page_raw_preview_chars: int = 65_536
    page_raw_resolver_chars: int = 524_288
    page_estimated_serialized_bytes: int = 2_097_152
    page_highlight_scan_chars: int = 262_144
    display_chunk_chars: int = 1_048_576


_READER_BUDGET_ENV = {
    "message_display_chars": "CHATGPT_ARCHIVE_READER_MESSAGE_TEXT_CHARS",
    "page_display_chars": "CHATGPT_ARCHIVE_READER_PAGE_TEXT_CHARS",
    "page_raw_preview_chars": "CHATGPT_ARCHIVE_READER_PAGE_RAW_PREVIEW_CHARS",
    "page_raw_resolver_chars": "CHATGPT_ARCHIVE_READER_PAGE_RAW_RESOLVER_CHARS",
    "page_estimated_serialized_bytes": "CHATGPT_ARCHIVE_READER_PAGE_ESTIMATED_BYTES",
    "page_highlight_scan_chars": "CHATGPT_ARCHIVE_READER_PAGE_HIGHLIGHT_CHARS",
    "display_chunk_chars": "CHATGPT_ARCHIVE_READER_DISPLAY_CHUNK_CHARS",
}


def reader_budget(environ: Mapping[str, str] | None = None) -> ReaderBudget:
    source = os.environ if environ is None else environ
    defaults = ReaderBudget()
    values: dict[str, int] = {}
    for field_name, env_name in _READER_BUDGET_ENV.items():
        default = int(getattr(defaults, field_name))
        raw = source.get(env_name)
        try:
            parsed = int(raw) if raw is not None else default
        except (TypeError, ValueError):
            parsed = default
        values[field_name] = parsed if 1_024 <= parsed <= 64 * 1024 * 1024 else default
    values["message_display_chars"] = min(values["message_display_chars"], values["page_display_chars"])
    values["display_chunk_chars"] = min(values["display_chunk_chars"], 1_048_576)
    values["page_display_chars"] = min(
        values["page_display_chars"], values["page_estimated_serialized_bytes"] // 8
    )
    values["page_raw_preview_chars"] = min(
        values["page_raw_preview_chars"], values["page_estimated_serialized_bytes"] // 32
    )
    values["message_display_chars"] = min(values["message_display_chars"], values["page_display_chars"])
    return ReaderBudget(**values)
_ALLOWED_ROLE_MODIFIER_VALUES = frozenset({"", "user", "assistant", "tool", "system", "developer", "tool/system", "tool_system"})
_INTERNAL_ROLE_VALUES = frozenset({"system", "developer", "tool", "tool/system"})
_API_STRING_MAX_LENGTHS = {
    "q": 500,
    "title": 200,
    "exact": 300,
    "exclude": 200,
    "source": 200,
    "suggest_q": 100,
}
NORMALIZE_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u3000": " ",
    }
)

_CAPABILITY_CACHE_LOCK = threading.RLock()
_CAPABILITY_CACHE: OrderedDict[int, tuple[sqlite3.Connection, int, dict[str, Any]]] = OrderedDict()
_CAPABILITY_CACHE_MAX = 128


@dataclass
class ParsedQuery:
    original: str
    terms: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    required_phrases: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    role: str | None = None
    title: str | None = None
    required_title: str | None = None
    scope: str = "all"
    before: float | None = None
    after: float | None = None
    path: str = "current"
    source: str | None = None
    match_mode: str = "contains"
    or_mode: bool = False
    errors: list[str] = field(default_factory=list)

    def has_search_text(self) -> bool:
        return bool(self.terms or self.phrases or self.required_phrases or self.title or self.required_title)

    def has_non_time_filters(self) -> bool:
        return bool(self.role or self.required_title or self.source)

    def has_effective_filters(self) -> bool:
        # Scope is only a search location constraint. By itself it must not turn
        # a normal conversation list into a synthetic search result.
        return bool(self.role or self.required_title or self.source or self.after is not None or self.before is not None or self.exclude)

    def has_search_context(self) -> bool:
        return self.has_search_text() or self.has_effective_filters()


def _search_database_contract(conn: sqlite3.Connection) -> dict[str, Any]:
    database_row = next(
        (row for row in conn.execute("PRAGMA database_list") if str(row[1]) == "main"),
        None,
    )
    database_path = str(database_row[2] or "") if database_row is not None else ""
    try:
        stat_result = os.stat(database_path)
        identity_source = [database_path, int(stat_result.st_dev), int(stat_result.st_ino)]
    except OSError:
        identity_source = [database_path, None, None]
    identity = hashlib.sha256(
        json.dumps(identity_source, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", errors="surrogatepass"
        )
    ).hexdigest()
    generations = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT name, generation FROM archive_generations "
            "WHERE name IN ('message', 'title', 'address', 'graph', 'query') ORDER BY name"
        )
    }
    optional_format = None
    try:
        optional_metadata = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT key, value FROM web_index_metadata "
                "WHERE key IN ('web_index_format_version', "
                "'stable_optional_address_version', "
                "'stable_optional_address_identity', "
                "'normalization_index_format_version', "
                "'display_text_resolver_version')"
            )
        }
        optional_format = optional_metadata.get("web_index_format_version")
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        optional_metadata = {}
    return {
        "database_identity": identity,
        "schema_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "optional_index_format": optional_format,
        "stable_optional_address_version": optional_metadata.get(
            "stable_optional_address_version"
        ),
        "stable_optional_address_identity": optional_metadata.get(
            "stable_optional_address_identity"
        ),
        "normalization_index_format_version": optional_metadata.get(
            "normalization_index_format_version"
        ),
        "display_text_resolver_version": optional_metadata.get(
            "display_text_resolver_version"
        ),
        "generations": generations,
    }


def _search_query_contract(
    parsed: ParsedQuery,
    *,
    conversation_id: str | None,
    order: str,
    limit: int,
    offset: int,
    count_total: bool,
) -> str:
    payload = {
        "query": {
            "original": parsed.original,
            "terms": parsed.terms,
            "phrases": parsed.phrases,
            "required_phrases": parsed.required_phrases,
            "exclude": parsed.exclude,
            "role": parsed.role,
            "title": parsed.title,
            "required_title": parsed.required_title,
            "scope": parsed.scope,
            "before": parsed.before,
            "after": parsed.after,
            "path": parsed.path,
            "source": parsed.source,
            "match_mode": parsed.match_mode,
            "or_mode": parsed.or_mode,
        },
        "conversation_id": conversation_id,
        "order": order,
        "limit": limit,
        "offset": offset,
        "count_total": bool(count_total),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8", errors="surrogatepass"
        )
    ).hexdigest()


def _encode_search_continuation(payload: Mapping[str, Any]) -> str:
    session_id = secrets.token_hex(16)
    now = time.monotonic()
    with _SEARCH_CONTINUATION_SESSIONS_LOCK:
        expired = [
            key
            for key, (expires_at, _payload) in _SEARCH_CONTINUATION_SESSIONS.items()
            if expires_at <= now
        ]
        for key in expired:
            _SEARCH_CONTINUATION_SESSIONS.pop(key, None)
        _SEARCH_CONTINUATION_SESSIONS[session_id] = (
            now + SEARCH_CONTINUATION_SESSION_TTL_SECONDS,
            dict(payload),
        )
        while len(_SEARCH_CONTINUATION_SESSIONS) > SEARCH_CONTINUATION_SESSION_LIMIT:
            _SEARCH_CONTINUATION_SESSIONS.popitem(last=False)
    raw = json.dumps(
        [SEARCH_CONTINUATION_VERSION, session_id],
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(_SEARCH_CONTINUATION_SECRET, raw, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(raw + signature).rstrip(b"=").decode("ascii")
    if len(token) > MAX_SEARCH_CONTINUATION_LENGTH:
        raise SearchResourceLimitError("search_response_resource_limit_exceeded")
    return token


def _decode_search_continuation(value: str) -> dict[str, Any]:
    if not value or len(value) > MAX_SEARCH_CONTINUATION_LENGTH:
        raise SearchContinuationError("invalid_search_continuation")
    try:
        packed = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        if len(packed) <= 32:
            raise ValueError("short token")
        raw, signature = packed[:-32], packed[-32:]
        expected = hmac.new(_SEARCH_CONTINUATION_SECRET, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        envelope = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SearchContinuationError("invalid_search_continuation") from exc
    if (
        not isinstance(envelope, list)
        or len(envelope) != 2
        or envelope[0] != SEARCH_CONTINUATION_VERSION
        or not isinstance(envelope[1], str)
        or len(envelope[1]) != 32
    ):
        raise SearchContinuationError("invalid_search_continuation")
    now = time.monotonic()
    with _SEARCH_CONTINUATION_SESSIONS_LOCK:
        session = _SEARCH_CONTINUATION_SESSIONS.get(envelope[1])
        if session is None or session[0] <= now:
            _SEARCH_CONTINUATION_SESSIONS.pop(envelope[1], None)
            raise SearchContinuationError("search_continuation_stale")
        _SEARCH_CONTINUATION_SESSIONS.move_to_end(envelope[1])
        return dict(session[1])


def normalize_search_text(value: str | None) -> str:
    """Normalize query/content for human search without changing stored archive text."""
    text = normalize_display_text(value)
    if text.isascii():
        # NFKC and the punctuation translation table are identities for
        # ASCII.  This is the dominant archive/index path and avoids two
        # additional complete-text passes without changing whitespace or
        # case-folding semantics.
        return re.sub(r"\s+", " ", text.casefold()).strip()
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(NORMALIZE_TRANSLATION).casefold()
    return re.sub(r"\s+", " ", text).strip()


def search_fragment_match(value: str | None, fragment: str | None, match_mode: str = "contains") -> int:
    """SQLite-friendly predicate for contains and conservative whole-word matching."""
    return 1 if _fragment_matches(value or "", fragment or "", match_mode) else 0


def _fragment_matches(value: str, fragment: str, match_mode: str) -> bool:
    normalized = normalize_search_text(value)
    needle = normalize_search_text(fragment)
    return _normalized_needle_matches(normalized, needle, match_mode)


def _normalized_needle_matches(normalized: str, needle: str, match_mode: str) -> bool:
    if not needle:
        return True
    token_spans = _word_token_spans(needle) if match_mode == "word" else []
    if match_mode != "word" or not token_spans:
        return needle in normalized
    start = 0
    while True:
        idx = normalized.find(needle, start)
        if idx < 0:
            return False
        if _candidate_has_word_boundaries(normalized, idx, token_spans, len(needle)):
            return True
        start = idx + 1


def _uses_word_boundaries(needle: str) -> bool:
    return bool(_word_token_spans(needle))


def _word_token_spans(needle: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in re.finditer(r"[a-z0-9_]+", needle)]


def _candidate_has_word_boundaries(normalized: str, index: int, token_spans: list[tuple[int, int]], needle_length: int) -> bool:
    before_fragment = normalized[index - 1] if index > 0 else ""
    # Punctuation-heavy fragments should not match when the whole fragment is
    # embedded in a longer ASCII token.
    if _is_word_char(before_fragment):
        return False
    after_fragment_index = index + needle_length
    after_fragment = normalized[after_fragment_index] if after_fragment_index < len(normalized) else ""
    if _is_word_char(after_fragment):
        return False
    for token_start, token_end in token_spans:
        start = index + token_start
        end = index + token_end
        before = normalized[start - 1] if start > 0 else ""
        after = normalized[end] if end < len(normalized) else ""
        if _is_word_char(before) or _is_word_char(after):
            return False
    return True


def _is_word_char(char: str) -> bool:
    return bool(char and re.fullmatch(r"[a-z0-9_]", char))


def search_exact_verify_limits() -> tuple[int, int]:
    chars = SEARCH_EXACT_VERIFY_CHARS
    byte_limit = SEARCH_EXACT_VERIFY_BYTES
    raw = os.environ.get(SEARCH_EXACT_VERIFY_ENV)
    if raw is not None and re.fullmatch(r"[1-9][0-9]{0,9}", raw):
        chars = min(SEARCH_EXACT_VERIFY_MAX_OPT_IN_CHARS, max(chars, int(raw)))
        # This is an explicit trusted-local capacity opt-in. Keep character
        # and byte accounting distinct while accepting any valid UTF-8 for the
        # opted-in character ceiling.
        byte_limit = max(byte_limit, chars * 4 + 4)
    return chars, byte_limit


def _ensure_search_functions(
    conn: sqlite3.Connection,
    parsed: ParsedQuery | None = None,
    *,
    raw_tier_index: int = 0,
) -> dict[str, Any]:
    verify_chars, verify_bytes = search_exact_verify_limits()
    raw_tiers: list[tuple[int, int]] = [
        (SEARCH_RAW_EXACT_MAX_CHARS, SEARCH_RAW_EXACT_MAX_BYTES)
    ]
    for tier in SEARCH_RAW_CONTINUATION_TIERS:
        raw_tiers.append((tier, tier))
    if verify_chars > raw_tiers[-1][0] or verify_bytes > raw_tiers[-1][1]:
        raw_tiers.append((verify_chars, verify_bytes))
    raw_tier_index = max(0, min(int(raw_tier_index), len(raw_tiers) - 1))
    raw_verify_chars, raw_verify_bytes = raw_tiers[raw_tier_index]
    try:
        temp_pages_before = int(conn.execute("PRAGMA temp.page_count").fetchone()[0])
    except sqlite3.Error:
        temp_pages_before = 0
    state: dict[str, Any] = {
        "exact_verify_limit_exceeded": False,
        "verify_chars": verify_chars,
        "verify_bytes": verify_bytes,
        "last_storage_rowid": None,
        "last_resolved_text": None,
        "request_verified_bytes": 0,
        "request_verified_chars": 0,
        "max_observed_verified_bytes_per_candidate": 0,
        "max_observed_verified_chars_per_candidate": 0,
        "request_verify_bytes_limit": SEARCH_REQUEST_VERIFY_BYTES,
        "request_verify_chars_limit": SEARCH_REQUEST_VERIFY_CHARS,
        "streamed_candidates": 0,
        "pending_rowids": set(),
        "pending_reasons": set(),
        # Long canonical rows keep only a bounded preview plus exact hit
        # coordinates.  The SQL verifier and response materializer can then
        # share one BLOB pass without retaining the full message in memory.
        "long_proxy_cache": {},
        "verified_artifacts": {},
        "candidate_count": 0,
        "resolver_calls": 0,
        "blob_reads": 0,
        "candidate_blob_bytes": 0,
        "raw_blob_bytes": 0,
        "decoded_chars": 0,
        "normalization_units": 0,
        "sqlite_vm_steps": 0,
        "temp_pages_before": temp_pages_before,
        "budget_exhausted": False,
        "budget_reason": None,
        "started_monotonic": time.monotonic(),
        "raw_tiers": raw_tiers,
        "raw_tier_index": raw_tier_index,
        "raw_verify_chars": raw_verify_chars,
        "raw_verify_bytes": raw_verify_bytes,
        "retry_pending_candidate": False,
    }
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text)

    fragments = [] if parsed is None else list(dict.fromkeys(
        parsed.required_phrases + parsed.phrases + parsed.terms + parsed.exclude
    ))
    normalized_fragments = [
        (fragment, normalize_search_text(fragment))
        for fragment in fragments
        if normalize_search_text(fragment)
    ]

    def mark_pending(storage_rowid: int, reason: str) -> None:
        state["exact_verify_limit_exceeded"] = True
        state["pending_rowids"].add(int(storage_rowid))
        state["pending_reasons"].add(reason)

    def stream_canonical_proxy(storage_rowid: int) -> tuple[str, int, bool]:
        preview_parts: list[str] = []
        preview_chars = 0
        total_chars = 0
        tail = ""
        tail_raw = ""
        tail_start_byte = 0
        found: set[str] = set()
        best_match: tuple[int, int, str, int] | None = None
        best_snippet = ""
        with conn.blobopen(
            "conversation_nodes", "content_text", storage_rowid, readonly=True
        ) as blob:
            state["blob_reads"] += 1
            size = len(blob)
            if size > verify_bytes:
                mark_pending(storage_rowid, "candidate_row_limit")
                return "", 0, False
            state["request_verified_bytes"] += size
            state["max_observed_verified_bytes_per_candidate"] = max(
                state["max_observed_verified_bytes_per_candidate"], size
            )
            if state["request_verified_bytes"] > SEARCH_REQUEST_VERIFY_BYTES:
                mark_pending(storage_rowid, "request_aggregate_limit")
                return "", 0, False
            if size <= SEARCH_CANDIDATE_SCAN_CHARS:
                raw_value = blob.read()
                state["candidate_blob_bytes"] += len(raw_value)
                value = normalize_display_text(raw_value.decode("utf-8", errors="replace"))
                state["decoded_chars"] += len(value)
                state["normalization_units"] += len(value)
                state["request_verified_chars"] += len(value)
                state["max_observed_verified_chars_per_candidate"] = max(
                    state["max_observed_verified_chars_per_candidate"], len(value)
                )
                if state["request_verified_chars"] > SEARCH_REQUEST_VERIFY_CHARS:
                    mark_pending(storage_rowid, "request_aggregate_limit")
                    return "", len(value), False
                return value, len(value), is_generated_non_text_placeholder(value)
            state["streamed_candidates"] += 1
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            input_byte_offset = 0
            placeholder_classifier = PlaceholderStreamClassifier()
            while True:
                data = blob.read(SEARCH_STREAM_CHUNK_BYTES)
                if not data:
                    break
                state["candidate_blob_bytes"] += len(data)
                buffered_before = len(decoder.getstate()[0])
                decoded_start_byte = input_byte_offset - buffered_before
                decoded_text = decoder.decode(data, final=False)
                visible = normalize_display_text(decoded_text)
                input_byte_offset += len(data)
                placeholder_classifier.feed(visible)
                total_chars += len(visible)
                state["decoded_chars"] += len(visible)
                state["normalization_units"] += len(visible)
                if total_chars > verify_chars:
                    mark_pending(storage_rowid, "candidate_row_limit")
                    return "", total_chars, False
                if preview_chars < SEARCH_HIT_PREVIEW_CHARS + 1:
                    part = visible[: SEARCH_HIT_PREVIEW_CHARS + 1 - preview_chars]
                    preview_parts.append(part)
                    preview_chars += len(part)
                scan = tail + visible
                scan_raw = tail_raw + decoded_text
                scan_start_byte = tail_start_byte if tail else decoded_start_byte
                normalized_scan = normalize_search_text(scan)
                newly_found = False
                for original, normalized in normalized_fragments:
                    if normalized not in found and _normalized_needle_matches(
                        normalized_scan,
                        normalized,
                        parsed.match_mode if parsed is not None else "contains",
                    ):
                        found.add(normalized)
                        newly_found = True
                source_span = (
                    _first_source_match_span(
                        scan,
                        [(fragment, parsed.match_mode if parsed is not None else "contains")
                         for fragment, _normalized in normalized_fragments],
                    )
                    if best_match is None and newly_found
                    else None
                )
                if source_span is not None:
                    base_offset = max(0, total_chars - len(visible) - len(tail))
                    candidate = (
                        base_offset + source_span[0],
                        base_offset + source_span[1],
                        source_span[2],
                        scan_start_byte
                        + len(scan_raw[:source_span[0]].encode("utf-8", errors="replace")),
                    )
                    if best_match is None or (candidate[0], -(candidate[1] - candidate[0])) < (
                        best_match[0], -(best_match[1] - best_match[0])
                    ):
                        best_match = candidate
                        best_snippet, _unused = _make_snippet_with_position(
                            scan,
                            [(fragment, parsed.match_mode if parsed is not None else "contains")
                             for fragment, _normalized in normalized_fragments],
                            parsed.match_mode if parsed is not None else "contains",
                            scan_chars=len(scan),
                        )
                tail_cut = max(0, len(scan) - SEARCH_STREAM_OVERLAP_CHARS)
                tail_start_byte = scan_start_byte + len(
                    scan_raw[:tail_cut].encode("utf-8", errors="replace")
                )
                tail = scan[tail_cut:]
                tail_raw = scan_raw[tail_cut:]
            final_decoded = decoder.decode(b"", final=True)
            final_visible = normalize_display_text(final_decoded)
            if final_visible:
                placeholder_classifier.feed(final_visible)
                if preview_chars < SEARCH_HIT_PREVIEW_CHARS + 1:
                    part = final_visible[: SEARCH_HIT_PREVIEW_CHARS + 1 - preview_chars]
                    preview_parts.append(part)
                    preview_chars += len(part)
                total_chars += len(final_visible)
                scan = tail + final_visible
                scan_raw = tail_raw + final_decoded
                scan_start_byte = tail_start_byte if tail else input_byte_offset
                normalized_scan = normalize_search_text(scan)
                newly_found = False
                for original, normalized in normalized_fragments:
                    if normalized not in found and _normalized_needle_matches(
                        normalized_scan,
                        normalized,
                        parsed.match_mode if parsed is not None else "contains",
                    ):
                        found.add(normalized)
                        newly_found = True
                source_span = (
                    _first_source_match_span(
                        scan,
                        [(fragment, parsed.match_mode if parsed is not None else "contains")
                         for fragment, _normalized in normalized_fragments],
                    )
                    if best_match is None and newly_found
                    else None
                )
                if source_span is not None:
                    base_offset = max(0, total_chars - len(final_visible) - len(tail))
                    candidate = (
                        base_offset + source_span[0],
                        base_offset + source_span[1],
                        source_span[2],
                        scan_start_byte
                        + len(scan_raw[:source_span[0]].encode("utf-8", errors="replace")),
                    )
                    if best_match is None or (candidate[0], -(candidate[1] - candidate[0])) < (
                        best_match[0], -(best_match[1] - best_match[0])
                    ):
                        best_match = candidate
                        best_snippet, _unused = _make_snippet_with_position(
                            scan,
                            [(fragment, parsed.match_mode if parsed is not None else "contains")
                             for fragment, _normalized in normalized_fragments],
                            parsed.match_mode if parsed is not None else "contains",
                            scan_chars=len(scan),
                        )
        state["request_verified_chars"] += total_chars
        state["max_observed_verified_chars_per_candidate"] = max(
            state["max_observed_verified_chars_per_candidate"], total_chars
        )
        if state["request_verified_chars"] > SEARCH_REQUEST_VERIFY_CHARS:
            mark_pending(storage_rowid, "request_aggregate_limit")
            return "", total_chars, False
        preview = "".join(preview_parts)
        # The proxy is bounded. Found normalized fragments preserve the exact
        # SQL predicates, while the leading prefix remains the returned preview.
        placeholder_exact = placeholder_classifier.exact_placeholder
        proxy = preview + " " + " ".join(sorted(found))
        if not placeholder_exact:
            state["verified_artifacts"][storage_rowid] = {
                "preview": preview[:SEARCH_HIT_PREVIEW_CHARS],
                "total_chars": total_chars,
                "snippet": best_snippet,
                "match_char_offset": best_match[0] if best_match else None,
                "match_length": (best_match[1] - best_match[0]) if best_match else None,
                "matched_term": best_match[2] if best_match else None,
                "source_byte_offset": best_match[3] if best_match else None,
                "source_kind": "canonical",
            }
        return proxy, total_chars, placeholder_exact

    def bounded_display_from_storage(
        storage_rowid: int, content_is_null: int, raw_is_null: int, content_type: str | None
    ) -> str:
        storage_rowid = int(storage_rowid)
        if state["last_storage_rowid"] == storage_rowid:
            return str(state["last_resolved_text"] or "")
        cached_proxy = state["long_proxy_cache"].get(storage_rowid)
        if cached_proxy is not None:
            state["last_storage_rowid"] = storage_rowid
            state["last_resolved_text"] = cached_proxy
            return str(cached_proxy)
        state["resolver_calls"] += 1
        content = ""
        total_chars = 0
        possible_placeholder = False
        if not content_is_null:
            content, total_chars, possible_placeholder = stream_canonical_proxy(storage_rowid)
            if storage_rowid in state["pending_rowids"]:
                state["last_storage_rowid"] = storage_rowid
                state["last_resolved_text"] = ""
                return ""
        visible_content = normalize_display_text(content)
        # Legacy rows can carry a generated placeholder even when their stored
        # content_type says "text".  The placeholder grammar, rather than the
        # advisory type column, decides whether bounded raw recovery is needed.
        if visible_content and not possible_placeholder:
            if total_chars > SEARCH_CANDIDATE_SCAN_CHARS:
                state["long_proxy_cache"][storage_rowid] = visible_content
            state["last_storage_rowid"] = storage_rowid
            state["last_resolved_text"] = visible_content
            return visible_content
        raw = ""
        if not raw_is_null:
            with conn.blobopen(
                "conversation_nodes", "raw_message_json", storage_rowid, readonly=True
            ) as blob:
                state["blob_reads"] += 1
                raw_size = len(blob)
                if raw_size > min(verify_bytes, raw_verify_bytes):
                    mark_pending(storage_rowid, "raw_fallback_limit")
                else:
                    state["request_verified_bytes"] += raw_size
                    state["max_observed_verified_bytes_per_candidate"] = max(
                        state["max_observed_verified_bytes_per_candidate"], raw_size
                    )
                    if state["request_verified_bytes"] > SEARCH_REQUEST_VERIFY_BYTES:
                        mark_pending(storage_rowid, "request_aggregate_limit")
                    else:
                        raw, _next, more, _invalid = _read_utf8_blob_chunk(
                            blob, 0, min(verify_chars, raw_verify_chars) + 1
                        )
                        state["raw_blob_bytes"] += min(
                            raw_size,
                            (min(verify_chars, raw_verify_chars) + 1) * 4 + 4,
                        )
                        state["decoded_chars"] += len(raw)
                        state["normalization_units"] += len(raw)
                        state["request_verified_chars"] += len(raw)
                        state["max_observed_verified_chars_per_candidate"] = max(
                            state["max_observed_verified_chars_per_candidate"], len(raw)
                        )
                        if (
                            more
                            or len(raw) > min(verify_chars, raw_verify_chars)
                            or state["request_verified_chars"] > SEARCH_REQUEST_VERIFY_CHARS
                        ):
                            mark_pending(
                                storage_rowid,
                                "request_aggregate_limit"
                                if state["request_verified_chars"] > SEARCH_REQUEST_VERIFY_CHARS
                                else "raw_fallback_limit",
                            )
                            raw = ""
        resolved = recover_message_display_text(
            "[non-text content: indexed-placeholder]" if possible_placeholder else content,
            raw,
            max_raw_chars=min(verify_chars, raw_verify_chars),
        )
        if total_chars > SEARCH_CANDIDATE_SCAN_CHARS:
            state["long_proxy_cache"][storage_rowid] = resolved
            if possible_placeholder:
                terms = _highlight_terms(parsed) if parsed is not None else []
                source_span = _first_source_match_span(resolved, terms)
                snippet, _snippet_offset = _make_snippet_with_position(
                    resolved,
                    terms,
                    parsed.match_mode if parsed is not None else "contains",
                    scan_chars=len(resolved),
                )
                state["verified_artifacts"][storage_rowid] = {
                    "preview": resolved[:SEARCH_HIT_PREVIEW_CHARS],
                    "total_chars": len(resolved),
                    "snippet": snippet,
                    "match_char_offset": source_span[0] if source_span else None,
                    "match_length": (
                        source_span[1] - source_span[0] if source_span else None
                    ),
                    "matched_term": source_span[2] if source_span else None,
                    "source_byte_offset": (
                        _utf8_prefix_bytes(resolved, source_span[0])
                        if source_span
                        else None
                    ),
                    "source_kind": "raw_fallback",
                }
        state["last_storage_rowid"] = storage_rowid
        state["last_resolved_text"] = resolved
        return resolved

    # The rowid-based resolver reads a fixed BLOB prefix.  It preserves NUL
    # bytes while avoiding full TEXT projection or CAST for oversized rows.
    conn.create_function("web_search_display", 4, bounded_display_from_storage)
    return state


def _sql_display_text(alias: str = "n") -> str:
    return _sql_search_display_text(alias)


def _sql_search_display_text(alias: str = "n") -> str:
    return (
        f"web_search_display({alias}.rowid, "
        f"{alias}.content_text IS NULL, {alias}.raw_message_json IS NULL, {alias}.content_type)"
    )


def parse_query(
    raw: str | None,
    *,
    path_default: str = "current",
    role: str | None = None,
    title: str | None = None,
    scope: str = "all",
    exact: str | None = None,
    exclude: str | None = None,
    after: str | None = None,
    before: str | None = None,
    source: str | None = None,
    match_mode: str = "contains",
    enforce_api_limits: bool = False,
) -> ParsedQuery:
    text = normalize_search_text(raw).strip()
    parsed = ParsedQuery(
        original=text,
        path=path_default if path_default in {"current", "all"} else "current",
        scope=scope if scope in {"all", "title", "message"} else "all",
        match_mode=match_mode if match_mode in {"contains", "word"} else "contains",
    )
    if len(text) > MAX_QUERY_LENGTH:
        text = text[:MAX_QUERY_LENGTH]
        parsed.original = text
        if enforce_api_limits:
            parsed.errors.append("q_too_long")
    if role:
        parsed.role = _canonical_role(role)
    if title:
        if enforce_api_limits and len(title) > _API_STRING_MAX_LENGTHS["title"]:
            parsed.errors.append("title_too_long")
        else:
            parsed.required_title = normalize_search_text(title)
    if exact:
        if enforce_api_limits and len(exact or "") > _API_STRING_MAX_LENGTHS["exact"]:
            parsed.errors.append("exact_too_long")
        else:
            parsed.required_phrases.append(normalize_search_text(exact))
    if exclude:
        if enforce_api_limits and len(exclude or "") > _API_STRING_MAX_LENGTHS["exclude"]:
            parsed.errors.append("exclude_too_long")
        else:
            parsed.exclude.extend(item for item in _split_filter_fragments(exclude) if item)
    if source:
        if enforce_api_limits and len(source or "") > _API_STRING_MAX_LENGTHS["source"]:
            parsed.errors.append("source_too_long")
        else:
            parsed.source = normalize_search_text(source)
    if after:
        parsed.after = _parse_date(after)
        if parsed.after is None:
            parsed.errors.append("invalid_after")
    if before:
        before_ts = _parse_date(before)
        if before_ts is None:
            parsed.errors.append("invalid_before")
        else:
            parsed.before = _date_end_exclusive(before_ts)
    for token, quoted, negated_quote, key in _query_tokens(text):
        if not token:
            continue
        if negated_quote:
            if key is not None:
                parsed.errors.append(f"negated_modifier_not_supported:{key}")
                continue
            parsed.exclude.append(normalize_search_text(token))
            continue
        if key is None and token == "or" and not quoted:
            parsed.or_mode = True
            continue
        if key is None and not quoted and token.startswith("-") and not token.startswith("--") and len(token) > 1:
            parsed.exclude.append(normalize_search_text(token[1:]))
            continue
        if key:
            value = normalize_search_text(token)
            if key == "role":
                if value and value in _ALLOWED_ROLE_MODIFIER_VALUES:
                    parsed.role = _canonical_role(value)
                elif value:
                    parsed.errors.append(f"invalid_role:{value}")
                continue
            if key == "title" and value:
                parsed.title = normalize_search_text(value)
                continue
            if key == "source" and value:
                parsed.source = normalize_search_text(value)
                continue
            if key == "path":
                if value in {"current", "all"}:
                    parsed.path = value
                elif value:
                    parsed.errors.append(f"invalid_path:{value}")
                continue
            if key == "scope":
                if value in {"all", "title", "message"}:
                    parsed.scope = value
                elif value:
                    parsed.errors.append(f"invalid_scope:{value}")
                continue
            if key in {"before", "after"}:
                ts = _parse_date(value)
                if ts is None:
                    parsed.errors.append(f"invalid_{key}")
                elif key == "before":
                    parsed.before = _date_end_exclusive(ts)
                else:
                    parsed.after = ts
                continue
        if quoted:
            parsed.phrases.append(normalize_search_text(token))
        else:
            parsed.terms.append(normalize_search_text(token))
    return parsed


def _split_words(text: str) -> list[str]:
    return [part for part in re.split(r"\s+", text.strip()) if part]


def _canonical_role(role: str | None) -> str:
    return (role or "").casefold().replace("_", "/")


def _role_filter_values(role: str | None) -> list[str]:
    canonical = _canonical_role(role)
    if canonical == "tool/system":
        return ["tool", "system", "tool/system"]
    return [canonical] if canonical else []


def _sql_canonical_role(alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return f"replace(lower(COALESCE({prefix}role, '')), '_', '/')"


def _sql_internal_role_condition(alias: str | None = None) -> str:
    return f"{_sql_canonical_role(alias)} IN ('system', 'developer', 'tool', 'tool/system')"


def _current_path_condition(alias: str = "n") -> str:
    """Match the shared effective reader collection for path=current."""
    return (
        "EXISTS (SELECT 1 FROM effective_current_nodes effective_current "
        f"WHERE effective_current.conversation_id = {alias}.conversation_id "
        f"AND effective_current.node_id = {alias}.node_id)"
    )


def _sql_empty_mapping_condition(alias: str = "n") -> str:
    return (
        f"{alias}.message_id IS NULL "
        f"AND COALESCE({alias}.content_text, '') = '' "
        f"AND COALESCE({alias}.raw_message_json, '') = ''"
    )


def _sql_internal_content_condition(alias: str = "n") -> str:
    return (
        f"{_sql_internal_role_condition(alias)} "
        f"OR lower(COALESCE({alias}.content_type, '')) IN ("
        "'user_editable_context',"
        "'model_editable_context',"
        "'system_context',"
        "'developer_context',"
        "'thoughts'"
        ") "
        f"OR lower(trim(COALESCE({alias}.content_text, ''))) LIKE 'source analysis msg id:%'"
    )


def _sql_visible_message_condition(alias: str = "n") -> str:
    return f"NOT ({_sql_empty_mapping_condition(alias)}) AND NOT ({_sql_internal_content_condition(alias)})"


def _current_path_fallback_to_all_from_counts(counts: dict[str, int] | dict[str, Any] | None) -> bool:
    if not counts:
        return False
    if "current_path_fallback_to_all" in counts:
        return bool(counts["current_path_fallback_to_all"])
    return int(counts.get("node_count") or 0) > 0 and int(counts.get("current_path_nodes") or 0) == 0


def _fallback_map_for_conversations(conn: sqlite3.Connection, conversation_ids: list[str]) -> dict[str, bool]:
    metadata = effective_current_metadata(conn, conversation_ids)
    return {cid: bool(value.get("current_path_fallback_to_all")) for cid, value in metadata.items()}


def _effective_pairs_for_rows(
    conn: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    conversation_ids = sorted({str(row["conversation_id"]) for row in rows})
    if not conversation_ids:
        return set()
    ensure_effective_current_views(conn, conversation_ids)
    placeholders = ",".join("?" for _ in conversation_ids)
    effective = conn.execute(
        f"""SELECT conversation_id, node_id
            FROM effective_current_nodes
            WHERE conversation_id IN ({placeholders})""",
        conversation_ids,
    ).fetchall()
    wanted = {(str(row["conversation_id"]), str(row["node_id"])) for row in rows}
    return {
        (str(row["conversation_id"]), str(row["node_id"]))
        for row in effective
        if (str(row["conversation_id"]), str(row["node_id"])) in wanted
    }


def _effective_visible_in_current_view(is_on_current_path: bool, current_path_fallback_to_all: bool) -> bool:
    return bool(is_on_current_path or current_path_fallback_to_all)


def _split_filter_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    for match in re.finditer(r'"((?:\\.|[^"\\])*)"|(\S+)', text or ""):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if raw is None:
            continue
        value = _unescape_filter_fragment(raw) if match.group(1) is not None else raw
        normalized = normalize_search_text(value)
        if normalized:
            fragments.append(normalized)
    return fragments


def _unescape_filter_fragment(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _parse_date(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _date_end_exclusive(start_ts: float) -> float:
    return start_ts + timedelta(days=1).total_seconds()


_KNOWN_MODIFIER_KEYS = frozenset({"role", "title", "source", "path", "scope", "before", "after"})


def _query_tokens(text: str) -> list[tuple[str, bool, bool, str | None]]:
    tokens: list[tuple[str, bool, bool, str | None]] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        negated = False
        if text[index] == "-" and index + 1 < length and text[index + 1] != "-" and not text[index + 1].isspace():
            negated = True
            index += 1
        if index < length and text[index] == '"':
            value, index = _read_quoted_token(text, index + 1)
            tokens.append((value, True, negated, None))
            continue
        start = index
        while index < length and not text[index].isspace() and text[index] not in {":", '"'}:
            index += 1
        head = text[start:index]
        if index < length and text[index] == ":" and head:
            raw_key = normalize_search_text(head)
            if raw_key in _KNOWN_MODIFIER_KEYS:
                index += 1
                if index < length and text[index] == '"':
                    value, index = _read_quoted_token(text, index + 1)
                    tokens.append((value, True, negated, raw_key))
                else:
                    value_start = index
                    while index < length and not text[index].isspace():
                        index += 1
                    tokens.append((text[value_start:index], False, negated, raw_key))
                continue
        if index < length and text[index] == ":" and head:
            index += 1
            if index < length and text[index] == '"':
                value, index = _read_quoted_token(text, index + 1)
                token_text = f"{head}:{value}"
            else:
                value_start = index
                while index < length and not text[index].isspace():
                    index += 1
                token_text = f"{head}:{text[value_start:index]}"
            if negated:
                token_text = "-" + token_text
        elif index < length and text[index] == '"':
            start_quote = start
            index += 1
            while index < length and text[index] != '"':
                index += 1
            if index < length:
                index += 1
                token_text = text[start_quote:index]
            else:
                token_text = text[start_quote:index]
            if negated and not token_text.startswith("-"):
                token_text = "-" + token_text
        else:
            if negated and not head.startswith("-"):
                token_text = "-" + head
            else:
                token_text = head
        tokens.append((token_text, False, False, None))
    return tokens


def _read_quoted_token(text: str, index: int) -> tuple[str, int]:
    chars: list[str] = []
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            chars.append(text[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    return "".join(chars), index


def _fts_token(value: str, match_mode: str = "contains") -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_]{2,64}", value):
        return value if match_mode == "word" else f"{value}*"
    if value and not any(ch in value for ch in '"\n\r\t'):
        return '"' + value.replace('"', '""') + '"'
    return None


def build_fts_query(parsed: ParsedQuery) -> str | None:
    pieces: list[str] = []
    for phrase in parsed.phrases + parsed.required_phrases:
        if phrase:
            pieces.append('"' + phrase.replace('"', '""') + '"')
    for term in parsed.terms:
        token = _fts_token(term, parsed.match_mode)
        if token:
            pieces.append(token)
    if not pieces:
        return None
    joiner = " OR " if parsed.or_mode else " AND "
    query = joiner.join(pieces)
    for term in parsed.exclude:
        token = _fts_token(term, parsed.match_mode)
        if token:
            query += f" NOT {token}"
    return query


def list_conversations(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
    sort: str,
    after: float | None = None,
    before: float | None = None,
    selected_id: str | None = None,
) -> dict[str, Any]:
    limit = _bounded_limit(limit, MAX_API_LIMIT)
    offset = max(0, offset)
    where, params = _conversation_time_where(after, before)
    order = {
        "created": "COALESCE(c.create_time, c.update_time, 0) DESC, c.conversation_id ASC",
        "updated": "COALESCE(c.update_time, c.create_time, 0) DESC, c.conversation_id ASC",
        "oldest": "COALESCE(c.create_time, c.update_time, 0) ASC, c.conversation_id ASC",
        "title": "LOWER(COALESCE(c.title, '')) ASC, c.conversation_id ASC",
    }.get(sort, "COALESCE(c.update_time, c.create_time, 0) DESC, c.conversation_id ASC")
    rows = conn.execute(
        f"""
        SELECT {_conversation_api_columns('c')}
        FROM conversations c
        {where}
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    selected_in_results = _selected_in_conversation_filter(conn, where, params, selected_id)
    page_ids = [str(row["conversation_id"]) for row in rows]
    selected_row = None
    if selected_id and selected_in_results and selected_id not in page_ids:
        selected_where = f"{where} {'AND' if where else 'WHERE'} c.conversation_id = ?"
        selected_row = conn.execute(
            f"""SELECT {_conversation_api_columns('c')}
                FROM conversations c
                {selected_where}
                LIMIT 1""",
            params + [selected_id],
        ).fetchone()
    scope_ids = page_ids + ([selected_id] if selected_row is not None and selected_id else [])
    counts = _node_counts_for_conversations(conn, scope_ids)
    total = conn.execute(f"SELECT COUNT(*) AS c FROM conversations c {where}", params).fetchone()["c"]
    return _page_payload(
        [_conversation_summary_with_counts(row, counts.get(row["conversation_id"], {})) for row in rows],
        total,
        limit,
        offset,
        extra={
            "order_exact": True,
            "scan_complete": True,
            "provisional_order": False,
        },
        selected_in_results=selected_in_results,
        selected_item=(
            _conversation_summary_with_counts(selected_row, counts.get(selected_id, {}))
            if selected_row is not None and selected_id is not None
            else None
        ),
    )


def search_messages(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    *,
    limit: int = 50,
    offset: int = 0,
    conversation_id: str | None = None,
    order: str = "relevance",
    max_page_limit: int = MAX_API_LIMIT,
    count_total: bool = True,
    continuation: str | None = None,
) -> dict[str, Any]:
    search_state = _ensure_search_functions(conn, parsed)
    limit = _bounded_limit(limit, max_page_limit)
    offset = max(0, offset)
    if parsed.scope == "title":
        return _page_payload([], 0, limit, offset, extra={"total_exact": True})
    has_message_text = bool(parsed.phrases or parsed.terms or parsed.required_phrases)
    if not has_message_text:
        return _page_payload([], 0, limit, offset, extra={"total_exact": True})
    query_contract = _search_query_contract(
        parsed,
        conversation_id=conversation_id,
        order=order,
        limit=limit,
        offset=offset,
        count_total=count_total,
    )
    database_contract = _search_database_contract(conn)
    resume_after_conversation_id = ""
    resume_after_node_id = ""
    confirmed_hits_before = 0
    pending_candidates_before = 0
    pending_reasons_before: set[str] = set()
    raw_tier_index = 0
    retry_pending_candidate = False
    resume_candidate_offset = 0
    if continuation:
        continuation_payload = _decode_search_continuation(continuation)
        if (
            continuation_payload.get("query_contract") != query_contract
            or continuation_payload.get("database_contract") != database_contract
            or continuation_payload.get("budget_contract_version")
            != SEARCH_BUDGET_CONTRACT_VERSION
        ):
            raise SearchContinuationError("search_continuation_stale")
        resume_conversation_value = continuation_payload.get(
            "candidate_conversation_cursor", ""
        )
        resume_node_value = continuation_payload.get("candidate_node_cursor", "")
        if (
            not isinstance(resume_conversation_value, str)
            or len(resume_conversation_value) > MAX_SEARCH_CONTINUATION_ID_CHARS
            or not isinstance(resume_node_value, str)
            or len(resume_node_value) > MAX_SEARCH_CONTINUATION_ID_CHARS
        ):
            raise SearchContinuationError("invalid_search_continuation")
        resume_after_conversation_id = resume_conversation_value
        resume_after_node_id = resume_node_value
        confirmed_value = continuation_payload.get("confirmed_hits_before", 0)
        if (
            isinstance(confirmed_value, bool)
            or not isinstance(confirmed_value, int)
            or confirmed_value < 0
        ):
            raise SearchContinuationError("invalid_search_continuation")
        confirmed_hits_before = confirmed_value
        pending_value = continuation_payload.get("pending_candidates_before", 0)
        pending_reason_values = continuation_payload.get("pending_reasons_before", [])
        if (
            isinstance(pending_value, bool)
            or not isinstance(pending_value, int)
            or pending_value < 0
            or not isinstance(pending_reason_values, list)
            or any(not isinstance(item, str) or len(item) > 64 for item in pending_reason_values)
        ):
            raise SearchContinuationError("invalid_search_continuation")
        pending_candidates_before = pending_value
        pending_reasons_before = set(pending_reason_values)
        raw_tier_value = continuation_payload.get("raw_tier_index", 0)
        retry_pending_value = continuation_payload.get(
            "retry_pending_candidate", False
        )
        if (
            isinstance(raw_tier_value, bool)
            or not isinstance(raw_tier_value, int)
            or raw_tier_value < 0
            or not isinstance(retry_pending_value, bool)
        ):
            raise SearchContinuationError("invalid_search_continuation")
        raw_tier_index = raw_tier_value
        retry_pending_candidate = retry_pending_value
        candidate_offset_value = continuation_payload.get("candidate_offset", 0)
        if (
            isinstance(candidate_offset_value, bool)
            or not isinstance(candidate_offset_value, int)
            or candidate_offset_value < 0
            or candidate_offset_value > MAX_SQLITE_CURSOR_OFFSET
        ):
            raise SearchContinuationError("invalid_search_continuation")
        resume_candidate_offset = candidate_offset_value
        if retry_pending_candidate:
            pending_candidates_before = max(0, pending_candidates_before - 1)
        search_state = _ensure_search_functions(
            conn, parsed, raw_tier_index=raw_tier_index
        )
    _prepare_verified_message_table(conn)
    effective_page_offset = 0 if continuation else offset
    max_verified_results = (
        SEARCH_CANDIDATE_LIMIT + 1
        if count_total
        else effective_page_offset + limit
    )
    scan_parsed = parsed
    if parsed.path == "current":
        if conversation_id:
            ensure_effective_current_views(conn, [conversation_id])
        else:
            # Resolve the path-independent exact candidate scope once, then
            # materialize effective-current only for conversations that can
            # actually contribute a hit. The verified TEMP artifact is reused
            # below instead of resolving those rows a second time.
            scan_parsed = replace(parsed, path="all")
    try:
        last_candidate_node_id, candidate_budget_exhausted, last_candidate_conversation_id, next_candidate_offset = _scan_verified_message_candidates(
            conn,
            scan_parsed,
            conversation_id,
            search_state,
            use_trigram=True,
            resume_after_conversation_id=resume_after_conversation_id,
            resume_after_node_id=resume_after_node_id,
            max_verified_results=max_verified_results,
            display_order=bool(order == "display" and conversation_id),
            resume_candidate_offset=resume_candidate_offset,
        )
        diagnostics = _message_search_diagnostics(conn, parsed, used_trigram=True)
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        _prepare_verified_message_table(conn)
        search_state = _ensure_search_functions(
            conn, parsed, raw_tier_index=raw_tier_index
        )
        last_candidate_node_id, candidate_budget_exhausted, last_candidate_conversation_id, next_candidate_offset = _scan_verified_message_candidates(
            conn,
            scan_parsed,
            conversation_id,
            search_state,
            use_trigram=False,
            resume_after_conversation_id=resume_after_conversation_id,
            resume_after_node_id=resume_after_node_id,
            max_verified_results=max_verified_results,
            display_order=bool(order == "display" and conversation_id),
            resume_candidate_offset=resume_candidate_offset,
        )
        diagnostics = _message_search_diagnostics(conn, parsed, used_trigram=False)
    if parsed.path == "current" and not conversation_id:
        candidate_conversation_ids = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT DISTINCT n.conversation_id
                FROM temp.web_verified_message_results verified
                JOIN conversation_nodes n ON n.rowid = verified.storage_rowid
                """
            )
        ]
        ensure_effective_current_views(conn, candidate_conversation_ids)
        conn.execute(
            """
            DELETE FROM temp.web_verified_message_results
            WHERE NOT EXISTS (
                SELECT 1
                FROM conversation_nodes n
                JOIN effective_current_nodes effective
                  ON effective.conversation_id = n.conversation_id
                 AND effective.node_id = n.node_id
                WHERE n.rowid = web_verified_message_results.storage_rowid
            )
            """
        )
    rows, total = _verified_message_page_rows(
        conn,
        parsed,
        conversation_id,
        limit,
        effective_page_offset,
        order,
        count_total=count_total,
    )
    pending_count = pending_candidates_before + len(search_state["pending_rowids"])
    cumulative_pending_reasons = pending_reasons_before | set(
        search_state["pending_reasons"]
    )
    result_ids = [str(row["conversation_id"]) for row in rows]
    fallback_map = _fallback_map_for_conversations(conn, result_ids) if rows else {}
    effective_pairs = _effective_pairs_for_rows(conn, rows) if rows else set()
    items = [
        _message_search_payload(
            conn,
            row,
            parsed,
            row["match_reason"] or ("exact phrase" if (parsed.phrases or parsed.required_phrases) else "substring"),
            row["bm25_score"],
            current_path_fallback_to_all=fallback_map.get(row["conversation_id"], False),
            effective_visible_in_current_view=(str(row["conversation_id"]), str(row["node_id"])) in effective_pairs,
            verified_artifact=search_state["verified_artifacts"].get(
                int(row["storage_rowid"])
            ),
        )
        for row in rows
    ]
    partial = pending_count > 0 or candidate_budget_exhausted
    segment_total = int(
        conn.execute("SELECT COUNT(*) FROM temp.web_verified_message_results").fetchone()[0]
    )
    cumulative_total = confirmed_hits_before + segment_total
    # Message searches always state whether the discovered total is exact.
    # A completed candidate-cursor scan proves the cumulative total even when
    # the caller skipped the eager COUNT-style path; only a partial segment is
    # approximate.
    total_exact = not partial
    result = _page_payload(
        items,
        cumulative_total,
        limit,
        effective_page_offset,
        extra={"total_exact": total_exact},
    )
    estimated_response_bytes = sum(
        len(str(item.get("display_text") or "").encode("utf-8"))
        + len(str(item.get("snippet") or "").encode("utf-8"))
        + 768
        for item in items
    )
    retry_pending = bool(search_state["retry_pending_candidate"])
    next_raw_tier_index = int(search_state["raw_tier_index"])
    retry_reason = str(search_state["budget_reason"] or "")
    if retry_pending and retry_reason == "raw_fallback_limit":
        next_raw_tier_index += 1
    raw_tier_available = next_raw_tier_index < len(search_state["raw_tiers"])
    retry_available = retry_pending and (
        retry_reason == "request_aggregate_limit"
        or (retry_reason == "raw_fallback_limit" and raw_tier_available)
    )
    continuation_token = (
        _encode_search_continuation(
            {
                "version": SEARCH_CONTINUATION_VERSION,
                "budget_contract_version": SEARCH_BUDGET_CONTRACT_VERSION,
                "database_contract": database_contract,
                "query_contract": query_contract,
                "candidate_conversation_cursor": last_candidate_conversation_id,
                "candidate_node_cursor": last_candidate_node_id,
                "candidate_offset": next_candidate_offset,
                "confirmed_hits_before": cumulative_total,
                "pending_candidates_before": pending_count,
                "pending_reasons_before": sorted(cumulative_pending_reasons),
                "raw_tier_index": (
                    next_raw_tier_index
                    if raw_tier_available
                    else int(search_state["raw_tier_index"])
                ),
                "retry_pending_candidate": retry_available,
            }
        )
        if candidate_budget_exhausted
        and (
            retry_available
            or (
                not retry_pending
                and (
                    next_candidate_offset > resume_candidate_offset
                    if order == "display" and conversation_id
                    else (
                        last_candidate_conversation_id > resume_after_conversation_id
                        or (
                            last_candidate_conversation_id == resume_after_conversation_id
                            and last_candidate_node_id > resume_after_node_id
                        )
                    )
                )
            )
        )
        else None
    )
    diagnostics.update(
        {
            "resource_contract": "streamed_message_search_v2",
            "configured_candidate_scan_char_limit": SEARCH_CANDIDATE_SCAN_CHARS,
            "configured_verified_char_limit_per_candidate": int(search_state["verify_chars"]),
            "configured_verified_byte_limit_per_candidate": int(search_state["verify_bytes"]),
            "candidate_scan_chars_per_row": SEARCH_CANDIDATE_SCAN_CHARS,
            "hit_preview_chars": SEARCH_HIT_PREVIEW_CHARS,
            "snippet_scan_chars": SEARCH_SNIPPET_SCAN_CHARS,
            "response_estimated_bytes": estimated_response_bytes,
            "response_estimated_bytes_limit": SEARCH_PAGE_ESTIMATED_BYTES,
            "partial_due_to_oversized_input": pending_count > 0,
            "partial": partial,
            "partial_reason": (
                ",".join(
                    sorted(
                        cumulative_pending_reasons
                        | ({str(search_state["budget_reason"])} if candidate_budget_exhausted else set())
                    )
                )
                if partial
                else None
            ),
            "verified_chars_per_candidate": int(search_state["verify_chars"]),
            "verified_bytes_per_candidate": int(search_state["verify_bytes"]),
            "max_observed_verified_chars_per_candidate": int(
                search_state["max_observed_verified_chars_per_candidate"]
            ),
            "max_observed_verified_bytes_per_candidate": int(
                search_state["max_observed_verified_bytes_per_candidate"]
            ),
            "request_verified_bytes": int(search_state["request_verified_bytes"]),
            "request_verified_chars": int(search_state["request_verified_chars"]),
            "request_verify_bytes_limit": int(search_state["request_verify_bytes_limit"]),
            "request_verify_chars_limit": int(search_state["request_verify_chars_limit"]),
            "raw_fallback_bytes_per_row": int(search_state["raw_verify_bytes"]),
            "raw_fallback_chars_per_row": int(search_state["raw_verify_chars"]),
            "raw_fallback_tier": int(search_state["raw_tier_index"]),
            "verify_chunk_bytes": SEARCH_STREAM_CHUNK_BYTES,
            "oversized_candidates_seen": max(
                int(search_state["streamed_candidates"]), pending_count
            ),
            "oversized_candidates_verified": max(
                0, int(search_state["streamed_candidates"]) - pending_count
            ),
            "oversized_candidates_pending": pending_count,
            "candidate_count": int(search_state["candidate_count"]),
            "candidate_sql_rows": int(search_state["candidate_count"]),
            "candidates_seen": int(search_state["candidate_count"]),
            "candidates_verified": max(
                0, int(search_state["candidate_count"]) - pending_count
            ),
            "candidate_limit": SEARCH_CANDIDATE_LIMIT,
            "resolver_calls": int(search_state["resolver_calls"]),
            "blob_reads": int(search_state["blob_reads"]),
            "candidate_blob_bytes": int(search_state["candidate_blob_bytes"]),
            "raw_blob_bytes": int(search_state["raw_blob_bytes"]),
            "blob_read_bytes": int(
                search_state["candidate_blob_bytes"] + search_state["raw_blob_bytes"]
            ),
            "temp_page_delta": max(
                0,
                int(conn.execute("PRAGMA temp.page_count").fetchone()[0])
                - int(search_state["temp_pages_before"]),
            ),
            "decoded_chars": int(search_state["decoded_chars"]),
            "normalization_units": int(search_state["normalization_units"]),
            "sqlite_vm_steps": int(search_state["sqlite_vm_steps"]),
            "wall_seconds": max(
                0.0, time.monotonic() - float(search_state["started_monotonic"])
            ),
            "continuation_available": continuation_token is not None,
            "continuation_token": continuation_token,
            "completion_state": "partial" if partial else "complete",
        }
    )
    result.update(
        {
            "order_exact": not partial,
            "scan_complete": not partial,
            "provisional_order": partial,
        }
    )
    if continuation or continuation_token is not None:
        # Continuation pages are candidate-cursor segments, not numeric-offset
        # pages over a materialized global result set.
        result["has_more"] = continuation_token is not None
        result["next_offset"] = None
        if continuation_token is not None:
            result["has_more"] = True
    result["diagnostics"] = diagnostics
    return _bounded_search_response(result)


def search_conversations(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    *,
    limit: int = 50,
    offset: int = 0,
    sort: str = "relevance",
    selected_id: str | None = None,
    continuation: str | None = None,
) -> dict[str, Any]:
    search_state = _ensure_search_functions(conn, parsed)
    if not parsed.has_search_context():
        if continuation:
            raise SearchContinuationError("invalid_search_continuation")
        return list_conversations(conn, limit=limit, offset=offset, sort=sort, after=parsed.after, before=parsed.before, selected_id=selected_id)
    body_positive = bool(
        parsed.scope != "title"
        and (parsed.terms or parsed.phrases or parsed.required_phrases)
    )
    if _conversation_search_requires_global_current(parsed) and not body_positive:
        ensure_effective_current_views(conn, _conversation_current_candidate_ids(conn, parsed))
    limit = _bounded_limit(limit, MAX_API_LIMIT)
    offset = max(0, offset)

    query_contract = _search_query_contract(
        parsed,
        conversation_id="__conversation_results__",
        order=sort,
        limit=limit,
        offset=offset,
        count_total=False,
    )
    database_contract = _search_database_contract(conn)
    resume_after_node_id = ""
    resume_after_conversation_id = ""
    last_confirmed_conversation_id = ""
    confirmed_hits_before = 0
    pending_candidates_before = 0
    pending_reasons_before: set[str] = set()
    raw_tier_index = 0
    retry_pending_candidate = False
    if continuation:
        if not body_positive:
            raise SearchContinuationError("invalid_search_continuation")
        continuation_payload = _decode_search_continuation(continuation)
        if (
            continuation_payload.get("query_contract") != query_contract
            or continuation_payload.get("database_contract") != database_contract
            or continuation_payload.get("budget_contract_version")
            != SEARCH_BUDGET_CONTRACT_VERSION
            or continuation_payload.get("result_kind") != "conversation"
        ):
            raise SearchContinuationError("search_continuation_stale")
        resume_node_value = continuation_payload.get("candidate_node_cursor", "")
        resume_conversation_value = continuation_payload.get(
            "candidate_conversation_cursor", ""
        )
        last_confirmed_value = continuation_payload.get(
            "last_confirmed_conversation_id", ""
        )
        confirmed_value = continuation_payload.get("confirmed_hits_before", 0)
        pending_value = continuation_payload.get("pending_candidates_before", 0)
        pending_reason_values = continuation_payload.get("pending_reasons_before", [])
        raw_tier_value = continuation_payload.get("raw_tier_index", 0)
        retry_value = continuation_payload.get("retry_pending_candidate", False)
        if (
            not isinstance(resume_node_value, str)
            or len(resume_node_value) > MAX_SEARCH_CONTINUATION_ID_CHARS
            or not isinstance(resume_conversation_value, str)
            or len(resume_conversation_value) > MAX_SEARCH_CONTINUATION_ID_CHARS
            or not isinstance(last_confirmed_value, str)
            or len(last_confirmed_value) > MAX_SEARCH_CONTINUATION_ID_CHARS
            or isinstance(confirmed_value, bool)
            or not isinstance(confirmed_value, int)
            or confirmed_value < 0
            or isinstance(pending_value, bool)
            or not isinstance(pending_value, int)
            or pending_value < 0
            or not isinstance(pending_reason_values, list)
            or any(not isinstance(item, str) or len(item) > 64 for item in pending_reason_values)
            or isinstance(raw_tier_value, bool)
            or not isinstance(raw_tier_value, int)
            or raw_tier_value < 0
            or not isinstance(retry_value, bool)
        ):
            raise SearchContinuationError("invalid_search_continuation")
        resume_after_node_id = resume_node_value
        resume_after_conversation_id = resume_conversation_value
        last_confirmed_conversation_id = last_confirmed_value
        confirmed_hits_before = confirmed_value
        pending_candidates_before = pending_value
        pending_reasons_before = set(pending_reason_values)
        raw_tier_index = raw_tier_value
        retry_pending_candidate = retry_value
        if retry_pending_candidate:
            pending_candidates_before = max(0, pending_candidates_before - 1)
        search_state = _ensure_search_functions(
            conn, parsed, raw_tier_index=raw_tier_index
        )

    candidate_budget_exhausted = False
    last_candidate_node_id = resume_after_node_id
    last_candidate_conversation_id = resume_after_conversation_id
    verified_messages = False
    effective_offset = 0 if continuation else offset

    def execute_page(use_trigram: bool) -> tuple[list[dict[str, Any]], int]:
        nonlocal search_state, candidate_budget_exhausted, last_candidate_node_id
        nonlocal last_candidate_conversation_id, verified_messages
        if body_positive:
            _prepare_verified_message_table(conn)
            search_state = _ensure_search_functions(
                conn, parsed, raw_tier_index=raw_tier_index
            )
            scan_parsed = replace(parsed, path="all") if parsed.path == "current" else parsed
            (
                last_candidate_node_id,
                candidate_budget_exhausted,
                last_candidate_conversation_id,
                _unused_candidate_offset,
            ) = _scan_verified_message_candidates(
                conn,
                scan_parsed,
                None,
                search_state,
                use_trigram=use_trigram,
                resume_after_node_id=resume_after_node_id,
                max_verified_results=SEARCH_CANDIDATE_LIMIT + 1,
                conversation_grouped=True,
                resume_after_conversation_id=resume_after_conversation_id,
            )
            if parsed.path == "current":
                candidate_conversation_ids = (
                    str(row[0])
                    for row in conn.execute(
                        """
                        SELECT DISTINCT n.conversation_id
                        FROM temp.web_verified_message_results verified
                        JOIN conversation_nodes n ON n.rowid = verified.storage_rowid
                        """
                    )
                )
                ensure_effective_current_views(conn, candidate_conversation_ids)
                conn.execute(
                    """
                    DELETE FROM temp.web_verified_message_results
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM conversation_nodes n
                        JOIN effective_current_nodes effective
                          ON effective.conversation_id = n.conversation_id
                         AND effective.node_id = n.node_id
                        WHERE n.rowid = web_verified_message_results.storage_rowid
                    )
                    """
                )
            if last_confirmed_conversation_id:
                # Conversation-grouped candidate order means only the group
                # straddling a continuation boundary can have been confirmed
                # by the previous segment. Remove that boundary duplicate
                # before counting or returning this segment.
                conn.execute(
                    """
                    DELETE FROM temp.web_verified_message_results
                    WHERE storage_rowid IN (
                        SELECT verified.storage_rowid
                        FROM temp.web_verified_message_results verified
                        JOIN conversation_nodes n
                          ON n.rowid = verified.storage_rowid
                        WHERE n.conversation_id = ?
                    )
                    """,
                    (last_confirmed_conversation_id,),
                )
            verified_messages = True
        return _conversation_search_page(
            conn,
            parsed,
            limit,
            effective_offset,
            sort,
            use_trigram=use_trigram,
            verified_messages=verified_messages,
            include_title_matches=not continuation,
        )

    try:
        items, total = execute_page(True)
        diagnostics = _conversation_search_diagnostics(conn, parsed, used_trigram=True)
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        items, total = execute_page(False)
        diagnostics = _conversation_search_diagnostics(conn, parsed, used_trigram=False)
    _add_counts_and_path_metadata(conn, items)
    for conv in items:
        conv["reasons"] = sorted(conv["reasons"])
    _batch_conversation_enrichment(
        conn, parsed, items, verified_messages=verified_messages
    )
    pending_count = pending_candidates_before + len(search_state["pending_rowids"])
    cumulative_pending_reasons = pending_reasons_before | set(
        search_state["pending_reasons"]
    )
    segment_total = int(total or 0)
    cumulative_total = confirmed_hits_before + segment_total
    current_last_confirmed = conn.execute(
        """
        SELECT MAX(n.conversation_id)
        FROM temp.web_verified_message_results verified
        JOIN conversation_nodes n ON n.rowid = verified.storage_rowid
        """
    ).fetchone()[0] if body_positive else None
    next_last_confirmed_conversation_id = (
        str(current_last_confirmed)
        if current_last_confirmed is not None
        else last_confirmed_conversation_id
    )
    partial = pending_count > 0 or candidate_budget_exhausted
    selected_in_results = None
    selected_item = None
    # A partial candidate segment cannot truthfully decide that an arbitrary
    # selected conversation is absent. Preserve it until the scan completes.
    # A continuation TEMP artifact contains only its own candidate segment.
    # Even the terminal segment cannot disprove that selected_id matched an
    # earlier segment, so continuation pages keep this tri-state unknown.
    if selected_id and not partial and not continuation:
        selected_in_results = _conversation_search_contains(
            conn,
            parsed,
            selected_id,
            verified_messages=verified_messages,
        )
        if selected_in_results and not any(item["conversation_id"] == selected_id for item in items):
            selected_item = _conversation_search_item(
                conn,
                parsed,
                selected_id,
                verified_messages=verified_messages,
            )
    if selected_item:
        _add_counts_and_path_metadata(conn, [selected_item])
        _batch_conversation_enrichment(
            conn,
            parsed,
            [selected_item],
            verified_messages=verified_messages,
        )
    result = _page_payload(
        items,
        cumulative_total if body_positive else total,
        limit,
        effective_offset,
        selected_in_results=selected_in_results,
        selected_item=selected_item,
    )
    retry_pending = bool(search_state["retry_pending_candidate"])
    next_raw_tier_index = int(search_state["raw_tier_index"])
    retry_reason = str(search_state["budget_reason"] or "")
    if retry_pending and retry_reason == "raw_fallback_limit":
        next_raw_tier_index += 1
    raw_tier_available = next_raw_tier_index < len(search_state["raw_tiers"])
    retry_available = retry_pending and (
        retry_reason == "request_aggregate_limit"
        or (retry_reason == "raw_fallback_limit" and raw_tier_available)
    )
    continuation_token = (
        _encode_search_continuation(
            {
                "version": SEARCH_CONTINUATION_VERSION,
                "result_kind": "conversation",
                "budget_contract_version": SEARCH_BUDGET_CONTRACT_VERSION,
                "database_contract": database_contract,
                "query_contract": query_contract,
                "candidate_node_cursor": last_candidate_node_id,
                "candidate_conversation_cursor": last_candidate_conversation_id,
                "last_confirmed_conversation_id": next_last_confirmed_conversation_id,
                "confirmed_hits_before": cumulative_total,
                "pending_candidates_before": pending_count,
                "pending_reasons_before": sorted(cumulative_pending_reasons),
                "raw_tier_index": (
                    next_raw_tier_index
                    if raw_tier_available
                    else int(search_state["raw_tier_index"])
                ),
                "retry_pending_candidate": retry_available,
            }
        )
        if body_positive
        and candidate_budget_exhausted
        and (
            retry_available
            or (
                not retry_pending
                and (
                    last_candidate_conversation_id > resume_after_conversation_id
                    or (
                        last_candidate_conversation_id
                        == resume_after_conversation_id
                        and last_candidate_node_id > resume_after_node_id
                    )
                )
            )
        )
        else None
    )
    diagnostics.update({
        "resource_contract": "streamed_conversation_search_v2",
        "configured_candidate_scan_char_limit": SEARCH_CANDIDATE_SCAN_CHARS,
        "configured_verified_char_limit_per_candidate": int(search_state["verify_chars"]),
        "configured_verified_byte_limit_per_candidate": int(search_state["verify_bytes"]),
        "max_observed_verified_chars_per_candidate": int(
            search_state["max_observed_verified_chars_per_candidate"]
        ),
        "max_observed_verified_bytes_per_candidate": int(
            search_state["max_observed_verified_bytes_per_candidate"]
        ),
        "partial_due_to_oversized_input": pending_count > 0,
        "partial": partial,
        "partial_reason": (
            ",".join(
                sorted(
                    cumulative_pending_reasons
                    | ({str(search_state["budget_reason"])} if candidate_budget_exhausted else set())
                )
            )
            if partial
            else None
        ),
        "oversized_candidates_seen": max(
            int(search_state["streamed_candidates"]), pending_count
        ),
        "oversized_candidates_verified": max(
            0, int(search_state["streamed_candidates"]) - pending_count
        ),
        "oversized_candidates_pending": pending_count,
        "candidate_count": int(search_state["candidate_count"]),
        "candidate_sql_rows": int(search_state["candidate_count"]),
        "candidates_seen": int(search_state["candidate_count"]),
        "candidates_verified": max(
            0, int(search_state["candidate_count"]) - pending_count
        ),
        "candidate_limit": SEARCH_CANDIDATE_LIMIT,
        "resolver_calls": int(search_state["resolver_calls"]),
        "blob_reads": int(search_state["blob_reads"]),
        "candidate_blob_bytes": int(search_state["candidate_blob_bytes"]),
        "raw_blob_bytes": int(search_state["raw_blob_bytes"]),
        "blob_read_bytes": int(
            search_state["candidate_blob_bytes"] + search_state["raw_blob_bytes"]
        ),
        "temp_page_delta": max(
            0,
            int(conn.execute("PRAGMA temp.page_count").fetchone()[0])
            - int(search_state["temp_pages_before"]),
        ),
        "decoded_chars": int(search_state["decoded_chars"]),
        "normalization_units": int(search_state["normalization_units"]),
        "sqlite_vm_steps": int(search_state["sqlite_vm_steps"]),
        "wall_seconds": max(
            0.0, time.monotonic() - float(search_state["started_monotonic"])
        ),
        "continuation_available": continuation_token is not None,
        "continuation_token": continuation_token,
        "completion_state": "partial" if partial else "complete",
    })
    result.update(
        {
            "order_exact": not partial,
            "scan_complete": not partial,
            "provisional_order": partial,
        }
    )
    if continuation or continuation_token is not None:
        result["has_more"] = continuation_token is not None
        result["next_offset"] = None
    result["diagnostics"] = diagnostics
    return _bounded_search_response(result)


def _search_has_positive_body_or_title(parsed: ParsedQuery) -> bool:
    return bool(parsed.terms or parsed.phrases or parsed.required_phrases or parsed.title or parsed.required_title)


def _conversation_search_requires_global_current(parsed: ParsedQuery) -> bool:
    """Return whether candidate filtering itself needs message membership."""

    if parsed.path != "current" or parsed.scope == "title":
        return False
    return bool(
        parsed.terms
        or parsed.phrases
        or parsed.required_phrases
        or parsed.role
        or parsed.exclude
    )


def _path_independent_candidate_query(parsed: ParsedQuery, *, keep_hit_excludes: bool) -> ParsedQuery:
    """Return a superset query that can run before effective-current exists."""

    return replace(parsed, path="all", exclude=list(parsed.exclude) if keep_hit_excludes else [])


def _conversation_ids_from_query(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any],
    *,
    fallback_sql: str | None = None,
    fallback_params: Sequence[Any] = (),
) -> Iterable[str]:
    """Open the candidate cursor lazily, after TEMP scope setup has finished."""

    def rows() -> Iterable[str]:
        try:
            cursor = conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if fallback_sql is None or not is_optional_search_capability_missing(exc):
                raise
            cursor = conn.execute(fallback_sql, fallback_params)
        for row in cursor:
            yield str(row[0])

    return rows()


def _message_current_candidate_ids(conn: sqlite3.Connection, parsed: ParsedQuery) -> Iterable[str]:
    """Find global message-search conversation candidates without path membership."""

    candidate = _path_independent_candidate_query(parsed, keep_hit_excludes=True)
    def select(*, use_trigram: bool) -> tuple[str, list[Any]]:
        source_sql, source_params, _score, _reason = _message_match_source(
            conn, candidate, use_trigram=use_trigram
        )
        where, params = _node_filters(candidate, None)
        return (
            f"SELECT DISTINCT n.conversation_id FROM {source_sql} "
            f"JOIN conversations c ON c.conversation_id = n.conversation_id "
            f"WHERE 1 = 1 {where}",
            source_params + params,
        )

    base_sql, params = select(use_trigram=True)
    fallback_base_sql, fallback_params = select(use_trigram=False)
    return _conversation_ids_from_query(
        conn,
        base_sql,
        params,
        fallback_sql=fallback_base_sql,
        fallback_params=fallback_params,
    )


def _conversation_current_candidate_ids(conn: sqlite3.Connection, parsed: ParsedQuery) -> Iterable[str] | None:
    """Find a safe path-independent superset for global conversation search.

    Exclusions are intentionally omitted here: an excluded fragment on an
    off-current branch must not remove a conversation before effective-current
    membership is known. When exclusion is the only usable predicate, ``None``
    explicitly selects the documented full-database fallback.
    """

    candidate = _path_independent_candidate_query(parsed, keep_hit_excludes=False)
    has_positive = _search_has_positive_body_or_title(candidate)
    has_safe_filter = bool(candidate.role or candidate.source or candidate.after is not None or candidate.before is not None)
    if not has_positive and not has_safe_filter:
        return None

    def candidate_select(*, use_trigram: bool) -> tuple[str, list[Any]] | None:
        if not has_positive:
            where, params = _filter_conversation_where(candidate)
            return f"SELECT c.conversation_id FROM conversations c {where}", params

        parts: list[str] = []
        params: list[Any] = []
        has_message_match = bool(candidate.terms or candidate.phrases or candidate.required_phrases or candidate.role)
        if candidate.scope != "title" and has_message_match:
            sql, sql_params = _message_conversation_select(conn, candidate, use_trigram=use_trigram)
            parts.append(sql)
            params.extend(sql_params)
        if candidate.scope != "message" and not candidate.role:
            sql, sql_params = _title_conversation_select(conn, candidate, use_trigram=use_trigram)
            parts.append(sql)
            params.extend(sql_params)
        if not parts:
            return None
        combined = " UNION ALL ".join(parts)
        return f"SELECT DISTINCT conversation_id FROM ({combined}) candidates", params

    primary = candidate_select(use_trigram=True)
    fallback = candidate_select(use_trigram=False)
    if primary is None:
        return ()
    return _conversation_ids_from_query(
        conn,
        primary[0],
        primary[1],
        fallback_sql=fallback[0] if fallback else None,
        fallback_params=fallback[1] if fallback else (),
    )


def _is_title_only_candidate_context(parsed: ParsedQuery) -> bool:
    return (
        parsed.scope != "message"
        and bool(parsed.title or parsed.required_title)
        and not (parsed.terms or parsed.phrases or parsed.required_phrases or parsed.role)
    )


def get_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any] | None:
    ensure_effective_current_views(conn, [conversation_id])
    row = conn.execute(
        f"""
        SELECT {_conversation_api_columns('c')}, COUNT(n.node_id) AS node_count,
               SUM(CASE WHEN n.is_on_current_path = 1 THEN 1 ELSE 0 END) AS current_path_nodes
        FROM conversations c
        LEFT JOIN conversation_nodes n ON n.conversation_id = c.conversation_id
        WHERE c.conversation_id = ?
        GROUP BY c.conversation_id
        """,
        (conversation_id,),
    ).fetchone()
    if not row:
        return None
    summary = _conversation_summary(row)
    metadata = effective_current_metadata(conn, [conversation_id]).get(conversation_id, {})
    summary.update(metadata)
    return summary


def get_messages(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    path: str,
    limit: int,
    offset: int,
    highlight_query: str | None = None,
    highlight_parsed: ParsedQuery | None = None,
    match_mode: str = "contains",
    around_node_id: str | None = None,
    include_internal: bool = True,
) -> dict[str, Any]:
    _ensure_search_functions(conn)
    limit = _bounded_limit(limit, MAX_MESSAGE_LIMIT)
    offset = max(0, offset)
    budget = reader_budget()
    if around_node_id:
        return _get_messages_around_node_sql(
            conn,
            conversation_id,
            path,
            limit,
            offset,
            highlight_query,
            highlight_parsed,
            match_mode,
            around_node_id,
            include_internal=include_internal,
            budget=budget,
        )
    conversation = get_conversation(conn, conversation_id)
    current_path_fallback_to_all = _current_path_fallback_to_all_from_counts(conversation) if path == "current" else False
    rows, total = _paged_conversation_rows(
        conn, conversation_id, path, limit, offset, include_internal=include_internal, budget=budget
    )

    effective_ids = _effective_node_ids_for_rows(conn, conversation_id, rows)
    parsed = highlight_parsed or parse_query(highlight_query or "", match_mode=match_mode)
    conversation_excluded = _conversation_has_excluded_in_scope(conn, parsed, conversation_id)
    budget_state: dict[str, Any] = {}
    items = _message_page_items(
            rows,
            parsed,
            conversation,
            path,
            effective_ids,
            conversation_excluded,
            current_path_fallback_to_all,
            budget=budget,
            budget_state=budget_state,
        )
    return _page_payload(
        items,
        total,
        limit,
        offset,
        extra={
            **_message_visibility_counts_for_path(conn, conversation_id, path),
            **_path_metadata_extra(path, current_path_fallback_to_all, conversation),
            **budget_state,
        },
    )


def _blob_byte_offset_for_char(blob: sqlite3.Blob, target_chars: int) -> tuple[int, int]:
    """Locate a character offset with fixed-size reads and no prefix allocation."""

    if target_chars <= 0:
        return 0, 0
    blob.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    stable_byte_offset = 0
    char_offset = 0
    while True:
        data = blob.read(SEARCH_STREAM_CHUNK_BYTES)
        final = not data
        decoded = decoder.decode(data, final=final)
        if decoded:
            needed = target_chars - char_offset
            if needed <= len(decoded):
                prefix = decoded[:needed]
                return min(len(blob), stable_byte_offset + len(prefix.encode("utf-8", errors="replace"))), target_chars
            char_offset += len(decoded)
            stable_byte_offset += len(decoded.encode("utf-8", errors="replace"))
        if final:
            return len(blob), char_offset


def _utf8_prefix_bytes(text: str, target_chars: int) -> int:
    """Count one UTF-8 prefix with bounded temporary allocations."""

    target = max(0, min(len(text), int(target_chars)))
    total = 0
    for offset in range(0, target, SEARCH_STREAM_CHUNK_BYTES):
        total += len(
            text[offset : min(target, offset + SEARCH_STREAM_CHUNK_BYTES)].encode(
                "utf-8", errors="replace"
            )
        )
    return total


def _blob_placeholder_exact(
    conn: sqlite3.Connection,
    storage_rowid: int,
    byte_size: int,
    *,
    max_bytes: int,
) -> bool | None:
    """Return exact placeholder classification, or None when over budget."""

    if byte_size < 0 or byte_size > max_bytes:
        return None
    classifier = PlaceholderStreamClassifier()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with conn.blobopen(
        "conversation_nodes", "content_text", storage_rowid, readonly=True
    ) as blob:
        while True:
            data = blob.read(SEARCH_STREAM_CHUNK_BYTES)
            if not data:
                break
            classifier.feed(decoder.decode(data, final=False))
            if classifier.phase == "invalid":
                return False
        classifier.feed(decoder.decode(b"", final=True))
    return classifier.exact_placeholder


def _display_revision_from_values(row: Mapping[str, Any]) -> str:
    """Return the durable, row-local display revision token."""

    value = row["display_revision"]
    if isinstance(value, str) and len(value) == 32 and all(
        ch in "0123456789abcdef" for ch in value
    ):
        return f"row:{value}"
    # A current schema never reaches this branch. Keeping a deterministic
    # incompatible marker makes an in-flight predecessor cursor stale rather
    # than accidentally reviving it.
    return "row:invalid"


def get_message_display_chunk(
    conn: sqlite3.Connection,
    conversation_id: str,
    node_id: str,
    *,
    offset: int,
    limit: int,
    cursor: str | None = None,
    anchor_char_offset: int | None = None,
) -> dict[str, Any] | None:
    """Return a bounded display-text chunk without exposing raw JSON."""

    budget = reader_budget()
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), budget.display_chunk_chars))
    if anchor_char_offset is not None:
        anchor_char_offset = max(0, min(int(anchor_char_offset), SEARCH_EXACT_VERIFY_MAX_OPT_IN_CHARS))
        if cursor:
            raise DisplayCursorError("invalid_display_cursor")
    if anchor_char_offset is None and not cursor and offset > MAX_LEGACY_DISPLAY_OFFSET:
        raise DisplayCursorError("display_cursor_required")
    row = conn.execute(
        """
        SELECT n.rowid AS storage_rowid,
               n.content_type, n.content_hash, n.display_revision,
               length(CAST(n.content_text AS BLOB)) AS content_storage_bytes,
               length(CAST(n.raw_message_json AS BLOB)) AS raw_storage_bytes
        FROM conversation_nodes n
        WHERE n.conversation_id = ? AND n.node_id = ?
        """,
        (conversation_id, node_id),
    ).fetchone()
    if row is None:
        return None
    storage_rowid = int(row["storage_rowid"])
    database_token_identity = _database_token_identity(conn)
    prefix_bytes = b""
    try:
        with conn.blobopen("conversation_nodes", "content_text", storage_rowid, readonly=True) as prefix_blob:
            prefix_bytes = prefix_blob.read(min(len(prefix_blob), 256))
    except sqlite3.OperationalError:
        is_null = conn.execute(
            "SELECT content_text IS NULL FROM conversation_nodes WHERE rowid = ?",
            (storage_rowid,),
        ).fetchone()
        if not is_null or not bool(is_null[0]):
            raise
    content_prefix = normalize_display_text(prefix_bytes.decode("utf-8", errors="replace"))
    revision = _display_revision_from_values(row)
    if cursor:
        (
            cursor_identity,
            cursor_revision,
            cursor_source,
            byte_offset,
            char_offset,
        ) = _decode_display_cursor(database_token_identity, cursor)
        if (
            cursor_identity != _display_cursor_identity(conversation_id, node_id)
            or cursor_revision != revision
            or char_offset != offset
            or cursor_source != "canonical"
        ):
            raise DisplayCursorError("display_cursor_stale")
    resolver_input_truncated = False
    next_cursor = None
    canonical_has_more = False
    content_storage_bytes = int(row["content_storage_bytes"] or 0)
    placeholder_exact: bool | None = False
    if placeholder_prefix_may_match(
        content_prefix,
        truncated=content_storage_bytes > len(prefix_bytes),
    ):
        placeholder_exact = _blob_placeholder_exact(
            conn,
            storage_rowid,
            content_storage_bytes,
            max_bytes=SEARCH_EXACT_VERIFY_BYTES,
        )
    if prefix_bytes and placeholder_exact is False:
        if not cursor:
            byte_offset = 0
            char_offset = 0
            requested_offset = (
                max(0, anchor_char_offset - min(limit // 2, 4096))
                if anchor_char_offset is not None
                else offset
            )
            if requested_offset:
                # Compatibility path for old clients. It is NUL-safe but scans
                # the requested prefix once; sequential clients use cursor.
                with conn.blobopen("conversation_nodes", "content_text", storage_rowid, readonly=True) as prefix_blob:
                    byte_offset, char_offset = _blob_byte_offset_for_char(
                        prefix_blob, requested_offset
                    )
            offset = char_offset
        with conn.blobopen("conversation_nodes", "content_text", storage_rowid, readonly=True) as blob:
            if byte_offset > len(blob):
                raise DisplayCursorError("invalid_display_cursor")
            chunk, next_byte, has_more, invalid_utf8 = _read_utf8_blob_chunk(blob, byte_offset, limit)
        canonical_has_more = has_more
        total_chars = offset + len(chunk)
        total_exact = not has_more
        if has_more and not invalid_utf8:
            next_cursor = _encode_display_cursor(
                database_token_identity,
                _display_cursor_identity(conversation_id, node_id),
                revision,
                next_byte,
                total_chars,
            )
        source = "canonical"
    else:
        raw_row = conn.execute(
            """
            SELECT raw_message_json IS NULL AS raw_is_null
            FROM conversation_nodes
            WHERE rowid = ?
            """,
            (storage_rowid,),
        ).fetchone()
        if bool(raw_row["raw_is_null"]):
            raw_bytes = 0
            raw_bounded_bytes = b""
        else:
            with conn.blobopen(
                "conversation_nodes", "raw_message_json", storage_rowid, readonly=True
            ) as raw_blob:
                raw_bytes = len(raw_blob)
                raw_bounded_bytes = raw_blob.read(min(raw_bytes, 800_004))
        raw_bounded = normalize_display_text(raw_bounded_bytes.decode("utf-8", errors="replace"))
        resolver_input_truncated = raw_bytes > len(raw_bounded_bytes) or len(raw_bounded) > 200_000
        canonical = (
            content_prefix
            if placeholder_exact and content_storage_bytes <= len(prefix_bytes)
            else "[non-text content: streamed-placeholder]"
            if placeholder_exact
            else content_prefix
        )
        recovered = recover_message_display_text(
            canonical,
            raw_bounded[:200_000] if not resolver_input_truncated else "",
        )
        total_chars = len(recovered)
        total_exact = not resolver_input_truncated
        if anchor_char_offset is not None:
            offset = max(0, anchor_char_offset - min(limit // 2, 4096))
        chunk = recovered[offset : offset + limit]
        source = "raw_fallback" if recovered != canonical else "canonical_placeholder"
        if placeholder_exact is None:
            resolver_input_truncated = True
    return {
        "conversation_id": conversation_id,
        "node_id": node_id,
        "display_text": chunk,
        "offset": offset,
        "returned_chars": len(chunk),
        "total_chars": total_chars,
        "total_chars_exact": total_exact,
        "has_more": canonical_has_more if source == "canonical" else offset + len(chunk) < total_chars,
        "next_offset": total_chars if canonical_has_more else (offset + len(chunk) if offset + len(chunk) < total_chars else None),
        "next_cursor": next_cursor,
        "content_revision": revision,
        "max_chunk_chars": budget.display_chunk_chars,
        "resolver_input_truncated": resolver_input_truncated,
        "source": source,
        "anchor_char_offset": anchor_char_offset,
        "anchor_offset_in_chunk": (
            max(0, anchor_char_offset - offset) if anchor_char_offset is not None else None
        ),
    }


_MESSAGE_SIMPLE_COLUMNS = (
    "node_id", "parent_node_id", "message_id", "role", "author_name",
    "create_time", "update_time", "content_type", "content_text", "content_hash", "is_on_current_path",
)

_READER_METADATA_COLUMNS = (
    "node_id", "parent_node_id", "message_id", "role", "author_name",
    "create_time", "update_time", "content_type", "content_hash", "is_on_current_path",
)


def _message_select_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    columns = [f"{prefix}{name}" for name in _MESSAGE_SIMPLE_COLUMNS]
    columns.extend(
        [
            f"substr({prefix}raw_message_json, 1, 200001) AS raw_message_json",
        ]
    )
    return ", ".join(columns)


def _reader_metadata_select_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{name}" for name in _READER_METADATA_COLUMNS)


def _hydrate_reader_rows(
    conn: sqlite3.Connection,
    conversation_id: str,
    rows: Sequence[sqlite3.Row],
    budget: ReaderBudget,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    output = [dict(row) for row in rows]
    count = len(output)
    display_limit = max(
        1,
        min(
            budget.message_display_chars,
            max(READER_MIN_TEXT_HYDRATION_CHARS, budget.page_display_chars // count),
        ),
    )
    raw_limit = max(1, min(200_000, budget.page_raw_resolver_chars // count))
    node_ids = [str(row["node_id"]) for row in output]
    placeholders = ",".join("?" for _ in node_ids)
    text_rows = conn.execute(
        f"""
        SELECT rowid AS storage_rowid, node_id,
               content_text IS NULL AS content_text_is_null,
               raw_message_json IS NULL AS raw_message_is_null,
               length(CAST(content_text AS BLOB)) AS content_storage_bytes
        FROM conversation_nodes
        WHERE conversation_id = ? AND node_id IN ({placeholders})
        """,
        [conversation_id, *node_ids],
    ).fetchall()
    by_id = {str(row["node_id"]): row for row in text_rows}
    placeholder_budget_remaining = SEARCH_EXACT_VERIFY_BYTES
    for row in output:
        text_row = by_id[str(row["node_id"])]
        storage_rowid = int(text_row["storage_rowid"])
        content, content_total, content_exact, content_truncated = _bounded_row_blob_text(
            conn,
            storage_rowid,
            "content_text",
            display_limit,
            is_null=bool(text_row["content_text_is_null"]),
        )
        raw, raw_total, raw_exact, raw_truncated = _bounded_row_blob_text(
            conn,
            storage_rowid,
            "raw_message_json",
            raw_limit,
            is_null=bool(text_row["raw_message_is_null"]),
        )
        placeholder_classification_exact = True
        placeholder_exact = False
        if placeholder_prefix_may_match(content, truncated=content_truncated):
            content_storage_bytes = int(text_row["content_storage_bytes"] or 0)
            classification = _blob_placeholder_exact(
                conn,
                storage_rowid,
                content_storage_bytes,
                max_bytes=placeholder_budget_remaining,
            )
            if classification is None:
                placeholder_classification_exact = False
            else:
                placeholder_budget_remaining = max(
                    0, placeholder_budget_remaining - content_storage_bytes
                )
                placeholder_exact = classification
                if placeholder_exact and content_truncated:
                    content = "[non-text content: streamed-placeholder]"
        row["content_text"] = content
        row["content_text_total_chars"] = content_total
        row["content_text_total_chars_exact"] = content_exact
        row["content_text_source_truncated"] = content_truncated
        row["raw_message_json"] = raw
        row["raw_message_total_chars"] = raw_total
        row["raw_message_total_chars_exact"] = raw_exact
        row["raw_message_source_truncated"] = raw_truncated
        row["content_placeholder_exact"] = placeholder_exact
        row["content_placeholder_classification_exact"] = placeholder_classification_exact
    return output


def _bounded_row_blob_text(
    conn: sqlite3.Connection,
    storage_rowid: int,
    column: str,
    limit: int,
    *,
    is_null: bool,
) -> tuple[str, int, bool, bool]:
    """Read at most ``limit + 1`` UTF-8 characters from one SQLite value."""

    if is_null:
        return "", 0, True, False
    with conn.blobopen("conversation_nodes", column, storage_rowid, readonly=True) as blob:
        text, _next_byte, has_more, _invalid_utf8 = _read_utf8_blob_chunk(
            blob, 0, limit + 1
        )
    truncated = has_more or len(text) > limit
    exact = not has_more
    total_or_lower_bound = len(text)
    return text[:limit], total_or_lower_bound, exact, truncated


def _conversation_rows(conn: sqlite3.Connection, conversation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT {_message_select_columns()}
        FROM conversation_nodes
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchall()


def _effective_node_ids_for_rows(
    conn: sqlite3.Connection,
    conversation_id: str,
    rows: list[sqlite3.Row],
) -> set[str]:
    node_ids = [str(row["node_id"]) for row in rows]
    if not node_ids:
        return set()
    ensure_effective_current_views(conn, [conversation_id])
    placeholders = ",".join("?" for _ in node_ids)
    matched = conn.execute(
        f"""
        SELECT node_id
        FROM effective_current_nodes
        WHERE conversation_id = ? AND node_id IN ({placeholders})
        """,
        [conversation_id] + node_ids,
    ).fetchall()
    return {str(row["node_id"]) for row in matched}


def _get_messages_around_node_sql(
    conn: sqlite3.Connection,
    conversation_id: str,
    path: str,
    limit: int,
    offset: int,
    highlight_query: str | None,
    highlight_parsed: ParsedQuery | None,
    match_mode: str,
    around_node_id: str,
    include_internal: bool = True,
    budget: ReaderBudget | None = None,
) -> dict[str, Any]:
    """Use SQL display-order lookup instead of reading all rows for around_node_id."""
    parsed = highlight_parsed or parse_query(highlight_query or "", match_mode=match_mode)
    conversation = get_conversation(conn, conversation_id)
    conversation_excluded = _conversation_has_excluded_in_scope(conn, parsed, conversation_id)
    index, total = _page_collection_index(conn, conversation_id, path, around_node_id, include_internal=include_internal)
    if index is not None:
        offset = max(0, min(index, max(0, total - limit)))
    else:
        offset = 0
    current_path_fallback_to_all = _current_path_fallback_to_all_from_counts(conversation) if path == "current" else False
    budget = budget or reader_budget()
    rows, page_total = _paged_conversation_rows(
        conn,
        conversation_id,
        path,
        limit,
        offset,
        include_internal=include_internal,
        budget=budget,
    )
    effective_ids = _effective_node_ids_for_rows(conn, conversation_id, rows)
    total = page_total
    visibility_counts = _message_visibility_counts_for_path(conn, conversation_id, path)
    around_metadata = _around_target_metadata(
        conn,
        conversation_id,
        path,
        around_node_id,
        include_internal=include_internal,
        applied=index is not None,
    )
    budget_state: dict[str, Any] = {}
    items = _message_page_items(
            rows,
            parsed,
            conversation,
            path,
            effective_ids,
            conversation_excluded,
            current_path_fallback_to_all,
            budget=budget,
            budget_state=budget_state,
        )
    return _page_payload(
        items,
        total,
        limit,
        offset,
        extra={
            **visibility_counts,
            **_path_metadata_extra(path, current_path_fallback_to_all, conversation),
            **around_metadata,
            **budget_state,
        },
    )


def _around_target_metadata(
    conn: sqlite3.Connection,
    conversation_id: str,
    path: str,
    node_id: str,
    *,
    include_internal: bool,
    applied: bool,
) -> dict[str, bool]:
    ensure_effective_current_views(conn, [conversation_id])
    row = conn.execute(
        f"""SELECT 1 AS found,
                   CASE WHEN {_sql_visible_message_condition('n')} THEN 1 ELSE 0 END AS reader_visible,
                   CASE WHEN {_current_path_condition('n')} THEN 1 ELSE 0 END AS effective_visible
            FROM conversation_nodes n
            WHERE n.conversation_id = ? AND n.node_id = ?""",
        (conversation_id, node_id),
    ).fetchone()
    found = row is not None
    in_effective_collection = bool(found and row["effective_visible"])
    in_requested_collection = bool(found and (path == "all" or in_effective_collection))
    visible = bool(in_requested_collection and (include_internal or row["reader_visible"]))
    return {
        "around_target_found": found,
        "around_target_in_effective_collection": in_effective_collection,
        "around_target_in_requested_collection": in_requested_collection,
        "around_target_visible": visible,
        "around_target_applied": bool(applied and visible),
    }


def _message_visibility_counts_for_path(conn: sqlite3.Connection, conversation_id: str, path: str) -> dict[str, int]:
    ensure_effective_current_views(conn, [conversation_id])
    path_clause = ""
    params: list[Any] = [conversation_id]
    if path == "current":
        path_clause = f"AND {_current_path_condition('conversation_nodes')}"
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE
                WHEN {_sql_empty_mapping_condition('conversation_nodes')}
                THEN 1 ELSE 0 END) AS empty_hidden_count,
            SUM(CASE
                WHEN NOT ({_sql_empty_mapping_condition('conversation_nodes')})
                AND ({_sql_internal_content_condition('conversation_nodes')})
                THEN 1 ELSE 0 END) AS internal_hidden_count,
            SUM(CASE
                WHEN NOT ({_sql_empty_mapping_condition('conversation_nodes')})
                AND ({_sql_internal_content_condition('conversation_nodes')})
                THEN 1 ELSE 0 END) AS technical_hidden_count
            -- Deprecated compatibility alias: technical_hidden_count is exactly
            -- internal_hidden_count and must not be interpreted as a second bucket.
        FROM conversation_nodes
        WHERE conversation_id = ? {path_clause}
        """,
        params,
    ).fetchone()
    total = int(row["total"] or 0)
    empty_hidden = int(row["empty_hidden_count"] or 0)
    internal_hidden = int(row["internal_hidden_count"] or 0)
    technical_hidden = int(row["technical_hidden_count"] or 0)
    return {
        "visible_total": max(0, total - empty_hidden - internal_hidden),
        "empty_hidden_count": empty_hidden,
        "internal_hidden_count": internal_hidden,
        "technical_hidden_count": technical_hidden,
    }


def _message_visibility_counts(
    rows: list[sqlite3.Row],
    resolved_fields: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    empty_hidden = 0
    internal_hidden = 0
    technical_hidden = 0
    visible = 0
    for row in rows:
        fields = (
            resolved_fields[str(row["node_id"])]
            if resolved_fields is not None
            else _message_display_fields(row)
        )

        if fields["is_empty_mapping_node"]:
            empty_hidden += 1
        elif fields["is_internal"]:
            internal_hidden += 1
            if fields["is_technical"]:
                technical_hidden += 1
        else:
            visible += 1
    return {
        "visible_total": visible,
        "empty_hidden_count": empty_hidden,
        "internal_hidden_count": internal_hidden,
        "technical_hidden_count": technical_hidden,
    }


def _path_metadata_extra(
    path: str,
    current_path_fallback_to_all: bool,
    conversation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "effective_path": "all" if path == "current" and current_path_fallback_to_all else path,
        "current_path_fallback_to_all": bool(path == "current" and current_path_fallback_to_all),
        "current_node_exists": bool(conversation and conversation.get("current_node_exists")),
        "current_collection_source": (
            conversation.get("current_collection_source", "fallback_all") if conversation else "fallback_all"
        ),
        "cycle_detected": bool(conversation and conversation.get("cycle_detected")),
        "missing_parent": bool(conversation and conversation.get("missing_parent")),
        "cross_conversation_parent": bool(conversation and conversation.get("cross_conversation_parent")),
        "partial_chain": bool(conversation and conversation.get("partial_chain")),
        "raw_flag_leaf_count": int(conversation.get("raw_flag_leaf_count", 0)) if conversation else 0,
        "selected_chain_cycle_detected": bool(conversation and conversation.get("selected_chain_cycle_detected")),
        "raw_flag_cycle_detected": bool(conversation and conversation.get("raw_flag_cycle_detected")),
        "selected_chain_missing_parent": bool(conversation and conversation.get("selected_chain_missing_parent")),
        "raw_flag_missing_parent": bool(conversation and conversation.get("raw_flag_missing_parent")),
        "selected_chain_cross_conversation_parent": bool(conversation and conversation.get("selected_chain_cross_conversation_parent")),
        "raw_flag_cross_conversation_parent": bool(conversation and conversation.get("raw_flag_cross_conversation_parent")),
    }


def _paged_conversation_rows(
    conn: sqlite3.Connection,
    conversation_id: str,
    path: str,
    limit: int,
    offset: int,
    *,
    include_internal: bool = True,
    budget: ReaderBudget | None = None,
) -> tuple[list[dict[str, Any]], int]:
    budget = budget or reader_budget()
    ensure_effective_current_views(conn, [conversation_id])
    visible_clause = "" if include_internal else f" AND {_sql_visible_message_condition('conversation_nodes')}"
    if path == "all":
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM conversation_nodes WHERE conversation_id = ?{visible_clause}",
            (conversation_id,),
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT {_reader_metadata_select_columns()}
            FROM conversation_nodes
            WHERE conversation_id = ?{visible_clause}
            ORDER BY create_time IS NULL,
                     COALESCE(create_time, update_time, 0),
                     node_id
            LIMIT ? OFFSET ?
            """,
            (conversation_id, limit, offset),
        ).fetchall()
        return _hydrate_reader_rows(conn, conversation_id, rows, budget), total

    output_filter = "" if include_internal else f"AND {_sql_visible_message_condition('n')}"
    total = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM conversation_nodes n
        JOIN effective_current_nodes ec
          ON ec.conversation_id = n.conversation_id AND ec.node_id = n.node_id
        WHERE n.conversation_id = ? {output_filter}
        """,
        (conversation_id,),
    ).fetchone()["c"]
    rows = conn.execute(
        f"""
        SELECT {_reader_metadata_select_columns('n')}
        FROM conversation_nodes n
        JOIN effective_current_nodes ec
          ON ec.conversation_id = n.conversation_id AND ec.node_id = n.node_id
        WHERE n.conversation_id = ? {output_filter}
        ORDER BY CASE WHEN ec.source = 'fallback_all' THEN 1 ELSE 0 END,
                 CASE WHEN ec.source <> 'fallback_all' THEN ec.depth END DESC,
                 CASE WHEN ec.source = 'fallback_all' THEN n.create_time IS NULL END,
                 CASE WHEN ec.source = 'fallback_all' THEN COALESCE(n.create_time, n.update_time, 0) END,
                 n.node_id
        LIMIT ? OFFSET ?
        """,
        (conversation_id, limit, offset),
    ).fetchall()
    return _hydrate_reader_rows(conn, conversation_id, rows, budget), int(total or 0)


def _page_collection_index(
    conn: sqlite3.Connection,
    conversation_id: str,
    path: str,
    node_id: str,
    *,
    include_internal: bool,
) -> tuple[int | None, int]:
    """Return node index within the exact collection used by reader pagination."""
    ensure_effective_current_views(conn, [conversation_id])
    visible_clause = "" if include_internal else f" AND {_sql_visible_message_condition('conversation_nodes')}"
    if path == "all":
        row = conn.execute(
            f"""
            WITH collection AS (
                SELECT node_id,
                       row_number() OVER (
                           ORDER BY create_time IS NULL,
                                    COALESCE(create_time, update_time, 0),
                                    node_id
                       ) - 1 AS idx
                FROM conversation_nodes
                WHERE conversation_id = ?{visible_clause}
            )
            SELECT MAX(CASE WHEN node_id = ? THEN idx END) AS idx,
                   COUNT(*) AS total
            FROM collection
            """,
            (conversation_id, node_id),
        ).fetchone()
        return (int(row["idx"]) if row["idx"] is not None else None, int(row["total"] or 0))

    output_filter = "" if include_internal else f"AND {_sql_visible_message_condition('n')}"
    row = conn.execute(
        f"""
        WITH collection AS (
            SELECT n.node_id,
                   row_number() OVER (
                       ORDER BY CASE WHEN ec.source = 'fallback_all' THEN 1 ELSE 0 END,
                                CASE WHEN ec.source <> 'fallback_all' THEN ec.depth END DESC,
                                CASE WHEN ec.source = 'fallback_all' THEN n.create_time IS NULL END,
                                CASE WHEN ec.source = 'fallback_all' THEN COALESCE(n.create_time, n.update_time, 0) END,
                                n.node_id
                   ) - 1 AS idx
            FROM conversation_nodes n
            JOIN effective_current_nodes ec
              ON ec.conversation_id = n.conversation_id AND ec.node_id = n.node_id
            WHERE n.conversation_id = ? {output_filter}
        )
        SELECT MAX(CASE WHEN node_id = ? THEN idx END) AS idx,
               COUNT(*) AS total
        FROM collection
        """,
        (conversation_id, node_id),
    ).fetchone()
    return (int(row["idx"]) if row["idx"] is not None else None, int(row["total"] or 0))


def _display_order_map(conn: sqlite3.Connection, conversation_id: str, path: str) -> dict[str, int]:
    rows = _conversation_order_rows(conn, conversation_id)
    current_row = conn.execute(
        "SELECT current_node FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    current_node = current_row["current_node"] if current_row else None
    return {row["node_id"]: index for index, row in enumerate(_order_nodes_for_display(rows, path, current_node))}


def _conversation_order_rows(conn: sqlite3.Connection, conversation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT node_id, parent_node_id, children_json, is_on_current_path,
               create_time, update_time
        FROM conversation_nodes
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchall()


def _fts_message_rows(conn: sqlite3.Connection, parsed: ParsedQuery, fts_query: str, conversation_id: str | None, limit: int | None) -> list[sqlite3.Row]:
    _ensure_search_functions(conn, parsed)
    if parsed.path == "current":
        ensure_effective_current_views(conn, [conversation_id] if conversation_id else None)
    effective_expression = _current_path_condition("n") if parsed.path == "current" else "0"
    where, params = _node_filters(parsed, conversation_id)
    limit_clause, limit_params = _limit_clause(limit)
    order_clause = "ORDER BY bm25(message_fts)" if limit is not None else ""
    return conn.execute(
        f"""
        SELECT n.conversation_id, n.node_id, n.role, n.create_time, n.update_time,
               n.content_type, n.content_text, n.is_on_current_path,
               CASE WHEN {effective_expression} THEN 1 ELSE 0 END AS effective_visible_in_current_view,
               {_bounded_scalar_projection("c.title", "title", MAX_API_TITLE_CHARS)},
               c.create_time AS conversation_create_time, c.update_time AS conversation_update_time,
               c.current_node,
               {_bounded_scalar_projection("c.source_file", "source_file", MAX_API_SOURCE_CHARS)},
               bm25(message_fts) AS bm25_score
        FROM message_fts
        JOIN conversation_nodes n
          ON n.conversation_id = message_fts.conversation_id AND n.node_id = message_fts.node_id
        JOIN conversations c ON c.conversation_id = n.conversation_id
        WHERE message_fts MATCH ? {where}
        {order_clause}
        {limit_clause}
        """,
        [fts_query] + params + limit_params,
    ).fetchall()


def _message_search_page_rows(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str | None,
    limit: int,
    offset: int,
    order: str,
    *,
    use_trigram: bool = True,
    count_total: bool = True,
    resolved_char_budget: int | None = None,
) -> tuple[list[sqlite3.Row], int]:
    if resolved_char_budget is None:
        resolved_char_budget = max(
            SEARCH_PAGE_RESOLVED_CHARS, search_exact_verify_limits()[0]
        )
    base_sql, params = _message_search_base_select(conn, parsed, conversation_id, use_trigram=use_trigram)
    order_clause = _message_search_order_clause(order, conversation_id, parsed.path)
    order_sql = f"ORDER BY {order_clause}"
    if order == "display" and conversation_id and parsed.path == "current":
        cursor = conn.execute(
            f"""
            WITH matched AS (
                {base_sql}
            )
            SELECT matched.*
            FROM matched
            JOIN effective_current_nodes effective_order
              ON effective_order.conversation_id = matched.conversation_id
             AND effective_order.node_id = matched.node_id
            {order_sql}
            """,
            params,
        )
    else:
        cursor = conn.execute(
            f"""
            SELECT *
            FROM ({base_sql}) matched
            {order_sql}
            """,
            params,
        )
    rows: list[sqlite3.Row] = []
    resolved_chars = 0
    matched_count = 0
    has_extra = False
    for row in cursor:
        matched_count += 1
        if matched_count <= offset:
            continue
        if len(rows) >= limit:
            has_extra = True
            if not count_total:
                break
            continue
        resolved_chars += len(str(row["content_text"] or ""))
        if resolved_chars > resolved_char_budget:
            cursor.close()
            raise SearchResourceLimitError("search_page_exact_materialization_limit")
        rows.append(row)
    total = matched_count if count_total else offset + len(rows) + (1 if has_extra else 0)
    return rows, int(total or 0)


def _resolved_message_matches(parsed: ParsedQuery, text: str, content_type: Any) -> bool:
    positives = [item for item in parsed.phrases + parsed.terms if item]
    positive_results = [
        _fragment_matches(text, fragment, parsed.match_mode) for fragment in positives
    ]
    if positive_results and not (
        any(positive_results) if parsed.or_mode else all(positive_results)
    ):
        return False
    if any(
        not _fragment_matches(text, fragment, parsed.match_mode)
        for fragment in parsed.required_phrases
        if fragment
    ):
        return False
    if any(
        _fragment_matches(text, fragment, parsed.match_mode)
        for fragment in parsed.exclude
        if fragment
    ):
        return False
    if (
        str(content_type or "").casefold() not in {"text", "code", "multimodal_text"}
        and _is_placeholder_text(text)
    ):
        return False
    return bool(text)


def _prepare_verified_message_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.web_verified_message_results")
    conn.execute(
        """
        CREATE TEMP TABLE web_verified_message_results (
            storage_rowid INTEGER PRIMARY KEY,
            resolved_text TEXT NOT NULL,
            bm25_score REAL,
            match_reason TEXT NOT NULL
        )
        """
    )


def _scan_verified_message_candidates(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str | None,
    search_state: dict[str, Any],
    *,
    use_trigram: bool,
    resume_after_node_id: str,
    max_verified_results: int,
    conversation_grouped: bool = False,
    resume_after_conversation_id: str = "",
    display_order: bool = False,
    resume_candidate_offset: int = 0,
) -> tuple[str, bool, str, int]:
    """Verify one deterministic logical-ID segment into a TEMP artifact."""

    source_sql, source_params, score_expr, reason = _message_match_source(
        conn, parsed, use_trigram=use_trigram
    )
    where, params = _node_filters(parsed, conversation_id)
    if display_order:
        resume_clause = ""
        resume_params: list[Any] = []
        if parsed.path == "current":
            display_join = """
                JOIN effective_current_nodes effective_candidate
                  ON effective_candidate.conversation_id = n.conversation_id
                 AND effective_candidate.node_id = n.node_id
            """
            candidate_order = """
                CASE WHEN effective_candidate.source = 'fallback_all' THEN 1 ELSE 0 END,
                CASE WHEN effective_candidate.source <> 'fallback_all'
                     THEN effective_candidate.depth END DESC,
                CASE WHEN effective_candidate.source = 'fallback_all'
                     THEN n.create_time IS NULL END,
                CASE WHEN effective_candidate.source = 'fallback_all'
                     THEN COALESCE(n.create_time, n.update_time, 0) END,
                n.node_id
            """
        else:
            display_join = ""
            candidate_order = (
                "n.create_time IS NULL, "
                "COALESCE(n.create_time, n.update_time, 0), n.node_id"
            )
        offset_sql = " OFFSET ?"
    else:
        resume_clause = (
            "AND (c.conversation_id > ? OR "
            "(c.conversation_id = ? AND n.node_id > ?))"
        )
        resume_params = [
            resume_after_conversation_id,
            resume_after_conversation_id,
            resume_after_node_id,
        ]
        display_join = ""
        candidate_order = "c.conversation_id, n.node_id"
        offset_sql = ""
    sql = f"""
        SELECT n.rowid AS storage_rowid,
               c.conversation_id AS candidate_conversation_id,
               n.node_id AS candidate_node_id,
               n.content_text IS NULL AS content_is_null,
               n.raw_message_json IS NULL AS raw_is_null,
               n.content_type,
               {_sql_search_display_text('n')} AS resolved_text,
               {score_expr} AS candidate_score
        FROM {source_sql}
        JOIN conversations c ON c.conversation_id = n.conversation_id
        {display_join}
        WHERE 1 = 1 {resume_clause} {where}
        ORDER BY {candidate_order}
        LIMIT ?{offset_sql}
    """
    query_params = source_params + resume_params + params + [SEARCH_CANDIDATE_LIMIT + 1]
    if display_order:
        query_params.append(resume_candidate_offset)
    pending_inserts: list[tuple[int, str, float | None, str]] = []
    last_processed = resume_after_node_id
    last_processed_conversation_id = resume_after_conversation_id
    interrupted = False
    verified_results = 0
    processed_candidates = 0
    deadline = time.monotonic() + SEARCH_WALL_DEADLINE_SECONDS

    def progress() -> int:
        search_state["sqlite_vm_steps"] += SEARCH_VM_PROGRESS_INTERVAL
        if time.monotonic() >= deadline:
            search_state["budget_exhausted"] = True
            search_state["budget_reason"] = "wall_deadline"
            return 1
        return 0

    conn.set_progress_handler(progress, SEARCH_VM_PROGRESS_INTERVAL)
    try:
        cursor = conn.execute(sql, query_params)
        for row in cursor:
            search_state["candidate_count"] += 1
            if search_state["candidate_count"] > SEARCH_CANDIDATE_LIMIT:
                search_state["budget_exhausted"] = True
                search_state["budget_reason"] = "candidate_count_limit"
                interrupted = True
                break
            storage_rowid = int(row["storage_rowid"])
            candidate_conversation_id = str(row["candidate_conversation_id"])
            candidate_node_id = str(row["candidate_node_id"])
            previous_processed = last_processed
            previous_processed_conversation_id = last_processed_conversation_id
            last_processed = candidate_node_id
            last_processed_conversation_id = candidate_conversation_id
            text = str(row["resolved_text"] or "")
            if storage_rowid in search_state["pending_rowids"]:
                # Stop on the first unverified row.  A continuation either
                # retries it with a fresh aggregate budget or advances the
                # bounded raw-only tier; advancing past it would make the
                # cumulative result permanently false-exact.
                last_processed = previous_processed
                last_processed_conversation_id = previous_processed_conversation_id
                search_state["budget_exhausted"] = True
                search_state["retry_pending_candidate"] = True
                if search_state["pending_reasons"]:
                    search_state["budget_reason"] = sorted(
                        search_state["pending_reasons"]
                    )[0]
                interrupted = True
                break
            processed_candidates += 1
            if _resolved_message_matches(parsed, text, row["content_type"]):
                verified_results += 1
                pending_inserts.append(
                    (
                        storage_rowid,
                        text,
                        float(row["candidate_score"])
                        if row["candidate_score"] is not None
                        else None,
                        reason,
                    )
                )
                if len(pending_inserts) >= 512:
                    conn.executemany(
                        "INSERT OR REPLACE INTO temp.web_verified_message_results "
                        "(storage_rowid, resolved_text, bm25_score, match_reason) "
                        "VALUES (?, ?, ?, ?)",
                        pending_inserts,
                    )
                    pending_inserts.clear()
                if verified_results >= max_verified_results:
                    search_state["budget_exhausted"] = True
                    search_state["budget_reason"] = "page_result_limit"
                    interrupted = True
                    break
    except sqlite3.OperationalError as exc:
        if not search_state["budget_exhausted"] or "interrupted" not in str(exc).casefold():
            raise
        interrupted = True
    finally:
        conn.set_progress_handler(None, 0)
    if pending_inserts:
        conn.executemany(
            "INSERT OR REPLACE INTO temp.web_verified_message_results "
            "(storage_rowid, resolved_text, bm25_score, match_reason) VALUES (?, ?, ?, ?)",
            pending_inserts,
        )
    return (
        last_processed,
        interrupted or bool(search_state["budget_exhausted"]),
        last_processed_conversation_id,
        resume_candidate_offset + processed_candidates
        if display_order
        else resume_candidate_offset,
    )


def _verified_message_page_rows(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str | None,
    limit: int,
    offset: int,
    order: str,
    *,
    count_total: bool,
) -> tuple[list[sqlite3.Row], int]:
    effective_expression = (
        _current_path_condition("n") if parsed.path == "current" else "0"
    )
    base_sql = f"""
        SELECT n.conversation_id, n.node_id, n.role, n.create_time, n.update_time,
               n.content_type, verified.resolved_text AS content_text, n.is_on_current_path,
               n.rowid AS storage_rowid,
               length(CAST(n.content_text AS BLOB)) AS content_storage_bytes,
               length(CAST(n.raw_message_json AS BLOB)) AS raw_storage_bytes,
               n.content_hash,
               (SELECT generation FROM archive_generations WHERE name = 'message') AS message_generation,
               n.display_revision,
               CASE WHEN {effective_expression} THEN 1 ELSE 0 END
                    AS effective_visible_in_current_view,
               {_bounded_scalar_projection('c.title', 'title', MAX_API_TITLE_CHARS)},
               c.create_time AS conversation_create_time,
               c.update_time AS conversation_update_time,
               c.current_node,
               {_bounded_scalar_projection('c.source_file', 'source_file', MAX_API_SOURCE_CHARS)},
               verified.bm25_score, verified.match_reason
        FROM temp.web_verified_message_results verified
        JOIN conversation_nodes n ON n.rowid = verified.storage_rowid
        JOIN conversations c ON c.conversation_id = n.conversation_id
    """
    order_clause = _message_search_order_clause(order, conversation_id, parsed.path)
    if order == "display" and conversation_id and parsed.path == "current":
        rows = conn.execute(
            f"""
            WITH matched AS ({base_sql})
            SELECT matched.* FROM matched
            JOIN effective_current_nodes effective_order
              ON effective_order.conversation_id = matched.conversation_id
             AND effective_order.node_id = matched.node_id
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM ({base_sql}) matched ORDER BY {order_clause} LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    verified_total = int(
        conn.execute("SELECT COUNT(*) FROM temp.web_verified_message_results").fetchone()[0]
    )
    if count_total:
        return rows, verified_total
    return rows, offset + len(rows) + (1 if verified_total > offset + len(rows) else 0)


def _bounded_search_response(result: dict[str, Any]) -> dict[str, Any]:
    """Apply the actual compact-JSON byte budget before an HTTP response exists."""

    try:
        safe_result = sanitize_json_value(result)
    except JsonSafetyLimitError as exc:
        raise SearchResourceLimitError("search_response_resource_limit_exceeded") from exc
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    serialized_bytes = 0
    for chunk in encoder.iterencode(safe_result):
        serialized_bytes += len(chunk.encode("utf-8"))
        if serialized_bytes > SEARCH_PAGE_ESTIMATED_BYTES:
            raise SearchResourceLimitError("search_response_resource_limit_exceeded")
    return safe_result


def _message_search_base_select(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str | None,
    *,
    use_trigram: bool = True,
) -> tuple[str, list[Any]]:
    source_sql, source_params, score_expr, reason = _message_match_source(conn, parsed, use_trigram=use_trigram)
    where, params = _node_filters(parsed, conversation_id)
    has_norm = _has_normalized_message_norm(conn)
    text_clause, text_params = _message_text_filter(
        parsed,
        has_norm,
        display_column="n.resolved_text",
        normalized_column="n.resolved_norm",
    )
    norm_join = """
        LEFT JOIN web_message_norm mn
          ON mn.conversation_id = n.conversation_id AND mn.node_id = n.node_id
    """ if has_norm else ""
    effective_expression = _current_path_condition("n") if parsed.path == "current" else "0"
    sql = f"""
        WITH resolved_source AS (
            SELECT n.*,
                   n.rowid AS storage_rowid,
                   length(CAST(n.content_text AS BLOB)) AS content_storage_bytes,
                   length(CAST(n.raw_message_json AS BLOB)) AS raw_storage_bytes,
                   n.display_revision,
                   (SELECT generation FROM archive_generations WHERE name = 'message') AS message_generation,
                   {_sql_search_display_text('n')} AS resolved_text,
                   {score_expr} AS candidate_score,
                   {f"COALESCE(mn.content_norm, web_norm({_sql_search_display_text('n')}))" if has_norm else "NULL"} AS resolved_norm,
                   {_bounded_scalar_projection("c.title", "conversation_title", MAX_API_TITLE_CHARS)},
                   c.create_time AS conversation_create_time,
                   c.update_time AS conversation_update_time,
                   c.current_node AS conversation_current_node,
                   {_bounded_scalar_projection("c.source_file", "conversation_source_file", MAX_API_SOURCE_CHARS)}
            FROM {source_sql}
            JOIN conversations c ON c.conversation_id = n.conversation_id
            {norm_join}
            WHERE 1 = 1 {where}
            LIMIT -1 OFFSET 0
        )
        SELECT n.conversation_id, n.node_id, n.role, n.create_time, n.update_time,
               n.content_type, n.resolved_text AS content_text, n.is_on_current_path,
               n.storage_rowid, n.content_storage_bytes, n.raw_storage_bytes,
               n.content_hash, n.message_generation,
               n.display_revision,
               CASE WHEN {effective_expression} THEN 1 ELSE 0 END AS effective_visible_in_current_view,
               n.conversation_title AS title, n.conversation_create_time, n.conversation_update_time,
               n.conversation_current_node AS current_node, n.conversation_source_file AS source_file,
               n.candidate_score AS bm25_score, ? AS match_reason
        FROM resolved_source n
        WHERE n.resolved_text <> '' {text_clause}
    """
    return sql, source_params + params + [reason] + text_params


def _message_match_source(conn: sqlite3.Connection, parsed: ParsedQuery, *, use_trigram: bool) -> tuple[str, list[Any], str, str]:
    trigram_query, _is_complete = _candidate_query(parsed.phrases + parsed.terms, parsed.required_phrases, parsed.or_mode)
    if (
        use_trigram
        and trigram_query
        and _has_normalized_message_trigram(conn)
    ):
        if _table_has_columns(conn, "web_message_trigram", {"conversation_id", "node_id"}):
            return (
                """
                (
                    SELECT conversation_id, node_id, rank AS fts_rank
                    FROM web_message_trigram
                    WHERE web_message_trigram MATCH ?
                    UNION ALL
                    SELECT conversation_id, node_id, NULL AS fts_rank
                    FROM web_index_oversized oversized
                    WHERE kind = 'message'
                      AND NOT EXISTS (
                          SELECT 1 FROM web_message_norm normalized
                          WHERE normalized.conversation_id = oversized.conversation_id
                            AND normalized.node_id = oversized.node_id
                      )
                ) mk
                JOIN conversation_nodes n
                  ON n.conversation_id = mk.conversation_id AND n.node_id = mk.node_id
                """,
                [trigram_query],
                "mk.fts_rank",
                "exact phrase" if (parsed.phrases or parsed.required_phrases) else "substring",
            )
        if _table_has_columns(conn, "web_message_norm", {"stable_id"}):
            return (
                """
                (
                    SELECT normalized.conversation_id,
                           normalized.node_id,
                           trigram.rank AS fts_rank
                    FROM web_message_trigram AS trigram
                    JOIN web_message_norm AS normalized
                      ON normalized.stable_id = trigram.rowid
                    WHERE web_message_trigram MATCH ?
                    UNION ALL
                    SELECT oversized.conversation_id,
                           oversized.node_id,
                           NULL AS fts_rank
                    FROM web_index_oversized AS oversized
                    WHERE oversized.kind = 'message'
                ) mk
                JOIN conversation_nodes n
                  ON n.conversation_id = mk.conversation_id
                 AND n.node_id = mk.node_id
                """,
                [trigram_query],
                "mk.fts_rank",
                "exact phrase" if (parsed.phrases or parsed.required_phrases) else "substring",
            )
        return (
            """
            (
                SELECT rowid AS node_rowid, rank AS fts_rank
                FROM web_message_trigram
                WHERE web_message_trigram MATCH ?
                UNION ALL
                SELECT source_rowid AS node_rowid, NULL AS fts_rank
                FROM web_index_oversized oversized
                WHERE kind = 'message'
                  AND NOT EXISTS (
                      SELECT 1 FROM web_message_norm normalized
                      WHERE normalized.conversation_id = oversized.conversation_id
                        AND normalized.node_id = oversized.node_id
                  )
            ) mk
            JOIN conversation_nodes n ON n.rowid = mk.node_rowid
            """,
            [trigram_query],
            "mk.fts_rank",
            "exact phrase" if (parsed.phrases or parsed.required_phrases) else "substring",
        )
    if not trigram_query:
        return "conversation_nodes n", [], "NULL", "substring"
    if not use_trigram:
        return "conversation_nodes n", [], "NULL", "substring"
    if parsed.match_mode == "word" and not _fts_candidates_are_safe(parsed):
        return "conversation_nodes n", [], "NULL", "substring"
    if not _raw_candidate_indexes_are_safe(conn):
        return "conversation_nodes n", [], "NULL", "substring"
    fts_query = build_fts_query(parsed)
    if fts_query and _table_exists(conn, "message_fts"):
        return (
            """
            (
                SELECT conversation_id, node_id, rank AS fts_rank
                FROM message_fts
                WHERE message_fts MATCH ?
            ) mf
            JOIN conversation_nodes n
              ON n.conversation_id = mf.conversation_id AND n.node_id = mf.node_id
            """,
            [fts_query],
            "mf.fts_rank",
            "fts",
        )
    return "conversation_nodes n", [], "NULL", "substring"


def _fts_candidates_are_safe(parsed: ParsedQuery) -> bool:
    if parsed.match_mode != "word":
        return True
    fragments = parsed.phrases + parsed.terms + parsed.required_phrases
    return all(_uses_word_boundaries(normalize_search_text(fragment)) for fragment in fragments if fragment)


def _raw_candidate_indexes_are_safe(conn: sqlite3.Connection) -> bool:
    """Raw FTS candidates can miss NFKC-equivalent text; normalized trigram is the safe fast path."""
    return _has_normalized_message_trigram(conn)


def _message_search_order_clause(order: str, conversation_id: str | None, path: str) -> str:
    if order == "display" and conversation_id:
        if path == "current":
            return """
                CASE WHEN effective_order.source = 'fallback_all' THEN 1 ELSE 0 END,
                CASE WHEN effective_order.source <> 'fallback_all' THEN effective_order.depth END DESC,
                CASE WHEN effective_order.source = 'fallback_all' THEN matched.create_time IS NULL END,
                CASE WHEN effective_order.source = 'fallback_all' THEN COALESCE(matched.create_time, matched.update_time, 0) END,
                matched.node_id ASC
            """
        return "matched.create_time IS NULL, COALESCE(matched.create_time, matched.update_time, 0) ASC, matched.node_id ASC"
    return """
        COALESCE(matched.bm25_score, 0) ASC,
        COALESCE(matched.conversation_update_time, matched.conversation_create_time, 0) DESC,
        matched.conversation_id ASC,
        matched.create_time ASC,
        matched.node_id ASC
    """


def _substring_message_rows(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str | None,
    limit: int | None,
    *,
    use_trigram: bool = True,
) -> list[sqlite3.Row]:
    limit_clause, limit_params = _limit_clause(limit)
    base_sql, params = _message_search_base_select(conn, parsed, conversation_id, use_trigram=use_trigram)
    return conn.execute(
        f"""
        SELECT *
        FROM ({base_sql}) matched
        ORDER BY COALESCE(matched.conversation_update_time, matched.conversation_create_time, 0) DESC,
                 matched.create_time ASC,
                 matched.node_id ASC
        {limit_clause}
        """,
        params + limit_params,
    ).fetchall()


def _title_rows(conn: sqlite3.Connection, parsed: ParsedQuery, limit: int | None, *, use_trigram: bool = True) -> list[sqlite3.Row]:
    _ensure_search_functions(conn, parsed)
    fragments = ([parsed.title] if parsed.title else []) + parsed.phrases + parsed.terms
    if not fragments:
        fragments = [""]
    where, params = _conversation_time_where(parsed.after, parsed.before)
    has_norm = _has_normalized_title_norm(conn)
    positive_clauses = []
    filter_clauses = []
    for frag in fragments:
        if not frag and parsed.has_non_time_filters():
            continue
        if parsed.match_mode == "word":
            column = "COALESCE(tn.title_norm, web_norm(COALESCE(c.title, '')))" if has_norm else "COALESCE(c.title, '')"
            positive_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            positive_clauses.append("instr(COALESCE(tn.title_norm, web_norm(COALESCE(c.title, ''))), ?) > 0")
            params.append(normalize_search_text(frag))
        else:
            positive_clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    if parsed.source:
        filter_clauses.append("web_search_match(COALESCE(c.source_file, ''), ?, 'contains') > 0")
        params.append(parsed.source)
    for frag in ([parsed.required_title] if parsed.required_title else []) + parsed.required_phrases:
        if not frag:
            continue
        if parsed.match_mode == "word":
            column = "COALESCE(tn.title_norm, web_norm(COALESCE(c.title, '')))" if has_norm else "COALESCE(c.title, '')"
            filter_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            filter_clauses.append("instr(COALESCE(tn.title_norm, web_norm(COALESCE(c.title, ''))), ?) > 0")
            params.append(normalize_search_text(frag))
        else:
            filter_clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    trigram_clause, trigram_params = _title_trigram_clause(conn, parsed, use_trigram)
    if trigram_clause:
        filter_clauses.append(trigram_clause)
        params.extend(trigram_params)
    for frag in parsed.exclude:
        if parsed.match_mode == "word":
            column = "COALESCE(tn.title_norm, web_norm(COALESCE(c.title, '')))" if has_norm else "COALESCE(c.title, '')"
            filter_clauses.append(f"web_search_match({column}, ?, ?) = 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            filter_clauses.append("instr(COALESCE(tn.title_norm, web_norm(COALESCE(c.title, ''))), ?) = 0")
            params.append(normalize_search_text(frag))
        else:
            filter_clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) = 0")
            params.extend([frag, parsed.match_mode])
    clauses = []
    if positive_clauses:
        clauses.append("(" + (" OR ".join(positive_clauses) if parsed.or_mode else " AND ".join(positive_clauses)) + ")")
    clauses.extend(filter_clauses)
    if not clauses:
        clauses.append("1 = 1")
    where += (" AND " if where else "WHERE ") + " AND ".join(clauses)
    norm_join = "LEFT JOIN web_title_norm tn ON tn.conversation_id = c.conversation_id" if has_norm else ""
    limit_clause, limit_params = _limit_clause(limit)
    return conn.execute(
        f"""
        SELECT {_conversation_api_columns("c")}
        FROM conversations c
        {norm_join}
        {where}
        ORDER BY COALESCE(c.update_time, c.create_time, 0) DESC
        {limit_clause}
        """,
        params + limit_params,
    ).fetchall()


def _conversation_search_page(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    limit: int,
    offset: int,
    sort: str,
    *,
    use_trigram: bool = True,
    verified_messages: bool = False,
    include_title_matches: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    if not _search_has_positive_body_or_title(parsed):
        return _filter_conversation_page(conn, parsed, limit, offset, sort)
    parts: list[str] = []
    params: list[Any] = []
    has_message_match = bool(parsed.terms or parsed.phrases or parsed.required_phrases or parsed.role)
    if parsed.scope != "title" and has_message_match:
        if verified_messages and (parsed.terms or parsed.phrases or parsed.required_phrases):
            message_sql, message_params = _verified_message_conversation_select()
        else:
            message_sql, message_params = _message_conversation_select(conn, parsed, use_trigram=use_trigram)
        parts.append(message_sql)
        params.extend(message_params)
    if include_title_matches and parsed.scope != "message" and not parsed.role:
        title_sql, title_params = _title_conversation_select(conn, parsed, use_trigram=use_trigram)
        parts.append(title_sql)
        params.extend(title_params)
    if not parts:
        return [], 0
    combined = " UNION ALL ".join(parts)
    exclude_filter, exclude_params = _conversation_level_exclude_filter(parsed)
    params.extend(exclude_params)
    order = _conversation_search_order(sort)
    base = f"""
        WITH raw_matches AS (
            {combined}
        ),
        grouped AS (
            SELECT conversation_id,
                   SUM(hit_count) AS hit_count,
                   SUM(score) AS score,
                   MAX(message_match) AS message_match,
                   MAX(title_match) AS title_match
            FROM raw_matches
            GROUP BY conversation_id
        ),
        filtered AS (
            SELECT {_conversation_api_columns("c")},
                   grouped.hit_count, grouped.score, grouped.message_match, grouped.title_match
            FROM grouped
            JOIN conversations c ON c.conversation_id = grouped.conversation_id
            {exclude_filter}
        )
        SELECT filtered.*, COUNT(*) OVER() AS total_rows
        FROM filtered
    """
    rows = conn.execute(f"{base} ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    total = rows[0]["total_rows"] if rows else 0
    if not rows and offset:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM ({base})", params).fetchone()["c"]
    items = []
    for row in rows:
        reasons = []
        if row["message_match"]:
            reasons.append("message match")
        if row["title_match"]:
            reasons.append("title match")
        items.append(
            {
                "conversation_id": row["conversation_id"],
                **_conversation_scalar_fields(row),
                "create_time": row["create_time"],
                "update_time": row["update_time"],
                "current_node": row["current_node"],
                "hit_count": int(row["hit_count"] or 0),
                "snippets": [],
                "reasons": reasons,
                "score": float(row["score"] or 0),
                "message_match": bool(row["message_match"]),
                "title_match": bool(row["title_match"]),
            }
        )
    return items, int(total or 0)


def _filter_conversation_page(conn: sqlite3.Connection, parsed: ParsedQuery, limit: int, offset: int, sort: str) -> tuple[list[dict[str, Any]], int]:
    where, params = _filter_conversation_where(parsed)
    order = _conversation_search_order(sort)
    rows = conn.execute(
        f"""
        SELECT {_conversation_api_columns("c")},
               1.0 AS score,
               COUNT(*) OVER() AS total_rows
        FROM conversations c
        {where}
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    total = rows[0]["total_rows"] if rows else 0
    if not rows and offset:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM conversations c {where}", params).fetchone()["c"]
    return [_filter_conversation_summary(row, parsed) for row in rows], int(total or 0)


def _filter_conversation_item(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> dict[str, Any] | None:
    where, params = _filter_conversation_where(parsed, conversation_id=conversation_id)
    row = conn.execute(
        f"""
        SELECT {_conversation_api_columns("c")}
        FROM conversations c
        {where}
        LIMIT 1
        """,
        params,
    ).fetchone()
    return _filter_conversation_summary(row, parsed) if row else None


def _filter_conversation_summary(row: sqlite3.Row, parsed: ParsedQuery) -> dict[str, Any]:
    return {
        "conversation_id": row["conversation_id"],
        **_conversation_scalar_fields(row),
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "current_node": row["current_node"],
        "hit_count": 0,
        "snippets": [],
        "reasons": _filter_reasons(parsed),
        "score": 1.0,
        "message_match": False,
        "title_match": False,
        "has_title_hits": False,
        "has_internal_hits": False,
        "has_branch_hits": False,
    }


def _filter_reasons(parsed: ParsedQuery) -> list[str]:
    reasons: list[str] = []
    if parsed.source:
        reasons.append("source match")
    if parsed.role:
        reasons.append("role filter")
    if parsed.after is not None or parsed.before is not None:
        reasons.append("date filter")
    if parsed.exclude:
        reasons.append("exclude filter")
    return reasons or ["filter match"]


def _filter_conversation_where(parsed: ParsedQuery, *, conversation_id: str | None = None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if conversation_id:
        clauses.append("c.conversation_id = ?")
        params.append(conversation_id)
    if parsed.after is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) >= ?")
        params.append(parsed.after)
    if parsed.before is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) < ?")
        params.append(parsed.before)
    if parsed.source:
        clauses.append("web_search_match(COALESCE(c.source_file, ''), ?, 'contains') > 0")
        params.append(parsed.source)
    if parsed.role:
        roles = _role_filter_values(parsed.role)
        role_path_clause = f"AND {_current_path_condition('rn')}" if parsed.path == "current" else ""
        clauses.append((
            """
            EXISTS (
                SELECT 1
                FROM conversation_nodes rn
                WHERE rn.conversation_id = c.conversation_id
                  {role_path_clause}
                  AND """ + _sql_canonical_role("rn") + """ IN (""" + ",".join("?" for _ in roles) + """)
            )
            """
        ).replace("{role_path_clause}", role_path_clause))
        params.extend(roles)
    for frag in parsed.exclude:
        exclude_sql, exclude_params = _conversation_level_exclude_clauses(parsed, frag)
        clauses.extend(exclude_sql)
        params.extend(exclude_params)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _conversation_search_item(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str,
    *,
    verified_messages: bool = False,
) -> dict[str, Any] | None:
    try:
        return _conversation_search_item_inner(
            conn,
            parsed,
            conversation_id,
            use_trigram=True,
            verified_messages=verified_messages,
        )
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        return _conversation_search_item_inner(
            conn,
            parsed,
            conversation_id,
            use_trigram=False,
            verified_messages=verified_messages,
        )


def _conversation_search_item_inner(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str,
    *,
    use_trigram: bool,
    verified_messages: bool = False,
) -> dict[str, Any] | None:
    if not _search_has_positive_body_or_title(parsed):
        return _filter_conversation_item(conn, parsed, conversation_id)
    parts: list[str] = []
    params: list[Any] = []
    has_message_match = bool(parsed.terms or parsed.phrases or parsed.required_phrases or parsed.role)
    if parsed.scope != "title" and has_message_match:
        if verified_messages and (parsed.terms or parsed.phrases or parsed.required_phrases):
            message_sql, message_params = _verified_message_conversation_select(conversation_id)
        else:
            message_sql, message_params = _message_conversation_select(conn, parsed, use_trigram=use_trigram, conversation_id=conversation_id)
        parts.append(message_sql)
        params.extend(message_params)
    if parsed.scope != "message" and not parsed.role:
        title_sql, title_params = _title_conversation_select(conn, parsed, use_trigram=use_trigram, conversation_id=conversation_id)
        parts.append(title_sql)
        params.extend(title_params)
    if not parts:
        return None
    combined = " UNION ALL ".join(parts)
    exclude_filter, exclude_params = _conversation_level_exclude_filter(parsed)
    params.extend(exclude_params)
    row = conn.execute(
        f"""
        WITH raw_matches AS (
            {combined}
        ),
        grouped AS (
            SELECT conversation_id,
                   SUM(hit_count) AS hit_count,
                   SUM(score) AS score,
                   MAX(message_match) AS message_match,
                   MAX(title_match) AS title_match
            FROM raw_matches
            GROUP BY conversation_id
        )
        SELECT {_conversation_api_columns("c")},
               grouped.hit_count, grouped.score, grouped.message_match, grouped.title_match
        FROM grouped
        JOIN conversations c ON c.conversation_id = grouped.conversation_id
        {exclude_filter}
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    reasons = []
    if row["message_match"]:
        reasons.append("message match")
    if row["title_match"]:
        reasons.append("title match")
    return {
        "conversation_id": row["conversation_id"],
        **_conversation_scalar_fields(row),
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "current_node": row["current_node"],
        "hit_count": int(row["hit_count"] or 0),
        "snippets": [],
        "reasons": reasons,
        "score": float(row["score"] or 0),
        "message_match": bool(row["message_match"]),
        "title_match": bool(row["title_match"]),
    }


def _message_conversation_select(conn: sqlite3.Connection, parsed: ParsedQuery, *, use_trigram: bool, conversation_id: str | None = None) -> tuple[str, list[Any]]:
    source_sql, source_params, score_expr, _reason = _message_match_source(conn, parsed, use_trigram=use_trigram)
    where, params = _node_filters(parsed, conversation_id)
    has_norm = _has_normalized_message_norm(conn)
    text_clause, text_params = _message_text_filter(
        parsed,
        has_norm,
        display_column="n.resolved_text",
        normalized_column="n.resolved_norm",
    )
    norm_join = """
        LEFT JOIN web_message_norm mn
          ON mn.conversation_id = n.conversation_id AND mn.node_id = n.node_id
    """ if has_norm else ""
    return (
        f"""
        WITH resolved_source AS (
            SELECT n.*,
                   {_sql_search_display_text('n')} AS resolved_text,
                   {score_expr} AS candidate_score,
                   {f"COALESCE(mn.content_norm, web_norm({_sql_search_display_text('n')}))" if has_norm else "NULL"} AS resolved_norm
            FROM {source_sql}
            JOIN conversations c ON c.conversation_id = n.conversation_id
            {norm_join}
            WHERE 1 = 1 {where}
            LIMIT -1 OFFSET 0
        )
        SELECT n.conversation_id,
               COUNT(*) AS hit_count,
               COUNT(*) * 10.0
                   + MAX(CASE WHEN n.candidate_score IS NULL THEN 0.0 ELSE 25.0 - min(25.0, abs(n.candidate_score)) END) AS score,
               1 AS message_match,
               0 AS title_match
        FROM resolved_source n
        WHERE n.resolved_text <> '' {text_clause}
        GROUP BY n.conversation_id
        """,
        source_params + params + text_params,
    )


def _verified_message_conversation_select(
    conversation_id: str | None = None,
) -> tuple[str, list[Any]]:
    """Aggregate the exact row-level artifact without invoking the resolver again."""

    where = "WHERE n.conversation_id = ?" if conversation_id is not None else ""
    params: list[Any] = [conversation_id] if conversation_id is not None else []
    return (
        f"""
        SELECT n.conversation_id,
               COUNT(*) AS hit_count,
               COUNT(*) * 10.0
                   + MAX(CASE WHEN verified.bm25_score IS NULL THEN 0.0
                              ELSE 25.0 - min(25.0, abs(verified.bm25_score)) END) AS score,
               1 AS message_match,
               0 AS title_match
        FROM temp.web_verified_message_results verified
        JOIN conversation_nodes n ON n.rowid = verified.storage_rowid
        {where}
        GROUP BY n.conversation_id
        """,
        params,
    )


def _title_conversation_select(conn: sqlite3.Connection, parsed: ParsedQuery, *, use_trigram: bool, conversation_id: str | None = None) -> tuple[str, list[Any]]:
    where, params = _conversation_time_where(parsed.after, parsed.before)
    has_norm = _has_normalized_title_norm(conn)
    clauses, clause_params = _title_filter_clauses(parsed, has_norm)
    params.extend(clause_params)
    source_sql = "conversations c"
    source_params: list[Any] = []
    trigram_query, _is_complete = _candidate_query(
        ([parsed.title] if parsed.title else []) + parsed.phrases + parsed.terms,
        ([parsed.required_title] if parsed.required_title else []) + parsed.required_phrases,
        parsed.or_mode,
    )
    if (
        use_trigram
        and trigram_query
        and _has_normalized_title_trigram(conn)
    ):
        if _table_has_columns(conn, "web_title_trigram", {"conversation_id"}):
            source_sql = """
                (
                    SELECT conversation_id, rank AS title_rank
                    FROM web_title_trigram
                    WHERE web_title_trigram MATCH ?
                    UNION ALL
                    SELECT conversation_id, NULL AS title_rank
                    FROM web_index_oversized WHERE kind = 'title'
                ) tk
                JOIN conversations c ON c.conversation_id = tk.conversation_id
            """
        elif _table_has_columns(conn, "web_title_norm", {"stable_id"}):
            source_sql = """
                (
                    SELECT normalized.conversation_id, trigram.rank AS title_rank
                    FROM web_title_trigram AS trigram
                    JOIN web_title_norm AS normalized
                      ON normalized.stable_id = trigram.rowid
                    WHERE web_title_trigram MATCH ?
                    UNION ALL
                    SELECT conversation_id, NULL AS title_rank
                    FROM web_index_oversized WHERE kind = 'title'
                ) tk
                JOIN conversations c ON c.conversation_id = tk.conversation_id
            """
        else:
            source_sql = """
                (
                    SELECT rowid AS conversation_rowid, rank AS title_rank
                    FROM web_title_trigram
                    WHERE web_title_trigram MATCH ?
                    UNION ALL
                    SELECT source_rowid AS conversation_rowid, NULL AS title_rank
                    FROM web_index_oversized WHERE kind = 'title'
                ) tk
                JOIN conversations c ON c.rowid = tk.conversation_rowid
            """
        source_params.append(trigram_query)
    if conversation_id:
        clauses.append("c.conversation_id = ?")
        params.append(conversation_id)
    if clauses:
        where += (" AND " if where else "WHERE ") + " AND ".join(clauses)
    norm_join = "LEFT JOIN web_title_norm tn ON tn.conversation_id = c.conversation_id" if has_norm else ""
    return (
        f"""
        SELECT c.conversation_id,
               0 AS hit_count,
               60.0 AS score,
               0 AS message_match,
               1 AS title_match
        FROM {source_sql}
        {norm_join}
        {where}
        """,
        source_params + params,
    )


def _conversation_snippets(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> list[dict[str, Any]]:
    if parsed.scope == "title":
        return []
    fallback_map = _fallback_map_for_conversations(conn, [conversation_id])
    current_path_fallback_to_all = fallback_map.get(conversation_id, False)
    try:
        rows = _substring_message_rows(conn, parsed, conversation_id, 3)
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        rows = _substring_message_rows(conn, parsed, conversation_id, 3, use_trigram=False)
    snippets = []
    for row in rows[:3]:
        snippets.append(
            {
                "node_id": row["node_id"],
                "role": row["role"],
                "content_type": row["content_type"],
                "snippet": make_snippet(row["content_text"] or "", _highlight_terms(parsed), parsed.match_mode),
                "is_on_current_path": bool(row["is_on_current_path"]),
                "current_path_fallback_to_all": current_path_fallback_to_all,
                "effective_visible_in_current_view": bool(row["effective_visible_in_current_view"]),
                "is_internal": _is_internal_message(row["role"], row["content_type"], row["content_text"]),
            }
        )
    return snippets


def _batch_conversation_enrichment(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    items: list[dict[str, Any]],
    *,
    verified_messages: bool = False,
) -> None:
    """Populate snippets and hit visibility with one query for the current page."""

    for item in items:
        item["snippets"] = []
        item["has_title_hits"] = bool(item.get("title_match"))
        item["has_internal_hits"] = False
        item["has_branch_hits"] = False
        item["enrichment_partial"] = False
    wanted = [item for item in items if item.get("message_match") and item.get("hit_count")]
    if not wanted or parsed.scope == "title" or not (parsed.terms or parsed.phrases or parsed.required_phrases):
        return
    ids = [item["conversation_id"] for item in wanted]
    placeholders = ",".join("?" for _ in ids)
    per_conversation_limit = max(3, SEARCH_ENRICHMENT_MATCH_LIMIT // max(1, len(ids)))

    def enrichment_sql(base_sql: str) -> str:
        return f"""
            WITH matched AS ({base_sql}),
            fair_ranked AS (
                SELECT matched.*,
                       MAX(CASE WHEN {_sql_internal_content_condition('matched')} THEN 1 ELSE 0 END)
                           OVER (PARTITION BY matched.conversation_id) AS any_internal_hit,
                       MAX(CASE WHEN NOT matched.effective_visible_in_current_view THEN 1 ELSE 0 END)
                           OVER (PARTITION BY matched.conversation_id) AS any_branch_hit,
                       row_number() OVER (
                           PARTITION BY matched.conversation_id
                           ORDER BY matched.create_time IS NULL,
                                    COALESCE(matched.create_time, matched.update_time, 0),
                                    matched.node_id
                       ) AS conversation_rank
                FROM matched
                WHERE matched.conversation_id IN ({placeholders})
            ),
            limited_matches AS (
                SELECT fair_ranked.*, COUNT(*) OVER () AS limited_match_count
                FROM fair_ranked
                WHERE conversation_rank <= ?
                ORDER BY conversation_rank, conversation_id, node_id
                LIMIT ?
            )
            SELECT * FROM limited_matches
            WHERE conversation_rank <= 3
            ORDER BY conversation_id, conversation_rank
        """
    if verified_messages:
        effective_expression = (
            _current_path_condition("n") if parsed.path == "current" else "0"
        )
        base_sql = f"""
            SELECT n.conversation_id, n.node_id, n.role, n.content_type,
                   verified.resolved_text AS content_text,
                   n.create_time, n.update_time, n.is_on_current_path,
                   CASE WHEN {effective_expression} THEN 1 ELSE 0 END
                       AS effective_visible_in_current_view
            FROM temp.web_verified_message_results verified
            JOIN conversation_nodes n ON n.rowid = verified.storage_rowid
        """
        rows = conn.execute(
            enrichment_sql(base_sql),
            ids + [per_conversation_limit, SEARCH_ENRICHMENT_MATCH_LIMIT + 1],
        ).fetchall()
    else:
        try:
            base_sql, params = _message_search_base_select(conn, parsed, None, use_trigram=True)
            rows = conn.execute(
                enrichment_sql(base_sql),
                params + ids + [per_conversation_limit, SEARCH_ENRICHMENT_MATCH_LIMIT + 1],
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if not is_optional_search_capability_missing(exc):
                raise
            base_sql, params = _message_search_base_select(conn, parsed, None, use_trigram=False)
            rows = conn.execute(
                enrichment_sql(base_sql),
                params + ids + [per_conversation_limit, SEARCH_ENRICHMENT_MATCH_LIMIT + 1],
            ).fetchall()
    by_id = {item["conversation_id"]: item for item in wanted}
    for item in wanted:
        item["enrichment_partial"] = int(item.get("hit_count") or 0) > per_conversation_limit
    for row in rows:
        item = by_id.get(row["conversation_id"])
        if item is None:
            continue
        internal = _is_internal_message(row["role"], row["content_type"], row["content_text"])
        effective_visible = bool(row["effective_visible_in_current_view"])
        item["has_internal_hits"] = bool(item["has_internal_hits"] or row["any_internal_hit"])
        item["has_branch_hits"] = bool(item["has_branch_hits"] or row["any_branch_hit"])
        if int(row["conversation_rank"] or 0) <= 3:
            item["snippets"].append(
                {
                    "node_id": row["node_id"],
                    "role": row["role"],
                    "content_type": row["content_type"],
                    "snippet": make_snippet(row["content_text"] or "", _highlight_terms(parsed), parsed.match_mode),
                    "is_on_current_path": bool(row["is_on_current_path"]),
                    "current_path_fallback_to_all": bool(item.get("current_path_fallback_to_all")),
                    "effective_visible_in_current_view": effective_visible,
                    "is_internal": internal,
                }
            )


def _add_conversation_visibility_metadata(conn: sqlite3.Connection, parsed: ParsedQuery, item: dict[str, Any]) -> None:
    item["has_title_hits"] = bool(item.get("title_match"))
    item["has_internal_hits"] = False
    item["has_branch_hits"] = False
    if not item.get("message_match"):
        return
    try:
        flags = _conversation_message_visibility_flags(conn, parsed, item["conversation_id"])
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        flags = _conversation_message_visibility_flags(conn, parsed, item["conversation_id"], use_trigram=False)
    item.update(flags)


def _conversation_message_visibility_flags(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str,
    *,
    use_trigram: bool = True,
) -> dict[str, bool]:
    if parsed.scope == "title" or not (parsed.terms or parsed.phrases or parsed.required_phrases):
        return {"has_internal_hits": False, "has_branch_hits": False}
    base_sql, params = _message_search_base_select(conn, parsed, conversation_id, use_trigram=use_trigram)
    row = conn.execute(
        f"""
        SELECT
            MAX(CASE
                WHEN {_sql_internal_content_condition('matched')}
                THEN 1 ELSE 0 END) AS has_internal_hits,
            MAX(CASE
                WHEN effective_visible_in_current_view = 0
                THEN 1 ELSE 0 END) AS has_branch_hits
        FROM ({base_sql}) matched
        """,
        params,
    ).fetchone()
    return {
        "has_internal_hits": bool(row and row["has_internal_hits"]),
        "has_branch_hits": bool(row and row["has_branch_hits"]),
    }


def _conversation_search_contains(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    conversation_id: str,
    *,
    verified_messages: bool = False,
) -> bool:
    row = conn.execute("SELECT 1 FROM conversations WHERE conversation_id = ? LIMIT 1", (conversation_id,)).fetchone()
    return row is not None and _conversation_search_item(
        conn,
        parsed,
        conversation_id,
        verified_messages=verified_messages,
    ) is not None


def _conversation_id_matches(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> bool:
    if parsed.scope != "title" and (parsed.terms or parsed.phrases or parsed.required_phrases or parsed.role):
        message_sql, message_params = _message_conversation_select(conn, parsed, use_trigram=True, conversation_id=conversation_id)
        row = conn.execute(f"SELECT 1 FROM ({message_sql}) LIMIT 1", message_params).fetchone()
        if row:
            return True
    if parsed.scope != "message" and not parsed.role:
        title_sql, title_params = _title_conversation_select(conn, parsed, use_trigram=True, conversation_id=conversation_id)
        row = conn.execute(f"SELECT 1 FROM ({title_sql}) LIMIT 1", title_params).fetchone()
        return row is not None
    return False


def _conversation_search_order(sort: str) -> str:
    if sort in {"newest", "updated"}:
        return "COALESCE(update_time, create_time, 0) DESC, title, conversation_id"
    if sort == "oldest":
        return "COALESCE(create_time, update_time, 0) ASC, title, conversation_id"
    if sort == "created":
        return "COALESCE(create_time, update_time, 0) DESC, title, conversation_id"
    if sort == "title":
        return "LOWER(COALESCE(title, '')) ASC, conversation_id"
    return "score DESC, COALESCE(update_time, create_time, 0) DESC, conversation_id"


def _message_text_filter(
    parsed: ParsedQuery,
    has_norm: bool,
    *,
    display_column: str = "",
    normalized_column: str = "mn.content_norm",
) -> tuple[str, list[Any]]:
    display_expression = display_column or _sql_display_text("n")
    params: list[Any] = []
    fragments = parsed.phrases + parsed.terms
    positive_clauses = []
    required_clauses = []
    exclude_clauses = []
    for frag in fragments:
        if not frag:
            continue
        norm = normalize_search_text(frag)
        if parsed.match_mode == "word":
            column = normalized_column if has_norm else display_expression
            positive_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([norm if has_norm else frag, parsed.match_mode])
        elif has_norm:
            positive_clauses.append(f"instr({normalized_column}, ?) > 0")
            params.append(norm)
        else:
            positive_clauses.append(f"web_search_match({display_expression}, ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    for frag in parsed.required_phrases:
        if not frag:
            continue
        norm = normalize_search_text(frag)
        if parsed.match_mode == "word":
            column = normalized_column if has_norm else display_expression
            required_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([norm if has_norm else frag, parsed.match_mode])
        elif has_norm:
            required_clauses.append(f"instr({normalized_column}, ?) > 0")
            params.append(norm)
        else:
            required_clauses.append(f"web_search_match({display_expression}, ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    for frag in parsed.exclude:
        norm = normalize_search_text(frag)
        if parsed.match_mode == "word":
            column = normalized_column if has_norm else display_expression
            exclude_clauses.append(f"web_search_match({column}, ?, ?) = 0")
            params.extend([norm if has_norm else frag, parsed.match_mode])
        elif has_norm:
            exclude_clauses.append(f"instr({normalized_column}, ?) = 0")
            params.append(norm)
        else:
            exclude_clauses.append(f"web_search_match({display_expression}, ?, ?) = 0")
            params.extend([frag, parsed.match_mode])
    clauses = []
    if positive_clauses:
        clauses.append("(" + (" OR ".join(positive_clauses) if parsed.or_mode else " AND ".join(positive_clauses)) + ")")
    clauses.extend(required_clauses)
    clauses.extend(exclude_clauses)
    clauses.append(
        f"NOT (lower(COALESCE(n.content_type, '')) NOT IN ('text', 'code', 'multimodal_text') "
        f"AND (trim({display_expression}) LIKE '[non-text content:%' "
        f"OR trim({display_expression}) LIKE '[non-text part:%'))"
    )
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _title_filter_clauses(parsed: ParsedQuery, has_norm: bool) -> tuple[list[str], list[Any]]:
    fragments = ([parsed.title] if parsed.title else []) + parsed.phrases + parsed.terms
    if not fragments:
        fragments = [""]
    params: list[Any] = []
    positive_clauses = []
    clauses = []
    for frag in fragments:
        if not frag and parsed.has_non_time_filters():
            continue
        if parsed.match_mode == "word":
            column = "COALESCE(tn.title_norm, web_norm(COALESCE(c.title, '')))" if has_norm else "COALESCE(c.title, '')"
            positive_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            positive_clauses.append("instr(COALESCE(tn.title_norm, web_norm(COALESCE(c.title, ''))), ?) > 0")
            params.append(normalize_search_text(frag))
        else:
            positive_clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    if positive_clauses:
        clauses.append("(" + (" OR ".join(positive_clauses) if parsed.or_mode else " AND ".join(positive_clauses)) + ")")
    for frag in ([parsed.required_title] if parsed.required_title else []) + parsed.required_phrases:
        if not frag:
            continue
        if parsed.match_mode == "word":
            column = "COALESCE(tn.title_norm, web_norm(COALESCE(c.title, '')))" if has_norm else "COALESCE(c.title, '')"
            clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            clauses.append("instr(COALESCE(tn.title_norm, web_norm(COALESCE(c.title, ''))), ?) > 0")
            params.append(normalize_search_text(frag))
        else:
            clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    if parsed.source:
        clauses.append("web_search_match(COALESCE(c.source_file, ''), ?, 'contains') > 0")
        params.append(parsed.source)
    for frag in parsed.exclude:
        if parsed.match_mode == "word":
            column = "COALESCE(tn.title_norm, web_norm(COALESCE(c.title, '')))" if has_norm else "COALESCE(c.title, '')"
            clauses.append(f"web_search_match({column}, ?, ?) = 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            clauses.append("instr(COALESCE(tn.title_norm, web_norm(COALESCE(c.title, ''))), ?) = 0")
            params.append(normalize_search_text(frag))
        else:
            clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) = 0")
            params.extend([frag, parsed.match_mode])
    if not clauses:
        clauses.append("1 = 1")
    return clauses, params


def _outer_title_exclude_filter(parsed: ParsedQuery) -> tuple[str, list[Any]]:
    if not parsed.exclude:
        return "", []
    clauses = []
    params: list[Any] = []
    for frag in parsed.exclude:
        if parsed.match_mode == "word":
            clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) = 0")
            params.extend([frag, parsed.match_mode])
        else:
            clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) = 0")
            params.extend([frag, parsed.match_mode])
    return "WHERE " + " AND ".join(clauses), params


def _conversation_level_exclude_clauses(parsed: ParsedQuery, frag: str) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if parsed.scope != "message":
        clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) = 0")
        params.extend([frag, parsed.match_mode])
    if parsed.scope != "title":
        exclude_path_clause = f"AND {_current_path_condition('en')}" if parsed.path == "current" else ""
        clauses.append(
            f"""
            NOT EXISTS (
                SELECT 1
                FROM conversation_nodes en
                WHERE en.conversation_id = c.conversation_id
                  {exclude_path_clause}
                  AND web_search_match({_sql_display_text('en')}, ?, ?) > 0
            )
            """
        )
        params.extend([frag, parsed.match_mode])
    return clauses, params


def _conversation_level_exclude_filter(parsed: ParsedQuery) -> tuple[str, list[Any]]:
    if not parsed.exclude:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    for frag in parsed.exclude:
        exclude_clauses, exclude_params = _conversation_level_exclude_clauses(parsed, frag)
        clauses.extend(exclude_clauses)
        params.extend(exclude_params)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _conversation_has_excluded_in_scope(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> bool:
    if not parsed.exclude:
        return False
    where, params = _conversation_level_exclude_filter(parsed)
    if not where:
        return False
    row = conn.execute(
        f"""
        SELECT 1
        FROM conversations c
        WHERE c.conversation_id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM conversations c2
              {where.replace('c.', 'c2.')}
                AND c2.conversation_id = c.conversation_id
          )
        LIMIT 1
        """,
        [conversation_id] + params,
    ).fetchone()
    return row is not None


def _message_trigram_clause(conn: sqlite3.Connection, parsed: ParsedQuery, use_trigram: bool) -> tuple[str, list[Any]]:
    query, _is_complete = _candidate_query(parsed.phrases + parsed.terms, parsed.required_phrases, parsed.or_mode)
    if (
        not use_trigram
        or not query
        or not _has_normalized_message_trigram(conn)
    ):
        return "", []
    if _table_has_columns(conn, "web_message_norm", {"stable_id"}):
        return (
            """
            AND EXISTS (
                SELECT 1
                FROM web_message_trigram AS trigram
                JOIN web_message_norm AS normalized
                  ON normalized.stable_id = trigram.rowid
                WHERE normalized.conversation_id = n.conversation_id
                  AND normalized.node_id = n.node_id
                  AND web_message_trigram MATCH ?
            )
            """,
            [query],
        )
    if not _table_has_columns(conn, "web_message_trigram", {"conversation_id", "node_id"}):
        return (
            """
            AND EXISTS (
                SELECT 1
                FROM web_message_trigram
                WHERE web_message_trigram.rowid = n.rowid
                  AND web_message_trigram MATCH ?
            )
            """,
            [query],
        )
    return (
        """
        AND EXISTS (
            SELECT 1
            FROM web_message_trigram
            WHERE web_message_trigram.conversation_id = n.conversation_id
              AND web_message_trigram.node_id = n.node_id
              AND web_message_trigram MATCH ?
        )
        """,
        [query],
    )


def _title_trigram_clause(conn: sqlite3.Connection, parsed: ParsedQuery, use_trigram: bool) -> tuple[str, list[Any]]:
    fragments = ([parsed.title] if parsed.title else []) + parsed.phrases + parsed.terms
    required = ([parsed.required_title] if parsed.required_title else []) + parsed.required_phrases
    query, _is_complete = _candidate_query(fragments, required, parsed.or_mode)
    if (
        not use_trigram
        or not query
        or not _has_normalized_title_trigram(conn)
    ):
        return "", []
    if _table_has_columns(conn, "web_title_norm", {"stable_id"}):
        return (
            """
            EXISTS (
                SELECT 1
                FROM web_title_trigram AS trigram
                JOIN web_title_norm AS normalized
                  ON normalized.stable_id = trigram.rowid
                WHERE normalized.conversation_id = c.conversation_id
                  AND web_title_trigram MATCH ?
            )
            """,
            [query],
        )
    if _table_has_columns(conn, "web_title_trigram", {"conversation_id"}):
        return (
            """
            EXISTS (
                SELECT 1
                FROM web_title_trigram
                WHERE web_title_trigram.conversation_id = c.conversation_id
                  AND web_title_trigram MATCH ?
            )
            """,
            [query],
        )
    return (
        """
        EXISTS (
            SELECT 1
            FROM web_title_trigram
            WHERE web_title_trigram.rowid = c.rowid
              AND web_title_trigram MATCH ?
        )
        """,
        [query],
    )


def _trigram_query(fragments: list[str], or_mode: bool) -> str | None:
    query, complete = _trigram_candidate_query(fragments, or_mode)
    return query if complete else None


def _candidate_query(raw_fragments: list[str], required_fragments: list[str], or_mode: bool) -> tuple[str | None, bool]:
    raw_query, raw_complete = _trigram_candidate_query(raw_fragments, or_mode)
    required_query, required_complete = _trigram_candidate_query(required_fragments, False)
    pieces = [query for query in (raw_query, required_query) if query]
    if not pieces:
        return None, False
    return " AND ".join(f"({query})" for query in pieces), raw_complete and required_complete


def _trigram_candidate_query(fragments: list[str], or_mode: bool) -> tuple[str | None, bool]:
    fragment_queries: list[str] = []
    usable = 0
    total = 0
    for frag in fragments:
        norm = normalize_search_text(frag)
        if not norm:
            continue
        total += 1
        fragment_query = _trigram_fragment_query(norm)
        if fragment_query is None:
            continue
        usable += 1
        fragment_queries.append(fragment_query)
    if not fragment_queries:
        return None, False
    if or_mode and usable != total:
        return None, False
    return (" OR " if or_mode else " AND ").join(fragment_queries), usable == total


def _trigram_fragment_query(norm: str) -> str | None:
    if '"' in norm or "\x00" in norm:
        return None
    parts = [part for part in norm.split(" ") if len(part) >= 3]
    if not parts:
        return None
    tokens = ['"' + part.replace('"', '""') + '"' for part in parts]
    if len(tokens) == 1:
        return tokens[0]
    return "(" + " AND ".join(tokens) + ")"


def _node_filters(parsed: ParsedQuery, conversation_id: str | None) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    if conversation_id:
        clauses.append("n.conversation_id = ?")
        params.append(conversation_id)
    if parsed.role:
        roles = _role_filter_values(parsed.role)
        clauses.append(_sql_canonical_role("n") + " IN (" + ",".join("?" for _ in roles) + ")")
        params.extend(roles)
    if parsed.path == "current":
        clauses.append(_current_path_condition("n"))
    if parsed.source:
        clauses.append("web_search_match(COALESCE(c.source_file, ''), ?, 'contains') > 0")
        params.append(parsed.source)
    if parsed.after is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) >= ?")
        params.append(parsed.after)
    if parsed.before is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) < ?")
        params.append(parsed.before)
    for title_filter in [frag for frag in (parsed.title, parsed.required_title) if frag]:
        if parsed.match_mode == "word":
            clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) > 0")
            params.extend([title_filter, parsed.match_mode])
        else:
            clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) > 0")
            params.extend([title_filter, parsed.match_mode])
    for frag in parsed.exclude:
        clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) = 0")
        params.extend([frag, parsed.match_mode])
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _conversation_time_where(after: float | None, before: float | None) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    if after is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) >= ?")
        params.append(after)
    if before is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) < ?")
        params.append(before)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _stream_selected_search_hit(
    conn: sqlite3.Connection,
    storage_rowid: int,
    parsed: ParsedQuery,
) -> tuple[str, int, str, int | None, int | None, str | None]:
    """Build a bounded preview/local snippet while counting one long BLOB."""

    preview_parts: list[str] = []
    preview_chars = 0
    total_chars = 0
    tail = ""
    snippet = ""
    match_offset: int | None = None
    match_length: int | None = None
    matched_term: str | None = None
    terms = _highlight_terms(parsed)
    with conn.blobopen(
        "conversation_nodes", "content_text", storage_rowid, readonly=True
    ) as blob:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            data = blob.read(SEARCH_STREAM_CHUNK_BYTES)
            if not data:
                break
            visible = normalize_display_text(decoder.decode(data, final=False))
            chunk_start = total_chars
            total_chars += len(visible)
            if preview_chars < SEARCH_HIT_PREVIEW_CHARS:
                part = visible[: SEARCH_HIT_PREVIEW_CHARS - preview_chars]
                preview_parts.append(part)
                preview_chars += len(part)
            scan = tail + visible
            if match_offset is None:
                source_span = _first_source_match_span(scan, terms)
                local_snippet, local_offset = _make_snippet_with_position(
                    scan,
                    terms,
                    parsed.match_mode,
                    scan_chars=len(scan),
                )
                if source_span is not None:
                    base_offset = max(0, chunk_start - len(tail))
                    match_offset = base_offset + source_span[0]
                    match_length = source_span[1] - source_span[0]
                    matched_term = source_span[2]
                    snippet = local_snippet
            tail = scan[-SEARCH_STREAM_OVERLAP_CHARS:]
        final_visible = normalize_display_text(decoder.decode(b"", final=True))
        if final_visible:
            total_chars += len(final_visible)
    preview = "".join(preview_parts)
    if not snippet:
        snippet, _unused = _make_snippet_with_position(
            preview,
            terms,
            parsed.match_mode,
            scan_chars=len(preview),
        )
    return preview, total_chars, snippet, match_offset, match_length, matched_term


def _message_search_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    parsed: ParsedQuery,
    reason: str,
    bm25_score: float | None,
    *,
    current_path_fallback_to_all: bool = False,
    effective_visible_in_current_view: bool | None = None,
    verified_artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_text = row["content_text"] or ""
    storage_bytes = int(row["content_storage_bytes"] or 0)
    if storage_bytes > SEARCH_CANDIDATE_SCAN_CHARS and verified_artifact is not None:
        text = str(verified_artifact["preview"])
        total_chars = int(verified_artifact["total_chars"])
        snippet = str(verified_artifact["snippet"])
        match_char_offset = verified_artifact["match_char_offset"]
        match_length = verified_artifact["match_length"]
        matched_term = verified_artifact["matched_term"]
        source_byte_offset = verified_artifact["source_byte_offset"]
        source_kind = str(verified_artifact.get("source_kind") or "canonical")
        preview_truncated = total_chars > len(text)
    elif storage_bytes > SEARCH_CANDIDATE_SCAN_CHARS:
        (
            text,
            total_chars,
            snippet,
            match_char_offset,
            match_length,
            matched_term,
        ) = _stream_selected_search_hit(
            conn,
            int(row["storage_rowid"]),
            parsed,
        )
        source_byte_offset = None
        source_kind = "canonical"
        preview_truncated = total_chars > len(text)
    else:
        text = resolved_text[:SEARCH_HIT_PREVIEW_CHARS]
        total_chars = len(resolved_text)
        preview_truncated = len(resolved_text) > len(text)
        snippet, match_char_offset = _make_snippet_with_position(
            resolved_text,
            _highlight_terms(parsed),
            parsed.match_mode,
            scan_chars=len(resolved_text),
        )
        source_span = _first_source_match_span(resolved_text, _highlight_terms(parsed))
        if source_span is not None:
            match_char_offset = source_span[0]
            match_length = source_span[1] - source_span[0]
            matched_term = source_span[2]
            source_byte_offset = (
                len(resolved_text[:match_char_offset].encode("utf-8", errors="replace"))
                if len(resolved_text.encode("utf-8", errors="replace")) == storage_bytes
                else None
            )
        else:
            match_length = None
            matched_term = None
            source_byte_offset = None
        source_kind = "canonical"
    reasons = {reason}
    score = 10.0
    effective_visible = (
        bool(row["effective_visible_in_current_view"])
        if effective_visible_in_current_view is None
        else effective_visible_in_current_view
    )
    if effective_visible:
        score += 5.0
        reasons.add("current path")
    for phrase in parsed.phrases + parsed.required_phrases:
        if phrase and _fragment_matches(resolved_text, phrase, parsed.match_mode):
            score += 35.0
            reasons.add("exact phrase")
    for term in parsed.terms:
        if term and _fragment_matches(resolved_text, term, parsed.match_mode):
            score += 12.0
            reasons.add("message match")
    if bm25_score is not None:
        score += max(0.0, 25.0 - min(25.0, abs(float(bm25_score))))
    role, role_truncated, role_length = _bounded_api_scalar(row["role"], MAX_API_ROLE_CHARS)
    content_type, content_type_truncated, content_type_length = _bounded_api_scalar(row["content_type"], MAX_API_CONTENT_TYPE_CHARS)
    title, title_truncated, title_length = _bounded_api_scalar(row["title"], MAX_API_TITLE_CHARS)
    source_file, source_truncated, source_length = _bounded_api_scalar(row["source_file"], MAX_API_SOURCE_CHARS)
    display_anchor_revision = _display_revision_from_values(row)
    display_anchor_cursor = (
        _encode_display_cursor(
            _database_token_identity(conn),
            _display_cursor_identity(str(row["conversation_id"]), str(row["node_id"])),
            display_anchor_revision,
            int(source_byte_offset),
            int(match_char_offset),
        )
        if (
            source_kind == "canonical"
            and source_byte_offset is not None
            and match_char_offset is not None
        )
        else None
    )
    return {
        "conversation_id": row["conversation_id"],
        "node_id": row["node_id"],
        "role": role,
        "role_truncated": role_truncated,
        "role_length": role_length,
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "content_type": content_type,
        "content_type_truncated": content_type_truncated,
        "content_type_length": content_type_length,
        "display_text": text,
        "display_preview": text,
        "display_preview_truncated": preview_truncated,
        "display_preview_returned_chars": len(text),
        "display_text_total_chars": total_chars,
        "display_text_total_chars_exact": True,
        "snippet": snippet,
        "match_char_offset": match_char_offset,
        "match_length": match_length,
        "matched_term": matched_term,
        "display_anchor_revision": display_anchor_revision,
        "display_anchor_cursor": display_anchor_cursor,
        "is_on_current_path": bool(row["is_on_current_path"]),
        "current_path_fallback_to_all": current_path_fallback_to_all,
        "effective_visible_in_current_view": effective_visible,
        "is_internal": _is_internal_message(row["role"], row["content_type"], text),
        "title": title,
        "title_truncated": title_truncated,
        "title_length": title_length,
        "conversation_create_time": row["conversation_create_time"],
        "conversation_update_time": row["conversation_update_time"],
        "current_node": row["current_node"],
        "source_file": source_file,
        "source_file_truncated": source_truncated,
        "source_file_length": source_length,
        "reasons": sorted(reasons),
        "score": score,
        "response_text_bounded": True,
    }


def _resolved_fields_by_node(rows: Sequence[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    return {str(row["node_id"]): _message_display_fields(row) for row in rows}


def _message_page_items(
    rows: Sequence[Mapping[str, Any]],
    parsed: ParsedQuery,
    conversation: Mapping[str, Any] | None,
    path: str,
    effective_ids: set[str],
    conversation_excluded: bool,
    current_path_fallback_to_all: bool,
    resolved_fields: dict[str, dict[str, Any]] | None = None,
    budget: ReaderBudget | None = None,
    budget_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    budget = budget or reader_budget()
    state = budget_state if budget_state is not None else {}
    resolved = resolved_fields or {}
    highlight_terms_for_query = _highlight_terms(parsed)
    has_positive_text = bool(parsed.terms or parsed.phrases or parsed.required_phrases)
    items: list[dict[str, Any]] = []
    display_remaining = budget.page_display_chars
    preview_remaining = budget.page_raw_preview_chars
    highlight_remaining = budget.page_highlight_scan_chars
    estimated_bytes = 0
    for row in rows:
        node_id = str(row["node_id"])
        fields = dict(resolved.get(node_id) or _message_display_fields(row, budget.message_display_chars))
        display_text = str(fields["display_text"] or "")
        returned_display = display_text[:display_remaining]
        if len(returned_display) < len(display_text):
            fields["display_text_truncated"] = True
        fields["display_text"] = returned_display
        fields["display_text_returned_chars"] = len(returned_display)
        display_remaining -= len(returned_display)
        raw_preview = str(fields["raw_preview"] or "")
        returned_preview = raw_preview[:preview_remaining]
        if len(returned_preview) < len(raw_preview):
            fields["raw_preview_truncated"] = True
        fields["raw_preview"] = returned_preview
        preview_remaining -= len(returned_preview)
        effective_visible = node_id in effective_ids
        terms = []
        if (
            has_positive_text
            and not conversation_excluded
            and _message_row_matches_highlight(
                row,
                conversation,
                parsed,
                path,
                effective_visible,
                display_text=returned_display,
            )
        ):
            terms = highlight_terms_for_query
        scan_limit = min(len(returned_display), HIGHLIGHT_MESSAGE_SCAN_CHARS, highlight_remaining)
        message_highlights, highlight_meta = _highlight_ranges_with_meta(
            returned_display,
            terms,
            max_chars=scan_limit,
        )
        highlight_remaining -= int(highlight_meta["highlight_scanned_chars"])
        if has_positive_text and fields.get("display_text_truncated"):
            highlight_meta["highlight_truncated"] = True
        estimated_bytes += len(returned_display.encode("utf-8")) + len(returned_preview.encode("utf-8")) + 512
        items.append(
            _message_payload(
                row,
                terms,
                current_path_fallback_to_all=current_path_fallback_to_all,
                effective_visible_in_current_view=effective_visible,
                fields=fields,
                message_highlights=message_highlights,
                highlight_meta=highlight_meta,
            )
        )
    state.update(
        {
            "page_text_budget_exhausted": bool(
                any(item.get("display_text_truncated") for item in items)
                and display_remaining < budget.message_display_chars
            ),
            "page_preview_budget_exhausted": bool(
                any(item.get("raw_preview_truncated") for item in items)
                and preview_remaining < budget.page_raw_preview_chars
            ),
            "page_highlight_budget_exhausted": highlight_remaining == 0 and has_positive_text,
            "response_budget_estimated": estimated_bytes,
            "response_budget_limit": budget.page_estimated_serialized_bytes,
            "response_budget_estimate_exhausted": estimated_bytes >= budget.page_estimated_serialized_bytes,
        }
    )
    return items


def _message_payload(
    row: Mapping[str, Any],
    terms: list[tuple[str, str]],
    *,
    current_path_fallback_to_all: bool = False,
    effective_visible_in_current_view: bool | None = None,
    fields: dict[str, Any] | None = None,
    message_highlights: list[dict[str, int]] | None = None,
    highlight_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = fields or _message_display_fields(row)
    if message_highlights is None:
        message_highlights, highlight_meta = _highlight_ranges_with_meta(fields["display_text"], terms)
    highlight_meta = highlight_meta or {}
    role, role_truncated, role_length = _bounded_api_scalar(row["role"], MAX_API_ROLE_CHARS)
    author_name, author_truncated, author_length = _bounded_api_scalar(row["author_name"], MAX_API_AUTHOR_CHARS)
    content_type, content_type_truncated, content_type_length = _bounded_api_scalar(row["content_type"], MAX_API_CONTENT_TYPE_CHARS)
    return {
        "node_id": row["node_id"],
        "parent_node_id": row["parent_node_id"],
        "message_id": row["message_id"],
        "role": role,
        "role_truncated": role_truncated,
        "role_length": role_length,
        "author_name": author_name,
        "author_name_truncated": author_truncated,
        "author_name_length": author_length,
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "content_type": content_type,
        "content_type_truncated": content_type_truncated,
        "content_type_length": content_type_length,
        "display_text": fields["display_text"],
        "display_text_truncated": bool(fields.get("display_text_truncated")),
        "display_text_total_chars": fields.get("display_text_total_chars", len(fields["display_text"])),
        "display_text_total_chars_exact": bool(fields.get("display_text_total_chars_exact", True)),
        "display_text_resolver_input_truncated": bool(
            fields.get("display_text_resolver_input_truncated", False)
        ),
        "display_text_returned_chars": len(fields["display_text"]),
        "has_text": bool(fields["display_text"]),
        "has_raw": bool(fields.get("raw_size") or fields["raw_preview"]),
        "raw_preview": fields["raw_preview"],
        "raw_preview_truncated": fields["raw_preview_truncated"],
        "content_hash": row["content_hash"],
        "is_on_current_path": bool(row["is_on_current_path"]),
        "current_path_fallback_to_all": current_path_fallback_to_all,
        "effective_visible_in_current_view": (
            bool(effective_visible_in_current_view)
            if effective_visible_in_current_view is not None
            else _effective_visible_in_current_view(bool(row["is_on_current_path"]), current_path_fallback_to_all)
        ),
        "is_internal": fields["is_internal"],
        "is_empty_mapping_node": fields["is_empty_mapping_node"],
        "highlight_ranges": message_highlights,
        "highlight_ranges_truncated": bool(highlight_meta.get("highlight_truncated")),
        "highlight_truncated": bool(highlight_meta.get("highlight_truncated")),
        "highlight_scanned_chars": int(highlight_meta.get("highlight_scanned_chars", 0)),
        "highlight_range_limit_reached": bool(highlight_meta.get("highlight_range_limit_reached")),
    }


def _message_display_fields(
    row: Mapping[str, Any], display_limit: int | None = None
) -> dict[str, Any]:
    row = dict(row)
    text = row["content_text"] or ""
    raw_message_json = row["raw_message_json"] or ""
    content_total = int(row.get("content_text_total_chars", len(text)))
    raw_total = int(row.get("raw_message_total_chars", len(raw_message_json)))
    content_total_exact = bool(row.get("content_text_total_chars_exact", True))
    raw_total_exact = bool(row.get("raw_message_total_chars_exact", True))
    content_source_truncated = bool(row.get("content_text_source_truncated", content_total > len(text)))
    raw_source_truncated = bool(row.get("raw_message_source_truncated", raw_total > len(raw_message_json)))
    parsed_message: Any = RAW_MESSAGE_NOT_PARSED
    parsed_ok = False
    if raw_message_json and not raw_source_truncated and len(raw_message_json) <= 200_000:
        try:
            validate_json_lexical_limits(raw_message_json)
            parsed_message = json_loads(raw_message_json)
            parsed_ok = True
        except (TypeError, ValueError, RecursionError):
            parsed_message = None
    raw_preview = _raw_preview(raw_message_json, parsed_message=parsed_message, parsed_ok=parsed_ok)
    resolved_display_text = recover_message_display_text(
        text,
        raw_message_json,
        parsed_message=parsed_message if parsed_message is not RAW_MESSAGE_NOT_PARSED else None,
    )
    placeholder_classification_exact = bool(
        row.get("content_placeholder_classification_exact", True)
    )
    marker_text = bool(row.get("content_placeholder_exact")) or is_generated_non_text_placeholder(text)
    placeholder_text = bool(
        marker_text
        and (
            (row["content_type"] or "").casefold() not in {"text", "code", "multimodal_text"}
            or resolved_display_text != text
        )
    )
    if text and not placeholder_text:
        display_total = content_total
        display_total_exact = content_total_exact
    elif resolved_display_text != text:
        display_total = len(resolved_display_text)
        display_total_exact = raw_total_exact and not raw_source_truncated
    else:
        display_total = content_total if text else len(resolved_display_text)
        display_total_exact = raw_total_exact and not raw_source_truncated
    display_text = resolved_display_text[:display_limit] if display_limit is not None else resolved_display_text
    display_depends_on_raw = bool(not text or placeholder_text)
    display_truncated = (
        (raw_source_truncated if display_depends_on_raw else content_source_truncated)
        or display_total > len(display_text)
    )
    is_empty_mapping_node = not row["message_id"] and not display_text and not raw_preview
    is_technical = _is_internal_message(row["role"], row["content_type"], display_text)
    return {
        "content_text": text,
        "display_text": display_text,
        "display_text_truncated": display_truncated,
        "display_text_total_chars": display_total,
        "display_text_total_chars_exact": display_total_exact,
        "display_text_resolver_input_truncated": bool(
            (raw_source_truncated and (not text or placeholder_text))
            or not placeholder_classification_exact
        ),
        "display_text_returned_chars": len(display_text),
        "raw_preview": raw_preview,
        "raw_preview_truncated": bool(raw_total > len(raw_preview)),
        "raw_size": raw_total,
        "is_empty_mapping_node": is_empty_mapping_node,
        "is_technical": is_technical,
        "is_internal": is_technical or is_empty_mapping_node,
    }


def _message_row_matches_highlight(
    row: sqlite3.Row,
    conversation: dict[str, Any] | None,
    parsed: ParsedQuery,
    path: str,
    effective_visible_in_current_view: bool,
    *,
    display_text: str | None = None,
) -> bool:
    if not (parsed.terms or parsed.phrases or parsed.required_phrases):
        return False
    if parsed.scope == "title":
        return False
    if parsed.role:
        roles = set(_role_filter_values(parsed.role))
        if _canonical_role(row["role"]) not in roles:
            return False
    if parsed.path == "current" and path == "all" and not effective_visible_in_current_view:
        return False
    if conversation:
        title = conversation.get("title") or ""
        if parsed.scope != "message" and any(excluded and _fragment_matches(title, excluded, parsed.match_mode) for excluded in parsed.exclude):
            return False
        source_file = conversation.get("source_file") or ""
        if parsed.source and not _fragment_matches(source_file, parsed.source, "contains"):
            return False
        timestamp = conversation.get("update_time") if conversation.get("update_time") is not None else conversation.get("create_time")
        timestamp = float(timestamp or 0)
        if parsed.after is not None and timestamp < parsed.after:
            return False
        if parsed.before is not None and timestamp >= parsed.before:
            return False
        for title_filter in (parsed.title, parsed.required_title):
            if title_filter and not _fragment_matches(conversation.get("title") or "", title_filter, parsed.match_mode):
                return False
    text = display_text if display_text is not None else recover_message_display_text(
        row["content_text"], row["raw_message_json"]
    )
    if not text or _is_placeholder_text(text, row["content_type"]):
        return False
    for excluded in parsed.exclude:
        if excluded and _fragment_matches(text, excluded, parsed.match_mode):
            return False
    positives = parsed.phrases + parsed.terms
    if not positives:
        raw_ok = True
    elif parsed.or_mode:
        raw_ok = any(fragment and _fragment_matches(text, fragment, parsed.match_mode) for fragment in positives)
    else:
        raw_ok = all((not fragment) or _fragment_matches(text, fragment, parsed.match_mode) for fragment in positives)
    required_ok = all((not fragment) or _fragment_matches(text, fragment, parsed.match_mode) for fragment in parsed.required_phrases)
    return raw_ok and required_ok


def _text_from_raw_message(raw_message_json: str) -> str:
    return recover_message_display_text("", raw_message_json)


def _raw_preview(
    raw_message_json: str | None,
    limit: int = min(20000, MAX_RAW_PREVIEW_BYTES),
    *,
    parsed_message: Any = RAW_MESSAGE_NOT_PARSED,
    parsed_ok: bool = False,
) -> str:
    if not raw_message_json:
        return ""
    if parsed_ok:
        try:
            return compact_json(_sanitize_raw_preview(parsed_message), limit)
        except JsonSafetyLimitError:
            return raw_message_json[:limit]
    if parsed_message is RAW_MESSAGE_NOT_PARSED and len(raw_message_json) <= 200_000:
        try:
            validate_json_lexical_limits(raw_message_json)
            return compact_json(_sanitize_raw_preview(json_loads(raw_message_json)), limit)
        except (ValueError, RecursionError):
            pass
    return raw_message_json[:limit]


def json_loads(value: str) -> Any:
    return json.loads(value)


def _is_internal_message(role: str | None, content_type: str | None, text: str | None = None) -> bool:
    role_value = _canonical_role(role)
    type_value = (content_type or "").casefold()
    text_value = (text or "").strip().casefold()
    return role_value in _INTERNAL_ROLE_VALUES or type_value in {
        "user_editable_context",
        "model_editable_context",
        "system_context",
        "developer_context",
        "thoughts",
    } or text_value.startswith("source analysis msg id:")


def _is_placeholder_text(text: str, content_type: str | None = None) -> bool:
    has_marker = is_generated_non_text_placeholder(text)
    return has_marker and (content_type or "").casefold() not in {"text", "code", "multimodal_text"}


def _sanitize_raw_preview(value: Any) -> Any:
    return sanitize_json_value(value, omit_metadata=True)


def _bounded_api_scalar(value: Any, limit: int) -> tuple[str | None, bool, int]:
    if value is None:
        return None, False, 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        text = normalize_display_text(bytes(value).decode("utf-8", errors="replace"))
    else:
        text = normalize_display_text(str(value))
    return text[:limit], len(text) > limit, len(text)


def _conversation_scalar_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    title, title_truncated, title_length = _bounded_api_scalar(row["title"], MAX_API_TITLE_CHARS)
    source_file, source_truncated, source_length = _bounded_api_scalar(row["source_file"], MAX_API_SOURCE_CHARS)
    return {
        "title": title,
        "title_truncated": title_truncated,
        "title_length": title_length,
        "source_file": source_file,
        "source_file_truncated": source_truncated,
        "source_file_length": source_length,
    }


def _conversation_summary(row: sqlite3.Row) -> dict[str, Any]:
    node_count = int(row["node_count"] or 0)
    current_path_nodes = int(row["current_path_nodes"] or 0)
    return {
        "conversation_id": row["conversation_id"],
        **_conversation_scalar_fields(row),
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "current_node": row["current_node"],
        "node_count": node_count,
        "current_path_nodes": current_path_nodes,
        "current_path_fallback_to_all": node_count > 0 and current_path_nodes == 0,
        "current_node_exists": bool(row["current_node"] and node_count),
        "effective_path": "all" if node_count > 0 and current_path_nodes == 0 else "current",
    }


def _node_counts_for_conversations(conn: sqlite3.Connection, conversation_ids: list[str]) -> dict[str, dict[str, int]]:
    """Count nodes only for the current page, avoiding a full-table GROUP BY for empty lists."""
    return effective_current_metadata(conn, conversation_ids)


def _conversation_summary_with_counts(row: sqlite3.Row, counts: dict[str, int]) -> dict[str, Any]:
    node_count = int(counts.get("node_count", 0))
    current_path_nodes = int(counts.get("current_path_nodes", 0))
    return {
        "conversation_id": row["conversation_id"],
        **_conversation_scalar_fields(row),
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "current_node": row["current_node"],
        "node_count": node_count,
        "current_path_nodes": current_path_nodes,
        "current_path_fallback_to_all": bool(counts.get("current_path_fallback_to_all", node_count > 0 and current_path_nodes == 0)),
        "current_node_exists": bool(counts.get("current_node_exists", False)),
        "current_collection_source": counts.get("current_collection_source", "fallback_all"),
        "effective_path": counts.get("effective_path", "all" if node_count > 0 and current_path_nodes == 0 else "current"),
        "cycle_detected": bool(counts.get("cycle_detected", False)),
        "missing_parent": bool(counts.get("missing_parent", False)),
        "cross_conversation_parent": bool(counts.get("cross_conversation_parent", False)),
        "partial_chain": bool(counts.get("partial_chain", False)),
        "raw_flag_leaf_count": int(counts.get("raw_flag_leaf_count", 0)),
        "selected_chain_cycle_detected": bool(counts.get("selected_chain_cycle_detected", False)),
        "raw_flag_cycle_detected": bool(counts.get("raw_flag_cycle_detected", False)),
        "selected_chain_missing_parent": bool(counts.get("selected_chain_missing_parent", False)),
        "raw_flag_missing_parent": bool(counts.get("raw_flag_missing_parent", False)),
        "selected_chain_cross_conversation_parent": bool(counts.get("selected_chain_cross_conversation_parent", False)),
        "raw_flag_cross_conversation_parent": bool(counts.get("raw_flag_cross_conversation_parent", False)),
    }


def _add_counts_and_path_metadata(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    counts = _node_counts_for_conversations(conn, [item["conversation_id"] for item in items])
    for item in items:
        item_counts = counts.get(item["conversation_id"], {})
        node_count = int(item_counts.get("node_count", item.get("node_count") or 0))
        current_path_nodes = int(item_counts.get("current_path_nodes", item.get("current_path_nodes") or 0))
        item["node_count"] = node_count
        item["current_path_nodes"] = current_path_nodes
        item["current_path_fallback_to_all"] = bool(item_counts.get("current_path_fallback_to_all", node_count > 0 and current_path_nodes == 0))
        item["current_node_exists"] = bool(item_counts.get("current_node_exists", False))
        item["current_collection_source"] = item_counts.get("current_collection_source", "fallback_all")
        item["effective_path"] = item_counts.get("effective_path", "all" if item["current_path_fallback_to_all"] else "current")
        item["cycle_detected"] = bool(item_counts.get("cycle_detected", False))
        item["missing_parent"] = bool(item_counts.get("missing_parent", False))
        item["cross_conversation_parent"] = bool(item_counts.get("cross_conversation_parent", False))
        item["partial_chain"] = bool(item_counts.get("partial_chain", False))
        item["raw_flag_leaf_count"] = int(item_counts.get("raw_flag_leaf_count", 0))
        for field in (
            "selected_chain_cycle_detected",
            "raw_flag_cycle_detected",
            "selected_chain_missing_parent",
            "raw_flag_missing_parent",
            "selected_chain_cross_conversation_parent",
            "raw_flag_cross_conversation_parent",
        ):
            item[field] = bool(item_counts.get(field, False))


def _order_nodes_for_display(rows: list[sqlite3.Row], path: str, current_node: str | None = None) -> list[sqlite3.Row]:
    if path == "all":
        return sorted(
            rows,
            key=lambda row: (
                row["create_time"] is None,
                row["create_time"] if row["create_time"] is not None else row["update_time"] if row["update_time"] is not None else 0,
                row["node_id"],
            ),
        )
    by_id = {str(row["node_id"]): row for row in rows}
    collection = resolve_effective_current_collection(current_node, rows)
    return [by_id[node_id] for node_id in collection.node_ids]


def _highlight_terms(parsed: ParsedQuery) -> list[tuple[str, str]]:
    return [(item, parsed.match_mode) for item in parsed.required_phrases + parsed.phrases + parsed.terms if item]


def _first_source_match_span(
    text: str,
    terms: list[tuple[str, str]],
) -> tuple[int, int, str] | None:
    """Map the earliest normalized match back to its exact source code-point span."""

    # ASCII NFKC/case-folding is one source code point per normalized code
    # point whenever whitespace collapsing did not change the length.  Avoid
    # allocating one five-field tuple per character for this dominant path.
    if text.isascii():
        normalized_ascii = normalize_search_text(text)
        if len(normalized_ascii) == len(text):
            best_ascii: tuple[int, int, str] | None = None
            for term, match_mode in terms:
                needle = normalize_search_text(term)
                if not needle:
                    continue
                token_spans = (
                    _word_token_spans(needle) if match_mode == "word" else []
                )
                offset = 0
                while True:
                    index = normalized_ascii.find(needle, offset)
                    if index < 0:
                        break
                    if token_spans and not _candidate_has_word_boundaries(
                        normalized_ascii, index, token_spans, len(needle)
                    ):
                        offset = index + 1
                        continue
                    candidate = (index, index + len(needle), term)
                    if best_ascii is None or (
                        candidate[0],
                        -(candidate[1] - candidate[0]),
                    ) < (
                        best_ascii[0],
                        -(best_ascii[1] - best_ascii[0]),
                    ):
                        best_ascii = candidate
                    break
            return best_ascii

    normalized = normalize_search_text(text)
    candidates: list[tuple[int, int, str]] = []
    for term, match_mode in terms:
        needle = normalize_search_text(term)
        if not needle:
            continue
        token_spans = _word_token_spans(needle) if match_mode == "word" else []
        offset = 0
        while True:
            index = normalized.find(needle, offset)
            if index < 0:
                break
            if token_spans and not _candidate_has_word_boundaries(
                normalized, index, token_spans, len(needle)
            ):
                offset = index + 1
                continue
            candidates.append((index, index + len(needle) - 1, term))
            break
    if not candidates:
        return None

    wanted = {
        position
        for start, end, _term in candidates
        for position in (start, end)
    }
    mapped: dict[int, tuple[int, int]] = {}
    for position, unit in enumerate(_iter_normalized_span_units(text)):
        if position in wanted:
            mapped[position] = (unit[1], unit[2])
            if len(mapped) == len(wanted):
                break

    best: tuple[int, int, str] | None = None
    for start, end, term in candidates:
        if start not in mapped or end not in mapped:
            continue
        candidate = (mapped[start][0], mapped[end][1], term)
        if best is None or (candidate[0], -(candidate[1] - candidate[0])) < (
            best[0], -(best[1] - best[0])
        ):
            best = candidate
    return best


def highlight_ranges(text: str, terms: list[tuple[str, str]]) -> list[dict[str, int]]:
    ranges, _meta = _highlight_ranges_with_meta(text, terms)
    return ranges


def _highlight_ranges_with_meta(
    text: str,
    terms: list[tuple[str, str]],
    *,
    max_chars: int = HIGHLIGHT_MESSAGE_SCAN_CHARS,
) -> tuple[list[dict[str, int]], dict[str, Any]]:
    # The early return is deliberately before normalization, UTF-16 encoding,
    # or span allocation. Ordinary reader/filter/title-only pages take it.
    if not text or not terms:
        return [], {
            "highlight_truncated": False,
            "highlight_scanned_chars": 0,
            "highlight_range_limit_reached": False,
        }
    scan_chars = max(0, min(len(text), int(max_chars)))
    if scan_chars == 0:
        return [], {
            "highlight_truncated": True,
            "highlight_scanned_chars": 0,
            "highlight_range_limit_reached": False,
        }
    # Web highlight ranges are consumed by JavaScript text.slice(), so offsets
    # are UTF-16 code units rather than Python code point indexes.
    normalized, spans = _normalized_with_utf16_spans(text[:scan_chars])
    ranges: list[dict[str, int]] = []
    range_limit_reached = False
    for term, match_mode in terms[:HIGHLIGHT_TERM_LIMIT]:
        needle = normalize_search_text(term)
        if not needle:
            continue
        start = 0
        token_spans = _word_token_spans(needle) if match_mode == "word" else []
        while len(ranges) < HIGHLIGHT_RANGE_LIMIT:
            idx = normalized.find(needle, start)
            if idx < 0:
                break
            if token_spans and not _candidate_has_word_boundaries(normalized, idx, token_spans, len(needle)):
                start = idx + 1
                continue
            end_idx = idx + len(needle) - 1
            if idx * 2 + 1 < len(spans) and end_idx * 2 + 1 < len(spans):
                ranges.append({"start": int(spans[idx * 2]), "end": int(spans[end_idx * 2 + 1])})
            start = idx + max(1, len(needle))
        if len(ranges) >= HIGHLIGHT_RANGE_LIMIT:
            range_limit_reached = True
            break
    ranges.sort(key=lambda item: (item["start"], -item["end"]))
    merged: list[dict[str, int]] = []
    for item in ranges:
        if not merged or item["start"] > merged[-1]["end"]:
            merged.append(dict(item))
        else:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
    return merged, {
        "highlight_truncated": bool(
            scan_chars < len(text)
            or len(terms) > HIGHLIGHT_TERM_LIMIT
            or range_limit_reached
        ),
        "highlight_scanned_chars": scan_chars,
        "highlight_range_limit_reached": range_limit_reached,
    }


def _make_snippet_with_position(
    text: str,
    terms: list[tuple[str, str]],
    match_mode: str = "contains",
    radius: int = 80,
    *,
    scan_chars: int = SEARCH_SNIPPET_SCAN_CHARS,
) -> tuple[str, int | None]:
    if not text:
        return "", None
    effective_scan = min(len(text), max(0, scan_chars))
    if effective_scan > SEARCH_SNIPPET_SCAN_CHARS:
        overlap = min(SEARCH_STREAM_OVERLAP_CHARS, SEARCH_SNIPPET_SCAN_CHARS // 4)
        step = SEARCH_SNIPPET_SCAN_CHARS - overlap
        start = 0
        while start < effective_scan:
            window_start = max(0, start - overlap)
            window_end = min(effective_scan, start + step)
            window = text[window_start:window_end]
            local_snippet, local_position = _make_snippet_with_position(
                window,
                terms,
                match_mode,
                radius,
                scan_chars=len(window),
            )
            if local_position is not None:
                return local_snippet, window_start + local_position
            start += step
        first = text[: min(effective_scan, SEARCH_SNIPPET_SCAN_CHARS)]
        return first[: radius].replace("\n", " ") + ("..." if len(text) > radius else ""), None
    bounded_text = text[: max(0, scan_chars)]
    normalized, spans = _normalized_with_codepoint_spans(bounded_text)
    positions = []
    for term, term_match_mode in terms:
        needle = normalize_search_text(term)
        if not needle:
            continue
        start = 0
        mode = term_match_mode or match_mode
        token_spans = _word_token_spans(needle) if mode == "word" else []
        while True:
            idx = normalized.find(needle, start)
            if idx < 0:
                break
            if token_spans and not _candidate_has_word_boundaries(normalized, idx, token_spans, len(needle)):
                start = idx + 1
                continue
            if idx < len(spans):
                positions.append(spans[idx])
            break
    center = min(positions) if positions else 0
    start = max(0, center - radius)
    end = min(len(bounded_text), center + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + bounded_text[start:end].replace("\n", " ") + suffix, (center if positions else None)


def make_snippet(text: str, terms: list[tuple[str, str]], match_mode: str = "contains", radius: int = 80) -> str:
    snippet, _position = _make_snippet_with_position(text, terms, match_mode, radius)
    return snippet


def _normalized_with_codepoint_spans(text: str) -> tuple[str, list[int]]:
    pieces: list[str] = []
    spans: list[int] = []
    for normalized_char, start, _end, _utf16_start, _utf16_end in _normalized_span_units(text):
        pieces.append(normalized_char)
        spans.append(start)
    return "".join(pieces), spans


def _normalized_with_utf16_spans(text: str) -> tuple[str, array[int]]:
    """Normalize with compact interleaved UTF-16 start/end spans."""

    pieces: list[str] = []
    spans = array("I")
    pending_space: tuple[int, int] | None = None
    utf16_index = 0
    index = 0
    while index < len(text):
        char_start = utf16_index
        cluster = text[index]
        utf16_index += _utf16_code_units(text[index])
        index += 1
        while index < len(text) and unicodedata.combining(text[index]):
            cluster += text[index]
            utf16_index += _utf16_code_units(text[index])
            index += 1
        normalized = unicodedata.normalize("NFKC", cluster).translate(NORMALIZE_TRANSLATION).casefold()
        for normalized_char in normalized:
            if normalized_char.isspace():
                if pending_space is None:
                    pending_space = (char_start, utf16_index)
                else:
                    pending_space = (pending_space[0], utf16_index)
                continue
            if pending_space is not None and pieces:
                pieces.append(" ")
                spans.extend(pending_space)
            pending_space = None
            pieces.append(normalized_char)
            spans.extend((char_start, utf16_index))
    return "".join(pieces), spans


def _iter_normalized_span_units(
    text: str,
) -> Iterator[tuple[str, int, int, int, int]]:
    """Yield normalized/source span units without a per-character list."""

    utf16_index = 0
    index = 0
    emitted = False
    pending_space: tuple[int, int, int, int] | None = None
    while index < len(text):
        start = index
        char_start = utf16_index
        cluster = text[index]
        utf16_index += _utf16_code_units(text[index])
        index += 1
        while index < len(text) and unicodedata.combining(text[index]):
            cluster += text[index]
            utf16_index += _utf16_code_units(text[index])
            index += 1
        normalized = unicodedata.normalize("NFKC", cluster).translate(NORMALIZE_TRANSLATION).casefold()
        for normalized_char in normalized:
            if normalized_char.isspace():
                if pending_space is None:
                    pending_space = (start, index, char_start, utf16_index)
                else:
                    pending_space = (
                        min(pending_space[0], start),
                        max(pending_space[1], index),
                        min(pending_space[2], char_start),
                        max(pending_space[3], utf16_index),
                    )
                continue
            if pending_space is not None and emitted:
                space_start, space_end, space_utf16_start, space_utf16_end = pending_space
                yield (
                    " ",
                    space_start,
                    space_end,
                    space_utf16_start,
                    space_utf16_end,
                )
            pending_space = None
            yield (normalized_char, start, index, char_start, utf16_index)
            emitted = True


def _normalized_span_units(text: str) -> list[tuple[str, int, int, int, int]]:
    return list(_iter_normalized_span_units(text))


def _utf16_code_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _bounded_limit(limit: int, maximum: int = 100) -> int:
    return max(1, min(maximum, int(limit or 50)))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return name in _connection_capabilities(conn)["tables"]


def _connection_capabilities(conn: sqlite3.Connection) -> dict[str, Any]:
    schema_row = conn.execute("PRAGMA main.schema_version").fetchone()
    schema_version = int(schema_row[0] if schema_row is not None else -1)
    cache_key = id(conn)
    with _CAPABILITY_CACHE_LOCK:
        cached = _CAPABILITY_CACHE.get(cache_key)
        if cached is not None and cached[0] is conn and cached[1] == schema_version:
            _CAPABILITY_CACHE.move_to_end(cache_key)
            return cached[2]
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view', 'virtual table')").fetchall()
    tables = {str(row["name"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows}
    metadata: dict[str, str] = {}
    if "web_index_metadata" in tables:
        metadata_columns = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in conn.execute('PRAGMA table_xinfo("web_index_metadata")')
        }
        if {"key", "value"}.issubset(metadata_columns):
            metadata = {
                str(row["key"] if isinstance(row, sqlite3.Row) else row[0]):
                str(row["value"] if isinstance(row, sqlite3.Row) else row[1])
                for row in conn.execute("SELECT key, value FROM web_index_metadata")
            }
    # A matching generation counter is trustworthy only while the managed
    # trigger/table contract itself is current. Import lazily to keep the
    # low-level database module independent from search. Runtime SQLite errors
    # propagate instead of masquerading as an optional-index miss.
    from .db import generation_schema_contract_is_current

    generation_schema_current = generation_schema_contract_is_current(conn)
    result = {
        "tables": tables,
        "metadata": metadata,
        "generation_schema_current": generation_schema_current,
    }
    with _CAPABILITY_CACHE_LOCK:
        _CAPABILITY_CACHE[cache_key] = (conn, schema_version, result)
        _CAPABILITY_CACHE.move_to_end(cache_key)
        while len(_CAPABILITY_CACHE) > _CAPABILITY_CACHE_MAX:
            _CAPABILITY_CACHE.popitem(last=False)
    return result


def invalidate_capability_cache(conn: sqlite3.Connection | None = None) -> None:
    """Invalidate cached schema/index capabilities after same-connection rebuilds."""

    with _CAPABILITY_CACHE_LOCK:
        if conn is None:
            _CAPABILITY_CACHE.clear()
        else:
            _CAPABILITY_CACHE.pop(id(conn), None)


def _table_has_columns(conn: sqlite3.Connection, name: str, columns: set[str]) -> bool:
    rows = conn.execute(f'PRAGMA table_xinfo("{name}")').fetchall()
    found = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}
    return columns.issubset(found)


def _web_index_metadata_value(conn: sqlite3.Connection, key: str) -> str | None:
    return _connection_capabilities(conn)["metadata"].get(key)


def _derived_generation_is_current(conn: sqlite3.Connection, name: str) -> bool:
    if (
        not _connection_capabilities(conn)["generation_schema_current"]
        or
        _web_index_metadata_value(conn, "web_index_format_version") != OPTIONAL_WEB_INDEX_FORMAT_VERSION
        or _web_index_metadata_value(conn, "display_text_resolver_version") != DISPLAY_TEXT_RESOLVER_VERSION
        or _web_index_metadata_value(conn, "normalization_index_format_version") != NORMALIZATION_INDEX_FORMAT_VERSION
        or _web_index_metadata_value(conn, "stable_optional_address_version") != STABLE_OPTIONAL_ADDRESS_VERSION
        or len(_web_index_metadata_value(conn, "stable_optional_address_identity") or "") != 32
        or _web_index_metadata_value(conn, "oversized_fallback") != "required"
        or not _table_exists(conn, "web_index_oversized")
    ):
        return False
    if not _table_exists(conn, "archive_generations"):
        return False
    expected = _web_index_metadata_value(conn, f"{name}_generation")
    expected_generation = parse_nonnegative_integer(expected)
    if expected_generation is None:
        return False
    row = conn.execute(
        "SELECT generation FROM archive_generations WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return False
    sqlite_type = conn.execute(
        "SELECT typeof(generation) FROM archive_generations WHERE name = ?",
        (name,),
    ).fetchone()
    return bool(
        sqlite_type is not None
        and str(sqlite_type[0]) == "integer"
        and parse_nonnegative_integer(row[0]) == expected_generation
    )


def _has_normalized_message_norm(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "web_message_norm") and (
        _web_index_metadata_value(conn, "message_norm_text") == "normalized"
        or _web_index_metadata_value(conn, "message_trigram_text") == "normalized"
    ) and _derived_generation_is_current(conn, "message")


def _has_normalized_title_norm(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "web_title_norm") and (
        _web_index_metadata_value(conn, "title_norm_text") == "normalized"
        or _web_index_metadata_value(conn, "title_trigram_text") == "normalized"
    ) and _derived_generation_is_current(conn, "title")


def _has_normalized_message_trigram(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "web_message_trigram") and _web_index_metadata_value(conn, "message_trigram_text") == "normalized" and _derived_generation_is_current(conn, "message")


def _has_normalized_title_trigram(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "web_title_trigram") and _web_index_metadata_value(conn, "title_trigram_text") == "normalized" and _derived_generation_is_current(conn, "title")


def _limit_clause(limit: int | None) -> tuple[str, list[int]]:
    if limit is None:
        return "", []
    return "LIMIT ?", [int(limit)]


def _page_payload(
    items: list[dict[str, Any]],
    total: int,
    limit: int,
    offset: int,
    *,
    selected_in_results: bool | None = None,
    selected_item: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_offset = offset + len(items)
    payload: dict[str, Any] = {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
    }
    if selected_in_results is not None:
        payload["selected_in_results"] = selected_in_results
    if selected_item is not None:
        payload["selected_item"] = selected_item
    if extra:
        payload.update(extra)
    return payload


def _selected_in_conversation_filter(
    conn: sqlite3.Connection,
    where: str,
    params: list[Any],
    selected_id: str | None,
) -> bool | None:
    if not selected_id:
        return None
    extra = " AND " if where else "WHERE "
    row = conn.execute(
        f"SELECT 1 FROM conversations c {where}{extra}c.conversation_id = ? LIMIT 1",
        params + [selected_id],
    ).fetchone()
    return row is not None


def _message_search_diagnostics(conn: sqlite3.Connection, parsed: ParsedQuery, *, used_trigram: bool) -> dict[str, Any]:
    diag: dict[str, Any] = {}
    has_norm = _has_normalized_message_norm(conn)
    normalized_trigram_present = _has_normalized_message_trigram(conn)
    has_trigram = normalized_trigram_present and used_trigram
    legacy_trigram = _table_exists(conn, "web_message_trigram") and not _has_normalized_message_trigram(conn)
    legacy_fts = _table_exists(conn, "message_fts")
    fragments = parsed.phrases + parsed.terms + parsed.required_phrases
    short_query = any(fragment and len(normalize_search_text(fragment)) < 3 for fragment in fragments)
    diag["web_index_missing"] = not has_norm
    diag["normalized_trigram_available"] = has_trigram
    diag["legacy_trigram_index"] = legacy_trigram
    diag["legacy_fts_present"] = legacy_fts
    diag["short_query"] = short_query
    diag["diagnostics_accuracy"] = "best_effort"
    trigram_query, _is_complete = _candidate_query(parsed.phrases + parsed.terms, parsed.required_phrases, parsed.or_mode)
    if has_trigram and trigram_query:
        diag["candidate_backend"] = "normalized_trigram"
    elif has_norm:
        diag["candidate_backend"] = "normalized_scan"
    else:
        diag["candidate_backend"] = "full_scan"
    if legacy_fts and not has_norm:
        diag["actual_fallback_note"] = "legacy_fts_present_not_normalized_safe_candidate"
    if not used_trigram:
        diag["estimated_backend_note"] = "OperationalError_fallback_no_trigram"
    return diag


def _conversation_search_diagnostics(conn: sqlite3.Connection, parsed: ParsedQuery, *, used_trigram: bool) -> dict[str, Any]:
    diag: dict[str, Any] = {}
    has_msg_norm = _has_normalized_message_norm(conn)
    has_title_norm = _has_normalized_title_norm(conn)
    has_msg_trigram = _has_normalized_message_trigram(conn) and used_trigram
    has_title_trigram = _has_normalized_title_trigram(conn) and used_trigram
    legacy_msg_trigram = _table_exists(conn, "web_message_trigram") and not _has_normalized_message_trigram(conn)
    legacy_title_trigram = _table_exists(conn, "web_title_trigram") and not _has_normalized_title_trigram(conn)
    legacy_fts = _table_exists(conn, "message_fts")
    title_candidate_context = parsed.scope == "title" or _is_title_only_candidate_context(parsed)
    fragments = parsed.phrases + parsed.terms + ([parsed.title] if parsed.title else []) + ([parsed.required_title] if parsed.required_title else [])
    short_query = any(fragment and len(normalize_search_text(fragment)) < 3 for fragment in fragments if fragment)
    diag["web_index_missing"] = not has_title_norm if title_candidate_context else (not has_msg_norm and not has_title_norm)
    diag["normalized_trigram_available"] = has_title_trigram if title_candidate_context else (has_msg_trigram or has_title_trigram)
    diag["legacy_trigram_index"] = legacy_title_trigram if title_candidate_context else (legacy_msg_trigram or legacy_title_trigram)
    diag["legacy_fts_present"] = legacy_fts
    diag["short_query"] = short_query
    diag["diagnostics_accuracy"] = "best_effort"
    if title_candidate_context:
        title_query, _is_complete = _candidate_query(
            ([parsed.title] if parsed.title else []) + parsed.phrases + parsed.terms,
            ([parsed.required_title] if parsed.required_title else []) + parsed.required_phrases,
            parsed.or_mode,
        )
        if has_title_trigram and title_query:
            diag["candidate_backend"] = "normalized_title_trigram"
        elif has_title_norm:
            diag["candidate_backend"] = "normalized_title_scan"
        else:
            diag["candidate_backend"] = "full_scan"
    else:
        message_query, _is_complete = _candidate_query(parsed.phrases + parsed.terms, parsed.required_phrases, parsed.or_mode)
        if has_msg_trigram and message_query:
            diag["candidate_backend"] = "normalized_trigram"
        elif has_msg_norm:
            diag["candidate_backend"] = "normalized_scan"
        elif has_title_norm and not (parsed.terms or parsed.phrases or parsed.required_phrases or parsed.role):
            diag["candidate_backend"] = "normalized_title_scan"
        else:
            diag["candidate_backend"] = "full_scan"
    if legacy_fts and not (has_msg_norm or has_title_norm):
        diag["actual_fallback_note"] = "legacy_fts_present_not_normalized_safe_candidate"
    if not used_trigram:
        diag["estimated_backend_note"] = "OperationalError_fallback_no_trigram"
    return diag
