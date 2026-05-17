from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .db import (
    begin_import_run,
    configure_import_connection,
    connect,
    connect_existing,
    connect_existing_readonly,
    drop_optional_web_indexes,
    finish_import_run,
    get_stats,
    init_db,
    optimize_after_import,
    record_source_entries,
    record_warning,
    record_warnings,
    rebuild_message_fts,
    update_import_run_summary,
    upsert_conversations_batch,
    verify_database,
)
from .exporter import export_conversations
from .logging_utils import configure_logging, get_logger
from .parser import WarningRecord, parse_conversation, validate_conversation_element
from .scanner import (
    InputSource,
    is_legacy_conversations_source,
    is_shard_conversation_source,
    list_source_entries,
    load_json_from_source,
    resolve_input,
    select_conversation_sources,
)
from .utils import compact_json, epoch_to_display, sha256_file, sha256_text
from .web_db import create_web_indexes

LOGGER = get_logger("cli")


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configure_logging(
            args.log_level,
            file_path=Path(args.log_file) if args.log_file else None,
            json_logs=args.json_logs,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    try:
        return args.func(args)
    except (ValueError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
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
    web_p.add_argument("--port", type=int, default=8787, help="Bind port.")
    web_p.add_argument("--allow-fallback", action="store_true", help="Allow the limited fallback HTML UI if the React build is missing.")
    web_p.set_defaults(func=cmd_web)
    _add_log_args(web_p)

    web_index_p = sub.add_parser("web-index", help="Build optional Web substring search indexes.")
    web_index_p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    web_index_p.set_defaults(func=cmd_web_index)
    _add_log_args(web_index_p)
    return parser


def cmd_init(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db))
    fts = init_db(conn)
    conn.close()
    print("initialized_db true")
    print(f"fts5_available {str(fts).lower()}")
    return 0


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

    ids: list[str] = []
    invalid_locations: list[tuple[str, int, str]] = []
    valid_count = 0
    top_level_bad = 0
    for entry in selected:
        try:
            data = load_json_from_source(source, entry.source_path)
        except json.JSONDecodeError:
            top_level_bad += 1
            print(f"source {Path(entry.source_path).name} top_level invalid_json valid 0 invalid 0")
            continue
        if not isinstance(data, list):
            top_level_bad += 1
            print(f"source {Path(entry.source_path).name} top_level {type(data).__name__} valid 0 invalid 0")
            continue
        source_valid = 0
        source_invalid = 0
        for idx, item in enumerate(data):
            warning = validate_conversation_element(item, entry.source_path, idx)
            if warning:
                source_invalid += 1
                invalid_locations.append((Path(entry.source_path).name, idx, warning.warning_type))
                continue
            source_valid += 1
            ids.append(str(item.get("id")))
        valid_count += source_valid
        print(f"source {Path(entry.source_path).name} top_level list valid {source_valid} invalid {source_invalid}")
    duplicate_count = len(ids) - len(set(ids))
    print(f"valid_conversations {valid_count}")
    print(f"invalid_elements {len(invalid_locations)}")
    for filename, idx, warning_type in invalid_locations[:50]:
        print(f"invalid_element source_file={filename} index={idx} warning_type={warning_type}")
    if len(invalid_locations) > 50:
        print(f"invalid_element_more {len(invalid_locations) - 50}")
    print(f"duplicate_conversation_ids {duplicate_count}")
    print(f"bad_top_level_files {top_level_bad}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
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
        "pragma_optimize_seconds",
        "finalize_commit_seconds",
        "close_seconds",
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
) -> dict[str, Any]:
    """Import a ZIP/directory and return structural summary without printing chat content."""
    import_started = time.perf_counter()
    source = resolve_input(input_value, cwd)
    if delete_input_on_success and source.kind != "zip":
        raise ValueError("--delete-input-on-success is only supported for ZIP inputs")
    if optimize_fts_after_import and not rebuild_fts:
        raise ValueError("--optimize-fts-after-import requires --rebuild-fts")
    LOGGER.info("import_start input_kind=%s input_size=%s", source.kind, source.size)
    conn = connect(db_path)
    configure_import_connection(conn)
    init_db(conn)
    input_sha = None if no_input_sha256 or source.kind != "zip" else sha256_file(source.path)
    run_id = begin_import_run(conn, source, input_sha)
    summary: dict[str, Any] = {
        "import_run_id": run_id,
        "source": source.kind,
        "valid_conversations": 0,
        "nodes": 0,
        "warnings": 0,
        "skipped_invalid_elements": 0,
        "unchanged_conversations": 0,
        "inserted_conversations": 0,
        "updated_conversations": 0,
        "source_scan_seconds": 0.0,
        "parse_and_upsert_seconds": 0.0,
        "fts_rebuild_seconds": 0.0,
        "pragma_optimize_seconds": 0.0,
        "finalize_commit_seconds": 0.0,
        "close_seconds": 0.0,
        "legacy_pre_commit_seconds": 0.0,
        "wall_total_seconds": 0.0,
        "total_import_seconds": 0.0,
    }
    import_succeeded = False
    result: dict[str, Any] = {
        "summary": summary,
        "delete_input_on_success": bool(delete_input_on_success),
        "deleted_input": None,
        "delete_input_failed": None,
        "delete_input_error_type": None,
        "summary_update_after_commit_failed": None,
        "import_connection_close_failed": None,
        "summary_update_after_close_failed": None,
    }

    def notify(stage: str) -> None:
        if progress_callback:
            progress_callback(stage, dict(summary))

    seen_conversation_ids: set[str] = set()

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

    try:
        conn.execute("BEGIN")
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
                        compact_json({"table": failure["table"], "error_type": failure["error_type"]}),
                        None,
                    ),
                )
            summary["warnings"] += len(optional_drop_failures)
        source_scan_started = time.perf_counter()
        entries = list_source_entries(source)
        selected = select_conversation_sources(entries)
        record_source_entries(conn, run_id, entries)
        summary["source_scan_seconds"] = _elapsed(source_scan_started)
        notify("source_scan_complete")

        def flush_batch(batch: list[Any]) -> None:
            if not batch:
                return
            statuses = upsert_conversations_batch(conn, run_id, batch, skip_fts=rebuild_fts)
            summary["unchanged_conversations"] += statuses["unchanged"]
            summary["updated_conversations"] += statuses["updated"]
            summary["inserted_conversations"] += statuses["inserted"]
            batch.clear()

        parse_started = time.perf_counter()
        for entry in selected:
            try:
                data = load_json_from_source(source, entry.source_path)
            except json.JSONDecodeError as exc:
                record_warning(conn, run_id, WarningRecord(entry.source_path, None, "invalid_json", None, str(exc)))
                summary["warnings"] += 1
                continue
            if not isinstance(data, list):
                record_warning(conn, run_id, WarningRecord(entry.source_path, None, "top_level_not_list", None, type(data).__name__))
                summary["warnings"] += 1
                continue
            parsed_conversations = []
            for idx, item in enumerate(data):
                warning = validate_conversation_element(item, entry.source_path, idx)
                if warning:
                    record_warning(conn, run_id, warning)
                    summary["warnings"] += 1
                    summary["skipped_invalid_elements"] += 1
                    continue
                parsed = parse_conversation(item, entry.source_path, idx)
                record_warnings(conn, run_id, parsed.warnings)
                summary["warnings"] += len(parsed.warnings)
                if parsed.conversation_id in seen_conversation_ids:
                    record_duplicate_warning(parsed)
                else:
                    seen_conversation_ids.add(parsed.conversation_id)
                summary["valid_conversations"] += 1
                summary["nodes"] += len(parsed.nodes)
                parsed_conversations.append(parsed)
                if len(parsed_conversations) >= 100:
                    flush_batch(parsed_conversations)
            flush_batch(parsed_conversations)
            notify("shard_complete")
        summary["parse_and_upsert_seconds"] = _elapsed(parse_started)
        if rebuild_fts:
            fts_started = time.perf_counter()
            summary["rebuild_fts"] = rebuild_message_fts(conn, optimize=optimize_fts_after_import)
            summary["fts_rebuild_seconds"] = _elapsed(fts_started)
            summary["optimize_fts_after_import"] = bool(optimize_fts_after_import)
            notify("fts_rebuild_complete")
        if optimize_after_import_flag:
            pragma_started = time.perf_counter()
            summary["optimize_after_import"] = optimize_after_import(conn)
            summary["pragma_optimize_seconds"] = _elapsed(pragma_started)
            notify("pragma_optimize_complete")
        summary["legacy_pre_commit_seconds"] = _elapsed(import_started)
        summary["wall_total_seconds"] = summary["legacy_pre_commit_seconds"]
        summary["total_import_seconds"] = summary["wall_total_seconds"]
        commit_started = time.perf_counter()
        finish_import_run(conn, run_id, "finished", summary)
        summary["finalize_commit_seconds"] = _elapsed(commit_started)
        summary["wall_total_seconds"] = _elapsed(import_started)
        summary["total_import_seconds"] = summary["wall_total_seconds"]
        try:
            update_import_run_summary(conn, run_id, summary)
        except (sqlite3.Error, OSError) as exc:
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
            summary_conn = connect(db_path)
            try:
                update_import_run_summary(summary_conn, run_id, summary)
            finally:
                summary_conn.close()
        except (sqlite3.Error, OSError) as exc:
            message = type(exc).__name__
            result["summary_update_after_close_failed"] = message
            LOGGER.warning("summary_update_after_close_failed %s", message)
        import_succeeded = True
    except Exception:
        try:
            in_transaction = conn.in_transaction
        except sqlite3.ProgrammingError:
            in_transaction = False
        if in_transaction:
            conn.rollback()
        summary["wall_total_seconds"] = _elapsed(import_started)
        summary["total_import_seconds"] = summary["wall_total_seconds"]
        try:
            finish_import_run(conn, run_id, "failed", summary)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        raise
    if not import_succeeded:
        raise RuntimeError("import did not complete")
    if delete_input_on_success:
        try:
            delete_target = source.delete_target or source.path
            delete_target.unlink()
        except OSError as exc:
            error_type = type(exc).__name__
            result["delete_input_failed"] = True
            result["delete_input_error_type"] = error_type
            summary["delete_input_failed"] = True
            summary["delete_input_error_type"] = error_type
            summary["warnings"] += 1
            _record_post_import_warning(
                db_path,
                run_id,
                WarningRecord("input", None, "delete_input_failed", compact_json({"error_type": error_type}), None),
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
    return result


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
    formats = ["md", "txt"] if args.format == "all" else [args.format]
    result = export_conversations(conn, Path(args.out), formats, args.from_date, args.to_date, args.force)
    conn.close()
    print(f"exported_conversations {result['conversations']}")
    print(f"formats {','.join(result['formats'])}")
    print(f"written {result['written']}")
    print(f"skipped_unchanged {result['skipped_unchanged']}")
    print("out directory")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = connect_existing_readonly(Path(args.db))
    stats = get_stats(conn)
    conn.close()
    for key, value in stats.items():
        if key.endswith("_time"):
            print(f"{key} {epoch_to_display(value)}")
        else:
            print(f"{key} {value}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    conn = connect_existing_readonly(Path(args.db))
    result = verify_database(conn)
    conn.close()
    print(f"ok {str(result['ok']).lower()}")
    print(f"schema_ok {str(result.get('schema_ok', True)).lower()}")
    if result.get("missing_tables"):
        print(f"missing_tables {','.join(result['missing_tables'])}")
    print(f"integrity_check {result['integrity_check']}")
    print(f"latest_import_run_id {result['latest_import_run_id']}")
    print(f"latest_run_warnings {result['latest_run_warnings']}")
    print(f"total_warnings {result['total_warnings']}")
    print(f"missing_current_node {result['missing_current_node']}")
    print(f"broken_parent_links {result['broken_parent_links']}")
    print(f"conversations_with_zero_nodes {result['conversations_with_zero_nodes']}")
    print(f"parent_cycles {result['parent_cycles']}")
    if result.get("optional_web_index_error"):
        print(f"optional_web_index_error {str(result['optional_web_index_error']).lower()}")
        print(f"optional_web_index_recovery_hint {result['optional_web_index_recovery_hint']}")
    for item in result["latest_warnings_by_type"]:
        print(f"latest_warning_type {item['warning_type']} count {item['count']}")
    for item in result["warnings_by_type"]:
        print(f"warning_type {item['warning_type']} count {item['count']}")
    return 0 if result["ok"] else 1


def cmd_search(args: argparse.Namespace) -> int:
    from .search import MAX_CANDIDATES, parse_query, search_messages

    conn = connect_existing_readonly(Path(args.db))
    parsed = parse_query(args.query)
    if parsed.errors:
        print("invalid_query true")
        conn.close()
        return 2
    page = search_messages(conn, parsed, limit=args.limit, candidate_limit=MAX_CANDIDATES)
    for row in page["items"]:
        print(f"conversation_id {row['conversation_id']} node_id {row['node_id']} role {row['role'] or ''}")
    print(f"matches {len(page['items'])}")
    conn.close()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if args.host != "127.0.0.1":
        print(f"WARNING: binding to {args.host}; only use this on trusted networks.")
    try:
        import uvicorn
    except ImportError as exc:
        raise ValueError("Missing Web dependency uvicorn. Install requirements-web.txt in the active Python environment.") from exc
    from .web_app import create_app

    app = create_app(db_path, allow_fallback=args.allow_fallback, log_level=args.log_level)
    if args.allow_fallback:
        print("WARNING: fallback UI is enabled. This is a limited emergency page, not the full React Web UI.")
    print(f"web_url http://{args.host}:{args.port}")
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
