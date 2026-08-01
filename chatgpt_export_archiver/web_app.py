from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

from .web_api import WebTrustPolicy, create_api_router
from .web_jobs import ImportJobManager
from .sqlite_errors import sqlite_runtime_error_code
from .current_path import EffectiveCurrentResourceLimitError
from .exporter import ExportResourceLimitError
from .json_safety import JsonSafetyLimitError, sanitize_json_value
from .schema_contract import API_SCHEMA_VERSION, OPTIONAL_WEB_INDEX_FORMAT_VERSION
from .web_db import WebIndexBuildError


class _UploadBodyTooLarge(Exception):
    pass


def _json_finite(value):
    return sanitize_json_value(value)


class FiniteJSONResponse(JSONResponse):
    def __init__(self, content=None, *, status_code: int = 200, **kwargs) -> None:
        try:
            safe_content = _json_finite(content)
        except JsonSafetyLimitError:
            safe_content = {"detail": {"code": "response_resource_limit_exceeded"}}
            if status_code < 400:
                status_code = 413
        super().__init__(content=safe_content, status_code=status_code, **kwargs)

    def render(self, content) -> bytes:
        return super().render(content)


_VALIDATION_LOCATIONS = frozenset({"query", "path", "body", "header", "cookie"})
_IDENTIFIER_FIELDS = frozenset(
    {"conversation_id", "node_id", "around_node_id", "selected_id", "job_id", "cursor"}
)
_LIMIT_FIELDS = frozenset({"limit", "max_chars"})


def _safe_validation_location_and_field(raw_location) -> tuple[str, str]:
    parts = raw_location if isinstance(raw_location, (list, tuple)) else ()
    location = str(parts[0]).casefold() if parts else "request"
    if location not in _VALIDATION_LOCATIONS:
        location = "request"
    field = "parameter"
    for part in reversed(parts[1:]):
        candidate = str(part)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", candidate):
            field = candidate
            break
    return location, field


def _validation_public_code(*, location: str, field: str, error_type: str) -> str:
    kind = error_type.casefold()
    if "missing" in kind:
        return "invalid_upload_metadata" if location == "body" and field == "file" else "missing_parameter"
    if "int_parsing" in kind or "int_type" in kind:
        return "invalid_integer"
    if any(marker in kind for marker in ("greater_than", "less_than", "multiple_of", "finite_number")):
        if field == "offset":
            return "invalid_offset"
        if field in _LIMIT_FIELDS:
            return "invalid_limit"
        return "numeric_parameter_out_of_range"
    if "too_long" in kind or "max_length" in kind:
        return "string_parameter_too_long"
    if "literal" in kind or "enum" in kind:
        return "invalid_enum_value"
    if field in _IDENTIFIER_FIELDS and any(marker in kind for marker in ("pattern", "string", "value_error")):
        return "invalid_identifier_token"
    if location == "body":
        return "invalid_upload_metadata" if field == "file" else "invalid_body"
    return "invalid_request"


def _host_name(value: str) -> str | None:
    if not value or any(char in value for char in "\r\n/\\@"):
        return None
    try:
        return urlsplit("//" + value).hostname
    except ValueError:
        return None


def _normalized_authority(value: str, scheme: str) -> str | None:
    if (
        not value
        or value != value.strip()
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        return None
    try:
        parsed = urlsplit("//" + value)
        if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return None
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    hostname = hostname.casefold()
    if port in ({"http": 80, "https": 443}.get(scheme), None):
        port = None
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{authority_host}:{port}" if port is not None else authority_host


class TrustedAccessMiddleware:
    """Validate Host using a strict single-hop trusted edge-proxy contract.

    A trusted edge must overwrite client-supplied forwarding headers. Repeated
    headers, comma chains, malformed quoted values, or conflicting Forwarded
    and X-Forwarded values are rejected instead of guessing which hop to trust.
    """

    def __init__(self, app, *, policy: WebTrustPolicy) -> None:
        self.app = app
        self.policy = policy
        self._proxy_networks = tuple(ipaddress.ip_network(value, strict=False) for value in policy.trusted_proxies)

    @staticmethod
    def _headers(scope) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key, value in scope.get("headers", []):
            result.setdefault(key.decode("latin-1").lower(), []).append(value.decode("latin-1"))
        return result

    def _trusted_proxy(self, scope) -> bool:
        client = scope.get("client")
        if not client or not self._proxy_networks:
            return False
        try:
            address = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        return any(address in network for network in self._proxy_networks)

    @staticmethod
    def _split_forwarded_element(value: str) -> list[str] | None:
        parts: list[str] = []
        current: list[str] = []
        quoted = False
        escaped = False
        for char in value:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
                current.append(char)
            elif char == "," and not quoted:
                return None
            elif char == ";" and not quoted:
                parts.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        if quoted or escaped:
            return None
        parts.append("".join(current).strip())
        return parts

    @classmethod
    def _forwarded_values(cls, headers: dict[str, list[str]]) -> tuple[str | None, str | None, bool]:
        for name in ("forwarded", "x-forwarded-host", "x-forwarded-proto"):
            if len(headers.get(name, [])) > 1:
                return None, None, False

        forwarded_host: str | None = None
        forwarded_proto: str | None = None
        raw_forwarded = headers.get("forwarded", [])
        if raw_forwarded:
            parts = cls._split_forwarded_element(raw_forwarded[0])
            if parts is None:
                return None, None, False
            seen: set[str] = set()
            for item in parts:
                key, separator, raw_value = item.partition("=")
                key = key.strip().casefold()
                raw_value = raw_value.strip()
                if not separator or not key or key in seen or not raw_value:
                    return None, None, False
                seen.add(key)
                if raw_value.startswith('"'):
                    if len(raw_value) < 2 or not raw_value.endswith('"'):
                        return None, None, False
                    clean = raw_value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                elif '"' in raw_value or any(char.isspace() for char in raw_value):
                    return None, None, False
                else:
                    clean = raw_value
                if key == "host":
                    forwarded_host = clean
                elif key == "proto":
                    forwarded_proto = clean.casefold()

        x_host_values = headers.get("x-forwarded-host", [])
        x_proto_values = headers.get("x-forwarded-proto", [])
        x_host = x_host_values[0].strip() if x_host_values else None
        x_proto = x_proto_values[0].strip().casefold() if x_proto_values else None
        if (x_host and "," in x_host) or (x_proto and "," in x_proto):
            return None, None, False
        if forwarded_host and x_host:
            comparison_scheme = forwarded_proto or x_proto or "http"
            if _normalized_authority(forwarded_host, comparison_scheme) != _normalized_authority(
                x_host, comparison_scheme
            ):
                return None, None, False
        if forwarded_proto and x_proto and forwarded_proto != x_proto:
            return None, None, False
        host = forwarded_host or x_host
        proto = forwarded_proto or x_proto
        if proto is not None and proto not in {"http", "https"}:
            return None, None, False
        return host, proto, True

    @staticmethod
    async def _reject(send, code: str = "host_not_allowed") -> None:
        await UploadIngressMiddleware._error(send, 400, code)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        host_values = headers.get("host", [])
        if len(host_values) != 1:
            await self._reject(send, "invalid_host_header")
            return
        effective_host = host_values[0]
        effective_scheme = scope.get("scheme", "http")
        trusted_proxy = self._trusted_proxy(scope)
        if trusted_proxy:
            forwarded_host, forwarded_proto, valid_forwarding = self._forwarded_values(headers)
            if not valid_forwarding:
                await self._reject(send, "invalid_forwarded_headers")
                return
            effective_host = forwarded_host or effective_host
            if forwarded_proto:
                effective_scheme = forwarded_proto
        normalized_authority = _normalized_authority(effective_host, effective_scheme)
        hostname = _host_name(normalized_authority or "")
        if hostname is None or hostname.casefold() not in self.policy.allowed_hosts:
            await self._reject(send)
            return
        state = scope.setdefault("state", {})
        state["trusted_host"] = normalized_authority
        state["trusted_hostname"] = hostname.casefold()
        state["trusted_scheme"] = effective_scheme
        state["trusted_proxy"] = trusted_proxy
        await self.app(scope, receive, send)


class UploadIngressMiddleware:
    """Reserve the writer slot and cap upload bytes before multipart parsing."""

    def __init__(self, app, *, manager: ImportJobManager, body_limit: int, trust_policy: WebTrustPolicy) -> None:
        self.app = app
        self.manager = manager
        self.body_limit = body_limit
        self.trust_policy = trust_policy

    @staticmethod
    def _headers(scope) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key, value in scope.get("headers", []):
            result.setdefault(key.decode("latin-1").lower(), []).append(value.decode("latin-1"))
        return result

    @staticmethod
    async def _error(send, status: int, code: str) -> None:
        body = json.dumps({"detail": code, "code": code}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/api/import/upload":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        if len(headers.get("content-length", [])) > 1:
            await self._error(send, 400, "upload_duplicate_content_length")
            return
        length_values = headers.get("content-length", [])
        raw_length = length_values[0] if length_values else None
        if raw_length is None and self.trust_policy.remote:
            await self._error(send, 411, "upload_content_length_required")
            return
        if raw_length is not None:
            if len(raw_length) > 20 or re.fullmatch(r"0|[1-9][0-9]*", raw_length, flags=re.ASCII) is None:
                await self._error(send, 400, "upload_invalid_content_length")
                return
            content_length = int(raw_length)
            if content_length > self.body_limit:
                await self._error(send, 413, "upload_multipart_body_too_large")
                return
        try:
            admitted = self.manager.acquire_pending_upload_slot()
        except WebIndexBuildError as exc:
            status = 501 if exc.code == "writer_process_lock_unsupported" else 409
            await self._error(send, status, exc.code)
            return
        if not admitted:
            await self._error(send, 409, "import_job_active")
            return
        state = scope.setdefault("state", {})
        state["upload_slot_reserved"] = True
        state["upload_slot_transferred"] = False
        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.body_limit:
                    raise _UploadBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _UploadBodyTooLarge:
            if not response_started:
                await self._error(send, 413, "upload_multipart_body_too_large")
        finally:
            if not state.get("upload_slot_transferred"):
                self.manager.release_pending_upload_slot()


class WriteAccessMiddleware:
    """Apply one fail-closed Origin/Fetch-Metadata contract to every unsafe method."""

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

    def __init__(self, app, *, policy: WebTrustPolicy) -> None:
        self.app = app
        self.policy = policy

    @staticmethod
    def _headers(scope) -> dict[str, list[str]]:
        return TrustedAccessMiddleware._headers(scope)

    @staticmethod
    def _code(scope, generic: str) -> str:
        prefix = "upload" if scope.get("path") == "/api/import/upload" else "write"
        return f"{prefix}_{generic}"

    def _origin_allowed(
        self, headers: dict[str, list[str]], scope
    ) -> tuple[bool, str | None]:
        sec_fetch_site = headers.get("sec-fetch-site", [])
        if sec_fetch_site:
            raw_fetch_site = sec_fetch_site[0]
            normalized_fetch_site = raw_fetch_site.casefold()
            if (
                raw_fetch_site != raw_fetch_site.strip()
                or normalized_fetch_site
                not in {"same-origin", "same-site", "none", "cross-site"}
                or normalized_fetch_site == "cross-site"
            ):
                return False, self._code(scope, "origin_not_allowed")
        origin_values = headers.get("origin", [])
        if not origin_values:
            if self.policy.allow_missing_origin_for_writes:
                return True, None
            return False, self._code(scope, "origin_required")
        origin = origin_values[0]
        if (
            origin != origin.strip()
            or not origin
            or "," in origin
            or any(unicodedata.category(char) == "Cc" for char in origin)
        ):
            return False, self._code(scope, "origin_not_allowed")
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False, self._code(scope, "origin_not_allowed")
        if parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
            return False, self._code(scope, "origin_not_allowed")
        try:
            origin_hostname = parsed.hostname.casefold() if parsed.hostname else ""
        except ValueError:
            return False, self._code(scope, "origin_not_allowed")
        origin_authority = _normalized_authority(parsed.netloc, parsed.scheme)
        state = scope.get("state", {})
        allowed = (
            parsed.scheme in {"http", "https"}
            and origin_hostname in self.policy.allowed_hosts
            and origin_authority == state.get("trusted_host", "")
            and parsed.scheme == state.get("trusted_scheme", scope.get("scheme", "http"))
        )
        return allowed, None if allowed else self._code(scope, "origin_not_allowed")

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") != "http"
            or str(scope.get("method", "GET")).upper() in self.SAFE_METHODS
        ):
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        duplicate_codes = {"origin": "duplicate_origin_header", "sec-fetch-site": "duplicate_sec_fetch_site"}
        for name, code in duplicate_codes.items():
            if len(headers.get(name, [])) > 1:
                await UploadIngressMiddleware._error(
                    send, 400, self._code(scope, code)
                )
                return
        origin_allowed, origin_error = self._origin_allowed(headers, scope)
        if not origin_allowed:
            await UploadIngressMiddleware._error(
                send,
                403,
                origin_error or self._code(scope, "origin_not_allowed"),
            )
            return
        await self.app(scope, receive, send)


def create_app(
    db_path: Path | None = None,
    static_dir: Path | None = None,
    allow_fallback: bool = False,
    log_level: str = "warning",
    host: str = "127.0.0.1",
    allowed_hosts: str | None = None,
    trusted_proxies: str | None = None,
) -> FastAPI:
    if db_path is None:
        db_path = Path("archive/chatgpt_archive.db")
    db_path = db_path.resolve()
    from .web_api import _get_upload_policy, _get_web_trust_policy, _remote_access_allowed

    upload_policy = _get_upload_policy(host=host)
    if upload_policy.remote and not _remote_access_allowed():
        raise ValueError(
            "non_loopback_access_requires_opt_in: set CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS=true "
            "only on a trusted network"
        )
    trust_policy = _get_web_trust_policy(
        host=host,
        allowed_hosts=allowed_hosts,
        trusted_proxies=trusted_proxies,
    )
    manager = ImportJobManager(db_path, log_level=log_level)
    app = FastAPI(
        title="ChatGPT Archive Web",
        docs_url=None,
        redoc_url=None,
        default_response_class=FiniteJSONResponse,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_response(_request, exc: RequestValidationError):
        validation_errors = exc.errors()
        errors = []
        for item in validation_errors[:16]:
            raw_loc = item.get("loc") if isinstance(item, dict) else ()
            location, field = _safe_validation_location_and_field(raw_loc)
            error_type = str(item.get("type", "invalid")) if isinstance(item, dict) else "invalid"
            errors.append(
                {
                    "location": location,
                    "field": field,
                    "code": _validation_public_code(
                        location=location,
                        field=field,
                        error_type=error_type,
                    ),
                }
            )
        return FiniteJSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "invalid_request",
                    "errors": errors,
                    "error_count": min(len(validation_errors), 10_000),
                    "errors_truncated": len(validation_errors) > len(errors),
                }
            },
        )

    @app.exception_handler(sqlite3.Error)
    async def sqlite_error_response(_request, exc: sqlite3.Error):
        code = sqlite_runtime_error_code(exc)
        status = 503 if code in {"database_locked", "database_readonly", "database_io_error"} else 500
        return FiniteJSONResponse(
            status_code=status,
            content={"detail": {"code": code, "error_type": type(exc).__name__}},
        )

    @app.exception_handler(EffectiveCurrentResourceLimitError)
    async def effective_current_limit_response(_request, exc: EffectiveCurrentResourceLimitError):
        return FiniteJSONResponse(status_code=413, content={"detail": exc.code})

    @app.exception_handler(ExportResourceLimitError)
    async def export_limit_response(_request, exc: ExportResourceLimitError):
        return FiniteJSONResponse(status_code=413, content={"detail": exc.code})

    app.include_router(create_api_router(db_path, manager, upload_policy=upload_policy, trust_policy=trust_policy))

    def safe_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=str(API_SCHEMA_VERSION),
            routes=app.routes,
        )
        invalid_request_schema = {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "const": "invalid_request"},
                        "errors": {
                            "type": "array",
                            "maxItems": 16,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "location": {"type": "string", "enum": sorted(_VALIDATION_LOCATIONS | {"request"})},
                                    "field": {"type": "string", "maxLength": 64},
                                    "code": {
                                        "type": "string",
                                        "enum": [
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
                                    },
                                },
                                "required": ["location", "field", "code"],
                            },
                        },
                        "error_count": {"type": "integer"},
                        "errors_truncated": {"type": "boolean"},
                    },
                }
            },
        }
        search_truth_properties = {
            "total_exact": {
                "type": "boolean",
                "description": "Whether total is exact; independent of ordering exactness.",
            },
            "order_exact": {
                "type": "boolean",
                "description": "Whether items belong to the final globally ordered result.",
            },
            "scan_complete": {
                "type": "boolean",
                "description": "Whether bounded candidate verification reached its terminal state.",
            },
            "provisional_order": {
                "type": "boolean",
                "description": "Whether this segment is explicitly provisional.",
            },
            "next_offset": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "description": "Null whenever signed continuation is present because numeric offset is not equivalent.",
            },
        }
        import_job_schema = {
            "type": "object",
            "properties": {
                "completion_outcome": {
                    "type": "string",
                    "enum": [
                        "queued", "running", "success", "success_with_warnings",
                        "partial_success", "failed_before_commit",
                        "failed_after_canonical_commit", "cleanup_warning", "cancelled",
                    ],
                },
                "canonical_import_outcome": {
                    "type": "string",
                    "enum": [
                        "queued", "running", "success", "success_with_warnings",
                        "partial_success", "failed_before_commit",
                    ],
                },
                "canonical_commit_succeeded": {"type": "boolean"},
                "summary": {
                    "type": ["object", "null"],
                    "description": "Safe aggregate counts, warnings_by_type, and JSON resource profile; never exported content or IDs.",
                },
            },
            "required": [
                "completion_outcome",
                "canonical_import_outcome",
                "canonical_commit_succeeded",
            ],
        }
        schema.setdefault("components", {}).setdefault("schemas", {}).update(
            {
                "SearchTruthContract": {
                    "type": "object",
                    "properties": search_truth_properties,
                    "required": [
                        "total_exact", "order_exact", "scan_complete",
                        "provisional_order", "next_offset",
                    ],
                },
                "ImportJobOutcomeContract": import_job_schema,
            }
        )
        schema["x-chatgpt-archive-contract"] = {
            "api_schema_version": API_SCHEMA_VERSION,
            "optional_web_index_format_version": OPTIONAL_WEB_INDEX_FORMAT_VERSION,
            "search_continuation_scope": "server-instance",
            "durable_generations": [
                "message", "title", "graph", "address", "query"
            ],
            "unsafe_method_origin_policy": (
                "all unsafe methods are default-deny after trusted Host/proxy "
                "normalization; remote writes require one valid same-origin Origin"
            ),
            "writer_process_lock": {
                "registry_max_files": 64,
                "windows": "writer_process_lock_unsupported",
                "upload_admission_before_spool": True,
            },
            "search_truth_fields": list(search_truth_properties),
            "import_outcome_fields": [
                "completion_outcome", "canonical_import_outcome",
                "canonical_commit_succeeded",
            ],
        }
        message_search = schema.get("paths", {}).get("/api/search/messages", {}).get("get")
        if isinstance(message_search, dict):
            message_search["x-search-truth-contract"] = {
                "total_exact": True,
                "order_exact": True,
                "scan_complete": True,
                "provisional_order": True,
                "continuation_requires_null_next_offset": True,
            }
        conversations = schema.get("paths", {}).get("/api/conversations", {}).get("get")
        if isinstance(conversations, dict):
            conversations["x-search-order-contract"] = {
                "order_exact": True,
                "scan_complete": True,
                "provisional_order": True,
                "continuation_requires_null_next_offset": True,
            }
        for path in (
            "/api/import/upload",
            "/api/import/jobs",
            "/api/import/jobs/{job_id}",
        ):
            path_item = schema.get("paths", {}).get(path, {})
            for operation in path_item.values():
                if isinstance(operation, dict):
                    operation["x-import-job-outcome-contract"] = {
                        "completion_outcome": True,
                        "canonical_import_outcome": True,
                        "canonical_commit_succeeded": True,
                    }
        for path_item in schema.get("paths", {}).values():
            for method, operation in path_item.items():
                if not isinstance(operation, dict):
                    continue
                if str(method).upper() not in WriteAccessMiddleware.SAFE_METHODS:
                    operation["x-write-origin-policy"] = {
                        "remote_same_origin_required": True,
                        "trusted_loopback_missing_origin_compatible": True,
                        "single_value_headers": ["Origin", "Sec-Fetch-Site"],
                    }
                response = operation.get("responses", {}).get("422")
                if response is not None:
                    response["content"] = {"application/json": {"schema": invalid_request_schema}}
        app.openapi_schema = schema
        return schema

    app.openapi = safe_openapi
    from .web_api import upload_body_limit

    app.add_middleware(
        UploadIngressMiddleware,
        manager=manager,
        body_limit=upload_body_limit(upload_policy),
        trust_policy=trust_policy,
    )
    app.add_middleware(WriteAccessMiddleware, policy=trust_policy)
    app.add_middleware(TrustedAccessMiddleware, policy=trust_policy)

    build_dir = static_dir or Path(__file__).resolve().parent.parent / "webui" / "dist"
    if build_dir.exists() and (build_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=build_dir, html=True), name="webui")
    else:
        if not allow_fallback:
            raise ValueError(
                "React Web UI build is missing. Run: cd webui && npm ci && npm run build. "
                "Use --allow-fallback only for the limited emergency HTML UI."
            )

        @app.get("/", response_class=HTMLResponse)
        def missing_build():
            return """
            <!doctype html>
            <html><head><title>ChatGPT Archive Web</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
            body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f6f4ef;color:#1f2933}
            .shell{display:grid;grid-template-columns:minmax(280px,34vw) 1fr;height:100vh}
            .fallback-warning{background:#7f1d1d;color:white;padding:12px 16px;font-weight:800}
            .fallback-warning code{background:rgba(255,255,255,.18);padding:2px 4px;border-radius:4px}
            aside{border-right:1px solid #ddd;background:#fbfaf7;overflow:auto} main{overflow:auto}
            .search{padding:16px;border-bottom:1px solid #ddd} input{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px}
            .item{display:block;width:100%;text-align:left;border:0;background:transparent;padding:12px;border-bottom:1px solid #eee;cursor:pointer}
            .item:hover,.item.selected{background:#ece8df}.title{font-weight:700}.meta,.snippet{font-size:12px;color:#667085;margin-top:4px}
            header{padding:16px 22px;border-bottom:1px solid #ddd;position:sticky;top:0;background:#f9f7f2}
            .msg{max-width:940px;margin:18px auto;padding:14px 16px;border:1px solid #ddd;border-radius:8px;background:#fff}
            .msg.user{background:#eef6f1}.role{font-size:12px;font-weight:700;color:#667085}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.55}
            mark{background:#ffe68a;border-radius:3px}.row{display:flex;gap:8px;margin-top:8px}select,button,a{padding:7px 10px;border:1px solid #ccc;border-radius:7px;background:#fff;color:inherit;text-decoration:none}
            @media(max-width:820px){.shell{grid-template-columns:1fr}aside{height:42vh;border-right:0;border-bottom:1px solid #ddd}}
            </style></head>
            <body>
              <div class="fallback-warning">Limited minimal fallback UI — not the full React UI. Some features (internal messages, search filters, raw JSON, full reader) are not available here. Build the full UI with <code>cd webui && npm ci && npm run build</code>.</div>
              <div class="shell">
                <aside>
                  <div class="search">
                    <input id="q" maxlength="500" placeholder='Search, "exact phrase", role:user, -exclude'>
                    <div class="row">
                      <select id="sort"><option value="relevance">Relevance</option><option value="newest">Newest</option><option value="oldest">Oldest</option><option value="title">Title</option></select>
                      <select id="path"><option value="current">Current path</option><option value="all">All nodes</option></select>
                    </div>
                    <p class="meta">Fallback UI. Build React UI with <code>cd webui && npm ci && npm run build</code>.</p>
                  </div>
                  <div id="list"></div>
                </aside>
                <main>
                  <header><h2 id="heading">Select a conversation</h2><div id="info" class="meta"></div><div id="actions" class="row"></div></header>
                  <section id="messages"></section>
                </main>
              </div>
              <script>
              const q=document.getElementById('q'), list=document.getElementById('list'), messages=document.getElementById('messages'), heading=document.getElementById('heading'), info=document.getElementById('info'), actions=document.getElementById('actions');
              const sort=document.getElementById('sort'), pathSel=document.getElementById('path'); let selected=null, timer=null;
              const COPY_MAX_BYTES=16*1024*1024, COPY_MAX_CHARS=8*1024*1024;
              const esc=s=>String(s??'').replace(/[&<>"'`]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;'}[c]));
              const date=v=>v!==null&&v!==undefined?new Date(v*1000).toLocaleString():'';
              const errorCode=p=>{const d=p&&p.detail; const c=d&&typeof d==='object'?d.code:(typeof d==='string'?d:p&&p.code); return typeof c==='string'&&/^[a-z0-9_:-]+$/.test(c)?c:'request_failed'};
              async function api(url){const r=await fetch(url,{headers:{Accept:'application/json'}}); let data=null; try{data=await r.json()}catch{} if(!r.ok)throw new Error(errorCode(data)); if(!data||typeof data!=='object')throw new Error('invalid_response'); return data;}
              async function boundedCopyBody(r){const declared=r.headers.get('content-length'); if(declared!==null&&(!/^(0|[1-9][0-9]*)$/.test(declared)||Number(declared)>COPY_MAX_BYTES)){await r.body?.cancel().catch(()=>{}); throw new Error('copy_too_large')} if(!r.body)throw new Error('copy_too_large'); const reader=r.body.getReader(), decoder=new TextDecoder(), parts=[]; let bytes=0,chars=0; try{for(;;){const {done,value}=await reader.read(); if(done)break; bytes+=value.byteLength; if(bytes>COPY_MAX_BYTES)throw new Error('copy_too_large'); const part=decoder.decode(value,{stream:true}); chars+=part.length; if(chars>COPY_MAX_CHARS)throw new Error('copy_too_large'); parts.push(part)} const tail=decoder.decode(); chars+=tail.length; if(chars>COPY_MAX_CHARS)throw new Error('copy_too_large'); parts.push(tail); return parts.join('')}catch(error){await reader.cancel().catch(()=>{}); throw error}finally{reader.releaseLock()}}
              const safeError=e=>({database_migration_required:'Database migration required. Create a verified backup, then run the CLI migrate command.',database_schema_newer:'Database schema is newer than this app.',database_schema_incompatible:'Database schema is incompatible. Run CLI verify.',database_foreign_key_violation:'Database references are damaged. Run CLI verify.',database_locked:'Database is locked; retry after the writer finishes.',database_malformed:'Database is malformed; restore a verified backup.',database_readonly:'Database cannot be opened read-only.',database_io_error:'Database I/O error.',copy_too_large:'Copy is unavailable because the response is too large. Use Download instead.'}[e&&e.message]||'The local archive request failed.');
              function showError(target,error){target.textContent=safeError(error); target.className='meta';}
              async function loadList(){try{const p=new URLSearchParams({q:q.value,sort:sort.value,path:pathSel.value,limit:'50'}); const data=await api('/api/conversations?'+p); const items=Array.isArray(data.items)?data.items:[]; list.innerHTML=items.map(x=>`<button class="item ${x.conversation_id===selected?'selected':''}" data-id="${esc(x.conversation_id)}"><span class="title">${esc(x.title||'untitled')}</span><div class="meta">${date(x.update_time??x.create_time)}${x.hit_count?' · '+x.hit_count+' hits':''}</div><div class="snippet">${esc((x.snippets&&x.snippets[0]&&x.snippets[0].snippet)||'')}</div></button>`).join('')}catch(error){showError(list,error)}}
              function byId(route,id,extra={}){const u=new URL(route,location.origin); u.search=new URLSearchParams({conversation_id:id,...extra}).toString(); return u.pathname+u.search}
              async function openConv(id,aroundNodeId=''){try{
                selected=id; const d=await api(byId('/api/by-id/conversation',id)); heading.textContent=d.title||'untitled'; info.textContent=`Created ${date(d.create_time)} · Updated ${date(d.update_time)} · ${d.current_path_nodes??0}/${d.node_count??0} raw current flags; effective path ${d.effective_path||pathSel.value}`;
                actions.replaceChildren();
                for(const fmt of ['md','txt']){const a=document.createElement('a'); a.href=byId('/api/by-id/export',id,{format:fmt,path:pathSel.value,include_internal:'false'}); a.textContent=`Download visible ${fmt.toUpperCase()}`; actions.append(a)}
                const copy=document.createElement('button'); copy.type='button'; copy.textContent='Copy visible current conversation'; copy.addEventListener('click',async()=>{try{const r=await fetch(byId('/api/by-id/copy',id,{path:pathSel.value,include_internal:'false'})); if(!r.ok)throw new Error('request_failed'); await navigator.clipboard.writeText(await boundedCopyBody(r))}catch(error){showError(info,error)}}); actions.append(copy);
                const page=await api(byId('/api/by-id/messages',id,{q:q.value,path:pathSel.value,limit:'300',include_internal:'false',...(aroundNodeId?{around_node_id:aroundNodeId}:{})})); const items=Array.isArray(page.items)?page.items:[]; messages.replaceChildren();
                for(const m of items){const article=document.createElement('article'); article.className='msg '+String(m.role||'message').replace(/[^a-z0-9_-]/gi,'_'); const role=document.createElement('div'); role.className='role'; role.textContent=`${m.role||'message'} · ${date(m.create_time??m.update_time)}`; const pre=document.createElement('pre'); pre.textContent=m.display_text||'[empty]'; const row=document.createElement('div'); row.className='row'; const raw=document.createElement('a'); raw.href=byId('/api/by-id/raw',id,{node_id:String(m.node_id),max_chars:'50000'}); raw.textContent='Bounded raw preview'; const display=document.createElement('a'); display.href=byId('/api/by-id/display',id,{node_id:String(m.node_id),offset:'0',limit:'65536'}); display.textContent='Display chunk'; const around=document.createElement('button'); around.type='button'; around.textContent='Open around message'; around.addEventListener('click',()=>openConv(id,String(m.node_id))); row.append(raw,display,around); article.append(role,pre,row); messages.append(article)}
                await loadList()
              }catch(error){showError(messages,error)}}
              list.addEventListener('click',e=>{const b=e.target.closest('button[data-id]'); if(b) openConv(b.dataset.id);});
              q.addEventListener('input',()=>{clearTimeout(timer); timer=setTimeout(loadList,220)}); sort.addEventListener('change',loadList); pathSel.addEventListener('change',()=>selected?openConv(selected):loadList());
              const interactiveSelector="button,a[href],summary,input,textarea,select,[contenteditable]:not([contenteditable='false']),[role='button'],[role='option'],[role='menuitem'],[role='checkbox'],[role='switch'],[role='tab'],[tabindex]:not([tabindex='-1'])";
              window.addEventListener('keydown',e=>{const t=e.target; const interactive=t instanceof Element&&t.closest(interactiveSelector)!==null; if(!interactive&&(e.key==='/'||((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'))){e.preventDefault();q.focus();}});
              loadList();
              </script>
            </body></html>
            """
    return app
