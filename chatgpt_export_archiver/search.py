from __future__ import annotations

import re
import json
import base64
import codecs
import os
import sqlite3
import threading
import unicodedata
from array import array
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .current_path import (
    effective_current_metadata,
    ensure_effective_current_views,
    resolve_effective_current_collection,
)
from .parser import RAW_MESSAGE_NOT_PARSED, extract_message_content, normalize_display_text, recover_message_display_text
from .schema_contract import (
    DISPLAY_TEXT_RESOLVER_VERSION,
    NORMALIZATION_INDEX_FORMAT_VERSION,
    OPTIONAL_WEB_INDEX_FORMAT_VERSION,
    parse_nonnegative_integer,
)
from .sqlite_errors import is_optional_search_capability_missing
from .utils import compact_json


MAX_QUERY_LENGTH = 500
MAX_API_LIMIT = 100
MAX_MESSAGE_LIMIT = 300
MAX_AROUND_NODE_ROWS = 8000
HIGHLIGHT_TERM_LIMIT = 10
HIGHLIGHT_RANGE_LIMIT = 50
HIGHLIGHT_MESSAGE_SCAN_CHARS = 100_000
READER_MIN_TEXT_HYDRATION_CHARS = 4096
MAX_API_TITLE_CHARS = 4096
MAX_API_SOURCE_CHARS = 4096
MAX_API_ROLE_CHARS = 256
MAX_API_AUTHOR_CHARS = 4096
MAX_API_CONTENT_TYPE_CHARS = 256
MAX_DISPLAY_CURSOR_LENGTH = 1024
MAX_LEGACY_DISPLAY_OFFSET = 1_048_576
MAX_SQLITE_CURSOR_OFFSET = 9_223_372_036_854_775_807


class DisplayCursorError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _encode_display_cursor(rowid: int, revision: str, byte_offset: int, char_offset: int) -> str:
    payload = json.dumps([rowid, revision, byte_offset, char_offset], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_display_cursor(value: str) -> tuple[int, str, int, int]:
    if not value or len(value) > MAX_DISPLAY_CURSOR_LENGTH:
        raise DisplayCursorError("invalid_display_cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        rowid, revision, byte_offset, char_offset = payload
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DisplayCursorError("invalid_display_cursor") from exc
    if (
        isinstance(rowid, bool) or not isinstance(rowid, int) or rowid < 1
        or not isinstance(revision, str) or len(revision) > 256
        or isinstance(byte_offset, bool) or not isinstance(byte_offset, int)
        or byte_offset < 0 or byte_offset > MAX_SQLITE_CURSOR_OFFSET
        or isinstance(char_offset, bool) or not isinstance(char_offset, int)
        or char_offset < 0 or char_offset > MAX_SQLITE_CURSOR_OFFSET
    ):
        raise DisplayCursorError("invalid_display_cursor")
    return rowid, revision, byte_offset, char_offset


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
    display_chunk_chars: int = 65_536


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


def normalize_search_text(value: str | None) -> str:
    """Normalize query/content for human search without changing stored archive text."""
    text = unicodedata.normalize("NFKC", normalize_display_text(value))
    text = text.translate(NORMALIZE_TRANSLATION).casefold()
    return re.sub(r"\s+", " ", text).strip()


def search_fragment_match(value: str | None, fragment: str | None, match_mode: str = "contains") -> int:
    """SQLite-friendly predicate for contains and conservative whole-word matching."""
    return 1 if _fragment_matches(value or "", fragment or "", match_mode) else 0


def _fragment_matches(value: str, fragment: str, match_mode: str) -> bool:
    normalized = normalize_search_text(value)
    needle = normalize_search_text(fragment)
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


def _ensure_search_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text)


def _sql_display_text(alias: str = "n") -> str:
    return f"web_display_text({alias}.content_text, substr({alias}.raw_message_json, 1, 200001))"


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
        SELECT c.conversation_id, c.title, c.create_time, c.update_time, c.current_node,
               c.source_file
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
            f"""SELECT c.conversation_id, c.title, c.create_time, c.update_time,
                       c.current_node, c.source_file
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
) -> dict[str, Any]:
    _ensure_search_functions(conn)
    limit = _bounded_limit(limit, max_page_limit)
    offset = max(0, offset)
    if parsed.scope == "title":
        return _page_payload([], 0, limit, offset, extra={"total_exact": True})
    has_message_text = bool(parsed.phrases or parsed.terms or parsed.required_phrases)
    if not has_message_text:
        return _page_payload([], 0, limit, offset, extra={"total_exact": True})
    if parsed.path == "current":
        if conversation_id:
            ensure_effective_current_views(conn, [conversation_id])
        else:
            ensure_effective_current_views(conn, _message_current_candidate_ids(conn, parsed))
    try:
        rows, total = _message_search_page_rows(conn, parsed, conversation_id, limit, offset, order, count_total=count_total)
        diagnostics = _message_search_diagnostics(conn, parsed, used_trigram=True)
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        rows, total = _message_search_page_rows(conn, parsed, conversation_id, limit, offset, order, use_trigram=False, count_total=count_total)
        diagnostics = _message_search_diagnostics(conn, parsed, used_trigram=False)
    result_ids = [str(row["conversation_id"]) for row in rows]
    fallback_map = _fallback_map_for_conversations(conn, result_ids) if rows else {}
    effective_pairs = _effective_pairs_for_rows(conn, rows) if rows else set()
    items = [
        _message_search_payload(
            row,
            parsed,
            row["match_reason"] or ("exact phrase" if (parsed.phrases or parsed.required_phrases) else "substring"),
            row["bm25_score"],
            current_path_fallback_to_all=fallback_map.get(row["conversation_id"], False),
            effective_visible_in_current_view=(str(row["conversation_id"]), str(row["node_id"])) in effective_pairs,
        )
        for row in rows
    ]
    result = _page_payload(items, total, limit, offset, extra={"total_exact": bool(count_total)})
    result["diagnostics"] = diagnostics
    return result


def search_conversations(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    *,
    limit: int = 50,
    offset: int = 0,
    sort: str = "relevance",
    selected_id: str | None = None,
) -> dict[str, Any]:
    _ensure_search_functions(conn)
    if not parsed.has_search_context():
        return list_conversations(conn, limit=limit, offset=offset, sort=sort, after=parsed.after, before=parsed.before, selected_id=selected_id)
    if _conversation_search_requires_global_current(parsed):
        ensure_effective_current_views(conn, _conversation_current_candidate_ids(conn, parsed))
    limit = _bounded_limit(limit, MAX_API_LIMIT)
    offset = max(0, offset)
    try:
        items, total = _conversation_search_page(conn, parsed, limit, offset, sort)
        diagnostics = _conversation_search_diagnostics(conn, parsed, used_trigram=True)
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        items, total = _conversation_search_page(conn, parsed, limit, offset, sort, use_trigram=False)
        diagnostics = _conversation_search_diagnostics(conn, parsed, used_trigram=False)
    _add_counts_and_path_metadata(conn, items)
    for conv in items:
        conv["reasons"] = sorted(conv["reasons"])
    _batch_conversation_enrichment(conn, parsed, items)
    selected_in_results = None
    selected_item = None
    if selected_id:
        selected_in_results = _conversation_search_contains(conn, parsed, selected_id)
        if selected_in_results and not any(item["conversation_id"] == selected_id for item in items):
            selected_item = _conversation_search_item(conn, parsed, selected_id)
    if selected_item:
        _add_counts_and_path_metadata(conn, [selected_item])
        _batch_conversation_enrichment(conn, parsed, [selected_item])
    result = _page_payload(items, total, limit, offset, selected_in_results=selected_in_results, selected_item=selected_item)
    result["diagnostics"] = diagnostics
    return result


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


def _message_current_candidate_ids(conn: sqlite3.Connection, parsed: ParsedQuery) -> list[str]:
    """Find global message-search conversation candidates without path membership."""

    candidate = _path_independent_candidate_query(parsed, keep_hit_excludes=True)
    try:
        base_sql, params = _message_search_base_select(conn, candidate, None, use_trigram=True)
        rows = conn.execute(
            f"SELECT DISTINCT conversation_id FROM ({base_sql}) candidates",
            params,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        base_sql, params = _message_search_base_select(conn, candidate, None, use_trigram=False)
        rows = conn.execute(
            f"SELECT DISTINCT conversation_id FROM ({base_sql}) candidates",
            params,
        ).fetchall()
    return [str(row[0]) for row in rows]


def _conversation_current_candidate_ids(conn: sqlite3.Connection, parsed: ParsedQuery) -> list[str] | None:
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

    def select_ids(*, use_trigram: bool) -> list[str]:
        if not has_positive:
            where, params = _filter_conversation_where(candidate)
            rows = conn.execute(
                f"SELECT c.conversation_id FROM conversations c {where}",
                params,
            ).fetchall()
            return [str(row[0]) for row in rows]

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
            return []
        combined = " UNION ALL ".join(parts)
        rows = conn.execute(
            f"SELECT DISTINCT conversation_id FROM ({combined}) candidates",
            params,
        ).fetchall()
        return [str(row[0]) for row in rows]

    try:
        return select_ids(use_trigram=True)
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        return select_ids(use_trigram=False)


def _is_title_only_candidate_context(parsed: ParsedQuery) -> bool:
    return (
        parsed.scope != "message"
        and bool(parsed.title or parsed.required_title)
        and not (parsed.terms or parsed.phrases or parsed.required_phrases or parsed.role)
    )


def get_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any] | None:
    ensure_effective_current_views(conn, [conversation_id])
    row = conn.execute(
        """
        SELECT c.*, COUNT(n.node_id) AS node_count,
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


def get_message_display_chunk(
    conn: sqlite3.Connection,
    conversation_id: str,
    node_id: str,
    *,
    offset: int,
    limit: int,
    cursor: str | None = None,
) -> dict[str, Any] | None:
    """Return a bounded display-text chunk without exposing raw JSON."""

    budget = reader_budget()
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), budget.display_chunk_chars))
    if not cursor and offset > MAX_LEGACY_DISPLAY_OFFSET:
        raise DisplayCursorError("display_cursor_required")
    row = conn.execute(
        """
        SELECT n.rowid AS storage_rowid,
               COALESCE(g.generation, 0) AS message_generation
        FROM conversation_nodes n
        LEFT JOIN archive_generations g ON g.name = 'message'
        WHERE n.conversation_id = ? AND n.node_id = ?
        """,
        (conversation_id, node_id),
    ).fetchone()
    if row is None:
        return None
    storage_rowid = int(row["storage_rowid"])
    revision = f"generation:{int(row['message_generation'] or 0)}"
    prefix_bytes = b""
    if cursor:
        cursor_rowid, cursor_revision, byte_offset, char_offset = _decode_display_cursor(cursor)
        if cursor_rowid != storage_rowid or cursor_revision != revision or char_offset != offset:
            raise DisplayCursorError("display_cursor_stale")
        prefix_bytes = b"cursor"
        content_prefix = ""
    else:
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
    resolver_input_truncated = False
    next_cursor = None
    canonical_has_more = False
    if prefix_bytes and not _is_placeholder_text(content_prefix):
        if not cursor:
            byte_offset = 0
            char_offset = 0
            if offset:
                # Compatibility path for old clients. It is NUL-safe but scans
                # the requested prefix once; sequential clients use cursor.
                with conn.blobopen("conversation_nodes", "content_text", storage_rowid, readonly=True) as prefix_blob:
                    _discard, byte_offset, _more, invalid = _read_utf8_blob_chunk(prefix_blob, 0, offset)
                    if invalid:
                        byte_offset = min(len(prefix_blob), offset)
                char_offset = offset
        with conn.blobopen("conversation_nodes", "content_text", storage_rowid, readonly=True) as blob:
            if byte_offset > len(blob):
                raise DisplayCursorError("invalid_display_cursor")
            chunk, next_byte, has_more, invalid_utf8 = _read_utf8_blob_chunk(blob, byte_offset, limit)
        canonical_has_more = has_more
        total_chars = offset + len(chunk)
        total_exact = not has_more
        if has_more and not invalid_utf8:
            next_cursor = _encode_display_cursor(storage_rowid, revision, next_byte, total_chars)
        source = "canonical"
    else:
        raw_row = conn.execute(
            """
            SELECT length(CAST(COALESCE(raw_message_json, '') AS BLOB)) AS raw_bytes,
                   substr(CAST(COALESCE(raw_message_json, '') AS BLOB), 1, 800004) AS raw_bounded_bytes
            FROM conversation_nodes
            WHERE rowid = ?
            """,
            (storage_rowid,),
        ).fetchone()
        raw_bytes = int(raw_row["raw_bytes"] or 0)
        raw_bounded_bytes = bytes(raw_row["raw_bounded_bytes"] or b"")
        raw_bounded = normalize_display_text(raw_bounded_bytes.decode("utf-8", errors="replace"))
        resolver_input_truncated = raw_bytes > len(raw_bounded_bytes) or len(raw_bounded) > 200_000
        canonical = content_prefix
        recovered = recover_message_display_text(
            canonical,
            raw_bounded[:200_000] if not resolver_input_truncated else "",
        )
        total_chars = len(recovered)
        total_exact = not resolver_input_truncated
        chunk = recovered[offset : offset + limit]
        source = "raw_fallback" if recovered != canonical else "canonical_placeholder"
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
        SELECT node_id,
               length(COALESCE(content_text, '')) AS content_text_total_chars,
               substr(COALESCE(content_text, ''), 1, ?) AS content_text,
               length(COALESCE(raw_message_json, '')) AS raw_message_total_chars,
               substr(COALESCE(raw_message_json, ''), 1, ?) AS raw_message_json
        FROM conversation_nodes
        WHERE conversation_id = ? AND node_id IN ({placeholders})
        """,
        [display_limit + 1, raw_limit + 1, conversation_id, *node_ids],
    ).fetchall()
    by_id = {str(row["node_id"]): row for row in text_rows}
    for row in output:
        text_row = by_id[str(row["node_id"])]
        content = str(text_row["content_text"] or "")
        raw = str(text_row["raw_message_json"] or "")
        row["content_text"] = content[:display_limit]
        row["content_text_total_chars"] = int(text_row["content_text_total_chars"] or 0)
        row["content_text_source_truncated"] = len(content) > display_limit
        row["raw_message_json"] = raw[:raw_limit]
        row["raw_message_total_chars"] = int(text_row["raw_message_total_chars"] or 0)
        row["raw_message_source_truncated"] = len(raw) > raw_limit
    return output


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
    _ensure_search_functions(conn)
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
               c.title, c.create_time AS conversation_create_time, c.update_time AS conversation_update_time,
               c.current_node, c.source_file, bm25(message_fts) AS bm25_score
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
) -> tuple[list[sqlite3.Row], int]:
    base_sql, params = _message_search_base_select(conn, parsed, conversation_id, use_trigram=use_trigram)
    query_limit = limit if count_total else limit + 1
    total = conn.execute(f"SELECT COUNT(*) AS c FROM ({base_sql})", params).fetchone()["c"] if count_total else 0
    order_clause = _message_search_order_clause(order, conversation_id, parsed.path)
    order_sql = f"ORDER BY {order_clause}"
    if order == "display" and conversation_id and parsed.path == "current":
        rows = conn.execute(
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
            LIMIT ? OFFSET ?
            """,
            params + [query_limit, offset],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT *
            FROM ({base_sql}) matched
            {order_sql}
            LIMIT ? OFFSET ?
            """,
            params + [query_limit, offset],
        ).fetchall()
    if not count_total:
        has_extra = len(rows) > limit
        rows = rows[:limit]
        total = offset + len(rows) + (1 if has_extra else 0)
    return rows, int(total or 0)


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
                   {_sql_display_text('n')} AS resolved_text,
                   {score_expr} AS candidate_score,
                   {f"COALESCE(mn.content_norm, web_norm({_sql_display_text('n')}))" if has_norm else "NULL"} AS resolved_norm,
                   c.title AS conversation_title,
                   c.create_time AS conversation_create_time,
                   c.update_time AS conversation_update_time,
                   c.current_node AS conversation_current_node,
                   c.source_file AS conversation_source_file
            FROM {source_sql}
            JOIN conversations c ON c.conversation_id = n.conversation_id
            {norm_join}
            WHERE 1 = 1 {where}
            LIMIT -1 OFFSET 0
        )
        SELECT n.conversation_id, n.node_id, n.role, n.create_time, n.update_time,
               n.content_type, n.resolved_text AS content_text, n.is_on_current_path,
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
                    FROM web_index_oversized WHERE kind = 'message'
                ) mk
                JOIN conversation_nodes n
                  ON n.conversation_id = mk.conversation_id AND n.node_id = mk.node_id
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
                FROM web_index_oversized WHERE kind = 'message'
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
    _ensure_search_functions(conn)
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
        SELECT c.conversation_id, c.title, c.create_time, c.update_time, c.current_node, c.source_file
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
) -> tuple[list[dict[str, Any]], int]:
    if not _search_has_positive_body_or_title(parsed):
        return _filter_conversation_page(conn, parsed, limit, offset, sort)
    parts: list[str] = []
    params: list[Any] = []
    has_message_match = bool(parsed.terms or parsed.phrases or parsed.required_phrases or parsed.role)
    if parsed.scope != "title" and has_message_match:
        message_sql, message_params = _message_conversation_select(conn, parsed, use_trigram=use_trigram)
        parts.append(message_sql)
        params.extend(message_params)
    if parsed.scope != "message" and not parsed.role:
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
            SELECT c.conversation_id, c.title, c.create_time, c.update_time, c.current_node, c.source_file,
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
                "title": row["title"],
                "create_time": row["create_time"],
                "update_time": row["update_time"],
                "current_node": row["current_node"],
                "source_file": row["source_file"],
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
        SELECT c.conversation_id, c.title, c.create_time, c.update_time, c.current_node, c.source_file,
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
        SELECT c.conversation_id, c.title, c.create_time, c.update_time, c.current_node, c.source_file
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
        "title": row["title"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "current_node": row["current_node"],
        "source_file": row["source_file"],
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


def _conversation_search_item(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> dict[str, Any] | None:
    try:
        return _conversation_search_item_inner(conn, parsed, conversation_id, use_trigram=True)
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        return _conversation_search_item_inner(conn, parsed, conversation_id, use_trigram=False)


def _conversation_search_item_inner(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str, *, use_trigram: bool) -> dict[str, Any] | None:
    if not _search_has_positive_body_or_title(parsed):
        return _filter_conversation_item(conn, parsed, conversation_id)
    parts: list[str] = []
    params: list[Any] = []
    has_message_match = bool(parsed.terms or parsed.phrases or parsed.required_phrases or parsed.role)
    if parsed.scope != "title" and has_message_match:
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
        SELECT c.conversation_id, c.title, c.create_time, c.update_time, c.current_node, c.source_file,
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
        "title": row["title"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "current_node": row["current_node"],
        "source_file": row["source_file"],
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
                   {_sql_display_text('n')} AS resolved_text,
                   {score_expr} AS candidate_score,
                   {f"COALESCE(mn.content_norm, web_norm({_sql_display_text('n')}))" if has_norm else "NULL"} AS resolved_norm
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
) -> None:
    """Populate snippets and hit visibility with one query for the current page."""

    for item in items:
        item["snippets"] = []
        item["has_title_hits"] = bool(item.get("title_match"))
        item["has_internal_hits"] = False
        item["has_branch_hits"] = False
    wanted = [item for item in items if item.get("message_match") and item.get("hit_count")]
    if not wanted or parsed.scope == "title" or not (parsed.terms or parsed.phrases or parsed.required_phrases):
        return
    ids = [item["conversation_id"] for item in wanted]
    placeholders = ",".join("?" for _ in ids)
    try:
        base_sql, params = _message_search_base_select(conn, parsed, None, use_trigram=True)
        rows = conn.execute(
            f"""
            WITH matched AS ({base_sql}),
            page_matches AS (
                SELECT matched.*,
                       row_number() OVER (
                           PARTITION BY matched.conversation_id
                           ORDER BY matched.create_time IS NULL,
                                    COALESCE(matched.create_time, matched.update_time, 0),
                                    matched.node_id
                       ) AS snippet_rank
                FROM matched
                WHERE matched.conversation_id IN ({placeholders})
            )
            SELECT * FROM page_matches
            ORDER BY conversation_id, snippet_rank
            """,
            params + ids,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not is_optional_search_capability_missing(exc):
            raise
        base_sql, params = _message_search_base_select(conn, parsed, None, use_trigram=False)
        rows = conn.execute(
            f"""
            WITH matched AS ({base_sql}),
            page_matches AS (
                SELECT matched.*,
                       row_number() OVER (
                           PARTITION BY matched.conversation_id
                           ORDER BY matched.create_time IS NULL,
                                    COALESCE(matched.create_time, matched.update_time, 0),
                                    matched.node_id
                       ) AS snippet_rank
                FROM matched
                WHERE matched.conversation_id IN ({placeholders})
            )
            SELECT * FROM page_matches
            ORDER BY conversation_id, snippet_rank
            """,
            params + ids,
        ).fetchall()
    by_id = {item["conversation_id"]: item for item in wanted}
    for row in rows:
        item = by_id.get(row["conversation_id"])
        if item is None:
            continue
        internal = _is_internal_message(row["role"], row["content_type"], row["content_text"])
        effective_visible = bool(row["effective_visible_in_current_view"])
        item["has_internal_hits"] = bool(item["has_internal_hits"] or internal)
        item["has_branch_hits"] = bool(item["has_branch_hits"] or not effective_visible)
        if int(row["snippet_rank"] or 0) <= 3:
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


def _conversation_search_contains(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM conversations WHERE conversation_id = ? LIMIT 1", (conversation_id,)).fetchone()
    return row is not None and _conversation_search_item(conn, parsed, conversation_id) is not None


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
        f"NOT (trim({display_expression}) LIKE '[non-text content:%' "
        f"OR trim({display_expression}) LIKE '[non-text part:%')"
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


def _message_search_payload(
    row: sqlite3.Row,
    parsed: ParsedQuery,
    reason: str,
    bm25_score: float | None,
    *,
    current_path_fallback_to_all: bool = False,
    effective_visible_in_current_view: bool | None = None,
) -> dict[str, Any]:
    text = row["content_text"] or ""
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
        if phrase and _fragment_matches(text, phrase, parsed.match_mode):
            score += 35.0
            reasons.add("exact phrase")
    for term in parsed.terms:
        if term and _fragment_matches(text, term, parsed.match_mode):
            score += 12.0
            reasons.add("message match")
    if bm25_score is not None:
        score += max(0.0, 25.0 - min(25.0, abs(float(bm25_score))))
    role, role_truncated, role_length = _bounded_api_scalar(row["role"], MAX_API_ROLE_CHARS)
    content_type, content_type_truncated, content_type_length = _bounded_api_scalar(row["content_type"], MAX_API_CONTENT_TYPE_CHARS)
    title, title_truncated, title_length = _bounded_api_scalar(row["title"], MAX_API_TITLE_CHARS)
    source_file, source_truncated, source_length = _bounded_api_scalar(row["source_file"], MAX_API_SOURCE_CHARS)
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
        "snippet": make_snippet(text, _highlight_terms(parsed), parsed.match_mode),
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
    content_source_truncated = bool(row.get("content_text_source_truncated", content_total > len(text)))
    raw_source_truncated = bool(row.get("raw_message_source_truncated", raw_total > len(raw_message_json)))
    parsed_message: Any = RAW_MESSAGE_NOT_PARSED
    parsed_ok = False
    if raw_message_json and not raw_source_truncated and len(raw_message_json) <= 200_000:
        try:
            parsed_message = json_loads(raw_message_json)
            parsed_ok = True
        except (TypeError, ValueError):
            parsed_message = None
    raw_preview = _raw_preview(raw_message_json, parsed_message=parsed_message, parsed_ok=parsed_ok)
    resolved_display_text = recover_message_display_text(
        text,
        raw_message_json,
        parsed_message=parsed_message if parsed_message is not RAW_MESSAGE_NOT_PARSED else None,
    )
    placeholder_text = _is_placeholder_text(text)
    if text and not placeholder_text:
        display_total = content_total
        display_total_exact = True
    elif resolved_display_text != text:
        display_total = len(resolved_display_text)
        display_total_exact = not raw_source_truncated
    else:
        display_total = content_total if text else len(resolved_display_text)
        display_total_exact = not raw_source_truncated
    display_text = resolved_display_text[:display_limit] if display_limit is not None else resolved_display_text
    display_truncated = (
        content_source_truncated
        or (raw_source_truncated and (not text or placeholder_text))
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
    text = display_text if display_text is not None else recover_message_display_text(row["content_text"], row["raw_message_json"])
    if not text or _is_placeholder_text(text):
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
    limit: int = 20000,
    *,
    parsed_message: Any = RAW_MESSAGE_NOT_PARSED,
    parsed_ok: bool = False,
) -> str:
    if not raw_message_json:
        return ""
    if parsed_ok:
        return compact_json(_sanitize_raw_preview(parsed_message), limit)
    if parsed_message is RAW_MESSAGE_NOT_PARSED and len(raw_message_json) <= 200_000:
        try:
            return compact_json(_sanitize_raw_preview(json_loads(raw_message_json)), limit)
        except ValueError:
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


def _is_placeholder_text(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("[non-text content:") or stripped.startswith("[non-text part:")


def _sanitize_raw_preview(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sanitize_raw_preview(v) for k, v in value.items() if k != "metadata"}
    if isinstance(value, list):
        return [_sanitize_raw_preview(item) for item in value]
    return value


def _bounded_api_scalar(value: Any, limit: int) -> tuple[str | None, bool, int]:
    if value is None:
        return None, False, 0
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


def make_snippet(text: str, terms: list[tuple[str, str]], match_mode: str = "contains", radius: int = 80) -> str:
    if not text:
        return ""
    normalized, spans = _normalized_with_codepoint_spans(text)
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
    end = min(len(text), center + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ") + suffix


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


def _normalized_span_units(text: str) -> list[tuple[str, int, int, int, int]]:
    raw_units: list[tuple[str, int, int, int, int]] = []
    utf16_index = 0
    index = 0
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
            raw_units.append((normalized_char, start, index, char_start, utf16_index))

    units: list[tuple[str, int, int, int, int]] = []
    pending_space: tuple[int, int, int, int] | None = None
    for normalized_char, start, end, utf16_start, utf16_end in raw_units:
        if normalized_char.isspace():
            if pending_space is None:
                pending_space = (start, end, utf16_start, utf16_end)
            else:
                pending_space = (
                    min(pending_space[0], start),
                    max(pending_space[1], end),
                    min(pending_space[2], utf16_start),
                    max(pending_space[3], utf16_end),
                )
            continue
        if pending_space is not None and units:
            space_start, space_end, space_utf16_start, space_utf16_end = pending_space
            units.append((" ", space_start, space_end, space_utf16_start, space_utf16_end))
        pending_space = None
        units.append((normalized_char, start, end, utf16_start, utf16_end))
    return units


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
