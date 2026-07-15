from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .db import CORE_SCHEMA_COLUMNS, _drop_table_with_shadows, configure_bulk_write_connection
from .parser import recover_message_display_text
from .search import _derived_generation_is_current, invalidate_capability_cache, normalize_search_text, search_fragment_match


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
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("web_norm", 1, normalize_search_text, deterministic=True)
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text, deterministic=True)
    return conn


def connect_writable(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ValueError("database_not_found")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("web_norm", 1, normalize_search_text, deterministic=True)
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text, deterministic=True)
    return conn


def check_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
    }
    missing = sorted(REQUIRED_TABLES - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required_columns in REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        try:
            columns = {row["name"] for row in conn.execute(f'PRAGMA table_xinfo("{table}")')}
        except sqlite3.Error:
            missing_columns[table] = sorted(required_columns)
            continue
        missing_for_table = sorted(required_columns - columns)
        if missing_for_table:
            missing_columns[table] = missing_for_table
    return {
        "ok": not missing and not missing_columns,
        "missing_tables": missing,
        "missing_columns": missing_columns,
        "schema_compatible": not missing and not missing_columns,
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
    message_norm_normalized = schema["web_message_norm"] and (
        metadata.get("message_norm_text") == "normalized" or metadata.get("message_trigram_text") == "normalized"
    ) and _derived_generation_is_current(conn, "message")
    title_norm_normalized = schema["web_title_norm"] and (
        metadata.get("title_norm_text") == "normalized" or metadata.get("title_trigram_text") == "normalized"
    ) and _derived_generation_is_current(conn, "title")
    message_trigram_normalized = schema["web_message_trigram"] and metadata.get("message_trigram_text") == "normalized" and _derived_generation_is_current(conn, "message")
    title_trigram_normalized = schema["web_title_trigram"] and metadata.get("title_trigram_text") == "normalized" and _derived_generation_is_current(conn, "title")
    return {
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
    except sqlite3.Error:
        return False


def detect_trigram(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._tri_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE temp._tri_probe")
        return True
    except sqlite3.Error:
        return False


def create_web_indexes(db_path: Path) -> dict[str, Any]:
    """Build optional Web search indexes without changing archive source tables."""
    conn = connect_writable(db_path)
    configure_bulk_write_connection(conn)
    try:
        trigram_available = detect_trigram(conn)
        drop_failures: list[dict[str, str]] = []
        conn.execute("BEGIN")
        drop_failures.extend(_drop_table_with_shadows(conn, "web_message_trigram"))
        drop_failures.extend(_drop_table_with_shadows(conn, "web_title_trigram"))
        conn.execute("DROP TABLE IF EXISTS web_message_norm")
        conn.execute("DROP TABLE IF EXISTS web_title_norm")
        conn.execute("DROP TABLE IF EXISTS web_index_metadata")
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
                SELECT rowid, web_norm(web_display_text(content_text, substr(raw_message_json, 1, 200001)))
                FROM conversation_nodes
                WHERE web_display_text(content_text, substr(raw_message_json, 1, 200001)) <> ''
                """
            )
            conn.execute(
                """
                INSERT INTO web_title_trigram(rowid, title)
                SELECT rowid, web_norm(COALESCE(title, ''))
                FROM conversations
                """
            )
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
            INSERT INTO web_message_norm(conversation_id, node_id, content_norm)
            SELECT conversation_id, node_id, web_norm(web_display_text(content_text, substr(raw_message_json, 1, 200001)))
            FROM conversation_nodes
            WHERE web_display_text(content_text, substr(raw_message_json, 1, 200001)) <> ''
            """
        )
        conn.execute(
            """
            INSERT INTO web_title_norm(conversation_id, title_norm)
            SELECT conversation_id, web_norm(COALESCE(title, ''))
            FROM conversations
            """
        )
        metadata = [
            ("message_norm_text", "normalized"),
            ("title_norm_text", "normalized"),
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
