from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .current_path import (
    EffectiveCurrentResourceLimitError,
    effective_current_metadata,
    ensure_effective_current_views,
)

from .parser import ParsedConversation, WarningRecord
from .scanner import InputSource, SourceEntry
from .schema_contract import (
    DATABASE_SCHEMA_VERSION,
    DISPLAY_TEXT_RESOLVER_VERSION,
    NORMALIZATION_INDEX_FORMAT_VERSION,
    OPTIONAL_WEB_INDEX_FORMAT_VERSION,
    parse_nonnegative_integer,
)
from .sqlite_errors import (
    is_fts5_capability_unavailable,
    is_optional_search_capability_missing,
    sqlite_runtime_error_code,
    sqlite_open_error_code,
)
from .utils import compact_json, finite_float_or_none, utc_now_iso

SQLITE_VARIABLE_CHUNK = 500
INSERT_ROW_CHUNK = 5000
CANONICAL_TABLE_DDL = (
    """CREATE TABLE IF NOT EXISTS import_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        input_path TEXT NOT NULL,
        input_kind TEXT NOT NULL,
        input_sha256 TEXT,
        input_size INTEGER,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        summary_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS source_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        import_run_id INTEGER NOT NULL,
        source_path TEXT NOT NULL,
        file_type TEXT NOT NULL,
        size INTEGER,
        sha256 TEXT,
        is_conversation_json INTEGER NOT NULL DEFAULT 0,
        is_selected_conversation_source INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
    )""",
    """CREATE TABLE IF NOT EXISTS import_warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        import_run_id INTEGER NOT NULL,
        source_file TEXT NOT NULL,
        array_index INTEGER,
        warning_type TEXT NOT NULL,
        keys_json TEXT,
        raw_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
    )""",
    """CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT NOT NULL PRIMARY KEY,
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
    )""",
    """CREATE TABLE IF NOT EXISTS conversation_nodes (
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
    )""",
    """CREATE TABLE IF NOT EXISTS exports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        format TEXT NOT NULL,
        output_path TEXT NOT NULL,
        output_hash TEXT NOT NULL,
        exported_at TEXT NOT NULL,
        export_options_json TEXT,
        UNIQUE(conversation_id, format, output_path)
    )""",
    """CREATE TABLE IF NOT EXISTS file_index (
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
    )""",
)

GENERATION_TABLE_DDL = """CREATE TABLE IF NOT EXISTS archive_generations (
    name TEXT NOT NULL PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
)"""

GENERATION_TRIGGER_DDL = {
    "archive_title_generation_insert": """CREATE TRIGGER IF NOT EXISTS archive_title_generation_insert
        AFTER INSERT ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title';
        END""",
    "archive_title_generation_update": """CREATE TRIGGER IF NOT EXISTS archive_title_generation_update
        AFTER UPDATE OF conversation_id, title ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title';
        END""",
    "archive_title_generation_delete": """CREATE TRIGGER IF NOT EXISTS archive_title_generation_delete
        AFTER DELETE ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title';
        END""",
    "archive_message_generation_insert": """CREATE TRIGGER IF NOT EXISTS archive_message_generation_insert
        AFTER INSERT ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'message';
        END""",
    "archive_message_generation_update": """CREATE TRIGGER IF NOT EXISTS archive_message_generation_update
        AFTER UPDATE OF conversation_id, node_id, message_id, role, author_name, content_type, content_text, content_hash, raw_message_json ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'message';
        END""",
    "archive_message_generation_delete": """CREATE TRIGGER IF NOT EXISTS archive_message_generation_delete
        AFTER DELETE ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'message';
        END""",
    "archive_address_generation_conversation_insert": """CREATE TRIGGER IF NOT EXISTS archive_address_generation_conversation_insert
        AFTER INSERT ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'address';
        END""",
    "archive_address_generation_conversation_update": """CREATE TRIGGER IF NOT EXISTS archive_address_generation_conversation_update
        AFTER UPDATE OF conversation_id, exported_id, current_node ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'address';
        END""",
    "archive_address_generation_conversation_delete": """CREATE TRIGGER IF NOT EXISTS archive_address_generation_conversation_delete
        AFTER DELETE ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'address';
        END""",
    "archive_address_generation_node_insert": """CREATE TRIGGER IF NOT EXISTS archive_address_generation_node_insert
        AFTER INSERT ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'address';
        END""",
    "archive_address_generation_node_update": """CREATE TRIGGER IF NOT EXISTS archive_address_generation_node_update
        AFTER UPDATE OF conversation_id, node_id, parent_node_id, children_json, message_id ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'address';
        END""",
    "archive_address_generation_node_delete": """CREATE TRIGGER IF NOT EXISTS archive_address_generation_node_delete
        AFTER DELETE ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'address';
        END""",
    "archive_graph_generation_conversation_insert": """CREATE TRIGGER IF NOT EXISTS archive_graph_generation_conversation_insert
        AFTER INSERT ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'graph';
        END""",
    "archive_graph_generation_conversation_update": """CREATE TRIGGER IF NOT EXISTS archive_graph_generation_conversation_update
        AFTER UPDATE OF conversation_id, current_node ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'graph';
        END""",
    "archive_graph_generation_conversation_delete": """CREATE TRIGGER IF NOT EXISTS archive_graph_generation_conversation_delete
        AFTER DELETE ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'graph';
        END""",
    "archive_graph_generation_node_insert": """CREATE TRIGGER IF NOT EXISTS archive_graph_generation_node_insert
        AFTER INSERT ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'graph';
        END""",
    "archive_graph_generation_node_update": """CREATE TRIGGER IF NOT EXISTS archive_graph_generation_node_update
        AFTER UPDATE OF conversation_id, node_id, parent_node_id, children_json, is_on_current_path ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'graph';
        END""",
    "archive_graph_generation_node_delete": """CREATE TRIGGER IF NOT EXISTS archive_graph_generation_node_delete
        AFTER DELETE ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'graph';
        END""",
}

# Row-specific display revisions let long-body cursors ignore unrelated writes
# without relying on a bounded content sample.  Keep these triggers active
# during bulk generation aggregation: unlike the four global domains, a row
# revision must advance for every affected canonical row.
DISPLAY_REVISION_TRIGGER_DDL = {
    "archive_display_revision_node_insert": """CREATE TRIGGER IF NOT EXISTS archive_display_revision_node_insert
        AFTER INSERT ON conversation_nodes BEGIN
            INSERT INTO archive_generations(name, generation)
            VALUES ('display:' || NEW.rowid, 1)
            ON CONFLICT(name) DO UPDATE SET generation = generation + 1;
        END""",
    "archive_display_revision_node_update": """CREATE TRIGGER IF NOT EXISTS archive_display_revision_node_update
        AFTER UPDATE OF content_type, content_text, content_hash, raw_message_json ON conversation_nodes BEGIN
            INSERT INTO archive_generations(name, generation)
            VALUES ('display:' || NEW.rowid, 1)
            ON CONFLICT(name) DO UPDATE SET generation = generation + 1;
        END""",
}

CANONICAL_TRIGGER_DDL = {**GENERATION_TRIGGER_DDL, **DISPLAY_REVISION_TRIGGER_DDL}

REQUIRED_INDEX_DDL = {
    "idx_nodes_conversation_path": """CREATE INDEX IF NOT EXISTS idx_nodes_conversation_path
        ON conversation_nodes(conversation_id, is_on_current_path)""",
    "idx_nodes_conversation_flag_parent": """CREATE INDEX IF NOT EXISTS idx_nodes_conversation_flag_parent
        ON conversation_nodes(conversation_id, is_on_current_path, parent_node_id)""",
    "idx_conversations_times": """CREATE INDEX IF NOT EXISTS idx_conversations_times
        ON conversations(create_time, update_time)""",
    "idx_warnings_run": """CREATE INDEX IF NOT EXISTS idx_warnings_run
        ON import_warnings(import_run_id, warning_type)""",
}

REQUIRED_INDEX_COLUMNS = {
    "idx_nodes_conversation_path": ("conversation_id", "is_on_current_path"),
    "idx_nodes_conversation_flag_parent": ("conversation_id", "is_on_current_path", "parent_node_id"),
    "idx_conversations_times": ("create_time", "update_time"),
    "idx_warnings_run": ("import_run_id", "warning_type"),
}

# Machine-readable contracts for objects whose exact shape is required by
# production INSERT/UPSERT, JOIN, search-generation, and migration paths.
# Only named project-managed indexes/triggers are eligible for automatic
# replacement; table constraints are deliberately diagnosed as incompatible.
CANONICAL_TABLE_CONTRACT = {
    "import_runs": {
        "primary_key": ("id",),
        "columns": {
            "id": ("INTEGER", False, None),
            "input_path": ("TEXT", True, None),
            "input_kind": ("TEXT", True, None),
            "started_at": ("TEXT", True, None),
            "status": ("TEXT", True, None),
        },
    },
    "source_files": {
        "primary_key": ("id",),
        "columns": {
            "id": ("INTEGER", False, None),
            "import_run_id": ("INTEGER", True, None),
            "source_path": ("TEXT", True, None),
            "file_type": ("TEXT", True, None),
            "is_conversation_json": ("INTEGER", True, "0"),
            "is_selected_conversation_source": ("INTEGER", True, "0"),
        },
    },
    "import_warnings": {
        "primary_key": ("id",),
        "columns": {
            "id": ("INTEGER", False, None),
            "import_run_id": ("INTEGER", True, None),
            "source_file": ("TEXT", True, None),
            "warning_type": ("TEXT", True, None),
            "created_at": ("TEXT", True, None),
        },
    },
    "conversations": {
        "primary_key": ("conversation_id",),
        "columns": {
            "conversation_id": ("TEXT", True, None),
            "aggregate_hash": ("TEXT", True, None),
        },
    },
    "conversation_nodes": {
        "primary_key": ("conversation_id", "node_id"),
        "columns": {
            "conversation_id": ("TEXT", True, None),
            "node_id": ("TEXT", True, None),
            "is_on_current_path": ("INTEGER", True, "0"),
        },
    },
    "exports": {
        "primary_key": ("id",),
        "unique": (("conversation_id", "format", "output_path"),),
        "columns": {
            "id": ("INTEGER", False, None),
            "conversation_id": ("TEXT", True, None),
            "format": ("TEXT", True, None),
            "output_path": ("TEXT", True, None),
            "output_hash": ("TEXT", True, None),
            "exported_at": ("TEXT", True, None),
        },
    },
    "file_index": {
        "primary_key": ("id",),
        "columns": {
            "id": ("INTEGER", False, None),
            "import_run_id": ("INTEGER", True, None),
            "source_path": ("TEXT", True, None),
            "file_type": ("TEXT", True, None),
        },
    },
    "archive_generations": {
        "primary_key": ("name",),
        "columns": {
            "name": ("TEXT", True, None),
            "generation": ("INTEGER", True, "0"),
        },
    },
}

REQUIRED_INDEX_CONTRACT = {
    name: {
        "table": {
            "idx_nodes_conversation_path": "conversation_nodes",
            "idx_nodes_conversation_flag_parent": "conversation_nodes",
            "idx_conversations_times": "conversations",
            "idx_warnings_run": "import_warnings",
        }[name],
        "unique": False,
        "partial": False,
        "origin": "c",
        "keys": tuple((column, "BINARY", False) for column in columns),
        "where": None,
    }
    for name, columns in REQUIRED_INDEX_COLUMNS.items()
}

GENERATION_TRIGGER_CONTRACT = {
    "archive_title_generation_insert": ("conversations", "AFTER", "INSERT", (), None, "title"),
    "archive_title_generation_update": (
        "conversations", "AFTER", "UPDATE", ("conversation_id", "title"), None, "title"
    ),
    "archive_title_generation_delete": ("conversations", "AFTER", "DELETE", (), None, "title"),
    "archive_message_generation_insert": (
        "conversation_nodes", "AFTER", "INSERT", (), None, "message"
    ),
    "archive_message_generation_update": (
        "conversation_nodes",
        "AFTER",
        "UPDATE",
        ("conversation_id", "node_id", "message_id", "role", "author_name", "content_type", "content_text", "content_hash", "raw_message_json"),
        None,
        "message",
    ),
    "archive_message_generation_delete": (
        "conversation_nodes", "AFTER", "DELETE", (), None, "message"
    ),
    "archive_address_generation_conversation_insert": ("conversations", "AFTER", "INSERT", (), None, "address"),
    "archive_address_generation_conversation_update": ("conversations", "AFTER", "UPDATE", ("conversation_id", "exported_id", "current_node"), None, "address"),
    "archive_address_generation_conversation_delete": ("conversations", "AFTER", "DELETE", (), None, "address"),
    "archive_address_generation_node_insert": ("conversation_nodes", "AFTER", "INSERT", (), None, "address"),
    "archive_address_generation_node_update": ("conversation_nodes", "AFTER", "UPDATE", ("conversation_id", "node_id", "parent_node_id", "children_json", "message_id"), None, "address"),
    "archive_address_generation_node_delete": ("conversation_nodes", "AFTER", "DELETE", (), None, "address"),
    "archive_graph_generation_conversation_insert": ("conversations", "AFTER", "INSERT", (), None, "graph"),
    "archive_graph_generation_conversation_update": ("conversations", "AFTER", "UPDATE", ("conversation_id", "current_node"), None, "graph"),
    "archive_graph_generation_conversation_delete": ("conversations", "AFTER", "DELETE", (), None, "graph"),
    "archive_graph_generation_node_insert": ("conversation_nodes", "AFTER", "INSERT", (), None, "graph"),
    "archive_graph_generation_node_update": ("conversation_nodes", "AFTER", "UPDATE", ("conversation_id", "node_id", "parent_node_id", "children_json", "is_on_current_path"), None, "graph"),
    "archive_graph_generation_node_delete": ("conversation_nodes", "AFTER", "DELETE", (), None, "graph"),
    "archive_display_revision_node_insert": (
        "conversation_nodes", "AFTER", "INSERT", (), None, "display"
    ),
    "archive_display_revision_node_update": (
        "conversation_nodes",
        "AFTER",
        "UPDATE",
        ("content_type", "content_text", "content_hash", "raw_message_json"),
        None,
        "display",
    ),
}

REQUIRED_GENERATION_ROWS = ("title", "message", "address", "graph")


def begin_bulk_generation_aggregation(conn: sqlite3.Connection) -> None:
    """Suspend row triggers inside the caller's authoritative write transaction.

    SQLite rolls transactional DDL back together with canonical writes.  The
    caller must hold the write transaction until
    :func:`finish_bulk_generation_aggregation` recreates the exact managed
    triggers and advances each dirty domain once.  There is deliberately no
    durable or connection-global suppression switch that an external writer
    could inherit or leave enabled after a crash.
    """

    if not conn.in_transaction:
        raise DatabaseMigrationError("bulk_generation_transaction_required")
    if not generation_schema_contract_is_current(conn):
        raise DatabaseMigrationError("bulk_generation_schema_incompatible")
    for name in GENERATION_TRIGGER_DDL:
        conn.execute(f'DROP TRIGGER "{name}"')


def finish_bulk_generation_aggregation(
    conn: sqlite3.Connection,
    dirty_domains: Iterable[str],
) -> None:
    """Publish aggregated generations and restore triggers before commit."""

    if not conn.in_transaction:
        raise DatabaseMigrationError("bulk_generation_transaction_required")
    domains = sorted(set(str(value) for value in dirty_domains))
    if any(value not in REQUIRED_GENERATION_ROWS for value in domains):
        raise DatabaseMigrationError("bulk_generation_domain_invalid")
    existing = conn.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name IN ({})".format(
            ",".join("?" for _ in GENERATION_TRIGGER_DDL)
        ),
        tuple(GENERATION_TRIGGER_DDL),
    ).fetchall()
    if existing:
        raise DatabaseMigrationError("bulk_generation_trigger_collision")
    if domains:
        conn.execute(
            "UPDATE archive_generations SET generation = generation + 1 "
            "WHERE name IN ({})".format(",".join("?" for _ in domains)),
            domains,
        )
    for statement in GENERATION_TRIGGER_DDL.values():
        conn.execute(statement)
    if not generation_schema_contract_is_current(conn):
        raise DatabaseMigrationError("bulk_generation_restore_failed")


class DatabaseMigrationError(ValueError):
    """Stable, content-free database schema or migration failure."""

    def __init__(self, code: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = dict(detail or {})

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class ReadRequestCapabilities:
    database_identity: tuple[str, int, int] | tuple[str]
    user_version: int
    schema_version: int
    generation_snapshot: tuple[int, int, int, int]
    schema_status: dict[str, Any]
    fts5_available: bool
    trigram_available: bool


_READ_CAPABILITY_CACHE: dict[
    tuple[tuple[str, int, int] | tuple[str], int, int, tuple[int, int, int, int]],
    tuple[dict[str, Any], bool, bool],
] = {}
_READ_CAPABILITY_CACHE_LOCK = threading.Lock()


def _read_database_identity(conn: sqlite3.Connection) -> tuple[str, int, int] | tuple[str]:
    row = conn.execute("PRAGMA database_list").fetchone()
    path = str(row[2] if row is not None else "")
    if not path:
        return ("memory",)
    try:
        info = os.stat(path)
    except OSError:
        return (path,)
    return (str(Path(path).resolve()), int(info.st_dev), int(info.st_ino))


def read_request_capabilities(conn: sqlite3.Connection) -> ReadRequestCapabilities:
    """Return a schema snapshot with full introspection cached by DB identity/version."""

    identity = _read_database_identity(conn)
    user_version = _database_user_version(conn)
    row = conn.execute("PRAGMA schema_version").fetchone()
    schema_version = int(row[0] if row is not None else -1)
    try:
        generations = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT name, generation FROM archive_generations "
                "WHERE name IN ('message', 'title', 'address', 'graph')"
            )
        }
    except sqlite3.Error:
        generations = {}
    generation_snapshot = (
        generations.get("message", -1),
        generations.get("title", -1),
        generations.get("address", -1),
        generations.get("graph", -1),
    )
    # data_version is connection-relative and cannot identify commits across
    # fresh request connections. Durable generation rows and schema_version do.
    key = (identity, user_version, schema_version, generation_snapshot)
    with _READ_CAPABILITY_CACHE_LOCK:
        cached = _READ_CAPABILITY_CACHE.get(key)
    if cached is None:
        schema_status = database_schema_status(conn)
        if schema_status.get("schema_compatible"):
            legacy_limit = 16 * 1024
            conn.create_function(
                "archive_legacy_id_safe",
                1,
                lambda value: 1 if _legacy_graph_id_is_safe(value, legacy_limit) else 0,
                deterministic=True,
            )
            incompatible = conn.execute(
                """SELECT 1 FROM conversations
                   WHERE archive_legacy_id_safe(conversation_id) = 0
                      OR archive_legacy_id_safe(exported_id) = 0
                      OR archive_legacy_id_safe(current_node) = 0
                   UNION ALL
                   SELECT 1 FROM conversation_nodes
                   WHERE archive_legacy_id_safe(conversation_id) = 0
                      OR archive_legacy_id_safe(node_id) = 0
                      OR archive_legacy_id_safe(parent_node_id) = 0
                      OR archive_legacy_id_safe(message_id) = 0
                      OR (children_json IS NOT NULL AND json_valid(children_json) = 0)
                      OR EXISTS (
                          SELECT 1
                          FROM json_each(
                              CASE WHEN json_valid(children_json) THEN children_json ELSE '[]' END
                          ) child
                          WHERE child.type != 'text'
                             OR archive_legacy_id_safe(child.value) = 0
                      )
                   LIMIT 1""",
            ).fetchone() is not None
            if incompatible:
                schema_status = {
                    **schema_status,
                    "data_compatible": False,
                    "data_error_code": "database_data_incompatible",
                    "max_addressable_legacy_id_chars": legacy_limit,
                    "repair_guidance": "use a trusted local SQLite backup and repair overlong legacy graph identifiers before serving",
                }
            else:
                schema_status = {**schema_status, "data_compatible": True}
        fts5_available = detect_fts5_runtime(conn)
        trigram_available = _detect_trigram_runtime(conn)
        cached = (schema_status, fts5_available, trigram_available)
        with _READ_CAPABILITY_CACHE_LOCK:
            if len(_READ_CAPABILITY_CACHE) >= 32:
                _READ_CAPABILITY_CACHE.clear()
            _READ_CAPABILITY_CACHE[key] = (dict(schema_status), fts5_available, trigram_available)
    schema_status, fts5_available, trigram_available = cached
    return ReadRequestCapabilities(
        database_identity=identity,
        user_version=user_version,
        schema_version=schema_version,
        generation_snapshot=generation_snapshot,
        schema_status=dict(schema_status),
        fts5_available=fts5_available,
        trigram_available=trigram_available,
    )


def _detect_trigram_runtime(conn: sqlite3.Connection) -> bool:
    table_name = f"__archive_trigram_probe_{id(conn):x}"
    try:
        conn.execute(
            f'CREATE VIRTUAL TABLE temp."{table_name}" USING fts5(value, tokenize="trigram")'
        )
        return True
    except sqlite3.OperationalError as exc:
        if is_fts5_capability_unavailable(exc):
            return False
        raise
    finally:
        try:
            conn.execute(f'DROP TABLE IF EXISTS temp."{table_name}"')
        except sqlite3.Error:
            pass


def invalidate_read_capability_cache() -> None:
    with _READ_CAPABILITY_CACHE_LOCK:
        _READ_CAPABILITY_CACHE.clear()


def database_schema_error_code(status: dict[str, Any]) -> str | None:
    """Map a schema status to the stable read/write dependency error code."""

    if status.get("data_compatible") is False:
        return str(status.get("data_error_code") or "database_data_incompatible")
    if status.get("database_schema_newer"):
        return "database_schema_newer"
    if status.get("migration_required"):
        return "database_migration_required"
    if not status.get("schema_compatible"):
        return "database_schema_incompatible"
    return None


def require_current_database_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Cheap production-read gate without migration, DDL, or full-table checks.

    Database-wide foreign-key verification is deliberately reserved for
    ``verify`` and the deep health endpoint.  Running it in every request
    makes an indexed lookup proportional to the entire archive.
    """

    status = read_request_capabilities(conn).schema_status
    code = database_schema_error_code(status)
    if code is not None:
        raise DatabaseMigrationError(code, detail=status)
    return status
IMPORT_REBUILDABLE_INDEXES = (
    (
        "idx_nodes_conversation_path",
        """
        CREATE INDEX IF NOT EXISTS idx_nodes_conversation_path
            ON conversation_nodes(conversation_id, is_on_current_path)
        """,
    ),
    (
        "idx_nodes_conversation_flag_parent",
        """
        CREATE INDEX IF NOT EXISTS idx_nodes_conversation_flag_parent
            ON conversation_nodes(conversation_id, is_on_current_path, parent_node_id)
        """,
    ),
)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    register_archive_sql_functions(conn)
    return conn


def connect_existing_readonly(db_path: Path) -> sqlite3.Connection:
    absolute = db_path.expanduser().absolute()
    uri = f"{absolute.as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ValueError(sqlite_open_error_code(exc, path_exists=absolute.exists())) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    register_archive_sql_functions(conn)
    return conn


def connect_existing(db_path: Path) -> sqlite3.Connection:
    absolute = db_path.expanduser().absolute()
    uri = f"{absolute.as_uri()}?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ValueError(sqlite_open_error_code(exc, path_exists=absolute.exists())) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    register_archive_sql_functions(conn)
    return conn


def register_archive_sql_functions(conn: sqlite3.Connection) -> None:
    """Install the SQL functions used by every archive search/read entry point.

    Keeping this registration beside the canonical connection helpers avoids
    making optional-index SQL work only through the Web-specific connectors.
    Imports are local to avoid coupling module initialization to the larger
    search implementation.
    """

    from .parser import recover_message_display_text
    from .search import normalize_search_text, search_fragment_match

    conn.create_function("web_norm", 1, normalize_search_text, deterministic=True)
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text)


def _legacy_graph_id_is_safe(value: Any, limit: int = 16 * 1024) -> bool:
    """Return whether a legacy identifier is JSON/URL/SQLite round-trip safe."""

    if value is None:
        return True
    if not isinstance(value, str) or len(value) > limit:
        return False
    for character in value:
        codepoint = ord(character)
        if (
            codepoint <= 0x1F
            or codepoint == 0x7F
            or 0xD800 <= codepoint <= 0xDFFF
            or 0xFDD0 <= codepoint <= 0xFDEF
            or codepoint & 0xFFFF in (0xFFFE, 0xFFFF)
        ):
            return False
    return True


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Keep one logical multi-query read operation on one SQLite snapshot.

    A caller-owned transaction is preserved.  Otherwise this context begins
    before schema/capability probes and releases the snapshot on every success
    or failure path.  Writable callers may deliberately upgrade and commit the
    transaction themselves after producing snapshot-consistent output.
    """

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise
    else:
        if owns_transaction and conn.in_transaction:
            conn.commit()


def configure_bulk_write_connection(conn: sqlite3.Connection) -> None:
    """Apply conservative write-time tuning for large local SQLite writes."""
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


def configure_import_connection(conn: sqlite3.Connection) -> None:
    """Apply conservative write-time tuning for the import command."""
    configure_bulk_write_connection(conn)


def drop_import_rebuildable_indexes(conn: sqlite3.Connection) -> None:
    """Drop ordinary indexes that are cheaper to rebuild after bulk node writes."""
    for index_name, _sql in IMPORT_REBUILDABLE_INDEXES:
        conn.execute(f'DROP INDEX IF EXISTS "{index_name}"')


def recreate_import_rebuildable_indexes(conn: sqlite3.Connection) -> None:
    """Restore indexes dropped by drop_import_rebuildable_indexes."""
    for _index_name, sql in IMPORT_REBUILDABLE_INDEXES:
        conn.execute(sql)


def _database_has_user_objects(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'view', 'index', 'trigger')
           LIMIT 1"""
    ).fetchone()
    return row is not None


def _database_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0] if row is not None else 0)


def _migration_sqlite_error_code(exc: sqlite3.Error) -> str:
    code = sqlite_runtime_error_code(exc)
    return "database_migration_failed" if code == "database_runtime_failure" else code


def _base_schema_compatibility(conn: sqlite3.Connection) -> dict[str, Any]:
    status = database_schema_status(conn)
    return {
        "compatible": status["base_schema_compatible"],
        "missing_tables": status["missing_tables"],
        "missing_columns": status["missing_columns"],
        "invalid_tables": status["invalid_tables"],
        "object_type_mismatches": status["object_type_mismatches"],
        "missing_foreign_keys": status["missing_foreign_keys"],
        "invalid_generation_rows": status["invalid_generation_rows"],
    }


# Every entry is a schema version that was actually shipped.  Keep this an
# explicit graph: a lower integer is not, by itself, evidence that its table
# shape is one we know how to migrate.
MIGRATIONS = {
    0: "rebuild_nullable_identity_v3",
    1: "rebuild_nullable_identity_v3",
    2: "rebuild_nullable_identity_v3",
    3: "add_durable_revision_domains_v4",
}
_SUPPORTED_SCHEMA_PREDECESSORS = frozenset(MIGRATIONS)

_MIGRATION_REBUILT_TABLES = frozenset({"conversations", "archive_generations"})
_SAFE_SCHEMA_OBJECT_NAME_CHARS = 160
_KNOWN_MANAGED_TRIGGER_PREDECESSORS = {
    "archive_message_generation_update": (
        """CREATE TRIGGER IF NOT EXISTS archive_message_generation_update
        AFTER UPDATE OF conversation_id, node_id, content_text, raw_message_json ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'message';
        END""",
    ),
    "archive_address_generation_conversation_update": (
        """CREATE TRIGGER IF NOT EXISTS archive_address_generation_conversation_update
        AFTER UPDATE OF conversation_id, current_node ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'address';
        END""",
    ),
}
_MANAGED_SCHEMA_NAMES = {
    name.casefold(): name
    for name in (*REQUIRED_INDEX_DDL, *CANONICAL_TRIGGER_DDL)
}


def _bounded_schema_object_name(value: object) -> str:
    text = str(value)
    return text[:_SAFE_SCHEMA_OBJECT_NAME_CHARS]


def _schema_sql_mentions(sql: str, identifier: str) -> bool:
    """Conservatively recognize an identifier in user-authored schema SQL."""

    escaped = re.escape(identifier)
    return re.search(
        rf"(?<![A-Za-z0-9_])(?:{escaped}|\"{escaped}\"|`{escaped}`|\[{escaped}\])(?![A-Za-z0-9_])",
        sql,
        re.IGNORECASE,
    ) is not None


def _migration_custom_object_conflicts(
    conn: sqlite3.Connection,
    *,
    status: dict[str, Any],
    rebuilt_tables: frozenset[str],
) -> list[dict[str, str]]:
    """Inventory schema objects and return unsafe rebuild dependencies.

    This intentionally chooses the refusal branch of the migration contract.
    SQLite DDL can contain arbitrary trigger programs and view dependency
    chains; replaying it safely would require a full SQLite parser.  Refusing
    before the first DROP/rename preserves every user object verbatim.
    """

    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE type IN ('table', 'view', 'trigger', 'index') "
        "ORDER BY type, name"
    ).fetchall()
    invalid_indexes = set(status.get("invalid_indexes") or {})
    invalid_triggers = set(status.get("invalid_triggers") or {})
    missing_indexes = set(status.get("missing_indexes") or ())
    missing_triggers = set(status.get("missing_triggers") or ())

    dependent_names = set(rebuilt_tables)
    # Resolve view dependencies to a fixed point.  False positives are safe:
    # they cause a manual-migration refusal before destructive DDL.
    changed = True
    while changed:
        changed = False
        for row in rows:
            object_type, name, _table, raw_sql = map(lambda value: value, row)
            if str(object_type) != "view" or str(name) in dependent_names:
                continue
            sql = raw_sql if isinstance(raw_sql, str) else ""
            if any(_schema_sql_mentions(sql, dependency) for dependency in dependent_names):
                dependent_names.add(str(name))
                changed = True

    conflicts: list[dict[str, str]] = []
    for row in rows:
        object_type = str(row[0])
        name = str(row[1])
        table = str(row[2])
        raw_sql = row[3]
        managed_canonical_name = _MANAGED_SCHEMA_NAMES.get(name.casefold())
        if name.startswith("sqlite_autoindex_") and raw_sql is None:
            continue
        if (
            object_type == "index"
            and managed_canonical_name == name
            and name in REQUIRED_INDEX_DDL
            and name not in invalid_indexes
            and name not in missing_indexes
        ):
            continue
        if (
            object_type == "trigger"
            and managed_canonical_name == name
            and name in CANONICAL_TRIGGER_DDL
            and name not in invalid_triggers
            and name not in missing_triggers
        ):
            continue

        sql = raw_sql if isinstance(raw_sql, str) else ""
        if (
            object_type == "trigger"
            and managed_canonical_name == name
            and name in _KNOWN_MANAGED_TRIGGER_PREDECESSORS
        ):
            actual_definition = _trigger_definition(sql)
            if any(
                actual_definition == _trigger_definition(predecessor)
                for predecessor in _KNOWN_MANAGED_TRIGGER_PREDECESSORS[name]
            ):
                continue

        # SQLite object names are ASCII case-insensitive across types.  A
        # differently-cased table/view/index/trigger can therefore block or be
        # mistaken for a managed object and must be rejected explicitly.
        if managed_canonical_name is not None:
            conflicts.append(
                {
                    "type": object_type[:32],
                    "name": _bounded_schema_object_name(name),
                    "reason": "managed_name_collision",
                    "target_table": _bounded_schema_object_name(table),
                }
            )
            continue
        if object_type == "table":
            continue

        depends_on_rebuild = bool(
            table in rebuilt_tables
            or name in dependent_names
            or any(_schema_sql_mentions(sql, dependency) for dependency in dependent_names)
        )
        # A project-owned name with a non-authoritative fingerprint is a name
        # collision, not permission to delete a user object.
        if depends_on_rebuild:
            conflicts.append(
                {
                    "type": object_type[:32],
                    "name": _bounded_schema_object_name(name),
                    "reason": "rebuild_dependency",
                    "target_table": _bounded_schema_object_name(table),
                }
            )
    return conflicts


def _raise_for_migration_custom_objects(
    conn: sqlite3.Connection,
    *,
    status: dict[str, Any],
    rebuilt_tables: frozenset[str],
) -> None:
    conflicts = _migration_custom_object_conflicts(
        conn,
        status=status,
        rebuilt_tables=rebuilt_tables,
    )
    if conflicts:
        managed_collisions = [
            item for item in conflicts if item.get("reason") == "managed_name_collision"
        ]
        if managed_collisions:
            raise DatabaseMigrationError(
                "database_managed_object_name_collision",
                detail={
                    "collision_count": len(managed_collisions),
                    "objects": managed_collisions[:32],
                    "manual_repair_required": True,
                },
            )
        raise DatabaseMigrationError(
            "database_custom_objects_require_manual_migration",
            detail={
                "object_count": len(conflicts),
                "objects": conflicts[:32],
                "manual_repair_required": True,
            },
        )


def _nullable_identity_only(error: dict[str, Any] | None, column: str) -> bool:
    """Recognize the exact legacy rowid-table TEXT PRIMARY KEY contract."""

    if not isinstance(error, dict) or set(error) != {"columns"}:
        return False
    columns = error.get("columns")
    if not isinstance(columns, dict) or set(columns) != {column}:
        return False
    detail = columns[column]
    return detail == {
        "expected": {"type": "TEXT", "not_null": True, "default": None},
        "actual": {"type": "TEXT", "not_null": False, "default": None},
    }


def _supported_predecessor_schema(status: dict[str, Any]) -> bool:
    """Return whether *status* is an exact schema that this version can rebuild."""

    version = int(status.get("current_database_schema_version", -1))
    if version not in _SUPPORTED_SCHEMA_PREDECESSORS:
        return False
    if status.get("base_schema_compatible"):
        return True
    if (
        status.get("database_schema_newer")
        or status.get("missing_columns")
        or status.get("object_type_mismatches")
        or status.get("missing_foreign_keys")
        or status.get("invalid_generation_rows")
    ):
        return False
    missing_tables = set(status.get("missing_tables") or ())
    if missing_tables - {"archive_generations"}:
        return False
    invalid = dict(status.get("invalid_tables") or {})
    conversations = invalid.pop("conversations", None)
    if not _nullable_identity_only(conversations, "conversation_id"):
        return False
    generations = invalid.pop("archive_generations", None)
    if generations is not None and not _nullable_identity_only(generations, "name"):
        return False
    return not invalid


def _identity_contains_null(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return conn.execute(
        f'SELECT 1 FROM "{table}" WHERE "{column}" IS NULL LIMIT 1'
    ).fetchone() is not None


def _rebuild_nullable_identity_tables(conn: sqlite3.Connection, *, has_generations: bool) -> None:
    """Apply the v3 NOT NULL identity migration inside the caller's transaction."""

    if _identity_contains_null(conn, "conversations", "conversation_id"):
        raise DatabaseMigrationError(
            "database_schema_incompatible",
            detail={"manual_repair_required": True, "null_identity": "conversations.conversation_id"},
        )
    if has_generations and _identity_contains_null(conn, "archive_generations", "name"):
        raise DatabaseMigrationError(
            "database_schema_incompatible",
            detail={"manual_repair_required": True, "null_identity": "archive_generations.name"},
        )

    for name in CANONICAL_TRIGGER_DDL:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    conn.execute(
        """CREATE TABLE conversations_v3 (
            conversation_id TEXT NOT NULL PRIMARY KEY,
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
        )"""
    )
    conversation_columns = (
        "conversation_id, exported_id, title, create_time, update_time, current_node, "
        "source_file, source_array_index, aggregate_hash, last_import_run_id, is_archived, "
        "is_starred, default_model_slug, metadata_json"
    )
    conn.execute(
        f"INSERT INTO conversations_v3({conversation_columns}) "
        f"SELECT {conversation_columns} FROM conversations"
    )
    conn.execute("DROP TABLE conversations")
    conn.execute("ALTER TABLE conversations_v3 RENAME TO conversations")

    if has_generations:
        conn.execute(
            """CREATE TABLE archive_generations_v3 (
                name TEXT NOT NULL PRIMARY KEY,
                generation INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            "INSERT INTO archive_generations_v3(name, generation) "
            "SELECT name, generation FROM archive_generations"
        )
        conn.execute("DROP TABLE archive_generations")
        conn.execute("ALTER TABLE archive_generations_v3 RENAME TO archive_generations")


def migrate_database(conn: sqlite3.Connection, *, allow_initialize: bool = False) -> dict[str, Any]:
    """Initialize or migrate the canonical schema in one protected transaction.

    Read-only callers never invoke this function.  The schema version is
    raised only after every required table, row, trigger, and index succeeds.
    """

    if conn.in_transaction:
        status = database_schema_status(conn)
        if not status["base_schema_compatible"]:
            raise DatabaseMigrationError(
                "database_schema_incompatible", detail=_base_schema_compatibility(conn)
            )
        raise DatabaseMigrationError("database_transaction_active")
    foreign_keys_before = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    try:
        # SQLite requires foreign_keys to be changed outside a transaction for
        # a parent-table rebuild. BEGIN IMMEDIATE then excludes every writer;
        # all authoritative inspection and both FK checks remain under that lock.
        if foreign_keys_before:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        has_objects = _database_has_user_objects(conn)
        validate_core_fts_ownership(conn)
        current_version = _database_user_version(conn)
        if current_version > DATABASE_SCHEMA_VERSION:
            raise DatabaseMigrationError(
                "database_schema_newer",
                detail={
                    "current_database_schema_version": current_version,
                    "required_database_schema_version": DATABASE_SCHEMA_VERSION,
                },
            )
        if not has_objects and not allow_initialize:
            raise DatabaseMigrationError("database_not_ready")
        locked_status = database_schema_status(conn) if has_objects else None
        upgrading_identity_contract = bool(has_objects and current_version < 3)
        if locked_status is not None:
            _raise_for_migration_custom_objects(
                conn,
                status=locked_status,
                rebuilt_tables=(
                    _MIGRATION_REBUILT_TABLES
                    if upgrading_identity_contract
                    else frozenset()
                ),
            )
        if locked_status is not None and current_version < DATABASE_SCHEMA_VERSION:
            if current_version not in MIGRATIONS:
                raise DatabaseMigrationError(
                    "database_schema_unsupported_predecessor",
                    detail={
                        "current_database_schema_version": current_version,
                        "required_database_schema_version": DATABASE_SCHEMA_VERSION,
                        "manual_repair_required": True,
                    },
                )
            if not _supported_predecessor_schema(locked_status):
                raise DatabaseMigrationError(
                    "database_schema_incompatible", detail=_base_schema_compatibility(conn)
                )
        elif locked_status is not None and not locked_status["base_schema_compatible"]:
            raise DatabaseMigrationError(
                "database_schema_incompatible", detail=_base_schema_compatibility(conn)
            )
        if has_objects and conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DatabaseMigrationError("database_foreign_key_violation")
        if locked_status is not None and locked_status["schema_compatible"] and not locked_status["migration_required"]:
            conn.commit()
            invalidate_read_capability_cache()
            return {"changed": False, "initialized": False, **locked_status}

        initialized = not has_objects
        if upgrading_identity_contract:
            _rebuild_nullable_identity_tables(
                conn,
                has_generations="archive_generations" not in set(locked_status["missing_tables"]),
            )
        if not has_objects:
            for statement in CANONICAL_TABLE_DDL:
                conn.execute(statement)
        web_index_source_triggers = {
            name for name, contract in GENERATION_TRIGGER_CONTRACT.items()
            if contract[-1] in {"message", "title"}
        }
        missing_generation_rows = set(
            (locked_status or {}).get("missing_generation_rows") or ()
        )
        invalid_generation_rows = set(
            ((locked_status or {}).get("invalid_generation_rows") or {}).keys()
        )
        missing_triggers = set((locked_status or {}).get("missing_triggers") or ())
        invalid_triggers = set(
            ((locked_status or {}).get("invalid_triggers") or {}).keys()
        )
        web_index_source_infrastructure_changed = bool(
            upgrading_identity_contract
            or (locked_status and locked_status.get("missing_generation_table"))
            or missing_generation_rows.intersection({"message", "title"})
            or invalid_generation_rows.intersection({"message", "title"})
            or missing_triggers.intersection(web_index_source_triggers)
            or invalid_triggers.intersection(web_index_source_triggers)
        )
        if web_index_source_infrastructure_changed and has_objects:
            failures = drop_optional_web_indexes(conn)
            if failures:
                raise DatabaseMigrationError(
                    "optional_web_index_cleanup_failed",
                    detail={"failures": failures},
                )
        conn.execute(GENERATION_TABLE_DDL)
        conn.executemany(
            "INSERT OR IGNORE INTO archive_generations(name, generation) VALUES (?, 0)",
            ((name,) for name in REQUIRED_GENERATION_ROWS),
        )
        for name in (locked_status or {}).get("invalid_triggers", {}):
            conn.execute(f'DROP TRIGGER "{name}"')
        for statement in CANONICAL_TRIGGER_DDL.values():
            conn.execute(statement)
        # Existing v4 rows predate row-specific display revisions.  Tombstones
        # are intentionally retained so a deleted/reinserted rowid cannot
        # recycle an earlier cursor revision.
        conn.execute(
            "INSERT OR IGNORE INTO archive_generations(name, generation) "
            "SELECT printf('display:%lld', rowid), 1 FROM conversation_nodes"
        )
        for name in (locked_status or {}).get("invalid_indexes", {}):
            conn.execute(f'DROP INDEX "{name}"')
        for statement in REQUIRED_INDEX_DDL.values():
            conn.execute(statement)
        pre_version = database_schema_status(conn)
        if (
            not pre_version["base_schema_compatible"]
            or pre_version["missing_generation_rows"]
            or pre_version["missing_triggers"]
            or pre_version["invalid_triggers"]
            or pre_version["missing_indexes"]
            or pre_version["invalid_indexes"]
            or conn.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise DatabaseMigrationError("database_migration_incomplete", detail=pre_version)
        if current_version < DATABASE_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
        after = database_schema_status(conn)
        if not after["schema_compatible"] or after["migration_required"]:
            raise DatabaseMigrationError("database_migration_incomplete", detail=after)
        conn.commit()
        invalidate_read_capability_cache()
    except DatabaseMigrationError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise DatabaseMigrationError(
            _migration_sqlite_error_code(exc),
            detail={"error_type": type(exc).__name__},
        ) from exc
    finally:
        if foreign_keys_before and not conn.in_transaction:
            conn.execute("PRAGMA foreign_keys = ON")
    return {"changed": True, "initialized": initialized, **after}


def init_db(conn: sqlite3.Connection) -> bool:
    validate_core_fts_ownership(conn)
    migrate_database(conn, allow_initialize=True)
    fts_enabled = ensure_fts(conn)
    conn.commit()
    invalidate_read_capability_cache()
    return fts_enabled


OPTIONAL_WEB_TRIGRAM_TABLES = ("web_message_trigram", "web_title_trigram")
OPTIONAL_WEB_NORM_TABLES = ("web_message_norm", "web_title_norm")
OPTIONAL_WEB_METADATA_TABLES = ("web_index_metadata", "web_index_oversized")
OPTIONAL_WEB_INDEX_TABLES = OPTIONAL_WEB_TRIGRAM_TABLES + OPTIONAL_WEB_NORM_TABLES + OPTIONAL_WEB_METADATA_TABLES
OPTIONAL_WEB_INDEX_OWNER = "chatgpt-sqlite-webui"
OPTIONAL_WEB_INDEX_OWNER_KEY = "project_object_owner"


def _schema_rows_for_names(
    conn: sqlite3.Connection,
    names: tuple[str, ...],
) -> dict[str, list[sqlite3.Row | tuple[Any, ...]]]:
    """Read all ASCII-case-insensitive collisions for authoritative names."""

    if not names:
        return {}
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE lower(name) IN ({})".format(
            ",".join("lower(?)" for _ in names)
        ),
        names,
    ).fetchall()
    result: dict[str, list[sqlite3.Row | tuple[Any, ...]]] = {
        name: [] for name in names
    }
    by_fold = {name.casefold(): name for name in names}
    for row in rows:
        name = str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        canonical = by_fold.get(name.casefold())
        if canonical is not None:
            result[canonical].append(row)
    return result


def _row_value(row: sqlite3.Row | tuple[Any, ...], key: str, index: int) -> Any:
    return row[key] if isinstance(row, sqlite3.Row) else row[index]


def _raise_object_collision(
    code: str,
    rows: list[sqlite3.Row | tuple[Any, ...]],
    *,
    expected_type: str = "table",
) -> None:
    objects = [
        {
            "name": _bounded_schema_object_name(_row_value(row, "name", 1)),
            "object_type": str(_row_value(row, "type", 0))[:32],
            "expected_type": expected_type,
        }
        for row in rows[:32]
    ]
    raise DatabaseMigrationError(
        code,
        detail={"collision_count": max(1, len(rows)), "objects": objects},
    )


def _table_xinfo_contract(
    conn: sqlite3.Connection,
    name: str,
) -> tuple[tuple[str, str, bool, str | None, int, bool], ...]:
    return tuple(
        (
            str(row[1]),
            str(row[2] or "").strip().upper(),
            bool(row[3]),
            _normalize_default(row[4]),
            int(row[5]),
            bool(row[6]),
        )
        for row in conn.execute(f'PRAGMA table_xinfo("{name}")')
    )


_OPTIONAL_TABLE_XINFO = {
    "web_message_norm": (
        ("conversation_id", "TEXT", True, None, 1, False),
        ("node_id", "TEXT", True, None, 2, False),
        ("content_norm", "TEXT", True, None, 0, False),
    ),
    "web_title_norm": (
        ("conversation_id", "TEXT", True, None, 1, False),
        ("title_norm", "TEXT", True, None, 0, False),
    ),
    "web_index_metadata": (
        ("key", "TEXT", True, None, 1, False),
        ("value", "TEXT", True, None, 0, False),
    ),
    "web_index_oversized": (
        ("kind", "TEXT", True, None, 1, False),
        ("source_rowid", "INTEGER", True, None, 2, False),
        ("conversation_id", "TEXT", True, None, 0, False),
        ("node_id", "TEXT", False, None, 0, False),
        ("input_bytes", "INTEGER", True, None, 0, False),
        ("reason", "TEXT", True, None, 0, False),
    ),
}


def _normalized_fts5_arguments(sql: Any) -> str | None:
    if not isinstance(sql, str):
        return None
    match = re.search(r"\bUSING\s+fts5\s*\((.*)\)\s*$", sql, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    value = re.sub(r"\s+", " ", match.group(1).strip()).casefold()
    value = re.sub(r"\s*,\s*", ",", value)
    value = re.sub(r"\s*=\s*", "=", value)
    value = value.replace('"', "'")
    return value


def _validate_fts5_family(
    conn: sqlite3.Connection,
    parent: str,
    *,
    visible_columns: tuple[str, ...],
    arguments: str,
    collision_code: str,
    allow_absent: bool = True,
    allow_missing_shadows: bool = False,
) -> bool:
    """Validate an exact project-created FTS5 parent and all known shadows."""

    shadow_suffixes = ("_data", "_idx", "_config", "_docsize")
    if "content=''" not in arguments:
        shadow_suffixes = ("_content",) + shadow_suffixes
    names = (parent,) + tuple(f"{parent}{suffix}" for suffix in shadow_suffixes)
    rows_by_name = _schema_rows_for_names(conn, names)
    present_rows = [row for rows in rows_by_name.values() for row in rows]
    if not present_rows:
        if allow_absent:
            return False
        _raise_object_collision(collision_code, [], expected_type="fts5")
    for name in names:
        rows = rows_by_name[name]
        if name != parent and not rows and allow_missing_shadows:
            continue
        if len(rows) != 1:
            _raise_object_collision(collision_code, present_rows, expected_type="fts5")
        if name != parent:
            suffix = name[len(parent):]
            expected_shadow = {
                "_data": (
                    ("id", "INTEGER", False, None, 1, False),
                    ("block", "BLOB", False, None, 0, False),
                ),
                "_idx": (
                    ("segid", "", True, None, 1, False),
                    ("term", "", True, None, 2, False),
                    ("pgno", "", False, None, 0, False),
                ),
                "_config": (
                    ("k", "", True, None, 1, False),
                    ("v", "", False, None, 0, False),
                ),
                "_docsize": (
                    ("id", "INTEGER", False, None, 1, False),
                    ("sz", "BLOB", False, None, 0, False),
                ),
                "_content": tuple(
                    [("id", "INTEGER", False, None, 1, False)]
                    + [
                        (f"c{index}", "", False, None, 0, False)
                        for index in range(len(visible_columns))
                    ]
                ),
            }[suffix]
            if _table_xinfo_contract(conn, name) != expected_shadow:
                _raise_object_collision(collision_code, present_rows, expected_type="fts5")
        row = rows[0]
        if (
            str(_row_value(row, "name", 1)) != name
            or str(_row_value(row, "type", 0)) != "table"
            or str(_row_value(row, "tbl_name", 2)) != name
        ):
            _raise_object_collision(collision_code, present_rows, expected_type="fts5")
    parent_row = rows_by_name[parent][0]
    if _normalized_fts5_arguments(_row_value(parent_row, "sql", 3)) != arguments:
        _raise_object_collision(collision_code, present_rows, expected_type="fts5")
    xinfo = _table_xinfo_contract(conn, parent)
    expected_names = visible_columns + (parent, "rank")
    if tuple(item[0] for item in xinfo) != expected_names or any(
        item[5] != (index >= len(visible_columns)) for index, item in enumerate(xinfo)
    ):
        _raise_object_collision(collision_code, present_rows, expected_type="fts5")
    return True


def validate_core_fts_ownership(conn: sqlite3.Connection, *, allow_absent: bool = True) -> bool:
    return _validate_fts5_family(
        conn,
        "message_fts",
        visible_columns=("conversation_id", "node_id", "role", "content_text"),
        arguments="conversation_id unindexed,node_id unindexed,role unindexed,content_text",
        collision_code="core_fts_name_collision",
        allow_absent=allow_absent,
    )


def validate_optional_web_index_ownership(conn: sqlite3.Connection) -> bool:
    """Validate the complete live optional-index family before destructive DDL.

    Current and exact predecessor objects are accepted.  A partial family or
    any name/type/shape/shadow collision is refused as an unknown user object.
    """

    base_names = tuple(_OPTIONAL_TABLE_XINFO)
    trigram_names = OPTIONAL_WEB_TRIGRAM_TABLES
    all_names = base_names + trigram_names + tuple(
        f"{name}{suffix}"
        for name in trigram_names
        for suffix in ("_data", "_idx", "_config", "_docsize")
    )
    rows_by_name = _schema_rows_for_names(conn, all_names)
    present_rows = [row for rows in rows_by_name.values() for row in rows]
    if not present_rows:
        return False
    base_present = [bool(rows_by_name[name]) for name in base_names]
    if not all(base_present):
        _raise_object_collision("optional_index_name_collision", present_rows)
    for name, expected in _OPTIONAL_TABLE_XINFO.items():
        rows = rows_by_name[name]
        if len(rows) != 1:
            _raise_object_collision("optional_index_name_collision", present_rows)
        row = rows[0]
        if (
            str(_row_value(row, "name", 1)) != name
            or str(_row_value(row, "type", 0)) != "table"
            or str(_row_value(row, "tbl_name", 2)) != name
            or _table_xinfo_contract(conn, name) != expected
        ):
            _raise_object_collision("optional_index_name_collision", present_rows)
    try:
        metadata = {
            str(row[0]): str(row[1])
            for row in conn.execute("SELECT key, value FROM web_index_metadata")
        }
    except sqlite3.Error:
        _raise_object_collision("optional_index_name_collision", present_rows)
    required_metadata = {
        "web_index_format_version",
        "display_text_resolver_version",
        "normalization_index_format_version",
        "message_generation",
        "title_generation",
    }
    if not required_metadata.issubset(metadata) or any(
        parse_nonnegative_integer(metadata.get(key)) is None
        for key in ("message_generation", "title_generation")
    ):
        _raise_object_collision("optional_index_name_collision", present_rows)
    if (
        metadata.get("web_index_format_version") != OPTIONAL_WEB_INDEX_FORMAT_VERSION
        or metadata.get("display_text_resolver_version") != DISPLAY_TEXT_RESOLVER_VERSION
        or metadata.get("normalization_index_format_version")
        != NORMALIZATION_INDEX_FORMAT_VERSION
    ):
        _raise_object_collision("optional_index_name_collision", present_rows)
    owner = metadata.get(OPTIONAL_WEB_INDEX_OWNER_KEY)
    if owner not in (None, OPTIONAL_WEB_INDEX_OWNER):
        _raise_object_collision("optional_index_name_collision", present_rows)
    trigram_any = any(bool(rows_by_name[name]) for name in all_names if name not in base_names)
    if trigram_any:
        _validate_fts5_family(
            conn,
            "web_message_trigram",
            visible_columns=("content_text",),
            arguments="content_text,content='',tokenize='trigram'",
            collision_code="optional_index_name_collision",
            allow_absent=False,
            allow_missing_shadows=owner == OPTIONAL_WEB_INDEX_OWNER,
        )
        _validate_fts5_family(
            conn,
            "web_title_trigram",
            visible_columns=("title",),
            arguments="title,content='',tokenize='trigram'",
            collision_code="optional_index_name_collision",
            allow_absent=False,
            allow_missing_shadows=owner == OPTIONAL_WEB_INDEX_OWNER,
        )
    return True


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
    modifies conversations and nodes, so keeping stale normalized rows or
    stale trigram candidates could create false-positive or false-negative
    search results after an incremental import.
    """
    validate_optional_web_index_ownership(conn)
    failures: list[dict[str, str]] = []
    for table in OPTIONAL_WEB_TRIGRAM_TABLES:
        failures.extend(_drop_table_with_shadows(conn, table))
    for table in OPTIONAL_WEB_NORM_TABLES:
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        except sqlite3.Error as exc:
            failures.append({"table": table, "error_type": type(exc).__name__})
    for table in OPTIONAL_WEB_METADATA_TABLES:
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        except sqlite3.Error as exc:
            failures.append({"table": table, "error_type": type(exc).__name__})
    return failures


def ensure_fts(conn: sqlite3.Connection) -> bool:
    try:
        if validate_core_fts_ownership(conn):
            return True
    except sqlite3.OperationalError as exc:
        if is_fts5_capability_unavailable(exc):
            return False
        raise
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts
            USING fts5(conversation_id UNINDEXED, node_id UNINDEXED, role UNINDEXED, content_text)
            """
        )
        return validate_core_fts_ownership(conn, allow_absent=False)
    except sqlite3.OperationalError as exc:
        if is_fts5_capability_unavailable(exc):
            return False
        raise


def detect_fts5_runtime(conn: sqlite3.Connection) -> bool:
    """Probe whether this connection can actually rebuild an FTS5 table."""

    table_name = f"__archive_fts5_probe_{id(conn):x}"
    try:
        conn.execute(f'CREATE VIRTUAL TABLE temp."{table_name}" USING fts5(value)')
        return True
    except sqlite3.OperationalError as exc:
        if is_fts5_capability_unavailable(exc):
            return False
        raise
    finally:
        try:
            conn.execute(f'DROP TABLE IF EXISTS temp."{table_name}"')
        except sqlite3.Error:
            pass


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
    invalidate_read_capability_cache()


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
) -> dict[str, Any]:
    """Upsert a shard worth of conversations with batched node and FTS writes."""
    if not conversations:
        return {"inserted": 0, "updated": 0, "unchanged": 0, "outcomes": {}}
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
        "outcomes": {
            **{conv.conversation_id: "inserted" for conv in inserted},
            **{conv.conversation_id: "updated" for conv in updated},
            **{conv.conversation_id: "unchanged" for conv in unchanged},
        },
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
        conn.execute("INSERT INTO message_fts(message_fts) VALUES('optimize')")
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
    return is_optional_search_capability_missing(exc)


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
    conv = conn.execute(
        """SELECT COUNT(*) AS c,
                  MIN(CASE WHEN create_time BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 THEN create_time END) AS min_ct,
                  MAX(CASE WHEN create_time BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 THEN create_time END) AS max_ct,
                  MIN(CASE WHEN update_time BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 THEN update_time END) AS min_ut,
                  MAX(CASE WHEN update_time BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 THEN update_time END) AS max_ut
           FROM conversations"""
    ).fetchone()
    nodes = conn.execute("SELECT COUNT(*) AS c FROM conversation_nodes").fetchone()
    warnings = conn.execute("SELECT COUNT(*) AS c FROM import_warnings").fetchone()
    exports = conn.execute("SELECT COUNT(*) AS c FROM exports").fetchone()
    return {
        "conversations": conv["c"],
        "nodes": nodes["c"],
        "warnings": warnings["c"],
        "earliest_create_time": finite_float_or_none(conv["min_ct"]),
        "latest_create_time": finite_float_or_none(conv["max_ct"]),
        "earliest_update_time": finite_float_or_none(conv["min_ut"]),
        "latest_update_time": finite_float_or_none(conv["max_ut"]),
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
    "exports",
    "archive_generations",
})

CORE_SCHEMA_COLUMNS = {
    "import_runs": frozenset({
        "id", "input_path", "input_kind", "input_sha256", "input_size",
        "started_at", "finished_at", "status", "summary_json",
    }),
    "source_files": frozenset({
        "id", "import_run_id", "source_path", "file_type", "size", "sha256",
        "is_conversation_json", "is_selected_conversation_source",
    }),
    "import_warnings": frozenset({
        "id", "import_run_id", "source_file", "array_index", "warning_type",
        "keys_json", "raw_json", "created_at",
    }),
    "conversations": frozenset({
        "conversation_id", "exported_id", "title", "create_time", "update_time",
        "current_node", "source_file", "source_array_index", "aggregate_hash",
        "last_import_run_id", "is_archived", "is_starred", "default_model_slug",
        "metadata_json",
    }),
    "conversation_nodes": frozenset({
        "conversation_id", "node_id", "parent_node_id", "children_json",
        "message_id", "role", "author_name", "create_time", "update_time",
        "content_type", "content_text", "content_hash", "metadata_json",
        "is_on_current_path", "raw_message_json", "last_import_run_id",
    }),
    "exports": frozenset({
        "id", "conversation_id", "format", "output_path", "output_hash",
        "exported_at", "export_options_json",
    }),
    "file_index": frozenset({
        "id", "import_run_id", "source_path", "file_type", "extension", "size",
        "sha256", "related_conversation_id", "related_message_id",
    }),
    "archive_generations": frozenset({"name", "generation"}),
}


REQUIRED_FOREIGN_KEYS = {
    "source_files": frozenset({("import_run_id", "import_runs", "id", "NO ACTION", "NO ACTION")}),
    "import_warnings": frozenset({("import_run_id", "import_runs", "id", "NO ACTION", "NO ACTION")}),
    "conversations": frozenset({("last_import_run_id", "import_runs", "id", "NO ACTION", "NO ACTION")}),
    "conversation_nodes": frozenset({
        ("conversation_id", "conversations", "conversation_id", "CASCADE", "NO ACTION"),
        ("last_import_run_id", "import_runs", "id", "NO ACTION", "NO ACTION"),
    }),
    "file_index": frozenset({("import_run_id", "import_runs", "id", "NO ACTION", "NO ACTION")}),
}


def _normalize_default(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while len(normalized) >= 2 and normalized[0] == "(" and normalized[-1] == ")":
        normalized = normalized[1:-1].strip()
    return normalized


def _index_keys(conn: sqlite3.Connection, name: str) -> tuple[tuple[str | None, str, bool], ...]:
    keys: list[tuple[str | None, str, bool]] = []
    for row in conn.execute(f'PRAGMA index_xinfo("{name}")'):
        if not bool(row[5]):
            continue
        column = None if int(row[1]) < 0 else str(row[2])
        keys.append((column, str(row[4] or "BINARY").upper(), bool(row[3])))
    return tuple(keys)


def _table_unique_keys(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    unique: set[tuple[str, ...]] = set()
    for row in conn.execute(f'PRAGMA index_list("{table}")'):
        if not bool(row[2]) or str(row[3]) != "u" or bool(row[4]):
            continue
        keys = _index_keys(conn, str(row[1]))
        if keys and all(
            column is not None and collation == "BINARY" and not desc
            for column, collation, desc in keys
        ):
            unique.add(tuple(str(column) for column, _collation, _desc in keys))
    return unique


_TRIGGER_HEADER_RE = re.compile(
    r"^\s*CREATE\s+TRIGGER(?:\s+IF\s+NOT\s+EXISTS)?\s+"
    r"(?:[`\"\[]?[^\s`\"\]]+[`\"\]]?)\s+"
    r"(BEFORE|AFTER|INSTEAD\s+OF)\s+"
    r"(INSERT|DELETE|UPDATE)(?:\s+OF\s+(.+?))?\s+ON\s+"
    r"([`\"\[]?[^\s`\"\]]+[`\"\]]?)\s*"
    r"(?:(WHEN)\s+.+?\s+)?BEGIN\s+(.+)\s+END\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _unquote_identifier(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and (value[0], value[-1]) in {("\"", "\""), ("`", "`"), ("[", "]")}:
        return value[1:-1]
    return value


def _trigger_definition(sql: str | None) -> dict[str, Any] | None:
    if not sql:
        return None
    match = _TRIGGER_HEADER_RE.match(sql)
    if match is None:
        return None
    timing, event, update_of, table, when_token, body = match.groups()
    columns = ()
    if update_of:
        columns = tuple(_unquote_identifier(value) for value in update_of.split(","))
    body_compact = re.sub(r"\s+", " ", body.strip()).rstrip(";").strip()
    body_match = re.fullmatch(
        r"UPDATE\s+archive_generations\s+SET\s+generation\s*=\s*generation\s*\+\s*1\s+"
        r"WHERE\s+name\s*=\s*'([^']+)'",
        body_compact,
        re.IGNORECASE,
    )
    display_match = re.fullmatch(
        r"INSERT\s+INTO\s+archive_generations\s*\(\s*name\s*,\s*generation\s*\)\s*"
        r"VALUES\s*\(\s*'display:'\s*\|\|\s*NEW\.rowid\s*,\s*1\s*\)\s*"
        r"ON\s+CONFLICT\s*\(\s*name\s*\)\s+DO\s+UPDATE\s+SET\s+"
        r"generation\s*=\s*generation\s*\+\s*1",
        body_compact,
        re.IGNORECASE,
    )
    return {
        "table": _unquote_identifier(table),
        "timing": re.sub(r"\s+", " ", timing.upper()),
        "event": event.upper(),
        "update_of": columns,
        "when": None if when_token is None else "present",
        "generation_name": body_match.group(1) if body_match else ("display" if display_match else None),
        "body_semantics": (
            "increment_by_one" if body_match else
            "row_revision_upsert" if display_match else
            "invalid"
        ),
    }


def _strict_nonnegative_integer(value: Any, sqlite_type: str) -> bool:
    return sqlite_type == "integer" and parse_nonnegative_integer(value) is not None


def generation_schema_contract_is_current(conn: sqlite3.Connection) -> bool:
    """Return whether generation counters are maintained by canonical DDL.

    This intentionally checks only the derived-index trust boundary.  Full
    compatibility remains the responsibility of ``database_schema_status``;
    search capability probes call this lightweight check once per schema
    version so they do not multiply unrelated table PRAGMAs.
    """

    if _database_user_version(conn) != DATABASE_SCHEMA_VERSION:
        return False
    table = conn.execute(
        "SELECT type FROM sqlite_schema WHERE name = 'archive_generations'"
    ).fetchone()
    if table is None or str(table[0]) != "table":
        return False
    xinfo = list(conn.execute('PRAGMA table_xinfo("archive_generations")'))
    pk = tuple(
        str(row[1]) for row in sorted((row for row in xinfo if int(row[5]) > 0), key=lambda row: int(row[5]))
    )
    columns = {str(row[1]): row for row in xinfo}
    generation = columns.get("generation")
    if (
        pk != ("name",)
        or generation is None
        or str(generation[2]).strip().upper() != "INTEGER"
        or not bool(generation[3])
        or _normalize_default(generation[4]) != "0"
    ):
        return False
    rows = conn.execute(
        "SELECT name, type, sql FROM sqlite_schema WHERE name IN ({})".format(
            ",".join("?" for _ in GENERATION_TRIGGER_CONTRACT)
        ),
        tuple(GENERATION_TRIGGER_CONTRACT),
    ).fetchall()
    found = {str(row[0]): (str(row[1]), row[2]) for row in rows}
    for name, expected_tuple in GENERATION_TRIGGER_CONTRACT.items():
        item = found.get(name)
        if item is None or item[0] != "trigger":
            return False
        is_display = expected_tuple[5] == "display"
        expected = {
            "table": expected_tuple[0],
            "timing": expected_tuple[1],
            "event": expected_tuple[2],
            "update_of": expected_tuple[3],
            "when": expected_tuple[4],
            "generation_name": expected_tuple[5],
            "body_semantics": "row_revision_upsert" if is_display else "increment_by_one",
        }
        if _trigger_definition(item[1]) != expected:
            return False
    return True


def database_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Inspect the read-only runtime schema contract without executing DDL."""

    current_version = _database_user_version(conn)
    rows = conn.execute(
        "SELECT name, type, tbl_name, sql FROM sqlite_schema "
        "WHERE type IN ('table', 'view', 'trigger', 'index')"
    ).fetchall()
    objects = {
        str(row[0]): {"type": str(row[1]), "table": str(row[2]), "sql": row[3]}
        for row in rows
    }
    tables = {name for name, item in objects.items() if item["type"] == "table"}
    object_type_mismatches: dict[str, dict[str, str]] = {}
    for name in CORE_SCHEMA_TABLES:
        item = objects.get(name)
        if item is not None and item["type"] != "table":
            object_type_mismatches[name] = {"expected": "table", "actual": str(item["type"])}
    for name in set(REQUIRED_INDEX_CONTRACT) | set(GENERATION_TRIGGER_CONTRACT):
        item = objects.get(name)
        expected_type = "index" if name in REQUIRED_INDEX_CONTRACT else "trigger"
        if item is not None and item["type"] != expected_type:
            object_type_mismatches[name] = {"expected": expected_type, "actual": str(item["type"])}

    missing_tables = sorted(CORE_SCHEMA_TABLES - tables)
    missing_columns: dict[str, list[str]] = {}
    invalid_tables: dict[str, dict[str, Any]] = {}
    for table, required_columns in CORE_SCHEMA_COLUMNS.items():
        if table not in tables:
            continue
        try:
            xinfo = list(conn.execute(f'PRAGMA table_xinfo("{table}")'))
            columns = {str(row[1]) for row in xinfo}
        except sqlite3.Error:
            missing_columns[table] = sorted(required_columns)
            continue
        missing = sorted(required_columns - columns)
        if missing:
            missing_columns[table] = missing

        contract = CANONICAL_TABLE_CONTRACT[table]
        actual_pk = tuple(
            str(row[1]) for row in sorted((row for row in xinfo if int(row[5]) > 0), key=lambda row: int(row[5]))
        )
        expected_pk = tuple(contract.get("primary_key", ()))
        table_errors: dict[str, Any] = {}
        if actual_pk != expected_pk:
            table_errors["primary_key"] = {"expected": list(expected_pk), "actual": list(actual_pk)}
        by_name = {str(row[1]): row for row in xinfo}
        invalid_columns: dict[str, dict[str, Any]] = {}
        for column, (expected_type, expected_not_null, expected_default) in contract.get("columns", {}).items():
            row = by_name.get(column)
            if row is None:
                continue
            actual = {
                "type": str(row[2]).strip().upper(),
                "not_null": bool(row[3]),
                "default": _normalize_default(row[4]),
            }
            expected = {
                "type": expected_type,
                "not_null": expected_not_null,
                "default": expected_default,
            }
            if actual != expected:
                invalid_columns[column] = {"expected": expected, "actual": actual}
        if invalid_columns:
            table_errors["columns"] = invalid_columns
        expected_unique = set(contract.get("unique", ()))
        missing_unique = sorted(
            expected_unique - _table_unique_keys(conn, table)
            if expected_unique
            else ()
        )
        if missing_unique:
            table_errors["missing_unique"] = [list(value) for value in missing_unique]
        if table_errors:
            invalid_tables[table] = table_errors

    missing_indexes: list[str] = []
    invalid_indexes: dict[str, dict[str, Any]] = {}
    for name, expected in REQUIRED_INDEX_CONTRACT.items():
        item = objects.get(name)
        if item is None:
            missing_indexes.append(name)
            continue
        if item["type"] != "index":
            continue
        index_row = next(
            (row for row in conn.execute(f'PRAGMA index_list("{expected["table"]}")') if str(row[1]) == name),
            None,
        )
        actual = {
            "table": item["table"],
            "unique": bool(index_row[2]) if index_row is not None else False,
            "origin": str(index_row[3]) if index_row is not None else "missing",
            "partial": bool(index_row[4]) if index_row is not None else False,
            "keys": _index_keys(conn, name),
            "where": None,
        }
        sql = str(item["sql"] or "")
        where_match = re.search(r"\bWHERE\b(.+)$", sql, re.IGNORECASE | re.DOTALL)
        if where_match:
            actual["where"] = re.sub(r"\s+", " ", where_match.group(1).strip()).rstrip(";")
        if actual != expected:
            invalid_indexes[name] = {
                "expected": {**expected, "keys": [list(value) for value in expected["keys"]]},
                "actual": {**actual, "keys": [list(value) for value in actual["keys"]]},
            }

    missing_triggers: list[str] = []
    invalid_triggers: dict[str, dict[str, Any]] = {}
    for name, expected_tuple in GENERATION_TRIGGER_CONTRACT.items():
        item = objects.get(name)
        if item is None:
            missing_triggers.append(name)
            continue
        if item["type"] != "trigger":
            continue
        is_display = expected_tuple[5] == "display"
        expected = {
            "table": expected_tuple[0],
            "timing": expected_tuple[1],
            "event": expected_tuple[2],
            "update_of": expected_tuple[3],
            "when": expected_tuple[4],
            "generation_name": expected_tuple[5],
            "body_semantics": "row_revision_upsert" if is_display else "increment_by_one",
        }
        actual = _trigger_definition(item["sql"])
        if actual != expected:
            invalid_triggers[name] = {
                "expected": {**expected, "update_of": list(expected["update_of"])},
                "actual": None if actual is None else {**actual, "update_of": list(actual["update_of"])},
            }

    generation_rows: set[str] = set()
    invalid_generation_rows: dict[str, dict[str, str]] = {}
    if "archive_generations" in tables and "archive_generations" not in missing_columns:
        try:
            for row in conn.execute(
                "SELECT name, generation, typeof(generation) FROM archive_generations"
            ):
                name = str(row[0])
                generation_rows.add(name)
                if name in REQUIRED_GENERATION_ROWS and not _strict_nonnegative_integer(row[1], str(row[2])):
                    invalid_generation_rows[name] = {
                        "expected": "nonnegative_integer",
                        "actual_type": str(row[2]),
                    }
        except sqlite3.Error:
            generation_rows = set()
    missing_generation_rows = sorted(set(REQUIRED_GENERATION_ROWS) - generation_rows)

    missing_foreign_keys: dict[str, list[dict[str, str]]] = {}
    for table, required in REQUIRED_FOREIGN_KEYS.items():
        if table not in tables:
            continue
        actual = {
            (str(row[3]), str(row[2]), str(row[4]), str(row[6]).upper(), str(row[5]).upper())
            for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        missing = required - actual
        if missing:
            missing_foreign_keys[table] = [
                {
                    "column": column,
                    "parent_table": parent,
                    "parent_column": parent_column,
                    "on_delete": on_delete,
                    "on_update": on_update,
                }
                for column, parent, parent_column, on_delete, on_update in sorted(missing)
            ]

    base_missing_tables = sorted((set(CORE_SCHEMA_COLUMNS) - {"archive_generations"}) - tables)
    base_missing_columns = {
        table: columns for table, columns in missing_columns.items() if table != "archive_generations"
    }
    base_object_mismatches = {
        name: detail
        for name, detail in object_type_mismatches.items()
        if name in CORE_SCHEMA_TABLES or name in REQUIRED_INDEX_CONTRACT or name in GENERATION_TRIGGER_CONTRACT
    }
    base_invalid_tables = {
        table: detail for table, detail in invalid_tables.items() if table != "archive_generations"
    }
    base_compatible = not (
        base_missing_tables
        or base_missing_columns
        or base_invalid_tables
        or missing_foreign_keys
        or base_object_mismatches
        or invalid_generation_rows
        or "archive_generations" in missing_columns
        or ("archive_generations" in tables and "archive_generations" in invalid_tables)
    )
    legacy_identity_compatible = bool(
        current_version in _SUPPORTED_SCHEMA_PREDECESSORS
        and not base_missing_tables
        and not base_missing_columns
        and not missing_foreign_keys
        and not base_object_mismatches
        and not invalid_generation_rows
        and _nullable_identity_only(invalid_tables.get("conversations"), "conversation_id")
        and (
            "archive_generations" not in tables
            or _nullable_identity_only(invalid_tables.get("archive_generations"), "name")
        )
        and not (set(invalid_tables) - {"conversations", "archive_generations"})
    )
    migration_base_compatible = base_compatible or legacy_identity_compatible
    managed_missing = bool(
        "archive_generations" in missing_tables
        or missing_generation_rows
        or missing_triggers
        or invalid_triggers
        or missing_indexes
        or invalid_indexes
    )
    version_migration_required = current_version < DATABASE_SCHEMA_VERSION
    schema_newer = current_version > DATABASE_SCHEMA_VERSION
    migration_required = bool(
        migration_base_compatible
        and not schema_newer
        and (version_migration_required or managed_missing)
    )
    schema_compatible = bool(base_compatible and not schema_newer and not migration_required)
    return {
        "ok": schema_compatible,
        "schema_compatible": schema_compatible,
        "base_schema_compatible": migration_base_compatible,
        "migration_required": migration_required,
        "current_database_schema_version": current_version,
        "required_database_schema_version": DATABASE_SCHEMA_VERSION,
        "database_schema_newer": schema_newer,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "invalid_tables": invalid_tables,
        "object_type_mismatches": object_type_mismatches,
        "missing_indexes": sorted(missing_indexes),
        "invalid_indexes": invalid_indexes,
        "missing_triggers": sorted(missing_triggers),
        "invalid_triggers": invalid_triggers,
        "missing_generation_table": "archive_generations" in missing_tables,
        "missing_generation_rows": missing_generation_rows,
        "invalid_generation_rows": invalid_generation_rows,
        "missing_foreign_keys": missing_foreign_keys,
        "foreign_keys_enabled": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
        "message_fts_available": "message_fts" in tables,
    }


def check_core_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    status = database_schema_status(conn)
    return {
        "schema_ok": status["ok"],
        **status,
    }


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


def _integrity_failure_is_message_fts_only(lines: list[str]) -> bool:
    """Return True when all integrity failures name only rebuildable message_fts objects."""

    if not lines:
        return False
    allowed = {"message_fts", *(f"message_fts{suffix}" for suffix in _WEB_INDEX_SHADOW_SUFFIXES)}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "ok":
            continue
        names = {
            match.group(1)
            for match in re.finditer(
                r"\b(?:table|index(?!\s+for\b))\s+(?:main\.)?([A-Za-z_][A-Za-z0-9_]*)\b",
                stripped,
            )
        }
        for name in allowed:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", stripped):
                names.add(name)
        if not names or not names.issubset(allowed):
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
    message_fts_available = bool(schema.get("message_fts_available"))
    fts5_rebuildable = detect_fts5_runtime(conn)
    integrity_lines = _run_integrity_check(conn)
    if len(integrity_lines) == 1 and integrity_lines[0] == "ok":
        integrity = "ok"
    else:
        integrity = "\n".join(integrity_lines)
    if not schema["schema_ok"]:
        return {
            **schema,
            "schema_ok": False,
            "database_error_code": database_schema_error_code(schema),
            "latest_import_run_id": None,
            "latest_run_warnings": 0,
            "total_warnings": 0,
            "missing_current_node": 0,
            "broken_parent_links": 0,
            "conversations_with_zero_nodes": 0,
            "parent_cycles": 0,
            "parent_cycle_nodes": 0,
            "parent_cycle_components": 0,
            "foreign_key_violations": 0,
            "foreign_key_violations_exact": False,
            "foreign_key_check_complete": False,
            "foreign_key_violation_sample_limit": 20,
            "foreign_key_violations_by_table": [],
            "foreign_key_violation_samples": [],
            "non_finite_timestamps": 0,
            "effective_current_diagnostics": {},
            "effective_current_checked": False,
            "effective_current_exact": False,
            "diagnostics_partial": True,
            "resource_limit_code": None,
            "integrity_check": integrity,
            "optional_web_index_error": False,
            "optional_web_index_recovery_hint": "",
            "message_fts_available": bool(message_fts_available and fts5_rebuildable),
            "message_fts_error": (
                None if message_fts_available and fts5_rebuildable
                else "missing" if fts5_rebuildable
                else "capability_unavailable"
            ),
            "message_fts_rebuildable": fts5_rebuildable,
            "optional_message_fts_error": False,
            "optional_message_fts_recovery_hint": "",
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
    cycle_diagnostics = parent_cycle_diagnostics(conn)
    cycle_nodes = cycle_diagnostics["parent_cycle_nodes"]
    cycle_components = cycle_diagnostics["parent_cycle_components"]
    foreign_keys = foreign_key_diagnostics(conn)
    non_finite_timestamps = 0
    for table in ("conversations", "conversation_nodes"):
        for row in conn.execute(f"SELECT create_time, update_time FROM {table}"):
            for value in row:
                if isinstance(value, float) and not math.isfinite(value):
                    non_finite_timestamps += 1
    effective_current_checked = True
    effective_current_exact = True
    diagnostics_partial = False
    resource_limit_code = None
    try:
        effective_diagnostics = _effective_current_diagnostics(conn)
    except EffectiveCurrentResourceLimitError as exc:
        effective_diagnostics = {}
        effective_current_checked = False
        effective_current_exact = False
        diagnostics_partial = True
        resource_limit_code = exc.code
    optional_web_index_error = False
    optional_web_index_recovery_hint = ""
    optional_message_fts_error = False
    optional_message_fts_recovery_hint = ""
    if integrity != "ok":
        optional_web_index_error = _integrity_failure_is_web_index_only(integrity_lines)
        if optional_web_index_error:
            optional_web_index_recovery_hint = "run `web-index` to rebuild optional web search indexes"
        optional_message_fts_error = _integrity_failure_is_message_fts_only(integrity_lines)
        if optional_message_fts_error:
            optional_message_fts_recovery_hint = "re-import with `--rebuild-fts` to rebuild optional message_fts"
    return {
        **schema,
        "schema_ok": True,
        "database_error_code": (
            "database_foreign_key_violation"
            if foreign_keys["foreign_key_violations"]
            else None
        ),
        "missing_tables": [],
        "latest_import_run_id": latest_run,
        "latest_run_warnings": latest_run_warnings,
        "total_warnings": total_warnings,
        "missing_current_node": missing_current,
        "broken_parent_links": broken_parent,
        "conversations_with_zero_nodes": zero_node,
        "parent_cycles": cycle_nodes,
        "parent_cycle_nodes": cycle_nodes,
        "parent_cycle_components": cycle_components,
        **foreign_keys,
        "non_finite_timestamps": non_finite_timestamps,
        "effective_current_diagnostics": effective_diagnostics,
        "effective_current_checked": effective_current_checked,
        "effective_current_exact": effective_current_exact,
        "diagnostics_partial": diagnostics_partial,
        "resource_limit_code": resource_limit_code,
        "integrity_check": integrity,
        "optional_web_index_error": optional_web_index_error,
        "optional_web_index_recovery_hint": optional_web_index_recovery_hint,
        "message_fts_available": bool(message_fts_available and fts5_rebuildable and not optional_message_fts_error),
        "message_fts_error": (
            "damaged" if optional_message_fts_error
            else None if message_fts_available and fts5_rebuildable
            else "missing" if fts5_rebuildable
            else "capability_unavailable"
        ),
        "message_fts_rebuildable": fts5_rebuildable,
        "optional_message_fts_error": optional_message_fts_error,
        "optional_message_fts_recovery_hint": optional_message_fts_recovery_hint,
        "warnings_by_type": warning_counts,
        "latest_warnings_by_type": latest_warning_counts,
        "ok": (
            not diagnostics_partial
            and missing_current == 0
            and broken_parent == 0
            and zero_node == 0
            and cycle_nodes == 0
            and foreign_keys["foreign_key_violations"] == 0
            and non_finite_timestamps == 0
            and integrity == "ok"
        ),
    }


def foreign_key_diagnostics(conn: sqlite3.Connection, *, sample_limit: int = 20) -> dict[str, Any]:
    """Stream a complete, exact FK check while retaining only bounded samples."""

    sample_limit = max(0, int(sample_limit))
    counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    total = 0
    for row in conn.execute("PRAGMA foreign_key_check"):
        total += 1
        table = str(row[0])
        counts[table] = counts.get(table, 0) + 1
        if len(samples) < sample_limit:
            samples.append(
                {
                    "table": table,
                    "rowid": row[1],
                    "parent_table": str(row[2]),
                    "constraint_index": int(row[3]),
                }
            )
    return {
        "foreign_key_violations": total,
        "foreign_key_violations_exact": True,
        "foreign_key_check_complete": True,
        "foreign_key_violation_sample_limit": sample_limit,
        "foreign_key_violations_by_table": [
            {"table": table, "count": count}
            for table, count in sorted(counts.items())
        ],
        "foreign_key_violation_samples": samples,
    }


def _effective_current_diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    conversation_rows = conn.execute(
        "SELECT conversation_id, current_node FROM conversations ORDER BY conversation_id"
    ).fetchall()
    conversation_ids = [str(row["conversation_id"]) for row in conversation_rows]
    ensure_effective_current_views(conn, None)
    metadata = effective_current_metadata(conn, conversation_ids)
    current_chains_with_unflagged_nodes = {
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT ec.conversation_id
               FROM effective_current_nodes ec
               JOIN effective_current_meta em
                 ON em.conversation_id = ec.conversation_id
               JOIN conversation_nodes n
                 ON n.conversation_id = ec.conversation_id AND n.node_id = ec.node_id
               WHERE em.current_collection_source = 'current_node'
                 AND n.is_on_current_path = 0"""
        )
    }
    counts = {
        "selected_current_node": 0,
        "selected_raw_flags": 0,
        "selected_fallback_all": 0,
        "valid_current_node_zero_flags": 0,
        "flags_missing_current_chain_nodes": 0,
        "multiple_flag_leaves": 0,
        "invalid_current_node_flags_used": 0,
        "cycle_detected": 0,
        "selected_chain_cycles": 0,
        "raw_flag_cycles": 0,
        "missing_parent_in_selected_chain": 0,
        "cross_conversation_parent_in_selected_chain": 0,
        "partial_selected_chain": 0,
        "missing_parent_in_raw_flag_topology": 0,
        "cross_conversation_parent_in_raw_flag_topology": 0,
        "partial_raw_flag_topology": 0,
    }
    for conversation in conversation_rows:
        conversation_id = str(conversation["conversation_id"])
        item = metadata.get(conversation_id, {})
        source = str(item.get("current_collection_source", "fallback_all"))
        counts[f"selected_{source}"] += 1
        if source == "current_node" and int(item.get("current_path_nodes") or 0) == 0:
            counts["valid_current_node_zero_flags"] += 1
        if conversation_id in current_chains_with_unflagged_nodes:
            counts["flags_missing_current_chain_nodes"] += 1
        if int(item.get("raw_flag_leaf_count") or 0) > 1:
            counts["multiple_flag_leaves"] += 1
        if source == "raw_flags" and conversation["current_node"]:
            counts["invalid_current_node_flags_used"] += 1
        if item.get("cycle_detected"):
            counts["cycle_detected"] += 1
        if item.get("selected_chain_cycle_detected"):
            counts["selected_chain_cycles"] += 1
        if item.get("raw_flag_cycle_detected"):
            counts["raw_flag_cycles"] += 1
        selected_missing = bool(item.get("selected_chain_missing_parent"))
        selected_cross = bool(item.get("selected_chain_cross_conversation_parent"))
        selected_cycle = bool(item.get("selected_chain_cycle_detected"))
        raw_missing = bool(item.get("raw_flag_missing_parent"))
        raw_cross = bool(item.get("raw_flag_cross_conversation_parent"))
        raw_cycle = bool(item.get("raw_flag_cycle_detected"))
        if selected_missing:
            counts["missing_parent_in_selected_chain"] += 1
        if selected_cross:
            counts["cross_conversation_parent_in_selected_chain"] += 1
        if selected_cycle or selected_missing or selected_cross:
            counts["partial_selected_chain"] += 1
        if raw_missing:
            counts["missing_parent_in_raw_flag_topology"] += 1
        if raw_cross:
            counts["cross_conversation_parent_in_raw_flag_topology"] += 1
        if raw_cycle or raw_missing or raw_cross:
            counts["partial_raw_flag_topology"] += 1
    return counts


def count_parent_cycles(conn: sqlite3.Connection) -> int:
    """Compatibility alias returning the number of nodes in parent cycles."""

    return parent_cycle_diagnostics(conn)["parent_cycle_nodes"]


def parent_cycle_diagnostics(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT conversation_id, node_id, parent_node_id FROM conversation_nodes WHERE parent_node_id IS NOT NULL"
    ).fetchall()
    parents = {(row["conversation_id"], row["node_id"]): row["parent_node_id"] for row in rows}
    cycle_nodes: set[tuple[str, str]] = set()
    cycle_components = 0
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
                cycle_components += 1
                break
            seen_at[current] = len(path)
            path.append(current)
            parent = parents.get(current)
            current = (current[0], parent) if parent is not None else None
        checked.update(path)
    return {
        "parent_cycle_nodes": len(cycle_nodes),
        "parent_cycle_components": cycle_components,
    }


def export_query(conn: sqlite3.Connection, start_ts: float | None, end_ts: float | None) -> list[sqlite3.Row]:
    where = []
    params: list[Any] = []
    if start_ts is not None:
        where.append("COALESCE(update_time, create_time, 0) >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("COALESCE(update_time, create_time, 0) < ?")
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
