from __future__ import annotations

import importlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
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


@unittest.skipIf(TestClient is None, "fastapi test client is not installed")
class WebApiTests(unittest.TestCase):
    def make_build_dir(self, base: Path) -> Path:
        build = base / "dist"
        build.mkdir()
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
        first_job = self.wait_job(client, response.json()["job_id"])
        self.assertEqual(first_job["status"], "succeeded")
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
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        client = TestClient(create_app(base / "archive.db", static_dir=self.make_build_dir(base)))
        with mock.patch("chatgpt_export_archiver.web_api.MAX_UPLOAD_BYTES", 1):
            response = client.post("/api/import/upload", files={"file": ("synthetic.zip", b"1234", "application/zip")})
        self.assertEqual(response.status_code, 413)
        self.assertIn("upload_too_large", response.text)

    def test_max_upload_bytes_env_parsing_is_safe(self):
        from chatgpt_export_archiver import web_api

        default = web_api.DEFAULT_MAX_UPLOAD_BYTES
        env_name = web_api.MAX_UPLOAD_ENV
        self.assertEqual(web_api._get_max_upload_bytes({}), default)
        self.assertEqual(web_api._get_max_upload_bytes({env_name: "12345"}), 12345)
        for value, error_type in (("not-a-number", "invalid_integer"), ("   ", "invalid_integer"), ("0", "non_positive"), ("-1", "non_positive")):
            with self.assertLogs("chatgpt_export_archiver.web_api", level="WARNING") as logs:
                self.assertEqual(web_api._get_max_upload_bytes({env_name: value}), default)
            payload = "\n".join(logs.output)
            self.assertIn(env_name, payload)
            self.assertIn(error_type, payload)
            if value.strip():
                self.assertNotIn(value, payload)

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
        all_nodes = client.get("/api/conversations/web-1/messages?path=all").json()
        self.assertLess(current["total"], all_nodes["total"])
        keys = json.dumps(current)
        self.assertNotIn("raw_message_json", keys)
        self.assertNotIn("private_note", keys)

    def test_messages_include_render_text_and_bounded_raw_preview(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        page = client.get("/api/conversations/web-3/messages?path=current").json()
        by_id = {item["node_id"]: item for item in page["items"]}
        self.assertIn("system readable fallback", by_id["sys"]["display_text"])
        self.assertTrue(by_id["sys"]["is_internal"])
        self.assertTrue(by_id["sys"]["has_raw"])
        self.assertIn("profile text", by_id["ctx"]["display_text"])
        payload = json.dumps(page)
        self.assertIn("raw_preview", payload)
        self.assertNotIn("raw_message_json", payload)
        self.assertNotIn("private_note", payload)

    def test_full_raw_endpoint_is_explicit(self):
        td, client, _db = self.make_client()
        self.addCleanup(td.cleanup)
        page = client.get("/api/conversations/web-3/messages?path=current").json()
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
        normalized_messages = client.get("/api/conversations/web-1/messages?q=%EF%BD%87%EF%BD%90%EF%BD%94%EF%BC%8D%EF%BC%95%EF%BC%8E%EF%BC%95&path=current").json()
        by_id = {item["node_id"]: item for item in normalized_messages["items"]}
        self.assertTrue(by_id["t1"]["highlight_ranges"])
        normalized_hits = client.get("/api/search/messages?q=%EF%BD%87%EF%BD%90%EF%BD%94%EF%BC%8D%EF%BC%95%EF%BC%8E%EF%BC%95&conversation_id=web-1").json()
        self.assertTrue(normalized_hits["items"])
        self.assertIn("gpt-5.5", normalized_hits["items"][0]["snippet"])

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
        self.assertEqual(first["total"], 361)
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
        self.assertIn("Limited fallback UI", html)
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
        finally:
            conn.close()
        self.assertIn("content_text", message_columns)
        self.assertIn("title", title_columns)
        self.assertNotIn("conversation_id", message_columns)
        self.assertNotIn("node_id", message_columns)
        self.assertNotIn("conversation_id", title_columns)

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
            self.assertFalse(any("web_message_trigram" in stmt for stmt in statements))
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
        zh_miss = client.get("/api/conversations?q=%E4%B8%AD%E5%8D%8E%E6%B0%91%E5%9B%BD&limit=50").json()
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
            self.assertFalse(any("raw_message_json" in stmt and "LIMIT" not in stmt for stmt in node_selects))
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
        self.assertEqual(messages["total"], 4)
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

    def test_tool_roles_are_internal(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        z = base / "tool.zip"
        mapping = {
            "root": root(["tool"]),
            "tool": node("tool", "root", "tool/system", "tool output", 1_701_100_001),
        }
        write_zip(z, [conv("tool-role", "Tool Role", mapping, "tool", 1_701_100_000)])
        db = base / "archive.db"
        self.assertEqual(main(["--db", str(db), "import", "--input", str(z), "--no-input-sha256"]), 0)
        client = TestClient(create_app(db))
        page = client.get("/api/conversations/tool-role/messages?path=current").json()
        by_id = {item["node_id"]: item for item in page["items"]}
        self.assertEqual(by_id["tool"]["role"], "tool/system")
        self.assertTrue(by_id["tool"]["is_internal"])

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
        self.assertEqual(job.error, "postcheck_failed")
        self.assertEqual(job.summary["import_run_id"], 1)
        web_index.assert_not_called()

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
        self.assertIn("error_type=RuntimeError", snapshot["error"])
        self.assertNotIn(unsafe_message, payload)
        self.assertNotIn("/private/path", payload)
        self.assertNotIn("private-upload.zip", payload)

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

if __name__ == "__main__":
    unittest.main()
