from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .db import (
    CORE_SCHEMA_COLUMNS,
    DatabaseMigrationError,
    _drop_table_with_shadows,
    configure_bulk_write_connection,
    database_schema_status,
)
from .parser import recover_message_display_text
from .schema_contract import (
    DISPLAY_TEXT_RESOLVER_VERSION,
    NORMALIZATION_INDEX_FORMAT_VERSION,
    OPTIONAL_WEB_INDEX_FORMAT_VERSION,
)
from .sqlite_errors import (
    is_fts5_capability_unavailable,
    is_optional_message_fts_damaged,
    is_optional_search_capability_missing,
)
from .search import _derived_generation_is_current, invalidate_capability_cache, normalize_search_text, search_fragment_match

WEB_INDEX_FORMAT_VERSION = OPTIONAL_WEB_INDEX_FORMAT_VERSION

REQUIRED_TABLES = {
    "conversations",
    "conversation_nodes",
    "import_runs",
    "import_warnings",
    "source_files",
}
REQUIRED_COLUMNS = {
    table: CORE_SCHEMA_COLUMNS[table]
    for table in REQUIRED_TABLES
}


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a per-request SQLite connection for Web API reads."""
    if not db_path.exists():
        raise ValueError("database_not_found")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("web_norm", 1, normalize_search_text, deterministic=True)
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text)
    return conn


def connect_writable(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ValueError("database_not_found")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("web_norm", 1, normalize_search_text, deterministic=True)
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text)
    return conn


def check_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    status = database_schema_status(conn)
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
    }
    return {
        **status,
        "message_fts": "message_fts" in tables,
        "web_message_trigram": "web_message_trigram" in tables,
        "web_title_trigram": "web_title_trigram" in tables,
        "web_message_norm": "web_message_norm" in tables,
        "web_title_norm": "web_title_norm" in tables,
        "web_index_metadata": "web_index_metadata" in tables,
    }


def web_index_status(conn: sqlite3.Connection) -> dict[str, Any]:
    schema = check_schema(conn)
    metadata: dict[str, str] = {}
    if schema["web_index_metadata"]:
        try:
            metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM web_index_metadata")}
        except sqlite3.Error:
            metadata = {}
    format_current = (
        metadata.get("web_index_format_version") == WEB_INDEX_FORMAT_VERSION
        and metadata.get("display_text_resolver_version") == DISPLAY_TEXT_RESOLVER_VERSION
        and metadata.get("normalization_index_format_version") == NORMALIZATION_INDEX_FORMAT_VERSION
    )
    message_norm_normalized = format_current and schema["web_message_norm"] and (
        metadata.get("message_norm_text") == "normalized" or metadata.get("message_trigram_text") == "normalized"
    ) and _derived_generation_is_current(conn, "message")
    title_norm_normalized = format_current and schema["web_title_norm"] and (
        metadata.get("title_norm_text") == "normalized" or metadata.get("title_trigram_text") == "normalized"
    ) and _derived_generation_is_current(conn, "title")
    message_trigram_normalized = format_current and schema["web_message_trigram"] and metadata.get("message_trigram_text") == "normalized" and _derived_generation_is_current(conn, "message")
    title_trigram_normalized = format_current and schema["web_title_trigram"] and metadata.get("title_trigram_text") == "normalized" and _derived_generation_is_current(conn, "title")
    return {
        "web_index_format_current": format_current,
        "web_index_format_version": metadata.get("web_index_format_version"),
        "required_web_index_format_version": WEB_INDEX_FORMAT_VERSION,
        "web_normalized_indexed": bool(message_norm_normalized and title_norm_normalized),
        "web_normalized_trigram_indexed": bool(message_trigram_normalized and title_trigram_normalized),
        "web_legacy_trigram_indexed": bool(
            schema["web_message_trigram"]
            and schema["web_title_trigram"]
            and not (message_trigram_normalized and title_trigram_normalized)
        ),
        "web_trigram_indexed": bool(schema["web_message_trigram"] and schema["web_title_trigram"]),
        "web_index_metadata": bool(schema["web_index_metadata"]),
    }


def require_compatible_schema(db_path: Path) -> dict[str, Any]:
    conn = connect_readonly(db_path)
    try:
        status = check_schema(conn)
    finally:
        conn.close()
    if not status["ok"]:
        details = []
        if status["missing_tables"]:
            details.append(f"missing required tables: {', '.join(status['missing_tables'])}")
        if status["missing_columns"]:
            columns = "; ".join(f"{table}: {', '.join(cols)}" for table, cols in status["missing_columns"].items())
            details.append(f"missing required columns: {columns}")
        raise ValueError(f"Database schema is not compatible ({'; '.join(details)})")
    return status


def detect_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts_probe")
        return True
    except sqlite3.OperationalError as exc:
        if is_fts5_capability_unavailable(exc):
            return False
        raise


def message_fts_status(conn: sqlite3.Connection, *, fts5_available: bool) -> dict[str, Any]:
    """Return a bounded capability probe that distinguishes missing and damaged FTS."""

    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message_fts'"
    ).fetchone() is not None
    if not present:
        return {
            "message_fts_available": False,
            "message_fts_rebuildable": fts5_available,
            "message_fts_error": "missing" if fts5_available else "capability_unavailable",
        }
    try:
        conn.execute(
            "SELECT rowid FROM message_fts WHERE message_fts MATCH ? LIMIT 1",
            ("__chatgpt_archive_health_probe__",),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if is_optional_message_fts_damaged(exc) or is_optional_search_capability_missing(exc):
            return {
                "message_fts_available": False,
                "message_fts_rebuildable": fts5_available,
                "message_fts_error": "damaged",
            }
        raise
    return {
        "message_fts_available": True,
        "message_fts_rebuildable": fts5_available,
        "message_fts_error": None,
    }


def detect_trigram(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._tri_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE temp._tri_probe")
        return True
    except sqlite3.OperationalError as exc:
        if is_fts5_capability_unavailable(exc):
            return False
        raise


def create_web_indexes(db_path: Path) -> dict[str, Any]:
    """Build optional Web search indexes without changing archive source tables."""
    conn = connect_writable(db_path)
    try:
        schema = check_schema(conn)
        if not schema["ok"]:
            code = "database_migration_required" if schema["migration_required"] else "database_schema_incompatible"
            raise DatabaseMigrationError(code, detail=schema)
        configure_bulk_write_connection(conn)
        trigram_available = detect_trigram(conn)
        drop_failures: list[dict[str, str]] = []
        conn.execute("BEGIN")
        drop_failures.extend(_drop_table_with_shadows(conn, "web_message_trigram"))
        drop_failures.extend(_drop_table_with_shadows(conn, "web_title_trigram"))
        conn.execute("DROP TABLE IF EXISTS web_message_norm")
        conn.execute("DROP TABLE IF EXISTS web_title_norm")
        conn.execute("DROP TABLE IF EXISTS web_index_metadata")
        conn.execute(
            """
            CREATE TABLE web_message_norm(
                conversation_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                content_norm TEXT NOT NULL,
                PRIMARY KEY(conversation_id, node_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE web_title_norm(
                conversation_id TEXT PRIMARY KEY,
                title_norm TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE web_index_metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            WITH resolved AS MATERIALIZED (
                SELECT conversation_id,
                       node_id,
                       web_norm(web_display_text(content_text, substr(raw_message_json, 1, 200001))) AS content_norm
                FROM conversation_nodes
            )
            INSERT INTO web_message_norm(conversation_id, node_id, content_norm)
            SELECT conversation_id, node_id, content_norm
            FROM resolved
            WHERE content_norm <> ''
            """
        )
        conn.execute(
            """
            INSERT INTO web_title_norm(conversation_id, title_norm)
            SELECT conversation_id, web_norm(COALESCE(title, ''))
            FROM conversations
            """
        )
        if trigram_available:
            conn.execute(
                """
                CREATE VIRTUAL TABLE web_message_trigram USING fts5(
                    content_text,
                    content='',
                    tokenize='trigram'
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE web_title_trigram USING fts5(
                    title,
                    content='',
                    tokenize='trigram'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO web_message_trigram(web_message_trigram, rank) VALUES('automerge', 0)
                """
            )
            conn.execute(
                """
                INSERT INTO web_title_trigram(web_title_trigram, rank) VALUES('automerge', 0)
                """
            )
            conn.execute(
                """
                INSERT INTO web_message_trigram(web_message_trigram, rank) VALUES('crisismerge', 64)
                """
            )
            conn.execute(
                """
                INSERT INTO web_title_trigram(web_title_trigram, rank) VALUES('crisismerge', 64)
                """
            )
            conn.execute(
                """
                INSERT INTO web_message_trigram(rowid, content_text)
                SELECT n.rowid, mn.content_norm
                FROM web_message_norm mn
                JOIN conversation_nodes n
                  ON n.conversation_id = mn.conversation_id AND n.node_id = mn.node_id
                """
            )
            conn.execute(
                """
                INSERT INTO web_title_trigram(rowid, title)
                SELECT c.rowid, tn.title_norm
                FROM web_title_norm tn
                JOIN conversations c ON c.conversation_id = tn.conversation_id
                """
            )
        metadata = [
            ("message_norm_text", "normalized"),
            ("title_norm_text", "normalized"),
            ("web_index_format_version", WEB_INDEX_FORMAT_VERSION),
            ("display_text_resolver_version", DISPLAY_TEXT_RESOLVER_VERSION),
            ("normalization_index_format_version", NORMALIZATION_INDEX_FORMAT_VERSION),
        ]
        try:
            generations = {
                str(row["name"]): str(row["generation"])
                for row in conn.execute("SELECT name, generation FROM archive_generations")
            }
        except sqlite3.Error:
            generations = {}
        if "message" in generations:
            metadata.append(("message_generation", generations["message"]))
        if "title" in generations:
            metadata.append(("title_generation", generations["title"]))
        if trigram_available:
            metadata.extend(
                [
                    ("message_trigram_text", "normalized"),
                    ("title_trigram_text", "normalized"),
                ]
            )
        conn.executemany("INSERT INTO web_index_metadata(key, value) VALUES(?, ?)", metadata)
        indexed_messages = conn.execute("SELECT COUNT(*) AS c FROM web_message_norm").fetchone()["c"]
        indexed_titles = conn.execute("SELECT COUNT(*) AS c FROM web_title_norm").fetchone()["c"]
        conn.commit()
        invalidate_capability_cache(conn)
        return {
            "trigram_available": trigram_available,
            "indexed_messages": indexed_messages,
            "indexed_titles": indexed_titles,
            "drop_failures_count": len(drop_failures),
            "drop_failures": drop_failures,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        invalidate_capability_cache(conn)
        raise
    finally:
        conn.close()
