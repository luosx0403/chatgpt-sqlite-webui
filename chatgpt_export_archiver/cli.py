from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from .db import (
    DatabaseMigrationError,
    begin_bulk_generation_aggregation,
    begin_import_run,
    configure_import_connection,
    connect,
    connect_existing,
    connect_existing_readonly,
    database_schema_status,
    drop_optional_web_indexes,
    drop_import_rebuildable_indexes,
    finish_import_run,
    finish_bulk_generation_aggregation,
    get_stats,
    init_db,
    legacy_compatibility_state,
    mark_legacy_compatibility_current,
    migrate_database,
    optimize_after_import,
    read_snapshot,
    recreate_import_rebuildable_indexes,
    require_current_database_schema,
    record_source_entries,
    record_warning,
    record_warnings,
    rebuild_message_fts,
    update_import_run_summary,
    upsert_conversations_batch,
    validate_optional_web_index_ownership,
    verify_database,
)
from .exporter import export_conversations
from .disk_resources import (
    DiskSpaceGuard,
    DiskSpaceInsufficientError,
    import_capacity_plan,
    import_required_bytes,
    is_disk_full_error,
    require_free_space,
)
from .logging_utils import configure_logging, get_logger
from .json_safety import JsonSafetyLimitError
from .parser import (
    MAX_IMPORT_NODES_PER_CONVERSATION,
    WarningRecord,
    conversation_id_from_value,
    parse_conversation,
    validate_conversation_element,
)
from .resource_contract import (
    IMPORT_BATCH_MAX_CONVERSATIONS,
    IMPORT_BATCH_MAX_DECODED_CHARS,
    IMPORT_BATCH_MAX_ESTIMATED_HEAP_BYTES,
    IMPORT_BATCH_MAX_INPUT_BYTES,
    IMPORT_BATCH_MAX_METADATA_BYTES,
    IMPORT_BATCH_MAX_NODES,
    IMPORT_BATCH_MAX_RAW_BYTES,
    IMPORT_BATCH_MAX_SQLITE_BIND_BYTES,
    import_batch_resource_profile,
)
from .scanner import (
    ConversationJsonTopLevelError,
    ConversationJsonElementTooLargeError,
    DeleteInputRecoveryRequired,
    EncryptedZipMemberError,
    InputSource,
    InvalidConversationEncodingError,
    JsonIntegerTooLargeError,
    MAX_JSON_ARRAY_ITEMS,
    MAX_JSON_ELEMENT_BYTES,
    MAX_JSON_ELEMENT_CHARS,
    MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES,
    MAX_JSON_INTEGER_DIGITS,
    MAX_JSON_MAPPING_ENTRIES,
    MAX_JSON_NESTING_DEPTH,
    MAX_JSON_STRING_PRIMITIVE_TOKENS,
    NonFiniteJsonNumberError,
    SourceChangedDuringReadError,
    ZipMemberCrcError,
    ZipMemberNotFoundError,
    ZipMemberReadError,
    delete_input_if_unchanged,
    delete_input_identity_is_current,
    delete_input_secure_identity_supported,
    is_legacy_conversations_source,
    is_shard_conversation_source,
    iter_source_array_sessions,
    list_source_entries,
    recover_delete_input,
    resolve_input,
    select_conversation_sources,
    sha256_input_source,
)
from .sqlite_errors import sqlite_runtime_error_code
from .utils import compact_json, epoch_to_display, sha256_text
from .web_db import WebIndexBuildError, acquire_writer_process_lock, create_web_indexes

LOGGER = get_logger("cli")


def _close_writer_lock_best_effort(writer_lock: Any) -> list[dict[str, str]]:
    try:
        writer_lock.close()
    except WebIndexBuildError as exc:
        return [dict(item) for item in exc.cleanup_warnings]
    return []
REQUIRED_IMPORT_GENERATION_DOMAINS = ("title", "message", "address", "graph", "query")


class ImportPipelineError(ValueError):
    """Safe structured failure shared by CLI and Web import jobs."""

    def __init__(
        self,
        code: str,
        *,
        stage: str,
        source_identifier: str = "input",
        run_id: int | None = None,
        summary: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.source_identifier = source_identifier
        self.run_id = run_id
        self.summary = dict(summary) if summary else None
        self.detail = dict(detail) if detail else {}
        self.failure_persistence_failed = False
        self.failure_persistence_error_type: str | None = None

    def __str__(self) -> str:
        parts = [self.code, f"stage={self.stage}"]
        if self.run_id is not None:
            parts.append(f"import_run_id={self.run_id}")
        if self.failure_persistence_failed:
            parts.extend(
                [
                    "failure_persistence_failed=true",
                    f"failure_persistence_error_type={self.failure_persistence_error_type or 'UnknownError'}",
                    f"original_failure_code={self.code}",
                    f"original_failure_stage={self.stage}",
                ]
            )
        return " ".join(parts)


def main(
    argv: list[str] | None = None,
    *,
    _process_started: float | None = None,
) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if _process_started is not None:
        args._process_started = _process_started
    try:
        configure_logging(
            args.log_level,
            file_path=Path(args.log_file) if args.log_file else None,
            json_logs=args.json_logs,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except DatabaseMigrationError as exc:
        print(f"ERROR: {exc.code}", file=sys.stderr)
        for key in (
            "current_database_schema_version",
            "required_database_schema_version",
            "required_bytes",
            "free_bytes",
            "estimated_peak_bytes",
        ):
            if key in exc.detail:
                print(f"{key} {exc.detail[key]}", file=sys.stderr)
        for key in (
            "missing_tables",
            "missing_indexes",
            "missing_triggers",
            "missing_generation_rows",
        ):
            values = exc.detail.get(key)
            if values:
                print(f"{key} {','.join(str(value) for value in values)}", file=sys.stderr)
        for key in (
            "missing_columns",
            "invalid_tables",
            "invalid_indexes",
            "invalid_triggers",
            "invalid_generation_rows",
            "object_type_mismatches",
            "missing_foreign_keys",
        ):
            values = exc.detail.get(key)
            if values:
                print(f"{key} {','.join(sorted(str(value) for value in values))}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"ERROR: {sqlite_runtime_error_code(exc)} error_type={type(exc).__name__}", file=sys.stderr)
        return 2
    except WebIndexBuildError as exc:
        print(f"ERROR: {exc.code}", file=sys.stderr)
        for warning in exc.cleanup_warnings:
            print(
                "cleanup_warning "
                f"code={warning['code']} error_type={warning['error_type']} "
                f"path_kind={warning['path_kind']}",
                file=sys.stderr,
            )
        return 2
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _configure_stdio() -> None:
    """Avoid UnicodeEncodeError for structural path output on non-UTF-8 consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (TypeError, ValueError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive OpenAI/ChatGPT export ZIPs into SQLite and export Markdown/TXT.")

    def _add_log_args(subparser: argparse.ArgumentParser) -> None:
        """Add --log-level / --log-file / --json-logs to a subparser.

        Uses default=argparse.SUPPRESS so that when these args are not
        present after the subcommand, the subparser does **not** overwrite
        the values already set by the parent parser (or its defaults).
        """
        subparser.add_argument(
            "--log-level", default=argparse.SUPPRESS,
            choices=["debug", "info", "warning", "error", "none"],
            help="Project log verbosity.")
        subparser.add_argument(
            "--log-file", default=argparse.SUPPRESS,
            help="Write project logs to this file instead of stderr.")
        subparser.add_argument(
            "--json-logs", default=argparse.SUPPRESS, action="store_true",
            help="Write project logs as JSON lines.")

    parser.add_argument("--db", default="archive/chatgpt_archive.db", help="SQLite database path.")
    parser.add_argument("--log-level", default="warning", choices=["debug", "info", "warning", "error", "none"], help="Project log verbosity.")
    parser.add_argument("--log-file", help="Write project logs to this file instead of stderr.")
    parser.add_argument("--json-logs", action="store_true", help="Write project logs as JSON lines.")
    sub = parser.add_subparsers(required=True)

    inspect_p = sub.add_parser("inspect", help="Inspect a ZIP or extracted directory without printing chat content.")
    inspect_p.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    inspect_p.add_argument("--input", help="Export ZIP or extracted directory. Defaults to the only ZIP in cwd.")
    inspect_p.set_defaults(func=cmd_inspect)
    _add_log_args(inspect_p)

    init_p = sub.add_parser("init", help="Initialize the SQLite database.")
    init_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    init_p.set_defaults(func=cmd_init)
    _add_log_args(init_p)

    migrate_p = sub.add_parser(
        "migrate",
        help="Upgrade an existing database schema after making an external backup.",
    )
    migrate_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    migrate_p.set_defaults(func=cmd_migrate)
    _add_log_args(migrate_p)

    import_p = sub.add_parser("import", help="Import conversations into SQLite.")
    import_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    import_p.add_argument("--input", help="Export ZIP or extracted directory. Defaults to the only ZIP in cwd.")
    import_p.add_argument("--no-input-sha256", action="store_true", help="Skip hashing the input ZIP/directory.")
    import_p.add_argument("--rebuild-fts", action="store_true", help="Rebuild message_fts once after importing instead of maintaining it per conversation.")
    import_p.add_argument("--optimize-after-import", action="store_true", help="Run PRAGMA optimize after a successful import.")
    import_p.add_argument("--optimize-fts-after-import", action="store_true", help="Run FTS5 optimize after --rebuild-fts. Can be slow on large archives.")
    import_p.add_argument("--delete-input-on-success", action="store_true", help="Permanently delete the input ZIP after a successful import.")
    import_p.set_defaults(func=cmd_import)
    _add_log_args(import_p)

    export_p = sub.add_parser("export", help="Export conversations from SQLite as Markdown and/or TXT.")
    export_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_p.add_argument("--format", choices=["md", "txt", "all"], default="md")
    export_p.add_argument("--out", default="exports")
    export_p.add_argument("--from", dest="from_date", help="Conversation create/update date lower bound YYYY-MM-DD.")
    export_p.add_argument("--to", dest="to_date", help="Conversation create/update date upper bound YYYY-MM-DD.")
    export_p.add_argument("--path", choices=["current", "all"], default="current", help="Export the effective current path (default) or every mapping node.")
    export_p.add_argument("--include-internal", action="store_true", help="Include system, developer, tool, technical, and other internal messages. Default exports visible messages only.")
    export_p.add_argument("--force", action="store_true", help="Rewrite files even if content hash is unchanged.")
    export_p.set_defaults(func=cmd_export)
    _add_log_args(export_p)

    stats_p = sub.add_parser("stats", help="Show database statistics without chat content.")
    stats_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    stats_p.set_defaults(func=cmd_stats)
    _add_log_args(stats_p)

    verify_p = sub.add_parser("verify", help="Check database consistency.")
    verify_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    verify_p.set_defaults(func=cmd_verify)
    _add_log_args(verify_p)

    search_p = sub.add_parser(
        "search",
        help="Search messages with the project query syntax. Prints IDs and roles, not snippets.",
        description="Search messages with the project query syntax. Prints IDs and roles, not snippets.",
    )
    search_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    search_p.add_argument("query")
    search_p.add_argument("--limit", type=int, default=20)
    search_p.set_defaults(func=cmd_search)
    _add_log_args(search_p)

    web_p = sub.add_parser("web", help="Start the local browser Web UI.")
    web_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    web_p.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    web_p.add_argument(
        "--allowed-hosts",
        help="Comma-separated exact browser hostnames/IPs. Required for non-loopback binds; also configurable with CHATGPT_ARCHIVE_ALLOWED_HOSTS.",
    )
    web_p.add_argument(
        "--trusted-proxies",
        help="Comma-separated proxy IPs/CIDRs allowed to supply forwarded host/proto headers. Defaults to CHATGPT_ARCHIVE_TRUSTED_PROXIES.",
    )
    web_p.add_argument("--port", type=int, default=8787, help="Bind port.")
    web_p.add_argument("--allow-fallback", action="store_true", help="Allow the limited fallback HTML UI if the React build is missing.")
    web_p.set_defaults(func=cmd_web)
    _add_log_args(web_p)

    web_index_p = sub.add_parser("web-index", help="Build optional Web substring search indexes.")
    web_index_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    web_index_p.set_defaults(func=cmd_web_index)
    _add_log_args(web_index_p)

    recover_delete_p = sub.add_parser(
        "recover-delete-input",
        help="Restore a crash-left delete-input staging entry using its recovery token.",
    )
    recover_delete_p.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    recover_delete_p.add_argument("--directory", required=True, help="Directory containing the recovery record.")
    recover_delete_p.add_argument("--token", required=True, help="32-character recovery token.")
    recover_delete_p.set_defaults(func=cmd_recover_delete_input)
    _add_log_args(recover_delete_p)
    return parser


def cmd_init(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    writer_lock = acquire_writer_process_lock(db_path)
    try:
        conn = connect(db_path)
        try:
            writer_lock.bind_database(db_path)
            writer_lock.revalidate(db_path)
            fts = init_db(conn)
        finally:
            conn.close()
    finally:
        cleanup_warnings = _close_writer_lock_best_effort(writer_lock)
        for warning in cleanup_warnings:
            print(
                f"WARNING: {warning['code']} error_type={warning['error_type']}",
                file=sys.stderr,
            )
    print("initialized_db true")
    print(f"fts5_available {str(fts).lower()}")
    return 0


def cmd_recover_delete_input(args: argparse.Namespace) -> int:
    status = recover_delete_input(Path(args.directory), args.token)
    print(f"delete_input_recovery_status {status}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    writer_lock = acquire_writer_process_lock(db_path)
    try:
        conn = connect_existing(db_path)
        try:
            writer_lock.bind_database(db_path)
            writer_lock.revalidate(db_path)
            before = database_schema_status(conn)
            print("WARNING: create and verify an external database backup before migration.", file=sys.stderr)
            def migration_progress(stage: str, progress: dict[str, int]) -> None:
                print(
                    f"migration_stage {stage} "
                    f"processed {progress.get('processed', 0)} "
                    f"total {progress.get('total', 0)}",
                    file=sys.stderr,
                )

            result = migrate_database(
                conn,
                refresh_compatibility=True,
                progress_callback=migration_progress,
            )
        finally:
            conn.close()
    finally:
        cleanup_warnings = _close_writer_lock_best_effort(writer_lock)
        for warning in cleanup_warnings:
            print(
                f"WARNING: {warning['code']} error_type={warning['error_type']}",
                file=sys.stderr,
            )
    print(f"current_database_schema_version {before['current_database_schema_version']}")
    print(f"required_database_schema_version {before['required_database_schema_version']}")
    print(f"schema_changed {str(bool(result['schema_changed'])).lower()}")
    print(f"compatibility_refreshed {str(bool(result['compatibility_refreshed'])).lower()}")
    print(f"compatibility_changed {str(bool(result['compatibility_changed'])).lower()}")
    print(f"migration_changed {str(bool(result['migration_changed'])).lower()}")
    print(f"database_schema_version {result['current_database_schema_version']}")
    if result.get("migration_disk_preflight"):
        print(
            "migration_disk_preflight_required_bytes "
            f"{result['migration_disk_preflight']['required_bytes']}"
        )
        print(
            "migration_disk_preflight_free_bytes "
            f"{result['migration_disk_preflight']['free_bytes']}"
        )
    print("backup_created false")
    return 0


def _classify_source_load_error(exc: BaseException) -> tuple[str, str]:
    """Map parser/read failures to stable public codes without OS details."""

    if isinstance(exc, JsonIntegerTooLargeError):
        return "json_integer_too_large", "json_decode"
    if isinstance(exc, ConversationJsonElementTooLargeError):
        return "conversation_json_element_too_large", "json_decode"
    if isinstance(exc, JsonSafetyLimitError):
        return exc.code, "json_decode"
    if isinstance(exc, NonFiniteJsonNumberError):
        return "non_finite_json_number", "json_decode"
    if isinstance(exc, (UnicodeDecodeError, InvalidConversationEncodingError)):
        return "invalid_conversation_encoding", "json_decode"
    if isinstance(exc, EncryptedZipMemberError):
        return "encrypted_zip_member_not_supported", "source_read"
    if isinstance(exc, ZipMemberNotFoundError):
        return "zip_member_not_found", "source_read"
    if isinstance(exc, SourceChangedDuringReadError):
        return "source_changed_during_read", "source_read"
    if isinstance(exc, ZipMemberCrcError):
        return "zip_member_crc_failed", "source_read"
    if isinstance(exc, ZipMemberReadError):
        return "zip_member_read_failed", "source_read"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_conversation_json", "json_decode"
    raw_code = str(exc).split(" ", 1)[0] if isinstance(exc, ValueError) else ""
    if raw_code == "source_changed_during_read":
        return raw_code, "source_read"
    if raw_code == "input_source_open_failed":
        return raw_code, "source_read"
    if raw_code == "input_source_not_regular_file":
        return raw_code, "source_read"
    if raw_code in {"input_symlink_not_allowed", "input_source_outside_root", "source_not_found"}:
        return "source_changed_during_read", "source_read"
    return "source_read_failed", "source_read"


def cmd_inspect(args: argparse.Namespace) -> int:
    source = resolve_input(args.input, Path.cwd())
    entries = list_source_entries(source)
    selected = select_conversation_sources(entries)
    type_counts = collections.Counter(e.file_type for e in entries)
    total_size = sum(e.size for e in entries)
    legacy_count = sum(1 for e in entries if is_legacy_conversations_source(e.source_path))
    shard_count = sum(1 for e in entries if is_shard_conversation_source(e.source_path))
    print(f"input_kind {source.kind}")
    print(f"input_size {source.size}")
    print(f"uncompressed_or_directory_size {total_size}")
    print(f"file_type_counts {dict(type_counts)}")
    print(f"conversation_json_files {legacy_count + shard_count}")
    print(f"selected_conversation_sources {len(selected)}")
    print(f"sharded {str(bool(shard_count)).lower()}")
    if shard_count and legacy_count:
        print("legacy_conversations_json_ignored true")

    # Keep exact duplicate detection on disk instead of retaining every ID as a
    # Python string.  An empty SQLite pathname creates a private temporary
    # database that is removed when the connection closes.
    inspect_ids = sqlite3.connect("")
    inspect_ids.execute("PRAGMA journal_mode = OFF")
    inspect_ids.execute("PRAGMA synchronous = OFF")
    inspect_ids.execute("PRAGMA cache_size = -2048")
    inspect_ids.execute(
        "CREATE TABLE inspect_conversation_ids (conversation_id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    invalid_locations: list[tuple[str, int, str]] = []
    invalid_count = 0
    id_count = 0
    valid_count = 0
    top_level_bad = 0
    try:
        for source_index, entry, source_items in iter_source_array_sessions(source, selected):
            source_valid = 0
            source_invalid = 0
            source_id_count = 0
            source_invalid_locations: list[tuple[str, int, str]] = []
            savepoint = f"inspect_source_{source_index}"
            inspect_ids.execute(f"SAVEPOINT {savepoint}")
            try:
                for idx, item in enumerate(source_items):
                    warning = validate_conversation_element(item, entry.source_path, idx)
                    if warning:
                        source_invalid += 1
                        if len(source_invalid_locations) < 50:
                            source_invalid_locations.append(
                                (Path(entry.source_path).name, idx, warning.warning_type)
                            )
                        continue
                    source_valid += 1
                    conversation_id = conversation_id_from_value(item)
                    if conversation_id is not None:
                        source_id_count += 1
                        inspect_ids.execute(
                            "INSERT OR IGNORE INTO inspect_conversation_ids(conversation_id) VALUES (?)",
                            (conversation_id,),
                        )
            except ConversationJsonTopLevelError as exc:
                inspect_ids.execute(f"ROLLBACK TO {savepoint}")
                inspect_ids.execute(f"RELEASE {savepoint}")
                top_level_bad += 1
                print(f"source {Path(entry.source_path).name} top_level {exc.top_level_type} valid 0 invalid 0")
                continue
            except (json.JSONDecodeError, NonFiniteJsonNumberError, JsonIntegerTooLargeError, InvalidConversationEncodingError, OSError, ValueError) as exc:
                inspect_ids.execute(f"ROLLBACK TO {savepoint}")
                inspect_ids.execute(f"RELEASE {savepoint}")
                top_level_bad += 1
                code, stage = _classify_source_load_error(exc)
                print(
                    f"source {Path(entry.source_path).name} top_level invalid_json "
                    f"valid 0 invalid 0 error_code {code} stage {stage}"
                )
                continue
            inspect_ids.execute(f"RELEASE {savepoint}")
            valid_count += source_valid
            id_count += source_id_count
            invalid_count += source_invalid
            if len(invalid_locations) < 50:
                invalid_locations.extend(
                    source_invalid_locations[: 50 - len(invalid_locations)]
                )
            print(f"source {Path(entry.source_path).name} top_level list valid {source_valid} invalid {source_invalid}")
        unique_id_count = int(
            inspect_ids.execute("SELECT COUNT(*) FROM inspect_conversation_ids").fetchone()[0]
        )
    finally:
        inspect_ids.close()
    duplicate_count = id_count - unique_id_count
    print(f"valid_conversations {valid_count}")
    print(f"invalid_elements {invalid_count}")
    for filename, idx, warning_type in invalid_locations:
        print(f"invalid_element source_file={filename} index={idx} warning_type={warning_type}")
    if invalid_count > 50:
        print(f"invalid_element_more {invalid_count - 50}")
    print(f"duplicate_conversation_ids {duplicate_count}")
    print(f"bad_top_level_files {top_level_bad}")
    return 2 if top_level_bad else 0


def cmd_import(args: argparse.Namespace) -> int:
    cli_started = float(getattr(args, "_process_started", time.perf_counter()))
    result = run_import_pipeline(
        Path(args.db),
        args.input,
        cwd=Path.cwd(),
        no_input_sha256=args.no_input_sha256,
        rebuild_fts=args.rebuild_fts,
        optimize_after_import_flag=args.optimize_after_import,
        optimize_fts_after_import=args.optimize_fts_after_import,
        delete_input_on_success=args.delete_input_on_success,
    )
    summary = result["summary"]
    for key in (
        "import_run_id",
        "source",
        "attempted_valid_conversations",
        "attempted_nodes",
        "attempted_inserted_conversations",
        "attempted_updated_conversations",
        "attempted_unchanged_conversations",
        "committed_conversations",
        "committed_nodes",
        "valid_conversations",
        "nodes",
        "warnings",
        "skipped_invalid_elements",
        "unchanged_conversations",
        "inserted_conversations",
        "updated_conversations",
        "source_scan_seconds",
        "parse_and_upsert_seconds",
        "fts_rebuild_seconds",
        "import_index_rebuild_seconds",
        "pragma_optimize_seconds",
        "finalize_commit_seconds",
        "close_seconds",
        "post_commit_summary_persistence_seconds",
        "canonical_pipeline_seconds",
        "pipeline_return_seconds",
        "legacy_pre_commit_seconds",
        "wall_total_seconds",
        "total_import_seconds",
    ):
        print(f"{key} {summary[key]}")
    if result.get("summary_update_after_commit_failed"):
        print(f"summary_update_after_commit_failed {result['summary_update_after_commit_failed']}")
    if result.get("import_connection_close_failed"):
        print(f"import_connection_close_failed {result['import_connection_close_failed']}")
    if result.get("summary_update_after_close_failed"):
        print(f"summary_update_after_close_failed {result['summary_update_after_close_failed']}")
    if summary.get("rebuild_fts"):
        print(f"rebuild_fts {str(bool(summary.get('rebuild_fts'))).lower()}")
        print(f"optimize_fts_after_import {str(bool(summary.get('optimize_fts_after_import'))).lower()}")
    if summary.get("optimize_after_import"):
        print(f"optimize_after_import {str(bool(summary.get('optimize_after_import'))).lower()}")
    if result.get("delete_input_on_success"):
        print("delete_input_on_success true")
        if result.get("deleted_input"):
            print(f"deleted_input {result['deleted_input']}")
        elif result.get("delete_input_failed"):
            print(f"delete_input_failed {result['delete_input_failed']}")
            print(f"delete_input_error_type {result['delete_input_error_type']}")
            if result.get("delete_input_recovery_required"):
                print("delete_input_recovery_required true")
                if result.get("delete_input_recovery_token"):
                    print(
                        "delete_input_recovery_token "
                        f"{result['delete_input_recovery_token']}"
                    )
                    print(
                        "delete_input_recovery_journal_format_version "
                        f"{result['delete_input_recovery_journal_format_version']}"
                    )
                    print(
                        "delete_input_recovery_command "
                        "recover-delete-input --directory <original-directory> "
                        f"--token {result['delete_input_recovery_token']}"
                    )
    output_flush_started = time.perf_counter()
    sys.stdout.flush()
    sys.stderr.flush()
    output_flush_seconds = _elapsed(output_flush_started)
    print(f"cli_output_flush_seconds {output_flush_seconds}")
    print(f"cli_controlled_wall_seconds {_elapsed(cli_started)}")
    sys.stdout.flush()
    return 0


def run_import_pipeline(
    db_path: Path,
    input_value: str | None,
    *,
    cwd: Path,
    no_input_sha256: bool = False,
    rebuild_fts: bool = False,
    optimize_after_import_flag: bool = False,
    optimize_fts_after_import: bool = False,
    delete_input_on_success: bool = False,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    writer_lock: Any | None = None,
) -> dict[str, Any]:
    """Serialize canonical import against optional Web-index builders."""

    pipeline_started = time.perf_counter()
    if optimize_fts_after_import and not rebuild_fts:
        raise ValueError("--optimize-fts-after-import requires --rebuild-fts")
    source = resolve_input(input_value, cwd)
    if delete_input_on_success and source.kind != "zip":
        raise ValueError("--delete-input-on-success is only supported for ZIP inputs")
    if delete_input_on_success and not delete_input_secure_identity_supported():
        raise ValueError("delete_input_secure_identity_unsupported")
    if delete_input_on_success and not delete_input_identity_is_current(source):
        raise SourceChangedDuringReadError("source_changed_during_read")
    db_path = db_path.expanduser()
    if db_path.exists() and (db_path.is_symlink() or not db_path.is_file()):
        raise ImportPipelineError("database_target_invalid", stage="input_preflight")
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImportPipelineError(
            "database_parent_create_failed",
            stage="input_preflight",
            detail={"error_type": type(exc).__name__},
        ) from None
    owns_writer_lock = writer_lock is None
    if writer_lock is None:
        try:
            writer_lock = acquire_writer_process_lock(db_path)
        except WebIndexBuildError as exc:
            raise ImportPipelineError(exc.code, stage="input_preflight") from None
    result: dict[str, Any] | None = None
    caught_error: BaseException | None = None
    writer_cleanup_warnings: list[dict[str, str]] = []
    try:
        writer_lock.bind_database(db_path)
        writer_lock.revalidate(db_path)
        result = _run_import_pipeline_locked(
            db_path,
            input_value,
            cwd=cwd,
            no_input_sha256=no_input_sha256,
            rebuild_fts=rebuild_fts,
            optimize_after_import_flag=optimize_after_import_flag,
            optimize_fts_after_import=optimize_fts_after_import,
            delete_input_on_success=delete_input_on_success,
            progress_callback=progress_callback,
            _resolved_source=source,
            _writer_lock=writer_lock,
        )
    except BaseException as exc:
        caught_error = exc
    finally:
        if owns_writer_lock:
            writer_cleanup_warnings = _close_writer_lock_best_effort(writer_lock)
    pipeline_seconds = _elapsed(pipeline_started)
    if caught_error is not None:
        if writer_cleanup_warnings:
            existing = list(getattr(caught_error, "cleanup_warnings", []))
            setattr(
                caught_error,
                "cleanup_warnings",
                [*existing, *writer_cleanup_warnings],
            )
        if isinstance(caught_error, ImportPipelineError):
            caught_error.summary["pipeline_return_seconds"] = pipeline_seconds
            caught_error.summary["wall_total_seconds"] = pipeline_seconds
            caught_error.summary["total_import_seconds"] = pipeline_seconds
        raise caught_error
    if result is None:
        raise RuntimeError("import pipeline returned no result")
    if writer_cleanup_warnings:
        result["writer_lock_cleanup_warnings"] = writer_cleanup_warnings
    summary = result["summary"]
    summary["pipeline_return_seconds"] = pipeline_seconds
    summary["wall_total_seconds"] = pipeline_seconds
    summary["total_import_seconds"] = pipeline_seconds
    return result


def _run_import_pipeline_locked(
    db_path: Path,
    input_value: str | None,
    *,
    cwd: Path,
    no_input_sha256: bool = False,
    rebuild_fts: bool = False,
    optimize_after_import_flag: bool = False,
    optimize_fts_after_import: bool = False,
    delete_input_on_success: bool = False,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    _resolved_source: InputSource | None = None,
    _writer_lock: Any | None = None,
) -> dict[str, Any]:
    """Import a ZIP/directory and return structural summary without printing chat content."""
    import_started = time.perf_counter()
    source = _resolved_source if _resolved_source is not None else resolve_input(input_value, cwd)
    if delete_input_on_success and source.kind != "zip":
        raise ValueError("--delete-input-on-success is only supported for ZIP inputs")
    if delete_input_on_success and not delete_input_secure_identity_supported():
        raise ValueError("delete_input_secure_identity_unsupported")
    if delete_input_on_success and not delete_input_identity_is_current(source):
        raise SourceChangedDuringReadError("source_changed_during_read")
    reported_source = source
    LOGGER.info("import_start input_kind=%s input_size=%s", source.kind, source.size)
    conn: sqlite3.Connection | None = None
    bound_source_sha256: str | None = None
    try:
        conn = connect(db_path)
        if _writer_lock is not None:
            _writer_lock.bind_database(db_path)
            _writer_lock.revalidate(db_path)
        configure_import_connection(conn)
        init_db(conn)
        compatibility = legacy_compatibility_state(conn)
        if compatibility["state"] != "compatible":
            raise ImportPipelineError(
                "database_data_incompatible",
                stage="input_preflight",
                detail={"compatibility_state": compatibility["state"]},
            )
        conn.execute("BEGIN IMMEDIATE")
        validate_optional_web_index_ownership(conn)
        conn.commit()
        if source.kind == "zip" and (not no_input_sha256 or delete_input_on_success):
            bound_source_sha256 = sha256_input_source(source)
        input_sha = None if no_input_sha256 else bound_source_sha256
        run_id = begin_import_run(conn, reported_source, input_sha)
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise
    summary: dict[str, Any] = {
        "import_run_id": run_id,
        "source": source.kind,
        "valid_conversations": 0,
        "nodes": 0,
        "attempted_valid_conversations": 0,
        "attempted_nodes": 0,
        "attempted_inserted_conversations": 0,
        "attempted_updated_conversations": 0,
        "attempted_unchanged_conversations": 0,
        "committed_conversations": 0,
        "committed_nodes": 0,
        "warnings": 0,
        "skipped_invalid_elements": 0,
        "unchanged_conversations": 0,
        "inserted_conversations": 0,
        "updated_conversations": 0,
        "source_scan_seconds": 0.0,
        "parse_and_upsert_seconds": 0.0,
        "fts_rebuild_seconds": 0.0,
        "import_index_rebuild_seconds": 0.0,
        "pragma_optimize_seconds": 0.0,
        "finalize_commit_seconds": 0.0,
        "close_seconds": 0.0,
        "post_commit_summary_persistence_seconds": 0.0,
        "canonical_pipeline_seconds": 0.0,
        "pipeline_return_seconds": 0.0,
        "legacy_pre_commit_seconds": 0.0,
        "wall_total_seconds": 0.0,
        "total_import_seconds": 0.0,
        "import_batch_count": 0,
        "import_batch_total_input_bytes": 0,
        "import_batch_peak_input_bytes": 0,
        "import_batch_peak_decoded_chars": 0,
        "import_batch_peak_nodes": 0,
        "import_batch_peak_raw_bytes": 0,
        "import_batch_peak_metadata_bytes": 0,
        "import_batch_peak_estimated_heap_bytes": 0,
        "import_batch_peak_sqlite_bind_bytes": 0,
        "json_resource_profile": {
            "max_element_utf8_bytes": MAX_JSON_ELEMENT_BYTES,
            "max_element_decoded_chars": MAX_JSON_ELEMENT_CHARS,
            "max_string_primitive_tokens": MAX_JSON_STRING_PRIMITIVE_TOKENS,
            "max_mapping_entries": MAX_JSON_MAPPING_ENTRIES,
            "max_array_items": MAX_JSON_ARRAY_ITEMS,
            "max_nesting_depth": MAX_JSON_NESTING_DEPTH,
            "max_integer_digits": MAX_JSON_INTEGER_DIGITS,
            "max_estimated_decoded_heap_bytes": MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES,
            "max_nodes_per_conversation": MAX_IMPORT_NODES_PER_CONVERSATION,
            "import_batch_materialized_bytes": IMPORT_BATCH_MAX_ESTIMATED_HEAP_BYTES,
            "import_batch": import_batch_resource_profile(),
        },
    }
    import_succeeded = False
    result: dict[str, Any] = {
        "summary": summary,
        "delete_input_on_success": bool(delete_input_on_success),
        "deleted_input": None,
        "delete_input_changed": None,
        "delete_input_failed": None,
        "delete_input_recovery_required": None,
        "delete_input_recovery_token": None,
        "delete_input_recovery_journal_format_version": None,
        "delete_input_error_type": None,
        "summary_update_after_commit_failed": None,
        "import_connection_close_failed": None,
        "summary_update_after_close_failed": None,
    }

    def notify(stage: str) -> None:
        if progress_callback:
            progress_callback(stage, dict(summary))

    def record_duplicate_warning(parsed: Any) -> None:
        record_warning(
            conn,
            run_id,
            WarningRecord(
                parsed.source_file,
                parsed.source_array_index,
                "duplicate_conversation_id",
                compact_json({"conversation_id_hash": sha256_text(parsed.conversation_id)[:16], "policy": "last_wins"}),
                None,
            ),
        )
        summary["warnings"] += 1

    dirty_domains: set[str] = set()
    bulk_generation_started = False

    try:
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("BEGIN")
        conn.execute(
            """CREATE TEMP TABLE import_run_state(
                   conversation_id TEXT NOT NULL PRIMARY KEY,
                   node_count INTEGER NOT NULL,
                   final_status TEXT
               ) WITHOUT ROWID"""
        )
        source_scan_started = time.perf_counter()
        try:
            entries = list_source_entries(source)
            selected = select_conversation_sources(entries)
        except (ValueError, zipfile.BadZipFile) as exc:
            raw_code = str(exc).split(" ", 1)[0]
            code = (
                "ambiguous_conversation_sources"
                if raw_code in {"duplicate_conversation_json_source", "ambiguous_conversation_source_identity"}
                else raw_code
                if raw_code in {
                    "input_symlink_not_allowed",
                    "input_source_outside_root",
                    "source_member_limit_exceeded",
                    "source_changed_during_read",
                }
                else "source_scan_failed"
            )
            summary["failure_code"] = code
            raise ImportPipelineError(code, stage="source_scan", run_id=run_id) from exc
        if not selected:
            summary["failure_code"] = "no_conversation_sources"
            raise ImportPipelineError("no_conversation_sources", stage="input_preflight", run_id=run_id)
        record_source_entries(conn, run_id, entries)
        summary["source_scan_seconds"] = _elapsed(source_scan_started)
        selected_json_bytes = sum(max(0, int(entry.size)) for entry in selected)
        try:
            existing_database_bytes = db_path.stat().st_size
        except OSError:
            existing_database_bytes = 0
        summary["disk_capacity_plan"] = import_capacity_plan(
            selected_json_bytes,
            compressed_source_bytes=source.size,
            pipeline_owned_zip_bytes=(
                source.size
                if source.path.parent.name.startswith("chatgpt-archive-upload-")
                else 0
            ),
            existing_database_bytes=existing_database_bytes,
        )
        try:
            capacity = require_free_space(
                db_path,
                import_required_bytes(selected_json_bytes),
                "import_disk_space_insufficient",
            )
        except DiskSpaceInsufficientError as exc:
            summary["failure_code"] = exc.code
            raise ImportPipelineError(
                exc.code,
                stage="input_preflight",
                run_id=run_id,
                detail={
                    "required_bytes": exc.required_bytes,
                    "free_bytes": exc.free_bytes,
                },
            ) from exc
        summary["disk_preflight_required_bytes"] = capacity["required_bytes"]
        summary["disk_preflight_free_bytes"] = capacity["free_bytes"]
        disk_guard = DiskSpaceGuard(db_path, "import_disk_space_insufficient")
        notify("source_scan_complete")

        batch_usage = {
            "conversations": 0,
            "nodes": 0,
            "input_bytes": 0,
            "decoded_chars": 0,
            "raw_bytes": 0,
            "metadata_bytes": 0,
            "estimated_heap_bytes": 0,
            "sqlite_bind_bytes": 0,
        }
        batch_limits = {
            "conversations": IMPORT_BATCH_MAX_CONVERSATIONS,
            "nodes": IMPORT_BATCH_MAX_NODES,
            "input_bytes": IMPORT_BATCH_MAX_INPUT_BYTES,
            "decoded_chars": IMPORT_BATCH_MAX_DECODED_CHARS,
            "raw_bytes": IMPORT_BATCH_MAX_RAW_BYTES,
            "metadata_bytes": IMPORT_BATCH_MAX_METADATA_BYTES,
            "estimated_heap_bytes": IMPORT_BATCH_MAX_ESTIMATED_HEAP_BYTES,
            "sqlite_bind_bytes": IMPORT_BATCH_MAX_SQLITE_BIND_BYTES,
        }

        def parsed_resource_metrics(item: Any, parsed: Any) -> dict[str, int]:
            input_bytes = getattr(item, "input_utf8_bytes", None)
            decoded_chars = getattr(item, "decoded_chars", None)
            if input_bytes is None or decoded_chars is None:
                serialized = compact_json(item)
                input_bytes = len(serialized.encode("utf-8"))
                decoded_chars = len(serialized)

            def byte_size(value: Any) -> int:
                return len(value.encode("utf-8")) if isinstance(value, str) else 0

            conversation_values = (
                parsed.conversation_id,
                parsed.exported_id,
                parsed.title,
                parsed.current_node,
                parsed.source_file,
                parsed.aggregate_hash,
                parsed.default_model_slug,
                parsed.metadata_json,
            )
            sqlite_bind_bytes = sum(byte_size(value) for value in conversation_values)
            raw_bytes = 0
            metadata_bytes = byte_size(parsed.metadata_json)
            for node_value in parsed.nodes:
                node_fields = (
                    node_value.node_id,
                    node_value.conversation_id,
                    node_value.parent_node_id,
                    node_value.children_json,
                    node_value.message_id,
                    node_value.role,
                    node_value.author_name,
                    node_value.content_type,
                    node_value.content_text,
                    node_value.content_hash,
                    node_value.metadata_json,
                    node_value.raw_message_json,
                )
                sqlite_bind_bytes += sum(byte_size(value) for value in node_fields)
                raw_bytes += byte_size(node_value.raw_message_json)
                metadata_bytes += byte_size(node_value.children_json) + byte_size(node_value.metadata_json)
            estimated_heap = max(
                int(getattr(item, "estimated_decoded_heap_bytes", 0) or 0),
                int(input_bytes)
                + sqlite_bind_bytes
                + len(parsed.nodes) * 512
                + len(parsed.warnings) * 256,
            )
            return {
                "conversations": 1,
                "nodes": len(parsed.nodes),
                "input_bytes": int(input_bytes),
                "decoded_chars": int(decoded_chars),
                "raw_bytes": raw_bytes,
                "metadata_bytes": metadata_bytes,
                "estimated_heap_bytes": estimated_heap,
                "sqlite_bind_bytes": sqlite_bind_bytes,
            }

        def flush_batch(batch: list[Any]) -> None:
            nonlocal bulk_generation_started
            if not batch:
                return

            def ensure_bulk_generation_started() -> None:
                nonlocal bulk_generation_started
                if not bulk_generation_started:
                    begin_bulk_generation_aggregation(conn)
                    bulk_generation_started = True

            statuses = upsert_conversations_batch(
                conn,
                run_id,
                batch,
                skip_fts=rebuild_fts,
                before_first_write=ensure_bulk_generation_started,
            )
            summary["attempted_unchanged_conversations"] += statuses["unchanged"]
            summary["attempted_updated_conversations"] += statuses["updated"]
            summary["attempted_inserted_conversations"] += statuses["inserted"]
            dirty_domains.update(statuses.get("dirty_domains", ()))
            for conversation_id, status in statuses.get("outcomes", {}).items():
                conn.execute(
                    """UPDATE import_run_state
                       SET final_status = CASE
                           WHEN final_status = 'inserted' THEN 'inserted'
                           WHEN final_status = 'updated' THEN 'updated'
                           WHEN final_status = 'unchanged' AND ? <> 'unchanged' THEN 'updated'
                           ELSE ?
                       END
                       WHERE conversation_id = ?""",
                    (status, status, conversation_id),
                )
            summary["import_batch_count"] += 1
            summary["import_batch_total_input_bytes"] += batch_usage["input_bytes"]
            for name in (
                "input_bytes",
                "decoded_chars",
                "nodes",
                "raw_bytes",
                "metadata_bytes",
                "estimated_heap_bytes",
                "sqlite_bind_bytes",
            ):
                summary[f"import_batch_peak_{name}"] = max(
                    summary[f"import_batch_peak_{name}"], batch_usage[name]
                )
            batch.clear()
            for name in batch_usage:
                batch_usage[name] = 0

        parse_started = time.perf_counter()
        def classified_source_sessions():
            try:
                yield from iter_source_array_sessions(source, selected)
            except ImportPipelineError:
                raise
            except (
                json.JSONDecodeError,
                NonFiniteJsonNumberError,
                JsonIntegerTooLargeError,
                InvalidConversationEncodingError,
                OSError,
                ValueError,
                zipfile.BadZipFile,
            ) as exc:
                code, stage = _classify_source_load_error(exc)
                summary["failure_code"] = code
                raise ImportPipelineError(
                    code,
                    stage=stage,
                    source_identifier="selected_source_session",
                    run_id=run_id,
                ) from exc

        for source_index, entry, source_items in classified_source_sessions():
            parsed_conversations = []
            idx = 0
            while True:
                try:
                    item = next(source_items)
                except StopIteration:
                    break
                except ConversationJsonTopLevelError as exc:
                    summary["failure_code"] = "conversation_json_top_level_not_list"
                    raise ImportPipelineError(
                        "conversation_json_top_level_not_list",
                        stage="top_level_contract",
                        source_identifier=f"selected_source_{source_index}",
                        run_id=run_id,
                        detail={"top_level_type": exc.top_level_type},
                    ) from exc
                except (json.JSONDecodeError, NonFiniteJsonNumberError, JsonIntegerTooLargeError, InvalidConversationEncodingError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    code, stage = _classify_source_load_error(exc)
                    summary["failure_code"] = code
                    raise ImportPipelineError(
                        code,
                        stage=stage,
                        source_identifier=f"selected_source_{source_index}",
                        run_id=run_id,
                    ) from exc
                warning = validate_conversation_element(item, entry.source_path, idx)
                if warning:
                    record_warning(conn, run_id, warning)
                    summary["warnings"] += 1
                    summary["skipped_invalid_elements"] += 1
                    idx += 1
                    continue
                parsed = parse_conversation(item, entry.source_path, idx)
                record_warnings(conn, run_id, parsed.warnings)
                summary["warnings"] += len(parsed.warnings)
                inserted_state = conn.execute(
                    "INSERT OR IGNORE INTO import_run_state(conversation_id, node_count) VALUES (?, ?)",
                    (parsed.conversation_id, len(parsed.nodes)),
                ).rowcount
                if not inserted_state:
                    record_duplicate_warning(parsed)
                    conn.execute(
                        "UPDATE import_run_state SET node_count = ? WHERE conversation_id = ?",
                        (len(parsed.nodes), parsed.conversation_id),
                    )
                summary["attempted_valid_conversations"] += 1
                summary["attempted_nodes"] += len(parsed.nodes)
                metrics = parsed_resource_metrics(item, parsed)
                disk_guard.check(advanced_bytes=metrics["input_bytes"])
                if parsed_conversations and any(
                    batch_usage[name] + metrics[name] > limit
                    for name, limit in batch_limits.items()
                ):
                    flush_batch(parsed_conversations)
                parsed_conversations.append(parsed)
                for name, value in metrics.items():
                    batch_usage[name] += value
                if any(
                    batch_usage[name] >= limit
                    for name, limit in batch_limits.items()
                ):
                    flush_batch(parsed_conversations)
                idx += 1
            flush_batch(parsed_conversations)
            notify("shard_complete")
        summary["parse_and_upsert_seconds"] = _elapsed(parse_started)
        summary["import_batch_average_input_bytes"] = (
            summary["import_batch_total_input_bytes"] // summary["import_batch_count"]
            if summary["import_batch_count"]
            else 0
        )
        if rebuild_fts:
            fts_started = time.perf_counter()
            summary["rebuild_fts"] = rebuild_message_fts(conn, optimize=optimize_fts_after_import)
            summary["fts_rebuild_seconds"] = _elapsed(fts_started)
            summary["optimize_fts_after_import"] = bool(optimize_fts_after_import)
            notify("fts_rebuild_complete")
        index_started = time.perf_counter()
        if dirty_domains.intersection({"message", "title"}):
            optional_drop_failures = drop_optional_web_indexes(conn)
            if optional_drop_failures:
                summary["optional_web_index_drop_failures"] = len(optional_drop_failures)
                for failure in optional_drop_failures:
                    record_warning(
                        conn,
                        run_id,
                        WarningRecord(
                            "optional_web_index",
                            None,
                            "optional_web_index_drop_failed",
                            compact_json(
                                {
                                    "table": failure["table"],
                                    "error_type": failure["error_type"],
                                }
                            ),
                            None,
                        ),
                    )
                summary["warnings"] += len(optional_drop_failures)
        summary["dirty_domains"] = sorted(dirty_domains)
        summary["import_index_rebuild_seconds"] = _elapsed(index_started)
        notify("import_index_rebuild_complete")
        if optimize_after_import_flag:
            pragma_started = time.perf_counter()
            summary["optimize_after_import"] = optimize_after_import(conn)
            summary["pragma_optimize_seconds"] = _elapsed(pragma_started)
            notify("pragma_optimize_complete")
        summary["valid_conversations"] = summary["attempted_valid_conversations"]
        state_totals = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(node_count), 0) FROM import_run_state"
        ).fetchone()
        summary["committed_conversations"] = int(state_totals[0])
        summary["committed_nodes"] = int(state_totals[1])
        summary["nodes"] = summary["committed_nodes"]
        final_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT final_status, COUNT(*) FROM import_run_state GROUP BY final_status"
            )
        }
        summary["inserted_conversations"] = final_counts.get("inserted", 0)
        summary["updated_conversations"] = final_counts.get("updated", 0)
        summary["unchanged_conversations"] = final_counts.get("unchanged", 0)
        summary["legacy_pre_commit_seconds"] = _elapsed(import_started)
        summary["wall_total_seconds"] = summary["legacy_pre_commit_seconds"]
        summary["total_import_seconds"] = summary["wall_total_seconds"]
        commit_started = time.perf_counter()
        warning_rows = conn.execute(
            """SELECT warning_type, COUNT(*) AS count
               FROM import_warnings WHERE import_run_id = ?
               GROUP BY warning_type ORDER BY warning_type""",
            (run_id,),
        ).fetchall()
        summary["warnings"] = sum(int(row["count"]) for row in warning_rows)
        summary["warnings_by_type"] = [dict(row) for row in warning_rows]
        if bulk_generation_started:
            finish_bulk_generation_aggregation(
                conn,
                dirty_domains,
            )
        mark_legacy_compatibility_current(conn)
        finish_import_run(conn, run_id, "finished", summary)
        summary["finalize_commit_seconds"] = _elapsed(commit_started)
        summary["wall_total_seconds"] = _elapsed(import_started)
        summary["total_import_seconds"] = summary["wall_total_seconds"]
        try:
            summary_persistence_started = time.perf_counter()
            update_import_run_summary(conn, run_id, summary)
            summary["post_commit_summary_persistence_seconds"] += _elapsed(
                summary_persistence_started
            )
        except (sqlite3.Error, OSError) as exc:
            summary["post_commit_summary_persistence_seconds"] += _elapsed(
                summary_persistence_started
            )
            message = type(exc).__name__
            result["summary_update_after_commit_failed"] = message
            LOGGER.warning("summary_update_after_commit_failed %s", message)
        close_started = time.perf_counter()
        try:
            conn.close()
        except (sqlite3.Error, OSError) as exc:
            message = type(exc).__name__
            result["import_connection_close_failed"] = message
            LOGGER.warning("import_connection_close_failed %s", message)
        summary["close_seconds"] = _elapsed(close_started)
        summary["wall_total_seconds"] = _elapsed(import_started)
        summary["total_import_seconds"] = summary["wall_total_seconds"]
        try:
            summary_persistence_started = time.perf_counter()
            summary_conn = connect(db_path)
            try:
                update_import_run_summary(summary_conn, run_id, summary)
            finally:
                summary_conn.close()
            summary["post_commit_summary_persistence_seconds"] += _elapsed(
                summary_persistence_started
            )
        except (sqlite3.Error, OSError) as exc:
            summary["post_commit_summary_persistence_seconds"] += _elapsed(
                summary_persistence_started
            )
            message = type(exc).__name__
            result["summary_update_after_close_failed"] = message
            LOGGER.warning("summary_update_after_close_failed %s", message)
        import_succeeded = True
    except Exception as exc:
        try:
            in_transaction = conn.in_transaction
        except sqlite3.ProgrammingError:
            in_transaction = False
        if in_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        if isinstance(exc, ImportPipelineError):
            pipeline_error = exc
        elif is_disk_full_error(exc):
            pipeline_error = ImportPipelineError(
                "import_disk_space_insufficient",
                stage="transaction",
                run_id=run_id,
            )
        else:
            pipeline_error = ImportPipelineError(
                "import_transaction_failed",
                stage="transaction",
                run_id=run_id,
            )
        summary["failure_code"] = pipeline_error.code
        summary["failure_stage"] = pipeline_error.stage
        summary["original_failure_code"] = pipeline_error.code
        summary["original_failure_stage"] = pipeline_error.stage
        summary["valid_conversations"] = 0
        summary["nodes"] = 0
        summary["committed_conversations"] = 0
        summary["committed_nodes"] = 0
        summary["inserted_conversations"] = 0
        summary["updated_conversations"] = 0
        summary["unchanged_conversations"] = 0
        summary["wall_total_seconds"] = _elapsed(import_started)
        summary["total_import_seconds"] = summary["wall_total_seconds"]
        try:
            conn.close()
        except Exception:
            pass
        summary["failure_persistence_failed"] = False
        persistence_error = _persist_failed_import_run(
            db_path,
            run_id,
            pipeline_error,
            summary,
        )
        if persistence_error is not None:
            pipeline_error.failure_persistence_failed = True
            pipeline_error.failure_persistence_error_type = persistence_error
            summary["failure_persistence_failed"] = True
            summary["failure_persistence_error_type"] = persistence_error
            LOGGER.error(
                "failure_persistence_failed import_run_id=%s original_failure_code=%s "
                "original_failure_stage=%s error_type=%s",
                run_id,
                pipeline_error.code,
                pipeline_error.stage,
                persistence_error,
            )
        else:
            summary["failure_persistence_failed"] = False
        pipeline_error.run_id = run_id
        pipeline_error.summary = dict(summary)
        raise pipeline_error from exc if pipeline_error is not exc else None
    if not import_succeeded:
        raise RuntimeError("import did not complete")
    if delete_input_on_success:
        recovery_required = False
        try:
            delete_succeeded = delete_input_if_unchanged(
                source,
                expected_source_sha256=bound_source_sha256,
            )
            delete_error: BaseException | None = None
        except (OSError, DeleteInputRecoveryRequired) as exc:
            delete_succeeded = False
            delete_error = exc
            recovery_required = isinstance(exc, DeleteInputRecoveryRequired)
        if not delete_succeeded and delete_error is None:
            result["delete_input_changed"] = True
            summary["delete_input_changed"] = True
            summary["warnings"] += 1
            _record_post_import_warning(
                db_path,
                run_id,
                WarningRecord("input", None, "delete_input_changed", None, None),
                summary,
            )
        elif delete_error is not None:
            error_type = type(delete_error).__name__
            result["delete_input_failed"] = True
            result["delete_input_error_type"] = error_type
            result["delete_input_recovery_required"] = recovery_required
            summary["delete_input_failed"] = True
            summary["delete_input_error_type"] = error_type
            summary["delete_input_recovery_required"] = recovery_required
            if recovery_required:
                recovery_token = getattr(delete_error, "recovery_token", None)
                recovery_version = getattr(
                    delete_error, "journal_format_version", None
                )
                result["delete_input_recovery_token"] = recovery_token
                result["delete_input_recovery_journal_format_version"] = recovery_version
                summary["delete_input_recovery_token"] = recovery_token
                summary["delete_input_recovery_journal_format_version"] = recovery_version
            summary["warnings"] += 1
            _record_post_import_warning(
                db_path,
                run_id,
                WarningRecord(
                    "input",
                    None,
                    "delete_input_recovery_required" if recovery_required else "delete_input_failed",
                    compact_json({"error_type": error_type}),
                    None,
                ),
                summary,
            )
        else:
            result["deleted_input"] = True
            summary["deleted_input"] = True
            _update_post_import_summary(db_path, run_id, summary)
    LOGGER.info(
        "import_finished run_id=%s valid=%s inserted=%s updated=%s unchanged=%s seconds=%s",
        run_id,
        summary["valid_conversations"],
        summary["inserted_conversations"],
        summary["updated_conversations"],
        summary["unchanged_conversations"],
        summary["wall_total_seconds"],
    )
    summary["canonical_pipeline_seconds"] = _elapsed(import_started)
    summary["wall_total_seconds"] = summary["canonical_pipeline_seconds"]
    summary["total_import_seconds"] = summary["wall_total_seconds"]
    return result


def _persist_failed_import_run(
    db_path: Path,
    run_id: int,
    pipeline_error: ImportPipelineError,
    summary: dict[str, Any],
) -> str | None:
    """Persist rollback outcome on a fresh connection and report failure safely."""

    failure_conn: sqlite3.Connection | None = None
    try:
        failure_conn = connect(db_path)
        record_warning(
            failure_conn,
            run_id,
            WarningRecord(
                pipeline_error.source_identifier,
                None,
                pipeline_error.code,
                compact_json({"stage": pipeline_error.stage, **pipeline_error.detail}),
                None,
            ),
        )
        warning_rows = failure_conn.execute(
            """SELECT warning_type, COUNT(*) AS count
               FROM import_warnings WHERE import_run_id = ?
               GROUP BY warning_type ORDER BY warning_type""",
            (run_id,),
        ).fetchall()
        summary["warnings"] = sum(int(row["count"]) for row in warning_rows)
        summary["warnings_by_type"] = [dict(row) for row in warning_rows]
        finish_import_run(failure_conn, run_id, "failed", summary)
        return None
    except Exception as persistence_exc:
        if failure_conn is not None:
            try:
                failure_conn.rollback()
            except Exception:
                pass
        return type(persistence_exc).__name__
    finally:
        if failure_conn is not None:
            try:
                failure_conn.close()
            except Exception:
                pass


def _record_post_import_warning(db_path: Path, run_id: int, warning: WarningRecord, summary: dict[str, Any]) -> None:
    try:
        conn = connect(db_path)
        try:
            record_warning(conn, run_id, warning)
            update_import_run_summary(conn, run_id, summary)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        LOGGER.warning("post_import_warning_record_failed %s", type(exc).__name__)


def _update_post_import_summary(db_path: Path, run_id: int, summary: dict[str, Any]) -> None:
    try:
        conn = connect(db_path)
        try:
            update_import_run_summary(conn, run_id, summary)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        LOGGER.warning("post_import_summary_update_failed %s", type(exc).__name__)


def _elapsed(start: float) -> float:
    return round(max(0.0, time.perf_counter() - start), 6)


def cmd_export(args: argparse.Namespace) -> int:
    conn = connect_existing(Path(args.db))
    try:
        with read_snapshot(conn):
            require_current_database_schema(conn)
            formats = ["md", "txt"] if args.format == "all" else [args.format]
            result = export_conversations(
                conn,
                Path(args.out),
                formats,
                args.from_date,
                args.to_date,
                args.force,
                path=args.path,
                include_internal=args.include_internal,
            )
    finally:
        conn.close()
    print(f"exported_conversations {result['conversations']}")
    print(f"formats {','.join(result['formats'])}")
    print(f"written {result['written']}")
    print(f"skipped_unchanged {result['skipped_unchanged']}")
    print(f"out_directory {Path(args.out).resolve()}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = connect_existing_readonly(Path(args.db))
    try:
        with read_snapshot(conn):
            require_current_database_schema(conn)
            stats = get_stats(conn)
    finally:
        conn.close()
    for key, value in stats.items():
        if key.endswith("_time"):
            print(f"{key} {epoch_to_display(value)}")
        else:
            print(f"{key} {value}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    conn = connect_existing_readonly(Path(args.db))
    try:
        with read_snapshot(conn):
            result = verify_database(conn)
    finally:
        conn.close()
    print(f"ok {str(result['ok']).lower()}")
    print(f"schema_ok {str(result.get('schema_ok', True)).lower()}")
    if result.get("database_error_code"):
        print(f"database_error_code {result['database_error_code']}")
    print(f"migration_required {str(bool(result.get('migration_required'))).lower()}")
    print(f"current_database_schema_version {result.get('current_database_schema_version')}")
    print(f"required_database_schema_version {result.get('required_database_schema_version')}")
    if result.get("missing_tables"):
        print(f"missing_tables {','.join(result['missing_tables'])}")
    if result.get("missing_columns"):
        pairs = [
            f"{table}.{column}"
            for table, columns in sorted(result["missing_columns"].items())
            for column in columns
        ]
        print(f"missing_columns {','.join(pairs)}")
    for key in (
        "invalid_tables",
        "object_type_mismatches",
        "invalid_indexes",
        "invalid_triggers",
        "invalid_generation_rows",
        "missing_foreign_keys",
    ):
        if result.get(key):
            print(f"{key} {','.join(sorted(result[key]))}")
    for key in ("missing_indexes", "missing_triggers", "missing_generation_rows"):
        if result.get(key):
            print(f"{key} {','.join(result[key])}")
    print(f"integrity_check {result['integrity_check']}")
    print(f"latest_import_run_id {result['latest_import_run_id']}")
    print(f"latest_run_warnings {result['latest_run_warnings']}")
    print(f"total_warnings {result['total_warnings']}")
    print(f"missing_current_node {result['missing_current_node']}")
    print(f"broken_parent_links {result['broken_parent_links']}")
    print(f"conversations_with_zero_nodes {result['conversations_with_zero_nodes']}")
    print(f"parent_cycles {result['parent_cycles']}")
    print(f"parent_cycle_nodes {result.get('parent_cycle_nodes', result['parent_cycles'])}")
    print(f"parent_cycle_components {result.get('parent_cycle_components', 0)}")
    print(f"foreign_key_violations {result.get('foreign_key_violations', 0)}")
    print(f"foreign_key_violations_exact {str(bool(result.get('foreign_key_violations_exact'))).lower()}")
    print(f"foreign_key_check_complete {str(bool(result.get('foreign_key_check_complete'))).lower()}")
    print(f"foreign_key_violation_sample_limit {result.get('foreign_key_violation_sample_limit', 0)}")
    for item in result.get("foreign_key_violations_by_table", []):
        print(f"foreign_key_violation_table {item['table']} count {item['count']}")
    for item in result.get("foreign_key_violation_samples", []):
        print(
            "foreign_key_violation_sample "
            f"table={item['table']} rowid={item['rowid']} "
            f"parent_table={item['parent_table']} constraint_index={item['constraint_index']}"
        )
    print(f"non_finite_timestamps {result.get('non_finite_timestamps', 0)}")
    print(f"effective_current_checked {str(bool(result.get('effective_current_checked'))).lower()}")
    print(f"effective_current_exact {str(bool(result.get('effective_current_exact'))).lower()}")
    print(f"diagnostics_partial {str(bool(result.get('diagnostics_partial'))).lower()}")
    if result.get("resource_limit_code"):
        print(f"resource_limit_code {result['resource_limit_code']}")
    for key, value in sorted(result.get("effective_current_diagnostics", {}).items()):
        print(f"effective_current_{key} {value}")
    if result.get("optional_web_index_error"):
        print(f"optional_web_index_error {str(result['optional_web_index_error']).lower()}")
        print(f"optional_web_index_recovery_hint {result['optional_web_index_recovery_hint']}")
    print(f"message_fts_available {str(bool(result.get('message_fts_available'))).lower()}")
    print(f"message_fts_rebuildable {str(bool(result.get('message_fts_rebuildable'))).lower()}")
    if result.get("message_fts_error"):
        print(f"message_fts_error {result['message_fts_error']}")
    if result.get("optional_message_fts_error"):
        print("optional_message_fts_error true")
        print(f"optional_message_fts_recovery_hint {result['optional_message_fts_recovery_hint']}")
    for item in result["latest_warnings_by_type"]:
        print(f"latest_warning_type {item['warning_type']} count {item['count']}")
    for item in result["warnings_by_type"]:
        print(f"warning_type {item['warning_type']} count {item['count']}")
    return 0 if result["ok"] else 1


def cmd_search(args: argparse.Namespace) -> int:
    from .search import parse_query, search_messages

    conn = connect_existing_readonly(Path(args.db))
    try:
        with read_snapshot(conn):
            require_current_database_schema(conn)
            parsed = parse_query(args.query)
            if parsed.errors:
                print("invalid_query true")
                return 2
            page = search_messages(conn, parsed, limit=args.limit, count_total=False)
    finally:
        conn.close()
    for row in page["items"]:
        print(f"conversation_id {row['conversation_id']} node_id {row['node_id']} role {row['role'] or ''}")
    print(f"matches {len(page['items'])}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    try:
        import uvicorn
    except ImportError as exc:
        raise ValueError("Missing Web dependency uvicorn. Install requirements-web.txt in the active Python environment.") from exc
    from .web_app import create_app
    from .web_api import _get_upload_policy, _is_loopback, _resolve_web_url_host

    if not _is_loopback(args.host):
        policy = _get_upload_policy(host=args.host)
        if policy.remote_profile == "local_profile":
            upload_policy = "full local upload limits"
        elif policy.remote_profile == "explicit_remote_override":
            upload_policy = "explicit remote upload overrides"
        else:
            upload_policy = "remote-safe upload limits"
        print(
            f"WARNING: binding to {args.host}; this exposes the local archive browser and upload endpoint. "
            f"Only use this on trusted networks. upload_policy {upload_policy}."
        )
    app = create_app(
        db_path,
        allow_fallback=args.allow_fallback,
        log_level=args.log_level,
        host=args.host,
        allowed_hosts=args.allowed_hosts,
        trusted_proxies=args.trusted_proxies,
    )
    if args.allow_fallback:
        print("WARNING: fallback UI is enabled. This is a limited emergency page, not the full React Web UI.")
    web_host = _resolve_web_url_host(args.host)
    print(f"web_url http://{web_host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


def cmd_web_index(args: argparse.Namespace) -> int:
    result = create_web_indexes(Path(args.db))
    for key, value in result.items():
        if key == "drop_failures":
            continue
        print(f"{key} {value}")
    for failure in result.get("drop_failures", []):
        print(f"drop_failure table={failure['table']} error_type={failure['error_type']}")
    return 0
