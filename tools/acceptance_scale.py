from __future__ import annotations

"""Fresh-process, content-safe parser and resource-scale acceptance.

Ordinary unit tests keep small production contracts.  This opt-in harness runs
each requested tier in a fresh child, emits one JSON record per tier/run, and
checks that every requested tier ran exactly once per repetition.  Fixtures are
synthetic and output contains hashes, sizes, counts, and resource metrics only.
"""

import argparse
import io
import hashlib
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import zipfile
from pathlib import Path
from typing import Any, Iterable
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatgpt_export_archiver import scanner  # noqa: E402
from chatgpt_export_archiver.cli import ImportPipelineError, run_import_pipeline  # noqa: E402
from chatgpt_export_archiver.db import verify_database  # noqa: E402
from chatgpt_export_archiver.disk_resources import DiskSpaceInsufficientError  # noqa: E402
from chatgpt_export_archiver.json_safety import JsonSafetyLimitError  # noqa: E402
from chatgpt_export_archiver.web_db import (  # noqa: E402
    _PROCESS_LOCK_SHARDS,
    _process_lock_registry,
    acquire_writer_process_lock,
    create_web_indexes,
)

MOVED_SCALE_CASES = {
    "tests.test_round11.Round11RegressionTests."
    "test_five_thousand_node_metadata_density_matrix_uses_joint_profile": {
        "scenario": "metadata-density",
        "tiers": [10, 25, 50, 100, 200],
        "nodes_per_tier": 5_000,
    },
    "tests.test_archiver.ArchiverTests."
    "test_round10_metadata_dense_five_thousand_node_element_imports": {
        "scenario": "metadata-density",
        "tiers": [30],
        "nodes_per_tier": 5_000,
    },
}


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _environment() -> dict[str, Any]:
    conn = __import__("sqlite3").connect(":memory:")
    try:
        compile_options = [
            str(row[0]) for row in conn.execute("PRAGMA compile_options")
        ]
    finally:
        conn.close()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "sqlite": __import__("sqlite3").sqlite_version,
        "sqlite_compile_options": compile_options,
        "filesystem": "local-temporary-not-used",
    }


def _chunks(value: str, chars: int = 64 * 1024) -> Iterable[str]:
    for offset in range(0, len(value), chars):
        yield value[offset : offset + chars]


def _measure(
    scenario: str,
    tier: int,
    payload: str,
    action,
    *,
    trace_allocations: bool,
) -> dict[str, Any]:
    encoded = payload.encode("utf-8")
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    traced_peak: int | None = None
    if trace_allocations:
        tracemalloc.start()
    try:
        result = action()
        if trace_allocations:
            _current, traced_peak = tracemalloc.get_traced_memory()
    finally:
        if trace_allocations:
            tracemalloc.stop()
    return {
        "scenario": scenario,
        "tier": tier,
        "fixture_sha256": hashlib.sha256(encoded).hexdigest(),
        "fixture_utf8_bytes": len(encoded),
        "wall_seconds": time.perf_counter() - started_wall,
        "cpu_seconds": time.process_time() - started_cpu,
        "peak_rss_bytes": _rss_bytes(),
        "tracemalloc_peak_bytes": traced_peak,
        "measurement_mode": (
            "allocation_diagnostic" if trace_allocations else "performance"
        ),
        "performance_eligible": not trace_allocations,
        **result,
    }


def _many_small_worker(elements: int, *, trace_allocations: bool) -> dict[str, Any]:
    element = '{"id":"s","mapping":{}}'
    payload = "[" + ",".join([element] * elements) + "]"

    def run() -> dict[str, Any]:
        count = sum(1 for _value in scanner._iter_json_array(_chunks(payload)))
        if count != elements:
            raise RuntimeError("many_small_element_count_mismatch")
        return {
            "elements": count,
            "source_chunks": (len(payload) + 65535) // 65536,
            "expected_error": None,
        }

    return _measure(
        "many-small", elements, payload, run,
        trace_allocations=trace_allocations,
    )


def _single_element_worker(size_mib: int, *, trace_allocations: bool) -> dict[str, Any]:
    target = size_mib * 1024 * 1024
    prefix = '[{"id":"s","mapping":{},"body":"'
    suffix = '"}]'
    payload = prefix + "x" * max(0, target - len(prefix) - len(suffix)) + suffix

    def run() -> dict[str, Any]:
        values = list(scanner._iter_json_array(_chunks(payload)))
        if len(values) != 1:
            raise RuntimeError("single_element_count_mismatch")
        return {
            "elements": 1,
            "decoded_chars": int(getattr(values[0], "decoded_chars", 0)),
            "source_chunks": (len(payload) + 65535) // 65536,
            "expected_error": None,
        }

    return _measure(
        "single-element-mib", size_mib, payload, run,
        trace_allocations=trace_allocations,
    )


def _mapping_budget_worker(entries: int, *, trace_allocations: bool) -> dict[str, Any]:
    # Duplicate keys intentionally minimize input bytes. The lexical contract
    # still counts every mapping entry before the decoder can materialize the
    # object or collapse duplicate keys.
    payload = "[{" + ",".join(['"k":0'] * entries) + "}]"

    class RefusingDecoder(json.JSONDecoder):
        def decode(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("decoder_called_after_predecode_limit")

        def raw_decode(self, *_args: Any, **_kwargs: Any) -> Any:
            raise json.JSONDecodeError("bounded probe", "", 0)

    def run() -> dict[str, Any]:
        observed: str | None = None
        try:
            with mock.patch.object(
                scanner, "MAX_JSON_MAPPING_ENTRIES", entries - 1
            ), mock.patch.object(scanner.json, "JSONDecoder", RefusingDecoder):
                list(scanner._iter_json_array(_chunks(payload)))
        except JsonSafetyLimitError as exc:
            observed = exc.code
        if observed != "json_mapping_entry_limit_exceeded":
            raise RuntimeError("mapping_predecode_contract_failed")
        return {
            "mapping_entries_scanned": entries,
            "source_chunks": (len(payload) + 65535) // 65536,
            "expected_error": observed,
            "decoder_materialization_calls": 0,
        }

    return _measure(
        "mapping-predecode", entries, payload, run,
        trace_allocations=trace_allocations,
    )


def _metadata_density_worker(fields_per_node: int, *, trace_allocations: bool) -> dict[str, Any]:
    node_count = 5_000
    metadata = {
        f"k{index:03d}": f"v{index:03d}" for index in range(fields_per_node)
    }
    mapping: dict[str, Any] = {}
    for index in range(node_count):
        node_id = f"n-{index}"
        next_id = f"n-{index + 1}" if index + 1 < node_count else None
        mapping[node_id] = {
            "id": node_id,
            "parent": f"n-{index - 1}" if index else None,
            "children": [next_id] if next_id is not None else [],
            "message": {
                "id": f"m-{index}",
                "author": {"role": "assistant"},
                "create_time": 1_700_000_000 + index,
                "update_time": 1_700_000_000 + index,
                "content": {"content_type": "text", "parts": ["synthetic"]},
                "metadata": metadata,
            },
        }
    payload = json.dumps(
        [
            {
                "id": f"density-{fields_per_node}",
                "conversation_id": f"density-{fields_per_node}",
                "title": "Synthetic",
                "create_time": 1_700_000_000,
                "update_time": 1_700_005_000,
                "current_node": f"n-{node_count - 1}",
                "mapping": mapping,
            }
        ],
        separators=(",", ":"),
    )

    def run() -> dict[str, Any]:
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "conversations.json"
            database = base / "archive.db"
            source.write_text(payload, encoding="utf-8")
            run_import_pipeline(
                database,
                str(source),
                cwd=base,
                no_input_sha256=True,
            )
            conn = sqlite3.connect(database)
            try:
                stored_nodes = int(
                    conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0]
                )
                stored_conversations = int(
                    conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                )
                warning_count = int(
                    conn.execute("SELECT COUNT(*) FROM import_warnings").fetchone()[0]
                )
                journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            finally:
                conn.close()
            if (
                stored_nodes != node_count
                or stored_conversations != 1
                or warning_count != 0
            ):
                raise RuntimeError("metadata_density_import_mismatch")
            return {
                "elements": 1,
                "nodes": stored_nodes,
                "metadata_fields_per_node": fields_per_node,
                "warning_count": warning_count,
                "source_chunks": (len(payload) + 65535) // 65536,
                "database_bytes": database.stat().st_size,
                "wal_bytes": (
                    database.with_name(database.name + "-wal").stat().st_size
                    if database.with_name(database.name + "-wal").exists()
                    else 0
                ),
                "temp_bytes": 0,
                "journal_mode": journal_mode,
                "expected_error": None,
            }

    return _measure(
        "metadata-density", fields_per_node, payload, run,
        trace_allocations=trace_allocations,
    )


def _registry_lifecycle_worker(database_count: int, *, trace_allocations: bool) -> dict[str, Any]:
    payload = f"registry-lifecycle:{database_count}"

    def run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for index in range(database_count):
                lock = acquire_writer_process_lock(base / f"db-{index}.sqlite3")
                lock.close()
            registry = _process_lock_registry()
            shard_files = [
                path
                for path in registry.iterdir()
                if path.name.startswith("writer-shard-") and path.name.endswith(".lock")
            ]
            if len(shard_files) > _PROCESS_LOCK_SHARDS:
                raise RuntimeError("writer_registry_not_bounded")
            return {
                "database_lifecycles": database_count,
                "registry_shard_files": len(shard_files),
                "registry_shard_limit": _PROCESS_LOCK_SHARDS,
                "expected_error": None,
            }

    return _measure(
        "registry-lifecycle", database_count, payload, run,
        trace_allocations=trace_allocations,
    )


def _write_exact_sized_valid_element(
    writer: Any,
    *,
    element_size: int,
    element_index: int,
) -> None:
    prefix = (
        b'{"id":"valid-'
        + str(element_index).encode("ascii")
        + b'","conversation_id":"valid-'
        + str(element_index).encode("ascii")
        + b'","title":"Synthetic","current_node":"node-'
        + str(element_index).encode("ascii")
        + b'","mapping":{"node-'
        + str(element_index).encode("ascii")
        + b'":{"id":"node-'
        + str(element_index).encode("ascii")
        + b'","parent":null,"children":[],"message":{"id":"message-'
        + str(element_index).encode("ascii")
        + b'","author":{"role":"user"},"create_time":1700000000,'
          b'"update_time":1700000000,"content":{"content_type":"text",'
          b'"parts":["synthetic valid payload"]},"metadata":{}}}},"padding":"'
    )
    suffix = b'"}'
    padding = element_size - len(prefix) - len(suffix)
    if padding < 0:
        raise RuntimeError("logical_archive_element_too_small")
    writer.write(prefix)
    block = b"x" * (1024 * 1024)
    while padding:
        count = min(padding, len(block))
        writer.write(block[:count])
        padding -= count
    writer.write(suffix)


def _write_exact_sized_invalid_element(
    writer: Any,
    *,
    element_size: int,
    element_index: int,
) -> None:
    prefix = (
        b'{"synthetic_resource_index":'
        + str(element_index).encode("ascii")
        + b',"padding":"'
    )
    suffix = b'"}'
    padding = element_size - len(prefix) - len(suffix)
    if padding < 0:
        raise RuntimeError("logical_archive_element_too_small")
    block = b"x" * (1024 * 1024)
    writer.write(prefix)
    while padding:
        count = min(padding, len(block))
        writer.write(block[:count])
        padding -= count
    writer.write(suffix)


def _valid_data_fixture_self_test() -> dict[str, int | bool]:
    stream = io.BytesIO()
    stream.write(b"[")
    _write_exact_sized_valid_element(
        stream, element_size=4096, element_index=0
    )
    stream.write(b"]")
    payload = stream.getvalue()
    decoded = json.loads(payload)
    valid = bool(
        isinstance(decoded, list)
        and len(decoded) == 1
        and isinstance(decoded[0], dict)
        and decoded[0].get("conversation_id") == "valid-0"
        and isinstance(decoded[0].get("mapping"), dict)
        and "node-0" in decoded[0]["mapping"]
        and decoded[0]["mapping"]["node-0"].get("message") is not None
    )
    if not valid:
        raise RuntimeError("valid_data_fixture_not_valid")
    return {
        "valid_data": True,
        "elements": 1,
        "fixture_utf8_bytes": len(payload),
    }


def _representative_valid_data_fixture_self_test() -> dict[str, Any]:
    """Exercise the representative fixture shapes through production entries."""
    import sqlite3

    def conversation(index: int, node_count: int, *, padding: int = 0) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for node_index in range(node_count):
            node_id = f"node-{index}-{node_index}"
            next_id = f"node-{index}-{node_index + 1}" if node_index + 1 < node_count else None
            mapping[node_id] = {
                "id": node_id,
                "parent": f"node-{index}-{node_index - 1}" if node_index else None,
                "children": [next_id] if next_id else [],
                "message": {
                    "id": f"message-{index}-{node_index}",
                    "author": {"role": "user" if node_index % 2 == 0 else "assistant"},
                    "create_time": 1_700_000_000 + node_index,
                    "update_time": 1_700_000_000 + node_index,
                    "content": {"content_type": "text", "parts": ["synthetic valid text"]},
                    "metadata": {},
                },
            }
        result = {
            "id": f"fixture-{index}",
            "conversation_id": f"fixture-{index}",
            "title": "Synthetic",
            "current_node": f"node-{index}-{node_count - 1}",
            "mapping": mapping,
        }
        if padding:
            result["ignored_padding"] = "x" * padding
        return result

    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        fixtures: list[tuple[str, dict[str, list[dict[str, Any]]], int]] = [
            (
                "many-shards-low-compression",
                {
                    f"conversations-{index:03d}.json": [conversation(index, 1)]
                    for index in range(3)
                },
                zipfile.ZIP_STORED,
            ),
            (
                "single-shard-mixed-high-compression",
                {
                    "conversations.json": [
                        *(conversation(100 + index, 1) for index in range(5)),
                        conversation(200, 50),
                        conversation(201, 2, padding=32 * 1024),
                    ]
                },
                zipfile.ZIP_DEFLATED,
            ),
        ]
        for run_index, (profile, members, compression) in enumerate(fixtures):
            archive = base / f"fixture-{run_index}.zip"
            database = base / f"fixture-{run_index}.db"
            with zipfile.ZipFile(archive, "w", compression=compression, allowZip64=True) as bundle:
                for member, rows in members.items():
                    bundle.writestr(
                        member,
                        json.dumps(rows, separators=(",", ":")).encode("utf-8"),
                    )
            run_import_pipeline(database, str(archive), cwd=base, no_input_sha256=False)
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                verify_ok = bool(verify_database(conn)["ok"])
                conversations = int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])
                nodes = int(conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0])
                warnings = int(conn.execute("SELECT COUNT(*) FROM import_warnings").fetchone()[0])
            finally:
                conn.close()
            index_result = create_web_indexes(database)
            if not verify_ok or warnings or not index_result.get("atomic_publish"):
                raise RuntimeError("representative_valid_data_self_test_failed")
            runs.append({
                "profile": profile,
                "physical_zip_bytes": archive.stat().st_size,
                "committed_conversations": conversations,
                "committed_nodes": nodes,
                "verify_ok": verify_ok,
                "web_index_complete": True,
            })
    return {
        "production_entry": True,
        "profiles": [
            "valid-many-small",
            "valid-mixed",
            "high-selected-json-ratio",
            "low-compression-valid",
            "high-compression-valid",
            "many-shards-valid",
            "single-large-shard-valid",
            "dense-conversation",
        ],
        "opt_in_scale_profiles": {
            "five_thousand_node_dense_conversation": "metadata-density",
            "physical_low_compression_gib": "valid-data-gib",
        },
        "runs": runs,
    }


def _valid_data_worker(
    size_gib: int, *, trace_allocations: bool
) -> dict[str, Any]:
    import sqlite3

    target_bytes = size_gib * 1024 * 1024 * 1024
    maximum_element_bytes = 30 * 1024 * 1024
    available_for_elements = target_bytes - 2
    element_count = max(1, math.ceil(available_for_elements / maximum_element_bytes))
    base_size, extra = divmod(
        available_for_elements - max(0, element_count - 1), element_count
    )
    if base_size <= 0 or base_size > maximum_element_bytes:
        raise RuntimeError("logical_archive_tier_layout_invalid")

    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    traced_peak: int | None = None
    if trace_allocations:
        tracemalloc.start()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        archive = base / "synthetic-scale.zip"
        database = base / "archive.db"
        with zipfile.ZipFile(
            archive,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as bundle:
            with bundle.open("conversations.json", "w", force_zip64=True) as writer:
                writer.write(b"[")
                for index in range(element_count):
                    if index:
                        writer.write(b",")
                    _write_exact_sized_valid_element(
                        writer,
                        element_size=base_size + (1 if index < extra else 0),
                        element_index=index,
                    )
                writer.write(b"]")
        compressed_bytes = archive.stat().st_size
        archive_sha256 = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                archive_sha256.update(chunk)
        outcome = "completed"
        capacity: dict[str, int] | None = None
        summary: dict[str, Any] | None = None
        index_result: dict[str, Any] | None = None
        verify_ok: bool | None = None
        committed_conversations = 0
        skipped_elements = 0
        warning_count = 0
        try:
            summary = run_import_pipeline(
                database,
                str(archive),
                cwd=base,
                no_input_sha256=False,
            )
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                verify_ok = bool(verify_database(conn)["ok"])
                committed_conversations = int(
                    conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                )
                warning_count = int(
                    conn.execute("SELECT COUNT(*) FROM import_warnings").fetchone()[0]
                )
                run_summary = json.loads(
                    conn.execute(
                        "SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]
                )
                skipped_elements = int(
                    run_summary.get(
                        "skipped_invalid_elements",
                        run_summary.get("skipped_elements", 0),
                    )
                )
            finally:
                conn.close()
            if (
                committed_conversations != element_count
                or skipped_elements != 0
                or warning_count != 0
            ):
                raise RuntimeError(
                    "resource_stress_import_contract_mismatch "
                    f"committed={committed_conversations} "
                    f"skipped={skipped_elements} warnings={warning_count} "
                    f"expected_committed={element_count}"
                )
            index_result = create_web_indexes(database)
        except (DiskSpaceInsufficientError, ImportPipelineError) as exc:
            if (
                isinstance(exc, ImportPipelineError)
                and exc.code != "import_disk_space_insufficient"
            ):
                raise
            outcome = exc.code
            detail = exc.detail if isinstance(exc, ImportPipelineError) else {}
            capacity = {
                "required_bytes": int(
                    detail.get(
                        "required_bytes",
                        getattr(exc, "required_bytes", 0),
                    )
                ),
                "free_bytes": int(
                    detail.get("free_bytes", getattr(exc, "free_bytes", 0))
                ),
            }
        if trace_allocations:
            _current, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        database_bytes = database.stat().st_size if database.exists() else 0
        wal = database.with_name(database.name + "-wal")
        result = {
            "scenario": "valid-data-gib",
            "tier": size_gib,
            "fixture_sha256": archive_sha256.hexdigest(),
            "fixture_utf8_bytes": target_bytes,
            "compressed_bytes": compressed_bytes,
            "selected_logical_json_bytes": target_bytes,
            "elements": element_count,
            "physical_zip_bytes": compressed_bytes,
            "valid_element_bytes": target_bytes - 2 - max(0, element_count - 1),
            "skipped_element_bytes": 0,
            "committed_conversations": committed_conversations,
            "committed_nodes": committed_conversations,
            "skipped_invalid_elements": skipped_elements,
            "warning_count": warning_count,
            "outcome": outcome,
            "capacity": capacity,
            "verify_ok": verify_ok,
            "web_index_complete": (
                bool(index_result.get("atomic_publish")) if index_result else None
            ),
            "indexed_message_bytes": (
                int(index_result.get("input_materialized_bytes", 0))
                if index_result else 0
            ),
            "canonical_db_peak": database_bytes,
            "wal_peak": wal.stat().st_size if wal.exists() else 0,
            "journal_peak": 0,
            "temp_peak": 0,
            "web_index_live_peak": None,
            "web_index_staging_peak": None,
            "spool_peak": 0,
            "diagnostic_limitations": [
                "filesystem peak sampler unavailable for unlinked SQLite TEMP files",
                "CLI input is not a Web upload spool",
            ],
            "database_bytes": database_bytes,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "temp_bytes": 0,
            "wall_seconds": time.perf_counter() - started_wall,
            "cpu_seconds": time.process_time() - started_cpu,
            "peak_rss_bytes": _rss_bytes(),
            "tracemalloc_peak_bytes": traced_peak,
            "measurement_mode": (
                "allocation_diagnostic" if trace_allocations else "performance"
            ),
            "performance_eligible": not trace_allocations,
        }
    return result


def _resource_stress_worker(
    size_gib: int, *, trace_allocations: bool
) -> dict[str, Any]:
    """Exercise large invalid elements plus one valid tail without mislabelling it."""
    import sqlite3

    target_bytes = size_gib * 1024 * 1024 * 1024
    maximum_element_bytes = 30 * 1024 * 1024
    valid_tail = (
        b'{"id":"resource-valid","conversation_id":"resource-valid",'
        b'"title":"Synthetic","current_node":"root","mapping":{"root":{'
        b'"id":"root","parent":null,"children":[],"message":null}}}'
    )
    available_for_invalid = target_bytes - len(valid_tail) - 3
    element_count = max(1, math.ceil(available_for_invalid / maximum_element_bytes))
    base_size, extra = divmod(
        available_for_invalid - max(0, element_count - 1), element_count
    )
    if base_size <= 0 or base_size > maximum_element_bytes:
        raise RuntimeError("resource_stress_tier_layout_invalid")

    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    traced_peak: int | None = None
    if trace_allocations:
        tracemalloc.start()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        archive = base / "synthetic-resource-stress.zip"
        database = base / "archive.db"
        with zipfile.ZipFile(
            archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as bundle:
            with bundle.open("conversations.json", "w", force_zip64=True) as writer:
                writer.write(b"[")
                for index in range(element_count):
                    if index:
                        writer.write(b",")
                    _write_exact_sized_invalid_element(
                        writer,
                        element_size=base_size + (1 if index < extra else 0),
                        element_index=index,
                    )
                writer.write(b",")
                writer.write(valid_tail)
                writer.write(b"]")
        physical_zip_bytes = archive.stat().st_size
        archive_sha256 = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                archive_sha256.update(chunk)
        outcome = "completed"
        capacity: dict[str, int] | None = None
        verify_ok: bool | None = None
        index_result: dict[str, Any] | None = None
        committed_conversations = 0
        committed_nodes = 0
        skipped_elements = 0
        warning_count = 0
        try:
            run_import_pipeline(
                database,
                str(archive),
                cwd=base,
                no_input_sha256=False,
            )
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                verify_ok = bool(verify_database(conn)["ok"])
                committed_conversations = int(
                    conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                )
                committed_nodes = int(
                    conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0]
                )
                warning_count = int(
                    conn.execute("SELECT COUNT(*) FROM import_warnings").fetchone()[0]
                )
                run_summary = json.loads(
                    conn.execute(
                        "SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]
                )
                skipped_elements = int(run_summary.get("skipped_invalid_elements", 0))
            finally:
                conn.close()
            if (
                committed_conversations != 1
                or skipped_elements != element_count
                or warning_count < element_count
            ):
                raise RuntimeError("resource_stress_import_contract_mismatch")
            index_result = create_web_indexes(database)
        except (DiskSpaceInsufficientError, ImportPipelineError) as exc:
            if isinstance(exc, ImportPipelineError) and exc.code != "import_disk_space_insufficient":
                raise
            outcome = exc.code
            detail = exc.detail if isinstance(exc, ImportPipelineError) else {}
            capacity = {
                "required_bytes": int(detail.get("required_bytes", getattr(exc, "required_bytes", 0))),
                "free_bytes": int(detail.get("free_bytes", getattr(exc, "free_bytes", 0))),
            }
        if trace_allocations:
            _current, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        database_bytes = database.stat().st_size if database.exists() else 0
        wal = database.with_name(database.name + "-wal")
        result = {
            "scenario": "resource-stress-gib",
            "tier": size_gib,
            "fixture_sha256": archive_sha256.hexdigest(),
            "fixture_utf8_bytes": target_bytes,
            "physical_zip_bytes": physical_zip_bytes,
            "selected_logical_json_bytes": target_bytes,
            "valid_element_bytes": len(valid_tail),
            "skipped_element_bytes": available_for_invalid,
            "elements": element_count + 1,
            "large_invalid_elements": element_count,
            "committed_conversations": committed_conversations,
            "committed_nodes": committed_nodes,
            "skipped_invalid_elements": skipped_elements,
            "warning_count": warning_count,
            "outcome": outcome,
            "capacity": capacity,
            "verify_ok": verify_ok,
            "web_index_complete": bool(index_result.get("atomic_publish")) if index_result else None,
            "indexed_message_bytes": int(index_result.get("input_materialized_bytes", 0)) if index_result else 0,
            "canonical_db_peak": database_bytes,
            "wal_peak": wal.stat().st_size if wal.exists() else 0,
            "journal_peak": 0,
            "temp_peak": 0,
            "web_index_live_peak": None,
            "web_index_staging_peak": None,
            "spool_peak": 0,
            "diagnostic_limitations": [
                "invalid elements are intentionally skipped",
                "filesystem peak sampler unavailable for unlinked SQLite TEMP files",
                "CLI input is not a Web upload spool",
            ],
            "database_bytes": database_bytes,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "temp_bytes": 0,
            "wall_seconds": time.perf_counter() - started_wall,
            "cpu_seconds": time.process_time() - started_cpu,
            "peak_rss_bytes": _rss_bytes(),
            "tracemalloc_peak_bytes": traced_peak,
            "measurement_mode": "allocation_diagnostic" if trace_allocations else "performance",
            "performance_eligible": not trace_allocations,
        }
    return result


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    trace_allocations = args.measurement_mode == "allocation_diagnostic"
    if args.scenario == "many-small":
        return _many_small_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "single-element-mib":
        return _single_element_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "mapping-predecode":
        return _mapping_budget_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "metadata-density":
        return _metadata_density_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "registry-lifecycle":
        return _registry_lifecycle_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "resource-stress-gib":
        return _resource_stress_worker(args.tier, trace_allocations=trace_allocations)
    if args.scenario == "valid-data-gib":
        return _valid_data_worker(args.tier, trace_allocations=trace_allocations)
    raise RuntimeError("unknown_scale_scenario")


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    performance = [sample for sample in samples if sample["performance_eligible"]]
    diagnostic = [sample for sample in samples if not sample["performance_eligible"]]
    result: dict[str, Any] = {
        "performance_measurement_mode": "fresh_subprocess_without_tracemalloc",
        "allocation_diagnostic_mode": "fresh_subprocess_with_tracemalloc",
    }
    for key in ("wall_seconds", "cpu_seconds", "peak_rss_bytes"):
        values = [float(sample[key]) for sample in performance]
        result[f"performance_{key}"] = {
            "all": values,
            "median": statistics.median(values),
            "worst": max(values),
        }
    allocation_values = [
        float(sample["tracemalloc_peak_bytes"]) for sample in diagnostic
    ]
    result["diagnostic_tracemalloc_peak_bytes"] = {
        "all": allocation_values,
        "median": statistics.median(allocation_values),
        "worst": max(allocation_values),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=(
            "many-small",
            "single-element-mib",
            "mapping-predecode",
            "metadata-density",
            "registry-lifecycle",
            "resource-stress-gib",
            "valid-data-gib",
        ),
        default="many-small",
    )
    parser.add_argument("--tiers", default="")
    parser.add_argument("--tier", type=int, default=1000, help=argparse.SUPPRESS)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--measurement-mode",
        choices=("performance", "allocation_diagnostic"),
        default="performance",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--confirm-huge",
        action="store_true",
        help="required for the opt-in 1/5/10 GiB production workload",
    )
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(_worker(args), sort_keys=True, separators=(",", ":")))
        return 0

    if args.self_test:
        args.scenario = "many-small"
        tiers = [1000]
        runs = 1
    else:
        defaults = {
            "many-small": [100_000, 1_000_000],
            "single-element-mib": [1, 4, 8, 16, 20, 31],
            "mapping-predecode": [100_000, 500_000, 1_000_000],
            "metadata-density": [10, 25, 50, 100, 200],
            "registry-lifecycle": [100_000],
            "resource-stress-gib": [1, 5, 10],
            "valid-data-gib": [1, 5, 10],
        }
        tiers = (
            [int(item) for item in args.tiers.split(",") if item]
            if args.tiers
            else defaults[args.scenario]
        )
        runs = max(1, args.runs)
    if args.scenario in {"resource-stress-gib", "valid-data-gib"} and not args.confirm_huge:
        raise SystemExit("resource_stress_gib_requires_--confirm-huge")
    if not tiers or any(tier <= 0 for tier in tiers) or len(tiers) != len(set(tiers)):
        raise SystemExit("invalid_scale_tiers")

    samples: list[dict[str, Any]] = []
    coverage: dict[str, int] = {}
    for run_index in range(runs):
        for tier in tiers:
            for measurement_mode in ("performance", "allocation_diagnostic"):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--scenario",
                        args.scenario,
                        "--tier",
                        str(tier),
                        "--measurement-mode",
                        measurement_mode,
                        "--worker",
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                sample = json.loads(completed.stdout)
                sample["run_index"] = run_index
                samples.append(sample)
                key = f"{args.scenario}:{tier}:{run_index}:{measurement_mode}"
                coverage[key] = coverage.get(key, 0) + 1
    expected = {
        f"{args.scenario}:{tier}:{run_index}:{measurement_mode}"
        for run_index in range(runs)
        for tier in tiers
        for measurement_mode in ("performance", "allocation_diagnostic")
    }
    if set(coverage) != expected or any(value != 1 for value in coverage.values()):
        raise RuntimeError("scale_tier_coverage_mismatch")
    grouped = {
        str(tier): _aggregate(
            [sample for sample in samples if int(sample["tier"]) == tier]
        )
        for tier in tiers
    }
    print(
        json.dumps(
            {
                "schema": "chatgpt-sqlite-webui-scale-v3",
                "environment": _environment(),
                "scenario": args.scenario,
                "moved_scale_cases": MOVED_SCALE_CASES,
                "resource_stress_fixture_self_test": (
                    {
                        "invalid_elements_are_not_valid_data": True,
                        "valid_tail_conversations": 1,
                    }
                    if args.self_test else None
                ),
                "valid_data_fixture_self_test": (
                    _valid_data_fixture_self_test() if args.self_test else None
                ),
                "representative_valid_data_fixture_self_test": (
                    _representative_valid_data_fixture_self_test()
                    if args.self_test else None
                ),
                "tiers": tiers,
                "runs_per_tier": runs,
                "coverage": coverage,
                "aggregate": grouped,
                "samples": samples,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
