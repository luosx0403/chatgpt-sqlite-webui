from __future__ import annotations

from pathlib import Path
from typing import Annotated, Mapping
import json
import os
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile

from .exporter import render_markdown, render_txt
from .logging_utils import get_logger
from .search import get_conversation, get_messages, list_conversations, normalize_search_text, parse_query, search_conversations, search_messages
from .utils import safe_filename_part
from .web_db import check_schema, connect_readonly, detect_fts5, detect_trigram, web_index_status
from .web_jobs import ImportJobManager, cleanup_upload_dir, make_upload_path

LOGGER = get_logger("web_api")

ALLOWED_SORTS = {"relevance", "newest", "oldest", "created", "updated", "title"}
ALLOWED_SCOPES = {"all", "title", "message"}
ALLOWED_ROLES = {"", "user", "assistant", "tool", "system", "developer", "tool/system"}
ALLOWED_PATHS = {"current", "all"}
ALLOWED_MESSAGE_ORDERS = {"relevance", "display"}
ALLOWED_MATCH_MODES = {"contains", "word"}
DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024 * 1024
MAX_UPLOAD_ENV = "CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES"
MAX_UPLOAD_JSON_MEMBER_BYTES = 64 * 1024 * 1024 * 1024
MAX_UPLOAD_JSON_MEMBERS = 5000
MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024 * 1024
MAX_UPLOAD_COMPRESSION_RATIO = 1000.0


def _get_max_upload_bytes(environ: Mapping[str, str] = os.environ) -> int:
    raw = environ.get(MAX_UPLOAD_ENV)
    if raw is None:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        LOGGER.warning("invalid_upload_size_limit env=%s error_type=invalid_integer", MAX_UPLOAD_ENV)
        return DEFAULT_MAX_UPLOAD_BYTES
    if value <= 0:
        LOGGER.warning("invalid_upload_size_limit env=%s error_type=non_positive", MAX_UPLOAD_ENV)
        return DEFAULT_MAX_UPLOAD_BYTES
    return value


MAX_UPLOAD_BYTES = _get_max_upload_bytes()


def create_api_router(db_path: Path, job_manager: ImportJobManager | None = None) -> APIRouter:
    router = APIRouter(prefix="/api")
    manager = job_manager or ImportJobManager(db_path)

    def get_conn():
        if not db_path.exists():
            raise HTTPException(status_code=409, detail="database is not ready")
        try:
            conn = connect_readonly(db_path)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="database is not ready") from exc
        schema = check_schema(conn)
        if not schema["ok"]:
            conn.close()
            raise HTTPException(status_code=409, detail=_schema_error_detail(schema))
        try:
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
        schema = check_schema(conn)
        if not schema["ok"]:
            conn.close()
            raise HTTPException(status_code=409, detail=_schema_error_detail(schema))
        try:
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
        schema = check_schema(conn)
        if not schema["ok"]:
            conn.close()
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
            "conversations": {"filters": ["q", "sort", "after", "before", "role", "title", "scope", "exact", "exclude", "source", "path", "match_mode"]},
            "messages": {"path": ["current", "all"], "filters": ["q", "after", "before", "role", "title", "scope", "exact", "exclude", "source", "match_mode"], "raw": "message pages return raw_preview only; full raw is available per message endpoint"},
            "raw": {"endpoint": "/api/conversations/{conversation_id}/messages/{node_id}/raw"},
        }

    @router.get("/conversations")
    def conversations(
        q: str = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        sort: str = "newest",
        after: str | None = None,
        before: str | None = None,
        role: str | None = None,
        title: str | None = None,
        scope: str = "all",
        exact: str | None = None,
        exclude: str | None = None,
        source: str | None = None,
        path: str = "current",
        match_mode: str = "contains",
        selected_id: str | None = None,
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
        )
        _raise_query_errors(parsed)
        if parsed.has_search_context():
            return search_conversations(conn, parsed, limit=limit, offset=offset, sort=sort, selected_id=selected_id)
        return list_conversations(conn, limit=limit, offset=offset, sort=sort, after=parsed.after, before=parsed.before, selected_id=selected_id)

    @router.post("/import/upload")
    async def import_upload(file: UploadFile = File(...)):
        if manager.has_running_job():
            raise HTTPException(status_code=409, detail="an import job is already running")
        filename = file.filename or "upload.zip"
        if not filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="only .zip uploads are supported")
        upload_dir, upload_path = make_upload_path()
        size = 0
        try:
            with upload_path.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="upload_too_large")
                    out.write(chunk)
            if not zipfile.is_zipfile(upload_path):
                raise HTTPException(status_code=400, detail="uploaded file is not a valid zip")
            _validate_upload_zip_members(upload_path)
            try:
                job = manager.start_import(upload_path, filename=Path(filename.replace("\\", "/")).name, size=size)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return job.snapshot()
        except Exception:
            cleanup_upload_dir(upload_dir)
            raise

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
    def conversation_detail(conversation_id: str, conn=Depends(get_conn)):
        item = get_conversation(conn, conversation_id)
        if not item:
            raise HTTPException(status_code=404, detail="conversation not found")
        return item

    @router.get("/conversations/{conversation_id}/messages")
    def conversation_messages(
        conversation_id: str,
        path: str = "current",
        q: str = "",
        match_mode: str = "contains",
        role: str | None = None,
        title: str | None = None,
        scope: str = "all",
        exact: str | None = None,
        exclude: str | None = None,
        after: str | None = None,
        before: str | None = None,
        source: str | None = None,
        limit: Annotated[int, Query(ge=1, le=300)] = 300,
        offset: Annotated[int, Query(ge=0)] = 0,
        around_node_id: str | None = None,
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
        )

    @router.get("/conversations/{conversation_id}/messages/{node_id}/raw")
    def conversation_message_raw(conversation_id: str, node_id: str, conn=Depends(get_conn)):
        row = conn.execute(
            """
            SELECT raw_message_json
            FROM conversation_nodes
            WHERE conversation_id = ? AND node_id = ?
            """,
            (conversation_id, node_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="message not found")
        if not row["raw_message_json"]:
            return {"conversation_id": conversation_id, "node_id": node_id, "raw_message": None}
        try:
            raw = json.loads(row["raw_message_json"])
        except json.JSONDecodeError:
            raw = row["raw_message_json"]
        return {"conversation_id": conversation_id, "node_id": node_id, "raw_message": raw}

    @router.get("/conversations/{conversation_id}/export")
    def conversation_export(conversation_id: str, format: str = "md", path: str = "current", include_internal: bool = False, conn=Depends(get_conn)):
        _validate_common(path=path)
        if format not in {"md", "txt"}:
            raise HTTPException(status_code=400, detail="format must be md or txt")
        conv = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="conversation not found")
        messages = []
        offset = 0
        while True:
            page = get_messages(conn, conversation_id, path=path, limit=300, offset=offset)
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
        q: str = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        sort: str = "relevance",
        path: str = "current",
        role: str | None = None,
        title: str | None = None,
        scope: str = "all",
        exact: str | None = None,
        exclude: str | None = None,
        after: str | None = None,
        before: str | None = None,
        source: str | None = None,
        match_mode: str = "contains",
        selected_id: str | None = None,
        conn=Depends(get_optional_conn),
    ):
        if conn is None:
            return _empty_page(limit, offset, selected_id=selected_id, db_ready=False)
        _validate_common(sort=sort, scope=scope, role=role, path=path, match_mode=match_mode)
        parsed = parse_query(q, path_default=path, role=role, title=title, scope=scope, exact=exact, exclude=exclude, after=after, before=before, source=source, match_mode=match_mode)
        _raise_query_errors(parsed)
        return search_conversations(conn, parsed, limit=limit, offset=offset, sort=sort, selected_id=selected_id)

    @router.get("/search/messages")
    def search_message_endpoint(
        q: str = "",
        conversation_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        path: str = "current",
        order: str = "relevance",
        role: str | None = None,
        title: str | None = None,
        scope: str = "all",
        exact: str | None = None,
        exclude: str | None = None,
        after: str | None = None,
        before: str | None = None,
        source: str | None = None,
        match_mode: str = "contains",
        count_total: bool = True,
        conn=Depends(get_optional_conn),
    ):
        if conn is None:
            return _empty_page(limit, offset, selected_id=None, db_ready=False)
        _validate_common(role=role, path=path, scope=scope, match_mode=match_mode)
        if order not in ALLOWED_MESSAGE_ORDERS:
            raise HTTPException(status_code=400, detail="invalid message order")
        parsed = parse_query(q, path_default=path, role=role, title=title, scope=scope, exact=exact, exclude=exclude, after=after, before=before, source=source, match_mode=match_mode)
        _raise_query_errors(parsed)
        return search_messages(conn, parsed, limit=limit, offset=offset, conversation_id=conversation_id, order=order, count_total=count_total)

    @router.get("/search/suggest")
    def suggest(q: str = "", limit: Annotated[int, Query(ge=1, le=20)] = 10, conn=Depends(get_conn)):
        normalized = normalize_search_text(q[:100])
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


def _validate_upload_zip_members(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            candidates = [_info for _info in zf.infolist() if _is_conversation_json_member(_info.filename)]
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="uploaded file is not a valid zip") from exc
    if len(candidates) > MAX_UPLOAD_JSON_MEMBERS:
        raise HTTPException(status_code=413, detail="upload_zip_too_many_json_members")
    total_uncompressed = 0
    total_compressed = 0
    for info in candidates:
        total_uncompressed += int(info.file_size or 0)
        total_compressed += max(1, int(info.compress_size or 0))
        if info.file_size > MAX_UPLOAD_JSON_MEMBER_BYTES:
            raise HTTPException(status_code=413, detail="upload_zip_member_too_large")
        if info.file_size >= 10 * 1024 * 1024 and (info.file_size / max(1, info.compress_size)) > MAX_UPLOAD_COMPRESSION_RATIO:
            raise HTTPException(status_code=413, detail="upload_zip_compression_ratio_too_high")
    if total_uncompressed > MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES:
        raise HTTPException(status_code=413, detail="upload_zip_uncompressed_too_large")
    if total_uncompressed >= 10 * 1024 * 1024 and (total_uncompressed / max(1, total_compressed)) > MAX_UPLOAD_COMPRESSION_RATIO:
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
