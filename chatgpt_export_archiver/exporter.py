from __future__ import annotations

import csv
import json
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .current_path import resolve_effective_current_collection
from .db import export_query, record_export
from .parser import recover_message_display_text
from .search import _is_internal_message
from .utils import epoch_to_date_part, epoch_to_display, finite_float_or_none, parse_date_boundary, safe_filename_part, sha256_text, truncate_utf8, write_bytes_if_changed, write_chunks_if_changed


MAX_EXPORT_BASENAME_BYTES = 240
EXPORT_CONVERSATION_BATCH_SIZE = 200
EXPORT_NODE_BATCH_SIZE = 8
MAX_EXPORT_NODES_PER_CONVERSATION = 100_000
MAX_EXPORT_NODE_INPUT_BYTES = 32 * 1024 * 1024
MAX_EXPORT_CONVERSATION_INPUT_BYTES = 128 * 1024 * 1024
MAX_EXPORT_HEADER_INPUT_BYTES = 4 * 1024 * 1024
MAX_EXPORT_BATCH_INPUT_BYTES = 160 * 1024 * 1024
MAX_EXPORT_OUTPUT_BYTES = 256 * 1024 * 1024


class ExportResourceLimitError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def check_conversation_export_budget(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> dict[str, int]:
    return check_conversation_export_budgets(conn, [conversation_id])[conversation_id]


def check_conversation_export_budgets(
    conn: sqlite3.Connection,
    conversation_ids: Sequence[str],
) -> dict[str, dict[str, int]]:
    ids = [str(value) for value in conversation_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    results = {
        conversation_id: {"node_count": 0, "input_bytes": 0, "max_node_bytes": 0, "header_bytes": 0}
        for conversation_id in ids
    }
    rows = conn.execute(
        f"""SELECT conversation_id, COUNT(*) AS node_count,
                  COALESCE(SUM(
                      COALESCE(length(CAST(content_text AS BLOB)), 0) +
                      COALESCE(length(CAST(raw_message_json AS BLOB)), 0) +
                      COALESCE(length(CAST(node_id AS BLOB)), 0) +
                      COALESCE(length(CAST(parent_node_id AS BLOB)), 0) +
                      COALESCE(length(CAST(children_json AS BLOB)), 0) +
                      COALESCE(length(CAST(message_id AS BLOB)), 0) +
                      COALESCE(length(CAST(role AS BLOB)), 0) +
                      COALESCE(length(CAST(author_name AS BLOB)), 0) +
                      COALESCE(length(CAST(content_type AS BLOB)), 0) +
                      COALESCE(length(CAST(content_hash AS BLOB)), 0) +
                      COALESCE(length(CAST(metadata_json AS BLOB)), 0)
                  ), 0) AS input_bytes,
                  COALESCE(MAX(
                      COALESCE(length(CAST(content_text AS BLOB)), 0) +
                      COALESCE(length(CAST(raw_message_json AS BLOB)), 0) +
                      COALESCE(length(CAST(children_json AS BLOB)), 0) +
                      COALESCE(length(CAST(metadata_json AS BLOB)), 0)
                  ), 0) AS max_node_bytes
            FROM conversation_nodes
            WHERE conversation_id IN ({placeholders})
            GROUP BY conversation_id""",
        ids,
    ).fetchall()
    for row in rows:
        results[str(row[0])].update({
            "node_count": int(row[1] or 0),
            "input_bytes": int(row[2] or 0),
            "max_node_bytes": int(row[3] or 0),
        })
    header_rows = conn.execute(
        f"""SELECT conversation_id,
                  COALESCE(length(CAST(conversation_id AS BLOB)), 0) +
                  COALESCE(length(CAST(title AS BLOB)), 0) +
                  COALESCE(length(CAST(current_node AS BLOB)), 0) +
                  COALESCE(length(CAST(source_file AS BLOB)), 0) +
                  COALESCE(length(CAST(default_model_slug AS BLOB)), 0) +
                  COALESCE(length(CAST(metadata_json AS BLOB)), 0)
            FROM conversations WHERE conversation_id IN ({placeholders})""",
        ids,
    ).fetchall()
    for row in header_rows:
        results[str(row[0])]["header_bytes"] = int(row[1] or 0)
    for result in results.values():
        if result["node_count"] > MAX_EXPORT_NODES_PER_CONVERSATION:
            raise ExportResourceLimitError("export_node_count_limit_exceeded")
        if result["max_node_bytes"] > MAX_EXPORT_NODE_INPUT_BYTES:
            raise ExportResourceLimitError("export_node_input_limit_exceeded")
        if result["input_bytes"] > MAX_EXPORT_CONVERSATION_INPUT_BYTES:
            raise ExportResourceLimitError("export_input_byte_limit_exceeded")
        if result["header_bytes"] > MAX_EXPORT_HEADER_INPUT_BYTES:
            raise ExportResourceLimitError("export_header_input_limit_exceeded")
    return results


def export_conversations(
    conn: sqlite3.Connection,
    out_dir: Path,
    formats: list[str],
    from_date: str | None = None,
    to_date: str | None = None,
    force: bool = False,
    path: str = "current",
    include_internal: bool = False,
    conversation_batch_size: int = EXPORT_CONVERSATION_BATCH_SIZE,
) -> dict[str, Any]:
    if path not in {"current", "all"}:
        raise ValueError("invalid_export_path")
    conversation_batch_size = max(1, min(400, int(conversation_batch_size)))
    formats = sorted({str(fmt).lower() for fmt in formats})
    out_dir.mkdir(parents=True, exist_ok=True)
    start_ts = parse_date_boundary(from_date)
    end_ts = parse_date_boundary(to_date, end_of_day=True)
    conversations = export_query(conn, start_ts, end_ts)
    filenames = build_filename_map(conversations, formats)
    _validate_filename_plan(filenames, conversations, formats)
    manifest_rows: list[dict[str, Any]] = []
    written = 0
    skipped = 0

    for batch_offset in range(0, len(conversations), conversation_batch_size):
        requested_batch = conversations[batch_offset : batch_offset + conversation_batch_size]
        budgets = check_conversation_export_budgets(
            conn, [str(conv["conversation_id"]) for conv in requested_batch]
        )
        bounded_batches: list[list[sqlite3.Row]] = []
        conversation_batch: list[sqlite3.Row] = []
        batch_input_bytes = 0
        for conv in requested_batch:
            budget = budgets[str(conv["conversation_id"])]
            if conversation_batch and batch_input_bytes + budget["input_bytes"] > MAX_EXPORT_BATCH_INPUT_BYTES:
                bounded_batches.append(conversation_batch)
                conversation_batch = []
                batch_input_bytes = 0
            conversation_batch.append(conv)
            batch_input_bytes += budget["input_bytes"]
        if conversation_batch:
            bounded_batches.append(conversation_batch)
        for conversation_batch in bounded_batches:
            nodes_by_conversation = _nodes_for_conversation_batch(conn, conversation_batch)
            for conv in conversation_batch:
                nodes = prepare_export_nodes(
                    conv,
                    nodes_by_conversation.get(str(conv["conversation_id"]), []),
                    path=path,
                    include_internal=include_internal,
                )
                for fmt in formats:
                    rel_path = filenames[(conv["conversation_id"], fmt)]
                    output_path = out_dir / rel_path
                    changed, output_hash, _output_bytes = write_chunks_if_changed(
                        output_path,
                        iter_rendered_conversation(conv, nodes, fmt),
                        force=force,
                        max_bytes=MAX_EXPORT_OUTPUT_BYTES,
                    )
                    if changed:
                        written += 1
                    else:
                        skipped += 1
                    record_export(
                        conn,
                        conv["conversation_id"],
                        fmt,
                        output_path,
                        output_hash,
                        {
                            "current_path_only": path == "current",
                            "path": path,
                            "include_internal": include_internal,
                            "from": from_date,
                            "to": to_date,
                            "deterministic_export": True,
                        },
                    )
                    manifest_rows.append(manifest_row(
                        conv,
                        fmt,
                        rel_path,
                        output_hash,
                        path=path,
                        include_internal=include_internal,
                    ))
    _validate_export_outputs(out_dir, manifest_rows, conversations, formats)
    write_manifest(out_dir, manifest_rows, force=force)
    conn.commit()
    return {"conversations": len(conversations), "formats": formats, "written": written, "skipped_unchanged": skipped}


def build_filename_map(conversations: list[sqlite3.Row], formats: list[str]) -> dict[tuple[str, str], Path]:
    """Build a stable, globally collision-free plan for this export set.

    All natural names are reserved before collision suffixes are allocated.
    This prevents one collision group from generating ``_001`` over another
    conversation's natural basename.  Collision keys model common
    case-insensitive and Unicode-normalizing filesystems.
    """
    normalized_formats = sorted({str(fmt).lower() for fmt in formats})
    requests: list[tuple[str, str, sqlite3.Row, str, str]] = []
    natural_groups: dict[str, list[tuple[str, str, sqlite3.Row, str, str]]] = {}
    for conv in conversations:
        conversation_id = str(conv["conversation_id"])
        for fmt in normalized_formats:
            natural_name = _base_filename(conv, fmt)
            collision_key = _filename_collision_key(natural_name)
            request = (conversation_id, fmt, conv, natural_name, collision_key)
            requests.append(request)
            natural_groups.setdefault(collision_key, []).append(request)

    # Reserve every natural basename, including names from duplicate groups,
    # before generating any suffix.  Generated and natural names therefore
    # share one global namespace independent of input traversal order.
    reserved = set(natural_groups)
    allocated: set[str] = set()
    result: dict[tuple[str, str], Path] = {}
    for conversation_id, fmt, conv, natural_name, natural_key in sorted(
        requests,
        key=lambda item: (item[4], item[1], item[0]),
    ):
        if len(natural_groups[natural_key]) == 1:
            candidate = natural_name
        else:
            suffix_index = 1
            while True:
                candidate = _base_filename(conv, fmt, collision_suffix=f"_{suffix_index:03d}")
                candidate_key = _filename_collision_key(candidate)
                if candidate_key not in reserved and candidate_key not in allocated:
                    break
                suffix_index += 1
        candidate_key = _filename_collision_key(candidate)
        if candidate_key in allocated:
            raise ValueError("export_filename_plan_collision")
        allocated.add(candidate_key)
        result[(conversation_id, fmt)] = Path(candidate)
    return result


def _filename_collision_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _requested_export_pairs(
    conversations: list[sqlite3.Row], formats: list[str]
) -> set[tuple[str, str]]:
    return {
        (str(conv["conversation_id"]), str(fmt).lower())
        for conv in conversations
        for fmt in formats
    }


def _validate_filename_plan(
    filenames: dict[tuple[str, str], Path],
    conversations: list[sqlite3.Row],
    formats: list[str],
) -> None:
    requested = _requested_export_pairs(conversations, formats)
    if set(filenames) != requested:
        raise ValueError("export_filename_plan_pair_mismatch")
    paths = [path.as_posix() for path in filenames.values()]
    collision_keys = [_filename_collision_key(path) for path in paths]
    if len(paths) != len(set(collision_keys)):
        raise ValueError("export_filename_plan_collision")
    for (_conversation_id, fmt), path in filenames.items():
        if path.parent != Path(".") or path.suffix != f".{fmt}":
            raise ValueError("export_filename_plan_invalid_path")
        if len(path.name.encode("utf-8")) > MAX_EXPORT_BASENAME_BYTES:
            raise ValueError("export_filename_plan_too_long")


def _validate_export_outputs(
    out_dir: Path,
    manifest_rows: list[dict[str, Any]],
    conversations: list[sqlite3.Row],
    formats: list[str],
) -> None:
    requested = _requested_export_pairs(conversations, formats)
    manifest_pairs = {
        (str(row["conversation_id"]), str(row["format"]).lower())
        for row in manifest_rows
    }
    manifest_paths = [str(row["output_path"]) for row in manifest_rows]
    collision_keys = [_filename_collision_key(path) for path in manifest_paths]
    existing_outputs = {path for path in manifest_paths if (out_dir / path).is_file()}
    if (
        manifest_pairs != requested
        or len(manifest_rows) != len(requested)
        or len(collision_keys) != len(set(collision_keys))
        or len(existing_outputs) != len(requested)
    ):
        raise RuntimeError("export_output_validation_failed")


def _base_filename(conv: sqlite3.Row, fmt: str, *, collision_suffix: str = "") -> str:
    timestamp = conv["create_time"] if conv["create_time"] is not None else conv["update_time"]
    date_part = epoch_to_date_part(timestamp)
    title = safe_filename_part(conv["title"], max_len=2048)
    raw_cid = str(conv["conversation_id"])
    cid = safe_filename_part(raw_cid, max_len=512)
    if len(cid.encode("utf-8")) > 96:
        digest = sha256_text(raw_cid)[:16]
        cid = truncate_utf8(cid, 79).rstrip("._ ") + "_" + digest
    extension = f".{fmt}"
    fixed = f"{date_part}__{cid}{collision_suffix}{extension}"
    title_budget = MAX_EXPORT_BASENAME_BYTES - len(fixed.encode("utf-8"))
    title = truncate_utf8(title, max(0, title_budget)).rstrip("._ ")
    if not title:
        title = "untitled"
    filename = f"{date_part}_{title}_{cid}{collision_suffix}{extension}"
    if len(filename.encode("utf-8")) > MAX_EXPORT_BASENAME_BYTES:
        overflow = len(filename.encode("utf-8")) - MAX_EXPORT_BASENAME_BYTES
        title = truncate_utf8(title, max(0, len(title.encode("utf-8")) - overflow)).rstrip("._ ") or "u"
        filename = f"{date_part}_{title}_{cid}{collision_suffix}{extension}"
    return filename


def order_current_path(conv: sqlite3.Row, nodes: list[sqlite3.Row]) -> list[sqlite3.Row]:
    by_id = {str(row["node_id"]): row for row in nodes}
    collection = resolve_effective_current_collection(conv["current_node"], nodes)
    return [by_id[node_id] for node_id in collection.node_ids]


def order_export_path(conv: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]], path: str) -> list[Mapping[str, Any]]:
    if path == "current":
        by_id = {str(row["node_id"]): row for row in nodes}
        collection = resolve_effective_current_collection(conv["current_node"], list(nodes))
        return [by_id[node_id] for node_id in collection.node_ids]
    return sorted(
        nodes,
        key=lambda row: (
            row["create_time"] is None,
            row["create_time"] if row["create_time"] is not None else row["update_time"] if row["update_time"] is not None else 0,
            row["node_id"],
        ),
    )


def _resolved_export_node(node: Mapping[str, Any], *, include_internal: bool) -> dict[str, Any] | None:
    resolved = recover_message_display_text(
        _optional_row_value(node, "content_text"),
        _optional_row_value(node, "raw_message_json"),
    )
    if not resolved:
        return None
    if not include_internal and _is_internal_message(
        _optional_row_value(node, "role"),
        _optional_row_value(node, "content_type"),
        resolved,
    ):
        return None
    output = dict(node)
    output["content_text"] = resolved
    output["raw_message_json"] = None
    return output


def prepare_export_nodes(
    conv: Mapping[str, Any],
    nodes: Sequence[Mapping[str, Any]],
    *,
    path: str,
    include_internal: bool,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for node in order_export_path(conv, nodes, path):
        resolved = _resolved_export_node(node, include_internal=include_internal)
        if resolved is not None:
            prepared.append(resolved)
    return prepared


def _nodes_for_conversation_batch(
    conn: sqlite3.Connection,
    conversations: Sequence[Mapping[str, Any]],
) -> dict[str, list[sqlite3.Row]]:
    ids = [str(conv["conversation_id"]) for conv in conversations]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM conversation_nodes WHERE conversation_id IN ({placeholders})",
        ids,
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {conversation_id: [] for conversation_id in ids}
    for row in rows:
        grouped[str(row["conversation_id"])].append(row)
    return grouped


def iter_conversation_export_nodes(
    conn: sqlite3.Connection,
    conv: Mapping[str, Any],
    *,
    path: str,
    include_internal: bool,
    batch_size: int = EXPORT_NODE_BATCH_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield complete export rows without accumulating reader page payloads."""

    if path not in {"current", "all"}:
        raise ValueError("invalid_export_path")
    check_conversation_export_budget(conn, str(conv["conversation_id"]))
    batch_size = max(1, min(EXPORT_NODE_BATCH_SIZE, int(batch_size)))
    if path == "all":
        yield from _iter_all_export_nodes_keyset(
            conn,
            str(conv["conversation_id"]),
            include_internal=include_internal,
            batch_size=batch_size,
        )
        return
    skeletons = conn.execute(
        """SELECT node_id, parent_node_id, is_on_current_path,
                  create_time, update_time
           FROM conversation_nodes
           WHERE conversation_id = ?""",
        (conv["conversation_id"],),
    ).fetchall()
    ordered_ids = [str(row["node_id"]) for row in order_export_path(conv, skeletons, path)]
    for offset in range(0, len(ordered_ids), batch_size):
        ids = ordered_ids[offset : offset + batch_size]
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM conversation_nodes WHERE conversation_id = ? AND node_id IN ({placeholders})",
            [conv["conversation_id"], *ids],
        ).fetchall()
        by_id = {str(row["node_id"]): row for row in rows}
        for node_id in ids:
            node = by_id.get(node_id)
            if node is None:
                continue
            resolved = _resolved_export_node(node, include_internal=include_internal)
            if resolved is not None:
                yield resolved


def _iter_all_export_nodes_keyset(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    include_internal: bool,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    """Stream all-node display order with a bounded keyset page."""

    missing_expr = "CASE WHEN create_time IS NULL THEN 1 ELSE 0 END"
    time_expr = "CASE WHEN create_time IS NOT NULL THEN create_time WHEN update_time IS NOT NULL THEN update_time ELSE 0 END"
    last_key: tuple[int, Any, str] | None = None
    while True:
        params: list[Any] = [conversation_id]
        predicate = ""
        if last_key is not None:
            predicate = f"""AND (
                {missing_expr} > ? OR
                ({missing_expr} = ? AND {time_expr} > ?) OR
                ({missing_expr} = ? AND {time_expr} = ? AND node_id > ?)
            )"""
            params.extend([
                last_key[0], last_key[0], last_key[1],
                last_key[0], last_key[1], last_key[2],
            ])
        params.append(batch_size)
        rows = conn.execute(
            f"""SELECT *, {missing_expr} AS export_sort_missing,
                       {time_expr} AS export_sort_time
                FROM conversation_nodes
                WHERE conversation_id = ? {predicate}
                ORDER BY export_sort_missing, export_sort_time, node_id
                LIMIT ?""",
            params,
        ).fetchall()
        if not rows:
            return
        for row in rows:
            resolved = _resolved_export_node(row, include_internal=include_internal)
            if resolved is not None:
                yield resolved
        tail = rows[-1]
        last_key = (
            int(tail["export_sort_missing"]),
            tail["export_sort_time"],
            str(tail["node_id"]),
        )


def _optional_row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _markdown_header(conv: Mapping[str, Any]) -> str:
    # No exported_at here by design: default exported files must be byte-stable
    # for identical database contents and CLI parameters.
    return "\n".join([
        f"# {conv['title'] or 'untitled'}",
        "",
        f"- conversation_id: `{conv['conversation_id']}`",
        f"- create_time: {epoch_to_display(conv['create_time'])}",
        f"- update_time: {epoch_to_display(conv['update_time'])}",
        f"- current_node: `{conv['current_node'] or ''}`",
        f"- source_file: `{conv['source_file'] or ''}`",
        "",
    ])


def _markdown_node(node: Mapping[str, Any]) -> str:
    content_text = recover_message_display_text(
        _optional_row_value(node, "content_text"),
        _optional_row_value(node, "raw_message_json"),
    )
    if not content_text:
        return ""
    role = (node["role"] or "message").title()
    node_time = node["create_time"] if node["create_time"] is not None else node["update_time"]
    timestamp = epoch_to_display(node_time)
    heading = f"## {role}" + (f" {timestamp}" if timestamp else "")
    return "\n".join([heading, "", content_text, ""])


def _txt_header(conv: Mapping[str, Any]) -> str:
    return "\n".join([
        conv["title"] or "untitled",
        f"conversation_id: {conv['conversation_id']}",
        f"create_time: {epoch_to_display(conv['create_time'])}",
        f"update_time: {epoch_to_display(conv['update_time'])}",
        f"current_node: {conv['current_node'] or ''}",
        f"source_file: {conv['source_file'] or ''}",
        "=" * 72,
        "",
    ])


def _txt_node(node: Mapping[str, Any]) -> str:
    content_text = recover_message_display_text(
        _optional_row_value(node, "content_text"),
        _optional_row_value(node, "raw_message_json"),
    )
    if not content_text:
        return ""
    role = (node["role"] or "message").upper()
    node_time = node["create_time"] if node["create_time"] is not None else node["update_time"]
    timestamp = epoch_to_display(node_time)
    return "\n".join([f"{role} {timestamp}".strip(), "-" * 72, content_text, ""])


def iter_rendered_conversation(
    conv: Mapping[str, Any],
    nodes: Iterable[Mapping[str, Any]],
    fmt: str,
) -> Iterator[str]:
    header = _markdown_header(conv) if fmt == "md" else _txt_header(conv)
    render_node = _markdown_node if fmt == "md" else _txt_node
    pending = header
    for node in nodes:
        fragment = render_node(node)
        if not fragment:
            continue
        yield from _bounded_text_chunks(pending)
        pending = fragment
    yield from _bounded_text_chunks(pending.rstrip() + "\n")


def _bounded_text_chunks(text: str, max_chars: int = 65_536) -> Iterator[str]:
    for offset in range(0, len(text), max_chars):
        yield text[offset : offset + max_chars]


def render_markdown(conv: Mapping[str, Any], nodes: Iterable[Mapping[str, Any]]) -> str:
    return "".join(iter_rendered_conversation(conv, nodes, "md"))


def render_txt(conv: Mapping[str, Any], nodes: Iterable[Mapping[str, Any]]) -> str:
    return "".join(iter_rendered_conversation(conv, nodes, "txt"))


def iter_copy_conversation(nodes: Iterable[Mapping[str, Any]]) -> Iterator[str]:
    first = True
    for node in nodes:
        content_text = recover_message_display_text(
            _optional_row_value(node, "content_text"),
            _optional_row_value(node, "raw_message_json"),
        )
        if not content_text or not content_text.strip():
            continue
        if not first:
            yield "\n\n"
        first = False
        yield f"{node['role'] or 'message'}:\n{content_text}"


def manifest_row(
    conv: Mapping[str, Any],
    fmt: str,
    relative_path: Path,
    output_hash: str,
    *,
    path: str = "current",
    include_internal: bool = False,
) -> dict[str, Any]:
    return {
        "aggregate_hash": conv["aggregate_hash"],
        "conversation_id": conv["conversation_id"],
        "create_time": finite_float_or_none(conv["create_time"]),
        "current_node": conv["current_node"],
        "format": fmt,
        "include_internal": include_internal,
        "output_hash": output_hash,
        "output_path": relative_path.as_posix(),
        "path": path,
        "source_file": conv["source_file"],
        "title": conv["title"],
        "update_time": finite_float_or_none(conv["update_time"]),
    }


def write_manifest(out_dir: Path, rows: list[dict[str, Any]], force: bool = False) -> None:
    rows = sorted(rows, key=lambda row: (row["output_path"], row["conversation_id"], row["format"]))
    jsonl = out_dir / "manifest.jsonl"
    jsonl_text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n" for row in rows)
    write_bytes_if_changed(jsonl, jsonl_text.encode("utf-8"), force=force)
    csv_path = out_dir / "manifest.csv"
    fieldnames = [
        "aggregate_hash",
        "conversation_id",
        "create_time",
        "current_node",
        "format",
        "include_internal",
        "output_hash",
        "output_path",
        "path",
        "source_file",
        "title",
        "update_time",
    ]
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    write_bytes_if_changed(csv_path, buffer.getvalue().encode("utf-8"), force=force)
