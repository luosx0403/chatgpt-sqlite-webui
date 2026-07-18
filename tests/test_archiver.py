from __future__ import annotations

import json
import argparse
import base64
import contextlib
import hashlib
import io
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unicodedata
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

from chatgpt_export_archiver.cli import build_parser, main
from chatgpt_export_archiver.db import DatabaseMigrationError, connect, export_query, init_db, verify_database, drop_optional_web_indexes, _drop_table_with_shadows, _integrity_failure_is_message_fts_only, _integrity_failure_is_web_index_only, _run_integrity_check, _line_names_web_index_table, _insert_fts_batch, _delete_fts_for_conversation
from chatgpt_export_archiver.logging_utils import configure_logging, get_logger, parse_log_level
from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager
from chatgpt_export_archiver.parser import _to_int_bool, compute_aggregate_hash, parse_conversation, validate_conversation_element
from chatgpt_export_archiver.scanner import SourceChangedDuringReadError, _load_json_from_source_for_tests, iter_json_array_from_source, list_source_entries, resolve_input
from chatgpt_export_archiver.search import parse_query
from chatgpt_export_archiver.utils import epoch_to_date_part, epoch_to_display, parse_date_boundary, safe_filename_part
from chatgpt_export_archiver.web_db import connect_readonly, create_web_indexes
from tools.check_delivery_clean import is_forbidden_member, main as delivery_clean_main
from tools import clean_generated_artifacts
from tools.clean_generated_artifacts import main as clean_generated_main


def message_node(node_id, parent, role, text, ts, children=None):
    return {
        "id": node_id,
        "parent": parent,
        "children": children or [],
        "message": {
            "id": f"msg-{node_id}",
            "author": {"role": role},
            "create_time": ts,
            "update_time": ts,
            "content": {"content_type": "text", "parts": text if isinstance(text, list) else [text]},
            "metadata": {},
        },
    }


def null_message_node(node_id, parent, children=None):
    return {"id": node_id, "parent": parent, "children": children or [], "message": None}


def conversation(cid="conv-1", title="Synthetic", current_node="a2", mapping=None, create_time=1_700_000_000):
    if mapping is None:
        mapping = {
            "root": null_message_node("root", None, ["u1"]),
            "u1": message_node("u1", "root", "user", "hello", create_time + 1, ["a1", "branch"]),
            "a1": message_node("a1", "u1", "assistant", "answer", create_time + 2, ["a2"]),
            "a2": message_node("a2", "a1", "user", ["part one", "part two"], create_time + 3, []),
            "branch": message_node("branch", "u1", "assistant", "not exported by default", create_time + 4, []),
        }
    return {
        "id": cid,
        "conversation_id": f"exported-{cid}",
        "title": title,
        "create_time": create_time,
        "update_time": create_time + 100,
        "current_node": current_node,
        "mapping": mapping,
        "is_archived": False,
        "is_starred": False,
        "default_model_slug": "synthetic",
    }


def write_zip(path: Path, files: dict[str, object]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, value in files.items():
            zf.writestr(name, json.dumps(value))


def run_cli(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue() + stderr.getvalue()


def file_hashes(base: Path) -> dict[str, str]:
    result = {}
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        result[path.relative_to(base).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def data_counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        counts = {
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "nodes": conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0],
        }
        try:
            counts["message_fts"] = conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]
        except sqlite3.OperationalError:
            counts["message_fts"] = -1
        for table in ("web_message_norm", "web_title_norm"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = -1
        return counts
    finally:
        conn.close()


class ArchiverTests(unittest.TestCase):
    def test_logging_levels_filter_project_logs(self):
        logger = get_logger("test")
        for level, expected in (
            ("warning", ["warning", "error"]),
            ("info", ["info", "warning", "error"]),
            ("debug", ["debug", "info", "warning", "error"]),
            ("error", ["error"]),
            ("none", []),
        ):
            stream = io.StringIO()
            configure_logging(level, stream=stream)
            logger.debug("debug")
            logger.info("info")
            logger.warning("warning")
            logger.error("error")
            output = stream.getvalue()
            for word in expected:
                self.assertIn(word, output)
            for word in {"debug", "info", "warning", "error"} - set(expected):
                self.assertNotIn(f" {word}", output)
        self.assertEqual(parse_log_level("INFO"), "info")

    def test_web_job_log_tail_respects_log_level(self):
        with tempfile.TemporaryDirectory() as td:
            manager = ImportJobManager(Path(td) / "archive.db", log_level="error")
            job = ImportJob("job", Path(td) / "archive.db", Path(td) / "upload.zip", "upload.zip", 0)
            manager._log(job, "info", "SENSITIVE_SYNTHETIC_TOKEN")
            manager._log(job, "error", "safe error")
            self.assertNotIn("SENSITIVE_SYNTHETIC_TOKEN", "\n".join(job.logs))
            self.assertIn("safe error", "\n".join(job.logs))

    def test_post_close_summary_update_failure_warns_but_import_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "summary-warning.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("summary-warning")]})
            calls = {"count": 0}

            def flaky_update(conn, run_id, summary):
                calls["count"] += 1
                if calls["count"] >= 2:
                    raise sqlite3.OperationalError("synthetic post-close lock")
                from chatgpt_export_archiver.db import update_import_run_summary as real_update
                return real_update(conn, run_id, summary)

            with mock.patch("chatgpt_export_archiver.cli.update_import_run_summary", side_effect=flaky_update):
                code, output = run_cli([
                    "--db",
                    str(db),
                    "import",
                    "--input",
                    str(z),
                    "--no-input-sha256",
                    "--delete-input-on-success",
                ])
            self.assertEqual(code, 0)
            self.assertIn("summary_update_after_close_failed OperationalError", output)
            self.assertIn("deleted_input", output)
            self.assertFalse(z.exists())
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                self.assertTrue(verify_database(conn)["ok"])
                row = conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(row["status"], "finished")
            finally:
                conn.close()

    def test_post_commit_summary_update_failure_warns_but_import_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "commit-warning.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("commit-warning")]})
            calls = {"count": 0}

            def flaky_first_update(conn, run_id, summary):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise sqlite3.OperationalError("synthetic post-commit lock")
                from chatgpt_export_archiver.db import update_import_run_summary as real_update
                return real_update(conn, run_id, summary)

            with mock.patch("chatgpt_export_archiver.cli.update_import_run_summary", side_effect=flaky_first_update):
                code, output = run_cli([
                    "--db",
                    str(db),
                    "import",
                    "--input",
                    str(z),
                    "--no-input-sha256",
                    "--delete-input-on-success",
                ])
            self.assertEqual(code, 0)
            self.assertIn("summary_update_after_commit_failed OperationalError", output)
            self.assertIn("deleted_input", output)
            self.assertFalse(z.exists())
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                self.assertTrue(verify_database(conn)["ok"])
                row = conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(row["status"], "finished")
            finally:
                conn.close()

    def test_date_boundaries_use_utc_days(self):
        self.assertEqual(parse_date_boundary("1970-01-02"), 86400)
        self.assertEqual(parse_date_boundary("1970-01-02", end_of_day=True), 172800)
        self.assertEqual(parse_query("", after="1970-01-02").after, 86400)
        self.assertEqual(parse_query("", before="1970-01-02").before, 172800)

    def test_export_timestamp_is_utc_and_epoch_zero_is_not_missing(self):
        from chatgpt_export_archiver.exporter import render_markdown, render_txt

        previous_tz = os.environ.get("TZ")
        zones = ("UTC0", "GMT+8") if hasattr(time, "tzset") else (None,)
        try:
            for zone in zones:
                if zone is not None:
                    os.environ["TZ"] = zone
                    time.tzset()
                self.assertEqual(epoch_to_display(0), "1970-01-01 00:00:00")
                self.assertEqual(epoch_to_date_part(0), "1970-01-01")
                conv_row = {
                    "title": "Synthetic", "conversation_id": "epoch-zero", "create_time": 0,
                    "update_time": 3600, "current_node": "n", "source_file": "synthetic.json",
                }
                node_row = {"role": "user", "create_time": 0, "update_time": 3600, "content_text": "synthetic"}
                self.assertIn("## User 1970-01-01 00:00:00", render_markdown(conv_row, [node_row]))
                self.assertIn("USER 1970-01-01 00:00:00", render_txt(conv_row, [node_row]))
        finally:
            if hasattr(time, "tzset"):
                if previous_tz is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = previous_tz
                time.tzset()

    def test_query_parser_keeps_quoted_modifier_values_and_escaped_phrases(self):
        parsed = parse_query('title:"foo bar" source:"conversations 1.json" role:"assistant" path:"all" scope:"title"')
        self.assertEqual(parsed.title, "foo bar")
        self.assertEqual(parsed.source, "conversations 1.json")
        self.assertEqual(parsed.role, "assistant")
        self.assertEqual(parsed.path, "all")
        self.assertEqual(parsed.scope, "title")
        excluded = parse_query('-"foo bar" "foo \\"bar\\"" -"baz \\"qux\\""')
        self.assertEqual(excluded.exclude, ["foo bar", 'baz "qux"'])
        self.assertEqual(excluded.phrases, ['foo "bar"'])
        self.assertEqual(parse_query('"foo bar"').phrases, ["foo bar"])
        self.assertEqual(parse_query("--no-input-sha256").terms, ["--no-input-sha256"])

    def test_export_date_to_includes_fractional_utc_day_end(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        db = base / "archive.db"
        out = base / "out"
        conn = connect(db)
        try:
            init_db(conn)
            conn.execute(
                """
                INSERT INTO conversations(conversation_id, title, create_time, update_time, current_node, source_file, aggregate_hash)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                ("fractional-date", "Fractional Date", 1_779_494_399.5, 1_779_494_399.5, "n1", "synthetic.json", "hash"),
            )
            conn.execute(
                """
                INSERT INTO conversation_nodes(conversation_id, node_id, message_id, role, create_time, update_time, content_type, content_text, content_hash, is_on_current_path)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("fractional-date", "n1", "msg-n1", "user", 1_779_494_399.5, 1_779_494_399.5, "text", "fractional export body", "nodehash", 1),
            )
            conn.commit()
            self.assertEqual([row["conversation_id"] for row in export_query(conn, None, parse_date_boundary("2026-05-22", end_of_day=True))], ["fractional-date"])
            self.assertEqual([row["conversation_id"] for row in export_query(conn, None, parse_date_boundary("2026-05-21", end_of_day=True))], [])
        finally:
            conn.close()
        self.assertEqual(main(["--db", str(db), "export", "--out", str(out), "--format", "md", "--to", "2026-05-22"]), 0)
        self.assertTrue(any("fractional export body" in path.read_text(encoding="utf-8") for path in out.glob("*.md")))

    def test_safe_filename_part_avoids_windows_reserved_names(self):
        cases = {
            "CON": "_CON",
            "con": "_con",
            "PRN": "_PRN",
            "AUX.txt": "_AUX.txt",
            "NUL": "_NUL",
            "COM1": "_COM1",
            "LPT9": "_LPT9",
            "COM¹": "_COM¹",
            "COM²": "_COM²",
            "COM³": "_COM³",
            "LPT¹": "_LPT¹",
            "LPT²": "_LPT²",
            "LPT³": "_LPT³",
            "COM¹.txt": "_COM¹.txt",
            "COM².md": "_COM².md",
            "LPT².md": "_LPT².md",
            "LPT³.txt": "_LPT³.txt",
            "normal.": "normal",
            "normal ": "normal",
            "my-COM¹-note": "my-COM¹-note",
            "": "untitled",
            "<>|": "untitled",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(safe_filename_part(raw), expected)
        long = "a" * 200
        self.assertEqual(len(safe_filename_part(long, 40)), 40)

    def test_export_uses_windows_safe_title_parts(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "reserved.zip"
            db = base / "archive.db"
            out = base / "out"
            write_zip(z, {"conversations.json": [conversation("reserved", title="CON", current_node="u1", mapping={"root": null_message_node("root", None, ["u1"]), "u1": message_node("u1", "root", "user", "reserved filename body", 1_700_000_001)})]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "export", "--out", str(out), "--format", "md"]), 0)
            names = [path.name for path in out.glob("*.md")]
            self.assertTrue(names)
            self.assertFalse(any(name.casefold() in {"con.md", "con.txt"} for name in names))
            self.assertTrue(any("_CON_" in name or name.startswith("_CON") for name in names))

    def test_string_boolean_metadata_parses_false_values(self):
        self.assertEqual(_to_int_bool(True), 1)
        self.assertEqual(_to_int_bool(False), 0)
        self.assertEqual(_to_int_bool("true"), 1)
        self.assertEqual(_to_int_bool("false"), 0)
        self.assertEqual(_to_int_bool("1"), 1)
        self.assertEqual(_to_int_bool("0"), 0)
        self.assertIsNone(_to_int_bool(""))

    def test_aggregate_hash_native_values_match_stored_json_fallback(self):
        parsed = parse_conversation(conversation("hash-native"), "conversations.json", 0)
        fast_hash = parsed.aggregate_hash
        for node in parsed.nodes:
            node.children_for_hash = None
            node.metadata_for_hash = None
            node.raw_message_for_hash = None
        self.assertEqual(compute_aggregate_hash(parsed.current_node, parsed.nodes), fast_hash)

    def test_readonly_sqlite_uri_handles_special_path_characters(self):
        with tempfile.TemporaryDirectory(prefix="db path # 中文 ") as td:
            db = Path(td) / "archive # 数据.db"
            conn = connect(db)
            init_db(conn)
            conn.close()
            ro = connect_readonly(db)
            try:
                self.assertEqual(ro.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
            finally:
                ro.close()

    def test_delivery_clean_zip_member_normalization(self):
        self.assertTrue(is_forbidden_member("webui/node_modules/x.js", "runnable"))
        self.assertTrue(is_forbidden_member(r"webui\node_modules\x.js", "runnable"))
        self.assertTrue(is_forbidden_member("../README.md", "runnable"))
        self.assertTrue(is_forbidden_member("/absolute/README.md", "runnable"))
        self.assertTrue(is_forbidden_member("C:/absolute/README.md", "runnable"))
        self.assertTrue(is_forbidden_member("__MACOSX/._x", "runnable"))
        self.assertTrue(is_forbidden_member(".DS_Store", "runnable"))
        self.assertTrue(is_forbidden_member("Thumbs.db", "runnable"))
        self.assertTrue(is_forbidden_member("Desktop.ini", "runnable"))
        self.assertTrue(is_forbidden_member("pkg/module.pyc", "runnable"))
        self.assertTrue(is_forbidden_member(".coverage", "runnable"))
        self.assertTrue(is_forbidden_member(".coverage.unit", "runnable"))
        self.assertTrue(is_forbidden_member(".pytest_cache/CACHEDIR.TAG", "runnable"))
        self.assertTrue(is_forbidden_member(".mypy_cache/x", "runnable"))
        self.assertTrue(is_forbidden_member(".ruff_cache/x", "runnable"))
        self.assertTrue(is_forbidden_member(".tox/x", "runnable"))
        self.assertTrue(is_forbidden_member(".nox/x", "runnable"))
        self.assertTrue(is_forbidden_member("htmlcov/index.html", "runnable"))
        self.assertTrue(is_forbidden_member("logs/import.log", "runnable"))
        self.assertTrue(is_forbidden_member("exports/manifest.csv", "runnable"))
        self.assertTrue(is_forbidden_member("import.jsonl", "runnable"))
        self.assertTrue(is_forbidden_member("acceptance_logs/run.txt", "runnable"))
        self.assertTrue(is_forbidden_member("archive/local.db", "runnable"))
        self.assertTrue(is_forbidden_member("archive/local.db-journal", "runnable"))
        self.assertTrue(is_forbidden_member("local.db-journal", "runnable"))
        self.assertTrue(is_forbidden_member("archive/local.sqlite3", "runnable"))
        self.assertTrue(is_forbidden_member("local.sqlite-wal", "runnable"))
        self.assertTrue(is_forbidden_member("local.sqlite-shm", "runnable"))
        self.assertTrue(is_forbidden_member("local.sqlite-journal", "runnable"))
        self.assertTrue(is_forbidden_member("local.sqlite3-wal", "runnable"))
        self.assertTrue(is_forbidden_member("local.sqlite3-shm", "runnable"))
        self.assertTrue(is_forbidden_member("local.sqlite3-journal", "runnable"))
        self.assertTrue(is_forbidden_member("private-export.zip", "runnable"))
        self.assertTrue(is_forbidden_member("chatgpt_export_2026.zip", "runnable"))
        self.assertTrue(is_forbidden_member("conversations-000.json", "runnable"))
        self.assertTrue(is_forbidden_member(".git/config", "runnable"))
        self.assertTrue(is_forbidden_member(".gitignore.md", "runnable"))
        self.assertTrue(is_forbidden_member("webui/tsconfig.tsbuildinfo", "runnable"))
        self.assertTrue(is_forbidden_member("webui/.vite/cache", "runnable"))
        self.assertTrue(is_forbidden_member("webui/.cache/cache", "runnable"))
        self.assertTrue(is_forbidden_member("webui/.turbo/cache", "runnable"))
        self.assertTrue(is_forbidden_member("webui/coverage/index.html", "runnable"))
        self.assertTrue(is_forbidden_member("playwright-report/index.html", "runnable"))
        self.assertTrue(is_forbidden_member("test-results/result.json", "runnable"))
        self.assertTrue(is_forbidden_member("webui/dist/private.zip", "runnable"))
        self.assertFalse(is_forbidden_member("webui/dist/index.html", "runnable"))
        self.assertTrue(is_forbidden_member("webui/dist/index.html", "source"))

    def test_delivery_clean_directory_allows_root_git_but_rejects_nested_git(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / ".git").mkdir()
            (base / "webui" / "dist").mkdir(parents=True)
            (base / "webui" / "dist" / "index.html").write_text("", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(base)]):
                self.assertEqual(delivery_clean_main(), 0)
            nested = base / "pkg" / ".git"
            nested.mkdir(parents=True)
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(base)]):
                self.assertEqual(delivery_clean_main(), 1)

    def test_delivery_clean_zip_strips_single_root_ignoring_macosx_and_appledouble(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "macosx.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("project/webui/dist/index.html", "")
                zf.writestr("project/webui/dist/assets/app.js", "")
                zf.writestr("project/webui/dist/assets/style.css", "")
                zf.writestr("__MACOSX/project/._README.md", "")
                zf.writestr("__MACOSX/._project", "")
                zf.writestr("project/.DS_Store", "")
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(z)]):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(delivery_clean_main(), 1)
            output = buf.getvalue()
            self.assertIn("forbidden_delivery_paths", output)
            self.assertNotIn("webui/dist/index.html", output)
            self.assertIn("__MACOSX/project/._README.md", output)
            self.assertIn(".DS_Store", output)
            # Now add a real forbidden member
            z2 = base / "bad.zip"
            with zipfile.ZipFile(z2, "w") as zf:
                zf.writestr("project/webui/dist/index.html", "")
                zf.writestr("project/archive/local.db", "")
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(z2)]):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(delivery_clean_main(), 1)
            self.assertNotIn("webui/dist/index.html", buf.getvalue() or "")
            self.assertNotIn("webui/dist/assets/app.js", buf.getvalue() or "")
            self.assertIn("archive/local.db", buf.getvalue() or "")

    def test_delivery_clean_zip_rejects_dangerous_member_paths_before_root_strip(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for member in ("pkg/../README.md", "/pkg/README.md", "C:/pkg/README.md"):
                z = base / "danger.zip"
                with zipfile.ZipFile(z, "w") as zf:
                    zf.writestr(member, "x")
                with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(z)]):
                    with contextlib.redirect_stdout(io.StringIO()) as buf:
                        self.assertEqual(delivery_clean_main(), 1)
                self.assertIn(member.replace("\\", "/"), buf.getvalue())
                z.unlink()

    def test_delivery_clean_rejects_jsonl_logs_in_directory(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            log = base / "import.jsonl"
            log.write_text("{}", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(base)]):
                self.assertEqual(delivery_clean_main(), 1)
            log.unlink()
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(base)]):
                self.assertEqual(delivery_clean_main(), 0)

    def test_delivery_clean_rejects_sensitive_and_cross_platform_pollutants(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "webui" / "dist" / "assets").mkdir(parents=True)
            (base / "webui" / "dist" / "index.html").write_text("", encoding="utf-8")
            (base / "webui" / "dist" / "assets" / "app.js").write_text("", encoding="utf-8")
            for rel in (
                "exports/manifest.csv",
                "private.zip",
                "local.db-journal",
                "local.sqlite-wal",
                "local.sqlite-shm",
                "local.sqlite-journal",
                "local.sqlite3-wal",
                "local.sqlite3-shm",
                "local.sqlite3-journal",
                "Thumbs.db",
                "Desktop.ini",
                ".coverage.unit",
                ".mypy_cache/cache",
                ".ruff_cache/cache",
                ".tox/cache",
                ".nox/cache",
                "htmlcov/index.html",
                "webui/.vite/cache",
                "webui/.turbo/cache",
                "playwright-report/index.html",
                "test-results/result.json",
            ):
                path = base / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(base)]):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(delivery_clean_main(), 1)
            output = buf.getvalue()
            self.assertIn("exports", output)
            self.assertIn("private.zip", output)
            self.assertIn("local.sqlite-wal", output)
            self.assertIn("Thumbs.db", output)
            self.assertIn("webui/.vite", output)
            self.assertNotIn(str(base), output)
            for rel in (
                "exports",
                "private.zip",
                "local.db-journal",
                "local.sqlite-wal",
                "local.sqlite-shm",
                "local.sqlite-journal",
                "local.sqlite3-wal",
                "local.sqlite3-shm",
                "local.sqlite3-journal",
                "Thumbs.db",
                "Desktop.ini",
                ".coverage.unit",
                ".mypy_cache",
                ".ruff_cache",
                ".tox",
                ".nox",
                "htmlcov",
                "webui/.vite",
                "webui/.turbo",
                "playwright-report",
                "test-results",
            ):
                path = base / rel
                if path.is_dir():
                    __import__("shutil").rmtree(path)
                elif path.exists():
                    path.unlink()
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(base)]):
                self.assertEqual(delivery_clean_main(), 0)

    def test_gitignore_covers_sensitive_outputs_and_preserves_webui_dist(self):
        text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            "archive/",
            "exports/",
            "*.db-journal",
            "*.sqlite-journal",
            "*.sqlite-shm",
            "*.sqlite-wal",
            "*.sqlite3-journal",
            "*.sqlite3-shm",
            "*.sqlite3-wal",
            "*.zip",
            "*.jsonl",
            ".coverage",
            ".coverage.*",
            "htmlcov/",
            ".mypy_cache/",
            ".ruff_cache/",
            ".tox/",
            ".nox/",
            "webui/node_modules/",
            "webui/tsconfig.tsbuildinfo",
            "webui/.vite/",
            "webui/.cache/",
            "webui/coverage/",
            "webui/.turbo/",
            "playwright-report/",
            "test-results/",
            "Thumbs.db",
            "Desktop.ini",
        ):
            self.assertIn(pattern, text)
        self.assertRegex(text, r"(?m)^!webui/dist/$")
        self.assertRegex(text, r"(?m)^!webui/dist/\*\*$")

    def test_cli_search_help_matches_safe_query_contract(self):
        parser = build_parser()
        subparser_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        full_help = subparser_action.choices["search"].format_help()
        normalized_help = re.sub(r"\s+", " ", full_help)
        self.assertIn("project query syntax", normalized_help)
        self.assertIn("not snippets", normalized_help)
        self.assertNotIn("FTS5", normalized_help)
        self.assertNotIn("timestamps", normalized_help)

    def test_clean_generated_artifacts_dry_run_and_execute(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pycache = base / "pkg" / "__pycache__"
            pycache.mkdir(parents=True)
            pyc = pycache / "module.cpython-313.pyc"
            pyc.write_bytes(b"bytecode")
            pyo = base / "pkg" / "module.pyo"
            pyo.write_bytes(b"optimized")
            readonly = base / "pkg" / "readonly.pyc"
            readonly.write_bytes(b"readonly")
            readonly.chmod(0o400)
            pytest_cache = base / ".pytest_cache"
            pytest_cache.mkdir()
            for rel in (
                ".mypy_cache",
                ".ruff_cache",
                ".tox",
                ".nox",
                "htmlcov",
                "build",
                "dist",
                ".eggs",
                "pkg.egg-info",
                "webui/.vite",
                "webui/.cache",
                "webui/coverage",
                "webui/.turbo",
                "playwright-report",
                "test-results",
                "__MACOSX",
            ):
                (base / rel).mkdir(parents=True)
            for rel in (".coverage", ".coverage.unit", ".DS_Store", "Thumbs.db", "Desktop.ini"):
                (base / rel).write_text("generated", encoding="utf-8")
            node_modules = base / "webui" / "node_modules"
            node_modules.mkdir(parents=True)
            (node_modules / "dep.txt").write_text("generated", encoding="utf-8")
            tsbuild = base / "webui" / "tsconfig.tsbuildinfo"
            tsbuild.write_text("generated", encoding="utf-8")
            web_dist = base / "webui" / "dist"
            web_dist.mkdir(parents=True)
            (web_dist / "index.html").write_text("built", encoding="utf-8")
            keep_db = base / "archive.db"
            keep_db.write_bytes(b"db")
            keep_sidecar = base / "archive.sqlite-wal"
            keep_sidecar.write_bytes(b"wal")
            keep_zip = base / "input.zip"
            keep_zip.write_bytes(b"zip")
            keep_jsonl = base / "import.jsonl"
            keep_jsonl.write_text("{}", encoding="utf-8")
            keep_conversations = base / "conversations-000.json"
            keep_conversations.write_text("[]", encoding="utf-8")
            keep_archive = base / "archive"
            keep_archive.mkdir()
            keep_exports = base / "exports"
            keep_exports.mkdir()
            keep_log = base / "logs" / "keep.log"
            keep_log.parent.mkdir()
            keep_log.write_text("user log", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["clean_generated_artifacts.py", "--dry-run", "--root", str(base)]):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(clean_generated_main(), 0)
            output = buf.getvalue()
            self.assertIn("__pycache__", output)
            self.assertIn("webui/node_modules", output)
            self.assertIn("blocked_sensitive_paths", output)
            self.assertIn("archive.db", output)
            self.assertIn("conversations-000.json", output)
            self.assertIn("exports", output)
            self.assertNotIn(str(base), output)
            self.assertTrue(pyc.exists())
            self.assertTrue(tsbuild.exists())
            self.assertTrue(node_modules.exists())
            with mock.patch.object(sys, "argv", ["clean_generated_artifacts.py", "--root", str(base)]):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(clean_generated_main(), 0)
            self.assertNotIn(str(base), buf.getvalue())
            self.assertFalse(pycache.exists())
            self.assertFalse(pyo.exists())
            self.assertFalse(readonly.exists())
            self.assertFalse(pytest_cache.exists())
            self.assertFalse(node_modules.exists())
            self.assertFalse(tsbuild.exists())
            self.assertFalse((base / ".mypy_cache").exists())
            self.assertFalse((base / "webui" / ".vite").exists())
            self.assertFalse((base / "Thumbs.db").exists())
            self.assertFalse((base / "__MACOSX").exists())
            self.assertTrue(web_dist.exists())
            self.assertTrue(keep_db.exists())
            self.assertTrue(keep_sidecar.exists())
            self.assertTrue(keep_zip.exists())
            self.assertTrue(keep_jsonl.exists())
            self.assertTrue(keep_conversations.exists())
            self.assertTrue(keep_archive.exists())
            self.assertTrue(keep_exports.exists())
            self.assertTrue(keep_log.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is not available")
    def test_clean_generated_artifacts_unlinks_symlink_without_chmod_target(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            target = base / "target.txt"
            target.write_text("synthetic", encoding="utf-8")
            target.chmod(0o400)
            link = base / "link.pyc"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {type(exc).__name__}")
            before_mode = target.stat().st_mode & 0o777
            try:
                with mock.patch.object(sys, "argv", ["clean_generated_artifacts.py", "--root", str(base)]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(clean_generated_main(), 0)
                self.assertFalse(link.exists())
                self.assertTrue(target.exists())
                self.assertEqual(target.stat().st_mode & 0o777, before_mode)
            finally:
                target.chmod(0o600)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is not available")
    def test_clean_generated_artifacts_rmtree_recovery_does_not_chmod_symlink_target(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            generated_dir = base / "__pycache__"
            generated_dir.mkdir()
            target = base / "target.txt"
            target.write_text("synthetic", encoding="utf-8")
            target.chmod(0o400)
            link = generated_dir / "module.pyc"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {type(exc).__name__}")
            before_mode = target.stat().st_mode & 0o777

            def retry_unlink(path):
                Path(path).unlink()

            try:
                clean_generated_artifacts._rmtree_onexc(retry_unlink, str(link), None)
                self.assertFalse(link.exists())
                self.assertTrue(target.exists())
                self.assertEqual(target.stat().st_mode & 0o777, before_mode)
            finally:
                target.chmod(0o600)

    def test_clean_generated_artifacts_fail_on_blocked_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            blocked = base / "archive.sqlite3-journal"
            blocked.write_text("sidecar", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["clean_generated_artifacts.py", "--root", str(base)]):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(clean_generated_main(), 0)
            self.assertIn("blocked_sensitive_paths_found 1", buf.getvalue())
            with mock.patch.object(sys, "argv", ["clean_generated_artifacts.py", "--fail-on-blocked", "--root", str(base)]):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(clean_generated_main(), 1)
            self.assertIn("archive.sqlite3-journal", buf.getvalue())
            self.assertTrue(blocked.exists())

    def test_dom_smoke_python_resolution_self_test(self):
        node = __import__("shutil").which("node")
        if not node:
            self.skipTest("node executable is unavailable")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, "webui/tests/dom-smoke.mjs", "--self-test-python-resolution"],
            cwd=root,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("python_resolution ok", result.stdout)

    def test_readme_command_blocks_and_valid_conversations_note_stay_synchronized(self):
        root = Path(__file__).resolve().parents[1]
        readmes = [
            root / "README.md",
            root / "README.zh-CN.md",
            root / "README.zh-TW.md",
            root / "README.ja-JP.md",
            root / "README.es-ES.md",
        ]
        command_blocks = []
        heading_shapes = []
        env_names = []
        common_required = [
            "CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS",
            "CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS",
            "CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS",
            "100,000",
            "10,000",
            "raw_text",
            "web-index",
            "webui/dist",
            "webui/src",
            "webui/tests",
            "webui/scripts",
            "webui/node_modules",
            "webui/tsconfig.tsbuildinfo",
            "message_fts",
            "optional_message_fts_error",
            "database_malformed",
            "database_locked",
            "database_readonly",
            "database_io_error",
            "database_runtime_failure",
            "web_message_norm",
            "web_title_norm",
            "web_message_trigram",
            "web_title_trigram",
            "legacy raw FTS",
            "candidate backend",
            "normalized title scan",
            "full scan",
            "remote-safe",
            "200.0",
            "1000.0",
            "PRAGMA user_version",
            "database_migration_required",
            "upload_preflight_failed",
            "invalid_conversation_encoding",
            "json_integer_too_large",
            "source_read_failed",
            "cleanup_warnings",
            "display_text",
            "/api/schema",
            "current-path node",
            "--input conversations.json",
            "--input ./extracted-export/",
            "conversations-*.json",
            "scanner discovery",
            ".DS_Store",
            "around_node_id",
            "visible-only rows",
            "effective all-node collection",
            "current_collection_source",
            "current_path_fallback_to_all",
            "constraints-web-py312.txt",
            "Content-Length",
            "Cmd/Ctrl+F",
            "normalized contains",
            "path:",
            "scope:",
            "CON",
            "COM¹",
            "LPT²",
            "._*",
            "__MACOSX",
            "conversations*.json",
            "*.jsonl",
            "*.zip",
            "archive/",
            "exports/",
            "ZIP64",
            "WAL",
            "/web-index/cancel",
            "--allowed-hosts",
            "--trusted-proxies",
            "Sec-Fetch-Site: cross-site",
            "staging",
        ]
        localized_required = {
            "README.md": ["Copy current path conversation", "Copy visible", "bounded larger raw preview", "current reader path", "conversation-level"],
            "README.zh-CN.md": ["复制当前路径整段对话", "复制当前可见", "有上限的较大 raw 预览", "当前 reader 路径", "conversation-level"],
            "README.zh-TW.md": ["複製目前路徑整段對話", "複製目前可見", "有上限的較大 raw 預覽", "目前 reader 路徑", "conversation-level"],
            "README.ja-JP.md": ["現在のパスの会話をコピー", "表示中をコピー", "上限付きの大きな raw プレビュー", "現在の reader パス", "conversation-level"],
            "README.es-ES.md": ["Copiar conversación de la ruta actual", "Copiar visibles", "vista previa raw ampliada con límite", "ruta actual del reader", "nivel conversación"],
        }
        banned_raw_promises = [
            "raw" + "TooLarge",
            "load" + "FullRaw",
            "full raw " + "JSON",
            "Full raw " + "JSON",
            "完整 raw " + "JSON",
            "完全な raw " + "JSON",
            "raw " + "JSON completo",
            "complete raw " + "payload",
            "完整原始" + "载荷",
        ]
        for path in readmes:
            text = path.read_text(encoding="utf-8")
            command_blocks.append(re.findall(r"```bash\n(.*?)\n```", text, flags=re.S))
            heading_shapes.append(re.findall(r"^(#{1,4})\s+", text, flags=re.M))
            env_names.append(sorted(set(re.findall(r"CHATGPT_ARCHIVE_[A-Z0-9_]+", text))))
            for literal in common_required:
                self.assertIn(literal.lower(), text.lower(), f"{path.name} missing {literal}")
            for literal in localized_required[path.name]:
                self.assertIn(literal, text, f"{path.name} missing localized fact {literal}")
            for literal in banned_raw_promises:
                self.assertNotIn(literal, text, f"{path.name} should not retain old raw promise {literal}")
            self.assertIn("valid_conversations", text)
            self.assertIn("inserted_conversations", text)
            self.assertIn("updated_conversations", text)
            self.assertIn("unchanged_conversations", text)
            self.assertIn("clean_generated_artifacts.py", text)
            self.assertIn("--fail-on-blocked", text)
            self.assertIn("py -3 tools/clean_generated_artifacts.py", text)
            self.assertIn("set NEW_ZIP=%USERPROFILE%", text)
            self.assertIn('"%NEW_ZIP%"', text)
            self.assertIn("sqlite", text.lower())
            self.assertIn("exports", text)
            self.assertIn("archive", text)
            self.assertIn("logs", text)
            self.assertNotIn("FTS5 query syntax", text)
            self.assertNotIn("FTS5 查询语法", text)
            self.assertNotIn("FTS5 查詢語法", text)
            self.assertNotIn("sintaxis de consulta FTS5", text)
            self.assertNotIn("Search message text through the CLI FTS index", text)
            self.assertNotIn("Show counts and index status", text)
            self.assertNotIn("Prints IDs and timestamps", text)
            self.assertNotIn("输出只包含 conversation ID、node ID、角色和时间戳", text)
            self.assertNotIn("輸出只包含 conversation ID、node ID、角色與時間戳記", text)
            self.assertNotIn("role、タイムスタンプ", text)
            self.assertNotIn("roles y marcas de tiempo", text)
            self.assertNotIn("estado de índices", text)
            self.assertNotIn("find . -type", text)
            self.assertNotIn("rm -rf", text)
            lowered = text.lower()
            self.assertIn("windows", lowered)
            self.assertIn("powershell", lowered)
        self.assertTrue(all(blocks == command_blocks[0] for blocks in command_blocks[1:]))
        self.assertTrue(all(shape == heading_shapes[0] for shape in heading_shapes[1:]))
        self.assertTrue(all(names == env_names[0] for names in env_names[1:]))

    def test_stdout_backslashreplace_avoids_unicode_encode_error(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "不存在.zip"
            env = dict(**__import__("os").environ, PYTHONIOENCODING="ascii:strict")
            result = subprocess.run(
                [sys.executable, "chatgpt_archive.py", "inspect", "--input", str(missing)],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("ERROR:", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_inspect_and_scanner_errors_do_not_print_input_names_or_paths(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            secret_zip = base / "private-name.zip"
            write_zip(secret_zip, {"conversations.json": [conversation("inspect-private")]})
            code, output = run_cli(["inspect", "--input", str(secret_zip)])
            self.assertEqual(code, 0, output)
            self.assertIn("input_kind zip", output)
            self.assertNotIn("input_name", output)
            self.assertNotIn(secret_zip.name, output)
            self.assertNotIn(str(secret_zip), output)

            missing = base / "missing-private.zip"
            code, output = run_cli(["inspect", "--input", str(missing)])
            self.assertEqual(code, 2)
            self.assertIn("input_not_found", output)
            self.assertNotIn(missing.name, output)
            self.assertNotIn(str(missing), output)

            write_zip(base / "another-private.zip", {"conversations.json": []})
            old_cwd = Path.cwd()
            try:
                os.chdir(base)
                code, output = run_cli(["inspect"])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(code, 2)
            self.assertIn("multiple_zip_files_found count 2", output)
            self.assertNotIn(secret_zip.name, output)
            self.assertNotIn("another-private.zip", output)

    def test_readonly_cli_commands_do_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "missing.db"
            commands = [
                ["--db", str(db), "verify"],
                ["--db", str(db), "stats"],
                ["--db", str(db), "search", "synthetic"],
                ["--db", str(db), "export", "--out", str(base / "exports")],
            ]
            for args in commands:
                code, output = run_cli(args)
                self.assertEqual(code, 2, args)
                self.assertIn("database_not_found", output)
                self.assertFalse(db.exists())
                self.assertNotIn(str(db), output)

    def test_init_and_export_cli_summaries_do_not_print_absolute_paths(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "private-archive.db"
            out = base / "private-exports"
            code, init_output = run_cli(["--db", str(db), "init"])
            self.assertEqual(code, 0, init_output)
            self.assertIn("initialized_db true", init_output)
            self.assertNotIn(str(db), init_output)
            self.assertNotIn(db.name, init_output)

            z = base / "synthetic.zip"
            write_zip(z, {"conversations.json": [conversation("safe-export-summary")]})
            code, import_output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0, import_output)
            code, export_output = run_cli(["--db", str(db), "export", "--format", "md", "--out", str(out)])
            self.assertEqual(code, 0, export_output)
            self.assertIn(f"out_directory {out.resolve()}", export_output)
            self.assertNotIn(str(db), export_output)
            self.assertNotIn(db.name, export_output)

    def test_verify_wrong_schema_reports_structured_failure_without_paths(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "private-wrong-schema.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()
            before_conn = sqlite3.connect(db)
            try:
                before_tables = before_conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            finally:
                before_conn.close()
            code, output = run_cli(["--db", str(db), "verify"])
            self.assertEqual(code, 1, output)
            self.assertIn("ok false", output)
            self.assertIn("schema_ok false", output)
            self.assertIn("missing_tables", output)
            self.assertNotIn("no such table", output)
            self.assertNotIn(str(db), output)
            self.assertNotIn(db.name, output)
            self.assertNotIn("raw JSON", output)
            after_conn = sqlite3.connect(db)
            try:
                after_tables = after_conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            finally:
                after_conn.close()
            self.assertEqual(before_tables, after_tables)

    def test_cli_verify_reports_missing_columns_without_paths(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "private-half-old.db"
            conn = sqlite3.connect(db)
            try:
                conn.executescript(
                    """
                    CREATE TABLE conversations(
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        create_time REAL,
                        update_time REAL,
                        current_node TEXT
                    );
                    CREATE TABLE conversation_nodes(
                        conversation_id TEXT,
                        node_id TEXT,
                        parent_node_id TEXT,
                        message_id TEXT
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            code, output = run_cli(["--db", str(db), "verify"])
            self.assertEqual(code, 1, output)
            self.assertIn("schema_ok false", output)
            self.assertIn("missing_columns", output)
            self.assertIn("conversations.source_file", output)
            self.assertIn("conversation_nodes.content_text", output)
            self.assertNotIn(str(db), output)
            self.assertNotIn(db.name, output)
            self.assertNotIn("raw_json", output)

    def test_verify_requires_source_tracking_tables(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "archive.db"
            conn = connect(db)
            try:
                init_db(conn)
                conn.execute("DROP TABLE source_files")
                conn.execute("DROP TABLE file_index")
                conn.commit()
                result = verify_database(conn)
            finally:
                conn.close()
            self.assertFalse(result["ok"])
            self.assertFalse(result["schema_ok"])
            self.assertEqual(result["missing_tables"], ["file_index", "source_files"])

    def test_web_index_missing_database_reports_safe_error(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "private-web-index.db"
            code, output = run_cli(["--db", str(db), "web-index"])
            self.assertEqual(code, 2)
            self.assertIn("database_not_found", output)
            self.assertNotIn(str(db), output)
            self.assertNotIn(db.name, output)
        self.assertFalse(db.exists())

    def test_core_only_cli_works_without_web_dependencies_and_web_fails_fast(self):
        from chatgpt_export_archiver.cli import cmd_web

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "core.db"
            self.assertEqual(main(["init", "--db", str(db)]), 0)
            with mock.patch.dict(sys.modules, {"fastapi": None, "uvicorn": None}):
                self.assertEqual(main(["stats", "--db", str(db)]), 0)
                args = argparse.Namespace(
                    db=str(db), host="127.0.0.1", port=8787,
                    allow_fallback=False, log_level="warning",
                )
                with self.assertRaisesRegex(ValueError, "Missing Web dependency uvicorn"):
                    cmd_web(args)

    def test_web_index_uses_bulk_write_connection_pragmas(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "archive.db"
            conn = connect(db)
            try:
                init_db(conn)
            finally:
                conn.close()
            with mock.patch("chatgpt_export_archiver.web_db.configure_bulk_write_connection") as configure:
                create_web_indexes(db)
            configure.assert_called_once()

    def test_web_index_scale_is_batched_observable_and_resolves_once(self):
        from chatgpt_export_archiver import web_db as web_db_module
        from chatgpt_export_archiver.web_db import web_index_status

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "scale.db"
            conn = connect(db)
            init_db(conn)
            count = 20_000
            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, ?, ?)",
                ((f"c-{index:05d}", f"Synthetic title {index}", f"h-{index}") for index in range(count)),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, role, content_type, content_text) VALUES (?, ?, 'user', 'text', ?)",
                ((f"c-{index:05d}", f"n-{index:05d}", f"Synthetic searchable body {index}") for index in range(count)),
            )
            conn.commit()
            conn.close()

            progress: list[tuple[str, dict[str, Any]]] = []
            resolver_calls = 0
            real_resolver = web_db_module.recover_message_display_text

            def counted_resolver(*args, **kwargs):
                nonlocal resolver_calls
                resolver_calls += 1
                return real_resolver(*args, **kwargs)

            tracemalloc.start()
            try:
                with mock.patch.object(web_db_module, "recover_message_display_text", side_effect=counted_resolver):
                    result = create_web_indexes(
                        db,
                        batch_size=127,
                        progress_callback=lambda stage, state: progress.append((stage, dict(state))),
                    )
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(result["indexed_messages"], count)
            self.assertEqual(result["indexed_titles"], count)
            self.assertEqual(result["batch_size"], 127)
            self.assertTrue(result["atomic_publish"])
            self.assertEqual(resolver_calls, count)
            self.assertLess(peak, 96 * 1024 * 1024)
            self.assertEqual({stage for stage, _state in progress}, set(result["progress_stages"]))
            for stage in result["progress_stages"]:
                states = [state for observed, state in progress if observed == stage]
                self.assertTrue(states, stage)
                self.assertEqual(states[0]["processed"], 0)
                self.assertTrue(states[-1]["complete"])
                self.assertEqual(states[-1]["processed"], states[-1]["total"])
            check = connect_readonly(db)
            try:
                status = web_index_status(check)
            finally:
                check.close()
            self.assertTrue(status["web_normalized_indexed"])
            if result["trigram_available"]:
                self.assertTrue(status["web_normalized_trigram_indexed"])

    def test_web_index_cancel_and_failure_roll_back_to_old_current_index(self):
        from chatgpt_export_archiver import web_db as web_db_module
        from chatgpt_export_archiver.web_db import WebIndexBuildCancelled, web_index_status

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "atomic.db"
            conn = connect(db)
            init_db(conn)
            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, ?, ?)",
                ((f"c-{index}", f"Title {index}", f"h-{index}") for index in range(30)),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, role, content_type, content_text) VALUES (?, ?, 'user', 'text', ?)",
                ((f"c-{index}", f"n-{index}", f"body {index}") for index in range(30)),
            )
            conn.commit()
            conn.close()
            create_web_indexes(db, batch_size=7)

            def snapshot():
                check = connect_readonly(db)
                try:
                    return {
                        "schema": [tuple(row) for row in check.execute(
                            "SELECT name, type, sql FROM sqlite_master WHERE name LIKE 'web_%' ORDER BY name"
                        )],
                        "metadata": [tuple(row) for row in check.execute(
                            "SELECT key, value FROM web_index_metadata ORDER BY key"
                        )],
                        "messages": [tuple(row) for row in check.execute(
                            "SELECT conversation_id, node_id, content_norm FROM web_message_norm ORDER BY conversation_id, node_id"
                        )],
                        "titles": [tuple(row) for row in check.execute(
                            "SELECT conversation_id, title_norm FROM web_title_norm ORDER BY conversation_id"
                        )],
                        "status": web_index_status(check),
                    }
                finally:
                    check.close()

            before = snapshot()
            stop = {"requested": False}

            def request_cancel(stage, state):
                if stage == "scan_normalize_messages" and state["processed"] >= 7:
                    stop["requested"] = True

            with self.assertRaises(WebIndexBuildCancelled):
                create_web_indexes(
                    db,
                    batch_size=7,
                    progress_callback=request_cancel,
                    cancel_check=lambda: stop["requested"],
                )
            self.assertEqual(snapshot(), before)
            self.assertTrue(snapshot()["status"]["web_normalized_indexed"])

            with mock.patch.object(
                web_db_module,
                "_canonical_generations",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    create_web_indexes(db, batch_size=7)
            self.assertEqual(snapshot(), before)

    def test_round8_web_index_releases_writer_between_batches_and_rechecks_generation(self):
        from chatgpt_export_archiver.web_db import WebIndexBuildError

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "writer-window.db"
            conn = connect(db)
            init_db(conn)
            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, ?, ?)",
                ((f"c-{index}", f"Title {index}", f"h-{index}") for index in range(20)),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES (?, ?, ?)",
                ((f"c-{index}", f"n-{index}", f"body {index}") for index in range(20)),
            )
            conn.commit()
            conn.close()
            create_web_indexes(db, batch_size=3)
            snapshot_conn = sqlite3.connect(db)
            try:
                before = snapshot_conn.execute(
                    "SELECT conversation_id, title_norm FROM web_title_norm ORDER BY conversation_id"
                ).fetchall()
            finally:
                snapshot_conn.close()
            wrote = False
            writer_elapsed = 0.0

            def write_between_batches(stage, state):
                nonlocal wrote, writer_elapsed
                if stage != "scan_normalize_messages" or state["processed"] < 3 or wrote:
                    return
                writer = sqlite3.connect(db, timeout=1)
                started = time.perf_counter()
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE conversations SET title='changed' WHERE conversation_id='c-0'")
                writer.commit()
                writer_elapsed = time.perf_counter() - started
                writer.close()
                wrote = True

            with self.assertRaises(WebIndexBuildError) as raised:
                create_web_indexes(
                    db, batch_size=3, progress_callback=write_between_batches
                )
            self.assertEqual(
                raised.exception.code, "web_index_generation_changed_before_publish"
            )
            self.assertTrue(wrote)
            self.assertLess(writer_elapsed, 1.0)
            check = sqlite3.connect(db)
            self.assertEqual(
                check.execute(
                    "SELECT conversation_id, title_norm FROM web_title_norm ORDER BY conversation_id"
                ).fetchall(),
                before,
            )
            self.assertEqual(check.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'web_%_build%'"
            ).fetchone()[0], 0)
            check.close()

    def test_round8_web_index_fts_binds_flush_on_utf8_byte_budget(self):
        from chatgpt_export_archiver import web_db as web_db_module

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "fts-bind-budget.db"
            conn = connect(db)
            init_db(conn)
            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, ?, ?)",
                ((f"c-{index}", f"Title {index}", f"h-{index}") for index in range(6)),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES (?, ?, ?)",
                ((f"c-{index}", f"n-{index}", "ab cd " * 200) for index in range(6)),
            )
            conn.commit()
            conn.close()
            observed: list[int] = []

            def progress(stage, state):
                if stage in {"build_message_trigram", "build_title_trigram"}:
                    observed.append(int(state["current_batch_normalized_bytes"]))

            with mock.patch.object(web_db_module, "WEB_INDEX_FTS_BIND_BATCH_BYTES", 2_048):
                result = create_web_indexes(
                    db, batch_size=6, progress_callback=progress
                )
            self.assertTrue(observed)
            self.assertLessEqual(max(observed), 2_048)
            self.assertEqual(result["fts_bind_batch_bytes"], 2_048)

    def test_optional_capability_metadata_propagates_runtime_sqlite_errors(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.sqlite_errors import is_optional_search_capability_missing
        from chatgpt_export_archiver.web_db import web_index_status

        self.assertTrue(is_optional_search_capability_missing(sqlite3.OperationalError(
            "malformed inverted index for FTS5 table main.web_message_trigram"
        )))
        self.assertTrue(is_optional_search_capability_missing(sqlite3.OperationalError(
            "malformed inverted index for FTS5 table main.web_title_trigram"
        )))
        self.assertFalse(is_optional_search_capability_missing(sqlite3.OperationalError(
            "database disk image is malformed"
        )))

        class Rows(list):
            def fetchone(self):
                return self[0] if self else None

            def fetchall(self):
                return list(self)

        class MetadataFailureConnection:
            def execute(self, sql, params=()):
                normalized = " ".join(sql.split()).casefold()
                if normalized.startswith("pragma main.schema_version"):
                    return Rows([(1,)])
                if "from sqlite_master" in normalized:
                    return Rows([("web_index_metadata",)])
                if normalized.startswith('pragma table_xinfo("web_index_metadata")'):
                    return Rows([(0, "key"), (1, "value")])
                if "select key, value from web_index_metadata" in normalized:
                    raise sqlite3.OperationalError("database is locked")
                raise AssertionError(sql)

        fake = MetadataFailureConnection()
        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            search_module._connection_capabilities(fake)
        schema = {
            "web_index_metadata": True,
            "web_message_norm": False,
            "web_title_norm": False,
            "web_message_trigram": False,
            "web_title_trigram": False,
        }
        with mock.patch("chatgpt_export_archiver.web_db.check_schema", return_value=schema):
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                web_index_status(fake)

    def test_legacy_single_file_imports(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {"conversations.json": [conversation("legacy-1")]})
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "init"]), 0)
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
            conn.close()

    def test_import_recreates_rebuildable_node_index_and_reimport_stays_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("index-rebuild")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256", "--rebuild-fts"]), 0)
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256", "--rebuild-fts"]), 0)
            conn = sqlite3.connect(db)
            try:
                index_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_nodes_conversation_path'"
                ).fetchone()
                self.assertIsNotNone(index_sql)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0], 5)
                statuses = conn.execute(
                    "SELECT summary_json FROM import_runs ORDER BY id"
                ).fetchall()
                self.assertIn('"unchanged_conversations":1', statuses[-1][0])
            finally:
                conn.close()

    def test_shards_skip_only_bad_elements(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(
                z,
                {
                    "conversations-000.json": [conversation("shard-1"), {}],
                    "conversations-001.json": [conversation("shard-2")],
                },
            )
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM import_warnings WHERE warning_type='missing_id'").fetchone()[0], 1)
            conn.close()

    def test_duplicate_conversation_ids_last_wins_in_single_file_and_warns_safely(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "synthetic.zip"
            db = base / "archive.db"
            first = conversation("dup-1", title="PRIVATE_TITLE_FIRST")
            second_mapping = {
                "root": null_message_node("root", None, ["n"]),
                "n": message_node("n", "root", "user", "PRIVATE_BODY_LAST", 10, []),
            }
            second = conversation("dup-1", title="PRIVATE_TITLE_LAST", current_node="n", mapping=second_mapping)
            write_zip(z, {"conversations.json": [first, second]})
            code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            self.assertNotIn("UNIQUE constraint", output)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT title FROM conversations WHERE conversation_id='dup-1'").fetchone()[0], "PRIVATE_TITLE_LAST")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes WHERE conversation_id='dup-1'").fetchone()[0], 2)
                warning = conn.execute("SELECT warning_type, keys_json, raw_json FROM import_warnings WHERE warning_type='duplicate_conversation_id'").fetchone()
                self.assertIsNotNone(warning)
                payload = json.dumps(dict(warning))
                self.assertIn("last_wins", payload)
                self.assertNotIn("PRIVATE_TITLE", payload)
                self.assertNotIn("PRIVATE_BODY", payload)
                self.assertNotIn(str(base), payload)
            finally:
                conn.close()

    def test_duplicate_conversation_ids_across_shards_same_and_different_hash(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            same_zip = base / "same.zip"
            diff_zip = base / "diff.zip"
            db = base / "archive.db"
            same = conversation("dup-shard", title="Same")
            write_zip(same_zip, {"conversations-000.json": [same], "conversations-001.json": [same]})
            code, output = run_cli(["--db", str(db), "import", "--input", str(same_zip), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            write_zip(
                diff_zip,
                {
                    "conversations-000.json": [conversation("dup-shard", title="Old")],
                    "conversations-001.json": [conversation("dup-shard", title="New", mapping={"root": null_message_node("root", None, [])}, current_node="root")],
                },
            )
            code, output = run_cli(["--db", str(db), "import", "--input", str(diff_zip), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            self.assertNotIn("UNIQUE constraint", output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id='dup-shard'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT title FROM conversations WHERE conversation_id='dup-shard'").fetchone()[0], "New")
                self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM import_warnings WHERE warning_type='duplicate_conversation_id'").fetchone()[0], 2)
            finally:
                conn.close()

    def test_malformed_conversation_id_and_title_are_tolerated_safely(self):
        cases = [
            ({}, "missing_id"),
            ({"id": None}, "invalid_conversation_id"),
            ({"id": ""}, "invalid_conversation_id"),
            ({"id": "   "}, "invalid_conversation_id"),
            ({"id": {}}, "invalid_conversation_id"),
            ({"id": []}, "invalid_conversation_id"),
        ]
        for patch, warning_type in cases:
            item = {"mapping": {}, **patch}
            warning = validate_conversation_element(item, "conversations.json", 0)
            self.assertIsNotNone(warning)
            self.assertEqual(warning.warning_type, warning_type)
        numeric = conversation(123, title=456)
        warning = validate_conversation_element(numeric, "conversations.json", 0)
        self.assertIsNone(warning)
        parsed = parse_conversation(numeric, "conversations.json", 0)
        self.assertEqual(parsed.conversation_id, "123")
        self.assertEqual(parsed.title, "456")
        fallback = conversation("", title=None)
        fallback["conversation_id"] = "fallback-id"
        warning = validate_conversation_element(fallback, "conversations.json", 0)
        self.assertIsNotNone(warning)
        self.assertEqual(warning.warning_type, "canonical_id_empty")
        bad_title = conversation("bad-title", title={"PRIVATE_TITLE": "hidden"})
        parsed = parse_conversation(bad_title, "conversations.json", 0)
        self.assertIsNone(parsed.title)
        self.assertEqual(parsed.warnings[0].warning_type, "invalid_title_type")
        payload = json.dumps(parsed.warnings[0].__dict__)
        self.assertNotIn("PRIVATE_TITLE", payload)

    def test_canonical_id_limit_rejects_unaddressable_graph_ids_without_values_in_warnings(self):
        accepted = "a" * 512
        rejected = "b" * 513

        valid = conversation(accepted, mapping={accepted: null_message_node(accepted, None, [])}, current_node=accepted)
        valid["conversation_id"] = "exported-valid"
        self.assertIsNone(validate_conversation_element(valid, "conversations.json", 0))
        parsed = parse_conversation(valid, "conversations.json", 0)
        self.assertEqual(len(parsed.conversation_id), 512)
        self.assertEqual(len(parsed.nodes[0].node_id), 512)

        cases = []
        over_conversation = conversation(rejected)
        over_conversation["conversation_id"] = "fallback-also-valid"
        cases.append(("id", over_conversation))
        for field in ("node_id", "parent", "children", "message_id", "current_node"):
            item = conversation(f"over-{field}", mapping={"root": null_message_node("root", None, [])}, current_node="root")
            if field == "node_id":
                item["mapping"] = {rejected: null_message_node(rejected, None, [])}
                item["current_node"] = rejected
            elif field == "parent":
                item["mapping"]["root"]["parent"] = rejected
            elif field == "children":
                item["mapping"]["root"]["children"] = [rejected]
            elif field == "message_id":
                item["mapping"]["root"] = message_node("root", None, "user", "synthetic", 1)
                item["mapping"]["root"]["message"]["id"] = rejected
            else:
                item["current_node"] = rejected
            cases.append((field, item))
        for field, item in cases:
            with self.subTest(field=field):
                warning = validate_conversation_element(item, "conversations.json", 4)
                self.assertIsNotNone(warning)
                self.assertEqual(warning.warning_type, "canonical_id_too_long")
                diagnostic = json.loads(warning.keys_json)
                self.assertEqual(diagnostic["length"], 513)
                self.assertEqual(diagnostic["limit"], 512)
                self.assertNotIn(rejected, warning.keys_json)

        numeric_valid = conversation(int("9" * 512))
        numeric_valid["conversation_id"] = "numeric-valid"
        self.assertIsNone(validate_conversation_element(numeric_valid, "conversations.json", 0))
        numeric_over = conversation(int("8" * 513))
        numeric_over["conversation_id"] = "numeric-fallback"
        warning = validate_conversation_element(numeric_over, "conversations.json", 0)
        self.assertEqual(warning.warning_type, "canonical_id_too_long")

    def test_id_length_matrix_is_identical_for_standalone_directory_and_zip_imports(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            rows = []
            expected_ids = set()
            for length in (511, 512, 513, 600, 1000):
                text_id = "s" * length
                text_row = conversation(text_id, mapping={"root": null_message_node("root", None, [])}, current_node="root")
                text_row["conversation_id"] = f"text-export-{length}"
                rows.append(text_row)
                numeric_id = int("9" * length)
                numeric_row = conversation(numeric_id, mapping={"root": null_message_node("root", None, [])}, current_node="root")
                numeric_row["conversation_id"] = f"numeric-export-{length}"
                rows.append(numeric_row)
                if length <= 512:
                    expected_ids.update({text_id, str(numeric_id)})

            encoded = json.dumps(rows).encode("utf-8")
            for mode in ("json", "directory", "zip"):
                root = base / mode
                root.mkdir()
                if mode == "zip":
                    target = root / "input.zip"
                    with zipfile.ZipFile(target, "w") as zf:
                        zf.writestr("conversations.json", encoded)
                else:
                    json_path = root / "conversations.json"
                    json_path.write_bytes(encoded)
                    target = json_path if mode == "json" else root
                db = base / f"{mode}.db"
                code, output = run_cli(["--db", str(db), "import", "--input", str(target), "--no-input-sha256"])
                self.assertEqual(code, 0, output)
                conn = sqlite3.connect(db)
                try:
                    actual = {row[0] for row in conn.execute("SELECT conversation_id FROM conversations")}
                    self.assertEqual(actual, expected_ids)
                    self.assertTrue(all(len(value) <= 512 for value in actual))
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM import_warnings WHERE warning_type='canonical_id_too_long'").fetchone()[0],
                        6,
                    )
                finally:
                    conn.close()

    def test_malformed_conversations_do_not_insert_string_none_or_bind_bad_title(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "malformed.zip"
            db = base / "archive.db"
            write_zip(
                z,
                {
                    "conversations.json": [
                        {"id": None, "title": "skip", "mapping": {}},
                        conversation("good-title-none", title=None),
                        conversation("good-title-list", title=["PRIVATE_TITLE"]),
                    ]
                },
            )
            code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            conn = sqlite3.connect(db)
            try:
                ids = {row[0] for row in conn.execute("SELECT conversation_id FROM conversations")}
                self.assertNotIn("None", ids)
                self.assertEqual(ids, {"good-title-none", "good-title-list"})
                self.assertIsNone(conn.execute("SELECT title FROM conversations WHERE conversation_id='good-title-list'").fetchone()[0])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM import_warnings WHERE warning_type='invalid_title_type'").fetchone()[0], 1)
            finally:
                conn.close()

    def test_zip_backslash_conversation_members_are_detected_and_imported(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(
                z,
                {
                    r"nested\conversations-000.json": [conversation("backslash-shard-1")],
                    r"nested\conversations-001.json": [conversation("backslash-shard-2")],
                },
            )
            source = resolve_input(str(z), Path.cwd())
            entries = list_source_entries(source)
            selected = [entry.source_path for entry in entries if entry.is_selected_conversation_source]
            self.assertEqual(selected, [r"nested\conversations-000.json", r"nested\conversations-001.json"])
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 2)
            finally:
                conn.close()

    def test_small_forced_zip64_member_uses_production_streaming_import(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive_path = base / "forced-zip64.zip"
            payload = json.dumps([conversation("forced-zip64")], separators=(",", ":")).encode("utf-8")
            with zipfile.ZipFile(archive_path, "w", allowZip64=True) as archive:
                with archive.open("conversations.json", "w", force_zip64=True) as member:
                    member.write(payload)

            source = resolve_input(str(archive_path), Path.cwd())
            self.assertEqual(
                [entry.source_path for entry in list_source_entries(source) if entry.is_selected_conversation_source],
                ["conversations.json"],
            )
            self.assertEqual(
                [item["id"] for item in iter_json_array_from_source(source, "conversations.json")],
                ["forced-zip64"],
            )
            db = base / "archive.db"
            code, output = run_cli(["--db", str(db), "import", "--input", str(archive_path), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT conversation_id FROM conversations").fetchone()[0], "forced-zip64")
            finally:
                conn.close()

    def test_standalone_conversations_json_is_detected_and_imported(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source_json = base / "conversations.json"
            source_json.write_text(json.dumps([conversation("standalone-json")]), encoding="utf-8")
            source = resolve_input(str(source_json), Path.cwd())
            self.assertEqual(source.kind, "json")
            entries = list_source_entries(source)
            self.assertEqual([entry.source_path for entry in entries if entry.is_selected_conversation_source], ["conversations.json"])
            self.assertEqual(_load_json_from_source_for_tests(source, "conversations.json")[0]["id"], "standalone-json")
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(source_json), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT input_kind FROM import_runs").fetchone()[0], "json")
            finally:
                conn.close()

    def test_streaming_json_and_bom_contract_are_identical_for_all_input_modes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            encoded = json.dumps([conversation("bom-ok")], separators=(",", ":")).encode("utf-8")

            def write_mode(mode: str, root: Path, payload: bytes) -> Path:
                root.mkdir()
                if mode == "zip":
                    target = root / "input.zip"
                    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as zf:
                        zf.writestr("conversations.json", payload)
                    return target
                target = root / "conversations.json"
                target.write_bytes(payload)
                return target if mode == "json" else root

            for mode in ("json", "directory", "zip"):
                target = write_mode(mode, base / f"ok-{mode}", b"\xef\xbb\xbf" + encoded)
                db = base / f"ok-{mode}.db"
                code, output = run_cli(["--db", str(db), "import", "--input", str(target), "--no-input-sha256"])
                self.assertEqual(code, 0, output)
                conn = sqlite3.connect(db)
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
                finally:
                    conn.close()

            invalid_payloads = {
                "repeated": b"\xef\xbb\xbf\xef\xbb\xbf[]",
                "middle": b"[\xef\xbb\xbf]",
                "utf16": b"\xff\xfe[\x00]\x00",
                "invalid": b"[\xff]",
            }
            for label, payload in invalid_payloads.items():
                for mode in ("json", "directory", "zip"):
                    with self.subTest(label=label, mode=mode):
                        target = write_mode(mode, base / f"bad-{label}-{mode}", payload)
                        db = base / f"bad-{label}-{mode}.db"
                        code, output = run_cli(["--db", str(db), "import", "--input", str(target), "--no-input-sha256"])
                        self.assertNotEqual(code, 0)
                        self.assertIn("invalid_conversation_encoding", output)
                        conn = sqlite3.connect(db)
                        try:
                            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
                        finally:
                            conn.close()

    def test_streaming_array_bounds_memory_and_late_syntax_error_rolls_back_batches(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source_json = base / "conversations.json"
            source_json.write_text(
                json.dumps([conversation(f"stream-{index}", mapping={"root": null_message_node("root", None, [])}, current_node="root") for index in range(3000)]),
                encoding="utf-8",
            )
            source = resolve_input(str(source_json), base)
            tracemalloc.start()
            count = sum(1 for _ in iter_json_array_from_source(source, "conversations.json"))
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(count, 3000)
            self.assertLess(peak, 8_000_000)

            valid_prefix = ",".join(
                json.dumps(conversation(f"rollback-{index}", mapping={"root": null_message_node("root", None, [])}, current_node="root"))
                for index in range(250)
            )
            source_json.write_text(f"[{valid_prefix},{{\"broken\":]", encoding="utf-8")
            db = base / "rollback.db"
            code, output = run_cli(["--db", str(db), "import", "--input", str(source_json), "--no-input-sha256"])
            self.assertNotEqual(code, 0)
            self.assertIn("invalid_conversation_json", output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0], "failed")
            finally:
                conn.close()

    def test_zip_member_source_read_failures_have_stable_codes_and_zero_commits(self):
        encrypted_bytes = base64.b64decode(
            "UEsDBAoACQAAADsq8Fwpu0wNDgAAAAIAAAASABwAY29udmVyc2F0aW9ucy5qc29uVVQJAAMB+VdqAflXanV4CwABBPUBAAAEFAAAAF+dhP2iZvJmd92wvlBKUEsHCCm7TA0OAAAAAgAAAFBLAQIeAwoACQAAADsq8Fwpu0wNDgAAAAIAAAASABgAAAAAAAEAAACkgQAAAABjb252ZXJzYXRpb25zLmpzb25VVAUAAwH5V2p1eAsAAQT1AQAABBQAAABQSwUGAAAAAAEAAQBYAAAAagAAAAAA"
        )
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)

            def assert_failure(target: Path, label: str, patcher=None) -> None:
                db = base / f"{label}.db"
                context = patcher if patcher is not None else contextlib.nullcontext()
                with context:
                    code, output = run_cli(["--db", str(db), "import", "--input", str(target), "--no-input-sha256"])
                self.assertNotEqual(code, 0, output)
                self.assertIn(label, output)
                conn = sqlite3.connect(db)
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
                finally:
                    conn.close()

            encrypted = base / "encrypted.zip"
            encrypted.write_bytes(encrypted_bytes)
            assert_failure(encrypted, "encrypted_zip_member_not_supported")

            crc = base / "crc.zip"
            with zipfile.ZipFile(crc, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr("conversations.json", b"[]")
            damaged = bytearray(crc.read_bytes())
            name_length = int.from_bytes(damaged[26:28], "little")
            extra_length = int.from_bytes(damaged[28:30], "little")
            data_offset = 30 + name_length + extra_length
            damaged[data_offset] ^= 0x01
            crc.write_bytes(damaged)
            assert_failure(crc, "zip_member_crc_failed")

            missing = base / "missing.zip"
            write_zip(missing, {"conversations.json": []})
            real_getinfo = zipfile.ZipFile.getinfo
            calls = {"count": 0}

            def missing_on_read(zf, name):
                calls["count"] += 1
                if name == "conversations.json":
                    raise KeyError(name)
                return real_getinfo(zf, name)

            assert_failure(missing, "zip_member_not_found", mock.patch.object(zipfile.ZipFile, "getinfo", missing_on_read))

            changed = base / "changed.zip"
            replacement = base / "replacement.zip"
            write_zip(changed, {"conversations.json": []})
            write_zip(replacement, {"conversations.json": [], "note.txt": "different identity"})
            from chatgpt_export_archiver import cli as cli_module
            real_iter = cli_module.iter_source_array_sessions

            def replace_then_iter(source, entries):
                replacement.replace(changed)
                return real_iter(source, entries)

            assert_failure(
                changed,
                "source_changed_during_read",
                mock.patch.object(
                    cli_module,
                    "iter_source_array_sessions",
                    side_effect=replace_then_iter,
                    autospec=True,
                ),
            )

    def test_duplicate_zip_conversation_json_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "duplicate.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("conversations.json", json.dumps([conversation("first")]))
                zf.writestr("conversations.json", json.dumps([conversation("second")]))
            source = resolve_input(str(z), Path.cwd())
            with self.assertRaises(ValueError) as ctx:
                list_source_entries(source)
            self.assertIn("duplicate_conversation_json_source", str(ctx.exception))
            code, output = run_cli(["import", "--db", str(base / "archive.db"), "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 2)
            self.assertIn("ambiguous_conversation_sources", output)

            ambiguous = base / "ambiguous-shards.zip"
            write_zip(
                ambiguous,
                {
                    "a/conversations-001.json": [conversation("first-shard")],
                    r"b\conversations-1.json": [conversation("second-shard")],
                },
            )
            with self.assertRaisesRegex(ValueError, "ambiguous_conversation_source_identity shard:1"):
                list_source_entries(resolve_input(str(ambiguous), Path.cwd()))
            code, output = run_cli(["import", "--db", str(base / "ambiguous.db"), "--input", str(ambiguous), "--no-input-sha256"])
            self.assertEqual(code, 2)
            self.assertIn("ambiguous_conversation_sources", output)

    def test_import_file_strictness_rolls_back_malformed_shard_but_tolerates_bad_elements(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "archive.db"
            initial = base / "initial.zip"
            write_zip(initial, {"conversations.json": [conversation("keep-existing", title="Before")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(initial), "--no-input-sha256"]), 0)
            create_web_indexes(db)

            mixed = base / "mixed.zip"
            changed = conversation("keep-existing", title="Must Roll Back")
            with zipfile.ZipFile(mixed, "w") as zf:
                zf.writestr("conversations-000.json", json.dumps([changed]))
                zf.writestr("conversations-001.json", "[{not valid json")
            code, output = run_cli(["--db", str(db), "import", "--input", str(mixed), "--no-input-sha256"])
            self.assertEqual(code, 2)
            self.assertIn("invalid_conversation_json", output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT title FROM conversations WHERE conversation_id = 'keep-existing'").fetchone()[0], "Before")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM web_title_norm WHERE conversation_id = 'keep-existing'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM web_message_norm WHERE conversation_id = 'keep-existing'").fetchone()[0], 4)
                run = conn.execute("SELECT status, summary_json FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(run[0], "failed")
                self.assertEqual(json.loads(run[1])["failure_code"], "invalid_conversation_json")
            finally:
                conn.close()

            tolerant = base / "tolerant.zip"
            write_zip(tolerant, {"conversations.json": [None, {}, {"id": "empty", "mapping": {}}, conversation("valid-after-bad")]})
            code, output = run_cli(["--db", str(db), "import", "--input", str(tolerant), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id = 'valid-after-bad'").fetchone()[0], 1)
                warning_types = {row[0] for row in conn.execute("SELECT warning_type FROM import_warnings WHERE import_run_id = (SELECT MAX(id) FROM import_runs)")}
                self.assertIn("invalid_element_type", warning_types)
                self.assertIn("empty_mapping", warning_types)
            finally:
                conn.close()

            non_list = base / "non-list.zip"
            with zipfile.ZipFile(non_list, "w") as zf:
                zf.writestr("conversations.json", json.dumps({"id": "not-a-list"}))
            code, output = run_cli(["--db", str(db), "import", "--input", str(non_list), "--no-input-sha256"])
            self.assertEqual(code, 2)
            self.assertIn("conversation_json_top_level_not_list", output)

            no_source = base / "no-source.zip"
            with zipfile.ZipFile(no_source, "w") as zf:
                zf.writestr("other.json", json.dumps({"synthetic": True}))
            code, output = run_cli(["--db", str(db), "import", "--input", str(no_source), "--no-input-sha256"])
            self.assertEqual(code, 2)
            self.assertIn("no_conversation_sources", output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT title FROM conversations WHERE conversation_id = 'keep-existing'").fetchone()[0], "Before")
                self.assertEqual(conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0], "failed")
            finally:
                conn.close()

    def test_scanner_ignores_macos_metadata_conversation_sources(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "metadata.zip"
            write_zip(
                z,
                {
                    "conversations.json": [conversation("real-legacy")],
                    "__MACOSX/conversations.json": [conversation("metadata-legacy")],
                    "__MACOSX/foo/conversations-000.json": [conversation("metadata-shard")],
                    "._conversations.json": [conversation("appledouble-legacy")],
                    "foo/._conversations-000.json": [conversation("appledouble-shard")],
                    "__MACOSX/._conversations.json": [conversation("metadata-appledouble")],
                    ".DS_Store": {"not": "conversation"},
                },
            )
            source = resolve_input(str(z), Path.cwd())
            entries = list_source_entries(source)
            self.assertEqual([entry.source_path for entry in entries if entry.is_conversation_json], ["conversations.json"])
            self.assertEqual([entry.source_path for entry in entries if entry.is_selected_conversation_source], ["conversations.json"])
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual({row[0] for row in conn.execute("SELECT conversation_id FROM conversations")}, {"real-legacy"})
            finally:
                conn.close()

            directory = base / "extracted"
            (directory / "__MACOSX" / "foo").mkdir(parents=True)
            (directory / "foo").mkdir()
            (directory / "conversations.json").write_text(json.dumps([conversation("dir-real")]), encoding="utf-8")
            (directory / "__MACOSX" / "conversations.json").write_text(json.dumps([conversation("dir-metadata")]), encoding="utf-8")
            (directory / "__MACOSX" / "foo" / "conversations-000.json").write_text(json.dumps([conversation("dir-metadata-shard")]), encoding="utf-8")
            (directory / "._conversations.json").write_text(json.dumps([conversation("dir-appledouble")]), encoding="utf-8")
            (directory / "foo" / "._conversations-000.json").write_text(json.dumps([conversation("dir-appledouble-shard")]), encoding="utf-8")
            (directory / ".DS_Store").write_text("metadata", encoding="utf-8")
            dir_source = resolve_input(str(directory), Path.cwd())
            dir_entries = list_source_entries(dir_source)
            self.assertEqual([entry.source_path for entry in dir_entries if entry.is_conversation_json], ["conversations.json"])
            self.assertEqual([entry.source_path for entry in dir_entries if entry.is_selected_conversation_source], ["conversations.json"])

    def test_default_input_uses_directory_for_unambiguous_sharded_json(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "conversations-000.json").write_text(json.dumps([conversation("default-shard-0")]), encoding="utf-8")
            (base / "conversations-001.json").write_text(json.dumps([conversation("default-shard-1")]), encoding="utf-8")
            source = resolve_input(None, base)
            self.assertEqual(source.kind, "directory")
            self.assertEqual(source.path, base.resolve())
            self.assertIsNone(source.delete_target)
            entries = list_source_entries(source)
            self.assertEqual(
                [entry.source_path for entry in entries if entry.is_selected_conversation_source],
                ["conversations-000.json", "conversations-001.json"],
            )
            db = base / "archive.db"
            with contextlib.chdir(base):
                self.assertEqual(main(["--db", str(db), "import", "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT input_kind FROM import_runs").fetchone()[0], "directory")
            finally:
                conn.close()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_directory_scanner_rejects_symlinks_and_scan_open_replacement(self):
        from chatgpt_export_archiver.scanner import find_default_input

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            outside = base / "outside"
            outside.mkdir()
            outside_json = outside / "outside.json"
            outside_json.write_text(json.dumps([conversation("outside")]), encoding="utf-8")

            for link_kind in ("file", "directory", "chain", "broken"):
                with self.subTest(link_kind=link_kind):
                    root = base / f"root-{link_kind}"
                    root.mkdir()
                    if link_kind == "file":
                        (root / "conversations.json").symlink_to(outside_json)
                    elif link_kind == "directory":
                        target_dir = outside / "members"
                        target_dir.mkdir(exist_ok=True)
                        (target_dir / "conversations-000.json").write_text("[]", encoding="utf-8")
                        (root / "linked").symlink_to(target_dir, target_is_directory=True)
                    elif link_kind == "chain":
                        first = root / "first"
                        first.symlink_to(outside_json)
                        (root / "conversations.json").symlink_to(first)
                    else:
                        (root / "conversations.json").symlink_to(outside / "missing.json")
                    source = resolve_input(str(root), base)
                    with self.assertRaisesRegex(ValueError, "input_symlink_not_allowed"):
                        list_source_entries(source)

            root = base / "toctou"
            root.mkdir()
            member = root / "conversations.json"
            member.write_text(json.dumps([conversation("inside")]), encoding="utf-8")
            source = resolve_input(str(root), base)
            entries = list_source_entries(source)
            self.assertEqual([entry.source_path for entry in entries if entry.is_selected_conversation_source], ["conversations.json"])
            member.rename(root / "original.json")
            member.symlink_to(outside_json)
            with self.assertRaisesRegex(ValueError, "input_symlink_not_allowed"):
                _load_json_from_source_for_tests(source, "conversations.json")

            for name in ("notes.txt", "other.json", "image.png", "empty"):
                path = base / name
                path.write_bytes(b"")
                with self.assertRaisesRegex(ValueError, "input_not_supported"):
                    find_default_input(path)
            direct_link = base / "direct-link.json"
            direct_link.symlink_to(outside_json)
            with self.assertRaisesRegex(ValueError, "input_not_supported"):
                find_default_input(direct_link)

            normal = base / "normal"
            normal.mkdir()
            (normal / "conversations-000.json").write_text("[]", encoding="utf-8")
            (normal / "conversations-001.json").write_text("[]", encoding="utf-8")
            self.assertEqual(find_default_input(normal).kind, "directory")

    def test_default_input_keeps_ambiguous_json_selection_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "conversations.json").write_text(json.dumps([conversation("legacy")]), encoding="utf-8")
            (base / "conversations-000.json").write_text(json.dumps([conversation("shard")]), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                resolve_input(None, base)
            self.assertIn("multiple_conversation_json_files_found", str(ctx.exception))

    def test_inspect_counts_backslash_conversation_members_as_shards(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {r"nested\conversations-000.json": [conversation("inspect-backslash-1")]})
            code, output = run_cli(["inspect", "--input", str(z)])
            self.assertEqual(code, 0)
            self.assertIn("conversation_json_files 1", output)
            self.assertIn("selected_conversation_sources 1", output)
            self.assertIn("sharded true", output)
            self.assertIn("valid_conversations 1", output)

    def test_inspect_uses_parser_conversation_id_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "conversations.json"
            id_only = conversation("id-only")
            id_only.pop("conversation_id")
            conversation_id_only = conversation("ignored")
            conversation_id_only.pop("id")
            conversation_id_only["conversation_id"] = "conversation-id-only"
            conflicting = conversation("primary-id")
            conflicting["conversation_id"] = "secondary-id"
            duplicate_fallback = conversation("ignored-too")
            duplicate_fallback.pop("id")
            duplicate_fallback["conversation_id"] = "conversation-id-only"
            missing = conversation("missing")
            missing.pop("id")
            missing.pop("conversation_id")
            source.write_text(
                json.dumps([id_only, conversation_id_only, conflicting, duplicate_fallback, missing]),
                encoding="utf-8",
            )
            code, output = run_cli(["inspect", "--input", str(source)])
            self.assertEqual(code, 0, output)
            self.assertIn("valid_conversations 4", output)
            self.assertIn("invalid_elements 1", output)
            self.assertIn("duplicate_conversation_ids 1", output)

    def test_graph_saves_all_nodes_exports_current_path_only(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {"conversations-000.json": [conversation("graph-1")]})
            db = base / "archive.db"
            out = base / "exports"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes WHERE conversation_id='graph-1'").fetchone()[0], 5)
            conn.close()
            self.assertEqual(main(["--db", str(db), "export", "--format", "md", "--out", str(out)]), 0)
            md = next(out.glob("*.md")).read_text(encoding="utf-8")
            self.assertIn("answer", md)
            self.assertIn("part one\n\npart two", md)
            self.assertNotIn("not exported by default", md)

    def test_null_message_node_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            mapping = {"root": null_message_node("root", None, [])}
            write_zip(z, {"conversations.json": [conversation("null-1", current_node="root", mapping=mapping)]})
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT content_text FROM conversation_nodes").fetchone()[0], "")
            conn.close()

    def test_content_parts_multiple_strings_are_joined(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            mapping = {
                "root": null_message_node("root", None, ["n1"]),
                "n1": message_node("n1", "root", "assistant", ["alpha", "beta", "gamma"], 1, []),
            }
            write_zip(z, {"conversations.json": [conversation("parts-1", current_node="n1", mapping=mapping)]})
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            text = conn.execute("SELECT content_text FROM conversation_nodes WHERE node_id='n1'").fetchone()[0]
            self.assertEqual(text, "alpha\n\nbeta\n\ngamma")
            conn.close()

    def test_repeat_import_and_export_skip_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {"conversations.json": [conversation("repeat-1")]})
            db = base / "archive.db"
            out = base / "exports"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0], 5)
            conn.close()
            self.assertEqual(main(["--db", str(db), "export", "--format", "md", "--out", str(out)]), 0)
            path = next(out.glob("*.md"))
            first_mtime = path.stat().st_mtime_ns
            time.sleep(0.01)
            self.assertEqual(main(["--db", str(db), "export", "--format", "md", "--out", str(out)]), 0)
            self.assertEqual(path.stat().st_mtime_ns, first_mtime)

    def test_export_is_deterministic_across_two_directories(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {"conversations.json": [conversation("det-1"), conversation("det-2", title="Other")]})
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            out_a = base / "det_A"
            out_b = base / "det_B"
            self.assertEqual(main(["--db", str(db), "export", "--format", "md", "--out", str(out_a)]), 0)
            self.assertEqual(main(["--db", str(db), "export", "--format", "md", "--out", str(out_b)]), 0)
            self.assertEqual(file_hashes(out_a), file_hashes(out_b))

    def test_export_same_directory_second_run_writes_zero(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {"conversations.json": [conversation("stable-1"), conversation("stable-2", title="Second")]})
            db = base / "archive.db"
            out = base / "exports"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            code, first = run_cli(["--db", str(db), "export", "--format", "md", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("written 2", first)
            before = {p.relative_to(out).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in out.rglob("*") if p.is_file()}
            time.sleep(0.02)
            code, second = run_cli(["--db", str(db), "export", "--format", "md", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("written 0", second)
            self.assertIn("skipped_unchanged 2", second)
            after = {p.relative_to(out).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in out.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_export_does_not_include_exported_at_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {"conversations.json": [conversation("no-time-1")]})
            db = base / "archive.db"
            out = base / "exports"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "export", "--format", "all", "--out", str(out)]), 0)
            for path in out.rglob("*"):
                if path.is_file():
                    self.assertNotIn("exported_at", path.read_text(encoding="utf-8"))

    def test_export_filename_preserves_epoch_zero_with_nullish_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "epoch-zero.zip"
            item = conversation("epoch-zero", title="Epoch Zero", create_time=0)
            item["update_time"] = 1_700_000_000
            write_zip(z, {"conversations.json": [item]})
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            out = base / "exports"
            self.assertEqual(main(["--db", str(db), "export", "--out", str(out), "--format", "md"]), 0)
            self.assertTrue(any(path.name.startswith("1970-01-01_") for path in out.glob("*.md")))

    def test_reimport_same_zip_is_idempotent_for_data_tables(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {"conversations.json": [conversation("idem-1"), conversation("idem-2", title="Two")]})
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0)
            self.assertIn("unchanged_conversations 2", output)
            self.assertIn("inserted_conversations 0", output)
            self.assertIn("updated_conversations 0", output)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0], 10)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0], 8)
            except sqlite3.OperationalError:
                pass
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0], 2)
            conn.close()

    def test_changed_conversation_replaces_old_nodes_without_stale_nodes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            old_mapping = {
                "a": null_message_node("a", None, ["b"]),
                "b": message_node("b", "a", "user", "old text", 1, ["c"]),
                "c": message_node("c", "b", "assistant", "old answer", 2, []),
            }
            new_mapping = {
                "x": null_message_node("x", None, ["y"]),
                "y": message_node("y", "x", "user", "new text", 1, ["z"]),
                "z": message_node("z", "y", "assistant", "new answer", 2, []),
            }
            write_zip(z, {"conversations.json": [conversation("changed-1", current_node="c", mapping=old_mapping)]})
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            write_zip(z, {"conversations.json": [conversation("changed-1", current_node="z", mapping=new_mapping)]})
            code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0)
            self.assertIn("updated_conversations 1", output)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes WHERE conversation_id='changed-1'").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes WHERE node_id IN ('a','b','c')").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT current_node FROM conversations WHERE conversation_id='changed-1'").fetchone()[0], "z")
            try:
                fts_text = "\n".join(row[0] for row in conn.execute("SELECT content_text FROM message_fts").fetchall())
                self.assertNotIn("old text", fts_text)
                self.assertIn("new text", fts_text)
            except sqlite3.OperationalError:
                pass
            conn.close()

    def test_incremental_newer_export_inserts_updates_keeps_missing_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            old_zip = base / "export_old.zip"
            new_zip = base / "export_new.zip"
            db = base / "archive.db"

            old_changed = {
                "r": null_message_node("r", None, ["u"]),
                "u": message_node("u", "r", "user", "old question", 10, ["a"]),
                "a": message_node("a", "u", "assistant", "old answer", 11, []),
            }
            new_changed = {
                "r": null_message_node("r", None, ["u"]),
                "u": message_node("u", "r", "user", "updated question", 10, ["a"]),
                "a": message_node("a", "u", "assistant", "updated answer", 11, ["extra"]),
                "extra": message_node("extra", "a", "assistant", "new follow up", 12, []),
            }
            write_zip(
                old_zip,
                {
                    "conversations.json": [
                        conversation("inc-keep", title="Keep"),
                        conversation("inc-change", title="Change", current_node="a", mapping=old_changed),
                        conversation("inc-missing-later", title="Missing Later"),
                    ]
                },
            )
            write_zip(
                new_zip,
                {
                    "conversations.json": [
                        conversation("inc-keep", title="Keep"),
                        conversation("inc-change", title="Change", current_node="extra", mapping=new_changed),
                        conversation("inc-new", title="New"),
                    ]
                },
            )

            code, first = run_cli(["--db", str(db), "import", "--input", str(old_zip), "--no-input-sha256"])
            self.assertEqual(code, 0)
            self.assertIn("inserted_conversations 3", first)
            self.assertIn("updated_conversations 0", first)
            self.assertIn("unchanged_conversations 0", first)
            self.assertEqual(data_counts(db)["conversations"], 3)

            code, second = run_cli(["--db", str(db), "import", "--input", str(new_zip), "--no-input-sha256"])
            self.assertEqual(code, 0)
            self.assertIn("inserted_conversations 1", second)
            self.assertIn("updated_conversations 1", second)
            self.assertIn("unchanged_conversations 1", second)
            after_incremental = data_counts(db)
            self.assertEqual(after_incremental["conversations"], 4)
            self.assertEqual(after_incremental["nodes"], 19)
            self.assertEqual(after_incremental["message_fts"], 15)

            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id='inc-missing-later'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes WHERE conversation_id='inc-change'").fetchone()[0], 4)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes WHERE conversation_id='inc-change' AND node_id='extra'").fetchone()[0], 1)
            finally:
                conn.close()

            code, third = run_cli(["--db", str(db), "import", "--input", str(new_zip), "--no-input-sha256"])
            self.assertEqual(code, 0)
            self.assertIn("inserted_conversations 0", third)
            self.assertIn("updated_conversations 0", third)
            self.assertIn("unchanged_conversations 3", third)
            self.assertEqual(data_counts(db), after_incremental)

            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            indexed_once = data_counts(db)
            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            self.assertEqual(data_counts(db), indexed_once)

    def test_batch_import_rebuild_fts_and_optimize_preserve_incremental_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            old_zip = base / "old.zip"
            new_zip = base / "new.zip"
            db = base / "archive.db"
            old_changed = {
                "r": null_message_node("r", None, ["u"]),
                "u": message_node("u", "r", "user", "old needle", 10, ["a"]),
                "a": message_node("a", "u", "assistant", "old answer", 11, []),
            }
            new_changed = {
                "r": null_message_node("r", None, ["u"]),
                "u": message_node("u", "r", "user", "new needle", 10, ["a"]),
                "a": message_node("a", "u", "assistant", "new answer", 11, ["extra"]),
                "extra": message_node("extra", "a", "assistant", "new follow up", 12, []),
            }
            write_zip(old_zip, {"conversations-000.json": [conversation("batch-keep"), conversation("batch-change", current_node="a", mapping=old_changed)]})
            write_zip(
                new_zip,
                {"conversations-000.json": [conversation("batch-keep"), conversation("batch-change", current_node="extra", mapping=new_changed), conversation("batch-new"), {}]},
            )

            self.assertEqual(main(["--db", str(db), "import", "--input", str(old_zip), "--no-input-sha256"]), 0)
            code, output = run_cli([
                "--db",
                str(db),
                "import",
                "--input",
                str(new_zip),
                "--no-input-sha256",
                "--rebuild-fts",
                "--optimize-after-import",
            ])
            self.assertEqual(code, 0)
            self.assertIn("inserted_conversations 1", output)
            self.assertIn("updated_conversations 1", output)
            self.assertIn("unchanged_conversations 1", output)
            self.assertIn("skipped_invalid_elements 1", output)
            self.assertIn("rebuild_fts true", output)
            self.assertIn("optimize_fts_after_import false", output)
            self.assertIn("optimize_after_import true", output)

            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                self.assertTrue(verify_database(conn)["ok"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0], 14)
                fts_text = "\n".join(row[0] for row in conn.execute("SELECT content_text FROM message_fts").fetchall())
                self.assertIn("new needle", fts_text)
                self.assertNotIn("old needle", fts_text)
                summary = json.loads(conn.execute("SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0])
                self.assertTrue(summary["rebuild_fts"])
                self.assertTrue(summary["optimize_after_import"])
                self.assertFalse(summary["optimize_fts_after_import"])
                timing_keys = (
                    "source_scan_seconds",
                    "parse_and_upsert_seconds",
                    "fts_rebuild_seconds",
                    "pragma_optimize_seconds",
                    "finalize_commit_seconds",
                    "close_seconds",
                    "legacy_pre_commit_seconds",
                    "wall_total_seconds",
                    "total_import_seconds",
                )
                for key in timing_keys:
                    self.assertIsInstance(summary[key], (int, float))
                    self.assertGreaterEqual(summary[key], 0)
                subtotal = sum(summary[key] for key in ("source_scan_seconds", "parse_and_upsert_seconds", "fts_rebuild_seconds", "pragma_optimize_seconds"))
                self.assertGreaterEqual(summary["wall_total_seconds"] + 0.001, subtotal)
                self.assertAlmostEqual(summary["total_import_seconds"], summary["wall_total_seconds"], delta=0.001)
            finally:
                conn.close()

    def test_import_wall_time_includes_finalize_commit_delay(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "delay.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("commit-delay")]})
            from chatgpt_export_archiver.db import finish_import_run as real_finish_import_run

            def delayed_finish(conn, run_id, status, summary):
                time.sleep(0.02)
                return real_finish_import_run(conn, run_id, status, summary)

            with mock.patch("chatgpt_export_archiver.cli.finish_import_run", side_effect=delayed_finish):
                code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0)
            self.assertIn("finalize_commit_seconds", output)
            conn = sqlite3.connect(db)
            try:
                summary = json.loads(conn.execute("SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0])
            finally:
                conn.close()
            self.assertGreaterEqual(summary["finalize_commit_seconds"], 0.015)
            self.assertGreaterEqual(summary["wall_total_seconds"], summary["legacy_pre_commit_seconds"] + 0.015)
            self.assertAlmostEqual(summary["total_import_seconds"], summary["wall_total_seconds"], delta=0.001)

    def test_rebuild_fts_optimize_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z1 = base / "one.zip"
            z2 = base / "two.zip"
            db1 = base / "one.db"
            db2 = base / "two.db"
            write_zip(z1, {"conversations.json": [conversation("fts-opt-default")]})
            write_zip(z2, {"conversations.json": [conversation("fts-opt-explicit")]})
            calls: list[bool] = []

            def fake_rebuild(conn, *, optimize=False):
                calls.append(optimize)
                return True

            with mock.patch("chatgpt_export_archiver.cli.rebuild_message_fts", side_effect=fake_rebuild):
                code, output = run_cli(["--db", str(db1), "import", "--input", str(z1), "--no-input-sha256", "--rebuild-fts"])
                self.assertEqual(code, 0)
                self.assertIn("optimize_fts_after_import false", output)
                code, output = run_cli([
                    "--db",
                    str(db2),
                    "import",
                    "--input",
                    str(z2),
                    "--no-input-sha256",
                    "--rebuild-fts",
                    "--optimize-fts-after-import",
                ])
                self.assertEqual(code, 0)
                self.assertIn("optimize_fts_after_import true", output)
            self.assertEqual(calls, [False, True])

    def test_incremental_import_updates_node_metadata_and_raw_json_when_text_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            first_zip = base / "first.zip"
            second_zip = base / "second.zip"
            db = base / "archive.db"
            first = conversation("metadata-update")
            second = json.loads(json.dumps(first))
            second["mapping"]["u1"]["message"]["metadata"]["synthetic_marker"] = "updated"
            second["mapping"]["u1"]["message"]["author"]["name"] = "updated-author"
            second["mapping"]["u1"]["message"]["update_time"] = first["mapping"]["u1"]["message"]["update_time"] + 50
            write_zip(first_zip, {"conversations.json": [first]})
            write_zip(second_zip, {"conversations.json": [second]})
            code, output = run_cli(["--db", str(db), "import", "--input", str(first_zip), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            code, output = run_cli(["--db", str(db), "import", "--input", str(second_zip), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            self.assertIn("updated_conversations 1", output)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT author_name, update_time, metadata_json, raw_message_json FROM conversation_nodes WHERE conversation_id = ? AND node_id = ?",
                    ("metadata-update", "u1"),
                ).fetchone()
                self.assertEqual(row["author_name"], "updated-author")
                self.assertIn("synthetic_marker", row["metadata_json"])
                self.assertIn("synthetic_marker", row["raw_message_json"])
                self.assertEqual(conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0], "finished")
                node_text_count = conn.execute(
                    "SELECT COUNT(*) FROM conversation_nodes WHERE conversation_id = ? AND content_text <> ''",
                    ("metadata-update",),
                ).fetchone()[0]
                fts_count = conn.execute(
                    "SELECT COUNT(*) FROM message_fts WHERE conversation_id = ?",
                    ("metadata-update",),
                ).fetchone()[0]
                self.assertEqual(fts_count, node_text_count)
            finally:
                conn.close()

    def test_cli_search_invalid_fts_syntax_uses_safe_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "search.zip"
            db = base / "archive.db"
            mapping = {
                "root": null_message_node("root", None, ["n"]),
                "n": message_node("n", "root", "user", "C++ token and ordinary synthetic text", 10, []),
            }
            write_zip(z, {"conversations.json": [conversation("cli-search", current_node="n", mapping=mapping)]})
            code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            for query in ['"', "C++", "ordinary"]:
                code, output = run_cli(["--db", str(db), "search", query])
                self.assertEqual(code, 0, output)
                self.assertNotIn("fts5_available false", output)
            code, output = run_cli(["--db", str(db), "search", "ordinary"])
            self.assertIn("conversation_id cli-search", output)

    def test_cli_search_uses_bounded_candidate_queries(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "search.zip"
            db = base / "archive.db"
            mapping = {
                "root": null_message_node("root", None, ["n"]),
                "n": message_node("n", "root", "user", "common synthetic text", 10, []),
            }
            write_zip(z, {"conversations.json": [conversation("cli-search-bound", current_node="n", mapping=mapping)]})
            code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0, output)

            import chatgpt_export_archiver.search as search_module

            calls: list[tuple[int, int, str]] = []
            original_page_rows = search_module._message_search_page_rows

            def wrapped_page_rows(conn, parsed, conversation_id, limit, offset, order, *, use_trigram=True, count_total=True):
                calls.append((limit, offset, order, count_total))
                return original_page_rows(conn, parsed, conversation_id, limit, offset, order, use_trigram=use_trigram, count_total=count_total)

            with mock.patch.object(search_module, "_message_search_page_rows", wrapped_page_rows):
                code, output = run_cli(["--db", str(db), "search", "common", "--limit", "5"])
            self.assertEqual(code, 0, output)
            self.assertTrue(calls)
            self.assertTrue(all(limit == 5 and offset == 0 and order == "relevance" and count_total is False for limit, offset, order, count_total in calls))

    def test_limited_fts_message_rows_are_rank_ordered(self):
        import chatgpt_export_archiver.search as search_module

        class Cursor:
            def fetchall(self):
                return []

        class FakeConn:
            def __init__(self):
                self.sql = ""
                self.params = []

            def execute(self, sql, params=()):
                self.sql = sql
                self.params = list(params)
                return Cursor()

        conn = FakeConn()
        with mock.patch.object(search_module, "_ensure_search_functions"), mock.patch.object(
            search_module, "ensure_effective_current_views"
        ):
            search_module._fts_message_rows(conn, parse_query("common"), "common*", None, 5)
        normalized_sql = re.sub(r"\s+", " ", conn.sql)
        self.assertIn("ORDER BY bm25(message_fts) LIMIT ?", normalized_sql)
        self.assertEqual(conn.params, ["common*", 5])

    def test_delete_input_on_success_deletes_zip_only_after_success(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "delete-me.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("delete-success")]})
            code, output = run_cli([
                "--db",
                str(db),
                "import",
                "--input",
                str(z),
                "--no-input-sha256",
                "--delete-input-on-success",
            ])
            self.assertEqual(code, 0)
            self.assertIn("delete_input_on_success true", output)
            self.assertIn("deleted_input True", output)
            self.assertNotIn(str(z), output)
            self.assertNotIn(z.name, output)
            self.assertFalse(z.exists())
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                self.assertTrue(verify_database(conn)["ok"])
            finally:
                conn.close()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is not available")
    def test_delete_input_on_success_unlinks_explicit_symlink_not_zip_target(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            target = base / "target.zip"
            link = base / "latest.zip"
            db = base / "archive.db"
            write_zip(target, {"conversations.json": [conversation("delete-symlink")]})
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {type(exc).__name__}")
            code, output = run_cli([
                "--db",
                str(db),
                "import",
                "--input",
                str(link),
                "--no-input-sha256",
                "--delete-input-on-success",
            ])
            self.assertEqual(code, 0, output)
            self.assertFalse(link.exists())
            self.assertTrue(target.exists())
            self.assertNotIn(str(link), output)
            self.assertNotIn(str(target), output)
            self.assertNotIn(link.name, output)
            self.assertNotIn(target.name, output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0], "finished")
            finally:
                conn.close()

    def test_delete_input_on_success_unlink_failure_keeps_successful_import(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "locked.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("delete-unlink-failure")]})
            with mock.patch("chatgpt_export_archiver.scanner.os.unlink", side_effect=PermissionError("synthetic lock")):
                code, output = run_cli([
                    "--db",
                    str(db),
                    "import",
                    "--input",
                    str(z),
                    "--no-input-sha256",
                    "--delete-input-on-success",
                ])
            self.assertEqual(code, 0)
            self.assertIn("delete_input_on_success true", output)
            self.assertIn("delete_input_failed True", output)
            self.assertIn("delete_input_error_type PermissionError", output)
            self.assertNotIn("delete_input_recovery_required true", output)
            self.assertNotIn(str(z), output)
            self.assertNotIn(z.name, output)
            self.assertTrue(z.exists())
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                self.assertTrue(verify_database(conn)["ok"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0], "finished")
                warning = conn.execute("SELECT keys_json, raw_json FROM import_warnings WHERE warning_type='delete_input_failed'").fetchone()
                self.assertIsNotNone(warning)
                self.assertIn("PermissionError", warning["keys_json"])
                self.assertIsNone(warning["raw_json"])
                self.assertNotIn(str(z), json.dumps(dict(warning)))
                self.assertNotIn(z.name, json.dumps(dict(warning)))
            finally:
                conn.close()

    def test_delete_input_on_success_failure_keeps_zip(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "keep-me.zip"
            write_zip(z, {"conversations.json": [conversation("delete-failure")]})
            code, output = run_cli([
                "--db",
                str(base),
                "import",
                "--input",
                str(z),
                "--no-input-sha256",
                "--delete-input-on-success",
            ])
            self.assertNotEqual(code, 0)
            self.assertIn("ERROR:", output)
            self.assertTrue(z.exists())

    def test_delete_input_on_success_rejects_directory_input(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            input_dir = base / "input"
            input_dir.mkdir()
            (input_dir / "conversations.json").write_text(json.dumps([conversation("dir-delete-reject")]), encoding="utf-8")
            code, output = run_cli([
                "--db",
                str(base / "archive.db"),
                "import",
                "--input",
                str(input_dir),
                "--delete-input-on-success",
            ])
            self.assertEqual(code, 2)
            self.assertIn("--delete-input-on-success is only supported for ZIP inputs", output)

    def test_incremental_export_rewrites_only_changed_and_new_conversations(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            old_zip = base / "export_old.zip"
            new_zip = base / "export_new.zip"
            db = base / "archive.db"
            out = base / "exports"
            old_changed = {
                "r": null_message_node("r", None, ["u"]),
                "u": message_node("u", "r", "user", "old question", 10, ["a"]),
                "a": message_node("a", "u", "assistant", "old answer", 11, []),
            }
            new_changed = {
                "r": null_message_node("r", None, ["u"]),
                "u": message_node("u", "r", "user", "updated question", 10, ["a"]),
                "a": message_node("a", "u", "assistant", "updated answer", 11, ["extra"]),
                "extra": message_node("extra", "a", "assistant", "new follow up", 12, []),
            }
            write_zip(
                old_zip,
                {"conversations.json": [conversation("inc-keep", title="Keep"), conversation("inc-change", title="Change", current_node="a", mapping=old_changed)]},
            )
            write_zip(
                new_zip,
                {"conversations.json": [conversation("inc-keep", title="Keep"), conversation("inc-change", title="Change", current_node="extra", mapping=new_changed), conversation("inc-new", title="New")]},
            )

            self.assertEqual(main(["--db", str(db), "import", "--input", str(old_zip), "--no-input-sha256"]), 0)
            code, first_export = run_cli(["--db", str(db), "export", "--format", "md", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("written 2", first_export)
            before = {p.relative_to(out).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in out.glob("*.md")}
            time.sleep(0.02)
            code, second_export = run_cli(["--db", str(db), "export", "--format", "md", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("written 0", second_export)
            self.assertIn("skipped_unchanged 2", second_export)
            self.assertEqual(before, {p.relative_to(out).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in out.glob("*.md")})

            self.assertEqual(main(["--db", str(db), "import", "--input", str(new_zip), "--no-input-sha256"]), 0)
            time.sleep(0.02)
            code, after_import_export = run_cli(["--db", str(db), "export", "--format", "md", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("written 2", after_import_export)
            self.assertIn("skipped_unchanged 1", after_import_export)
            after = {p.relative_to(out).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in out.glob("*.md")}
            self.assertEqual(len(after), 3)
            keep_files = [name for name in before if "inc-keep" in name]
            self.assertEqual(len(keep_files), 1)
            self.assertEqual(after[keep_files[0]], before[keep_files[0]])

    def test_current_path_parent_chain_not_mapping_order(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            mapping = {
                "leaf": message_node("leaf", "mid", "assistant", "third", 3, []),
                "root": null_message_node("root", None, ["first"]),
                "mid": message_node("mid", "first", "user", "second", 2, ["leaf"]),
                "first": message_node("first", "root", "user", "first", 1, ["mid"]),
            }
            write_zip(z, {"conversations.json": [conversation("order-1", current_node="leaf", mapping=mapping)]})
            db = base / "archive.db"
            out = base / "exports"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "export", "--format", "md", "--out", str(out)]), 0)
            md = next(out.glob("*.md")).read_text(encoding="utf-8")
            self.assertLess(md.index("first"), md.index("second"))
            self.assertLess(md.index("second"), md.index("third"))

    def test_filename_collision_is_stable(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(
                z,
                {
                    "conversations.json": [
                        conversation("collisionABCDEFone", title="Same", create_time=1_700_000_000),
                        conversation("collisionABCDEFtwo", title="Same", create_time=1_700_000_000),
                    ]
                },
            )
            db = base / "archive.db"
            out_a = base / "a"
            out_b = base / "b"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "export", "--format", "md", "--out", str(out_a)]), 0)
            self.assertEqual(main(["--db", str(db), "export", "--format", "md", "--out", str(out_b)]), 0)
            names_a = sorted(p.name for p in out_a.glob("*.md"))
            names_b = sorted(p.name for p in out_b.glob("*.md"))
            self.assertEqual(names_a, names_b)
            self.assertEqual(len(names_a), len(set(names_a)))

    def test_manifest_is_stable_and_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            write_zip(z, {"conversations.json": [conversation("m-2", title="B"), conversation("m-1", title="A")]})
            db = base / "archive.db"
            out = base / "exports"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "export", "--format", "all", "--out", str(out)]), 0)
            csv_header = (out / "manifest.csv").read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(
                csv_header,
                "aggregate_hash,conversation_id,create_time,current_node,format,include_internal,output_hash,output_path,path,source_file,title,update_time",
            )
            rows = [json.loads(line) for line in (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["output_path"] for row in rows], sorted(row["output_path"] for row in rows))
            self.assertEqual((out / "manifest.jsonl").read_bytes(), (out / "manifest.jsonl").read_text(encoding="utf-8").encode("utf-8"))

    def test_export_batches_twenty_thousand_conversations_without_node_n_plus_one(self):
        from chatgpt_export_archiver import exporter

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "batch-export.db"
            conn = connect(db)
            init_db(conn)
            count = 20_000
            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, 'Synthetic', ?, ?)",
                ((f"export-{index:05d}", f"node-{index}", f"hash-{index}") for index in range(count)),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, role, content_text, is_on_current_path) VALUES (?, ?, 'user', 'synthetic export body', 1)",
                ((f"export-{index:05d}", f"node-{index}") for index in range(count)),
            )
            conn.commit()
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            tracemalloc.start()
            try:
                with (
                    mock.patch.object(exporter, "write_chunks_if_changed", return_value=(False, "synthetic-hash", 0)),
                    mock.patch.object(exporter, "record_export"),
                    mock.patch.object(exporter, "_write_manifest_from_plan"),
                    mock.patch.object(exporter, "_validate_archive_export_outputs"),
                ):
                    result = exporter.export_conversations(
                        conn,
                        Path(td) / "synthetic-output",
                        ["txt"],
                        conversation_batch_size=200,
                    )
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                conn.set_trace_callback(None)
                conn.close()
            node_selects = [
                sql for sql in statements
                if "FROM effective_current_nodes e" in sql
                and "JOIN conversation_nodes n" in sql
                and "ORDER BY n.conversation_id" in sql
            ]
            self.assertEqual(result["conversations"], count)
            self.assertEqual(len(node_selects), count // 200)
            self.assertFalse(any("WHERE conversation_id = 'export-" in sql for sql in statements))
            self.assertLess(peak, 160 * 1024 * 1024)

    @unittest.skipUnless(
        os.environ.get("CHATGPT_ARCHIVE_ROUND7_SCALE_TEST") == "1",
        "set CHATGPT_ARCHIVE_ROUND7_SCALE_TEST=1 for the million-conversation acceptance",
    )
    def test_round7_archive_export_one_million_conversation_production_path(self):
        from chatgpt_export_archiver import exporter

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            conn = sqlite3.connect(base / "scale.db")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            count = 1_000_000
            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, 'scale', 'h')",
                ((f"scale-{index:07d}",) for index in range(count)),
            )
            conn.commit()

            def synthetic_budgets(_conn, conversation_ids):
                return {
                    str(conversation_id): {
                        "node_count": 0,
                        "input_bytes": 0,
                        "max_node_bytes": 0,
                        "header_bytes": 0,
                    }
                    for conversation_id in conversation_ids
                }

            tracemalloc.start()
            started = time.perf_counter()
            with (
                mock.patch.object(exporter, "check_conversation_export_budgets", new=synthetic_budgets),
                mock.patch.object(exporter, "write_chunks_if_changed", new=lambda *_args, **_kwargs: (False, "synthetic-hash", 0)),
                mock.patch.object(exporter, "_write_manifest_from_plan", new=lambda *_args, **_kwargs: None),
                mock.patch.object(exporter, "_validate_archive_export_outputs", new=lambda *_args, **_kwargs: None),
                mock.patch.object(exporter, "record_export", new=lambda *_args, **_kwargs: None),
            ):
                result = exporter.export_conversations(conn, base / "output", ["txt"])
            elapsed = time.perf_counter() - started
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(result["conversations"], count)
            self.assertLess(peak, 256 * 1024 * 1024)
            self.assertLess(elapsed, 600)
            self.assertFalse(list((base / "output").glob(".chatgpt-archive-export-plan-*.sqlite3")))
            conn.close()

    def test_no_chat_content_in_cli_logs_for_import_export_verify(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            secret = "SECRET_PRIVATE_TEXT"
            z = base / "export.zip"
            mapping = {
                "root": null_message_node("root", None, ["n1"]),
                "n1": message_node("n1", "root", "user", secret, 1, []),
            }
            write_zip(z, {"conversations.json": [conversation("secret-1", current_node="n1", mapping=mapping)]})
            db = base / "archive.db"
            out = base / "exports"
            logs = []
            for args in (
                ["--db", str(db), "import", "--input", str(z), "--no-input-sha256"],
                ["--db", str(db), "export", "--format", "md", "--out", str(out)],
                ["--db", str(db), "verify"],
            ):
                code, output = run_cli(args)
                self.assertEqual(code, 0)
                logs.append(output)
            self.assertNotIn(secret, "\n".join(logs))

    def test_log_args_work_before_and_after_subcommand(self):
        parser = build_parser()

        def parse(argv):
            return parser.parse_args(argv)

        # --log-level before subcommand (old style)
        args = parse(["--log-level", "debug", "web", "--host", "127.0.0.1", "--port", "9999"])
        self.assertEqual(args.log_level, "debug")
        self.assertFalse(args.json_logs)

        # --log-level after subcommand (new style)
        args = parse(["web", "--log-level", "info", "--host", "127.0.0.1", "--port", "9999"])
        self.assertEqual(args.log_level, "info")

        # No --log-level at all: parent parser default
        args = parse(["web", "--host", "127.0.0.1", "--port", "9999"])
        self.assertEqual(args.log_level, "warning")

        # import with log arguments after subcommand
        args = parse(["import", "--db", "test.db", "--input", "export.zip", "--log-level", "info", "--log-file", "logs/import.log", "--no-input-sha256"])
        self.assertEqual(args.log_level, "info")
        self.assertEqual(args.log_file, "logs/import.log")

        # --json-logs after subcommand
        args = parse(["web", "--json-logs", "--host", "127.0.0.1", "--port", "9999"])
        self.assertTrue(args.json_logs)

        # Top-level --log-level with default subcommand defaults not overwriting
        args = parse(["--log-level", "error", "verify"])
        self.assertEqual(args.log_level, "error")

    def test_zh_tw_i18n_does_not_inherit_obvious_simplified_terms(self):
        text = (Path(__file__).resolve().parents[1] / "webui" / "src" / "i18n.ts").read_text(encoding="utf-8")
        zh_hant = text.split("const zhHant: Dict = {", 1)[1].split("};", 1)[0]
        self.assertNotIn("...zhHans", zh_hant)
        for simplified in ("搜索", "消息", "设置", "加载", "简体中文", "任务日志"):
            self.assertNotIn(simplified, zh_hant)
        for traditional in ("搜尋", "訊息", "設定", "載入", "紀錄檔"):
            self.assertIn(traditional, zh_hant)

    def test_web_ui_refreshes_after_postcheck_failed_import_job(self):
        text = (Path(__file__).resolve().parents[1] / "webui" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('job.status === "succeeded" || job.status === "postcheck_failed"', text)

    def test_missing_parent_and_current_node_warning(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            missing_parent = {"n1": message_node("n1", "missing", "user", "hello", 1, [])}
            write_zip(
                z,
                {
                    "conversations-000.json": [
                        conversation("missing-parent", current_node="n1", mapping=missing_parent),
                        conversation("missing-current", current_node="absent", mapping=missing_parent),
                    ]
                },
            )
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            warnings = {row[0] for row in conn.execute("SELECT warning_type FROM import_warnings").fetchall()}
            self.assertIn("parent_missing", warnings)
            self.assertIn("current_node_missing", warnings)
            conn.close()

    def test_integrity_failure_is_web_index_only_detects_web_table_errors(self):
        # Single line mentioning only web index tables → True
        self.assertTrue(_integrity_failure_is_web_index_only(
            ["malformed inverted index for FTS5 table main.web_message_trigram"]
        ))
        self.assertTrue(_integrity_failure_is_web_index_only(
            ["malformed inverted index for FTS5 table main.web_title_trigram"]
        ))
        # Shadow table lines → True
        self.assertTrue(_integrity_failure_is_web_index_only(
            ["wrong # of entries in index web_message_trigram_data"]
        ))
        self.assertTrue(_integrity_failure_is_web_index_only(
            ["wrong # of entries in index web_title_trigram_idx"]
        ))
        # Multiple web-index/shadow-only lines → True
        self.assertTrue(_integrity_failure_is_web_index_only([
            "malformed inverted index for FTS5 table main.web_message_trigram",
            "wrong # of entries in index web_message_trigram_data",
        ]))
        # Lines mentioning core tables → False
        self.assertFalse(_integrity_failure_is_web_index_only(
            ["wrong # of entries in index message_fts_idx"]
        ))
        self.assertFalse(_integrity_failure_is_web_index_only(
            ["row 5 missing from index conversations"]
        ))
        # Mixed web-index + core → False
        self.assertFalse(_integrity_failure_is_web_index_only([
            "malformed inverted index for FTS5 table main.web_message_trigram",
            "row 5 missing from index conversations",
        ]))
        # Mixed web-index shadow + core → False
        self.assertFalse(_integrity_failure_is_web_index_only([
            "wrong # of entries in index web_message_trigram_data",
            "row 123 missing from index some_core_index",
        ]))
        # Empty list
        self.assertFalse(_integrity_failure_is_web_index_only([]))

    def test_integrity_web_index_attribution_is_conservative(self):
        self.assertFalse(_line_names_web_index_table("row missing from index core_web_message_trigram_shadow"))
        self.assertFalse(_line_names_web_index_table("row missing from index unknown_index"))
        self.assertFalse(_integrity_failure_is_web_index_only([
            "row missing from index web_message_trigram_data and index conversations",
        ]))
        lines = [
            "malformed inverted index for FTS5 table main.web_message_trigram",
            "wrong # of entries in index web_title_trigram_docsize",
        ]
        self.assertTrue(_integrity_failure_is_web_index_only(lines))

    def test_drop_table_with_shadows_reports_sanitized_failures(self):
        class FakeConn:
            def execute(self, sql):
                if "web_message_trigram_data" in sql:
                    raise sqlite3.OperationalError("synthetic /private/path should not be reported")

        failures = _drop_table_with_shadows(FakeConn(), "web_message_trigram")
        self.assertEqual(failures, [{"table": "web_message_trigram_data", "error_type": "OperationalError"}])
        self.assertNotIn("/private/path", json.dumps(failures))

    def test_drop_optional_web_indexes_aggregates_sanitized_failures(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "synthetic.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("optional-drop-failure")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            create_web_indexes(db)
            conn = connect(db)
            conn.execute("BEGIN")

            def fake_shadow_drop(_conn, table):
                if table == "web_message_trigram":
                    return [{"table": "web_message_trigram_data", "error_type": "OperationalError"}]
                return []

            try:
                with mock.patch(
                    "chatgpt_export_archiver.db._drop_table_with_shadows",
                    side_effect=fake_shadow_drop,
                    autospec=True,
                ):
                    failures = drop_optional_web_indexes(conn)
                self.assertEqual(
                    failures,
                    [{"table": "web_message_trigram_data", "error_type": "OperationalError"}],
                )
                self.assertNotIn("/private/path", json.dumps(failures))
            finally:
                conn.rollback()
                conn.close()

    def test_import_records_optional_web_index_drop_failure_warning_without_failing(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "synthetic.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("optional-drop-warning")]})
            failures = [{"table": "web_message_trigram_data", "error_type": "OperationalError"}]
            with mock.patch("chatgpt_export_archiver.cli.drop_optional_web_indexes", return_value=failures):
                code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                self.assertEqual(conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0], "finished")
                warning = conn.execute(
                    "SELECT keys_json, raw_json FROM import_warnings WHERE warning_type='optional_web_index_drop_failed'"
                ).fetchone()
                self.assertIsNotNone(warning)
                self.assertIn("web_message_trigram_data", warning["keys_json"])
                self.assertIn("OperationalError", warning["keys_json"])
                self.assertIsNone(warning["raw_json"])
                self.assertNotIn(str(base), json.dumps(dict(warning)))
                self.assertNotIn(z.name, json.dumps(dict(warning)))
            finally:
                conn.close()

    def test_web_index_cli_reports_shadow_drop_failures_safely(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "synthetic.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("web-index-drop-output")]})
            code, output = run_cli(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            self.assertEqual(run_cli(["--db", str(db), "web-index"])[0], 0)

            def fake_shadow_drop(conn, table):
                if table == "web_message_trigram":
                    return [{"table": "web_message_trigram_data", "error_type": "OperationalError"}]
                return []

            with mock.patch("chatgpt_export_archiver.db._drop_table_with_shadows", side_effect=fake_shadow_drop):
                code, output = run_cli(["--db", str(db), "web-index"])
            self.assertEqual(code, 2, output)
            self.assertIn("ERROR: web_index_drop_failed", output)
            self.assertNotIn(str(base), output)
            self.assertNotIn(z.name, output)
            conn = connect_readonly(db)
            try:
                from chatgpt_export_archiver.web_db import web_index_status

                self.assertTrue(web_index_status(conn)["web_normalized_indexed"])
            finally:
                conn.close()

    def test_core_fts_unavailable_is_downgraded_but_other_errors_raise(self):
        from chatgpt_export_archiver.db import ensure_fts

        class FakeConn:
            def __init__(self, message):
                self.message = message

            def executemany(self, sql, rows):
                raise sqlite3.OperationalError(self.message)

            def execute(self, sql, params=()):
                raise sqlite3.OperationalError(self.message)

        parsed = parse_conversation(conversation("fts-safe"), "conversations.json", 0)
        _insert_fts_batch(FakeConn("no such table: message_fts"), [parsed])
        _delete_fts_for_conversation(FakeConn("no such module: fts5"), "fts-safe")
        self.assertFalse(ensure_fts(FakeConn("no such module: fts5")))
        for message in (
            "database disk image is malformed /private/path",
            "attempt to write a readonly database",
            "database is locked",
            "disk I/O error",
            "near synthetic: syntax error",
        ):
            with self.subTest(message=message):
                with self.assertRaises(sqlite3.OperationalError):
                    _insert_fts_batch(FakeConn(message), [parsed])
                with self.assertRaises(sqlite3.OperationalError):
                    ensure_fts(FakeConn(message))

    def test_optional_message_fts_missing_and_damaged_are_distinct(self):
        self.assertTrue(_integrity_failure_is_message_fts_only([
            "malformed inverted index for FTS5 table main.message_fts",
            "wrong # of entries in index message_fts_idx",
        ]))
        self.assertFalse(_integrity_failure_is_message_fts_only([
            "wrong # of entries in index message_fts_idx",
            "row missing from index conversations",
        ]))
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "archive.db"
            conn = connect(db)
            init_db(conn)
            conn.execute("DROP TABLE IF EXISTS message_fts")
            missing = verify_database(conn)
            self.assertTrue(missing["ok"])
            self.assertFalse(missing["message_fts_available"])
            self.assertEqual(missing["message_fts_error"], "missing")
            with mock.patch("chatgpt_export_archiver.db.detect_fts5_runtime", return_value=False):
                unavailable = verify_database(conn)
            self.assertFalse(unavailable["message_fts_available"])
            self.assertFalse(unavailable["message_fts_rebuildable"])
            self.assertEqual(unavailable["message_fts_error"], "capability_unavailable")
            with mock.patch(
                "chatgpt_export_archiver.db._run_integrity_check",
                return_value=["malformed inverted index for FTS5 table main.message_fts"],
            ):
                damaged = verify_database(conn)
            self.assertFalse(damaged["ok"])
            self.assertTrue(damaged["optional_message_fts_error"])
            self.assertEqual(damaged["message_fts_error"], "damaged")
            self.assertIn("--rebuild-fts", damaged["optional_message_fts_recovery_hint"])
            with mock.patch("chatgpt_export_archiver.db.detect_fts5_runtime", return_value=False), mock.patch(
                "chatgpt_export_archiver.db._run_integrity_check",
                return_value=["malformed inverted index for FTS5 table main.message_fts"],
            ):
                damaged_unavailable = verify_database(conn)
            self.assertEqual(damaged_unavailable["message_fts_error"], "damaged")
            self.assertFalse(damaged_unavailable["message_fts_rebuildable"])
            conn.close()

    def test_cli_sqlite_runtime_errors_are_classified_without_messages(self):
        private = "/private/synthetic/archive.db"
        for message, code in (
            (f"database disk image is malformed {private}", "database_malformed"),
            (f"database is locked {private}", "database_locked"),
            (f"attempt to write a readonly database {private}", "database_readonly"),
            (f"disk I/O error {private}", "database_io_error"),
            (f"near secret: syntax error {private}", "database_runtime_failure"),
        ):
            with self.subTest(code=code), mock.patch(
                "chatgpt_export_archiver.cli.cmd_search",
                side_effect=sqlite3.OperationalError(message),
            ):
                exit_code, output = run_cli(["--db", "unused.db", "search", "synthetic"])
            self.assertEqual(exit_code, 2)
            self.assertIn(code, output)
            self.assertIn("error_type=OperationalError", output)
            self.assertNotIn(private, output)
            self.assertNotIn("secret", output)

    def test_web_index_cleanup_drops_shadow_tables(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("shadow-cleanup")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE web_message_trigram_content(x)")
                conn.execute("CREATE TABLE web_message_trigram_data(x)")
                conn.execute("CREATE TABLE web_message_trigram_idx(x)")
                conn.execute("CREATE TABLE web_message_trigram_config(x)")
                conn.execute("CREATE TABLE web_message_trigram_docsize(x)")
                conn.execute("CREATE TABLE web_title_trigram_data(x)")
                conn.execute("CREATE TABLE web_title_trigram_idx(x)")
                conn.execute("CREATE TABLE web_title_trigram_docsize(x)")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(DatabaseMigrationError) as caught:
                create_web_indexes(db)
            self.assertEqual(caught.exception.code, "optional_index_name_collision")
            rebuild_conn = sqlite3.connect(db)
            try:
                tables = {
                    row[0]
                    for row in rebuild_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                    ).fetchall()
                }
                for name in (
                    "web_message_trigram_content", "web_message_trigram_data",
                    "web_message_trigram_idx", "web_message_trigram_config",
                    "web_message_trigram_docsize", "web_title_trigram_data",
                    "web_title_trigram_idx", "web_title_trigram_docsize",
                ):
                    self.assertIn(name, tables)
                    self.assertEqual(
                        rebuild_conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0],
                        0,
                    )
            finally:
                rebuild_conn.close()

    def test_web_index_norm_tables_drop_plainly_and_trigram_uses_shadow_helper(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("web-drop-policy")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            conn = connect(db)
            try:
                self.assertTrue(verify_database(conn)["ok"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM web_index_lease").fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '__chatgpt_webidx_%'"
                ).fetchone()[0], 0)
            finally:
                conn.close()

    def test_web_index_recovers_after_synthetic_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("web-recovery-test", title="Web Recovery")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            # Build web indexes
            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            # Verify is ok after first build
            conn = connect(db)
            try:
                v1 = verify_database(conn)
                self.assertTrue(v1["ok"], f"initial verify failed: {v1}")
            finally:
                conn.close()
            # Simulate verify_database returning optional_web_index_error=True
            fake_verify = {
                "latest_import_run_id": v1["latest_import_run_id"],
                "latest_run_warnings": v1["latest_run_warnings"],
                "total_warnings": v1["total_warnings"],
                "missing_current_node": v1["missing_current_node"],
                "broken_parent_links": v1["broken_parent_links"],
                "conversations_with_zero_nodes": v1["conversations_with_zero_nodes"],
                "parent_cycles": v1["parent_cycles"],
                "integrity_check": "malformed inverted index for FTS5 table main.web_message_trigram",
                "optional_web_index_error": True,
                "optional_web_index_recovery_hint": "run `web-index` to rebuild optional web search indexes",
                "warnings_by_type": v1["warnings_by_type"],
                "latest_warnings_by_type": v1["latest_warnings_by_type"],
                "ok": False,
            }
            with mock.patch("chatgpt_export_archiver.cli.verify_database", return_value=fake_verify):
                code, output = run_cli(["--db", str(db), "verify"])
            self.assertEqual(code, 1)
            self.assertIn("optional_web_index_error true", output)
            self.assertIn("optional_web_index_recovery_hint", output)
            # web-index should rebuild cleanly (unmock for the real call)
            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            conn = connect(db)
            try:
                v3 = verify_database(conn)
                self.assertTrue(v3["ok"])
                self.assertFalse(v3["optional_web_index_error"])
            finally:
                conn.close()

    def test_verify_cli_outputs_optional_web_index_error_with_recovery_hint(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("verify-diag-test", title="Verify Diag")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            # Get baseline verify result
            conn = connect(db)
            try:
                baseline = verify_database(conn)
                self.assertTrue(baseline["ok"])
            finally:
                conn.close()
            # Simulate corrupt web index by returning optional_web_index_error
            fake_verify = dict(baseline)
            fake_verify.update(
                integrity_check="malformed inverted index for FTS5 table main.web_message_trigram",
                optional_web_index_error=True,
                optional_web_index_recovery_hint="run `web-index` to rebuild optional web search indexes",
                ok=False,
            )
            with mock.patch("chatgpt_export_archiver.cli.verify_database", return_value=fake_verify):
                code, output = run_cli(["--db", str(db), "verify"])
            self.assertIn("optional_web_index_error true", output)
            self.assertIn("optional_web_index_recovery_hint run `web-index` to rebuild optional web search indexes", output)
            self.assertEqual(code, 1)

    def test_verify_mixed_integrity_errors_not_optional_web_index_only(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("mixed-err-test", title="Mixed Err")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            # Monkeypatch _run_integrity_check to return mixed errors
            mixed_lines = [
                "malformed inverted index for FTS5 table main.web_message_trigram",
                "row 123 missing from index some_core_index",
            ]
            with mock.patch("chatgpt_export_archiver.db._run_integrity_check", return_value=mixed_lines):
                conn = connect(db)
                try:
                    result = verify_database(conn)
                finally:
                    conn.close()
            self.assertFalse(result["ok"])
            self.assertFalse(result["optional_web_index_error"])
            self.assertEqual(result["optional_web_index_recovery_hint"], "")
            self.assertIn("some_core_index", result["integrity_check"])

    def test_verify_all_web_index_shadow_errors_set_optional_web_index_error(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("all-web-err-test", title="All Web Err")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            # Monkeypatch _run_integrity_check to return all-web-index shadow errors
            web_only_lines = [
                "malformed inverted index for FTS5 table main.web_message_trigram",
                "wrong # of entries in index web_message_trigram_data",
                "wrong # of entries in index web_title_trigram_idx",
                "wrong # of entries in index web_title_trigram_config",
            ]
            with mock.patch("chatgpt_export_archiver.db._run_integrity_check", return_value=web_only_lines):
                conn = connect(db)
                try:
                    result = verify_database(conn)
                finally:
                    conn.close()
            self.assertFalse(result["ok"])
            self.assertTrue(result["optional_web_index_error"])
            self.assertIn("web-index", result["optional_web_index_recovery_hint"])


    def test_make_release_zip_includes_frontend_source_and_excludes_pollution(self):
        root = Path(__file__).resolve().parents[1]
        import shutil as _sh, subprocess as _sp, sys as _sys, zipfile as _zf, tempfile as _td
        work_parent = Path(_td.mkdtemp())
        work = work_parent / "project"
        output = work_parent / "release.zip"
        try:
            def ignore(_dir, names):
                return {name for name in names if name in {".git", "node_modules", "__pycache__"}}

            _sh.copytree(root, work, ignore=ignore)
            for rel in (
                ".git/config",
                "archive/chatgpt_archive.db",
                "archive/chatgpt_archive.db-wal",
                "archive/chatgpt_archive.db-shm",
                "exports/manifest.csv",
                "tools/release.zip",
                "webui/node_modules/pkg/index.js",
                "chatgpt_export_archiver/__pycache__/search.cpython-313.pyc",
                "tests/__pycache__/test_archiver.cpython-313.pyc",
                ".hidden-local",
                "tools/.DS_Store",
                "webui/._index.html",
                "__MACOSX/._README.md",
                "webui/tsconfig.tsbuildinfo",
                "local.sqlite3",
                "chatgpt-export.zip",
                "conversations-000.json",
                "logs/import.jsonl",
                "run.log",
                "tools/chatgpt-sqlite-webui-release.zip",
            ):
                path = work / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("pollution", encoding="utf-8")
            result = _sp.run(
                [_sys.executable, str(work / "tools" / "make_release_zip.py"), "--output", str(output), "--no-check"],
                capture_output=True, text=True, cwd=work,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            namelist = {n.replace("\\", "/").rstrip("/") for n in _zf.ZipFile(output).namelist()}
            required = [
                "README.md", "README.zh-CN.md", "README.zh-TW.md", "README.ja-JP.md", "README.es-ES.md",
                "requirements-web.txt", "constraints-web-py312.txt", "LICENSE", "AGENTS.md", "chatgpt_archive.py",
                "chatgpt_export_archiver/search.py", "chatgpt_export_archiver/web_api.py",
                "tests/__init__.py", "tests/test_archiver.py", "tests/test_web_api.py",
                "tools/check_delivery_clean.py", "tools/clean_generated_artifacts.py", "tools/make_release_zip.py",
                "webui/src/App.tsx", "webui/src/i18n.ts", "webui/src/components/ConversationPane.tsx",
                "webui/tests/dom-smoke.mjs", "webui/index.html", "webui/package.json",
                "webui/package-lock.json", "webui/tsconfig.json", "webui/vite.config.ts",
                "webui/dist/index.html",
            ]
            for path in required:
                self.assertIn(path, namelist, f"missing {path}")
            self.assertIn("manifest_files", result.stdout)
            self.assertIn("manifest_sha256", result.stdout)
            self.assertIn("dist_assets_verified", result.stdout)
            self.assertTrue(any(name.startswith("webui/dist/assets/") and name.endswith(".js") for name in namelist))
            self.assertTrue(any(name.startswith("webui/dist/assets/") and name.endswith(".css") for name in namelist))
            forbidden_markers = [
                ".git", "node_modules", "__pycache__", "._", "__macosx", ".ds_store",
                "archive/", "exports/", ".db", ".db-wal", ".db-shm", ".sqlite",
                "tsconfig.tsbuildinfo", "tools/release.zip", "chatgpt-export.zip",
                "conversations-000.json", ".jsonl", ".log", "chatgpt-sqlite-webui-release.zip",
            ]
            for name in namelist:
                if name == ".gitignore":
                    continue
                for fb in forbidden_markers:
                    self.assertNotIn(fb, name.lower(), f"forbidden in release: {name}")
            delivery_check = _sp.run(
                [_sys.executable, str(work / "tools" / "check_delivery_clean.py"), "--mode", "runnable", str(output)],
                capture_output=True, text=True,
            )
            self.assertEqual(delivery_check.returncode, 0, delivery_check.stdout or delivery_check.stderr)

            dist_index = work / "webui" / "dist" / "index.html"
            original_index = dist_index.read_text(encoding="utf-8")
            dist_index.write_text(original_index.replace("</head>", '<script src="/assets/missing-release-asset.js"></script></head>'), encoding="utf-8")
            missing_asset = _sp.run(
                [_sys.executable, str(work / "tools" / "make_release_zip.py"), "--output", str(work_parent / "missing-asset.zip"), "--no-check"],
                capture_output=True, text=True, cwd=work,
            )
            self.assertNotEqual(missing_asset.returncode, 0)
            self.assertIn("dist_missing_assets", missing_asset.stdout)
            dist_index.write_text(original_index, encoding="utf-8")

            missing_readme_path = work / "README.es-ES.md"
            missing_readme_bytes = missing_readme_path.read_bytes()
            missing_readme_path.unlink()
            missing_required = _sp.run(
                [_sys.executable, str(work / "tools" / "make_release_zip.py"), "--output", str(work_parent / "missing-required.zip"), "--no-check"],
                capture_output=True, text=True, cwd=work,
            )
            self.assertNotEqual(missing_required.returncode, 0)
            self.assertIn("required_release_paths_missing", missing_required.stdout)
            self.assertIn("README.es-ES.md", missing_required.stdout)
            missing_readme_path.write_bytes(missing_readme_bytes)
            # Every authoritative leaf is validated independently of what the
            # collector happened to find, and a failure cannot replace the
            # previously verified release.
            from tools import make_release_zip as _release

            existing_release = output.read_bytes()
            for relative in (
                "webui/vite.config.ts",
                "webui/tsconfig.json",
                "webui/index.html",
                "LICENSE",
                ".gitignore",
                "chatgpt_export_archiver/logging_utils.py",
                "chatgpt_export_archiver/utils.py",
                "tools/benchmark_effective_current.py",
                "tools/clean_generated_artifacts.py",
                "tests/__init__.py",
            ):
                target = work / relative
                original = target.read_bytes()
                target.unlink()
                try:
                    with self.subTest(missing_authoritative_leaf=relative), self.assertRaisesRegex(
                        ValueError, "required_release_paths_missing"
                    ):
                        _release.build_release(work, output, check=False)
                    self.assertEqual(output.read_bytes(), existing_release)
                finally:
                    target.write_bytes(original)
        finally:
            _sh.rmtree(work_parent, ignore_errors=True)

    def test_non_finite_json_and_timestamp_contracts_are_persisted(self):
        for label, value in (("nan", float("nan")), ("positive", float("inf")), ("negative", float("-inf"))):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                source = base / "non-finite.zip"
                payload = conversation(f"non-finite-{label}")
                payload["create_time"] = value
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("conversations.json", json.dumps([payload], allow_nan=True))
                db = base / "archive.db"
                code, output = run_cli(["--db", str(db), "import", "--input", str(source), "--no-input-sha256"])
                self.assertEqual(code, 2, output)
                self.assertIn("non_finite_json_number", output)
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                try:
                    run = conn.execute("SELECT status, summary_json FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
                    summary = json.loads(run["summary_json"])
                    self.assertEqual(run["status"], "failed")
                    self.assertEqual(summary["failure_code"], "non_finite_json_number")
                    warnings = conn.execute(
                        "SELECT warning_type FROM import_warnings WHERE import_run_id = ?",
                        (summary["import_run_id"],),
                    ).fetchall()
                    self.assertEqual([row["warning_type"] for row in warnings], ["non_finite_json_number"])
                    self.assertEqual(summary["warnings"], len(warnings))
                    self.assertEqual(
                        summary["warnings_by_type"],
                        [{"count": 1, "warning_type": "non_finite_json_number"}],
                    )
                finally:
                    conn.close()

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            payload = conversation("finite-and-invalid")
            payload["create_time"] = "abc"
            payload["update_time"] = {"invalid": True}
            payload["mapping"]["u1"]["message"]["create_time"] = []
            payload["mapping"]["u1"]["message"]["update_time"] = {"invalid": True}
            payload["mapping"]["a1"]["message"]["create_time"] = 1e308
            payload["mapping"]["a1"]["message"]["update_time"] = 5e-324
            payload["mapping"]["a1"]["message"]["metadata"]["finite"] = 1e308
            source = base / "finite-invalid.zip"
            write_zip(source, {"conversations.json": [payload]})
            db = base / "finite-invalid.db"
            code, output = run_cli([
                "--db", str(db), "import", "--input", str(source), "--no-input-sha256",
            ])
            self.assertEqual(code, 0, output)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                warnings = conn.execute(
                    "SELECT warning_type, keys_json, raw_json FROM import_warnings WHERE warning_type = 'invalid_timestamp'"
                ).fetchall()
                self.assertEqual(len(warnings), 4)
                self.assertTrue(all(row["raw_json"] is None for row in warnings))
                self.assertTrue(all(set(json.loads(row["keys_json"])) == {"field", "value_type"} for row in warnings))
                node = conn.execute(
                    "SELECT create_time, update_time, metadata_json, raw_message_json FROM conversation_nodes WHERE node_id = 'a1'"
                ).fetchone()
                self.assertEqual(node["create_time"], 1e308)
                self.assertEqual(node["update_time"], 5e-324)
                self.assertNotRegex(node["metadata_json"], r"(?:NaN|Infinity)")
                self.assertNotRegex(node["raw_message_json"], r"(?:NaN|Infinity)")
            finally:
                conn.close()

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "string-non-finite.zip"
            payload = conversation("string-non-finite")
            payload["create_time"] = "Infinity"
            payload["update_time"] = "NaN"
            payload["mapping"]["u1"]["message"]["create_time"] = "-Infinity"
            payload["mapping"]["u1"]["message"]["update_time"] = "NaN"
            write_zip(source, {"conversations.json": [payload]})
            db = base / "archive.db"
            code, output = run_cli(["--db", str(db), "import", "--input", str(source), "--no-input-sha256"])
            self.assertEqual(code, 0, output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT create_time, update_time FROM conversations").fetchone(), (None, None))
                self.assertEqual(conn.execute("SELECT create_time, update_time FROM conversation_nodes WHERE node_id = 'u1'").fetchone(), (None, None))
                self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM import_warnings WHERE warning_type = 'non_finite_timestamp'").fetchone()[0], 4)
            finally:
                conn.close()

    def test_large_exponent_json_and_invalid_timestamp_contract(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for token in ("1e9999", "-1e9999"):
                source = base / f"overflow-{token[0]}.zip"
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("conversations.json", f'[{{"id":"overflow","x":{token}}}]')
                code, output = run_cli([
                    "--db", str(base / f"overflow-{token[0]}.db"),
                    "import", "--input", str(source), "--no-input-sha256",
                ])
                self.assertEqual(code, 2, output)
                self.assertIn("non_finite_json_number", output)

    def test_json_encoding_and_integer_limits_are_stable_for_all_input_forms(self):
        fixtures = (
            ("invalid_conversation_encoding", b"[\xff]"),
            (
                "json_integer_too_large",
                b'[{"id":"bounded-int","mapping":{},"metadata":' + (b"9" * 1001) + b"}]",
            ),
        )
        for expected_code, payload in fixtures:
            for input_kind in ("json", "directory", "zip"):
                with self.subTest(expected_code=expected_code, input_kind=input_kind), tempfile.TemporaryDirectory() as td:
                    base = Path(td)
                    if input_kind == "json":
                        source = base / "conversations.json"
                        source.write_bytes(payload)
                    elif input_kind == "directory":
                        source = base / "extracted"
                        source.mkdir()
                        (source / "conversations.json").write_bytes(payload)
                    else:
                        source = base / "export.zip"
                        with zipfile.ZipFile(source, "w") as archive:
                            archive.writestr("conversations.json", payload)
                    db = base / "archive.db"
                    code, output = run_cli(
                        ["--db", str(db), "import", "--input", str(source), "--no-input-sha256"]
                    )
                    self.assertEqual(code, 2, output)
                    self.assertIn(expected_code, output)
                    self.assertIn("stage=json_decode", output)
                    conn = sqlite3.connect(db)
                    try:
                        status, summary_json = conn.execute(
                            "SELECT status, summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()
                    finally:
                        conn.close()
                    summary = json.loads(summary_json)
                    self.assertEqual(status, "failed")
                    self.assertEqual(summary["failure_code"], expected_code)
                    self.assertEqual(summary["failure_stage"], "json_decode")

                    inspect_code, inspect_output = run_cli(["inspect", "--input", str(source)])
                    self.assertEqual(inspect_code, 0, inspect_output)
                    self.assertIn(f"error_code {expected_code} stage json_decode", inspect_output)

    def test_json_integer_boundary_values_are_stable_and_sqlite_safe(self):
        values = {
            "zero": 0,
            "negative": -1,
            "sqlite_max": (1 << 63) - 1,
            "above_sqlite": 1 << 63,
            "hundreds": int("8" * 300),
            "at_limit": int("9" * 1000),
        }
        for input_kind in ("json", "directory", "zip"):
            with self.subTest(input_kind=input_kind), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                payload = conversation(f"integer-boundary-{input_kind}")
                payload["create_time"] = values["above_sqlite"]
                payload["update_time"] = values["sqlite_max"]
                payload["is_archived"] = values["above_sqlite"]
                payload["is_starred"] = values["negative"]
                payload["context_scopes"] = {"nested": values}
                payload["mapping"]["a1"]["message"]["metadata"]["nested"] = values
                encoded = json.dumps([payload], ensure_ascii=False).encode("utf-8")
                if input_kind == "json":
                    source = base / "conversations.json"
                    source.write_bytes(encoded)
                elif input_kind == "directory":
                    source = base / "extracted"
                    source.mkdir()
                    (source / "conversations.json").write_bytes(encoded)
                else:
                    source = base / "export.zip"
                    with zipfile.ZipFile(source, "w") as archive:
                        archive.writestr("conversations.json", encoded)
                db = base / "archive.db"
                code, output = run_cli([
                    "--db", str(db), "import", "--input", str(source), "--no-input-sha256",
                ])
                self.assertEqual(code, 0, output)
                conn = sqlite3.connect(db)
                try:
                    row = conn.execute(
                        "SELECT create_time, update_time, is_archived, is_starred, metadata_json FROM conversations"
                    ).fetchone()
                    self.assertTrue(math.isfinite(row[0]))
                    self.assertTrue(math.isfinite(row[1]))
                    self.assertEqual(row[2:4], (1, 1))
                    metadata = json.loads(row[4])
                    self.assertEqual(metadata["context_scopes"]["nested"], values)
                    node_metadata = json.loads(conn.execute(
                        "SELECT metadata_json FROM conversation_nodes WHERE node_id = 'a1'"
                    ).fetchone()[0])
                    self.assertEqual(node_metadata["message_metadata"]["nested"], values)
                finally:
                    conn.close()

    def test_source_read_errors_are_not_transaction_failures(self):
        from chatgpt_export_archiver import cli as cli_module

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "conversations.json"
            source.write_text("[]", encoding="utf-8")
            db = base / "archive.db"
            for error, expected_code in (
                (ValueError("input_source_open_failed"), "input_source_open_failed"),
                (ValueError("input_source_not_regular_file"), "input_source_not_regular_file"),
                (OSError("private OS detail"), "source_read_failed"),
            ):
                with self.subTest(expected_code=expected_code), mock.patch.object(
                    cli_module, "iter_source_array_sessions", side_effect=error,
                    autospec=True,
                ):
                    with self.assertRaises(cli_module.ImportPipelineError) as caught:
                        cli_module.run_import_pipeline(
                            db,
                            str(source),
                            cwd=base,
                            no_input_sha256=True,
                        )
                    self.assertEqual(caught.exception.code, expected_code)
                    self.assertEqual(caught.exception.stage, "source_read")
                    self.assertNotIn("private OS detail", str(caught.exception))

    def test_import_preflight_and_transaction_failures_persist_consistent_runs(self):
        cases = []
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            no_source = base / "no-source.zip"
            with zipfile.ZipFile(no_source, "w") as archive:
                archive.writestr("notes.txt", "synthetic")
            ambiguous = base / "ambiguous.zip"
            with zipfile.ZipFile(ambiguous, "w") as archive:
                archive.writestr("a/conversations-001.json", "[]")
                archive.writestr(r"b\conversations-1.json", "[]")
            valid = base / "valid.zip"
            write_zip(valid, {"conversations.json": [conversation("transaction-failure")]})
            cases.extend([
                ("no-source", no_source, None, "no_conversation_sources", "input_preflight"),
                ("ambiguous", ambiguous, None, "ambiguous_conversation_sources", "source_scan"),
                ("sqlite", valid, sqlite3.OperationalError("synthetic"), "import_transaction_failed", "transaction"),
                ("unknown", valid, RuntimeError("synthetic"), "import_transaction_failed", "transaction"),
            ])
            for label, source, injected, code, stage in cases:
                with self.subTest(label=label):
                    db = base / f"{label}.db"
                    patcher = (
                        mock.patch("chatgpt_export_archiver.cli.upsert_conversations_batch", side_effect=injected)
                        if injected is not None
                        else contextlib.nullcontext()
                    )
                    with patcher:
                        exit_code, output = run_cli(["--db", str(db), "import", "--input", str(source), "--no-input-sha256"])
                    self.assertEqual(exit_code, 2, output)
                    self.assertIn(code, output)
                    conn = sqlite3.connect(db)
                    conn.row_factory = sqlite3.Row
                    try:
                        run = conn.execute("SELECT id, status, summary_json FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
                        summary = json.loads(run["summary_json"])
                        warning_rows = conn.execute("SELECT warning_type FROM import_warnings WHERE import_run_id = ?", (run["id"],)).fetchall()
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
                    finally:
                        conn.close()
                    self.assertEqual(run["status"], "failed")
                    self.assertEqual(summary["failure_code"], code)
                    self.assertEqual(summary["failure_stage"], stage)
                    self.assertEqual([row["warning_type"] for row in warning_rows], [code])
                    self.assertEqual(summary["warnings"], 1)

    def test_mixed_shard_rollback_separates_attempted_and_committed_counts(self):
        from chatgpt_export_archiver.cli import ImportPipelineError, run_import_pipeline

        for valid_shard_count in (1, 2):
            with self.subTest(valid_shard_count=valid_shard_count), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                source = base / "mixed.zip"
                with zipfile.ZipFile(source, "w") as archive:
                    for index in range(valid_shard_count):
                        archive.writestr(
                            f"conversations-{index:03d}.json",
                            json.dumps([conversation(f"attempted-{index}")]),
                        )
                    archive.writestr(f"conversations-{valid_shard_count:03d}.json", "{")
                db = base / "archive.db"
                with self.assertRaises(ImportPipelineError) as caught:
                    run_import_pipeline(db, str(source), cwd=base, no_input_sha256=True)
                summary = caught.exception.summary
                self.assertIsNotNone(summary)
                self.assertEqual(summary["attempted_valid_conversations"], valid_shard_count)
                self.assertEqual(summary["attempted_nodes"], valid_shard_count * 5)
                self.assertEqual(summary["valid_conversations"], 0)
                self.assertEqual(summary["nodes"], 0)
                self.assertEqual(summary["committed_conversations"], 0)
                self.assertEqual(summary["committed_nodes"], 0)
                self.assertEqual(summary["inserted_conversations"], 0)
                self.assertEqual(summary["updated_conversations"], 0)
                self.assertEqual(summary["unchanged_conversations"], 0)
                self.assertEqual(summary["attempted_inserted_conversations"], valid_shard_count)
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
                    run = conn.execute("SELECT status, summary_json FROM import_runs").fetchone()
                    persisted = json.loads(run["summary_json"])
                    self.assertEqual(run["status"], "failed")
                    self.assertEqual(persisted["attempted_valid_conversations"], valid_shard_count)
                    self.assertEqual(persisted["committed_conversations"], 0)
                    self.assertFalse(persisted["failure_persistence_failed"])
                finally:
                    conn.close()

    def test_failed_run_secondary_persistence_failures_are_explicit(self):
        from chatgpt_export_archiver import cli as cli_module
        from chatgpt_export_archiver.cli import ImportPipelineError

        cases = ("warning", "finish", "open", "readonly", "locked", "io")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                source = base / "invalid.zip"
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("conversations.json", "[{")
                db = base / "archive.db"
                real_connect = cli_module.connect
                patches: list[Any] = []
                if case == "open":
                    calls = {"count": 0}

                    def connect_then_fail(path):
                        calls["count"] += 1
                        if calls["count"] >= 2:
                            raise OSError("synthetic open failure")
                        return real_connect(path)

                    patches.append(mock.patch.object(cli_module, "connect", side_effect=connect_then_fail))
                elif case == "finish":
                    patches.append(
                        mock.patch.object(
                            cli_module,
                            "finish_import_run",
                            side_effect=sqlite3.OperationalError("synthetic commit failure"),
                        )
                    )
                else:
                    message = {
                        "warning": "synthetic warning write failure",
                        "readonly": "attempt to write a readonly database",
                        "locked": "database is locked",
                        "io": "disk I/O error",
                    }[case]
                    patches.append(
                        mock.patch.object(
                            cli_module,
                            "record_warning",
                            side_effect=sqlite3.OperationalError(message),
                        )
                    )
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(ImportPipelineError) as caught:
                        cli_module.run_import_pipeline(db, str(source), cwd=base, no_input_sha256=True)
                error = caught.exception
                self.assertEqual(error.code, "invalid_conversation_json")
                self.assertEqual(error.stage, "json_decode")
                self.assertTrue(error.failure_persistence_failed)
                self.assertIn(error.failure_persistence_error_type, {"OperationalError", "OSError"})
                self.assertTrue(error.summary["failure_persistence_failed"])
                self.assertEqual(error.summary["original_failure_code"], "invalid_conversation_json")
                self.assertEqual(error.summary["original_failure_stage"], "json_decode")
                self.assertIn("failure_persistence_failed=true", str(error))

    def test_legacy_non_finite_stats_verify_and_export_are_safe(self):
        from chatgpt_export_archiver.db import get_stats
        from chatgpt_export_archiver.exporter import export_conversations

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "archive.db"
            source = base / "finite.zip"
            write_zip(source, {"conversations.json": [conversation("legacy-infinity")]})
            self.assertEqual(run_cli(["--db", str(db), "import", "--input", str(source), "--no-input-sha256"])[0], 0)
            conn = connect(db)
            try:
                conn.execute("UPDATE conversations SET create_time = ?, update_time = ?", (float("inf"), 1_700_000_100.0))
                conn.execute("UPDATE conversation_nodes SET update_time = ? WHERE node_id = 'u1'", (float("-inf"),))
                conn.commit()
                stats = get_stats(conn)
                self.assertEqual(stats["earliest_update_time"], 1_700_000_100.0)
                verify = verify_database(conn)
                self.assertFalse(verify["ok"])
                self.assertEqual(verify["non_finite_timestamps"], 2)
                result = export_conversations(conn, base / "exports", ["md", "txt"])
                self.assertEqual(result["written"], 2)
                for path in (base / "exports").iterdir():
                    self.assertLessEqual(len(path.name.encode("utf-8")), 255)
            finally:
                conn.close()

    def test_verify_reports_foreign_key_violations_and_cli_fails_safely(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "foreign-keys.db"
            conn = connect(db)
            init_db(conn)
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO source_files(import_run_id, source_path, file_type) VALUES (999, 'synthetic', 'json')"
            )
            conn.execute(
                "INSERT INTO import_warnings(import_run_id, source_file, warning_type, created_at) VALUES (999, 'synthetic', 'synthetic', 'now')"
            )
            conn.execute(
                "INSERT INTO conversations(conversation_id, aggregate_hash, last_import_run_id) VALUES ('orphan-run', 'h', 999)"
            )
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, last_import_run_id) VALUES ('missing-conversation', 'node', 999)"
            )
            conn.execute(
                "INSERT INTO file_index(import_run_id, source_path, file_type) VALUES (999, 'synthetic', 'json')"
            )
            conn.commit()
            result = verify_database(conn)
            conn.close()
            self.assertFalse(result["ok"])
            self.assertGreaterEqual(result["foreign_key_violations"], 6)
            self.assertEqual(
                {item["table"] for item in result["foreign_key_violations_by_table"]},
                {"source_files", "import_warnings", "conversations", "conversation_nodes", "file_index"},
            )
            self.assertLessEqual(len(result["foreign_key_violation_samples"]), 20)
            self.assertTrue(
                all(
                    set(item) == {"table", "rowid", "parent_table", "constraint_index"}
                    for item in result["foreign_key_violation_samples"]
                )
            )
            code, output = run_cli(["--db", str(db), "verify"])
            self.assertEqual(code, 1)
            self.assertIn("database_error_code database_foreign_key_violation", output)
            self.assertIn("foreign_key_violations ", output)
            self.assertIn("foreign_key_violation_table conversation_nodes", output)
            export_dir = base / "must-not-exist"
            for command in (
                ["--db", str(db), "stats"],
                ["--db", str(db), "search", "synthetic"],
                ["--db", str(db), "export", "--out", str(export_dir)],
            ):
                command_code, command_output = run_cli(command)
                self.assertEqual(command_code, 0)
                self.assertNotIn("database_foreign_key_violation", command_output)

    def test_parent_cycle_nodes_and_components_have_explicit_units(self):
        from chatgpt_export_archiver.db import parent_cycle_diagnostics

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        fixtures = {
            "self": [("a", "a")],
            "two": [("a", "b"), ("b", "a"), ("tail", "a")],
            "three": [("a", "b"), ("b", "c"), ("c", "a")],
            "two-components": [("a", "b"), ("b", "a"), ("c", "d"), ("d", "c")],
        }
        expected = {
            "self": (1, 1),
            "two": (2, 1),
            "three": (3, 1),
            "two-components": (4, 2),
        }
        for conversation_id, pairs in fixtures.items():
            conn.execute(
                "INSERT INTO conversations(conversation_id, aggregate_hash) VALUES (?, ?)",
                (conversation_id, conversation_id),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id) VALUES (?, ?, ?)",
                ((conversation_id, node_id, parent_id) for node_id, parent_id in pairs),
            )
        for conversation_id, (nodes, components) in expected.items():
            scoped = sqlite3.connect(":memory:")
            scoped.row_factory = sqlite3.Row
            init_db(scoped)
            scoped.execute(
                "INSERT INTO conversations(conversation_id, aggregate_hash) VALUES (?, ?)",
                (conversation_id, conversation_id),
            )
            scoped.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id) VALUES (?, ?, ?)",
                ((conversation_id, node_id, parent_id) for node_id, parent_id in fixtures[conversation_id]),
            )
            diagnostics = parent_cycle_diagnostics(scoped)
            verify = verify_database(scoped)
            scoped.close()
            self.assertEqual(diagnostics["parent_cycle_nodes"], nodes)
            self.assertEqual(diagnostics["parent_cycle_components"], components)
            self.assertEqual(verify["parent_cycles"], nodes)
            self.assertEqual(verify["parent_cycle_nodes"], nodes)
            self.assertEqual(verify["parent_cycle_components"], components)
        conn.close()

    def test_verify_effective_diagnostics_uses_batched_queries_not_per_conversation(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        count = 20_000
        conn.executemany(
            "INSERT INTO conversations(conversation_id, current_node, aggregate_hash) VALUES (?, ?, ?)",
            ((f"verify-{index}", f"node-{index}", f"hash-{index}") for index in range(count)),
        )
        conn.executemany(
            "INSERT INTO conversation_nodes(conversation_id, node_id, is_on_current_path) VALUES (?, ?, 0)",
            ((f"verify-{index}", f"node-{index}") for index in range(count)),
        )
        conn.commit()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            result = verify_database(conn)
        finally:
            conn.set_trace_callback(None)
            conn.close()
        self.assertTrue(result["ok"])
        selects = [
            sql for sql in statements
            if sql.lstrip().upper().startswith(("SELECT", "WITH", "PRAGMA"))
        ]
        self.assertLess(len(selects), 100)
        self.assertFalse(
            any(
                "WHERE ec.conversation_id =" in sql
                and "effective_current_nodes ec" in sql
                for sql in statements
            )
        )
        self.assertEqual(
            result["effective_current_diagnostics"]["flags_missing_current_chain_nodes"],
            count,
        )

    def test_verify_separates_selected_chain_and_raw_flag_topology_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "path-diagnostics.db"
            conn = connect(db)
            init_db(conn)
            conn.execute("PRAGMA foreign_keys = OFF")
            conversations = [
                ("raw-missing", "a-current"),
                ("raw-cycle", "b-current"),
                ("selected-missing", "c-current"),
                ("selected-cross", "d-current"),
                ("raw-cross", "e-current"),
                ("selected-cycle", "f-one"),
                ("foreign-selected", "foreign-parent"),
                ("foreign-raw", "foreign-raw-parent"),
            ]
            conn.executemany(
                "INSERT INTO conversations(conversation_id, current_node, aggregate_hash) VALUES (?, ?, ?)",
                ((cid, current, f"hash-{cid}") for cid, current in conversations),
            )
            rows = [
                ("raw-missing", "a-root", None, 0),
                ("raw-missing", "a-current", "a-root", 0),
                ("raw-missing", "a-raw", "absent-raw-parent", 1),
                ("raw-cycle", "b-current", None, 0),
                ("raw-cycle", "b-one", "b-two", 1),
                ("raw-cycle", "b-two", "b-one", 1),
                ("selected-missing", "c-current", "absent-selected-parent", 0),
                ("selected-missing", "c-raw-root", None, 1),
                ("selected-missing", "c-raw-leaf", "c-raw-root", 1),
                ("selected-cross", "d-current", "foreign-parent", 0),
                ("selected-cross", "d-raw", None, 1),
                ("raw-cross", "e-current", None, 0),
                ("raw-cross", "e-raw", "foreign-raw-parent", 1),
                ("selected-cycle", "f-one", "f-two", 0),
                ("selected-cycle", "f-two", "f-one", 0),
                ("foreign-selected", "foreign-parent", None, 0),
                ("foreign-raw", "foreign-raw-parent", None, 0),
            ]
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, is_on_current_path) VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            diagnostics = verify_database(conn)["effective_current_diagnostics"]
            conn.close()
            self.assertEqual(diagnostics["selected_chain_cycles"], 1)
            self.assertEqual(diagnostics["raw_flag_cycles"], 1)
            self.assertEqual(diagnostics["missing_parent_in_selected_chain"], 1)
            self.assertEqual(diagnostics["cross_conversation_parent_in_selected_chain"], 1)
            self.assertEqual(diagnostics["partial_selected_chain"], 3)
            self.assertEqual(diagnostics["missing_parent_in_raw_flag_topology"], 1)
            self.assertEqual(diagnostics["cross_conversation_parent_in_raw_flag_topology"], 1)
            self.assertEqual(diagnostics["partial_raw_flag_topology"], 3)
            self.assertEqual(diagnostics["cycle_detected"], 2)

    def test_foreign_key_diagnostics_streams_million_row_check_and_exact_orphans(self):
        from chatgpt_export_archiver.db import foreign_key_diagnostics

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "foreign-keys.db"
            conn = connect(db)
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, aggregate_hash) VALUES ('valid-parent', 'hash')"
            )
            conn.execute(
                """WITH RECURSIVE seq(value) AS (
                       VALUES(1) UNION ALL SELECT value + 1 FROM seq WHERE value < 1000000
                   )
                   INSERT INTO conversation_nodes(conversation_id, node_id)
                   SELECT 'valid-parent', printf('valid-%07d', value) FROM seq"""
            )
            conn.commit()
            no_violations = foreign_key_diagnostics(conn, sample_limit=20)
            self.assertEqual(no_violations["foreign_key_violations"], 0)
            self.assertTrue(no_violations["foreign_key_check_complete"])
            self.assertTrue(no_violations["foreign_key_violations_exact"])

            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                """WITH RECURSIVE seq(value) AS (
                       VALUES(1) UNION ALL SELECT value + 1 FROM seq WHERE value < 100000
                   )
                   INSERT INTO conversation_nodes(conversation_id, node_id)
                   SELECT printf('missing-%06d', value), 'orphan' FROM seq"""
            )
            conn.commit()
            progress_calls = 0

            def progress():
                nonlocal progress_calls
                progress_calls += 1
                return 0

            conn.set_progress_handler(progress, 10_000)
            tracemalloc.start()
            try:
                violations = foreign_key_diagnostics(conn, sample_limit=20)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                conn.set_progress_handler(None, 0)
                conn.close()
            self.assertEqual(violations["foreign_key_violations"], 100_000)
            self.assertEqual(violations["foreign_key_violations_by_table"], [
                {"table": "conversation_nodes", "count": 100_000},
            ])
            self.assertEqual(len(violations["foreign_key_violation_samples"]), 20)
            self.assertEqual(violations["foreign_key_violation_sample_limit"], 20)
            self.assertTrue(violations["foreign_key_check_complete"])
            self.assertTrue(violations["foreign_key_violations_exact"])
            self.assertGreater(progress_calls, 0)
            self.assertLess(peak, 16 * 1024 * 1024)

    def test_export_basenames_respect_utf8_component_budget_on_disk(self):
        from chatgpt_export_archiver.exporter import MAX_EXPORT_BASENAME_BYTES, export_conversations

        titles = [
            "😀" * 80,
            "汉字" * 100,
            ("e\u0301" * 140),
            "é" * 140,
            "A" * 400,
            "CON",
            "AUX. ",
            "NUL",
            "COM¹.txt",
            "LPT². ",
        ]
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "unicode.zip"
            conversations = [conversation(f"unicode-{index}", title=title) for index, title in enumerate(titles)]
            conversations.extend([
                conversation("collision/a", title="same"),
                conversation(r"collision\a", title="same"),
            ])
            write_zip(source, {"conversations.json": conversations})
            db = base / "archive.db"
            self.assertEqual(run_cli(["--db", str(db), "import", "--input", str(source), "--no-input-sha256"])[0], 0)
            conn = connect(db)
            try:
                result = export_conversations(conn, base / "out", ["md", "txt"])
                self.assertEqual(result["written"], len(conversations) * 2)
            finally:
                conn.close()
            exported = [path for path in (base / "out").iterdir() if path.suffix in {".md", ".txt"}]
            self.assertEqual(len(exported), len(conversations) * 2)
            for path in exported:
                self.assertLessEqual(len(path.name.encode("utf-8")), MAX_EXPORT_BASENAME_BYTES)
                self.assertNotRegex(path.stem.rstrip(". ").casefold(), r"^(con|aux|nul|com[1-9]|lpt[1-9])$")
            self.assertTrue(any("_001." in path.name for path in exported))
            self.assertTrue(any("_002." in path.name for path in exported))

    def test_export_filename_plan_is_globally_unique_across_collision_groups(self):
        from chatgpt_export_archiver.exporter import export_conversations

        collision_ids = [
            "a/b",
            r"a\b",
            "a_b_001",
            "Case",
            "case",
            "é",
            "e\u0301",
            "😀" * 70 + "/x",
            "😀" * 70 + r"\x",
        ]
        payloads = []
        sentinels: dict[str, str] = {}
        for index, conversation_id in enumerate(collision_ids):
            sentinel = f"SYNTHETIC_SENTINEL_{index:02d}"
            item = conversation(conversation_id, title="same", create_time=1_700_000_000)
            item["mapping"]["u1"]["message"]["content"]["parts"] = [sentinel]
            payloads.append(item)
            sentinels[conversation_id] = sentinel

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "collisions.zip"
            write_zip(source, {"conversations.json": payloads})
            db = base / "archive.db"
            self.assertEqual(
                run_cli(["--db", str(db), "import", "--input", str(source), "--no-input-sha256"])[0],
                0,
            )
            output = base / "output"
            output.mkdir()
            # A historical leftover must not affect this run's deterministic plan.
            (output / "unrelated-old.md").write_text("old", encoding="utf-8")
            conn = connect(db)
            try:
                first = export_conversations(conn, output, ["md", "txt"])
                manifest_first = (output / "manifest.jsonl").read_bytes()
                second = export_conversations(conn, output, ["txt", "md"])
                manifest_second = (output / "manifest.jsonl").read_bytes()
            finally:
                conn.close()

            self.assertEqual(first["written"], len(payloads) * 2)
            self.assertEqual(second["skipped_unchanged"], len(payloads) * 2)
            self.assertEqual(manifest_first, manifest_second)
            rows = [json.loads(line) for line in manifest_first.splitlines()]
            paths = [row["output_path"] for row in rows]
            collision_keys = [unicodedata.normalize("NFC", path).casefold() for path in paths]
            self.assertEqual(len(rows), len(payloads) * 2)
            self.assertEqual(len(collision_keys), len(set(collision_keys)))
            self.assertEqual(len({(row["conversation_id"], row["format"]) for row in rows}), len(rows))
            self.assertTrue(all(len(Path(path).name.encode("utf-8")) <= 240 for path in paths))
            self.assertTrue(all(Path(path).suffix in {".md", ".txt"} for path in paths))
            for row in rows:
                body = (output / row["output_path"]).read_text(encoding="utf-8")
                self.assertIn(sentinels[row["conversation_id"]], body)

    def test_write_bytes_if_changed_streams_existing_file_comparison(self):
        from chatgpt_export_archiver.utils import write_bytes_if_changed

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "large.bin"
            original = (b"synthetic-block-" * 100_000) + b"end"
            path.write_bytes(original)
            original_stat = path.stat()
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes forbidden")):
                self.assertFalse(write_bytes_if_changed(path, original))
                self.assertTrue(write_bytes_if_changed(path, original[:-1] + b"X"))
            self.assertNotEqual(path.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertEqual(path.read_bytes(), original[:-1] + b"X")

    def test_logging_none_is_project_scoped_and_reconfigurable(self):
        project_stream = io.StringIO()
        third_party_stream = io.StringIO()
        third_party = logging.getLogger("synthetic.third_party")
        third_party.handlers.clear()
        third_party.propagate = False
        third_party.setLevel(logging.DEBUG)
        third_party.addHandler(logging.StreamHandler(third_party_stream))
        try:
            configure_logging("none", stream=project_stream)
            get_logger("scope").critical("project-critical")
            third_party.error("third-error")
            logging.getLogger("uvicorn.error").disabled = False
            self.assertEqual(project_stream.getvalue(), "")
            self.assertIn("third-error", third_party_stream.getvalue())
            configure_logging("warning", stream=project_stream)
            configure_logging("warning", stream=project_stream)
            get_logger("scope").warning("project-warning")
            self.assertEqual(project_stream.getvalue().count("project-warning"), 1)
            self.assertFalse(logging.getLogger("uvicorn.error").disabled)
        finally:
            third_party.handlers.clear()

    def test_logging_configuration_preserves_host_global_disable_and_other_loggers(self):
        project_logger = get_logger("host-state")
        third_party = logging.getLogger("synthetic.host.third_party")
        uvicorn_logger = logging.getLogger("uvicorn.error")
        saved_disable = logging.root.manager.disable
        saved_third = (third_party.level, third_party.disabled, third_party.propagate, list(third_party.handlers))
        saved_uvicorn = (uvicorn_logger.level, uvicorn_logger.disabled, uvicorn_logger.propagate, list(uvicorn_logger.handlers))
        try:
            for global_disable in (logging.CRITICAL, logging.NOTSET):
                with self.subTest(global_disable=global_disable):
                    logging.disable(global_disable)
                    stream = io.StringIO()
                    third_stream = io.StringIO()
                    third_party.handlers[:] = [logging.StreamHandler(third_stream)]
                    third_party.setLevel(logging.DEBUG)
                    third_party.disabled = False
                    third_party.propagate = False
                    uvicorn_before = (uvicorn_logger.level, uvicorn_logger.disabled, uvicorn_logger.propagate, tuple(uvicorn_logger.handlers))

                    configure_logging("warning", stream=stream)
                    self.assertEqual(logging.root.manager.disable, global_disable)
                    configure_logging("none", stream=stream)
                    self.assertEqual(logging.root.manager.disable, global_disable)
                    project_logger.critical("project-none")
                    self.assertEqual(stream.getvalue(), "")
                    configure_logging("warning", stream=stream)
                    configure_logging("warning", stream=stream)
                    project_logger.warning("project-warning")
                    third_party.error("third-error")
                    self.assertEqual(logging.root.manager.disable, global_disable)
                    if global_disable == logging.NOTSET:
                        self.assertEqual(stream.getvalue().count("project-warning"), 1)
                        self.assertIn("third-error", third_stream.getvalue())
                    else:
                        self.assertNotIn("project-warning", stream.getvalue())
                    self.assertEqual(
                        (uvicorn_logger.level, uvicorn_logger.disabled, uvicorn_logger.propagate, tuple(uvicorn_logger.handlers)),
                        uvicorn_before,
                    )
        finally:
            logging.disable(saved_disable)
            third_party.level, third_party.disabled, third_party.propagate = saved_third[:3]
            third_party.handlers[:] = saved_third[3]
            uvicorn_logger.level, uvicorn_logger.disabled, uvicorn_logger.propagate = saved_uvicorn[:3]
            uvicorn_logger.handlers[:] = saved_uvicorn[3]

    def test_release_failures_preserve_existing_output_and_clean_temp_files(self):
        from tools import make_release_zip

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "release.zip"
            original = b"previous-verified-release"
            output.write_bytes(original)
            failure_patches = [
                mock.patch.object(make_release_zip, "_write_archive_paths", side_effect=OSError("synthetic")),
                mock.patch.object(make_release_zip, "_delivery_check", side_effect=ValueError("delivery_check_failed")),
                mock.patch.object(make_release_zip.os, "replace", side_effect=OSError("replace failed")),
            ]
            for patcher in failure_patches:
                output.write_bytes(original)
                with self.subTest(patcher=repr(patcher)), patcher, self.assertRaises((OSError, ValueError)):
                    make_release_zip.build_release(root, output, check=True)
                self.assertEqual(output.read_bytes(), original)
                self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp.zip")), [])

            output.write_bytes(original)
            with mock.patch.object(make_release_zip, "_manifest_bytes", return_value=b"[]\n"), self.assertRaisesRegex(
                ValueError, "release_manifest_mismatch"
            ):
                make_release_zip.build_release(root, output, check=False)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp.zip")), [])

            original_write = make_release_zip._write_archive_paths

            def tampered_write(path, payload, manifest):
                original_write(path, payload, manifest)
                with zipfile.ZipFile(path) as archive:
                    members = {name: archive.read(name) for name in archive.namelist()}
                target = next(name for name in members if name != make_release_zip.MANIFEST_NAME)
                members[target] = b"tampered-after-write"
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, data in members.items():
                        archive.writestr(name, data)

            output.write_bytes(original)
            with mock.patch.object(make_release_zip, "_write_archive_paths", side_effect=tampered_write), self.assertRaisesRegex(
                ValueError, "release_hash_mismatch"
            ):
                make_release_zip.build_release(root, output, check=False)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp.zip")), [])

            manifest, _assets = make_release_zip.build_release(root, output, check=False)
            with zipfile.ZipFile(output) as archive:
                stored = json.loads(archive.read(make_release_zip.MANIFEST_NAME))
                self.assertEqual(stored, manifest)
                self.assertEqual(set(archive.namelist()), {item["path"] for item in manifest} | {make_release_zip.MANIFEST_NAME})
                for item in manifest:
                    data = archive.read(item["path"])
                    self.assertEqual(len(data), item["size"])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])

    def test_release_archive_bytes_are_reproducible(self):
        from tools import make_release_zip

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            first = base / "a.zip"
            second = base / "b.zip"
            payload = {
                "README.md": b"synthetic\n",
                "nested/unicode-测试.txt": b"payload\n",
            }
            make_release_zip._write_archive_test_only(first, payload)
            time.sleep(2.05)
            make_release_zip._write_archive_test_only(second, payload)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), hashlib.sha256(second.read_bytes()).hexdigest())
            for archive_path in (first, second):
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertEqual(json.loads(archive.read(make_release_zip.MANIFEST_NAME)), make_release_zip._manifest(payload))
                    for info in archive.infolist():
                        self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                        self.assertEqual(info.extra, b"")
                        self.assertEqual(info.comment, b"")

    def test_real_legacy_fa37_fixture_migrates_without_canonical_data_changes(self):
        from chatgpt_export_archiver.db import DATABASE_SCHEMA_VERSION, database_schema_status, migrate_database

        root = Path(__file__).resolve().parent
        fixture = root / "fixtures" / "legacy-fa37b3d.sql"
        provenance = json.loads((root / "fixtures" / "legacy-fa37b3d.json").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), provenance["fixture_sha256"])
        self.assertEqual(provenance["source_commit"], "fa37b3d70ff501b59b168690ea8de69bcadb0c38")

        def canonical_snapshot(conn):
            counts = {
                table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ("conversations", "conversation_nodes", "import_runs", "source_files", "file_index")
            }
            hashes = {}
            for table, order in (("conversations", "conversation_id"), ("conversation_nodes", "conversation_id, node_id")):
                rows = [list(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
                hashes[table] = hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                ).hexdigest()
            return counts, hashes

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.executescript(fixture.read_text(encoding="utf-8"))
            conn.execute("PRAGMA foreign_keys = ON")
            before_counts, before_hashes = canonical_snapshot(conn)
            before = database_schema_status(conn)
            self.assertEqual(before["current_database_schema_version"], 0)
            self.assertTrue(before["migration_required"])
            self.assertIn("idx_nodes_conversation_flag_parent", before["missing_indexes"])
            conn.close()

            verify_code, verify_output = run_cli(["--db", str(db), "verify"])
            self.assertEqual(verify_code, 1)
            self.assertIn("schema_ok false", verify_output)
            self.assertIn("database_error_code database_migration_required", verify_output)
            self.assertIn("migration_required true", verify_output)
            self.assertIn("current_database_schema_version 0", verify_output)
            self.assertIn(f"required_database_schema_version {DATABASE_SCHEMA_VERSION}", verify_output)
            self.assertNotIn("no such index", verify_output)
            legacy_exports = Path(td) / "legacy-exports"
            for command in (
                ["--db", str(db), "stats"],
                ["--db", str(db), "search", "synthetic"],
                ["--db", str(db), "export", "--out", str(legacy_exports)],
            ):
                code, output = run_cli(command)
                self.assertEqual(code, 2)
                self.assertIn("database_migration_required", output)
                self.assertIn("current_database_schema_version 0", output)
                self.assertIn(f"required_database_schema_version {DATABASE_SCHEMA_VERSION}", output)
            self.assertFalse(legacy_exports.exists())

            conn = connect(db)
            first = migrate_database(conn)
            after_counts, after_hashes = canonical_snapshot(conn)
            second = migrate_database(conn)
            after = database_schema_status(conn)
            conn.close()

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(after["current_database_schema_version"], DATABASE_SCHEMA_VERSION)
            self.assertTrue(after["ok"])
            self.assertEqual(before_counts, after_counts)
            self.assertEqual(before_hashes, after_hashes)
            self.assertEqual(before_counts, {
                "conversations": 1,
                "conversation_nodes": 3,
                "import_runs": 1,
                "source_files": 1,
                "file_index": 1,
            })
            self.assertEqual(run_cli(["--db", str(db), "verify"])[0], 0)
            self.assertEqual(run_cli(["--db", str(db), "stats"])[0], 0)
            self.assertEqual(run_cli(["--db", str(db), "search", "synthetic"])[0], 0)

    def test_migration_repairs_drift_and_rolls_back_midway_failure(self):
        from chatgpt_export_archiver.db import (
            DATABASE_SCHEMA_VERSION,
            DatabaseMigrationError,
            database_schema_status,
            migrate_database,
        )

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "CREATE TRIGGER IF NOT EXISTS archive_message_generation_insert" in sql:
                    raise sqlite3.OperationalError("synthetic migration statement failure")
                return super().execute(sql, parameters)

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "drift.db"
            conn = connect(db)
            init_db(conn)
            conn.execute("DROP TRIGGER archive_message_generation_insert")
            conn.execute("DROP TRIGGER archive_message_generation_update")
            conn.execute("DROP TRIGGER archive_message_generation_delete")
            conn.execute("DROP TRIGGER archive_title_generation_insert")
            conn.execute("DROP TRIGGER archive_title_generation_update")
            conn.execute("DROP TRIGGER archive_title_generation_delete")
            conn.execute("DROP TABLE archive_generations")
            conn.execute("DROP INDEX idx_nodes_conversation_flag_parent")
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
            conn.close()

            failing = sqlite3.connect(db, factory=FailingConnection)
            failing.row_factory = sqlite3.Row
            failing.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(failing)
            self.assertEqual(caught.exception.code, "database_migration_failed")
            self.assertFalse(failing.in_transaction)
            status = database_schema_status(failing)
            self.assertEqual(status["current_database_schema_version"], 0)
            self.assertTrue(status["missing_generation_table"])
            self.assertIn("idx_nodes_conversation_flag_parent", status["missing_indexes"])
            failing.close()

            repaired = connect(db)
            result = migrate_database(repaired)
            self.assertTrue(result["changed"])
            self.assertEqual(database_schema_status(repaired)["current_database_schema_version"], DATABASE_SCHEMA_VERSION)
            repaired.close()

    def test_schema_contract_detects_and_repairs_every_managed_trigger_shape(self):
        from chatgpt_export_archiver.db import (
            DatabaseMigrationError,
            GENERATION_TRIGGER_CONTRACT,
            connect,
            database_schema_status,
            init_db,
            migrate_database,
        )

        def fresh_db(base: Path, name: str):
            conn = connect(base / f"{name}.db")
            init_db(conn)
            return conn

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for index, (name, contract) in enumerate(GENERATION_TRIGGER_CONTRACT.items()):
                with self.subTest(trigger=name, drift="noop"):
                    conn = fresh_db(base, f"noop-{index}")
                    conn.execute(f'DROP TRIGGER "{name}"')
                    conn.execute(
                        f'CREATE TRIGGER "{name}" {contract[1]} {contract[2]} '
                        f'ON "{contract[0]}" BEGIN SELECT 1; END'
                    )
                    status = database_schema_status(conn)
                    self.assertTrue(status["migration_required"])
                    self.assertIn(name, status["invalid_triggers"])
                    before = conn.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)
                    ).fetchone()[0]
                    with self.assertRaises(DatabaseMigrationError) as caught:
                        migrate_database(conn)
                    self.assertEqual(caught.exception.code, "database_managed_object_name_collision")
                    self.assertEqual(before, conn.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)
                    ).fetchone()[0])
                    conn.close()

            variants = {
                "wrong_target": "AFTER UPDATE OF conversation_id, title ON source_files BEGIN UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title'; END",
                "wrong_timing": "BEFORE UPDATE OF conversation_id, title ON conversations BEGIN UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title'; END",
                "wrong_event": "AFTER DELETE ON conversations BEGIN UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title'; END",
                "wrong_update_of": "AFTER UPDATE OF title ON conversations BEGIN UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title'; END",
                "wrong_generation": "AFTER UPDATE OF conversation_id, title ON conversations BEGIN UPDATE archive_generations SET generation = generation + 1 WHERE name = 'message'; END",
                "wrong_body": "AFTER UPDATE OF conversation_id, title ON conversations BEGIN UPDATE archive_generations SET generation = generation + 2 WHERE name = 'title'; END",
            }
            trigger_name = "archive_title_generation_update"
            for index, (label, definition) in enumerate(variants.items()):
                with self.subTest(trigger=trigger_name, drift=label):
                    conn = fresh_db(base, f"variant-{index}")
                    conn.execute(f'DROP TRIGGER "{trigger_name}"')
                    conn.execute(f'CREATE TRIGGER "{trigger_name}" {definition}')
                    self.assertIn(trigger_name, database_schema_status(conn)["invalid_triggers"])
                    with self.assertRaises(DatabaseMigrationError) as caught:
                        migrate_database(conn)
                    self.assertEqual(caught.exception.code, "database_managed_object_name_collision")
                    self.assertIn(trigger_name, database_schema_status(conn)["invalid_triggers"])
                    conn.close()

    def test_schema_contract_detects_and_repairs_managed_index_shape(self):
        from chatgpt_export_archiver.db import (
            DatabaseMigrationError,
            connect,
            database_schema_status,
            init_db,
            migrate_database,
        )

        variants = {
            "partial": "CREATE INDEX idx_nodes_conversation_path ON conversation_nodes(conversation_id, is_on_current_path) WHERE is_on_current_path = 1",
            "wrong_table": "CREATE INDEX idx_nodes_conversation_path ON conversations(create_time, update_time)",
            "wrong_order": "CREATE INDEX idx_nodes_conversation_path ON conversation_nodes(is_on_current_path, conversation_id)",
            "unique": "CREATE UNIQUE INDEX idx_nodes_conversation_path ON conversation_nodes(conversation_id, is_on_current_path)",
            "collation": "CREATE INDEX idx_nodes_conversation_path ON conversation_nodes(conversation_id COLLATE NOCASE, is_on_current_path)",
            "descending": "CREATE INDEX idx_nodes_conversation_path ON conversation_nodes(conversation_id DESC, is_on_current_path)",
            "expression": "CREATE INDEX idx_nodes_conversation_path ON conversation_nodes((conversation_id || ''), is_on_current_path)",
        }
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for index, (label, ddl) in enumerate(variants.items()):
                with self.subTest(drift=label):
                    conn = connect(base / f"index-{index}.db")
                    init_db(conn)
                    conn.execute("DROP INDEX idx_nodes_conversation_path")
                    conn.execute(ddl)
                    status = database_schema_status(conn)
                    self.assertTrue(status["migration_required"])
                    self.assertIn("idx_nodes_conversation_path", status["invalid_indexes"])
                    before = conn.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='idx_nodes_conversation_path'"
                    ).fetchone()[0]
                    with self.assertRaises(DatabaseMigrationError) as caught:
                        migrate_database(conn)
                    self.assertEqual(caught.exception.code, "database_managed_object_name_collision")
                    self.assertEqual(before, conn.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='idx_nodes_conversation_path'"
                    ).fetchone()[0])
                    conn.close()

            conn = connect(base / "collision.db")
            init_db(conn)
            conn.execute("DROP INDEX idx_nodes_conversation_path")
            conn.execute("CREATE TABLE idx_nodes_conversation_path(marker TEXT)")
            status = database_schema_status(conn)
            self.assertFalse(status["base_schema_compatible"])
            self.assertEqual(status["object_type_mismatches"]["idx_nodes_conversation_path"]["actual"], "table")
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(conn)
            self.assertEqual(caught.exception.code, "database_managed_object_name_collision")
            self.assertIsNotNone(conn.execute("SELECT marker FROM idx_nodes_conversation_path").description)
            conn.close()

    def test_schema_contract_rejects_unsafe_table_and_generation_drift(self):
        from chatgpt_export_archiver.db import (
            DATABASE_SCHEMA_VERSION,
            DatabaseMigrationError,
            connect,
            database_schema_status,
            init_db,
            migrate_database,
        )

        fixture = (Path(__file__).resolve().parent / "fixtures" / "legacy-fa37b3d.sql").read_text(encoding="utf-8")
        variants = {
            "missing_exports_unique": fixture.replace(
                "export_options_json TEXT,\n            UNIQUE(conversation_id, format, output_path)",
                "export_options_json TEXT",
            ),
            "missing_conversation_pk": fixture.replace(
                "conversation_id TEXT PRIMARY KEY,", "conversation_id TEXT,", 1
            ),
            "missing_aggregate_not_null": fixture.replace(
                "aggregate_hash TEXT NOT NULL,", "aggregate_hash TEXT,", 1
            ),
            "wrong_default_and_nullability": fixture.replace(
                "is_conversation_json INTEGER NOT NULL DEFAULT 0,",
                "is_conversation_json INTEGER DEFAULT 1,",
                1,
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for index, (label, sql) in enumerate(variants.items()):
                with self.subTest(drift=label):
                    db = base / f"table-{index}.db"
                    conn = sqlite3.connect(db)
                    conn.row_factory = sqlite3.Row
                    conn.executescript(sql)
                    conn.execute("PRAGMA foreign_keys = ON")
                    status = database_schema_status(conn)
                    self.assertFalse(status["base_schema_compatible"])
                    self.assertTrue(status["invalid_tables"])
                    with self.assertRaises(DatabaseMigrationError) as caught:
                        migrate_database(conn)
                    self.assertEqual(caught.exception.code, "database_schema_incompatible")
                    conn.close()

            for index, value in enumerate((-1, 1.5, "invalid")):
                with self.subTest(generation=value):
                    conn = connect(base / f"generation-{index}.db")
                    init_db(conn)
                    conn.execute("UPDATE archive_generations SET generation = ? WHERE name = 'title'", (value,))
                    status = database_schema_status(conn)
                    self.assertIn("title", status["invalid_generation_rows"])
                    self.assertFalse(status["base_schema_compatible"])
                    with self.assertRaises(DatabaseMigrationError) as caught:
                        migrate_database(conn)
                    self.assertEqual(caught.exception.code, "database_schema_incompatible")
                    conn.close()

            conn = connect(base / "missing-row.db")
            init_db(conn)
            conn.execute("DELETE FROM archive_generations WHERE name = 'title'")
            conn.commit()
            self.assertTrue(database_schema_status(conn)["migration_required"])
            self.assertTrue(migrate_database(conn)["changed"])
            self.assertEqual(conn.execute("SELECT generation FROM archive_generations WHERE name = 'title'").fetchone()[0], 0)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
            self.assertTrue(migrate_database(conn)["changed"])
            self.assertEqual(database_schema_status(conn)["current_database_schema_version"], DATABASE_SCHEMA_VERSION)
            self.assertFalse(migrate_database(conn)["changed"])
            conn.close()

    def test_trigger_drift_invalidates_stale_web_indexes_and_generation_metadata_is_strict(self):
        from chatgpt_export_archiver.db import DatabaseMigrationError, GENERATION_TRIGGER_DDL, connect, database_schema_status, init_db, migrate_database
        from chatgpt_export_archiver.search import parse_query, search_conversations
        from chatgpt_export_archiver.web_db import create_web_indexes, web_index_status

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "stale.db"
            conn = connect(db)
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 'old ghost title', 'h')"
            )
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES ('c', 'n', 'old ghost message')"
            )
            conn.commit()
            conn.close()
            create_web_indexes(db)

            conn = connect(db)
            self.assertTrue(web_index_status(conn)["web_normalized_indexed"])
            conn.execute("DROP TRIGGER archive_title_generation_update")
            conn.execute(
                "CREATE TRIGGER archive_title_generation_update AFTER UPDATE OF conversation_id, title ON conversations BEGIN SELECT 1; END"
            )
            conn.execute("UPDATE conversations SET title = 'new live title' WHERE conversation_id = 'c'")
            conn.commit()
            self.assertIn("archive_title_generation_update", database_schema_status(conn)["invalid_triggers"])
            self.assertFalse(web_index_status(conn)["web_normalized_indexed"])
            page = search_conversations(conn, parse_query("new live", scope="title"), limit=10)
            self.assertEqual([item["conversation_id"] for item in page["items"]], ["c"])
            self.assertEqual(search_conversations(conn, parse_query("old ghost", scope="title"), limit=10)["items"], [])

            conn.commit()
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(conn)
            self.assertEqual(getattr(caught.exception, "code", None), "database_managed_object_name_collision")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
            self.assertIn("web_title_norm", tables)
            conn.execute("DROP TRIGGER archive_title_generation_update")
            conn.execute(GENERATION_TRIGGER_DDL["archive_title_generation_update"])
            conn.execute(
                "UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title'"
            )
            conn.commit()
            conn.close()

            create_web_indexes(db)
            conn = connect(db)
            current = conn.execute(
                "SELECT value FROM web_index_metadata WHERE key = 'title_generation'"
            ).fetchone()[0]
            conn.execute(
                "UPDATE web_index_metadata SET value = ? WHERE key = 'title_generation'",
                (f"0{current}",),
            )
            conn.commit()
            conn.close()
            fresh = connect(db)
            self.assertFalse(web_index_status(fresh)["web_normalized_indexed"])
            fresh.close()

    def test_round6_migration_lock_race_repairs_title_and_message_drift_and_invalidates_indexes(self):
        import threading
        from chatgpt_export_archiver.db import DatabaseMigrationError, connect, database_schema_status, init_db, migrate_database
        from chatgpt_export_archiver.search import parse_query, search_conversations, search_messages
        from chatgpt_export_archiver.web_db import create_web_indexes, web_index_status

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "migration-race.db"
            setup = connect(db)
            init_db(setup)
            setup.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES ('c', 'old title ghost', 'n', 'h')"
            )
            setup.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       content_hash, is_on_current_path
                   ) VALUES ('c', 'n', 'assistant', 'text', 'old message ghost', 'h', 1)"""
            )
            setup.commit()
            setup.close()
            create_web_indexes(db)

            writer = connect(db)
            writer.execute("BEGIN IMMEDIATE")
            migration_started = threading.Event()
            outcome: list[Any] = []

            def migrate_worker():
                conn = connect(db)
                conn.set_trace_callback(
                    lambda sql: migration_started.set() if sql.strip().upper() == "BEGIN IMMEDIATE" else None
                )
                try:
                    outcome.append(migrate_database(conn))
                except BaseException as exc:
                    outcome.append(exc)
                finally:
                    conn.close()

            thread = threading.Thread(target=migrate_worker, daemon=True)
            thread.start()
            self.assertTrue(migration_started.wait(5))
            for trigger in ("archive_title_generation_update", "archive_message_generation_update"):
                writer.execute(f"DROP TRIGGER {trigger}")
            writer.execute(
                "CREATE TRIGGER archive_title_generation_update AFTER UPDATE OF title ON conversations BEGIN SELECT 1; END"
            )
            writer.execute(
                "CREATE TRIGGER archive_message_generation_update AFTER UPDATE OF content_text ON conversation_nodes BEGIN SELECT 1; END"
            )
            writer.execute("UPDATE conversations SET title='new title live' WHERE conversation_id='c'")
            writer.execute(
                "UPDATE conversation_nodes SET content_text='new message live', content_hash='new' WHERE conversation_id='c' AND node_id='n'"
            )
            writer.commit()
            writer.close()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], DatabaseMigrationError)
            self.assertEqual(outcome[0].code, "database_managed_object_name_collision")

            check = connect(db)
            self.assertFalse(database_schema_status(check)["ok"])
            self.assertFalse(web_index_status(check)["web_normalized_indexed"])
            optional_tables = {
                row[0] for row in check.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'web_%'"
                )
            }
            self.assertIn("web_title_norm", optional_tables)
            self.assertIn("web_message_norm", optional_tables)
            title_page = search_conversations(check, parse_query("new title", scope="title"), limit=10)
            self.assertEqual([item["conversation_id"] for item in title_page["items"]], ["c"])
            self.assertEqual(search_conversations(
                check, parse_query("old title", scope="title"), limit=10
            )["items"], [])
            message_page = search_messages(check, parse_query("new message"), conversation_id="c")
            self.assertEqual([item["node_id"] for item in message_page["items"]], ["n"])
            self.assertEqual(search_messages(
                check, parse_query("old message"), conversation_id="c"
            )["items"], [])
            check.close()

    def test_readonly_cli_schema_newer_gate_precedes_queries_and_export_output(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "newer.db"
            conn = connect(db)
            init_db(conn)
            conn.execute("PRAGMA user_version = 999")
            conn.commit()
            conn.close()
            output_dir = base / "must-not-exist"
            for command in (
                ["--db", str(db), "stats"],
                ["--db", str(db), "search", "synthetic"],
                ["--db", str(db), "export", "--out", str(output_dir)],
            ):
                code, output = run_cli(command)
                self.assertEqual(code, 2)
                self.assertIn("database_schema_newer", output)
                self.assertIn("current_database_schema_version 999", output)
                self.assertIn("required_database_schema_version ", output)
            self.assertFalse(output_dir.exists())
            code, output = run_cli(["--db", str(db), "verify"])
            self.assertEqual(code, 1)
            self.assertIn("database_error_code database_schema_newer", output)

    def test_migration_readonly_locked_and_foreign_key_failures_are_stable(self):
        from chatgpt_export_archiver.db import DatabaseMigrationError, migrate_database

        fixture = Path(__file__).resolve().parent / "fixtures" / "legacy-fa37b3d.sql"
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "legacy.db"
            conn = sqlite3.connect(db)
            conn.executescript(fixture.read_text(encoding="utf-8"))
            conn.close()

            readonly = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
            readonly.row_factory = sqlite3.Row
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(readonly)
            self.assertEqual(caught.exception.code, "database_readonly")
            readonly.close()

            lock = sqlite3.connect(db)
            lock.execute("BEGIN EXCLUSIVE")
            blocked = sqlite3.connect(db)
            blocked.row_factory = sqlite3.Row
            blocked.execute("PRAGMA busy_timeout = 1")
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(blocked)
            self.assertEqual(caught.exception.code, "database_locked")
            blocked.close()
            lock.rollback()
            lock.close()

            damaged = sqlite3.connect(base / "damaged.db")
            damaged.row_factory = sqlite3.Row
            init_db(damaged)
            damaged.execute("PRAGMA foreign_keys = OFF")
            damaged.execute("INSERT INTO source_files(import_run_id, source_path, file_type) VALUES (999, 'synthetic', 'json')")
            damaged.execute("DROP INDEX idx_nodes_conversation_flag_parent")
            damaged.commit()
            damaged.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(damaged)
            self.assertEqual(caught.exception.code, "database_foreign_key_violation")
            damaged.close()

    def test_concurrent_writable_migrations_are_serialized_and_idempotent(self):
        import threading

        from chatgpt_export_archiver.db import database_schema_status, migrate_database

        fixture = Path(__file__).resolve().parent / "fixtures" / "legacy-fa37b3d.sql"
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.db"
            seed = sqlite3.connect(db)
            seed.executescript(fixture.read_text(encoding="utf-8"))
            seed.close()
            barrier = threading.Barrier(2)
            changed: list[bool] = []
            errors: list[str] = []

            def worker():
                conn = connect(db)
                try:
                    barrier.wait()
                    changed.append(bool(migrate_database(conn)["changed"]))
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(type(exc).__name__)
                finally:
                    conn.close()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(sorted(changed), [False, True])
            conn = connect(db)
            self.assertTrue(database_schema_status(conn)["ok"])
            conn.close()

    def test_round6_migration_reads_future_version_only_after_write_lock(self):
        import threading

        from chatgpt_export_archiver.db import DatabaseMigrationError, migrate_database

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "race.db"
            seed = connect(db)
            init_db(seed)
            seed.close()
            writer = sqlite3.connect(db)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("PRAGMA user_version = 99")
            begin_seen = threading.Event()
            outcome: list[str] = []

            def worker():
                conn = connect(db)
                conn.set_trace_callback(
                    lambda sql: begin_seen.set() if sql.strip().upper() == "BEGIN IMMEDIATE" else None
                )
                try:
                    migrate_database(conn)
                except DatabaseMigrationError as exc:
                    outcome.append(exc.code)
                finally:
                    conn.close()

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(begin_seen.wait(5))
            writer.commit()
            writer.close()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, ["database_schema_newer"])
            check = sqlite3.connect(db)
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 99)
            check.close()

    def test_round6_legacy_null_identity_is_rejected_and_valid_rows_migrate(self):
        from chatgpt_export_archiver.db import DatabaseMigrationError, DATABASE_SCHEMA_VERSION, migrate_database

        fixture = Path(__file__).resolve().parent / "fixtures" / "legacy-fa37b3d.sql"
        with tempfile.TemporaryDirectory() as td:
            valid_db = Path(td) / "valid.db"
            valid = sqlite3.connect(valid_db)
            valid.row_factory = sqlite3.Row
            valid.executescript(fixture.read_text(encoding="utf-8"))
            before_hash = valid.execute(
                "SELECT aggregate_hash FROM conversations ORDER BY conversation_id"
            ).fetchall()
            self.assertTrue(migrate_database(valid)["changed"])
            self.assertEqual(valid.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION)
            self.assertEqual(before_hash, valid.execute(
                "SELECT aggregate_hash FROM conversations ORDER BY conversation_id"
            ).fetchall())
            with self.assertRaises(sqlite3.IntegrityError):
                valid.execute("INSERT INTO conversations(conversation_id, aggregate_hash) VALUES(NULL, 'x')")
            with self.assertRaises(sqlite3.IntegrityError):
                valid.execute(
                    "UPDATE conversations SET conversation_id=NULL WHERE conversation_id='legacy-synthetic'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                valid.execute("INSERT INTO archive_generations(name, generation) VALUES(NULL, 0)")
            with self.assertRaises(sqlite3.IntegrityError):
                valid.execute("UPDATE archive_generations SET name=NULL WHERE name='title'")
            valid.close()

            damaged = sqlite3.connect(Path(td) / "null.db")
            damaged.row_factory = sqlite3.Row
            damaged.executescript(fixture.read_text(encoding="utf-8"))
            damaged.execute("INSERT INTO conversations(conversation_id, aggregate_hash) VALUES(NULL, 'x')")
            damaged.commit()
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(damaged)
            self.assertEqual(caught.exception.code, "database_schema_incompatible")
            self.assertEqual(damaged.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(damaged.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id IS NULL"
            ).fetchone()[0], 1)
            damaged.close()

            generation_null = sqlite3.connect(Path(td) / "generation-null.db")
            generation_null.row_factory = sqlite3.Row
            generation_null.executescript(fixture.read_text(encoding="utf-8"))
            generation_null.execute(
                "CREATE TABLE archive_generations(name TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0)"
            )
            generation_null.executemany(
                "INSERT INTO archive_generations(name, generation) VALUES (?, 0)",
                (("title",), ("message",), (None,), (None,)),
            )
            generation_null.commit()
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(generation_null)
            self.assertEqual(caught.exception.code, "database_schema_incompatible")
            self.assertEqual(generation_null.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(generation_null.execute(
                "SELECT COUNT(*) FROM archive_generations WHERE name IS NULL"
            ).fetchone()[0], 2)
            generation_null.close()

    def test_round6_delete_input_replacement_is_preserved(self):
        from chatgpt_export_archiver.cli import run_import_pipeline

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source.zip"
            replacement_payload = [conversation("replacement")]
            write_zip(source, {"conversations.json": [conversation("original")]})
            replaced = False

            def progress(stage, _summary):
                nonlocal replaced
                if stage == "import_index_rebuild_complete" and not replaced:
                    self.assertTrue(os.path.lexists(source))
                    write_zip(source, {"conversations.json": replacement_payload})
                    replaced = True

            result = run_import_pipeline(
                base / "archive.db", str(source), cwd=base,
                no_input_sha256=True, delete_input_on_success=True,
                progress_callback=progress,
            )
            self.assertTrue(replaced)
            self.assertTrue(source.exists())
            self.assertIsNone(result["deleted_input"])
            self.assertTrue(result["delete_input_changed"])
            conn = sqlite3.connect(base / "archive.db")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM import_warnings WHERE warning_type='delete_input_changed'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id='original'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id='replacement'"
            ).fetchone()[0], 0)
            conn.close()

    @unittest.skipUnless(hasattr(os, "link"), "hardlink support required")
    def test_round6_delete_input_hardlink_is_conservatively_preserved(self):
        from chatgpt_export_archiver.cli import run_import_pipeline

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source.zip"
            sibling = base / "same-object.zip"
            write_zip(source, {"conversations.json": [conversation("hardlink-safe")]})
            os.link(source, sibling)
            with self.assertRaises(SourceChangedDuringReadError):
                run_import_pipeline(
                    base / "archive.db", str(source), cwd=base,
                    no_input_sha256=True, delete_input_on_success=True,
                )
            self.assertTrue(source.exists())
            self.assertTrue(sibling.exists())
            self.assertFalse((base / "archive.db").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_round6_delete_input_retargeted_symlink_is_preserved(self):
        from chatgpt_export_archiver.cli import run_import_pipeline

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            original = base / "original.zip"
            replacement = base / "replacement.zip"
            link = base / "source.zip"
            write_zip(original, {"conversations.json": [conversation("symlink-original")]})
            write_zip(replacement, {"conversations.json": [conversation("symlink-replacement")]})
            link.symlink_to(original)
            retargeted = False

            def progress(stage, _summary):
                nonlocal retargeted
                if stage == "import_index_rebuild_complete" and not retargeted:
                    self.assertTrue(os.path.lexists(link))
                    link.unlink()
                    link.symlink_to(replacement)
                    retargeted = True

            result = run_import_pipeline(
                base / "archive.db", str(link), cwd=base,
                no_input_sha256=True, delete_input_on_success=True,
                progress_callback=progress,
            )
            self.assertTrue(retargeted)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), replacement.resolve())
            self.assertTrue(original.exists())
            self.assertTrue(replacement.exists())
            self.assertIsNone(result["deleted_input"])
            self.assertTrue(result["delete_input_changed"])

    def test_round6_streamed_export_compare_and_failure_preserve_old_output(self):
        from chatgpt_export_archiver.utils import write_chunks_if_changed

        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "synthetic.txt"
            output.write_bytes(b"stable-output")
            original_mtime = output.stat().st_mtime_ns
            changed, digest, size = write_chunks_if_changed(
                output, ["stable", b"-output"], max_bytes=1024
            )
            self.assertFalse(changed)
            self.assertEqual(size, len(b"stable-output"))
            self.assertEqual(digest, hashlib.sha256(b"stable-output").hexdigest())
            self.assertEqual(output.stat().st_mtime_ns, original_mtime)

            def failing_chunks():
                yield b"replacement-prefix"
                raise RuntimeError("synthetic failure")

            with self.assertRaises(RuntimeError):
                write_chunks_if_changed(output, failing_chunks(), max_bytes=1024)
            self.assertEqual(output.read_bytes(), b"stable-output")
            with self.assertRaisesRegex(ValueError, "export_output_byte_limit_exceeded"):
                write_chunks_if_changed(output, [b"too-large"], max_bytes=3)
            self.assertEqual(output.read_bytes(), b"stable-output")
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_round6_unicode_scalar_and_timestamp_overflow_are_classified(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "conversations.json"
            valid = conversation(
                "unicode",
                title="literal\ufeff isolated \ud800",
                current_node="node",
                mapping={"node": message_node("node", None, "assistant", "left\ufeffright \ud800", 1)},
                create_time=10 ** 400,
            )
            invalid_id = conversation(
                "bad-id",
                current_node="bad\ud800",
                mapping={"bad\ud800": message_node("bad\ud800", None, "assistant", "hidden", 1)},
            )
            source.write_text(json.dumps([valid, invalid_id]), encoding="utf-8")
            db = base / "archive.db"
            result = run_cli(["--db", str(db), "import", "--input", str(source), "--no-input-sha256"])
            self.assertEqual(result[0], 0, result[1])
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT title, create_time FROM conversations WHERE conversation_id='unicode'"
            ).fetchone()
            self.assertEqual(row["title"], "literal\ufeff isolated \ufffd")
            self.assertIsNone(row["create_time"])
            self.assertEqual(conn.execute(
                "SELECT content_text FROM conversation_nodes WHERE conversation_id='unicode' AND node_id='node'"
            ).fetchone()[0], "left\ufeffright \ufffd")
            warning_types = {
                row[0] for row in conn.execute("SELECT warning_type FROM import_warnings")
            }
            self.assertIn("unicode_text_normalized", warning_types)
            self.assertIn("invalid_timestamp", warning_types)
            self.assertIn("canonical_id_invalid_unicode", warning_types)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id='bad-id'"
            ).fetchone()[0], 0)
            conn.close()

    def test_round6_json_framer_rejects_invalid_long_tail_and_single_element_budget(self):
        import chatgpt_export_archiver.scanner as scanner

        with mock.patch.object(scanner, "MAX_JSON_ELEMENT_CHARS", 128):
            with self.assertRaises(scanner.ConversationJsonElementTooLargeError):
                list(scanner._iter_json_array(iter(["[{\"padding\":\"" + "x" * 256 + "\"}]"])))
            with self.assertRaises(scanner.ConversationJsonElementTooLargeError):
                list(scanner._iter_json_array(iter(["[!" + " " * 256 + "]"])))
        with mock.patch.object(scanner, "MAX_JSON_ELEMENT_BYTES", 16):
            self.assertEqual(
                list(scanner._iter_json_array(iter(['["中中中中"]']))),
                ["中中中中"],
            )
            with self.assertRaises(scanner.ConversationJsonElementTooLargeError):
                list(scanner._iter_json_array(iter(['["中中中中中"]'])))
        with self.assertRaises(scanner.InvalidConversationEncodingError):
            list(scanner._iter_json_array(iter(["[{\"x\":1}\ufeff]"])))
        self.assertEqual(
            list(scanner._iter_json_array(iter(["[{\"literal\":\"a\ufeffb\",\"escaped\":\"a\\ufeffb\"}]"]))),
            [{"literal": "a\ufeffb", "escaped": "a\ufeffb"}],
        )

    def test_round6_cli_stats_and_export_use_one_snapshot(self):
        import threading
        import chatgpt_export_archiver.cli as cli_module

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "archive.db"
            seed = connect(db)
            init_db(seed)
            seed.execute("PRAGMA journal_mode=WAL")
            seed.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) "
                "VALUES ('snapshot', 'old title', 'n', 'h')"
            )
            seed.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, role, content_type, "
                "content_text, content_hash, is_on_current_path) "
                "VALUES ('snapshot', 'n', 'assistant', 'text', 'old body', 'old', 1)"
            )
            seed.commit()
            seed.close()

            stats_entered = threading.Event()
            stats_release = threading.Event()
            original_stats = cli_module.get_stats

            def paused_stats(conn):
                stats_entered.set()
                self.assertTrue(stats_release.wait(5))
                return original_stats(conn)

            stats_result: list[tuple[int, str]] = []
            with mock.patch.object(cli_module, "get_stats", side_effect=paused_stats):
                thread = threading.Thread(
                    target=lambda: stats_result.append(run_cli(["--db", str(db), "stats"])),
                    daemon=True,
                )
                thread.start()
                self.assertTrue(stats_entered.wait(5))
                writer = sqlite3.connect(db)
                writer.execute(
                    "INSERT INTO conversations(conversation_id, title, aggregate_hash) "
                    "VALUES ('later', 'later', 'later')"
                )
                writer.commit()
                writer.close()
                stats_release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(stats_result[0][0], 0)
            self.assertIn("conversations 1", stats_result[0][1])

            export_entered = threading.Event()
            export_release = threading.Event()
            original_export = cli_module.export_conversations

            def paused_export(conn, *args, **kwargs):
                export_entered.set()
                self.assertTrue(export_release.wait(5))
                return original_export(conn, *args, **kwargs)

            output = base / "exports"
            export_result: list[tuple[int, str]] = []
            with mock.patch.object(cli_module, "export_conversations", side_effect=paused_export):
                thread = threading.Thread(
                    target=lambda: export_result.append(run_cli([
                        "--db", str(db), "export", "--out", str(output), "--format", "txt",
                    ])),
                    daemon=True,
                )
                thread.start()
                self.assertTrue(export_entered.wait(5))
                writer = sqlite3.connect(db)
                writer.execute(
                    "UPDATE conversation_nodes SET content_text='new body', content_hash='new' "
                    "WHERE conversation_id='snapshot' AND node_id='n'"
                )
                writer.commit()
                writer.close()
                export_release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(export_result[0][0], 0, export_result[0][1])
            exported = "\n".join(path.read_text(encoding="utf-8") for path in output.glob("*.txt"))
            self.assertIn("old body", exported)
            self.assertNotIn("new body", exported)
            check = sqlite3.connect(db)
            self.assertGreaterEqual(check.execute("SELECT COUNT(*) FROM exports").fetchone()[0], 1)
            check.close()

    def test_round7_json_nesting_limit_is_lexical_and_stable(self):
        from chatgpt_export_archiver import scanner
        from chatgpt_export_archiver.json_safety import MAX_JSON_NESTING_DEPTH, JsonSafetyLimitError

        accepted = "[" + "[" * (MAX_JSON_NESTING_DEPTH - 1) + "0" + "]" * (MAX_JSON_NESTING_DEPTH - 1) + "]"
        self.assertEqual(len(list(scanner._iter_json_array([accepted]))), 1)
        rejected = "[" + "[" * (MAX_JSON_NESTING_DEPTH + 1) + "0" + "]" * (MAX_JSON_NESTING_DEPTH + 1) + "]"
        with self.assertRaises(JsonSafetyLimitError) as caught:
            list(scanner._iter_json_array([rejected]))
        self.assertEqual(caught.exception.code, "json_nesting_limit_exceeded")

    def test_round7_large_integer_failures_are_stable_and_content_free(self):
        for digits in (5_000, 100_000):
            with self.subTest(digits=digits), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                archive = base / "input.zip"
                sentinel = b"7" * digits
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr(
                        "conversations.json",
                        b'[{"id":"bounded","mapping":{},"metadata":' + sentinel + b"}]",
                    )
                code, output = run_cli([
                    "--db", str(base / "archive.db"), "import",
                    "--input", str(archive), "--no-input-sha256",
                ])
                self.assertEqual(code, 2, output)
                self.assertIn("json_integer_too_large", output)
                self.assertLess(len(output), 4096)
                self.assertNotIn("7" * 256, output)

    def test_round7_import_nesting_and_scalar_limits_use_stable_json_decode_codes(self):
        from chatgpt_export_archiver.json_safety import MAX_JSON_SCALAR_COUNT

        fixtures = {
            "json_nesting_limit_exceeded": (
                b'[{"id":"deep","mapping":{},"metadata":'
                + b"[" * 300 + b"0" + b"]" * 300 + b"}]"
            ),
            "json_scalar_limit_exceeded": (
                b'[{"id":"many","mapping":{},"metadata":['
                + b",".join([b"0"] * (MAX_JSON_SCALAR_COUNT + 1)) + b"]}]"
            ),
        }
        for expected, payload in fixtures.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                source = base / "conversations.json"
                source.write_bytes(payload)
                code, output = run_cli([
                    "--db", str(base / "archive.db"), "import",
                    "--input", str(source), "--no-input-sha256",
                ])
                self.assertEqual(code, 2, output)
                self.assertIn(expected, output)
                self.assertLess(len(output), 4096)

    def test_realistic_large_conversation_scalar_count_remains_importable(self):
        from chatgpt_export_archiver.json_safety import MAX_JSON_SCALAR_COUNT

        self.assertGreaterEqual(MAX_JSON_SCALAR_COUNT, 200_000)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "conversations.json"
            source.write_text(
                json.dumps(
                    [{
                        "id": "large-synthetic",
                        "title": "synthetic",
                        "current_node": "node-1",
                        "mapping": {
                            "node-1": {
                                "id": "node-1",
                                "parent": None,
                                "children": [],
                                "message": {
                                    "id": "message-1",
                                    "author": {"role": "assistant"},
                                    "content": {
                                        "content_type": "text",
                                        "parts": ["synthetic body"],
                                    },
                                },
                            }
                        },
                        "metadata": [0] * 190_000,
                    }],
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            db = base / "archive.db"
            code, output = run_cli([
                "--db", str(db), "import", "--input", str(source), "--no-input-sha256",
            ])
            self.assertEqual(code, 0, output)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
            finally:
                conn.close()

    def test_round7_identity_rebuild_refuses_dependent_user_objects_before_ddl(self):
        from chatgpt_export_archiver.db import DatabaseMigrationError, migrate_database

        fixture = (Path(__file__).resolve().parent / "fixtures" / "legacy-fa37b3d.sql").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "custom.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.executescript(fixture)
            conn.executescript(
                """
                CREATE UNIQUE INDEX "user quoted index"
                    ON conversations(lower(title)) WHERE title IS NOT NULL;
                CREATE VIEW user_conversation_view AS
                    SELECT conversation_id, title FROM conversations;
                CREATE TRIGGER user_conversation_trigger AFTER UPDATE OF title ON conversations
                    BEGIN SELECT NEW.title; END;
                """
            )
            conn.commit()
            before = list(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"))
            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(conn)
            self.assertEqual(caught.exception.code, "database_custom_objects_require_manual_migration")
            self.assertNotIn("sql", caught.exception.detail)
            self.assertEqual(
                {item["type"] for item in caught.exception.detail["objects"]},
                {"index", "trigger", "view"},
            )
            self.assertEqual(before, list(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name")))
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE '%_v3'").fetchone())
            conn.close()

    def test_round7_export_budget_token_avoids_second_aggregate(self):
        from chatgpt_export_archiver.db import init_db
        from chatgpt_export_archiver.exporter import iter_conversation_export_nodes, validate_conversation_export_budget

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute("INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 't', 'h')")
        conn.execute("INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES ('c', 'n', 'body')")
        conn.commit()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        conv = conn.execute("SELECT * FROM conversations WHERE conversation_id='c'").fetchone()
        token = validate_conversation_export_budget(conn, "c", path="all", include_internal=True)
        list(iter_conversation_export_nodes(conn, conv, path="all", include_internal=True, validated_budget=token))
        budget_scans = [
            sql for sql in statements
            if "FROM conversation_nodes WHERE conversation_id IN" in sql
            and "storage_rowid" in sql
        ]
        self.assertEqual(len(budget_scans), 1)
        self.assertFalse(any("length(CAST(content_text AS BLOB))" in sql for sql in statements))
        conn.close()

    def test_round7_delete_barrier_never_unlinks_replacement_after_identity_check(self):
        from chatgpt_export_archiver import scanner

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "input.zip"
            path.write_bytes(b"original synthetic bytes")
            source = scanner.InputSource(
                path=path, kind="zip", size=path.stat().st_size, delete_target=path
            )
            replacement = b"replacement synthetic bytes"

            def replace_after_check(_source):
                path.unlink()
                path.write_bytes(replacement)
                return True

            with mock.patch.object(
                scanner, "delete_input_identity_is_current", side_effect=replace_after_check
            ):
                self.assertFalse(scanner.delete_input_if_unchanged(source))
            self.assertEqual(path.read_bytes(), replacement)
            self.assertFalse(any(Path(td).glob(".chatgpt-archive-delete-*")))

    def test_round7_directory_source_open_rejects_post_scan_replacement(self):
        from chatgpt_export_archiver.scanner import SourceChangedDuringReadError

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source_path = base / "conversations.json"
            source_path.write_text("[]", encoding="utf-8")
            source = resolve_input(str(base), base)
            entries = list_source_entries(source)
            self.assertEqual([entry.source_path for entry in entries], ["conversations.json"])
            replacement = base / "replacement.json"
            replacement.write_text("[]", encoding="utf-8")
            os.replace(replacement, source_path)
            with self.assertRaises((SourceChangedDuringReadError, ValueError)) as caught:
                list(iter_json_array_from_source(source, "conversations.json"))
            self.assertIn("source_changed_during_read", str(caught.exception))

    def test_round7_file_descriptor_hash_rejects_post_resolution_replacement(self):
        from chatgpt_export_archiver import scanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            path = base / "conversations.json"
            path.write_text("[]", encoding="utf-8")
            source = resolve_input(str(path), base)
            replacement = base / "replacement.json"
            replacement.write_text("[]", encoding="utf-8")
            os.replace(replacement, path)
            with self.assertRaises(scanner.SourceChangedDuringReadError):
                scanner.sha256_input_source(source)

    def test_round7_source_total_member_limit_counts_zip_metadata_and_directories(self):
        from chatgpt_export_archiver import scanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "members.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("__MACOSX/", b"")
                zf.writestr("conversations.json", b"[]")
            source = scanner.resolve_input(str(archive), base)
            with mock.patch.object(scanner, "MAX_SOURCE_TOTAL_MEMBERS", 1), self.assertRaisesRegex(
                ValueError, "source_member_limit_exceeded"
            ):
                scanner.list_source_entries(source)

            directory = base / "directory"
            directory.mkdir()
            (directory / "conversations.json").write_text("[]", encoding="utf-8")
            (directory / "nested").mkdir()
            (directory / "nested" / "other.txt").write_text("synthetic", encoding="utf-8")
            with mock.patch.object(scanner, "MAX_SOURCE_TOTAL_MEMBERS", 2), self.assertRaisesRegex(
                ValueError, "source_member_limit_exceeded"
            ):
                scanner.list_source_entries(scanner.resolve_input(str(directory), base))

    def test_round7_migration_preserves_unrelated_user_objects_and_behavior(self):
        from chatgpt_export_archiver.db import DATABASE_SCHEMA_VERSION, migrate_database

        fixture = (Path(__file__).resolve().parent / "fixtures" / "legacy-fa37b3d.sql").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "unrelated.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.executescript(fixture)
            conn.executescript(
                """
                CREATE TABLE user_notes(note_id INTEGER PRIMARY KEY, note TEXT NOT NULL);
                CREATE INDEX user_notes_text ON user_notes(note);
                CREATE TRIGGER user_notes_default AFTER INSERT ON user_notes
                WHEN NEW.note = '' BEGIN
                    UPDATE user_notes SET note = 'synthetic-default' WHERE note_id = NEW.note_id;
                END;
                """
            )
            before = list(conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name LIKE 'user_notes%' ORDER BY type, name"
            ))
            result = migrate_database(conn)
            self.assertTrue(result["changed"])
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION)
            self.assertEqual(before, list(conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name LIKE 'user_notes%' ORDER BY type, name"
            )))
            conn.execute("INSERT INTO user_notes(note) VALUES ('')")
            self.assertEqual(conn.execute("SELECT note FROM user_notes").fetchone()[0], "synthetic-default")
            plan = " ".join(str(row[3]) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT note_id FROM user_notes WHERE note='synthetic-default'"
            ))
            self.assertIn("user_notes_text", plan)
            conn.close()

    def test_round7_read_capability_cache_is_cheap_and_invalidates_external_data(self):
        from chatgpt_export_archiver.db import (
            init_db,
            invalidate_read_capability_cache,
            read_request_capabilities,
        )

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "capabilities.db"
            writer = sqlite3.connect(db)
            writer.row_factory = sqlite3.Row
            init_db(writer)
            writer.commit()
            reader = sqlite3.connect(db)
            reader.row_factory = sqlite3.Row
            invalidate_read_capability_cache()
            self.assertTrue(read_request_capabilities(reader).schema_status["schema_compatible"])
            statements: list[str] = []
            reader.set_trace_callback(statements.append)
            cached = read_request_capabilities(reader)
            self.assertTrue(cached.schema_status["data_compatible"])
            self.assertLessEqual(len(statements), 6)
            self.assertFalse(any("sqlite_schema" in sql or "pragma_table" in sql.casefold() for sql in statements))

            overlong = "x" * (16 * 1024 + 1)
            writer.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, 'synthetic', 'h')",
                (overlong,),
            )
            writer.commit()
            statements.clear()
            refreshed = read_request_capabilities(reader)
            self.assertFalse(refreshed.schema_status["data_compatible"])
            self.assertEqual(refreshed.schema_status["data_error_code"], "database_data_incompatible")
            self.assertTrue(any("FROM conversations" in sql for sql in statements))
            reader.close()
            writer.close()

    def test_round7_read_capability_cache_invalidates_database_file_replacement(self):
        from chatgpt_export_archiver.db import (
            init_db,
            invalidate_read_capability_cache,
            read_request_capabilities,
        )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            active = base / "active.db"
            replacement = base / "replacement.db"
            for path in (active, replacement):
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                init_db(conn)
                conn.commit()
                conn.close()
            replacement_conn = sqlite3.connect(replacement)
            replacement_conn.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, 'synthetic', 'h')",
                ("r" * (16 * 1024 + 1),),
            )
            replacement_conn.commit()
            replacement_conn.close()

            invalidate_read_capability_cache()
            first = sqlite3.connect(active)
            first.row_factory = sqlite3.Row
            self.assertTrue(read_request_capabilities(first).schema_status["data_compatible"])
            first.close()
            original_inode = active.stat().st_ino
            os.replace(replacement, active)
            self.assertNotEqual(active.stat().st_ino, original_inode)
            second = sqlite3.connect(active)
            second.row_factory = sqlite3.Row
            status = read_request_capabilities(second).schema_status
            self.assertFalse(status["data_compatible"])
            self.assertEqual(status["data_error_code"], "database_data_incompatible")
            second.close()

    def test_round8_fresh_reader_cache_uses_durable_address_generation(self):
        from chatgpt_export_archiver.db import invalidate_read_capability_cache, read_request_capabilities

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "durable-revision.db"
            writer = sqlite3.connect(db)
            writer.row_factory = sqlite3.Row
            init_db(writer)
            writer.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 't', 'h')"
            )
            writer.commit()
            invalidate_read_capability_cache()
            first = sqlite3.connect(db)
            first.row_factory = sqlite3.Row
            self.assertTrue(read_request_capabilities(first).schema_status["data_compatible"])
            first.close()

            writer.execute(
                "UPDATE conversations SET current_node = ? WHERE conversation_id = 'c'",
                ("x" * (16 * 1024 + 1),),
            )
            writer.commit()
            second = sqlite3.connect(db)
            second.row_factory = sqlite3.Row
            refreshed = read_request_capabilities(second)
            self.assertFalse(refreshed.schema_status["data_compatible"])
            self.assertGreaterEqual(refreshed.generation_snapshot[2], 1)
            second.close()
            writer.close()

    def test_round8_generation_field_matrix_and_rollback_are_domain_specific(self):
        def generations(conn):
            return dict(conn.execute(
                "SELECT name, generation FROM archive_generations "
                "WHERE name IN ('title', 'message', 'address', 'graph') ORDER BY name"
            ))

        def display_revision(conn):
            return int(conn.execute(
                "SELECT generation FROM archive_generations WHERE name = 'display:1'"
            ).fetchone()[0])

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            "INSERT INTO conversations(conversation_id, title, current_node, source_file, aggregate_hash) "
            "VALUES ('c', 't', 'n', 's', 'h')"
        )
        conn.execute(
            """INSERT INTO conversation_nodes(
                   conversation_id, node_id, parent_node_id, children_json,
                   message_id, role, author_name, content_type, content_text,
                   content_hash, metadata_json, is_on_current_path, raw_message_json
               ) VALUES ('c', 'n', NULL, '[]', 'm', 'user', 'a', 'text', 'body',
                         'hash', '{}', 1, '{}')"""
        )
        conn.commit()

        def assert_domains(sql, params, expected, *, display_changed=False):
            before = generations(conn)
            before_display = display_revision(conn)
            conn.execute(sql, params)
            conn.commit()
            after = generations(conn)
            changed = {name for name in before if after[name] == before[name] + 1}
            self.assertEqual(changed, set(expected), sql)
            self.assertTrue(all(
                after[name] == before[name] + (1 if name in expected else 0)
                for name in before
            ), sql)
            self.assertEqual(
                display_revision(conn),
                before_display + (1 if display_changed else 0),
                sql,
            )

        assert_domains("UPDATE conversations SET title=? WHERE conversation_id='c'", ("t2",), {"title"})
        assert_domains("UPDATE conversations SET current_node=? WHERE conversation_id='c'", ("n2",), {"address", "graph"})
        assert_domains("UPDATE conversations SET source_file=? WHERE conversation_id='c'", ("s2",), set())
        for column, value, domains, display_changed in (
            ("message_id", "m2", {"message", "address"}, False),
            ("role", "assistant", {"message"}, False),
            ("author_name", "a2", {"message"}, False),
            ("content_type", "code", {"message"}, True),
            ("content_text", "body2", {"message"}, True),
            ("content_hash", "hash2", {"message"}, True),
            ("raw_message_json", '{"x":1}', {"message"}, True),
            ("parent_node_id", "p", {"address", "graph"}, False),
            ("children_json", '["child"]', {"address", "graph"}, False),
            ("is_on_current_path", 0, {"graph"}, False),
            ("metadata_json", '{"safe":true}', set(), False),
        ):
            assert_domains(
                f"UPDATE conversation_nodes SET {column}=? WHERE conversation_id='c' AND node_id='n'",
                (value,),
                domains,
                display_changed=display_changed,
            )
        before_rollback = generations(conn)
        before_display_rollback = display_revision(conn)
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE conversation_nodes SET content_text='rolled back' "
            "WHERE conversation_id='c' AND node_id='n'"
        )
        conn.rollback()
        self.assertEqual(generations(conn), before_rollback)
        self.assertEqual(display_revision(conn), before_display_rollback)
        conn.close()

    def test_round8_v3_to_v4_migration_adds_durable_domains_without_invalidating_web_index(self):
        from chatgpt_export_archiver.db import GENERATION_TRIGGER_DDL, migrate_database
        from chatgpt_export_archiver.web_db import web_index_status

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "v3-predecessor.db"
            conn = connect(db)
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 't', 'h')"
            )
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES ('c', 'n', 'body')"
            )
            conn.commit()
            conn.close()
            create_web_indexes(db)
            predecessor = sqlite3.connect(db)
            for name in list(GENERATION_TRIGGER_DDL):
                if "_address_" in name or "_graph_" in name:
                    predecessor.execute(f'DROP TRIGGER "{name}"')
            predecessor.execute("DELETE FROM archive_generations WHERE name IN ('address', 'graph')")
            predecessor.execute("PRAGMA user_version = 3")
            predecessor.commit()
            metadata_before = predecessor.execute(
                "SELECT key, value FROM web_index_metadata ORDER BY key"
            ).fetchall()
            predecessor.row_factory = sqlite3.Row
            migrate_database(predecessor)
            self.assertEqual(predecessor.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                {row[0] for row in predecessor.execute(
                    "SELECT name FROM archive_generations "
                    "WHERE name IN ('title', 'message', 'address', 'graph')"
                )},
                {"title", "message", "address", "graph"},
            )
            self.assertGreaterEqual(predecessor.execute(
                "SELECT generation FROM archive_generations WHERE name='display:1'"
            ).fetchone()[0], 1)
            self.assertEqual(
                [tuple(row) for row in predecessor.execute(
                    "SELECT key, value FROM web_index_metadata ORDER BY key"
                )],
                metadata_before,
            )
            self.assertTrue(web_index_status(predecessor)["web_normalized_indexed"])
            predecessor.close()

    def test_round7_manifest_generation_failure_retains_previous_pair(self):
        from chatgpt_export_archiver import exporter
        from chatgpt_export_archiver.db import init_db

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "archive.db"
            out = base / "export"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute("INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 'title', 'h')")
            conn.execute("INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES ('c', 'n', 'body')")
            conn.commit()
            exporter.export_conversations(conn, out, ["txt"])
            previous = {
                name: (out / name).read_bytes()
                for name in ("manifest.jsonl", "manifest.csv")
            }

            def fail_csv(*_args, **_kwargs):
                yield b"conversation_id\n"
                raise OSError("synthetic disk full")

            with mock.patch.object(exporter, "_iter_csv_manifest", side_effect=fail_csv), self.assertRaises(OSError):
                exporter.export_conversations(conn, out, ["txt"], force=True)
            for name, data in previous.items():
                self.assertEqual((out / name).read_bytes(), data)
            self.assertFalse(list(out.glob(".*.candidate-*.tmp")))
            self.assertFalse(list(out.glob(".*.backup-*.tmp")))
            conn.close()

    def test_round7_archive_export_record_failure_keeps_valid_outputs_and_cleans_plan(self):
        from chatgpt_export_archiver import exporter
        from chatgpt_export_archiver.db import init_db

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            out = base / "export"
            conn = sqlite3.connect(base / "archive.db")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute("INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 'title', 'h')")
            conn.execute("INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES ('c', 'n', 'body')")
            conn.commit()
            with mock.patch.object(
                exporter, "record_export", side_effect=sqlite3.OperationalError("synthetic")
            ), self.assertRaises(sqlite3.OperationalError):
                exporter.export_conversations(conn, out, ["txt"])
            rows = [json.loads(line) for line in (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            output = out / rows[0]["output_path"]
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), rows[0]["output_hash"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0], 0)
            self.assertFalse(list(out.glob(".archive-export-plan-*.sqlite3")))
            conn.close()

    def test_round7_manifest_interrupt_cleans_candidates_and_preserves_previous_pair(self):
        from chatgpt_export_archiver import exporter
        from chatgpt_export_archiver.db import init_db

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            out = base / "export"
            conn = sqlite3.connect(base / "archive.db")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute("INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 'title', 'h')")
            conn.execute("INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES ('c', 'n', 'body')")
            conn.commit()
            exporter.export_conversations(conn, out, ["txt"])
            previous = {(out / name): (out / name).read_bytes() for name in ("manifest.jsonl", "manifest.csv")}

            def interrupt(*_args, **_kwargs):
                yield b"partial"
                raise KeyboardInterrupt()

            with mock.patch.object(exporter, "_iter_jsonl_manifest", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                exporter.export_conversations(conn, out, ["txt"], force=True)
            for path, data in previous.items():
                self.assertEqual(path.read_bytes(), data)
            self.assertFalse(list(out.glob(".*.candidate-*.tmp")))
            self.assertFalse(list(out.glob(".archive-export-plan-*.sqlite3")))
            conn.close()

    def test_round8_manifest_partial_rollback_preserves_recovery_backup(self):
        from chatgpt_export_archiver import exporter
        from chatgpt_export_archiver.db import init_db

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            out = base / "export"
            conn = sqlite3.connect(base / "archive.db")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 'title', 'h')"
            )
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES ('c', 'n', 'body')"
            )
            conn.commit()
            exporter.export_conversations(conn, out, ["txt"])
            previous_jsonl = (out / "manifest.jsonl").read_bytes()
            previous_csv = (out / "manifest.csv").read_bytes()
            real_replace = exporter.os.replace

            def fail_publish_and_one_restore(src, dst):
                source_name = Path(src).name
                target_name = Path(dst).name
                if source_name.startswith(".manifest.csv.candidate-") and target_name == "manifest.csv":
                    raise OSError("synthetic publish failure")
                if source_name.startswith(".manifest.jsonl.backup-") and target_name == "manifest.jsonl":
                    raise PermissionError("synthetic restore failure")
                return real_replace(src, dst)

            with mock.patch.object(exporter.os, "replace", side_effect=fail_publish_and_one_restore):
                with self.assertRaises(exporter.ManifestPairRecoveryError) as raised:
                    exporter.export_conversations(conn, out, ["txt"], force=True)
            self.assertEqual(raised.exception.code, "manifest_pair_partial_recovery")
            self.assertEqual(
                raised.exception.diagnostics,
                [{"operation": "rollback_restore_failed", "error_type": "PermissionError"}],
            )
            self.assertFalse((out / "manifest.jsonl").exists())
            self.assertEqual((out / "manifest.csv").read_bytes(), previous_csv)
            recovery = list(out.glob(".manifest.jsonl.backup-*.tmp"))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_bytes(), previous_jsonl)
            self.assertFalse(list(out.glob(".*.candidate-*.tmp")))
            self.assertNotIn(str(out), json.dumps(raised.exception.diagnostics))
            conn.close()

    def test_round8_manifest_restore_overwrites_target_after_unlink_failure(self):
        from chatgpt_export_archiver import exporter
        from chatgpt_export_archiver.db import init_db

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            out = base / "export"
            conn = sqlite3.connect(base / "archive.db")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('c', 'title', 'h')"
            )
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES ('c', 'n', 'body')"
            )
            conn.commit()
            exporter.export_conversations(conn, out, ["txt"])
            previous = {
                name: (out / name).read_bytes()
                for name in ("manifest.jsonl", "manifest.csv")
            }
            real_replace = exporter.os.replace
            real_unlink = exporter.os.unlink
            refused_target_unlink = False

            def fail_second_publish(src, dst):
                if Path(src).name.startswith(".manifest.csv.candidate-") and Path(dst).name == "manifest.csv":
                    raise OSError("synthetic publish failure")
                return real_replace(src, dst)

            def fail_published_unlink(path, *args, **kwargs):
                nonlocal refused_target_unlink
                if Path(path) == out / "manifest.jsonl" and not refused_target_unlink:
                    refused_target_unlink = True
                    raise PermissionError("synthetic unlink failure")
                return real_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(exporter.os, "replace", side_effect=fail_second_publish),
                mock.patch.object(exporter.os, "unlink", side_effect=fail_published_unlink),
                self.assertRaises(OSError),
            ):
                exporter.export_conversations(conn, out, ["txt"], force=True)
            self.assertTrue(refused_target_unlink)
            for name, data in previous.items():
                self.assertEqual((out / name).read_bytes(), data)
            self.assertFalse(list(out.glob(".*.backup-*.tmp")))
            self.assertFalse(list(out.glob(".*.candidate-*.tmp")))
            conn.close()

    def test_round7_release_path_writer_streams_large_payload(self):
        from tools import make_release_zip

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "large.bin"
            source.write_bytes(b"round7-streaming-payload\n" * 500_000)
            output = base / "large.zip"
            payload = [("large.bin", source)]
            manifest = make_release_zip._file_manifest(payload)
            tracemalloc.start()
            make_release_zip._write_archive_paths(output, payload, manifest)
            make_release_zip._verify_archive_paths(output, manifest)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertLess(peak, 8 * 1024 * 1024)
            self.assertEqual(manifest[0]["size"], source.stat().st_size)

    def test_round7_effective_current_metadata_never_uses_cardinality_as_identity(self):
        from chatgpt_export_archiver.current_path import (
            effective_current_metadata,
            ensure_effective_current_views,
        )
        from chatgpt_export_archiver.db import init_db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        ids = [f"c{index:03d}" for index in range(401)]
        conn.executemany(
            "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, ?, 'h')",
            ((conversation_id, conversation_id) for conversation_id in ids),
        )
        conn.executemany(
            "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES (?, ?, 'body')",
            ((conversation_id, f"n{index:03d}") for index, conversation_id in enumerate(ids)),
        )
        conn.commit()
        ensure_effective_current_views(conn, None)
        requested = ids[:-1] + ["missing"]
        result = effective_current_metadata(conn, requested)
        self.assertEqual(set(result), set(ids[:-1]))
        conn.close()

    def test_round8_finite_effective_current_scope_enforces_aggregate_budget(self):
        from chatgpt_export_archiver import current_path
        from chatgpt_export_archiver.db import init_db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.executemany(
            "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, 't', 'h')",
            (("c1",), ("c2",)),
        )
        conn.executemany(
            "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) VALUES (?, ?, 'body')",
            (("c1", "n1"), ("c2", "n2")),
        )
        conn.commit()
        with mock.patch.object(current_path, "MAX_EFFECTIVE_CURRENT_SCOPE_NODES", 1):
            with self.assertRaises(current_path.EffectiveCurrentResourceLimitError) as raised:
                current_path.ensure_effective_current_views(conn, ["c1", "c2"])
        self.assertEqual(raised.exception.code, "effective_current_scope_too_large")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM effective_current_nodes").fetchone()[0], 0)
        conn.close()

    def test_round8_managed_object_name_collisions_are_refused_before_ddl(self):
        from chatgpt_export_archiver.db import DatabaseMigrationError, migrate_database

        for object_kind in ("trigger", "display_trigger", "index"):
            with self.subTest(object_kind=object_kind):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                init_db(conn)
                conn.execute("CREATE TABLE user_owned(value INTEGER, touched INTEGER DEFAULT 0)")
                if object_kind in {"trigger", "display_trigger"}:
                    name = (
                        "archive_display_revision_node_insert"
                        if object_kind == "display_trigger"
                        else "archive_title_generation_insert"
                    )
                    conn.execute(f'DROP TRIGGER "{name}"')
                    conn.execute(
                        f'''CREATE TRIGGER "{name}" AFTER INSERT ON user_owned
                            BEGIN UPDATE user_owned SET touched = 1 WHERE rowid = NEW.rowid; END'''
                    )
                else:
                    name = "idx_nodes_conversation_path"
                    conn.execute(f'DROP INDEX "{name}"')
                    conn.execute(f'CREATE INDEX "{name}" ON user_owned(value)')
                conn.commit()
                before = list(conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
                ))
                generations = list(conn.execute(
                    "SELECT name, generation FROM archive_generations ORDER BY name"
                ))
                with self.assertRaises(DatabaseMigrationError) as raised:
                    migrate_database(conn)
                self.assertEqual(raised.exception.code, "database_managed_object_name_collision")
                self.assertEqual(
                    before,
                    list(conn.execute(
                        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
                    )),
                )
                self.assertEqual(
                    generations,
                    list(conn.execute(
                        "SELECT name, generation FROM archive_generations ORDER BY name"
                    )),
                )
                if object_kind in {"trigger", "display_trigger"}:
                    conn.execute("INSERT INTO user_owned(value) VALUES (1)")
                    self.assertEqual(conn.execute("SELECT touched FROM user_owned").fetchone()[0], 1)
                conn.close()

    def test_round8_managed_object_wrong_type_and_case_variation_are_collisions(self):
        from chatgpt_export_archiver.db import DatabaseMigrationError, migrate_database

        for scenario in ("wrong_type", "case_variation"):
            with self.subTest(scenario=scenario):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                init_db(conn)
                conn.execute("CREATE TABLE user_owned(value INTEGER, touched INTEGER DEFAULT 0)")
                if scenario == "wrong_type":
                    conn.execute('DROP INDEX "idx_nodes_conversation_path"')
                    conn.execute('CREATE TABLE "idx_nodes_conversation_path"(value TEXT)')
                else:
                    conn.execute('DROP TRIGGER "archive_title_generation_insert"')
                    conn.execute(
                        '''CREATE TRIGGER "ARCHIVE_TITLE_GENERATION_INSERT"
                           AFTER INSERT ON user_owned BEGIN
                           UPDATE user_owned SET touched = 1 WHERE rowid = NEW.rowid; END'''
                    )
                conn.commit()
                before = list(conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
                ))
                with self.assertRaises(DatabaseMigrationError) as raised:
                    migrate_database(conn)
                self.assertEqual(raised.exception.code, "database_managed_object_name_collision")
                self.assertEqual(before, list(conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
                )))
                if scenario == "case_variation":
                    conn.execute("INSERT INTO user_owned(value) VALUES (1)")
                    self.assertEqual(conn.execute("SELECT touched FROM user_owned").fetchone()[0], 1)
                conn.close()

    def test_round8_nonlist_json_is_rejected_before_deep_materialization(self):
        from chatgpt_export_archiver.scanner import ConversationJsonTopLevelError, _iter_json_array

        deeply_nested = "{" + '"x":{' * 10_000
        with self.assertRaises(ConversationJsonTopLevelError):
            list(_iter_json_array([deeply_nested]))

    def test_round8_directory_depth_limit_is_iterative_and_stable(self):
        from chatgpt_export_archiver import scanner

        with tempfile.TemporaryDirectory() as td:
            root_dir = Path(td) / "input"
            root_dir.mkdir()
            (root_dir / "conversations.json").write_text("[]", encoding="utf-8")
            current = root_dir
            for _index in range(scanner.MAX_SOURCE_DIRECTORY_DEPTH + 1):
                current = current / "d"
                current.mkdir()
            (current / "metadata.txt").write_text("synthetic", encoding="utf-8")
            source = scanner.resolve_input(str(root_dir), Path(td))
            with self.assertRaisesRegex(ValueError, "source_directory_depth_limit_exceeded"):
                scanner.list_source_entries(source)

    def test_round8_zip_member_preflight_runs_before_zipfile_materialization(self):
        from chatgpt_export_archiver import scanner

        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "members.zip"
            with zipfile.ZipFile(archive, "w") as writer:
                writer.writestr("conversations.json", "[]")
                writer.writestr("a.txt", "a")
                writer.writestr("b.txt", "b")
            source = scanner.resolve_input(str(archive), Path(td))
            with (
                mock.patch.object(scanner, "MAX_SOURCE_TOTAL_MEMBERS", 2),
                mock.patch.object(
                    scanner.zipfile,
                    "ZipFile",
                    side_effect=AssertionError("ZipFile must not run after central-directory rejection"),
                ),
                self.assertRaisesRegex(ValueError, "source_member_limit_exceeded"),
            ):
                scanner.list_source_entries(source)

    def test_round8_zip_preflight_ignores_eocd_signature_inside_comment(self):
        from chatgpt_export_archiver import scanner

        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "comment-signature.zip"
            with zipfile.ZipFile(archive, "w") as writer:
                writer.writestr("conversations.json", "[]")
                writer.comment = b"synthetic PK\x05\x06 comment bytes"
            with archive.open("rb") as stream:
                self.assertEqual(
                    scanner.preflight_zip_central_directory(stream, max_members=10),
                    1,
                )
                self.assertEqual(stream.tell(), 0)

    def test_round8_root_empty_parent_is_compatible_but_other_empty_ids_are_not(self):
        fixture = conversation()
        fixture["mapping"]["root"]["parent"] = ""
        self.assertIsNone(validate_conversation_element(fixture, "synthetic.json", 0))
        parsed = parse_conversation(fixture, "synthetic.json", 0)
        self.assertIsNone(next(node for node in parsed.nodes if node.node_id == "root").parent_node_id)
        fixture["mapping"]["root"]["id"] = ""
        self.assertEqual(
            validate_conversation_element(fixture, "synthetic.json", 0).warning_type,
            "canonical_id_empty",
        )

    def test_round8_delete_staging_rejects_same_inode_rewrite_with_restored_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source.zip"
            db = base / "archive.db"
            write_zip(source, {"conversations.json": [conversation("identity-a")]})
            original = source.read_bytes()
            mutated = bytearray(original)
            mutated[len(mutated) // 2] ^= 1
            original_stat = source.stat()
            changed = False

            def rewrite_after_read(stage, _summary):
                nonlocal changed
                if stage == "import_index_rebuild_complete" and not changed:
                    source.write_bytes(mutated)
                    os.utime(
                        source,
                        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                    )
                    changed = True

            from chatgpt_export_archiver.cli import run_import_pipeline
            result = run_import_pipeline(
                db, str(source), cwd=base, no_input_sha256=True,
                delete_input_on_success=True, progress_callback=rewrite_after_read,
            )
            self.assertTrue(changed)
            self.assertTrue(source.exists())
            self.assertEqual(source.stat().st_size, len(original))
            self.assertEqual(source.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertEqual(source.read_bytes(), bytes(mutated))
            self.assertTrue(db.exists())
            self.assertTrue(result["delete_input_changed"])
            self.assertIsNone(result["deleted_input"])

    def test_round8_delete_replacement_b_and_c_preserves_recovery_object(self):
        from chatgpt_export_archiver import scanner
        from chatgpt_export_archiver.cli import run_import_pipeline

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source.zip"
            db = base / "archive.db"
            write_zip(source, {"conversations.json": [conversation("identity-a")]})
            real_unlink = scanner.os.unlink

            def crash_after_staging(path, *args, **kwargs):
                if str(path).startswith(".chatgpt-archive-delete-"):
                    raise SystemExit("synthetic crash after durable staging")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(scanner.os, "unlink", side_effect=crash_after_staging):
                with self.assertRaises(SystemExit):
                    run_import_pipeline(
                        db, str(source), cwd=base, no_input_sha256=True,
                        delete_input_on_success=True,
                    )
            self.assertFalse(source.exists())
            journals = list(base.glob(f"{scanner.DELETE_INPUT_RECOVERY_PREFIX}*.json"))
            staged = [
                path for path in base.glob(".chatgpt-archive-delete-*")
                if not path.name.endswith(".json")
            ]
            self.assertEqual(len(journals), 1)
            self.assertEqual(len(staged), 1)
            token = journals[0].name.removeprefix(scanner.DELETE_INPUT_RECOVERY_PREFIX).removesuffix(".json")
            self.assertEqual(scanner.recover_delete_input(base, token), "restored")
            self.assertTrue(source.exists())
            self.assertFalse(journals[0].exists())
            self.assertFalse(staged[0].exists())
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute(
                    "SELECT status FROM import_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()[0], "finished")
            finally:
                conn.close()

    def test_round9_import_node_limit_is_independent_and_content_free(self):
        from chatgpt_export_archiver.parser import MAX_IMPORT_NODES_PER_CONVERSATION

        mapping = {
            f"n-{index}": null_message_node(f"n-{index}", None)
            for index in range(MAX_IMPORT_NODES_PER_CONVERSATION + 1)
        }
        fixture = conversation("node-limit", current_node="n-0", mapping=mapping)
        warning = validate_conversation_element(fixture, "synthetic.json", 7)
        self.assertIsNotNone(warning)
        self.assertEqual(warning.warning_type, "conversation_node_limit_exceeded")
        self.assertIsNone(warning.raw_json)
        self.assertEqual(json.loads(warning.keys_json), {"limit": 5000})
        del mapping[f"n-{MAX_IMPORT_NODES_PER_CONVERSATION}"]
        self.assertIsNone(validate_conversation_element(fixture, "synthetic.json", 7))

    def test_round9_bulk_import_advances_each_generation_once(self):
        from chatgpt_export_archiver.cli import run_import_pipeline
        from chatgpt_export_archiver.db import GENERATION_TRIGGER_DDL, generation_schema_contract_is_current

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "bulk.zip"
            db = base / "archive.db"
            children = [f"n-{index}" for index in range(1000)]
            mapping = {"root": null_message_node("root", None, children)}
            mapping.update({
                node_id: message_node(node_id, "root", "assistant", "synthetic bulk", 1_700_100_000 + index)
                for index, node_id in enumerate(children)
            })
            write_zip(source, {"conversations.json": [
                conversation("bulk-generation", current_node=children[-1], mapping=mapping)
            ]})
            run_import_pipeline(db, str(source), cwd=base, no_input_sha256=True)
            conn = connect(db)
            try:
                generations = dict(conn.execute(
                    "SELECT name, generation FROM archive_generations "
                    "WHERE name IN ('title', 'message', 'address', 'graph')"
                ))
                self.assertEqual(generations, {
                    "title": 1, "message": 1, "address": 1, "graph": 1,
                })
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM archive_generations WHERE name LIKE 'display:%'"
                ).fetchone()[0], 1001)
                self.assertTrue(generation_schema_contract_is_current(conn))
                trigger_count = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN ({})".format(
                        ",".join("?" for _ in GENERATION_TRIGGER_DDL)
                    ),
                    tuple(GENERATION_TRIGGER_DDL),
                ).fetchone()[0]
                self.assertEqual(trigger_count, len(GENERATION_TRIGGER_DDL))
            finally:
                conn.close()

    def test_round9_bulk_generation_rollback_restores_trigger_ddl(self):
        from chatgpt_export_archiver.db import (
            GENERATION_TRIGGER_DDL,
            begin_bulk_generation_aggregation,
            generation_schema_contract_is_current,
        )

        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "rollback.db")
            init_db(conn)
            conn.commit()
            try:
                before = dict(conn.execute(
                    "SELECT name, generation FROM archive_generations"
                ))
                conn.execute("BEGIN IMMEDIATE")
                begin_bulk_generation_aggregation(conn)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN ({})".format(
                        ",".join("?" for _ in GENERATION_TRIGGER_DDL)
                    ), tuple(GENERATION_TRIGGER_DDL),
                ).fetchone()[0], 0)
                conn.rollback()
                self.assertTrue(generation_schema_contract_is_current(conn))
                self.assertEqual(dict(conn.execute(
                    "SELECT name, generation FROM archive_generations"
                )), before)
            finally:
                conn.close()

    def test_round9_web_index_cross_process_lease_refuses_contender(self):
        from chatgpt_export_archiver.cli import run_import_pipeline

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "lease.zip"
            db = base / "archive.db"
            write_zip(source, {"conversations.json": [conversation("lease")]})
            run_import_pipeline(db, str(source), cwd=base, no_input_sha256=True)
            contender = []

            def progress(stage, _payload):
                if contender:
                    return
                script = (
                    "import sys\n"
                    "from pathlib import Path\n"
                    "from chatgpt_export_archiver.web_db import create_web_indexes, WebIndexBuildError\n"
                    "try:\n"
                    " create_web_indexes(Path(sys.argv[1]))\n"
                    "except WebIndexBuildError as exc:\n"
                    " print(exc.code)\n"
                    " raise SystemExit(0 if exc.code == 'web_index_build_in_progress' else 3)\n"
                    "raise SystemExit(4)\n"
                )
                contender.append(subprocess.run(
                    [sys.executable, "-c", script, str(db)],
                    cwd=Path(__file__).resolve().parents[1],
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                ))

            create_web_indexes(db, progress_callback=progress)
            self.assertEqual(len(contender), 1)
            self.assertEqual(contender[0].returncode, 0, contender[0].stderr)
            self.assertEqual(contender[0].stdout.strip(), "web_index_build_in_progress")
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM web_index_lease").fetchone()[0], 0)
                self.assertFalse(conn.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name GLOB '__chatgpt_webidx_*' LIMIT 1"
                ).fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
