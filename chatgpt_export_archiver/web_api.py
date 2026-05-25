from __future__ import annotations

import ipaddress
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Path as ApiPath, Query, Response, UploadFile

from .exporter import render_markdown, render_txt
from .logging_utils import get_logger
from .search import get_conversation, get_messages, list_conversations, normalize_search_text, parse_query, search_conversations, search_messages
from .utils import safe_filename_part
from .web_db import check_schema, connect_readonly, detect_fts5, detect_trigram, web_index_status
from .web_jobs import ImportJobManager, cleanup_upload_dir, make_upload_path

LOGGER = get_logger("web_api")

ALLOWED_SORTS = {"relevance", "newest", "oldest", "created", "updated", "title"}
ALLOWED_SCOPES = {"all", "title", "message"}
ALLOWED_ROLES = {"", "user", "assistant", "tool", "system", "developer", "tool/system", "tool_system"}
ALLOWED_PATHS = {"current", "all"}
ALLOWED_MESSAGE_ORDERS = {"relevance", "display"}
ALLOWED_MATCH_MODES = {"contains", "word"}
MAX_DATE_PARAM_LENGTH = 64
MAX_ID_PARAM_LENGTH = 512

DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024 * 1024
DEFAULT_MAX_UPLOAD_JSON_MEMBER_BYTES = 64 * 1024 * 1024 * 1024
DEFAULT_MAX_UPLOAD_JSON_MEMBERS = 5000
DEFAULT_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024 * 1024
DEFAULT_MAX_UPLOAD_COMPRESSION_RATIO = 1000.0
DEFAULT_MAX_UPLOAD_TOTAL_MEMBERS = 100000

MAX_UPLOAD_ENV = "CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES"
_MAX_UPLOAD_JSON_MEMBER_BYTES_ENV = "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES"
_MAX_UPLOAD_JSON_MEMBERS_ENV = "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS"
_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES_ENV = "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES"
_MAX_UPLOAD_COMPRESSION_RATIO_ENV = "CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO"
_MAX_UPLOAD_TOTAL_MEMBERS_ENV = "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS"
ALLOW_REMOTE_UPLOADS_ENV = "CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS"
REMOTE_UPLOAD_PROFILE_ENV = "CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE"

REMOTE_DEFAULT_MAX_UPLOAD_BYTES = 128 * 1024 * 1024
REMOTE_DEFAULT_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
REMOTE_DEFAULT_MEMBER_BYTES = 256 * 1024 * 1024
REMOTE_DEFAULT_COMPRESSION_RATIO = 200.0
REMOTE_DEFAULT_TOTAL_MEMBERS = 10000


@dataclass(frozen=True)
class UploadPolicy:
    max_upload_bytes: int
    max_json_member_bytes: int
    max_json_members: int
    max_total_uncompressed_bytes: int
    max_compression_ratio: float
    max_total_members: int
    remote: bool
    remote_profile: str = "local"


def _positive_int(value_str: str, default: int) -> int:
    try:
        value = int(value_str)
    except (TypeError, ValueError):
        LOGGER.warning("invalid_upload_config error_type=invalid_integer")
        return default
    return max(1, value)


def _positive_float_from_env(name: str, default: float, environ: Mapping[str, str] = os.environ) -> float:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        LOGGER.warning("invalid_upload_config env=%s", name)
        return default
    return max(1.0, value)


def _env_truthy(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().casefold() in {"true", "1", "yes"}


def _env_int_value(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    return _positive_int(raw.strip(), default)


def _remote_default_policy() -> UploadPolicy:
    return UploadPolicy(
        max_upload_bytes=REMOTE_DEFAULT_MAX_UPLOAD_BYTES,
        max_json_member_bytes=REMOTE_DEFAULT_MEMBER_BYTES,
        max_json_members=min(DEFAULT_MAX_UPLOAD_JSON_MEMBERS, 200),
        max_total_uncompressed_bytes=REMOTE_DEFAULT_TOTAL_UNCOMPRESSED,
        max_compression_ratio=REMOTE_DEFAULT_COMPRESSION_RATIO,
        max_total_members=REMOTE_DEFAULT_TOTAL_MEMBERS,
        remote=True,
        remote_profile="remote_safe",
    )


def _is_loopback(host: str) -> bool:
    if host in ("127.0.0.1", "localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        return False


def _resolve_web_url_host(host: str) -> str:
    try:
        addr = ipaddress.ip_address(host)
        if addr.version == 6:
            return f"[{host}]"
    except ValueError:
        pass
    return host


def _get_upload_policy(
    environ: Mapping[str, str] = os.environ,
    host: str = "127.0.0.1",
) -> UploadPolicy:
    remote = not _is_loopback(host)
    if not remote:
        return UploadPolicy(
            max_upload_bytes=_env_int_value(environ, MAX_UPLOAD_ENV, DEFAULT_MAX_UPLOAD_BYTES),
            max_json_member_bytes=_env_int_value(environ, _MAX_UPLOAD_JSON_MEMBER_BYTES_ENV, DEFAULT_MAX_UPLOAD_JSON_MEMBER_BYTES),
            max_json_members=_env_int_value(environ, _MAX_UPLOAD_JSON_MEMBERS_ENV, DEFAULT_MAX_UPLOAD_JSON_MEMBERS),
            max_total_uncompressed_bytes=_env_int_value(environ, _MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES_ENV, DEFAULT_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES),
            max_compression_ratio=_positive_float_from_env(_MAX_UPLOAD_COMPRESSION_RATIO_ENV, DEFAULT_MAX_UPLOAD_COMPRESSION_RATIO, environ=environ),
            max_total_members=_env_int_value(environ, _MAX_UPLOAD_TOTAL_MEMBERS_ENV, DEFAULT_MAX_UPLOAD_TOTAL_MEMBERS),
            remote=False,
            remote_profile="local",
        )
    remote_defaults = _remote_default_policy()
    if environ.get(REMOTE_UPLOAD_PROFILE_ENV, "").strip().casefold() == "local":
        return UploadPolicy(
            max_upload_bytes=_env_int_value(environ, MAX_UPLOAD_ENV, DEFAULT_MAX_UPLOAD_BYTES),
            max_json_member_bytes=_env_int_value(environ, _MAX_UPLOAD_JSON_MEMBER_BYTES_ENV, DEFAULT_MAX_UPLOAD_JSON_MEMBER_BYTES),
            max_json_members=_env_int_value(environ, _MAX_UPLOAD_JSON_MEMBERS_ENV, DEFAULT_MAX_UPLOAD_JSON_MEMBERS),
            max_total_uncompressed_bytes=_env_int_value(environ, _MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES_ENV, DEFAULT_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES),
            max_compression_ratio=_positive_float_from_env(_MAX_UPLOAD_COMPRESSION_RATIO_ENV, DEFAULT_MAX_UPLOAD_COMPRESSION_RATIO, environ=environ),
            max_total_members=_env_int_value(environ, _MAX_UPLOAD_TOTAL_MEMBERS_ENV, DEFAULT_MAX_UPLOAD_TOTAL_MEMBERS),
            remote=True,
            remote_profile="local_profile",
        )
    allow_remote_overrides = _env_truthy(environ, ALLOW_REMOTE_UPLOADS_ENV)
    configured = {
        "max_upload_bytes": _env_int_value(environ, MAX_UPLOAD_ENV, remote_defaults.max_upload_bytes),
        "max_json_member_bytes": _env_int_value(environ, _MAX_UPLOAD_JSON_MEMBER_BYTES_ENV, remote_defaults.max_json_member_bytes),
        "max_json_members": _env_int_value(environ, _MAX_UPLOAD_JSON_MEMBERS_ENV, remote_defaults.max_json_members),
        "max_total_uncompressed_bytes": _env_int_value(environ, _MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES_ENV, remote_defaults.max_total_uncompressed_bytes),
        "max_compression_ratio": _positive_float_from_env(_MAX_UPLOAD_COMPRESSION_RATIO_ENV, remote_defaults.max_compression_ratio, environ=environ),
        "max_total_members": _env_int_value(environ, _MAX_UPLOAD_TOTAL_MEMBERS_ENV, remote_defaults.max_total_members),
    }
    explicit_names = {
        "max_upload_bytes": MAX_UPLOAD_ENV,
        "max_json_member_bytes": _MAX_UPLOAD_JSON_MEMBER_BYTES_ENV,
        "max_json_members": _MAX_UPLOAD_JSON_MEMBERS_ENV,
        "max_total_uncompressed_bytes": _MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES_ENV,
        "max_compression_ratio": _MAX_UPLOAD_COMPRESSION_RATIO_ENV,
        "max_total_members": _MAX_UPLOAD_TOTAL_MEMBERS_ENV,
    }
    if not allow_remote_overrides:
        exceeded = any(configured[field] > getattr(remote_defaults, field) for field in configured if explicit_names[field] in environ)
        if exceeded:
            LOGGER.warning(
                "remote_upload_limits clamped; set %s=true to use explicit configured limits or %s=local for trusted local-profile limits",
                ALLOW_REMOTE_UPLOADS_ENV,
                REMOTE_UPLOAD_PROFILE_ENV,
            )
        return UploadPolicy(
            max_upload_bytes=min(configured["max_upload_bytes"], remote_defaults.max_upload_bytes),
            max_json_member_bytes=min(configured["max_json_member_bytes"], remote_defaults.max_json_member_bytes),
            max_json_members=min(configured["max_json_members"], remote_defaults.max_json_members),
            max_total_uncompressed_bytes=min(configured["max_total_uncompressed_bytes"], remote_defaults.max_total_uncompressed_bytes),
            max_compression_ratio=min(configured["max_compression_ratio"], remote_defaults.max_compression_ratio),
            max_total_members=min(configured["max_total_members"], remote_defaults.max_total_members),
            remote=True,
            remote_profile="remote_safe",
        )
    return UploadPolicy(
        max_upload_bytes=configured["max_upload_bytes"],
        max_json_member_bytes=configured["max_json_member_bytes"],
        max_json_members=configured["max_json_members"],
        max_total_uncompressed_bytes=configured["max_total_uncompressed_bytes"],
        max_compression_ratio=configured["max_compression_ratio"],
        max_total_members=configured["max_total_members"],
        remote=remote,
        remote_profile="explicit_remote_override",
    )


MAX_UPLOAD_BYTES = _get_upload_policy(host="127.0.0.1").max_upload_bytes

# Backward-compatible module-level constants (may be overridden by tests for monkeypatch)
# These are _minimum_ caps for test safety; the environment variables via UploadPolicy
# can raise limits above these values in the normal code path.
MAX_UPLOAD_JSON_MEMBER_BYTES = DEFAULT_MAX_UPLOAD_JSON_MEMBER_BYTES
MAX_UPLOAD_JSON_MEMBERS = DEFAULT_MAX_UPLOAD_JSON_MEMBERS
MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES = DEFAULT_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES
MAX_UPLOAD_COMPRESSION_RATIO = DEFAULT_MAX_UPLOAD_COMPRESSION_RATIO


def _effective_upload_policy(policy: UploadPolicy) -> UploadPolicy:
    return policy


def _upload_policy_schema(policy: UploadPolicy) -> dict[str, object]:
    return {
        "max_upload_bytes": policy.max_upload_bytes,
        "max_json_member_bytes": policy.max_json_member_bytes,
        "max_json_members": policy.max_json_members,
        "max_total_uncompressed_bytes": policy.max_total_uncompressed_bytes,
        "max_compression_ratio": policy.max_compression_ratio,
        "max_total_members": policy.max_total_members,
        "remote": policy.remote,
        "remote_profile": policy.remote_profile,
        "explicit_remote_override": policy.remote and policy.remote_profile == "explicit_remote_override",
        "local_profile": policy.remote_profile in {"local", "local_profile"},
    }


def create_api_router(db_path: Path, job_manager: ImportJobManager | None = None, upload_policy: UploadPolicy | None = None) -> APIRouter:
    router = APIRouter(prefix="/api")
    manager = job_manager or ImportJobManager(db_path)
    policy = _effective_upload_policy(upload_policy or _get_upload_policy())

    def get_conn():
        if not db_path.exists():
            raise HTTPException(status_code=409, detail="database is not ready")
        try:
            conn = connect_readonly(db_path)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="database is not ready") from exc
        try:
            schema = check_schema(conn)
            if not schema["ok"]:
                raise HTTPException(status_code=409, detail=_schema_error_detail(schema))
            yield conn
        finally:
            conn.close()

    def get_optional_conn():
        if not db_path.exists():
            yield None
            return
        try:
            conn = connect_readonly(db_path)
        except ValueError:
            yield None
            return
        try:
            schema = check_schema(conn)
            if not schema["ok"]:
                raise HTTPException(status_code=409, detail=_schema_error_detail(schema))
            yield conn
        finally:
            conn.close()

    @router.get("/health")
    def health():
        if not db_path.exists():
            return {
                "ok": True,
                "db_ready": False,
                "database": {"name": "database", "exists": False},
                "schema_version": 1,
                "fts5_available": False,
                "message_fts_available": False,
                "trigram_available": False,
                "web_trigram_indexed": False,
                "web_normalized_indexed": False,
                "web_normalized_trigram_indexed": False,
                "web_legacy_trigram_indexed": False,
                "schema_compatible": False,
            }
        try:
            conn = connect_readonly(db_path)
        except ValueError:
            return {"ok": True, "db_ready": False, "database": {"name": "database", "exists": db_path.exists()}, "schema_version": 1}
        try:
            schema = check_schema(conn)
            web_status = web_index_status(conn)
            fts5 = detect_fts5(conn)
            trigram = detect_trigram(conn)
        finally:
            conn.close()
        return {
            "ok": schema["ok"],
            "db_ready": schema["ok"],
            "schema_compatible": schema["schema_compatible"],
            "missing_tables": schema["missing_tables"],
            "missing_columns": schema["missing_columns"],
            "database": {"name": "database", "exists": db_path.exists()},
            "schema_version": 1,
            "fts5_available": fts5,
            "message_fts_available": schema["message_fts"],
            "trigram_available": trigram,
            **web_status,
        }

    @router.get("/stats")
    def stats():
        if not db_path.exists():
            return _empty_stats(db_ready=False)
        try:
            conn = connect_readonly(db_path)
        except ValueError:
            return _empty_stats(db_ready=False)
        try:
            schema = check_schema(conn)
            if not schema["ok"]:
                raise HTTPException(status_code=409, detail=_schema_error_detail(schema))
            row = conn.execute(
                """
                SELECT COUNT(*) AS conversations,
                       MIN(create_time) AS earliest_create_time,
                       MAX(create_time) AS latest_create_time,
                       MIN(update_time) AS earliest_update_time,
                       MAX(update_time) AS latest_update_time
                FROM conversations
                """
            ).fetchone()
            nodes = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN is_on_current_path = 1 THEN 1 ELSE 0 END) AS current_path FROM conversation_nodes"
            ).fetchone()
            warnings = conn.execute("SELECT COUNT(*) AS c FROM import_warnings").fetchone()["c"]
        finally:
            conn.close()
        return {
            "db_ready": True,
            "conversations": row["conversations"],
            "nodes": nodes["total"],
            "current_path_nodes": nodes["current_path"] or 0,
            "warnings": warnings,
            "earliest_create_time": row["earliest_create_time"],
            "latest_create_time": row["latest_create_time"],
            "earliest_update_time": row["earliest_update_time"],
            "latest_update_time": row["latest_update_time"],
        }

    @router.get("/schema")
    def schema_docs():
        return {
            "pagination": {"fields": ["items", "total", "limit", "offset", "has_more", "next_offset"]},
            "conversations": {
                "filters": ["q", "sort", "after", "before", "role", "title", "scope", "exact", "exclude", "source", "path", "match_mode"],
                "limits": {"q": 500, "title": 200, "exact": 300, "exclude": 200, "source": 200, "after": MAX_DATE_PARAM_LENGTH, "before": MAX_DATE_PARAM_LENGTH, "selected_id": MAX_ID_PARAM_LENGTH},
                "path": ["current", "all"],
                "match_mode": ["contains", "word"],
                "date": "after/before use UTC calendar days as YYYY-MM-DD; before is exclusive (next-day 00:00:00 UTC)",
                "response": ["total", "items", "has_more", "next_offset", "selected_in_results", "selected_item", "node_count", "current_path_nodes", "current_path_fallback_to_all"],
                "diagnostics": "best-effort search diagnostics; see search.diagnostics",
            },
            "messages": {
                "path": ["current", "all"],
                "include_internal": "boolean; default false for reader pages so pagination is over visible messages, true includes root/internal/technical nodes",
                "filters": ["q", "after", "before", "role", "title", "scope", "exact", "exclude", "source", "match_mode"],
                "limits": {"conversation_id": MAX_ID_PARAM_LENGTH, "around_node_id": MAX_ID_PARAM_LENGTH, "q": 500, "title": 200, "exact": 300, "exclude": 200, "source": 200, "after": MAX_DATE_PARAM_LENGTH, "before": MAX_DATE_PARAM_LENGTH},
                "raw": "message pages return raw_preview only; capped raw preview is available per message endpoint",
                "hidden_counts": ["visible_total", "empty_hidden_count", "internal_hidden_count", "technical_hidden_count"],
                "path_metadata": ["effective_path", "current_path_fallback_to_all", "effective_visible_in_current_view"],
                "highlight": "highlight_ranges use UTF-16 code-unit offsets for JS text.slice()",
                "match_mode": ["contains", "word"],
                "around_node_id": "optional scroll-to-node; include_internal=false computes offset in the visible-only reader pagination collection, include_internal=true uses the full node collection, and path=current with no current-path nodes uses the effective all collection",
            },
            "raw": {
                "endpoint": "/api/conversations/{conversation_id}/messages/{node_id}/raw",
                "max_chars": "1-200000, default 50000; when raw_size exceeds max_chars, returns truncated raw_text; max_chars=0 is rejected",
                "response": ["raw_message", "raw_text", "raw_size", "truncated"],
                "truncated": "when true, use raw_text as plain preview; raw_message is a compat field in truncated mode",
            },
            "export": {
                "endpoint": "/api/conversations/{conversation_id}/export",
                "format": ["md", "txt"],
                "path": ["current", "all"],
                "include_internal": "boolean; when true, internal/technical nodes are included in the export",
            },
            "search": {
                "count_total": "boolean; disable exact total count for faster navigation pages",
                "raw_query_override": "path: and scope: modifiers in q override sidebar path/scope selectors",
                "diagnostics": {
                    "fields": [
                        "candidate_backend",
                        "web_index_missing",
                        "normalized_trigram_available",
                        "legacy_trigram_index",
                        "legacy_fts_present",
                        "short_query",
                        "diagnostics_accuracy",
                        "actual_fallback_note",
                        "estimated_backend_note",
                    ],
                    "candidate_backend": "best-effort normalized-safe candidate or scan estimate for the dominant search path: normalized_trigram, normalized_title_trigram, normalized_scan, normalized_title_scan, or full_scan",
                    "legacy": "legacy raw FTS/index presence is reported separately and is not a normalized-safe candidate backend",
                    "accuracy": "best_effort — reflects available normalized-safe indexes and fallbacks, not every predicate branch",
                },
            },
            "suggest": {
                "endpoint": "/api/search/suggest",
                "q_limit": "100 characters max",
            },
            "upload": {
                "endpoint": "/api/import/upload",
                "env": {
                    "CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES": "max compressed ZIP size (default 20 GiB local, 128 MiB remote)",
                    "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES": "max single conversation JSON member uncompressed (default 64 GiB local, 256 MiB remote)",
                    "CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS": "max conversation JSON member count (default 5000)",
                    "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES": "max total uncompressed JSON data (default 128 GiB local, 512 MiB remote)",
                    "CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO": "max compression ratio (default 1000.0 local, 200.0 remote)",
                    "CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS": "max total ZIP members (default 100000 local, 10000 remote)",
                    "CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS": "set to true on non-loopback hosts to allow explicit per-limit env overrides above remote-safe defaults",
                    "CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE": "set to local on trusted non-loopback hosts only to use local large defaults for unset limits",
                },
                "remote": "non-loopback hosts use conservative defaults; ALLOW_REMOTE_UPLOADS=true only honors explicit per-limit overrides, while CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local restores local large defaults",
                "effective_policy": _upload_policy_schema(policy),
                "limits_note": "ZIP size checks run before import; JSON parsing, SQLite writes, and web-index rebuild still consume memory, disk, and CPU proportional to decoded conversation JSON size.",
            },
        }

    @router.get("/conversations")
    def conversations(
        q: Annotated[str, Query(max_length=500)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        sort: str = "newest",
        after: Annotated[str | None, Query(max_length=MAX_DATE_PARAM_LENGTH)] = None,
        before: Annotated[str | None, Query(max_length=MAX_DATE_PARAM_LENGTH)] = None,
        role: str | None = None,
        title: Annotated[str | None, Query(max_length=200)] = None,
        scope: str = "all",
        exact: Annotated[str | None, Query(max_length=300)] = None,
        exclude: Annotated[str | None, Query(max_length=200)] = None,
        source: Annotated[str | None, Query(max_length=200)] = None,
        path: str = "current",
        match_mode: str = "contains",
        selected_id: Annotated[str | None, Query(max_length=MAX_ID_PARAM_LENGTH)] = None,
        conn=Depends(get_optional_conn),
    ):
        if conn is None:
            return _empty_page(limit, offset, selected_id=selected_id, db_ready=False)
        _validate_common(sort=sort, scope=scope, role=role, path=path, match_mode=match_mode)
        parsed = parse_query(
            q,
            path_default=path,
            role=role,
            title=title,
            scope=scope,
            exact=exact,
            exclude=exclude,
            after=after,
            before=before,
            source=source,
            match_mode=match_mode,
            enforce_api_limits=True,
        )
        _raise_query_errors(parsed)
        if parsed.has_search_context():
            return search_conversations(conn, parsed, limit=limit, offset=offset, sort=sort, selected_id=selected_id)
        return list_conversations(conn, limit=limit, offset=offset, sort=sort, after=parsed.after, before=parsed.before, selected_id=selected_id)

    @router.post("/import/upload")
    async def import_upload(file: UploadFile = File(...)):
        if not manager.acquire_pending_upload_slot():
            raise HTTPException(status_code=409, detail="an import job is already running")
        upload_dir: Path | None = None
        transferred = False
        filename = file.filename or "upload.zip"
        try:
            if not filename.lower().endswith(".zip"):
                raise HTTPException(status_code=400, detail="only .zip uploads are supported")
            upload_dir, upload_path = make_upload_path()
            size = 0
            with upload_path.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > policy.max_upload_bytes:
                        raise HTTPException(status_code=413, detail="upload_too_large")
                    out.write(chunk)
            if not zipfile.is_zipfile(upload_path):
                raise HTTPException(status_code=400, detail="uploaded file is not a valid zip")
            _validate_upload_zip_members(upload_path, policy)
            try:
                job = manager.start_import(upload_path, filename=Path(filename.replace("\\", "/")).name, size=size)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            transferred = True
            return job.snapshot()
        finally:
            if not transferred:
                manager.release_pending_upload_slot()
                if upload_dir is not None:
                    cleanup_upload_dir(upload_dir)
            # On success, start_import() took ownership of the slot and upload_dir;
            # the import job thread will clean up.

    @router.get("/import/jobs")
    def import_jobs():
        return {"items": [job.snapshot() for job in manager.list_jobs()]}

    @router.get("/import/jobs/{job_id}")
    def import_job(job_id: str):
        job = manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job.snapshot()

    @router.get("/conversations/{conversation_id}")
    def conversation_detail(conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)], conn=Depends(get_conn)):
        item = get_conversation(conn, conversation_id)
        if not item:
            raise HTTPException(status_code=404, detail="conversation not found")
        return item

    @router.get("/conversations/{conversation_id}/messages")
    def conversation_messages(
        conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)],
        path: str = "current",
        q: Annotated[str, Query(max_length=500)] = "",
        match_mode: str = "contains",
        role: str | None = None,
        title: Annotated[str | None, Query(max_length=200)] = None,
        scope: str = "all",
        exact: Annotated[str | None, Query(max_length=300)] = None,
        exclude: Annotated[str | None, Query(max_length=200)] = None,
        after: Annotated[str | None, Query(max_length=MAX_DATE_PARAM_LENGTH)] = None,
        before: Annotated[str | None, Query(max_length=MAX_DATE_PARAM_LENGTH)] = None,
        source: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=300)] = 300,
        offset: Annotated[int, Query(ge=0)] = 0,
        around_node_id: Annotated[str | None, Query(max_length=MAX_ID_PARAM_LENGTH)] = None,
        include_internal: bool = False,
        conn=Depends(get_conn),
    ):
        _validate_common(scope=scope, role=role, path=path, match_mode=match_mode)
        parsed = parse_query(
            q,
            path_default=path,
            role=role,
            title=title,
            scope=scope,
            exact=exact,
            exclude=exclude,
            after=after,
            before=before,
            source=source,
            match_mode=match_mode,
            enforce_api_limits=True,
        )
        _raise_query_errors(parsed)
        if not get_conversation(conn, conversation_id):
            raise HTTPException(status_code=404, detail="conversation not found")
        return get_messages(
            conn,
            conversation_id,
            path=parsed.path,
            limit=limit,
            offset=offset,
            highlight_query=q,
            highlight_parsed=parsed,
            match_mode=match_mode,
            around_node_id=around_node_id,
            include_internal=include_internal,
        )

    @router.get("/conversations/{conversation_id}/messages/{node_id}/raw")
    def conversation_message_raw(
        conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)],
        node_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)],
        max_chars: int = Query(default=50000, ge=1, le=200000, alias="max_chars"),
        conn=Depends(get_conn),
    ):
        size_row = conn.execute(
            "SELECT COALESCE(length(raw_message_json), 0) AS raw_size FROM conversation_nodes WHERE conversation_id = ? AND node_id = ?",
            (conversation_id, node_id),
        ).fetchone()
        if size_row is None:
            raise HTTPException(status_code=404, detail="message not found")
        raw_size = int(size_row["raw_size"] or 0)
        if raw_size == 0:
            return {"conversation_id": conversation_id, "node_id": node_id, "raw_message": None, "raw_size": 0, "truncated": False}
        if max_chars > 0 and raw_size > max_chars:
            truncated_row = conn.execute(
                "SELECT substr(raw_message_json, 1, ?) AS raw_preview FROM conversation_nodes WHERE conversation_id = ? AND node_id = ?",
                (max_chars, conversation_id, node_id),
            ).fetchone()
            raw_preview = truncated_row["raw_preview"] or ""
            return {
                "conversation_id": conversation_id,
                "node_id": node_id,
                "raw_message": raw_preview,
                "raw_text": raw_preview,
                "raw_size": raw_size,
                "truncated": True,
            }
        row = conn.execute(
            "SELECT raw_message_json FROM conversation_nodes WHERE conversation_id = ? AND node_id = ?",
            (conversation_id, node_id),
        ).fetchone()
        try:
            raw = json.loads(row["raw_message_json"] or "null")
        except json.JSONDecodeError:
            raw = row["raw_message_json"]
        return {"conversation_id": conversation_id, "node_id": node_id, "raw_message": raw, "raw_size": raw_size, "truncated": False}

    @router.get("/conversations/{conversation_id}/export")
    def conversation_export(conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)], format: str = "md", path: str = "current", include_internal: bool = False, conn=Depends(get_conn)):
        _validate_common(path=path)
        if format not in {"md", "txt"}:
            raise HTTPException(status_code=400, detail="format must be md or txt")
        conv = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="conversation not found")
        messages = []
        offset = 0
        while True:
            page = get_messages(conn, conversation_id, path=path, limit=300, offset=offset, include_internal=True)
            messages.extend(page["items"])
            offset += page["limit"]
            if offset >= page["total"]:
                break
        visible_messages = [
            row for row in messages
            if not row.get("is_empty_mapping_node") and (include_internal or not row.get("is_internal"))
        ]
        rows = [_dict_row_to_mapping(_export_message_row(row)) for row in visible_messages]
        text = render_markdown(conv, rows) if format == "md" else render_txt(conv, rows)
        media_type = "text/markdown; charset=utf-8" if format == "md" else "text/plain; charset=utf-8"
        filename = _download_filename(conversation_id, format)
        return Response(
            content=text,
            media_type=media_type,
            headers={"Content-Disposition": _content_disposition(filename)},
        )

    @router.get("/search")
    def search(
        q: Annotated[str, Query(max_length=500)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        sort: str = "relevance",
        path: str = "current",
        role: str | None = None,
        title: Annotated[str | None, Query(max_length=200)] = None,
        scope: str = "all",
        exact: Annotated[str | None, Query(max_length=300)] = None,
        exclude: Annotated[str | None, Query(max_length=200)] = None,
        after: Annotated[str | None, Query(max_length=MAX_DATE_PARAM_LENGTH)] = None,
        before: Annotated[str | None, Query(max_length=MAX_DATE_PARAM_LENGTH)] = None,
        source: Annotated[str | None, Query(max_length=200)] = None,
        match_mode: str = "contains",
        selected_id: Annotated[str | None, Query(max_length=MAX_ID_PARAM_LENGTH)] = None,
        conn=Depends(get_optional_conn),
    ):
        if conn is None:
            return _empty_page(limit, offset, selected_id=selected_id, db_ready=False)
        _validate_common(sort=sort, scope=scope, role=role, path=path, match_mode=match_mode)
        parsed = parse_query(q, path_default=path, role=role, title=title, scope=scope, exact=exact, exclude=exclude, after=after, before=before, source=source, match_mode=match_mode, enforce_api_limits=True)
        _raise_query_errors(parsed)
        return search_conversations(conn, parsed, limit=limit, offset=offset, sort=sort, selected_id=selected_id)

    @router.get("/search/messages")
    def search_message_endpoint(
        q: Annotated[str, Query(max_length=500)] = "",
        conversation_id: Annotated[str | None, Query(max_length=MAX_ID_PARAM_LENGTH)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        path: str = "current",
        order: str = "relevance",
        role: str | None = None,
        title: Annotated[str | None, Query(max_length=200)] = None,
        scope: str = "all",
        exact: Annotated[str | None, Query(max_length=300)] = None,
        exclude: Annotated[str | None, Query(max_length=200)] = None,
        after: Annotated[str | None, Query(max_length=MAX_DATE_PARAM_LENGTH)] = None,
        before: Annotated[str | None, Query(max_length=MAX_DATE_PARAM_LENGTH)] = None,
        source: Annotated[str | None, Query(max_length=200)] = None,
        match_mode: str = "contains",
        count_total: bool = True,
        conn=Depends(get_optional_conn),
    ):
        if conn is None:
            return _empty_page(limit, offset, selected_id=None, db_ready=False)
        _validate_common(role=role, path=path, scope=scope, match_mode=match_mode)
        if order not in ALLOWED_MESSAGE_ORDERS:
            raise HTTPException(status_code=400, detail="invalid message order")
        parsed = parse_query(q, path_default=path, role=role, title=title, scope=scope, exact=exact, exclude=exclude, after=after, before=before, source=source, match_mode=match_mode, enforce_api_limits=True)
        _raise_query_errors(parsed)
        return search_messages(conn, parsed, limit=limit, offset=offset, conversation_id=conversation_id, order=order, count_total=count_total)

    @router.get("/search/suggest")
    def suggest(q: Annotated[str, Query(max_length=100)] = "", limit: Annotated[int, Query(ge=1, le=20)] = 10, conn=Depends(get_optional_conn)):
        if conn is None:
            return {"items": []}
        normalized = normalize_search_text(q)
        if _table_exists(conn, "web_title_norm") and normalized:
            rows = conn.execute(
                """
                SELECT c.conversation_id, c.title
                FROM web_title_norm tn
                JOIN conversations c ON c.conversation_id = tn.conversation_id
                WHERE instr(tn.title_norm, ?) > 0
                ORDER BY COALESCE(c.update_time, c.create_time, 0) DESC
                LIMIT ?
                """,
                (normalized, limit),
            ).fetchall()
        else:
            needle = _like_pattern(q[:100])
            rows = conn.execute(
                """
                SELECT conversation_id, title
                FROM conversations
                WHERE ? = '%%' OR title LIKE ? ESCAPE '\\'
                ORDER BY COALESCE(update_time, create_time, 0) DESC
                LIMIT ?
                """,
                (needle, needle, limit),
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    return router


def _empty_stats(*, db_ready: bool) -> dict[str, object]:
    return {
        "db_ready": db_ready,
        "conversations": 0,
        "nodes": 0,
        "current_path_nodes": 0,
        "warnings": 0,
        "earliest_create_time": None,
        "latest_create_time": None,
        "earliest_update_time": None,
        "latest_update_time": None,
    }


def _validate_upload_zip_members(path: Path, policy: UploadPolicy) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            all_infos = zf.infolist()
            total_members = len(all_infos)
            candidates = [_info for _info in all_infos if _is_conversation_json_member(_info.filename)]
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="uploaded file is not a valid zip") from exc
    if total_members > policy.max_total_members:
        raise HTTPException(status_code=413, detail="upload_zip_too_many_members")
    if len(candidates) > policy.max_json_members:
        raise HTTPException(status_code=413, detail="upload_zip_too_many_json_members")
    total_uncompressed = 0
    total_compressed = 0
    for info in candidates:
        total_uncompressed += int(info.file_size or 0)
        total_compressed += max(1, int(info.compress_size or 0))
        if info.file_size > policy.max_json_member_bytes:
            raise HTTPException(status_code=413, detail="upload_zip_member_too_large")
        if info.file_size >= 10 * 1024 * 1024 and (info.file_size / max(1, info.compress_size)) > policy.max_compression_ratio:
            raise HTTPException(status_code=413, detail="upload_zip_compression_ratio_too_high")
    if total_uncompressed > policy.max_total_uncompressed_bytes:
        raise HTTPException(status_code=413, detail="upload_zip_uncompressed_too_large")
    if total_uncompressed >= 10 * 1024 * 1024 and (total_uncompressed / max(1, total_compressed)) > policy.max_compression_ratio:
        raise HTTPException(status_code=413, detail="upload_zip_compression_ratio_too_high")


def _is_conversation_json_member(name: str) -> bool:
    basename = Path(name.replace("\\", "/")).name
    return basename == "conversations.json" or (basename.startswith("conversations-") and basename.endswith(".json"))


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _table_exists(conn, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row is not None


def _empty_page(limit: int, offset: int, *, selected_id: str | None, db_ready: bool) -> dict[str, object]:
    return {
        "db_ready": db_ready,
        "items": [],
        "total": 0,
        "limit": limit,
        "offset": offset,
        "has_more": False,
        "next_offset": None,
        "selected_in_results": False if selected_id else None,
    }


def _schema_error_detail(schema: dict[str, object]) -> dict[str, object]:
    return {
        "error": "database schema is not compatible",
        "schema_compatible": False,
        "missing_tables": schema.get("missing_tables", []),
        "missing_columns": schema.get("missing_columns", {}),
    }


def _dict_row_to_mapping(row: dict):
    class MappingRow(dict):
        def __getitem__(self, key):
            return dict.get(self, key)

    return MappingRow(row)


def _export_message_row(row: dict) -> dict:
    output = dict(row)
    display_text = output.get("display_text") or output.get("render_text") or output.get("content_text") or ""
    output["content_text"] = display_text
    return output


def _download_filename(conversation_id: str, fmt: str) -> str:
    return f"{safe_filename_part(conversation_id, 80)}.{fmt}"


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = safe_filename_part(ascii_name, 80)
    if "." not in ascii_name and "." in filename:
        ascii_name = f"{ascii_name}.{filename.rsplit('.', 1)[-1]}"
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _validate_common(
    *,
    sort: str | None = None,
    scope: str | None = None,
    role: str | None = None,
    path: str | None = None,
    match_mode: str | None = None,
) -> None:
    if sort is not None and sort not in ALLOWED_SORTS:
        raise HTTPException(status_code=400, detail="invalid sort")
    if scope is not None and scope not in ALLOWED_SCOPES:
        raise HTTPException(status_code=400, detail="invalid scope")
    if role is not None and role not in ALLOWED_ROLES:
        raise HTTPException(status_code=400, detail="invalid role")
    if path is not None and path not in ALLOWED_PATHS:
        raise HTTPException(status_code=400, detail="path must be current or all")
    if match_mode is not None and match_mode not in ALLOWED_MATCH_MODES:
        raise HTTPException(status_code=400, detail="invalid match mode")


def _raise_query_errors(parsed) -> None:
    if parsed.errors:
        raise HTTPException(status_code=400, detail="; ".join(parsed.errors))
