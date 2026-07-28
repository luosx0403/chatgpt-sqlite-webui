from __future__ import annotations

import importlib
import asyncio
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import tracemalloc
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import quote

try:
    from fastapi.testclient import TestClient
    from chatgpt_export_archiver.web_app import create_app
except ImportError:  # pragma: no cover
    TestClient = None
    create_app = None

from chatgpt_export_archiver.cli import main
from chatgpt_export_archiver.db import migrate_database
from chatgpt_export_archiver.web_db import connect_readonly
from chatgpt_export_archiver.search import _message_display_fields, _message_visibility_counts, _message_visibility_counts_for_path, _is_internal_message


def node(node_id, parent, role, text, ts, children=None):
    return {
        "id": node_id,
        "parent": parent,
        "children": children or [],
        "message": {
            "id": f"msg-{node_id}",
            "author": {"role": role},
            "create_time": ts,
            "update_time": ts,
            "content": {"content_type": "text", "parts": [text]},
            "metadata": {"private_note": "must stay out of api"},
        },
    }


def custom_content_node(node_id, parent, role, content, ts, children=None):
    return {
        "id": node_id,
        "parent": parent,
        "children": children or [],
        "message": {
            "id": f"msg-{node_id}",
            "author": {"role": role},
            "create_time": ts,
            "update_time": ts,
            "content": content,
            "metadata": {"private_note": "must stay out of api"},
        },
    }


def root(children):
    return {"id": "root", "parent": None, "children": children, "message": None}


def empty_mapping_node(node_id, parent, children=None):
    return {"id": node_id, "parent": parent, "children": children or [], "message": None}


def conv(cid, title, mapping, current_node, ts):
    return {
        "id": cid,
        "conversation_id": f"exported-{cid}",
        "title": title,
        "create_time": ts,
        "update_time": ts + 100,
        "current_node": current_node,
        "mapping": mapping,
    }


def write_zip(path: Path, conversations):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps(conversations, ensure_ascii=False))


def write_zip_members(path: Path, members: dict[str, object], *, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, value in members.items():
            payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            zf.writestr(name, payload)


def js_slice(text: str, start: int, end: int) -> str:
    data = text.encode("utf-16-le")
    return data[start * 2 : end * 2].decode("utf-16-le")


def refresh_test_database_compatibility(db: Path) -> None:
    """Publish compatibility after a fixture intentionally writes SQLite directly."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        migrate_database(conn, refresh_compatibility=True)
    finally:
        conn.close()


@unittest.skipIf(TestClient is None, "fastapi test client is not installed")
class WebApiTests(unittest.TestCase):
    def make_build_dir(self, base: Path) -> Path:
        build = base / "dist"
        build.mkdir(exist_ok=True)
        (build / "index.html").write_text("<!doctype html><html><body><div id=\"root\"></div></body></html>", encoding="utf-8")
        return build

    def make_client(self):
        td = tempfile.TemporaryDirectory()
        base = Path(td.name)
        z = base / "export.zip"
        mapping1 = {
            "root": root(["u1"]),
            "u1": node("u1", "root", "user", "Run python -m unittest discover and inspect conversations-000.json with --no-input-sha256", 1_700_000_001, ["a1", "b1"]),
            "a1": node("a1", "u1", "assistant", "Use SQLite FTS5 MATCH plus exact phrase fallback for 中文子串搜索.", 1_700_000_002, ["t1"]),
            "t1": node("t1", "a1", "tool", "sqlite3.OperationalError should not leak internal payload. C++ C# gpt-5.5 Python 3.13", 1_700_000_003),
            "b1": node("b1", "u1", "assistant", "This branch mentions pandas and should be excluded.", 1_700_000_004),
        }
        mapping2 = {
            "root": root(["u2"]),
            "u2": node("u2", "root", "user", "盈亏平衡点 is only synthetic test text.", 1_710_000_001, ["a2"]),
            "a2": node("a2", "u2", "assistant", "React Vite TypeScript local web UI.", 1_710_000_002),
        }
        mapping3 = {
            "root": root(["sys"]),
            "sys": custom_content_node("sys", "root", "system", {"content_type": "text", "text": "system readable fallback"}, 1_720_000_001, ["dev"]),
            "dev": node("dev", "sys", "developer", "developer synthetic instruction", 1_720_000_001, ["ctx"]),
            "ctx": custom_content_node(
                "ctx",
                "dev",
                "user",
                {"content_type": "user_editable_context", "user_profile": "profile text", "user_instructions": {"text": "instruction text"}},
                1_720_000_002,
                ["a3"],
            ),
            "a3": node("a3", "ctx", "assistant", "assistant visible answer", 1_720_000_003),
        }
        write_zip(
            z,
            [
                conv("web-1", "Python SQLite Archive", mapping1, "t1", 1_700_000_000),
                conv("web-2", "中文搜索标题", mapping2, "a2", 1_710_000_000),
                conv("web-3", "Raw Fallback", mapping3, "a3", 1_720_000_000),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        self.addCleanup(client.close)
        self.addCleanup(client.close)
        return td, client, db

    def wait_job(self, client, job_id: str, timeout: float = 20.0):
        deadline = time.time() + timeout
        latest = None
        while time.time() < deadline:
            latest = client.get(f"/api/import/jobs/{job_id}").json()
            if latest["status"] in {"succeeded", "failed", "postcheck_failed"}:
                return latest
            time.sleep(0.05)
        self.fail(f"job did not finish: {latest}")

    def test_health_stats_and_lists(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        health = client.get("/api/health").json()
        self.assertTrue(health["ok"])
        self.assertEqual(health["integrity_mode"], "quick")
        self.assertFalse(health["foreign_key_check_complete"])
        self.assertFalse(health["foreign_key_violations_exact"])
        deep = client.get("/api/health?deep=true").json()
        self.assertEqual(deep["integrity_mode"], "deep")
        self.assertTrue(deep["foreign_key_check_complete"])
        self.assertTrue(deep["foreign_key_violations_exact"])
        self.assertEqual(health["foreign_key_violation_sample_limit"], 20)
        self.assertEqual(health["database"]["name"], "database")
        self.assertNotIn(db.name, json.dumps(health))
        self.assertEqual(client.get("/api/stats").json()["conversations"], 3)
        page = client.get("/api/conversations?limit=1").json()
        self.assertEqual(page["limit"], 1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 1)

    def test_sqlite_runtime_failures_use_safe_structured_http_errors(self):
        td, original_client, db = self.make_client()
        self.addCleanup(td.cleanup)
        original_client.close()
        app = create_app(db)
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        private = "/private/synthetic/archive.db"
        for message, code, status in (
            (f"database disk image is malformed {private}", "database_malformed", 500),
            (f"database is locked {private}", "database_locked", 503),
            (f"attempt to write a readonly database {private}", "database_readonly", 503),
            (f"disk I/O error {private}", "database_io_error", 503),
            (f"near secret: syntax error {private}", "database_runtime_failure", 500),
        ):
            with self.subTest(code=code), mock.patch(
                "chatgpt_export_archiver.web_api.list_conversations",
                side_effect=sqlite3.OperationalError(message),
            ):
                response = client.get("/api/conversations")
            self.assertEqual(response.status_code, status)
            self.assertEqual(
                response.json(),
                {"detail": {"code": code, "error_type": "OperationalError"}},
            )
            self.assertNotIn(private, response.text)
            self.assertNotIn("secret", response.text)
            self.assertIn(code, client.get("/api/schema").json()["stable_error_codes"])

    def test_web_starts_without_database_and_serves_empty_contract(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        db = base / "missing.db"
        client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
        health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        health_json = health.json()
        self.assertFalse(health_json["db_ready"])
        self.assertEqual(health_json["readiness"], "database_missing_or_uninitialized")
        self.assertEqual(health_json["database_error_code"], "database_not_ready")
        self.assertEqual(health_json["database"]["name"], "database")
        self.assertNotIn(db.name, json.dumps(health_json))
        stats = client.get("/api/stats").json()
        self.assertFalse(stats["db_ready"])
        self.assertEqual(stats["conversations"], 0)
        page = client.get("/api/conversations?limit=5").json()
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 0)
        html = client.get("/").text
        self.assertNotIn("Fallback UI", html)

    def test_health_reports_incompatible_schema_columns_without_500(self):
        from chatgpt_export_archiver.db import check_core_schema, connect

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        db = base / "old.db"
        conn = connect(db)
        try:
            conn.executescript(
                """
                CREATE TABLE import_runs(id INTEGER PRIMARY KEY, input_path TEXT, input_kind TEXT, started_at TEXT, status TEXT);
                CREATE TABLE source_files(id INTEGER PRIMARY KEY, import_run_id INTEGER, source_path TEXT, file_type TEXT);
                CREATE TABLE import_warnings(id INTEGER PRIMARY KEY, import_run_id INTEGER, source_file TEXT, warning_type TEXT, created_at TEXT);
                CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY, title TEXT, create_time REAL, update_time REAL);
                CREATE TABLE conversation_nodes(conversation_id TEXT, node_id TEXT, content_text TEXT);
                CREATE VIRTUAL TABLE message_fts USING fts5(conversation_id UNINDEXED, node_id UNINDEXED, role UNINDEXED, content_text);
                CREATE TABLE exports(id INTEGER PRIMARY KEY, conversation_id TEXT, format TEXT, output_path TEXT, output_hash TEXT, exported_at TEXT);
                CREATE TABLE file_index(id INTEGER PRIMARY KEY, import_run_id INTEGER, source_path TEXT, file_type TEXT);
                """
            )
            conn.commit()
            schema = check_core_schema(conn)
        finally:
            conn.close()
        self.assertFalse(schema["schema_ok"])
        self.assertIn("current_node", schema["missing_columns"]["conversations"])
        self.assertIn("is_on_current_path", schema["missing_columns"]["conversation_nodes"])
        self.assertIn("raw_message_json", schema["missing_columns"]["conversation_nodes"])

        client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
        health = client.get("/api/health").json()
        self.assertEqual(health["database"]["name"], "database")
        self.assertFalse(health["db_ready"])
        self.assertFalse(health["schema_compatible"])
        self.assertEqual(health["readiness"], "schema_incompatible")
        self.assertEqual(health["database_error_code"], "database_schema_incompatible")
        self.assertIn("conversations", health["missing_columns"])
        self.assertEqual(client.get("/api/stats").status_code, 409)
        self.assertEqual(client.get("/api/conversations").status_code, 409)
        for path in [
            "/api/conversations/missing/messages",
            "/api/search/messages?q=python",
            "/api/search/suggest?q=python",
        ]:
            response = client.get(path)
            self.assertEqual(response.status_code, 409)
            detail = response.json()["detail"]
            self.assertFalse(detail["schema_compatible"])
            self.assertIn("missing_columns", detail)

    def test_health_readiness_schema_newer_foreign_key_and_malformed_contracts(self):
        from chatgpt_export_archiver.db import connect, init_db

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)

            newer = base / "newer.db"
            conn = connect(newer)
            init_db(conn)
            conn.execute("PRAGMA user_version = 999")
            conn.commit()
            conn.close()
            build = self.make_build_dir(base)
            newer_client = TestClient(create_app(newer, static_dir=build))
            self.addCleanup(newer_client.close)
            health = newer_client.get("/api/health").json()
            self.assertEqual(health["readiness"], "schema_newer")
            self.assertEqual(health["database_error_code"], "database_schema_newer")
            response = newer_client.get("/api/conversations")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "database_schema_newer")

            damaged = base / "foreign.db"
            conn = connect(damaged)
            init_db(conn)
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO source_files(import_run_id, source_path, file_type) VALUES (999, 'synthetic', 'json')"
            )
            conn.commit()
            conn.close()
            damaged_client = TestClient(create_app(damaged, static_dir=build))
            self.addCleanup(damaged_client.close)
            health = damaged_client.get("/api/health?deep=true").json()
            self.assertEqual(health["readiness"], "foreign_key_violation")
            self.assertEqual(health["database_error_code"], "database_foreign_key_violation")
            self.assertGreater(health["foreign_key_violations"], 0)
            self.assertTrue(health["foreign_key_check_complete"])
            self.assertTrue(health["foreign_key_violations_exact"])
            sample = health["foreign_key_violation_samples"][0]
            self.assertEqual(set(sample), {"table", "rowid", "parent_table", "constraint_index"})
            response = damaged_client.get("/api/stats")
            self.assertEqual(response.status_code, 200)

            malformed = base / "malformed.db"
            malformed.write_bytes(b"synthetic-not-a-sqlite-database")
            malformed_client = TestClient(create_app(malformed, static_dir=build))
            self.addCleanup(malformed_client.close)
            response = malformed_client.get("/api/health")
            self.assertEqual(response.status_code, 200)
            health = response.json()
            self.assertEqual(health["readiness"], "database_malformed")
            self.assertEqual(health["database_error_code"], "database_malformed")
            self.assertNotIn(str(malformed), response.text)

    def test_web_upload_import_first_and_incremental(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        db = base / "archive.db"
        first_zip = base / "first.zip"
        second_zip = base / "second.zip"
        changed_old = {"root": root(["u"]), "u": node("u", "root", "user", "old synthetic body", 1_701_200_001)}
        changed_new = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "new synthetic body", 1_701_200_001, ["a"]),
            "a": node("a", "u", "assistant", "new synthetic answer", 1_701_200_002),
        }
        write_zip(first_zip, [conv("web-upload-keep", "Keep", {"root": root(["u"]), "u": node("u", "root", "user", "stable synthetic", 1_701_201_000)}, "u", 1_701_201_000), conv("web-upload-change", "Change", changed_old, "u", 1_701_200_000)])
        write_zip(second_zip, [conv("web-upload-keep", "Keep", {"root": root(["u"]), "u": node("u", "root", "user", "stable synthetic", 1_701_201_000)}, "u", 1_701_201_000), conv("web-upload-change", "Change", changed_new, "a", 1_701_200_000), conv("web-upload-new", "New", {"root": root(["u"]), "u": node("u", "root", "user", "brand new synthetic", 1_701_202_000)}, "u", 1_701_202_000)])
        client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
        with first_zip.open("rb") as handle:
            response = client.post("/api/import/upload", files={"file": ("first.zip", handle, "application/zip")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["filename"], "first.zip")
        first_job = self.wait_job(client, response.json()["job_id"])
        self.assertEqual(first_job["status"], "succeeded")
        self.assertEqual(first_job["filename"], "first.zip")
        self.assertTrue(db.exists())
        self.assertTrue(first_job["verify"]["ok"])
        self.assertIn("indexed_messages", first_job["web_index"])
        self.assertEqual(
            set(first_job["stage_timings"]),
            {"upload", "import", "verify", "stats", "web_index"},
        )
        self.assertTrue(all(value >= 0 for value in first_job["stage_timings"].values()))
        self.assertEqual(client.get("/api/conversations?limit=10").json()["total"], 2)

        with second_zip.open("rb") as handle:
            response = client.post("/api/import/upload", files={"file": ("second.zip", handle, "application/zip")})
        second_job = self.wait_job(client, response.json()["job_id"])
        self.assertEqual(second_job["status"], "succeeded")
        self.assertEqual(client.get("/api/conversations?limit=10").json()["total"], 3)
        counts_before = client.get("/api/stats").json()
        with second_zip.open("rb") as handle:
            response = client.post("/api/import/upload", files={"file": ("repeat.zip", handle, "application/zip")})
        repeat_job = self.wait_job(client, response.json()["job_id"])
        self.assertEqual(repeat_job["status"], "succeeded")
        self.assertEqual(client.get("/api/stats").json()["nodes"], counts_before["nodes"])

    def test_web_upload_rejects_non_zip_and_protects_concurrent_imports(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        db = base / "archive.db"
        z = base / "slow.zip"
        write_zip(z, [conv("slow-import", "Slow", {"root": root(["u"]), "u": node("u", "root", "user", "synthetic", 1_701_300_000)}, "u", 1_701_300_000)])
        client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
        self.assertEqual(client.post("/api/import/upload", files={"file": ("not.txt", b"not zip", "text/plain")}).status_code, 400)

        from chatgpt_export_archiver import web_jobs
        real_run = web_jobs.run_import_pipeline
        started = threading.Event()
        release = threading.Event()

        def slow_run(*args, **kwargs):
            started.set()
            release.wait(5)
            return real_run(*args, **kwargs)

        with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", side_effect=slow_run):
            with z.open("rb") as handle:
                first = client.post("/api/import/upload", files={"file": ("slow.zip", handle, "application/zip")})
            self.assertEqual(first.status_code, 200)
            self.assertTrue(started.wait(5))
            with z.open("rb") as handle:
                second = client.post("/api/import/upload", files={"file": ("evil/../slow.zip", handle, "application/zip")})
            self.assertEqual(second.status_code, 409)
            release.set()
            self.assertEqual(self.wait_job(client, first.json()["job_id"])["status"], "succeeded")

    def test_web_upload_size_limit_cleans_temp_copy(self):
        from chatgpt_export_archiver.web_api import UploadPolicy

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        upload_dir = base / "upload-tmp"
        upload_dir.mkdir()
        small_policy = UploadPolicy(
            max_upload_bytes=1,
            max_json_member_bytes=1,
            max_json_members=1,
            max_total_uncompressed_bytes=1,
            max_compression_ratio=2.0,
            max_total_members=100000,
            remote=False,
        )
        with mock.patch("chatgpt_export_archiver.web_api._get_upload_policy", return_value=small_policy), \
             mock.patch("chatgpt_export_archiver.web_api.MAX_UPLOAD_BYTES", 1), \
             mock.patch("chatgpt_export_archiver.web_api.make_upload_path", return_value=(upload_dir, upload_dir / "upload.zip")):
            client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
            response = client.post("/api/import/upload", files={"file": ("synthetic.zip", b"1234", "application/zip")})
        self.assertEqual(response.status_code, 413)
        self.assertIn("upload_too_large", response.text)
        self.assertFalse(upload_dir.exists())

    def test_web_preflight_cleanup_failure_is_structured_without_masking_primary_error(self):
        from chatgpt_export_archiver import web_api
        from chatgpt_export_archiver.web_jobs import ImportJobStartError

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
        self.addCleanup(client.close)
        cleanup_failure = {
            "ok": False,
            "error_type": "PermissionError",
            "path_still_exists": True,
            "partial_cleanup": False,
        }

        with mock.patch.object(web_api, "cleanup_upload_dir", return_value=cleanup_failure):
            invalid_zip = client.post(
                "/api/import/upload",
                files={"file": ("invalid.zip", b"not a zip", "application/zip")},
            )
        self.assertEqual(invalid_zip.status_code, 400)
        self.assertEqual(invalid_zip.json()["detail"], {
            "code": "uploaded_file_invalid_zip",
            "cleanup_warning": "temporary_upload_cleanup_failed",
            "cleanup_error_type": "PermissionError",
            "cleanup_warnings": [{
                "code": "temporary_upload_cleanup_failed",
                "error_type": "PermissionError",
                "path_kind": "upload_directory",
            }],
        })
        self.assertNotIn(str(base), invalid_zip.text)

        no_source = base / "no-source.zip"
        write_zip_members(no_source, {"README.txt": b"synthetic"})
        with mock.patch.object(web_api, "cleanup_upload_dir", return_value=cleanup_failure):
            response = client.post(
                "/api/import/upload",
                files={"file": ("no-source.zip", no_source.read_bytes(), "application/zip")},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "upload_zip_no_conversation_sources")
        self.assertEqual(response.json()["detail"]["cleanup_warning"], "temporary_upload_cleanup_failed")

        valid = base / "valid.zip"
        write_zip(valid, [conv("start-failure", "Synthetic", {"root": root([])}, "root", 1_700_000_000)])
        with mock.patch.object(web_api.ImportJobManager, "start_import", side_effect=ImportJobStartError("RuntimeError")), \
             mock.patch.object(web_api, "cleanup_upload_dir", return_value=cleanup_failure):
            response = client.post(
                "/api/import/upload",
                files={"file": ("valid.zip", valid.read_bytes(), "application/zip")},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "import_job_start_failed")
        self.assertEqual(response.json()["detail"]["error_type"], "RuntimeError")
        self.assertEqual(response.json()["detail"]["cleanup_warning"], "temporary_upload_cleanup_failed")

    def test_web_upload_rejects_zip_bomb_like_conversation_member(self):
        from chatgpt_export_archiver import web_api
        from chatgpt_export_archiver.web_api import UploadPolicy

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "small.zip"
        write_zip(z, [conv("small", "Small", {"root": root(["u"]), "u": node("u", "root", "user", "synthetic", 1_701_000_001)}, "u", 1_701_000_000)])
        tiny_policy = UploadPolicy(
            max_upload_bytes=20 * 1024 * 1024 * 1024,
            max_json_member_bytes=1,
            max_json_members=5000,
            max_total_uncompressed_bytes=128 * 1024 * 1024 * 1024,
            max_compression_ratio=1000.0,
            max_total_members=100000,
            remote=False,
        )
        with mock.patch.object(web_api, "_get_upload_policy", return_value=tiny_policy):
            client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
            with z.open("rb") as handle:
                response = client.post("/api/import/upload", files={"file": ("small.zip", handle, "application/zip")})
        self.assertEqual(response.status_code, 413)
        self.assertIn("upload_zip_member_too_large", response.text)

    def test_web_upload_zip_member_limits_clean_temp_copy(self):
        from chatgpt_export_archiver import web_api
        from chatgpt_export_archiver.web_api import UploadPolicy

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)

        many = base / "many.zip"
        write_zip_members(many, {
            "conversations-000.json": [],
            "conversations-001.json": [],
        })
        upload_dir = base / "many-upload"
        upload_dir.mkdir()
        few_policy = UploadPolicy(
            max_upload_bytes=20 * 1024 * 1024 * 1024,
            max_json_member_bytes=64 * 1024 * 1024 * 1024,
            max_json_members=1,
            max_total_uncompressed_bytes=1,
            max_compression_ratio=1000.0,
            max_total_members=100000,
            remote=False,
        )
        with mock.patch.object(web_api, "_get_upload_policy", return_value=few_policy), \
             mock.patch("chatgpt_export_archiver.web_api.make_upload_path", return_value=(upload_dir, upload_dir / "upload.zip")):
            client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
            with many.open("rb") as handle:
                response = client.post("/api/import/upload", files={"file": ("many.zip", handle, "application/zip")})
        self.assertEqual(response.status_code, 413)
        self.assertIn("upload_zip_too_many_json_members", response.text)
        self.assertFalse(upload_dir.exists())

        total = base / "total.zip"
        write_zip_members(total, {"conversations.json": [conv("total", "Total", {"root": root(["u"]), "u": node("u", "root", "user", "synthetic", 1)}, "u", 1)]})
        with mock.patch.object(web_api, "_get_upload_policy", return_value=few_policy):
            client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
            with total.open("rb") as handle:
                response = client.post("/api/import/upload", files={"file": ("total.zip", handle, "application/zip")})
        self.assertEqual(response.status_code, 413)
        self.assertIn("upload_zip_uncompressed_too_large", response.text)

    def test_web_upload_compression_ratio_and_invalid_zip_are_diagnostic(self):
        from chatgpt_export_archiver import web_api
        from chatgpt_export_archiver.web_api import UploadPolicy

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        compressed = base / "compressed.zip"
        write_zip_members(compressed, {"conversations.json": b" " * (11 * 1024 * 1024)}, compression=zipfile.ZIP_DEFLATED)
        low_ratio_policy = UploadPolicy(
            max_upload_bytes=20 * 1024 * 1024 * 1024,
            max_json_member_bytes=64 * 1024 * 1024 * 1024,
            max_json_members=5000,
            max_total_uncompressed_bytes=128 * 1024 * 1024 * 1024,
            max_compression_ratio=2.0,
            max_total_members=100000,
            remote=False,
        )
        with mock.patch.object(web_api, "_get_upload_policy", return_value=low_ratio_policy):
            client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
            with compressed.open("rb") as handle:
                response = client.post("/api/import/upload", files={"file": ("compressed.zip", handle, "application/zip")})
        self.assertEqual(response.status_code, 413)
        self.assertIn("upload_zip_compression_ratio_too_high", response.text)

        upload_dir = base / "invalid-upload"
        upload_dir.mkdir()
        with mock.patch("chatgpt_export_archiver.web_api.make_upload_path", return_value=(upload_dir, upload_dir / "upload.zip")):
            response = client.post("/api/import/upload", files={"file": ("bad.zip", b"not actually a zip", "application/zip")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("uploaded_file_invalid_zip", response.text)
        self.assertFalse(upload_dir.exists())

    def test_web_upload_metadata_only_and_duplicate_shard_identity_are_rejected(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
        self.addCleanup(client.close)

        metadata_only = base / "metadata-only.zip"
        with zipfile.ZipFile(metadata_only, "w") as zf:
            zf.writestr("__MACOSX/conversations.json", "[]")
            zf.writestr("._conversations.json", "metadata")
            zf.writestr(".DS_Store", "metadata")
        with metadata_only.open("rb") as handle:
            response = client.post("/api/import/upload", files={"file": ("metadata-only.zip", handle, "application/zip")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "upload_zip_no_conversation_sources")

        duplicate_identity = base / "duplicate-identity.zip"
        with zipfile.ZipFile(duplicate_identity, "w") as zf:
            zf.writestr("conversations-001.json", "[]")
            zf.writestr("nested/conversations-1.json", "[]")
        with duplicate_identity.open("rb") as handle:
            response = client.post("/api/import/upload", files={"file": ("duplicate-identity.zip", handle, "application/zip")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "upload_zip_ambiguous_conversation_sources")

    def test_web_upload_job_error_state_is_diagnostic(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        db = base / "archive.db"
        z = base / "job-error.zip"
        write_zip(z, [conv("job-error", "Job Error", {"root": root(["u"]), "u": node("u", "root", "user", "synthetic", 1)}, "u", 1)])
        client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
        with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", side_effect=RuntimeError("synthetic failure")):
            with z.open("rb") as handle:
                response = client.post("/api/import/upload", files={"file": ("job-error.zip", handle, "application/zip")})
            self.assertEqual(response.status_code, 200)
            job = self.wait_job(client, response.json()["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "import_transaction_failed")
        self.assertEqual(job["error_code"], "import_transaction_failed")
        self.assertEqual(job["outcome"], "import_transaction_failed")
        self.assertFalse(job["canonical_commit_succeeded"])
        self.assertEqual(job["stage"], "transaction")

    def test_max_upload_bytes_env_parsing_is_safe(self):
        from chatgpt_export_archiver import web_api

        default = web_api.DEFAULT_MAX_UPLOAD_BYTES
        env_name = web_api.MAX_UPLOAD_ENV
        self.assertEqual(web_api._get_upload_policy({}).max_upload_bytes, default)
        self.assertEqual(web_api._get_upload_policy({env_name: "12345"}).max_upload_bytes, 12345)
        for value in ("not-a-number", "   "):
            with self.assertLogs("chatgpt_export_archiver.web_api", level="WARNING") as logs:
                self.assertEqual(web_api._get_upload_policy({env_name: value}).max_upload_bytes, default)
            payload = "\n".join(logs.output)
            self.assertIn("invalid_upload_config", payload)

        self.addCleanup(importlib.reload, web_api)
        with mock.patch.dict(os.environ, {env_name: "not-a-number"}):
            with self.assertLogs("chatgpt_export_archiver.web_api", level="WARNING"):
                reloaded = importlib.reload(web_api)
            self.assertEqual(reloaded.MAX_UPLOAD_BYTES, default)

    def test_conversation_detail_and_messages_current_all(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        detail = client.get("/api/conversations/web-1").json()
        self.assertEqual(detail["conversation_id"], "web-1")
        current = client.get("/api/conversations/web-1/messages?path=current").json()
        all_nodes = client.get("/api/conversations/web-1/messages?path=all&include_internal=true").json()
        self.assertLess(current["total"], all_nodes["total"])
        self.assertIn("visible_total", all_nodes)
        self.assertGreaterEqual(all_nodes["empty_hidden_count"], 1)
        self.assertGreaterEqual(all_nodes["internal_hidden_count"], 1)
        root_item = next(item for item in all_nodes["items"] if item["node_id"] == "root")
        self.assertTrue(root_item["is_internal"])
        self.assertTrue(root_item["is_empty_mapping_node"])
        self.assertFalse(root_item["has_text"])
        keys = json.dumps(current)
        self.assertNotIn("raw_message_json", keys)
        self.assertNotIn("private_note", keys)

    def test_message_page_visible_counts_for_internal_and_empty_nodes(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        page = client.get("/api/conversations/web-3/messages?path=current&limit=20&include_internal=true").json()
        self.assertEqual(page["total"], 5)
        self.assertEqual(page["visible_total"], 1)
        self.assertEqual(page["empty_hidden_count"], 1)
        self.assertEqual(page["internal_hidden_count"], 3)
        visible = [item for item in page["items"] if not item["is_empty_mapping_node"] and not item["is_internal"]]
        self.assertEqual([item["node_id"] for item in visible], ["a3"])

    def test_reader_default_paginates_visible_messages_before_internal_prefix(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "visible-page.zip"
        mapping = {"root": root(["sys0"])}
        previous = "root"
        for index in range(8):
            node_id = f"sys{index}"
            next_id = f"sys{index + 1}" if index < 7 else "u"
            mapping[node_id] = node(node_id, previous, "system", f"internal prefix {index}", 1_700_300_000 + index, [next_id])
            previous = node_id
        mapping["u"] = node("u", previous, "user", "visible-after-internal-prefix", 1_700_300_100)
        write_zip(z, [conv("visible-page", "Visible Page", mapping, "u", 1_700_300_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        self.addCleanup(client.close)

        default_page = client.get("/api/conversations/visible-page/messages?path=current&limit=3").json()
        self.assertEqual([item["node_id"] for item in default_page["items"]], ["u"])
        self.assertEqual(default_page["total"], 1)
        self.assertEqual(default_page["visible_total"], 1)
        self.assertGreater(default_page["internal_hidden_count"], 0)

        full_page = client.get("/api/conversations/visible-page/messages?path=current&limit=3&include_internal=true").json()
        self.assertGreater(full_page["total"], default_page["total"])
        self.assertTrue(any(item["is_internal"] for item in full_page["items"]))

    def test_search_suggest_treats_like_wildcards_as_literals(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "suggest.zip"
        db = base / "archive.db"
        write_zip(
            z,
            [
                conv("suggest-percent", "Literal % sign", {"root": root(["u"]), "u": node("u", "root", "user", "one", 1_701_000_001)}, "u", 1_701_000_000),
                conv("suggest-under", "Literal _ underscore", {"root": root(["u"]), "u": node("u", "root", "user", "two", 1_701_000_002)}, "u", 1_701_000_000),
                conv("suggest-plus", "C++ and gpt-5.5", {"root": root(["u"]), "u": node("u", "root", "user", "three", 1_701_000_003)}, "u", 1_701_000_000),
                conv("suggest-other", "Plain title", {"root": root(["u"]), "u": node("u", "root", "user", "four", 1_701_000_004)}, "u", 1_701_000_000),
            ],
        )
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        self.assertEqual({item["conversation_id"] for item in client.get("/api/search/suggest?q=%25").json()["items"]}, {"suggest-percent"})
        self.assertEqual({item["conversation_id"] for item in client.get("/api/search/suggest?q=_").json()["items"]}, {"suggest-under"})
        self.assertEqual({item["conversation_id"] for item in client.get("/api/search/suggest?q=C%2B%2B").json()["items"]}, {"suggest-plus"})
        self.assertEqual({item["conversation_id"] for item in client.get("/api/search/suggest?q=gpt-5.5").json()["items"]}, {"suggest-plus"})

    def test_search_suggest_uses_normalized_title_index_when_available(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "suggest-normalized.zip"
        db = base / "archive.db"
        write_zip(
            z,
            [
                conv("suggest-fullwidth", "Title Ｉｎｔｅｌ", {"root": root(["u"]), "u": node("u", "root", "user", "one", 1_701_000_001)}, "u", 1_701_000_000),
                conv("suggest-ligature", "Title ﬁle", {"root": root(["u"]), "u": node("u", "root", "user", "two", 1_701_000_002)}, "u", 1_701_000_000),
                conv("suggest-combining", "Title cafe\u0301", {"root": root(["u"]), "u": node("u", "root", "user", "three", 1_701_000_003)}, "u", 1_701_000_000),
            ],
        )
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        no_index_client = TestClient(create_app(db))
        self.addCleanup(no_index_client.close)
        self.assertEqual({item["conversation_id"] for item in no_index_client.get("/api/search/suggest?q=Intel").json()["items"]}, {"suggest-fullwidth"})
        self.assertEqual({item["conversation_id"] for item in no_index_client.get("/api/search/suggest?q=fi").json()["items"]}, {"suggest-ligature"})
        self.assertEqual({item["conversation_id"] for item in no_index_client.get("/api/search/suggest?q=caf%C3%A9").json()["items"]}, {"suggest-combining"})
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        client = TestClient(create_app(db))
        self.assertEqual({item["conversation_id"] for item in client.get("/api/search/suggest?q=Intel").json()["items"]}, {"suggest-fullwidth"})
        self.assertEqual({item["conversation_id"] for item in client.get("/api/search/suggest?q=fi").json()["items"]}, {"suggest-ligature"})
        self.assertEqual({item["conversation_id"] for item in client.get("/api/search/suggest?q=caf%C3%A9").json()["items"]}, {"suggest-combining"})

    def test_conversation_search_never_injects_implicit_fuzzy_results_or_resets_offset(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        typo = client.get("/api/conversations?q=Pythn&limit=10").json()
        self.assertEqual(typo["total"], 0)
        self.assertEqual(typo["items"], [])
        exact = client.get("/api/conversations?q=python&limit=1&offset=999").json()
        self.assertGreater(exact["total"], 0)
        self.assertEqual(exact["items"], [])

    def test_messages_use_single_display_text_and_bounded_raw_preview(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        page = client.get("/api/conversations/web-3/messages?path=current&include_internal=true").json()
        by_id = {item["node_id"]: item for item in page["items"]}
        self.assertIn("system readable fallback", by_id["sys"]["display_text"])
        self.assertTrue(by_id["sys"]["is_internal"])
        self.assertTrue(by_id["sys"]["has_raw"])
        self.assertIn("profile text", by_id["ctx"]["display_text"])
        payload = json.dumps(page)
        self.assertIn("raw_preview", payload)
        self.assertNotIn("raw_message_json", payload)
        for item in page["items"]:
            self.assertNotIn("content_text", item)
            self.assertNotIn("render_text", item)
        self.assertNotIn("private_note", payload)

    def test_reader_reuses_one_raw_decode_per_row_for_around_paths(self):
        from chatgpt_export_archiver import search as search_module

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        raw_rows = []
        for index in range(30):
            raw = json.dumps(
                {
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [f"legacy raw {index}"]},
                }
            )
            raw_rows.append((f"legacy-{index}", f"msg-legacy-{index}", 1_800_000_000 + index, raw))
        conn = sqlite3.connect(db)
        try:
            conn.executemany(
                """
                INSERT INTO conversation_nodes(
                    conversation_id, node_id, parent_node_id, children_json,
                    message_id, role, create_time, update_time, content_type,
                    content_text, content_hash, is_on_current_path, raw_message_json
                ) VALUES('web-1', ?, 'root', '[]', ?, 'user', ?, ?, 'text',
                         '[non-text content: legacy]', NULL, 0, ?)
                """,
                [(node_id, message_id, ts, ts, raw) for node_id, message_id, ts, raw in raw_rows],
            )
            conn.commit()
        finally:
            conn.close()

        real_loads = search_module.json_loads
        for include_internal in (True, False):
            for force_sql in (False, True):
                with self.subTest(include_internal=include_internal, force_sql=force_sql):
                    calls = {"count": 0}

                    def counting_loads(value):
                        calls["count"] += 1
                        return real_loads(value)

                    conn = connect_readonly(db)
                    try:
                        with mock.patch.object(search_module, "json_loads", side_effect=counting_loads), mock.patch.object(
                            search_module,
                            "MAX_AROUND_NODE_ROWS",
                            10 if force_sql else 10_000,
                        ):
                            page = search_module.get_messages(
                                conn,
                                "web-1",
                                path="all",
                                limit=100,
                                offset=0,
                                around_node_id="legacy-15",
                                highlight_query="legacy raw",
                                include_internal=include_internal,
                            )
                    finally:
                        conn.close()
                    legacy_items = [item for item in page["items"] if item["node_id"].startswith("legacy-")]
                    self.assertEqual(len(legacy_items), 30)
                    self.assertEqual(calls["count"], 34 if include_internal else 33)

    def test_long_message_response_is_bounded_but_complete_streams_are_full(self):
        from chatgpt_export_archiver import web_api as web_api_module

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        canonical = "c" * 100_000
        recovered = "r" * 100_000
        raw = json.dumps(
            {"author": {"role": "user"}, "content": {"content_type": "text", "parts": [recovered]}},
            separators=(",", ":"),
        )
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE conversation_nodes SET content_text = ?, raw_message_json = NULL WHERE conversation_id = 'web-1' AND node_id = 'u1'",
                (canonical,),
            )
            conn.execute(
                "UPDATE conversation_nodes SET content_text = '[non-text content: legacy]', raw_message_json = ? WHERE conversation_id = 'web-1' AND node_id = 'a1'",
                (raw,),
            )
            conn.commit()
        finally:
            conn.close()
        response = client.get("/api/conversations/web-1/messages?path=all&include_internal=true&limit=20")
        page = response.json()
        by_id = {item["node_id"]: item for item in page["items"]}
        returned = by_id["u1"]["display_text_returned_chars"]
        self.assertLessEqual(returned, 65_536)
        self.assertEqual(by_id["u1"]["display_text"], canonical[:returned])
        self.assertTrue(by_id["u1"]["display_text_truncated"])
        self.assertFalse(by_id["u1"]["display_text_total_chars_exact"])
        self.assertGreater(by_id["u1"]["display_text_total_chars"], returned)
        self.assertFalse(by_id["u1"]["display_text_resolver_input_truncated"])
        self.assertEqual(by_id["u1"]["display_text_returned_chars"], returned)
        recovered_returned = by_id["a1"]["display_text_returned_chars"]
        self.assertEqual(by_id["a1"]["display_text"], recovered[:recovered_returned])
        self.assertTrue(by_id["a1"]["display_text_truncated"])
        self.assertNotIn("content_text", by_id["u1"])
        self.assertNotIn("render_text", by_id["u1"])
        self.assertLess(len(response.content), 260_000)
        complete = client.get(f"/api/conversations/web-1/messages/u1/display?offset={returned}&limit=65536").json()
        self.assertEqual(complete["display_text"], canonical[returned : returned + 65_536])
        self.assertEqual(complete["has_more"], returned + 65_536 < len(canonical))
        recovered_complete = client.get("/api/conversations/web-1/messages/a1/display?offset=0&limit=65536").json()
        self.assertEqual(recovered_complete["display_text"], recovered[:65_536])
        self.assertTrue(recovered_complete["has_more"])
        with mock.patch.object(web_api_module, "get_messages", side_effect=AssertionError("export must not use reader pages")):
            exported = client.get("/api/conversations/web-1/export?format=txt&path=all&include_internal=true")
            copied = client.get("/api/conversations/web-1/copy?path=all&include_internal=true")
        self.assertEqual(exported.status_code, 200)
        self.assertIn(canonical, exported.text)
        self.assertIn(recovered, exported.text)
        self.assertEqual(copied.status_code, 200)
        self.assertIn(canonical, copied.text)
        self.assertIn(recovered, copied.text)

    def test_raw_resolver_decode_counts_scale_once_per_row(self):
        from chatgpt_export_archiver import search as search_module

        template = {
            "message_id": "synthetic",
            "role": "user",
            "content_type": "text",
            "content_text": "[non-text content: legacy]",
        }
        real_loads = search_module.json_loads
        for count in (1, 30, 300):
            rows = []
            for index in range(count):
                rows.append({
                    **template,
                    "node_id": f"n-{index}",
                    "raw_message_json": json.dumps({
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": [f"raw body {index}"]},
                    }),
                })
            calls = {"count": 0}

            def counting_loads(value):
                calls["count"] += 1
                return real_loads(value)

            with self.subTest(count=count), mock.patch.object(search_module, "json_loads", side_effect=counting_loads):
                resolved = [search_module._message_display_fields(row) for row in rows]
            self.assertEqual(calls["count"], count)
            self.assertEqual(len({item["display_text"] for item in resolved}), count)

    def test_raw_resolver_boundaries_do_not_parse_oversized_values(self):
        from chatgpt_export_archiver import search as search_module

        prefix = '{"author":{"role":"user"},"content":{"content_type":"text","parts":["'
        suffix = '"]}}'

        def raw_of_size(size):
            return prefix + ("x" * (size - len(prefix) - len(suffix))) + suffix

        base = {
            "node_id": "boundary",
            "message_id": "synthetic",
            "role": "user",
            "content_type": "text",
            "content_text": "[non-text content: legacy]",
        }
        for size, expected_decodes, recovered in (
            (200_000, 1, True),
            (200_001, 0, False),
            (1_000_000, 0, False),
            (30_000_000, 0, False),
        ):
            raw = raw_of_size(size)
            with self.subTest(size=size), mock.patch.object(search_module, "json_loads", wraps=search_module.json_loads) as loads:
                fields = search_module._message_display_fields({**base, "raw_message_json": raw})
            self.assertEqual(loads.call_count, expected_decodes)
            self.assertEqual(fields["display_text"].startswith("x"), recovered)
            self.assertLessEqual(len(fields["raw_preview"]), 20_100)
            self.assertEqual(fields["raw_preview_truncated"], size > 20_000)

    def test_search_and_web_index_sql_resolve_each_candidate_once(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver import web_db as web_db_module

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        real_search_resolver = search_module.recover_message_display_text
        search_calls = {"count": 0}

        def count_search(content_text, raw_message_json, **kwargs):
            search_calls["count"] += 1
            return real_search_resolver(content_text, raw_message_json, **kwargs)

        conn = connect_readonly(db)
        try:
            with mock.patch.object(search_module, "recover_message_display_text", side_effect=count_search):
                page = search_module.search_messages(
                    conn,
                    search_module.parse_query("python", path_default="all"),
                    conversation_id="web-1",
                    limit=20,
                    count_total=False,
                )
        finally:
            conn.close()
        self.assertTrue(page["items"])
        # Canonical non-placeholder text never opens or decodes raw JSON.  In
        # this fixture only the single legacy-placeholder row needs recovery.
        self.assertEqual(search_calls["count"], 1)

        real_index_resolver = web_db_module.recover_message_display_text
        index_calls = {"count": 0}

        def count_index(content_text, raw_message_json):
            index_calls["count"] += 1
            return real_index_resolver(content_text, raw_message_json)

        conn = sqlite3.connect(db)
        try:
            node_count = conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0]
        finally:
            conn.close()
        with mock.patch.object(web_db_module, "recover_message_display_text", side_effect=count_index):
            web_db_module.create_web_indexes(db)
        # Canonical rows bypass the legacy resolver; only the three synthetic
        # placeholder/empty exception rows need bounded raw recovery, once
        # each.
        self.assertEqual(index_calls["count"], 3)
        self.assertLess(index_calls["count"], node_count)

    def test_plain_reader_bounds_large_raw_blob_in_sql(self):
        from chatgpt_export_archiver.search import get_messages

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE conversation_nodes SET raw_message_json = ? WHERE conversation_id = ? AND node_id = ?",
                (json.dumps({"prefix": "synthetic", "tail": "z" * 500_000}), "web-1", "u1"),
            )
            conn.commit()
        finally:
            conn.close()
        conn = connect_readonly(db)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            page = get_messages(conn, "web-1", path="current", include_internal=True, limit=20, offset=0)
        finally:
            conn.close()
        item = next(item for item in page["items"] if item["node_id"] == "u1")
        self.assertLessEqual(len(item["raw_preview"]), 20_001)
        self.assertTrue(item["raw_preview_truncated"])
        normalized_sql = [re.sub(r"\s+", "", sql).lower() for sql in statements]
        self.assertFalse(any("length(raw_message_json)" in sql for sql in normalized_sql))
        self.assertFalse(any("substr(coalesce(raw_message_json,''),1," in sql for sql in normalized_sql))
        self.assertFalse(any("selectraw_message_json" in sql for sql in normalized_sql))
        self.assertFalse(any("selectcontent_text" in sql for sql in normalized_sql))

    def test_full_raw_endpoint_is_explicit(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        page = client.get("/api/conversations/web-3/messages?path=current&include_internal=true").json()
        payload = json.dumps(page)
        self.assertNotIn("raw_message_json", payload)
        raw = client.get("/api/conversations/web-3/messages/ctx/raw")
        self.assertEqual(raw.status_code, 200)
        body = raw.json()
        self.assertEqual(body["conversation_id"], "web-3")
        self.assertEqual(body["node_id"], "ctx")
        self.assertIsInstance(body["raw_message"], dict)

    def test_basic_exact_chinese_code_title_role_date_exclude_search(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        for url in [
            "/api/conversations?q=SQLite%20FTS5&limit=2",
            "/api/conversations?q=盈亏平衡点&limit=2",
            "/api/conversations?q=C%2B%2B&limit=2",
            "/api/conversations?q=gpt-5.5&limit=2",
            "/api/conversations?q=Python%203.13&limit=2",
            "/api/conversations?q=--no-input-sha256&limit=2",
            '/api/conversations?q="python%20-m%20unittest"&limit=2',
            "/api/conversations?q=pandas%20-pandas&path=all&limit=2",
            "/api/conversations?role=system&limit=2",
            "/api/conversations?role=developer&limit=2",
            "/api/conversations?sort=created&limit=2",
            "/api/conversations?sort=updated&limit=2",
        ]:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(client.get("/api/search?q=SQLite%20FTS5").json()["total"], 1)
        self.assertEqual(client.get('/api/search?q="python%20-m%20unittest"').json()["total"], 1)
        self.assertEqual(client.get("/api/search?q=盈亏平衡点").json()["items"][0]["conversation_id"], "web-2")
        self.assertEqual(client.get("/api/search?q=conversations-000.json").json()["items"][0]["conversation_id"], "web-1")
        self.assertEqual(client.get("/api/search?q=C%2B%2B").json()["items"][0]["conversation_id"], "web-1")
        self.assertEqual(client.get("/api/search?q=gpt-5.5").json()["items"][0]["conversation_id"], "web-1")
        self.assertEqual(client.get("/api/search?q=Python%203.13").json()["items"][0]["conversation_id"], "web-1")
        self.assertEqual(client.get("/api/search?q=--no-input-sha256").json()["items"][0]["conversation_id"], "web-1")
        self.assertEqual(client.get("/api/search?q=%EF%BD%87%EF%BD%90%EF%BD%94%EF%BC%8D%EF%BC%95%EF%BC%8E%EF%BC%95").json()["items"][0]["conversation_id"], "web-1")
        self.assertEqual(client.get("/api/search?q=title:Python").json()["items"][0]["conversation_id"], "web-1")
        self.assertEqual(client.get("/api/conversations?scope=title&title=Python").json()["items"][0]["conversation_id"], "web-1")
        self.assertEqual(client.get("/api/conversations?exact=python%20-m%20unittest").json()["items"][0]["conversation_id"], "web-1")
        role_items = client.get("/api/search/messages?q=python%20role:user").json()["items"]
        self.assertTrue(role_items)
        self.assertTrue(all(item["role"] == "user" for item in role_items))
        role_param_items = client.get("/api/search/messages?q=gpt-5.5&role=tool/system").json()["items"]
        self.assertTrue(role_param_items)
        self.assertTrue(all(item["role"] in {"tool", "system"} for item in role_param_items))
        developer_items = client.get("/api/search/messages?q=developer&role=developer").json()["items"]
        self.assertTrue(developer_items)
        self.assertTrue(all(item["role"] == "developer" for item in developer_items))
        self.assertEqual(client.get("/api/search?q=React%20after:2024-01-01").json()["items"][0]["conversation_id"], "web-2")
        self.assertEqual(client.get("/api/conversations?q=React&after=2024-01-01").json()["items"][0]["conversation_id"], "web-2")
        excluded = client.get("/api/search?q=pandas%20-pandas&path=all").json()
        self.assertEqual(excluded["total"], 0)
        normalized_messages = client.get("/api/conversations/web-1/messages?q=%EF%BD%87%EF%BD%90%EF%BD%94%EF%BC%8D%EF%BC%95%EF%BC%8E%EF%BC%95&path=current&include_internal=true").json()
        by_id = {item["node_id"]: item for item in normalized_messages["items"]}
        self.assertTrue(by_id["t1"]["highlight_ranges"])
        normalized_hits = client.get("/api/search/messages?q=%EF%BD%87%EF%BD%90%EF%BD%94%EF%BC%8D%EF%BC%95%EF%BC%8E%EF%BC%95&conversation_id=web-1").json()
        self.assertTrue(normalized_hits["items"])
        self.assertIn("gpt-5.5", normalized_hits["items"][0]["snippet"])

    def test_exclude_only_scope_only_and_filter_reasons_are_consistent(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)

        excluded = client.get("/api/conversations?q=-SQLite&limit=10").json()
        self.assertEqual({item["conversation_id"] for item in excluded["items"]}, {"web-2", "web-3"})
        excluded_param = client.get("/api/conversations?exclude=SQLite&limit=10").json()
        self.assertEqual({item["conversation_id"] for item in excluded_param["items"]}, {"web-2", "web-3"})
        message_hits = client.get("/api/search/messages?q=-SQLite&limit=100").json()
        self.assertEqual(message_hits["total"], 0)
        message_hits_param = client.get("/api/search/messages?exclude=SQLite&limit=100").json()
        self.assertEqual(message_hits_param["total"], 0)
        reader = client.get("/api/conversations/web-1/messages?q=-SQLite&limit=20").json()
        self.assertFalse(any(item["highlight_ranges"] for item in reader["items"]))

        normal = client.get("/api/conversations?limit=10").json()
        scope_title = client.get("/api/conversations?scope=title&limit=10").json()
        scope_message = client.get("/api/conversations?scope=message&limit=10").json()
        self.assertEqual(scope_title["total"], normal["total"])
        self.assertEqual(scope_message["total"], normal["total"])
        self.assertFalse(any(item.get("title_match") for item in scope_title["items"]))

        source = client.get("/api/conversations?source=CONVERSATIONS.JSON&limit=10").json()
        self.assertEqual(source["total"], 3)
        self.assertIn("source match", source["items"][0]["reasons"])
        self.assertFalse(source["items"][0].get("title_match"))
        role = client.get("/api/conversations?role=developer&limit=10").json()
        self.assertEqual(role["total"], 1)
        self.assertIn("role filter", role["items"][0]["reasons"])
        role_hits = client.get("/api/search/messages?role=developer&limit=10").json()
        self.assertEqual(role_hits["total"], 0)
        source_hits = client.get("/api/search/messages?source=CONVERSATIONS.JSON&limit=10").json()
        self.assertEqual(source_hits["total"], 0)
        date = client.get("/api/conversations?after=2024-01-01&limit=10").json()
        self.assertTrue(date["total"] > 0)
        self.assertIn("date filter", date["items"][0]["reasons"])
        date_hits = client.get("/api/search/messages?after=2024-01-01&limit=10").json()
        self.assertEqual(date_hits["total"], 0)
        title_hits = client.get("/api/search/messages?title=Python&limit=10").json()
        self.assertEqual(title_hits["total"], 0)
        scope_hits = client.get("/api/search/messages?scope=message&limit=10").json()
        self.assertEqual(scope_hits["total"], 0)

    def test_date_filters_use_utc_calendar_days(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "utc.zip"
        db = base / "archive.db"
        late_utc_22 = datetime(2026, 5, 22, 23, 59, 59, 500000, tzinfo=timezone.utc).timestamp()
        early_utc_23 = datetime(2026, 5, 23, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        write_zip(
            z,
            [
                conv("utc-22", "UTC May 22", {"root": root(["u"]), "u": node("u", "root", "user", "utc-boundary-token", late_utc_22)}, "u", late_utc_22),
                conv("utc-23", "UTC May 23", {"root": root(["u"]), "u": node("u", "root", "user", "utc-boundary-token", early_utc_23)}, "u", early_utc_23),
            ],
        )
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        conn = sqlite3.connect(db)
        try:
            conn.execute("UPDATE conversations SET update_time = create_time WHERE conversation_id IN ('utc-22', 'utc-23')")
            conn.commit()
        finally:
            conn.close()
        client = TestClient(create_app(db))

        after = client.get("/api/conversations?q=utc-boundary-token&after=2026-05-23&limit=10").json()
        self.assertEqual([item["conversation_id"] for item in after["items"]], ["utc-23"])
        before = client.get("/api/conversations?q=utc-boundary-token&before=2026-05-22&limit=10").json()
        self.assertEqual([item["conversation_id"] for item in before["items"]], ["utc-22"])
        previous_day = client.get("/api/conversations?q=utc-boundary-token&before=2026-05-21&limit=10").json()
        self.assertEqual(previous_day["total"], 0)
        messages = client.get("/api/search/messages?q=utc-boundary-token&after=2026-05-23&limit=10").json()
        self.assertEqual([item["conversation_id"] for item in messages["items"]], ["utc-23"])
        message_before = client.get("/api/search/messages?q=utc-boundary-token&before=2026-05-22&limit=10").json()
        self.assertEqual([item["conversation_id"] for item in message_before["items"]], ["utc-22"])
        reader = client.get("/api/conversations/utc-22/messages?q=utc-boundary-token&before=2026-05-22&limit=10").json()
        highlighted = [item for item in reader["items"] if item["highlight_ranges"]]
        self.assertEqual([item["node_id"] for item in highlighted], ["u"])
        hidden = client.get("/api/conversations/utc-22/messages?q=utc-boundary-token&before=2026-05-21&limit=10").json()
        self.assertFalse(any(item["highlight_ranges"] for item in hidden["items"]))

    def test_message_hidden_counts_match_payload_visibility_for_raw_fallback_nodes(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        raw_message = {
            "id": "msg-raw-only",
            "author": {"role": "user"},
            "create_time": 1_720_000_004,
            "update_time": 1_720_000_004,
            "content": {"content_type": "text", "parts": ["raw-only readable text"]},
            "metadata": {},
        }
        placeholder_raw = {
            "id": "msg-placeholder",
            "author": {"role": "user"},
            "create_time": 1_720_000_005,
            "update_time": 1_720_000_005,
            "content": {"content_type": "text", "parts": ["placeholder raw readable text"]},
            "metadata": {},
        }
        internal_raw = {
            "id": "msg-internal-raw",
            "author": {"role": "system"},
            "content": {"content_type": "text", "parts": ["internal recoverable sentinel"]},
            "metadata": {},
        }
        huge_raw = json.dumps({
            "id": "msg-huge",
            "content": {"content_type": "text", "parts": ["huge raw sentinel " + ("x" * 200_100)]},
        })
        real_non_text_raw = json.dumps({
            "id": "msg-real-non-text",
            "author": {"role": "user"},
            "content": {"content_type": "image_asset_pointer", "asset_pointer": "synthetic-asset"},
        })
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                """
                INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, children_json, message_id, role, create_time, update_time, content_type, content_text, content_hash, is_on_current_path, raw_message_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("web-3", "raw-only", "root", "[]", None, "user", 1_720_000_004, 1_720_000_004, "text", "", None, 0, json.dumps(raw_message, ensure_ascii=False)),
            )
            conn.execute(
                """
                INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, children_json, message_id, role, create_time, update_time, content_type, content_text, content_hash, is_on_current_path, raw_message_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("web-3", "placeholder-raw", "root", "[]", "msg-placeholder", "user", 1_720_000_005, 1_720_000_005, "text", "[non-text content: image]", None, 0, json.dumps(placeholder_raw, ensure_ascii=False)),
            )
            conn.executemany(
                """
                INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, children_json, message_id, role, create_time, update_time, content_type, content_text, content_hash, is_on_current_path, raw_message_json)
                VALUES('web-3', ?, 'root', '[]', ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                """,
                [
                    ("invalid-raw", "msg-invalid", "user", 1_720_000_006, 1_720_000_006, "legacy", "[non-text content: legacy]", "{invalid raw sentinel"),
                    ("huge-raw", "msg-huge", "user", 1_720_000_007, 1_720_000_007, "legacy", "[non-text content: legacy]", huge_raw),
                    ("real-non-text", "msg-real-non-text", "user", 1_720_000_008, 1_720_000_008, "image_asset_pointer", "[non-text content: image_asset_pointer]", real_non_text_raw),
                    ("internal-raw", "msg-internal-raw", "system", 1_720_000_009, 1_720_000_009, "text", "[non-text content: legacy]", json.dumps(internal_raw)),
                ],
            )
            conn.execute("UPDATE conversations SET current_node = 'placeholder-raw' WHERE conversation_id = 'web-3'")
            conn.commit()
        finally:
            conn.close()
        refresh_test_database_compatibility(db)

        page = client.get("/api/conversations/web-3/messages?path=all&limit=20").json()
        self.assertEqual(page["empty_hidden_count"], 1)
        self.assertEqual(page["internal_hidden_count"], 4)
        self.assertEqual(page["technical_hidden_count"], 4)
        self.assertEqual(page["visible_total"], 6)
        by_node = {item["node_id"]: item for item in page["items"]}
        self.assertEqual(by_node["raw-only"]["display_text"], "raw-only readable text")
        self.assertFalse(by_node["raw-only"]["is_empty_mapping_node"])
        self.assertEqual(by_node["placeholder-raw"]["display_text"], "placeholder raw readable text")
        visible = [item for item in page["items"] if not item["is_empty_mapping_node"] and not item["is_internal"]]
        self.assertEqual(
            {item["node_id"] for item in visible},
            {"a3", "raw-only", "placeholder-raw", "invalid-raw", "huge-raw", "real-non-text"},
        )
        self.assertEqual(by_node["invalid-raw"]["display_text"], "[non-text content: legacy]")
        self.assertEqual(by_node["huge-raw"]["display_text"], "[non-text content: legacy]")
        self.assertEqual(by_node["real-non-text"]["display_text"], "[non-text content: image_asset_pointer]")

        current_raw_only = client.get("/api/search/messages?q=raw-only%20readable&path=current").json()
        self.assertEqual(current_raw_only["items"], [])
        all_raw_only = client.get("/api/search/messages?q=raw-only%20readable&path=all").json()
        self.assertEqual([item["node_id"] for item in all_raw_only["items"]], ["raw-only"])
        for rejected_needle in ("invalid raw sentinel", "synthetic-asset"):
            self.assertEqual(
                client.get(f"/api/search/messages?q={quote(rejected_needle)}&path=all").json()["items"],
                [],
            )
        huge_hit = client.get(
            f"/api/search/messages?q={quote('huge raw sentinel')}&path=all"
        ).json()
        self.assertEqual([item["node_id"] for item in huge_hit["items"]], ["huge-raw"])
        self.assertTrue(huge_hit["total_exact"])
        self.assertTrue(huge_hit["items"][0]["display_preview_truncated"])
        self.assertEqual(huge_hit["items"][0]["match_char_offset"], 0)
        internal_hidden = client.get("/api/conversations/web-3/messages?path=all&include_internal=false&q=internal%20recoverable%20sentinel").json()
        self.assertNotIn("internal-raw", {item["node_id"] for item in internal_hidden["items"]})
        internal_visible = client.get("/api/conversations/web-3/messages?path=all&include_internal=true&q=internal%20recoverable%20sentinel").json()
        internal_item = next(item for item in internal_visible["items"] if item["node_id"] == "internal-raw")
        self.assertEqual(internal_item["display_text"], "internal recoverable sentinel")
        self.assertTrue(internal_item["highlight_ranges"])
        self.assertNotIn(
            "internal recoverable sentinel",
            client.get("/api/conversations/web-3/export?format=txt&path=all&include_internal=false").text,
        )
        self.assertIn(
            "internal recoverable sentinel",
            client.get("/api/conversations/web-3/export?format=txt&path=all&include_internal=true").text,
        )

        message_hits = client.get(
            f"/api/search/messages?q={quote('placeholder raw readable')}&path=all"
        ).json()
        self.assertEqual([item["node_id"] for item in message_hits["items"]], ["placeholder-raw"])
        conversation_hits = client.get(
            f"/api/conversations?q={quote('placeholder raw readable')}&path=all"
        ).json()
        self.assertEqual([item["conversation_id"] for item in conversation_hits["items"]], ["web-3"])
        highlighted = client.get(
            f"/api/conversations/web-3/messages?path=all&q={quote('placeholder raw readable')}&limit=20"
        ).json()
        target = next(item for item in highlighted["items"] if item["node_id"] == "placeholder-raw")
        self.assertTrue(target["highlight_ranges"])
        web_export = client.get("/api/conversations/web-3/export?format=txt&path=current")
        self.assertIn("placeholder raw readable text", web_export.text)

        from chatgpt_export_archiver.db import connect
        from chatgpt_export_archiver.exporter import export_conversations

        output = Path(td.name) / "legacy-export"
        export_conn = connect(db)
        try:
            export_conversations(export_conn, output, ["md", "txt"])
        finally:
            export_conn.close()
        manifest = [json.loads(line) for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
        web3_paths = [output / row["output_path"] for row in manifest if row["conversation_id"] == "web-3"]
        self.assertEqual(len(web3_paths), 2)
        for exported_path in web3_paths:
            self.assertIn("placeholder raw readable text", exported_path.read_text(encoding="utf-8"))

    def test_web_export_respects_reader_internal_visibility(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        visible = client.get("/api/conversations/web-1/export?format=txt&path=all&include_internal=false")
        self.assertEqual(visible.status_code, 200)
        self.assertIn("Run python -m unittest", visible.text)
        self.assertNotIn("sqlite3.OperationalError", visible.text)
        self.assertNotIn("root", visible.text.lower())

        full = client.get("/api/conversations/web-1/export?format=txt&path=all&include_internal=true")
        self.assertEqual(full.status_code, 200)
        self.assertIn("sqlite3.OperationalError should not leak internal payload", full.text)

        current = client.get("/api/conversations/web-1/export?format=txt&path=current&include_internal=false")
        all_nodes = client.get("/api/conversations/web-1/export?format=txt&path=all&include_internal=false")
        self.assertNotIn("This branch mentions pandas", current.text)
        self.assertIn("This branch mentions pandas", all_nodes.text)

        for path in ("current", "all"):
            for include_internal in (False, True):
                for fmt in ("md", "txt"):
                    with self.subTest(path=path, include_internal=include_internal, format=fmt):
                        output = Path(td.name) / f"cli-{path}-{include_internal}-{fmt}"
                        args = [
                            "--db", str(db), "export", "--out", str(output),
                            "--format", fmt, "--path", path,
                        ]
                        if include_internal:
                            args.append("--include-internal")
                        self.assertEqual(main(args), 0)
                        manifest = [
                            json.loads(line)
                            for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
                        ]
                        row = next(item for item in manifest if item["conversation_id"] == "web-1")
                        self.assertEqual(row["path"], path)
                        self.assertEqual(row["include_internal"], include_internal)
                        cli_text = (output / row["output_path"]).read_text(encoding="utf-8")
                        web = client.get(
                            f"/api/conversations/web-1/export?format={fmt}&path={path}&include_internal={str(include_internal).lower()}"
                        )
                        self.assertEqual(web.status_code, 200)
                        self.assertEqual(cli_text, web.text)

        default_output = Path(td.name) / "cli-default-visible"
        self.assertEqual(main(["--db", str(db), "export", "--out", str(default_output), "--format", "txt"]), 0)
        default_manifest = [json.loads(line) for line in (default_output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
        default_row = next(item for item in default_manifest if item["conversation_id"] == "web-1")
        default_text = (default_output / default_row["output_path"]).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3.OperationalError should not leak internal payload", default_text)

        copy_visible = client.get("/api/conversations/web-1/copy?path=all&include_internal=false")
        copy_internal = client.get("/api/conversations/web-1/copy?path=all&include_internal=true")
        self.assertNotIn("sqlite3.OperationalError should not leak internal payload", copy_visible.text)
        self.assertIn("sqlite3.OperationalError should not leak internal payload", copy_internal.text)

    def test_advanced_exclude_supports_quoted_phrase_and_source_normalization(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)

        broad = client.get(f"/api/conversations?q=python&exclude={quote('python-missing SQLite')}&limit=10").json()
        self.assertEqual(broad["total"], 0, "unquoted exclude splits into separate fragments")
        quoted_phrase = quote('"python-missing SQLite"')
        phrase = client.get(f"/api/conversations?q=python&exclude={quoted_phrase}&limit=10").json()
        self.assertEqual(phrase["total"], 1, "quoted exclude phrase should not exclude separated terms")
        quoted_negative = client.get('/api/conversations?q=python%20-"python-missing%20SQLite"&limit=10').json()
        self.assertEqual(quoted_negative["total"], 1)
        source_query = client.get(f"/api/conversations?q={quote('source:ＣＯＮＶＥＲＳＡＴＩＯＮＳ.JSON')}&limit=10").json()
        source_param = client.get(f"/api/conversations?source={quote('ＣＯＮＶＥＲＳＡＴＩＯＮＳ.JSON')}&limit=10").json()
        self.assertEqual(source_query["total"], source_param["total"])
        self.assertEqual(source_param["total"], 3)

    def test_contains_and_whole_word_search_modes(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "word.zip"
        mapping1 = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "Intel ships CPUs. Intel(R) appears with punctuation. intel. ends a sentence.", 1_700_300_001, ["a"]),
            "a": node("a", "u", "assistant", "No longer token here.", 1_700_300_002),
        }
        mapping2 = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "Intelligence and IntelliSense should only match contains mode.", 1_700_300_003),
        }
        mapping3 = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "英特尔中文词在全词模式下仍按保守包含匹配。", 1_700_300_004),
        }
        mapping4 = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "SuperIntel ships CPUs. SuperIntel shipsXYZ should not match a whole phrase.", 1_700_300_005),
        }
        mapping5 = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "SuperIntel(R) and Intel(R)XYZ are longer token boundaries.", 1_700_300_006),
        }
        mapping6 = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "gpt-5.5 is standalone.", 1_700_300_007),
        }
        mapping7 = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "mygpt-5.5 and gpt-5.5abc are embedded longer tokens.", 1_700_300_008),
        }
        write_zip(
            z,
            [
                conv("word-intel", "Intel Title", mapping1, "a", 1_700_300_000),
                conv("word-longer", "Intelligence Title", mapping2, "u", 1_700_300_001),
                conv("word-zh", "英特尔 标题", mapping3, "u", 1_700_300_002),
                conv("word-embedded-phrase", "SuperIntel Ships Title", mapping4, "u", 1_700_300_003),
                conv("word-embedded-punct", "SuperIntel(R) Title", mapping5, "u", 1_700_300_004),
                conv("word-version", "gpt-5.5 Title", mapping6, "u", 1_700_300_005),
                conv("word-version-embedded", "mygpt-5.5 Title", mapping7, "u", 1_700_300_006),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))

        contains = client.get("/api/search/messages?q=Intel&match_mode=contains&path=all&order=display&limit=10").json()
        self.assertEqual(
            {item["conversation_id"] for item in contains["items"]},
            {"word-intel", "word-longer", "word-embedded-phrase", "word-embedded-punct"},
        )
        word = client.get("/api/search/messages?q=Intel&match_mode=word&path=all&order=display&limit=10").json()
        word_ids = {item["conversation_id"] for item in word["items"]}
        self.assertIn("word-intel", word_ids)
        self.assertNotIn("word-longer", word_ids)
        self.assertNotIn("word-embedded-phrase", word_ids)
        self.assertNotIn("Intelligence", word["items"][0]["snippet"])
        reader = client.get("/api/conversations/word-intel/messages?q=Intel&match_mode=word&path=all").json()
        self.assertTrue(reader["items"][0]["highlight_ranges"])
        embedded_reader = client.get("/api/conversations/word-longer/messages?q=Intel&match_mode=word&path=all").json()
        self.assertFalse(embedded_reader["items"][0]["highlight_ranges"])

        title_contains = client.get("/api/conversations?q=Intel&scope=title&match_mode=contains&sort=title").json()
        self.assertEqual(
            {item["conversation_id"] for item in title_contains["items"]},
            {"word-intel", "word-longer", "word-embedded-phrase", "word-embedded-punct"},
        )
        title_word = client.get("/api/conversations?q=Intel&scope=title&match_mode=word&sort=title").json()
        self.assertEqual([item["conversation_id"] for item in title_word["items"]], ["word-intel"])

        excluded = client.get("/api/search/messages?q=Intel%20-Intel&match_mode=word&path=all").json()
        self.assertEqual(excluded["total"], 0)
        phrase = client.get('/api/search/messages?q="Intel%20ships"&match_mode=word&path=all').json()
        self.assertEqual(phrase["total"], 1)
        self.assertEqual(phrase["items"][0]["conversation_id"], "word-intel")
        phrase_exclude = client.get('/api/search/messages?q="Intel%20ships"%20-"SuperIntel%20ships"&match_mode=word&path=all').json()
        self.assertEqual([item["conversation_id"] for item in phrase_exclude["items"]], ["word-intel"])
        punct = client.get("/api/search/messages?q=Intel(R)&match_mode=word&path=all&limit=10").json()
        self.assertEqual([item["conversation_id"] for item in punct["items"]], ["word-intel"])
        version = client.get("/api/search/messages?q=gpt-5.5&match_mode=word&path=all&limit=10").json()
        self.assertEqual([item["conversation_id"] for item in version["items"]], ["word-version"])
        casefold = client.get("/api/search/messages?q=intel&match_mode=word&path=all").json()
        self.assertIn("word-intel", {item["conversation_id"] for item in casefold["items"]})
        self.assertNotIn("word-longer", {item["conversation_id"] for item in casefold["items"]})
        chinese = client.get("/api/search/messages?q=%E8%8B%B1%E7%89%B9%E5%B0%94&match_mode=word&path=all").json()
        self.assertEqual(chinese["total"], 1)
        invalid = client.get("/api/search/messages?q=Intel&match_mode=bad")
        self.assertEqual(invalid.status_code, 400)

    def test_word_mode_multiscript_keeps_index_candidates_and_semantics(self):
        from chatgpt_export_archiver import search as search_module

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "multi-word.zip"
        mapping = {
            "root": root(["n"]),
            "n": node(
                "n",
                "root",
                "user",
                "python Intel Intel. Intel(R) gpt-5.5 英特尔 かなテスト 한글테스트 Ｉｎｔｅｌ ｐｙｔｈｏｎ 英特尔 Intel 🔥 prefix",
                1_700_350_001,
            ),
        }
        embedded = {
            "root": root(["n"]),
            "n": node("n", "root", "assistant", "Intelligence IntelliSense Intellicode SuperIntel ships gpt-5.5abc mygpt-5.5", 1_700_350_002),
        }
        write_zip(
            z,
            [
                conv("multi-visible", "英特尔 かなテスト 한글테스트 Intel gpt-5.5", mapping, "n", 1_700_350_000),
                conv("multi-embedded", "Intelligence embedded", embedded, "n", 1_700_350_001),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        client = TestClient(create_app(db))

        for label, query in [
            ("english common", "python"),
            ("english proper", "Intel"),
            ("punctuation version", "gpt-5.5"),
            ("chinese", "英特尔"),
            ("japanese", "かなテスト"),
            ("korean", "한글테스트"),
            ("fullwidth latin", "Ｉｎｔｅｌ"),
            ("mixed cjk latin", "英特尔 Intel"),
        ]:
            with self.subTest(label=label):
                page = client.get(f"/api/search/messages?q={quote(query)}&match_mode=word&path=all&limit=10").json()
                self.assertIn("multi-visible", {item["conversation_id"] for item in page["items"]})

        self.assertEqual(client.get("/api/search/messages?q=no-such-token&match_mode=word&path=all").json()["total"], 0)
        contains = client.get("/api/search/messages?q=Intel&match_mode=contains&path=all&limit=10").json()
        self.assertIn("multi-embedded", {item["conversation_id"] for item in contains["items"]})
        word = client.get("/api/search/messages?q=Intel&match_mode=word&path=all&limit=10").json()
        self.assertNotIn("multi-embedded", {item["conversation_id"] for item in word["items"]})
        phrase = client.get('/api/search/messages?q="Intel%20ships"&match_mode=word&path=all').json()
        self.assertEqual(phrase["total"], 0)
        version = client.get("/api/search/messages?q=gpt-5.5&match_mode=word&path=all").json()
        self.assertNotIn("multi-embedded", {item["conversation_id"] for item in version["items"]})

        conn = connect_readonly(db)
        try:
            for query in ["英特尔", "かなテスト", "한글테스트"]:
                parsed = search_module.parse_query(query, path_default="all", match_mode="word")
                source_sql, _source_params, _score_expr, _reason = search_module._message_match_source(conn, parsed, use_trigram=True)
                self.assertIn("web_message_trigram", source_sql)
                self.assertNotEqual(source_sql.strip(), "conversation_nodes n")
                title_sql, _title_params = search_module._title_conversation_select(conn, parsed, use_trigram=True)
                self.assertIn("web_title_trigram", title_sql)
        finally:
            conn.close()

    def test_search_visibility_metadata_for_title_internal_and_branch_hits(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "visibility.zip"
        title_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "Synthetic body without the title token.", 1_700_400_001),
        }
        internal_mapping = {
            "root": root(["sys"]),
            "sys": node("sys", "root", "system", "internal-only-token lives in a system message.", 1_700_400_002, ["a"]),
            "a": node("a", "sys", "assistant", "Visible answer without the token.", 1_700_400_003),
        }
        mixed_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "mixed-visible-token appears in visible text.", 1_700_400_004, ["tool"]),
            "tool": node("tool", "u", "tool", "mixed-visible-token also appears in an internal tool message.", 1_700_400_005, ["a"]),
            "a": node("a", "tool", "assistant", "Final visible answer.", 1_700_400_006),
        }
        branch_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "Current path text.", 1_700_400_007, ["a", "b"]),
            "a": node("a", "u", "assistant", "Current path answer.", 1_700_400_008),
            "b": node("b", "u", "assistant", "branch-only-token appears off the current path.", 1_700_400_009),
        }
        write_zip(
            z,
            [
                conv("visibility-title", "title-only-token synthetic title", title_mapping, "u", 1_700_400_000),
                conv("visibility-internal", "Internal visibility", internal_mapping, "a", 1_700_400_001),
                conv("visibility-mixed", "Mixed visibility", mixed_mapping, "a", 1_700_400_002),
                conv("visibility-branch", "Branch visibility", branch_mapping, "a", 1_700_400_003),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))

        title_results = client.get("/api/conversations?q=title-only-token&path=current").json()
        self.assertEqual([item["conversation_id"] for item in title_results["items"]], ["visibility-title"])
        self.assertIn("title match", title_results["items"][0]["reasons"])
        title_messages = client.get("/api/search/messages?q=title-only-token&conversation_id=visibility-title&path=current").json()
        self.assertEqual(title_messages["total"], 0)

        internal_results = client.get("/api/conversations?q=internal-only-token&path=current").json()
        self.assertEqual([item["conversation_id"] for item in internal_results["items"]], ["visibility-internal"])
        self.assertTrue(internal_results["items"][0]["has_internal_hits"])
        self.assertTrue(internal_results["items"][0]["snippets"][0]["is_internal"])
        internal_hits = client.get("/api/search/messages?q=internal-only-token&conversation_id=visibility-internal&path=current").json()
        self.assertEqual(internal_hits["total"], 1)
        self.assertTrue(internal_hits["items"][0]["is_internal"])

        mixed_hits = client.get("/api/search/messages?q=mixed-visible-token&conversation_id=visibility-mixed&path=current&order=display").json()
        self.assertEqual(mixed_hits["total"], 2)
        self.assertEqual([item["is_internal"] for item in mixed_hits["items"]], [False, True])

        current_branch = client.get("/api/search/messages?q=branch-only-token&conversation_id=visibility-branch&path=current").json()
        self.assertEqual(current_branch["total"], 0)
        all_branch = client.get("/api/search/messages?q=branch-only-token&conversation_id=visibility-branch&path=all").json()
        self.assertEqual(all_branch["total"], 1)
        self.assertFalse(all_branch["items"][0]["is_on_current_path"])
        branch_results = client.get("/api/conversations?q=branch-only-token&path=all").json()
        self.assertTrue(branch_results["items"][0]["has_branch_hits"])

    def test_current_path_search_falls_back_when_conversation_has_no_current_nodes(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "current-fallback.zip"
        broken_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "fallback-current-needle is visible when current path is missing.", 1_700_410_001, ["a"]),
            "a": node("a", "u", "assistant", "fallback-current-needle assistant response.", 1_700_410_002),
        }
        branch_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "normal current text.", 1_700_410_003, ["a", "b"]),
            "a": node("a", "u", "assistant", "normal current answer.", 1_700_410_004),
            "b": node("b", "u", "assistant", "offcurrentneedlexyz appears only off current path.", 1_700_410_005),
        }
        write_zip(
            z,
            [
                conv("missing-current", "Missing Current", broken_mapping, "a", 1_700_410_000),
                conv("normal-branch", "Different Title", branch_mapping, "a", 1_700_410_001),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        conn = sqlite3.connect(db)
        try:
            conn.execute("UPDATE conversation_nodes SET is_on_current_path = 0 WHERE conversation_id = 'missing-current'")
            conn.execute("UPDATE conversations SET current_node = NULL WHERE conversation_id = 'missing-current'")
            conn.commit()
            from chatgpt_export_archiver.db import migrate_database
            migrate_database(conn, refresh_compatibility=True)
            conn.commit()
        finally:
            conn.close()
        client = TestClient(create_app(db))
        self.addCleanup(client.close)

        reader = client.get("/api/conversations/missing-current/messages?path=current&limit=10").json()
        self.assertGreaterEqual(reader["total"], 2)
        self.assertTrue(reader["current_path_fallback_to_all"])
        self.assertEqual(reader["effective_path"], "all")
        self.assertIn("u", {item["node_id"] for item in reader["items"]})
        self.assertTrue(any(item["highlight_ranges"] for item in client.get("/api/conversations/missing-current/messages?q=fallback-current-needle&path=current&limit=10").json()["items"]))
        self.assertTrue(all(item["effective_visible_in_current_view"] for item in reader["items"]))
        hits = client.get("/api/search/messages?q=fallback-current-needle&conversation_id=missing-current&path=current").json()
        self.assertEqual(hits["total"], 2)
        self.assertEqual({item["node_id"] for item in hits["items"]}, {"u", "a"})
        self.assertTrue(all(item["current_path_fallback_to_all"] for item in hits["items"]))
        self.assertTrue(all(item["effective_visible_in_current_view"] for item in hits["items"]))
        conversations = client.get("/api/conversations?q=fallback-current-needle&path=current").json()
        fallback_item = next(item for item in conversations["items"] if item["conversation_id"] == "missing-current")
        self.assertEqual(fallback_item["current_path_nodes"], 0)
        self.assertTrue(fallback_item["current_path_fallback_to_all"])
        self.assertFalse(fallback_item["has_branch_hits"])
        self.assertTrue(all(snippet["effective_visible_in_current_view"] for snippet in fallback_item["snippets"]))

        current_branch = client.get("/api/search/messages?q=offcurrentneedlexyz&conversation_id=normal-branch&path=current").json()
        self.assertEqual(current_branch["total"], 0)
        all_branch = client.get("/api/search/messages?q=offcurrentneedlexyz&conversation_id=normal-branch&path=all").json()
        self.assertEqual(all_branch["total"], 1)
        branch_conversations = client.get("/api/conversations?q=offcurrentneedlexyz&path=current&scope=message").json()
        self.assertNotIn("normal-branch", [item["conversation_id"] for item in branch_conversations["items"]])

    def test_valid_current_node_chain_wins_when_raw_flags_are_zero_everywhere(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "zero-flags-valid-current.zip"
        mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "effective-current-user", 1_700_411_001, ["a", "branch"]),
            "a": node("a", "u", "assistant", "effective-current-answer", 1_700_411_002),
            "branch": node("branch", "u", "assistant", "stray-branch-must-not-appear", 1_700_411_003),
        }
        write_zip(z, [conv("zero-flags-valid-current", "Zero Flags", mapping, "a", 1_700_411_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        writer = sqlite3.connect(db)
        try:
            writer.execute("UPDATE conversation_nodes SET is_on_current_path = 0 WHERE conversation_id = ?", ("zero-flags-valid-current",))
            writer.commit()
        finally:
            writer.close()
        refresh_test_database_compatibility(db)

        from chatgpt_export_archiver.db import connect, verify_database
        verify_conn = connect(db)
        try:
            diagnostics = verify_database(verify_conn)["effective_current_diagnostics"]
        finally:
            verify_conn.close()
        self.assertEqual(diagnostics["valid_current_node_zero_flags"], 1)
        self.assertEqual(diagnostics["selected_current_node"], 1)

        client = TestClient(create_app(db))
        self.addCleanup(client.close)
        reader = client.get("/api/conversations/zero-flags-valid-current/messages?path=current&include_internal=true&limit=20").json()
        self.assertFalse(reader["current_path_fallback_to_all"])
        self.assertEqual(reader["effective_path"], "current")
        self.assertEqual({item["node_id"] for item in reader["items"]}, {"root", "u", "a"})
        self.assertTrue(all(item["effective_visible_in_current_view"] for item in reader["items"]))
        self.assertTrue(all(not item["is_on_current_path"] for item in reader["items"]))
        for target in ("branch", "does-not-exist"):
            around = client.get(
                f"/api/conversations/zero-flags-valid-current/messages?path=current&include_internal=true&limit=20&around_node_id={target}"
            ).json()
            self.assertEqual({item["node_id"] for item in around["items"]}, {"root", "u", "a"})
        self.assertEqual(client.get("/api/search/messages?q=stray-branch-must-not-appear&conversation_id=zero-flags-valid-current&path=current").json()["total"], 0)
        self.assertEqual(client.get("/api/conversations?q=stray-branch-must-not-appear&path=current").json()["total"], 0)
        web_export = client.get("/api/conversations/zero-flags-valid-current/export?format=md&path=current&include_internal=true")
        self.assertEqual(web_export.status_code, 200)
        self.assertNotIn("stray-branch-must-not-appear", web_export.text)
        self.assertIn("effective-current-answer", web_export.text)

        out = base / "exports"
        self.assertEqual(main(["--db", str(db), "export", "--out", str(out), "--format", "md"]), 0)
        markdown = next(out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("effective-current-answer", markdown)
        self.assertNotIn("stray-branch-must-not-appear", markdown)

        writer = sqlite3.connect(db)
        try:
            writer.execute(
                "UPDATE conversation_nodes SET is_on_current_path = 1 WHERE conversation_id = ? AND node_id = 'branch'",
                ("zero-flags-valid-current",),
            )
            writer.commit()
        finally:
            writer.close()
        refresh_test_database_compatibility(db)
        stray_flag_reader = client.get("/api/conversations/zero-flags-valid-current/messages?path=current&include_internal=true&limit=20").json()
        self.assertEqual(stray_flag_reader["current_collection_source"], "current_node")
        self.assertEqual({item["node_id"] for item in stray_flag_reader["items"]}, {"root", "u", "a"})

        writer = sqlite3.connect(db)
        try:
            writer.execute("UPDATE conversations SET current_node = 'missing' WHERE conversation_id = ?", ("zero-flags-valid-current",))
            writer.execute("UPDATE conversation_nodes SET is_on_current_path = 0 WHERE conversation_id = ?", ("zero-flags-valid-current",))
            writer.execute(
                "UPDATE conversation_nodes SET is_on_current_path = 1 WHERE conversation_id = ? AND node_id IN ('root', 'u', 'a')",
                ("zero-flags-valid-current",),
            )
            writer.commit()
        finally:
            writer.close()
        refresh_test_database_compatibility(db)
        flag_reader = client.get("/api/conversations/zero-flags-valid-current/messages?path=current&include_internal=true&limit=20").json()
        self.assertFalse(flag_reader["current_node_exists"])
        self.assertEqual(flag_reader["current_collection_source"], "raw_flags")
        self.assertEqual({item["node_id"] for item in flag_reader["items"]}, {"root", "u", "a"})
        self.assertEqual(client.get("/api/search/messages?q=stray-branch-must-not-appear&conversation_id=zero-flags-valid-current&path=current").json()["total"], 0)
        flags_out = base / "flags-exports"
        self.assertEqual(main(["--db", str(db), "export", "--out", str(flags_out), "--format", "txt"]), 0)
        self.assertNotIn("stray-branch-must-not-appear", next(flags_out.glob("*.txt")).read_text(encoding="utf-8"))

    def test_effective_current_cycle_is_finite_across_reader_search_and_export(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "cycle.zip"
        mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "cycle-visible-user", 1_700_412_001, ["a"]),
            "a": node("a", "u", "assistant", "cycle-visible-answer", 1_700_412_002),
        }
        write_zip(z, [conv("cycle-current", "Cycle Current", mapping, "a", 1_700_412_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        writer = sqlite3.connect(db)
        try:
            writer.execute("UPDATE conversation_nodes SET parent_node_id = 'a', is_on_current_path = 0 WHERE conversation_id = 'cycle-current' AND node_id = 'u'")
            writer.execute("UPDATE conversation_nodes SET parent_node_id = 'u', is_on_current_path = 0 WHERE conversation_id = 'cycle-current' AND node_id = 'a'")
            writer.commit()
            from chatgpt_export_archiver.db import migrate_database
            migrate_database(writer, refresh_compatibility=True)
            writer.commit()
        finally:
            writer.close()
        client = TestClient(create_app(db))
        self.addCleanup(client.close)
        reader = client.get("/api/conversations/cycle-current/messages?path=current&include_internal=true&limit=20")
        self.assertEqual(reader.status_code, 200)
        self.assertEqual({item["node_id"] for item in reader.json()["items"]}, {"u", "a"})
        hits = client.get("/api/search/messages?q=cycle-visible&conversation_id=cycle-current&path=current")
        self.assertEqual(hits.status_code, 200)
        self.assertEqual(hits.json()["total"], 2)
        listing = client.get("/api/conversations?q=cycle-visible&path=current")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["total"], 1)
        export = client.get("/api/conversations/cycle-current/export?format=txt&path=current&include_internal=true")
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export.text.count("cycle-visible-user"), 1)
        self.assertEqual(export.text.count("cycle-visible-answer"), 1)

    def test_highlight_ranges_use_utf16_offsets_for_web(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "unicode-highlight.zip"
        chinese_text = "\n".join([
            "[2026/03/13, 21:00:59] - youtoob🔥: warmup",
            "🔥🔥 youtoob🔥: 英特尔有资源",
        ])
        english_text = "prefix 🔥🔥 Intel ships as a standalone token. Fullwidth Ｉｎｔｅｌ and cafe\u0301 marker."
        mapping = {
            "root": root(["sys"]),
            "sys": node("sys", "root", "system", chinese_text, 1_700_500_001, ["u"]),
            "u": node("u", "sys", "user", english_text, 1_700_500_002),
        }
        write_zip(z, [conv("unicode-highlight", "Unicode Highlight", mapping, "u", 1_700_500_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))

        chinese = client.get("/api/conversations/unicode-highlight/messages?q=%E8%8B%B1%E7%89%B9%E5%B0%94&path=current&include_internal=true").json()
        sys_item = next(item for item in chinese["items"] if item["node_id"] == "sys")
        chinese_range = sys_item["highlight_ranges"][0]
        self.assertEqual(js_slice(sys_item["display_text"], chinese_range["start"], chinese_range["end"]), "英特尔")
        english = client.get("/api/conversations/unicode-highlight/messages?q=Intel&match_mode=word&path=current").json()
        user_item = next(item for item in english["items"] if item["node_id"] == "u")
        english_range = user_item["highlight_ranges"][0]
        self.assertEqual(js_slice(user_item["display_text"], english_range["start"], english_range["end"]), "Intel")
        english_hit = client.get("/api/search/messages?q=Intel&match_mode=word&conversation_id=unicode-highlight&path=current").json()
        self.assertIn("Intel", english_hit["items"][0]["snippet"])
        fullwidth = client.get("/api/conversations/unicode-highlight/messages?q=Intel&match_mode=word&path=current").json()
        fullwidth_ranges = next(item for item in fullwidth["items"] if item["node_id"] == "u")["highlight_ranges"]
        fullwidth_slices = [js_slice(user_item["display_text"], item["start"], item["end"]) for item in fullwidth_ranges]
        self.assertIn("Ｉｎｔｅｌ", fullwidth_slices)
        combining = client.get("/api/conversations/unicode-highlight/messages?q=caf%C3%A9&match_mode=word&path=current").json()
        combining_item = next(item for item in combining["items"] if item["node_id"] == "u")
        combining_range = combining_item["highlight_ranges"][0]
        self.assertEqual(js_slice(combining_item["display_text"], combining_range["start"], combining_range["end"]), "cafe\u0301")

    def test_highlight_cap_is_disclosed_without_hiding_message_hit(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        terms = ["amber", "birch", "cedar", "denim", "ember", "frost", "glade", "hazel", "ivory", "jewel", "khaki"]
        mapping = {"root": root(["u"]), "u": node("u", "root", "user", " ".join(terms), 1_700_420_001)}
        z = base / "highlight-cap.zip"
        db = base / "archive.db"
        write_zip(z, [conv("highlight-cap", "Highlight Cap", mapping, "u", 1_700_420_000)])
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        self.addCleanup(client.close)
        query = quote(" ".join(terms))
        page = client.get(f"/api/conversations/highlight-cap/messages?q={query}&path=current").json()
        item = next(item for item in page["items"] if item["node_id"] == "u")
        self.assertEqual(len(item["highlight_ranges"]), 10)
        self.assertTrue(item["highlight_ranges_truncated"])
        hits = client.get(f"/api/search/messages?q={query}&conversation_id=highlight-cap&path=current").json()
        self.assertEqual(hits["total"], 1)

    def test_reader_without_positive_text_never_allocates_utf16_highlight_spans(self):
        from chatgpt_export_archiver import search as search_module

        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        with mock.patch.object(
            search_module,
            "_normalized_with_utf16_spans",
            side_effect=AssertionError("empty/filter/title reader normalized highlight text"),
        ):
            for query in (
                "",
                "?role=user",
                "?title=Web%20One",
                "?q=title%3AWeb",
                "?q=role%3Auser",
            ):
                response = client.get(f"/api/conversations/web-1/messages{query}")
                self.assertEqual(response.status_code, 200, query)
                self.assertFalse(any(item["highlight_ranges"] for item in response.json()["items"]))

    def test_reader_page_budgets_and_display_chunk_endpoint_are_bounded(self):
        from chatgpt_export_archiver.db import connect, init_db
        from chatgpt_export_archiver.search import get_messages

        env = {
            "CHATGPT_ARCHIVE_READER_MESSAGE_TEXT_CHARS": "8192",
            "CHATGPT_ARCHIVE_READER_PAGE_TEXT_CHARS": "32768",
            "CHATGPT_ARCHIVE_READER_PAGE_RAW_PREVIEW_CHARS": "4096",
            "CHATGPT_ARCHIVE_READER_PAGE_RAW_RESOLVER_CHARS": "8192",
            "CHATGPT_ARCHIVE_READER_PAGE_ESTIMATED_BYTES": "1048576",
            "CHATGPT_ARCHIVE_READER_PAGE_HIGHLIGHT_CHARS": "4096",
            "CHATGPT_ARCHIVE_READER_DISPLAY_CHUNK_CHARS": "4096",
        }
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, env, clear=False):
            base = Path(td)
            db = base / "budget.db"
            conn = connect(db)
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('budget', 'Budget', 'h')"
            )
            long_text = "🔥cafe\u0301" + ("x" * 20_000) + " tail-hit"
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, message_id, role, content_text) VALUES ('budget', ?, ?, 'user', ?)",
                ((f"n-{index:03d}", f"m-{index:03d}", long_text) for index in range(300)),
            )
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, message_id, role, content_text, raw_message_json) VALUES ('budget', 'raw-large', 'raw-message', 'assistant', '', ?)",
                ("{" + ("x" * 1_048_576) + "}",),
            )
            conn.commit()
            from chatgpt_export_archiver.db import migrate_database
            migrate_database(conn, refresh_compatibility=True)
            conn.commit()
            response_sizes: dict[int, int] = {}
            peak_bytes: dict[int, int] = {}
            for requested_limit in (1, 30, 100, 300):
                tracing_before = tracemalloc.is_tracing()
                if not tracing_before:
                    tracemalloc.start()
                baseline_current, _baseline_peak = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
                try:
                    bounded_page = get_messages(
                        conn,
                        "budget",
                        path="all",
                        limit=requested_limit,
                        offset=0,
                        include_internal=True,
                    )
                    _, peak = tracemalloc.get_traced_memory()
                    peak_bytes[requested_limit] = max(0, peak - baseline_current)
                finally:
                    if not tracing_before:
                        tracemalloc.stop()
                encoded = json.dumps(bounded_page, ensure_ascii=False).encode("utf-8")
                response_sizes[requested_limit] = len(encoded)
                self.assertLessEqual(sum(len(item["display_text"]) for item in bounded_page["items"]), 32768)
                self.assertLessEqual(sum(len(item["raw_preview"]) for item in bounded_page["items"]), 4096)
                self.assertLessEqual(len(encoded), 1_200_000)
            self.assertLess(peak_bytes[300], 12_000_000)
            self.assertLess(response_sizes[300], response_sizes[1] * 350)
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            page = get_messages(
                conn,
                "budget",
                path="all",
                limit=300,
                offset=0,
                highlight_query="tail-hit",
                include_internal=True,
            )
            conn.set_trace_callback(None)
            self.assertLessEqual(sum(len(item["display_text"]) for item in page["items"]), 32768)
            self.assertLessEqual(sum(len(item["raw_preview"]) for item in page["items"]), 4096)
            self.assertTrue(page["page_text_budget_exhausted"])
            self.assertLessEqual(page["response_budget_estimated"], page["response_budget_limit"])
            self.assertTrue(all(item["display_text_truncated"] for item in page["items"]))
            self.assertTrue(all(
                item["display_text_total_chars"] > len(item["display_text"])
                for item in page["items"]
            ))
            self.assertTrue(all(
                item["display_text_total_chars_exact"] is False
                for item in page["items"]
            ))
            self.assertTrue(all(
                item["display_text_resolver_input_truncated"] is False
                for item in page["items"]
            ))
            self.assertTrue(page["items"][0]["highlight_truncated"])
            self.assertEqual(page["items"][0]["highlight_ranges"], [])
            self.assertFalse(any(
                "LENGTH(COALESCE(CONTENT_TEXT" in sql.upper()
                or "SUBSTR(COALESCE(CONTENT_TEXT" in sql.upper()
                or "LENGTH(COALESCE(RAW_MESSAGE_JSON" in sql.upper()
                for sql in statements
            ))
            self.assertFalse(any(re.search(r"SELECT\s+content_text\s*,\s*raw_message_json", sql, re.I) for sql in statements))
            raw_page = get_messages(
                conn,
                "budget",
                path="all",
                limit=1,
                offset=300,
                include_internal=True,
            )
            self.assertEqual(raw_page["items"][0]["node_id"], "raw-large")
            self.assertTrue(raw_page["items"][0]["raw_preview_truncated"])
            self.assertTrue(raw_page["items"][0]["display_text_truncated"])
            self.assertTrue(raw_page["items"][0]["display_text_resolver_input_truncated"])
            conn.close()

            client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
            self.addCleanup(client.close)
            large_page_response = client.get(
                "/api/conversations/budget/messages?path=all&include_internal=true&limit=300"
            )
            self.assertEqual(large_page_response.status_code, 200)
            large_page = large_page_response.json()
            self.assertIn("items", large_page)
            self.assertNotIn("detail", large_page)
            self.assertEqual(len(large_page["items"]), 300)
            self.assertLess(len(large_page_response.content), 4 * 1024 * 1024)
            schema = client.get("/api/schema").json()
            self.assertEqual(schema["messages"]["budgets"]["display_chunk_chars"], 4096)
            first = client.get("/api/conversations/budget/messages/n-000/display?offset=0&limit=4096")
            self.assertEqual(first.status_code, 200)
            first_json = first.json()
            self.assertEqual(first_json["returned_chars"], 4096)
            self.assertTrue(first_json["has_more"])
            second = client.get(
                f"/api/conversations/budget/messages/n-000/display?offset={first_json['next_offset']}&limit=4096"
            ).json()
            self.assertEqual(second["offset"], 4096)
            self.assertNotIn("raw_message_json", first.text)
            raw = client.get("/api/conversations/budget/messages/raw-large/display?offset=0&limit=4096").json()
            self.assertTrue(raw["resolver_input_truncated"])
            self.assertFalse(raw["total_chars_exact"])
            self.assertNotIn("x" * 1000, json.dumps(raw))

    def test_whitespace_collapsed_phrase_highlights_and_snippets(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "whitespace-highlight.zip"
        text = "\n".join(
            [
                "Prefix before emoji 🔥 foo\nbar suffix.",
                "Second phrase has foo   bar with multiple spaces.",
                "Third phrase has foo\tbar with a tab.",
                "Mixed text 英特尔\nIntel keeps both sides visible.",
                "Fullwidth Ｐｙｔｈｏｎ plus cafe\u0301 and ﬁle compatibility text.",
            ]
        )
        mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", text, 1_700_510_001),
        }
        write_zip(z, [conv("whitespace-highlight", "Whitespace Highlight", mapping, "u", 1_700_510_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        client = TestClient(create_app(db))

        phrase = quote('"foo bar"')
        reader = client.get(f"/api/conversations/whitespace-highlight/messages?q={phrase}&path=current").json()
        item = next(item for item in reader["items"] if item["node_id"] == "u")
        slices = [js_slice(item["display_text"], r["start"], r["end"]) for r in item["highlight_ranges"]]
        self.assertIn("foo\nbar", slices)
        self.assertIn("foo   bar", slices)
        self.assertIn("foo\tbar", slices)
        hit = client.get(f"/api/search/messages?q={phrase}&conversation_id=whitespace-highlight&path=current").json()
        self.assertIn("foo bar", hit["items"][0]["snippet"])

        mixed = quote('"英特尔 Intel"')
        mixed_reader = client.get(f"/api/conversations/whitespace-highlight/messages?q={mixed}&path=current").json()
        mixed_item = next(item for item in mixed_reader["items"] if item["node_id"] == "u")
        mixed_slices = [js_slice(mixed_item["display_text"], r["start"], r["end"]) for r in mixed_item["highlight_ranges"]]
        self.assertIn("英特尔\nIntel", mixed_slices)

        fullwidth = client.get("/api/conversations/whitespace-highlight/messages?q=python&match_mode=word&path=current").json()
        fullwidth_item = next(item for item in fullwidth["items"] if item["node_id"] == "u")
        fullwidth_slices = [js_slice(fullwidth_item["display_text"], r["start"], r["end"]) for r in fullwidth_item["highlight_ranges"]]
        self.assertIn("Ｐｙｔｈｏｎ", fullwidth_slices)
        combining = client.get("/api/conversations/whitespace-highlight/messages?q=caf%C3%A9&match_mode=word&path=current").json()
        combining_item = next(item for item in combining["items"] if item["node_id"] == "u")
        combining_range = combining_item["highlight_ranges"][0]
        self.assertEqual(js_slice(combining_item["display_text"], combining_range["start"], combining_range["end"]), "cafe\u0301")

    def test_normalized_recall_for_messages_and_titles_without_and_with_web_index(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "normalized-recall.zip"
        text = "Fullwidth Ｉｎｔｅｌ body, decomposed cafe\u0301 marker, ﬁle ligature text, standalone ﬁ token, short Ｉｎ, CJK 子串, and emoji 🔥."
        mapping = {"root": root(["u"]), "u": node("u", "root", "user", text, 1_700_515_001)}
        write_zip(z, [conv("normalized-recall", "Title Ｉｎｔｅｌ cafe\u0301 ﬁle", mapping, "u", 1_700_515_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))

        def assert_normalized_hits(label: str):
            with self.subTest(label=label):
                for query in ["Intel", "café", "file"]:
                    message_page = client.get(f"/api/search/messages?q={quote(query)}&match_mode=word&path=all").json()
                    self.assertEqual({item["conversation_id"] for item in message_page["items"]}, {"normalized-recall"})
                    reader = client.get(f"/api/conversations/normalized-recall/messages?q={quote(query)}&match_mode=word&path=all").json()
                    item = next(row for row in reader["items"] if row["node_id"] == "u")
                    slices = [js_slice(item["display_text"], r["start"], r["end"]) for r in item["highlight_ranges"]]
                    self.assertTrue(slices, query)
                title_page = client.get("/api/conversations?q=Intel&scope=title&match_mode=word").json()
                self.assertEqual([item["conversation_id"] for item in title_page["items"]], ["normalized-recall"])
                for query in ["fi", "in", "子串", "🔥"]:
                    message_page = client.get(f"/api/search/messages?q={quote(query)}&path=all").json()
                    self.assertEqual({item["conversation_id"] for item in message_page["items"]}, {"normalized-recall"}, query)
                    conv_page = client.get(f"/api/conversations?q={quote(query)}&path=all").json()
                    self.assertEqual({item["conversation_id"] for item in conv_page["items"]}, {"normalized-recall"}, query)
                    reader = client.get(f"/api/conversations/normalized-recall/messages?q={quote(query)}&path=all").json()
                    item = next(row for row in reader["items"] if row["node_id"] == "u")
                    self.assertTrue(item["highlight_ranges"], query)
                word_short = client.get("/api/search/messages?q=fi&match_mode=word&path=all").json()
                self.assertEqual({item["conversation_id"] for item in word_short["items"]}, {"normalized-recall"})

        assert_normalized_hits("without web-index")
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        health = client.get("/api/health").json()
        self.assertTrue(health["web_normalized_indexed"])
        self.assertTrue(health["web_normalized_trigram_indexed"])
        self.assertFalse(health["web_legacy_trigram_indexed"])
        assert_normalized_hits("with normalized web-index")

        missing_trigram = sqlite3.connect(db)
        try:
            missing_trigram.execute("DROP TABLE IF EXISTS web_message_trigram")
            missing_trigram.execute("DROP TABLE IF EXISTS web_title_trigram")
            missing_trigram.commit()
        finally:
            missing_trigram.close()
        health = client.get("/api/health").json()
        self.assertTrue(health["web_normalized_indexed"])
        self.assertFalse(health["web_normalized_trigram_indexed"])
        self.assertFalse(health["web_trigram_indexed"])
        assert_normalized_hits("metadata normalized but trigram tables missing")

        legacy = sqlite3.connect(db)
        try:
            legacy.execute("DROP TABLE IF EXISTS web_index_metadata")
            legacy.execute("DROP TABLE IF EXISTS web_message_norm")
            legacy.execute("DROP TABLE IF EXISTS web_title_norm")
            legacy.execute("CREATE TABLE web_message_norm(conversation_id TEXT NOT NULL, node_id TEXT NOT NULL, content_norm TEXT NOT NULL, PRIMARY KEY(conversation_id, node_id))")
            legacy.execute("INSERT INTO web_message_norm SELECT conversation_id, node_id, lower(content_text) FROM conversation_nodes WHERE content_text IS NOT NULL")
            legacy.execute("CREATE TABLE web_title_norm(conversation_id TEXT PRIMARY KEY, title_norm TEXT NOT NULL)")
            legacy.execute("INSERT INTO web_title_norm SELECT conversation_id, lower(COALESCE(title, '')) FROM conversations")
            legacy.commit()
        finally:
            legacy.close()
        assert_normalized_hits("legacy raw-normalized tables")

    def test_reader_highlights_respect_full_search_filters(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "reader-filter.zip"
        mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "foo user body", 1_700_516_001, ["a"]),
            "a": node("a", "u", "assistant", "foo assistant body", 1_700_516_002, ["b"]),
            "b": node("b", "a", "assistant", "foo bar excluded body", 1_700_516_003),
        }
        write_zip(z, [conv("reader-filter", "Foo Title", mapping, "b", 1_700_516_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))

        excluded = client.get("/api/conversations/reader-filter/messages?q=foo&exclude=bar&path=current").json()
        self.assertFalse(any(item["highlight_ranges"] for item in excluded["items"]))

        role_filtered = client.get("/api/conversations/reader-filter/messages?q=foo&role=assistant&path=current").json()
        by_id = {item["node_id"]: item for item in role_filtered["items"]}
        self.assertFalse(by_id["u"]["highlight_ranges"])
        self.assertTrue(by_id["a"]["highlight_ranges"])

        title_scope = client.get("/api/conversations/reader-filter/messages?q=Foo&scope=title&path=current").json()
        self.assertFalse(any(item["highlight_ranges"] for item in title_scope["items"]))
        title_filter = client.get("/api/conversations/reader-filter/messages?title=Foo&path=current").json()
        self.assertFalse(any(item["highlight_ranges"] for item in title_filter["items"]))

    def test_reader_highlight_respects_title_excludes(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "reader-title-exclude.zip"
        clean_mapping = {"root": root(["u"]), "u": node("u", "root", "user", "foo body", 1_700_518_001)}
        blocked_mapping = {"root": root(["u"]), "u": node("u", "root", "user", "foo body", 1_700_518_002)}
        write_zip(
            z,
            [
                conv("reader-title-clean", "Clean Title", clean_mapping, "u", 1_700_518_000),
                conv("reader-title-blocked", "Blocked bar Title", blocked_mapping, "u", 1_700_518_001),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        page = client.get("/api/conversations?q=foo%20-bar&path=current&sort=title").json()
        self.assertEqual([item["conversation_id"] for item in page["items"]], ["reader-title-clean"])
        clean = client.get("/api/conversations/reader-title-clean/messages?q=foo%20-bar&path=current").json()
        self.assertTrue(any(item["highlight_ranges"] for item in clean["items"]))
        blocked = client.get("/api/conversations/reader-title-blocked/messages?q=foo%20-bar&path=current").json()
        self.assertFalse(any(item["highlight_ranges"] for item in blocked["items"]))

    def test_conversation_search_exclude_is_conversation_level(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "exclude-contract.zip"
        clean = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "foo only body", 1_700_519_001),
        }
        blocked = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "foo only body", 1_700_519_002, ["a"]),
            "a": node("a", "u", "assistant", "bar only body", 1_700_519_003),
        }
        title_blocked = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "foo only body", 1_700_519_004),
        }
        write_zip(
            z,
            [
                conv("exclude-clean", "Clean Title", clean, "u", 1_700_519_000),
                conv("exclude-blocked", "Neutral Title", blocked, "a", 1_700_519_001),
                conv("exclude-title-blocked", "Bar Title", title_blocked, "u", 1_700_519_002),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        self.addCleanup(client.close)

        for suffix in ("", "&scope=message", "&path=current", "&path=all", "&match_mode=contains", "&match_mode=word"):
            with self.subTest(suffix=suffix):
                page = client.get(f"/api/conversations?q=foo%20-bar{suffix}&sort=title").json()
                self.assertEqual([item["conversation_id"] for item in page["items"]], ["exclude-clean"])

        title_scope = client.get("/api/conversations?q=foo%20-bar&scope=title&sort=title").json()
        self.assertEqual(title_scope["total"], 0)
        title_filter = client.get("/api/conversations?q=foo&title=Title&exclude=bar&sort=title").json()
        self.assertEqual([item["conversation_id"] for item in title_filter["items"]], ["exclude-clean"])

        message_hits = client.get("/api/search/messages?q=foo%20-bar&conversation_id=exclude-blocked&path=all").json()
        self.assertEqual([item["node_id"] for item in message_hits["items"]], ["u"])
        reader = client.get("/api/conversations/exclude-blocked/messages?q=foo%20-bar&path=all").json()
        self.assertFalse(any(item["highlight_ranges"] for item in reader["items"]))

    def test_selected_conversation_metadata_is_returned_outside_current_page(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "selected-meta.zip"
        conversations = []
        for cid, title in [("sel-a", "A first"), ("sel-b", "B second"), ("sel-c", "C selected")]:
            mapping = {"root": root(["u"]), "u": node("u", "root", "user", "selneedle body", 1_700_517_001)}
            conversations.append(conv(cid, title, mapping, "u", 1_700_517_000))
        write_zip(z, conversations)
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        page = client.get("/api/conversations?q=selneedle&sort=title&limit=1&selected_id=sel-c").json()
        self.assertTrue(page["selected_in_results"])
        self.assertEqual([item["conversation_id"] for item in page["items"]], ["sel-a"])
        self.assertEqual(page["selected_item"]["conversation_id"], "sel-c")
        self.assertTrue(page["selected_item"]["message_match"])

    def test_selected_metadata_for_normalized_short_fragment_outside_page(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "selected-normalized-short.zip"
        newer = {"root": root(["u"]), "u": node("u", "root", "user", "newer ﬁ hit", 1_700_519_101)}
        older = {"root": root(["u"]), "u": node("u", "root", "user", "older ﬁ hit", 1_700_519_001)}
        write_zip(
            z,
            [
                conv("selected-short-new", "New", newer, "u", 1_700_519_100),
                conv("selected-short-old", "Old", older, "u", 1_700_519_000),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        client = TestClient(create_app(db))
        page = client.get("/api/conversations?q=fi&sort=newest&limit=1&selected_id=selected-short-old").json()
        self.assertTrue(page["selected_in_results"])
        self.assertEqual([item["conversation_id"] for item in page["items"]], ["selected-short-new"])
        self.assertEqual(page["selected_item"]["conversation_id"], "selected-short-old")
        self.assertTrue(page["selected_item"]["message_match"])

    def test_conversation_relevance_sort_uses_score_not_newest(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "relevance-sort.zip"
        strong_title = {"root": root(["u"]), "u": node("u", "root", "user", "body without query", 1_700_521_001)}
        weak_body = {"root": root(["u"]), "u": node("u", "root", "user", "foo body", 1_700_522_001)}
        write_zip(
            z,
            [
                conv("relevance-title-old", "foo title", strong_title, "u", 1_700_521_000),
                conv("relevance-body-new", "plain title", weak_body, "u", 1_700_522_000),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        relevance = client.get("/api/conversations?q=foo&sort=relevance&limit=2&selected_id=relevance-body-new").json()
        self.assertEqual([item["conversation_id"] for item in relevance["items"]], ["relevance-title-old", "relevance-body-new"])
        self.assertTrue(relevance["selected_in_results"])
        newest = client.get("/api/conversations?q=foo&sort=newest&limit=2").json()
        self.assertEqual([item["conversation_id"] for item in newest["items"]], ["relevance-body-new", "relevance-title-old"])

    def test_message_search_without_count_preserves_order_and_has_more_probe(self):
        from chatgpt_export_archiver.search import parse_query, search_messages

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = connect_readonly(db)
        try:
            parsed = parse_query("python", path_default="all")
            first = search_messages(conn, parsed, limit=1, offset=0, order="display", count_total=False)
            continuation = first["diagnostics"]["continuation_token"]
            second = search_messages(
                conn,
                parsed,
                limit=1,
                offset=0,
                order="display",
                count_total=False,
                continuation=continuation,
            )
            self.assertEqual(len(first["items"]), 1)
            self.assertTrue(first["has_more"])
            self.assertIsNone(first["next_offset"])
            self.assertIsInstance(continuation, str)
            self.assertNotEqual(first["items"][0]["node_id"], second["items"][0]["node_id"])
            self.assertLessEqual(first["total"], 2)
        finally:
            conn.close()

    def test_message_search_api_count_total_false_uses_fast_page_probe(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        payload = client.get("/api/search/messages?q=python&limit=1&count_total=false").json()
        self.assertEqual(len(payload["items"]), 1)
        self.assertTrue(payload["has_more"])
        self.assertLessEqual(payload["total"], 2)
        self.assertFalse(payload["total_exact"])
        self.assertLessEqual(payload["diagnostics"]["candidate_count"], 5)
        self.assertEqual(payload["diagnostics"]["partial_reason"], "page_result_limit")

    def test_trigram_partial_candidates_keep_safe_and_terms_without_full_scan(self):
        from chatgpt_export_archiver import search as search_module

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "partial-trigram.zip"
        mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "go partiallong 英特尔 Intel gpt-5.5 quoted phrase target", 1_700_520_001),
        }
        write_zip(z, [conv("partial-trigram", "go partiallong title", mapping, "u", 1_700_520_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        conn = connect_readonly(db)
        try:
            for raw in ["go partiallong", "go 英特尔", "gpt-5.5 go", '"quoted phrase" go']:
                with self.subTest(raw=raw):
                    parsed = search_module.parse_query(raw, path_default="all")
                    source_sql, _params, _score, _reason = search_module._message_match_source(conn, parsed, use_trigram=True)
                    self.assertIn("web_message_trigram", source_sql)
                    title_sql, _title_params = search_module._title_conversation_select(conn, parsed, use_trigram=True)
                    self.assertIn("web_title_trigram", title_sql)
            parsed_or = search_module.parse_query("go OR partiallong", path_default="all")
            source_sql, _params, _score, _reason = search_module._message_match_source(conn, parsed_or, use_trigram=True)
            self.assertNotIn("web_message_trigram", source_sql)
            complete_or = search_module.parse_query("partiallong OR 英特尔", path_default="all")
            source_sql, _params, _score, _reason = search_module._message_match_source(conn, complete_or, use_trigram=True)
            self.assertIn("web_message_trigram", source_sql)
            page = search_module.search_messages(conn, search_module.parse_query("go partiallong", path_default="all"), limit=5, order="display")
            self.assertEqual(page["total"], 1)
        finally:
            conn.close()

    def test_illegal_query_pagination_and_no_raw_json(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        response = client.get('/api/search?q="%3A%3A%3A%20(((')
        self.assertEqual(response.status_code, 200)
        first = client.get("/api/conversations?limit=1&offset=0&sort=title").json()
        second = client.get("/api/conversations?limit=1&offset=1&sort=title").json()
        self.assertNotEqual(first["items"][0]["conversation_id"], second["items"][0]["conversation_id"])
        payload = json.dumps(client.get("/api/search/messages?q=raw_json").json())
        self.assertNotIn("raw_message_json", payload)
        self.assertNotIn("raw_json", payload)
        self.assertEqual(client.get("/api/conversations?sort=bad").status_code, 400)
        self.assertEqual(client.get("/api/conversations?scope=bad").status_code, 400)
        self.assertEqual(client.get("/api/conversations?role=bad").status_code, 400)
        self.assertEqual(client.get("/api/conversations?path=bad").status_code, 400)
        self.assertEqual(client.get("/api/conversations?after=not-a-date").status_code, 400)
        self.assertEqual(client.get("/api/search?q=React&before=not-a-date").status_code, 400)
        self.assertEqual(client.get("/api/conversations?limit=1000").status_code, 422)
        too_long_date = "2" * 65
        too_long_id = "x" * (16 * 1024 + 1)
        self.assertEqual(client.get(f"/api/conversations?after={too_long_date}").status_code, 422)
        self.assertEqual(client.get(f"/api/conversations?selected_id={too_long_id}").status_code, 422)
        self.assertEqual(client.get(f"/api/search?selected_id={too_long_id}").status_code, 422)
        self.assertEqual(client.get(f"/api/search/messages?conversation_id={too_long_id}").status_code, 422)
        self.assertEqual(client.get(f"/api/by-id/messages?conversation_id=web-1&around_node_id={too_long_id}").status_code, 422)
        self.assertEqual(client.get(f"/api/by-id/conversation?conversation_id={too_long_id}").status_code, 422)

    def test_selected_membership_and_empty_results_contract(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        kept = client.get("/api/conversations?q=React&selected_id=web-2").json()
        self.assertTrue(kept["selected_in_results"])
        moved = client.get("/api/conversations?q=React&selected_id=web-1").json()
        self.assertFalse(moved["selected_in_results"])
        empty = client.get("/api/conversations?q=no-such-synthetic-result&selected_id=web-1").json()
        self.assertEqual(empty["items"], [])
        self.assertFalse(empty["selected_in_results"])

    def test_message_pagination_for_long_conversation(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "long.zip"
        mapping = {"root": root(["n0"])}
        previous = "root"
        for idx in range(360):
            node_id = f"n{idx}"
            child = f"n{idx + 1}" if idx < 359 else None
            mapping[node_id] = node(node_id, previous, "user" if idx % 2 else "assistant", f"message {idx}", 1_700_100_000 + idx, [child] if child else [])
            previous = node_id
        write_zip(z, [conv("long-1", "Long Conversation", mapping, "n359", 1_700_100_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        first = client.get("/api/conversations/long-1/messages?limit=120&offset=0").json()
        second = client.get("/api/conversations/long-1/messages?limit=120&offset=120").json()
        self.assertEqual(first["total"], 360)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 120)
        self.assertEqual(len(second["items"]), 120)
        around = client.get("/api/conversations/long-1/messages?limit=20&around_node_id=n350").json()
        self.assertTrue(any(item["node_id"] == "n350" for item in around["items"]))

    def test_conversation_search_counts_more_than_public_message_page_limit(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "many.zip"
        conversations = []
        for idx in range(150):
            cid = f"many-{idx:03d}"
            mapping = {
                "root": root(["u"]),
                "u": node("u", "root", "user", f"needle synthetic message {idx}", 1_700_300_000 + idx),
            }
            conversations.append(conv(cid, f"Many {idx}", mapping, "u", 1_700_300_000 + idx))
        write_zip(z, conversations)
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        page = client.get("/api/conversations?q=needle&limit=100").json()
        self.assertEqual(page["total"], 150)
        self.assertEqual(len(page["items"]), 100)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 100)

    def test_conversation_search_total_is_not_capped_at_internal_candidate_limit(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "many-3105.zip"
        conversations = []
        for idx in range(3105):
            cid = f"many-cap-{idx:04d}"
            mapping = {
                "root": root(["u"]),
                "u": node("u", "root", "user", f"capneedle synthetic message {idx}", 1_700_700_000 + idx),
            }
            conversations.append(conv(cid, f"Cap {idx}", mapping, "u", 1_700_700_000 + idx))
        write_zip(z, conversations)
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        page = client.get("/api/conversations?q=capneedle&limit=100").json()
        self.assertEqual(page["total"], 3105)
        self.assertEqual(len(page["items"]), 100)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 100)

    def test_conversation_search_does_not_call_unbounded_message_search(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.search import parse_query, search_conversations

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = connect_readonly(db)
        try:
            with mock.patch.object(search_module, "search_messages", side_effect=AssertionError("unbounded path")):
                page = search_conversations(conn, parse_query("synthetic"), limit=2, offset=0)
            self.assertGreaterEqual(page["total"], 1)
            self.assertLessEqual(len(page["items"]), 2)
        finally:
            conn.close()

    def test_conversation_search_late_page_uses_sql_level_pagination(self):
        from chatgpt_export_archiver.search import parse_query, search_conversations

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "many-late.zip"
        rows = []
        for idx in range(3200):
            mapping = {
                "root": root(["n"]),
                "n": node("n", "root", "user", f"commonneedle synthetic {idx}", 1_700_500_000 + idx),
            }
            rows.append(conv(f"many-late-{idx:04d}", f"Many {idx}", mapping, "n", 1_700_500_000 + idx))
        write_zip(z, rows)
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        conn = connect_readonly(db)
        try:
            page = search_conversations(conn, parse_query("commonneedle"), limit=50, offset=3150, sort="newest")
            self.assertEqual(page["total"], 3200)
            self.assertEqual(len(page["items"]), 50)
            self.assertFalse(page["has_more"])
        finally:
            conn.close()

    def test_message_search_total_and_late_pages_are_not_capped(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "messages-3105.zip"
        mapping = {"root": root(["n0"])}
        previous = "root"
        for idx in range(3105):
            node_id = f"n{idx}"
            child = f"n{idx + 1}" if idx < 3104 else None
            mapping[node_id] = node(node_id, previous, "user", f"pagecap synthetic hit {idx}", 1_700_800_000 + idx, [child] if child else [])
            previous = node_id
        write_zip(z, [conv("message-cap", "Message Cap", mapping, "n3104", 1_700_800_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        page = client.get("/api/search/messages?q=pagecap&conversation_id=message-cap&path=current&order=display&limit=100&offset=3000").json()
        self.assertEqual(page["total"], 3105)
        self.assertEqual(len(page["items"]), 100)
        self.assertEqual(page["items"][0]["node_id"], "n3000")
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 3100)
        tail = client.get("/api/search/messages?q=pagecap&conversation_id=message-cap&path=current&order=display&limit=100&offset=3100").json()
        self.assertEqual(tail["total"], 3105)
        self.assertEqual(len(tail["items"]), 5)
        self.assertFalse(tail["has_more"])

    def test_message_hits_are_not_polluted_by_title_matches(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "title.zip"
        mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "plain first body", 1_700_400_001, ["a"]),
            "a": node("a", "u", "assistant", "plain second body", 1_700_400_002, ["b"]),
            "b": node("b", "a", "user", "plain third body", 1_700_400_003),
        }
        write_zip(z, [conv("title-only", "Needle Title Only", mapping, "b", 1_700_400_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        hits = client.get("/api/search/messages?q=Needle&conversation_id=title-only&order=display").json()
        self.assertEqual(hits["total"], 0)
        conversations = client.get("/api/conversations?q=Needle").json()
        self.assertEqual(conversations["total"], 1)
        title_only = client.get("/api/search/messages?q=title:Needle&conversation_id=title-only&order=display").json()
        self.assertEqual(title_only["total"], 0)
        scoped_hits = client.get("/api/search/messages?q=Needle&conversation_id=title-only&scope=title&order=display").json()
        self.assertEqual(scoped_hits["total"], 0)
        title_conversations = client.get("/api/conversations?q=title:Needle").json()
        self.assertEqual(title_conversations["total"], 1)
        self.assertEqual(title_conversations["items"][0]["hit_count"], 0)
        self.assertEqual(title_conversations["items"][0]["snippets"], [])
        self.assertIn("title match", title_conversations["items"][0]["reasons"])
        title_param = client.get("/api/conversations?title=Needle").json()
        self.assertEqual(title_param["total"], 1)
        self.assertEqual(title_param["items"][0]["hit_count"], 0)
        self.assertEqual(title_param["items"][0]["snippets"], [])

    def test_source_filter_does_not_create_message_hits_or_snippets(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "source.zip"
        write_zip(
            z,
            [
                conv("source-only", "Source Only", {"root": root(["u"]), "u": node("u", "root", "user", "plain body", 1_700_401_000)}, "u", 1_700_401_000),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        hits = client.get("/api/search/messages?q=source:conversations.json&conversation_id=source-only&order=display").json()
        self.assertEqual(hits["total"], 0)
        conversations = client.get("/api/conversations?q=source:conversations.json").json()
        self.assertEqual(conversations["total"], 1)
        self.assertEqual(conversations["items"][0]["hit_count"], 0)
        self.assertEqual(conversations["items"][0]["snippets"], [])

    def test_or_terms_and_exclude_are_combined_as_positive_then_not(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "or.zip"
        mapping = {
            "root": root(["alpha"]),
            "alpha": node("alpha", "root", "user", "alpha ok", 1_700_500_001, ["beta"]),
            "beta": node("beta", "alpha", "assistant", "alpha bad", 1_700_500_002, ["gamma"]),
            "gamma": node("gamma", "beta", "user", "gamma ok", 1_700_500_003, ["delta"]),
            "delta": node("delta", "gamma", "assistant", "delta ok", 1_700_500_004),
        }
        write_zip(z, [conv("or-1", "OR Exclude", mapping, "delta", 1_700_500_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        hits = client.get("/api/search/messages?q=alpha%20OR%20gamma%20-bad&conversation_id=or-1&path=current&order=display").json()
        self.assertEqual([item["node_id"] for item in hits["items"]], ["alpha", "gamma"])

    def test_or_query_keeps_advanced_filters_required(self):
        from chatgpt_export_archiver.web_db import create_web_indexes

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "or-required.zip"
        db = base / "archive.db"
        write_zip(
            z,
            [
                conv("raw-only", "Plain", {"root": root(["n"]), "n": node("n", "root", "user", "foo only", 1_700_700_001)}, "n", 1_700_700_000),
                conv("required-body", "Plain", {"root": root(["n"]), "n": node("n", "root", "user", "foo baz", 1_700_700_002)}, "n", 1_700_700_001),
                conv("required-title", "Target title", {"root": root(["n"]), "n": node("n", "root", "user", "bar body", 1_700_700_003)}, "n", 1_700_700_002),
                conv("wrong-title", "Other title", {"root": root(["n"]), "n": node("n", "root", "user", "bar body", 1_700_700_004)}, "n", 1_700_700_003),
                conv("exact-only", "Exact Only", {"root": root(["n"]), "n": node("n", "root", "user", "foo exact body", 1_700_700_005)}, "n", 1_700_700_004),
            ],
        )
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)

        def assert_required_semantics(client):
            exact_hits = client.get("/api/search/messages?q=OR&exact=foo&path=all&limit=20").json()
            self.assertIn("exact-only", {item["conversation_id"] for item in exact_hits["items"]})
            exact_conversations = client.get("/api/conversations?q=OR&exact=foo&path=all&limit=20").json()
            self.assertIn("exact-only", {item["conversation_id"] for item in exact_conversations["items"]})
            exact_reader = client.get("/api/conversations/exact-only/messages?q=OR&exact=foo&path=all&limit=20").json()
            self.assertTrue(next(item for item in exact_reader["items"] if item["node_id"] == "n")["highlight_ranges"])

            hits = client.get("/api/search/messages?q=foo%20OR%20bar&exact=baz&path=all&limit=20").json()
            self.assertEqual([item["conversation_id"] for item in hits["items"]], ["required-body"])
            conversations = client.get("/api/conversations?q=foo%20OR%20bar&exact=baz&path=all&limit=20").json()
            self.assertEqual([item["conversation_id"] for item in conversations["items"]], ["required-body"])
            title_filtered = client.get("/api/conversations?q=foo%20OR%20bar&title=Target&path=all&limit=20").json()
            self.assertEqual([item["conversation_id"] for item in title_filtered["items"]], ["required-title"])
            title_hits = client.get("/api/search/messages?q=foo%20OR%20bar&title=Target&path=all&limit=20").json()
            self.assertEqual([item["conversation_id"] for item in title_hits["items"]], ["required-title"])
            title_reader = client.get("/api/conversations/required-title/messages?q=foo%20OR%20bar&title=Target&path=all&limit=20").json()
            self.assertTrue(next(item for item in title_reader["items"] if item["node_id"] == "n")["highlight_ranges"])
            excluded_hits = client.get("/api/search/messages?q=foo%20OR%20bar&exclude=baz&path=all&limit=20").json()
            excluded_hit_ids = {item["conversation_id"] for item in excluded_hits["items"]}
            self.assertIn("raw-only", excluded_hit_ids)
            self.assertNotIn("required-body", excluded_hit_ids)
            excluded_conversations = client.get("/api/conversations?q=foo%20OR%20bar&exclude=baz&path=all&limit=20").json()
            excluded_conversation_ids = {item["conversation_id"] for item in excluded_conversations["items"]}
            self.assertIn("raw-only", excluded_conversation_ids)
            self.assertNotIn("required-body", excluded_conversation_ids)
            excluded_reader = client.get("/api/conversations/required-body/messages?q=foo%20OR%20bar&exclude=baz&path=all&limit=20").json()
            self.assertFalse(any(item["highlight_ranges"] for item in excluded_reader["items"]))
            excluded = client.get("/api/conversations?q=foo%20OR%20bar&exact=baz&exclude=foo&path=all&limit=20").json()
            self.assertEqual(excluded["total"], 0)
            reader = client.get("/api/conversations/raw-only/messages?q=foo%20OR%20bar&exact=baz&path=all&limit=20").json()
            self.assertFalse(any(item["highlight_ranges"] for item in reader["items"]))
            overlap = client.get("/api/conversations/required-body/messages?q=foo&exact=foo%20baz&path=all&limit=20").json()
            ranges = next(item for item in overlap["items"] if item["node_id"] == "n")["highlight_ranges"]
            self.assertEqual([js_slice("foo baz", r["start"], r["end"]) for r in ranges], ["foo baz"])

        client = TestClient(create_app(db))
        assert_required_semantics(client)
        create_web_indexes(db)
        assert_required_semantics(client)

    def test_raw_path_scope_modifiers_and_technical_hits_are_consistent(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "technical.zip"
        db = base / "archive.db"
        mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "visible current", 1_700_710_001, ["a", "branch"]),
            "a": custom_content_node("a", "u", "assistant", {"content_type": "thoughts", "parts": ["hiddenneedle thoughts"]}, 1_700_710_002),
            "branch": node("branch", "u", "assistant", "branchtoken branch message", 1_700_710_003),
        }
        write_zip(z, [conv("technical", "Scope Target", mapping, "a", 1_700_710_000)])
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))

        current = client.get("/api/search/messages?q=branchtoken&conversation_id=technical&path=current").json()
        self.assertEqual(current["total"], 0)
        path_all = client.get("/api/search/messages?q=path:all%20branchtoken&conversation_id=technical&path=current").json()
        self.assertEqual(path_all["total"], 1)
        reader = client.get("/api/conversations/technical/messages?q=path:all%20branchtoken&path=current&limit=20").json()
        by_id = {item["node_id"]: item for item in reader["items"]}
        self.assertTrue(by_id["branch"]["highlight_ranges"])

        title_scope = client.get("/api/conversations?q=scope:title%20Target&path=current").json()
        self.assertEqual(title_scope["total"], 1)
        title_reader = client.get("/api/conversations/technical/messages?q=scope:title%20Target&path=current&limit=20").json()
        self.assertFalse(any(item["highlight_ranges"] for item in title_reader["items"]))

        hidden = client.get("/api/conversations?q=hiddenneedle&path=all").json()
        self.assertEqual(hidden["items"][0]["conversation_id"], "technical")
        self.assertTrue(hidden["items"][0]["has_internal_hits"])
        hidden_hit = client.get("/api/search/messages?q=hiddenneedle&conversation_id=technical&path=all").json()
        self.assertTrue(hidden_hit["items"][0]["is_internal"])
        messages = client.get("/api/conversations/technical/messages?path=all&limit=20&include_internal=true").json()
        self.assertTrue(next(item for item in messages["items"] if item["node_id"] == "a")["is_internal"])

    def test_generated_non_text_placeholders_are_not_body_hits(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "placeholder.zip"
        db = base / "archive.db"
        mapping = {
            "root": root(["n"]),
            "n": custom_content_node("n", "root", "assistant", {"content_type": "image_asset_pointer"}, 1_700_720_001),
        }
        write_zip(z, [conv("placeholder", "Placeholder", mapping, "n", 1_700_720_000)])
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        self.assertEqual(client.get("/api/search/messages?q=image_asset_pointer&path=all").json()["total"], 0)
        self.assertEqual(client.get("/api/conversations?q=image_asset_pointer&path=all").json()["total"], 0)
        reader = client.get("/api/conversations/placeholder/messages?q=image_asset_pointer&path=all").json()
        self.assertFalse(any(item["highlight_ranges"] for item in reader["items"]))

    def test_long_generated_placeholder_recovers_raw_text_before_and_after_web_index(self):
        from chatgpt_export_archiver.db import init_db
        from chatgpt_export_archiver.search import parse_query, search_messages
        from chatgpt_export_archiver.web_db import create_web_indexes, connect_readonly

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "long-placeholder.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            placeholder = "[non-text content: " + ("legacy-kind-" * 40) + "]"
            raw = json.dumps(
                {"content": {"content_type": "text", "parts": ["recovered-index-needle"]}},
                separators=(",", ":"),
            )
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) "
                "VALUES ('c', 'Synthetic', 'n', 'h')"
            )
            conn.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       raw_message_json, content_hash, is_on_current_path
                   ) VALUES ('c', 'n', 'assistant', 'text', ?, ?, 'r1', 1)""",
                (placeholder, raw),
            )
            conn.commit()
            before = search_messages(
                conn, parse_query("recovered-index-needle"), conversation_id="c"
            )
            self.assertEqual([item["node_id"] for item in before["items"]], ["n"])
            conn.close()

            result = create_web_indexes(db)
            self.assertEqual(result["indexed_messages"], 1)
            reader = connect_readonly(db)
            after = search_messages(
                reader, parse_query("recovered-index-needle"), conversation_id="c"
            )
            self.assertEqual([item["node_id"] for item in after["items"]], ["n"])
            indexed = reader.execute(
                "SELECT content_norm FROM web_message_norm WHERE conversation_id='c' AND node_id='n'"
            ).fetchone()[0]
            self.assertIn("recovered-index-needle", indexed)
            self.assertNotIn("legacy-kind", indexed)
            reader.close()

    def test_web_index_canonical_path_does_not_materialize_unrelated_raw_blob(self):
        from chatgpt_export_archiver.db import init_db
        from chatgpt_export_archiver.web_db import create_web_indexes

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "canonical-raw.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) "
                "VALUES ('c', 'Synthetic', 'n', 'h')"
            )
            conn.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       raw_message_json, content_hash, is_on_current_path
                   ) VALUES ('c', 'n', 'assistant', 'text', 'canonical-index-needle', ?, 'r1', 1)""",
                ("x" * (5 * 1024 * 1024),),
            )
            conn.commit()
            conn.close()

            result = create_web_indexes(db)
            self.assertEqual(result["indexed_messages"], 1)
            self.assertEqual(
                result["input_materialized_bytes"],
                len("canonical-index-needle") + len("Synthetic"),
            )
            self.assertLess(result["peak_batch_input_bytes"], 1024)

    def test_unrelated_oversized_raw_returns_confirmed_hits_as_partial_not_413(self):
        from chatgpt_export_archiver.db import init_db, migrate_database
        from chatgpt_export_archiver.web_db import create_web_indexes

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "oversized-search.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) "
                "VALUES (?, 'Synthetic', 'n', ?)",
                (("confirmed", "h1"), ("oversized", "h2")),
            )
            conn.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       content_hash, is_on_current_path
                   ) VALUES ('confirmed', 'n', 'assistant', 'text',
                             'needle wanted', 'r1', 1)"""
            )
            raw = json.dumps(
                {"content": {"content_type": "text", "parts": ["x" * (5 * 1024 * 1024)]}},
                separators=(",", ":"),
            )
            conn.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       raw_message_json, content_hash, is_on_current_path
                   ) VALUES ('oversized', 'n', 'assistant', 'text',
                             '[non-text content: legacy]', ?, 'r2', 1)""",
                (raw,),
            )
            conn.commit()
            migrate_database(conn, refresh_compatibility=True)
            conn.close()

            client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
            self.addCleanup(client.close)

            def assert_partial() -> None:
                response = client.get(
                    "/api/search/messages",
                    params={"q": "needle wanted", "path": "all", "count_total": "true"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(
                    [(item["conversation_id"], item["node_id"]) for item in payload["items"]],
                    [("confirmed", "n")],
                )
                self.assertFalse(payload["total_exact"])
                self.assertTrue(payload["diagnostics"]["partial"])
                self.assertEqual(payload["diagnostics"]["completion_state"], "partial")
                self.assertGreaterEqual(payload["diagnostics"]["oversized_candidates_pending"], 1)

            assert_partial()
            create_web_indexes(db)
            assert_partial()

    def test_export_path_validation_and_advanced_search_endpoint_defaults(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        self.assertEqual(client.get("/api/conversations/web-1/export?path=bad").status_code, 400)
        self.assertNotEqual(client.get("/api/search?exact=python%20-m%20unittest").status_code, 422)
        self.assertNotEqual(client.get("/api/search?title=Python").status_code, 422)
        self.assertNotEqual(client.get("/api/search?exclude=SQLite").status_code, 422)

    def test_title_exclude_filters_title_matches(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "title-exclude.zip"
        write_zip(
            z,
            [
                conv("title-ok", "Needle clean", {"root": root(["u"]), "u": node("u", "root", "user", "body", 1_700_600_001)}, "u", 1_700_600_000),
                conv("title-bad", "Needle bad", {"root": root(["u"]), "u": node("u", "root", "user", "body", 1_700_600_002)}, "u", 1_700_600_001),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        page = client.get("/api/conversations?q=Needle%20-bad&scope=title").json()
        self.assertEqual([item["conversation_id"] for item in page["items"]], ["title-ok"])

    def test_search_messages_display_order_matches_reader_order(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "order.zip"
        mapping = {
            "root": root(["z-first"]),
            "z-first": node("z-first", "root", "user", "displayneedle first visual message", 1_700_200_300, ["a-second"]),
            "a-second": node("a-second", "z-first", "assistant", "displayneedle second visual message", 1_700_200_100, ["m-third"]),
            "m-third": node("m-third", "a-second", "user", "displayneedle third visual message", 1_700_200_200),
        }
        write_zip(z, [conv("order-1", "Display Order", mapping, "m-third", 1_700_200_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        reader = client.get("/api/conversations/order-1/messages?path=current&limit=20").json()
        expected = [
            item["node_id"]
            for item in reader["items"]
            if item["node_id"] in {"z-first", "a-second", "m-third"}
        ]
        self.assertEqual(expected, ["z-first", "a-second", "m-third"])
        display_hits = client.get("/api/search/messages?q=displayneedle&conversation_id=order-1&path=current&order=display&limit=10").json()
        self.assertEqual([item["node_id"] for item in display_hits["items"]], expected)
        relevance_hits = client.get("/api/search/messages?q=displayneedle&conversation_id=order-1&path=current&limit=10").json()
        self.assertEqual(relevance_hits["total"], 3)
        self.assertCountEqual([item["node_id"] for item in relevance_hits["items"]], expected)

    def test_fallback_html_escape_covers_attributes(self):
        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        with self.assertRaises(ValueError) as ctx:
            create_app(db, static_dir=Path(td.name) / "missing-build")
        self.assertIn("React Web UI build is missing", str(ctx.exception))
        fallback_client = TestClient(create_app(db, static_dir=Path(td.name) / "missing-build", allow_fallback=True))
        html = fallback_client.get("/").text
        self.assertIn("Limited minimal fallback UI", html)
        self.assertIn("const interactiveSelector", html)
        self.assertIn("[role='button']", html)
        self.assertIn("[contenteditable]", html)
        self.assertIn("&quot;", html)
        self.assertIn("&#39;", html)
        self.assertIn("&#96;", html)
        self.assertIn("if(!r.ok)", html)
        self.assertIn("Array.isArray(data.items)", html)
        self.assertIn("database_migration_required", html)
        self.assertIn("r.body.getReader()", html)
        self.assertIn("reader.cancel()", html)
        self.assertNotIn("const body=await r.text()", html)
        for route in (
            "/api/by-id/conversation",
            "/api/by-id/messages",
            "/api/by-id/export",
            "/api/by-id/copy",
            "/api/by-id/raw",
            "/api/by-id/display",
        ):
            self.assertIn(route, html)
        self.assertNotIn("/api/conversations/${", html)

    def test_react_build_served_when_present_not_fallback(self):
        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        build = Path(td.name) / "dist"
        build.mkdir()
        (build / "index.html").write_text("<!doctype html><html><body><div id=\"root\"></div><script type=\"module\" src=\"/assets/app.js\"></script></body></html>", encoding="utf-8")
        (build / "assets").mkdir()
        (build / "assets" / "app.js").write_text("document.body.dataset.reactSmoke='ok';", encoding="utf-8")
        client = TestClient(create_app(db, static_dir=build))
        html = client.get("/").text
        self.assertNotIn("Fallback UI", html)
        self.assertIn('id="root"', html)

    def test_web_index_builds_normalized_tables(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        health = client.get("/api/health").json()
        self.assertTrue(health["web_normalized_indexed"])
        self.assertEqual(client.get("/api/search?q=%EF%BD%87%EF%BD%90%EF%BD%94%EF%BC%8D%EF%BC%95%EF%BC%8E%EF%BC%95").json()["items"][0]["conversation_id"], "web-1")
        conn = connect_readonly(db)
        try:
            message_columns = {row["name"] for row in conn.execute('PRAGMA table_xinfo("web_message_trigram")')}
            title_columns = {row["name"] for row in conn.execute('PRAGMA table_xinfo("web_title_trigram")')}
            metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM web_index_metadata")}
        finally:
            conn.close()
        self.assertIn("content_text", message_columns)
        self.assertIn("title", title_columns)
        self.assertNotIn("conversation_id", message_columns)
        self.assertNotIn("node_id", message_columns)
        self.assertNotIn("conversation_id", title_columns)
        self.assertEqual(metadata["message_trigram_text"], "normalized")
        self.assertEqual(metadata["title_trigram_text"], "normalized")

    def test_search_remains_compatible_with_legacy_contentful_web_trigram(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE VIRTUAL TABLE web_message_trigram USING fts5(conversation_id UNINDEXED, node_id UNINDEXED, role UNINDEXED, content_text, tokenize='trigram')")
            conn.execute("CREATE VIRTUAL TABLE web_title_trigram USING fts5(conversation_id UNINDEXED, title, tokenize='trigram')")
            conn.execute(
                """
                INSERT INTO web_message_trigram(conversation_id, node_id, role, content_text)
                SELECT conversation_id, node_id, role, content_text
                FROM conversation_nodes
                WHERE content_text IS NOT NULL AND content_text <> ''
                """
            )
            conn.execute(
                """
                INSERT INTO web_title_trigram(conversation_id, title)
                SELECT conversation_id, COALESCE(title, '')
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
                INSERT INTO web_message_norm(conversation_id, node_id, content_norm)
                SELECT conversation_id, node_id, lower(content_text)
                FROM conversation_nodes
                WHERE content_text IS NOT NULL AND content_text <> ''
                """
            )
            conn.execute("CREATE TABLE web_title_norm(conversation_id TEXT PRIMARY KEY, title_norm TEXT NOT NULL)")
            conn.execute("INSERT INTO web_title_norm(conversation_id, title_norm) SELECT conversation_id, lower(COALESCE(title, '')) FROM conversations")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(client.get("/api/search/messages?q=python&limit=5").json()["total"], 2)
        self.assertEqual(client.get("/api/conversations?q=title:Python&limit=5").json()["total"], 1)

    def test_web_search_uses_trigram_candidates_and_preserves_filtering(self):
        from chatgpt_export_archiver.search import parse_query, search_conversations
        from chatgpt_export_archiver.web_db import create_web_indexes

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "trigram.zip"
        db = base / "archive.db"
        keep_mapping = {"root": root(["n"]), "n": node("n", "root", "user", "needlelong keep", 1_700_600_000)}
        drop_mapping = {"root": root(["n"]), "n": node("n", "root", "user", "needlelong drop", 1_700_600_001)}
        body_mapping = {"root": root(["n"]), "n": node("n", "root", "user", "body", 1_700_600_002)}
        write_zip(
            z,
            [
                conv("tri-message-keep", "Message Keep", keep_mapping, "n", 1_700_600_000),
                conv("tri-message-drop", "Message Drop", drop_mapping, "n", 1_700_600_001),
                conv("tri-title", "Title Needlelong Unique", body_mapping, "n", 1_700_600_002),
            ],
        )
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        create_web_indexes(db)
        conn = connect_readonly(db)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            page = search_conversations(conn, parse_query("needlelong -drop", scope="message"), limit=10, offset=0)
            ids = {item["conversation_id"] for item in page["items"]}
            self.assertIn("tri-message-keep", ids)
            self.assertNotIn("tri-message-drop", ids)
            traced_sql = "\n".join(statements)
            self.assertIn("web_message_trigram", traced_sql)
            self.assertIn("web_message_trigram MATCH", traced_sql)
            self.assertNotIn("EXISTS (\n            SELECT 1\n            FROM web_message_trigram", traced_sql)
            statements.clear()
            title_page = search_conversations(conn, parse_query("needlelong", scope="title"), limit=10, offset=0)
            self.assertIn("tri-title", {item["conversation_id"] for item in title_page["items"]})
            self.assertTrue(any("web_title_trigram" in stmt for stmt in statements))
            statements.clear()
            short_page = search_conversations(conn, parse_query("bo"), limit=10, offset=0)
            self.assertGreaterEqual(short_page["total"], 1)
            self.assertFalse(any("web_message_trigram MATCH" in stmt for stmt in statements))
        finally:
            conn.close()

        db_no_index = base / "archive-no-index.db"
        self.assertEqual(main(["--db", str(db_no_index), "import", "--input", str(z), "--no-input-sha256"]), 0)
        conn = connect_readonly(db_no_index)
        try:
            fallback = search_conversations(conn, parse_query("needlelong -drop", scope="message"), limit=10, offset=0)
            fallback_ids = {item["conversation_id"] for item in fallback["items"]}
            self.assertIn("tri-message-keep", fallback_ids)
            self.assertNotIn("tri-message-drop", fallback_ids)
        finally:
            conn.close()

    def test_high_hit_message_search_paginates_before_payload_construction(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.search import parse_query, search_messages
        from chatgpt_export_archiver.web_db import create_web_indexes

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "many-common.zip"
        rows = []
        for idx in range(420):
            mapping = {
                "root": root(["n"]),
                "n": node("n", "root", "user" if idx % 2 else "assistant", f"python synthetic common body {idx} 逻辑严谨", 1_700_910_000 + idx),
            }
            rows.append(conv(f"common-{idx:04d}", f"Common {idx}", mapping, "n", 1_700_910_000 + idx))
        write_zip(z, rows)
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        create_web_indexes(db)
        conn = connect_readonly(db)
        real_payload = search_module._message_search_payload
        calls = {"count": 0}

        def counted_payload(*args, **kwargs):
            calls["count"] += 1
            return real_payload(*args, **kwargs)

        try:
            with mock.patch.object(search_module, "_message_search_payload", side_effect=counted_payload):
                page = search_messages(conn, parse_query("python"), limit=5, offset=200, order="relevance")
            self.assertEqual(page["total"], 420)
            self.assertEqual(len(page["items"]), 5)
            self.assertEqual(calls["count"], 5)
            calls["count"] = 0
            with mock.patch.object(search_module, "_message_search_payload", side_effect=counted_payload):
                zh_page = search_messages(conn, parse_query("逻辑严谨"), limit=5, offset=415, order="display")
            self.assertEqual(zh_page["total"], 420)
            self.assertEqual(len(zh_page["items"]), 5)
            self.assertEqual(calls["count"], 5)
            self.assertFalse(zh_page["has_more"])
        finally:
            conn.close()

    def test_around_node_message_window_only_builds_visible_payloads(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.search import get_messages

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "around.zip"
        db = base / "archive.db"
        mapping = {"root": root(["n0000"])}
        previous = "root"
        for idx in range(1200):
            node_id = f"n{idx:04d}"
            child = f"n{idx + 1:04d}" if idx < 1199 else None
            mapping[node_id] = node(node_id, previous, "user", f"around synthetic {idx}", 1_702_000_000 + idx, [child] if child else [])
            previous = node_id
        write_zip(z, [conv("around-long", "Around Long", mapping, "n1199", 1_702_000_000)])
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        conn = connect_readonly(db)
        real_payload = search_module._message_payload
        calls = {"count": 0}

        def counted_payload(*args, **kwargs):
            calls["count"] += 1
            return real_payload(*args, **kwargs)

        try:
            with mock.patch.object(search_module, "_message_payload", side_effect=counted_payload):
                page = get_messages(conn, "around-long", path="current", limit=20, offset=0, around_node_id="n1150")
            self.assertEqual(page["total"], 1201)
            self.assertEqual(len(page["items"]), 20)
            self.assertTrue(any(item["node_id"] == "n1150" for item in page["items"]))
            self.assertEqual(calls["count"], 20)
        finally:
            conn.close()

    def test_high_hit_web_index_search_total_late_pages_and_filters(self):
        from chatgpt_export_archiver.web_db import create_web_indexes

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "many-filtered.zip"
        rows = []
        for idx in range(260):
            text = f"python 逻辑严谨 shared body {idx}"
            role = "assistant" if idx % 2 else "user"
            if idx % 17 == 0:
                text += " excluded"
            mapping = {"root": root(["n"]), "n": node("n", "root", role, text, 1_701_010_000 + idx)}
            rows.append(conv(f"filtered-{idx:04d}", f"Filtered {idx}", mapping, "n", 1_701_010_000 + idx))
        write_zip(z, rows)
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        create_web_indexes(db)
        client = TestClient(create_app(db))
        conv_page = client.get("/api/conversations?q=python&limit=50&offset=200").json()
        self.assertEqual(conv_page["total"], 260)
        self.assertEqual(len(conv_page["items"]), 50)
        self.assertTrue(conv_page["has_more"])
        zh_tail = client.get("/api/conversations?q=%E9%80%BB%E8%BE%91%E4%B8%A5%E8%B0%A8&limit=50&offset=250").json()
        self.assertEqual(zh_tail["total"], 260)
        self.assertEqual(len(zh_tail["items"]), 10)
        self.assertFalse(zh_tail["has_more"])
        zh_miss = client.get("/api/conversations?q=%E6%97%A0%E6%AD%A4%E8%AF%8D%E6%9D%A1&limit=50").json()
        self.assertEqual(zh_miss["total"], 0)
        role_page = client.get("/api/search/messages?q=python&role=assistant&limit=100").json()
        self.assertEqual(role_page["total"], 130)
        excluded = client.get("/api/search/messages?q=python%20-excluded&limit=100").json()
        self.assertEqual(excluded["total"], 244)
        path_all = client.get("/api/search/messages?q=%E9%80%BB%E8%BE%91%E4%B8%A5%E8%B0%A8&path=all&limit=5").json()
        self.assertEqual(path_all["total"], 260)

    def test_high_hit_search_without_web_index_uses_sql_pagination_fallback(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "many-no-web-index.zip"
        rows = []
        for idx in range(180):
            mapping = {"root": root(["n"]), "n": node("n", "root", "user", f"python fallback body {idx}", 1_701_020_000 + idx)}
            rows.append(conv(f"fallback-{idx:04d}", f"Fallback {idx}", mapping, "n", 1_701_020_000 + idx))
        write_zip(z, rows)
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        messages = client.get("/api/search/messages?q=python&limit=25&offset=150").json()
        self.assertEqual(messages["total"], 180)
        self.assertEqual(len(messages["items"]), 25)
        conversations = client.get("/api/conversations?q=python&limit=25&offset=150").json()
        self.assertEqual(conversations["total"], 180)
        self.assertEqual(len(conversations["items"]), 25)

    def test_message_pagination_does_not_read_entire_conversation_for_plain_pages(self):
        from chatgpt_export_archiver.search import get_messages

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "paged.zip"
        db = base / "archive.db"
        mapping = {"root": root(["n000"])}
        previous = "root"
        for idx in range(500):
            node_id = f"n{idx:03d}"
            child = f"n{idx + 1:03d}" if idx < 499 else None
            mapping[node_id] = node(node_id, previous, "user", f"paged synthetic {idx}", 1_700_700_000 + idx, [child] if child else [])
            previous = node_id
        write_zip(z, [conv("paged-conversation", "Paged", mapping, "n499", 1_700_700_000)])
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        conn = connect_readonly(db)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            page = get_messages(conn, "paged-conversation", path="all", limit=5, offset=10)
            self.assertEqual(page["total"], 501)
            self.assertEqual(len(page["items"]), 5)
            metadata_selects = [stmt for stmt in statements if "FROM conversation_nodes" in stmt and "raw_message_json" not in stmt]
            hydration_selects = [stmt for stmt in statements if "FROM conversation_nodes" in stmt and "raw_message_json" in stmt]
            self.assertTrue(any("LIMIT 5 OFFSET 10" in stmt for stmt in metadata_selects))
            page_hydration = [stmt for stmt in hydration_selects if "node_id IN (" in stmt]
            self.assertEqual(len(page_hydration), 1)
            self.assertEqual(page_hydration[0].count("'n0"), 5)
            self.assertFalse(any("SELECT raw_message_json" in stmt for stmt in hydration_selects))
        finally:
            conn.close()

    def test_import_after_web_index_invalidates_stale_normalized_indexes(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        old_zip = base / "old.zip"
        new_zip = base / "new.zip"
        db = base / "archive.db"
        old_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "old needle", 1_700_900_001),
        }
        new_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "new needle", 1_700_900_001),
        }
        write_zip(old_zip, [conv("stale-index", "Old Title", old_mapping, "u", 1_700_900_000)])
        write_zip(new_zip, [conv("stale-index", "New Title", new_mapping, "u", 1_700_900_000)])
        self.assertEqual(main(["--db", str(db), "import", "--input", str(old_zip), "--no-input-sha256"]), 0)
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        client = TestClient(create_app(db))
        self.assertTrue(client.get("/api/health").json()["web_normalized_indexed"])
        self.assertEqual(client.get("/api/conversations?q=old&limit=5").json()["total"], 1)
        self.assertEqual(client.get("/api/conversations?q=title:Old&limit=5").json()["total"], 1)
        self.assertEqual(main(["--db", str(db), "import", "--input", str(new_zip), "--no-input-sha256"]), 0)
        health = client.get("/api/health").json()
        self.assertFalse(health["web_normalized_indexed"])
        self.assertEqual(client.get("/api/conversations?q=old&limit=5").json()["total"], 0)
        self.assertEqual(client.get("/api/conversations?q=new&limit=5").json()["total"], 1)
        self.assertEqual(client.get("/api/conversations?q=title:Old&limit=5").json()["total"], 0)
        self.assertEqual(client.get("/api/conversations?q=title:New&limit=5").json()["total"], 1)

    def test_incremental_import_then_web_api_sees_new_and_updated_conversations(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        old_zip = base / "old.zip"
        new_zip = base / "new.zip"
        db = base / "archive.db"
        old_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "old synthetic question", 1_730_000_001, ["a"]),
            "a": node("a", "u", "assistant", "old synthetic answer", 1_730_000_002),
        }
        new_mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "updated synthetic question", 1_730_000_001, ["a"]),
            "a": node("a", "u", "assistant", "updated synthetic answer", 1_730_000_002, ["extra"]),
            "extra": node("extra", "a", "assistant", "extra synthetic node", 1_730_000_003),
        }
        write_zip(
            old_zip,
            [
                conv("api-keep", "API Keep", {"root": root(["u"]), "u": node("u", "root", "user", "stable synthetic text", 1_730_001_000)}, "u", 1_730_001_000),
                conv("api-change", "API Change", old_mapping, "a", 1_730_000_000),
            ],
        )
        write_zip(
            new_zip,
            [
                conv("api-keep", "API Keep", {"root": root(["u"]), "u": node("u", "root", "user", "stable synthetic text", 1_730_001_000)}, "u", 1_730_001_000),
                conv("api-change", "API Change", new_mapping, "extra", 1_730_000_000),
                conv("api-new", "API New", {"root": root(["u"]), "u": node("u", "root", "user", "new synthetic text", 1_730_002_000)}, "u", 1_730_002_000),
            ],
        )
        self.assertEqual(main(["--db", str(db), "import", "--input", str(old_zip), "--no-input-sha256"]), 0)
        self.assertEqual(main(["--db", str(db), "import", "--input", str(new_zip), "--no-input-sha256"]), 0)
        self.assertEqual(main(["--db", str(db), "web-index"]), 0)
        client = TestClient(create_app(db))
        stats = client.get("/api/stats").json()
        self.assertEqual(stats["conversations"], 3)
        self.assertEqual(stats["nodes"], 8)
        page = client.get("/api/conversations?limit=10&sort=title").json()
        self.assertEqual(page["total"], 3)
        ids = {item["conversation_id"] for item in page["items"]}
        self.assertEqual(ids, {"api-keep", "api-change", "api-new"})
        changed = client.get("/api/conversations/api-change").json()
        self.assertEqual(changed["node_count"], 4)
        messages = client.get("/api/conversations/api-change/messages?limit=10").json()
        self.assertEqual(messages["total"], 3)
        self.assertTrue(any(item["node_id"] == "extra" for item in messages["items"]))
        search = client.get("/api/conversations?q=extra%20synthetic&limit=5").json()
        self.assertEqual(search["items"][0]["conversation_id"], "api-change")

    def test_export_endpoint(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        response = client.get("/api/conversations/web-1/export?format=md")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/markdown", response.headers["content-type"])
        self.assertIn("Python SQLite Archive", response.text)

    def test_export_endpoint_content_disposition_is_header_safe(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "header.zip"
        cid = "bad \" 空 id"
        write_zip(z, [conv(cid, "Header Safe", {"root": root(["u"]), "u": node("u", "root", "user", "body", 1_701_000_001)}, "u", 1_701_000_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        response = client.get(f"/api/conversations/{quote(cid, safe='')}/export?format=md")
        self.assertEqual(response.status_code, 200)
        disposition = response.headers["content-disposition"]
        self.assertIn("filename=", disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertNotIn("\n", disposition)
        self.assertNotIn("\r", disposition)
        self.assertNotIn('bad "', disposition)

    def test_content_disposition_handles_unicode_reserved_and_edge_basenames(self):
        from chatgpt_export_archiver.web_api import _content_disposition

        cases = {
            "中文.md": ("download.md", "%E4%B8%AD%E6%96%87.md"),
            "日本語.txt": ("download.txt", "%E6%97%A5%E6%9C%AC%E8%AA%9E.txt"),
            "café.md": ("caf.md", "caf%C3%A9.md"),
            "🔥.txt": ("download.txt", "%F0%9F%94%A5.txt"),
            ".md": ("download.md", "download.md"),
            "CON.md": ("_CON.md", "CON.md"),
            "AUX.txt": ("_AUX.txt", "AUX.txt"),
            "../bad\r\nname.md": ("badname.md", "badname.md"),
            ("long" * 100) + ".md": (("long" * 100)[:77] + ".md", ("long" * 100) + ".md"),
        }
        for raw, (ascii_name, utf8_fragment) in cases.items():
            with self.subTest(raw=raw[:20]):
                header = _content_disposition(raw)
                self.assertIn(f'filename="{ascii_name}"', header)
                self.assertIn("filename*=UTF-8''", header)
                self.assertIn(utf8_fragment, header)
                self.assertNotIn("\r", header)
                self.assertNotIn("\n", header)
                self.assertNotIn(".md.md", header)
                self.assertNotIn(".txt.txt", header)

    def test_tool_roles_are_internal(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "tool.zip"
        mapping = {
            "root": root(["tool"]),
            "tool": node("tool", "root", "tool_system", "tool system alias output", 1_701_100_001, ["visible"]),
            "visible": node("visible", "tool", "assistant", "visible alias response", 1_701_100_002),
        }
        write_zip(z, [conv("tool-role", "Tool Role", mapping, "tool", 1_701_100_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        self.addCleanup(client.close)
        page = client.get("/api/conversations/tool-role/messages?path=current&include_internal=true").json()
        by_id = {item["node_id"]: item for item in page["items"]}
        self.assertEqual(by_id["tool"]["role"], "tool_system")
        self.assertTrue(by_id["tool"]["is_internal"])
        self.assertEqual(page["internal_hidden_count"], 1)
        self.assertEqual(page["technical_hidden_count"], 1)

        conn = connect_readonly(db)
        try:
            rows = conn.execute(
                "SELECT node_id, parent_node_id, children_json, message_id, role, author_name, create_time, update_time, content_type, content_text, content_hash, is_on_current_path, raw_message_json FROM conversation_nodes WHERE conversation_id = ? AND is_on_current_path = 1",
                ("tool-role",),
            ).fetchall()
            row_counts = _message_visibility_counts(rows)
            fast_counts = _message_visibility_counts_for_path(conn, "tool-role", "current")
            self.assertEqual(row_counts, fast_counts)
            self.assertTrue(_is_internal_message("tool_system", "text", "tool system alias output"))
        finally:
            conn.close()

        for role in ("tool/system", "tool_system"):
            with self.subTest(role=role):
                messages = client.get(f"/api/search/messages?q=alias&role={quote(role)}&conversation_id=tool-role&path=current").json()
                self.assertEqual([item["node_id"] for item in messages["items"]], ["tool"])
                conversations = client.get(f"/api/conversations?q=alias&role={quote(role)}&path=current").json()
                self.assertEqual([item["conversation_id"] for item in conversations["items"]], ["tool-role"])
                reader = client.get(f"/api/conversations/tool-role/messages?q=alias&role={quote(role)}&path=current&include_internal=true").json()
                self.assertTrue(by_id := {item["node_id"]: item for item in reader["items"]})
                self.assertTrue(by_id["tool"]["highlight_ranges"])

        assistant_reader = client.get("/api/conversations/tool-role/messages?q=alias&role=assistant&path=current&include_internal=true").json()
        self.assertFalse({item["node_id"]: item for item in assistant_reader["items"]}["tool"]["highlight_ranges"])

    def test_readonly_connection_can_be_used_in_worker_thread(self):
        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = connect_readonly(db)
        errors = []

        def worker():
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 3)
            except Exception as exc:  # pragma: no cover - failure detail propagated below
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        conn.close()
        self.assertEqual(errors, [])

    def test_web_job_recovers_optional_web_index_postcheck_failure(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        upload = base / "upload.zip"
        upload.write_bytes(b"synthetic")
        manager = ImportJobManager(base / "archive.db")
        job = ImportJob("job", base / "archive.db", upload, "synthetic.zip", upload.stat().st_size)
        optional_fail = {"ok": False, "optional_web_index_error": True, "integrity_check": "malformed inverted index for FTS5 table main.web_message_trigram"}
        ok = {"ok": True, "optional_web_index_error": False, "integrity_check": "ok"}
        with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", return_value={"summary": {"valid_conversations": 1}}), \
             mock.patch("chatgpt_export_archiver.web_jobs.connect"), \
             mock.patch("chatgpt_export_archiver.web_jobs.verify_database", side_effect=[optional_fail, ok]), \
             mock.patch("chatgpt_export_archiver.web_jobs.get_stats", return_value={"conversations": 1}), \
             mock.patch("chatgpt_export_archiver.web_jobs.create_web_indexes", return_value={"indexed_messages": 1}) as web_index:
            manager._run_job(job)
        self.assertEqual(job.status, "succeeded")
        self.assertTrue(job.web_index["recovered_optional_web_index"])
        self.assertEqual(web_index.call_count, 1)

    def test_web_job_exposes_bounded_index_build_progress(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload = base / "upload.zip"
            upload.write_bytes(b"synthetic")
            manager = ImportJobManager(base / "archive.db")
            job = ImportJob("job", base / "archive.db", upload, "synthetic.zip", upload.stat().st_size)
            observed: list[dict[str, Any]] = []

            def build_index(_path, *, progress_callback, **_kwargs):
                progress_callback("scan_normalize_messages", {
                    "build_stage": "scan_normalize_messages",
                    "processed": 100,
                    "total": 250,
                    "complete": False,
                    "batch_size": 100,
                })
                observed.append(job.snapshot())
                return {"indexed_messages": 250, "atomic_publish": True}

            with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", return_value={"summary": {"valid_conversations": 1}}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.connect"), \
                 mock.patch("chatgpt_export_archiver.web_jobs.verify_database", return_value={"ok": True}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.get_stats", return_value={"conversations": 1}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.create_web_indexes", side_effect=build_index):
                manager._run_job(job)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["stage"], "web-index")
            self.assertEqual(observed[0]["web_index"]["status"], "building")
            self.assertEqual(observed[0]["web_index"]["processed"], 100)
            self.assertEqual(observed[0]["web_index"]["total"], 250)
            self.assertEqual(job.status, "succeeded")
            self.assertTrue(job.web_index["atomic_publish"])

    def test_web_job_cancel_request_reaches_atomic_index_builder(self):
        from chatgpt_export_archiver.web_db import WebIndexBuildCancelled
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload = base / "upload.zip"
            upload.write_bytes(b"synthetic")
            manager = ImportJobManager(base / "archive.db")
            job_id = "a" * 32
            job = ImportJob(job_id, base / "archive.db", upload, "synthetic.zip", upload.stat().st_size)
            with manager._lock:
                manager._jobs[job_id] = job
                manager._running_job_id = job_id

            def cancel_during_build(_path, *, progress_callback, cancel_check):
                progress_callback("scan_normalize_messages", {"processed": 1, "total": 2, "complete": False})
                self.assertFalse(cancel_check())
                requested, accepted = manager.request_web_index_cancel(job_id)
                self.assertIs(requested, job)
                self.assertTrue(accepted)
                self.assertTrue(cancel_check())
                raise WebIndexBuildCancelled()

            with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", return_value={"summary": {"valid_conversations": 1}}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.connect"), \
                 mock.patch("chatgpt_export_archiver.web_jobs.verify_database", return_value={"ok": True}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.get_stats", return_value={"conversations": 1}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.create_web_indexes", side_effect=cancel_during_build):
                manager._run_job(job)

            snapshot = job.snapshot()
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["stage"], "web_index_cancelled")
            self.assertEqual(snapshot["outcome"], "web_index_cancelled")
            self.assertTrue(snapshot["canonical_commit_succeeded"])
            self.assertTrue(snapshot["web_index_cancel_requested"])
            self.assertTrue(snapshot["web_index_cancelled"])
            self.assertEqual(snapshot["web_index"]["status"], "cancelled")
            self.assertIsNone(snapshot["error_code"])

    def test_web_index_cancel_endpoint_has_bounded_state_contract(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manager = ImportJobManager(base / "archive.db")
            job_id = "b" * 32
            job = ImportJob(job_id, base / "archive.db", base / "upload.zip", "synthetic.zip", 1)
            job.status = "running"
            job.stage = "web-index"
            job.web_index = {"status": "building", "processed": 1, "total": 2}
            with manager._lock:
                manager._jobs[job_id] = job
                manager._running_job_id = job_id
            with mock.patch("chatgpt_export_archiver.web_app.ImportJobManager", return_value=manager):
                client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
            self.addCleanup(client.close)

            response = client.post(f"/api/import/jobs/{job_id}/web-index/cancel")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["web_index_cancel_requested"])
            self.assertEqual(payload["web_index"]["status"], "cancelling")
            self.assertNotIn(str(base), response.text)

            repeated = client.post(f"/api/import/jobs/{job_id}/web-index/cancel")
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(client.post("/api/import/jobs/invalid/web-index/cancel").status_code, 400)
            job.status = "succeeded"
            job.stage = "succeeded"
            refused = client.post(f"/api/import/jobs/{job_id}/web-index/cancel")
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(refused.json()["detail"], "web_index_not_cancellable")

    def test_web_job_marks_postcheck_failed_without_rollback_implication(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        upload = base / "upload.zip"
        upload.write_bytes(b"synthetic")
        manager = ImportJobManager(base / "archive.db")
        job = ImportJob("job", base / "archive.db", upload, "synthetic.zip", upload.stat().st_size)
        core_fail = {"ok": False, "optional_web_index_error": False, "integrity_check": "row missing from index conversations"}
        with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", return_value={"summary": {"valid_conversations": 1, "import_run_id": 1}}), \
             mock.patch("chatgpt_export_archiver.web_jobs.connect"), \
             mock.patch("chatgpt_export_archiver.web_jobs.verify_database", return_value=core_fail), \
             mock.patch("chatgpt_export_archiver.web_jobs.create_web_indexes") as web_index:
            manager._run_job(job)
        self.assertEqual(job.status, "postcheck_failed")
        self.assertEqual(job.error, "verify_failed")
        self.assertEqual(job.error_code, "verify_failed")
        self.assertEqual(job.outcome, "verify_failed")
        self.assertTrue(job.canonical_commit_succeeded)
        self.assertEqual(job.summary["import_run_id"], 1)
        web_index.assert_not_called()

    def test_web_job_distinguishes_stats_and_web_index_failures_after_commit(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        for failing_stage in ("stats", "web_index"):
            with self.subTest(failing_stage=failing_stage), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                upload = base / "upload.zip"
                upload.write_bytes(b"synthetic")
                manager = ImportJobManager(base / "archive.db")
                job = ImportJob("job", base / "archive.db", upload, "synthetic.zip", upload.stat().st_size)
                stats_effect = RuntimeError("synthetic") if failing_stage == "stats" else {"conversations": 1}
                index_effect = RuntimeError("synthetic") if failing_stage == "web_index" else {"indexed_messages": 1}
                with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", return_value={"summary": {"import_run_id": 1}}), \
                     mock.patch("chatgpt_export_archiver.web_jobs.connect"), \
                     mock.patch("chatgpt_export_archiver.web_jobs.verify_database", return_value={"ok": True}), \
                     mock.patch("chatgpt_export_archiver.web_jobs.get_stats", side_effect=stats_effect), \
                     mock.patch("chatgpt_export_archiver.web_jobs.create_web_indexes", side_effect=index_effect):
                    manager._run_job(job)
                expected = f"{failing_stage}_failed"
                self.assertEqual(job.status, "postcheck_failed")
                self.assertEqual(job.outcome, expected)
                self.assertEqual(job.error_code, expected)
                self.assertTrue(job.canonical_commit_succeeded)

    def test_web_job_preserves_specific_web_index_capacity_code(self):
        from chatgpt_export_archiver.web_db import WebIndexBuildError
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload = base / "upload.zip"
            upload.write_bytes(b"synthetic")
            manager = ImportJobManager(base / "archive.db")
            job = ImportJob("job", base / "archive.db", upload, "synthetic.zip", upload.stat().st_size)
            with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", return_value={"summary": {"import_run_id": 1}}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.connect"), \
                 mock.patch("chatgpt_export_archiver.web_jobs.verify_database", return_value={"ok": True}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.get_stats", return_value={"conversations": 1}), \
                 mock.patch(
                     "chatgpt_export_archiver.web_jobs.create_web_indexes",
                     side_effect=WebIndexBuildError("web_index_disk_space_insufficient"),
                 ):
                manager._run_job(job)
            self.assertEqual(job.status, "postcheck_failed")
            self.assertEqual(job.outcome, "web_index_failed")
            self.assertEqual(job.error_code, "web_index_disk_space_insufficient")
            self.assertTrue(job.canonical_commit_succeeded)

    def test_web_job_preserves_postcommit_cleanup_warning_on_success(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload = base / "upload.zip"
            upload.write_bytes(b"synthetic")
            manager = ImportJobManager(base / "archive.db")
            job = ImportJob("job", base / "archive.db", upload, "synthetic.zip", upload.stat().st_size)
            result = {"summary": {"import_run_id": 1}, "summary_update_after_commit_failed": "synthetic"}
            with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", return_value=result), \
                 mock.patch("chatgpt_export_archiver.web_jobs.connect"), \
                 mock.patch("chatgpt_export_archiver.web_jobs.verify_database", return_value={"ok": True}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.get_stats", return_value={"conversations": 1}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.create_web_indexes", return_value={"indexed_messages": 1}):
                manager._run_job(job)
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.cleanup_warning, "summary_update_after_commit_failed")
            self.assertEqual(job.cleanup_warnings, [{
                "code": "summary_update_after_commit_failed",
                "error_type": "synthetic",
                "path_kind": "import_summary",
            }])
            self.assertTrue(job.canonical_commit_succeeded)

    def test_web_job_accumulates_cleanup_warnings_without_changing_success(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload = base / "upload.zip"
            upload.write_bytes(b"synthetic")
            manager = ImportJobManager(base / "archive.db")
            job = ImportJob("job", base / "archive.db", upload, "synthetic.zip", upload.stat().st_size)
            result = {
                "summary": {"import_run_id": 1},
                "summary_update_after_commit_failed": "OperationalError",
                "import_connection_close_failed": "OSError",
            }
            cleanup_failure = {
                "ok": False,
                "error_type": "PermissionError",
                "path_still_exists": True,
                "partial_cleanup": True,
            }
            with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", return_value=result), \
                 mock.patch("chatgpt_export_archiver.web_jobs.connect"), \
                 mock.patch("chatgpt_export_archiver.web_jobs.verify_database", return_value={"ok": True}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.get_stats", return_value={"conversations": 1}), \
                 mock.patch("chatgpt_export_archiver.web_jobs.create_web_indexes", return_value={"indexed_messages": 1}), \
                 mock.patch.object(Path, "unlink", side_effect=PermissionError("private path")), \
                 mock.patch("chatgpt_export_archiver.web_jobs.cleanup_upload_dir", return_value=cleanup_failure):
                manager._run_job(job)
            snapshot = job.snapshot()
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertTrue(snapshot["canonical_commit_succeeded"])
            self.assertEqual(snapshot["cleanup_warning"], "summary_update_after_commit_failed")
            self.assertEqual(
                [item["code"] for item in snapshot["cleanup_warnings"]],
                [
                    "summary_update_after_commit_failed",
                    "import_connection_close_failed",
                    "upload_file_unlink_failed",
                    "upload_directory_cleanup_failed",
                ],
            )
            self.assertNotIn(str(base), json.dumps(snapshot))
            self.assertFalse(manager.has_running_job())

    def test_web_job_error_is_sanitized(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        upload = base / "upload.zip"
        upload.write_bytes(b"synthetic")
        manager = ImportJobManager(base / "archive.db", log_level="debug")
        job = ImportJob("job", base / "archive.db", upload, "private-upload.zip", upload.stat().st_size)
        unsafe_message = "synthetic failure /private/path/private-upload.zip"
        with mock.patch("chatgpt_export_archiver.web_jobs.run_import_pipeline", side_effect=RuntimeError(unsafe_message)):
            manager._run_job(job)
        snapshot = job.snapshot()
        payload = json.dumps(snapshot)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["filename"], "private-upload.zip")
        self.assertEqual(snapshot["error"], "import_transaction_failed")
        self.assertEqual(snapshot["error_code"], "import_transaction_failed")
        self.assertNotIn(unsafe_message, payload)
        self.assertNotIn("/private/path", payload)
        self.assertNotIn("private-upload.zip", json.dumps({"error": snapshot["error"], "log_tail": snapshot["log_tail"]}))

    def test_web_job_history_prunes_old_terminal_jobs_but_keeps_running_and_recent(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        manager = ImportJobManager(base / "archive.db", history_limit=2, history_ttl_seconds=10)
        now = time.time()
        running = ImportJob("running", base / "archive.db", base / "running.zip", "running.zip", 0, status="running")
        running.created_at = now - 1000
        old_1 = ImportJob("old1", base / "archive.db", base / "old1.zip", "old1.zip", 0, status="succeeded")
        old_1.created_at = now - 1000
        old_1.finished_at = now - 1000
        old_2 = ImportJob("old2", base / "archive.db", base / "old2.zip", "old2.zip", 0, status="failed")
        old_2.created_at = now - 900
        old_2.finished_at = now - 900
        recent_1 = ImportJob("recent1", base / "archive.db", base / "recent1.zip", "recent1.zip", 0, status="succeeded")
        recent_1.created_at = now - 2
        recent_1.finished_at = now - 2
        recent_2 = ImportJob("recent2", base / "archive.db", base / "recent2.zip", "recent2.zip", 0, status="postcheck_failed")
        recent_2.created_at = now - 1
        recent_2.finished_at = now - 1
        manager._jobs = {job.job_id: job for job in [running, old_1, old_2, recent_1, recent_2]}
        manager._running_job_id = "running"
        listed = manager.list_jobs()
        ids = {job.job_id for job in listed}
        self.assertIn("running", ids)
        self.assertIn("recent1", ids)
        self.assertIn("recent2", ids)
        self.assertNotIn("old1", manager._jobs)
        self.assertNotIn("old2", manager._jobs)
        self.assertEqual([job.job_id for job in listed[:2]], ["recent2", "recent1"])

    def test_web_job_history_hard_limit_applies_inside_ttl(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        manager = ImportJobManager(base / "archive.db", history_limit=3, history_ttl_seconds=86_400)
        now = time.time()
        jobs = []
        for idx in range(6):
            job = ImportJob(f"done{idx}", base / "archive.db", base / f"done{idx}.zip", "zip", 0, status="succeeded")
            job.created_at = now - idx
            job.finished_at = now - idx
            jobs.append(job)
        running = ImportJob("running", base / "archive.db", base / "running.zip", "zip", 0, status="running")
        running.created_at = now - 10_000
        manager._jobs = {job.job_id: job for job in [running, *jobs]}
        manager._running_job_id = "running"
        listed = manager.list_jobs()
        self.assertIn("running", manager._jobs)
        self.assertEqual({job_id for job_id in manager._jobs if job_id.startswith("done")}, {"done0", "done1", "done2"})
        self.assertEqual([job.job_id for job in listed[:3]], ["done0", "done1", "done2"])

    def test_web_job_history_ttl_prunes_when_under_limit(self):
        from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        manager = ImportJobManager(base / "archive.db", history_limit=10, history_ttl_seconds=5)
        now = time.time()
        old = ImportJob("old", base / "archive.db", base / "old.zip", "zip", 0, status="succeeded")
        old.created_at = now - 100
        old.finished_at = now - 100
        recent = ImportJob("recent", base / "archive.db", base / "recent.zip", "zip", 0, status="failed")
        recent.created_at = now - 1
        recent.finished_at = now - 1
        manager._jobs = {job.job_id: job for job in [old, recent]}
        listed = manager.list_jobs()
        self.assertNotIn("old", manager._jobs)
        self.assertIn("recent", manager._jobs)
        self.assertEqual([job.job_id for job in listed], ["recent"])

    def test_web_job_history_env_invalid_values_are_safe(self):
        from chatgpt_export_archiver import web_jobs

        with mock.patch.dict(os.environ, {
            web_jobs.JOB_HISTORY_LIMIT_ENV: "not-a-number",
            web_jobs.JOB_HISTORY_TTL_ENV: "  ",
        }):
            with self.assertLogs("chatgpt_export_archiver.web_jobs", level="WARNING") as logs:
                manager = web_jobs.ImportJobManager(Path("archive.db"))
        payload = "\n".join(logs.output)
        self.assertEqual(manager.history_limit, web_jobs.DEFAULT_JOB_HISTORY_LIMIT)
        self.assertEqual(manager.history_ttl_seconds, web_jobs.DEFAULT_JOB_HISTORY_TTL_SECONDS)
        self.assertIn(web_jobs.JOB_HISTORY_LIMIT_ENV, payload)
        self.assertIn(web_jobs.JOB_HISTORY_TTL_ENV, payload)
        self.assertNotIn("not-a-number", payload)

    def test_raw_query_modifier_validation_produces_errors(self):
        from chatgpt_export_archiver.search import parse_query

        parsed = parse_query("role:banana")
        self.assertIn("invalid_role:banana", parsed.errors)
        parsed = parse_query("path:banana")
        self.assertIn("invalid_path:banana", parsed.errors)
        parsed = parse_query("scope:banana")
        self.assertIn("invalid_scope:banana", parsed.errors)
        parsed = parse_query("before:bad after:bad")
        self.assertEqual(parsed.errors.count("invalid_before"), 1)
        self.assertEqual(parsed.errors.count("invalid_after"), 1)

        parsed = parse_query('role:"assistant"')
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.role, "assistant")
        parsed = parse_query("role:assistant")
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.role, "assistant")
        parsed = parse_query("role:tool_system")
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.role, "tool/system")
        parsed = parse_query('source:"conversations 1.json"')
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.source, "conversations 1.json")
        parsed = parse_query('title:"foo bar"')
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.title, "foo bar")
        parsed = parse_query('-"foo bar"')
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.exclude, ["foo bar"])
        parsed = parse_query("--no-input-sha256")
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.terms, ["--no-input-sha256"])
        for query in ("-role:user", "-source:x", "-path:all", "-scope:title"):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertEqual(parsed.errors, [f"negated_modifier_not_supported:{query[1:].split(':', 1)[0]}"])

    def test_raw_query_modifier_errors_are_propagated_by_api(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        for endpoint in ("/api/conversations", "/api/search"):
            for query in ("role:banana", "path:banana", "scope:banana", "-role:user"):
                with self.subTest(endpoint=endpoint, query=query):
                    response = client.get(f"{endpoint}?q={quote(query)}")
                    self.assertEqual(response.status_code, 400)
                    detail = response.json()["detail"]
                    self.assertEqual(detail["code"], "invalid_query")
                    self.assertTrue(detail["reasons"])

    def test_unknown_colon_tokens_are_preserved_as_literals(self):
        from chatgpt_export_archiver.search import parse_query

        for query, expected_terms, expected_phrases, expected_exclude in (
            ("foo:bar", ["foo:bar"], [], []),
            ("model:gpt-5.5", ["model:gpt-5.5"], [], []),
            ("http://example.com", ["http://example.com"], [], []),
            ('url:"foo bar"', ["url:foo bar"], [], []),
            ("-foo:bar", [], [], ["foo:bar"]),
            ("gpt-5.5", ["gpt-5.5"], [], []),
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertEqual(parsed.errors, [])
                self.assertEqual(parsed.terms, expected_terms)
                self.assertEqual(parsed.phrases, expected_phrases)
                if expected_exclude:
                    self.assertEqual(parsed.exclude, expected_exclude)

        parsed = parse_query('source:"conversations 1.json"')
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.source, "conversations 1.json")
        parsed = parse_query("--no-input-sha256")
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.terms, ["--no-input-sha256"])
        parsed = parse_query("role:assistant")
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.role, "assistant")

    def test_api_string_length_limits_return_400(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        long_q = "x" * 501
        self.assertEqual(client.get(f"/api/conversations?q={quote(long_q)}").status_code, 422)
        long_title = "x" * 201
        self.assertEqual(client.get(f"/api/conversations?title={quote(long_title)}").status_code, 422)
        long_exact = "x" * 301
        self.assertEqual(client.get(f"/api/conversations?exact={quote(long_exact)}").status_code, 422)
        long_exclude = "x" * 201
        self.assertEqual(client.get(f"/api/conversations?exclude={quote(long_exclude)}").status_code, 422)
        long_source = "x" * 201
        self.assertEqual(client.get(f"/api/conversations?source={quote(long_source)}").status_code, 422)
        long_suggest = "x" * 101
        self.assertEqual(client.get(f"/api/search/suggest?q={quote(long_suggest)}").status_code, 422)
        long_id = "x" * 513
        self.assertEqual(client.get(f"/api/conversations/{quote(long_id)}").status_code, 422)
        self.assertEqual(client.get(f"/api/conversations?selected_id={quote(long_id)}").status_code, 200)

    def test_imported_maximum_ids_are_addressable_across_reader_raw_display_export_and_selected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            conversation_id = "c" * 512
            node_id = "n" * 512
            message_id = "m" * 512
            mapping = {
                "root": root([node_id]),
                node_id: {
                    "id": node_id,
                    "parent": "root",
                    "children": [],
                    "message": {
                        "id": message_id,
                        "author": {"role": "user"},
                        "create_time": 1_700_000_001,
                        "update_time": 1_700_000_001,
                        "content": {"content_type": "text", "parts": ["synthetic maximum id body"]},
                        "metadata": {},
                    },
                },
            }
            row = conv(conversation_id, "Maximum IDs", mapping, node_id, 1_700_000_000)
            row["conversation_id"] = "exported-maximum"
            archive = base / "input.zip"
            write_zip(archive, [row])
            db = base / "archive.db"
            self.assertEqual(main(["--db", str(db), "import", "--input", str(archive), "--no-input-sha256"]), 0)
            client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
            self.addCleanup(client.close)
            cid = quote(conversation_id, safe="")
            nid = quote(node_id, safe="")
            self.assertEqual(client.get(f"/api/conversations/{cid}").status_code, 200)
            selected = client.get(f"/api/conversations?limit=1&offset=99&selected_id={cid}").json()
            self.assertEqual(selected["selected_item"]["conversation_id"], conversation_id)
            messages = client.get(f"/api/conversations/{cid}/messages?path=all&around_node_id={nid}").json()
            self.assertEqual(messages["items"][0]["node_id"], node_id)
            self.assertEqual(client.get(f"/api/conversations/{cid}/messages/{nid}/raw").status_code, 200)
            display = client.get(f"/api/conversations/{cid}/messages/{nid}/display?offset=0&limit=1024").json()
            self.assertEqual(display["display_text"], "synthetic maximum id body")
            self.assertEqual(client.get(f"/api/conversations/{cid}/export?format=txt&path=all").status_code, 200)

    def test_cli_search_does_not_enforce_api_length_limits(self):
        from chatgpt_export_archiver.search import parse_query

        parsed = parse_query("x" * 600, enforce_api_limits=False)
        self.assertNotIn("q_too_long", parsed.errors)
        parsed_api = parse_query("x" * 600, enforce_api_limits=True)
        self.assertIn("q_too_long", parsed_api.errors)

    def test_schema_endpoint_includes_new_fields(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        schema = client.get("/api/schema").json()
        self.assertEqual(schema["version"], 7)
        self.assertEqual(schema["versions"]["required_database_schema_version"], 5)
        self.assertEqual(schema["versions"]["optional_web_index_format_version"], "6")
        self.assertIn("include_internal", json.dumps(schema))
        self.assertIn("hidden_counts", json.dumps(schema))
        self.assertIn("match_mode", json.dumps(schema))
        self.assertIn("selected_in_results", json.dumps(schema))
        self.assertIn("selected_item", json.dumps(schema))
        self.assertIn("count_total", json.dumps(schema))
        self.assertIn("technical_hidden_count", json.dumps(schema))
        self.assertEqual(schema["messages"]["limits"]["around_node_id"], 16 * 1024)
        self.assertEqual(schema["conversations"]["limits"]["selected_id"], 16 * 1024)
        self.assertEqual(schema["id_addressing"]["new_import_id_max_chars"], 512)
        self.assertEqual(schema["import_contract"]["max_element_utf8_bytes"], 32 * 1024 * 1024)
        self.assertEqual(schema["import_contract"]["max_element_decoded_chars"], 32 * 1024 * 1024)
        self.assertEqual(
            schema["import_contract"]["json_limits"]["max_conversation_element_scalar_count"],
            2_500_000,
        )
        self.assertEqual(
            schema["import_contract"]["json_limits"]["max_legacy_sanitizer_scalar_count"],
            250_000,
        )
        self.assertEqual(schema["request_validation"]["detail_code"], "invalid_request")
        self.assertEqual(schema["request_validation"]["item_fields"], ["location", "field", "code"])
        self.assertEqual(schema["raw"]["units"].split(";")[0], "raw_size is always exact UTF-8 bytes for compatibility")
        self.assertEqual(schema["export"]["resource_limits"]["archive_max_conversations"], 1_000_000)
        self.assertEqual(schema["export"]["resource_limits"]["effective_current_batch_rows"], 20_000)
        self.assertEqual(schema["export"]["resource_limits"]["effective_current_batch_input_bytes"], 64 * 1024 * 1024)
        self.assertIn("conversation_json_element_too_large", schema["jobs"]["failure_codes"])
        self.assertIn("visible-only reader pagination collection", schema["messages"]["around_node_id"]["description"])
        self.assertEqual(
            schema["messages"]["around_node_id"]["response"],
            ["around_target_found", "around_target_in_effective_collection", "around_target_in_requested_collection", "around_target_visible", "around_target_applied"],
        )
        self.assertIn("effective all collection", schema["messages"]["around_node_id"]["description"])
        self.assertEqual(schema["conversations"]["detail_endpoint"], "/api/by-id/conversation?conversation_id=...")
        self.assertEqual(schema["export"]["copy_endpoint"], "/api/by-id/copy?conversation_id=...")
        self.assertIn("default false", schema["export"]["include_internal"])
        self.assertIn("bounded server-side node batches", schema["export"]["streaming"])
        self.assertEqual(
            schema["web_index_build"]["stages"],
            [
                "scan_normalize_messages",
                "normalize_titles",
                "build_message_trigram",
                "build_title_trigram",
                "write_metadata",
                "commit_swap",
            ],
        )
        self.assertIn("processed", schema["web_index_build"]["progress"])
        self.assertIn("rolls back", schema["web_index_build"]["publication"])
        self.assertIn("/web-index/cancel", schema["web_index_build"]["cancellation"])
        self.assertTrue(schema["import_contract"]["zip64"]["runtime_supported"])
        self.assertFalse(schema["import_contract"]["zip64"]["physical_over_4_gib_acceptance_tested"])
        self.assertIn("delay WAL checkpoint", schema["export"]["snapshot"]["wal_operational_limit"])
        self.assertEqual(schema["jobs"]["web_index_progress"], [
            "status", "build_stage", "processed", "total", "complete", "batch_size",
            "processed_input_bytes", "processed_normalized_bytes",
            "current_batch_input_bytes", "current_batch_normalized_bytes",
            "current_batch_derived_bytes", "peak_batch_input_bytes",
            "peak_batch_normalized_bytes", "peak_batch_derived_bytes", "oversized_rows",
        ])
        self.assertEqual(schema["import_contract"]["max_nodes_per_conversation"], 5000)
        self.assertIn("conversation_node_limit_exceeded", schema["jobs"]["failure_codes"])
        self.assertEqual(
            schema["search"]["message_resource_contract"]["raw_fallback_bytes_per_row"],
            1024 * 1024,
        )
        self.assertIn("continuation", schema["search"]["parameters"])
        self.assertIn(
            "signed opaque server-instance session ID",
            schema["search"]["message_resource_contract"]["continuation"],
        )
        self.assertIn("direct UTF-8 byte seek", schema["messages"]["display_cursor"])
        for field in (
            "candidate_count",
            "candidate_limit",
            "resolver_calls",
            "blob_reads",
            "candidate_blob_bytes",
            "raw_blob_bytes",
            "decoded_chars",
            "normalization_units",
            "sqlite_vm_steps",
            "wall_seconds",
            "continuation_available",
            "continuation_token",
            "completion_state",
        ):
            self.assertIn(field, schema["search"]["diagnostics"]["fields"])
        self.assertIn("foreign_key_check_complete", schema["database_compatibility"]["health_fields"])
        self.assertEqual(
            schema["database_compatibility"]["effective_current_verify_counters"]["unit"],
            "conversation count",
        )

    def test_schema_field_lists_match_actual_page_contracts(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        schema = client.get("/api/schema").json()
        conversation_page = client.get("/api/conversations?limit=1").json()
        selected_page = client.get("/api/conversations?limit=1&offset=99&selected_id=web-1").json()
        conversation_fields = (
            set(conversation_page)
            | set(conversation_page["items"][0])
            | set(selected_page)
            | set(selected_page.get("selected_item", {}))
        )
        self.assertTrue(set(schema["conversations"]["response"]) <= conversation_fields)
        self.assertEqual(
            set(schema["pagination"]["conversation_page"]),
            {
                "items", "total", "limit", "offset", "has_more", "next_offset",
                "order_exact", "scan_complete", "provisional_order",
            },
        )
        self.assertTrue(set(schema["pagination"]["conversation_page"]) <= set(conversation_page))
        self.assertNotIn("total_exact", conversation_page)

        message_page = client.get(
            "/api/conversations/web-1/messages?path=all&include_internal=true&around_node_id=b1&limit=2"
        ).json()
        message_fields = set(message_page) | set(message_page["items"][0])
        self.assertTrue(set(schema["messages"]["path_metadata"]) <= message_fields)
        self.assertTrue(set(schema["messages"]["around_node_id"]["response"]) <= set(message_page))
        self.assertTrue(set(schema["pagination"]["message_page"]) <= set(message_page))

        search_page = client.get("/api/search/messages?q=python&limit=1").json()
        self.assertTrue(set(schema["pagination"]["message_search_page"]) <= set(search_page))
        self.assertIsInstance(search_page["total_exact"], bool)

        types_source = (Path(__file__).resolve().parents[1] / "webui/src/types.ts").read_text(encoding="utf-8")
        for field in (
            "cycle_detected",
            "missing_parent",
            "cross_conversation_parent",
            "partial_chain",
            "raw_flag_leaf_count",
            "raw_flag_cycle_detected",
            "around_target_in_requested_collection",
        ):
            self.assertIn(field, types_source)

    def test_empty_database_message_search_pagination_and_job_id_contract(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        client = TestClient(create_app(base / "missing.db", static_dir=self.make_build_dir(base)))
        self.addCleanup(client.close)
        for count_total in ("true", "false"):
            page = client.get(f"/api/search/messages?q=synthetic&count_total={count_total}&offset=99").json()
            self.assertEqual(page["total"], 0)
            self.assertTrue(page["total_exact"])
            self.assertEqual(page["offset"], 99)
            self.assertFalse(page["has_more"])
            self.assertIsNone(page["next_offset"])
        conversation_page = client.get("/api/conversations").json()
        self.assertNotIn("total_exact", conversation_page)

        for invalid in ("short", "A" * 32, "g" * 32, "0" * 31, "0" * 33, "x" * 129, "%01" + "0" * 31):
            with self.subTest(invalid_job_id=invalid):
                response = client.get(f"/api/import/jobs/{invalid}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["detail"], "invalid_job_id")
        missing = client.get("/api/import/jobs/" + "0" * 32)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "job_not_found")

    def test_suggest_no_db_returns_empty_items(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        db = base / "missing.db"
        client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
        suggest = client.get("/api/search/suggest?q=python").json()
        self.assertEqual(suggest, {"items": []})

    def test_suggest_respects_q_length_limit(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        long_q = "x" * 101
        self.assertEqual(client.get(f"/api/search/suggest?q={quote(long_q)}").status_code, 422)

    def test_suggest_rejects_stale_or_legacy_title_norm_and_matches_canonical_normalization(self):
        from chatgpt_export_archiver.db import connect
        from chatgpt_export_archiver.search import invalidate_capability_cache
        from chatgpt_export_archiver.web_db import create_web_indexes

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = connect(db)
        conn.execute("UPDATE conversations SET title = ? WHERE conversation_id = 'web-1'", ("Ｆｕｌｌｗｉｄｔｈ Ｉｎｔｅｌ café ﬁ CJK短词",))
        conn.commit()
        conn.close()
        create_web_indexes(db)
        for query in ("Intel", "cafe\u0301", "fi", "CJK短"):
            with self.subTest(current_index_query=query):
                items = client.get(f"/api/search/suggest?q={quote(query)}").json()["items"]
                self.assertEqual(items[0]["conversation_id"], "web-1")

        # Canonical mutation increments the durable generation.  The old
        # normalized table still exists, but suggest must ignore it.
        conn = connect(db)
        conn.execute("UPDATE conversations SET title = 'NEW CANONICAL NEEDLE' WHERE conversation_id = 'web-1'")
        conn.commit()
        conn.close()
        invalidate_capability_cache()
        items = client.get("/api/search/suggest?q=canonical").json()["items"]
        self.assertEqual(items[0]["conversation_id"], "web-1")

        # A legacy/raw-lower table without metadata is also untrusted.
        conn = connect(db)
        conn.execute("DROP TABLE web_index_metadata")
        conn.execute("DELETE FROM web_title_norm")
        conn.execute("INSERT INTO web_title_norm(conversation_id, title_norm) VALUES ('web-1', 'stale')")
        conn.commit()
        conn.close()
        invalidate_capability_cache()
        items = client.get("/api/search/suggest?q=canonical").json()["items"]
        self.assertEqual(items[0]["conversation_id"], "web-1")

    def test_upload_policy_detects_loopback_and_remote(self):
        from chatgpt_export_archiver.web_api import _get_upload_policy

        local = _get_upload_policy(host="127.0.0.1")
        self.assertFalse(local.remote)
        local2 = _get_upload_policy(host="localhost")
        self.assertFalse(local2.remote)
        remote = _get_upload_policy(host="0.0.0.0")
        self.assertTrue(remote.remote)

    def test_non_loopback_web_start_requires_explicit_remote_access_opt_in(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        build = self.make_build_dir(base)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "non_loopback_access_requires_opt_in"):
                create_app(base / "archive.db", static_dir=build, host="0.0.0.0")
        with mock.patch.dict(
            os.environ,
            {
                "CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS": "true",
                "CHATGPT_ARCHIVE_ALLOWED_HOSTS": "testserver",
            },
            clear=True,
        ):
            client = TestClient(create_app(base / "archive.db", static_dir=build, host="0.0.0.0"))
            self.addCleanup(client.close)
            health = client.get("/api/health").json()
            self.assertTrue(health["remote_access"])
            self.assertEqual(health["access_profile"], "remote_opt_in")

    def test_concurrent_upload_slot_before_file_read(self):
        from chatgpt_export_archiver.web_jobs import ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        manager = ImportJobManager(base / "archive.db")

        self.assertTrue(manager.acquire_pending_upload_slot())
        self.assertFalse(manager.acquire_pending_upload_slot())
        manager.release_pending_upload_slot()
        self.assertTrue(manager.acquire_pending_upload_slot())
        manager.release_pending_upload_slot()

    def test_import_thread_constructor_and_start_failures_release_writer_slot(self):
        from chatgpt_export_archiver.web_jobs import ImportJobManager, ImportJobStartError

        for failure_point in ("constructor", "start"):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                upload_dir = base / "upload"
                upload_dir.mkdir()
                upload = upload_dir / "upload.zip"
                upload.write_bytes(b"synthetic")
                manager = ImportJobManager(base / "archive.db")
                self.assertTrue(manager.acquire_pending_upload_slot())
                if failure_point == "constructor":
                    patcher = mock.patch(
                        "chatgpt_export_archiver.web_jobs.threading.Thread",
                        side_effect=RuntimeError("synthetic constructor failure"),
                    )
                else:
                    fake_thread = mock.Mock()
                    fake_thread.start.side_effect = RuntimeError("synthetic start failure")
                    patcher = mock.patch(
                        "chatgpt_export_archiver.web_jobs.threading.Thread",
                        return_value=fake_thread,
                    )
                with patcher, self.assertRaises(ImportJobStartError) as caught:
                    manager.start_import(upload, filename="synthetic.zip", size=9)
                self.assertEqual(caught.exception.code, "import_job_start_failed")
                self.assertEqual(caught.exception.error_type, "RuntimeError")
                self.assertFalse(manager.has_running_job())
                self.assertEqual(manager.list_jobs(), [])
                self.assertTrue(manager.acquire_pending_upload_slot())
                manager.release_pending_upload_slot()

    def test_import_worker_setup_failure_is_terminal_cleans_upload_and_releases_slot(self):
        from chatgpt_export_archiver.web_jobs import ImportJobManager

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload_dir = base / "upload"
            upload_dir.mkdir()
            upload = upload_dir / "upload.zip"
            upload.write_bytes(b"synthetic")
            manager = ImportJobManager(base / "archive.db")
            with mock.patch.object(manager, "_set_stage", side_effect=RuntimeError("synthetic setup")):
                job = manager.start_import(upload, filename="synthetic.zip", size=9)
                deadline = time.time() + 5
                while manager.has_running_job() and time.time() < deadline:
                    time.sleep(0.01)
            snapshot = job.snapshot()
            self.assertEqual(snapshot["status"], "failed")
            self.assertEqual(snapshot["stage"], "job_setup")
            self.assertEqual(snapshot["error_code"], "import_job_start_failed")
            self.assertEqual(snapshot["error_type"], "RuntimeError")
            self.assertFalse(upload_dir.exists())
            self.assertFalse(manager.has_running_job())
            self.assertTrue(manager.acquire_pending_upload_slot())
            manager.release_pending_upload_slot()

    def test_message_hidden_counts_fast_path_matches_row_path(self):
        from chatgpt_export_archiver.search import _message_visibility_counts, _message_visibility_counts_for_path

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = connect_readonly(db)
        try:
            for path in ("current", "all"):
                with self.subTest(path=path):
                    rows = conn.execute(
                        "SELECT node_id, parent_node_id, children_json, message_id, role, author_name, create_time, update_time, content_type, content_text, content_hash, is_on_current_path, raw_message_json FROM conversation_nodes WHERE conversation_id = ?",
                        ("web-3",),
                    ).fetchall()
                    row_counts = _message_visibility_counts(rows)
                    fast_counts = _message_visibility_counts_for_path(conn, "web-3", path)
                    self.assertEqual(row_counts["visible_total"], fast_counts["visible_total"], f"{path} visible_total mismatch")
                    self.assertEqual(row_counts["empty_hidden_count"], fast_counts["empty_hidden_count"], f"{path} empty_hidden_count mismatch")
                    self.assertEqual(row_counts["internal_hidden_count"], fast_counts["internal_hidden_count"], f"{path} internal_hidden_count mismatch")
                    self.assertEqual(row_counts["technical_hidden_count"], fast_counts["technical_hidden_count"], f"{path} technical_hidden_count mismatch")
        finally:
            conn.close()

    def test_hidden_counts_distinguish_empty_technical_and_internal(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "hidden-types.zip"
        mapping = {
            "root": root(["sys"]),
            "sys": node("sys", "root", "system", "system synthetic text", 1_700_000_001, ["dev"]),
            "dev": node("dev", "sys", "developer", "developer synthetic instruction", 1_700_000_002, ["ctx"]),
            "ctx": custom_content_node(
                "ctx", "dev", "user",
                {"content_type": "user_editable_context", "user_profile": "profile text", "user_instructions": {"text": "instruction text"}},
                1_700_000_003, ["tool"]),
            "tool": node("tool", "ctx", "tool", "tool output", 1_700_000_004, ["a"]),
            "a": node("a", "tool", "assistant", "visible answer", 1_700_000_005, ["branch"]),
            "branch": node("branch", "a", "assistant", "off-current branch text", 1_700_000_006),
        }
        write_zip(z, [conv("hidden-types", "Hidden Types Test", mapping, "a", 1_700_000_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))

        for path_label, path in [("current", "current"), ("all", "all")]:
            with self.subTest(path=path_label):
                page = client.get(f"/api/conversations/hidden-types/messages?path={path}&limit=20&include_internal=true").json()
                self.assertIn("visible_total", page, f"{path_label}: missing visible_total")
                self.assertIn("empty_hidden_count", page, f"{path_label}: missing empty_hidden_count")
                self.assertIn("internal_hidden_count", page, f"{path_label}: missing internal_hidden_count")
                self.assertIn("technical_hidden_count", page, f"{path_label}: missing technical_hidden_count")
                self.assertGreater(page["empty_hidden_count"], 0, f"{path_label}: root empty node not counted")
                self.assertGreater(page["internal_hidden_count"], 0, f"{path_label}: no internal count")
                self.assertGreater(page["technical_hidden_count"], 0, f"{path_label}: no technical count")
                self.assertEqual(page["visible_total"], page["total"] - page["empty_hidden_count"] - page["internal_hidden_count"])

        conn = connect_readonly(db)
        try:
            for path in ("current", "all"):
                with self.subTest(f"fast-vs-row-{path}"):
                    rows = conn.execute(
                        "SELECT node_id, parent_node_id, children_json, message_id, role, author_name, create_time, update_time, content_type, content_text, content_hash, is_on_current_path, raw_message_json FROM conversation_nodes WHERE conversation_id = ?",
                        ("hidden-types",),
                    ).fetchall()
                    from chatgpt_export_archiver.search import _message_visibility_counts, _message_visibility_counts_for_path
                    row_counts = _message_visibility_counts(rows)
                    fast_counts = _message_visibility_counts_for_path(conn, "hidden-types", path)
                    if path == "current":
                        path_rows = [r for r in rows if r["is_on_current_path"]]
                        row_counts = _message_visibility_counts(path_rows)
                    for field in ("visible_total", "empty_hidden_count", "internal_hidden_count", "technical_hidden_count"):
                        self.assertEqual(row_counts[field], fast_counts[field],
                                         f"{path} {field}: row={row_counts[field]} fast={fast_counts[field]}")
        finally:
            conn.close()

    def test_search_diagnostics_present_in_results(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        conversations = client.get("/api/conversations?q=python").json()
        self.assertIn("diagnostics", conversations)
        self.assertIn("web_index_missing", conversations["diagnostics"])
        messages = client.get("/api/search/messages?q=python").json()
        self.assertIn("diagnostics", messages)

    def test_search_diagnostics_do_not_report_legacy_fts_as_candidate_backend(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        try:
            for table in ("web_message_trigram", "web_title_trigram"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            for table in ("web_message_norm", "web_title_norm", "web_index_metadata"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'message_fts'").fetchone())
            conn.commit()
        finally:
            conn.close()

        conversations = client.get("/api/conversations?q=hello").json()
        messages = client.get("/api/search/messages?q=hello").json()
        for diag in (conversations["diagnostics"], messages["diagnostics"]):
            self.assertNotEqual(diag.get("candidate_backend"), "fts_legacy")
            self.assertNotEqual(diag.get("candidate_backend"), "normalized_trigram")
            self.assertIn(diag.get("candidate_backend"), {"full_scan", "normalized_scan", "normalized_title_scan"})
            self.assertTrue(diag.get("web_index_missing"))
            self.assertTrue(diag.get("legacy_fts_present"))

    def test_search_diagnostics_cover_normalized_trigram_fallback_short_and_title_paths(self):
        from chatgpt_export_archiver.search import _conversation_search_diagnostics, _message_search_diagnostics, parse_query
        from chatgpt_export_archiver.web_db import create_web_indexes, detect_trigram

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        probe = sqlite3.connect(":memory:")
        try:
            if not detect_trigram(probe):
                self.skipTest("SQLite FTS5 trigram tokenizer is unavailable")
        finally:
            probe.close()
        create_web_indexes(db)

        normalized = client.get("/api/search/messages?q=python").json()["diagnostics"]
        self.assertEqual(normalized.get("candidate_backend"), "normalized_trigram")
        self.assertFalse(normalized.get("web_index_missing"))

        short_query = client.get("/api/search/messages?q=py").json()["diagnostics"]
        self.assertEqual(short_query.get("candidate_backend"), "normalized_scan")
        self.assertTrue(short_query.get("short_query"))

        title_only = client.get("/api/conversations?scope=title&q=Python").json()["diagnostics"]
        self.assertEqual(title_only.get("candidate_backend"), "normalized_title_trigram")
        self.assertTrue(title_only.get("normalized_trigram_available"))
        for query in (
            "/api/conversations?title=Python",
            "/api/conversations?q=&title=Python",
            "/api/conversations?q=title:Python",
        ):
            with self.subTest(query=query):
                diag = client.get(query).json()["diagnostics"]
                self.assertEqual(diag.get("candidate_backend"), "normalized_title_trigram")
                self.assertTrue(diag.get("normalized_trigram_available"))
                self.assertNotEqual(diag.get("candidate_backend"), "normalized_scan")

        title_short = client.get("/api/conversations?scope=title&q=Py").json()["diagnostics"]
        self.assertEqual(title_short.get("candidate_backend"), "normalized_title_scan")
        self.assertTrue(title_short.get("short_query"))

        conn = connect_readonly(db)
        try:
            parsed = parse_query("python", path_default="all")
            message_fallback = _message_search_diagnostics(conn, parsed, used_trigram=False)
            conversation_fallback = _conversation_search_diagnostics(conn, parsed, used_trigram=False)
            self.assertEqual(message_fallback.get("candidate_backend"), "normalized_scan")
            self.assertEqual(conversation_fallback.get("candidate_backend"), "normalized_scan")
            self.assertFalse(message_fallback.get("normalized_trigram_available"))
            self.assertFalse(conversation_fallback.get("normalized_trigram_available"))
        finally:
            conn.close()

        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TABLE IF EXISTS web_title_trigram")
            conn.commit()
        finally:
            conn.close()
        title_scan = client.get("/api/conversations?title=Python").json()["diagnostics"]
        self.assertEqual(title_scan.get("candidate_backend"), "normalized_title_scan")
        self.assertFalse(title_scan.get("normalized_trigram_available"))
        self.assertNotEqual(title_scan.get("candidate_backend"), "normalized_scan")

        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TABLE IF EXISTS web_title_norm")
            conn.commit()
        finally:
            conn.close()
        title_full_scan = client.get("/api/conversations?title=Python").json()["diagnostics"]
        self.assertEqual(title_full_scan.get("candidate_backend"), "full_scan")
        self.assertTrue(title_full_scan.get("web_index_missing"))
        self.assertNotEqual(title_full_scan.get("candidate_backend"), "normalized_scan")

    def test_raw_endpoint_uses_sqlite_substr_for_truncation(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        import sqlite3 as _sql
        conn = _sql.connect(db)
        conn.row_factory = _sql.Row
        try:
            raw = conn.execute(
                "SELECT COALESCE(raw_message_json, '{}') AS raw_message_json FROM conversation_nodes WHERE conversation_id = 'web-1' AND node_id = 'u1'"
            ).fetchone()["raw_message_json"]
            big_raw = raw + " " * 100000
            conn.execute(
                "UPDATE conversation_nodes SET raw_message_json = ? WHERE conversation_id = 'web-1' AND node_id = 'u1'",
                (big_raw,),
            )
            conn.commit()
        finally:
            conn.close()

        resp = client.get("/api/conversations/web-1/messages/u1/raw?max_chars=500")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["truncated"])
        self.assertGreater(body["raw_size"], 500)
        self.assertIn("raw_text", body)
        self.assertLess(len(body.get("raw_message", "")), 1000)

    def test_raw_endpoint_max_chars_bounds(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        self.assertEqual(client.get("/api/conversations/web-1/messages/u1/raw?max_chars=0").status_code, 422)
        self.assertEqual(client.get("/api/conversations/web-1/messages/u1/raw?max_chars=500").status_code, 200)
        self.assertEqual(client.get("/api/conversations/web-1/messages/u1/raw?max_chars=200001").status_code, 422)
        self.assertEqual(client.get("/api/conversations/web-1/messages/u1/raw?max_chars=1").status_code, 200)
        self.assertEqual(client.get("/api/conversations/web-1/messages/u1/raw?max_chars=200000").status_code, 200)

    def test_schema_endpoint_includes_raw_suggest_upload_diagnostics(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        schema = client.get("/api/schema").json()
        schema_json = json.dumps(schema)
        self.assertIn("max_chars", schema_json)
        self.assertIn("truncated", schema_json)
        self.assertIn("diagnostics", schema_json)
        self.assertIn("best_effort", schema_json)
        self.assertIn("path-independent conversation candidates", schema["search"]["current_path_candidates"])
        self.assertIn("one initial compact page", schema["search"]["hit_navigation"])
        self.assertIn("without requiring AS MATERIALIZED", schema["search"]["sqlite_query_shape"])
        self.assertNotIn("fts_legacy", schema_json)
        self.assertIn("legacy_fts_present", schema_json)
        fields = set(schema["search"]["diagnostics"]["fields"])
        for field in (
            "candidate_backend",
            "web_index_missing",
            "normalized_trigram_available",
            "legacy_trigram_index",
            "legacy_fts_present",
            "short_query",
            "diagnostics_accuracy",
            "actual_fallback_note",
            "estimated_backend_note",
        ):
            self.assertIn(field, fields)
        self.assertIn("upload", schema)
        self.assertIn("suggest", schema)
        self.assertIn("100 characters", schema["suggest"]["q_limit"])
        for env_name in (
            "CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS",
            "CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS",
            "CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS",
            "CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE",
        ):
            self.assertIn(env_name, schema["upload"]["env"])
        self.assertIn("effective_policy", schema["upload"])
        effective = schema["upload"]["effective_policy"]
        for field in (
            "max_upload_bytes",
            "max_multipart_body_bytes",
            "max_json_member_bytes",
            "max_json_members",
            "max_total_uncompressed_bytes",
            "max_compression_ratio",
            "max_total_members",
            "remote",
            "remote_profile",
        ):
            self.assertIn(field, effective)
        self.assertTrue(math.isfinite(effective["max_compression_ratio"]))
        host_origin_policy = schema["upload"]["host_origin_policy"]
        self.assertEqual(
            host_origin_policy["single_value_headers"],
            ["Origin", "Content-Length", "Sec-Fetch-Site"],
        )
        self.assertIn("canonical nonnegative ASCII decimal", host_origin_policy["content_length"])
        for code in (
            "database_not_ready", "conversation_not_found", "message_not_found",
            "invalid_query", "invalid_sort", "invalid_message_order",
            "import_transaction_failed", "verify_failed", "stats_failed", "web_index_failed",
            "upload_disk_space_insufficient", "import_disk_space_insufficient",
            "web_index_disk_space_insufficient",
            "upload_duplicate_origin_header", "upload_duplicate_content_length",
            "upload_duplicate_sec_fetch_site",
        ):
            self.assertIn(code, schema["stable_error_codes"])
        self.assertEqual(
            schema["disk_capacity"]["error_codes"],
            [
                "upload_disk_space_insufficient",
                "import_disk_space_insufficient",
                "migration_disk_space_insufficient",
                "web_index_disk_space_insufficient",
            ],
        )

    def test_schema_effective_upload_policy_matches_bound_host_policy(self):
        from chatgpt_export_archiver.web_api import _get_upload_policy

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        build = self.make_build_dir(base)
        db = base / "missing.db"
        for host in ("127.0.0.1", "0.0.0.0"):
            with self.subTest(host=host):
                env = {
                    "CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS": "true",
                    "CHATGPT_ARCHIVE_ALLOWED_HOSTS": "testserver",
                } if host == "0.0.0.0" else {}
                with mock.patch.dict(os.environ, env, clear=False):
                    client = TestClient(create_app(db, static_dir=build, host=host))
                self.addCleanup(client.close)
                schema_policy = client.get("/api/schema").json()["upload"]["effective_policy"]
                actual = _get_upload_policy(host=host)
                self.assertEqual(schema_policy["max_upload_bytes"], actual.max_upload_bytes)
                self.assertEqual(schema_policy["max_json_member_bytes"], actual.max_json_member_bytes)
                self.assertEqual(schema_policy["max_json_members"], actual.max_json_members)
                self.assertEqual(schema_policy["max_total_uncompressed_bytes"], actual.max_total_uncompressed_bytes)
                self.assertEqual(schema_policy["max_total_members"], actual.max_total_members)
                self.assertEqual(schema_policy["remote"], actual.remote)
                self.assertEqual(schema_policy["remote_profile"], actual.remote_profile)

    def test_upload_slot_released_on_cancellation_like_scenario(self):
        from chatgpt_export_archiver.web_jobs import ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        manager = ImportJobManager(base / "archive.db")
        self.assertTrue(manager.acquire_pending_upload_slot())
        manager.release_pending_upload_slot()
        self.assertTrue(manager.acquire_pending_upload_slot())
        manager.release_pending_upload_slot()

    def test_upload_ingress_rejects_before_multipart_and_caps_chunked_body(self):
        from chatgpt_export_archiver.web_api import _get_web_trust_policy
        from chatgpt_export_archiver.web_app import UploadIngressMiddleware
        from chatgpt_export_archiver.web_jobs import ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        manager = ImportJobManager(Path(td.name) / "archive.db")
        app_calls = 0

        async def consuming_app(scope, receive, send):
            nonlocal app_calls
            app_calls += 1
            while True:
                message = await receive()
                if not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def invoke(*, headers: list[tuple[bytes, bytes]], chunks: list[bytes], remote: bool = False):
            sent: list[dict] = []
            messages = [
                {"type": "http.request", "body": chunk, "more_body": idx < len(chunks) - 1}
                for idx, chunk in enumerate(chunks)
            ] or [{"type": "http.request", "body": b"", "more_body": False}]

            async def receive():
                return messages.pop(0)

            async def send(message):
                sent.append(message)

            policy = _get_web_trust_policy(
                host="0.0.0.0" if remote else "127.0.0.1",
                allowed_hosts="testserver",
            )
            middleware = UploadIngressMiddleware(
                consuming_app,
                manager=manager,
                body_limit=10,
                trust_policy=policy,
            )
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/api/import/upload",
                "headers": headers,
                "scheme": "http",
                "state": {"trusted_host": "testserver", "trusted_scheme": "http"},
            }
            await middleware(scope, receive, send)
            return sent

        response = asyncio.run(invoke(headers=[(b"content-length", b"11")], chunks=[b"ignored"]))
        self.assertEqual(response[0]["status"], 413)
        self.assertEqual(app_calls, 0)
        response = asyncio.run(invoke(
            headers=[(b"origin", b"http://testserver"), (b"host", b"testserver")],
            chunks=[b"ignored"],
            remote=True,
        ))
        self.assertEqual(response[0]["status"], 411)
        self.assertEqual(app_calls, 0)
        response = asyncio.run(invoke(headers=[(b"origin", b"https://evil.invalid"), (b"host", b"127.0.0.1")], chunks=[b"ignored"]))
        self.assertEqual(response[0]["status"], 403)
        self.assertEqual(app_calls, 0)
        response = asyncio.run(invoke(headers=[], chunks=[b"123456", b"78901"]))
        self.assertEqual(response[0]["status"], 413)
        self.assertEqual(app_calls, 1)
        self.assertFalse(manager.has_running_job(), "oversized chunked upload must release its reserved slot")
        response = asyncio.run(invoke(headers=[(b"content-length", b"4")], chunks=[b"safe"]))
        self.assertEqual(response[0]["status"], 200)
        self.assertFalse(manager.has_running_job())

        def response_code(response):
            return json.loads(response[-1]["body"])["code"]

        for name, headers, expected in (
            ("origin", [(b"origin", b"http://testserver"), (b"origin", b"https://evil.invalid")], "upload_duplicate_origin_header"),
            ("length", [(b"content-length", b"4"), (b"content-length", b"5")], "upload_duplicate_content_length"),
            ("fetch", [(b"sec-fetch-site", b"same-origin"), (b"sec-fetch-site", b"cross-site")], "upload_duplicate_sec_fetch_site"),
        ):
            with self.subTest(duplicate=name):
                response = asyncio.run(invoke(headers=headers, chunks=[b"safe"]))
                self.assertEqual(response[0]["status"], 400)
                self.assertEqual(response_code(response), expected)

        for origin in (
            b"http://testserver/path",
            b"http://testserver?query",
            b"http://testserver#fragment",
            b"http://user@testserver",
            b"null",
            b"http://testserver,http://evil.invalid",
            b"http://testserver\x01",
        ):
            with self.subTest(origin=origin):
                response = asyncio.run(invoke(
                    headers=[(b"origin", origin), (b"content-length", b"4")],
                    chunks=[b"safe"],
                    remote=True,
                ))
                self.assertEqual(response[0]["status"], 403)
                self.assertEqual(response_code(response), "upload_origin_not_allowed")

        for raw_length in (b"+1", b"-0", b" 1", b"1 ", b"01", "١".encode("utf-8"), b"1,2", b"9" * 21):
            with self.subTest(content_length=raw_length):
                response = asyncio.run(invoke(headers=[(b"content-length", raw_length)], chunks=[b"safe"]))
                self.assertEqual(response[0]["status"], 400)
                self.assertEqual(response_code(response), "upload_invalid_content_length")

        response = asyncio.run(invoke(
            headers=[(b"origin", b"http://testserver"), (b"content-length", b"4"), (b"sec-fetch-site", b"same-origin")],
            chunks=[b"safe"],
            remote=True,
        ))
        self.assertEqual(response[0]["status"], 200)

        async def cancelled_app(scope, receive, send):
            await receive()
            raise asyncio.CancelledError

        async def invoke_cancelled():
            async def receive():
                return {"type": "http.request", "body": b"safe", "more_body": False}

            async def send(_message):
                return None

            policy = _get_web_trust_policy(host="127.0.0.1", allowed_hosts="testserver")
            middleware = UploadIngressMiddleware(
                cancelled_app,
                manager=manager,
                body_limit=10,
                trust_policy=policy,
            )
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/api/import/upload",
                "headers": [(b"content-length", b"4")],
                "state": {"trusted_host": "testserver", "trusted_scheme": "http"},
            }
            await middleware(scope, receive, send)

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(invoke_cancelled())
        self.assertFalse(manager.has_running_job(), "client cancellation must release the pre-parser writer slot")

    def test_upload_compression_ratio_config_rejects_nonfinite_and_nonpositive_values(self):
        from chatgpt_export_archiver.web_api import (
            DEFAULT_MAX_UPLOAD_COMPRESSION_RATIO,
            REMOTE_DEFAULT_COMPRESSION_RATIO,
            _get_upload_policy,
        )

        env_name = "CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO"
        for raw in ("NaN", "Infinity", "-Infinity", "0", "-0", "-1", "invalid"):
            with self.subTest(raw=raw):
                local = _get_upload_policy({env_name: raw}, host="127.0.0.1")
                remote = _get_upload_policy({env_name: raw, "CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS": "true"}, host="0.0.0.0")
                self.assertEqual(local.max_compression_ratio, DEFAULT_MAX_UPLOAD_COMPRESSION_RATIO)
                self.assertEqual(remote.max_compression_ratio, REMOTE_DEFAULT_COMPRESSION_RATIO)
                self.assertTrue(math.isfinite(local.max_compression_ratio))
                self.assertTrue(math.isfinite(remote.max_compression_ratio))

    def test_connect_writable_mode_rw_never_creates_a_missing_database(self):
        from chatgpt_export_archiver.web_db import connect_writable

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            missing = base / "missing.db"
            with self.assertRaises(ValueError):
                connect_writable(missing)
            self.assertFalse(missing.exists())
            with mock.patch.object(Path, "exists", return_value=True):
                with self.assertRaises(ValueError):
                    connect_writable(missing)
            self.assertFalse(missing.exists(), "mode=rw must not create a database after an exists/open race")
            with self.assertRaises(ValueError):
                connect_writable(base)
            normal = base / "normal.db"
            sqlite3.connect(normal).close()
            conn = connect_writable(normal)
            try:
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 0)
            finally:
                conn.close()

    def test_upload_slot_released_when_make_upload_path_fails(self):
        from fastapi import FastAPI
        from chatgpt_export_archiver.web_api import create_api_router
        from chatgpt_export_archiver.web_jobs import ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        manager = ImportJobManager(base / "archive.db")
        app = FastAPI()
        app.include_router(create_api_router(base / "archive.db", manager))
        client = TestClient(app, raise_server_exceptions=False)

        with mock.patch("chatgpt_export_archiver.web_api.make_upload_path", side_effect=OSError("synthetic temp failure")):
            response = client.post("/api/import/upload", files={"file": ("synthetic.zip", b"not a real zip", "application/zip")})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["detail"],
            {"code": "upload_preflight_failed", "error_type": "OSError"},
        )
        self.assertNotIn(str(base), response.text)
        self.assertFalse(manager.has_running_job())

        good_zip = base / "good.zip"
        write_zip(good_zip, [conv("upload-after-failure", "Upload After Failure", {"root": root(["u"]), "u": node("u", "root", "user", "synthetic upload", 1_701_300_000)}, "u", 1_701_300_000)])
        with good_zip.open("rb") as handle:
            second = client.post("/api/import/upload", files={"file": ("good.zip", handle, "application/zip")})
        self.assertNotEqual(second.status_code, 409, second.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIn(second.json()["status"], {"queued", "running", "succeeded"})
        self.wait_job(client, second.json()["job_id"])
        self.assertFalse(manager.has_running_job())
        client.close()

    def test_unexpected_upload_preflight_failures_return_safe_json(self):
        from fastapi import FastAPI
        from chatgpt_export_archiver import web_api
        from chatgpt_export_archiver.web_api import create_api_router
        from chatgpt_export_archiver.web_jobs import ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        valid = base / "valid.zip"
        write_zip(valid, [conv("unexpected-upload", "Synthetic", {"root": root([])}, "root", 1)])
        cases = (
            ("zip_probe", mock.patch.object(web_api.zipfile, "is_zipfile", side_effect=RuntimeError("private probe detail")), "RuntimeError"),
            ("zip_validation", mock.patch.object(web_api, "_validate_upload_zip_members", side_effect=PermissionError("private validation detail")), "PermissionError"),
            ("job_start", mock.patch.object(web_api.ImportJobManager, "start_import", side_effect=ValueError("private start detail")), "ValueError"),
        )
        for label, patcher, error_type in cases:
            with self.subTest(label=label):
                manager = ImportJobManager(base / f"{label}.db")
                app = FastAPI()
                app.include_router(create_api_router(base / f"{label}.db", manager))
                client = TestClient(app, raise_server_exceptions=False)
                with patcher:
                    response = client.post(
                        "/api/import/upload",
                        files={"file": ("valid.zip", valid.read_bytes(), "application/zip")},
                    )
                self.assertEqual(response.status_code, 500, response.text)
                self.assertEqual(response.headers["content-type"].split(";", 1)[0], "application/json")
                self.assertEqual(response.json()["detail"], {
                    "code": "upload_preflight_failed",
                    "error_type": error_type,
                })
                self.assertNotIn(str(base), response.text)
                self.assertNotIn("private", response.text)
                self.assertFalse(manager.has_running_job())
                client.close()

    def test_upload_disk_capacity_failure_is_507_and_cleans_temporary_copy(self):
        from fastapi import FastAPI
        from chatgpt_export_archiver.disk_resources import DiskSpaceInsufficientError
        from chatgpt_export_archiver.web_api import create_api_router
        from chatgpt_export_archiver.web_jobs import ImportJobManager

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        upload_dir = base / "upload-temp"
        upload_path = upload_dir / "upload.zip"
        manager = ImportJobManager(base / "archive.db")
        app = FastAPI()
        app.include_router(create_api_router(base / "archive.db", manager))
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)

        def make_known_upload_path():
            upload_dir.mkdir()
            return upload_dir, upload_path

        failure = DiskSpaceInsufficientError(
            "upload_disk_space_insufficient",
            required_bytes=900,
            free_bytes=100,
        )
        with mock.patch(
            "chatgpt_export_archiver.web_api.make_upload_path",
            side_effect=make_known_upload_path,
        ), mock.patch(
            "chatgpt_export_archiver.web_api.require_free_space",
            side_effect=failure,
        ):
            response = client.post(
                "/api/import/upload",
                files={"file": ("synthetic.zip", b"synthetic", "application/zip")},
            )
        self.assertEqual(response.status_code, 507, response.text)
        self.assertEqual(response.json()["detail"], "upload_disk_space_insufficient")
        self.assertNotIn(str(base), response.text)
        self.assertFalse(upload_dir.exists())
        self.assertFalse(manager.has_running_job())

    def test_upload_policy_remote_opt_in_for_all_limits(self):
        from chatgpt_export_archiver.web_api import _get_upload_policy, REMOTE_DEFAULT_MAX_UPLOAD_BYTES, REMOTE_DEFAULT_TOTAL_UNCOMPRESSED

        with mock.patch.dict(os.environ, {}, clear=True):
            local = _get_upload_policy(host="127.0.0.1", environ={})
            self.assertFalse(local.remote)
            self.assertGreater(local.max_upload_bytes, REMOTE_DEFAULT_MAX_UPLOAD_BYTES)

            remote_no_optin = _get_upload_policy(host="0.0.0.0", environ={})
            self.assertTrue(remote_no_optin.remote)
            self.assertLessEqual(remote_no_optin.max_total_uncompressed_bytes, REMOTE_DEFAULT_TOTAL_UNCOMPRESSED)

            remote_with_allow = _get_upload_policy(
                host="0.0.0.0",
                environ={"CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS": "true"},
            )
            self.assertTrue(remote_with_allow.remote)
            self.assertEqual(remote_with_allow.max_upload_bytes, REMOTE_DEFAULT_MAX_UPLOAD_BYTES)
            self.assertEqual(remote_with_allow.max_total_uncompressed_bytes, REMOTE_DEFAULT_TOTAL_UNCOMPRESSED)

            remote_only_total = _get_upload_policy(
                host="0.0.0.0",
                environ={"CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES": str(1024 * 1024 * 1024)},
            )
            self.assertEqual(remote_only_total.max_total_uncompressed_bytes, REMOTE_DEFAULT_TOTAL_UNCOMPRESSED)

            remote_explicit_total = _get_upload_policy(
                host="0.0.0.0",
                environ={
                    "CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS": "true",
                    "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES": str(1024 * 1024 * 1024),
                },
            )
            self.assertEqual(remote_explicit_total.max_upload_bytes, REMOTE_DEFAULT_MAX_UPLOAD_BYTES)
            self.assertEqual(remote_explicit_total.max_total_uncompressed_bytes, 1024 * 1024 * 1024)

            remote_local_profile = _get_upload_policy(
                host="0.0.0.0",
                environ={"CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE": "local"},
            )
            self.assertGreater(remote_local_profile.max_upload_bytes, REMOTE_DEFAULT_MAX_UPLOAD_BYTES)
            self.assertGreater(remote_local_profile.max_total_uncompressed_bytes, REMOTE_DEFAULT_TOTAL_UNCOMPRESSED)

    def test_remote_upload_policy_allows_full_limits_with_env(self):
        from chatgpt_export_archiver.web_api import _get_upload_policy

        env = {
            "CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS": "true",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES": str(10 * 1024 * 1024 * 1024),
            "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES": str(10 * 1024 * 1024 * 1024),
            "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES": str(10 * 1024 * 1024 * 1024),
            "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS": "9000",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS": "120000",
            "CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO": "500.0",
        }
        policy = _get_upload_policy(host="0.0.0.0", environ=env)
        self.assertGreater(policy.max_upload_bytes, 128 * 1024 * 1024)
        self.assertGreater(policy.max_total_uncompressed_bytes, 512 * 1024 * 1024)
        self.assertEqual(policy.max_json_members, 9000)
        self.assertEqual(policy.max_total_members, 120000)

        blocked = _get_upload_policy(host="0.0.0.0", environ={"CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS": "120000"})
        self.assertEqual(blocked.max_total_members, 10000)

    def test_embedded_quote_tokens_are_preserved_as_literals(self):
        from chatgpt_export_archiver.search import parse_query

        for query, expected in (
            ('foo"bar', ["foo\"bar"]),
            ('foo:"bar"', ["foo:bar"]),
            ('unknown:"foo bar"', ["unknown:foo bar"]),
            ('url:"foo bar"', ["url:foo bar"]),
            ('source:"conversations 1.json"', None),
            ('-"foo bar"', None),
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertEqual(parsed.errors, [])
                if expected is None:
                    continue
                self.assertEqual(parsed.terms, expected)

        parsed = parse_query('source:"conversations 1.json"')
        self.assertEqual(parsed.source, "conversations 1.json")
        excluded = parse_query('-"foo bar"')
        self.assertEqual(excluded.exclude, ["foo bar"])

    def test_upload_total_members_limit(self):
        from chatgpt_export_archiver import web_api
        from chatgpt_export_archiver.web_api import UploadPolicy, _validate_upload_zip_members

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "many-members.zip"
        members = {"conversations.json": [conv("member-limit", "Member Limit", {"root": root(["u"]), "u": node("u", "root", "user", "synthetic", 1)}, "u", 1)]}
        for i in range(200):
            members[f"dummy_{i}.txt"] = b"x"
        write_zip_members(z, members)

        tight_policy = UploadPolicy(
            max_upload_bytes=20 * 1024 * 1024 * 1024,
            max_json_member_bytes=64 * 1024 * 1024 * 1024,
            max_json_members=5000,
            max_total_uncompressed_bytes=128 * 1024 * 1024 * 1024,
            max_compression_ratio=1000.0,
            max_total_members=50,
            remote=False,
        )
        with self.assertRaises(Exception) as ctx:
            _validate_upload_zip_members(z, tight_policy)
        self.assertIn("upload_zip_too_many_members", str(ctx.exception.detail))

    def test_upload_total_members_passes_with_legal_zip(self):
        from chatgpt_export_archiver import web_api
        from chatgpt_export_archiver.web_api import UploadPolicy, _validate_upload_zip_members

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        members = {"conversations.json": [conv("ok", "OK", {"root": root(["u"]), "u": node("u", "root", "user", "ok", 1)}, "u", 1)]}
        z = base / "ok.zip"
        write_zip_members(z, members)

        normal_policy = UploadPolicy(
            max_upload_bytes=20 * 1024 * 1024 * 1024,
            max_json_member_bytes=64 * 1024 * 1024 * 1024,
            max_json_members=5000,
            max_total_uncompressed_bytes=128 * 1024 * 1024 * 1024,
            max_compression_ratio=1000.0,
            max_total_members=100000,
            remote=False,
        )
        try:
            _validate_upload_zip_members(z, normal_policy)
        except Exception:
            self.fail("legal zip should not raise")

    def test_upload_total_members_counts_directory_entries(self):
        from chatgpt_export_archiver.web_api import UploadPolicy, _validate_upload_zip_members

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        z = Path(td.name) / "directory-members.zip"
        with zipfile.ZipFile(z, "w") as archive:
            for index in range(10):
                archive.writestr(f"directory-{index}/", b"")
            archive.writestr("conversations.json", "[]")
        policy = UploadPolicy(
            max_upload_bytes=1024 * 1024,
            max_json_member_bytes=1024 * 1024,
            max_json_members=3,
            max_total_uncompressed_bytes=1024 * 1024,
            max_compression_ratio=100.0,
            max_total_members=3,
            remote=False,
        )
        with self.assertRaises(Exception) as caught:
            _validate_upload_zip_members(z, policy)
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(caught.exception.detail, "upload_zip_too_many_members")

    def test_around_node_id_fallback_avoids_full_row_read(self):
        from chatgpt_export_archiver.search import get_messages, MAX_AROUND_NODE_ROWS, _conversation_rows

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "around-fallback.zip"
        mapping = {"root": root(["n0"])}
        previous = "root"
        for idx in range(120):
            node_id = f"n{idx}"
            child = f"n{idx + 1}" if idx < 119 else None
            mapping[node_id] = node(node_id, previous, "user", f"around fallback {idx}", 1_700_000_000 + idx, [child] if child else [])
            previous = node_id
        write_zip(z, [conv("around-fallback", "Around Fallback", mapping, "n119", 1_700_000_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        conn = connect_readonly(db)
        try:
            with mock.patch("chatgpt_export_archiver.search.MAX_AROUND_NODE_ROWS", 50), \
                 mock.patch("chatgpt_export_archiver.search._conversation_rows", side_effect=AssertionError("_conversation_rows should not be called in fallback")):
                page = get_messages(conn, "around-fallback", path="current", limit=10, offset=0, around_node_id="n100")
            self.assertEqual(page["total"], 121)
            self.assertTrue(any(item["node_id"] == "n100" for item in page["items"]),
                            "should include the around_node_id target")
            self.assertNotIn("raw_message_json", json.dumps(page["items"][0]),
                             "response should not contain raw_message_json")
        finally:
            conn.close()

    def test_long_effective_current_chain_uses_finite_sql_collection(self):
        from chatgpt_export_archiver.search import get_messages

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "long-current-chain.zip"
        mapping = {"root": root(["n0000"])}
        previous = "root"
        for idx in range(2500):
            node_id = f"n{idx:04d}"
            child = f"n{idx + 1:04d}" if idx < 2499 else None
            mapping[node_id] = node(node_id, previous, "user", f"long chain synthetic {idx}", 1_700_800_000 + idx, [child] if child else [])
            previous = node_id
        write_zip(z, [conv("long-current-chain", "Long Current Chain", mapping, "n2499", 1_700_800_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        writer = sqlite3.connect(db)
        try:
            writer.execute("UPDATE conversation_nodes SET is_on_current_path = 0 WHERE conversation_id = 'long-current-chain'")
            writer.commit()
        finally:
            writer.close()
        conn = connect_readonly(db)
        try:
            with mock.patch("chatgpt_export_archiver.search._conversation_rows", side_effect=AssertionError("SQL reader path must not load the whole chain into Python")):
                page = get_messages(conn, "long-current-chain", path="current", limit=5, offset=2496, include_internal=True)
            self.assertEqual(page["total"], 2501)
            self.assertEqual(page["effective_path"], "current")
            self.assertFalse(page["current_path_fallback_to_all"])
            self.assertEqual(page["items"][-1]["node_id"], "n2499")
        finally:
            conn.close()

    def test_around_node_id_uses_visible_page_collection_in_sql_fast_path(self):
        from chatgpt_export_archiver.search import get_messages

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "around-visible-prefix.zip"
        mapping = {"root": root(["h000"])}
        previous = "root"
        for idx in range(90):
            node_id = f"h{idx:03d}"
            child = f"h{idx + 1:03d}" if idx < 89 else "target"
            if idx % 4 == 0:
                mapping[node_id] = empty_mapping_node(node_id, previous, [child])
            else:
                role = ("system", "developer", "tool")[idx % 3]
                mapping[node_id] = node(node_id, previous, role, f"hidden around prefix {idx}", 1_700_700_000 + idx, [child])
            previous = node_id
        mapping["target"] = node("target", previous, "user", "visible-around-target needle", 1_700_700_500, ["tail"])
        mapping["tail"] = node("tail", "target", "assistant", "visible tail after target", 1_700_700_501)
        mapping["off-current"] = node("off-current", "root", "user", "off current target", 1_700_700_502)
        write_zip(z, [conv("around-visible-prefix", "Around Visible Prefix", mapping, "tail", 1_700_700_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        conn = connect_readonly(db)
        try:
            with mock.patch("chatgpt_export_archiver.search.MAX_AROUND_NODE_ROWS", 50), \
                 mock.patch("chatgpt_export_archiver.search._conversation_rows", side_effect=AssertionError("_conversation_rows should not be called in SQL fast path")):
                all_visible = get_messages(conn, "around-visible-prefix", path="all", limit=5, offset=0, around_node_id="target", include_internal=False)
                current_visible = get_messages(conn, "around-visible-prefix", path="current", limit=5, offset=0, around_node_id="target", include_internal=False)
                hidden_target = get_messages(conn, "around-visible-prefix", path="current", limit=5, offset=999, around_node_id="h050", include_internal=False)
                full_nodes = get_messages(conn, "around-visible-prefix", path="current", limit=5, offset=0, around_node_id="target", include_internal=True)
                off_current_all = get_messages(conn, "around-visible-prefix", path="all", limit=5, offset=0, around_node_id="off-current", include_internal=True)
                off_current_current = get_messages(conn, "around-visible-prefix", path="current", limit=5, offset=0, around_node_id="off-current", include_internal=True)
            for page in (all_visible, current_visible):
                with self.subTest(path=page["effective_path"]):
                    self.assertGreater(page["total"], 0)
                    self.assertIn("target", [item["node_id"] for item in page["items"]])
                    self.assertTrue(page["items"], "visible collection around target should not return an empty page")
                    self.assertTrue(page["around_target_found"])
                    self.assertTrue(page["around_target_visible"])
                    self.assertTrue(page["around_target_in_effective_collection"])
                    self.assertTrue(page["around_target_applied"])
            self.assertEqual(hidden_target["offset"], 0)
            self.assertTrue(hidden_target["items"])
            self.assertIn("target", [item["node_id"] for item in hidden_target["items"]])
            self.assertTrue(hidden_target["around_target_found"])
            self.assertFalse(hidden_target["around_target_visible"])
            self.assertTrue(hidden_target["around_target_in_effective_collection"])
            self.assertFalse(hidden_target["around_target_applied"])
            self.assertGreater(full_nodes["total"], current_visible["total"])
            self.assertIn("target", [item["node_id"] for item in full_nodes["items"]])
            self.assertFalse(off_current_all["around_target_in_effective_collection"])
            self.assertTrue(off_current_all["around_target_in_requested_collection"])
            self.assertTrue(off_current_all["around_target_visible"])
            self.assertTrue(off_current_all["around_target_applied"])
            self.assertFalse(off_current_current["around_target_in_effective_collection"])
            self.assertFalse(off_current_current["around_target_in_requested_collection"])
            self.assertFalse(off_current_current["around_target_visible"])
            self.assertFalse(off_current_current["around_target_applied"])

            writer = sqlite3.connect(db)
            try:
                writer.execute("UPDATE conversation_nodes SET is_on_current_path = 0 WHERE conversation_id = 'around-visible-prefix'")
                writer.execute("UPDATE conversations SET current_node = NULL WHERE conversation_id = 'around-visible-prefix'")
                writer.commit()
            finally:
                writer.close()
            conn.close()
            conn = connect_readonly(db)
            with mock.patch("chatgpt_export_archiver.search.MAX_AROUND_NODE_ROWS", 50), \
                 mock.patch("chatgpt_export_archiver.search._conversation_rows", side_effect=AssertionError("_conversation_rows should not be called in SQL fast path")):
                damaged = get_messages(conn, "around-visible-prefix", path="current", limit=5, offset=0, around_node_id="target", include_internal=False)
            self.assertTrue(damaged["current_path_fallback_to_all"])
            self.assertEqual(damaged["effective_path"], "all")
            self.assertIn("target", [item["node_id"] for item in damaged["items"]])
            self.assertTrue(damaged["around_target_applied"])
        finally:
            conn.close()

    def test_diagnostics_fallback_path_not_reports_normalized_trigram(self):
        from chatgpt_export_archiver.search import _message_search_diagnostics, _conversation_search_diagnostics, parse_query

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = connect_readonly(db)
        try:
            parsed = parse_query("python", path_default="all")
            diag_normal = _message_search_diagnostics(conn, parsed, used_trigram=True)
            diag_fallback = _message_search_diagnostics(conn, parsed, used_trigram=False)
            self.assertIn("diagnostics_accuracy", diag_normal)
            self.assertIn("diagnostics_accuracy", diag_fallback)
            if diag_normal.get("candidate_backend") == "normalized_trigram" and \
               diag_fallback.get("candidate_backend") == "normalized_trigram":
                if diag_fallback.get("normalized_trigram_available"):
                    self.fail("fallback path should not report normalized trigram as candidate")
            conv_diag = _conversation_search_diagnostics(conn, parsed, used_trigram=False)
            self.assertIn("diagnostics_accuracy", conv_diag)
        finally:
            conn.close()

    def test_search_capability_probe_is_connection_cached_and_schema_change_invalidates_it(self):
        from chatgpt_export_archiver.search import _table_exists, invalidate_capability_cache

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            invalidate_capability_cache(conn)
            self.assertFalse(_table_exists(conn, "synthetic_capability"))
            for _ in range(20):
                self.assertFalse(_table_exists(conn, "synthetic_capability"))
            sqlite_master_reads = [sql for sql in statements if "sqlite_master" in sql.lower()]
            self.assertEqual(len(sqlite_master_reads), 1)
            conn.execute("CREATE TABLE synthetic_capability(value TEXT)")
            self.assertTrue(_table_exists(conn, "synthetic_capability"), "schema_version change must invalidate cached table capabilities")
            self.assertEqual(len([sql for sql in statements if "sqlite_master" in sql.lower()]), 2)
        finally:
            invalidate_capability_cache(conn)
            conn.close()

    def test_trusted_host_origin_and_proxy_boundary(self):
        from chatgpt_export_archiver.web_api import WebTrustPolicy
        from chatgpt_export_archiver.web_app import TrustedAccessMiddleware

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        build = self.make_build_dir(base)
        local = TestClient(create_app(base / "local.db", static_dir=build))
        self.addCleanup(local.close)
        for host in ("localhost", "127.0.0.1", "[::1]"):
            with self.subTest(loopback_host=host):
                self.assertEqual(local.get("/api/health", headers={"host": host}).status_code, 200)
        self.assertEqual(local.get("/api/health", headers={"host": "evil.example"}).status_code, 400)
        self.assertEqual(local.get("/", headers={"host": "evil.example"}).status_code, 400)
        evil_write = local.post(
            "/api/import/upload",
            headers={"host": "evil.example", "origin": "http://evil.example"},
            files={"file": ("bad.zip", b"not-a-zip", "application/zip")},
        )
        self.assertEqual(evil_write.status_code, 400)
        self.assertEqual(evil_write.json()["detail"], "host_not_allowed")

        env = {
            "CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS": "true",
            "CHATGPT_ARCHIVE_ALLOWED_HOSTS": "archive.lan",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            remote = TestClient(
                create_app(base / "remote.db", static_dir=build, host="0.0.0.0"),
                base_url="http://archive.lan",
            )
        self.addCleanup(remote.close)
        self.assertEqual(remote.get("/api/health").status_code, 200)
        missing_origin = remote.post(
            "/api/import/upload",
            files={"file": ("bad.zip", b"not-a-zip", "application/zip")},
        )
        self.assertEqual(missing_origin.status_code, 403)
        self.assertEqual(missing_origin.json()["detail"], "upload_origin_required")
        for headers in (
            {"origin": "http://evil.example"},
            {"origin": "http://archive.lan", "sec-fetch-site": "cross-site"},
        ):
            response = remote.post(
                "/api/import/upload",
                headers=headers,
                files={"file": ("bad.zip", b"not-a-zip", "application/zip")},
            )
            self.assertEqual(response.status_code, 403)
        allowed_write = remote.post(
            "/api/import/upload",
            headers={"origin": "http://archive.lan"},
            files={"file": ("bad.zip", b"not-a-zip", "application/zip")},
        )
        self.assertEqual(allowed_write.status_code, 400)
        self.assertEqual(allowed_write.json()["detail"], "uploaded_file_invalid_zip")

        calls = 0

        async def downstream(_scope, _receive, send):
            nonlocal calls
            calls += 1
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def invoke(
            client_ip: str,
            headers: list[tuple[bytes, bytes]],
            *,
            allowed_hosts: tuple[str, ...] = ("archive.lan",),
        ):
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            policy = WebTrustPolicy(
                allowed_hosts=allowed_hosts,
                trusted_proxies=("10.0.0.0/24",),
                remote=True,
                allow_missing_origin_for_writes=False,
            )
            middleware = TrustedAccessMiddleware(downstream, policy=policy)
            await middleware(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/health",
                    "scheme": "http",
                    "client": (client_ip, 1234),
                    "headers": headers,
                },
                receive,
                send,
            )
            return sent

        spoofed = asyncio.run(invoke("192.0.2.10", [(b"host", b"evil.example"), (b"x-forwarded-host", b"archive.lan")]))
        self.assertEqual(spoofed[0]["status"], 400)
        trusted = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b"host=archive.lan;proto=http")]))
        self.assertEqual(trusted[0]["status"], 204)
        ignored_bad_forward = asyncio.run(invoke("192.0.2.10", [(b"host", b"archive.lan"), (b"x-forwarded-host", b"evil.example")]))
        self.assertEqual(ignored_bad_forward[0]["status"], 204)
        injected_prefix = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b"host=evil.example, host=archive.lan;proto=http")]))
        self.assertEqual(injected_prefix[0]["status"], 400)
        two_hops = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b"for=192.0.2.1;host=archive.lan, for=10.0.0.7;host=archive.lan")]))
        self.assertEqual(two_hops[0]["status"], 400)
        conflicting = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b"host=archive.lan;proto=https"), (b"x-forwarded-host", b"evil.example"), (b"x-forwarded-proto", b"http")]))
        self.assertEqual(conflicting[0]["status"], 400)
        repeated_host = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"host", b"archive.lan"), (b"forwarded", b"host=archive.lan")]))
        self.assertEqual(repeated_host[0]["status"], 400)
        repeated_forwarded = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b"host=archive.lan"), (b"forwarded", b"proto=http")]))
        self.assertEqual(repeated_forwarded[0]["status"], 400)
        matching_families = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b"host=archive.lan;proto=http"), (b"x-forwarded-host", b"archive.lan"), (b"x-forwarded-proto", b"http")]))
        self.assertEqual(matching_families[0]["status"], 204)
        normalized_default_port = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b"host=archive.lan:80;proto=http"), (b"x-forwarded-host", b"archive.lan"), (b"x-forwarded-proto", b"http")]))
        self.assertEqual(normalized_default_port[0]["status"], 204)
        quoted_ipv6 = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b'for="[2001:db8::2]";host="[2001:db8::1]:443";proto=https')], allowed_hosts=("2001:db8::1",)))
        self.assertEqual(quoted_ipv6[0]["status"], 204)
        invalid_port = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b"host=archive.lan:99999;proto=https")]))
        self.assertEqual(invalid_port[0]["status"], 400)
        invalid_syntax = asyncio.run(invoke("10.0.0.8", [(b"host", b"internal"), (b"forwarded", b'host="archive.lan;proto=https')]))
        self.assertEqual(invalid_syntax[0]["status"], 400)
        self.assertEqual(calls, 5)

    def test_legacy_non_finite_database_is_json_safe_across_web_endpoints(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        writer = sqlite3.connect(db)
        try:
            writer.execute("UPDATE conversations SET create_time = ? WHERE conversation_id = 'web-1'", (float("inf"),))
            writer.execute("UPDATE conversation_nodes SET update_time = ? WHERE conversation_id = 'web-1' AND node_id = 'u1'", (float("-inf"),))
            writer.commit()
        finally:
            writer.close()
        stats = client.get("/api/stats")
        self.assertEqual(stats.status_code, 200)
        json.dumps(stats.json(), allow_nan=False)
        listing = client.get("/api/conversations?limit=10")
        self.assertEqual(listing.status_code, 200)
        item = next(row for row in listing.json()["items"] if row["conversation_id"] == "web-1")
        self.assertIsNone(item["create_time"])
        detail = client.get("/api/conversations/web-1")
        self.assertEqual(detail.status_code, 200)
        self.assertIsNone(detail.json()["create_time"])
        messages = client.get("/api/conversations/web-1/messages?path=all&include_internal=true")
        self.assertEqual(messages.status_code, 200)
        bad_node = next(row for row in messages.json()["items"] if row["node_id"] == "u1")
        self.assertIsNone(bad_node["update_time"])
        exported = client.get("/api/conversations/web-1/export?format=md&path=current")
        self.assertEqual(exported.status_code, 200)

    def test_upload_source_predicate_rejects_pseudo_shards_synchronously(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
        self.addCleanup(client.close)
        for name in ("conversations-foo.json", "conversations-.json", "conversations--1.json", "conversations-1.5.json"):
            path = base / (name.replace("/", "_") + ".zip")
            write_zip_members(path, {name: []})
            with path.open("rb") as handle:
                response = client.post("/api/import/upload", files={"file": (path.name, handle, "application/zip")})
            self.assertEqual(response.status_code, 400, name)
            self.assertEqual(response.json()["detail"], "upload_zip_no_conversation_sources")
        valid = base / "valid-shards.zip"
        write_zip_members(valid, {"nested/conversations-001.json": []})
        with valid.open("rb") as handle:
            response = client.post("/api/import/upload", files={"file": (valid.name, handle, "application/zip")})
        self.assertEqual(response.status_code, 200)
        self.wait_job(client, response.json()["job_id"])

    def test_web_import_jobs_preserve_structured_file_failure_diagnostics(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        db = base / "archive.db"
        client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
        self.addCleanup(client.close)
        cases = [
            ("invalid-json", {"conversations.json": b"[{"}, "invalid_conversation_json", "json_decode", "json_decode_failed"),
            ("invalid-encoding", {"conversations.json": b"\xef\xbb\xbf\xef\xbb\xbf[]"}, "invalid_conversation_encoding", "json_decode", "json_decode_failed"),
            ("top-level", {"conversations.json": {"not": "a list"}}, "conversation_json_top_level_not_list", "top_level_contract", "top_level_contract_failed"),
            (
                "mixed-shards",
                {"conversations-000.json": [conv("good-before-bad", "Good", {"root": root([])}, "root", 1)], "conversations-001.json": b"{"},
                "conversation_json_top_level_not_list",
                "top_level_contract",
                "top_level_contract_failed",
            ),
        ]
        for label, members, code, stage, outcome in cases:
            with self.subTest(label=label):
                source = base / f"{label}.zip"
                write_zip_members(source, members)
                with source.open("rb") as handle:
                    response = client.post("/api/import/upload", files={"file": (source.name, handle, "application/zip")})
                self.assertEqual(response.status_code, 200, response.text)
                job = self.wait_job(client, response.json()["job_id"])
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["error_code"], code)
                self.assertEqual(job["stage"], stage)
                self.assertEqual(job["outcome"], outcome)
                self.assertFalse(job["canonical_commit_succeeded"])
                self.assertEqual(job["summary"]["failure_code"], code)
                run_id = job["summary"]["import_run_id"]
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                try:
                    run = conn.execute("SELECT status, summary_json FROM import_runs WHERE id = ?", (run_id,)).fetchone()
                    stored = json.loads(run["summary_json"])
                    warning_rows = conn.execute("SELECT warning_type FROM import_warnings WHERE import_run_id = ?", (run_id,)).fetchall()
                finally:
                    conn.close()
                self.assertEqual(run["status"], "failed")
                self.assertEqual([row["warning_type"] for row in warning_rows], [code])
                self.assertEqual(stored["warnings"], len(warning_rows))
                self.assertEqual(job["summary"]["warnings"], len(warning_rows))

    def test_cleanup_upload_dir_reports_failure_incomplete_and_partial_cleanup(self):
        from chatgpt_export_archiver import web_jobs

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            locked = base / "locked"
            locked.mkdir()
            (locked / "file.tmp").write_bytes(b"synthetic")
            with mock.patch.object(web_jobs.shutil, "rmtree", side_effect=PermissionError("locked")):
                result = web_jobs.cleanup_upload_dir(locked)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_type"], "PermissionError")
            self.assertTrue(result["path_still_exists"])

            incomplete = base / "incomplete"
            incomplete.mkdir()
            with mock.patch.object(web_jobs.shutil, "rmtree", return_value=None):
                result = web_jobs.cleanup_upload_dir(incomplete)
            self.assertFalse(result["ok"])
            self.assertIsNone(result["error_type"])
            self.assertTrue(result["path_still_exists"])

            partial = base / "partial"
            partial.mkdir()
            child = partial / "child.tmp"
            child.write_bytes(b"synthetic")

            def partial_remove(_path):
                child.unlink()
                raise PermissionError("directory locked")

            with mock.patch.object(web_jobs.shutil, "rmtree", side_effect=partial_remove):
                result = web_jobs.cleanup_upload_dir(partial)
            self.assertFalse(result["ok"])
            self.assertTrue(result["partial_cleanup"])
            self.assertFalse(child.exists())

    def test_effective_current_python_sql_parity_and_scoped_scaling(self):
        from chatgpt_export_archiver.current_path import (
            effective_current_metadata,
            ensure_effective_current_views,
            resolve_effective_current_collection,
        )
        from chatgpt_export_archiver.db import init_db

        fixtures = {
            "normal": ("a", [("root", None, 1), ("a", "root", 1)], set()),
            "zero-flags": ("a", [("root", None, 0), ("a", "root", 0)], set()),
            "stray-flag": ("a", [("root", None, 1), ("a", "root", 1), ("branch", "root", 1)], set()),
            "invalid-current-flags": ("missing", [("root", None, 1), ("a", "root", 1)], set()),
            "fallback-all": ("missing", [("root", None, 0), ("a", "root", 0)], set()),
            "cycle": ("a", [("a", "b", 0), ("b", "a", 0)], set()),
            "missing-parent": ("a", [("a", "gone", 0)], set()),
            "cross-parent": ("a", [("a", "foreign", 0)], {"foreign"}),
            "multiple-leaves": ("missing", [("root", None, 1), ("a", "root", 1), ("b", "root", 1)], set()),
            "raw-cycle-two": ("missing", [("a", "b", 1), ("b", "a", 1)], set()),
            "raw-cycle-three": ("missing", [("a", "c", 1), ("b", "a", 1), ("c", "b", 1)], set()),
            "raw-cycle-with-leaf": ("missing", [("a", "b", 1), ("b", "a", 1), ("leaf", None, 1)], set()),
            "raw-cycle-with-leaves": ("missing", [("a", "b", 1), ("b", "a", 1), ("leaf-a", None, 1), ("leaf-b", None, 1)], set()),
            "valid-current-stray-raw-cycle": ("cur", [("root", None, 0), ("cur", "root", 0), ("a", "b", 1), ("b", "a", 1)], set()),
            "no-current-no-flags": (None, [("root", None, 0)], set()),
            "empty": (None, [], set()),
        }
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        try:
            for cid, (current_node, raw_rows, foreign_ids) in fixtures.items():
                conn.execute(
                    "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, ?, ?, ?)",
                    (cid, cid, current_node, cid),
                )
                rows = []
                for index, (node_id, parent_id, flag) in enumerate(raw_rows):
                    conn.execute(
                        "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, create_time, is_on_current_path) VALUES (?, ?, ?, ?, ?)",
                        (cid, node_id, parent_id, float(index), flag),
                    )
                    rows.append({"node_id": node_id, "parent_node_id": parent_id, "create_time": float(index), "update_time": None, "is_on_current_path": flag})
                python_result = resolve_effective_current_collection(
                    current_node,
                    rows,
                    foreign_node_ids=foreign_ids,
                )
                fixtures[cid] = (python_result, raw_rows, foreign_ids)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES ('foreign-owner', 'foreign', 'foreign')"
            )
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, is_on_current_path) VALUES ('foreign-owner', 'foreign', 0)"
            )
            conn.commit()
            metadata = effective_current_metadata(conn, fixtures.keys())
            for cid, (python_result, _raw_rows, _foreign_ids) in fixtures.items():
                with self.subTest(fixture=cid):
                    sql_result = metadata[cid]
                    self.assertEqual(sql_result["current_collection_source"], python_result.source)
                    self.assertEqual(sql_result["current_node_exists"], python_result.current_node_exists)
                    self.assertEqual(sql_result["current_path_fallback_to_all"], python_result.current_path_fallback_to_all)
                    self.assertEqual(sql_result["effective_path"], python_result.effective_path)
                    self.assertEqual(sql_result["cycle_detected"], python_result.cycle_detected)
                    self.assertEqual(sql_result["missing_parent"], python_result.missing_parent)
                    self.assertEqual(sql_result["cross_conversation_parent"], python_result.cross_conversation_parent)
                    self.assertEqual(sql_result["partial_chain"], python_result.partial_chain)
                    self.assertEqual(sql_result["current_path_nodes"], python_result.raw_flag_count)
                    self.assertEqual(sql_result["raw_flag_leaf_count"], python_result.raw_flag_leaf_count)
                    for field in (
                        "selected_chain_cycle_detected",
                        "raw_flag_cycle_detected",
                        "selected_chain_missing_parent",
                        "raw_flag_missing_parent",
                        "selected_chain_cross_conversation_parent",
                        "raw_flag_cross_conversation_parent",
                    ):
                        self.assertEqual(sql_result[field], getattr(python_result, field))

            for cid in (
                "raw-cycle-two",
                "raw-cycle-three",
                "raw-cycle-with-leaf",
                "raw-cycle-with-leaves",
                "valid-current-stray-raw-cycle",
            ):
                self.assertTrue(metadata[cid]["raw_flag_cycle_detected"])
                self.assertTrue(metadata[cid]["cycle_detected"])
            self.assertEqual(metadata["valid-current-stray-raw-cycle"]["current_collection_source"], "current_node")

            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, ?, ?, ?)",
                ((f"scale-{index}", "scale", f"node-{index}", f"hash-{index}") for index in range(10_000)),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, is_on_current_path) VALUES (?, ?, 0)",
                ((f"scale-{index}", f"node-{index}") for index in range(10_000)),
            )
            conn.commit()
            ensure_effective_current_views(conn, ["scale-0"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM effective_current_scope").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM effective_current_nodes").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM effective_current_meta").fetchone()[0], 1)
        finally:
            conn.close()

        for depth in (1_000, 5_000, 10_000):
            with self.subTest(chain_depth=depth):
                chain = sqlite3.connect(":memory:")
                chain.row_factory = sqlite3.Row
                init_db(chain)
                chain.execute(
                    "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES ('chain', 'chain', ?, 'chain')",
                    (f"n{depth - 1}",),
                )
                chain.executemany(
                    "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, is_on_current_path) VALUES ('chain', ?, ?, 0)",
                    ((f"n{index}", f"n{index - 1}" if index else None) for index in range(depth)),
                )
                chain.commit()
                ensure_effective_current_views(chain, ["chain"])
                self.assertEqual(chain.execute("SELECT COUNT(*) FROM effective_current_nodes").fetchone()[0], depth)
                self.assertFalse(effective_current_metadata(chain, ["chain"])["chain"]["partial_chain"])
                chain.close()

    def test_single_conversation_effective_scope_uses_indexed_plan_and_bounded_vm_work(self):
        from chatgpt_export_archiver.current_path import (
            _SCOPED_EFFECTIVE_CURRENT_SQL,
            ensure_effective_current_views,
            invalidate_effective_current_cache,
        )
        from chatgpt_export_archiver.db import init_db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conversation_count = 20_000
        conn.executemany(
            "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, 'scale', ?, ?)",
            ((f"scale-{index}", f"n{index}-4", f"hash-{index}") for index in range(conversation_count)),
        )
        conn.executemany(
            "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, is_on_current_path) VALUES (?, ?, ?, 0)",
            (
                (f"scale-{index}", f"n{index}-{node_index}", f"n{index}-{node_index - 1}" if node_index else None)
                for index in range(conversation_count)
                for node_index in range(5)
            ),
        )
        conn.commit()
        steps = [0]
        conn.set_progress_handler(lambda: (steps.__setitem__(0, steps[0] + 1), 0)[1], 1)
        try:
            ensure_effective_current_views(conn, ["scale-0"])
        finally:
            conn.set_progress_handler(None, 0)
        self.assertLess(steps[0], 10_000)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM effective_current_scope").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM effective_current_nodes").fetchone()[0], 5)
        plan = "\n".join(
            str(row[3])
            for row in conn.execute("EXPLAIN QUERY PLAN " + _SCOPED_EFFECTIVE_CURRENT_SQL)
        )
        self.assertIn("SCAN scope", plan)
        self.assertIn("SEARCH c USING INDEX", plan)
        self.assertIn("idx_nodes_conversation_flag_parent", plan)
        self.assertFalse(any(line.startswith("SCAN c ") for line in plan.splitlines()))

        # Invalid current + no raw flags takes the per-conversation fallback,
        # but unrelated nodes still must not affect the work bound.
        conn.execute("UPDATE conversations SET current_node = 'missing' WHERE conversation_id = 'scale-0'")
        invalidate_effective_current_cache(conn)
        fallback_steps = [0]
        conn.set_progress_handler(lambda: (fallback_steps.__setitem__(0, fallback_steps[0] + 1), 0)[1], 1)
        try:
            ensure_effective_current_views(conn, ["scale-0"])
        finally:
            conn.set_progress_handler(None, 0)
            conn.close()
        self.assertLess(fallback_steps[0], 10_000)

    def test_round10_single_conversation_effective_current_uses_bounded_python_heap(self):
        from chatgpt_export_archiver.current_path import ensure_effective_current_views
        from chatgpt_export_archiver.db import init_db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        node_count = 25_000
        conn.execute(
            "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) "
            "VALUES ('bounded-chain', 'Synthetic', ?, 'h')",
            (f"n{node_count - 1:05d}",),
        )
        conn.executemany(
            "INSERT INTO conversation_nodes("
            "conversation_id, node_id, parent_node_id, is_on_current_path"
            ") VALUES ('bounded-chain', ?, ?, 1)",
            (
                (f"n{index:05d}", f"n{index - 1:05d}" if index else None)
                for index in range(node_count)
            ),
        )
        conn.commit()
        tracing_before = tracemalloc.is_tracing()
        if not tracing_before:
            tracemalloc.start()
        tracemalloc.reset_peak()
        baseline = tracemalloc.get_traced_memory()[0]
        try:
            ensure_effective_current_views(conn, ["bounded-chain"])
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            if not tracing_before:
                tracemalloc.stop()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM effective_current_nodes").fetchone()[0],
            node_count,
        )
        self.assertLess(peak - baseline, 16 * 1024 * 1024)
        metadata = conn.execute("SELECT * FROM effective_current_meta").fetchone()
        self.assertFalse(metadata["cycle_detected"])
        self.assertFalse(metadata["partial_chain"])
        conn.close()

    def test_empty_and_filter_only_search_defer_effective_current_materialization(self):
        from chatgpt_export_archiver import current_path as current_path_module
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.db import init_db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.executemany(
            "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, ?, ?, ?)",
            ((f"empty-{index}", "synthetic", f"n{index}", f"h{index}") for index in range(100)),
        )
        conn.executemany(
            "INSERT INTO conversation_nodes(conversation_id, node_id, role, content_text, is_on_current_path) VALUES (?, ?, 'user', 'synthetic', 0)",
            ((f"empty-{index}", f"n{index}") for index in range(100)),
        )
        conn.commit()
        scopes: list[tuple[str, ...] | None] = []
        real_ensure = current_path_module.ensure_effective_current_views

        def spy(connection, ids):
            normalized = None if ids is None else tuple(sorted(str(value) for value in ids))
            scopes.append(normalized)
            return real_ensure(connection, normalized)

        with (
            mock.patch.object(current_path_module, "ensure_effective_current_views", side_effect=spy),
            mock.patch.object(search_module, "ensure_effective_current_views", side_effect=spy),
        ):
            for parsed in (
                search_module.parse_query(""),
                search_module.parse_query("synthetic", scope="title"),
                search_module.parse_query("role:user"),
                search_module.parse_query("", exclude="synthetic"),
            ):
                scopes.clear()
                page = search_module.search_messages(conn, parsed, limit=20)
                self.assertEqual(page["items"], [])
                self.assertEqual(page["total"], 0)
                self.assertTrue(page["total_exact"])
                self.assertEqual(scopes, [])

            scopes.clear()
            page = search_module.search_conversations(
                conn,
                search_module.parse_query(""),
                limit=20,
                selected_id="empty-99",
            )
            self.assertEqual(len(page["items"]), 20)
            self.assertNotIn(None, scopes)
            self.assertTrue(scopes)
            self.assertLessEqual(max(len(scope or ()) for scope in scopes), 21)

            scopes.clear()
            role_page = search_module.search_conversations(
                conn,
                search_module.parse_query("role:user"),
                limit=20,
            )
            self.assertEqual(len(role_page["items"]), 20)
            self.assertEqual(role_page["total"], 100)
        conn.close()

    def test_effective_current_cache_invalidates_after_same_connection_graph_mutations(self):
        from chatgpt_export_archiver.current_path import ensure_effective_current_views
        from chatgpt_export_archiver.db import init_db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        try:
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES ('mutable', 'mutable', 'root', 'mutable')"
            )
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, is_on_current_path) VALUES ('mutable', 'root', 0)"
            )
            conn.commit()

            def effective_ids():
                ensure_effective_current_views(conn, ["mutable"])
                return {
                    row["node_id"]
                    for row in conn.execute("SELECT node_id FROM effective_current_nodes WHERE conversation_id = 'mutable'")
                }

            self.assertEqual(effective_ids(), {"root"})
            conn.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, is_on_current_path) VALUES ('mutable', 'child', 'root', 0)"
            )
            conn.execute("UPDATE conversations SET current_node = 'child' WHERE conversation_id = 'mutable'")
            self.assertEqual(effective_ids(), {"root", "child"})
            conn.execute("UPDATE conversation_nodes SET parent_node_id = NULL WHERE conversation_id = 'mutable' AND node_id = 'child'")
            self.assertEqual(effective_ids(), {"child"})
            conn.execute("DELETE FROM conversation_nodes WHERE conversation_id = 'mutable' AND node_id = 'child'")
            self.assertEqual(effective_ids(), {"root"})
        finally:
            conn.close()

    def test_conversation_relevance_ignores_raw_path_flag_differences(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        source = base / "ranking.zip"
        mapping = {
            "root": root(["u"]),
            "u": node("u", "root", "user", "identical-ranking-token", 100, ["a", "branch"]),
            "a": node("a", "u", "assistant", "identical-ranking-token", 101),
            "branch": node("branch", "u", "assistant", "irrelevant branch", 102),
        }
        write_zip(
            source,
            [
                conv("rank-a", "Same title", mapping, "a", 99),
                conv("rank-b", "Same title", mapping, "a", 99),
            ],
        )
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(source), "--no-input-sha256"]), 0)
        writer = sqlite3.connect(db)
        try:
            writer.execute("UPDATE conversation_nodes SET is_on_current_path = 0 WHERE conversation_id = 'rank-b'")
            writer.execute("UPDATE conversation_nodes SET is_on_current_path = 1 WHERE conversation_id = 'rank-b' AND node_id = 'branch'")
            writer.commit()
            from chatgpt_export_archiver.db import migrate_database
            migrate_database(writer, refresh_compatibility=True)
            writer.commit()
        finally:
            writer.close()
        client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
        self.addCleanup(client.close)
        page = client.get("/api/conversations?q=identical-ranking-token&path=current&sort=relevance").json()
        items = {item["conversation_id"]: item for item in page["items"]}
        self.assertEqual(set(items), {"rank-a", "rank-b"})
        self.assertEqual(items["rank-a"]["score"], items["rank-b"]["score"])
        self.assertEqual(items["rank-a"]["current_collection_source"], "current_node")
        self.assertEqual(items["rank-b"]["current_collection_source"], "current_node")

    def test_total_exact_around_metadata_and_selected_item_contracts(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        exact = client.get("/api/search/messages?q=python&path=all&count_total=true&limit=1").json()
        self.assertTrue(exact["total_exact"])
        approximate = client.get("/api/search/messages?q=python&path=all&count_total=false&limit=1").json()
        self.assertFalse(approximate["total_exact"])
        empty = client.get("/api/search/messages?q=does-not-exist&count_total=false&limit=1").json()
        self.assertTrue(empty["total_exact"])
        self.assertEqual(empty["total"], 0)

        selected = client.get("/api/conversations?limit=1&offset=0&sort=created&selected_id=web-1").json()
        if not any(item["conversation_id"] == "web-1" for item in selected["items"]):
            self.assertEqual(selected["selected_item"]["conversation_id"], "web-1")
            self.assertIn("partial_chain", selected["selected_item"])

        visible = client.get("/api/conversations/web-1/messages?path=all&include_internal=false&around_node_id=u1&limit=1").json()
        self.assertTrue(visible["around_target_found"])
        self.assertTrue(visible["around_target_visible"])
        self.assertTrue(visible["around_target_in_effective_collection"])
        self.assertTrue(visible["around_target_applied"])
        hidden = client.get("/api/conversations/web-1/messages?path=all&include_internal=false&around_node_id=root&limit=1").json()
        self.assertTrue(hidden["around_target_found"])
        self.assertFalse(hidden["around_target_visible"])
        self.assertFalse(hidden["around_target_applied"])
        off_current = client.get("/api/conversations/web-1/messages?path=current&include_internal=true&around_node_id=b1&limit=1").json()
        self.assertTrue(off_current["around_target_found"])
        self.assertFalse(off_current["around_target_in_effective_collection"])
        self.assertFalse(off_current["around_target_applied"])
        off_current_all = client.get("/api/conversations/web-1/messages?path=all&include_internal=true&around_node_id=b1&limit=1").json()
        self.assertTrue(off_current_all["around_target_found"])
        self.assertFalse(off_current_all["around_target_in_effective_collection"])
        self.assertTrue(off_current_all["around_target_in_requested_collection"])
        self.assertTrue(off_current_all["around_target_visible"])
        self.assertTrue(off_current_all["around_target_applied"])
        missing = client.get("/api/conversations/web-1/messages?path=all&around_node_id=missing&limit=1").json()
        self.assertFalse(missing["around_target_found"])
        self.assertFalse(missing["around_target_applied"])

    def test_legacy_health_dependency_gate_migration_and_web_index_contract(self):
        from chatgpt_export_archiver.db import DatabaseMigrationError, connect, migrate_database
        from chatgpt_export_archiver.web_db import create_web_indexes, web_index_status

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "legacy.db"
            fixture = Path(__file__).resolve().parent / "fixtures" / "legacy-fa37b3d.sql"
            conn = sqlite3.connect(db)
            conn.executescript(fixture.read_text(encoding="utf-8"))
            conn.execute("CREATE TABLE web_message_norm(marker TEXT)")
            conn.execute("INSERT INTO web_message_norm VALUES ('legacy-marker')")
            conn.commit()
            conn.close()

            before_tables = None
            conn = sqlite3.connect(db)
            before_tables = conn.execute(
                "SELECT name, type, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            conn.close()
            with self.assertRaises(DatabaseMigrationError) as caught:
                create_web_indexes(db)
            self.assertEqual(caught.exception.code, "database_migration_required")
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT marker FROM web_message_norm").fetchone()[0], "legacy-marker")
            self.assertEqual(before_tables, conn.execute("SELECT name, type, sql FROM sqlite_master ORDER BY name").fetchall())
            conn.close()

            client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            degraded = health.json()
            self.assertFalse(degraded["ok"])
            self.assertFalse(degraded["db_ready"])
            self.assertFalse(degraded["schema_compatible"])
            self.assertTrue(degraded["migration_required"])
            self.assertEqual(degraded["current_database_schema_version"], 0)
            self.assertIn("idx_nodes_conversation_flag_parent", degraded["missing_indexes"])
            for endpoint in (
                "/api/conversations",
                "/api/conversations/legacy-synthetic",
                "/api/conversations/legacy-synthetic/messages",
                "/api/search/messages?q=synthetic",
                "/api/search/suggest?q=synthetic",
            ):
                with self.subTest(endpoint=endpoint):
                    response = client.get(endpoint)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()["detail"]["code"], "database_migration_required")
            client.close()

            writer = connect(db)
            with self.assertRaises(DatabaseMigrationError) as collision:
                migrate_database(writer)
            self.assertEqual(collision.exception.code, "optional_index_name_collision")
            writer.close()
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT marker FROM web_message_norm").fetchone()[0], "legacy-marker")
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 0)
            conn.execute("DROP TABLE web_message_norm")
            conn.commit()
            conn.close()

            writer = connect(db)
            migration = migrate_database(writer)
            self.assertTrue(migration["changed"])
            writer.close()
            client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
            ready = client.get("/api/health").json()
            self.assertTrue(ready["ok"])
            self.assertTrue(ready["db_ready"])
            self.assertTrue(ready["schema_compatible"])
            self.assertFalse(ready["migration_required"])
            self.assertEqual(client.get("/api/conversations").status_code, 200)
            self.assertEqual(client.get("/api/conversations/legacy-synthetic").status_code, 200)
            self.assertEqual(client.get("/api/conversations/legacy-synthetic/messages").status_code, 200)
            self.assertEqual(client.get("/api/search/messages?q=synthetic").status_code, 200)
            client.close()

            built = create_web_indexes(db)
            self.assertEqual(built["indexed_titles"], 1)
            conn = connect_readonly(db)
            status = web_index_status(conn)
            conn.close()
            self.assertTrue(status["web_normalized_indexed"])

    def test_web_index_disk_preflight_preserves_published_index(self):
        from chatgpt_export_archiver.disk_resources import DiskSpaceInsufficientError
        from chatgpt_export_archiver.web_db import (
            WebIndexBuildError,
            create_web_indexes,
            web_index_status,
        )

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        self.addCleanup(client.close)
        create_web_indexes(db)
        conn = sqlite3.connect(db)
        before_schema = conn.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE name LIKE 'web_%' ORDER BY name"
        ).fetchall()
        before_rows = conn.execute("SELECT COUNT(*) FROM web_message_norm").fetchone()[0]
        conn.close()
        failure = DiskSpaceInsufficientError(
            "web_index_disk_space_insufficient",
            required_bytes=900,
            free_bytes=100,
        )
        with mock.patch(
            "chatgpt_export_archiver.web_db.require_free_space",
            side_effect=failure,
        ):
            with self.assertRaises(WebIndexBuildError) as caught:
                create_web_indexes(db)
        self.assertEqual(caught.exception.code, "web_index_disk_space_insufficient")
        conn = connect_readonly(db)
        try:
            self.assertTrue(web_index_status(conn)["web_normalized_indexed"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM web_message_norm").fetchone()[0], before_rows)
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT name, type, sql FROM sqlite_master WHERE name LIKE 'web_%' ORDER BY name"
                ).fetchall()],
                before_schema,
            )
            private_names = conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'wai_%'"
            ).fetchall()
            self.assertEqual(private_names, [])
        finally:
            conn.close()

    def test_missing_required_index_is_migration_required_not_operational_error(self):
        from chatgpt_export_archiver.db import connect, migrate_database

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        client.close()
        conn = sqlite3.connect(db)
        conn.execute("DROP INDEX idx_nodes_conversation_flag_parent")
        conn.commit()
        conn.close()
        client = TestClient(create_app(db, static_dir=self.make_build_dir(Path(td.name))))
        self.addCleanup(client.close)
        health = client.get("/api/health").json()
        self.assertFalse(health["db_ready"])
        self.assertTrue(health["migration_required"])
        self.assertEqual(health["current_database_schema_version"], health["required_database_schema_version"])
        response = client.get("/api/conversations")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "database_migration_required")
        writer = connect(db)
        self.assertTrue(migrate_database(writer)["changed"])
        writer.close()
        self.assertTrue(client.get("/api/health").json()["db_ready"])
        self.assertEqual(client.get("/api/conversations").status_code, 200)

    def test_title_only_current_search_materializes_only_page_and_selected_scope(self):
        from chatgpt_export_archiver import current_path as current_path_module
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.db import connect, init_db

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "titles.db"
            conn = connect(db)
            init_db(conn)
            count = 20_000
            conn.executemany(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, ?, ?, ?)",
                ((f"title-{index:05d}", f"Needle {index:05d}", f"node-{index}", f"hash-{index}") for index in range(count)),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, is_on_current_path) VALUES (?, ?, 0)",
                ((f"title-{index:05d}", f"node-{index}") for index in range(count)),
            )
            conn.commit()

            real_ensure = current_path_module.ensure_effective_current_views
            scopes: list[tuple[str, ...] | None] = []

            def spy(connection, ids):
                if ids is None:
                    scopes.append(None)
                    return real_ensure(connection, None)
                captured: list[str] = []

                def observed_ids():
                    for value in ids:
                        captured.append(str(value))
                        yield str(value)

                result = real_ensure(connection, observed_ids())
                scopes.append(tuple(sorted(captured)))
                return result

            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            with (
                mock.patch.object(current_path_module, "ensure_effective_current_views", side_effect=spy),
                mock.patch.object(search_module, "ensure_effective_current_views", side_effect=spy),
            ):
                queries = (
                    search_module.parse_query("title:needle", path_default="current"),
                    search_module.parse_query("needle", path_default="current", scope="title"),
                    search_module.parse_query("", path_default="current", title="needle"),
                    search_module.parse_query("", path_default="current", exact="needle", scope="title"),
                    search_module.parse_query("needle OR absent", path_default="current", scope="title"),
                )
                for parsed in queries:
                    scopes.clear()
                    statements.clear()
                    current = search_module.search_conversations(
                        conn,
                        parsed,
                        limit=20,
                        selected_id="title-19999",
                        sort="title",
                    )
                    self.assertEqual(current["total"], count)
                    self.assertEqual(current["selected_item"]["conversation_id"], "title-19999")
                    self.assertNotIn(None, scopes)
                    self.assertTrue(scopes)
                    self.assertLessEqual(max(len(scope or ()) for scope in scopes), 21)
                    self.assertFalse(any(
                        "INSERT INTO effective_current_scope SELECT conversation_id FROM conversations" in sql
                        for sql in statements
                    ))
                    parsed_all = search_module.parse_query(
                        parsed.original,
                        path_default="all",
                        title=parsed.required_title,
                        scope=parsed.scope,
                    )
                    all_page = search_module.search_conversations(
                        conn,
                        parsed_all,
                        limit=20,
                        selected_id="title-19999",
                        sort="title",
                    )
                    self.assertEqual(
                        [item["conversation_id"] for item in current["items"]],
                        [item["conversation_id"] for item in all_page["items"]],
                    )
            conn.set_trace_callback(None)
            conn.close()
            refresh_test_database_compatibility(db)

            client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
            self.addCleanup(client.close)
            scopes.clear()
            with (
                mock.patch.object(current_path_module, "ensure_effective_current_views", side_effect=spy),
                mock.patch.object(search_module, "ensure_effective_current_views", side_effect=spy),
            ):
                response = client.get(
                    "/api/conversations?q=title%3Aneedle&path=current&sort=title&limit=20&selected_id=title-19999"
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], count)
            self.assertNotIn(None, scopes)
            self.assertLessEqual(max(len(scope or ()) for scope in scopes), 21)

    def test_global_current_search_materializes_only_path_independent_candidates(self):
        from chatgpt_export_archiver import current_path as current_path_module
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.db import connect, init_db
        from chatgpt_export_archiver.web_db import create_web_indexes

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "candidate-scope.db"
            writer = connect(db)
            init_db(writer)
            count = 20_000
            rare = {17, 9_999, 19_998}
            writer.executemany(
                "INSERT INTO conversations(conversation_id, title, current_node, source_file, aggregate_hash) VALUES (?, 'Synthetic', ?, 'synthetic.json', ?)",
                ((f"candidate-{index:05d}", f"node-{index}", f"hash-{index}") for index in range(count)),
            )
            writer.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, role, content_text, is_on_current_path) VALUES (?, ?, 'user', ?, 0)",
                (
                    (f"candidate-{index:05d}", f"node-{index}", "rare-candidate-token" if index in rare else "ordinary synthetic text")
                    for index in range(count)
                ),
            )
            writer.commit()
            writer.close()
            create_web_indexes(db)

            conn = connect_readonly(db)
            real_ensure = current_path_module.ensure_effective_current_views
            scopes: list[tuple[str, ...] | None] = []

            def spy(connection, ids):
                if ids is None:
                    scopes.append(None)
                    return real_ensure(connection, None)
                captured: list[str] = []

                def observed_ids():
                    for value in ids:
                        captured.append(str(value))
                        yield str(value)

                result = real_ensure(connection, observed_ids())
                scopes.append(tuple(sorted(captured)))
                return result

            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            parsed = search_module.parse_query("rare-candidate-token", path_default="current", scope="message")
            with (
                mock.patch.object(current_path_module, "ensure_effective_current_views", side_effect=spy),
                mock.patch.object(search_module, "ensure_effective_current_views", side_effect=spy),
            ):
                message_page = search_module.search_messages(conn, parsed, limit=10, count_total=False)
                self.assertEqual({item["conversation_id"] for item in message_page["items"]}, {f"candidate-{index:05d}" for index in rare})
                self.assertNotIn(None, scopes)
                self.assertLessEqual(max(len(scope or ()) for scope in scopes), len(rare))
                self.assertFalse(any("INSERT INTO effective_current_scope SELECT conversation_id FROM conversations" in sql for sql in statements))

                scopes.clear()
                statements.clear()
                conversation_page = search_module.search_conversations(conn, parsed, limit=10)
                self.assertEqual({item["conversation_id"] for item in conversation_page["items"]}, {f"candidate-{index:05d}" for index in rare})
                self.assertNotIn(None, scopes)
                self.assertLessEqual(max(len(scope or ()) for scope in scopes), len(rare))
                self.assertFalse(any("INSERT INTO effective_current_scope SELECT conversation_id FROM conversations" in sql for sql in statements))

                # A committed graph change from another connection invalidates
                # both the optional-index generation and the connection-local
                # effective-current scope before the next search.
                other = connect(db)
                try:
                    other.execute(
                        "INSERT INTO conversations(conversation_id, title, current_node, source_file, aggregate_hash) VALUES ('candidate-new', 'Synthetic', 'node-new', 'synthetic.json', 'hash-new')"
                    )
                    other.execute(
                        "INSERT INTO conversation_nodes(conversation_id, node_id, role, content_text, is_on_current_path) VALUES ('candidate-new', 'node-new', 'user', 'rare-candidate-token', 0)"
                    )
                    other.commit()
                finally:
                    other.close()
                scopes.clear()
                updated = search_module.search_messages(conn, parsed, limit=10, count_total=False)
                self.assertIn("candidate-new", {item["conversation_id"] for item in updated["items"]})
                self.assertNotIn(None, scopes)
                self.assertLessEqual(max(len(scope or ()) for scope in scopes), len(rare) + 1)
            conn.set_trace_callback(None)
            conn.close()

    def test_conversation_exclude_resolves_each_candidate_row_once_without_materialized_hint(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver import web_db as web_db_module
        from chatgpt_export_archiver.db import init_db

        source = Path(search_module.__file__).read_text(encoding="utf-8") + Path(web_db_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("AS MATERIALIZED", source)

        for path in ("all", "current"):
            for match_mode in ("contains", "word"):
                with self.subTest(path=path, match_mode=match_mode):
                    conn = sqlite3.connect(":memory:")
                    conn.row_factory = sqlite3.Row
                    init_db(conn)
                    conn.execute(
                        "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES ('exclude-once', 'Synthetic', 'n2', 'hash')"
                    )
                    raw = json.dumps({"content": {"content_type": "text", "parts": ["legacy safe text"]}})
                    conn.executemany(
                        "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, role, content_text, raw_message_json, is_on_current_path) VALUES ('exclude-once', ?, ?, 'user', ?, ?, 1)",
                        (
                            ("n0", None, "canonical safe zero", None),
                            ("n1", "n0", "[non-text content: legacy]", raw),
                            ("n2", "n1", "canonical safe two", None),
                        ),
                    )
                    conn.commit()
                    real_resolver = search_module.recover_message_display_text
                    calls = {"count": 0}

                    def count_resolver(content_text, raw_message_json, **kwargs):
                        calls["count"] += 1
                        return real_resolver(content_text, raw_message_json, **kwargs)

                    parsed = search_module.parse_query(
                        "",
                        exclude="blocked-fragment",
                        scope="message",
                        path_default=path,
                        match_mode=match_mode,
                    )
                    with mock.patch.object(search_module, "recover_message_display_text", side_effect=count_resolver):
                        page = search_module.search_conversations(conn, parsed, limit=10)
                    self.assertEqual([item["conversation_id"] for item in page["items"]], ["exclude-once"])
                    # Only the legacy placeholder row needs raw recovery;
                    # ordinary canonical rows do not open or decode raw JSON.
                    self.assertEqual(calls["count"], 1)
                    conn.close()

    def test_round6_by_id_routes_address_slash_and_legacy_ids(self):
        from chatgpt_export_archiver.db import init_db

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.db"
            conversation_id = "conversation/with?hash#colon:" + "x" * 600
            node_id = "node/with?hash#colon:" + "y" * 600
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, 'Synthetic', ?, 'h')",
                (conversation_id, node_id),
            )
            raw = json.dumps({"content": {"content_type": "text", "parts": ["synthetic body"]}})
            conn.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       content_hash, raw_message_json, is_on_current_path
                   ) VALUES (?, ?, 'assistant', 'text', 'synthetic body', 'r1', ?, 1)""",
                (conversation_id, node_id, raw),
            )
            conn.commit()
            from chatgpt_export_archiver.db import migrate_database
            migrate_database(conn, refresh_compatibility=True)
            conn.commit()
            conn.close()
            client = TestClient(create_app(db))
            self.addCleanup(client.close)
            detail = client.get("/api/by-id/conversation", params={"conversation_id": conversation_id})
            self.assertEqual(detail.status_code, 200)
            messages = client.get(
                "/api/by-id/messages",
                params={"conversation_id": conversation_id, "around_node_id": node_id},
            )
            self.assertEqual(messages.status_code, 200)
            self.assertEqual(messages.json()["items"][0]["node_id"], node_id)
            for endpoint in ("raw", "display"):
                response = client.get(
                    f"/api/by-id/{endpoint}",
                    params={"conversation_id": conversation_id, "node_id": node_id},
                )
                self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get(
                "/api/by-id/copy", params={"conversation_id": conversation_id}
            ).status_code, 200)
            self.assertEqual(client.get(
                "/api/by-id/export", params={"conversation_id": conversation_id, "format": "txt"}
            ).status_code, 200)

    def test_round6_nul_display_and_index_recall_are_consistent(self):
        from chatgpt_export_archiver.db import init_db
        from chatgpt_export_archiver.search import get_message_display_chunk, get_messages, parse_query, search_messages
        from chatgpt_export_archiver.web_db import create_web_indexes, connect_readonly

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "nul.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES ('c', ?, 'n', 'h')",
                ("abc\x00def needle",),
            )
            conn.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       content_hash, is_on_current_path
                   ) VALUES ('c', 'n', 'assistant', 'text', ?, 'r1', 1)""",
                ("abc\x00def needle",),
            )
            conn.commit()
            first = get_message_display_chunk(conn, "c", "n", offset=0, limit=4)
            self.assertEqual(first["display_text"], "abc\ufffd")
            reader_page = get_messages(
                conn, "c", path="all", limit=10, offset=0, include_internal=True
            )
            self.assertEqual(reader_page["items"][0]["display_text"], "abc\ufffddef needle")
            self.assertTrue(reader_page["items"][0]["display_text_total_chars_exact"])
            before = search_messages(conn, parse_query("needle"), conversation_id="c")
            self.assertEqual([item["node_id"] for item in before["items"]], ["n"])
            conn.close()
            create_web_indexes(db)
            reader = connect_readonly(db)
            after = search_messages(reader, parse_query("needle"), conversation_id="c")
            self.assertEqual([item["node_id"] for item in after["items"]], ["n"])
            reader.close()

    def test_round6_oversized_web_index_row_uses_recall_fallback(self):
        from chatgpt_export_archiver.db import init_db
        from chatgpt_export_archiver.search import parse_query, search_messages
        from chatgpt_export_archiver.web_db import create_web_indexes, connect_readonly

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "oversized.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            text = "a" * (2 * 1024 * 1024 + 32) + " synthetic-oversized-needle"
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES ('c', 'Synthetic', 'n', 'h')"
            )
            conn.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       content_hash, is_on_current_path
                   ) VALUES ('c', 'n', 'assistant', 'text', ?, 'r1', 1)""",
                (text,),
            )
            conn.commit()
            conn.close()
            result = create_web_indexes(db)
            self.assertEqual(result["oversized_messages"], 1)
            reader = connect_readonly(db)
            self.assertEqual(reader.execute(
                "SELECT COUNT(*) FROM web_index_oversized WHERE kind='message'"
            ).fetchone()[0], 1)
            page = search_messages(
                reader, parse_query("synthetic-oversized-needle"), conversation_id="c"
            )
            self.assertEqual([item["node_id"] for item in page["items"]], ["n"])
            reader.close()

    def test_round6_sqlite_integer_boundary_is_rejected_before_bind(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        self.assertEqual(
            client.get("/api/conversations", params={"offset": 1 << 63}).status_code,
            422,
        )

    def test_round6_list_and_detail_bound_oversized_scalar_projection(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        huge = "界" * 400_000
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO conversations(conversation_id, title, source_file, current_node, aggregate_hash, update_time) "
            "VALUES ('huge-scalar', ?, ?, 'n', 'h', 9999999999)",
            (huge, huge),
        )
        conn.execute(
            "INSERT INTO conversation_nodes(conversation_id, node_id, role, content_type, content_text, "
            "content_hash, is_on_current_path) VALUES ('huge-scalar', 'n', 'assistant', 'text', 'zz', 'nh', 1)"
        )
        conn.commit()
        conn.close()
        refresh_test_database_compatibility(db)
        listing = client.get("/api/conversations", params={"limit": 1}).json()["items"][0]
        detail = client.get(
            "/api/by-id/conversation", params={"conversation_id": "huge-scalar"}
        ).json()
        conversation_search = next(
            item for item in client.get("/api/search", params={"q": "zz"}).json()["items"]
            if item["conversation_id"] == "huge-scalar"
        )
        message_search = next(
            item for item in client.get("/api/search/messages", params={"q": "zz"}).json()["items"]
            if item["conversation_id"] == "huge-scalar"
        )
        for item in (listing, detail, conversation_search, message_search):
            self.assertLessEqual(len(item["title"]), 4096)
            self.assertLessEqual(len(item["source_file"]), 4096)
            self.assertTrue(item["title_truncated"])
            self.assertTrue(item["source_file_truncated"])
        self.assertLess(len(json.dumps(detail, ensure_ascii=False).encode("utf-8")), 40_000)

    def test_round6_display_cursor_is_sequential_and_legacy_revision_bound(self):
        from chatgpt_export_archiver.db import init_db
        from chatgpt_export_archiver.search import DisplayCursorError, get_message_display_chunk

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "cursor.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            text = ("ab\x00中🙂" * 180_000) + "tail"
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES ('c', 'Synthetic', 'n', 'h')"
            )
            conn.execute(
                """INSERT INTO conversation_nodes(
                       conversation_id, node_id, role, content_type, content_text,
                       content_hash, is_on_current_path
                   ) VALUES ('c', 'n', 'assistant', 'text', ?, NULL, 1)""",
                (text,),
            )
            conn.commit()
            offset = 0
            cursor = None
            parts: list[str] = []
            statement_counts: list[int] = []
            while True:
                statements: list[str] = []
                conn.set_trace_callback(statements.append)
                chunk = get_message_display_chunk(
                    conn, "c", "n", offset=offset, limit=65_536, cursor=cursor
                )
                conn.set_trace_callback(None)
                self.assertIsNotNone(chunk)
                statement_counts.append(sum(
                    1 for sql in statements if sql.lstrip().upper().startswith("SELECT")
                ))
                parts.append(chunk["display_text"])
                if not chunk["has_more"]:
                    self.assertIsNone(chunk["next_cursor"])
                    break
                self.assertTrue(chunk["next_cursor"])
                cursor = chunk["next_cursor"]
                offset = chunk["next_offset"]
            self.assertEqual("".join(parts), text.replace("\x00", "\ufffd"))
            self.assertTrue(all(count == statement_counts[0] for count in statement_counts))

            first = get_message_display_chunk(conn, "c", "n", offset=0, limit=65_536)
            self.assertTrue(first["next_cursor"])
            conn.execute(
                "UPDATE conversation_nodes SET content_text = ? WHERE conversation_id='c' AND node_id='n'",
                ("z" * len(text),),
            )
            conn.commit()
            with self.assertRaises(DisplayCursorError) as caught:
                get_message_display_chunk(
                    conn, "c", "n", offset=first["next_offset"], limit=65_536,
                    cursor=first["next_cursor"],
                )
            self.assertEqual(caught.exception.code, "display_cursor_stale")
            with self.assertRaises(DisplayCursorError) as required:
                get_message_display_chunk(
                    conn, "c", "n", offset=1_048_577, limit=65_536,
                )
            self.assertEqual(required.exception.code, "display_cursor_required")
            import base64
            invalid_payload = json.dumps([
                conn.execute(
                    "SELECT rowid FROM conversation_nodes WHERE conversation_id='c' AND node_id='n'"
                ).fetchone()[0],
                first["content_revision"],
                2**100,
                first["next_offset"],
            ], separators=(",", ":")).encode("utf-8")
            invalid_cursor = base64.urlsafe_b64encode(invalid_payload).rstrip(b"=").decode("ascii")
            with self.assertRaises(DisplayCursorError) as invalid:
                get_message_display_chunk(
                    conn, "c", "n", offset=first["next_offset"], limit=65_536,
                    cursor=invalid_cursor,
                )
            self.assertEqual(invalid.exception.code, "display_cursor_stale")
            conn.close()

    def test_round6_effective_current_and_export_node_budgets_reject_before_materialization(self):
        from chatgpt_export_archiver.current_path import MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conversation_id = "resource-budget"
        count = MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION + 1
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, 'Synthetic', ?, 'h')",
            (conversation_id, f"n-{count - 1}"),
        )
        conn.executemany(
            """INSERT INTO conversation_nodes(
                   conversation_id, node_id, role, content_type, content_text,
                   content_hash, is_on_current_path
               ) VALUES (?, ?, 'assistant', 'text', 'x', 'h', 0)""",
            ((conversation_id, f"n-{index}") for index in range(count)),
        )
        conn.commit()
        conn.close()
        refresh_test_database_compatibility(db)
        reader = client.get("/api/by-id/messages", params={"conversation_id": conversation_id})
        self.assertEqual(reader.status_code, 413)
        self.assertEqual(reader.json()["detail"], "effective_current_node_limit_exceeded")
        export = client.get("/api/by-id/export", params={"conversation_id": conversation_id})
        self.assertEqual(export.status_code, 413)
        self.assertEqual(export.json()["detail"], "export_node_count_limit_exceeded")
        deep_health = client.get("/api/health", params={"deep": "true"})
        self.assertEqual(deep_health.status_code, 200)
        health = deep_health.json()
        self.assertFalse(health["db_ready"])
        self.assertEqual(health["readiness"], "resource_contract_exceeded")
        self.assertTrue(health["reader_resource_contract_checked"])
        self.assertTrue(health["reader_resource_contract_exact"])
        self.assertEqual(health["reader_resource_contract_violations"], 1)

        from chatgpt_export_archiver.db import verify_database

        verify_conn = sqlite3.connect(db)
        verify_conn.row_factory = sqlite3.Row
        report = verify_database(verify_conn)
        verify_conn.close()
        self.assertFalse(report["ok"])
        self.assertFalse(report["effective_current_exact"])
        self.assertTrue(report["diagnostics_partial"])
        self.assertEqual(report["resource_limit_code"], "effective_current_node_limit_exceeded")

    def test_round6_reader_and_streaming_export_hold_one_read_snapshot(self):
        import threading
        import chatgpt_export_archiver.search as search_module
        import chatgpt_export_archiver.web_api as web_api_module

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        writer = sqlite3.connect(db, timeout=5)
        old_title = writer.execute(
            "SELECT title FROM conversations WHERE conversation_id='web-1'"
        ).fetchone()[0]
        entered = threading.Event()
        release = threading.Event()
        original_ensure = search_module.ensure_effective_current_views

        def paused_ensure(conn, ids):
            result = original_ensure(conn, ids)
            if ids == ["web-1"] and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5))
            return result

        detail_result: list[Any] = []
        with mock.patch.object(search_module, "ensure_effective_current_views", side_effect=paused_ensure):
            thread = threading.Thread(
                target=lambda: detail_result.append(client.get(
                    "/api/by-id/conversation", params={"conversation_id": "web-1"}
                )),
                daemon=True,
            )
            thread.start()
            self.assertTrue(entered.wait(5))
            writer.execute("UPDATE conversations SET title='new committed title' WHERE conversation_id='web-1'")
            writer.commit()
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(detail_result[0].status_code, 200)
        self.assertEqual(detail_result[0].json()["title"], old_title)

        old_body = writer.execute(
            "SELECT content_text FROM conversation_nodes WHERE conversation_id='web-1' AND node_id='a1'"
        ).fetchone()[0]
        entered.clear()
        release.clear()
        original_iter = web_api_module.iter_conversation_export_nodes

        def paused_iter(conn, conv, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original_iter(conn, conv, **kwargs)

        export_result: list[Any] = []
        with mock.patch.object(web_api_module, "iter_conversation_export_nodes", side_effect=paused_iter):
            thread = threading.Thread(
                target=lambda: export_result.append(client.get(
                    "/api/by-id/export",
                    params={"conversation_id": "web-1", "format": "txt", "path": "current"},
                )),
                daemon=True,
            )
            thread.start()
            self.assertTrue(entered.wait(5))
            writer.execute(
                "UPDATE conversation_nodes SET content_text='new committed body', content_hash='new-revision' "
                "WHERE conversation_id='web-1' AND node_id='a1'"
            )
            writer.commit()
            release.set()
            thread.join(5)
        writer.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(export_result[0].status_code, 200)
        self.assertIn(old_body, export_result[0].text)
        self.assertNotIn("new committed body", export_result[0].text)

    def test_round6_streaming_export_failure_releases_snapshot_connection(self):
        import chatgpt_export_archiver.web_api as web_api_module

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)

        def failing_render(*_args, **_kwargs):
            yield "synthetic-prefix"
            raise RuntimeError("synthetic stream failure")

        with mock.patch.object(
            web_api_module, "iter_rendered_conversation", side_effect=failing_render
        ):
            with self.assertRaises(Exception):
                client.get(
                    "/api/by-id/export",
                    params={"conversation_id": "web-1", "format": "txt"},
                )

        writer = sqlite3.connect(db, timeout=0.25)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
        finally:
            writer.close()

    def test_round6_ordinary_reads_never_run_database_wide_foreign_key_check(self):
        import chatgpt_export_archiver.web_api as web_api_module

        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        original_connect = web_api_module.connect_readonly
        statements: list[str] = []

        def traced_connect(path):
            conn = original_connect(path)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(web_api_module, "connect_readonly", side_effect=traced_connect):
            responses = [
                client.get("/api/health"),
                client.get("/api/stats"),
                client.get("/api/conversations?limit=2"),
                client.get("/api/by-id/conversation", params={"conversation_id": "web-1"}),
                client.get("/api/by-id/messages", params={"conversation_id": "web-1", "limit": 2}),
                client.get("/api/search/messages", params={"q": "SQLite", "limit": 2}),
                client.get("/api/search/suggest", params={"q": "python"}),
                client.get("/api/by-id/raw", params={"conversation_id": "web-1", "node_id": "a1"}),
                client.get("/api/by-id/export", params={"conversation_id": "web-1", "format": "txt"}),
            ]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertFalse(any("FOREIGN_KEY_CHECK" in sql.upper() for sql in statements))

        deep_statements: list[str] = []

        def deep_connect(path):
            conn = original_connect(path)
            conn.set_trace_callback(deep_statements.append)
            return conn

        with mock.patch.object(web_api_module, "connect_readonly", side_effect=deep_connect):
            self.assertEqual(client.get("/api/health?deep=true").status_code, 200)
        self.assertTrue(any("FOREIGN_KEY_CHECK" in sql.upper() for sql in deep_statements))

    def test_round7_request_validation_is_bounded_and_never_echoes_input(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        secret = "ROUND7_PRIVATE_" + "x" * 10_000
        response = client.get("/api/conversations", params={"offset": secret})
        self.assertEqual(response.status_code, 422)
        self.assertLess(len(response.content), 8192)
        self.assertNotIn("ROUND7_PRIVATE_", response.text)
        payload = response.json()["detail"]
        self.assertEqual(payload["code"], "invalid_request")
        self.assertLessEqual(len(payload["errors"]), 16)
        self.assertEqual(
            payload["errors"][0],
            {"location": "query", "field": "offset", "code": "invalid_integer"},
        )
        self.assertNotIn("input", response.text.casefold())

        cases = (
            ("/api/conversations?offset=-1", "invalid_offset", "offset"),
            ("/api/conversations?limit=1000", "invalid_limit", "limit"),
            ("/api/by-id/conversation", "missing_parameter", "conversation_id"),
            (
                "/api/conversations?selected_id=" + ("i" * (16 * 1024 + 1)),
                "string_parameter_too_long",
                "selected_id",
            ),
        )
        for url, expected_code, expected_field in cases:
            with self.subTest(url=url[:80]):
                invalid = client.get(url)
                self.assertEqual(invalid.status_code, 422)
                item = invalid.json()["detail"]["errors"][0]
                self.assertEqual(item["code"], expected_code)
                self.assertEqual(item["field"], expected_field)
                self.assertIn(item["location"], {"query", "path", "body", "header", "cookie", "request"})
                self.assertNotIn("input", invalid.text.casefold())

        missing_upload = client.post("/api/import/upload", data={})
        self.assertEqual(missing_upload.status_code, 422)
        upload_item = missing_upload.json()["detail"]["errors"][0]
        self.assertEqual(upload_item["code"], "invalid_upload_metadata")
        self.assertEqual(upload_item["field"], "file")
        openapi = client.get("/openapi.json").json()
        response_schema = openapi["paths"]["/api/conversations"]["get"]["responses"]["422"]
        serialized = json.dumps(response_schema, sort_keys=True)
        self.assertIn("invalid_request", serialized)
        self.assertIn("invalid_upload_metadata", serialized)
        self.assertNotIn("HTTPValidationError", serialized)
        self.assertNotIn('"input"', serialized)
        custom = client.get("/api/schema").json()["request_validation"]
        self.assertEqual(custom["max_errors"], 16)
        self.assertIn("invalid_integer", custom["codes"])

    def test_round7_overlong_legacy_ids_gate_database_readiness(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        legacy_id = "l" * (16 * 1024 + 1)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO conversations(conversation_id, title, aggregate_hash) VALUES (?, 'legacy', 'h')",
            (legacy_id,),
        )
        conn.commit()
        conn.close()
        health = client.get("/api/health").json()
        self.assertFalse(health["db_ready"])
        self.assertEqual(health["database_error_code"], "database_data_incompatible")
        page = client.get("/api/conversations")
        self.assertEqual(page.status_code, 409)
        self.assertNotIn(legacy_id[:256], page.text)

    def test_round10_invalid_utf8_legacy_ids_are_blob_safe_and_incompatible(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO conversations(conversation_id, title, aggregate_hash) "
            "VALUES (CAST(X'80' AS TEXT), 'synthetic', 'bad-id')"
        )
        conn.execute(
            "UPDATE conversations SET exported_id=CAST(X'81' AS TEXT), "
            "current_node=CAST(X'82' AS TEXT) WHERE conversation_id='web-1'"
        )
        conn.execute(
            "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) "
            "VALUES ('web-1', CAST(X'83' AS TEXT), 'synthetic')"
        )
        conn.execute(
            "UPDATE conversation_nodes SET parent_node_id=CAST(X'84' AS TEXT), "
            "message_id=CAST(X'85' AS TEXT) "
            "WHERE conversation_id='web-1' AND node_id='a1'"
        )
        conn.commit()
        conn.close()
        health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["db_ready"])
        self.assertEqual(health.json()["database_error_code"], "database_data_incompatible")
        listing = client.get("/api/conversations")
        self.assertEqual(listing.status_code, 409)
        self.assertEqual(listing.json()["detail"]["code"], "database_data_incompatible")

    def test_round7_deep_legacy_raw_is_bounded_and_reports_incomplete(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        raw = "[" * 300 + "0" + "]" * 300
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET raw_message_json=? WHERE conversation_id='web-1' AND node_id='a1'",
            (raw,),
        )
        conn.commit()
        conn.close()
        response = client.get(
            "/api/by-id/raw", params={"conversation_id": "web-1", "node_id": "a1"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["parsed"])
        self.assertTrue(body["incomplete"])
        self.assertEqual(body["error_code"], "json_nesting_limit_exceeded")
        self.assertEqual(body["raw_size_unit"], "bytes")
        self.assertLess(len(response.content), 100_000)

    def test_round7_literal_placeholder_prefix_remains_canonical_everywhere(self):
        from chatgpt_export_archiver.web_db import create_web_indexes

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        literal = "[non-text content: image] literal-round7-canonical"
        raw = json.dumps(
            {
                "id": "msg-a1",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["different raw fallback"]},
            },
            ensure_ascii=False,
        )
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_type='text', content_text=?, raw_message_json=? "
            "WHERE conversation_id='web-1' AND node_id='a1'",
            (literal, raw),
        )
        conn.commit()
        conn.close()

        page = client.get("/api/by-id/messages", params={"conversation_id": "web-1", "path": "all"}).json()
        item = next(row for row in page["items"] if row["node_id"] == "a1")
        self.assertEqual(item["display_text"], literal)
        self.assertNotIn("different raw fallback", item["display_text"])
        hit = client.get("/api/search/messages", params={"q": "literal-round7-canonical", "path": "all"}).json()
        self.assertEqual([row["node_id"] for row in hit["items"]], ["a1"])
        self.assertEqual(
            client.get("/api/search/messages", params={"q": "different raw fallback", "path": "all"}).json()["items"],
            [],
        )
        exported = client.get(
            "/api/by-id/export",
            params={"conversation_id": "web-1", "format": "txt", "path": "all", "include_internal": "true"},
        ).text
        self.assertIn(literal, exported)
        self.assertNotIn("different raw fallback", exported)

        create_web_indexes(db)
        indexed_hit = client.get(
            "/api/search/messages", params={"q": "literal-round7-canonical", "path": "all"}
        ).json()
        self.assertEqual([row["node_id"] for row in indexed_hit["items"]], ["a1"])
        self.assertEqual(
            client.get("/api/search/messages", params={"q": "different raw fallback", "path": "all"}).json()["items"],
            [],
        )

    def test_round7_raw_size_units_cover_ascii_cjk_emoji_and_nul(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        raw = json.dumps({"text": "A中文🙂\\u0000"}, ensure_ascii=False)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET raw_message_json=? "
            "WHERE conversation_id='web-1' AND node_id='a1'",
            (raw,),
        )
        conn.commit()
        conn.close()

        complete = client.get(
            "/api/by-id/raw",
            params={"conversation_id": "web-1", "node_id": "a1", "max_chars": 200000},
        ).json()
        self.assertEqual(complete["raw_size"], len(raw.encode("utf-8")))
        self.assertEqual(complete["raw_size_bytes"], len(raw.encode("utf-8")))
        self.assertEqual(complete["raw_size_chars"], len(raw))
        self.assertEqual(complete["raw_size_unit"], "bytes")
        self.assertTrue(complete["raw_size_chars_exact"])
        self.assertTrue(complete["raw_size_bytes_exact"])

        truncated = client.get(
            "/api/by-id/raw",
            params={"conversation_id": "web-1", "node_id": "a1", "max_chars": 3},
        ).json()
        self.assertTrue(truncated["truncated"])
        self.assertEqual(truncated["raw_size_bytes"], len(raw.encode("utf-8")))
        self.assertIsNone(truncated["raw_size_chars"])
        self.assertFalse(truncated["raw_size_chars_exact"])

    def test_round7_message_search_response_is_bounded_and_partial_is_explicit(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        text = "round7-needle " + "z" * (2 * 1024 * 1024)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_text=? WHERE conversation_id='web-1' AND node_id='a1'",
            (text,),
        )
        conn.commit()
        conn.close()
        response = client.get("/api/search/messages", params={"q": "round7-needle"})
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 40_000)
        body = response.json()
        self.assertTrue(body["total_exact"])
        self.assertFalse(body["diagnostics"]["partial_due_to_oversized_input"])
        self.assertLessEqual(len(body["items"][0]["display_text"]), 8192)

    def test_round8_real_web_index_callback_signature_is_shared(self):
        import inspect
        from chatgpt_export_archiver.web_db import create_web_indexes

        signature = inspect.signature(create_web_indexes)
        self.assertIn("cancel_check", signature.parameters)
        self.assertEqual(signature.parameters["cancel_check"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertNotIn("cancel_callback", signature.parameters)

    def test_round8_late_ascii_match_is_exact_past_old_candidate_boundary(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_text=? WHERE conversation_id='web-1' AND node_id='a1'",
            ("x" * 210_000 + " round8-late-needle",),
        )
        conn.commit()
        conn.close()
        response = client.get("/api/search/messages", params={"q": "round8-late-needle"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertTrue(body["total_exact"])
        self.assertEqual(body["diagnostics"]["completion_state"], "complete")
        self.assertEqual(body["diagnostics"]["oversized_candidates_seen"], 1)
        hit = body["items"][0]
        self.assertEqual(hit["match_char_offset"], 210_001)
        self.assertIsInstance(hit["display_anchor_cursor"], str)
        anchored = client.get(
            "/api/by-id/display",
            params={
                "conversation_id": hit["conversation_id"],
                "node_id": hit["node_id"],
                "offset": hit["match_char_offset"],
                "limit": 1024,
                "cursor": hit["display_anchor_cursor"],
            },
        )
        self.assertEqual(anchored.status_code, 200, anchored.text)
        self.assertEqual(anchored.json()["offset"], 210_001)
        self.assertTrue(anchored.json()["display_text"].startswith("round8-late-needle"))

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_text=content_text || ' changed' "
            "WHERE conversation_id='web-1' AND node_id='a1'"
        )
        conn.commit()
        conn.close()
        stale = client.get(
            "/api/by-id/display",
            params={
                "conversation_id": hit["conversation_id"],
                "node_id": hit["node_id"],
                "offset": hit["match_char_offset"],
                "limit": 1024,
                "cursor": hit["display_anchor_cursor"],
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"], "display_cursor_stale")

    def test_round10_selected_long_hit_reuses_verifier_and_anchor_seeks_directly(self):
        from chatgpt_export_archiver.search import (
            get_message_display_chunk,
            parse_query,
            search_messages,
        )

        class CountingBlob:
            def __init__(self, inner, counters, key):
                self.inner = inner
                self.counters = counters
                self.key = key

            def __len__(self):
                return len(self.inner)

            def read(self, length=-1):
                value = self.inner.read(length)
                self.counters["bytes"] += len(value)
                return value

            def seek(self, offset, origin=0):
                self.counters["seeks"].append((self.key, offset, origin))
                return self.inner.seek(offset, origin)

            def close(self):
                return self.inner.close()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()
                return False

        counters = {"opens": {}, "bytes": 0, "seeks": []}

        class CountingConnection(sqlite3.Connection):
            def blobopen(self, table, column, rowid, *, readonly=False, name="main"):
                key = (int(rowid), str(column))
                counters["opens"][key] = counters["opens"].get(key, 0) + 1
                inner = super().blobopen(
                    table, column, rowid, readonly=readonly, name=name
                )
                return CountingBlob(inner, counters, key)

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        needle = "round10-direct-anchor"
        writer = sqlite3.connect(db)
        writer.execute(
            "UPDATE conversation_nodes SET content_text=?, raw_message_json=NULL "
            "WHERE conversation_id='web-1' AND node_id='a1'",
            ("x" * (2 * 1024 * 1024) + needle,),
        )
        writer.commit()
        rowid = int(writer.execute(
            "SELECT rowid FROM conversation_nodes "
            "WHERE conversation_id='web-1' AND node_id='a1'"
        ).fetchone()[0])
        writer.close()

        conn = sqlite3.connect(
            f"file:{quote(str(db))}?mode=ro",
            uri=True,
            factory=CountingConnection,
        )
        conn.row_factory = sqlite3.Row
        page = search_messages(
            conn,
            parse_query(needle, path_default="all"),
            limit=10,
            count_total=True,
        )
        hit = next(item for item in page["items"] if item["node_id"] == "a1")
        self.assertEqual(counters["opens"].get((rowid, "content_text")), 1)
        self.assertEqual(
            page["diagnostics"]["blob_reads"],
            page["diagnostics"]["resolver_calls"],
        )
        target_bytes = 2 * 1024 * 1024 + len(needle)
        self.assertGreaterEqual(page["diagnostics"]["candidate_blob_bytes"], target_bytes)
        self.assertLess(page["diagnostics"]["candidate_blob_bytes"], target_bytes + 10_000)

        before_anchor_bytes = counters["bytes"]
        chunk = get_message_display_chunk(
            conn,
            "web-1",
            "a1",
            offset=hit["match_char_offset"],
            limit=1024,
            cursor=hit["display_anchor_cursor"],
        )
        self.assertIsNotNone(chunk)
        self.assertTrue(chunk["display_text"].startswith(needle))
        self.assertLess(counters["bytes"] - before_anchor_bytes, 8_000)
        self.assertTrue(any(
            key == (rowid, "content_text") and offset == hit["match_char_offset"]
            for key, offset, _origin in counters["seeks"]
        ))
        conn.close()

    def test_round10_search_hit_reports_actual_source_span(self):
        from chatgpt_export_archiver import search as search_module

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_text=? "
            "WHERE conversation_id='web-1' AND node_id='a1'",
            ("longneedle and a",),
        )
        conn.commit()
        response = client.get(
            "/api/search/messages",
            params={"q": "a longneedle", "path": "all"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        hit = next(
            item for item in response.json()["items"] if item["node_id"] == "a1"
        )
        self.assertEqual(hit["match_char_offset"], 0)
        self.assertEqual(hit["match_length"], len("longneedle"))
        self.assertEqual(hit["matched_term"], "longneedle")

        conn.execute(
            "UPDATE conversation_nodes SET content_text='ﬁ' "
            "WHERE conversation_id='web-1' AND node_id='a1'"
        )
        conn.commit()
        conn.close()
        response = client.get(
            "/api/search/messages",
            params={"q": "fi", "path": "all"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        hit = next(
            item for item in response.json()["items"] if item["node_id"] == "a1"
        )
        self.assertEqual(hit["match_char_offset"], 0)
        self.assertEqual(hit["match_length"], 1)
        self.assertEqual(hit["matched_term"], "fi")
        long_non_ascii = ("界" * 300_000) + " ﬁ"
        with mock.patch.object(
            search_module,
            "_normalized_span_units",
            side_effect=AssertionError(
                "long source-span lookup must not allocate one tuple per character"
            ),
        ):
            span = search_module._first_source_match_span(
                long_non_ascii,
                [("fi", "contains")],
            )
        self.assertEqual(span, (300_001, 300_002, "fi"))

    def test_round10_message_search_continuation_is_query_and_generation_bound(self):
        import chatgpt_export_archiver.search as search_module

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        params = {"q": "assistant", "path": "all", "limit": 100}
        complete = client.get("/api/search/messages", params=params)
        self.assertEqual(complete.status_code, 200, complete.text)
        expected = {
            (item["conversation_id"], item["node_id"])
            for item in complete.json()["items"]
        }

        seen: set[tuple[str, str]] = set()
        continuation = None
        with mock.patch.object(search_module, "SEARCH_CANDIDATE_LIMIT", 2):
            for _page in range(20):
                request_params = dict(params)
                if continuation is not None:
                    request_params["continuation"] = continuation
                response = client.get("/api/search/messages", params=request_params)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                seen.update(
                    (item["conversation_id"], item["node_id"])
                    for item in payload["items"]
                )
                diagnostics = payload["diagnostics"]
                self.assertLessEqual(diagnostics["candidate_count"], 3)
                self.assertGreaterEqual(diagnostics["resolver_calls"], 0)
                self.assertGreaterEqual(diagnostics["sqlite_vm_steps"], 0)
                if diagnostics["completion_state"] == "complete":
                    self.assertFalse(diagnostics["continuation_available"])
                    self.assertIsNone(diagnostics["continuation_token"])
                    break
                self.assertTrue(diagnostics["partial"])
                self.assertFalse(payload["total_exact"])
                self.assertTrue(diagnostics["continuation_available"])
                continuation = diagnostics["continuation_token"]
            else:
                self.fail("message-search continuation did not finish")
        self.assertEqual(seen, expected)

        # Fast message pages must not consume an unreturned probe row when
        # their signed candidate cursor advances.  Every confirmed hit is
        # returned exactly once and the terminal segment proves the total.
        fast_seen: set[tuple[str, str]] = set()
        fast_continuation = None
        for _page in range(len(expected) + 5):
            request_params = {
                **params,
                "limit": 1,
                "count_total": "false",
            }
            if fast_continuation is not None:
                request_params["continuation"] = fast_continuation
            response = client.get("/api/search/messages", params=request_params)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            fast_seen.update(
                (item["conversation_id"], item["node_id"])
                for item in payload["items"]
            )
            if payload["diagnostics"]["completion_state"] == "complete":
                self.assertTrue(payload["total_exact"])
                break
            fast_continuation = payload["diagnostics"]["continuation_token"]
            self.assertIsInstance(fast_continuation, str)
        else:
            self.fail("fast message-search continuation did not finish")
        self.assertEqual(fast_seen, expected)

        with mock.patch.object(search_module, "SEARCH_CANDIDATE_LIMIT", 1):
            first = client.get("/api/search/messages", params=params).json()
            stale_token = first["diagnostics"]["continuation_token"]
            self.assertIsInstance(stale_token, str)
            changed_query = client.get(
                "/api/search/messages",
                params={**params, "q": "visible", "continuation": stale_token},
            )
            self.assertEqual(changed_query.status_code, 409)
            self.assertEqual(changed_query.json()["detail"], "search_continuation_stale")

            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE conversation_nodes SET content_text=content_text || ' generation-change' "
                "WHERE conversation_id='web-1' AND node_id='a1'"
            )
            conn.commit()
            conn.close()
            stale_generation = client.get(
                "/api/search/messages",
                params={**params, "continuation": stale_token},
            )
            self.assertEqual(stale_generation.status_code, 409)
            self.assertEqual(stale_generation.json()["detail"], "search_continuation_stale")

        invalid = client.get(
            "/api/search/messages", params={**params, "continuation": "not-a-token"}
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["detail"], "invalid_search_continuation")

    def test_round8_exact_search_boundary_matrix_and_raw_only_fallback(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        needle = "round8-boundary-needle"
        conn = sqlite3.connect(db)
        for size in (199_999, 200_000, 200_001, 200_100, 210_000, 799_999, 800_000, 2 * 1024 * 1024):
            with self.subTest(size=size):
                text = "x" * (size - len(needle)) + needle
                conn.execute(
                    "UPDATE conversation_nodes SET content_text=?, raw_message_json=NULL "
                    "WHERE conversation_id='web-1' AND node_id='a1'",
                    (text,),
                )
                conn.commit()
                response = client.get(
                    "/api/search/messages", params={"q": needle, "path": "all"}
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["total"], 1)
                self.assertTrue(payload["total_exact"])
                self.assertEqual(payload["items"][0]["match_char_offset"], size - len(needle))
                self.assertEqual(payload["diagnostics"]["completion_state"], "complete")

        raw_text = "r" * 210_000 + " " + needle
        raw_message = json.dumps(
            {"content": {"content_type": "text", "parts": [raw_text]}},
            separators=(",", ":"),
        )
        conn.execute(
            "UPDATE conversation_nodes SET content_text='', raw_message_json=? "
            "WHERE conversation_id='web-1' AND node_id='a1'",
            (raw_message,),
        )
        conn.commit()
        conn.close()
        raw_response = client.get(
            "/api/search/messages", params={"q": needle, "path": "all"}
        )
        self.assertEqual(raw_response.status_code, 200)
        raw_payload = raw_response.json()
        self.assertEqual(raw_payload["total"], 1)
        self.assertEqual(raw_payload["items"][0]["match_char_offset"], 210_001)

    def test_round10_exact_verify_limit_preserves_confirmed_results_and_opt_in_completes(self):
        import chatgpt_export_archiver.search as search_module

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        needle = "round8-opt-in-needle"
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_text=? "
            "WHERE conversation_id='web-1' AND node_id='a1'",
            ("x" * 210_000 + needle,),
        )
        conn.commit()
        conn.close()
        with (
            mock.patch.object(search_module, "SEARCH_EXACT_VERIFY_CHARS", 200_000),
            mock.patch.object(search_module, "SEARCH_EXACT_VERIFY_MAX_OPT_IN_CHARS", 1_000_000),
            mock.patch.dict(os.environ, {search_module.SEARCH_EXACT_VERIFY_ENV: ""}, clear=False),
        ):
            limited = client.get(
                "/api/search/messages", params={"q": needle, "path": "all"}
            )
            self.assertEqual(limited.status_code, 200)
            limited_payload = limited.json()
            self.assertEqual(limited_payload["total"], 0)
            self.assertFalse(limited_payload["total_exact"])
            self.assertEqual(limited_payload["diagnostics"]["completion_state"], "partial")
            self.assertEqual(limited_payload["diagnostics"]["oversized_candidates_pending"], 1)
            self.assertFalse(limited_payload["diagnostics"]["continuation_available"])
            self.assertIsNone(limited_payload["diagnostics"]["continuation_token"])
            with mock.patch.dict(
                os.environ,
                {search_module.SEARCH_EXACT_VERIFY_ENV: "220000"},
                clear=False,
            ):
                completed = client.get(
                    "/api/search/messages", params={"q": needle, "path": "all"}
                )
                self.assertEqual(completed.status_code, 200)
                self.assertEqual(completed.json()["total"], 1)
                self.assertTrue(completed.json()["total_exact"])

    def test_round8_search_business_budget_returns_non_2xx_before_render(self):
        import chatgpt_export_archiver.search as search_module

        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        with mock.patch.object(search_module, "SEARCH_PAGE_ESTIMATED_BYTES", 256):
            message_response = client.get(
                "/api/search/messages", params={"q": "answer", "path": "all"}
            )
            conversation_response = client.get(
                "/api/conversations", params={"q": "answer", "path": "all"}
            )
        self.assertEqual(message_response.status_code, 413)
        self.assertEqual(
            message_response.json()["detail"],
            "search_response_resource_limit_exceeded",
        )
        self.assertEqual(conversation_response.status_code, 413)
        self.assertEqual(
            conversation_response.json()["detail"],
            "search_response_resource_limit_exceeded",
        )

    def test_round10_exact_search_tracks_utf8_byte_limited_candidate_as_pending(self):
        import chatgpt_export_archiver.search as search_module

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_text=? "
            "WHERE conversation_id='web-1' AND node_id='a1'",
            ("🙂" * 40 + " byte-boundary-needle",),
        )
        conn.commit()
        conn.close()
        with (
            mock.patch.object(search_module, "SEARCH_EXACT_VERIFY_CHARS", 1_000),
            mock.patch.object(search_module, "SEARCH_EXACT_VERIFY_BYTES", 100),
            mock.patch.dict(os.environ, {search_module.SEARCH_EXACT_VERIFY_ENV: ""}, clear=False),
        ):
            response = client.get(
                "/api/search/messages",
                params={"q": "byte-boundary-needle", "path": "all"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["total_exact"])
        self.assertEqual(payload["diagnostics"]["completion_state"], "partial")
        self.assertEqual(payload["diagnostics"]["partial_reason"], "candidate_row_limit")
        self.assertEqual(payload["diagnostics"]["oversized_candidates_pending"], 1)

    def test_round8_conversation_enrichment_marks_every_item_partial_at_global_cap(self):
        import chatgpt_export_archiver.search as search_module

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        for conversation_id in ("web-1", "web-2"):
            conn.executemany(
                "INSERT INTO conversation_nodes("
                "conversation_id, node_id, role, content_type, content_text, is_on_current_path"
                ") VALUES(?, ?, 'user', 'text', 'round8 enrichment token', 1)",
                [(conversation_id, f"enrich-{conversation_id}-{index}") for index in range(4)],
            )
        conn.commit()
        conn.close()
        refresh_test_database_compatibility(db)
        with mock.patch.object(search_module, "SEARCH_ENRICHMENT_MATCH_LIMIT", 2):
            response = client.get(
                "/api/conversations",
                params={"q": "round8 enrichment token", "path": "all", "limit": 10},
            )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual({item["conversation_id"] for item in items}, {"web-1", "web-2"})
        self.assertTrue(all(item["enrichment_partial"] for item in items))

    def test_round8_json_safety_surrogates_and_http_resource_status(self):
        from chatgpt_export_archiver.json_safety import MAX_SANITIZED_OUTPUT_BYTES, sanitize_json_value
        from chatgpt_export_archiver.web_app import FiniteJSONResponse

        safe = sanitize_json_value({"\ud800": "\udc00", "pair": "\ud83d\ude00", "nul": "\x00"})
        encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
        self.assertIn("�", encoded.decode("utf-8"))
        self.assertIn("😀", encoded.decode("utf-8"))
        response = FiniteJSONResponse({"value": "\n" * MAX_SANITIZED_OUTPUT_BYTES})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(json.loads(response.body)["detail"]["code"], "response_resource_limit_exceeded")

    def test_round8_display_cursor_ignores_unrelated_row_but_rejects_target_change(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_text=? WHERE conversation_id='web-1' AND node_id='a1'",
            ("a" * 200_000,),
        )
        conn.commit()
        conn.close()
        first = client.get(
            "/api/by-id/display",
            params={"conversation_id": "web-1", "node_id": "a1", "offset": 0, "limit": 65536},
        )
        self.assertEqual(first.status_code, 200)
        payload = first.json()
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_text='unrelated' WHERE conversation_id='web-2' AND node_id='u2'"
        )
        conn.commit()
        conn.close()
        second = client.get(
            "/api/by-id/display",
            params={
                "conversation_id": "web-1",
                "node_id": "a1",
                "offset": len(payload["display_text"]),
                "limit": 65536,
                "cursor": payload["next_cursor"],
            },
        )
        self.assertEqual(second.status_code, 200)
        conn = sqlite3.connect(db)
        # Same-size mutation outside all bounded first/middle/last samples,
        # deliberately without updating content_hash.  The durable per-row
        # trigger revision must still reject the old cursor.
        changed = list("a" * 200_000)
        changed[5_000] = "b"
        conn.execute(
            "UPDATE conversation_nodes SET content_text=? WHERE conversation_id='web-1' AND node_id='a1'",
            ("".join(changed),),
        )
        conn.commit()
        conn.close()
        stale = client.get(
            "/api/by-id/display",
            params={
                "conversation_id": "web-1",
                "node_id": "a1",
                "offset": len(payload["display_text"]),
                "limit": 65536,
                "cursor": payload["next_cursor"],
            },
        )
        self.assertEqual(stale.status_code, 409)

    def test_round8_display_revision_does_not_hash_unbounded_raw_fallback(self):
        from chatgpt_export_archiver.search import get_message_display_chunk

        td, _client, db = self.make_client()
        self.addCleanup(td.cleanup)
        raw = json.dumps({
            "content": {"content_type": "text", "parts": ["r" * (2 * 1024 * 1024)]},
        })
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE conversation_nodes SET content_type='legacy', "
            "content_text='[non-text content: legacy]', raw_message_json=? "
            "WHERE conversation_id='web-1' AND node_id='a1'",
            (raw,),
        )
        conn.commit()
        conn.close()

        reader = connect_readonly(db)
        raw_bytes_read = 0

        class CountingBlob:
            def __init__(self, blob, column):
                self.blob = blob
                self.column = column

            def __len__(self):
                return len(self.blob)

            def read(self, size=-1):
                nonlocal raw_bytes_read
                value = self.blob.read(size)
                if self.column == "raw_message_json":
                    raw_bytes_read += len(value)
                return value

            def seek(self, offset):
                return self.blob.seek(offset)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.blob.close()

        class CountingConnection:
            def execute(self, *args, **kwargs):
                return reader.execute(*args, **kwargs)

            def blobopen(self, table, column, rowid, *, readonly=False):
                return CountingBlob(
                    reader.blobopen(table, column, rowid, readonly=readonly),
                    column,
                )

        try:
            payload = get_message_display_chunk(
                CountingConnection(), "web-1", "a1", offset=0, limit=65_536
            )
        finally:
            reader.close()
        self.assertEqual(payload["source"], "canonical_placeholder")
        self.assertTrue(payload["resolver_input_truncated"])
        self.assertLess(raw_bytes_read, len(raw.encode("utf-8")))
        self.assertLessEqual(raw_bytes_read, 2 * 800_004)

    def test_round10_raw_only_exact_search_over_one_mib_isolated_as_pending(self):
        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        needle = "round9-raw-limit-needle"
        raw_message = json.dumps({
            "content": {
                "content_type": "text",
                "parts": ["r" * (1024 * 1024) + needle],
            },
        }, separators=(",", ":"))
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE conversation_nodes SET content_type='legacy', content_text='', "
                "raw_message_json=? WHERE conversation_id='web-1' AND node_id='a1'",
                (raw_message,),
            )
            conn.commit()
        finally:
            conn.close()
        response = client.get(
            "/api/search/messages", params={"q": needle, "path": "all"}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["total_exact"])
        self.assertEqual(payload["diagnostics"]["completion_state"], "partial")
        self.assertEqual(payload["diagnostics"]["partial_reason"], "raw_fallback_limit")
        self.assertEqual(payload["diagnostics"]["oversized_candidates_pending"], 1)
        self.assertTrue(payload["diagnostics"]["continuation_available"])

        continued = client.get(
            "/api/search/messages",
            params={
                "q": needle,
                "path": "all",
                "continuation": payload["diagnostics"]["continuation_token"],
            },
        )
        self.assertEqual(continued.status_code, 200, continued.text)
        continued_payload = continued.json()
        self.assertEqual(
            [(item["conversation_id"], item["node_id"]) for item in continued_payload["items"]],
            [("web-1", "a1")],
        )
        self.assertTrue(continued_payload["total_exact"])
        self.assertEqual(continued_payload["diagnostics"]["completion_state"], "complete")
        self.assertEqual(continued_payload["diagnostics"]["oversized_candidates_pending"], 0)
        self.assertFalse(continued_payload["diagnostics"]["continuation_available"])

    def test_round10_unrelated_five_mib_raw_continuation_keeps_confirmed_hit(self):
        from chatgpt_export_archiver.db import init_db, migrate_database

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "raw-continuation.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.executemany(
                "INSERT INTO conversations(conversation_id,title,current_node,aggregate_hash) "
                "VALUES (?, 'Synthetic', 'n', ?)",
                (("confirmed", "h1"), ("raw", "h2")),
            )
            conn.execute(
                "INSERT INTO conversation_nodes("
                "conversation_id,node_id,content_type,content_text,content_hash,is_on_current_path"
                ") VALUES ('confirmed','n','text','tiered needle','h1',1)"
            )
            raw = json.dumps(
                {"content": {"content_type": "text", "parts": ["x" * (5 * 1024 * 1024)]}},
                separators=(",", ":"),
            )
            conn.execute(
                "INSERT INTO conversation_nodes("
                "conversation_id,node_id,content_type,content_text,raw_message_json,content_hash,is_on_current_path"
                ") VALUES ('raw','n','legacy','[non-text content: legacy]',?,'h2',1)",
                (raw,),
            )
            conn.commit()
            migrate_database(conn, refresh_compatibility=True)
            conn.close()
            client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
            self.addCleanup(client.close)

            seen: set[tuple[str, str]] = set()
            continuation = None
            tiers: list[int] = []
            for _page in range(4):
                params = {"q": "tiered needle", "path": "all", "count_total": "true"}
                if continuation is not None:
                    params["continuation"] = continuation
                response = client.get("/api/search/messages", params=params)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                seen.update(
                    (item["conversation_id"], item["node_id"])
                    for item in payload["items"]
                )
                tiers.append(payload["diagnostics"]["raw_fallback_tier"])
                continuation = payload["diagnostics"]["continuation_token"]
                if payload["diagnostics"]["completion_state"] == "complete":
                    self.assertTrue(payload["total_exact"])
                    break
                self.assertFalse(payload["total_exact"])
                self.assertIsInstance(continuation, str)
            else:
                self.fail("raw-only verification continuation did not finish")
            self.assertEqual(seen, {("confirmed", "n")})
            self.assertEqual(tiers, [0, 1, 2])

    def test_round10_long_placeholder_uses_raw_recall_in_current_index_format(self):
        from chatgpt_export_archiver.web_db import WEB_INDEX_FORMAT_VERSION, create_web_indexes

        td, client, db = self.make_client()
        self.addCleanup(td.cleanup)
        needle = "round9-placeholder-recall"
        placeholder = "[non-text content:" + ("synthetic-kind-" * 40) + "]"
        self.assertGreater(len(placeholder), 256)
        raw_message = json.dumps({
            "content": {"content_type": "text", "parts": [needle]},
        }, separators=(",", ":"))
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE conversation_nodes SET content_type='legacy', content_text=?, "
                "raw_message_json=? WHERE conversation_id='web-1' AND node_id='a1'",
                (placeholder, raw_message),
            )
            conn.commit()
        finally:
            conn.close()
        result = create_web_indexes(db)
        self.assertEqual(WEB_INDEX_FORMAT_VERSION, "6")
        self.assertGreater(result["peak_batch_derived_bytes"], 0)
        response = client.get(
            "/api/search/messages", params={"q": needle, "path": "all"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)

    def test_round10_conversation_search_continuation_reuses_verified_segments(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.db import init_db, migrate_database
        from chatgpt_export_archiver.web_db import connect_readonly

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "conversation-continuation.db"
            writer = sqlite3.connect(db)
            writer.row_factory = sqlite3.Row
            init_db(writer)
            for index in range(5):
                conversation_id = f"segment-{index}"
                node_id = f"node-{index}-0"
                writer.execute(
                    "INSERT INTO conversations(conversation_id,title,current_node,aggregate_hash) "
                    "VALUES (?, 'Synthetic', ?, ?)",
                    (conversation_id, node_id, f"hash-{index}"),
                )
                writer.execute(
                    "INSERT INTO conversation_nodes("
                    "conversation_id,node_id,content_type,content_text,content_hash,is_on_current_path"
                    ") VALUES (?, ?, 'text', 'segmented exact needle', ?, 1)",
                    (conversation_id, node_id, f"node-hash-{index}"),
                )
                if index == 0:
                    for extra in range(1, 4):
                        writer.execute(
                            "INSERT INTO conversation_nodes("
                            "conversation_id,node_id,content_type,content_text,"
                            "content_hash,is_on_current_path"
                            ") VALUES (?, ?, 'text', 'segmented exact needle', ?, 1)",
                            (
                                conversation_id,
                                f"node-{index}-{extra}",
                                f"node-hash-{index}-{extra}",
                            ),
                        )
            writer.commit()
            migrate_database(writer, refresh_compatibility=True)
            writer.close()

            reader = connect_readonly(db)
            parsed = search_module.parse_query(
                "segmented exact needle", path_default="all", scope="message"
            )
            continuation = None
            seen: set[str] = set()
            totals: list[int] = []
            with mock.patch.object(search_module, "SEARCH_CANDIDATE_LIMIT", 2):
                for _page in range(4):
                    payload = search_module.search_conversations(
                        reader,
                        parsed,
                        limit=10,
                        offset=0,
                        selected_id="segment-0",
                        continuation=continuation,
                    )
                    self.assertIsNone(payload.get("selected_in_results"))
                    seen.update(item["conversation_id"] for item in payload["items"])
                    totals.append(payload["total"])
                    continuation = payload["diagnostics"]["continuation_token"]
                self.assertIsNone(continuation)
            reader.close()
            self.assertEqual(seen, {f"segment-{index}" for index in range(5)})
            self.assertEqual(totals, [1, 1, 3, 5])

    def test_round10_conversation_enrichment_is_fair_with_one_hundred_thousand_hits(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.db import (
            begin_bulk_generation_aggregation,
            finish_bulk_generation_aggregation,
            init_db,
        )

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            init_db(conn)
            conn.execute("BEGIN IMMEDIATE")
            begin_bulk_generation_aggregation(conn)
            for conversation_id in ("fair-big", "fair-small-a", "fair-small-b"):
                conn.execute(
                    "INSERT INTO conversations("
                    "conversation_id,title,current_node,aggregate_hash"
                    ") VALUES (?, 'Synthetic', 'n000001', ?)",
                    (conversation_id, f"hash-{conversation_id}"),
                )
            conn.execute(
                """
                WITH RECURSIVE sequence(value) AS (
                    VALUES (1)
                    UNION ALL
                    SELECT value + 1 FROM sequence WHERE value < 100000
                )
                INSERT INTO conversation_nodes(
                    conversation_id,node_id,role,content_type,content_text,
                    content_hash,is_on_current_path
                )
                SELECT 'fair-big', printf('n%06d', value), 'assistant', 'text',
                       'fair exact needle', printf('hash-%06d', value), 1
                FROM sequence
                """
            )
            for conversation_id in ("fair-small-a", "fair-small-b"):
                conn.execute(
                    "INSERT INTO conversation_nodes("
                    "conversation_id,node_id,role,content_type,content_text,"
                    "content_hash,is_on_current_path"
                    ") VALUES (?, 'n000001', 'assistant', 'text',"
                    "'fair exact needle', ?, 1)",
                    (conversation_id, f"node-hash-{conversation_id}"),
                )
            finish_bulk_generation_aggregation(
                conn, ("title", "message", "address", "graph")
            )
            conn.commit()

            search_module._prepare_verified_message_table(conn)
            conn.execute(
                """
                INSERT INTO temp.web_verified_message_results(
                    storage_rowid,resolved_text,bm25_score,match_reason
                )
                SELECT rowid, content_text, NULL, 'substring'
                FROM conversation_nodes
                """
            )
            items = [
                {
                    "conversation_id": "fair-big",
                    "message_match": True,
                    "title_match": False,
                    "hit_count": 100000,
                },
                {
                    "conversation_id": "fair-small-a",
                    "message_match": True,
                    "title_match": False,
                    "hit_count": 1,
                },
                {
                    "conversation_id": "fair-small-b",
                    "message_match": True,
                    "title_match": False,
                    "hit_count": 1,
                },
            ]
            parsed = search_module.parse_query(
                "fair exact needle", path_default="all", scope="message"
            )
            search_module._batch_conversation_enrichment(
                conn, parsed, items, verified_messages=True
            )
            self.assertEqual([len(item["snippets"]) for item in items], [3, 1, 1])
            self.assertTrue(items[0]["enrichment_partial"])
            self.assertFalse(items[1]["enrichment_partial"])
            self.assertFalse(items[2]["enrichment_partial"])
        finally:
            conn.close()

    def test_round10_placeholder_boundaries_share_reader_search_and_index_semantics(self):
        from chatgpt_export_archiver import search as search_module
        from chatgpt_export_archiver.db import init_db, migrate_database
        from chatgpt_export_archiver.web_db import create_web_indexes

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = base / "placeholder-boundaries.db"
            writer = sqlite3.connect(db)
            writer.row_factory = sqlite3.Row
            init_db(writer)
            writer.execute(
                "INSERT INTO conversations(conversation_id,title,current_node,aggregate_hash) "
                "VALUES ('boundaries','Synthetic','n7','hash')"
            )
            lengths = [255, 256, 257, 199_999, 200_000, 200_001, 300_000, 4 * 1024 * 1024]
            content_types = ["text", "tool", "code", None, "multimodal_text", "legacy", "text", "tool"]
            prefix = "[non-text content:"
            for index, (length, content_type) in enumerate(zip(lengths, content_types)):
                placeholder = prefix + ("x" * (length - len(prefix) - 1)) + "]"
                self.assertEqual(len(placeholder), length)
                raw = json.dumps(
                    {
                        "content": {
                            "content_type": "text",
                            "parts": [f"shared boundary recall needle {index}"],
                        }
                    },
                    separators=(",", ":"),
                )
                writer.execute(
                    "INSERT INTO conversation_nodes("
                    "conversation_id,node_id,content_type,content_text,raw_message_json,"
                    "content_hash,is_on_current_path) VALUES ('boundaries',?,?,?,?,?,1)",
                    (f"n{index}", content_type, placeholder, raw, f"h{index}"),
                )
            writer.commit()
            migrate_database(writer, refresh_compatibility=True)
            writer.close()

            client = TestClient(create_app(db, static_dir=self.make_build_dir(base)))
            self.addCleanup(client.close)
            params = {"q": "shared boundary recall needle", "path": "all", "scope": "message"}
            with mock.patch.object(
                search_module,
                "_stream_selected_search_hit",
                side_effect=AssertionError("placeholder selected hit must reuse verified raw artifact"),
            ):
                before = client.get("/api/search/messages", params=params)
            self.assertEqual(before.status_code, 200, before.text)
            self.assertEqual(before.json()["total"], len(lengths))
            reader = client.get(
                "/api/conversations/boundaries/messages",
                params={"path": "all", "include_internal": "true", "limit": 20},
            )
            self.assertEqual(reader.status_code, 200, reader.text)
            self.assertEqual(
                {item["display_text"] for item in reader.json()["items"]},
                {f"shared boundary recall needle {index}" for index in range(len(lengths))},
            )
            self.assertFalse(
                any(
                    item["display_text_resolver_input_truncated"]
                    for item in reader.json()["items"]
                )
            )
            display = client.get(
                "/api/by-id/display",
                params={
                    "conversation_id": "boundaries",
                    "node_id": "n7",
                    "offset": 0,
                    "limit": 1024,
                },
            )
            self.assertEqual(display.status_code, 200, display.text)
            self.assertEqual(display.json()["display_text"], "shared boundary recall needle 7")
            self.assertEqual(display.json()["source"], "raw_fallback")

            result = create_web_indexes(db)
            self.assertEqual(result["indexed_messages"], len(lengths) - 1)
            self.assertEqual(result["oversized_messages"], 1)
            with mock.patch.object(
                search_module,
                "_stream_selected_search_hit",
                side_effect=AssertionError("indexed placeholder selected hit must reuse verified raw artifact"),
            ):
                after = client.get("/api/search/messages", params=params)
            self.assertEqual(after.status_code, 200, after.text)
            self.assertEqual(after.json()["total"], len(lengths))


if __name__ == "__main__":
    unittest.main()
