from __future__ import annotations

"""Fresh-process, content-safe Round 12 parser scale acceptance.

Ordinary unit tests keep small production contracts.  This opt-in harness runs
each requested tier in a fresh child, emits one JSON record per tier/run, and
checks that every requested tier ran exactly once per repetition.  Fixtures are
synthetic and output contains hashes, sizes, counts, and resource metrics only.
"""

import argparse
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


def _measure(scenario: str, tier: int, payload: str, action) -> dict[str, Any]:
    encoded = payload.encode("utf-8")
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    tracemalloc.start()
    result = action()
    _current, traced_peak = tracemalloc.get_traced_memory()
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
        **result,
    }


def _many_small_worker(elements: int) -> dict[str, Any]:
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

    return _measure("many-small", elements, payload, run)


def _single_element_worker(size_mib: int) -> dict[str, Any]:
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

    return _measure("single-element-mib", size_mib, payload, run)


def _mapping_budget_worker(entries: int) -> dict[str, Any]:
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

    return _measure("mapping-predecode", entries, payload, run)


def _metadata_density_worker(fields_per_node: int) -> dict[str, Any]:
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

    return _measure("metadata-density", fields_per_node, payload, run)


def _registry_lifecycle_worker(database_count: int) -> dict[str, Any]:
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

    return _measure("registry-lifecycle", database_count, payload, run)


def _write_exact_sized_invalid_element(
    writer: Any,
    *,
    element_size: int,
    element_index: int,
) -> None:
    prefix = (
        b'{"synthetic_scale_index":'
        + str(element_index).encode("ascii")
        + b',"padding":"'
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


def _logical_archive_worker(size_gib: int) -> dict[str, Any]:
    import sqlite3

    target_bytes = size_gib * 1024 * 1024 * 1024
    maximum_element_bytes = 30 * 1024 * 1024
    valid_tail = (
        b'{"id":"synthetic-valid","conversation_id":"synthetic-valid",'
        b'"title":"Synthetic","current_node":"root","mapping":{"root":{'
        b'"id":"root","parent":null,"children":[],"message":null}}}'
    )
    available_for_large = target_bytes - len(valid_tail) - 3
    element_count = max(1, math.ceil(available_for_large / maximum_element_bytes))
    base_size, extra = divmod(available_for_large - max(0, element_count - 1), element_count)
    if base_size <= 0 or base_size > maximum_element_bytes:
        raise RuntimeError("logical_archive_tier_layout_invalid")

    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    tracemalloc.start()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        archive = base / "synthetic-scale.zip"
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
                committed_conversations != 1
                or skipped_elements != element_count
                or warning_count < element_count
            ):
                raise RuntimeError(
                    "logical_archive_import_contract_mismatch "
                    f"committed={committed_conversations} "
                    f"skipped={skipped_elements} warnings={warning_count} "
                    f"expected_skipped={element_count}"
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
        _current, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        database_bytes = database.stat().st_size if database.exists() else 0
        wal = database.with_name(database.name + "-wal")
        result = {
            "scenario": "logical-archive-gib",
            "tier": size_gib,
            "fixture_sha256": archive_sha256.hexdigest(),
            "fixture_utf8_bytes": target_bytes,
            "compressed_bytes": compressed_bytes,
            "selected_logical_json_bytes": target_bytes,
            "elements": element_count + 1,
            "large_invalid_elements": element_count,
            "valid_elements": committed_conversations,
            "skipped_invalid_elements": skipped_elements,
            "warning_count": warning_count,
            "outcome": outcome,
            "capacity": capacity,
            "verify_ok": verify_ok,
            "web_index_complete": (
                bool(index_result.get("atomic_publish")) if index_result else None
            ),
            "database_bytes": database_bytes,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "temp_bytes": 0,
            "wall_seconds": time.perf_counter() - started_wall,
            "cpu_seconds": time.process_time() - started_cpu,
            "peak_rss_bytes": _rss_bytes(),
            "tracemalloc_peak_bytes": traced_peak,
        }
    return result


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    if args.scenario == "many-small":
        return _many_small_worker(args.tier)
    if args.scenario == "single-element-mib":
        return _single_element_worker(args.tier)
    if args.scenario == "mapping-predecode":
        return _mapping_budget_worker(args.tier)
    if args.scenario == "metadata-density":
        return _metadata_density_worker(args.tier)
    if args.scenario == "registry-lifecycle":
        return _registry_lifecycle_worker(args.tier)
    if args.scenario == "logical-archive-gib":
        return _logical_archive_worker(args.tier)
    raise RuntimeError("unknown_scale_scenario")


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "wall_seconds",
        "cpu_seconds",
        "peak_rss_bytes",
        "tracemalloc_peak_bytes",
    ):
        values = [float(sample[key]) for sample in samples]
        result[key] = {
            "all": values,
            "median": statistics.median(values),
            "worst": max(values),
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
            "logical-archive-gib",
        ),
        default="many-small",
    )
    parser.add_argument("--tiers", default="")
    parser.add_argument("--tier", type=int, default=1000, help=argparse.SUPPRESS)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
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
            "logical-archive-gib": [1, 5, 10],
        }
        tiers = (
            [int(item) for item in args.tiers.split(",") if item]
            if args.tiers
            else defaults[args.scenario]
        )
        runs = max(1, args.runs)
    if args.scenario == "logical-archive-gib" and not args.confirm_huge:
        raise SystemExit("logical_archive_gib_requires_--confirm-huge")
    if not tiers or any(tier <= 0 for tier in tiers) or len(tiers) != len(set(tiers)):
        raise SystemExit("invalid_scale_tiers")

    samples: list[dict[str, Any]] = []
    coverage: dict[str, int] = {}
    for run_index in range(runs):
        for tier in tiers:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--scenario",
                    args.scenario,
                    "--tier",
                    str(tier),
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
            key = f"{args.scenario}:{tier}:{run_index}"
            coverage[key] = coverage.get(key, 0) + 1
    expected = {
        f"{args.scenario}:{tier}:{run_index}"
        for run_index in range(runs)
        for tier in tiers
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
                "schema": "chatgpt-sqlite-webui-round12-scale-v2",
                "environment": _environment(),
                "scenario": args.scenario,
                "moved_scale_cases": MOVED_SCALE_CASES,
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
