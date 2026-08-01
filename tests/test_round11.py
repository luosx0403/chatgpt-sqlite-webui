from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_export_archiver.db import (
    DatabaseMigrationError,
    GENERATION_TRIGGER_DDL,
    connect,
    init_db,
    migrate_database,
)
from chatgpt_export_archiver.disk_resources import DiskSpaceInsufficientError
from chatgpt_export_archiver.search import parse_query, search_conversations, search_messages
from chatgpt_export_archiver.search import (
    DisplayCursorError,
    get_message_display_chunk,
    get_messages,
)
from chatgpt_export_archiver import search as search_module
from chatgpt_export_archiver.web_db import (
    WebIndexBuildError,
    _database_lock_keys,
    _process_lock_registry,
    acquire_web_index_process_lock,
    create_web_indexes,
    web_index_status,
)
from chatgpt_export_archiver.web_jobs import ImportJobManager
from tests.test_archiver import conversation, message_node, run_cli, write_zip


class Round11Regressions(unittest.TestCase):
    def test_search_contract_is_structurally_aligned_across_runtime_schema_openapi_and_types(self):
        from fastapi.testclient import TestClient

        from chatgpt_export_archiver.web_app import create_app

        truth_fields = {
            "total_exact",
            "order_exact",
            "scan_complete",
            "provisional_order",
            "next_offset",
        }
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "contract.db"
            writer = connect(database)
            init_db(writer)
            writer.close()
            client = TestClient(
                create_app(database, allow_fallback=True, log_level="none"),
                base_url="http://localhost",
            )
            runtime = client.get(
                "/api/search/messages",
                params={"q": "synthetic", "count_total": "false"},
            ).json()
            custom = client.get("/api/schema").json()
            openapi = client.get("/openapi.json").json()

        self.assertTrue(truth_fields.issubset(runtime))
        self.assertTrue(
            truth_fields.issubset(custom["pagination"]["message_search_page"])
        )
        openapi_contract = openapi["components"]["schemas"]["SearchTruthContract"]
        self.assertEqual(set(openapi_contract["required"]), truth_fields)
        self.assertEqual(
            set(openapi["x-chatgpt-archive-contract"]["search_truth_fields"]),
            truth_fields,
        )

        types_source = (
            Path(__file__).resolve().parents[1] / "webui" / "src" / "types.ts"
        ).read_text(encoding="utf-8")
        base_page = re.search(
            r"export interface BasePage<[^>]+>\s*\{(?P<body>.*?)^\}",
            types_source,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(base_page)
        type_fields = set(
            re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)\??:", base_page.group("body"), re.MULTILINE)
        )
        self.assertTrue(truth_fields.issubset(type_fields))

    def test_global_partial_order_is_explicit_and_numeric_offset_is_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "provisional.db"
            writer = connect(database)
            init_db(writer)
            for index, text in enumerate(
                ("needle", "needle needle needle", "prefix needle suffix")
            ):
                conversation_id = f"c-{index}"
                node_id = f"n-{index}"
                writer.execute(
                    "INSERT INTO conversations("
                    "conversation_id,title,current_node,aggregate_hash"
                    ") VALUES (?, 'synthetic', ?, ?)",
                    (conversation_id, node_id, f"h-{index}"),
                )
                writer.execute(
                    "INSERT INTO conversation_nodes("
                    "conversation_id,node_id,content_type,content_text,content_hash,"
                    "is_on_current_path) VALUES (?, ?, 'text', ?, ?, 1)",
                    (conversation_id, node_id, text, f"nh-{index}"),
                )
            writer.commit()
            parsed = parse_query("needle", path_default="all")
            with mock.patch.object(search_module, "SEARCH_CANDIDATE_LIMIT", 1):
                first = search_messages(
                    writer,
                    parsed,
                    limit=1,
                    offset=0,
                    count_total=False,
                    order="relevance",
                )
            writer.close()
        self.assertFalse(first["order_exact"])
        self.assertTrue(first["provisional_order"])
        self.assertFalse(first["scan_complete"])
        self.assertFalse(first["total_exact"])
        self.assertIsNone(first["next_offset"])
        self.assertIsInstance(first["diagnostics"]["continuation_token"], str)

    def test_round11_benchmark_harness_emits_required_json_counters(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "tools" / "benchmark_round11.py"),
                "--scenario",
                "json-framing",
                "--size-mib",
                "1",
                "--runs",
                "1",
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema"], "chatgpt-sqlite-webui-round12-benchmark-v2")
        sample = payload["samples"][0]
        self.assertEqual(
            sample["decoder"]["decode_calls"], sample["decoder"]["elements"]
        )
        self.assertLessEqual(sample["decoder"]["raw_decode_failed_probes"], 1)
        self.assertGreater(
            sample["decoder"]["raw_decode_available_chars"],
            sample["decoder"]["output_decoded_chars"],
        )
        self.assertEqual(sample["cleanup"]["remaining_bytes"], 0)
        self.assertEqual(
            set(sample),
            {
                "scenario",
                "environment",
                "fixture_sha256",
                "wall_seconds",
                "cpu_seconds",
                "peak_rss_bytes",
                "storage",
                "sqlite",
                "blob",
                "decoder",
                "resolver",
                "normalizer",
                "lock",
                "cleanup",
            },
        )

    def test_stable_optional_index_survives_canonical_rowid_changes_and_vacuum(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            item = conversation("stable-address", title="Stable address title")
            item["mapping"]["u1"]["message"]["content"]["parts"] = [
                "x" * (4 * 1024 * 1024 + 4096) + " stable-address-needle"
            ]
            ordinary = conversation("stable-ordinary", title="Stable ordinary title")
            ordinary["mapping"]["u1"]["message"]["content"]["parts"] = [
                "stable-ordinary-needle"
            ]
            write_zip(archive, {"conversations.json": [item, ordinary]})
            code, output = run_cli([
                "--db", str(database), "import", "--input", str(archive),
                "--no-input-sha256",
            ])
            self.assertEqual(code, 0, output)
            built = create_web_indexes(database)
            self.assertGreaterEqual(built["oversized_messages"], 1)

            writer = sqlite3.connect(database)
            writer.execute("UPDATE conversations SET rowid = rowid + 10000")
            writer.execute("UPDATE conversation_nodes SET rowid = rowid + 20000")
            writer.commit()
            writer.execute("VACUUM")
            writer.close()

            reader = connect(database)
            try:
                self.assertTrue(web_index_status(reader)["web_index_format_current"])
                messages = search_messages(
                    reader,
                    parse_query("stable-address-needle"),
                    limit=10,
                    offset=0,
                    count_total=True,
                )
                titles = search_conversations(
                    reader,
                    parse_query("", title="Stable address title", scope="title"),
                    limit=10,
                    offset=0,
                )
                self.assertEqual(len(messages["items"]), 1)
                self.assertTrue(messages["total_exact"])
                ordinary_messages = search_messages(
                    reader,
                    parse_query("stable-ordinary-needle"),
                    limit=10,
                    offset=0,
                    count_total=True,
                )
                self.assertEqual(len(ordinary_messages["items"]), 1)
                self.assertTrue(ordinary_messages["total_exact"])
                self.assertEqual(len(titles["items"]), 1)
            finally:
                reader.close()

            sqlite_cli = Path("/usr/bin/sqlite3")
            if sqlite_cli.exists():
                rebuilt = base / "dump-loaded.db"
                dump = subprocess.run(
                    [str(sqlite_cli), str(database), ".dump"],
                    check=True,
                    capture_output=True,
                ).stdout
                subprocess.run(
                    [str(sqlite_cli), str(rebuilt)],
                    input=dump,
                    check=True,
                    capture_output=True,
                )
                rebuilt_reader = connect(rebuilt)
                try:
                    self.assertEqual(
                        rebuilt_reader.execute("PRAGMA integrity_check").fetchone()[0],
                        "ok",
                    )
                    self.assertTrue(
                        web_index_status(rebuilt_reader)["web_index_format_current"]
                    )
                    self.assertEqual(
                        len(
                            search_messages(
                                rebuilt_reader,
                                parse_query("stable-ordinary-needle"),
                                limit=10,
                                offset=0,
                                count_total=True,
                            )["items"]
                        ),
                        1,
                    )
                    self.assertEqual(
                        len(
                            search_messages(
                                rebuilt_reader,
                                parse_query("stable-address-needle"),
                                limit=10,
                                offset=0,
                                count_total=True,
                            )["items"]
                        ),
                        1,
                    )
                    self.assertEqual(
                        len(
                            search_conversations(
                                rebuilt_reader,
                                parse_query(
                                    "",
                                    title="Stable address title",
                                    scope="title",
                                ),
                                limit=10,
                                offset=0,
                            )["items"]
                        ),
                        1,
                    )
                finally:
                    rebuilt_reader.close()

    def test_database_lock_aliases_contend_and_unknown_registry_file_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            registry_root = base / "registry-root"
            registry_root.mkdir()
            with mock.patch(
                "chatgpt_export_archiver.web_db.tempfile.gettempdir",
                return_value=str(registry_root),
            ):
                database = base / "archive.db"
                database.write_bytes(b"synthetic database identity")
                hardlink = base / "hardlink.db"
                os.link(database, hardlink)
                symlink = base / "symlink.db"
                symlink.symlink_to(database)

                held = acquire_web_index_process_lock(database)
                try:
                    for alias in (hardlink, symlink):
                        with self.assertRaises(WebIndexBuildError) as caught:
                            acquire_web_index_process_lock(alias)
                        self.assertEqual(caught.exception.code, "writer_process_lock_busy")
                finally:
                    held.close()

                missing = base / "missing.db"
                key = _database_lock_keys(missing)[0]
                from chatgpt_export_archiver.web_db import _process_lock_name
                collision = _process_lock_registry() / _process_lock_name(key)
                collision.write_bytes(b"unknown-owner")
                collision.chmod(0o600)
                before = (
                    collision.read_bytes(),
                    collision.stat().st_mode,
                    collision.stat().st_mtime_ns,
                )
                with self.assertRaises(WebIndexBuildError) as caught:
                    acquire_web_index_process_lock(missing)
                self.assertEqual(caught.exception.code, "web_index_process_lock_failed")
                self.assertEqual(
                    before,
                    (
                        collision.read_bytes(),
                        collision.stat().st_mode,
                        collision.stat().st_mtime_ns,
                    ),
                )

    def test_noop_reimport_preserves_generations_nodes_and_current_web_index(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            write_zip(archive, {"conversations.json": [conversation("no-op")]})
            self.assertEqual(run_cli([
                "--db", str(database), "import", "--input", str(archive),
                "--no-input-sha256",
            ])[0], 0)
            create_web_indexes(database)
            before = sqlite3.connect(database)
            generations = before.execute(
                "SELECT name, generation FROM archive_generations ORDER BY name"
            ).fetchall()
            nodes = before.execute(
                "SELECT node_id, display_revision, last_import_run_id "
                "FROM conversation_nodes ORDER BY node_id"
            ).fetchall()
            optional_objects = before.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE name LIKE 'web_%' ORDER BY type, name"
            ).fetchall()
            before.close()

            code, output = run_cli([
                "--db", str(database), "import", "--input", str(archive),
                "--no-input-sha256",
            ])
            self.assertEqual(code, 0, output)
            self.assertIn("unchanged_conversations 1", output)
            after = sqlite3.connect(database)
            self.assertEqual(generations, after.execute(
                "SELECT name, generation FROM archive_generations ORDER BY name"
            ).fetchall())
            self.assertEqual(nodes, after.execute(
                "SELECT node_id, display_revision, last_import_run_id "
                "FROM conversation_nodes ORDER BY node_id"
            ).fetchall())
            self.assertEqual(optional_objects, after.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE name LIKE 'web_%' ORDER BY type, name"
            ).fetchall())
            after.close()
            status_reader = connect(database)
            try:
                self.assertTrue(web_index_status(status_reader)["web_index_format_current"])
            finally:
                status_reader.close()

    def test_title_and_metadata_only_reimports_report_real_dirty_domains(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            initial = conversation("dirty-domain", title="Initial synthetic title")
            write_zip(archive, {"conversations.json": [initial]})
            self.assertEqual(
                run_cli(
                    [
                        "--db",
                        str(database),
                        "import",
                        "--input",
                        str(archive),
                        "--no-input-sha256",
                    ]
                )[0],
                0,
            )
            create_web_indexes(database)

            def state() -> tuple[dict[str, int], dict[str, object]]:
                reader = sqlite3.connect(database)
                try:
                    generations = {
                        str(name): int(value)
                        for name, value in reader.execute(
                            "SELECT name, generation FROM archive_generations "
                            "WHERE name IN ('message','title','address','graph')"
                        )
                    }
                    summary = json.loads(
                        reader.execute(
                            "SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
                    )
                    return generations, summary
                finally:
                    reader.close()

            before_generations, _ = state()
            metadata_only = conversation(
                "dirty-domain", title="Initial synthetic title"
            )
            metadata_only["is_archived"] = True
            write_zip(archive, {"conversations.json": [metadata_only]})
            code, output = run_cli(
                [
                    "--db",
                    str(database),
                    "import",
                    "--input",
                    str(archive),
                    "--no-input-sha256",
                ]
            )
            self.assertEqual(code, 0, output)
            metadata_generations, metadata_summary = state()
            self.assertEqual(metadata_summary["updated_conversations"], 1)
            self.assertEqual(metadata_summary["unchanged_conversations"], 0)
            self.assertEqual(metadata_summary["dirty_domains"], [])
            self.assertEqual(metadata_generations, before_generations)
            metadata_reader = connect(database)
            try:
                self.assertTrue(
                    web_index_status(metadata_reader)["web_index_format_current"]
                )
            finally:
                metadata_reader.close()

            title_only = conversation("dirty-domain", title="Changed synthetic title")
            title_only["is_archived"] = True
            write_zip(archive, {"conversations.json": [title_only]})
            code, output = run_cli(
                [
                    "--db",
                    str(database),
                    "import",
                    "--input",
                    str(archive),
                    "--no-input-sha256",
                ]
            )
            self.assertEqual(code, 0, output)
            title_generations, title_summary = state()
            self.assertEqual(title_summary["updated_conversations"], 1)
            self.assertEqual(title_summary["unchanged_conversations"], 0)
            self.assertEqual(title_summary["dirty_domains"], ["title"])
            self.assertEqual(
                title_generations["title"], metadata_generations["title"] + 1
            )
            for domain in ("message", "address", "graph"):
                self.assertEqual(
                    title_generations[domain], metadata_generations[domain]
                )
            title_reader = connect(database)
            try:
                self.assertFalse(
                    web_index_status(title_reader)["web_index_format_current"]
                )
            finally:
                title_reader.close()

    def test_continuation_binds_real_index_contract_and_instance_secret(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "continuation-contract.db"
            writer = connect(database)
            init_db(writer)
            for index in range(4):
                conversation_id = f"contract-{index}"
                node_id = f"node-{index}"
                writer.execute(
                    "INSERT INTO conversations("
                    "conversation_id,title,current_node,aggregate_hash"
                    ") VALUES (?, 'synthetic', ?, ?)",
                    (conversation_id, node_id, f"hash-{index}"),
                )
                writer.execute(
                    "INSERT INTO conversation_nodes("
                    "conversation_id,node_id,content_type,content_text,"
                    "content_hash,is_on_current_path"
                    ") VALUES (?, ?, 'text', 'contract continuation needle', ?, 1)",
                    (conversation_id, node_id, f"node-hash-{index}"),
                )
            writer.commit()
            writer.close()
            create_web_indexes(database)
            reader = connect(database)
            try:
                def token() -> str:
                    with mock.patch.object(
                        search_module, "SEARCH_CANDIDATE_LIMIT", 1
                    ):
                        page = search_messages(
                            reader,
                            parse_query(
                                "contract continuation needle",
                                path_default="all",
                            ),
                            limit=1,
                            offset=0,
                            count_total=False,
                        )
                    value = page["diagnostics"]["continuation_token"]
                    self.assertIsInstance(value, str)
                    return value

                for key in (
                    "web_index_format_version",
                    "display_text_resolver_version",
                    "normalization_index_format_version",
                    "stable_optional_address_version",
                    "stable_optional_address_identity",
                ):
                    continuation = token()
                    original = reader.execute(
                        "SELECT value FROM web_index_metadata WHERE key = ?", (key,)
                    ).fetchone()[0]
                    reader.execute(
                        "UPDATE web_index_metadata SET value = ? WHERE key = ?",
                        (f"{original}-changed", key),
                    )
                    reader.commit()
                    with self.assertRaises(search_module.SearchContinuationError) as caught:
                        search_messages(
                            reader,
                            parse_query(
                                "contract continuation needle",
                                path_default="all",
                            ),
                            limit=1,
                            offset=0,
                            count_total=False,
                            continuation=continuation,
                        )
                    self.assertEqual(
                        caught.exception.code, "search_continuation_stale"
                    )
                    reader.execute(
                        "UPDATE web_index_metadata SET value = ? WHERE key = ?",
                        (original, key),
                    )
                    reader.commit()

                continuation = token()
                old_secret = search_module._SEARCH_CONTINUATION_SECRET
                search_module._SEARCH_CONTINUATION_SECRET = os.urandom(32)
                try:
                    with self.assertRaises(
                        search_module.SearchContinuationError
                    ) as caught:
                        search_messages(
                            reader,
                            parse_query(
                                "contract continuation needle",
                                path_default="all",
                            ),
                            limit=1,
                            offset=0,
                            count_total=False,
                            continuation=continuation,
                        )
                    self.assertEqual(
                        caught.exception.code, "invalid_search_continuation"
                    )
                finally:
                    search_module._SEARCH_CONTINUATION_SECRET = old_secret
            finally:
                reader.close()

    def test_five_thousand_node_metadata_density_matrix_uses_joint_profile(self):
        # The 5,000-node × five-density matrix now runs in fresh subprocesses
        # through tools/acceptance_scale_round12.py --scenario metadata-density.
        # This retained test ID exercises the same production path at ordinary
        # suite scale so ResourceWarning/lifecycle coverage is never skipped.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            node_count = 64
            for density in (30,):
                archive = base / f"density-{density}.zip"
                database = base / f"density-{density}.db"
                metadata = {f"k{index:03d}": "v" for index in range(density)}
                mapping = {
                    f"n-{index}": {
                        **message_node(
                            f"n-{index}",
                            None,
                            "assistant",
                            "synthetic",
                            1_700_000_000 + index,
                            [],
                        ),
                    }
                    for index in range(node_count)
                }
                for node in mapping.values():
                    node["message"]["metadata"] = metadata
                write_zip(
                    archive,
                    {
                        "conversations.json": [
                            conversation(
                                f"density-{density}",
                                current_node=f"n-{node_count - 1}",
                                mapping=mapping,
                            )
                        ]
                    },
                )
                inspect_code, inspect_output = run_cli(
                    ["inspect", "--input", str(archive)]
                )
                self.assertEqual(inspect_code, 0, inspect_output)
                self.assertIn("valid_conversations 1", inspect_output)
                code, output = run_cli(
                    [
                        "--db",
                        str(database),
                        "import",
                        "--input",
                        str(archive),
                        "--no-input-sha256",
                    ]
                )
                self.assertEqual(code, 0, output)
                reader = sqlite3.connect(database)
                try:
                    self.assertEqual(
                        reader.execute(
                            "SELECT COUNT(*) FROM conversation_nodes"
                        ).fetchone()[0],
                        node_count,
                    )
                    summary = json.loads(
                        reader.execute(
                            "SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
                    )
                    self.assertEqual(summary["committed_conversations"], 1)
                    self.assertEqual(summary["committed_nodes"], node_count)
                    self.assertEqual(summary["skipped_invalid_elements"], 0)
                finally:
                    reader.close()

    def test_placeholder_leading_whitespace_is_consistent_before_and_after_index(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "placeholder.db"
            writer = connect(database)
            init_db(writer)
            lengths = (0, 1, 255, 256, 257, 300, 4096, 65536)
            for index, length in enumerate(lengths):
                conversation_id = f"placeholder-{index}"
                node_id = f"node-{index}"
                needle = f"safe-placeholder-needle-{index}"
                whitespace = ("\u3000" if index == 1 else " ") * length
                placeholder = whitespace + "[non-text content: synthetic]"
                raw = json.dumps(
                    {"content": {"content_type": "text", "parts": [needle]}},
                    separators=(",", ":"),
                )
                writer.execute(
                    "INSERT INTO conversations("
                    "conversation_id, title, current_node, aggregate_hash"
                    ") VALUES (?, ?, ?, ?)",
                    (conversation_id, f"title-{index}", node_id, f"hash-{index}"),
                )
                writer.execute(
                    """INSERT INTO conversation_nodes(
                           conversation_id, node_id, content_type, content_text,
                           content_hash, raw_message_json, is_on_current_path
                       ) VALUES (?, ?, 'legacy', ?, ?, ?, 1)""",
                    (conversation_id, node_id, placeholder, f"node-hash-{index}", raw),
                )
            writer.commit()

            for index, _length in enumerate(lengths):
                conversation_id = f"placeholder-{index}"
                needle = f"safe-placeholder-needle-{index}"
                page = get_messages(
                    writer,
                    conversation_id,
                    path="all",
                    limit=10,
                    offset=0,
                )
                self.assertEqual(page["items"][0]["display_text"], needle)
                hits = search_messages(
                    writer,
                    parse_query(needle, path_default="all"),
                    conversation_id=conversation_id,
                    limit=10,
                    offset=0,
                    count_total=True,
                )
                self.assertEqual(len(hits["items"]), 1)
            writer.close()

            create_web_indexes(database)
            reader = connect(database)
            try:
                for index, _length in enumerate(lengths):
                    hits = search_messages(
                        reader,
                        parse_query(
                            f"safe-placeholder-needle-{index}",
                            path_default="all",
                        ),
                        conversation_id=f"placeholder-{index}",
                        limit=10,
                        offset=0,
                        count_total=True,
                    )
                    self.assertEqual(len(hits["items"]), 1)
                    self.assertTrue(hits["total_exact"])
            finally:
                reader.close()

    def test_display_cursor_tamper_is_rejected_without_rescanning(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "cursor.db"
            writer = connect(database)
            init_db(writer)
            writer.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) "
                "VALUES ('c', 't', 'n', 'h')"
            )
            writer.execute(
                "INSERT INTO conversation_nodes("
                "conversation_id, node_id, content_type, content_text, content_hash, "
                "is_on_current_path"
                ") VALUES ('c', 'n', 'text', ?, 'nh', 1)",
                ("🙂e\u0301" * 100000,),
            )
            writer.commit()
            first = get_message_display_chunk(
                writer,
                "c",
                "n",
                offset=0,
                limit=65536,
            )
            cursor = first["next_cursor"]
            self.assertIsInstance(cursor, str)
            replacement = "A" if cursor[-1] != "A" else "B"
            tampered = cursor[:-1] + replacement
            with self.assertRaises(DisplayCursorError) as caught:
                get_message_display_chunk(
                    writer,
                    "c",
                    "n",
                    offset=first["next_offset"],
                    limit=65536,
                    cursor=tampered,
                )
            self.assertEqual(caught.exception.code, "invalid_display_cursor")
            writer.close()

    def test_legacy_16k_identifier_uses_fixed_size_search_continuation(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "long-id.db"
            writer = connect(database)
            init_db(writer)
            long_id = ("長/?:#%" * 3000)[: 16 * 1024]
            for index, conversation_id in enumerate((long_id, "z-short")):
                node_id = f"node-{index}"
                writer.execute(
                    "INSERT INTO conversations("
                    "conversation_id, title, current_node, aggregate_hash"
                    ") VALUES (?, ?, ?, ?)",
                    (conversation_id, f"title-{index}", node_id, f"hash-{index}"),
                )
                writer.execute(
                    "INSERT INTO conversation_nodes("
                    "conversation_id, node_id, content_type, content_text, "
                    "content_hash, is_on_current_path"
                    ") VALUES (?, ?, 'text', 'continuation needle', ?, 1)",
                    (conversation_id, node_id, f"node-hash-{index}"),
                )
            writer.commit()
            with mock.patch.object(search_module, "SEARCH_CANDIDATE_LIMIT", 1):
                first = search_conversations(
                    writer,
                    parse_query("continuation needle"),
                    limit=1,
                    offset=0,
                )
                token = first["diagnostics"]["continuation_token"]
                self.assertIsInstance(token, str)
                self.assertLess(len(token), 256)
                self.assertNotIn(long_id[:64], token)
                self.assertIsNone(first["next_offset"])
                second = search_conversations(
                    writer,
                    parse_query("continuation needle"),
                    limit=1,
                    offset=0,
                    continuation=token,
                )
                self.assertIsNone(second["next_offset"])
            writer.close()

    def test_noncharacter_identifier_import_and_compatibility_refresh_agree(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "input.zip"
            database = base / "archive.db"
            identifier = "noncharacter-\ufdd0-\ufffe-\U0010ffff"
            write_zip(
                archive,
                {"conversations.json": [conversation(identifier)]},
            )
            code, output = run_cli([
                "--db", str(database), "import", "--input", str(archive),
                "--no-input-sha256",
            ])
            self.assertEqual(code, 0, output)
            writer = connect(database)
            refreshed = migrate_database(writer, refresh_compatibility=True)
            self.assertTrue(refreshed["schema_compatible"])
            self.assertEqual(
                writer.execute(
                    "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?",
                    (identifier,),
                ).fetchone()[0],
                1,
            )
            writer.close()

    def test_missing_input_and_new_parent_create_no_lock_or_database_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            database = base / "new-parent" / "archive.db"
            lock_paths = [
                _process_lock_registry() / f"{kind}-{identity}.lock"
                for kind, identity in _database_lock_keys(database)
            ]
            before = {path: path.exists() for path in lock_paths}
            code, output = run_cli([
                "--db", str(database), "import",
                "--input", str(base / "missing.zip"),
            ])
            self.assertEqual(code, 2)
            self.assertIn("input_not_found", output)
            self.assertFalse(database.exists())
            self.assertFalse(database.parent.exists())
            self.assertEqual(before, {path: path.exists() for path in lock_paths})

    def test_migration_disk_preflight_and_cancel_leave_v4_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "predecessor.db"
            predecessor = connect(database)
            init_db(predecessor)
            predecessor.execute(
                "INSERT INTO conversations(conversation_id, title, aggregate_hash) "
                "VALUES ('c', 't', 'h')"
            )
            predecessor.execute(
                "INSERT INTO conversation_nodes(conversation_id, node_id, content_text) "
                "VALUES ('c', 'n', 'body')"
            )
            predecessor.commit()
            for name in GENERATION_TRIGGER_DDL:
                predecessor.execute(f'DROP TRIGGER "{name}"')
            predecessor.execute('DROP TRIGGER "archive_display_revision_node_insert"')
            predecessor.execute('DROP TRIGGER "archive_display_revision_node_update"')
            predecessor.execute("ALTER TABLE conversation_nodes DROP COLUMN display_revision")
            predecessor.execute(
                "INSERT INTO archive_generations(name, generation) VALUES ('display:1', 1)"
            )
            predecessor.execute("PRAGMA user_version = 4")
            predecessor.commit()
            before = (
                database.read_bytes(),
                predecessor.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0],
                predecessor.execute("PRAGMA user_version").fetchone()[0],
            )

            with mock.patch(
                "chatgpt_export_archiver.db.require_free_space",
                side_effect=DiskSpaceInsufficientError(
                    "migration_disk_space_insufficient",
                    required_bytes=999,
                    free_bytes=1,
                ),
            ):
                with self.assertRaises(DatabaseMigrationError) as caught:
                    migrate_database(predecessor)
            self.assertEqual(caught.exception.code, "migration_disk_space_insufficient")
            self.assertFalse(predecessor.in_transaction)
            self.assertEqual(before[1:], (
                predecessor.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0],
                predecessor.execute("PRAGMA user_version").fetchone()[0],
            ))
            self.assertEqual(before[0], database.read_bytes())

            with self.assertRaises(DatabaseMigrationError) as caught:
                migrate_database(predecessor, cancel_check=lambda: True)
            self.assertEqual(caught.exception.code, "database_migration_cancelled")
            self.assertFalse(predecessor.in_transaction)
            progress: list[tuple[str, int, int]] = []
            result = migrate_database(
                predecessor,
                progress_callback=lambda stage, value: progress.append(
                    (stage, value["processed"], value["total"])
                ),
            )
            self.assertTrue(result["changed"])
            self.assertTrue(any(stage == "backfill_display_revisions" for stage, _, _ in progress))
            predecessor.close()

    def test_web_import_job_reports_partial_success_aggregates(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            upload = base / "upload.zip"
            mapping = {
                f"n-{index}": {
                    "id": f"n-{index}",
                    "parent": None,
                    "children": [],
                    "message": None,
                }
                for index in range(5001)
            }
            write_zip(
                upload,
                {
                    "conversations.json": [
                        conversation("too-many", current_node="n-0", mapping=mapping),
                        conversation("valid-after-limit"),
                    ]
                },
            )
            manager = ImportJobManager(base / "archive.db", log_level="none")
            job = manager.start_import(
                upload,
                filename="synthetic.zip",
                size=upload.stat().st_size,
            )
            deadline = time.monotonic() + 30
            while job.snapshot()["status"] in {"queued", "running"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            snapshot = job.snapshot()
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["canonical_import_outcome"], "partial_success")
            self.assertEqual(snapshot["completion_outcome"], "partial_success")
            self.assertTrue(snapshot["canonical_commit_succeeded"])
            self.assertEqual(snapshot["summary"]["committed_conversations"], 1)
            self.assertEqual(snapshot["summary"]["skipped_invalid_elements"], 1)
            self.assertEqual(snapshot["summary"]["warnings"], 1)
            self.assertEqual(
                snapshot["summary"]["warnings_by_type"],
                [{"warning_type": "conversation_node_limit_exceeded", "count": 1}],
            )
            serialized = json.dumps(snapshot)
            self.assertNotIn("too-many", serialized)
            self.assertNotIn("valid-after-limit", serialized)


if __name__ == "__main__":
    unittest.main()
