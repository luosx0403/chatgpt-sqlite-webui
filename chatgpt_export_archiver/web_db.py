from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from .db import (
    CORE_SCHEMA_COLUMNS,
    DatabaseMigrationError,
    OPTIONAL_WEB_INDEX_OWNER,
    OPTIONAL_WEB_INDEX_OWNER_KEY,
    _drop_table_with_shadows,
    _read_database_identity,
    _raise_object_collision,
    _schema_rows_for_names,
    _table_xinfo_contract,
    _validate_fts5_family,
    configure_bulk_write_connection,
    database_schema_status,
    drop_optional_web_indexes,
    invalidate_read_capability_cache,
    register_archive_sql_functions,
    validate_optional_web_index_ownership,
)
from .parser import recover_message_display_text
from .schema_contract import (
    DISPLAY_TEXT_RESOLVER_VERSION,
    NORMALIZATION_INDEX_FORMAT_VERSION,
    OPTIONAL_WEB_INDEX_FORMAT_VERSION,
    parse_nonnegative_integer,
)
from .sqlite_errors import (
    is_fts5_capability_unavailable,
    is_optional_message_fts_damaged,
    is_optional_search_capability_missing,
    sqlite_open_error_code,
)
from .search import _derived_generation_is_current, invalidate_capability_cache, normalize_search_text

WEB_INDEX_FORMAT_VERSION = OPTIONAL_WEB_INDEX_FORMAT_VERSION
WEB_INDEX_BATCH_SIZE = 200
WEB_INDEX_PROGRESS_VM_STEPS = 10_000
WEB_INDEX_MAX_INPUT_BYTES = 4 * 1024 * 1024
WEB_INDEX_MAX_NORMALIZED_BYTES = 2 * 1024 * 1024
WEB_INDEX_MAX_DERIVED_BYTES = 8 * 1024 * 1024
WEB_INDEX_BATCH_INPUT_BYTES = 16 * 1024 * 1024
WEB_INDEX_BATCH_NORMALIZED_BYTES = 16 * 1024 * 1024
WEB_INDEX_BATCH_DERIVED_BYTES = 32 * 1024 * 1024
WEB_INDEX_FTS_BIND_BATCH_BYTES = 16 * 1024 * 1024
WEB_INDEX_BUILD_LEASE_SECONDS = 120.0
WEB_INDEX_BUILD_LEASE_TABLE = "web_index_lease"
WEB_INDEX_BUILD_NAME_PREFIX = "__chatgpt_webidx_"
WEB_INDEX_BUILD_STAGES = (
    "scan_normalize_messages",
    "normalize_titles",
    "build_message_trigram",
    "build_title_trigram",
    "write_metadata",
    "commit_swap",
)


class WebIndexBuildCancelled(ValueError):
    """A requested optional-index cancellation that leaves the old index current."""

    def __init__(self) -> None:
        super().__init__("web_index_cancelled")
        self.code = "web_index_cancelled"


class WebIndexBuildError(ValueError):
    """A sanitized optional-index publication failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_LEASE_TABLE_DDL = f"""CREATE TABLE {WEB_INDEX_BUILD_LEASE_TABLE}(
    slot INTEGER NOT NULL PRIMARY KEY CHECK(slot = 1),
    build_id TEXT NOT NULL UNIQUE,
    owner_token TEXT NOT NULL,
    database_identity TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    message_generation TEXT NOT NULL,
    title_generation TEXT NOT NULL,
    format_version TEXT NOT NULL,
    created_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    phase TEXT NOT NULL,
    object_names_json TEXT NOT NULL
)"""

_LEASE_TABLE_XINFO = (
    ("slot", "INTEGER", True, None, 1, False),
    ("build_id", "TEXT", True, None, 0, False),
    ("owner_token", "TEXT", True, None, 0, False),
    ("database_identity", "TEXT", True, None, 0, False),
    ("schema_version", "INTEGER", True, None, 0, False),
    ("message_generation", "TEXT", True, None, 0, False),
    ("title_generation", "TEXT", True, None, 0, False),
    ("format_version", "TEXT", True, None, 0, False),
    ("created_at", "REAL", True, None, 0, False),
    ("heartbeat_at", "REAL", True, None, 0, False),
    ("lease_expires_at", "REAL", True, None, 0, False),
    ("phase", "TEXT", True, None, 0, False),
    ("object_names_json", "TEXT", True, None, 0, False),
)


def _database_lease_identity(conn: sqlite3.Connection) -> str:
    value = json.dumps(_read_database_identity(conn), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_lease_table(conn: sqlite3.Connection, *, create: bool) -> bool:
    rows = _schema_rows_for_names(conn, (WEB_INDEX_BUILD_LEASE_TABLE,))[
        WEB_INDEX_BUILD_LEASE_TABLE
    ]
    if not rows:
        if not create:
            return False
        conn.execute(_LEASE_TABLE_DDL)
        rows = _schema_rows_for_names(conn, (WEB_INDEX_BUILD_LEASE_TABLE,))[
            WEB_INDEX_BUILD_LEASE_TABLE
        ]
    if len(rows) != 1:
        _raise_object_collision("staging_name_collision", rows)
    row = rows[0]
    row_name = str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
    row_type = str(row["type"] if isinstance(row, sqlite3.Row) else row[0])
    row_table = str(row["tbl_name"] if isinstance(row, sqlite3.Row) else row[2])
    raw_sql = row["sql"] if isinstance(row, sqlite3.Row) else row[3]
    normalized_sql = " ".join(str(raw_sql or "").split()).casefold()
    expected_sql = " ".join(_LEASE_TABLE_DDL.split()).casefold()
    if (
        row_name != WEB_INDEX_BUILD_LEASE_TABLE
        or row_type != "table"
        or row_table != WEB_INDEX_BUILD_LEASE_TABLE
        or normalized_sql != expected_sql
        or _table_xinfo_contract(conn, WEB_INDEX_BUILD_LEASE_TABLE) != _LEASE_TABLE_XINFO
    ):
        _raise_object_collision("staging_name_collision", rows)
    unique_build_id = False
    for index_row in conn.execute(f'PRAGMA index_list("{WEB_INDEX_BUILD_LEASE_TABLE}")'):
        if bool(index_row[2]):
            columns = tuple(
                str(info[2])
                for info in conn.execute(f'PRAGMA index_info("{str(index_row[1])}")')
            )
            unique_build_id = unique_build_id or columns == ("build_id",)
    if not unique_build_id:
        _raise_object_collision("staging_name_collision", rows)
    return True


def _build_names(build_id: str) -> tuple[str, ...]:
    if len(build_id) != 32 or any(ch not in "0123456789abcdef" for ch in build_id):
        raise WebIndexBuildError("staging_name_collision")
    prefix = f"{WEB_INDEX_BUILD_NAME_PREFIX}{build_id}_"
    return tuple(prefix + suffix for suffix in ("mn", "tn", "meta", "over", "mt", "tt"))


def _decode_owned_names(value: Any, build_id: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise WebIndexBuildError("staging_name_collision") from None
    expected = _build_names(build_id)
    if not isinstance(decoded, list) or tuple(decoded) != expected:
        raise WebIndexBuildError("staging_name_collision")
    return expected


def _validate_staging_objects(
    conn: sqlite3.Connection,
    names: tuple[str, ...],
    *,
    allow_missing: bool,
    trigram_expected: bool | None = None,
) -> None:
    plain_contracts = (
        _LEASE_MESSAGE_XINFO,
        _LEASE_TITLE_XINFO,
        _LEASE_METADATA_XINFO,
        _LEASE_OVERSIZED_XINFO,
    )
    rows_by_name = _schema_rows_for_names(conn, names)
    for name, expected in zip(names[:4], plain_contracts):
        rows = rows_by_name[name]
        if not rows and allow_missing:
            continue
        if len(rows) != 1:
            _raise_object_collision("staging_name_collision", rows)
        row = rows[0]
        if (
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) != name
            or str(row["type"] if isinstance(row, sqlite3.Row) else row[0]) != "table"
            or str(row["tbl_name"] if isinstance(row, sqlite3.Row) else row[2]) != name
            or _table_xinfo_contract(conn, name) != expected
        ):
            _raise_object_collision("staging_name_collision", rows)
    for name, column in ((names[4], "content_text"), (names[5], "title")):
        family_rows = [
            row
            for candidate in (name,) + tuple(f"{name}{suffix}" for suffix in ("_data", "_idx", "_config", "_docsize"))
            for row in _schema_rows_for_names(conn, (candidate,))[candidate]
        ]
        if not family_rows and (allow_missing or trigram_expected is False):
            continue
        if family_rows and trigram_expected is False:
            _raise_object_collision("staging_name_collision", family_rows)
        _validate_fts5_family(
            conn,
            name,
            visible_columns=(column,),
            arguments=f"{column},content='',tokenize='trigram'",
            collision_code="staging_name_collision",
            allow_absent=False,
        )


_LEASE_MESSAGE_XINFO = (
    ("conversation_id", "TEXT", True, None, 1, False),
    ("node_id", "TEXT", True, None, 2, False),
    ("content_norm", "TEXT", True, None, 0, False),
)
_LEASE_TITLE_XINFO = (
    ("conversation_id", "TEXT", True, None, 1, False),
    ("title_norm", "TEXT", True, None, 0, False),
)
_LEASE_METADATA_XINFO = (
    ("key", "TEXT", True, None, 1, False),
    ("value", "TEXT", True, None, 0, False),
)
_LEASE_OVERSIZED_XINFO = (
    ("kind", "TEXT", True, None, 1, False),
    ("source_rowid", "INTEGER", True, None, 2, False),
    ("conversation_id", "TEXT", True, None, 0, False),
    ("node_id", "TEXT", False, None, 0, False),
    ("input_bytes", "INTEGER", True, None, 0, False),
    ("reason", "TEXT", True, None, 0, False),
)

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
    absolute = db_path.expanduser().absolute()
    uri = f"{absolute.as_uri()}?mode=ro"
    # TEMP effective-current tables perform DML.  Autocommit prevents that
    # connection-local work from pinning an old main-database read snapshot
    # across later requests/tests after another connection commits.
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
    except sqlite3.Error as exc:
        raise ValueError(sqlite_open_error_code(exc, path_exists=absolute.exists())) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    register_archive_sql_functions(conn)
    return conn


def connect_writable(db_path: Path) -> sqlite3.Connection:
    absolute = db_path.expanduser().absolute()
    uri = f"{absolute.as_uri()}?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:
        raise ValueError(sqlite_open_error_code(exc, path_exists=absolute.exists())) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    register_archive_sql_functions(conn)
    return conn


def check_schema(
    conn: sqlite3.Connection,
    *,
    schema_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = dict(schema_status) if schema_status is not None else database_schema_status(conn)
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
        "web_index_oversized": "web_index_oversized" in tables,
    }


def web_index_status(
    conn: sqlite3.Connection,
    *,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = check_schema(conn) if schema is None else schema
    metadata: dict[str, str] = {}
    if schema["web_index_metadata"]:
        columns = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in conn.execute('PRAGMA table_xinfo("web_index_metadata")')
        }
        if {"key", "value"}.issubset(columns):
            metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM web_index_metadata")}
    format_current = (
        schema["web_index_oversized"]
        and metadata.get("web_index_format_version") == WEB_INDEX_FORMAT_VERSION
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


def _canonical_generations(conn: sqlite3.Connection) -> dict[str, str]:
    rows = list(conn.execute(
        "SELECT name, generation, typeof(generation) AS generation_type FROM archive_generations"
    ))
    values: dict[str, str] = {}
    for row in rows:
        name = str(row["name"])
        parsed = parse_nonnegative_integer(row["generation"])
        if name in {"message", "title"} and row["generation_type"] == "integer" and parsed is not None:
            values[name] = str(parsed)
    if set(values) != {"message", "title"}:
        raise WebIndexBuildError("web_index_generation_invalid")
    return values


def _blob_is_generated_non_text_placeholder(
    conn: sqlite3.Connection,
    rowid: int,
    size: int,
) -> bool:
    """Recognize the generated placeholder grammar without a prefix guess."""

    prefixes = (b"[non-text content:", b"[non-text part:")
    if size < min(map(len, prefixes)) + 2:
        return False
    with conn.blobopen("conversation_nodes", "content_text", rowid, readonly=True) as blob:
        head = blob.read(max(map(len, prefixes)))
        prefix = next((item for item in prefixes if head.startswith(item)), None)
        if prefix is None:
            return False
        blob.seek(len(prefix))
        remaining = size - len(prefix) - 1
        payload_nonspace = False
        while remaining > 0:
            chunk = blob.read(min(64 * 1024, remaining))
            if not chunk:
                return False
            if b"]" in chunk or b"\n" in chunk or b"\r" in chunk:
                return False
            payload_nonspace = payload_nonspace or bool(chunk.strip())
            remaining -= len(chunk)
        return payload_nonspace and blob.read(1) == b"]"


def _drop_owned_staging_objects(conn: sqlite3.Connection, names: tuple[str, ...]) -> None:
    _validate_staging_objects(conn, names, allow_missing=True)
    for name in names[-2:]:
        failures = _drop_table_with_shadows(conn, name)
        if failures:
            raise WebIndexBuildError("web_index_cleanup_failed")
    for name in names[:4]:
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')


def _claim_web_index_build(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    owner_token: str,
    names: tuple[str, ...],
    generations: dict[str, str],
) -> str:
    """Claim the durable single-builder slot under an authoritative write lock."""

    conn.execute("BEGIN IMMEDIATE")
    _validate_lease_table(conn, create=True)
    validate_optional_web_index_ownership(conn)
    identity = _database_lease_identity(conn)
    now = time.time()
    row = conn.execute(
        f"SELECT * FROM {WEB_INDEX_BUILD_LEASE_TABLE} WHERE slot = 1"
    ).fetchone()
    if row is not None:
        existing_build_id = str(row["build_id"])
        existing_names = _decode_owned_names(row["object_names_json"], existing_build_id)
        if (
            str(row["database_identity"]) != identity
            or str(row["format_version"]) != WEB_INDEX_FORMAT_VERSION
            or int(row["schema_version"]) != int(conn.execute("PRAGMA user_version").fetchone()[0])
        ):
            raise WebIndexBuildError("staging_name_collision")
        if float(row["lease_expires_at"]) >= now:
            raise WebIndexBuildError("web_index_build_in_progress")
        _drop_owned_staging_objects(conn, existing_names)
        conn.execute(f"DELETE FROM {WEB_INDEX_BUILD_LEASE_TABLE} WHERE slot = 1")
    _validate_staging_objects(conn, names, allow_missing=True)
    collisions = [
        item
        for rows in _schema_rows_for_names(conn, names).values()
        for item in rows
    ]
    if collisions:
        _raise_object_collision("staging_name_collision", collisions)
    conn.execute(
        f"""INSERT INTO {WEB_INDEX_BUILD_LEASE_TABLE}(
                slot, build_id, owner_token, database_identity, schema_version,
                message_generation, title_generation, format_version,
                created_at, heartbeat_at, lease_expires_at, phase, object_names_json
            ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            build_id,
            owner_token,
            identity,
            int(conn.execute("PRAGMA user_version").fetchone()[0]),
            generations["message"],
            generations["title"],
            WEB_INDEX_FORMAT_VERSION,
            now,
            now,
            now + WEB_INDEX_BUILD_LEASE_SECONDS,
            "create_staging",
            json.dumps(names, ensure_ascii=True, separators=(",", ":")),
        ),
    )
    return identity


def _heartbeat_web_index_build(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    owner_token: str,
    phase: str,
) -> None:
    now = time.time()
    changed = conn.execute(
        f"""UPDATE {WEB_INDEX_BUILD_LEASE_TABLE}
            SET heartbeat_at = ?, lease_expires_at = ?, phase = ?
            WHERE slot = 1 AND build_id = ? AND owner_token = ?""",
        (now, now + WEB_INDEX_BUILD_LEASE_SECONDS, phase, build_id, owner_token),
    ).rowcount
    if changed != 1:
        raise WebIndexBuildError("web_index_build_lease_lost")


def assert_no_active_web_index_build(conn: sqlite3.Connection) -> None:
    """Refuse import/migration invalidation while another process owns staging."""

    if not _validate_lease_table(conn, create=False):
        return
    row = conn.execute(
        f"SELECT build_id, object_names_json, database_identity, lease_expires_at "
        f"FROM {WEB_INDEX_BUILD_LEASE_TABLE} WHERE slot = 1"
    ).fetchone()
    if row is None:
        return
    build_id = str(row["build_id"])
    _decode_owned_names(row["object_names_json"], build_id)
    if str(row["database_identity"]) != _database_lease_identity(conn):
        raise WebIndexBuildError("staging_name_collision")
    if float(row["lease_expires_at"]) >= time.time():
        raise WebIndexBuildError("web_index_build_in_progress")
    raise WebIndexBuildError("web_index_stale_build_recovery_required")


def _cleanup_web_index_staging(
    conn: sqlite3.Connection,
    names: tuple[str, ...],
    *,
    build_id: str,
    owner_token: str,
) -> None:
    """Clean only objects bound to this durable owner; never another build."""

    try:
        conn.execute("BEGIN IMMEDIATE")
        _validate_lease_table(conn, create=False)
        row = conn.execute(
            f"SELECT build_id, owner_token, object_names_json FROM {WEB_INDEX_BUILD_LEASE_TABLE} "
            "WHERE slot = 1"
        ).fetchone()
        if row is None:
            conn.rollback()
            return
        if str(row["build_id"]) != build_id or not secrets.compare_digest(
            str(row["owner_token"]), owner_token
        ):
            conn.rollback()
            return
        if _decode_owned_names(row["object_names_json"], build_id) != names:
            raise WebIndexBuildError("staging_name_collision")
        _drop_owned_staging_objects(conn, names)
        conn.execute(
            f"DELETE FROM {WEB_INDEX_BUILD_LEASE_TABLE} "
            "WHERE slot = 1 AND build_id = ? AND owner_token = ?",
            (build_id, owner_token),
        )
        conn.commit()
    except (sqlite3.Error, WebIndexBuildError, DatabaseMigrationError):
        if conn.in_transaction:
            conn.rollback()


def create_web_indexes(
    db_path: Path,
    *,
    batch_size: int = WEB_INDEX_BATCH_SIZE,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build bounded staging indexes, then publish in a short transaction.

    Canonical generations are captured before staging and rechecked while the
    final write lock is held.  The old optional index remains readable until
    the atomic rename transaction commits.
    """

    batch_size = max(1, min(1_000, int(batch_size)))
    conn = connect_writable(db_path)
    build_id = secrets.token_hex(16)
    owner_token = secrets.token_hex(32)
    build_names = _build_names(build_id)
    (
        message_norm_build,
        title_norm_build,
        metadata_build,
        oversized_build,
        message_trigram_build,
        title_trigram_build,
    ) = build_names
    cancelled = False
    resource_progress = {
        "input_materialized_bytes": 0,
        "normalized_materialized_bytes": 0,
        "current_batch_input_bytes": 0,
        "current_batch_normalized_bytes": 0,
        "current_batch_derived_bytes": 0,
        "peak_batch_input_bytes": 0,
        "peak_batch_normalized_bytes": 0,
        "peak_batch_derived_bytes": 0,
        "oversized_rows": 0,
    }

    def check_cancel() -> None:
        nonlocal cancelled
        if cancel_check is not None and cancel_check():
            cancelled = True
            raise WebIndexBuildCancelled()

    def progress_handler() -> int:
        nonlocal cancelled
        if cancel_check is not None and cancel_check():
            cancelled = True
            return 1
        return 0

    def report(stage: str, processed: int, total: int) -> None:
        if progress_callback is not None:
            progress_callback(stage, {
                "build_stage": stage,
                "processed": processed,
                "processed_rows": processed,
                "total": total,
                "complete": processed >= total,
                "batch_size": batch_size,
                "processed_input_bytes": resource_progress["input_materialized_bytes"],
                "processed_normalized_bytes": resource_progress["normalized_materialized_bytes"],
                "cancel_requested": cancelled,
                **resource_progress,
            })
        check_cancel()

    def commit_phase(phase: str) -> None:
        _heartbeat_web_index_build(
            conn,
            build_id=build_id,
            owner_token=owner_token,
            phase=phase,
        )
        conn.commit()

    try:
        schema = check_schema(conn)
        if not schema["ok"]:
            code = "database_migration_required" if schema["migration_required"] else "database_schema_incompatible"
            raise DatabaseMigrationError(code, detail=schema)
        configure_bulk_write_connection(conn)
        trigram_available = detect_trigram(conn)
        conn.set_progress_handler(progress_handler, WEB_INDEX_PROGRESS_VM_STEPS)
        starting_generations = _canonical_generations(conn)
        database_identity = _claim_web_index_build(
            conn,
            build_id=build_id,
            owner_token=owner_token,
            names=build_names,
            generations=starting_generations,
        )
        message_total = int(conn.execute("SELECT COUNT(*) AS c FROM conversation_nodes").fetchone()["c"])
        title_total = int(conn.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()["c"])
        conn.execute(
            f"""CREATE TABLE {message_norm_build}(
                   conversation_id TEXT NOT NULL,
                   node_id TEXT NOT NULL,
                   content_norm TEXT NOT NULL,
                   PRIMARY KEY(conversation_id, node_id)
               )"""
        )
        conn.execute(
            f"""CREATE TABLE {title_norm_build}(
                   conversation_id TEXT NOT NULL PRIMARY KEY,
                   title_norm TEXT NOT NULL
               )"""
        )
        conn.execute(
            f"""CREATE TABLE {metadata_build}(
                   key TEXT NOT NULL PRIMARY KEY,
                   value TEXT NOT NULL
               )"""
        )
        conn.execute(
            f"""CREATE TABLE {oversized_build}(
                   kind TEXT NOT NULL,
                   source_rowid INTEGER NOT NULL,
                   conversation_id TEXT NOT NULL,
                   node_id TEXT,
                   input_bytes INTEGER NOT NULL,
                   reason TEXT NOT NULL,
                   PRIMARY KEY(kind, source_rowid)
               )"""
        )
        commit_phase("scan_normalize_messages")

        report("scan_normalize_messages", 0, message_total)
        last_rowid = 0
        message_processed = 0
        indexed_messages = 0
        oversized_messages = 0
        row_limit = batch_size
        message_materialized_bytes = 0
        normalized_materialized_bytes = 0
        while True:
            rows = conn.execute(
                """SELECT rowid, conversation_id, node_id, content_type,
                          content_text IS NULL AS content_is_null,
                          raw_message_json IS NULL AS raw_is_null
                   FROM conversation_nodes
                   WHERE rowid > ?
                   ORDER BY rowid
                   LIMIT ?""",
                (last_rowid, row_limit),
            ).fetchall()
            if not rows:
                break
            normalized_rows: list[tuple[str, str, str]] = []
            batch_materialized_bytes = 0
            batch_normalized_bytes = 0
            batch_derived_bytes = 0
            processed_in_batch = 0
            for row in rows:
                check_cancel()
                rowid = int(row["rowid"])
                content_size = 0
                content_prefix_bytes = b""
                if not row["content_is_null"]:
                    with conn.blobopen("conversation_nodes", "content_text", rowid, readonly=True) as blob:
                        content_size = len(blob)
                        content_prefix_bytes = blob.read(min(len(blob), 256))
                content_prefix = content_prefix_bytes.decode("utf-8", errors="replace")
                marker_prefix = _blob_is_generated_non_text_placeholder(
                    conn, rowid, content_size
                )
                canonical_usable = bool(content_prefix) and not (
                    marker_prefix
                    and str(row["content_type"] or "").casefold()
                    not in {"text", "code", "multimodal_text"}
                    and not row["raw_is_null"]
                )
                chosen_size = content_size
                raw_size = 0
                if not canonical_usable and not row["raw_is_null"]:
                    with conn.blobopen("conversation_nodes", "raw_message_json", rowid, readonly=True) as blob:
                        raw_size = len(blob)
                    chosen_size = raw_size
                input_bytes = chosen_size if canonical_usable else content_size + raw_size
                if input_bytes > WEB_INDEX_MAX_INPUT_BYTES:
                    conn.execute(
                        f"INSERT INTO {oversized_build} VALUES ('message', ?, ?, ?, ?, 'input_bytes')",
                        (rowid, row["conversation_id"], row["node_id"], input_bytes),
                    )
                    oversized_messages += 1
                    last_rowid = rowid
                    processed_in_batch += 1
                    continue
                if processed_in_batch and batch_materialized_bytes + input_bytes > WEB_INDEX_BATCH_INPUT_BYTES:
                    break
                if canonical_usable:
                    with conn.blobopen("conversation_nodes", "content_text", rowid, readonly=True) as blob:
                        content_value = blob.read().decode("utf-8", errors="replace")
                    raw_value = None
                    actual_bytes = len(content_value.encode("utf-8"))
                else:
                    with conn.blobopen("conversation_nodes", "content_text", rowid, readonly=True) as blob:
                        content_value = blob.read().decode("utf-8", errors="replace")
                    if raw_size:
                        with conn.blobopen("conversation_nodes", "raw_message_json", rowid, readonly=True) as blob:
                            raw_value = blob.read().decode("utf-8", errors="replace")
                    else:
                        raw_value = None
                    actual_bytes = len(content_value.encode("utf-8")) + (
                        len(raw_value.encode("utf-8")) if raw_value else 0
                    )
                display_text = recover_message_display_text(content_value, raw_value)
                content_norm = normalize_search_text(display_text)
                if content_norm:
                    normalized_bytes = len(content_norm.encode("utf-8"))
                    derived_bytes = normalized_bytes * 4
                    if (
                        normalized_bytes > WEB_INDEX_MAX_NORMALIZED_BYTES
                        or derived_bytes > WEB_INDEX_MAX_DERIVED_BYTES
                    ):
                        batch_materialized_bytes += actual_bytes
                        message_materialized_bytes += actual_bytes
                        batch_normalized_bytes += normalized_bytes
                        normalized_materialized_bytes += normalized_bytes
                        conn.execute(
                            f"INSERT INTO {oversized_build} VALUES ('message', ?, ?, ?, ?, 'derived_bytes')",
                            (rowid, row["conversation_id"], row["node_id"], actual_bytes),
                        )
                        oversized_messages += 1
                        last_rowid = rowid
                        processed_in_batch += 1
                        continue
                    if processed_in_batch and (
                        batch_normalized_bytes + normalized_bytes
                        > WEB_INDEX_BATCH_NORMALIZED_BYTES
                        or batch_derived_bytes + derived_bytes
                        > WEB_INDEX_BATCH_DERIVED_BYTES
                    ):
                        # The current row is retried as the first row of the
                        # next keyset batch; it is never appended to this
                        # batch's Python bind collection.
                        break
                    batch_normalized_bytes += normalized_bytes
                    batch_derived_bytes += derived_bytes
                    normalized_materialized_bytes += normalized_bytes
                    normalized_rows.append((row["conversation_id"], row["node_id"], content_norm))
                batch_materialized_bytes += actual_bytes
                message_materialized_bytes += actual_bytes
                last_rowid = rowid
                processed_in_batch += 1
            if normalized_rows:
                conn.executemany(
                    f"INSERT INTO {message_norm_build}(conversation_id, node_id, content_norm) VALUES (?, ?, ?)",
                    normalized_rows,
                )
                indexed_messages += len(normalized_rows)
            message_processed += processed_in_batch
            resource_progress.update(
                input_materialized_bytes=message_materialized_bytes,
                normalized_materialized_bytes=normalized_materialized_bytes,
                current_batch_input_bytes=batch_materialized_bytes,
                current_batch_normalized_bytes=batch_normalized_bytes,
                current_batch_derived_bytes=batch_derived_bytes,
                oversized_rows=oversized_messages,
            )
            resource_progress["peak_batch_input_bytes"] = max(
                resource_progress["peak_batch_input_bytes"], batch_materialized_bytes
            )
            resource_progress["peak_batch_normalized_bytes"] = max(
                resource_progress["peak_batch_normalized_bytes"], batch_normalized_bytes
            )
            resource_progress["peak_batch_derived_bytes"] = max(
                resource_progress["peak_batch_derived_bytes"], batch_derived_bytes
            )
            commit_phase("scan_normalize_messages")
            report("scan_normalize_messages", message_processed, message_total)

        report("normalize_titles", 0, title_total)
        last_rowid = 0
        title_processed = 0
        oversized_titles = 0
        title_materialized_bytes = 0
        title_normalized_materialized_bytes = 0
        while True:
            rows = conn.execute(
                """SELECT rowid, conversation_id, title IS NULL AS title_is_null
                   FROM conversations
                   WHERE rowid > ?
                   ORDER BY rowid
                   LIMIT ?""",
                (last_rowid, row_limit),
            ).fetchall()
            if not rows:
                break
            title_rows: list[tuple[str, str]] = []
            processed_in_batch = 0
            batch_materialized_bytes = 0
            batch_normalized_bytes = 0
            batch_derived_bytes = 0
            for row in rows:
                check_cancel()
                rowid = int(row["rowid"])
                input_bytes = 0
                if not row["title_is_null"]:
                    with conn.blobopen("conversations", "title", rowid, readonly=True) as blob:
                        input_bytes = len(blob)
                if input_bytes > WEB_INDEX_MAX_INPUT_BYTES:
                    title_value = ""
                else:
                    if processed_in_batch and batch_materialized_bytes + input_bytes > WEB_INDEX_BATCH_INPUT_BYTES:
                        break
                    if input_bytes:
                        with conn.blobopen("conversations", "title", rowid, readonly=True) as blob:
                            title_value = blob.read().decode("utf-8", errors="replace")
                    else:
                        title_value = ""
                    actual_bytes = len(title_value.encode("utf-8"))
                title_norm = normalize_search_text(title_value) if input_bytes <= WEB_INDEX_MAX_INPUT_BYTES else ""
                normalized_bytes = len(title_norm.encode("utf-8"))
                derived_bytes = normalized_bytes * 4
                if processed_in_batch and input_bytes <= WEB_INDEX_MAX_INPUT_BYTES and (
                    batch_normalized_bytes + normalized_bytes
                    > WEB_INDEX_BATCH_NORMALIZED_BYTES
                    or batch_derived_bytes + derived_bytes
                    > WEB_INDEX_BATCH_DERIVED_BYTES
                ):
                    break
                if input_bytes <= WEB_INDEX_MAX_INPUT_BYTES:
                    batch_materialized_bytes += actual_bytes
                    title_materialized_bytes += actual_bytes
                batch_normalized_bytes += normalized_bytes
                batch_derived_bytes += derived_bytes
                title_normalized_materialized_bytes += normalized_bytes
                if (
                    input_bytes > WEB_INDEX_MAX_INPUT_BYTES
                    or normalized_bytes > WEB_INDEX_MAX_NORMALIZED_BYTES
                    or normalized_bytes * 4 > WEB_INDEX_MAX_DERIVED_BYTES
                ):
                    conn.execute(
                        f"INSERT INTO {oversized_build} VALUES ('title', ?, ?, NULL, ?, 'byte_budget')",
                        (rowid, row["conversation_id"], input_bytes),
                    )
                    oversized_titles += 1
                else:
                    title_rows.append((row["conversation_id"], title_norm))
                last_rowid = rowid
                processed_in_batch += 1
            if title_rows:
                conn.executemany(
                    f"INSERT INTO {title_norm_build}(conversation_id, title_norm) VALUES (?, ?)",
                    title_rows,
                )
            title_processed += processed_in_batch
            resource_progress.update(
                input_materialized_bytes=message_materialized_bytes + title_materialized_bytes,
                normalized_materialized_bytes=(
                    normalized_materialized_bytes + title_normalized_materialized_bytes
                ),
                current_batch_input_bytes=batch_materialized_bytes,
                current_batch_normalized_bytes=batch_normalized_bytes,
                current_batch_derived_bytes=batch_derived_bytes,
                oversized_rows=oversized_messages + oversized_titles,
            )
            resource_progress["peak_batch_input_bytes"] = max(
                resource_progress["peak_batch_input_bytes"], batch_materialized_bytes
            )
            resource_progress["peak_batch_normalized_bytes"] = max(
                resource_progress["peak_batch_normalized_bytes"], batch_normalized_bytes
            )
            resource_progress["peak_batch_derived_bytes"] = max(
                resource_progress["peak_batch_derived_bytes"], batch_derived_bytes
            )
            commit_phase("normalize_titles")
            report("normalize_titles", title_processed, title_total)
        indexed_titles = title_processed - oversized_titles

        if trigram_available:
            resource_progress["current_batch_input_bytes"] = 0
            resource_progress["current_batch_normalized_bytes"] = 0
            conn.execute(
                f"""CREATE VIRTUAL TABLE {message_trigram_build} USING fts5(
                       content_text, content='', tokenize='trigram'
                   )"""
            )
            conn.execute(
                f"""CREATE VIRTUAL TABLE {title_trigram_build} USING fts5(
                       title, content='', tokenize='trigram'
                   )"""
            )
            for table in (message_trigram_build, title_trigram_build):
                conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('automerge', 0)")
                conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('crisismerge', 64)")
            commit_phase("build_message_trigram")

            report("build_message_trigram", 0, indexed_messages)
            last_rowid = 0
            trigram_processed = 0
            while True:
                rows = conn.execute(
                    f"""SELECT n.rowid AS source_rowid, mn.rowid AS normalized_rowid
                       FROM conversation_nodes n
                       JOIN {message_norm_build} mn
                         ON mn.conversation_id = n.conversation_id AND mn.node_id = n.node_id
                       WHERE n.rowid > ?
                       ORDER BY n.rowid
                       LIMIT ?""",
                    (last_rowid, batch_size),
                ).fetchall()
                if not rows:
                    break
                bind_rows: list[tuple[int, str]] = []
                bind_bytes = 0
                peak_bind_bytes = 0
                for row in rows:
                    check_cancel()
                    with conn.blobopen(
                        message_norm_build, "content_norm", int(row["normalized_rowid"]), readonly=True
                    ) as blob:
                        value_bytes = blob.read()
                    if bind_rows and bind_bytes + len(value_bytes) > WEB_INDEX_FTS_BIND_BATCH_BYTES:
                        conn.executemany(
                            f"INSERT INTO {message_trigram_build}(rowid, content_text) VALUES (?, ?)",
                            bind_rows,
                        )
                        bind_rows = []
                        bind_bytes = 0
                        check_cancel()
                    bind_rows.append((int(row["source_rowid"]), value_bytes.decode("utf-8", errors="replace")))
                    bind_bytes += len(value_bytes)
                    peak_bind_bytes = max(peak_bind_bytes, bind_bytes)
                if bind_rows:
                    conn.executemany(
                        f"INSERT INTO {message_trigram_build}(rowid, content_text) VALUES (?, ?)",
                        bind_rows,
                    )
                resource_progress["current_batch_normalized_bytes"] = peak_bind_bytes
                resource_progress["peak_batch_normalized_bytes"] = max(
                    resource_progress["peak_batch_normalized_bytes"], peak_bind_bytes
                )
                last_rowid = int(rows[-1]["source_rowid"])
                trigram_processed += len(rows)
                commit_phase("build_message_trigram")
                report("build_message_trigram", trigram_processed, indexed_messages)

            report("build_title_trigram", 0, indexed_titles)
            resource_progress["current_batch_input_bytes"] = 0
            resource_progress["current_batch_normalized_bytes"] = 0
            last_rowid = 0
            trigram_processed = 0
            while True:
                rows = conn.execute(
                    f"""SELECT c.rowid AS source_rowid, tn.rowid AS normalized_rowid
                       FROM conversations c
                       JOIN {title_norm_build} tn ON tn.conversation_id = c.conversation_id
                       WHERE c.rowid > ?
                       ORDER BY c.rowid
                       LIMIT ?""",
                    (last_rowid, batch_size),
                ).fetchall()
                if not rows:
                    break
                bind_rows = []
                bind_bytes = 0
                peak_bind_bytes = 0
                for row in rows:
                    check_cancel()
                    with conn.blobopen(
                        title_norm_build, "title_norm", int(row["normalized_rowid"]), readonly=True
                    ) as blob:
                        value_bytes = blob.read()
                    if bind_rows and bind_bytes + len(value_bytes) > WEB_INDEX_FTS_BIND_BATCH_BYTES:
                        conn.executemany(
                            f"INSERT INTO {title_trigram_build}(rowid, title) VALUES (?, ?)",
                            bind_rows,
                        )
                        bind_rows = []
                        bind_bytes = 0
                        check_cancel()
                    bind_rows.append((int(row["source_rowid"]), value_bytes.decode("utf-8", errors="replace")))
                    bind_bytes += len(value_bytes)
                    peak_bind_bytes = max(peak_bind_bytes, bind_bytes)
                if bind_rows:
                    conn.executemany(
                        f"INSERT INTO {title_trigram_build}(rowid, title) VALUES (?, ?)",
                        bind_rows,
                    )
                resource_progress["current_batch_normalized_bytes"] = peak_bind_bytes
                resource_progress["peak_batch_normalized_bytes"] = max(
                    resource_progress["peak_batch_normalized_bytes"], peak_bind_bytes
                )
                last_rowid = int(rows[-1]["source_rowid"])
                trigram_processed += len(rows)
                commit_phase("build_title_trigram")
                report("build_title_trigram", trigram_processed, indexed_titles)
        else:
            resource_progress["current_batch_input_bytes"] = 0
            resource_progress["current_batch_normalized_bytes"] = 0
            report("build_message_trigram", 0, 0)
            report("build_title_trigram", 0, 0)

        metadata = [
            ("message_norm_text", "normalized"),
            ("title_norm_text", "normalized"),
            ("web_index_format_version", WEB_INDEX_FORMAT_VERSION),
            ("display_text_resolver_version", DISPLAY_TEXT_RESOLVER_VERSION),
            ("normalization_index_format_version", NORMALIZATION_INDEX_FORMAT_VERSION),
            ("oversized_fallback", "required"),
            ("max_input_bytes", str(WEB_INDEX_MAX_INPUT_BYTES)),
            ("max_normalized_bytes", str(WEB_INDEX_MAX_NORMALIZED_BYTES)),
            ("max_derived_bytes", str(WEB_INDEX_MAX_DERIVED_BYTES)),
            ("batch_input_bytes", str(WEB_INDEX_BATCH_INPUT_BYTES)),
            ("batch_normalized_bytes", str(WEB_INDEX_BATCH_NORMALIZED_BYTES)),
            ("batch_derived_bytes", str(WEB_INDEX_BATCH_DERIVED_BYTES)),
            ("message_generation", starting_generations["message"]),
            ("title_generation", starting_generations["title"]),
            (OPTIONAL_WEB_INDEX_OWNER_KEY, OPTIONAL_WEB_INDEX_OWNER),
        ]
        if trigram_available:
            metadata.extend([
                ("message_trigram_text", "normalized"),
                ("title_trigram_text", "normalized"),
            ])
        resource_progress["current_batch_input_bytes"] = 0
        resource_progress["current_batch_normalized_bytes"] = 0
        report("write_metadata", 0, len(metadata))
        conn.executemany(f"INSERT INTO {metadata_build}(key, value) VALUES(?, ?)", metadata)
        commit_phase("write_metadata")
        report("write_metadata", len(metadata), len(metadata))

        report("commit_swap", 0, 1)
        conn.execute("BEGIN IMMEDIATE")
        _validate_lease_table(conn, create=False)
        lease = conn.execute(
            f"SELECT * FROM {WEB_INDEX_BUILD_LEASE_TABLE} WHERE slot = 1"
        ).fetchone()
        if (
            lease is None
            or str(lease["build_id"]) != build_id
            or not secrets.compare_digest(str(lease["owner_token"]), owner_token)
            or str(lease["database_identity"]) != database_identity
            or str(lease["format_version"]) != WEB_INDEX_FORMAT_VERSION
            or _decode_owned_names(lease["object_names_json"], build_id) != build_names
        ):
            raise WebIndexBuildError("web_index_build_lease_lost")
        if _canonical_generations(conn) != starting_generations:
            raise WebIndexBuildError("web_index_generation_changed_before_publish")
        if (
            str(lease["message_generation"]) != starting_generations["message"]
            or str(lease["title_generation"]) != starting_generations["title"]
            or int(lease["schema_version"]) != int(conn.execute("PRAGMA user_version").fetchone()[0])
        ):
            raise WebIndexBuildError("web_index_generation_changed_before_publish")
        _validate_staging_objects(
            conn,
            build_names,
            allow_missing=False,
            trigram_expected=trigram_available,
        )
        publish_drop_failures = drop_optional_web_indexes(conn)
        if publish_drop_failures:
            raise WebIndexBuildError("web_index_drop_failed")
        conn.execute(f"ALTER TABLE {message_norm_build} RENAME TO web_message_norm")
        conn.execute(f"ALTER TABLE {title_norm_build} RENAME TO web_title_norm")
        conn.execute(f"ALTER TABLE {metadata_build} RENAME TO web_index_metadata")
        conn.execute(f"ALTER TABLE {oversized_build} RENAME TO web_index_oversized")
        if trigram_available:
            conn.execute(f"ALTER TABLE {message_trigram_build} RENAME TO web_message_trigram")
            conn.execute(f"ALTER TABLE {title_trigram_build} RENAME TO web_title_trigram")
        invalidate_capability_cache(conn)
        status = web_index_status(conn)
        expected_current = status["web_normalized_indexed"] and (
            not trigram_available or status["web_normalized_trigram_indexed"]
        )
        if not status["web_index_format_current"] or not expected_current:
            raise WebIndexBuildError("web_index_publish_validation_failed")
        conn.execute(
            f"DELETE FROM {WEB_INDEX_BUILD_LEASE_TABLE} "
            "WHERE slot = 1 AND build_id = ? AND owner_token = ?",
            (build_id, owner_token),
        )
        conn.commit()
        invalidate_capability_cache(conn)
        invalidate_read_capability_cache()
        if progress_callback is not None:
            try:
                progress_callback("commit_swap", {
                    "build_stage": "commit_swap",
                    "processed": 1,
                    "total": 1,
                    "complete": True,
                    "batch_size": batch_size,
                })
            except Exception:
                # Publication already committed. Observer failure cannot make a
                # valid current index look like a failed build.
                pass
        return {
            "trigram_available": trigram_available,
            "indexed_messages": indexed_messages,
            "indexed_titles": indexed_titles,
            "oversized_messages": oversized_messages,
            "oversized_titles": oversized_titles,
            "input_materialized_bytes": message_materialized_bytes + title_materialized_bytes,
            "normalized_materialized_bytes": (
                normalized_materialized_bytes + title_normalized_materialized_bytes
            ),
            "peak_batch_input_bytes": resource_progress["peak_batch_input_bytes"],
            "peak_batch_normalized_bytes": resource_progress["peak_batch_normalized_bytes"],
            "peak_batch_derived_bytes": resource_progress["peak_batch_derived_bytes"],
            "max_input_bytes": WEB_INDEX_MAX_INPUT_BYTES,
            "max_normalized_bytes": WEB_INDEX_MAX_NORMALIZED_BYTES,
            "max_derived_bytes": WEB_INDEX_MAX_DERIVED_BYTES,
            "fts_bind_batch_bytes": WEB_INDEX_FTS_BIND_BATCH_BYTES,
            "batch_input_bytes": WEB_INDEX_BATCH_INPUT_BYTES,
            "batch_normalized_bytes": WEB_INDEX_BATCH_NORMALIZED_BYTES,
            "batch_derived_bytes": WEB_INDEX_BATCH_DERIVED_BYTES,
            "batch_size": batch_size,
            "atomic_publish": True,
            "progress_stages": list(WEB_INDEX_BUILD_STAGES),
            "drop_failures_count": 0,
            "drop_failures": [],
        }
    except sqlite3.OperationalError as exc:
        if conn.in_transaction:
            conn.rollback()
        conn.set_progress_handler(None, 0)
        _cleanup_web_index_staging(
            conn, build_names, build_id=build_id, owner_token=owner_token
        )
        invalidate_capability_cache(conn)
        if cancelled:
            raise WebIndexBuildCancelled() from None
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        conn.set_progress_handler(None, 0)
        _cleanup_web_index_staging(
            conn, build_names, build_id=build_id, owner_token=owner_token
        )
        invalidate_capability_cache(conn)
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
