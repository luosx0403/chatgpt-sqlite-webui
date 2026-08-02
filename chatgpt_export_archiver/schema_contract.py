"""Versioned runtime contracts that must not be conflated with each other."""

import re

API_SCHEMA_VERSION = 9
DATABASE_SCHEMA_VERSION = 6
OPTIONAL_WEB_INDEX_FORMAT_VERSION = "6"
OPTIONAL_WEB_INDEX_PREDECESSOR_FORMAT_VERSIONS = ("3", "4", "5")
STABLE_OPTIONAL_ADDRESS_VERSION = "1"
DISPLAY_TEXT_RESOLVER_VERSION = "2"
NORMALIZATION_INDEX_FORMAT_VERSION = "1"


def parse_nonnegative_integer(value: object) -> int | None:
    """Return a strict canonical nonnegative integer, never a coercion.

    SQLite canonical generation rows are stored as INTEGER while Web index
    metadata is TEXT.  Both representations are accepted deliberately; bool,
    floats, signs, whitespace, leading zeroes, and non-decimal strings are not.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 9_223_372_036_854_775_807 else None
    if (
        isinstance(value, str)
        and len(value) <= 19
        and re.fullmatch(r"(?:0|[1-9][0-9]*)", value)
    ):
        parsed = int(value)
        return parsed if parsed <= 9_223_372_036_854_775_807 else None
    return None
