from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_export_archiver import scanner
from chatgpt_export_archiver.cli import cmd_init, cmd_migrate, run_import_pipeline
from chatgpt_export_archiver.db import (
    DATABASE_SCHEMA_VERSION,
    GENERATION_TRIGGER_DDL,
    connect,
    init_db,
    generation_schema_contract_is_current,
    migrate_database,
)
from chatgpt_export_archiver.identifiers import identifier_text_is_safe
from chatgpt_export_archiver.json_safety import JsonSafetyLimitError
from chatgpt_export_archiver.search import (
    SearchContinuationError,
    parse_query,
    search_messages,
)
from chatgpt_export_archiver import search as search_module
from chatgpt_export_archiver.utils import safe_filename_part
from chatgpt_export_archiver.web_api import _content_disposition
from chatgpt_export_archiver.web_db import (
    WebIndexBuildError,
    _PROCESS_LOCK_SHARDS,
    _database_lock_keys,
    _process_lock_name,
    _process_lock_registry,
    acquire_writer_process_lock,
    create_web_indexes,
)
from chatgpt_export_archiver.web_jobs import ImportJobManager
from tests.test_archiver import conversation, write_zip


def _generations(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(name): int(value)
        for name, value in conn.execute(
            "SELECT name, generation FROM archive_generations ORDER BY name"
        )
    }


class Round12Regressions(unittest.TestCase):
    def test_query_generation_external_matrix_and_rollback(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        conn.execute(
            "INSERT INTO conversations("
            "conversation_id,title,current_node,source_file,create_time,update_time,"
            "aggregate_hash) VALUES ('c','t','n','a.json',1,2,'h')"
        )
        conn.execute(
            "INSERT INTO conversation_nodes("
            "conversation_id,node_id,create_time,update_time,content_type,"
            "content_text,content_hash,is_on_current_path"
            ") VALUES ('c','n',1,2,'text','body','nh',1)"
        )
        conn.commit()
        baseline = _generations(conn)
        self.assertEqual(set(baseline), {"address", "graph", "message", "query", "title"})

        for statement, params in (
            ("UPDATE conversations SET source_file=? WHERE conversation_id='c'", ("b.json",)),
            ("UPDATE conversations SET create_time=? WHERE conversation_id='c'", (3,)),
            ("UPDATE conversations SET update_time=? WHERE conversation_id='c'", (4,)),
            (
                "UPDATE conversation_nodes SET create_time=? "
                "WHERE conversation_id='c' AND node_id='n'",
                (5,),
            ),
            (
                "UPDATE conversation_nodes SET update_time=? "
                "WHERE conversation_id='c' AND node_id='n'",
                (6,),
            ),
        ):
            before = _generations(conn)
            conn.execute(statement, params)
            conn.commit()
            after = _generations(conn)
            self.assertEqual(after["query"], before["query"] + 1)

        before = _generations(conn)
        conn.execute(
            "UPDATE conversations SET source_file=source_file,"
            "create_time=create_time,update_time=update_time "
            "WHERE conversation_id='c'"
        )
        conn.execute(
            "UPDATE conversation_nodes SET create_time=create_time,"
            "update_time=update_time "
            "WHERE conversation_id='c' AND node_id='n'"
        )
        conn.commit()
        self.assertEqual(_generations(conn), before)

        before = _generations(conn)
        conn.execute(
            "UPDATE conversations SET is_archived=1,is_starred=1,"
            "default_model_slug='synthetic',metadata_json='{}' "
            "WHERE conversation_id='c'"
        )
        conn.commit()
        self.assertEqual(_generations(conn), before)

        before = _generations(conn)
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE conversations SET source_file='rolled-back.json' "
            "WHERE conversation_id='c'"
        )
        conn.rollback()
        self.assertEqual(_generations(conn), before)
        conn.close()

    def test_query_trigger_predicate_is_part_of_exact_schema_contract(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        self.assertTrue(generation_schema_contract_is_current(conn))
        conn.execute('DROP TRIGGER "archive_query_generation_node_update"')
        conn.execute(
            "CREATE TRIGGER archive_query_generation_node_update "
            "AFTER UPDATE OF create_time, update_time ON conversation_nodes "
            "WHEN 0 BEGIN "
            "UPDATE archive_generations SET generation=generation+1 "
            "WHERE name='query'; END"
        )
        self.assertFalse(generation_schema_contract_is_current(conn))
        conn.close()

    def test_source_only_change_stales_search_continuation(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "continuation.db"
            conn = connect(database)
            init_db(conn)
            for index in range(4):
                cid = f"c-{index}"
                nid = f"n-{index}"
                conn.execute(
                    "INSERT INTO conversations("
                    "conversation_id,title,current_node,source_file,aggregate_hash"
                    ") VALUES (?, 'synthetic', ?, 'one.json', ?)",
                    (cid, nid, f"h-{index}"),
                )
                conn.execute(
                    "INSERT INTO conversation_nodes("
                    "conversation_id,node_id,content_type,content_text,content_hash,"
                    "is_on_current_path) VALUES (?,?,'text','needle',?,1)",
                    (cid, nid, f"nh-{index}"),
                )
            conn.commit()
            with mock.patch.object(search_module, "SEARCH_CANDIDATE_LIMIT", 1):
                first = search_messages(
                    conn,
                    parse_query("needle", path_default="all"),
                    limit=1,
                    count_total=False,
                )
            token = first["diagnostics"]["continuation_token"]
            self.assertIsInstance(token, str)
            conn.execute(
                "UPDATE conversations SET source_file='two.json' "
                "WHERE conversation_id='c-0'"
            )
            conn.commit()
            with self.assertRaises(SearchContinuationError) as caught:
                search_messages(
                    conn,
                    parse_query("needle", path_default="all"),
                    limit=1,
                    count_total=False,
                    continuation=token,
                )
            self.assertEqual(caught.exception.code, "search_continuation_stale")
            conn.close()

    def test_conversation_and_node_time_only_changes_stale_continuations(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "time-continuation.db"
            conn = connect(database)
            init_db(conn)
            for index in range(4):
                conn.execute(
                    "INSERT INTO conversations("
                    "conversation_id,title,current_node,source_file,create_time,"
                    "update_time,aggregate_hash"
                    ") VALUES (?, 'synthetic', ?, 'one.json', 1, 2, ?)",
                    (f"c-{index}", f"n-{index}", f"h-{index}"),
                )
                conn.execute(
                    "INSERT INTO conversation_nodes("
                    "conversation_id,node_id,create_time,update_time,content_type,"
                    "content_text,content_hash,is_on_current_path"
                    ") VALUES (?,?,1,2,'text','needle',?,1)",
                    (f"c-{index}", f"n-{index}", f"nh-{index}"),
                )
            conn.commit()
            mutations = (
                "UPDATE conversations SET create_time=create_time+1 "
                "WHERE conversation_id='c-0'",
                "UPDATE conversations SET update_time=update_time+1 "
                "WHERE conversation_id='c-0'",
                "UPDATE conversation_nodes SET create_time=create_time+1 "
                "WHERE conversation_id='c-0' AND node_id='n-0'",
                "UPDATE conversation_nodes SET update_time=update_time+1 "
                "WHERE conversation_id='c-0' AND node_id='n-0'",
            )
            for mutation in mutations:
                with self.subTest(mutation=mutation), mock.patch.object(
                    search_module, "SEARCH_CANDIDATE_LIMIT", 1
                ):
                    first = search_messages(
                        conn,
                        parse_query("needle", path_default="all"),
                        limit=1,
                        count_total=False,
                    )
                    token = first["diagnostics"]["continuation_token"]
                    self.assertIsInstance(token, str)
                    conn.execute(mutation)
                    conn.commit()
                    with self.assertRaises(SearchContinuationError) as caught:
                        search_messages(
                            conn,
                            parse_query("needle", path_default="all"),
                            limit=1,
                            count_total=False,
                            continuation=token,
                        )
                    self.assertEqual(
                        caught.exception.code, "search_continuation_stale"
                    )
            conn.close()

    def test_schema_v5_migrates_query_generation_and_current_is_true_noop(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "migration.db"
            conn = connect(database)
            init_db(conn)
            for name in tuple(
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE type='trigger' AND name LIKE 'archive_query_generation_%'"
                )
            ):
                conn.execute(f'DROP TRIGGER "{name}"')
            conn.execute("DELETE FROM archive_generations WHERE name='query'")
            conn.execute("PRAGMA user_version=5")
            conn.commit()
            migrated = migrate_database(conn)
            self.assertTrue(migrated["schema_changed"])
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION)
            self.assertIn("query", _generations(conn))
            before_changes = conn.total_changes
            repeated = migrate_database(conn, refresh_compatibility=True)
            self.assertFalse(repeated["schema_changed"])
            self.assertFalse(repeated["compatibility_refreshed"])
            self.assertFalse(repeated["migration_changed"])
            self.assertEqual(conn.total_changes, before_changes)
            conn.close()

    def test_migration_recounts_under_write_lock_after_outer_preflight_race(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "migration-race.db"
            migrating = connect(database)
            init_db(migrating)
            migrating.execute(
                "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
                "VALUES ('c','synthetic','h')"
            )
            migrating.execute(
                "INSERT INTO conversation_nodes(conversation_id,node_id,content_text) "
                "VALUES ('c','initial','body')"
            )
            migrating.commit()
            for name in GENERATION_TRIGGER_DDL:
                migrating.execute(f'DROP TRIGGER "{name}"')
            migrating.execute('DROP TRIGGER "archive_display_revision_node_insert"')
            migrating.execute('DROP TRIGGER "archive_display_revision_node_update"')
            migrating.execute(
                "ALTER TABLE conversation_nodes DROP COLUMN display_revision"
            )
            migrating.execute("PRAGMA user_version=4")
            migrating.commit()
            progress: list[tuple[str, int, int]] = []
            inserted = False

            def callback(stage: str, value: dict[str, int]) -> None:
                nonlocal inserted
                progress.append((stage, value["processed"], value["total"]))
                if stage != "preflight" or inserted:
                    return
                contender = sqlite3.connect(database)
                try:
                    contender.executemany(
                        "INSERT INTO conversation_nodes("
                        "conversation_id,node_id,content_text"
                        ") VALUES ('c',?,'body')",
                        ((f"late-{index}",) for index in range(100_000)),
                    )
                    contender.commit()
                finally:
                    contender.close()
                inserted = True

            result = migrate_database(migrating, progress_callback=callback)
            self.assertTrue(result["migration_changed"])
            self.assertEqual(
                result["migration_disk_preflight"]["locked_node_count"], 100_001
            )
            locked = [item for item in progress if item[0] == "locked_preflight"]
            backfill = [
                item for item in progress if item[0] == "backfill_display_revisions"
            ]
            self.assertEqual(locked, [("locked_preflight", 0, 100_001)])
            self.assertTrue(backfill)
            self.assertEqual(backfill[-1][1:], (100_001, 100_001))
            self.assertTrue(all(processed <= total for _, processed, total in progress))
            migrating.close()

    def test_all_unsafe_routes_are_origin_guarded(self):
        from fastapi.testclient import TestClient
        from chatgpt_export_archiver.web_app import create_app

        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "origin.db"
            conn = connect(database)
            init_db(conn)
            conn.close()
            app = create_app(database, allow_fallback=True, log_level="none")
            client = TestClient(app, base_url="http://localhost")
            unsafe: list[tuple[str, str]] = []
            routes = []
            for top_level in app.routes:
                original_router = getattr(top_level, "original_router", None)
                if original_router is not None:
                    routes.extend(original_router.routes)
                else:
                    routes.append(top_level)
            for route in routes:
                path = getattr(route, "path", "")
                for method in getattr(route, "methods", set()) or set():
                    if method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
                        unsafe.append((method, path))
            self.assertTrue(unsafe)
            for method, path in unsafe:
                concrete = path.replace("{job_id}", "0" * 32)
                with self.subTest(method=method, path=path):
                    response = client.request(
                        method,
                        concrete,
                        headers={
                            "Origin": "https://evil.example",
                            "Sec-Fetch-Site": "cross-site",
                        },
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertIn(
                        response.json()["code"],
                        {"write_origin_not_allowed", "upload_origin_not_allowed"},
                    )
                    same_origin = client.request(
                        method,
                        concrete,
                        headers={
                            "Origin": "http://localhost",
                            "Sec-Fetch-Site": "same-origin",
                        },
                    )
                    self.assertNotIn(
                        same_origin.json().get("code")
                        if same_origin.headers.get("content-type", "").startswith("application/json")
                        else None,
                        {"write_origin_not_allowed", "upload_origin_not_allowed"},
                    )

    def test_writer_lock_windows_fails_before_registry_and_registry_is_bounded(self):
        database = Path("/synthetic/not-created.db")
        with mock.patch("chatgpt_export_archiver.web_db.os.name", "nt"), mock.patch(
            "chatgpt_export_archiver.web_db._process_lock_registry"
        ) as registry:
            with self.assertRaises(WebIndexBuildError) as caught:
                acquire_writer_process_lock(database)
            self.assertEqual(caught.exception.code, "writer_process_lock_unsupported")
            registry.assert_not_called()

        names = {
            _process_lock_name(("path", f"{index:064x}"))
            for index in range(100_000)
        }
        self.assertLessEqual(len(names), _PROCESS_LOCK_SHARDS)

    def test_two_managers_share_cross_process_upload_admission(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "jobs.db"
            child_code = (
                "import pathlib,sys;"
                "from chatgpt_export_archiver.web_jobs import ImportJobManager;"
                "m=ImportJobManager(pathlib.Path(sys.argv[1]));"
                "assert m.acquire_pending_upload_slot();"
                "print('ready',flush=True);"
                "sys.stdin.readline();"
                "m.release_pending_upload_slot()"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(database)],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                second = ImportJobManager(database)
                self.assertFalse(second.acquire_pending_upload_slot())
            finally:
                if child.stdin is not None:
                    child.stdin.write("\n")
                    child.stdin.flush()
                    child.stdin.close()
                stderr = child.stderr.read() if child.stderr is not None else ""
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()
                child.wait(timeout=10)
                self.assertEqual(child.returncode, 0, stderr)
            self.assertTrue(second.acquire_pending_upload_slot())
            second.release_pending_upload_slot()

    def test_held_writer_lock_blocks_all_writer_entrypoints(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            database = base / "writer.db"
            conn = connect(database)
            init_db(conn)
            conn.close()
            archive = base / "input.zip"
            write_zip(archive, {"conversations.json": [conversation("writer-lock")]})
            held = acquire_writer_process_lock(database)
            try:
                for operation in (
                    lambda: cmd_init(argparse.Namespace(db=str(database))),
                    lambda: cmd_migrate(argparse.Namespace(db=str(database))),
                    lambda: run_import_pipeline(
                        database,
                        str(archive),
                        cwd=base,
                        no_input_sha256=True,
                    ),
                    lambda: create_web_indexes(database),
                ):
                    with self.subTest(operation=operation), self.assertRaises(
                        (WebIndexBuildError, ValueError)
                    ) as caught:
                        operation()
                    self.assertIn(
                        "writer_process_lock_busy",
                        str(caught.exception),
                    )
            finally:
                held.close()

    def test_c1_policy_is_shared_by_ids_filenames_and_headers(self):
        rejected = ["\x00", "\x1f", "\x7f", "\x80", "\x85", "\x9b", "\x9f", "\ud800"]
        accepted = ["\ufdd0", "🙂", "e\u0301", "中文"]
        for value in rejected:
            with self.subTest(value=ascii(value)):
                self.assertFalse(identifier_text_is_safe(value, limit=100))
                filename = safe_filename_part(f"a{value}b")
                self.assertFalse(
                    any(unicodedata.category(char) == "Cc" for char in filename)
                )
                header = _content_disposition(f"a{value}b.txt")
                self.assertFalse(
                    any(unicodedata.category(char) == "Cc" for char in header)
                )
                header.encode("ascii")
        for value in accepted:
            with self.subTest(value=ascii(value)):
                self.assertTrue(identifier_text_is_safe(value, limit=100))

        from fastapi.testclient import TestClient
        from chatgpt_export_archiver.web_app import create_app

        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "c1.db"
            conn = connect(database)
            init_db(conn)
            conn.close()
            client = TestClient(create_app(database, allow_fallback=True))
            response = client.get("/api/conversations", params={"selected_id": "\x7f"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"], "invalid_identifier_token")
            response = client.post(
                "/api/import/jobs/" + ("0" * 32) + "/web-index/cancel",
                headers={"Origin": "http://localhost/\x7f"},
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["code"], "write_origin_not_allowed")
            client.close()

    def test_delete_cannot_be_enabled_by_boolean_monkeypatch(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "input.zip"
            original = b"synthetic"
            path.write_bytes(original)
            source = scanner.InputSource(
                path=path,
                kind="zip",
                size=len(original),
                delete_target=path,
            )
            with mock.patch.object(
                scanner, "delete_input_secure_identity_supported", return_value=True
            ), mock.patch.object(scanner.os, "rename") as rename, mock.patch.object(
                scanner.os, "unlink"
            ) as unlink, self.assertRaises(scanner.DeleteInputRecoveryRequired) as caught:
                scanner.delete_input_if_unchanged(source)
            self.assertEqual(caught.exception.code, "delete_input_secure_identity_unsupported")
            rename.assert_not_called()
            unlink.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    def test_historical_owned_recovery_journal_restores_without_creating_one(self):
        if not scanner._DELETE_DIR_FD_SUPPORTED or os.link not in os.supports_dir_fd:
            with self.assertRaises(scanner.DeleteInputRecoveryRequired) as caught:
                scanner.recover_delete_input(Path("."), "a" * 32)
            self.assertEqual(caught.exception.code, "delete_input_secure_identity_unsupported")
            return
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            token = "a" * 32
            original_name = "historical.zip"
            staged_name = f".chatgpt-archive-delete-{token}"
            staged = base / staged_name
            payload = b"synthetic historical recovery payload"
            staged.write_bytes(payload)
            entry_identity = scanner._entry_identity(staged, follow_symlinks=False)
            target_identity = scanner._entry_identity(staged, follow_symlinks=True)
            parent_identity = scanner._entry_identity(base, follow_symlinks=True)
            record = {
                "format_version": 1,
                "owner_token": token,
                "state": "staged",
                "original_name": original_name,
                "staged_name": staged_name,
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "entry_identity": scanner._delete_identity_record(entry_identity),
                "target_identity": scanner._delete_identity_record(target_identity),
                "parent_identity": scanner._delete_identity_record(parent_identity),
            }
            journal = base / f"{scanner.DELETE_INPUT_RECOVERY_PREFIX}{token}.json"
            journal.write_text(
                json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            self.assertEqual(scanner.recover_delete_input(base, token), "restored")
            self.assertEqual((base / original_name).read_bytes(), payload)
            self.assertFalse(staged.exists())
            self.assertFalse(journal.exists())

    def test_predecode_mapping_array_and_heap_reject_before_decoder(self):
        class RefusingDecoder(json.JSONDecoder):
            def decode(self, *args, **kwargs):
                raise AssertionError("decoder must not run after lexical rejection")

            def raw_decode(self, *args, **kwargs):
                raise json.JSONDecodeError("probe refused", "", 0)

        mapping = "[{" + ",".join(f'"k{i}":0' for i in range(11)) + "}]"
        with mock.patch.object(scanner, "MAX_JSON_MAPPING_ENTRIES", 10), mock.patch.object(
            scanner.json, "JSONDecoder", RefusingDecoder
        ), self.assertRaises(JsonSafetyLimitError) as caught:
            list(scanner._iter_json_array([mapping]))
        self.assertEqual(caught.exception.code, "json_mapping_entry_limit_exceeded")

        array = "[[" + ",".join("0" for _ in range(11)) + "]]"
        with mock.patch.object(scanner, "MAX_JSON_ARRAY_ITEMS", 10), mock.patch.object(
            scanner.json, "JSONDecoder", RefusingDecoder
        ), self.assertRaises(JsonSafetyLimitError) as caught:
            list(scanner._iter_json_array([array]))
        self.assertEqual(caught.exception.code, "json_array_item_limit_exceeded")

        heap = '[{"a":"one","b":"two"}]'
        with mock.patch.object(
            scanner, "MAX_JSON_ESTIMATED_DECODED_HEAP_BYTES", 1
        ), mock.patch.object(
            scanner.json, "JSONDecoder", RefusingDecoder
        ), self.assertRaises(JsonSafetyLimitError) as caught:
            list(scanner._iter_json_array([heap]))
        self.assertEqual(caught.exception.code, "json_estimated_heap_limit_exceeded")

    def test_hybrid_probe_does_not_accept_a_split_numeric_prefix(self):
        self.assertEqual(
            list(scanner._iter_json_array(["[1", "e2,3", "]"])),
            [100.0, 3],
        )
        self.assertEqual(
            list(scanner._iter_json_array(["[-1.", "25,0", "]"])),
            [-1.25, 0],
        )

    def test_hybrid_extended_proof_stops_at_large_shallow_element_boundary(self):
        first = {"id": "a", "body": "x" * (scanner.JSON_HYBRID_MEDIUM_PROBE_CHARS + 1)}
        second = {"id": "b", "mapping": {}}
        payload = json.dumps([first, second], separators=(",", ":"))
        values = list(scanner._iter_json_array([payload]))
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0]["id"], "a")
        self.assertEqual(values[1]["id"], "b")

    def test_benchmark_decoder_counter_and_real_harness_self_tests(self):
        root = Path(__file__).resolve().parents[1]
        child_env = os.environ.copy()
        # These subprocesses include production wall-clock acceptance checks.
        # A strict parent suite may use deep allocation tracing to diagnose
        # resource lifecycles; do not let that diagnostic mode alter the
        # independent process timings or job deadlines being exercised here.
        child_env.pop("PYTHONTRACEMALLOC", None)
        counter = subprocess.run(
            [
                sys.executable,
                str(root / "tools" / "benchmark_resources.py"),
                "--counter-self-test",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            env=child_env,
        )
        self.assertTrue(json.loads(counter.stdout)["detected"])
        harness = subprocess.run(
            [
                sys.executable,
                str(root / "tools" / "acceptance_real_pipeline.py"),
                "--self-test",
                "--python",
                sys.executable,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            env=child_env,
        )
        payload = json.loads(harness.stdout)
        self.assertEqual(payload["aggregate"]["successful_runs"], 1)
        self.assertTrue(payload["samples"][0]["cleanup"]["complete"])

        for extra_args, expected_error in (
            (["--max-job-seconds", "0"], None),
            (["--self-test-cleanup-failure"], "PermissionError"),
        ):
            failed = subprocess.run(
                [
                    sys.executable,
                    str(root / "tools" / "acceptance_real_pipeline.py"),
                    "--self-test",
                    "--python",
                    sys.executable,
                    *extra_args,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                env=child_env,
            )
            self.assertNotEqual(failed.returncode, 0)
            failed_payload = json.loads(failed.stdout)
            self.assertFalse(failed_payload["samples"][0]["success"])
            if expected_error is not None:
                self.assertEqual(
                    failed_payload["samples"][0]["cleanup"]["error_type"],
                    expected_error,
                )

        scale = subprocess.run(
            [
                sys.executable,
                str(root / "tools" / "acceptance_scale.py"),
                "--self-test",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            env=child_env,
        )
        scale_payload = json.loads(scale.stdout)
        self.assertEqual(scale_payload["tiers"], [1000])
        self.assertEqual(scale_payload["coverage"], {
            "many-small:1000:0:performance": 1,
            "many-small:1000:0:allocation_diagnostic": 1,
        })
        self.assertEqual(
            scale_payload["aggregate"]["1000"]["performance_measurement_mode"],
            "fresh_subprocess_without_tracemalloc",
        )
        self.assertTrue(
            scale_payload["resource_stress_fixture_self_test"]
            ["invalid_elements_are_not_valid_data"]
        )
        self.assertTrue(scale_payload["valid_data_fixture_self_test"]["valid_data"])
        representative = scale_payload["representative_valid_data_fixture_self_test"]
        self.assertTrue(representative["production_entry"])
        self.assertEqual(len(representative["runs"]), 2)
        self.assertTrue(all(run["verify_ok"] for run in representative["runs"]))
        self.assertTrue(all(run["web_index_complete"] for run in representative["runs"]))
        hash_lock = subprocess.run(
            [
                sys.executable,
                str(root / "tools" / "verify_web_hash_lock.py"),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            env=child_env,
        )
        self.assertIn("hash_lock_valid true packages 17", hash_lock.stdout)

    def test_cli_public_wall_time_tracks_subprocess_exit(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            write_zip(
                archive,
                {"conversations.json": [conversation("timing-synthetic")]},
            )
            started = time.perf_counter()
            child_env = os.environ.copy()
            # The outer resource-lifecycle acceptance intentionally enables
            # deep allocation tracing.  Do not inject that test-only teardown
            # cost into the production CLI process whose public wall clock is
            # being compared with its actual subprocess lifetime.
            child_env.pop("PYTHONTRACEMALLOC", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(root / "chatgpt_archive.py"),
                    "--db",
                    str(database),
                    "import",
                    "--input",
                    str(archive),
                    "--no-input-sha256",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                env=child_env,
            )
            external = time.perf_counter() - started
            fields: dict[str, float] = {}
            for line in completed.stdout.splitlines():
                key, separator, value = line.partition(" ")
                if separator and key.endswith("_seconds"):
                    fields[key] = float(value)
            self.assertGreaterEqual(
                fields["cli_controlled_wall_seconds"],
                fields["pipeline_return_seconds"],
            )
            self.assertGreaterEqual(fields["cli_output_flush_seconds"], 0.0)
            for value in (*fields.values(), external):
                self.assertTrue(math.isfinite(value))
                self.assertGreaterEqual(value, 0.0)
            # The CLI-controlled clock ends before interpreter teardown and
            # process wait.  Only the outer harness measures process wall.
            self.assertGreaterEqual(
                external,
                fields["cli_controlled_wall_seconds"],
            )


if __name__ == "__main__":
    unittest.main()
