from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chatgpt_export_archiver import current_path, exporter, scanner, search, web_api, web_db
from chatgpt_export_archiver.cli import main
from chatgpt_export_archiver.current_path import EffectiveCurrentResourceLimitError
from chatgpt_export_archiver.db import DatabaseMigrationError, connect, init_db, migrate_database
from chatgpt_export_archiver.disk_resources import migration_capacity_plan
from chatgpt_export_archiver.search import SearchContinuationError
from chatgpt_export_archiver.web_db import WebIndexBuildError, create_web_indexes, web_index_status
from chatgpt_export_archiver.web_jobs import ImportJob, ImportJobManager, ImportJobStartError
from chatgpt_export_archiver.web_app import create_app
from tests.test_archiver import conversation, write_zip


class ResourceSecurityRegressions(unittest.TestCase):
    def test_unrelated_database_shard_collision_uses_independent_ranges(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            web_db, "_process_lock_name", return_value="writer-shard-00.lock"
        ):
            first = web_db.acquire_writer_process_lock(Path(td) / "one.db")
            try:
                second = web_db.acquire_writer_process_lock(Path(td) / "two.db")
                second.close()
            finally:
                first.close()

    def test_writer_cleanup_attempts_every_handle_and_retains_failure(self):
        handles = [
            (('registry', 'one'), 1),
            (('registry', 'two'), 2),
            (('registry', 'three'), 3),
        ]
        for failed in ({handles[0]}, {handles[1]}, {handles[2]}, {handles[0], handles[2]}):
            with self.subTest(failed=failed):
                lock = web_db._WriterProcessLock(handles.copy(), set(), set())
                attempted: list[object] = []

                def release(handle):
                    attempted.append(handle)
                    if handle in failed:
                        raise OSError("synthetic")

                with mock.patch.object(web_db, "_release_process_lock_handle", side_effect=release):
                    with self.assertRaises(WebIndexBuildError) as caught:
                        lock.close()
                self.assertEqual(caught.exception.code, "writer_process_lock_cleanup_failed")
                self.assertEqual(attempted, list(reversed(handles)))
                self.assertEqual(lock.fds, [handle for handle in handles if handle in failed])
                self.assertEqual(len(caught.exception.cleanup_warnings), len(failed))

    def test_writer_acquire_preserves_primary_when_cleanup_also_fails(self):
        acquired = (("registry", "first"), 1)
        primary = OSError("primary")
        calls = 0

        def acquire(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return acquired
            raise primary

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            web_db, "_database_lock_keys", return_value=[("path", "one"), ("entity", "two")]
        ), mock.patch.object(web_db, "_process_lock_domain", side_effect=[("writer-shard-00.lock", 1), ("writer-shard-01.lock", 2)]), mock.patch.object(
            web_db, "_acquire_process_lock_key", side_effect=acquire
        ), mock.patch.object(
            web_db, "_release_process_lock_handle", side_effect=OSError("cleanup")
        ):
            with self.assertRaises(OSError) as caught:
                web_db.acquire_writer_process_lock(Path(td) / "archive.db")
        self.assertIs(caught.exception, primary)
        self.assertEqual(
            caught.exception.cleanup_warnings[0]["code"],
            "writer_process_lock_cleanup_failed",
        )

    def test_pending_slot_and_thread_start_return_cleanup_warnings(self):
        warning = {
            "code": "writer_process_lock_cleanup_failed",
            "error_type": "OSError",
            "path_kind": "process_lock",
        }

        class FailingLock:
            def close(self):
                raise WebIndexBuildError(
                    "writer_process_lock_cleanup_failed",
                    cleanup_warnings=[warning],
                )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manager = ImportJobManager(base / "archive.db")
            manager._running_job_id = "__pending_upload__"
            manager._pending_writer_lock = FailingLock()
            self.assertEqual(manager.release_pending_upload_slot(), [warning])
            self.assertFalse(manager.has_running_job())

            upload = base / "upload.zip"
            upload.write_bytes(b"synthetic")
            manager._running_job_id = "__pending_upload__"
            manager._pending_writer_lock = FailingLock()
            with mock.patch(
                "chatgpt_export_archiver.web_jobs.threading.Thread",
                side_effect=RuntimeError("synthetic thread failure"),
            ), self.assertRaises(ImportJobStartError) as caught:
                manager.start_import(upload, filename="synthetic.zip", size=9)
            self.assertEqual(caught.exception.code, "import_job_start_failed")
            self.assertEqual(caught.exception.cleanup_warnings, [warning])
            self.assertFalse(manager.has_running_job())

    def test_postcommit_writer_cleanup_cannot_regress_canonical_outcome(self):
        class FailingLock:
            def close(self):
                raise WebIndexBuildError(
                    "writer_process_lock_cleanup_failed",
                    cleanup_warnings=[{
                        "code": "writer_process_lock_cleanup_failed",
                        "error_type": "OSError",
                        "path_kind": "process_lock",
                    }],
                )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload = base / "upload" / "upload.zip"
            manager = ImportJobManager(base / "archive.db")
            job = ImportJob(
                job_id="0" * 32,
                db_path=base / "archive.db",
                upload_path=upload,
                filename="synthetic.zip",
                size=0,
                status="running",
                stage="succeeded",
                outcome="succeeded",
                completion_outcome="success",
                canonical_import_outcome="success",
                canonical_commit_succeeded=True,
                _terminal_status="succeeded",
                _writer_lock=FailingLock(),
            )
            manager._jobs[job.job_id] = job
            manager._running_job_id = job.job_id
            manager._finalize_job(job)
            snapshot = job.snapshot()
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertTrue(snapshot["canonical_commit_succeeded"])
        self.assertEqual(snapshot["canonical_import_outcome"], "success")
        self.assertEqual(snapshot["completion_outcome"], "cleanup_warning")
        self.assertEqual(
            snapshot["cleanup_warnings"][0]["code"],
            "writer_process_lock_cleanup_failed",
        )

    def test_zip_metadata_limits_run_before_zipfile_materialization(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "input.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("conversations.json", b"[]")
            with archive.open("rb") as stream, mock.patch.object(
                scanner, "MAX_ZIP_CENTRAL_DIRECTORY_BYTES", 1
            ):
                with self.assertRaisesRegex(
                    ValueError, "source_zip_central_directory_limit_exceeded"
                ):
                    scanner.preflight_zip_central_directory(stream, max_members=10)

    def test_zip_member_path_limit_is_checked_in_central_directory(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "input.zip"
            long_name = "x" * 33_000 + "/conversations.json"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(long_name, b"[]")
            with archive.open("rb") as stream:
                with self.assertRaisesRegex(
                    ValueError, "source_relative_path_limit_exceeded"
                ):
                    scanner.preflight_zip_central_directory(stream, max_members=10)

    def test_high_width_unicode_coalesces_by_utf8_bytes(self):
        with mock.patch.object(scanner, "MAX_JSON_ELEMENT_BYTES", 8):
            windows = list(scanner._coalesce_json_text_chunks(iter(["😀", "😀", "😀"])))
        self.assertEqual(windows, ["😀😀", "😀"])

    def test_search_continuation_rejects_unused_bit_alias(self):
        token = search._encode_search_continuation({"synthetic": True})
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        index = alphabet.index(token[-1])
        alias = token[:-1] + alphabet[index ^ 1]
        self.assertNotEqual(alias, token)
        with self.assertRaises(SearchContinuationError) as caught:
            search._decode_search_continuation(alias)
        self.assertEqual(caught.exception.code, "invalid_search_continuation")

    def test_global_scope_limit_rejects_before_scope_population(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from chatgpt_export_archiver.db import init_db

        init_db(conn)
        conn.execute(
            "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
            "VALUES ('one','synthetic','hash')"
        )
        conn.commit()
        with mock.patch.object(current_path, "MAX_EFFECTIVE_CURRENT_CONVERSATIONS", 0):
            with self.assertRaises(EffectiveCurrentResourceLimitError):
                current_path.ensure_effective_current_views(conn, None)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM effective_current_scope").fetchone()[0],
            0,
        )
        conn.close()

    def test_project_node_time_only_reimport_preserves_text_domains(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            initial = conversation("time-only")
            write_zip(archive, {"conversations.json": [initial]})
            self.assertEqual(
                main(["--db", str(database), "import", "--input", str(archive), "--no-input-sha256"]),
                0,
            )
            create_web_indexes(database)
            reader = connect(database)
            before_generations = dict(reader.execute(
                "SELECT name,generation FROM archive_generations"
            ))
            before_revision, before_raw = reader.execute(
                "SELECT display_revision,raw_message_json FROM conversation_nodes "
                "WHERE conversation_id='time-only' AND node_id='u1'"
            ).fetchone()
            reader.close()

            changed = conversation("time-only")
            changed["mapping"]["u1"]["message"]["create_time"] += 10
            changed["mapping"]["u1"]["message"]["update_time"] += 10
            write_zip(archive, {"conversations.json": [changed]})
            self.assertEqual(
                main(["--db", str(database), "import", "--input", str(archive), "--no-input-sha256"]),
                0,
            )
            reader = connect(database)
            after_generations = dict(reader.execute(
                "SELECT name,generation FROM archive_generations"
            ))
            after_revision, after_raw = reader.execute(
                "SELECT display_revision,raw_message_json FROM conversation_nodes "
                "WHERE conversation_id='time-only' AND node_id='u1'"
            ).fetchone()
            summary = json.loads(reader.execute(
                "SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])
            self.assertEqual(after_generations["query"], before_generations["query"] + 1)
            for domain in ("message", "title", "address", "graph"):
                self.assertEqual(after_generations[domain], before_generations[domain])
            self.assertEqual(after_revision, before_revision)
            self.assertNotEqual(after_raw, before_raw)
            self.assertEqual(summary["dirty_domains"], ["query"])
            self.assertTrue(web_index_status(reader)["web_index_format_current"])
            reader.close()

    def test_visible_copy_uses_one_snapshot_across_selected_rows(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            fixture = conversation("copy-snapshot")
            fixture["mapping"]["u1"]["message"]["content"]["parts"] = ["old-first"]
            fixture["mapping"]["a1"]["message"]["content"]["parts"] = ["old-second"]
            write_zip(archive, {"conversations.json": [fixture]})
            self.assertEqual(
                main(["--db", str(database), "import", "--input", str(archive), "--no-input-sha256"]),
                0,
            )
            original_resolver = exporter.recover_message_display_text
            calls = 0

            def update_between_rows(content_text, raw_message_json):
                nonlocal calls
                calls += 1
                value = original_resolver(content_text, raw_message_json)
                if calls == 1:
                    writer = sqlite3.connect(database, timeout=5)
                    try:
                        writer.execute(
                            "UPDATE conversation_nodes SET content_text='new-second' "
                            "WHERE conversation_id='copy-snapshot' AND node_id='a1'"
                        )
                        writer.commit()
                    finally:
                        writer.close()
                return value

            with mock.patch.object(web_api, "VISIBLE_COPY_QUERY_BATCH", 1), mock.patch.object(
                exporter, "recover_message_display_text", side_effect=update_between_rows
            ):
                response = TestClient(create_app(database)).post(
                    "/api/by-id/copy-visible?conversation_id=copy-snapshot",
                    json={"node_ids": ["u1", "a1"]},
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn("old-first", response.text)
            self.assertIn("old-second", response.text)
            self.assertNotIn("new-second", response.text)
            stale = TestClient(create_app(database)).post(
                "/api/by-id/copy-visible?conversation_id=copy-snapshot",
                json={"node_ids": ["u1", "missing-node"]},
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["detail"], "copy_selection_stale")

    def test_display_chunk_unclassifiable_large_canonical_is_never_exact(self):
        from chatgpt_export_archiver.utils import sha256_text

        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "display-budget.db"
            conn = connect(database)
            init_db(conn)
            big = "[non-text content: " + "x" * (40 * 1024 * 1024)
            conn.execute(
                "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
                "VALUES ('c','synthetic','h')"
            )
            conn.execute(
                "INSERT INTO conversation_nodes("
                "conversation_id,node_id,content_type,content_text,content_hash,"
                "is_on_current_path,display_revision) VALUES ("
                "'c','n','text',?,?,1,'deadbeefdeadbeefdeadbeefdeadbeef')",
                (big, sha256_text("synthetic")),
            )
            conn.commit()
            chunk = search.get_message_display_chunk(
                conn, "c", "n", offset=0, limit=65536
            )
            self.assertIsNotNone(chunk)
            self.assertTrue(chunk["resolver_input_truncated"])
            self.assertFalse(chunk["total_chars_exact"])
            self.assertTrue(chunk["has_more"])
            self.assertIsNone(chunk["next_offset"])
            self.assertLess(chunk["returned_chars"], 40 * 1024 * 1024)
            conn.close()

    def test_migration_capacity_is_step_specific(self):
        light = migration_capacity_plan(5, 8 * 1024 * 1024, 1_000_000)
        rewrite = migration_capacity_plan(4, 8 * 1024 * 1024, 1_000_000)
        self.assertEqual(light["planned_steps"], [
            "query_generation_metadata", "compatibility_scan"
        ])
        self.assertEqual(light["estimated_peak_category"], "metadata_and_scan")
        self.assertLess(light["required_free_bytes"], 64 * 1024 * 1024)
        self.assertGreater(rewrite["required_free_bytes"], light["required_free_bytes"])

    def test_current_compatibility_refresh_cancels_mid_scan_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "compatibility.db"
            conn = connect(database)
            init_db(conn)
            conn.execute(
                "INSERT INTO conversations(conversation_id,title,aggregate_hash) "
                "VALUES ('c','synthetic','h')"
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id,node_id,content_text) "
                "VALUES ('c',?,'synthetic')",
                ((f"n-{index}",) for index in range(6_000)),
            )
            conn.execute(
                "UPDATE archive_generations SET generation=generation+1 WHERE name='address'"
            )
            conn.commit()
            before = [tuple(row) for row in conn.execute(
                "SELECT domain,checked_generation,status,incompatible_count "
                "FROM archive_compatibility_state ORDER BY domain"
            )]
            cancel = False
            progress: list[int] = []

            def on_progress(stage, value):
                nonlocal cancel
                if stage == "compatibility_scan":
                    progress.append(value["processed"])
                    if value["processed"] >= 5_000:
                        cancel = True

            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(
                    conn,
                    refresh_compatibility=True,
                    progress_callback=on_progress,
                    cancel_check=lambda: cancel,
                )
            self.assertEqual(caught.exception.code, "database_migration_cancelled")
            self.assertTrue(progress)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT domain,checked_generation,status,incompatible_count "
                    "FROM archive_compatibility_state ORDER BY domain"
                )],
                before,
            )
            # A cancelled progress handler must not contaminate later SQL.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0], 6_000)
            conn.close()

if __name__ == "__main__":
    unittest.main()
