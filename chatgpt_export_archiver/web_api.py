from __future__ import annotations

import ipaddress
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Path as ApiPath, Query, Request, Response, UploadFile

from .exporter import render_markdown, render_txt
from .db import foreign_key_diagnostics
from .logging_utils import get_logger
from .scanner import SourceEntry, is_conversation_json_source, is_metadata_path, select_conversation_sources
from .search import _has_normalized_title_norm, get_conversation, get_messages, list_conversations, normalize_search_text, parse_query, search_conversations, search_messages
from .utils import finite_float_or_none, safe_filename_part
from .web_db import check_schema, connect_readonly, detect_fts5, detect_trigram, web_index_status
from .web_jobs import ImportJobManager, ImportJobStartError, cleanup_upload_dir, make_upload_path

LOGGER = get_logger("web_api")

ALLOWED_SORTS = {"relevance", "newest", "oldest", "created", "updated", "title"}
ALLOWED_SCOPES = {"all", "title", "message"}
ALLOWED_ROLES = {"", "user", "assistant", "tool", "system", "developer", "tool/system", "tool_system"}
ALLOWED_PATHS = {"current", "all"}
ALLOWED_MESSAGE_ORDERS = {"relevance", "display"}
ALLOWED_MATCH_MODES = {"contains", "word"}
MAX_DATE_PARAM_LENGTH = 64
MAX_ID_PARAM_LENGTH = 512
JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")

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
ALLOW_REMOTE_ACCESS_ENV = "CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS"
REMOTE_UPLOAD_PROFILE_ENV = "CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE"
ALLOWED_HOSTS_ENV = "CHATGPT_ARCHIVE_ALLOWED_HOSTS"
TRUSTED_PROXIES_ENV = "CHATGPT_ARCHIVE_TRUSTED_PROXIES"

REMOTE_DEFAULT_MAX_UPLOAD_BYTES = 128 * 1024 * 1024
REMOTE_DEFAULT_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
REMOTE_DEFAULT_MEMBER_BYTES = 256 * 1024 * 1024
REMOTE_DEFAULT_COMPRESSION_RATIO = 200.0
REMOTE_DEFAULT_TOTAL_MEMBERS = 10000
MULTIPART_OVERHEAD_ALLOWANCE = 16 * 1024 * 1024


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
    max_multipart_body_bytes: int | None = None


@dataclass(frozen=True)
class WebTrustPolicy:
    allowed_hosts: tuple[str, ...]
    trusted_proxies: tuple[str, ...]
    remote: bool
    allow_missing_origin_for_writes: bool


def _split_config_list(raw: str | None) -> tuple[str, ...]:
    return tuple(sorted({part.strip().casefold() for part in (raw or "").split(",") if part.strip()}))


def _get_web_trust_policy(
    *,
    host: str,
    environ: Mapping[str, str] = os.environ,
    allowed_hosts: str | None = None,
    trusted_proxies: str | None = None,
) -> WebTrustPolicy:
    remote = not _is_loopback(host)
    configured_hosts = _split_config_list(
        allowed_hosts if allowed_hosts is not None else environ.get(ALLOWED_HOSTS_ENV)
    )
    if "*" in configured_hosts:
        raise ValueError("allowed_hosts_wildcard_not_permitted")
    if remote:
        if not configured_hosts:
            raise ValueError(
                "non_loopback_access_requires_allowed_hosts: set CHATGPT_ARCHIVE_ALLOWED_HOSTS "
                "to the actual browser hostname or LAN IP"
            )
        effective_hosts = configured_hosts
    else:
        effective_hosts = tuple(sorted(set(configured_hosts) | {"localhost", "127.0.0.1", "::1", "testserver", host.casefold()}))
    proxy_values = _split_config_list(
        trusted_proxies if trusted_proxies is not None else environ.get(TRUSTED_PROXIES_ENV)
    )
    for value in proxy_values:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError("invalid_trusted_proxy") from exc
    return WebTrustPolicy(
        allowed_hosts=effective_hosts,
        trusted_proxies=proxy_values,
        remote=remote,
        allow_missing_origin_for_writes=not remote,
    )


def upload_body_limit(policy: UploadPolicy) -> int:
    return int(policy.max_multipart_body_bytes or (policy.max_upload_bytes + MULTIPART_OVERHEAD_ALLOWANCE))


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


def _remote_access_allowed(environ: Mapping[str, str] = os.environ) -> bool:
    return (
        _env_truthy(environ, ALLOW_REMOTE_ACCESS_ENV)
        or _env_truthy(environ, ALLOW_REMOTE_UPLOADS_ENV)
        or environ.get(REMOTE_UPLOAD_PROFILE_ENV, "").strip().casefold() == "local"
    )


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
        "max_multipart_body_bytes": upload_body_limit(policy),
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
        "remote_access_requires_opt_in": policy.remote,
    }


def create_api_router(
    db_path: Path,
    job_manager: ImportJobManager | None = None,
    upload_policy: UploadPolicy | None = None,
    trust_policy: WebTrustPolicy | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    manager = job_manager or ImportJobManager(db_path)
    policy = _effective_upload_policy(upload_policy or _get_upload_policy())
    trust = trust_policy or _get_web_trust_policy(host="127.0.0.1")

    def get_conn():
        if not db_path.exists():
            raise HTTPException(status_code=409, detail="database_not_ready")
        try:
            conn = connect_readonly(db_path)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="database_not_ready") from exc
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
        access = {
            "access_profile": "remote_opt_in" if policy.remote else "loopback_local",
            "remote_access": policy.remote,
            "allowed_hosts": list(trust.allowed_hosts),
            "trusted_proxies": list(trust.trusted_proxies),
            "write_origin_required": not trust.allow_missing_origin_for_writes,
        }
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
                **access,
            }
        try:
            conn = connect_readonly(db_path)
        except ValueError:
            return {"ok": True, "db_ready": False, "database": {"name": "database", "exists": db_path.exists()}, "schema_version": 1, **access}
        try:
            schema = check_schema(conn)
            web_status = web_index_status(conn)
            fts5 = detect_fts5(conn)
            trigram = detect_trigram(conn)
            foreign_keys = foreign_key_diagnostics(conn) if schema["ok"] else {
                "foreign_key_violations": 0,
                "foreign_key_violations_by_table": [],
                "foreign_key_violation_samples": [],
            }
        finally:
            conn.close()
        return {
            "ok": schema["ok"] and foreign_keys["foreign_key_violations"] == 0,
            "db_ready": schema["ok"],
            "schema_compatible": schema["schema_compatible"],
            "missing_tables": schema["missing_tables"],
            "missing_columns": schema["missing_columns"],
            "database": {"name": "database", "exists": db_path.exists()},
            "schema_version": 1,
            "fts5_available": fts5,
            "message_fts_available": schema["message_fts"],
            "trigram_available": trigram,
            **foreign_keys,
            **access,
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
                       MIN(CASE WHEN create_time BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 THEN create_time END) AS earliest_create_time,
                       MAX(CASE WHEN create_time BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 THEN create_time END) AS latest_create_time,
                       MIN(CASE WHEN update_time BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 THEN update_time END) AS earliest_update_time,
                       MAX(CASE WHEN update_time BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 THEN update_time END) AS latest_update_time
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
            "earliest_create_time": finite_float_or_none(row["earliest_create_time"]),
            "latest_create_time": finite_float_or_none(row["latest_create_time"]),
            "earliest_update_time": finite_float_or_none(row["earliest_update_time"]),
            "latest_update_time": finite_float_or_none(row["latest_update_time"]),
        }

    @router.get("/schema")
    def schema_docs():
        return {
            "version": 2,
            "pagination": {
                "conversation_page": ["items", "total", "limit", "offset", "has_more", "next_offset"],
                "message_page": ["items", "total", "limit", "offset", "has_more", "next_offset"],
                "message_search_page": ["items", "total", "total_exact", "limit", "offset", "has_more", "next_offset"],
                "total_exact": "true means total is an exact count; false means total is only the known lower bound from the current page probe",
            },
            "conversations": {
                "endpoint": "/api/conversations",
                "filters": ["q", "sort", "after", "before", "role", "title", "scope", "exact", "exclude", "source", "path", "match_mode"],
                "limits": {"q": 500, "title": 200, "exact": 300, "exclude": 200, "source": 200, "after": MAX_DATE_PARAM_LENGTH, "before": MAX_DATE_PARAM_LENGTH, "selected_id": MAX_ID_PARAM_LENGTH},
                "path": ["current", "all"],
                "match_mode": ["contains", "word"],
                "date": "after/before use UTC calendar days as YYYY-MM-DD; before is exclusive (next-day 00:00:00 UTC)",
                "selection": ["selected_id", "selected_in_results", "selected_item"],
                "response": ["total", "items", "has_more", "next_offset", "selected_in_results", "selected_item", "node_count", "current_path_nodes", "current_node_exists", "current_collection_source", "current_path_fallback_to_all", "effective_path", "cycle_detected", "missing_parent", "cross_conversation_parent", "partial_chain", "raw_flag_leaf_count", "selected_chain_cycle_detected", "raw_flag_cycle_detected", "selected_chain_missing_parent", "raw_flag_missing_parent", "selected_chain_cross_conversation_parent", "raw_flag_cross_conversation_parent"],
                "diagnostics": "best-effort search diagnostics; see search.diagnostics",
            },
            "messages": {
                "endpoint": "/api/conversations/{conversation_id}/messages",
                "path": ["current", "all"],
                "include_internal": "boolean; default false for reader pages so pagination is over visible messages, true includes root/internal/technical nodes",
                "filters": ["q", "after", "before", "role", "title", "scope", "exact", "exclude", "source", "match_mode"],
                "limits": {"conversation_id": MAX_ID_PARAM_LENGTH, "around_node_id": MAX_ID_PARAM_LENGTH, "q": 500, "title": 200, "exact": 300, "exclude": 200, "source": 200, "after": MAX_DATE_PARAM_LENGTH, "before": MAX_DATE_PARAM_LENGTH},
                "raw": "message pages return raw_preview only; capped raw preview is available per message endpoint",
                "hidden_counts": {
                    "fields": ["visible_total", "empty_hidden_count", "internal_hidden_count", "technical_hidden_count"],
                    "contract": "internal_hidden_count is the one canonical non-empty internal-node count; technical_hidden_count is a deprecated exact alias retained for compatibility",
                },
                "path_metadata": ["current_node_exists", "current_collection_source", "effective_path", "current_path_fallback_to_all", "effective_visible_in_current_view", "cycle_detected", "missing_parent", "cross_conversation_parent", "partial_chain", "raw_flag_leaf_count", "selected_chain_cycle_detected", "raw_flag_cycle_detected", "selected_chain_missing_parent", "raw_flag_missing_parent", "selected_chain_cross_conversation_parent", "raw_flag_cross_conversation_parent"],
                "highlight": "highlight_ranges use UTF-16 code-unit offsets for JS text.slice(); highlight_ranges_truncated discloses the bounded preview cap",
                "match_mode": ["contains", "word"],
                "around_node_id": {
                    "description": "optional scroll-to-node; include_internal=false computes offset in the visible-only reader pagination collection, include_internal=true uses the full node collection, and path=current with no current-path nodes uses the effective all collection",
                    "response": ["around_target_found", "around_target_in_effective_collection", "around_target_in_requested_collection", "around_target_visible", "around_target_applied"],
                },
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
                "endpoints": ["/api/conversations", "/api/search/messages"],
                "parameters": ["q", "title", "exact", "exclude", "role", "source", "after", "before", "scope", "path", "match_mode", "order", "conversation_id", "count_total"],
                "message_order": ["relevance", "display"],
                "count_total": "boolean; false disables the exact count and returns total_exact=false with a known lower-bound total",
                "filter_only": "filter-only and exclude-only queries may filter conversation results; message hits and reader highlights require a positive message-text term, and role/source/date filters alone do not create hit navigation",
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
                "parameters": ["q", "limit"],
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
                    "CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS": "set to true to explicitly permit non-loopback browser access while retaining remote-safe upload limits",
                    "CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE": "set to local on trusted non-loopback hosts only to use local large defaults for unset limits",
                    "CHATGPT_ARCHIVE_ALLOWED_HOSTS": "comma-separated exact browser hostnames/IPs; required for non-loopback binding; wildcard is rejected",
                    "CHATGPT_ARCHIVE_TRUSTED_PROXIES": "comma-separated proxy IPs/CIDRs allowed to supply Forwarded/X-Forwarded-Host/Proto",
                },
                "remote": "non-loopback hosts use conservative defaults; ALLOW_REMOTE_UPLOADS=true only honors explicit per-limit overrides, while CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local restores local large defaults",
                "effective_policy": _upload_policy_schema(policy),
                "host_origin_policy": {
                    "allowed_hosts": list(trust.allowed_hosts),
                    "trusted_proxies": list(trust.trusted_proxies),
                    "missing_origin_write_allowed": trust.allow_missing_origin_for_writes,
                    "origin": "writes require a trusted Host and a same-origin Origin in remote mode; loopback permits non-browser clients without Origin",
                    "forwarded_headers": "strict edge-proxy model: ignored from untrusted peers; a trusted direct edge must overwrite client values and provide at most one Forwarded element or one X-Forwarded-Host/Proto value; duplicates, chains, malformed syntax, and conflicts are rejected",
                },
                "limits_note": "ZIP size checks run before import; JSON parsing, SQLite writes, and web-index rebuild still consume memory, disk, and CPU proportional to decoded conversation JSON size.",
            },
            "jobs": {
                "endpoints": ["/api/import/jobs", "/api/import/jobs/{job_id}"],
                "job_id": "lowercase 32-character hexadecimal UUID; invalid syntax returns invalid_job_id and a valid unknown ID returns job_not_found",
                "statuses": ["queued", "running", "succeeded", "failed", "postcheck_failed"],
                "outcomes": ["queued", "import_running", "input_preflight_failed", "source_scan_failed", "json_decode_failed", "top_level_contract_failed", "import_transaction_failed", "canonical_commit_succeeded", "verify_failed", "stats_failed", "web_index_failed", "succeeded"],
                "fields": ["status", "stage", "outcome", "canonical_commit_succeeded", "error_code", "error_type", "cleanup_warning", "summary", "verify", "stats", "web_index"],
                "failure_codes": ["import_job_start_failed", "no_conversation_sources", "ambiguous_conversation_sources", "source_scan_failed", "invalid_conversation_json", "non_finite_json_number", "conversation_json_top_level_not_list", "import_transaction_failed"],
                "cleanup_warnings": ["upload_file_unlink_failed", "upload_directory_cleanup_failed", "upload_directory_cleanup_incomplete"],
                "preflight_cleanup_error": ["code", "cleanup_warning", "cleanup_error_type"],
            },
            "ui_state": {
                "canonical_copy_url": ["match_mode", "layout", "show_internal", "sort", "path", "scope", "q", "role", "title", "exact", "exclude", "source", "after", "before", "selected conversation"],
                "url_precedence": "explicit URL values win over localStorage; missing values may use local settings",
                "back_forward": "incremental search and selection history restoration is not implemented; routine address-bar updates use replaceState",
            },
            "database_compatibility": "older databases are checked but are not automatically migrated; re-import the original export into a new database when required columns are missing",
            "stable_error_codes": [
                "database_not_ready", "database_schema_incompatible", "invalid_job_id", "job_not_found",
                "conversation_not_found", "message_not_found", "invalid_export_format",
                "invalid_sort", "invalid_scope", "invalid_role", "invalid_path",
                "invalid_match_mode", "invalid_message_order", "invalid_query",
                "host_not_allowed", "invalid_host_header", "invalid_forwarded_headers", "import_job_active", "import_job_start_failed", "upload_origin_required", "upload_origin_not_allowed", "upload_content_length_required",
                "upload_invalid_content_length", "upload_multipart_body_too_large", "upload_too_large",
                "uploaded_file_not_zip", "uploaded_file_invalid_zip", "upload_zip_no_conversation_sources",
                "upload_zip_ambiguous_conversation_sources", "upload_zip_too_many_members",
                "upload_zip_too_many_json_members", "upload_zip_member_too_large",
                "upload_zip_uncompressed_too_large", "upload_zip_compression_ratio_too_high",
                "no_conversation_sources", "ambiguous_conversation_sources", "source_scan_failed",
                "invalid_conversation_json", "non_finite_json_number", "conversation_json_top_level_not_list",
                "import_transaction_failed", "verify_failed", "stats_failed", "web_index_failed",
            ],
            "provenance": {
                "input_sha256": "computed only for ZIP imports unless --no-input-sha256 is used",
                "source_entry_sha256": "source_files/file_index sha256 columns are reserved and currently unset",
                "raw_preservation": "full raw JSON is stored for messages only; conversation and mapping-node raw objects are normalized, not preserved byte-for-byte",
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
        if conn is None:
            return _empty_page(limit, offset, selected_id=selected_id, db_ready=False)
        if parsed.has_search_context():
            return search_conversations(conn, parsed, limit=limit, offset=offset, sort=sort, selected_id=selected_id)
        return list_conversations(conn, limit=limit, offset=offset, sort=sort, after=parsed.after, before=parsed.before, selected_id=selected_id)

    @router.post("/import/upload")
    async def import_upload(request: Request, file: UploadFile = File(...)):
        ingress_reserved = bool(getattr(request.state, "upload_slot_reserved", False))
        if not ingress_reserved and not manager.acquire_pending_upload_slot():
            raise HTTPException(status_code=409, detail="import_job_active")
        upload_dir: Path | None = None
        transferred = False
        primary_http_error: HTTPException | None = None
        filename = file.filename or "upload.zip"
        try:
            if not filename.lower().endswith(".zip"):
                raise HTTPException(status_code=400, detail="uploaded_file_not_zip")
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
                raise HTTPException(status_code=400, detail="uploaded_file_invalid_zip")
            _validate_upload_zip_members(upload_path, policy)
            try:
                job = manager.start_import(upload_path, filename=Path(filename.replace("\\", "/")).name, size=size)
            except ImportJobStartError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"code": exc.code, "error_type": exc.error_type},
                ) from exc
            except RuntimeError as exc:
                detail = "import_job_active" if manager.has_running_job() else "import_job_start_failed"
                raise HTTPException(status_code=409 if detail == "import_job_active" else 503, detail=detail) from exc
            transferred = True
            request.state.upload_slot_transferred = True
            return job.snapshot()
        except HTTPException as exc:
            primary_http_error = exc
            raise
        finally:
            if not transferred:
                manager.release_pending_upload_slot()
                if upload_dir is not None:
                    cleanup = cleanup_upload_dir(upload_dir)
                    if not cleanup["ok"]:
                        if primary_http_error is not None:
                            primary_code = (
                                primary_http_error.detail.get("code")
                                if isinstance(primary_http_error.detail, dict)
                                else str(primary_http_error.detail)
                            )
                            detail = dict(primary_http_error.detail) if isinstance(primary_http_error.detail, dict) else {"code": primary_code}
                            detail.update(
                                {
                                    "cleanup_warning": "temporary_upload_cleanup_failed",
                                    "cleanup_error_type": cleanup["error_type"] or "PathStillExists",
                                }
                            )
                            primary_http_error.detail = detail
                        LOGGER.warning(
                            "upload_preflight_cleanup_failed error_type=%s path_still_exists=%s",
                            cleanup["error_type"] or "none",
                            cleanup["path_still_exists"],
                        )
            # On success, start_import() took ownership of the slot and upload_dir;
            # the import job thread will clean up.

    @router.get("/import/jobs")
    def import_jobs():
        return {"items": [job.snapshot() for job in manager.list_jobs()]}

    @router.get("/import/jobs/{job_id}")
    def import_job(job_id: str):
        if JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise HTTPException(status_code=400, detail="invalid_job_id")
        job = manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_not_found")
        return job.snapshot()

    @router.get("/conversations/{conversation_id}")
    def conversation_detail(conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)], conn=Depends(get_conn)):
        item = get_conversation(conn, conversation_id)
        if not item:
            raise HTTPException(status_code=404, detail="conversation_not_found")
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
            raise HTTPException(status_code=404, detail="conversation_not_found")
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
            raise HTTPException(status_code=404, detail="message_not_found")
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
            raise HTTPException(status_code=400, detail="invalid_export_format")
        conv = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="conversation_not_found")
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
        _validate_common(sort=sort, scope=scope, role=role, path=path, match_mode=match_mode)
        parsed = parse_query(q, path_default=path, role=role, title=title, scope=scope, exact=exact, exclude=exclude, after=after, before=before, source=source, match_mode=match_mode, enforce_api_limits=True)
        _raise_query_errors(parsed)
        if conn is None:
            return _empty_page(limit, offset, selected_id=selected_id, db_ready=False)
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
        _validate_common(role=role, path=path, scope=scope, match_mode=match_mode)
        if order not in ALLOWED_MESSAGE_ORDERS:
            raise HTTPException(status_code=400, detail="invalid_message_order")
        parsed = parse_query(q, path_default=path, role=role, title=title, scope=scope, exact=exact, exclude=exclude, after=after, before=before, source=source, match_mode=match_mode, enforce_api_limits=True)
        _raise_query_errors(parsed)
        if conn is None:
            return _empty_message_search_page(limit, offset, db_ready=False)
        return search_messages(conn, parsed, limit=limit, offset=offset, conversation_id=conversation_id, order=order, count_total=count_total)

    @router.get("/search/suggest")
    def suggest(q: Annotated[str, Query(max_length=100)] = "", limit: Annotated[int, Query(ge=1, le=20)] = 10, conn=Depends(get_optional_conn)):
        if conn is None:
            return {"items": []}
        normalized = normalize_search_text(q)
        if _has_normalized_title_norm(conn) and normalized:
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
            conn.create_function("web_normalize", 1, normalize_search_text, deterministic=True)
            rows = conn.execute(
                """
                SELECT conversation_id, title
                FROM conversations
                WHERE ? = '' OR instr(web_normalize(COALESCE(title, '')), ?) > 0
                ORDER BY COALESCE(update_time, create_time, 0) DESC
                LIMIT ?
                """,
                (normalized, normalized, limit),
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
            file_infos = [info for info in all_infos if not info.is_dir()]
            total_members = len(file_infos)
            source_infos = [info for info in file_infos if not is_metadata_path(info.filename)]
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="uploaded_file_invalid_zip") from exc
    if total_members > policy.max_total_members:
        raise HTTPException(status_code=413, detail="upload_zip_too_many_members")
    try:
        selected = select_conversation_sources(
            [
                SourceEntry(
                    source_path=info.filename,
                    file_type="json",
                    size=int(info.file_size or 0),
                    extension=".json",
                    is_conversation_json=is_conversation_json_source(info.filename),
                )
                for info in source_infos
            ]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="upload_zip_ambiguous_conversation_sources") from exc
    if not selected:
        raise HTTPException(status_code=400, detail="upload_zip_no_conversation_sources")
    selected_paths = {entry.source_path for entry in selected}
    candidates = [info for info in source_infos if info.filename in selected_paths]
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


def _empty_message_search_page(limit: int, offset: int, *, db_ready: bool) -> dict[str, object]:
    return {
        "db_ready": db_ready,
        "items": [],
        "total": 0,
        "total_exact": True,
        "limit": limit,
        "offset": offset,
        "has_more": False,
        "next_offset": None,
    }


def _schema_error_detail(schema: dict[str, object]) -> dict[str, object]:
    return {
        "code": "database_schema_incompatible",
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
    clean_name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    clean_name = "".join(ch for ch in clean_name if ch not in "\r\n" and ord(ch) >= 32).strip().rstrip(" .")
    if clean_name.casefold() in {".md", ".txt"}:
        suffix = clean_name.casefold()
        stem = ""
    else:
        candidate_suffix = Path(clean_name).suffix.casefold()
        suffix = candidate_suffix if candidate_suffix in {".md", ".txt"} else ".txt"
        stem = clean_name[: -len(suffix)] if candidate_suffix == suffix else clean_name
    utf8_stem = stem.strip(" .") or "download"
    utf8_name = f"{utf8_stem}{suffix}"
    ascii_stem = utf8_stem.encode("ascii", "ignore").decode("ascii").strip(" .")
    if not ascii_stem:
        ascii_stem = "download"
    ascii_stem = safe_filename_part(ascii_stem, max(1, 80 - len(suffix)))
    if ascii_stem.casefold().endswith(suffix):
        ascii_stem = ascii_stem[: -len(suffix)] or "download"
    ascii_name = f"{ascii_stem}{suffix}"
    quoted = quote(utf8_name, safe="")
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
        raise HTTPException(status_code=400, detail="invalid_sort")
    if scope is not None and scope not in ALLOWED_SCOPES:
        raise HTTPException(status_code=400, detail="invalid_scope")
    if role is not None and role not in ALLOWED_ROLES:
        raise HTTPException(status_code=400, detail="invalid_role")
    if path is not None and path not in ALLOWED_PATHS:
        raise HTTPException(status_code=400, detail="invalid_path")
    if match_mode is not None and match_mode not in ALLOWED_MATCH_MODES:
        raise HTTPException(status_code=400, detail="invalid_match_mode")


def _raise_query_errors(parsed) -> None:
    if parsed.errors:
        raise HTTPException(status_code=400, detail={"code": "invalid_query", "reasons": parsed.errors})
