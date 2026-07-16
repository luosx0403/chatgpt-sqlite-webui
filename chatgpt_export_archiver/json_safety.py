from __future__ import annotations

import math
from typing import Any


# Shared import, legacy-raw, reader, and API encoder resource contract.
MAX_JSON_NESTING_DEPTH = 256
MAX_JSON_SCALAR_COUNT = 100_000
MAX_RAW_PREVIEW_NODES = 100_000
MAX_RAW_PREVIEW_BYTES = 80_000
# This ceiling covers complete API payloads.  It must remain above the reader's
# independently enforced 2 MiB estimated page budget, including JSON structure
# and bounded per-row metadata, while still placing a fixed upper bound on the
# iterative encoder copy.
MAX_SANITIZED_OUTPUT_BYTES = 4 * 1024 * 1024


class JsonSafetyLimitError(ValueError):
    def __init__(self, code: str, *, limit: int) -> None:
        super().__init__(code)
        self.code = code
        self.limit = limit


def validate_json_lexical_limits(text: str) -> None:
    """Validate JSON nesting/scalar budgets without recursive parsing."""

    depth = 0
    scalars = 0
    in_string = False
    escaped = False
    token = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                scalars += 1
            continue
        if char == '"':
            if token:
                scalars += 1
                token = False
            in_string = True
        elif char in "[{":
            if token:
                scalars += 1
                token = False
            depth += 1
            if depth > MAX_JSON_NESTING_DEPTH:
                raise JsonSafetyLimitError(
                    "json_nesting_limit_exceeded", limit=MAX_JSON_NESTING_DEPTH
                )
        elif char in "]},:":
            if token:
                scalars += 1
                token = False
            if char in "]}":
                depth = max(0, depth - 1)
        elif char in " \t\r\n":
            if token:
                scalars += 1
                token = False
        else:
            token = True
        if scalars > MAX_JSON_SCALAR_COUNT:
            raise JsonSafetyLimitError(
                "json_scalar_limit_exceeded", limit=MAX_JSON_SCALAR_COUNT
            )
    if token:
        scalars += 1
    if scalars > MAX_JSON_SCALAR_COUNT:
        raise JsonSafetyLimitError("json_scalar_limit_exceeded", limit=MAX_JSON_SCALAR_COUNT)


def sanitize_json_value(value: Any, *, omit_metadata: bool = False) -> Any:
    """Copy a JSON-like value iteratively while enforcing shared budgets."""

    if not isinstance(value, (dict, list, tuple)):
        return _safe_scalar(value)
    root: Any = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any, int]] = [(value, root, 1)]
    nodes = 0
    scalars = 0
    output_bytes = 0
    while stack:
        source, target, depth = stack.pop()
        if depth > MAX_JSON_NESTING_DEPTH:
            raise JsonSafetyLimitError(
                "json_nesting_limit_exceeded", limit=MAX_JSON_NESTING_DEPTH
            )
        items = source.items() if isinstance(source, dict) else enumerate(source)
        pending: list[tuple[Any, Any, int]] = []
        for key, item in items:
            if omit_metadata and isinstance(source, dict) and str(key) == "metadata":
                continue
            nodes += 1
            if nodes > MAX_RAW_PREVIEW_NODES:
                raise JsonSafetyLimitError(
                    "json_preview_node_limit_exceeded", limit=MAX_RAW_PREVIEW_NODES
                )
            safe_key = str(key)
            if isinstance(source, dict):
                safe_key = _safe_text(safe_key)
                output_bytes += len(safe_key.encode("utf-8"))
            if isinstance(item, dict):
                child: Any = {}
                if isinstance(source, dict):
                    target[safe_key] = child
                else:
                    target.append(child)
                pending.append((item, child, depth + 1))
            elif isinstance(item, (list, tuple)):
                child = []
                if isinstance(source, dict):
                    target[safe_key] = child
                else:
                    target.append(child)
                pending.append((item, child, depth + 1))
            else:
                safe = _safe_scalar(item)
                scalars += 1
                if scalars > MAX_JSON_SCALAR_COUNT:
                    raise JsonSafetyLimitError(
                        "json_scalar_limit_exceeded", limit=MAX_JSON_SCALAR_COUNT
                    )
                output_bytes += len(str(safe).encode("utf-8"))
                if output_bytes > MAX_SANITIZED_OUTPUT_BYTES:
                    raise JsonSafetyLimitError(
                        "json_sanitized_output_limit_exceeded",
                        limit=MAX_SANITIZED_OUTPUT_BYTES,
                    )
                if isinstance(source, dict):
                    target[safe_key] = safe
                else:
                    target.append(safe)
        stack.extend(reversed(pending))
    return root


def _safe_text(value: str) -> str:
    return value.replace("\x00", "\ufffd")


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(str(value))
