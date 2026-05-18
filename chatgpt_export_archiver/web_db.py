from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .db import _drop_table_with_shadows, configure_bulk_write_connection
from .search import normalize_search_text


REQUIRED_TABLES = {
    "conversations",
    "conversation_nodes",
    "import_runs",
    "import_warnings",
    "source_files",
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
    return conn


def connect_writable(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ValueError("database_not_found")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("web_norm", 1, normalize_search_text, deterministic=True)
    return conn


def check_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
    }
    missing = sorted(REQUIRED_TABLES - tables)
    return {
        "ok": not missing,
        "missing_tables": missing,
        "message_fts": "message_fts" in tables,
        "web_message_trigram": "web_message_trigram" in tables,
        "web_title_trigram": "web_title_trigram" in tables,
        "web_message_norm": "web_message_norm" in tables,
        "web_title_norm": "web_title_norm" in tables,
    }


def require_compatible_schema(db_path: Path) -> dict[str, Any]:
    conn = connect_readonly(db_path)
    try:
        status = check_schema(conn)
    finally:
        conn.close()
    if not status["ok"]:
        raise ValueError(f"Database is missing required tables: {', '.join(status['missing_tables'])}")
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
                SELECT rowid, content_text
                FROM conversation_nodes
                WHERE content_text IS NOT NULL AND content_text <> ''
                """
            )
            conn.execute(
                """
                INSERT INTO web_title_trigram(rowid, title)
                SELECT rowid, COALESCE(title, '')
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
            INSERT INTO web_message_norm(conversation_id, node_id, content_norm)
            SELECT conversation_id, node_id, web_norm(content_text)
            FROM conversation_nodes
            WHERE content_text IS NOT NULL AND content_text <> ''
            """
        )
        conn.execute(
            """
            INSERT INTO web_title_norm(conversation_id, title_norm)
            SELECT conversation_id, web_norm(COALESCE(title, ''))
            FROM conversations
            """
        )
        indexed_messages = conn.execute("SELECT COUNT(*) AS c FROM web_message_norm").fetchone()["c"]
        indexed_titles = conn.execute("SELECT COUNT(*) AS c FROM web_title_norm").fetchone()["c"]
        conn.commit()
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
        raise
    finally:
        conn.close()
