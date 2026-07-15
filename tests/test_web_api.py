from __future__ import annotations

import importlib
import asyncio
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import quote

try:
    from fastapi.testclient import TestClient
    from chatgpt_export_archiver.web_app import create_app
except ImportError:  # pragma: no cover
    TestClient = None
    create_app = None

from chatgpt_export_archiver.cli import main
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
        self.assertEqual(health["database"]["name"], "database")
        self.assertNotIn(db.name, json.dumps(health))
        self.assertEqual(client.get("/api/stats").json()["conversations"], 3)
        page = client.get("/api/conversations?limit=1").json()
        self.assertEqual(page["limit"], 1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 1)

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

    def test_messages_include_render_text_and_bounded_raw_preview(self):
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
        self.assertNotIn("private_note", payload)

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
        self.assertTrue(any("substr(n.raw_message_json,1,200001)" in sql for sql in normalized_sql))
        self.assertFalse(any("selectn.raw_message_json" in sql for sql in normalized_sql))

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
        for rejected_needle in ("invalid raw sentinel", "huge raw sentinel", "synthetic-asset"):
            self.assertEqual(
                client.get(f"/api/search/messages?q={quote(rejected_needle)}&path=all").json()["items"],
                [],
            )
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
        td, client, _db = self.make_client()
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
            second = search_messages(conn, parsed, limit=1, offset=1, order="display", count_total=False)
            self.assertEqual(len(first["items"]), 1)
            self.assertTrue(first["has_more"])
            self.assertEqual(first["next_offset"], 1)
            self.assertNotEqual(first["items"][0]["node_id"], second["items"][0]["node_id"])
            self.assertLessEqual(first["total"], 2)
        finally:
            conn.close()

    def test_message_search_api_count_total_false_uses_fast_page_probe(self):
        from chatgpt_export_archiver import search as search_module

        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        original = search_module._message_search_page_rows
        calls: list[bool] = []

        def wrapped(conn, parsed, conversation_id, limit, offset, order, *, use_trigram=True, count_total=True):
            calls.append(count_total)
            return original(conn, parsed, conversation_id, limit, offset, order, use_trigram=use_trigram, count_total=count_total)

        with mock.patch.object(search_module, "_message_search_page_rows", side_effect=wrapped):
            payload = client.get("/api/search/messages?q=python&limit=1&count_total=false").json()
        self.assertIn(False, calls)
        self.assertEqual(len(payload["items"]), 1)
        self.assertTrue(payload["has_more"])
        self.assertLessEqual(payload["total"], 2)

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
        too_long_id = "x" * 513
        self.assertEqual(client.get(f"/api/conversations?after={too_long_date}").status_code, 422)
        self.assertEqual(client.get(f"/api/conversations?selected_id={too_long_id}").status_code, 422)
        self.assertEqual(client.get(f"/api/search?selected_id={too_long_id}").status_code, 422)
        self.assertEqual(client.get(f"/api/search/messages?conversation_id={too_long_id}").status_code, 422)
        self.assertEqual(client.get(f"/api/conversations/web-1/messages?around_node_id={too_long_id}").status_code, 422)
        self.assertEqual(client.get(f"/api/conversations/{too_long_id}").status_code, 422)

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
        self.assertIn("const typing", html)
        self.assertIn("&quot;", html)
        self.assertIn("&#39;", html)
        self.assertIn("&#96;", html)

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
            node_selects = [stmt for stmt in statements if "FROM conversation_nodes" in stmt and "raw_message_json" in stmt]
            self.assertTrue(any("LIMIT 5 OFFSET 10" in stmt for stmt in node_selects))
            self.assertFalse(any("raw_message_json" in stmt and "LIMIT" not in stmt and "COUNT(" not in stmt for stmt in node_selects))
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
            self.assertTrue(job.canonical_commit_succeeded)

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
        self.assertIn("include_internal", json.dumps(schema))
        self.assertIn("hidden_counts", json.dumps(schema))
        self.assertIn("match_mode", json.dumps(schema))
        self.assertIn("selected_in_results", json.dumps(schema))
        self.assertIn("selected_item", json.dumps(schema))
        self.assertIn("count_total", json.dumps(schema))
        self.assertIn("technical_hidden_count", json.dumps(schema))
        self.assertEqual(schema["messages"]["limits"]["around_node_id"], 512)
        self.assertEqual(schema["conversations"]["limits"]["selected_id"], 512)
        self.assertIn("visible-only reader pagination collection", schema["messages"]["around_node_id"]["description"])
        self.assertEqual(
            schema["messages"]["around_node_id"]["response"],
            ["around_target_found", "around_target_in_effective_collection", "around_target_in_requested_collection", "around_target_visible", "around_target_applied"],
        )
        self.assertIn("effective all collection", schema["messages"]["around_node_id"]["description"])

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
            {"items", "total", "limit", "offset", "has_more", "next_offset"},
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
        for code in (
            "database_not_ready", "conversation_not_found", "message_not_found",
            "invalid_query", "invalid_sort", "invalid_message_order",
            "import_transaction_failed", "verify_failed", "stats_failed", "web_index_failed",
        ):
            self.assertIn(code, schema["stable_error_codes"])

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
            ("top-level", {"conversations.json": {"not": "a list"}}, "conversation_json_top_level_not_list", "top_level_contract", "top_level_contract_failed"),
            (
                "mixed-shards",
                {"conversations-000.json": [conv("good-before-bad", "Good", {"root": root([])}, "root", 1)], "conversations-001.json": b"{"},
                "invalid_conversation_json",
                "json_decode",
                "json_decode_failed",
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
            return real_ensure(connection, ids)

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
        self.assertFalse(empty["total_exact"])
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
                normalized = None if ids is None else tuple(sorted(str(value) for value in ids))
                scopes.append(normalized)
                return real_ensure(connection, ids)

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

if __name__ == "__main__":
    unittest.main()
