from __future__ import annotations

import re
import json
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from .parser import extract_message_content
from .utils import compact_json


MAX_QUERY_LENGTH = 500
MAX_CANDIDATES = 3000
MAX_API_LIMIT = 100
MAX_MESSAGE_LIMIT = 300
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
    text = unicodedata.normalize("NFKC", value or "")
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
) -> ParsedQuery:
    text = normalize_search_text(raw).strip()
    if len(text) > MAX_QUERY_LENGTH:
        text = text[:MAX_QUERY_LENGTH]
    parsed = ParsedQuery(
        original=text,
        path=path_default if path_default in {"current", "all"} else "current",
        scope=scope if scope in {"all", "title", "message"} else "all",
        match_mode=match_mode if match_mode in {"contains", "word"} else "contains",
    )
    if role:
        parsed.role = role.casefold()
    if title:
        parsed.required_title = normalize_search_text(title)
    if exact:
        parsed.required_phrases.append(normalize_search_text(exact))
    if exclude:
        parsed.exclude.extend(item for item in _split_filter_fragments(exclude) if item)
    if source:
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
            if key == "role" and value:
                parsed.role = value.casefold()
                continue
            if key == "title" and value:
                parsed.title = normalize_search_text(value)
                continue
            if key == "source" and value:
                parsed.source = normalize_search_text(value)
                continue
            if key == "path" and value in {"current", "all"}:
                parsed.path = value
                continue
            if key == "scope" and value in {"all", "title", "message"}:
                parsed.scope = value
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
            key = normalize_search_text(head)
            index += 1
            if index < length and text[index] == '"':
                value, index = _read_quoted_token(text, index + 1)
                tokens.append((value, True, negated, key))
            else:
                value_start = index
                while index < length and not text[index].isspace():
                    index += 1
                tokens.append((text[value_start:index], False, negated, key))
            continue
        if index < length and text[index] == '"':
            value, index = _read_quoted_token(text, index + 1)
            tokens.append((f"{head}{value}", False, negated, None))
            continue
        tokens.append((("-" if negated else "") + head if negated and not head.startswith("-") else head, False, False, None))
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
    counts = _node_counts_for_conversations(conn, [row["conversation_id"] for row in rows])
    total = conn.execute(f"SELECT COUNT(*) AS c FROM conversations c {where}", params).fetchone()["c"]
    return _page_payload(
        [_conversation_summary_with_counts(row, counts.get(row["conversation_id"], {})) for row in rows],
        total,
        limit,
        offset,
        selected_in_results=_selected_in_conversation_filter(conn, where, params, selected_id),
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
        return _page_payload([], 0, limit, offset)
    has_message_text = bool(parsed.phrases or parsed.terms or parsed.required_phrases)
    if not has_message_text:
        return _page_payload([], 0, limit, offset)
    try:
        rows, total = _message_search_page_rows(conn, parsed, conversation_id, limit, offset, order, count_total=count_total)
    except sqlite3.OperationalError:
        rows, total = _message_search_page_rows(conn, parsed, conversation_id, limit, offset, order, use_trigram=False, count_total=count_total)
    items = [
        _message_search_payload(row, parsed, row["match_reason"] or ("exact phrase" if (parsed.phrases or parsed.required_phrases) else "substring"), row["bm25_score"])
        for row in rows
    ]
    return _page_payload(items, total, limit, offset)


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
    limit = _bounded_limit(limit, MAX_API_LIMIT)
    offset = max(0, offset)
    try:
        items, total = _conversation_search_page(conn, parsed, limit, offset, sort)
    except sqlite3.OperationalError:
        items, total = _conversation_search_page(conn, parsed, limit, offset, sort, use_trigram=False)
    used_fuzzy = False
    if not items and parsed.terms and parsed.scope != "message" and parsed.match_mode != "word" and not parsed.has_effective_filters():
        used_fuzzy = True
        items = _fuzzy_title_items(conn, parsed, 30)
        total = len(items)
    for conv in items:
        conv["reasons"] = sorted(conv["reasons"])
        conv["snippets"] = _conversation_snippets(conn, parsed, conv["conversation_id"]) if conv.get("hit_count") else []
        _add_conversation_visibility_metadata(conn, parsed, conv)
    selected_in_results = None
    selected_item = None
    if selected_id:
        selected_item = _fuzzy_title_item(conn, parsed, selected_id) if used_fuzzy else None
        selected_in_results = selected_item is not None if used_fuzzy else _conversation_search_contains(conn, parsed, selected_id)
        if selected_in_results and not any(item["conversation_id"] == selected_id for item in items):
            selected_item = selected_item or _conversation_search_item(conn, parsed, selected_id)
            if selected_item and selected_item.get("hit_count"):
                selected_item["snippets"] = _conversation_snippets(conn, parsed, selected_id)
            if selected_item:
                _add_conversation_visibility_metadata(conn, parsed, selected_item)
    return _page_payload(items, total, limit, offset, selected_in_results=selected_in_results, selected_item=selected_item)


def _search_has_positive_body_or_title(parsed: ParsedQuery) -> bool:
    return bool(parsed.terms or parsed.phrases or parsed.required_phrases or parsed.title or parsed.required_title)


def get_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any] | None:
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
    return _conversation_summary(row) if row else None


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
) -> dict[str, Any]:
    limit = _bounded_limit(limit, MAX_MESSAGE_LIMIT)
    offset = max(0, offset)
    if around_node_id:
        rows = _conversation_rows(conn, conversation_id)
        ordered = _order_nodes_for_display(rows, path)
        parsed = highlight_parsed or parse_query(highlight_query or "", match_mode=match_mode)
        conversation = get_conversation(conn, conversation_id)
        total = len(ordered)
        visibility_counts = _message_visibility_counts(ordered)
        index = next((idx for idx, row in enumerate(ordered) if row["node_id"] == around_node_id), None)
        if index is not None:
            offset = max(0, min(index, max(0, total - limit)))
        window = ordered[offset : offset + limit]
        return _page_payload(
            [_message_payload(row, _highlight_terms(parsed) if _message_row_matches_highlight(row, conversation, parsed, path) else []) for row in window],
            total,
            limit,
            offset,
            extra=visibility_counts,
        )
    rows, total = _paged_conversation_rows(conn, conversation_id, path, limit, offset)
    parsed = highlight_parsed or parse_query(highlight_query or "", match_mode=match_mode)
    conversation = get_conversation(conn, conversation_id)
    return _page_payload(
        [_message_payload(row, _highlight_terms(parsed) if _message_row_matches_highlight(row, conversation, parsed, path) else []) for row in rows],
        total,
        limit,
        offset,
        extra=_message_visibility_counts_for_path(conn, conversation_id, path),
    )


_MESSAGE_SELECT_COLUMNS = """
    node_id, parent_node_id, children_json, message_id, role, author_name,
    create_time, update_time, content_type, content_text, content_hash,
    is_on_current_path, raw_message_json
"""


def _conversation_rows(conn: sqlite3.Connection, conversation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT {_MESSAGE_SELECT_COLUMNS}
        FROM conversation_nodes
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchall()


def _message_visibility_counts_for_path(conn: sqlite3.Connection, conversation_id: str, path: str) -> dict[str, int]:
    path_clause = ""
    params: list[Any] = [conversation_id]
    if path == "current":
        current_total = conn.execute(
            "SELECT COUNT(*) AS c FROM conversation_nodes WHERE conversation_id = ? AND is_on_current_path = 1",
            (conversation_id,),
        ).fetchone()["c"]
        if current_total:
            path_clause = "AND is_on_current_path = 1"
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE
                WHEN message_id IS NULL
                     AND COALESCE(content_text, '') = ''
                     AND COALESCE(raw_message_json, '') = ''
                THEN 1 ELSE 0 END) AS empty_hidden_count,
            SUM(CASE
                WHEN NOT (
                    message_id IS NULL
                    AND COALESCE(content_text, '') = ''
                    AND COALESCE(raw_message_json, '') = ''
                )
                AND (
                    lower(COALESCE(role, '')) IN ('system', 'developer', 'tool', 'tool/system')
                    OR lower(COALESCE(content_type, '')) IN (
                        'user_editable_context',
                        'model_editable_context',
                        'system_context',
                        'developer_context',
                        'thoughts'
                    )
                    OR lower(trim(COALESCE(content_text, ''))) LIKE 'source analysis msg id:%'
                )
                THEN 1 ELSE 0 END) AS internal_hidden_count
        FROM conversation_nodes
        WHERE conversation_id = ? {path_clause}
        """,
        params,
    ).fetchone()
    total = int(row["total"] or 0)
    empty_hidden = int(row["empty_hidden_count"] or 0)
    internal_hidden = int(row["internal_hidden_count"] or 0)
    return {
        "visible_total": max(0, total - empty_hidden - internal_hidden),
        "empty_hidden_count": empty_hidden,
        "internal_hidden_count": internal_hidden,
        "technical_hidden_count": internal_hidden,
    }


def _message_visibility_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
    empty_hidden = 0
    internal_hidden = 0
    technical_hidden = 0
    visible = 0
    for row in rows:
        fields = _message_display_fields(row)
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


def _paged_conversation_rows(conn: sqlite3.Connection, conversation_id: str, path: str, limit: int, offset: int) -> tuple[list[sqlite3.Row], int]:
    if path == "all":
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM conversation_nodes WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT {_MESSAGE_SELECT_COLUMNS}
            FROM conversation_nodes
            WHERE conversation_id = ?
            ORDER BY create_time IS NULL,
                     COALESCE(create_time, update_time, 0),
                     node_id
            LIMIT ? OFFSET ?
            """,
            (conversation_id, limit, offset),
        ).fetchall()
        return rows, total

    current_total = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM conversation_nodes
        WHERE conversation_id = ? AND is_on_current_path = 1
        """,
        (conversation_id,),
    ).fetchone()["c"]
    if not current_total:
        return _paged_conversation_rows(conn, conversation_id, "all", limit, offset)
    rows = conn.execute(
        f"""
        WITH current_nodes AS (
            SELECT {_MESSAGE_SELECT_COLUMNS}
            FROM conversation_nodes
            WHERE conversation_id = ? AND is_on_current_path = 1
        ),
        leaf AS (
            SELECT node_id
            FROM current_nodes
            WHERE node_id NOT IN (
                SELECT parent_node_id FROM current_nodes WHERE parent_node_id IS NOT NULL
            )
            ORDER BY node_id
            LIMIT 1
        ),
        path_nodes(node_id, depth) AS (
            SELECT node_id, 0 FROM leaf
            UNION ALL
            SELECT n.parent_node_id, p.depth + 1
            FROM current_nodes n
            JOIN path_nodes p ON p.node_id = n.node_id
            WHERE n.parent_node_id IS NOT NULL
        )
        SELECT n.*
        FROM current_nodes n
        JOIN path_nodes p ON p.node_id = n.node_id
        ORDER BY p.depth DESC
        LIMIT ? OFFSET ?
        """,
        (conversation_id, limit, offset),
    ).fetchall()
    return rows, current_total


def _display_order_map(conn: sqlite3.Connection, conversation_id: str, path: str) -> dict[str, int]:
    rows = _conversation_rows(conn, conversation_id)
    return {row["node_id"]: index for index, row in enumerate(_order_nodes_for_display(rows, path))}


def _fts_message_rows(conn: sqlite3.Connection, parsed: ParsedQuery, fts_query: str, conversation_id: str | None, limit: int | None) -> list[sqlite3.Row]:
    where, params = _node_filters(parsed, conversation_id)
    if "web_search_match" in where:
        _ensure_search_functions(conn)
    limit_clause, limit_params = _limit_clause(limit)
    order_clause = "ORDER BY bm25(message_fts)" if limit is not None else ""
    return conn.execute(
        f"""
        SELECT n.conversation_id, n.node_id, n.role, n.create_time, n.update_time,
               n.content_type, n.content_text, n.is_on_current_path,
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
            WITH current_nodes AS (
                SELECT node_id, parent_node_id
                FROM conversation_nodes
                WHERE conversation_id = ? AND is_on_current_path = 1
            ),
            leaf AS (
                SELECT node_id
                FROM current_nodes
                WHERE node_id NOT IN (
                    SELECT parent_node_id FROM current_nodes WHERE parent_node_id IS NOT NULL
                )
                ORDER BY node_id
                LIMIT 1
            ),
            path_nodes(node_id, depth) AS (
                SELECT node_id, 0 FROM leaf
                UNION ALL
                SELECT n.parent_node_id, p.depth + 1
                FROM current_nodes n
                JOIN path_nodes p ON p.node_id = n.node_id
                WHERE n.parent_node_id IS NOT NULL
            ),
            matched AS (
                {base_sql}
            )
            SELECT matched.*, p.depth AS display_depth
            FROM matched
            LEFT JOIN path_nodes p ON p.node_id = matched.node_id
            {order_sql}
            LIMIT ? OFFSET ?
            """,
            [conversation_id] + params + [query_limit, offset],
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
    text_clause, text_params = _message_text_filter(parsed, has_norm)
    where += text_clause
    params.extend(text_params)
    norm_join = """
        LEFT JOIN web_message_norm mn
          ON mn.conversation_id = n.conversation_id AND mn.node_id = n.node_id
    """ if has_norm else ""
    sql = f"""
        SELECT n.conversation_id, n.node_id, n.role, n.create_time, n.update_time,
               n.content_type, n.content_text, n.is_on_current_path,
               c.title, c.create_time AS conversation_create_time, c.update_time AS conversation_update_time,
               c.current_node, c.source_file, {score_expr} AS bm25_score, ? AS match_reason
        FROM {source_sql}
        JOIN conversations c ON c.conversation_id = n.conversation_id
        {norm_join}
        WHERE n.content_text IS NOT NULL AND n.content_text <> '' {where}
    """
    return sql, [reason] + source_params + params


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
            return "display_depth DESC, matched.node_id ASC"
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
            column = "tn.title_norm" if has_norm else "COALESCE(c.title, '')"
            positive_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            positive_clauses.append("instr(tn.title_norm, ?) > 0")
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
            column = "tn.title_norm" if has_norm else "COALESCE(c.title, '')"
            filter_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            filter_clauses.append("instr(tn.title_norm, ?) > 0")
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
            column = "tn.title_norm" if has_norm else "COALESCE(c.title, '')"
            filter_clauses.append(f"web_search_match({column}, ?, ?) = 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            filter_clauses.append("instr(tn.title_norm, ?) = 0")
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
    title_filter, title_params = _outer_title_exclude_filter(parsed)
    params.extend(title_params)
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
            {title_filter}
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
        roles = ["tool", "system", "tool/system"] if parsed.role in {"tool/system", "tool_system"} else [parsed.role]
        role_path_clause = "AND rn.is_on_current_path = 1" if parsed.path == "current" else ""
        clauses.append((
            """
            EXISTS (
                SELECT 1
                FROM conversation_nodes rn
                WHERE rn.conversation_id = c.conversation_id
                  {role_path_clause}
                  AND lower(COALESCE(rn.role, '')) IN (""" + ",".join("?" for _ in roles) + """)
            )
            """
        ).replace("{role_path_clause}", role_path_clause))
        params.extend(roles)
    for frag in parsed.exclude:
        clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) = 0")
        params.extend([frag, parsed.match_mode])
        exclude_path_clause = "AND en.is_on_current_path = 1" if parsed.path == "current" else ""
        clauses.append((
            """
            NOT EXISTS (
                SELECT 1
                FROM conversation_nodes en
                WHERE en.conversation_id = c.conversation_id
                  {exclude_path_clause}
                  AND en.content_text IS NOT NULL
                  AND en.content_text <> ''
                  AND web_search_match(en.content_text, ?, ?) > 0
            )
            """
        ).replace("{exclude_path_clause}", exclude_path_clause))
        params.extend([frag, parsed.match_mode])
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _conversation_search_item(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> dict[str, Any] | None:
    try:
        return _conversation_search_item_inner(conn, parsed, conversation_id, use_trigram=True)
    except sqlite3.OperationalError:
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
    title_filter, title_params = _outer_title_exclude_filter(parsed)
    params.extend(title_params)
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
        {title_filter}
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
    text_clause, text_params = _message_text_filter(parsed, has_norm)
    where += text_clause
    params.extend(text_params)
    norm_join = """
        LEFT JOIN web_message_norm mn
          ON mn.conversation_id = n.conversation_id AND mn.node_id = n.node_id
    """ if has_norm else ""
    return (
        f"""
        SELECT n.conversation_id,
               COUNT(*) AS hit_count,
               COUNT(*) * 10.0 + SUM(CASE WHEN n.is_on_current_path = 1 THEN 5.0 ELSE 0.0 END)
                   + MAX(CASE WHEN {score_expr} IS NULL THEN 0.0 ELSE 25.0 - min(25.0, abs({score_expr})) END) AS score,
               1 AS message_match,
               0 AS title_match
        FROM {source_sql}
        JOIN conversations c ON c.conversation_id = n.conversation_id
        {norm_join}
        WHERE n.content_text IS NOT NULL AND n.content_text <> '' {where}
        GROUP BY n.conversation_id
        """,
        source_params + params,
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
                ) tk
                JOIN conversations c ON c.conversation_id = tk.conversation_id
            """
        else:
            source_sql = """
                (
                    SELECT rowid AS conversation_rowid, rank AS title_rank
                    FROM web_title_trigram
                    WHERE web_title_trigram MATCH ?
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
    try:
        rows = _substring_message_rows(conn, parsed, conversation_id, 3)
    except sqlite3.OperationalError:
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
                "is_internal": _is_internal_message(row["role"], row["content_type"], row["content_text"]),
            }
        )
    return snippets


def _add_conversation_visibility_metadata(conn: sqlite3.Connection, parsed: ParsedQuery, item: dict[str, Any]) -> None:
    item["has_title_hits"] = bool(item.get("title_match"))
    item["has_internal_hits"] = False
    item["has_branch_hits"] = False
    if not item.get("message_match"):
        return
    try:
        flags = _conversation_message_visibility_flags(conn, parsed, item["conversation_id"])
    except sqlite3.OperationalError:
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
                WHEN lower(COALESCE(role, '')) IN ('system', 'developer', 'tool', 'tool/system')
                     OR lower(COALESCE(content_type, '')) IN (
                         'user_editable_context',
                         'model_editable_context',
                         'system_context',
                         'developer_context',
                         'thoughts'
                     )
                     OR lower(trim(COALESCE(content_text, ''))) LIKE 'source analysis msg id:%'
                THEN 1 ELSE 0 END) AS has_internal_hits,
            MAX(CASE WHEN is_on_current_path = 0 THEN 1 ELSE 0 END) AS has_branch_hits
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


def _message_text_filter(parsed: ParsedQuery, has_norm: bool) -> tuple[str, list[Any]]:
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
            column = "mn.content_norm" if has_norm else "n.content_text"
            positive_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([norm if has_norm else frag, parsed.match_mode])
        elif has_norm:
            positive_clauses.append("instr(mn.content_norm, ?) > 0")
            params.append(norm)
        else:
            positive_clauses.append("web_search_match(n.content_text, ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    for frag in parsed.required_phrases:
        if not frag:
            continue
        norm = normalize_search_text(frag)
        if parsed.match_mode == "word":
            column = "mn.content_norm" if has_norm else "n.content_text"
            required_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([norm if has_norm else frag, parsed.match_mode])
        elif has_norm:
            required_clauses.append("instr(mn.content_norm, ?) > 0")
            params.append(norm)
        else:
            required_clauses.append("web_search_match(n.content_text, ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    for frag in parsed.exclude:
        norm = normalize_search_text(frag)
        if parsed.match_mode == "word":
            column = "mn.content_norm" if has_norm else "n.content_text"
            exclude_clauses.append(f"web_search_match({column}, ?, ?) = 0")
            params.extend([norm if has_norm else frag, parsed.match_mode])
        elif has_norm:
            exclude_clauses.append("instr(mn.content_norm, ?) = 0")
            params.append(norm)
        else:
            exclude_clauses.append("web_search_match(n.content_text, ?, ?) = 0")
            params.extend([frag, parsed.match_mode])
    clauses = []
    if positive_clauses:
        clauses.append("(" + (" OR ".join(positive_clauses) if parsed.or_mode else " AND ".join(positive_clauses)) + ")")
    clauses.extend(required_clauses)
    clauses.extend(exclude_clauses)
    clauses.append("NOT (trim(COALESCE(n.content_text, '')) LIKE '[non-text content:%' OR trim(COALESCE(n.content_text, '')) LIKE '[non-text part:%')")
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
            column = "tn.title_norm" if has_norm else "COALESCE(c.title, '')"
            positive_clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            positive_clauses.append("instr(tn.title_norm, ?) > 0")
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
            column = "tn.title_norm" if has_norm else "COALESCE(c.title, '')"
            clauses.append(f"web_search_match({column}, ?, ?) > 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            clauses.append("instr(tn.title_norm, ?) > 0")
            params.append(normalize_search_text(frag))
        else:
            clauses.append("web_search_match(COALESCE(c.title, ''), ?, ?) > 0")
            params.extend([frag, parsed.match_mode])
    if parsed.source:
        clauses.append("web_search_match(COALESCE(c.source_file, ''), ?, 'contains') > 0")
        params.append(parsed.source)
    for frag in parsed.exclude:
        if parsed.match_mode == "word":
            column = "tn.title_norm" if has_norm else "COALESCE(c.title, '')"
            clauses.append(f"web_search_match({column}, ?, ?) = 0")
            params.extend([normalize_search_text(frag) if has_norm else frag, parsed.match_mode])
        elif has_norm:
            clauses.append("instr(tn.title_norm, ?) = 0")
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


def _fuzzy_title_rows(conn: sqlite3.Connection, parsed: ParsedQuery, limit: int) -> list[dict[str, Any]]:
    needle = normalize_search_text(" ".join(parsed.terms)).strip()
    if len(needle) < 3 or parsed.role:
        return []
    where, params = _conversation_time_where(parsed.after, parsed.before)
    clauses = []
    if parsed.source:
        clauses.append("web_search_match(COALESCE(c.source_file, ''), ?, 'contains') > 0")
        params.append(parsed.source)
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
    if clauses:
        where += (" AND " if where else "WHERE ") + " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT c.conversation_id, c.title, c.create_time, c.update_time, c.current_node, c.source_file
        FROM conversations c
        {where}
        ORDER BY COALESCE(c.update_time, c.create_time, 0) DESC
        LIMIT 2000
        """,
        params,
    ).fetchall()
    scored = []
    for row in rows:
        title = normalize_search_text(row["title"] or "")
        score = SequenceMatcher(None, needle, title).ratio() * 45
        if score >= 18:
            item = dict(row)
            item["score"] = score
            scored.append(item)
    scored.sort(key=lambda row: (-row["score"], -(row["update_time"] or row["create_time"] or 0), row["conversation_id"]))
    return scored[:limit]


def _fuzzy_title_items(conn: sqlite3.Connection, parsed: ParsedQuery, limit: int) -> list[dict[str, Any]]:
    items = []
    for row in _fuzzy_title_rows(conn, parsed, limit):
        if _title_has_excluded(row.get("title"), parsed):
            continue
        items.append(
            {
                "conversation_id": row["conversation_id"],
                "title": row["title"],
                "create_time": row["create_time"],
                "update_time": row["update_time"],
                "current_node": row["current_node"],
                "source_file": row["source_file"],
                "hit_count": 0,
                "snippets": [],
                "reasons": ["fuzzy title"],
                "score": float(row["score"] or 0),
                "message_match": False,
                "title_match": True,
            }
        )
    return items


def _fuzzy_title_item(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> dict[str, Any] | None:
    for item in _fuzzy_title_items(conn, parsed, 2000):
        if item["conversation_id"] == conversation_id:
            return item
    return None


def _title_has_excluded(title: str | None, parsed: ParsedQuery) -> bool:
    return any(term and _fragment_matches(title or "", term, parsed.match_mode) for term in parsed.exclude)


def _node_filters(parsed: ParsedQuery, conversation_id: str | None) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    if conversation_id:
        clauses.append("n.conversation_id = ?")
        params.append(conversation_id)
    if parsed.role:
        roles = ["tool", "system", "tool/system"] if parsed.role in {"tool/system", "tool_system"} else [parsed.role]
        clauses.append("lower(COALESCE(n.role, '')) IN (" + ",".join("?" for _ in roles) + ")")
        params.extend(roles)
    if parsed.path == "current":
        clauses.append("n.is_on_current_path = 1")
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


def _message_search_payload(row: sqlite3.Row, parsed: ParsedQuery, reason: str, bm25_score: float | None) -> dict[str, Any]:
    text = row["content_text"] or ""
    reasons = {reason}
    score = 10.0
    if row["is_on_current_path"]:
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
    return {
        "conversation_id": row["conversation_id"],
        "node_id": row["node_id"],
        "role": row["role"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "content_type": row["content_type"],
        "content_text": text,
        "snippet": make_snippet(text, _highlight_terms(parsed), parsed.match_mode),
        "is_on_current_path": bool(row["is_on_current_path"]),
        "is_internal": _is_internal_message(row["role"], row["content_type"], text),
        "title": row["title"],
        "conversation_create_time": row["conversation_create_time"],
        "conversation_update_time": row["conversation_update_time"],
        "current_node": row["current_node"],
        "source_file": row["source_file"],
        "reasons": sorted(reasons),
        "score": score,
    }


def _message_payload(row: sqlite3.Row, terms: list[str]) -> dict[str, Any]:
    fields = _message_display_fields(row)
    return {
        "node_id": row["node_id"],
        "parent_node_id": row["parent_node_id"],
        "children_json": row["children_json"],
        "message_id": row["message_id"],
        "role": row["role"],
        "author_name": row["author_name"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "content_type": row["content_type"],
        "content_text": fields["content_text"],
        "display_text": fields["display_text"],
        "render_text": fields["display_text"],
        "has_text": bool(fields["display_text"]),
        "has_raw": bool(fields["raw_preview"]),
        "raw_preview": fields["raw_preview"],
        "content_hash": row["content_hash"],
        "is_on_current_path": bool(row["is_on_current_path"]),
        "is_internal": fields["is_internal"],
        "is_empty_mapping_node": fields["is_empty_mapping_node"],
        "highlight_ranges": highlight_ranges(fields["display_text"], terms),
    }


def _message_display_fields(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    text = row["content_text"] or ""
    raw_preview = _raw_preview(row["raw_message_json"])
    raw_text = ""
    if (not text or _is_placeholder_text(text)) and row["raw_message_json"]:
        raw_text = _text_from_raw_message(row["raw_message_json"])
    display_text = raw_text or text
    is_empty_mapping_node = not row["message_id"] and not display_text and not raw_preview
    is_technical = _is_internal_message(row["role"], row["content_type"], display_text)
    return {
        "content_text": text,
        "display_text": display_text,
        "raw_preview": raw_preview,
        "is_empty_mapping_node": is_empty_mapping_node,
        "is_technical": is_technical,
        "is_internal": is_technical or is_empty_mapping_node,
    }


def _message_row_matches_highlight(row: sqlite3.Row, conversation: dict[str, Any] | None, parsed: ParsedQuery, path: str) -> bool:
    if not (parsed.terms or parsed.phrases or parsed.required_phrases):
        return False
    if parsed.scope == "title":
        return False
    if parsed.role:
        roles = {"tool", "system", "tool/system"} if parsed.role in {"tool/system", "tool_system"} else {parsed.role}
        if (row["role"] or "").casefold() not in roles:
            return False
    if parsed.path == "current" and path == "all" and not row["is_on_current_path"]:
        return False
    if conversation:
        title = conversation.get("title") or ""
        if any(excluded and _fragment_matches(title, excluded, parsed.match_mode) for excluded in parsed.exclude):
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
    text = row["content_text"] or ""
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
    try:
        message = json_loads(raw_message_json)
    except ValueError:
        return ""
    if not isinstance(message, dict):
        return ""
    _content_type, text, _notes = extract_message_content(message)
    return text


def _raw_preview(raw_message_json: str | None, limit: int = 20000) -> str:
    if not raw_message_json:
        return ""
    try:
        value = json_loads(raw_message_json)
        return compact_json(_sanitize_raw_preview(value), limit)
    except ValueError:
        return raw_message_json[:limit]


def json_loads(value: str) -> Any:
    return json.loads(value)


def _is_internal_message(role: str | None, content_type: str | None, text: str | None = None) -> bool:
    role_value = (role or "").casefold().replace("_", "/")
    type_value = (content_type or "").casefold()
    text_value = (text or "").strip().casefold()
    return role_value in {"system", "developer", "tool", "tool/system"} or type_value in {
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


def _conversation_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "conversation_id": row["conversation_id"],
        "title": row["title"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "current_node": row["current_node"],
        "source_file": row["source_file"],
        "node_count": row["node_count"],
        "current_path_nodes": row["current_path_nodes"] or 0,
    }


def _node_counts_for_conversations(conn: sqlite3.Connection, conversation_ids: list[str]) -> dict[str, dict[str, int]]:
    """Count nodes only for the current page, avoiding a full-table GROUP BY for empty lists."""
    if not conversation_ids:
        return {}
    placeholders = ",".join("?" for _ in conversation_ids)
    rows = conn.execute(
        f"""
        SELECT conversation_id,
               COUNT(node_id) AS node_count,
               SUM(CASE WHEN is_on_current_path = 1 THEN 1 ELSE 0 END) AS current_path_nodes
        FROM conversation_nodes
        WHERE conversation_id IN ({placeholders})
        GROUP BY conversation_id
        """,
        conversation_ids,
    ).fetchall()
    return {
        row["conversation_id"]: {
            "node_count": int(row["node_count"] or 0),
            "current_path_nodes": int(row["current_path_nodes"] or 0),
        }
        for row in rows
    }


def _conversation_summary_with_counts(row: sqlite3.Row, counts: dict[str, int]) -> dict[str, Any]:
    return {
        "conversation_id": row["conversation_id"],
        "title": row["title"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "current_node": row["current_node"],
        "source_file": row["source_file"],
        "node_count": counts.get("node_count", 0),
        "current_path_nodes": counts.get("current_path_nodes", 0),
    }


def _order_nodes_for_display(rows: list[sqlite3.Row], path: str) -> list[sqlite3.Row]:
    if path == "all":
        return sorted(
            rows,
            key=lambda row: (
                row["create_time"] is None,
                row["create_time"] if row["create_time"] is not None else row["update_time"] if row["update_time"] is not None else 0,
                row["node_id"],
            ),
        )
    by_id = {row["node_id"]: row for row in rows}
    current_candidates = [row for row in rows if row["is_on_current_path"]]
    current = None
    if current_candidates:
        child_parents = {row["parent_node_id"] for row in current_candidates if row["parent_node_id"]}
        leaves = [row["node_id"] for row in current_candidates if row["node_id"] not in child_parents]
        current = sorted(leaves)[0] if leaves else current_candidates[-1]["node_id"]
    ordered = []
    seen: set[str] = set()
    while current and current in by_id and current not in seen:
        seen.add(current)
        row = by_id[current]
        if row["is_on_current_path"]:
            ordered.append(row)
        current = row["parent_node_id"]
    ordered.reverse()
    return ordered or _order_nodes_for_display(rows, "all")


def _highlight_terms(parsed: ParsedQuery) -> list[tuple[str, str]]:
    return [(item, parsed.match_mode) for item in parsed.required_phrases + parsed.phrases + parsed.terms if item]


def highlight_ranges(text: str, terms: list[tuple[str, str]]) -> list[dict[str, int]]:
    # Web highlight ranges are consumed by JavaScript text.slice(), so offsets
    # are UTF-16 code units rather than Python code point indexes.
    normalized, spans = _normalized_with_utf16_spans(text)
    ranges = []
    for term, match_mode in terms[:10]:
        needle = normalize_search_text(term)
        if not needle:
            continue
        start = 0
        token_spans = _word_token_spans(needle) if match_mode == "word" else []
        while len(ranges) < 50:
            idx = normalized.find(needle, start)
            if idx < 0:
                break
            if token_spans and not _candidate_has_word_boundaries(normalized, idx, token_spans, len(needle)):
                start = idx + 1
                continue
            end_idx = idx + len(needle) - 1
            if idx < len(spans) and end_idx < len(spans):
                ranges.append({"start": spans[idx][0], "end": spans[end_idx][1]})
            start = idx + max(1, len(needle))
    ranges.sort(key=lambda item: (item["start"], -item["end"]))
    merged: list[dict[str, int]] = []
    for item in ranges:
        if not merged or item["start"] > merged[-1]["end"]:
            merged.append(dict(item))
        else:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
    return merged


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


def _normalized_with_utf16_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    for normalized_char, _start, _end, utf16_start, utf16_end in _normalized_span_units(text):
        pieces.append(normalized_char)
        spans.append((utf16_start, utf16_end))
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
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row is not None


def _table_has_columns(conn: sqlite3.Connection, name: str, columns: set[str]) -> bool:
    rows = conn.execute(f'PRAGMA table_xinfo("{name}")').fetchall()
    found = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}
    return columns.issubset(found)


def _web_index_metadata_value(conn: sqlite3.Connection, key: str) -> str | None:
    if not _table_exists(conn, "web_index_metadata"):
        return None
    try:
        row = conn.execute("SELECT value FROM web_index_metadata WHERE key = ? LIMIT 1", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row["value"] if row else None


def _has_normalized_message_norm(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "web_message_norm") and (
        _web_index_metadata_value(conn, "message_norm_text") == "normalized"
        or _web_index_metadata_value(conn, "message_trigram_text") == "normalized"
    )


def _has_normalized_title_norm(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "web_title_norm") and (
        _web_index_metadata_value(conn, "title_norm_text") == "normalized"
        or _web_index_metadata_value(conn, "title_trigram_text") == "normalized"
    )


def _has_normalized_message_trigram(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "web_message_trigram") and _web_index_metadata_value(conn, "message_trigram_text") == "normalized"


def _has_normalized_title_trigram(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "web_title_trigram") and _web_index_metadata_value(conn, "title_trigram_text") == "normalized"


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
