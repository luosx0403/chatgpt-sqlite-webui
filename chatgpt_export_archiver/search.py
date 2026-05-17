from __future__ import annotations

import re
import json
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    exclude: list[str] = field(default_factory=list)
    role: str | None = None
    title: str | None = None
    scope: str = "all"
    before: float | None = None
    after: float | None = None
    path: str = "current"
    source: str | None = None
    or_mode: bool = False
    errors: list[str] = field(default_factory=list)

    def has_search_text(self) -> bool:
        return bool(self.terms or self.phrases or self.title)

    def has_non_time_filters(self) -> bool:
        return bool(self.role or self.title or self.source or self.scope in {"title", "message"})


def normalize_search_text(value: str | None) -> str:
    """Normalize query/content for human search without changing stored archive text."""
    text = unicodedata.normalize("NFKC", value or "")
    text = text.translate(NORMALIZE_TRANSLATION).casefold()
    return re.sub(r"\s+", " ", text).strip()


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
) -> ParsedQuery:
    text = normalize_search_text(raw).strip()
    if len(text) > MAX_QUERY_LENGTH:
        text = text[:MAX_QUERY_LENGTH]
    parsed = ParsedQuery(
        original=text,
        path=path_default if path_default in {"current", "all"} else "current",
        scope=scope if scope in {"all", "title", "message"} else "all",
    )
    if role:
        parsed.role = role.casefold()
    if title:
        parsed.title = normalize_search_text(title)
    if exact:
        parsed.phrases.append(normalize_search_text(exact))
    if exclude:
        parsed.exclude.extend(item for item in _split_words(normalize_search_text(exclude)) if item)
    if source:
        parsed.source = source
    if after:
        parsed.after = _parse_date(after)
        if parsed.after is None:
            parsed.errors.append("invalid_after")
    if before:
        before_ts = _parse_date(before)
        if before_ts is None:
            parsed.errors.append("invalid_before")
        else:
            parsed.before = before_ts + 86399
    for match in re.finditer(r'"([^"]+)"|(\S+)', text):
        token = match.group(1) if match.group(1) is not None else match.group(2)
        quoted = match.group(1) is not None
        if not token:
            continue
        if token.upper() == "OR" and not quoted:
            parsed.or_mode = True
            continue
        if not quoted and token.startswith("-") and not token.startswith("--") and len(token) > 1:
            parsed.exclude.append(token[1:])
            continue
        if not quoted and ":" in token:
            key, value = token.split(":", 1)
            key = key.lower()
            if key == "role" and value:
                parsed.role = value.casefold()
                continue
            if key == "title" and value:
                parsed.title = normalize_search_text(value)
                continue
            if key == "source" and value:
                parsed.source = value
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
                    parsed.before = ts + 86399
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


def _parse_date(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _fts_token(value: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_]{2,64}", value):
        return f"{value}*"
    if value and not any(ch in value for ch in '"\n\r\t'):
        return '"' + value.replace('"', '""') + '"'
    return None


def build_fts_query(parsed: ParsedQuery) -> str | None:
    pieces: list[str] = []
    for phrase in parsed.phrases:
        if phrase:
            pieces.append('"' + phrase.replace('"', '""') + '"')
    for term in parsed.terms:
        token = _fts_token(term)
        if token:
            pieces.append(token)
    if not pieces:
        return None
    joiner = " OR " if parsed.or_mode else " AND "
    query = joiner.join(pieces)
    for term in parsed.exclude:
        token = _fts_token(term)
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
    candidate_limit: int | None = None,
) -> dict[str, Any]:
    limit = _bounded_limit(limit, max_page_limit)
    offset = max(0, offset)
    if parsed.scope == "title":
        return _page_payload([], 0, limit, offset)
    has_message_text = bool(parsed.phrases or parsed.terms)
    if not has_message_text and (parsed.title or parsed.source) and not parsed.role:
        return _page_payload([], 0, limit, offset)
    try:
        rows, total = _message_search_page_rows(conn, parsed, conversation_id, limit, offset, order)
    except sqlite3.OperationalError:
        rows, total = _message_search_page_rows(conn, parsed, conversation_id, limit, offset, order, use_trigram=False)
    items = [
        _message_search_payload(row, parsed, row["match_reason"] or ("exact phrase" if parsed.phrases else "substring"), row["bm25_score"])
        for row in rows
    ]
    return _page_payload(items, total, limit, offset)


def _needs_substring_fallback(parsed: ParsedQuery) -> bool:
    fragments = parsed.phrases + parsed.terms
    if not fragments:
        return True
    return any(not re.fullmatch(r"[A-Za-z0-9_]{2,64}", fragment) for fragment in fragments)


def search_conversations(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    *,
    limit: int = 50,
    offset: int = 0,
    sort: str = "relevance",
    selected_id: str | None = None,
) -> dict[str, Any]:
    if not parsed.has_search_text() and not parsed.has_non_time_filters():
        return list_conversations(conn, limit=limit, offset=offset, sort=sort, after=parsed.after, before=parsed.before, selected_id=selected_id)
    limit = _bounded_limit(limit, MAX_API_LIMIT)
    offset = max(0, offset)
    try:
        items, total = _conversation_search_page(conn, parsed, limit, offset, sort)
    except sqlite3.OperationalError:
        items, total = _conversation_search_page(conn, parsed, limit, offset, sort, use_trigram=False)
    if not items and parsed.terms and parsed.scope != "message":
        grouped: dict[str, dict[str, Any]] = {}
        for row in _fuzzy_title_rows(conn, parsed, 30):
            conv = grouped.setdefault(
                row["conversation_id"],
                {
                    "conversation_id": row["conversation_id"],
                    "title": row["title"],
                    "create_time": row["create_time"],
                    "update_time": row["update_time"],
                    "current_node": row["current_node"],
                    "source_file": row["source_file"],
                    "hit_count": 0,
                    "snippets": [],
                    "reasons": set(),
                    "score": 0.0,
                },
            )
            conv["score"] += row["score"]
            conv["reasons"].add("fuzzy title")
        items = []
        for conv in grouped.values():
            if _title_has_excluded(conv.get("title"), parsed):
                continue
            conv["reasons"] = sorted(conv["reasons"])
            items.append(conv)
        total = len(items)
    for conv in items:
        conv["reasons"] = sorted(conv["reasons"])
        conv["snippets"] = _conversation_snippets(conn, parsed, conv["conversation_id"]) if conv.get("hit_count") else []
    selected_in_results = None
    if selected_id:
        selected_in_results = _conversation_search_contains(conn, parsed, selected_id)
    return _page_payload(items, total, limit, offset, selected_in_results=selected_in_results)


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
    around_node_id: str | None = None,
) -> dict[str, Any]:
    limit = _bounded_limit(limit, MAX_MESSAGE_LIMIT)
    offset = max(0, offset)
    if around_node_id:
        rows = _conversation_rows(conn, conversation_id)
        ordered = _order_nodes_for_display(rows, path)
        terms = _highlight_terms(parse_query(highlight_query or ""))
        items = [_message_payload(row, terms) for row in ordered]
        total = len(items)
        index = next((idx for idx, item in enumerate(items) if item["node_id"] == around_node_id), None)
        if index is not None:
            offset = max(0, min(index, max(0, total - limit)))
        return _page_payload(items[offset : offset + limit], total, limit, offset)
    rows, total = _paged_conversation_rows(conn, conversation_id, path, limit, offset)
    terms = _highlight_terms(parse_query(highlight_query or ""))
    return _page_payload([_message_payload(row, terms) for row in rows], total, limit, offset)


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
) -> tuple[list[sqlite3.Row], int]:
    base_sql, params = _message_search_base_select(conn, parsed, conversation_id, use_trigram=use_trigram)
    total = conn.execute(f"SELECT COUNT(*) AS c FROM ({base_sql})", params).fetchone()["c"]
    order_clause = _message_search_order_clause(order, conversation_id, parsed.path)
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
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            [conversation_id] + params + [limit, offset],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT *
            FROM ({base_sql}) matched
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
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
    has_norm = _table_exists(conn, "web_message_norm")
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
    trigram_query = _trigram_query(parsed.phrases + parsed.terms, parsed.or_mode)
    if use_trigram and trigram_query and _table_exists(conn, "web_message_trigram"):
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
            "exact phrase" if parsed.phrases else "substring",
        )
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
    fragments = ([parsed.title] if parsed.title else []) + parsed.phrases + parsed.terms
    if not fragments:
        fragments = [""]
    where, params = _conversation_time_where(parsed.after, parsed.before)
    has_norm = _table_exists(conn, "web_title_norm")
    positive_clauses = []
    filter_clauses = []
    for frag in fragments:
        if not frag and parsed.has_non_time_filters():
            continue
        if has_norm:
            positive_clauses.append("instr(tn.title_norm, ?) > 0")
            params.append(normalize_search_text(frag))
        else:
            positive_clauses.append("instr(lower(COALESCE(c.title, '')), lower(?)) > 0")
            params.append(frag)
    if parsed.source:
        filter_clauses.append("instr(c.source_file, ?) > 0")
        params.append(parsed.source)
    trigram_clause, trigram_params = _title_trigram_clause(conn, parsed, use_trigram)
    if trigram_clause:
        filter_clauses.append(trigram_clause)
        params.extend(trigram_params)
    for frag in parsed.exclude:
        if has_norm:
            filter_clauses.append("instr(tn.title_norm, ?) = 0")
            params.append(normalize_search_text(frag))
        else:
            filter_clauses.append("instr(lower(COALESCE(c.title, '')), lower(?)) = 0")
            params.append(frag)
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
    parts: list[str] = []
    params: list[Any] = []
    has_message_match = bool(parsed.terms or parsed.phrases or parsed.role)
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
            }
        )
    return items, int(total or 0)


def _message_conversation_select(conn: sqlite3.Connection, parsed: ParsedQuery, *, use_trigram: bool, conversation_id: str | None = None) -> tuple[str, list[Any]]:
    source_sql, source_params, score_expr, _reason = _message_match_source(conn, parsed, use_trigram=use_trigram)
    where, params = _node_filters(parsed, conversation_id)
    has_norm = _table_exists(conn, "web_message_norm")
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
    has_norm = _table_exists(conn, "web_title_norm")
    clauses, clause_params = _title_filter_clauses(parsed, has_norm)
    params.extend(clause_params)
    source_sql = "conversations c"
    source_params: list[Any] = []
    trigram_query = _trigram_query(([parsed.title] if parsed.title else []) + parsed.phrases + parsed.terms, parsed.or_mode)
    if use_trigram and trigram_query and _table_exists(conn, "web_title_trigram"):
        source_sql = """
            (
                SELECT conversation_id, rank AS title_rank
                FROM web_title_trigram
                WHERE web_title_trigram MATCH ?
            ) tk
            JOIN conversations c ON c.conversation_id = tk.conversation_id
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
                "snippet": make_snippet(row["content_text"] or "", _highlight_terms(parsed)),
                "is_on_current_path": bool(row["is_on_current_path"]),
            }
        )
    return snippets


def _conversation_search_contains(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM conversations WHERE conversation_id = ? LIMIT 1", (conversation_id,)).fetchone()
    return row is not None and _conversation_id_matches(conn, parsed, conversation_id)


def _conversation_id_matches(conn: sqlite3.Connection, parsed: ParsedQuery, conversation_id: str) -> bool:
    if parsed.scope != "title" and (parsed.terms or parsed.phrases or parsed.role):
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
    exclude_clauses = []
    for frag in fragments:
        if not frag:
            continue
        norm = normalize_search_text(frag)
        if has_norm:
            positive_clauses.append("instr(mn.content_norm, ?) > 0")
            params.append(norm)
        else:
            positive_clauses.append("instr(lower(n.content_text), lower(?)) > 0")
            params.append(frag)
    for frag in parsed.exclude:
        norm = normalize_search_text(frag)
        if has_norm:
            exclude_clauses.append("instr(mn.content_norm, ?) = 0")
            params.append(norm)
        else:
            exclude_clauses.append("instr(lower(n.content_text), lower(?)) = 0")
            params.append(frag)
    clauses = []
    if positive_clauses:
        clauses.append("(" + (" OR ".join(positive_clauses) if parsed.or_mode else " AND ".join(positive_clauses)) + ")")
    clauses.extend(exclude_clauses)
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
        if has_norm:
            positive_clauses.append("instr(tn.title_norm, ?) > 0")
            params.append(normalize_search_text(frag))
        else:
            positive_clauses.append("instr(lower(COALESCE(c.title, '')), lower(?)) > 0")
            params.append(frag)
    if positive_clauses:
        clauses.append("(" + (" OR ".join(positive_clauses) if parsed.or_mode else " AND ".join(positive_clauses)) + ")")
    if parsed.source:
        clauses.append("instr(c.source_file, ?) > 0")
        params.append(parsed.source)
    for frag in parsed.exclude:
        if has_norm:
            clauses.append("instr(tn.title_norm, ?) = 0")
            params.append(normalize_search_text(frag))
        else:
            clauses.append("instr(lower(COALESCE(c.title, '')), lower(?)) = 0")
            params.append(frag)
    if not clauses:
        clauses.append("1 = 1")
    return clauses, params


def _outer_title_exclude_filter(parsed: ParsedQuery) -> tuple[str, list[Any]]:
    if not parsed.exclude:
        return "", []
    clauses = []
    params: list[Any] = []
    for frag in parsed.exclude:
        clauses.append("instr(lower(COALESCE(c.title, '')), lower(?)) = 0")
        params.append(frag)
    return "WHERE " + " AND ".join(clauses), params


def _message_trigram_clause(conn: sqlite3.Connection, parsed: ParsedQuery, use_trigram: bool) -> tuple[str, list[Any]]:
    query = _trigram_query(parsed.phrases + parsed.terms, parsed.or_mode)
    if not use_trigram or not query or not _table_exists(conn, "web_message_trigram"):
        return "", []
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
    query = _trigram_query(fragments, parsed.or_mode)
    if not use_trigram or not query or not _table_exists(conn, "web_title_trigram"):
        return "", []
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


def _trigram_query(fragments: list[str], or_mode: bool) -> str | None:
    tokens = []
    for frag in fragments:
        norm = normalize_search_text(frag)
        if len(norm) < 3 or '"' in norm or "\x00" in norm:
            return None
        tokens.append('"' + norm.replace('"', '""') + '"')
    if not tokens:
        return None
    return (" OR " if or_mode else " AND ").join(tokens)


def _fuzzy_title_rows(conn: sqlite3.Connection, parsed: ParsedQuery, limit: int) -> list[dict[str, Any]]:
    needle = normalize_search_text(" ".join(parsed.terms)).strip()
    if len(needle) < 3 or parsed.role:
        return []
    where, params = _conversation_time_where(parsed.after, parsed.before)
    clauses = []
    if parsed.source:
        clauses.append("instr(c.source_file, ?) > 0")
        params.append(parsed.source)
    if parsed.title:
        clauses.append("instr(lower(COALESCE(c.title, '')), lower(?)) > 0")
        params.append(parsed.title)
    for frag in parsed.exclude:
        clauses.append("instr(lower(COALESCE(c.title, '')), lower(?)) = 0")
        params.append(frag)
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


def _title_has_excluded(title: str | None, parsed: ParsedQuery) -> bool:
    normalized = normalize_search_text(title or "")
    return any(term and term in normalized for term in parsed.exclude)


def _message_has_excluded(text: str | None, parsed: ParsedQuery) -> bool:
    normalized = normalize_search_text(text or "")
    return any(term and term in normalized for term in parsed.exclude)


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
        clauses.append("instr(c.source_file, ?) > 0")
        params.append(parsed.source)
    if parsed.after is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) >= ?")
        params.append(parsed.after)
    if parsed.before is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) <= ?")
        params.append(parsed.before)
    if parsed.title:
        clauses.append("instr(lower(COALESCE(c.title, '')), lower(?)) > 0")
        params.append(parsed.title)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _conversation_time_where(after: float | None, before: float | None) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    if after is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) >= ?")
        params.append(after)
    if before is not None:
        clauses.append("COALESCE(c.update_time, c.create_time, 0) <= ?")
        params.append(before)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _add_message_candidate(candidates: dict[tuple[str, str], dict[str, Any]], row: sqlite3.Row, parsed: ParsedQuery, reason: str, bm25_score: float | None) -> None:
    if _message_has_excluded(row["content_text"], parsed):
        return
    key = (row["conversation_id"], row["node_id"])
    current = candidates.get(key)
    item = _message_search_payload(row, parsed, reason, bm25_score)
    if current is None or item["score"] > current["score"]:
        candidates[key] = item
    else:
        current["reasons"] = sorted(set(current["reasons"]) | set(item["reasons"]))


def _message_search_payload(row: sqlite3.Row, parsed: ParsedQuery, reason: str, bm25_score: float | None) -> dict[str, Any]:
    text = row["content_text"] or ""
    normalized_text = normalize_search_text(text)
    reasons = {reason}
    score = 10.0
    if row["is_on_current_path"]:
        score += 5.0
        reasons.add("current path")
    for phrase in parsed.phrases:
        if phrase and normalize_search_text(phrase) in normalized_text:
            score += 35.0
            reasons.add("exact phrase")
    for term in parsed.terms:
        if term and normalize_search_text(term) in normalized_text:
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
        "snippet": make_snippet(text, _highlight_terms(parsed)),
        "is_on_current_path": bool(row["is_on_current_path"]),
        "title": row["title"],
        "conversation_create_time": row["conversation_create_time"],
        "conversation_update_time": row["conversation_update_time"],
        "current_node": row["current_node"],
        "source_file": row["source_file"],
        "reasons": sorted(reasons),
        "score": score,
    }


def _message_payload(row: sqlite3.Row, terms: list[str]) -> dict[str, Any]:
    text = row["content_text"] or ""
    raw_preview = _raw_preview(row["raw_message_json"])
    raw_text = ""
    if (not text or _is_placeholder_text(text)) and row["raw_message_json"]:
        raw_text = _text_from_raw_message(row["raw_message_json"])
    display_text = raw_text or text
    content_type = row["content_type"]
    is_internal = _is_internal_message(row["role"], content_type)
    return {
        "node_id": row["node_id"],
        "parent_node_id": row["parent_node_id"],
        "children_json": row["children_json"],
        "message_id": row["message_id"],
        "role": row["role"],
        "author_name": row["author_name"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "content_type": content_type,
        "content_text": text,
        "display_text": display_text,
        "render_text": display_text,
        "has_text": bool(display_text),
        "has_raw": bool(raw_preview),
        "raw_preview": raw_preview,
        "content_hash": row["content_hash"],
        "is_on_current_path": bool(row["is_on_current_path"]),
        "is_internal": is_internal,
        "highlight_ranges": highlight_ranges(display_text, terms),
    }


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


def _is_internal_message(role: str | None, content_type: str | None) -> bool:
    role_value = (role or "").casefold().replace("_", "/")
    type_value = (content_type or "").casefold()
    return role_value in {"system", "developer", "tool", "tool/system"} or type_value in {
        "user_editable_context",
        "model_editable_context",
        "system_context",
        "developer_context",
    }


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


def _highlight_terms(parsed: ParsedQuery) -> list[str]:
    return [item for item in parsed.phrases + parsed.terms if item]


def highlight_ranges(text: str, terms: list[str]) -> list[dict[str, int]]:
    normalized, spans = _normalized_with_spans(text)
    ranges = []
    for term in terms[:10]:
        needle = normalize_search_text(term)
        if not needle:
            continue
        start = 0
        while len(ranges) < 50:
            idx = normalized.find(needle, start)
            if idx < 0:
                break
            end_idx = idx + len(needle) - 1
            if idx < len(spans) and end_idx < len(spans):
                ranges.append({"start": spans[idx], "end": spans[end_idx] + 1})
            start = idx + max(1, len(needle))
    ranges.sort(key=lambda item: (item["start"], item["end"]))
    return ranges


def make_snippet(text: str, terms: list[str], radius: int = 80) -> str:
    if not text:
        return ""
    normalized, spans = _normalized_with_spans(text)
    positions = []
    for term in terms:
        needle = normalize_search_text(term)
        if not needle:
            continue
        idx = normalized.find(needle)
        if idx >= 0 and idx < len(spans):
            positions.append(spans[idx])
    center = min(positions) if positions else 0
    start = max(0, center - radius)
    end = min(len(text), center + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ") + suffix


def _normalized_with_spans(text: str) -> tuple[str, list[int]]:
    pieces: list[str] = []
    spans: list[int] = []
    for index, char in enumerate(text):
        normalized = unicodedata.normalize("NFKC", char).translate(NORMALIZE_TRANSLATION).casefold()
        for normalized_char in normalized:
            pieces.append(normalized_char)
            spans.append(index)
    return "".join(pieces), spans


def _bounded_limit(limit: int, maximum: int = 100) -> int:
    return max(1, min(maximum, int(limit or 50)))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row is not None


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
