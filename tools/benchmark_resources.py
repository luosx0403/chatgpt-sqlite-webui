from __future__ import annotations

"""Opt-in synthetic resource and performance benchmark.

The parent process starts each sample in a fresh Python subprocess so peak RSS
and SQLite state do not leak between samples. Output is JSON and contains no
archive content, paths, titles, messages, or identifiers from user data.
"""

import argparse
import hashlib
import io
import json
import os
import platform
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatgpt_export_archiver.db import (  # noqa: E402
    _KNOWN_MANAGED_TRIGGER_PREDECESSORS,
    init_db,
    migrate_database,
)
from chatgpt_export_archiver.scanner import (  # noqa: E402
    _iter_json_array,
    _iter_utf8_chunks,
)


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _tree_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total


def _environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "pid": os.getpid(),
    }


def _base_metrics(scenario: str, fixture_sha256: str) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "environment": _environment(),
        "fixture_sha256": fixture_sha256,
        "wall_seconds": 0.0,
        "cpu_seconds": 0.0,
        "peak_rss_bytes": 0,
        "storage": {
            "db_bytes": 0,
            "wal_peak_bytes": 0,
            "temp_peak_bytes": 0,
            "index_peak_bytes": 0,
        },
        "sqlite": {"sql_statements": 0, "vm_steps_approx": 0},
        "blob": {"reads": 0, "read_bytes": 0},
        "decoder": {
            "source_chunks": 0,
            "source_utf8_bytes": 0,
            "elements": 0,
            "decode_calls": 0,
            "decode_input_chars": 0,
            "decode_input_bytes": 0,
            "decode_failed_calls": 0,
            "raw_decode_calls": 0,
            "raw_decode_available_chars": 0,
            "raw_decode_available_bytes": 0,
            "raw_decode_failed_probes": 0,
            "raw_decode_success_consumed_chars": 0,
            "raw_decode_input_length_counts": {},
            "output_decoded_chars": 0,
        },
        "resolver": {"calls": 0, "input_bytes": 0},
        "normalizer": {"calls": 0, "input_chars": 0},
        "lock": {"writer_lock_seconds": 0.0, "contentions": 0},
        "cleanup": {"attempted": 0, "succeeded": 0, "remaining_bytes": 0},
    }


def _json_fixture(target: int, variant: str) -> tuple[bytes, bool]:
    prefix = '[{"id":"synthetic","mapping":{},"body":"'
    suffix = '"}]'
    expected_error = variant in {"syntax-error-end", "multi-element-final-error"}
    if variant == "metadata-dense":
        metadata = ",".join(f'"k{index:05d}":"v"' for index in range(20_000))
        prefix = (
            '[{"id":"synthetic","mapping":{},"metadata":{'
            + metadata
            + '},"body":"'
        )
        payload = prefix + "x" * max(1, target - len(prefix) - len(suffix)) + suffix
    elif variant in {"escape-heavy", "unicode-escape-split"}:
        unit = "\\u4e2d" if variant == "unicode-escape-split" else "\\\\n"
        repetitions = max(1, (target - len(prefix) - len(suffix)) // len(unit))
        payload = prefix + unit * repetitions + suffix
    elif variant == "utf8-split":
        available = max(4, target - len(prefix) - len(suffix))
        payload = prefix + "🙂" * (available // 4) + suffix
    elif variant == "multi-element-final-error":
        invalid_suffix = '",}]'
        second_prefix = '[{"id":"first"},{"id":"synthetic","mapping":{},"body":"'
        payload = (
            second_prefix
            + "x" * max(1, target - len(second_prefix) - len(invalid_suffix))
            + invalid_suffix
        )
    else:
        payload = prefix + "x" * max(1, target - len(prefix) - len(suffix)) + suffix
        if variant == "syntax-error-end":
            payload = payload[:-3] + '",}]'
    return payload.encode("utf-8"), expected_error


def _json_worker(size_mib: int, chunk_kib: int, variant: str) -> dict[str, Any]:
    target = max(1, size_mib) * 1024 * 1024
    payload_bytes, expected_error = _json_fixture(target, variant)
    metrics = _base_metrics("json-framing", hashlib.sha256(payload_bytes).hexdigest())
    chunk_chars = max(1, chunk_kib) * 1024
    if variant == "utf8-split":
        chunks = _iter_utf8_chunks(io.BytesIO(payload_bytes))
        source_chunks = (len(payload_bytes) + 64 * 1024 - 1) // (64 * 1024)
    else:
        payload = payload_bytes.decode("utf-8")
        chunks = (
            payload[offset : offset + chunk_chars]
            for offset in range(0, len(payload), chunk_chars)
        )
        source_chunks = (len(payload) + chunk_chars - 1) // chunk_chars

    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    values: list[Any] = []
    observed_error: str | None = None
    decoder_metrics: dict[str, Any] = {
        "decode_calls": 0,
        "decode_input_chars": 0,
        "decode_input_bytes": 0,
        "decode_failed_calls": 0,
        "raw_decode_calls": 0,
        "raw_decode_available_chars": 0,
        "raw_decode_available_bytes": 0,
        "raw_decode_failed_probes": 0,
        "raw_decode_success_consumed_chars": 0,
        "raw_decode_input_length_counts": {},
    }
    original_decoder = json.JSONDecoder

    class CountingDecoder(original_decoder):
        _benchmark_inside_decode = False

        def decode(self, value: str, *args: Any, **kwargs: Any) -> Any:
            decoder_metrics["decode_calls"] += 1
            decoder_metrics["decode_input_chars"] += len(value)
            decoder_metrics["decode_input_bytes"] += len(value.encode("utf-8"))
            self._benchmark_inside_decode = True
            try:
                return super().decode(value, *args, **kwargs)
            except BaseException:
                decoder_metrics["decode_failed_calls"] += 1
                raise
            finally:
                self._benchmark_inside_decode = False

        def raw_decode(
            self, value: str, idx: int = 0, *args: Any, **kwargs: Any
        ) -> Any:
            available = value[idx:]
            available_chars = len(available)
            available_bytes = len(available.encode("utf-8"))
            decoder_metrics["raw_decode_calls"] += 1
            decoder_metrics["raw_decode_available_chars"] += available_chars
            decoder_metrics["raw_decode_available_bytes"] += available_bytes
            counts = decoder_metrics["raw_decode_input_length_counts"]
            length_key = str(available_chars)
            counts[length_key] = int(counts.get(length_key, 0)) + 1
            try:
                result = super().raw_decode(value, idx, *args, **kwargs)
            except BaseException:
                decoder_metrics["raw_decode_failed_probes"] += 1
                raise
            decoder_metrics["raw_decode_success_consumed_chars"] += int(result[1]) - idx
            # The hybrid parser calls raw_decode() directly once a bounded
            # boundary/resource proof succeeds.  Count that as the same
            # logical element decode represented by decode_calls, while
            # avoiding a double count when JSONDecoder.decode() delegates to
            # this method internally.
            if not self._benchmark_inside_decode:
                consumed = value[idx : int(result[1])]
                decoder_metrics["decode_calls"] += 1
                decoder_metrics["decode_input_chars"] += len(consumed)
                decoder_metrics["decode_input_bytes"] += len(
                    consumed.encode("utf-8")
                )
            return result

    json.JSONDecoder = CountingDecoder
    try:
        for value in _iter_json_array(chunks):
            values.append(value)
    except (json.JSONDecodeError, ValueError) as exc:
        observed_error = type(exc).__name__
        if not expected_error:
            raise
    finally:
        json.JSONDecoder = original_decoder
    if expected_error and observed_error is None:
        raise RuntimeError("invalid JSON benchmark fixture was unexpectedly accepted")
    metrics["wall_seconds"] = time.perf_counter() - started_wall
    metrics["cpu_seconds"] = time.process_time() - started_cpu
    metrics["peak_rss_bytes"] = _peak_rss_bytes()
    metrics["decoder"].update(
        {
            "source_chunks": source_chunks,
            "source_utf8_bytes": len(payload_bytes),
            "elements": len(values),
            **decoder_metrics,
            "output_decoded_chars": sum(
                int(getattr(value, "decoded_chars", 0)) for value in values
            ),
            "variant": variant,
            "expected_error": expected_error,
            "observed_error": observed_error,
        }
    )
    return metrics


def _decoder_counter_negative_self_test() -> dict[str, Any]:
    """Prove that repeated growing raw-decode probes cannot look linear."""

    decoder = json.JSONDecoder()
    payload = '{"synthetic":"' + ("x" * 4096) + '"}'
    failed = 0
    available_chars = 0
    for stop in (64, 128, 256, 512, 1024, 2048, 4096):
        probe = payload[:stop]
        available_chars += len(probe)
        try:
            decoder.raw_decode(probe)
        except json.JSONDecodeError:
            failed += 1
    if failed <= 1 or available_chars <= len(payload):
        raise RuntimeError("decoder_counter_negative_self_test_failed")
    return {
        "repeated_failed_probes": failed,
        "repeated_available_chars": available_chars,
        "source_chars": len(payload),
        "detected": True,
    }


def _prepare_v4_database(path: Path, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute('DROP TRIGGER "archive_display_revision_node_insert"')
    conn.execute('DROP TRIGGER "archive_display_revision_node_update"')
    conn.execute("ALTER TABLE conversation_nodes DROP COLUMN display_revision")
    conn.execute(_KNOWN_MANAGED_TRIGGER_PREDECESSORS["archive_display_revision_node_insert"][0])
    conn.execute(_KNOWN_MANAGED_TRIGGER_PREDECESSORS["archive_display_revision_node_update"][0])
    conn.execute("PRAGMA user_version = 4")
    conn.execute(
        "INSERT INTO conversations(conversation_id,title,current_node,aggregate_hash) "
        "VALUES ('synthetic','synthetic',?,'synthetic')",
        (f"n{max(0, rows - 1)}",),
    )
    batch = 10_000
    for start in range(0, rows, batch):
        stop = min(rows, start + batch)
        conn.executemany(
            "INSERT INTO conversation_nodes("
            "conversation_id,node_id,role,content_type,content_text,content_hash,"
            "is_on_current_path) VALUES ('synthetic',?,'assistant','text','x',?,1)",
            ((f"n{index}", f"h{index}") for index in range(start, stop)),
        )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def _migration_worker(rows: int, journal_mode: str) -> dict[str, Any]:
    spec = json.dumps(
        {"scenario": "migration-v4-v5", "rows": rows, "journal_mode": journal_mode},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    metrics = _base_metrics("migration-v4-v5", hashlib.sha256(spec).hexdigest())
    temporary = tempfile.TemporaryDirectory(prefix="chatgpt-archive-resource-bench-")
    root = Path(temporary.name)
    database = root / "synthetic.db"
    _prepare_v4_database(database, max(1, rows))
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA journal_mode = {journal_mode}")
    sql_statements = 0
    vm_callbacks = 0
    wal_peak = 0
    temp_peak = _tree_bytes(root)
    lock_started: float | None = None

    def trace(statement: str) -> None:
        nonlocal sql_statements, lock_started
        sql_statements += 1
        if lock_started is None and statement.lstrip().upper().startswith("BEGIN IMMEDIATE"):
            lock_started = time.perf_counter()

    def progress() -> int:
        nonlocal vm_callbacks
        vm_callbacks += 1
        return 0

    def migration_progress(_stage: str, _progress: dict[str, int]) -> None:
        nonlocal wal_peak, temp_peak
        wal = Path(str(database) + "-wal")
        if wal.exists():
            wal_peak = max(wal_peak, wal.stat().st_size)
        temp_peak = max(temp_peak, _tree_bytes(root))

    conn.set_trace_callback(trace)
    conn.set_progress_handler(progress, 1_000)
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    migrate_database(conn, progress_callback=migration_progress)
    metrics["wall_seconds"] = time.perf_counter() - started_wall
    metrics["cpu_seconds"] = time.process_time() - started_cpu
    metrics["lock"]["writer_lock_seconds"] = (
        time.perf_counter() - lock_started if lock_started is not None else 0.0
    )
    wal = Path(str(database) + "-wal")
    if wal.exists():
        wal_peak = max(wal_peak, wal.stat().st_size)
    temp_peak = max(temp_peak, _tree_bytes(root))
    conn.set_trace_callback(None)
    conn.set_progress_handler(None, 0)
    conn.close()
    if wal.exists():
        wal_peak = max(wal_peak, wal.stat().st_size)
    metrics["peak_rss_bytes"] = _peak_rss_bytes()
    metrics["storage"].update(
        {
            "db_bytes": database.stat().st_size,
            "wal_peak_bytes": wal_peak,
            "temp_peak_bytes": max(temp_peak, _tree_bytes(root)),
        }
    )
    metrics["sqlite"].update(
        {"sql_statements": sql_statements, "vm_steps_approx": vm_callbacks * 1_000}
    )
    metrics["cleanup"]["attempted"] = 1
    temporary.cleanup()
    metrics["cleanup"]["succeeded"] = int(not root.exists())
    metrics["cleanup"]["remaining_bytes"] = _tree_bytes(root) if root.exists() else 0
    return metrics


def _display_insert_case(
    root: Path,
    *,
    name: str,
    rows: int,
    journal_mode: str,
    direct_revision: bool,
) -> dict[str, Any]:
    database = root / f"{name}.db"
    conn = sqlite3.connect(database)
    conn.execute(f"PRAGMA journal_mode = {journal_mode}")
    init_db(conn)
    conn.execute(
        "INSERT INTO conversations(conversation_id,title,current_node,aggregate_hash) "
        "VALUES ('synthetic','synthetic',?,'synthetic')",
        (f"n{max(0, rows - 1)}",),
    )
    conn.commit()
    vm_callbacks = 0
    sql_statements = 0
    wal_peak = 0
    storage_peak = _tree_bytes(root)

    def progress() -> int:
        nonlocal vm_callbacks
        vm_callbacks += 1
        return 0

    def trace(_statement: str) -> None:
        nonlocal sql_statements
        sql_statements += 1

    conn.set_progress_handler(progress, 1_000)
    conn.set_trace_callback(trace)
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    conn.execute("BEGIN IMMEDIATE")
    batch = 10_000
    for start in range(0, rows, batch):
        stop = min(rows, start + batch)
        if direct_revision:
            conn.executemany(
                "INSERT INTO conversation_nodes("
                "conversation_id,node_id,role,content_type,content_text,"
                "content_hash,is_on_current_path,display_revision"
                ") VALUES ('synthetic',?,'assistant','text','x',?,1,?)",
                (
                    (f"n{index}", f"h{index}", f"{index + 1:032x}")
                    for index in range(start, stop)
                ),
            )
        else:
            conn.executemany(
                "INSERT INTO conversation_nodes("
                "conversation_id,node_id,role,content_type,content_text,"
                "content_hash,is_on_current_path"
                ") VALUES ('synthetic',?,'assistant','text','x',?,1)",
                ((f"n{index}", f"h{index}") for index in range(start, stop)),
            )
        wal = Path(str(database) + "-wal")
        if wal.exists():
            wal_peak = max(wal_peak, wal.stat().st_size)
        storage_peak = max(storage_peak, _tree_bytes(root))
    conn.commit()
    wall_seconds = time.perf_counter() - started_wall
    cpu_seconds = time.process_time() - started_cpu
    wal = Path(str(database) + "-wal")
    if wal.exists():
        wal_peak = max(wal_peak, wal.stat().st_size)
    storage_peak = max(storage_peak, _tree_bytes(root))
    invalid_revisions = conn.execute(
        "SELECT COUNT(*) FROM conversation_nodes "
        "WHERE display_revision IS NULL OR length(display_revision) <> 32"
    ).fetchone()[0]
    inserted = conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0]
    conn.set_progress_handler(None, 0)
    conn.set_trace_callback(None)
    conn.close()
    if inserted != rows or invalid_revisions:
        raise RuntimeError("display revision insert benchmark invariant failed")
    return {
        "direct_revision": direct_revision,
        "rows": rows,
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "sql_statements": sql_statements,
        "vm_steps_approx": vm_callbacks * 1_000,
        "db_bytes": database.stat().st_size,
        "wal_peak_bytes": wal_peak,
        "storage_peak_bytes": storage_peak,
    }


def _display_insert_worker(rows: int, journal_mode: str) -> dict[str, Any]:
    rows = max(1, rows)
    spec = json.dumps(
        {
            "scenario": "display-revision-insert",
            "rows": rows,
            "journal_mode": journal_mode,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    metrics = _base_metrics(
        "display-revision-insert", hashlib.sha256(spec).hexdigest()
    )
    temporary = tempfile.TemporaryDirectory(
        prefix="chatgpt-archive-display-bench-"
    )
    root = Path(temporary.name)
    direct = _display_insert_case(
        root,
        name="direct",
        rows=rows,
        journal_mode=journal_mode,
        direct_revision=True,
    )
    trigger_generated = _display_insert_case(
        root,
        name="trigger-generated",
        rows=rows,
        journal_mode=journal_mode,
        direct_revision=False,
    )
    metrics["wall_seconds"] = direct["wall_seconds"]
    metrics["cpu_seconds"] = direct["cpu_seconds"]
    metrics["peak_rss_bytes"] = _peak_rss_bytes()
    metrics["storage"].update(
        {
            "db_bytes": direct["db_bytes"],
            "wal_peak_bytes": direct["wal_peak_bytes"],
            "temp_peak_bytes": max(
                direct["storage_peak_bytes"],
                trigger_generated["storage_peak_bytes"],
            ),
        }
    )
    metrics["sqlite"].update(
        {
            "sql_statements": direct["sql_statements"],
            "vm_steps_approx": direct["vm_steps_approx"],
        }
    )
    metrics["comparison"] = {
        "direct": direct,
        "trigger_generated": trigger_generated,
        "direct_to_trigger_wall_ratio": (
            direct["wall_seconds"] / trigger_generated["wall_seconds"]
            if trigger_generated["wall_seconds"]
            else None
        ),
        "direct_to_trigger_vm_ratio": (
            direct["vm_steps_approx"] / trigger_generated["vm_steps_approx"]
            if trigger_generated["vm_steps_approx"]
            else None
        ),
        "direct_to_trigger_wal_ratio": (
            direct["wal_peak_bytes"] / trigger_generated["wal_peak_bytes"]
            if trigger_generated["wal_peak_bytes"]
            else None
        ),
    }
    metrics["cleanup"]["attempted"] = 1
    temporary.cleanup()
    metrics["cleanup"]["succeeded"] = int(not root.exists())
    metrics["cleanup"]["remaining_bytes"] = _tree_bytes(root) if root.exists() else 0
    return metrics


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    def values(path: tuple[str, ...]) -> list[float]:
        result: list[float] = []
        for sample in samples:
            value: Any = sample
            for key in path:
                value = value[key]
            result.append(float(value))
        return result

    fields = {
        "wall_seconds": ("wall_seconds",),
        "cpu_seconds": ("cpu_seconds",),
        "peak_rss_bytes": ("peak_rss_bytes",),
        "db_bytes": ("storage", "db_bytes"),
        "wal_peak_bytes": ("storage", "wal_peak_bytes"),
        "temp_peak_bytes": ("storage", "temp_peak_bytes"),
        "vm_steps_approx": ("sqlite", "vm_steps_approx"),
    }
    return {
        name: {
            "median": statistics.median(values(path)),
            "worst": max(values(path)),
        }
        for name, path in fields.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=("json-framing", "migration-v4-v5", "display-revision-insert"),
        default="json-framing",
    )
    parser.add_argument("--size-mib", type=int, default=8)
    parser.add_argument("--chunk-kib", type=int, default=64)
    parser.add_argument(
        "--variant",
        choices=(
            "simple",
            "metadata-dense",
            "escape-heavy",
            "utf8-split",
            "unicode-escape-split",
            "late-closing-brace",
            "syntax-error-end",
            "multi-element-final-error",
        ),
        default="simple",
    )
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--journal-mode", choices=("WAL", "DELETE"), default="WAL")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--counter-self-test", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.counter_self_test:
        print(json.dumps(
            _decoder_counter_negative_self_test(),
            sort_keys=True,
            separators=(",", ":"),
        ))
        return 0

    if args.worker:
        if args.scenario == "json-framing":
            result = _json_worker(args.size_mib, args.chunk_kib, args.variant)
        elif args.scenario == "migration-v4-v5":
            result = _migration_worker(max(1, args.rows), args.journal_mode)
        else:
            result = _display_insert_worker(max(1, args.rows), args.journal_mode)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0

    samples: list[dict[str, Any]] = []
    for _index in range(max(1, args.runs)):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--scenario",
            args.scenario,
            "--size-mib",
            str(args.size_mib),
            "--chunk-kib",
            str(args.chunk_kib),
            "--variant",
            args.variant,
            "--rows",
            str(args.rows),
            "--journal-mode",
            args.journal_mode,
            "--worker",
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        samples.append(json.loads(completed.stdout))
    output = {
        "schema": "chatgpt-sqlite-webui-resource-benchmark-v3",
        "scenario": args.scenario,
        "runs": len(samples),
        "aggregate": _aggregate(samples),
        "samples": samples,
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
