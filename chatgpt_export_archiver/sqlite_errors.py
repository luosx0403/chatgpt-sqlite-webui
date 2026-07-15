"""Narrow, shared SQLite error classification for optional capabilities."""

from __future__ import annotations

import re
import sqlite3


OPTIONAL_SEARCH_OBJECTS = frozenset({
    "message_fts",
    "web_message_trigram",
    "web_title_trigram",
    "web_message_norm",
    "web_title_norm",
    "web_index_metadata",
})
MESSAGE_FTS_OBJECTS = frozenset({
    "message_fts",
    "message_fts_config",
    "message_fts_content",
    "message_fts_data",
    "message_fts_docsize",
    "message_fts_idx",
})


def sqlite_error_message(exc: sqlite3.Error) -> str:
    return str(exc).strip().casefold()


def is_fts5_capability_unavailable(exc: sqlite3.Error) -> bool:
    message = sqlite_error_message(exc)
    return "no such module: fts5" in message or ("no such tokenizer" in message and "trigram" in message)


def is_optional_search_capability_missing(exc: sqlite3.Error) -> bool:
    if is_fts5_capability_unavailable(exc):
        return True
    match = re.fullmatch(r"no such table:\s*(?:main\.)?([a-z_][a-z0-9_]*)", sqlite_error_message(exc))
    if match and match.group(1) in OPTIONAL_SEARCH_OBJECTS | MESSAGE_FTS_OBJECTS:
        return True
    return is_optional_message_fts_damaged(exc)


def is_optional_message_fts_damaged(exc: sqlite3.Error) -> bool:
    """Recognize corruption explicitly attributed to the rebuildable FTS table.

    A generic ``database disk image is malformed`` is deliberately excluded:
    only SQLite diagnostics naming ``message_fts`` are safe to downgrade.
    """

    message = sqlite_error_message(exc)
    return (
        "malformed inverted index for fts5 table" in message
        and re.search(r"(?:^|\.)message_fts(?:\b|_)", message) is not None
    )


def sqlite_runtime_error_code(exc: sqlite3.Error) -> str:
    message = sqlite_error_message(exc)
    if "malformed" in message or "not a database" in message:
        return "database_malformed"
    if "locked" in message or "busy" in message:
        return "database_locked"
    if "readonly" in message or "read-only" in message:
        return "database_readonly"
    if "disk i/o" in message or "i/o error" in message:
        return "database_io_error"
    return "database_runtime_failure"
