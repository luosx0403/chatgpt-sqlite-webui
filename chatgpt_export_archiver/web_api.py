from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Path as ApiPath, Query, Request, Response, UploadFile
from starlette.responses import StreamingResponse

from .exporter import (
    ExportResourceLimitError,
    MAX_ARCHIVE_EXPORT_CONVERSATIONS,
    MAX_ARCHIVE_EXPORT_MANIFEST_BYTES,
    MAX_ARCHIVE_EXPORT_METADATA_BYTES,
    MAX_EXPORT_CONVERSATION_INPUT_BYTES,
    MAX_EXPORT_NODE_INPUT_BYTES,
    MAX_EXPORT_NODES_PER_CONVERSATION,
    MAX_EXPORT_OUTPUT_BYTES,
    iter_conversation_export_nodes,
    iter_copy_conversation,
    iter_rendered_conversation,
    validate_conversation_export_budget,
)
from .current_path import (
    EFFECTIVE_CURRENT_SCOPE_BATCH_INPUT_BYTES,
    EFFECTIVE_CURRENT_SCOPE_BATCH_NODES,
    EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS,
    MAX_EFFECTIVE_CURRENT_CONVERSATIONS,
    MAX_EFFECTIVE_CURRENT_GRAPH_BYTES_PER_CONVERSATION,
    MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION,
    MAX_EFFECTIVE_CURRENT_SCOPE_INPUT_BYTES,
    MAX_EFFECTIVE_CURRENT_SCOPE_NODES,
    MAX_EFFECTIVE_CURRENT_TEMP_BYTES,
)
from .db import (
    DatabaseMigrationError,
    database_schema_error_code,
    foreign_key_diagnostics,
    read_request_capabilities,
    require_current_database_schema,
)
from .disk_resources import (
    DiskSpaceGuard,
    DiskSpaceInsufficientError,
    is_disk_full_error,
    require_free_space,
    upload_required_bytes,
)
from .logging_utils import get_logger
from .identifiers import MAX_CANONICAL_ID_LENGTH
from .json_safety import (
    JsonSafetyLimitError,
    MAX_JSON_NESTING_DEPTH,
    MAX_JSON_SCALAR_COUNT,
    MAX_RAW_PREVIEW_BYTES,
    MAX_RAW_PREVIEW_NODES,
    MAX_SANITIZED_OUTPUT_BYTES,
    sanitize_json_value,
    validate_json_lexical_limits,
)
from .parser import MAX_IMPORT_NODES_PER_CONVERSATION, normalize_display_text
from .scanner import (
    MAX_CONVERSATION_JSON_SCALARS,
    MAX_JSON_ELEMENT_BYTES,
    MAX_JSON_ELEMENT_CHARS,
    MAX_SOURCE_TOTAL_MEMBERS,
    SourceEntry,
    is_conversation_json_source,
    is_metadata_path,
    preflight_zip_central_directory,
    select_conversation_sources,
)
from .schema_contract import API_SCHEMA_VERSION, DATABASE_SCHEMA_VERSION
from .sqlite_errors import sqlite_runtime_error_code
from .search import DisplayCursorError, SearchContinuationError, SearchResourceLimitError, SEARCH_CANDIDATE_LIMIT, SEARCH_CANDIDATE_SCAN_CHARS, SEARCH_EXACT_VERIFY_ENV, SEARCH_EXACT_VERIFY_MAX_OPT_IN_CHARS, SEARCH_HIT_PREVIEW_CHARS, SEARCH_PAGE_ESTIMATED_BYTES, SEARCH_RAW_EXACT_MAX_BYTES, SEARCH_RAW_EXACT_MAX_CHARS, SEARCH_REQUEST_VERIFY_BYTES, SEARCH_REQUEST_VERIFY_CHARS, SEARCH_SNIPPET_SCAN_CHARS, SEARCH_STREAM_CHUNK_BYTES, SEARCH_WALL_DEADLINE_SECONDS, _READER_BUDGET_ENV, _has_normalized_title_norm, get_conversation, get_message_display_chunk, get_messages, list_conversations, normalize_search_text, parse_query, reader_budget, search_conversations, search_exact_verify_limits, search_messages
from .utils import finite_float_or_none, safe_filename_part, utc_now_iso
from .web_db import (
    DISPLAY_TEXT_RESOLVER_VERSION,
    NORMALIZATION_INDEX_FORMAT_VERSION,
    WEB_INDEX_BUILD_STAGES,
    WEB_INDEX_FTS_BIND_BATCH_BYTES,
    WEB_INDEX_MAX_DERIVED_BYTES,
    WEB_INDEX_MAX_INPUT_BYTES,
    WEB_INDEX_MAX_NORMALIZED_BYTES,
    WEB_INDEX_FORMAT_VERSION,
    check_schema,
    connect_readonly,
    message_fts_status,
    web_index_status,
)
from .web_jobs import ImportJobManager, ImportJobStartError, cleanup_upload_dir, make_upload_path

LOGGER = get_logger("web_api")

ALLOWED_SORTS = {"relevance", "newest", "oldest", "created", "updated", "title"}
ALLOWED_SCOPES = {"all", "title", "message"}
ALLOWED_ROLES = {"", "user", "assistant", "tool", "system", "developer", "tool/system", "tool_system"}
ALLOWED_PATHS = {"current", "all"}
ALLOWED_MESSAGE_ORDERS = {"relevance", "display"}
ALLOWED_MATCH_MODES = {"contains", "word"}
MAX_DATE_PARAM_LENGTH = 64
MAX_ID_PARAM_LENGTH = MAX_CANONICAL_ID_LENGTH
MAX_LEGACY_ID_PARAM_LENGTH = 16 * 1024
MAX_SQLITE_OFFSET = (1 << 63) - 1
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
    if not math.isfinite(value) or value <= 0:
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
        try:
            conn = connect_readonly(db_path)
        except ValueError as exc:
            code = str(exc) if str(exc).startswith("database_") else "database_not_ready"
            raise HTTPException(status_code=409, detail=code) from exc
        try:
            conn.execute("BEGIN")
            try:
                require_current_database_schema(conn)
            except DatabaseMigrationError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=_schema_error_detail(exc.detail, code=exc.code),
                ) from exc
            yield conn
            if conn.in_transaction:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def get_optional_conn():
        try:
            conn = connect_readonly(db_path)
        except ValueError as exc:
            if str(exc) in {"database_not_found", "database_not_ready"}:
                yield None
                return
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            conn.execute("BEGIN")
            try:
                require_current_database_schema(conn)
            except DatabaseMigrationError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=_schema_error_detail(exc.detail, code=exc.code),
                ) from exc
            yield conn
            if conn.in_transaction:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @router.get("/health")
    def health(deep: bool = False):
        access = {
            "access_profile": "remote_opt_in" if policy.remote else "loopback_local",
            "remote_access": policy.remote,
            "allowed_hosts": list(trust.allowed_hosts),
            "trusted_proxies": list(trust.trusted_proxies),
            "write_origin_required": not trust.allow_missing_origin_for_writes,
        }
        incomplete_integrity = {
            "integrity_mode": "deep" if deep else "quick",
            "foreign_key_check_last_completed_at": None,
            "foreign_key_check_connection_data_version": None,
            "result_stale": True,
        }
        if not db_path.exists():
            return {
                "ok": True,
                "db_ready": False,
                "readiness": "database_missing_or_uninitialized",
                "database_error_code": "database_not_ready",
                "database": {"name": "database", "exists": False},
                "schema_version": API_SCHEMA_VERSION,
                "api_schema_version": API_SCHEMA_VERSION,
                "current_database_schema_version": None,
                "required_database_schema_version": DATABASE_SCHEMA_VERSION,
                "migration_required": False,
                "fts5_available": False,
                "message_fts_available": False,
                "message_fts_rebuildable": False,
                "message_fts_error": None,
                "trigram_available": False,
                "web_trigram_indexed": False,
                "web_normalized_indexed": False,
                "web_normalized_trigram_indexed": False,
                "web_legacy_trigram_indexed": False,
                "schema_compatible": False,
                "foreign_key_violations": 0,
                "foreign_key_violations_exact": False,
                "foreign_key_check_complete": False,
                "foreign_key_violation_sample_limit": 20,
                "foreign_key_violations_by_table": [],
                "foreign_key_violation_samples": [],
                **incomplete_integrity,
                **access,
            }
        try:
            conn = connect_readonly(db_path)
        except (ValueError, sqlite3.Error) as exc:
            error_code = (
                sqlite_runtime_error_code(exc)
                if isinstance(exc, sqlite3.Error)
                else (str(exc) if str(exc).startswith("database_") else "database_not_ready")
            )
            return {
                "ok": False,
                "db_ready": False,
                "readiness": _readiness_from_error_code(error_code),
                "database_error_code": error_code,
                "database": {"name": "database", "exists": db_path.exists()},
                "schema_version": API_SCHEMA_VERSION,
                "api_schema_version": API_SCHEMA_VERSION,
                "current_database_schema_version": None,
                "required_database_schema_version": DATABASE_SCHEMA_VERSION,
                "migration_required": False,
                "schema_compatible": False,
                "foreign_key_violations": 0,
                "foreign_key_violations_exact": False,
                "foreign_key_check_complete": False,
                "foreign_key_violation_sample_limit": 20,
                "foreign_key_violations_by_table": [],
                "foreign_key_violation_samples": [],
                **incomplete_integrity,
                **access,
            }
        try:
            conn.execute("BEGIN")
            try:
                capabilities = read_request_capabilities(conn)
                if deep:
                    schema = check_schema(conn)
                    schema.update(capabilities.schema_status)
                else:
                    schema = check_schema(
                        conn, schema_status=capabilities.schema_status
                    )
                web_status = web_index_status(conn, schema=schema)
                fts5 = capabilities.fts5_available
                fts_status = message_fts_status(conn, fts5_available=fts5)
                trigram = capabilities.trigram_available
                foreign_keys = foreign_key_diagnostics(conn) if deep and schema["base_schema_compatible"] else {
                    "foreign_key_violations": 0,
                    "foreign_key_violations_exact": False,
                    "foreign_key_check_complete": False,
                    "foreign_key_violation_sample_limit": 20,
                    "foreign_key_violations_by_table": [],
                    "foreign_key_violation_samples": [],
                }
                foreign_key_connection_data_version = (
                    int(conn.execute("PRAGMA data_version").fetchone()[0]) if deep else None
                )
                foreign_key_checked_at = utc_now_iso() if foreign_keys["foreign_key_check_complete"] else None
                reader_resource_violations = (
                    int(conn.execute(
                        """SELECT COUNT(*) FROM (
                               SELECT conversation_id
                               FROM conversation_nodes
                               GROUP BY conversation_id
                               HAVING COUNT(*) > ?
                           )""",
                        (MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION,),
                    ).fetchone()[0])
                    if deep and schema["base_schema_compatible"]
                    else 0
                )
                conversation_count = (
                    int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])
                    if database_schema_error_code(schema) is None and foreign_keys["foreign_key_violations"] == 0
                    else 0
                )
            except sqlite3.Error as exc:
                error_code = sqlite_runtime_error_code(exc)
                return {
                    "ok": False,
                    "db_ready": False,
                    "readiness": _readiness_from_error_code(error_code),
                    "database_error_code": error_code,
                    "database": {"name": "database", "exists": True},
                    "schema_version": API_SCHEMA_VERSION,
                    "api_schema_version": API_SCHEMA_VERSION,
                    "current_database_schema_version": None,
                    "required_database_schema_version": DATABASE_SCHEMA_VERSION,
                    "migration_required": False,
                    "schema_compatible": False,
                    **incomplete_integrity,
                    **access,
                }
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
        schema_error = database_schema_error_code(schema)
        if foreign_keys["foreign_key_violations"]:
            database_error_code = "database_foreign_key_violation"
        elif reader_resource_violations:
            database_error_code = "database_resource_contract_exceeded"
        else:
            database_error_code = schema_error
        readiness = (
            _readiness_from_error_code(database_error_code)
            if database_error_code
            else ("ready_with_data" if conversation_count else "ready_empty")
        )
        return {
            "ok": database_schema_error_code(schema) is None and foreign_keys["foreign_key_violations"] == 0 and reader_resource_violations == 0,
            "db_ready": database_schema_error_code(schema) is None and foreign_keys["foreign_key_violations"] == 0 and reader_resource_violations == 0,
            "readiness": readiness,
            "database_error_code": database_error_code,
            "schema_compatible": schema["schema_compatible"],
            "missing_tables": schema["missing_tables"],
            "missing_columns": schema["missing_columns"],
            "missing_indexes": schema["missing_indexes"],
            "invalid_indexes": schema["invalid_indexes"],
            "invalid_tables": schema["invalid_tables"],
            "object_type_mismatches": schema["object_type_mismatches"],
            "missing_triggers": schema["missing_triggers"],
            "invalid_triggers": schema["invalid_triggers"],
            "missing_generation_rows": schema["missing_generation_rows"],
            "invalid_generation_rows": schema["invalid_generation_rows"],
            "missing_foreign_keys": schema["missing_foreign_keys"],
            "migration_required": schema["migration_required"],
            "current_database_schema_version": schema["current_database_schema_version"],
            "required_database_schema_version": schema["required_database_schema_version"],
            "database": {"name": "database", "exists": db_path.exists()},
            "schema_version": API_SCHEMA_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "display_text_resolver_version": DISPLAY_TEXT_RESOLVER_VERSION,
            "normalization_index_format_version": NORMALIZATION_INDEX_FORMAT_VERSION,
            "optional_web_index_format_version": WEB_INDEX_FORMAT_VERSION,
            "fts5_available": fts5,
            **fts_status,
            "trigram_available": trigram,
            "integrity_mode": "deep" if deep else "quick",
            "foreign_key_check_last_completed_at": foreign_key_checked_at,
            "foreign_key_check_connection_data_version": foreign_key_connection_data_version,
            "result_stale": not bool(foreign_keys["foreign_key_check_complete"]),
            "reader_resource_contract_checked": deep,
            "reader_resource_contract_exact": deep,
            "reader_resource_contract_violations": reader_resource_violations,
            "reader_resource_contract_limit_nodes_per_conversation": MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION,
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
        except ValueError as exc:
            if str(exc) in {"database_not_found", "database_not_ready"}:
                return _empty_stats(db_ready=False)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            conn.execute("BEGIN")
            try:
                require_current_database_schema(conn)
            except DatabaseMigrationError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=_schema_error_detail(exc.detail, code=exc.code),
                ) from exc
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
            if conn.in_transaction:
                conn.rollback()
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
        effective_reader_budget = reader_budget()
        return {
            "version": API_SCHEMA_VERSION,
            "versions": {
                "api_schema_version": API_SCHEMA_VERSION,
                "required_database_schema_version": DATABASE_SCHEMA_VERSION,
                "optional_web_index_format_version": WEB_INDEX_FORMAT_VERSION,
                "display_text_resolver_version": DISPLAY_TEXT_RESOLVER_VERSION,
                "normalization_index_format_version": NORMALIZATION_INDEX_FORMAT_VERSION,
            },
            "pagination": {
                "conversation_page": ["items", "total", "limit", "offset", "has_more", "next_offset"],
                "message_page": ["items", "total", "limit", "offset", "has_more", "next_offset"],
                "message_search_page": ["items", "total", "total_exact", "limit", "offset", "has_more", "next_offset"],
                "total_exact": "true means total is an exact count; false means total is only the known lower bound from the current page probe",
            },
            "id_addressing": {
                "primary": "query-based by-id endpoints; URLSearchParams encoding is reversible and unambiguous for slash, percent, question mark, hash, colon, and Unicode IDs",
                "legacy_id_max_chars": MAX_LEGACY_ID_PARAM_LENGTH,
                "new_import_id_max_chars": MAX_CANONICAL_ID_LENGTH,
                "endpoints": ["/api/by-id/conversation", "/api/by-id/messages", "/api/by-id/raw", "/api/by-id/display", "/api/by-id/copy", "/api/by-id/export"],
                "legacy_path_routes": "retained only for route-safe IDs up to the new-import limit",
                "incompatible_legacy_data": "IDs above the bounded by-id limit make health/readiness fail with database_data_incompatible; unusable rows are never listed as ready",
            },
            "conversations": {
                "endpoint": "/api/conversations",
                "detail_endpoint": "/api/by-id/conversation?conversation_id=...",
                "filters": ["q", "sort", "after", "before", "role", "title", "scope", "exact", "exclude", "source", "path", "match_mode"],
                "limits": {"q": 500, "title": 200, "exact": 300, "exclude": 200, "source": 200, "after": MAX_DATE_PARAM_LENGTH, "before": MAX_DATE_PARAM_LENGTH, "selected_id": MAX_LEGACY_ID_PARAM_LENGTH},
                "path": ["current", "all"],
                "match_mode": ["contains", "word"],
                "date": "after/before use UTC calendar days as YYYY-MM-DD; before is exclusive (next-day 00:00:00 UTC)",
                "selection": ["selected_id", "selected_in_results", "selected_item"],
                "response": ["total", "items", "has_more", "next_offset", "selected_in_results", "selected_item", "node_count", "current_path_nodes", "current_node_exists", "current_collection_source", "current_path_fallback_to_all", "effective_path", "cycle_detected", "missing_parent", "cross_conversation_parent", "partial_chain", "raw_flag_leaf_count", "selected_chain_cycle_detected", "raw_flag_cycle_detected", "selected_chain_missing_parent", "raw_flag_missing_parent", "selected_chain_cross_conversation_parent", "raw_flag_cross_conversation_parent"],
                "search_item_fields": ["hit_count", "snippets", "reasons", "message_match", "title_match", "has_title_hits", "has_internal_hits", "has_branch_hits", "enrichment_partial"],
                "diagnostics": "best-effort search diagnostics; see search.diagnostics",
            },
            "messages": {
                "endpoint": "/api/by-id/messages?conversation_id=...",
                "path": ["current", "all"],
                "include_internal": "boolean; default false for reader pages so pagination is over visible messages, true includes root/internal/technical nodes",
                "filters": ["q", "after", "before", "role", "title", "scope", "exact", "exclude", "source", "match_mode"],
                "limits": {"conversation_id": MAX_LEGACY_ID_PARAM_LENGTH, "around_node_id": MAX_LEGACY_ID_PARAM_LENGTH, "q": 500, "title": 200, "exact": 300, "exclude": 200, "source": 200, "after": MAX_DATE_PARAM_LENGTH, "before": MAX_DATE_PARAM_LENGTH, "offset": MAX_SQLITE_OFFSET},
                "raw": "message pages return raw_preview only; capped raw preview is available per message endpoint",
                "item_fields": ["node_id", "parent_node_id", "message_id", "role", "author_name", "create_time", "update_time", "content_type", "display_text", "display_text_truncated", "display_text_total_chars", "display_text_total_chars_exact", "display_text_resolver_input_truncated", "display_text_returned_chars", "has_text", "has_raw", "raw_preview", "raw_preview_truncated", "content_hash", "is_on_current_path", "effective_visible_in_current_view", "is_internal", "is_empty_mapping_node", "highlight_ranges", "highlight_ranges_truncated", "highlight_scanned_chars", "highlight_range_limit_reached"],
                "display_text_contract": "display_text is the bounded reader preview of the resolved user-visible body; total_chars_exact=false can mean a normal cursor-expandable canonical body, while display_text_resolver_input_truncated=true means raw fallback cannot be completely recovered; use the display chunk endpoint for explicit expansion and copy",
                "budgets": {
                    "message_display_chars": effective_reader_budget.message_display_chars,
                    "page_display_chars": effective_reader_budget.page_display_chars,
                    "page_raw_preview_chars": effective_reader_budget.page_raw_preview_chars,
                    "page_raw_resolver_chars": effective_reader_budget.page_raw_resolver_chars,
                    "page_estimated_serialized_bytes": effective_reader_budget.page_estimated_serialized_bytes,
                    "page_highlight_scan_chars": effective_reader_budget.page_highlight_scan_chars,
                    "display_chunk_chars": effective_reader_budget.display_chunk_chars,
                    "env": dict(_READER_BUDGET_ENV),
                },
                "page_budget_fields": ["page_text_budget_exhausted", "page_preview_budget_exhausted", "page_highlight_budget_exhausted", "response_budget_estimated", "response_budget_limit", "response_budget_estimate_exhausted"],
                "display_endpoint": "/api/by-id/display?conversation_id=...&node_id=...&offset=0&limit=65536",
                "display_cursor": "next_cursor is a revision-bound opaque sequential cursor. Search hits may also return display_anchor_cursor, bound to the canonical source field, row identity, row revision, source code-point position, and direct UTF-8 byte seek; stale revisions return display_cursor_stale (409). Numeric offset is a compatibility scan capped at 1048576 characters and larger values return display_cursor_required (409)",
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
                "endpoint": "/api/by-id/raw?conversation_id=...&node_id=...",
                "max_chars": "1-200000, default 50000; when the bounded UTF-8 preview cannot cover the full value, returns truncated raw_text; max_chars=0 is rejected",
                "response": ["raw_message", "raw_text", "raw_size", "raw_size_unit", "raw_size_exact", "raw_size_chars", "raw_size_chars_exact", "raw_size_bytes", "raw_size_bytes_exact", "parsed", "incomplete", "error_code", "truncated"],
                "units": "raw_size is always exact UTF-8 bytes for compatibility; raw_size_chars is characters and is null with raw_size_chars_exact=false when a truncated byte prefix cannot establish the total",
                "truncated": "when true, use raw_text as plain preview; raw_message is a compat field in truncated mode",
            },
            "export": {
                "endpoint": "/api/by-id/export?conversation_id=...",
                "format": ["md", "txt"],
                "path": ["current", "all"],
                "include_internal": "boolean; default false, matching CLI export and the visible reader; true includes internal/technical nodes",
                "copy_endpoint": "/api/by-id/copy?conversation_id=...",
                "streaming": "export and full-conversation copy use complete canonical display text from bounded server-side node batches and never accumulate reader page payloads",
                "snapshot": {
                    "consistency": "one SQLite read snapshot is held from schema/capability checks through the final streamed byte and is released on completion, error, or client disconnect",
                    "wal_operational_limit": "a long reader can delay WAL checkpoint progress; WAL size, CPU, VM work, temporary disk, and duration remain proportional to the selected data",
                },
                "resource_limits": {
                    "max_nodes_per_conversation": MAX_EXPORT_NODES_PER_CONVERSATION,
                    "max_node_input_bytes": MAX_EXPORT_NODE_INPUT_BYTES,
                    "max_conversation_input_bytes": MAX_EXPORT_CONVERSATION_INPUT_BYTES,
                    "max_output_bytes": MAX_EXPORT_OUTPUT_BYTES,
                    "effective_current_max_nodes_per_conversation": MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION,
                    "effective_current_max_graph_bytes_per_conversation": MAX_EFFECTIVE_CURRENT_GRAPH_BYTES_PER_CONVERSATION,
                    "effective_current_max_scope_nodes": MAX_EFFECTIVE_CURRENT_SCOPE_NODES,
                    "effective_current_max_scope_input_bytes": MAX_EFFECTIVE_CURRENT_SCOPE_INPUT_BYTES,
                    "effective_current_max_temp_bytes": MAX_EFFECTIVE_CURRENT_TEMP_BYTES,
                    "effective_current_max_conversations": MAX_EFFECTIVE_CURRENT_CONVERSATIONS,
                    "effective_current_batch_rows": EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS,
                    "effective_current_batch_nodes": EFFECTIVE_CURRENT_SCOPE_BATCH_NODES,
                    "effective_current_batch_input_bytes": EFFECTIVE_CURRENT_SCOPE_BATCH_INPUT_BYTES,
                    "archive_max_conversations": MAX_ARCHIVE_EXPORT_CONVERSATIONS,
                    "archive_max_plan_metadata_bytes": MAX_ARCHIVE_EXPORT_METADATA_BYTES,
                    "archive_max_manifest_bytes": MAX_ARCHIVE_EXPORT_MANIFEST_BYTES,
                    "archive_planning": "one keyset conversation scan into a same-output-directory temporary SQLite plan; filenames, hashes, and manifests are streamed without an archive-sized Python collection",
                    "browser_copy": "16 MiB UTF-8 and 8 Mi characters; over-limit copy is aborted before clipboard write and the UI directs users to Download",
                },
            },
            "web_index_build": {
                "command": "python chatgpt_archive.py web-index --db <archive.db>",
                "stages": list(WEB_INDEX_BUILD_STAGES),
                "progress": ["build_stage", "processed", "processed_rows", "total", "complete", "batch_size", "processed_input_bytes", "processed_normalized_bytes", "current_batch_input_bytes", "current_batch_normalized_bytes", "current_batch_derived_bytes", "peak_batch_input_bytes", "peak_batch_normalized_bytes", "peak_batch_derived_bytes", "oversized_rows", "cancel_requested"],
                "bounded": {"row_keyset": True, "max_input_bytes": WEB_INDEX_MAX_INPUT_BYTES, "max_normalized_bytes": WEB_INDEX_MAX_NORMALIZED_BYTES, "max_derived_bytes": WEB_INDEX_MAX_DERIVED_BYTES, "fts_bind_batch_bytes": WEB_INDEX_FTS_BIND_BATCH_BYTES, "oversized_recall": "web_index_oversized rows are unioned into candidates and verified against canonical text"},
                "publication": "per-build uniquely named staging objects are built in bounded committed batches; a short BEGIN IMMEDIATE transaction rechecks canonical generations and exact object ownership, replaces the previous optional index by atomic renames, validates metadata, and commits; publication failure rolls back the rename transaction and retains the previous index",
                "cancellation": "a running import job exposes POST /api/import/jobs/{job_id}/web-index/cancel; the internal callback and SQLite progress handler remove private staging objects and retain the previous optional index",
                "locking": "a persistent owner-token lease admits one builder per database and returns web_index_build_in_progress to contenders; normalization and trigram batches release the writer lock between commits; other writers are blocked only by an active bounded batch and the final generation-check/publish transaction",
                "ownership_errors": ["core_fts_name_collision", "optional_index_name_collision", "staging_name_collision", "web_index_build_in_progress"],
            },
            "search": {
                "endpoints": ["/api/conversations", "/api/search/messages"],
                "parameters": ["q", "title", "exact", "exclude", "role", "source", "after", "before", "scope", "path", "match_mode", "order", "conversation_id", "count_total", "continuation"],
                "exact_verify": {"effective_chars": search_exact_verify_limits()[0], "effective_utf8_bytes": search_exact_verify_limits()[1], "opt_in_env": SEARCH_EXACT_VERIFY_ENV, "max_opt_in_chars": SEARCH_EXACT_VERIFY_MAX_OPT_IN_CHARS},
                "message_order": ["relevance", "display"],
                "count_total": "boolean; false disables the exact count and returns total_exact=false with a known lower-bound total",
                "message_resource_contract": {
                    "candidate_scan_chars_per_row": SEARCH_CANDIDATE_SCAN_CHARS,
                    "hit_preview_chars": SEARCH_HIT_PREVIEW_CHARS,
                    "snippet_scan_chars": SEARCH_SNIPPET_SCAN_CHARS,
                    "response_estimated_bytes": SEARCH_PAGE_ESTIMATED_BYTES,
                    "stream_chunk_bytes": SEARCH_STREAM_CHUNK_BYTES,
                    "request_verify_bytes": SEARCH_REQUEST_VERIFY_BYTES,
                    "request_verify_chars": SEARCH_REQUEST_VERIFY_CHARS,
                    "raw_fallback_bytes_per_row": SEARCH_RAW_EXACT_MAX_BYTES,
                    "raw_fallback_chars_per_row": SEARCH_RAW_EXACT_MAX_CHARS,
                    "candidate_limit": SEARCH_CANDIDATE_LIMIT,
                    "wall_deadline_seconds": SEARCH_WALL_DEADLINE_SECONDS,
                    "exact_verifier": "candidate rows are verified once into a connection-local TEMP result artifact through bounded incremental BLOB reads; count, page, payload, snippet, source span, and byte anchor reuse that artifact. Candidate, decoded-character, UTF-8 byte, raw-fallback, request-aggregate, SQLite VM, wall, and response ceilings produce an explicit partial page rather than erasing confirmed hits",
                    "continuation": "a signed opaque candidate cursor binds database identity, schema and optional-index format, durable generations, the full query/filter/path/scope/match/order/page contract, cumulative confirmed/pending counts, and the budget-contract version; query or database changes return search_continuation_stale",
                    "resource_error_codes": ["search_page_exact_materialization_limit", "search_response_resource_limit_exceeded", "invalid_search_continuation", "search_continuation_stale"],
                },
                "filter_only": "filter-only and exclude-only queries may filter conversation results; message hits and reader highlights require a positive message-text term, and role/source/date filters alone do not create hit navigation",
                "raw_query_override": "path: and scope: modifiers in q override sidebar path/scope selectors",
                "current_path_candidates": "global path=current searches derive path-independent conversation candidates first and materialize effective-current only for that scope; exclusion-only queries may require the explicit full-database fallback",
                "hit_navigation": "the reader requests one initial compact page with count_total=false and appends lazily near the loaded boundary, capped at 1000 navigable hits",
                "sqlite_query_shape": "portable non-flattening CTEs keep display-text resolution once per candidate stage without requiring AS MATERIALIZED",
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
                        "partial",
                        "partial_reason",
                        "verified_chars_per_candidate",
                        "verified_bytes_per_candidate",
                        "oversized_candidates_seen",
                        "oversized_candidates_verified",
                        "oversized_candidates_pending",
                        "candidate_count",
                        "candidate_limit",
                        "resolver_calls",
                        "blob_reads",
                        "candidate_blob_bytes",
                        "raw_blob_bytes",
                        "decoded_chars",
                        "normalization_units",
                        "sqlite_vm_steps",
                        "wall_seconds",
                        "continuation_available",
                        "continuation_token",
                        "completion_state",
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
                    "single_value_headers": ["Origin", "Content-Length", "Sec-Fetch-Site"],
                    "content_length": "canonical nonnegative ASCII decimal with at most 20 digits; duplicates and alternate numeric syntax are rejected before multipart parsing",
                    "forwarded_headers": "strict edge-proxy model: ignored from untrusted peers; a trusted direct edge must overwrite client values and provide at most one Forwarded element or one X-Forwarded-Host/Proto value; duplicates, chains, malformed syntax, and conflicts are rejected",
                },
                "limits_note": "ZIP size checks run before import; JSON parsing and SQLite writes still consume memory, disk, and CPU proportional to decoded conversation JSON size; one top-level JSON element is independently capped at 32 MiB of UTF-8 input, 32 Mi decoded characters, and 5000 mapping nodes.",
                "zip64": "Python zipfile and this pipeline accept ZIP64 structures, including forced-ZIP64 members; a physical archive above 4 GiB is not part of the regular acceptance suite and remains subject to every configured member, byte, ratio, disk, and CPU limit.",
            },
            "jobs": {
                "endpoints": ["/api/import/jobs", "/api/import/jobs/{job_id}", "/api/import/jobs/{job_id}/web-index/cancel"],
                "job_id": "lowercase 32-character hexadecimal UUID; invalid syntax returns invalid_job_id and a valid unknown ID returns job_not_found",
                "statuses": ["queued", "running", "succeeded", "failed", "postcheck_failed"],
                "outcomes": ["queued", "import_running", "import_job_start_failed", "input_preflight_failed", "source_scan_failed", "source_read_failed", "json_decode_failed", "top_level_contract_failed", "import_transaction_failed", "canonical_commit_succeeded", "verify_failed", "stats_failed", "web_index_failed", "web_index_cancelled", "succeeded"],
                "fields": ["status", "stage", "outcome", "canonical_commit_succeeded", "error_code", "error_type", "cleanup_warning", "cleanup_warnings", "summary", "verify", "stats", "web_index", "web_index_cancel_requested", "web_index_cancelled"],
                "web_index_progress": ["status", "build_stage", "processed", "total", "complete", "batch_size", "processed_input_bytes", "processed_normalized_bytes", "current_batch_input_bytes", "current_batch_normalized_bytes", "current_batch_derived_bytes", "peak_batch_input_bytes", "peak_batch_normalized_bytes", "peak_batch_derived_bytes", "oversized_rows"],
                "web_index_cancellation": "the cancel endpoint is accepted only while the import job is in web-index or web-index-recovery; cancellation rolls back staging objects and keeps the previous optional index readable",
                "failure_codes": ["import_job_start_failed", "upload_preflight_failed", "upload_disk_space_insufficient", "no_conversation_sources", "ambiguous_conversation_sources", "source_scan_failed", "source_member_limit_exceeded", "input_source_open_failed", "input_source_not_regular_file", "source_read_failed", "source_changed_during_read", "encrypted_zip_member_not_supported", "zip_member_not_found", "zip_member_crc_failed", "zip_member_read_failed", "invalid_conversation_encoding", "json_integer_too_large", "json_nesting_limit_exceeded", "json_scalar_limit_exceeded", "conversation_json_element_too_large", "conversation_node_limit_exceeded", "invalid_conversation_json", "non_finite_json_number", "conversation_json_top_level_not_list", "canonical_id_empty", "import_disk_space_insufficient", "import_transaction_failed", "verify_failed", "stats_failed", "web_index_disk_space_insufficient", "web_index_failed"],
                "cleanup_warnings": {"item_fields": ["code", "error_type", "path_kind"], "codes": ["summary_update_after_commit_failed", "import_connection_close_failed", "summary_update_after_close_failed", "upload_file_unlink_failed", "upload_directory_cleanup_failed", "upload_directory_cleanup_incomplete", "web_index_staging_cleanup_failed"]},
                "preflight_cleanup_error": ["code", "cleanup_warning", "cleanup_error_type", "cleanup_warnings"],
            },
            "disk_capacity": {
                "preflight_stages": ["upload", "canonical_import", "optional_web_index"],
                "runtime_checks": True,
                "reserve_bytes": 268435456,
                "error_codes": ["upload_disk_space_insufficient", "import_disk_space_insufficient", "web_index_disk_space_insufficient"],
                "contract": "capacity estimates include an emergency reserve but are not guarantees; filesystem quotas, concurrent writers, WAL, temporary pages, and actual SQLite amplification may still exhaust space, in which case the active transaction or private index build is rolled back",
            },
            "import_contract": {
                "top_level": "conversation JSON must be one array; a single-pass incremental framer scans each element once, checks lexical nesting, then decodes it once; one element is capped at 32 MiB of UTF-8 input and 32 Mi decoded characters",
                "max_element_utf8_bytes": MAX_JSON_ELEMENT_BYTES,
                "max_element_decoded_chars": MAX_JSON_ELEMENT_CHARS,
                "max_nodes_per_conversation": MAX_IMPORT_NODES_PER_CONVERSATION,
                "legacy_reader_export_max_nodes_per_conversation": MAX_EXPORT_NODES_PER_CONVERSATION,
                "node_limit_scope": "the 5000-node ceiling is an independent new-import validation limit; the 100000-node reader/export ceiling exists only for legacy or externally written compatible databases and is not an import promise",
                "encoding": "UTF-8 only; exactly one file-leading UTF-8 BOM is removed; repeated or JSON-outside-string U+FEFF, UTF-16/32, mixed, and invalid encodings are rejected, while U+FEFF inside a JSON string is preserved",
                "canonical_id_max_chars": MAX_CANONICAL_ID_LENGTH,
                "max_source_total_members": MAX_SOURCE_TOTAL_MEMBERS,
                "json_limits": {
                    "max_nesting_depth": MAX_JSON_NESTING_DEPTH,
                    "max_conversation_element_scalar_count": MAX_CONVERSATION_JSON_SCALARS,
                    "max_legacy_sanitizer_scalar_count": MAX_JSON_SCALAR_COUNT,
                    "max_raw_preview_nodes": MAX_RAW_PREVIEW_NODES,
                    "max_raw_preview_bytes": MAX_RAW_PREVIEW_BYTES,
                    "max_sanitized_output_bytes": MAX_SANITIZED_OUTPUT_BYTES,
                },
                "id_fields": ["conversation_id", "exported_conversation_id", "mapping_node_key", "node_id", "message_id", "current_node", "parent", "children"],
                "overlong_id": "the conversation element is skipped with canonical_id_too_long; IDs are never truncated",
                "zip_source_read_codes": ["encrypted_zip_member_not_supported", "zip_member_not_found", "source_changed_during_read", "zip_member_crc_failed", "zip_member_read_failed"],
                "zip64": {"runtime_supported": True, "small_forced_fixture_tested": True, "physical_over_4_gib_acceptance_tested": False},
            },
            "ui_state": {
                "canonical_copy_url": ["match_mode", "layout", "show_internal", "sort", "path", "scope", "q", "role", "title", "exact", "exclude", "source", "after", "before", "selected conversation"],
                "url_precedence": "explicit URL values win over localStorage; missing values may use local settings",
                "back_forward": "incremental search and selection history restoration is not implemented; routine address-bar updates use replaceState",
            },
            "database_compatibility": {
                "readonly_contract": "health and read endpoints inspect schema but never execute migration DDL",
                "migration": "run the explicit CLI migrate command after creating and verifying an external backup; import initializes new databases and upgrades migratable databases, while web-index requires a current core schema",
                "health_fields": ["integrity_mode", "readiness", "database_error_code", "schema_compatible", "migration_required", "current_database_schema_version", "required_database_schema_version", "foreign_key_violations", "foreign_key_violations_exact", "foreign_key_check_complete", "foreign_key_check_last_completed_at", "foreign_key_check_connection_data_version", "result_stale", "reader_resource_contract_checked", "reader_resource_contract_exact", "reader_resource_contract_violations", "reader_resource_contract_limit_nodes_per_conversation"],
                "foreign_key_check": "GET /api/health is a bounded quick schema gate and does not run PRAGMA foreign_key_check; GET /api/health?deep=true and CLI verify stream the complete check and retain bounded samples",
                "effective_current_verify_counters": {
                    "unit": "conversation count",
                    "selected_chain": ["selected_chain_cycles", "missing_parent_in_selected_chain", "cross_conversation_parent_in_selected_chain", "partial_selected_chain"],
                    "raw_flag_topology": ["raw_flag_cycles", "missing_parent_in_raw_flag_topology", "cross_conversation_parent_in_raw_flag_topology", "partial_raw_flag_topology"],
                    "aggregate": ["cycle_detected"],
                },
                "readiness_states": ["database_missing_or_uninitialized", "migration_required", "schema_newer", "schema_incompatible", "data_incompatible", "foreign_key_violation", "database_malformed", "database_locked", "database_readonly_or_io", "ready_empty", "ready_with_data"],
                "errors": ["database_not_ready", "database_migration_required", "database_schema_unsupported_predecessor", "database_custom_objects_require_manual_migration", "database_schema_newer", "database_schema_incompatible", "database_data_incompatible", "database_foreign_key_violation"],
                "optional_fts_fields": ["message_fts_available", "message_fts_rebuildable", "message_fts_error", "optional_message_fts_error", "optional_message_fts_recovery_hint"],
            },
            "request_validation": {
                "http_status": 422,
                "detail_code": "invalid_request",
                "max_errors": 16,
                "item_fields": ["location", "field", "code"],
                "locations": ["query", "path", "body", "header", "cookie", "request"],
                "codes": [
                    "invalid_request",
                    "invalid_integer",
                    "numeric_parameter_out_of_range",
                    "invalid_offset",
                    "invalid_limit",
                    "string_parameter_too_long",
                    "invalid_identifier_token",
                    "missing_parameter",
                    "invalid_enum_value",
                    "invalid_body",
                    "invalid_upload_metadata",
                ],
                "privacy": "never returns submitted input, request bodies, raw query/path values, Pydantic reprs, paths, OS messages, or tracebacks",
            },
            "stable_error_codes": [
                "database_not_ready", "database_migration_required", "database_schema_newer", "database_schema_incompatible", "database_foreign_key_violation", "invalid_job_id", "job_not_found",
                "database_malformed", "database_locked", "database_readonly", "database_io_error", "database_runtime_failure",
                "conversation_not_found", "message_not_found", "invalid_export_format", "invalid_display_cursor", "display_cursor_stale", "display_cursor_required",
                "export_node_count_limit_exceeded", "export_node_input_limit_exceeded", "export_input_byte_limit_exceeded", "export_header_input_limit_exceeded", "export_output_byte_limit_exceeded", "effective_current_node_limit_exceeded", "effective_current_input_limit_exceeded",
                "invalid_request", "invalid_integer", "numeric_parameter_out_of_range", "invalid_offset", "invalid_limit", "string_parameter_too_long", "invalid_identifier_token", "missing_parameter", "invalid_enum_value", "invalid_body", "invalid_upload_metadata",
                "invalid_sort", "invalid_scope", "invalid_role", "invalid_path",
                "invalid_match_mode", "invalid_message_order", "invalid_query",
                "host_not_allowed", "invalid_host_header", "invalid_forwarded_headers", "import_job_active", "import_job_start_failed", "upload_preflight_failed", "upload_disk_space_insufficient", "upload_origin_required", "upload_origin_not_allowed", "upload_content_length_required", "upload_duplicate_origin_header", "upload_duplicate_content_length", "upload_duplicate_sec_fetch_site",
                "upload_invalid_content_length", "upload_multipart_body_too_large", "upload_too_large",
                "uploaded_file_not_zip", "uploaded_file_invalid_zip", "upload_zip_no_conversation_sources",
                "upload_zip_ambiguous_conversation_sources", "upload_zip_too_many_members",
                "upload_zip_too_many_json_members", "upload_zip_member_too_large",
                "upload_zip_uncompressed_too_large", "upload_zip_compression_ratio_too_high",
                "no_conversation_sources", "ambiguous_conversation_sources", "source_scan_failed", "input_source_open_failed", "input_source_not_regular_file", "source_read_failed", "source_changed_during_read",
                "invalid_conversation_encoding", "json_integer_too_large", "conversation_json_element_too_large", "conversation_node_limit_exceeded", "invalid_conversation_json", "non_finite_json_number", "conversation_json_top_level_not_list", "canonical_id_invalid_unicode", "delete_input_changed",
                "import_disk_space_insufficient", "import_transaction_failed", "verify_failed", "stats_failed", "web_index_disk_space_insufficient", "web_index_failed", "web_index_cancelled", "web_index_not_cancellable",
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
        offset: Annotated[int, Query(ge=0, le=MAX_SQLITE_OFFSET)] = 0,
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
        selected_id: Annotated[str | None, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)] = None,
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
        upload_file_closed = False
        primary_http_error: HTTPException | None = None
        filename = file.filename or "upload.zip"
        try:
            if not filename.lower().endswith(".zip"):
                raise HTTPException(status_code=400, detail="uploaded_file_not_zip")
            upload_dir, upload_path = make_upload_path()
            announced_length = int(request.headers.get("content-length", "0"))
            require_free_space(
                upload_dir,
                upload_required_bytes(announced_length),
                "upload_disk_space_insufficient",
            )
            disk_guard = DiskSpaceGuard(upload_dir, "upload_disk_space_insufficient")
            size = 0
            with upload_path.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > policy.max_upload_bytes:
                        raise HTTPException(status_code=413, detail="upload_too_large")
                    written = out.write(chunk)
                    if written != len(chunk):
                        raise OSError("short upload write")
                    disk_guard.check(advanced_bytes=written)
            await file.close()
            upload_file_closed = True
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
        except DiskSpaceInsufficientError as exc:
            primary_http_error = HTTPException(status_code=507, detail=exc.code)
            raise primary_http_error from exc
        except Exception as exc:
            if is_disk_full_error(exc):
                primary_http_error = HTTPException(
                    status_code=507,
                    detail="upload_disk_space_insufficient",
                )
                raise primary_http_error from exc
            primary_http_error = HTTPException(
                status_code=500,
                detail={"code": "upload_preflight_failed", "error_type": type(exc).__name__},
            )
            raise primary_http_error from exc
        finally:
            if not transferred:
                manager.release_pending_upload_slot()
                if not upload_file_closed:
                    try:
                        await file.close()
                    except Exception as exc:
                        if primary_http_error is not None:
                            detail = (
                                dict(primary_http_error.detail)
                                if isinstance(primary_http_error.detail, dict)
                                else {"code": str(primary_http_error.detail)}
                            )
                            detail.update(
                                {
                                    "cleanup_warning": "upload_file_close_failed",
                                    "cleanup_error_type": type(exc).__name__,
                                    "cleanup_warnings": [
                                        {
                                            "code": "upload_file_close_failed",
                                            "error_type": type(exc).__name__,
                                            "path_kind": "upload_file",
                                        }
                                    ],
                                }
                            )
                            primary_http_error.detail = detail
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
                                    "cleanup_warnings": [
                                        *list(detail.get("cleanup_warnings", [])),
                                        {
                                            "code": "temporary_upload_cleanup_failed",
                                            "error_type": cleanup["error_type"] or "PathStillExists",
                                            "path_kind": "upload_directory",
                                        },
                                    ],
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

    @router.post("/import/jobs/{job_id}/web-index/cancel")
    def cancel_import_web_index(job_id: str):
        if JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise HTTPException(status_code=400, detail="invalid_job_id")
        job, accepted = manager.request_web_index_cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        if not accepted:
            raise HTTPException(status_code=409, detail="web_index_not_cancellable")
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
        offset: Annotated[int, Query(ge=0, le=MAX_SQLITE_OFFSET)] = 0,
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

    @router.get("/conversations/{conversation_id}/messages/{node_id}/display")
    def conversation_message_display(
        conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)],
        node_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)],
        offset: Annotated[int, Query(ge=0, le=MAX_SQLITE_OFFSET)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_048_576)] = 65_536,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        anchor_char_offset: Annotated[int | None, Query(ge=0, le=100 * 1024 * 1024)] = None,
        conn=Depends(get_conn),
    ):
        try:
            item = get_message_display_chunk(
                conn, conversation_id, node_id, offset=offset, limit=limit, cursor=cursor,
                anchor_char_offset=anchor_char_offset,
            )
        except DisplayCursorError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        if item is None:
            raise HTTPException(status_code=404, detail="message_not_found")
        return item

    @router.get("/conversations/{conversation_id}/messages/{node_id}/raw")
    def conversation_message_raw(
        conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)],
        node_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)],
        max_chars: int = Query(default=50000, ge=1, le=200000, alias="max_chars"),
        conn=Depends(get_conn),
    ):
        max_bytes = max_chars * 4 + 4
        row = conn.execute(
            """SELECT rowid AS storage_rowid, raw_message_json IS NULL AS raw_is_null
               FROM conversation_nodes WHERE conversation_id = ? AND node_id = ?""",
            (conversation_id, node_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="message_not_found")
        if bool(row["raw_is_null"]):
            raw_size_bytes = 0
            prefix_bytes = b""
        else:
            with conn.blobopen(
                "conversation_nodes", "raw_message_json", int(row["storage_rowid"]), readonly=True
            ) as blob:
                raw_size_bytes = len(blob)
                prefix_bytes = blob.read(min(raw_size_bytes, max_bytes))
        if raw_size_bytes == 0:
            return {
                "conversation_id": conversation_id, "node_id": node_id,
                "raw_message": None, "raw_size": 0, "raw_size_exact": True,
                "raw_size_unit": "bytes",
                "raw_size_chars": 0, "raw_size_chars_exact": True,
                "raw_size_bytes": 0, "raw_size_bytes_exact": True,
                "parsed": True, "incomplete": False, "truncated": False,
            }
        decoded = normalize_display_text(prefix_bytes.decode("utf-8", errors="replace"))
        truncated = raw_size_bytes > len(prefix_bytes) or len(decoded) > max_chars
        raw_text = decoded[:max_chars]
        if truncated:
            return {
                "conversation_id": conversation_id,
                "node_id": node_id,
                "raw_message": raw_text,
                "raw_text": raw_text,
                "raw_size": raw_size_bytes,
                "raw_size_exact": True,
                "raw_size_unit": "bytes",
                "raw_size_chars": None,
                "raw_size_chars_exact": False,
                "raw_size_bytes": raw_size_bytes,
                "raw_size_bytes_exact": True,
                "parsed": False,
                "incomplete": True,
                "truncated": True,
            }
        try:
            validate_json_lexical_limits(raw_text or "null")
            raw = _safe_json_scalars(json.loads(raw_text or "null"))
        except JsonSafetyLimitError as exc:
            return {
                "conversation_id": conversation_id,
                "node_id": node_id,
                "raw_message": raw_text[: min(max_chars, MAX_RAW_PREVIEW_BYTES)],
                "raw_text": raw_text[: min(max_chars, MAX_RAW_PREVIEW_BYTES)],
                "raw_size": raw_size_bytes,
                "raw_size_exact": True,
                "raw_size_unit": "bytes",
                "raw_size_chars": len(raw_text),
                "raw_size_chars_exact": True,
                "raw_size_bytes": raw_size_bytes,
                "raw_size_bytes_exact": True,
                "parsed": False,
                "incomplete": True,
                "error_code": exc.code,
                "limit": exc.limit,
                "truncated": len(raw_text) > min(max_chars, MAX_RAW_PREVIEW_BYTES),
            }
        except (json.JSONDecodeError, RecursionError):
            raw = raw_text
        return {
            "conversation_id": conversation_id, "node_id": node_id,
            "raw_message": raw, "raw_text": raw_text,
            "raw_size": raw_size_bytes, "raw_size_exact": True,
            "raw_size_unit": "bytes",
            "raw_size_chars": len(raw_text), "raw_size_chars_exact": True,
            "raw_size_bytes": raw_size_bytes, "raw_size_bytes_exact": True,
            "parsed": not isinstance(raw, str), "incomplete": False,
            "truncated": False,
        }

    @router.get("/conversations/{conversation_id}/export")
    def conversation_export(conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)], format: str = "md", path: str = "current", include_internal: bool = False, conn=Depends(get_conn)):
        _validate_common(path=path)
        if format not in {"md", "txt"}:
            raise HTTPException(status_code=400, detail="invalid_export_format")
        conv = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        try:
            validated_budget = validate_conversation_export_budget(
                conn, conversation_id, path=path, include_internal=include_internal
            )
        except ExportResourceLimitError as exc:
            raise HTTPException(status_code=413, detail=exc.code) from None
        media_type = "text/markdown; charset=utf-8" if format == "md" else "text/plain; charset=utf-8"
        filename = _download_filename(conversation_id, format)
        nodes = iter_conversation_export_nodes(
            conn,
            conv,
            path=path,
            include_internal=include_internal,
            validated_budget=validated_budget,
        )
        return StreamingResponse(
            iter_rendered_conversation(conv, nodes, format),
            media_type=media_type,
            headers={"Content-Disposition": _content_disposition(filename)},
        )

    @router.get("/conversations/{conversation_id}/copy")
    def conversation_copy(conversation_id: Annotated[str, ApiPath(max_length=MAX_ID_PARAM_LENGTH)], path: str = "current", include_internal: bool = False, conn=Depends(get_conn)):
        _validate_common(path=path)
        conv = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        try:
            validated_budget = validate_conversation_export_budget(
                conn, conversation_id, path=path, include_internal=include_internal
            )
        except ExportResourceLimitError as exc:
            raise HTTPException(status_code=413, detail=exc.code) from None
        nodes = iter_conversation_export_nodes(
            conn,
            conv,
            path=path,
            include_internal=include_internal,
            validated_budget=validated_budget,
        )
        return StreamingResponse(iter_copy_conversation(nodes), media_type="text/plain; charset=utf-8")

    # Query-based by-id routes are the primary unambiguous addressing contract.
    # Legacy path routes above remain for route-safe clients.
    @router.get("/by-id/conversation")
    def conversation_detail_by_id(
        conversation_id: Annotated[str, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)],
        conn=Depends(get_conn),
    ):
        return conversation_detail(conversation_id, conn)

    @router.get("/by-id/messages")
    def conversation_messages_by_id(
        conversation_id: Annotated[str, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)],
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
        offset: Annotated[int, Query(ge=0, le=MAX_SQLITE_OFFSET)] = 0,
        around_node_id: Annotated[str | None, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)] = None,
        include_internal: bool = False,
        conn=Depends(get_conn),
    ):
        return conversation_messages(
            conversation_id, path, q, match_mode, role, title, scope, exact,
            exclude, after, before, source, limit, offset, around_node_id,
            include_internal, conn,
        )

    @router.get("/by-id/display")
    def conversation_message_display_by_id(
        conversation_id: Annotated[str, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)],
        node_id: Annotated[str, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)],
        offset: Annotated[int, Query(ge=0, le=MAX_SQLITE_OFFSET)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_048_576)] = 65_536,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        anchor_char_offset: Annotated[int | None, Query(ge=0, le=100 * 1024 * 1024)] = None,
        conn=Depends(get_conn),
    ):
        return conversation_message_display(
            conversation_id, node_id, offset, limit, cursor, anchor_char_offset, conn
        )

    @router.get("/by-id/raw")
    def conversation_message_raw_by_id(
        conversation_id: Annotated[str, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)],
        node_id: Annotated[str, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)],
        max_chars: int = Query(default=50000, ge=1, le=200000, alias="max_chars"),
        conn=Depends(get_conn),
    ):
        return conversation_message_raw(conversation_id, node_id, max_chars, conn)

    @router.get("/by-id/export")
    def conversation_export_by_id(
        conversation_id: Annotated[str, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)],
        format: str = "md",
        path: str = "current",
        include_internal: bool = False,
        conn=Depends(get_conn),
    ):
        return conversation_export(conversation_id, format, path, include_internal, conn)

    @router.get("/by-id/copy")
    def conversation_copy_by_id(
        conversation_id: Annotated[str, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)],
        path: str = "current",
        include_internal: bool = False,
        conn=Depends(get_conn),
    ):
        return conversation_copy(conversation_id, path, include_internal, conn)

    @router.get("/search")
    def search(
        q: Annotated[str, Query(max_length=500)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=MAX_SQLITE_OFFSET)] = 0,
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
        selected_id: Annotated[str | None, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)] = None,
        conn=Depends(get_optional_conn),
    ):
        _validate_common(sort=sort, scope=scope, role=role, path=path, match_mode=match_mode)
        parsed = parse_query(q, path_default=path, role=role, title=title, scope=scope, exact=exact, exclude=exclude, after=after, before=before, source=source, match_mode=match_mode, enforce_api_limits=True)
        _raise_query_errors(parsed)
        if conn is None:
            return _empty_page(limit, offset, selected_id=selected_id, db_ready=False)
        try:
            return search_conversations(conn, parsed, limit=limit, offset=offset, sort=sort, selected_id=selected_id)
        except SearchResourceLimitError as exc:
            raise HTTPException(status_code=413, detail=exc.code) from exc

    @router.get("/search/messages")
    def search_message_endpoint(
        q: Annotated[str, Query(max_length=500)] = "",
        conversation_id: Annotated[str | None, Query(max_length=MAX_LEGACY_ID_PARAM_LENGTH)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=MAX_SQLITE_OFFSET)] = 0,
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
        continuation: Annotated[str | None, Query(max_length=4096)] = None,
        conn=Depends(get_optional_conn),
    ):
        _validate_common(role=role, path=path, scope=scope, match_mode=match_mode)
        if order not in ALLOWED_MESSAGE_ORDERS:
            raise HTTPException(status_code=400, detail="invalid_message_order")
        parsed = parse_query(q, path_default=path, role=role, title=title, scope=scope, exact=exact, exclude=exclude, after=after, before=before, source=source, match_mode=match_mode, enforce_api_limits=True)
        _raise_query_errors(parsed)
        if conn is None:
            return _empty_message_search_page(limit, offset, db_ready=False)
        try:
            return search_messages(conn, parsed, limit=limit, offset=offset, conversation_id=conversation_id, order=order, count_total=count_total, continuation=continuation)
        except SearchContinuationError as exc:
            status = 409 if exc.code == "search_continuation_stale" else 400
            raise HTTPException(status_code=status, detail=exc.code) from exc
        except SearchResourceLimitError as exc:
            raise HTTPException(status_code=413, detail=exc.code) from exc

    @router.get("/search/suggest")
    def suggest(q: Annotated[str, Query(max_length=100)] = "", limit: Annotated[int, Query(ge=1, le=20)] = 10, conn=Depends(get_optional_conn)):
        if conn is None:
            return {"items": []}
        normalized = normalize_search_text(q)
        if _has_normalized_title_norm(conn) and normalized:
            rows = conn.execute(
                """
                SELECT c.conversation_id,
                       substr(CAST(COALESCE(c.title, '') AS BLOB), 1, 16388) AS title_prefix,
                       length(CAST(COALESCE(c.title, '') AS BLOB)) AS title_bytes
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
                SELECT conversation_id,
                       substr(CAST(COALESCE(title, '') AS BLOB), 1, 16388) AS title_prefix,
                       length(CAST(COALESCE(title, '') AS BLOB)) AS title_bytes
                FROM conversations
                WHERE ? = '' OR instr(web_normalize(COALESCE(title, '')), ?) > 0
                ORDER BY COALESCE(update_time, create_time, 0) DESC
                LIMIT ?
                """,
                (normalized, normalized, limit),
            ).fetchall()
        items = []
        for row in rows:
            prefix = bytes(row["title_prefix"] or b"").decode("utf-8", errors="replace")
            title = normalize_display_text(prefix)[:4096]
            title_bytes = int(row["title_bytes"] or 0)
            items.append({
                "conversation_id": row["conversation_id"],
                "title": title,
                "title_truncated": title_bytes > len(bytes(row["title_prefix"] or b"")) or len(prefix) > 4096,
                "title_length": len(prefix) if title_bytes <= len(bytes(row["title_prefix"] or b"")) else None,
                "title_bytes": title_bytes,
            })
        return {"items": items}

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
        with path.open("rb") as stream:
            preflight_zip_central_directory(stream, max_members=policy.max_total_members)
        with zipfile.ZipFile(path) as zf:
            all_infos = zf.infolist()
            file_infos = [info for info in all_infos if not info.is_dir()]
            total_members = len(all_infos)
            source_infos = [info for info in file_infos if not is_metadata_path(info.filename)]
    except ValueError as exc:
        if str(exc) == "source_member_limit_exceeded":
            raise HTTPException(status_code=413, detail="upload_zip_too_many_members") from exc
        raise HTTPException(status_code=400, detail="uploaded_file_invalid_zip") from exc
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


def _safe_json_scalars(value):
    """Make a bounded raw preview safe for the API JSON encoder."""
    return sanitize_json_value(value)


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


def _readiness_from_error_code(code: str | None) -> str:
    return {
        "database_not_found": "database_missing_or_uninitialized",
        "database_not_ready": "database_missing_or_uninitialized",
        "database_migration_required": "migration_required",
        "database_schema_newer": "schema_newer",
        "database_schema_incompatible": "schema_incompatible",
        "database_data_incompatible": "data_incompatible",
        "database_foreign_key_violation": "foreign_key_violation",
        "database_resource_contract_exceeded": "resource_contract_exceeded",
        "database_malformed": "database_malformed",
        "database_locked": "database_locked",
        "database_readonly": "database_readonly_or_io",
        "database_io_error": "database_readonly_or_io",
    }.get(code or "", "schema_incompatible")


def _schema_error_detail(
    schema: dict[str, object], *, code: str | None = None
) -> dict[str, object]:
    code = code or database_schema_error_code(schema) or "database_schema_incompatible"
    return {
        "code": code,
        "schema_compatible": False,
        "migration_required": bool(schema.get("migration_required")),
        "current_database_schema_version": schema.get("current_database_schema_version"),
        "required_database_schema_version": schema.get("required_database_schema_version"),
        "missing_tables": schema.get("missing_tables", []),
        "missing_columns": schema.get("missing_columns", {}),
        "missing_indexes": schema.get("missing_indexes", []),
        "invalid_indexes": schema.get("invalid_indexes", {}),
        "invalid_tables": schema.get("invalid_tables", {}),
        "object_type_mismatches": schema.get("object_type_mismatches", {}),
        "missing_triggers": schema.get("missing_triggers", []),
        "invalid_triggers": schema.get("invalid_triggers", {}),
        "missing_generation_rows": schema.get("missing_generation_rows", []),
        "invalid_generation_rows": schema.get("invalid_generation_rows", {}),
        "missing_foreign_keys": schema.get("missing_foreign_keys", {}),
        "foreign_key_violation": bool(schema.get("foreign_key_violation")),
        "foreign_key_violation_sample": schema.get("foreign_key_violation_sample"),
    }


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
