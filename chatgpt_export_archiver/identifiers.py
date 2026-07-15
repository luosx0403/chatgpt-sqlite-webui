from __future__ import annotations

import math
from typing import Any


MAX_CANONICAL_ID_LENGTH = 512


def canonical_id_text(value: Any, *, strip: bool = False) -> str | None:
    """Return an addressable scalar ID without truncating or coercing containers."""

    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return None
    if isinstance(value, str):
        result = value.strip() if strip else value
    elif isinstance(value, int):
        result = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        result = str(value)
    else:
        return None
    if not result or len(result) > MAX_CANONICAL_ID_LENGTH:
        return None
    return result


def canonical_id_length(value: Any, *, strip: bool = False) -> int | None:
    """Return scalar ID length for safe diagnostics, never the ID value."""

    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return None
    if isinstance(value, str):
        result = value.strip() if strip else value
    elif isinstance(value, int):
        result = str(value)
    elif isinstance(value, float) and math.isfinite(value):
        result = str(value)
    else:
        return None
    return len(result)
