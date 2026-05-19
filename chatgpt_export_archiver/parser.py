from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .utils import canonical_json, compact_json, sha256_text


@dataclass
class WarningRecord:
    source_file: str
    array_index: int | None
    warning_type: str
    keys_json: str | None = None
    raw_json: str | None = None


@dataclass
class ParsedNode:
    node_id: str
    conversation_id: str
    parent_node_id: str | None
    children_json: str
    message_id: str | None
    role: str | None
    author_name: str | None
    create_time: float | None
    update_time: float | None
    content_type: str | None
    content_text: str
    content_hash: str
    metadata_json: str | None
    is_on_current_path: int
    raw_message_json: str | None
    children_for_hash: Any | None = None
    metadata_for_hash: Any | None = None
    raw_message_for_hash: Any | None = None


@dataclass
class ParsedConversation:
    conversation_id: str
    exported_id: str | None
    title: str | None
    create_time: float | None
    update_time: float | None
    current_node: str | None
    source_file: str
    source_array_index: int
    aggregate_hash: str
    is_archived: int | None
    is_starred: int | None
    default_model_slug: str | None
    metadata_json: str
    nodes: list[ParsedNode]
    warnings: list[WarningRecord]


def validate_conversation_element(value: Any, source_file: str, array_index: int) -> WarningRecord | None:
    if not isinstance(value, dict):
        return WarningRecord(
            source_file=source_file,
            array_index=array_index,
            warning_type="invalid_element_type",
            keys_json=None,
            raw_json=compact_json({"type": type(value).__name__}),
        )
    keys_json = compact_json(sorted(value.keys()))
    if _normalize_conversation_id(value.get("id")) is None and _normalize_conversation_id(value.get("conversation_id")) is None:
        warning_type = "missing_id" if "id" not in value and "conversation_id" not in value else "invalid_conversation_id"
        return WarningRecord(
            source_file=source_file,
            array_index=array_index,
            warning_type=warning_type,
            keys_json=keys_json,
            raw_json=compact_json(
                {
                    "id_type": type(value.get("id")).__name__ if "id" in value else "missing",
                    "conversation_id_type": type(value.get("conversation_id")).__name__ if "conversation_id" in value else "missing",
                }
            ),
        )
    if "mapping" not in value:
        return WarningRecord(
            source_file=source_file,
            array_index=array_index,
            warning_type="missing_mapping",
            keys_json=keys_json,
            raw_json=None,
        )
    if not isinstance(value.get("mapping"), dict):
        return WarningRecord(
            source_file=source_file,
            array_index=array_index,
            warning_type="invalid_mapping_type",
            keys_json=keys_json,
            raw_json=compact_json({"id_type": type(value.get("id")).__name__, "mapping_type": type(value.get("mapping")).__name__}, 1000),
        )
    return None


def parse_conversation(value: dict[str, Any], source_file: str, array_index: int) -> ParsedConversation:
    conversation_id = _normalize_conversation_id(value.get("id")) or _normalize_conversation_id(value.get("conversation_id"))
    if conversation_id is None:
        raise ValueError("parse_conversation called with invalid conversation id")
    exported_id = _normalize_conversation_id(value.get("conversation_id"))
    mapping = value.get("mapping") or {}
    current_node = str(value.get("current_node")) if value.get("current_node") is not None else None
    warnings: list[WarningRecord] = []
    title = _normalize_title(value.get("title"), source_file, array_index, warnings)
    current_path = _compute_current_path(mapping, current_node, source_file, array_index, warnings)
    current_set = set(current_path)
    nodes: list[ParsedNode] = []

    for node_id, node in mapping.items():
        node_key = str(node_id)
        if not isinstance(node, dict):
            warnings.append(
                WarningRecord(
                    source_file=source_file,
                    array_index=array_index,
                    warning_type="invalid_node_type",
                    keys_json=compact_json([node_key]),
                    raw_json=compact_json({"node_id": node_key, "type": type(node).__name__}, 1000),
                )
            )
            continue
        message = node.get("message")
        message_dict = message if isinstance(message, dict) else None
        content_type, content_text, metadata = extract_message_content(message_dict)
        message_id = str(message_dict.get("id")) if message_dict and message_dict.get("id") is not None else None
        author = message_dict.get("author") if message_dict else None
        author = author if isinstance(author, dict) else {}
        role = str(author.get("role")) if author.get("role") is not None else None
        author_name = str(author.get("name")) if author.get("name") is not None else None
        metadata_value = message_dict.get("metadata") if message_dict else None
        combined_metadata = {
            "node_metadata": node.get("metadata") if isinstance(node.get("metadata"), dict) else None,
            "message_metadata": metadata_value,
            "content_notes": metadata,
        }
        children_value = node.get("children") if isinstance(node.get("children"), list) else []
        nodes.append(
            ParsedNode(
                node_id=node_key,
                conversation_id=conversation_id,
                parent_node_id=str(node.get("parent")) if node.get("parent") is not None else None,
                children_json=compact_json(children_value),
                message_id=message_id,
                role=role,
                author_name=author_name,
                create_time=_to_float(message_dict.get("create_time") if message_dict else node.get("create_time")),
                update_time=_to_float(message_dict.get("update_time") if message_dict else node.get("update_time")),
                content_type=content_type,
                content_text=content_text,
                content_hash=sha256_text(canonical_json({"content_type": content_type, "content_text": content_text})),
                metadata_json=compact_json(combined_metadata),
                is_on_current_path=1 if node_key in current_set else 0,
                raw_message_json=compact_json(message_dict) if message_dict is not None else None,
                children_for_hash=children_value,
                metadata_for_hash=combined_metadata,
                raw_message_for_hash=message_dict,
            )
        )

    aggregate_hash = compute_aggregate_hash(current_node, nodes)
    metadata_keys = [
        "async_status",
        "atlas_mode_enabled",
        "context_scopes",
        "conversation_origin",
        "conversation_template_id",
        "disabled_tool_ids",
        "gizmo_type",
        "is_do_not_remember",
        "memory_scope",
        "moderation_results",
        "voice",
    ]
    metadata_json = compact_json({k: value.get(k) for k in metadata_keys if k in value})
    return ParsedConversation(
        conversation_id=conversation_id,
        exported_id=exported_id,
        title=title,
        create_time=_to_float(value.get("create_time")),
        update_time=_to_float(value.get("update_time")),
        current_node=current_node,
        source_file=source_file,
        source_array_index=array_index,
        aggregate_hash=aggregate_hash,
        is_archived=_to_int_bool(value.get("is_archived")),
        is_starred=_to_int_bool(value.get("is_starred")),
        default_model_slug=str(value.get("default_model_slug")) if value.get("default_model_slug") is not None else None,
        metadata_json=metadata_json,
        nodes=nodes,
        warnings=warnings,
    )


def _normalize_conversation_id(value: Any) -> str | None:
    """Return a usable conversation id, rejecting empty values and containers."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _normalize_title(value: Any, source_file: str, array_index: int, warnings: list[WarningRecord]) -> str | None:
    """Normalize title to SQLite-safe str|None without recording raw content."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    warnings.append(
        WarningRecord(
            source_file=source_file,
            array_index=array_index,
            warning_type="invalid_title_type",
            keys_json=compact_json({"title_type": type(value).__name__}),
            raw_json=None,
        )
    )
    return None


def _compute_current_path(
    mapping: dict[str, Any],
    current_node: str | None,
    source_file: str,
    array_index: int,
    warnings: list[WarningRecord],
) -> list[str]:
    if not current_node:
        return []
    if current_node not in mapping:
        warnings.append(WarningRecord(source_file, array_index, "current_node_missing", compact_json([current_node]), None))
        return []
    path: list[str] = []
    seen: set[str] = set()
    node_id: str | None = current_node
    while node_id:
        if node_id in seen:
            warnings.append(WarningRecord(source_file, array_index, "parent_cycle", compact_json(path + [node_id]), None))
            break
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            warnings.append(WarningRecord(source_file, array_index, "current_path_invalid_node", compact_json([node_id]), None))
            break
        path.append(node_id)
        parent = node.get("parent")
        if parent is None:
            break
        parent_id = str(parent)
        if parent_id not in mapping:
            warnings.append(WarningRecord(source_file, array_index, "parent_missing", compact_json([node_id, parent_id]), None))
            break
        node_id = parent_id
    path.reverse()
    return path


def extract_message_content(message: dict[str, Any] | None) -> tuple[str | None, str, list[str]]:
    if not message:
        return None, "", []
    content = message.get("content")
    if not isinstance(content, dict):
        if content is None:
            return None, "", []
        return "unknown", f"[non-text content: {type(content).__name__}]", ["content_not_object"]
    content_type = str(content.get("content_type")) if content.get("content_type") is not None else None
    parts = content.get("parts")
    notes: list[str] = []
    if isinstance(parts, list):
        extracted = [_extract_part_text(part, notes) for part in parts]
        text = "\n\n".join(part for part in extracted if part)
    elif isinstance(parts, str):
        text = parts
    elif parts is None:
        # Some export content types store text in content fields other than
        # parts, for example user editable context and multimodal structures.
        remainder = {k: v for k, v in content.items() if k != "content_type"}
        text = _extract_part_text(remainder, notes) if remainder else ""
    else:
        text = _extract_part_text(parts, notes)
    if text:
        return content_type, text, notes
    if content_type and content_type != "text":
        return content_type, f"[non-text content: {content_type}]", notes
    return content_type, "", notes


def _extract_part_text(part: Any, notes: list[str], depth: int = 0) -> str:
    if depth > 8:
        notes.append("max_depth_reached")
        return ""
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        for key in ("text", "content", "value", "name"):
            value = part.get(key)
            if isinstance(value, str) and value:
                notes.append(f"dict_part_text_field:{key}")
                return value
        extracted: list[str] = []
        for key in sorted(part):
            value = part.get(key)
            if value in (None, "", [], {}):
                continue
            child = _extract_part_text(value, notes, depth + 1)
            if child:
                label = str(key).replace("_", " ")
                extracted.append(f"{label}: {child}")
        if extracted:
            notes.append("dict_part_recursive_text")
            return "\n\n".join(extracted)
        notes.append("dict_part_without_text")
        return ""
    if isinstance(part, list):
        notes.append("list_part")
        extracted = [_extract_part_text(item, notes, depth + 1) for item in part]
        return "\n\n".join(item for item in extracted if item)
    if part is None:
        return ""
    notes.append(f"scalar_part:{type(part).__name__}")
    return f"[non-text part: {type(part).__name__}]"


def compute_aggregate_hash(current_node: str | None, nodes: list[ParsedNode]) -> str:
    core = {
        "current_node": current_node,
        "nodes": [
            {
                "node_id": n.node_id,
                "parent_node_id": n.parent_node_id,
                "children_json": n.children_for_hash if n.children_for_hash is not None else json.loads(n.children_json or "[]"),
                "message_id": n.message_id,
                "role": n.role,
                "author_name": n.author_name,
                "create_time": n.create_time,
                "update_time": n.update_time,
                "content_type": n.content_type,
                "content_text": n.content_text,
                "content_hash": n.content_hash,
                "metadata_json": n.metadata_for_hash if n.metadata_for_hash is not None else json.loads(n.metadata_json or "{}"),
                "is_on_current_path": n.is_on_current_path,
                "raw_message_json": (
                    n.raw_message_for_hash
                    if n.raw_message_for_hash is not None
                    else json.loads(n.raw_message_json or "null")
                ),
            }
            for n in sorted(nodes, key=lambda item: item.node_id)
        ],
    }
    return sha256_text(canonical_json(core))


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_bool(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value != 0 else 0
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "t", "yes", "y", "1", "on"}:
            return 1
        if normalized in {"false", "f", "no", "n", "0", "off"}:
            return 0
        return None
    return 1 if bool(value) else 0
