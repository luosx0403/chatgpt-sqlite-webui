from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .parser import ParsedConversation, WarningRecord
from .scanner import InputSource, SourceEntry
from .utils import compact_json, utc_now_iso

SQLITE_VARIABLE_CHUNK = 500
INSERT_ROW_CHUNK = 5000


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_existing_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ValueError("database_not_found")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_existing(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ValueError("database_not_found")
    uri = f"{db_path.resolve().as_uri()}?mode=rw"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def configure_import_connection(conn: sqlite3.Connection) -> None:
    """Apply conservative write-time tuning for the import command only."""
    pragmas = (
        "PRAGMA foreign_keys = ON",
        "PRAGMA journal_mode = WAL",
        "PRAGMA synchronous = NORMAL",
        "PRAGMA temp_store = MEMORY",
        "PRAGMA cache_size = -262144",
        "PRAGMA mmap_size = 268435456",
        "PRAGMA busy_timeout = 60000",
    )
    for pragma in pragmas:
        try:
            conn.execute(pragma)
        except sqlite3.OperationalError:
            # Some SQLite builds or filesystems may reject a tuning pragma.
            # Keep import functional and let the default setting apply.
            pass


def init_db(conn: sqlite3.Connection) -> bool:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            input_path TEXT NOT NULL,
            input_kind TEXT NOT NULL,
            input_sha256 TEXT,
            input_size INTEGER,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            summary_json TEXT
        );

        CREATE TABLE IF NOT EXISTS source_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_run_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            size INTEGER,
            sha256 TEXT,
            is_conversation_json INTEGER NOT NULL DEFAULT 0,
            is_selected_conversation_source INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
        );

        CREATE TABLE IF NOT EXISTS import_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_run_id INTEGER NOT NULL,
            source_file TEXT NOT NULL,
            array_index INTEGER,
            warning_type TEXT NOT NULL,
            keys_json TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
        );

        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            exported_id TEXT,
            title TEXT,
            create_time REAL,
            update_time REAL,
            current_node TEXT,
            source_file TEXT,
            source_array_index INTEGER,
            aggregate_hash TEXT NOT NULL,
            last_import_run_id INTEGER,
            is_archived INTEGER,
            is_starred INTEGER,
            default_model_slug TEXT,
            metadata_json TEXT,
            FOREIGN KEY(last_import_run_id) REFERENCES import_runs(id)
        );

        CREATE TABLE IF NOT EXISTS conversation_nodes (
            conversation_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            parent_node_id TEXT,
            children_json TEXT,
            message_id TEXT,
            role TEXT,
            author_name TEXT,
            create_time REAL,
            update_time REAL,
            content_type TEXT,
            content_text TEXT,
            content_hash TEXT,
            metadata_json TEXT,
            is_on_current_path INTEGER NOT NULL DEFAULT 0,
            raw_message_json TEXT,
            last_import_run_id INTEGER,
            PRIMARY KEY(conversation_id, node_id),
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
            FOREIGN KEY(last_import_run_id) REFERENCES import_runs(id)
        );

        CREATE TABLE IF NOT EXISTS exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            format TEXT NOT NULL,
            output_path TEXT NOT NULL,
            output_hash TEXT NOT NULL,
            exported_at TEXT NOT NULL,
            export_options_json TEXT,
            UNIQUE(conversation_id, format, output_path)
        );

        CREATE TABLE IF NOT EXISTS file_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_run_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            extension TEXT,
            size INTEGER,
            sha256 TEXT,
            related_conversation_id TEXT,
            related_message_id TEXT,
            FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_nodes_conversation_path
            ON conversation_nodes(conversation_id, is_on_current_path);
        CREATE INDEX IF NOT EXISTS idx_conversations_times
            ON conversations(create_time, update_time);
        CREATE INDEX IF NOT EXISTS idx_warnings_run
            ON import_warnings(import_run_id, warning_type);
        """
    )
    fts_enabled = ensure_fts(conn)
    conn.commit()
    return fts_enabled


OPTIONAL_WEB_TRIGRAM_TABLES = ("web_message_trigram", "web_title_trigram")
OPTIONAL_WEB_NORM_TABLES = ("web_message_norm", "web_title_norm")
OPTIONAL_WEB_INDEX_TABLES = OPTIONAL_WEB_TRIGRAM_TABLES + OPTIONAL_WEB_NORM_TABLES


def _fts5_shadow_suffixes() -> list[str]:
    """Return FTS5 shadow-table suffixes for a content-table FTS5 virtual table.

    DROPping the virtual table normally removes all shadows automatically,
    but when a table is corrupt a bare DROP may fail or leave orphans.
    """
    return ["_content", "_data", "_idx", "_config", "_docsize"]


def _drop_table_with_shadows(conn: sqlite3.Connection, table_name: str) -> list[dict[str, str]]:
    """Drop *table_name* and known FTS5 shadow tables, returning sanitized failures."""
    failures: list[dict[str, str]] = []
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    except sqlite3.Error as exc:
        failures.append({"table": table_name, "error_type": type(exc).__name__})
    for suffix in _fts5_shadow_suffixes():
        shadow_name = f"{table_name}{suffix}"
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{shadow_name}"')
        except sqlite3.Error as exc:
            failures.append({"table": shadow_name, "error_type": type(exc).__name__})
    return failures


def drop_optional_web_indexes(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Invalidate optional Web search indexes before archive tables change.

    These indexes are rebuilt by the explicit `web-index` command. Import/upsert
    modifies conversations and nodes, so keeping stale normalized rows would
    create false-positive search results after an incremental import.
    """
    failures: list[dict[str, str]] = []
    for table in OPTIONAL_WEB_TRIGRAM_TABLES:
        failures.extend(_drop_table_with_shadows(conn, table))
    for table in OPTIONAL_WEB_NORM_TABLES:
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        except sqlite3.Error as exc:
            failures.append({"table": table, "error_type": type(exc).__name__})
    return failures


def ensure_fts(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts
            USING fts5(conversation_id UNINDEXED, node_id UNINDEXED, role UNINDEXED, content_text)
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def begin_import_run(
    conn: sqlite3.Connection,
    input_source: InputSource,
    input_sha256: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO import_runs(input_path, input_kind, input_sha256, input_size, started_at, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(input_source.path), input_source.kind, input_sha256, input_source.size, utc_now_iso(), "running"),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_import_run(conn: sqlite3.Connection, run_id: int, status: str, summary: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE import_runs
        SET finished_at = ?, status = ?, summary_json = ?
        WHERE id = ?
        """,
        (utc_now_iso(), status, compact_json(summary), run_id),
    )
    conn.commit()


def update_import_run_summary(conn: sqlite3.Connection, run_id: int, summary: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE import_runs SET summary_json = ? WHERE id = ?",
        (compact_json(summary), run_id),
    )
    conn.commit()


def record_source_entries(conn: sqlite3.Connection, run_id: int, entries: list[SourceEntry]) -> None:
    conn.executemany(
        """
        INSERT INTO source_files(
            import_run_id, source_path, file_type, size, sha256,
            is_conversation_json, is_selected_conversation_source
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        """,
        [
            (
                run_id,
                e.source_path,
                e.file_type,
                e.size,
                1 if e.is_conversation_json else 0,
                1 if e.is_selected_conversation_source else 0,
            )
            for e in entries
        ],
    )
    conn.executemany(
        """
        INSERT INTO file_index(
            import_run_id, source_path, file_type, extension, size, sha256,
            related_conversation_id, related_message_id
        )
        VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
        """,
        [(run_id, e.source_path, e.file_type, e.extension, e.size) for e in entries],
    )


def record_warning(conn: sqlite3.Connection, run_id: int, warning: WarningRecord) -> None:
    conn.execute(
        """
        INSERT INTO import_warnings(
            import_run_id, source_file, array_index, warning_type, keys_json, raw_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            warning.source_file,
            warning.array_index,
            warning.warning_type,
            warning.keys_json,
            warning.raw_json,
            utc_now_iso(),
        ),
    )


def record_warnings(conn: sqlite3.Connection, run_id: int, warnings: list[WarningRecord]) -> None:
    for warning in warnings:
        record_warning(conn, run_id, warning)


def upsert_conversation(conn: sqlite3.Connection, run_id: int, conv: ParsedConversation) -> str:
    existing = conn.execute(
        "SELECT aggregate_hash FROM conversations WHERE conversation_id = ?",
        (conv.conversation_id,),
    ).fetchone()
    status = "inserted"
    if existing and existing["aggregate_hash"] == conv.aggregate_hash:
        status = "unchanged"
        conn.execute(
            """
            UPDATE conversations
            SET exported_id = ?, title = ?, create_time = ?, update_time = ?, current_node = ?,
                source_file = ?, source_array_index = ?, last_import_run_id = ?,
                is_archived = ?, is_starred = ?, default_model_slug = ?, metadata_json = ?
            WHERE conversation_id = ?
            """,
            (
                conv.exported_id,
                conv.title,
                conv.create_time,
                conv.update_time,
                conv.current_node,
                conv.source_file,
                conv.source_array_index,
                run_id,
                conv.is_archived,
                conv.is_starred,
                conv.default_model_slug,
                conv.metadata_json,
                conv.conversation_id,
            ),
        )
        return status
    if existing:
        status = "updated"
        conn.execute("DELETE FROM conversation_nodes WHERE conversation_id = ?", (conv.conversation_id,))
        _delete_fts_for_conversation(conn, conv.conversation_id)
    conn.execute(
        """
        INSERT INTO conversations(
            conversation_id, exported_id, title, create_time, update_time, current_node,
            source_file, source_array_index, aggregate_hash, last_import_run_id,
            is_archived, is_starred, default_model_slug, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            exported_id = excluded.exported_id,
            title = excluded.title,
            create_time = excluded.create_time,
            update_time = excluded.update_time,
            current_node = excluded.current_node,
            source_file = excluded.source_file,
            source_array_index = excluded.source_array_index,
            aggregate_hash = excluded.aggregate_hash,
            last_import_run_id = excluded.last_import_run_id,
            is_archived = excluded.is_archived,
            is_starred = excluded.is_starred,
            default_model_slug = excluded.default_model_slug,
            metadata_json = excluded.metadata_json
        """,
        (
            conv.conversation_id,
            conv.exported_id,
            conv.title,
            conv.create_time,
            conv.update_time,
            conv.current_node,
            conv.source_file,
            conv.source_array_index,
            conv.aggregate_hash,
            run_id,
            conv.is_archived,
            conv.is_starred,
            conv.default_model_slug,
            conv.metadata_json,
        ),
    )
    conn.executemany(
        """
        INSERT INTO conversation_nodes(
            conversation_id, node_id, parent_node_id, children_json, message_id,
            role, author_name, create_time, update_time, content_type, content_text,
            content_hash, metadata_json, is_on_current_path, raw_message_json, last_import_run_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                n.conversation_id,
                n.node_id,
                n.parent_node_id,
                n.children_json,
                n.message_id,
                n.role,
                n.author_name,
                n.create_time,
                n.update_time,
                n.content_type,
                n.content_text,
                n.content_hash,
                n.metadata_json,
                n.is_on_current_path,
                n.raw_message_json,
                run_id,
            )
            for n in conv.nodes
        ],
    )
    _insert_fts(conn, conv)
    return status


def upsert_conversations_batch(
    conn: sqlite3.Connection,
    run_id: int,
    conversations: list[ParsedConversation],
    *,
    skip_fts: bool = False,
) -> dict[str, int]:
    """Upsert a shard worth of conversations with batched node and FTS writes."""
    if not conversations:
        return {"inserted": 0, "updated": 0, "unchanged": 0}
    # Deterministic duplicate policy: within a batch, the last occurrence wins.
    # The import pipeline also records duplicate_conversation_id warnings.
    conversations = _dedupe_conversations_last_wins(conversations)

    ids = [conv.conversation_id for conv in conversations]
    existing_hashes = _load_existing_hashes(conn, ids)
    inserted: list[ParsedConversation] = []
    updated: list[ParsedConversation] = []
    unchanged: list[ParsedConversation] = []
    for conv in conversations:
        existing_hash = existing_hashes.get(conv.conversation_id)
        if existing_hash is None:
            inserted.append(conv)
        elif existing_hash == conv.aggregate_hash:
            unchanged.append(conv)
        else:
            updated.append(conv)

    if updated:
        _delete_nodes_for_conversations(conn, [conv.conversation_id for conv in updated])
        if not skip_fts:
            _delete_fts_for_conversations(conn, [conv.conversation_id for conv in updated])

    conn.executemany(_UPSERT_CONVERSATION_SQL, [_conversation_row(conv, run_id) for conv in conversations])

    changed = inserted + updated
    _insert_nodes_batch(conn, run_id, changed)
    if changed and not skip_fts:
        _insert_fts_batch(conn, changed)

    return {
        "inserted": len(inserted),
        "updated": len(updated),
        "unchanged": len(unchanged),
    }


def _dedupe_conversations_last_wins(conversations: list[ParsedConversation]) -> list[ParsedConversation]:
    by_id: dict[str, ParsedConversation] = {}
    order: list[str] = []
    for conv in conversations:
        if conv.conversation_id not in by_id:
            order.append(conv.conversation_id)
        by_id[conv.conversation_id] = conv
    return [by_id[conversation_id] for conversation_id in order]


def rebuild_message_fts(conn: sqlite3.Connection, *, optimize: bool = False) -> bool:
    """Rebuild message_fts from conversation_nodes inside the active transaction."""
    if not ensure_fts(conn):
        return False
    # Recreating the FTS table is faster than deleting every row on large
    # incremental imports, and remains transactional in SQLite.
    conn.execute("DROP TABLE IF EXISTS message_fts")
    if not ensure_fts(conn):
        return False
    conn.execute(
        """
        INSERT INTO message_fts(conversation_id, node_id, role, content_text)
        SELECT conversation_id, node_id, role, content_text
        FROM conversation_nodes
        WHERE content_text IS NOT NULL AND content_text != ''
        """
    )
    if optimize:
        try:
            conn.execute("INSERT INTO message_fts(message_fts) VALUES('optimize')")
        except sqlite3.OperationalError:
            pass
    return True


def optimize_after_import(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("PRAGMA optimize")
        return True
    except sqlite3.OperationalError:
        return False


def _load_existing_hashes(conn: sqlite3.Connection, conversation_ids: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for chunk in _chunks(conversation_ids, SQLITE_VARIABLE_CHUNK):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT conversation_id, aggregate_hash FROM conversations WHERE conversation_id IN ({placeholders})",
            chunk,
        ).fetchall()
        result.update({row["conversation_id"]: row["aggregate_hash"] for row in rows})
    return result


_UPSERT_CONVERSATION_SQL = """
    INSERT INTO conversations(
        conversation_id, exported_id, title, create_time, update_time, current_node,
        source_file, source_array_index, aggregate_hash, last_import_run_id,
        is_archived, is_starred, default_model_slug, metadata_json
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(conversation_id) DO UPDATE SET
        exported_id = excluded.exported_id,
        title = excluded.title,
        create_time = excluded.create_time,
        update_time = excluded.update_time,
        current_node = excluded.current_node,
        source_file = excluded.source_file,
        source_array_index = excluded.source_array_index,
        aggregate_hash = excluded.aggregate_hash,
        last_import_run_id = excluded.last_import_run_id,
        is_archived = excluded.is_archived,
        is_starred = excluded.is_starred,
        default_model_slug = excluded.default_model_slug,
        metadata_json = excluded.metadata_json
"""


def _conversation_row(conv: ParsedConversation, run_id: int) -> tuple[Any, ...]:
    return (
        conv.conversation_id,
        conv.exported_id,
        conv.title,
        conv.create_time,
        conv.update_time,
        conv.current_node,
        conv.source_file,
        conv.source_array_index,
        conv.aggregate_hash,
        run_id,
        conv.is_archived,
        conv.is_starred,
        conv.default_model_slug,
        conv.metadata_json,
    )


_INSERT_NODE_SQL = """
    INSERT INTO conversation_nodes(
        conversation_id, node_id, parent_node_id, children_json, message_id,
        role, author_name, create_time, update_time, content_type, content_text,
        content_hash, metadata_json, is_on_current_path, raw_message_json, last_import_run_id
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _node_row(node: Any, run_id: int) -> tuple[Any, ...]:
    return (
        node.conversation_id,
        node.node_id,
        node.parent_node_id,
        node.children_json,
        node.message_id,
        node.role,
        node.author_name,
        node.create_time,
        node.update_time,
        node.content_type,
        node.content_text,
        node.content_hash,
        node.metadata_json,
        node.is_on_current_path,
        node.raw_message_json,
        run_id,
    )


def _insert_nodes_batch(conn: sqlite3.Connection, run_id: int, conversations: list[ParsedConversation]) -> None:
    rows: list[tuple[Any, ...]] = []
    for conv in conversations:
        for node in conv.nodes:
            rows.append(_node_row(node, run_id))
            if len(rows) >= INSERT_ROW_CHUNK:
                conn.executemany(_INSERT_NODE_SQL, rows)
                rows.clear()
    if rows:
        conn.executemany(_INSERT_NODE_SQL, rows)


def _insert_fts_batch(conn: sqlite3.Connection, conversations: list[ParsedConversation]) -> None:
    try:
        rows: list[tuple[Any, ...]] = []
        for conv in conversations:
            for node in conv.nodes:
                if not node.content_text:
                    continue
                rows.append((node.conversation_id, node.node_id, node.role, node.content_text))
                if len(rows) >= INSERT_ROW_CHUNK:
                    conn.executemany(
                        "INSERT INTO message_fts(conversation_id, node_id, role, content_text) VALUES (?, ?, ?, ?)",
                        rows,
                    )
                    rows.clear()
        if rows:
            conn.executemany(
                "INSERT INTO message_fts(conversation_id, node_id, role, content_text) VALUES (?, ?, ?, ?)",
                rows,
            )
    except sqlite3.OperationalError as exc:
        if not _is_acceptable_fts_operational_error(exc):
            raise


def _delete_nodes_for_conversations(conn: sqlite3.Connection, conversation_ids: list[str]) -> None:
    for chunk in _chunks(conversation_ids, SQLITE_VARIABLE_CHUNK):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM conversation_nodes WHERE conversation_id IN ({placeholders})", chunk)


def _delete_fts_for_conversations(conn: sqlite3.Connection, conversation_ids: list[str]) -> None:
    try:
        for chunk in _chunks(conversation_ids, SQLITE_VARIABLE_CHUNK):
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM message_fts WHERE conversation_id IN ({placeholders})", chunk)
    except sqlite3.OperationalError as exc:
        if not _is_acceptable_fts_operational_error(exc):
            raise


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _delete_fts_for_conversation(conn: sqlite3.Connection, conversation_id: str) -> None:
    try:
        conn.execute("DELETE FROM message_fts WHERE conversation_id = ?", (conversation_id,))
    except sqlite3.OperationalError as exc:
        if not _is_acceptable_fts_operational_error(exc):
            raise


def _insert_fts(conn: sqlite3.Connection, conv: ParsedConversation) -> None:
    try:
        conn.executemany(
            "INSERT INTO message_fts(conversation_id, node_id, role, content_text) VALUES (?, ?, ?, ?)",
            [
                (n.conversation_id, n.node_id, n.role, n.content_text)
                for n in conv.nodes
                if n.content_text
            ],
        )
    except sqlite3.OperationalError as exc:
        if not _is_acceptable_fts_operational_error(exc):
            raise


def _is_acceptable_fts_operational_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "no such table: message_fts",
            "no such module: fts5",
            "no such module: fts",
        )
    )


def record_export(
    conn: sqlite3.Connection,
    conversation_id: str,
    fmt: str,
    output_path: Path,
    output_hash: str,
    options: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO exports(conversation_id, format, output_path, output_hash, exported_at, export_options_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(conversation_id, format, output_path) DO UPDATE SET
            output_hash = excluded.output_hash,
            exported_at = excluded.exported_at,
            export_options_json = excluded.export_options_json
        """,
        (conversation_id, fmt, str(output_path), output_hash, utc_now_iso(), compact_json(options)),
    )


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    conv = conn.execute("SELECT COUNT(*) AS c, MIN(create_time) AS min_ct, MAX(create_time) AS max_ct, MIN(update_time) AS min_ut, MAX(update_time) AS max_ut FROM conversations").fetchone()
    nodes = conn.execute("SELECT COUNT(*) AS c FROM conversation_nodes").fetchone()
    warnings = conn.execute("SELECT COUNT(*) AS c FROM import_warnings").fetchone()
    exports = conn.execute("SELECT COUNT(*) AS c FROM exports").fetchone()
    return {
        "conversations": conv["c"],
        "nodes": nodes["c"],
        "warnings": warnings["c"],
        "earliest_create_time": conv["min_ct"],
        "latest_create_time": conv["max_ct"],
        "earliest_update_time": conv["min_ut"],
        "latest_update_time": conv["max_ut"],
        "exports": exports["c"],
    }


_WEB_INDEX_BASE_NAMES = frozenset(OPTIONAL_WEB_INDEX_TABLES)

_WEB_INDEX_SHADOW_SUFFIXES = frozenset({
    "_content", "_data", "_idx", "_config", "_docsize",
})

CORE_SCHEMA_TABLES = frozenset({
    "import_runs",
    "import_warnings",
    "source_files",
    "file_index",
    "conversations",
    "conversation_nodes",
    "message_fts",
    "exports",
})


def check_core_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')").fetchall()
    found = {row["name"] if isinstance(row, sqlite3.Row) else row[0] for row in rows}
    missing = sorted(CORE_SCHEMA_TABLES - found)
    return {"schema_ok": not missing, "missing_tables": missing}


def _run_integrity_check(conn: sqlite3.Connection) -> list[str]:
    """Return every row from ``PRAGMA integrity_check`` as a list of strings."""
    return [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]


def _integrity_failure_is_web_index_only(lines: list[str]) -> bool:
    """Return True when every line in *lines* only mentions optional web index tables.

    SQLite PRAGMA integrity_check reports one error per line.  We walk each
    line and check whether it contains the name of a table that is *not* an
    optional Web index table (including its known FTS5 shadow tables).  If
    *all* lines are about web index tables, the corruption is limited to
    optional indexes and ``web-index`` can rebuild them.

    ``["ok"]`` is treated as *no* failure at all — the caller should not
    invoke this function for the "ok" case.
    """
    if not lines:
        return False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "ok":
            continue
        if _line_names_web_index_table(stripped):
            continue
        return False
    return True


def _line_names_web_index_table(line: str) -> bool:
    """Check whether *line* refers exclusively to whitelisted web-index objects."""
    allowed = set(_WEB_INDEX_BASE_NAMES)
    for base in OPTIONAL_WEB_TRIGRAM_TABLES:
        allowed.update(f"{base}{suffix}" for suffix in _WEB_INDEX_SHADOW_SUFFIXES)
    names = set(
        match.group(1)
        for match in re.finditer(
            r"\b(?:table|index(?!\s+for\b))\s+(?:main\.)?([A-Za-z_][A-Za-z0-9_]*)\b",
            line,
        )
    )
    for name in allowed:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", line):
            names.add(name)
    return bool(names) and names.issubset(allowed)


def verify_database(conn: sqlite3.Connection) -> dict[str, Any]:
    schema = check_core_schema(conn)
    integrity_lines = _run_integrity_check(conn)
    if len(integrity_lines) == 1 and integrity_lines[0] == "ok":
        integrity = "ok"
    else:
        integrity = "\n".join(integrity_lines)
    if not schema["schema_ok"]:
        return {
            "schema_ok": False,
            "missing_tables": schema["missing_tables"],
            "latest_import_run_id": None,
            "latest_run_warnings": 0,
            "total_warnings": 0,
            "missing_current_node": 0,
            "broken_parent_links": 0,
            "conversations_with_zero_nodes": 0,
            "parent_cycles": 0,
            "integrity_check": integrity,
            "optional_web_index_error": False,
            "optional_web_index_recovery_hint": "",
            "warnings_by_type": [],
            "latest_warnings_by_type": [],
            "ok": False,
        }
    latest_run = conn.execute("SELECT MAX(id) AS id FROM import_runs").fetchone()["id"]
    missing_current = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM conversations c
        WHERE c.current_node IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM conversation_nodes n
              WHERE n.conversation_id = c.conversation_id AND n.node_id = c.current_node
          )
        """
    ).fetchone()["c"]
    broken_parent = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM conversation_nodes n
        WHERE n.parent_node_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM conversation_nodes p
              WHERE p.conversation_id = n.conversation_id AND p.node_id = n.parent_node_id
          )
        """
    ).fetchone()["c"]
    zero_node = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM conversations c
        WHERE NOT EXISTS (
            SELECT 1 FROM conversation_nodes n WHERE n.conversation_id = c.conversation_id
        )
        """
    ).fetchone()["c"]
    warning_counts = [
        dict(row)
        for row in conn.execute(
            "SELECT warning_type, COUNT(*) AS count FROM import_warnings GROUP BY warning_type ORDER BY count DESC"
        ).fetchall()
    ]
    latest_warning_counts: list[dict[str, Any]] = []
    latest_run_warnings = 0
    if latest_run is not None:
        latest_run_warnings = conn.execute(
            "SELECT COUNT(*) AS c FROM import_warnings WHERE import_run_id = ?",
            (latest_run,),
        ).fetchone()["c"]
        latest_warning_counts = [
            dict(row)
            for row in conn.execute(
                """
                SELECT warning_type, COUNT(*) AS count
                FROM import_warnings
                WHERE import_run_id = ?
                GROUP BY warning_type
                ORDER BY count DESC, warning_type
                """,
                (latest_run,),
            ).fetchall()
        ]
    total_warnings = conn.execute("SELECT COUNT(*) AS c FROM import_warnings").fetchone()["c"]
    cycles = count_parent_cycles(conn)
    optional_web_index_error = False
    optional_web_index_recovery_hint = ""
    if integrity != "ok":
        optional_web_index_error = _integrity_failure_is_web_index_only(integrity_lines)
        if optional_web_index_error:
            optional_web_index_recovery_hint = "run `web-index` to rebuild optional web search indexes"
    return {
        "schema_ok": True,
        "missing_tables": [],
        "latest_import_run_id": latest_run,
        "latest_run_warnings": latest_run_warnings,
        "total_warnings": total_warnings,
        "missing_current_node": missing_current,
        "broken_parent_links": broken_parent,
        "conversations_with_zero_nodes": zero_node,
        "parent_cycles": cycles,
        "integrity_check": integrity,
        "optional_web_index_error": optional_web_index_error,
        "optional_web_index_recovery_hint": optional_web_index_recovery_hint,
        "warnings_by_type": warning_counts,
        "latest_warnings_by_type": latest_warning_counts,
        "ok": missing_current == 0 and broken_parent == 0 and zero_node == 0 and cycles == 0 and integrity == "ok",
    }


def count_parent_cycles(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT conversation_id, node_id, parent_node_id FROM conversation_nodes WHERE parent_node_id IS NOT NULL"
    ).fetchall()
    parents = {(row["conversation_id"], row["node_id"]): row["parent_node_id"] for row in rows}
    cycle_nodes: set[tuple[str, str]] = set()
    checked: set[tuple[str, str]] = set()
    for start in parents:
        if start in checked:
            continue
        path: list[tuple[str, str]] = []
        seen_at: dict[tuple[str, str], int] = {}
        current: tuple[str, str] | None = start
        while current is not None and current not in checked:
            if current in seen_at:
                cycle_nodes.update(path[seen_at[current] :])
                break
            seen_at[current] = len(path)
            path.append(current)
            parent = parents.get(current)
            current = (current[0], parent) if parent is not None else None
        checked.update(path)
    return len(cycle_nodes)


def export_query(conn: sqlite3.Connection, start_ts: float | None, end_ts: float | None) -> list[sqlite3.Row]:
    where = []
    params: list[Any] = []
    if start_ts is not None:
        where.append("COALESCE(update_time, create_time, 0) >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("COALESCE(update_time, create_time, 0) <= ?")
        params.append(end_ts)
    clause = "WHERE " + " AND ".join(where) if where else ""
    return conn.execute(
        f"""
        SELECT *
        FROM conversations
        {clause}
        ORDER BY COALESCE(create_time, update_time, 0), conversation_id
        """,
        params,
    ).fetchall()
