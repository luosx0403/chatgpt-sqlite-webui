from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from .db import (
    CORE_SCHEMA_COLUMNS,
    DatabaseMigrationError,
    _drop_table_with_shadows,
    configure_bulk_write_connection,
    database_schema_status,
    invalidate_read_capability_cache,
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
from .search import _derived_generation_is_current, invalidate_capability_cache, normalize_search_text, search_fragment_match

WEB_INDEX_FORMAT_VERSION = OPTIONAL_WEB_INDEX_FORMAT_VERSION
WEB_INDEX_BATCH_SIZE = 200
WEB_INDEX_PROGRESS_VM_STEPS = 10_000
WEB_INDEX_MAX_INPUT_BYTES = 4 * 1024 * 1024
WEB_INDEX_MAX_NORMALIZED_BYTES = 2 * 1024 * 1024
WEB_INDEX_MAX_DERIVED_BYTES = 8 * 1024 * 1024
WEB_INDEX_BATCH_INPUT_BYTES = 16 * 1024 * 1024
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
    conn.create_function("web_norm", 1, normalize_search_text, deterministic=True)
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text)
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
    conn.create_function("web_norm", 1, normalize_search_text, deterministic=True)
    conn.create_function("web_search_match", 3, search_fragment_match, deterministic=True)
    conn.create_function("web_display_text", 2, recover_message_display_text)
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


def create_web_indexes(
    db_path: Path,
    *,
    batch_size: int = WEB_INDEX_BATCH_SIZE,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build optional Web indexes in bounded batches and publish in one transaction.

    The old optional index remains visible to other connections until commit. Any
    failure or requested cancellation rolls the transaction back in full.
    """

    batch_size = max(1, min(1_000, int(batch_size)))
    conn = connect_writable(db_path)
    cancelled = False
    resource_progress = {
        "input_materialized_bytes": 0,
        "normalized_materialized_bytes": 0,
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
                "total": total,
                "complete": processed >= total,
                "batch_size": batch_size,
                **resource_progress,
            })
        check_cancel()

    try:
        schema = check_schema(conn)
        if not schema["ok"]:
            code = "database_migration_required" if schema["migration_required"] else "database_schema_incompatible"
            raise DatabaseMigrationError(code, detail=schema)
        configure_bulk_write_connection(conn)
        trigram_available = detect_trigram(conn)
        conn.set_progress_handler(progress_handler, WEB_INDEX_PROGRESS_VM_STEPS)
        conn.execute("BEGIN IMMEDIATE")
        message_total = int(conn.execute("SELECT COUNT(*) AS c FROM conversation_nodes").fetchone()["c"])
        title_total = int(conn.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()["c"])
        drop_failures: list[dict[str, str]] = []
        drop_failures.extend(_drop_table_with_shadows(conn, "web_message_trigram"))
        drop_failures.extend(_drop_table_with_shadows(conn, "web_title_trigram"))
        if drop_failures:
            if cancelled:
                raise WebIndexBuildCancelled()
            raise WebIndexBuildError("web_index_drop_failed")
        conn.execute("DROP TABLE IF EXISTS web_message_norm")
        conn.execute("DROP TABLE IF EXISTS web_title_norm")
        conn.execute("DROP TABLE IF EXISTS web_index_metadata")
        conn.execute("DROP TABLE IF EXISTS web_index_oversized")
        conn.execute(
            """CREATE TABLE web_message_norm(
                   conversation_id TEXT NOT NULL,
                   node_id TEXT NOT NULL,
                   content_norm TEXT NOT NULL,
                   PRIMARY KEY(conversation_id, node_id)
               )"""
        )
        conn.execute(
            """CREATE TABLE web_title_norm(
                   conversation_id TEXT NOT NULL PRIMARY KEY,
                   title_norm TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE web_index_metadata(
                   key TEXT NOT NULL PRIMARY KEY,
                   value TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE web_index_oversized(
                   kind TEXT NOT NULL,
                   source_rowid INTEGER NOT NULL,
                   conversation_id TEXT NOT NULL,
                   node_id TEXT,
                   input_bytes INTEGER NOT NULL,
                   reason TEXT NOT NULL,
                   PRIMARY KEY(kind, source_rowid)
               )"""
        )

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
                marker_prefix = (
                    content_prefix.strip().startswith("[non-text content:")
                    or content_prefix.strip().startswith("[non-text part:")
                )
                canonical_usable = bool(content_prefix) and not (
                    marker_prefix
                    and str(row["content_type"] or "").casefold()
                    not in {"text", "code", "multimodal_text"}
                )
                chosen_size = content_size
                raw_size = 0
                if not canonical_usable and not row["raw_is_null"]:
                    with conn.blobopen("conversation_nodes", "raw_message_json", rowid, readonly=True) as blob:
                        raw_size = len(blob)
                    chosen_size = raw_size
                input_bytes = chosen_size if canonical_usable else len(content_prefix_bytes) + raw_size
                if chosen_size > WEB_INDEX_MAX_INPUT_BYTES:
                    conn.execute(
                        "INSERT INTO web_index_oversized VALUES ('message', ?, ?, ?, ?, 'input_bytes')",
                        (rowid, row["conversation_id"], row["node_id"], chosen_size),
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
                    content_value = content_prefix
                    if raw_size:
                        with conn.blobopen("conversation_nodes", "raw_message_json", rowid, readonly=True) as blob:
                            raw_value = blob.read().decode("utf-8", errors="replace")
                    else:
                        raw_value = None
                    actual_bytes = len(content_prefix_bytes) + (len(raw_value.encode("utf-8")) if raw_value else 0)
                batch_materialized_bytes += actual_bytes
                message_materialized_bytes += actual_bytes
                display_text = recover_message_display_text(content_value, raw_value)
                content_norm = normalize_search_text(display_text)
                if content_norm:
                    normalized_bytes = len(content_norm.encode("utf-8"))
                    normalized_materialized_bytes += normalized_bytes
                    if (
                        normalized_bytes > WEB_INDEX_MAX_NORMALIZED_BYTES
                        or normalized_bytes * 4 > WEB_INDEX_MAX_DERIVED_BYTES
                    ):
                        conn.execute(
                            "INSERT INTO web_index_oversized VALUES ('message', ?, ?, ?, ?, 'derived_bytes')",
                            (rowid, row["conversation_id"], row["node_id"], actual_bytes),
                        )
                        oversized_messages += 1
                        # Keep a strictly bounded compatibility tier searchable
                        # while the row remains marked for canonical fallback.
                        # This preserves exact recall for moderately oversized
                        # legacy rows without ever materializing inputs above the
                        # 4 MiB hard input budget.
                        if (
                            normalized_bytes <= WEB_INDEX_MAX_INPUT_BYTES
                            and normalized_bytes * 4 <= WEB_INDEX_MAX_DERIVED_BYTES * 2
                        ):
                            normalized_rows.append(
                                (row["conversation_id"], row["node_id"], content_norm)
                            )
                        last_rowid = rowid
                        processed_in_batch += 1
                        continue
                    normalized_rows.append((row["conversation_id"], row["node_id"], content_norm))
                last_rowid = rowid
                processed_in_batch += 1
            if normalized_rows:
                conn.executemany(
                    "INSERT INTO web_message_norm(conversation_id, node_id, content_norm) VALUES (?, ?, ?)",
                    normalized_rows,
                )
                indexed_messages += len(normalized_rows)
            message_processed += processed_in_batch
            resource_progress.update(
                input_materialized_bytes=message_materialized_bytes,
                normalized_materialized_bytes=normalized_materialized_bytes,
                oversized_rows=oversized_messages,
            )
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
                    batch_materialized_bytes += actual_bytes
                    title_materialized_bytes += actual_bytes
                title_norm = normalize_search_text(title_value) if input_bytes <= WEB_INDEX_MAX_INPUT_BYTES else ""
                normalized_bytes = len(title_norm.encode("utf-8"))
                title_normalized_materialized_bytes += normalized_bytes
                if (
                    input_bytes > WEB_INDEX_MAX_INPUT_BYTES
                    or normalized_bytes > WEB_INDEX_MAX_NORMALIZED_BYTES
                    or normalized_bytes * 4 > WEB_INDEX_MAX_DERIVED_BYTES
                ):
                    conn.execute(
                        "INSERT INTO web_index_oversized VALUES ('title', ?, ?, NULL, ?, 'byte_budget')",
                        (rowid, row["conversation_id"], input_bytes),
                    )
                    oversized_titles += 1
                else:
                    title_rows.append((row["conversation_id"], title_norm))
                last_rowid = rowid
                processed_in_batch += 1
            if title_rows:
                conn.executemany(
                    "INSERT INTO web_title_norm(conversation_id, title_norm) VALUES (?, ?)",
                    title_rows,
                )
            title_processed += processed_in_batch
            resource_progress.update(
                input_materialized_bytes=message_materialized_bytes + title_materialized_bytes,
                normalized_materialized_bytes=(
                    normalized_materialized_bytes + title_normalized_materialized_bytes
                ),
                oversized_rows=oversized_messages + oversized_titles,
            )
            report("normalize_titles", title_processed, title_total)
        indexed_titles = title_processed - oversized_titles

        if trigram_available:
            conn.execute(
                """CREATE VIRTUAL TABLE web_message_trigram USING fts5(
                       content_text, content='', tokenize='trigram'
                   )"""
            )
            conn.execute(
                """CREATE VIRTUAL TABLE web_title_trigram USING fts5(
                       title, content='', tokenize='trigram'
                   )"""
            )
            for table in ("web_message_trigram", "web_title_trigram"):
                conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('automerge', 0)")
                conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('crisismerge', 64)")

            report("build_message_trigram", 0, indexed_messages)
            last_rowid = 0
            trigram_processed = 0
            while True:
                rows = conn.execute(
                    """SELECT n.rowid, mn.content_norm
                       FROM conversation_nodes n
                       JOIN web_message_norm mn
                         ON mn.conversation_id = n.conversation_id AND mn.node_id = n.node_id
                       WHERE n.rowid > ?
                       ORDER BY n.rowid
                       LIMIT ?""",
                    (last_rowid, batch_size),
                ).fetchall()
                if not rows:
                    break
                conn.executemany(
                    "INSERT INTO web_message_trigram(rowid, content_text) VALUES (?, ?)",
                    [(row["rowid"], row["content_norm"]) for row in rows],
                )
                last_rowid = int(rows[-1]["rowid"])
                trigram_processed += len(rows)
                report("build_message_trigram", trigram_processed, indexed_messages)

            report("build_title_trigram", 0, indexed_titles)
            last_rowid = 0
            trigram_processed = 0
            while True:
                rows = conn.execute(
                    """SELECT c.rowid, tn.title_norm
                       FROM conversations c
                       JOIN web_title_norm tn ON tn.conversation_id = c.conversation_id
                       WHERE c.rowid > ?
                       ORDER BY c.rowid
                       LIMIT ?""",
                    (last_rowid, batch_size),
                ).fetchall()
                if not rows:
                    break
                conn.executemany(
                    "INSERT INTO web_title_trigram(rowid, title) VALUES (?, ?)",
                    [(row["rowid"], row["title_norm"]) for row in rows],
                )
                last_rowid = int(rows[-1]["rowid"])
                trigram_processed += len(rows)
                report("build_title_trigram", trigram_processed, indexed_titles)
        else:
            report("build_message_trigram", 0, 0)
            report("build_title_trigram", 0, 0)

        generations = _canonical_generations(conn)
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
            ("message_generation", generations["message"]),
            ("title_generation", generations["title"]),
        ]
        if trigram_available:
            metadata.extend([
                ("message_trigram_text", "normalized"),
                ("title_trigram_text", "normalized"),
            ])
        report("write_metadata", 0, len(metadata))
        conn.executemany("INSERT INTO web_index_metadata(key, value) VALUES(?, ?)", metadata)
        report("write_metadata", len(metadata), len(metadata))

        invalidate_capability_cache(conn)
        status = web_index_status(conn)
        expected_current = status["web_normalized_indexed"] and (
            not trigram_available or status["web_normalized_trigram_indexed"]
        )
        if not status["web_index_format_current"] or not expected_current:
            raise WebIndexBuildError("web_index_publish_validation_failed")
        report("commit_swap", 0, 1)
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
            "max_input_bytes": WEB_INDEX_MAX_INPUT_BYTES,
            "max_normalized_bytes": WEB_INDEX_MAX_NORMALIZED_BYTES,
            "max_derived_bytes": WEB_INDEX_MAX_DERIVED_BYTES,
            "batch_size": batch_size,
            "atomic_publish": True,
            "progress_stages": list(WEB_INDEX_BUILD_STAGES),
            "drop_failures_count": 0,
            "drop_failures": [],
        }
    except sqlite3.OperationalError as exc:
        if conn.in_transaction:
            conn.rollback()
        invalidate_capability_cache(conn)
        if cancelled:
            raise WebIndexBuildCancelled() from None
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        invalidate_capability_cache(conn)
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
