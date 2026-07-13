from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .current_path import resolve_effective_current_collection
from .db import export_query, record_export
from .utils import epoch_to_date_part, epoch_to_display, finite_float_or_none, parse_date_boundary, safe_filename_part, sha256_bytes, sha256_text, truncate_utf8, write_bytes_if_changed


MAX_EXPORT_BASENAME_BYTES = 240


def export_conversations(
    conn: sqlite3.Connection,
    out_dir: Path,
    formats: list[str],
    from_date: str | None = None,
    to_date: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    start_ts = parse_date_boundary(from_date)
    end_ts = parse_date_boundary(to_date, end_of_day=True)
    conversations = export_query(conn, start_ts, end_ts)
    filenames = build_filename_map(conversations, formats)
    manifest_rows: list[dict[str, Any]] = []
    written = 0
    skipped = 0

    for conv in conversations:
        all_nodes = conn.execute(
            """
            SELECT *
            FROM conversation_nodes
            WHERE conversation_id = ?
            """,
            (conv["conversation_id"],),
        ).fetchall()
        nodes = order_current_path(conv, all_nodes)
        for fmt in formats:
            rel_path = filenames[(conv["conversation_id"], fmt)]
            output_path = out_dir / rel_path
            text = render_markdown(conv, nodes) if fmt == "md" else render_txt(conv, nodes)
            data = text.encode("utf-8")
            output_hash = sha256_bytes(data)
            changed = write_bytes_if_changed(output_path, data, force=force)
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
                {"current_path_only": True, "from": from_date, "to": to_date, "deterministic_export": True},
            )
            manifest_rows.append(manifest_row(conv, fmt, rel_path, output_hash))
    write_manifest(out_dir, manifest_rows, force=force)
    conn.commit()
    return {"conversations": len(conversations), "formats": formats, "written": written, "skipped_unchanged": skipped}


def build_filename_map(conversations: list[sqlite3.Row], formats: list[str]) -> dict[tuple[str, str], Path]:
    """Build stable, collision-free relative output paths for this export set."""
    result: dict[tuple[str, str], Path] = {}
    for fmt in formats:
        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for conv in conversations:
            groups[_base_filename(conv, fmt)].append(conv)
        for base_name in sorted(groups):
            group = sorted(groups[base_name], key=lambda row: str(row["conversation_id"]))
            if len(group) == 1:
                result[(group[0]["conversation_id"], fmt)] = Path(base_name)
                continue
            for idx, conv in enumerate(group, start=1):
                result[(conv["conversation_id"], fmt)] = Path(
                    _base_filename(conv, fmt, collision_suffix=f"_{idx:03d}")
                )
    return result


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


def render_markdown(conv: sqlite3.Row, nodes: list[sqlite3.Row]) -> str:
    # No exported_at here by design: default exported files must be byte-stable
    # for identical database contents and CLI parameters.
    lines = [
        f"# {conv['title'] or 'untitled'}",
        "",
        f"- conversation_id: `{conv['conversation_id']}`",
        f"- create_time: {epoch_to_display(conv['create_time'])}",
        f"- update_time: {epoch_to_display(conv['update_time'])}",
        f"- current_node: `{conv['current_node'] or ''}`",
        f"- source_file: `{conv['source_file'] or ''}`",
        "",
    ]
    for node in nodes:
        if not node["content_text"]:
            continue
        role = (node["role"] or "message").title()
        node_time = node["create_time"] if node["create_time"] is not None else node["update_time"]
        timestamp = epoch_to_display(node_time)
        heading = f"## {role}" + (f" {timestamp}" if timestamp else "")
        lines.extend([heading, "", node["content_text"], ""])
    return "\n".join(lines).rstrip() + "\n"


def render_txt(conv: sqlite3.Row, nodes: list[sqlite3.Row]) -> str:
    lines = [
        conv["title"] or "untitled",
        f"conversation_id: {conv['conversation_id']}",
        f"create_time: {epoch_to_display(conv['create_time'])}",
        f"update_time: {epoch_to_display(conv['update_time'])}",
        f"current_node: {conv['current_node'] or ''}",
        f"source_file: {conv['source_file'] or ''}",
        "=" * 72,
        "",
    ]
    for node in nodes:
        if not node["content_text"]:
            continue
        role = (node["role"] or "message").upper()
        node_time = node["create_time"] if node["create_time"] is not None else node["update_time"]
        timestamp = epoch_to_display(node_time)
        lines.extend([f"{role} {timestamp}".strip(), "-" * 72, node["content_text"], ""])
    return "\n".join(lines).rstrip() + "\n"


def manifest_row(conv: sqlite3.Row, fmt: str, relative_path: Path, output_hash: str) -> dict[str, Any]:
    return {
        "aggregate_hash": conv["aggregate_hash"],
        "conversation_id": conv["conversation_id"],
        "create_time": finite_float_or_none(conv["create_time"]),
        "current_node": conv["current_node"],
        "format": fmt,
        "output_hash": output_hash,
        "output_path": relative_path.as_posix(),
        "source_file": conv["source_file"],
        "title": conv["title"],
        "update_time": finite_float_or_none(conv["update_time"]),
    }


def write_manifest(out_dir: Path, rows: list[dict[str, Any]], force: bool = False) -> None:
    rows = sorted(rows, key=lambda row: (row["output_path"], row["conversation_id"], row["format"]))
    jsonl = out_dir / "manifest.jsonl"
    jsonl_text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    write_bytes_if_changed(jsonl, jsonl_text.encode("utf-8"), force=force)
    csv_path = out_dir / "manifest.csv"
    fieldnames = [
        "aggregate_hash",
        "conversation_id",
        "create_time",
        "current_node",
        "format",
        "output_hash",
        "output_path",
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
