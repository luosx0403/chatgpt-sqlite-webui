from __future__ import annotations

import json
import argparse
import contextlib
import hashlib
import io
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

from chatgpt_export_archiver.cli import build_parser, main
from chatgpt_export_archiver.db import connect, export_query, init_db, verify_database, drop_optional_web_indexes, _drop_table_with_shadows, _integrity_failure_is_web_index_only, _run_integrity_check, _line_names_web_index_table, _insert_fts_batch, _delete_fts_for_conversation
from chatgpt_export_archiver.logging_utils import configure_logging, get_logger, parse_log_level
from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager
from chatgpt_export_archiver.parser import _to_int_bool, compute_aggregate_hash, parse_conversation, validate_conversation_element
from chatgpt_export_archiver.scanner import list_source_entries, load_json_from_source, resolve_input
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
            "webui/node_modules",
            "webui/tsconfig.tsbuildinfo",
            "message_fts",
            "web_message_norm",
            "web_title_norm",
            "web_message_trigram",
            "web_title_trigram",
            "legacy raw FTS",
            "candidate backend",
            "normalized title scan",
            "full scan",
            "remote-safe",
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
        self.assertIsNone(warning)
        self.assertEqual(parse_conversation(fallback, "conversations.json", 0).conversation_id, "fallback-id")
        bad_title = conversation("bad-title", title={"PRIVATE_TITLE": "hidden"})
        parsed = parse_conversation(bad_title, "conversations.json", 0)
        self.assertIsNone(parsed.title)
        self.assertEqual(parsed.warnings[0].warning_type, "invalid_title_type")
        payload = json.dumps(parsed.warnings[0].__dict__)
        self.assertNotIn("PRIVATE_TITLE", payload)

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

    def test_standalone_conversations_json_is_detected_and_imported(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source_json = base / "conversations.json"
            source_json.write_text(json.dumps([conversation("standalone-json")]), encoding="utf-8")
            source = resolve_input(str(source_json), Path.cwd())
            self.assertEqual(source.kind, "json")
            entries = list_source_entries(source)
            self.assertEqual([entry.source_path for entry in entries if entry.is_selected_conversation_source], ["conversations.json"])
            self.assertEqual(load_json_from_source(source, "conversations.json")[0]["id"], "standalone-json")
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(source_json), "--no-input-sha256"]), 0)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT input_kind FROM import_runs").fetchone()[0], "json")
            finally:
                conn.close()

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
                zf.writestr("conversations-001.json", "{not valid json")
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
                load_json_from_source(source, "conversations.json")

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
            with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("synthetic lock")):
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
                "aggregate_hash,conversation_id,create_time,current_node,format,output_hash,output_path,source_file,title,update_time",
            )
            rows = [json.loads(line) for line in (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["output_path"] for row in rows], sorted(row["output_path"] for row in rows))
            self.assertEqual((out / "manifest.jsonl").read_bytes(), (out / "manifest.jsonl").read_text(encoding="utf-8").encode("utf-8"))

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
        class FakeConn:
            def execute(self, sql):
                if "web_title_norm" in sql:
                    raise sqlite3.OperationalError("synthetic /private/path should not be reported")

        def fake_shadow_drop(conn, table):
            if table == "web_message_trigram":
                return [{"table": "web_message_trigram_data", "error_type": "OperationalError"}]
            return []

        with mock.patch("chatgpt_export_archiver.db._drop_table_with_shadows", side_effect=fake_shadow_drop):
            failures = drop_optional_web_indexes(FakeConn())
        self.assertEqual(
            failures,
            [
                {"table": "web_message_trigram_data", "error_type": "OperationalError"},
                {"table": "web_title_norm", "error_type": "OperationalError"},
            ],
        )
        self.assertNotIn("/private/path", json.dumps(failures))

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

            def fake_shadow_drop(conn, table):
                if table == "web_message_trigram":
                    return [{"table": "web_message_trigram_data", "error_type": "OperationalError"}]
                return []

            with mock.patch("chatgpt_export_archiver.web_db._drop_table_with_shadows", side_effect=fake_shadow_drop):
                code, output = run_cli(["--db", str(db), "web-index"])
            self.assertEqual(code, 0, output)
            self.assertIn("drop_failures_count 1", output)
            self.assertIn("drop_failure table=web_message_trigram_data error_type=OperationalError", output)
            self.assertNotIn(str(base), output)
            self.assertNotIn(z.name, output)

    def test_core_fts_unavailable_is_downgraded_but_other_errors_raise(self):
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
        with self.assertRaises(sqlite3.OperationalError):
            _insert_fts_batch(FakeConn("database disk image is malformed /private/path"), [parsed])

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
            result = create_web_indexes(db)
            self.assertIn("indexed_messages", result)
            self.assertIn("indexed_titles", result)
            rebuild_conn = sqlite3.connect(db)
            try:
                tables = {
                    row[0]
                    for row in rebuild_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                    ).fetchall()
                }
                self.assertIn("web_index_metadata", tables)
                self.assertIn("web_message_norm", tables)
                self.assertIn("web_title_norm", tables)
                self.assertIn("web_message_trigram", tables)
                self.assertIn("web_title_trigram", tables)
                self.assertTrue(rebuild_conn.execute("SELECT 1 FROM web_index_metadata").fetchone() is not None)
                self.assertTrue(rebuild_conn.execute("SELECT 1 FROM web_message_norm").fetchone() is not None)
            finally:
                rebuild_conn.close()

    def test_web_index_norm_tables_drop_plainly_and_trigram_uses_shadow_helper(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            z = base / "export.zip"
            db = base / "archive.db"
            write_zip(z, {"conversations.json": [conversation("web-drop-policy")]})
            self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
            calls: list[str] = []

            def wrapped(conn, table):
                calls.append(table)
                return _drop_table_with_shadows(conn, table)

            with mock.patch("chatgpt_export_archiver.web_db._drop_table_with_shadows", side_effect=wrapped):
                self.assertEqual(main(["--db", str(db), "web-index"]), 0)
            self.assertEqual(calls, ["web_message_trigram", "web_title_trigram"])
            conn = connect(db)
            try:
                self.assertTrue(verify_database(conn)["ok"])
            finally:
                conn.close()
            # _drop_table_with_shadows must clean them all
            conn = sqlite3.connect(db)
            try:
                _drop_table_with_shadows(conn, "web_message_trigram")
                conn.commit()
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                    ).fetchall()
                }
                self.assertNotIn("web_message_trigram", tables)
                self.assertNotIn("web_message_trigram_content", tables)
                self.assertNotIn("web_message_trigram_data", tables)
                self.assertNotIn("web_message_trigram_idx", tables)
                self.assertNotIn("web_message_trigram_config", tables)
                self.assertNotIn("web_message_trigram_docsize", tables)
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
                "tests/test_archiver.py", "tests/test_web_api.py",
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
                    warnings = conn.execute("SELECT warning_type FROM import_warnings WHERE import_run_id = ?", (summary["import_run_id"],)).fetchall()
                    self.assertEqual([row["warning_type"] for row in warnings], ["non_finite_json_number"])
                    self.assertEqual(summary["warnings"], len(warnings))
                    self.assertEqual(summary["warnings_by_type"], [{"count": 1, "warning_type": "non_finite_json_number"}])
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
                    archive.writestr("conversations.json", "{")
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
            self.assertIn("foreign_key_violations ", output)
            self.assertIn("foreign_key_violation_table conversation_nodes", output)

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
                mock.patch.object(make_release_zip, "_write_archive", side_effect=OSError("synthetic")),
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

            original_write = make_release_zip._write_archive

            def tampered_write(path, payload):
                original_write(path, payload)
                with zipfile.ZipFile(path) as archive:
                    members = {name: archive.read(name) for name in archive.namelist()}
                target = next(name for name in members if name != make_release_zip.MANIFEST_NAME)
                members[target] = b"tampered-after-write"
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, data in members.items():
                        archive.writestr(name, data)

            output.write_bytes(original)
            with mock.patch.object(make_release_zip, "_write_archive", side_effect=tampered_write), self.assertRaisesRegex(
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

if __name__ == "__main__":
    unittest.main()
