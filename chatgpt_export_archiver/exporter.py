from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .db import export_query, record_export
from .utils import epoch_to_date_part, epoch_to_display, parse_date_boundary, safe_filename_part, sha256_bytes, write_bytes_if_changed


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
            stem = Path(base_name).stem
            suffix = Path(base_name).suffix
            for idx, conv in enumerate(group, start=1):
                result[(conv["conversation_id"], fmt)] = Path(f"{stem}_{idx:03d}{suffix}")
    return result


def _base_filename(conv: sqlite3.Row, fmt: str) -> str:
    date_part = epoch_to_date_part(conv["create_time"] or conv["update_time"])
    title = safe_filename_part(conv["title"])
    cid = safe_filename_part(str(conv["conversation_id"]), max_len=12)
    return f"{date_part}_{title}_{cid}.{fmt}"


def order_current_path(conv: sqlite3.Row, nodes: list[sqlite3.Row]) -> list[sqlite3.Row]:
    by_id = {row["node_id"]: row for row in nodes}
    current = conv["current_node"]
    ordered: list[sqlite3.Row] = []
    seen: set[str] = set()
    while current and current in by_id and current not in seen:
        seen.add(current)
        row = by_id[current]
        if row["is_on_current_path"]:
            ordered.append(row)
        current = row["parent_node_id"]
    ordered.reverse()
    if ordered:
        return ordered
    return sorted(
        nodes,
        key=lambda row: (
            row["create_time"] is None,
            row["create_time"] if row["create_time"] is not None else row["update_time"] if row["update_time"] is not None else 0,
            row["node_id"],
        ),
    )


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
        timestamp = epoch_to_display(node["create_time"] or node["update_time"])
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
        timestamp = epoch_to_display(node["create_time"] or node["update_time"])
        lines.extend([f"{role} {timestamp}".strip(), "-" * 72, node["content_text"], ""])
    return "\n".join(lines).rstrip() + "\n"


def manifest_row(conv: sqlite3.Row, fmt: str, relative_path: Path, output_hash: str) -> dict[str, Any]:
    return {
        "aggregate_hash": conv["aggregate_hash"],
        "conversation_id": conv["conversation_id"],
        "create_time": conv["create_time"],
        "current_node": conv["current_node"],
        "format": fmt,
        "output_hash": output_hash,
        "output_path": relative_path.as_posix(),
        "source_file": conv["source_file"],
        "title": conv["title"],
        "update_time": conv["update_time"],
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
