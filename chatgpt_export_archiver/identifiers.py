from __future__ import annotations

import math
from typing import Any


MAX_CANONICAL_ID_LENGTH = 512


def unicode_scalar_text(value: str, *, replace_invalid: bool = False) -> tuple[str | None, bool]:
    """Combine valid surrogate pairs and reject or replace non-scalars."""

    result: list[str] = []
    changed = False
    index = 0
    while index < len(value):
        code = ord(value[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < len(value):
            low = ord(value[index + 1])
            if 0xDC00 <= low <= 0xDFFF:
                result.append(chr(0x10000 + ((code - 0xD800) << 10) + low - 0xDC00))
                changed = True
                index += 2
                continue
        if 0xD800 <= code <= 0xDFFF:
            if not replace_invalid:
                return None, True
            result.append("\ufffd")
            changed = True
        else:
            result.append(value[index])
        index += 1
    return "".join(result), changed


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
    result, _changed = unicode_scalar_text(result)
    if not identifier_text_is_safe(result, limit=MAX_CANONICAL_ID_LENGTH, allow_empty=False):
        return None
    return result


def identifier_text_is_safe(
    value: str,
    *,
    limit: int,
    allow_empty: bool = True,
) -> bool:
    """Apply the shared canonical/legacy identifier Unicode policy.

    Unicode noncharacters are valid scalar values and are intentionally
    accepted. Controls, DEL, isolated surrogates, and overlong values are not.
    """

    if not isinstance(value, str) or len(value) > limit or (not value and not allow_empty):
        return False
    return not any(
        ord(character) <= 0x1F
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    )


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
