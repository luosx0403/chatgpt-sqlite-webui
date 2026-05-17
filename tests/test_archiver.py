from __future__ import annotations

import json
import argparse
import contextlib
import hashlib
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from chatgpt_export_archiver.cli import build_parser, main
from chatgpt_export_archiver.db import connect, init_db, verify_database, drop_optional_web_indexes, _drop_table_with_shadows, _integrity_failure_is_web_index_only, _run_integrity_check, _line_names_web_index_table, _insert_fts_batch, _delete_fts_for_conversation
from chatgpt_export_archiver.logging_utils import configure_logging, get_logger, parse_log_level
from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager
from chatgpt_export_archiver.parser import _to_int_bool, parse_conversation, validate_conversation_element
from chatgpt_export_archiver.scanner import list_source_entries, resolve_input
from chatgpt_export_archiver.search import parse_query
from chatgpt_export_archiver.utils import parse_date_boundary
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
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(args)
    return code, buffer.getvalue()


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
        self.assertEqual(parse_date_boundary("1970-01-02", end_of_day=True), 172799)
        self.assertEqual(parse_query("", after="1970-01-02").after, 86400)
        self.assertEqual(parse_query("", before="1970-01-02").before, 172799)

    def test_string_boolean_metadata_parses_false_values(self):
        self.assertEqual(_to_int_bool(True), 1)
        self.assertEqual(_to_int_bool(False), 0)
        self.assertEqual(_to_int_bool("true"), 1)
        self.assertEqual(_to_int_bool("false"), 0)
        self.assertEqual(_to_int_bool("1"), 1)
        self.assertEqual(_to_int_bool("0"), 0)
        self.assertIsNone(_to_int_bool(""))

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

    def test_delivery_clean_zip_strips_single_root_and_rejects_git(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ok_zip = base / "ok.zip"
            bad_zip = base / "bad.zip"
            with zipfile.ZipFile(ok_zip, "w") as zf:
                zf.writestr("v17/webui/dist/index.html", "")
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("package/.git/config", "")
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(ok_zip)]):
                self.assertEqual(delivery_clean_main(), 0)
            with mock.patch.object(sys, "argv", ["check_delivery_clean.py", "--mode", "runnable", str(bad_zip)]):
                self.assertEqual(delivery_clean_main(), 1)

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
        for path in readmes:
            text = path.read_text(encoding="utf-8")
            command_blocks.append(re.findall(r"```bash\n(.*?)\n```", text, flags=re.S))
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
            self.assertIn("ERROR:", result.stdout)

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
            self.assertIn("out directory", export_output)
            self.assertNotIn(str(out), export_output)
            self.assertNotIn(out.name, export_output)

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

            def wrapped_page_rows(conn, parsed, conversation_id, limit, offset, order, *, use_trigram=True):
                calls.append((limit, offset, order))
                return original_page_rows(conn, parsed, conversation_id, limit, offset, order, use_trigram=use_trigram)

            with mock.patch.object(search_module, "_message_search_page_rows", wrapped_page_rows):
                code, output = run_cli(["--db", str(db), "search", "common", "--limit", "5"])
            self.assertEqual(code, 0, output)
            self.assertTrue(calls)
            self.assertTrue(all(limit == 5 and offset == 0 and order == "relevance" for limit, offset, order in calls))

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
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "init"]), 0)
            conn = sqlite3.connect(db)
            try:
                # Simulate orphaned shadow tables left after a corrupt DROP
                conn.execute("CREATE TABLE web_message_trigram_content(x)")
                conn.execute("CREATE TABLE web_message_trigram_data(x)")
                conn.execute("CREATE TABLE web_message_trigram_idx(x)")
                conn.execute("CREATE TABLE web_message_trigram_config(x)")
                conn.execute("CREATE TABLE web_message_trigram_docsize(x)")
                conn.commit()
            finally:
                conn.close()

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


if __name__ == "__main__":
    unittest.main()
