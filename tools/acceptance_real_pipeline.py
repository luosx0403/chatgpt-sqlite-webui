from __future__ import annotations

"""Opt-in, content-safe localhost production acceptance for a real export ZIP.

The harness never prints archive content or logical identifiers. It starts the
shipped CLI Web entry point, streams multipart from the read-only source file,
waits for the complete import/verify/stats/Web-index job, runs bounded API
smoke checks, and removes only its fresh temporary workspace.
"""

import argparse
import hashlib
import http.client
import json
import os
import platform
import resource
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatgpt_export_archiver.scanner import (  # noqa: E402
    list_source_entries,
    resolve_input,
    select_conversation_sources,
)

READ_CHUNK = 1024 * 1024
TERMINAL_JOB_STATES = {"succeeded", "failed", "postcheck_failed"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    info = path.lstat()
    return {
        "absolute_path": str(path.resolve()),
        "file_type": "regular" if path.is_file() and not path.is_symlink() else "other",
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
        "mode": int(info.st_mode & 0o7777),
        "nlink": int(info.st_nlink),
        "sha256": _sha256(path),
    }


def _identity_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before == after


def _public_identity(value: dict[str, Any]) -> dict[str, Any]:
    """Retain the required identity evidence without exposing a user path."""

    return {
        key: item
        for key, item in value.items()
        if key != "absolute_path"
    } | {
        "absolute_path_sha256": hashlib.sha256(
            str(value["absolute_path"]).encode("utf-8")
        ).hexdigest()
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    port: int,
    method: str,
    path: str,
    *,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            method,
            path,
            headers={
                "Accept": "application/json",
                "Host": f"127.0.0.1:{port}",
            },
        )
        response = connection.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    finally:
        connection.close()


def _stream_upload(port: int, source: Path) -> tuple[dict[str, Any], float]:
    boundary = "chatgpt-archive-acceptance-boundary"
    preamble = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="archive.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    total = len(preamble) + source.stat().st_size + len(suffix)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3600)
    started = time.perf_counter()
    try:
        connection.putrequest("POST", "/api/import/upload", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{port}")
        connection.putheader("Origin", f"http://127.0.0.1:{port}")
        connection.putheader("Sec-Fetch-Site", "same-origin")
        connection.putheader("Accept", "application/json")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(total))
        connection.endheaders()
        connection.send(preamble)
        with source.open("rb") as stream:
            while chunk := stream.read(READ_CHUNK):
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        payload = response.read()
        if response.status not in {200, 201, 202}:
            try:
                error_payload = json.loads(payload)
                detail = error_payload.get("detail") if isinstance(error_payload, dict) else None
                code = (
                    detail
                    if isinstance(detail, str)
                    else error_payload.get("code")
                    if isinstance(error_payload, dict)
                    else None
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                code = None
            safe_code = (
                code
                if isinstance(code, str)
                and code.replace("_", "").isalnum()
                else f"upload_http_{response.status}"
            )
            raise RuntimeError(safe_code)
        value = json.loads(payload)
        if not isinstance(value, dict) or not isinstance(value.get("job_id"), str):
            raise RuntimeError("upload_job_response_invalid")
        return value, time.perf_counter() - started
    finally:
        connection.close()


def _process_sample(pid: int) -> tuple[int, float]:
    try:
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-o", "%cpu=", "-p", str(pid)],
            text=True,
            capture_output=True,
            check=True,
        )
        fields = completed.stdout.strip().split()
        return int(float(fields[0])) * 1024, float(fields[1])
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return 0, 0.0


def _tree_metrics(root: Path, database: Path) -> dict[str, int]:
    total = 0
    spool = 0
    temp = 0
    for path in root.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        name = path.name
        if "upload" in str(path.parent) or name == "upload.zip":
            spool += size
        if name != database.name and not name.startswith(database.name):
            temp += size
    def size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0
    return {
        "workspace_bytes": total,
        "spool_bytes": spool,
        "db_bytes": size(database),
        "wal_bytes": size(Path(str(database) + "-wal")),
        "journal_bytes": size(Path(str(database) + "-journal")),
        "temp_bytes": temp,
    }


def _wait_server(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("production_server_exited")
        try:
            status, _payload = _request_json(port, "GET", "/api/health", timeout=2)
            if status in {200, 409, 503}:
                return
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise RuntimeError("production_server_start_timeout")


def _bounded_smoke(port: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, path in (
        ("health", "/api/health"),
        ("stats", "/api/stats"),
        ("schema", "/api/schema"),
        ("suggest", "/api/search/suggest?q=&limit=1"),
    ):
        status, payload = _request_json(port, "GET", path)
        results[name] = {"status": status, "json_object": isinstance(payload, dict)}
        if status != 200:
            raise RuntimeError(f"{name}_smoke_failed")
    status, page = _request_json(port, "GET", "/api/conversations?limit=1&offset=0")
    if status != 200 or not isinstance(page, dict):
        raise RuntimeError("list_smoke_failed")
    items = page.get("items")
    results["list"] = {
        "status": status,
        "returned": len(items) if isinstance(items, list) else 0,
        "total_nonnegative": isinstance(page.get("total"), int) and page["total"] >= 0,
    }
    if isinstance(items, list) and items:
        conversation_id = items[0].get("conversation_id")
        if not isinstance(conversation_id, str):
            raise RuntimeError("list_identifier_missing")
        encoded = urlencode({"conversation_id": conversation_id})
        for name, path in (
            ("detail", f"/api/by-id/conversation?{encoded}"),
            ("messages", f"/api/by-id/messages?{encoded}&limit=1&offset=0"),
            (
                "message_search",
                f"/api/search/messages?{encoded}&q=&limit=1&offset=0&count_total=false",
            ),
        ):
            item_status, payload = _request_json(port, "GET", path)
            results[name] = {
                "status": item_status,
                "json_object": isinstance(payload, dict),
            }
            if item_status != 200:
                raise RuntimeError(f"{name}_smoke_failed")
    return results


def _run_once(
    source: Path,
    python: Path,
    run_number: int,
    *,
    max_job_seconds: float,
    force_cleanup_failure: bool = False,
) -> dict[str, Any]:
    workspace = Path(tempfile.mkdtemp(prefix="chatgpt-real-pipeline-"))
    database = workspace / "fresh.db"
    temp_root = workspace / "tmp"
    temp_root.mkdir()
    log_path = workspace / "server.log"
    process: subprocess.Popen[bytes] | None = None
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    metrics: dict[str, Any] = {
        "run": run_number,
        "success": False,
        "pipeline_success": False,
        "cleanup_success": False,
        "correctness_pass": False,
        "performance_pass": False,
        "cleanup": {"attempted": True, "complete": False},
    }
    try:
        inspect_before = _identity(source)
        inspect_started = time.perf_counter()
        inspected = subprocess.run(
            [
                str(python),
                str(ROOT / "chatgpt_archive.py"),
                "--log-level",
                "none",
                "inspect",
                "--input",
                str(source),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "TMPDIR": str(temp_root)},
            check=False,
        )
        metrics["inspect_seconds"] = time.perf_counter() - inspect_started
        metrics["inspect_exit_code"] = inspected.returncode
        if inspected.returncode != 0:
            raise RuntimeError("production_inspect_failed")
        inspect_after = _identity(source)
        if not _identity_equal(inspect_before, inspect_after):
            raise RuntimeError("input_identity_changed_after_inspect")
        metrics["inspect_input_identity"] = {
            "before": _public_identity(inspect_before),
            "after": _public_identity(inspect_after),
            "unchanged": True,
        }

        preflight_before = _identity(source)
        source_scan_started = time.perf_counter()
        resolved = resolve_input(str(source), ROOT)
        entries = list_source_entries(resolved)
        selected = select_conversation_sources(entries)
        with source.open("rb") as source_stream:
            with zipfile.ZipFile(source_stream) as archive:
                central_directory_members = len(archive.infolist())
        metrics["zip_preflight_seconds"] = time.perf_counter() - source_scan_started
        preflight_after = _identity(source)
        if not _identity_equal(preflight_before, preflight_after):
            raise RuntimeError("input_identity_changed_after_preflight")
        metrics["preflight_input_identity"] = {
            "before": _public_identity(preflight_before),
            "after": _public_identity(preflight_after),
            "unchanged": True,
        }
        metrics["archive"] = {
            "compressed_bytes": source.stat().st_size,
            "central_directory_members": central_directory_members,
            "discovered_non_metadata_files": len(entries),
            "selected_json_members": len(selected),
            "selected_logical_json_bytes": sum(entry.size for entry in selected),
        }

        upload_before = _identity(source)
        port = _free_port()
        env = {
            **os.environ,
            "TMPDIR": str(temp_root),
            "PYTHONUNBUFFERED": "1",
        }
        log_stream = log_path.open("wb")
        try:
            process = subprocess.Popen(
                [
                    str(python),
                    str(ROOT / "chatgpt_archive.py"),
                    "--db",
                    str(database),
                    "--log-level",
                    "none",
                    "web",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
            )
        finally:
            log_stream.close()
        _wait_server(port, process)
        total_started = time.perf_counter()
        job, upload_seconds = _stream_upload(port, source)
        metrics["upload_seconds"] = upload_seconds
        job_id = job["job_id"]
        peaks = _tree_metrics(workspace, database)
        peak_rss = 0
        peak_cpu_percent = 0.0
        deadline = time.monotonic() + 24 * 60 * 60
        while time.monotonic() < deadline:
            status, snapshot = _request_json(
                port, "GET", f"/api/import/jobs/{job_id}", timeout=30
            )
            if status != 200 or not isinstance(snapshot, dict):
                raise RuntimeError("job_poll_failed")
            rss, cpu_percent = _process_sample(process.pid)
            peak_rss = max(peak_rss, rss)
            peak_cpu_percent = max(peak_cpu_percent, cpu_percent)
            current = _tree_metrics(workspace, database)
            peaks = {key: max(peaks.get(key, 0), value) for key, value in current.items()}
            if snapshot.get("status") in TERMINAL_JOB_STATES:
                job = snapshot
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("job_timeout")
        if job.get("status") != "succeeded":
            raise RuntimeError(str(job.get("error_code") or "production_job_failed"))
        upload_after = _identity(source)
        if not _identity_equal(upload_before, upload_after):
            raise RuntimeError("input_identity_changed_after_upload")
        metrics["input_identity"] = {
            "before": _public_identity(upload_before),
            "after": _public_identity(upload_after),
            "unchanged": True,
        }
        metrics["job"] = {
            "status": job.get("status"),
            "completion_outcome": job.get("completion_outcome"),
            "canonical_import_outcome": job.get("canonical_import_outcome"),
            "elapsed_seconds": job.get("elapsed_seconds"),
            "stage_timings": job.get("stage_timings"),
            "committed_conversations": (job.get("summary") or {}).get(
                "committed_conversations"
            ),
            "committed_nodes": (job.get("summary") or {}).get("committed_nodes"),
            "warnings": (job.get("summary") or {}).get("warnings"),
            "web_index_status": (job.get("web_index") or {}).get("status", "built"),
        }
        metrics["smoke"] = _bounded_smoke(port)
        metrics["wall_seconds"] = time.perf_counter() - total_started
        metrics["peak_rss_bytes"] = peak_rss
        metrics["peak_cpu_percent_sample"] = peak_cpu_percent
        metrics["storage_peaks"] = peaks
        metrics["pipeline_success"] = True
        return metrics
    except BaseException as exc:
        metrics["error_code"] = (
            str(exc)
            if isinstance(exc, RuntimeError)
            and str(exc).replace("_", "").isalnum()
            else type(exc).__name__
        )
        return metrics
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        metrics["child_cpu_seconds"] = max(
            0.0,
            (after_children.ru_utime + after_children.ru_stime)
            - (before_children.ru_utime + before_children.ru_stime),
        )
        cleanup_error: str | None = None
        try:
            if force_cleanup_failure:
                raise PermissionError("synthetic cleanup fault")
            shutil.rmtree(workspace)
        except OSError as exc:
            cleanup_error = type(exc).__name__
        metrics["cleanup"]["complete"] = (
            cleanup_error is None and not workspace.exists()
        )
        metrics["cleanup_success"] = bool(metrics["cleanup"]["complete"])
        if cleanup_error is not None:
            metrics["cleanup"]["error_type"] = cleanup_error
        metrics["correctness_pass"] = bool(
            metrics.get("pipeline_success") and metrics["cleanup_success"]
        )
        elapsed = (metrics.get("job") or {}).get("elapsed_seconds")
        metrics["performance_pass"] = bool(
            metrics.get("pipeline_success")
            and isinstance(elapsed, (int, float))
            and float(elapsed) <= max_job_seconds
        )
        metrics["success"] = bool(
            metrics["correctness_pass"] and metrics["performance_pass"]
        )
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [sample for sample in samples if sample.get("success")]
    fields = ("wall_seconds", "upload_seconds", "peak_rss_bytes", "child_cpu_seconds")
    result: dict[str, Any] = {"successful_runs": len(successful)}
    for field in fields:
        values = [float(sample[field]) for sample in successful if field in sample]
        result[field] = {
            "median": statistics.median(values) if values else None,
            "worst": max(values) if values else None,
            "all": values,
        }
    job_values = [
        float(sample["job"]["elapsed_seconds"])
        for sample in samples
        if isinstance((sample.get("job") or {}).get("elapsed_seconds"), (int, float))
    ]
    result["job_elapsed_seconds"] = {
        "median": statistics.median(job_values) if job_values else None,
        "worst": max(job_values) if job_values else None,
        "all": job_values,
    }
    result["performance_pass"] = all(
        bool(sample.get("performance_pass")) for sample in samples
    )
    result["correctness_pass"] = all(
        bool(sample.get("correctness_pass")) for sample in samples
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--max-job-seconds",
        type=float,
        default=300.0,
        help="Formal per-job performance threshold; any slower run fails acceptance.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
    )
    parser.add_argument(
        "--confirm-read-only-real-input",
        action="store_true",
        help="Required opt-in. The input is opened read-only and never staged or deleted.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run one tiny synthetic production pipeline; no real input is used.",
    )
    parser.add_argument(
        "--self-test-cleanup-failure",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    synthetic_root: Path | None = None
    if args.self_test or args.self_test_cleanup_failure:
        synthetic_root = Path(tempfile.mkdtemp(prefix="chatgpt-harness-selftest-"))
        source = synthetic_root / "synthetic.zip"
        payload = [
            {
                "id": "synthetic-conversation",
                "title": "Synthetic",
                "current_node": "node-1",
                "mapping": {
                    "node-1": {
                        "id": "node-1",
                        "parent": None,
                        "children": [],
                        "message": {
                            "id": "message-1",
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["synthetic"]},
                        },
                    }
                },
            }
        ]
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "conversations.json",
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )
        args.runs = 1
    elif args.input is None:
        parser.error("--input is required unless --self-test is used")
    elif not args.confirm_read_only_real_input:
        parser.error("--confirm-read-only-real-input is required")
    else:
        source = args.input.expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        parser.error("--input must be a regular ZIP file")
    if not isinstance(args.max_job_seconds, float) or args.max_job_seconds < 0:
        parser.error("--max-job-seconds must be nonnegative")
    try:
        samples = [
            _run_once(
                source,
                args.python.expanduser().resolve(),
                run_number,
                max_job_seconds=args.max_job_seconds,
                force_cleanup_failure=args.self_test_cleanup_failure,
            )
            for run_number in range(1, max(1, args.runs) + 1)
        ]
    finally:
        if synthetic_root is not None:
            shutil.rmtree(synthetic_root, ignore_errors=True)
    output = {
        "schema": "chatgpt-sqlite-webui-real-pipeline-v1",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "runs": len(samples),
        "aggregate": _aggregate(samples),
        "samples": samples,
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0 if all(sample.get("success") for sample in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
