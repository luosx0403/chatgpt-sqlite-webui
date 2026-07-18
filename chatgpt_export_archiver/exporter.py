from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .current_path import ensure_effective_current_views, resolve_effective_current_collection
from .db import record_export
from .parser import recover_message_display_text
from .search import _is_internal_message
from .utils import epoch_to_date_part, epoch_to_display, finite_float_or_none, parse_date_boundary, safe_filename_part, sha256_text, truncate_utf8, write_chunks_if_changed


MAX_EXPORT_BASENAME_BYTES = 240
EXPORT_CONVERSATION_BATCH_SIZE = 200
EXPORT_NODE_BATCH_SIZE = 8
MAX_EXPORT_NODES_PER_CONVERSATION = 100_000
MAX_EXPORT_NODE_INPUT_BYTES = 32 * 1024 * 1024
MAX_EXPORT_CONVERSATION_INPUT_BYTES = 128 * 1024 * 1024
MAX_EXPORT_HEADER_INPUT_BYTES = 4 * 1024 * 1024
MAX_EXPORT_BATCH_INPUT_BYTES = 160 * 1024 * 1024
MAX_EXPORT_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_EXPORT_CONVERSATIONS = 1_000_000
MAX_ARCHIVE_EXPORT_METADATA_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_EXPORT_MANIFEST_BYTES = 2 * 1024 * 1024 * 1024
EXPORT_PLAN_BATCH_SIZE = 2_000


class ExportResourceLimitError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_EXPORT_BUDGET_TOKEN_SEAL = object()
EXPORT_BUDGET_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class ValidatedConversationExportBudget:
    conversation_id: str
    path: str
    include_internal: bool
    data_version: int
    generation_snapshot: tuple[int, int]
    node_count: int
    input_bytes: int
    contract_version: int
    _seal: object


def _export_snapshot_identity(conn: sqlite3.Connection) -> tuple[int, tuple[int, int]]:
    data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    generations = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT name, generation FROM archive_generations WHERE name IN ('message', 'title')"
        )
    }
    return data_version, (generations.get("message", -1), generations.get("title", -1))


def validate_conversation_export_budget(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    path: str,
    include_internal: bool,
) -> ValidatedConversationExportBudget:
    budget = check_conversation_export_budget(conn, conversation_id)
    data_version, generations = _export_snapshot_identity(conn)
    return ValidatedConversationExportBudget(
        conversation_id=str(conversation_id),
        path=path,
        include_internal=bool(include_internal),
        data_version=data_version,
        generation_snapshot=generations,
        node_count=budget["node_count"],
        input_bytes=budget["input_bytes"],
        contract_version=EXPORT_BUDGET_CONTRACT_VERSION,
        _seal=_EXPORT_BUDGET_TOKEN_SEAL,
    )


def check_conversation_export_budget(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> dict[str, int]:
    return check_conversation_export_budgets(conn, [conversation_id])[conversation_id]


def check_conversation_export_budgets(
    conn: sqlite3.Connection,
    conversation_ids: Sequence[str],
) -> dict[str, dict[str, int]]:
    ids = [str(value) for value in conversation_ids]
    if not ids:
        return {}
    results = {
        conversation_id: {"node_count": 0, "input_bytes": 0, "max_node_bytes": 0, "header_bytes": 0}
        for conversation_id in ids
    }
    node_columns = (
        "conversation_id", "node_id", "parent_node_id", "children_json", "message_id",
        "role", "author_name", "content_type", "content_text", "raw_message_json",
        "content_hash", "metadata_json",
    )
    large_node_columns = {"children_json", "content_text", "raw_message_json", "metadata_json"}
    header_columns = (
        "conversation_id", "title", "current_node", "source_file",
        "default_model_slug", "metadata_json",
    )
    placeholders = ",".join("?" for _ in ids)
    node_presence = ", ".join(f'"{column}" IS NOT NULL AS "has_{column}"' for column in node_columns)
    for row in conn.execute(
        f"""SELECT rowid AS storage_rowid, conversation_id, {node_presence}
            FROM conversation_nodes WHERE conversation_id IN ({placeholders})""",
        ids,
    ):
        conversation_id = str(row["conversation_id"])
        result = results[conversation_id]
        result["node_count"] += 1
        node_bytes = 0
        large_node_bytes = 0
        for column in node_columns:
            column_bytes = _sqlite_value_bytes(
                conn,
                "conversation_nodes",
                column,
                int(row["storage_rowid"]),
                present=bool(row[f"has_{column}"]),
            )
            node_bytes += column_bytes
            if column in large_node_columns:
                large_node_bytes += column_bytes
        result["input_bytes"] += node_bytes
        result["max_node_bytes"] = max(result["max_node_bytes"], large_node_bytes)
        if result["node_count"] > MAX_EXPORT_NODES_PER_CONVERSATION:
            raise ExportResourceLimitError("export_node_count_limit_exceeded")
        if result["max_node_bytes"] > MAX_EXPORT_NODE_INPUT_BYTES:
            raise ExportResourceLimitError("export_node_input_limit_exceeded")
        if result["input_bytes"] > MAX_EXPORT_CONVERSATION_INPUT_BYTES:
            raise ExportResourceLimitError("export_input_byte_limit_exceeded")

    header_presence = ", ".join(f'"{column}" IS NOT NULL AS "has_{column}"' for column in header_columns)
    for row in conn.execute(
        f"""SELECT rowid AS storage_rowid, conversation_id, {header_presence}
            FROM conversations WHERE conversation_id IN ({placeholders})""",
        ids,
    ):
        header_bytes = sum(
            _sqlite_value_bytes(
                conn,
                "conversations",
                column,
                int(row["storage_rowid"]),
                present=bool(row[f"has_{column}"]),
            )
            for column in header_columns
        )
        results[str(row["conversation_id"])]["header_bytes"] = header_bytes
    for result in results.values():
        if result["node_count"] > MAX_EXPORT_NODES_PER_CONVERSATION:
            raise ExportResourceLimitError("export_node_count_limit_exceeded")
        if result["max_node_bytes"] > MAX_EXPORT_NODE_INPUT_BYTES:
            raise ExportResourceLimitError("export_node_input_limit_exceeded")
        if result["input_bytes"] > MAX_EXPORT_CONVERSATION_INPUT_BYTES:
            raise ExportResourceLimitError("export_input_byte_limit_exceeded")
        if result["header_bytes"] > MAX_EXPORT_HEADER_INPUT_BYTES:
            raise ExportResourceLimitError("export_header_input_limit_exceeded")
    return results


def _sqlite_value_bytes(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    rowid: int,
    *,
    present: bool,
) -> int:
    """Read only SQLite's BLOB length metadata, never the value payload."""

    if not present:
        return 0
    with conn.blobopen(table, column, rowid, readonly=True) as blob:
        return len(blob)


def export_conversations(
    conn: sqlite3.Connection,
    out_dir: Path,
    formats: list[str],
    from_date: str | None = None,
    to_date: str | None = None,
    force: bool = False,
    path: str = "current",
    include_internal: bool = False,
    conversation_batch_size: int = EXPORT_CONVERSATION_BATCH_SIZE,
) -> dict[str, Any]:
    if path not in {"current", "all"}:
        raise ValueError("invalid_export_path")
    conversation_batch_size = max(1, min(400, int(conversation_batch_size)))
    formats = sorted({str(fmt).lower() for fmt in formats})
    out_dir.mkdir(parents=True, exist_ok=True)
    start_ts = parse_date_boundary(from_date)
    end_ts = parse_date_boundary(to_date, end_of_day=True)
    plan_path: str | None = None
    plan: sqlite3.Connection | None = None
    try:
        plan_fd, plan_path = tempfile.mkstemp(
            prefix=".chatgpt-archive-export-plan-", suffix=".sqlite3", dir=out_dir
        )
        os.close(plan_fd)
        plan = sqlite3.connect(plan_path)
        plan.row_factory = sqlite3.Row
        plan.execute("PRAGMA journal_mode=OFF")
        plan.execute("PRAGMA synchronous=OFF")
        _create_archive_export_plan(plan)
        conversation_count = _populate_archive_export_plan(
            conn, plan, start_ts=start_ts, end_ts=end_ts, formats=formats
        )
        _allocate_archive_export_filenames(plan, formats)

        written = 0
        skipped = 0
        last_order = 0
        while True:
            requested_batch = plan.execute(
                "SELECT * FROM conversations WHERE plan_order > ? ORDER BY plan_order LIMIT ?",
                (last_order, conversation_batch_size),
            ).fetchall()
            if not requested_batch:
                break
            last_order = int(requested_batch[-1]["plan_order"])
            budgets = check_conversation_export_budgets(
                conn, [str(conv["conversation_id"]) for conv in requested_batch]
            )
            bounded_batches: list[list[sqlite3.Row]] = []
            bounded_batch: list[sqlite3.Row] = []
            batch_input_bytes = 0
            for conv in requested_batch:
                budget = budgets[str(conv["conversation_id"])]
                if bounded_batch and batch_input_bytes + budget["input_bytes"] > MAX_EXPORT_BATCH_INPUT_BYTES:
                    bounded_batches.append(bounded_batch)
                    bounded_batch = []
                    batch_input_bytes = 0
                bounded_batch.append(conv)
                batch_input_bytes += budget["input_bytes"]
            if bounded_batch:
                bounded_batches.append(bounded_batch)

            for conversation_batch in bounded_batches:
                ids = [str(conv["conversation_id"]) for conv in conversation_batch]
                _spool_export_nodes_for_batch(conn, plan, ids, path=path)
                placeholders = ",".join("?" for _ in ids)
                filename_rows = plan.execute(
                    f"SELECT conversation_id, format, output_path FROM requests WHERE conversation_id IN ({placeholders})",
                    ids,
                ).fetchall()
                filenames = {
                    (str(row["conversation_id"]), str(row["format"])): Path(str(row["output_path"]))
                    for row in filename_rows
                }
                for conv in conversation_batch:
                    conversation_id = str(conv["conversation_id"])
                    for fmt in formats:
                        rel_path = filenames[(conversation_id, fmt)]
                        nodes = _iter_spooled_export_nodes(
                            plan,
                            conversation_id,
                            include_internal=include_internal,
                        )
                        changed, output_hash, _output_bytes = write_chunks_if_changed(
                            out_dir / rel_path,
                            iter_rendered_conversation(conv, nodes, fmt),
                            force=force,
                            max_bytes=MAX_EXPORT_OUTPUT_BYTES,
                        )
                        written += int(changed)
                        skipped += int(not changed)
                        plan.execute(
                            "UPDATE requests SET output_hash = ? WHERE conversation_id = ? AND format = ?",
                            (output_hash, conversation_id, fmt),
                        )
                plan.commit()

        _validate_archive_export_outputs(plan, out_dir, conversation_count, formats)
        manifest_cleanup_warnings = _write_manifest_from_plan(
            plan,
            out_dir,
            path=path,
            include_internal=include_internal,
            force=force,
        )
        # End the canonical read snapshot before recording export bookkeeping.
        conn.commit()
        _record_archive_exports(
            conn,
            plan,
            out_dir,
            path=path,
            include_internal=include_internal,
            from_date=from_date,
            to_date=to_date,
        )
        return {
            "conversations": conversation_count,
            "formats": formats,
            "written": written,
            "skipped_unchanged": skipped,
            "cleanup_warnings": manifest_cleanup_warnings,
        }
    finally:
        if plan is not None:
            plan.close()
        if plan_path is not None:
            try:
                os.unlink(plan_path)
            except FileNotFoundError:
                pass


def _create_archive_export_plan(plan: sqlite3.Connection) -> None:
    plan.executescript(
        """
        CREATE TABLE conversations (
            plan_order INTEGER PRIMARY KEY,
            conversation_id TEXT NOT NULL UNIQUE,
            title TEXT,
            create_time REAL,
            update_time REAL,
            current_node TEXT,
            source_file TEXT,
            aggregate_hash TEXT NOT NULL
        );
        CREATE TABLE requests (
            conversation_id TEXT NOT NULL,
            format TEXT NOT NULL,
            natural_name TEXT NOT NULL,
            collision_key TEXT NOT NULL,
            output_path TEXT,
            output_collision_key TEXT,
            output_hash TEXT,
            PRIMARY KEY (conversation_id, format)
        );
        CREATE INDEX requests_natural_key ON requests(collision_key, format, conversation_id);
        CREATE UNIQUE INDEX requests_output_key ON requests(output_collision_key)
            WHERE output_collision_key IS NOT NULL;
        CREATE TABLE export_nodes (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            role TEXT,
            content_type TEXT,
            content_text TEXT,
            raw_message_json TEXT,
            create_time REAL,
            update_time REAL
        );
        CREATE INDEX export_nodes_conversation_sequence
            ON export_nodes(conversation_id, sequence);
        CREATE TABLE natural_counts (
            collision_key TEXT PRIMARY KEY,
            request_count INTEGER NOT NULL,
            next_suffix INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE allocated_names (collision_key TEXT PRIMARY KEY);
        """
    )


def _iter_export_conversation_rows(
    conn: sqlite3.Connection,
    *,
    start_ts: float | None,
    end_ts: float | None,
    batch_size: int = EXPORT_PLAN_BATCH_SIZE,
) -> Iterator[sqlite3.Row]:
    where: list[str] = []
    params: list[Any] = []
    if start_ts is not None:
        where.append("COALESCE(update_time, create_time, 0) >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("COALESCE(update_time, create_time, 0) < ?")
        params.append(end_ts)
    last_key: tuple[Any, str] | None = None
    sort_expr = "COALESCE(create_time, update_time, 0)"
    while True:
        page_where = list(where)
        page_params = list(params)
        if last_key is not None:
            page_where.append(
                f"({sort_expr} > ? OR ({sort_expr} = ? AND conversation_id > ?))"
            )
            page_params.extend((last_key[0], last_key[0], last_key[1]))
        clause = "WHERE " + " AND ".join(page_where) if page_where else ""
        page_params.append(max(1, min(EXPORT_PLAN_BATCH_SIZE, int(batch_size))))
        cursor = conn.execute(
            f"""SELECT conversation_id, title, create_time, update_time,
                       current_node, source_file, aggregate_hash,
                       {sort_expr} AS export_sort_time,
                       COALESCE(length(CAST(conversation_id AS BLOB)), 0) +
                       COALESCE(length(CAST(title AS BLOB)), 0) +
                       COALESCE(length(CAST(current_node AS BLOB)), 0) +
                       COALESCE(length(CAST(source_file AS BLOB)), 0) +
                       COALESCE(length(CAST(aggregate_hash AS BLOB)), 0) + 24
                           AS export_metadata_bytes
                FROM conversations
                {clause}
                ORDER BY export_sort_time, conversation_id
                LIMIT ?""",
            page_params,
        )
        row_count = 0
        tail: sqlite3.Row | None = None
        for row in cursor:
            row_count += 1
            tail = row
            yield row
        if row_count == 0 or tail is None:
            return
        last_key = (tail["export_sort_time"], str(tail["conversation_id"]))


def _populate_archive_export_plan(
    conn: sqlite3.Connection,
    plan: sqlite3.Connection,
    *,
    start_ts: float | None,
    end_ts: float | None,
    formats: Sequence[str],
) -> int:
    conversation_count = 0
    metadata_bytes = 0
    pending_conversations: list[tuple[Any, ...]] = []
    pending_requests: list[tuple[str, str, str, str]] = []
    for row in _iter_export_conversation_rows(conn, start_ts=start_ts, end_ts=end_ts):
        conversation_count += 1
        if conversation_count > MAX_ARCHIVE_EXPORT_CONVERSATIONS:
            raise ExportResourceLimitError("archive_export_conversation_limit_exceeded")
        row_bytes = int(row["export_metadata_bytes"] or 0)
        if row_bytes > MAX_EXPORT_HEADER_INPUT_BYTES:
            raise ExportResourceLimitError("export_header_input_limit_exceeded")
        metadata_bytes += row_bytes
        if metadata_bytes > MAX_ARCHIVE_EXPORT_METADATA_BYTES:
            raise ExportResourceLimitError("archive_export_metadata_limit_exceeded")
        conversation_id = str(row["conversation_id"])
        pending_conversations.append((
            conversation_count,
            conversation_id,
            row["title"],
            row["create_time"],
            row["update_time"],
            row["current_node"],
            row["source_file"],
            row["aggregate_hash"],
        ))
        for fmt in formats:
            natural_name = _base_filename(row, fmt)
            pending_requests.append((
                conversation_id, fmt, natural_name, _filename_collision_key(natural_name)
            ))
        if len(pending_conversations) >= EXPORT_PLAN_BATCH_SIZE:
            plan.executemany(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                pending_conversations,
            )
            plan.executemany(
                "INSERT INTO requests(conversation_id, format, natural_name, collision_key) VALUES (?, ?, ?, ?)",
                pending_requests,
            )
            plan.commit()
            pending_conversations.clear()
            pending_requests.clear()
    if pending_conversations:
        plan.executemany(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            pending_conversations,
        )
        plan.executemany(
            "INSERT INTO requests(conversation_id, format, natural_name, collision_key) VALUES (?, ?, ?, ?)",
            pending_requests,
        )
    plan.execute(
        "INSERT INTO natural_counts(collision_key, request_count) SELECT collision_key, COUNT(*) FROM requests GROUP BY collision_key"
    )
    plan.commit()
    return conversation_count


def _allocate_archive_export_filenames(
    plan: sqlite3.Connection, formats: Sequence[str]
) -> None:
    del formats
    cursor = plan.execute(
        """SELECT r.conversation_id, r.format, r.natural_name, r.collision_key,
                  n.request_count, n.next_suffix,
                  c.title, c.create_time, c.update_time, c.current_node,
                  c.source_file, c.aggregate_hash
           FROM requests AS r
           JOIN natural_counts AS n ON n.collision_key = r.collision_key
           JOIN conversations AS c ON c.conversation_id = r.conversation_id
           ORDER BY r.collision_key, r.format, r.conversation_id"""
    )
    for row in cursor:
        if int(row["request_count"]) == 1:
            candidate = str(row["natural_name"])
        else:
            suffix_index = int(
                plan.execute(
                    "SELECT next_suffix FROM natural_counts WHERE collision_key = ?",
                    (row["collision_key"],),
                ).fetchone()[0]
            )
            while True:
                candidate = _base_filename(
                    row, str(row["format"]), collision_suffix=f"_{suffix_index:03d}"
                )
                candidate_key = _filename_collision_key(candidate)
                reserved = plan.execute(
                    "SELECT 1 FROM natural_counts WHERE collision_key = ?",
                    (candidate_key,),
                ).fetchone()
                allocated = plan.execute(
                    "SELECT 1 FROM allocated_names WHERE collision_key = ?",
                    (candidate_key,),
                ).fetchone()
                suffix_index += 1
                if reserved is None and allocated is None:
                    plan.execute(
                        "UPDATE natural_counts SET next_suffix = ? WHERE collision_key = ?",
                        (suffix_index, row["collision_key"]),
                    )
                    break
        candidate_key = _filename_collision_key(candidate)
        plan.execute("INSERT INTO allocated_names(collision_key) VALUES (?)", (candidate_key,))
        plan.execute(
            """UPDATE requests SET output_path = ?, output_collision_key = ?
               WHERE conversation_id = ? AND format = ?""",
            (candidate, candidate_key, row["conversation_id"], row["format"]),
        )
    plan.commit()


def _validate_archive_export_outputs(
    plan: sqlite3.Connection,
    out_dir: Path,
    conversation_count: int,
    formats: Sequence[str],
) -> None:
    expected = conversation_count * len(formats)
    actual = int(plan.execute(
        "SELECT COUNT(*) FROM requests WHERE output_path IS NOT NULL AND output_hash IS NOT NULL"
    ).fetchone()[0])
    if actual != expected:
        raise RuntimeError("export_output_validation_failed")
    cursor = plan.execute("SELECT output_path FROM requests ORDER BY output_path")
    for row in cursor:
        path = Path(str(row[0]))
        if (
            path.parent != Path(".")
            or len(path.name.encode("utf-8")) > MAX_EXPORT_BASENAME_BYTES
            or not (out_dir / path).is_file()
        ):
            raise RuntimeError("export_output_validation_failed")


_MANIFEST_FIELDS = [
    "aggregate_hash", "conversation_id", "create_time", "current_node", "format",
    "include_internal", "output_hash", "output_path", "path", "source_file",
    "title", "update_time",
]


def _iter_manifest_rows_from_plan(
    plan: sqlite3.Connection, *, path: str, include_internal: bool
) -> Iterator[dict[str, Any]]:
    cursor = plan.execute(
        """SELECT c.aggregate_hash, c.conversation_id, c.create_time, c.current_node,
                  r.format, r.output_hash, r.output_path, c.source_file, c.title,
                  c.update_time
           FROM requests AS r JOIN conversations AS c USING(conversation_id)
           ORDER BY r.output_path, c.conversation_id, r.format"""
    )
    for row in cursor:
        yield {
            "aggregate_hash": row["aggregate_hash"],
            "conversation_id": row["conversation_id"],
            "create_time": finite_float_or_none(row["create_time"]),
            "current_node": row["current_node"],
            "format": row["format"],
            "include_internal": include_internal,
            "output_hash": row["output_hash"],
            "output_path": row["output_path"],
            "path": path,
            "source_file": row["source_file"],
            "title": row["title"],
            "update_time": finite_float_or_none(row["update_time"]),
        }


def _iter_jsonl_manifest(
    plan: sqlite3.Connection, *, path: str, include_internal: bool
) -> Iterator[str]:
    for row in _iter_manifest_rows_from_plan(plan, path=path, include_internal=include_internal):
        yield json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ) + "\n"


def _iter_csv_manifest(
    plan: sqlite3.Connection, *, path: str, include_internal: bool
) -> Iterator[str]:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_MANIFEST_FIELDS, lineterminator="\n")
    writer.writeheader()
    yield buffer.getvalue()
    for row in _iter_manifest_rows_from_plan(plan, path=path, include_internal=include_internal):
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(row)
        yield buffer.getvalue()


def _files_equal(left: Path, right: Path, chunk_size: int = 1024 * 1024) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                left_chunk = a.read(chunk_size)
                if left_chunk != b.read(chunk_size):
                    return False
                if not left_chunk:
                    return True
    except (FileNotFoundError, IsADirectoryError, OSError):
        return False


class ManifestPairRecoveryError(RuntimeError):
    def __init__(self, code: str, diagnostics: list[dict[str, str]]) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics


def _write_manifest_from_plan(
    plan: sqlite3.Connection,
    out_dir: Path,
    *,
    path: str,
    include_internal: bool,
    force: bool,
) -> list[dict[str, str]]:
    candidates: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    pair_committed = False
    recovery_incomplete = False
    cleanup_warnings: list[dict[str, str]] = []

    def diagnostic(operation: str, exc: BaseException) -> dict[str, str]:
        return {"operation": operation, "error_type": type(exc).__name__}

    try:
        for target, chunks in (
            (out_dir / "manifest.jsonl", _iter_jsonl_manifest(plan, path=path, include_internal=include_internal)),
            (out_dir / "manifest.csv", _iter_csv_manifest(plan, path=path, include_internal=include_internal)),
        ):
            fd, candidate_name = tempfile.mkstemp(
                prefix=f".{target.name}.candidate-", suffix=".tmp", dir=out_dir
            )
            os.close(fd)
            candidate = Path(candidate_name)
            # Register before the first write so a generator/disk failure can
            # never strand a partially written candidate.
            candidates[target] = candidate
            write_chunks_if_changed(
                candidate, chunks, force=True, max_bytes=MAX_ARCHIVE_EXPORT_MANIFEST_BYTES
            )

        changed = {
            target for target, candidate in candidates.items()
            if force or not _files_equal(target, candidate)
        }
        ordered_targets = tuple(candidates)
        for target in ordered_targets:
            if target not in changed:
                continue
            if target.exists():
                backup_fd, backup_name = tempfile.mkstemp(
                    prefix=f".{target.name}.backup-", suffix=".tmp", dir=out_dir
                )
                os.close(backup_fd)
                os.unlink(backup_name)
                backup = Path(backup_name)
                os.replace(target, backup)
                backups[target] = backup
        for target in ordered_targets:
            if target not in changed:
                continue
            os.replace(candidates[target], target)
            published.append(target)
        pair_committed = True
        for backup in backups.values():
            try:
                os.unlink(backup)
            except OSError as exc:
                cleanup_warnings.append(diagnostic("backup_cleanup_pending", exc))
        for target, candidate in candidates.items():
            if target not in changed:
                try:
                    os.unlink(candidate)
                except OSError as exc:
                    cleanup_warnings.append(diagnostic("candidate_cleanup_pending", exc))
        return cleanup_warnings
    except BaseException as primary:
        recovery_errors: list[dict[str, str]] = []
        unlink_errors: dict[Path, dict[str, str]] = {}
        for target in published:
            try:
                os.unlink(target)
            except FileNotFoundError:
                continue
            except OSError as exc:
                unlink_errors[target] = diagnostic("rollback_target_unlink_failed", exc)
        for target, backup in backups.items():
            if backup.exists():
                try:
                    # os.replace also overwrites a published target whose
                    # explicit unlink failed, so that earlier cleanup error is
                    # not itself evidence of partial recovery.
                    os.replace(backup, target)
                    unlink_errors.pop(target, None)
                except OSError as exc:
                    recovery_errors.append(diagnostic("rollback_restore_failed", exc))
        # A newly created target has no backup that can overwrite it. Failure
        # to remove that target therefore leaves the pair potentially mixed.
        recovery_errors.extend(unlink_errors.values())
        if recovery_errors:
            recovery_incomplete = True
            raise ManifestPairRecoveryError(
                "manifest_pair_partial_recovery", recovery_errors
            ) from primary
        raise
    finally:
        for candidate in candidates.values():
            try:
                os.unlink(candidate)
            except OSError:
                pass
        if not pair_committed and not recovery_incomplete:
            for backup in backups.values():
                try:
                    os.unlink(backup)
                except OSError:
                    pass


def _record_archive_exports(
    conn: sqlite3.Connection,
    plan: sqlite3.Connection,
    out_dir: Path,
    *,
    path: str,
    include_internal: bool,
    from_date: str | None,
    to_date: str | None,
) -> None:
    options = {
        "current_path_only": path == "current",
        "path": path,
        "include_internal": include_internal,
        "from": from_date,
        "to": to_date,
        "deterministic_export": True,
    }
    cursor = plan.execute(
        "SELECT conversation_id, format, output_path, output_hash FROM requests ORDER BY conversation_id, format"
    )
    pending = 0
    try:
        for row in cursor:
            record_export(
                conn,
                str(row["conversation_id"]),
                str(row["format"]),
                out_dir / str(row["output_path"]),
                str(row["output_hash"]),
                options,
            )
            pending += 1
            if pending >= EXPORT_PLAN_BATCH_SIZE:
                conn.commit()
                pending = 0
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def build_filename_map(conversations: list[sqlite3.Row], formats: list[str]) -> dict[tuple[str, str], Path]:
    """Build a stable, globally collision-free plan for this export set.

    All natural names are reserved before collision suffixes are allocated.
    This prevents one collision group from generating ``_001`` over another
    conversation's natural basename.  Collision keys model common
    case-insensitive and Unicode-normalizing filesystems.
    """
    normalized_formats = sorted({str(fmt).lower() for fmt in formats})
    requests: list[tuple[str, str, sqlite3.Row, str, str]] = []
    natural_groups: dict[str, list[tuple[str, str, sqlite3.Row, str, str]]] = {}
    for conv in conversations:
        conversation_id = str(conv["conversation_id"])
        for fmt in normalized_formats:
            natural_name = _base_filename(conv, fmt)
            collision_key = _filename_collision_key(natural_name)
            request = (conversation_id, fmt, conv, natural_name, collision_key)
            requests.append(request)
            natural_groups.setdefault(collision_key, []).append(request)

    # Reserve every natural basename, including names from duplicate groups,
    # before generating any suffix.  Generated and natural names therefore
    # share one global namespace independent of input traversal order.
    reserved = set(natural_groups)
    allocated: set[str] = set()
    result: dict[tuple[str, str], Path] = {}
    for conversation_id, fmt, conv, natural_name, natural_key in sorted(
        requests,
        key=lambda item: (item[4], item[1], item[0]),
    ):
        if len(natural_groups[natural_key]) == 1:
            candidate = natural_name
        else:
            suffix_index = 1
            while True:
                candidate = _base_filename(conv, fmt, collision_suffix=f"_{suffix_index:03d}")
                candidate_key = _filename_collision_key(candidate)
                if candidate_key not in reserved and candidate_key not in allocated:
                    break
                suffix_index += 1
        candidate_key = _filename_collision_key(candidate)
        if candidate_key in allocated:
            raise ValueError("export_filename_plan_collision")
        allocated.add(candidate_key)
        result[(conversation_id, fmt)] = Path(candidate)
    return result


def _filename_collision_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _requested_export_pairs(
    conversations: list[sqlite3.Row], formats: list[str]
) -> set[tuple[str, str]]:
    return {
        (str(conv["conversation_id"]), str(fmt).lower())
        for conv in conversations
        for fmt in formats
    }


def _validate_filename_plan(
    filenames: dict[tuple[str, str], Path],
    conversations: list[sqlite3.Row],
    formats: list[str],
) -> None:
    requested = _requested_export_pairs(conversations, formats)
    if set(filenames) != requested:
        raise ValueError("export_filename_plan_pair_mismatch")
    paths = [path.as_posix() for path in filenames.values()]
    collision_keys = [_filename_collision_key(path) for path in paths]
    if len(paths) != len(set(collision_keys)):
        raise ValueError("export_filename_plan_collision")
    for (_conversation_id, fmt), path in filenames.items():
        if path.parent != Path(".") or path.suffix != f".{fmt}":
            raise ValueError("export_filename_plan_invalid_path")
        if len(path.name.encode("utf-8")) > MAX_EXPORT_BASENAME_BYTES:
            raise ValueError("export_filename_plan_too_long")


def _validate_export_outputs(
    out_dir: Path,
    manifest_rows: list[dict[str, Any]],
    conversations: list[sqlite3.Row],
    formats: list[str],
) -> None:
    requested = _requested_export_pairs(conversations, formats)
    manifest_pairs = {
        (str(row["conversation_id"]), str(row["format"]).lower())
        for row in manifest_rows
    }
    manifest_paths = [str(row["output_path"]) for row in manifest_rows]
    collision_keys = [_filename_collision_key(path) for path in manifest_paths]
    existing_outputs = {path for path in manifest_paths if (out_dir / path).is_file()}
    if (
        manifest_pairs != requested
        or len(manifest_rows) != len(requested)
        or len(collision_keys) != len(set(collision_keys))
        or len(existing_outputs) != len(requested)
    ):
        raise RuntimeError("export_output_validation_failed")


def _base_filename(conv: sqlite3.Row, fmt: str, *, collision_suffix: str = "") -> str:
    timestamp = conv["create_time"] if conv["create_time"] is not None else conv["update_time"]
    date_part = epoch_to_date_part(timestamp)
    title = safe_filename_part(conv["title"], max_len=2048)
    raw_cid = str(conv["conversation_id"])
    cid = safe_filename_part(raw_cid, max_len=512)
    if len(cid.encode("utf-8")) > 96:
        digest = sha256_text(raw_cid)[:16]
        cid = truncate_utf8(cid, 79).rstrip("._ ") + "_" + digest
    extension = f".{fmt}"
    fixed = f"{date_part}__{cid}{collision_suffix}{extension}"
    title_budget = MAX_EXPORT_BASENAME_BYTES - len(fixed.encode("utf-8"))
    title = truncate_utf8(title, max(0, title_budget)).rstrip("._ ")
    if not title:
        title = "untitled"
    filename = f"{date_part}_{title}_{cid}{collision_suffix}{extension}"
    if len(filename.encode("utf-8")) > MAX_EXPORT_BASENAME_BYTES:
        overflow = len(filename.encode("utf-8")) - MAX_EXPORT_BASENAME_BYTES
        title = truncate_utf8(title, max(0, len(title.encode("utf-8")) - overflow)).rstrip("._ ") or "u"
        filename = f"{date_part}_{title}_{cid}{collision_suffix}{extension}"
    return filename


def order_current_path(conv: sqlite3.Row, nodes: list[sqlite3.Row]) -> list[sqlite3.Row]:
    by_id = {str(row["node_id"]): row for row in nodes}
    collection = resolve_effective_current_collection(conv["current_node"], nodes)
    return [by_id[node_id] for node_id in collection.node_ids]


def order_export_path(conv: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]], path: str) -> list[Mapping[str, Any]]:
    if path == "current":
        by_id = {str(row["node_id"]): row for row in nodes}
        collection = resolve_effective_current_collection(conv["current_node"], list(nodes))
        return [by_id[node_id] for node_id in collection.node_ids]
    return sorted(
        nodes,
        key=lambda row: (
            row["create_time"] is None,
            row["create_time"] if row["create_time"] is not None else row["update_time"] if row["update_time"] is not None else 0,
            row["node_id"],
        ),
    )


def _resolved_export_node(node: Mapping[str, Any], *, include_internal: bool) -> dict[str, Any] | None:
    resolved = recover_message_display_text(
        _optional_row_value(node, "content_text"),
        _optional_row_value(node, "raw_message_json"),
    )
    if not resolved:
        return None
    if not include_internal and _is_internal_message(
        _optional_row_value(node, "role"),
        _optional_row_value(node, "content_type"),
        resolved,
    ):
        return None
    output = dict(node)
    output["content_text"] = resolved
    output["raw_message_json"] = None
    return output


def prepare_export_nodes(
    conv: Mapping[str, Any],
    nodes: Sequence[Mapping[str, Any]],
    *,
    path: str,
    include_internal: bool,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for node in order_export_path(conv, nodes, path):
        resolved = _resolved_export_node(node, include_internal=include_internal)
        if resolved is not None:
            prepared.append(resolved)
    return prepared


def _spool_export_nodes_for_batch(
    conn: sqlite3.Connection,
    plan: sqlite3.Connection,
    ids: Sequence[str],
    *,
    path: str,
) -> None:
    """Copy one bounded node batch to the disk-backed export plan in order."""

    plan.execute("DELETE FROM export_nodes")
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    missing_expr = "CASE WHEN n.create_time IS NULL THEN 1 ELSE 0 END"
    time_expr = (
        "CASE WHEN n.create_time IS NOT NULL THEN n.create_time "
        "WHEN n.update_time IS NOT NULL THEN n.update_time ELSE 0 END"
    )
    if path == "current":
        ensure_effective_current_views(conn, ids)
        rows = conn.execute(
            f"""SELECT n.conversation_id, n.node_id, n.role, n.content_type,
                       n.content_text, n.raw_message_json, n.create_time, n.update_time
                FROM effective_current_nodes e
                JOIN conversation_nodes n
                  ON n.conversation_id = e.conversation_id AND n.node_id = e.node_id
                WHERE e.conversation_id IN ({placeholders})
                ORDER BY n.conversation_id,
                         CASE WHEN e.source = 'fallback_all' THEN {missing_expr} ELSE 0 END,
                         CASE WHEN e.source = 'fallback_all' THEN {time_expr} ELSE -COALESCE(e.depth, 0) END,
                         n.node_id""",
            list(ids),
        )
    else:
        rows = conn.execute(
            f"""SELECT n.conversation_id, n.node_id, n.role, n.content_type,
                       n.content_text, n.raw_message_json, n.create_time, n.update_time
                FROM conversation_nodes n
                WHERE n.conversation_id IN ({placeholders})
                ORDER BY n.conversation_id, {missing_expr}, {time_expr}, n.node_id""",
            list(ids),
        )
    pending: list[tuple[Any, ...]] = []
    for row in rows:
        pending.append(tuple(row))
        if len(pending) >= 128:
            plan.executemany(
                """INSERT INTO export_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       raw_message_json, create_time, update_time
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                pending,
            )
            pending.clear()
    if pending:
        plan.executemany(
            """INSERT INTO export_nodes(
                   conversation_id, node_id, role, content_type, content_text,
                   raw_message_json, create_time, update_time
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            pending,
        )


def _iter_spooled_export_nodes(
    plan: sqlite3.Connection,
    conversation_id: str,
    *,
    include_internal: bool,
) -> Iterator[dict[str, Any]]:
    for row in plan.execute(
        "SELECT * FROM export_nodes WHERE conversation_id = ? ORDER BY sequence",
        (conversation_id,),
    ):
        resolved = _resolved_export_node(row, include_internal=include_internal)
        if resolved is not None:
            yield resolved


def iter_conversation_export_nodes(
    conn: sqlite3.Connection,
    conv: Mapping[str, Any],
    *,
    path: str,
    include_internal: bool,
    batch_size: int = EXPORT_NODE_BATCH_SIZE,
    validated_budget: ValidatedConversationExportBudget | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield complete export rows without accumulating reader page payloads."""

    if path not in {"current", "all"}:
        raise ValueError("invalid_export_path")
    conversation_id = str(conv["conversation_id"])
    if validated_budget is None:
        validated_budget = validate_conversation_export_budget(
            conn,
            conversation_id,
            path=path,
            include_internal=include_internal,
        )
    current_identity = _export_snapshot_identity(conn)
    if (
        validated_budget._seal is not _EXPORT_BUDGET_TOKEN_SEAL
        or validated_budget.contract_version != EXPORT_BUDGET_CONTRACT_VERSION
        or validated_budget.conversation_id != conversation_id
        or validated_budget.path != path
        or validated_budget.include_internal != bool(include_internal)
        or current_identity
        != (validated_budget.data_version, validated_budget.generation_snapshot)
    ):
        raise ExportResourceLimitError("export_budget_token_stale")
    batch_size = max(1, min(EXPORT_NODE_BATCH_SIZE, int(batch_size)))
    if path == "all":
        yield from _iter_all_export_nodes_keyset(
            conn,
            str(conv["conversation_id"]),
            include_internal=include_internal,
            batch_size=batch_size,
        )
        return
    ensure_effective_current_views(conn, [conversation_id])
    source_row = conn.execute(
        "SELECT source FROM effective_current_nodes WHERE conversation_id = ? LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if source_row is None:
        return
    if str(source_row[0]) == "fallback_all":
        yield from _iter_all_export_nodes_keyset(
            conn,
            conversation_id,
            include_internal=include_internal,
            batch_size=batch_size,
        )
        return
    yield from _iter_current_export_nodes_keyset(
        conn,
        conversation_id,
        include_internal=include_internal,
        batch_size=batch_size,
    )


def _iter_current_export_nodes_keyset(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    include_internal: bool,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    """Stream the disk-backed effective chain from root toward its leaf."""

    last_key: tuple[int, str] | None = None
    while True:
        predicate = ""
        params: list[Any] = [conversation_id]
        if last_key is not None:
            predicate = "AND (e.depth < ? OR (e.depth = ? AND e.node_id > ?))"
            params.extend((last_key[0], last_key[0], last_key[1]))
        params.append(batch_size)
        rows = conn.execute(
            f"""SELECT n.*, e.depth AS export_depth
                FROM effective_current_nodes e
                JOIN conversation_nodes n
                  ON n.conversation_id = e.conversation_id AND n.node_id = e.node_id
                WHERE e.conversation_id = ? AND e.depth IS NOT NULL {predicate}
                ORDER BY e.depth DESC, e.node_id
                LIMIT ?""",
            params,
        ).fetchall()
        if not rows:
            return
        for row in rows:
            resolved = _resolved_export_node(row, include_internal=include_internal)
            if resolved is not None:
                yield resolved
        tail = rows[-1]
        last_key = (int(tail["export_depth"]), str(tail["node_id"]))


def _iter_all_export_nodes_keyset(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    include_internal: bool,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    """Stream all-node display order with a bounded keyset page."""

    missing_expr = "CASE WHEN create_time IS NULL THEN 1 ELSE 0 END"
    time_expr = "CASE WHEN create_time IS NOT NULL THEN create_time WHEN update_time IS NOT NULL THEN update_time ELSE 0 END"
    last_key: tuple[int, Any, str] | None = None
    while True:
        params: list[Any] = [conversation_id]
        predicate = ""
        if last_key is not None:
            predicate = f"""AND (
                {missing_expr} > ? OR
                ({missing_expr} = ? AND {time_expr} > ?) OR
                ({missing_expr} = ? AND {time_expr} = ? AND node_id > ?)
            )"""
            params.extend([
                last_key[0], last_key[0], last_key[1],
                last_key[0], last_key[1], last_key[2],
            ])
        params.append(batch_size)
        rows = conn.execute(
            f"""SELECT *, {missing_expr} AS export_sort_missing,
                       {time_expr} AS export_sort_time
                FROM conversation_nodes
                WHERE conversation_id = ? {predicate}
                ORDER BY export_sort_missing, export_sort_time, node_id
                LIMIT ?""",
            params,
        ).fetchall()
        if not rows:
            return
        for row in rows:
            resolved = _resolved_export_node(row, include_internal=include_internal)
            if resolved is not None:
                yield resolved
        tail = rows[-1]
        last_key = (
            int(tail["export_sort_missing"]),
            tail["export_sort_time"],
            str(tail["node_id"]),
        )


def _optional_row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _markdown_header(conv: Mapping[str, Any]) -> str:
    # No exported_at here by design: default exported files must be byte-stable
    # for identical database contents and CLI parameters.
    return "\n".join([
        f"# {conv['title'] or 'untitled'}",
        "",
        f"- conversation_id: `{conv['conversation_id']}`",
        f"- create_time: {epoch_to_display(conv['create_time'])}",
        f"- update_time: {epoch_to_display(conv['update_time'])}",
        f"- current_node: `{conv['current_node'] or ''}`",
        f"- source_file: `{conv['source_file'] or ''}`",
        "",
    ])


def _markdown_node(node: Mapping[str, Any]) -> str:
    content_text = recover_message_display_text(
        _optional_row_value(node, "content_text"),
        _optional_row_value(node, "raw_message_json"),
    )
    if not content_text:
        return ""
    role = (node["role"] or "message").title()
    node_time = node["create_time"] if node["create_time"] is not None else node["update_time"]
    timestamp = epoch_to_display(node_time)
    heading = f"## {role}" + (f" {timestamp}" if timestamp else "")
    return "\n".join([heading, "", content_text, ""])


def _txt_header(conv: Mapping[str, Any]) -> str:
    return "\n".join([
        conv["title"] or "untitled",
        f"conversation_id: {conv['conversation_id']}",
        f"create_time: {epoch_to_display(conv['create_time'])}",
        f"update_time: {epoch_to_display(conv['update_time'])}",
        f"current_node: {conv['current_node'] or ''}",
        f"source_file: {conv['source_file'] or ''}",
        "=" * 72,
        "",
    ])


def _txt_node(node: Mapping[str, Any]) -> str:
    content_text = recover_message_display_text(
        _optional_row_value(node, "content_text"),
        _optional_row_value(node, "raw_message_json"),
    )
    if not content_text:
        return ""
    role = (node["role"] or "message").upper()
    node_time = node["create_time"] if node["create_time"] is not None else node["update_time"]
    timestamp = epoch_to_display(node_time)
    return "\n".join([f"{role} {timestamp}".strip(), "-" * 72, content_text, ""])


def iter_rendered_conversation(
    conv: Mapping[str, Any],
    nodes: Iterable[Mapping[str, Any]],
    fmt: str,
) -> Iterator[str]:
    header = _markdown_header(conv) if fmt == "md" else _txt_header(conv)
    render_node = _markdown_node if fmt == "md" else _txt_node
    pending = header
    for node in nodes:
        fragment = render_node(node)
        if not fragment:
            continue
        yield from _bounded_text_chunks(pending)
        pending = fragment
    yield from _bounded_text_chunks(pending.rstrip() + "\n")


def _bounded_text_chunks(text: str, max_chars: int = 65_536) -> Iterator[str]:
    for offset in range(0, len(text), max_chars):
        yield text[offset : offset + max_chars]


def render_markdown(conv: Mapping[str, Any], nodes: Iterable[Mapping[str, Any]]) -> str:
    return "".join(iter_rendered_conversation(conv, nodes, "md"))


def render_txt(conv: Mapping[str, Any], nodes: Iterable[Mapping[str, Any]]) -> str:
    return "".join(iter_rendered_conversation(conv, nodes, "txt"))


def iter_copy_conversation(nodes: Iterable[Mapping[str, Any]]) -> Iterator[str]:
    first = True
    for node in nodes:
        content_text = recover_message_display_text(
            _optional_row_value(node, "content_text"),
            _optional_row_value(node, "raw_message_json"),
        )
        if not content_text or not content_text.strip():
            continue
        if not first:
            yield "\n\n"
        first = False
        yield f"{node['role'] or 'message'}:\n{content_text}"
