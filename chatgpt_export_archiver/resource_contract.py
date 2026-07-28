from __future__ import annotations

"""Authoritative cross-entry-point import materialization limits."""

IMPORT_BATCH_MAX_CONVERSATIONS = 100
IMPORT_BATCH_MAX_NODES = 20_000
IMPORT_BATCH_MAX_INPUT_BYTES = 32 * 1024 * 1024
IMPORT_BATCH_MAX_DECODED_CHARS = 32 * 1024 * 1024
IMPORT_BATCH_MAX_RAW_BYTES = 24 * 1024 * 1024
IMPORT_BATCH_MAX_METADATA_BYTES = 16 * 1024 * 1024
IMPORT_BATCH_MAX_ESTIMATED_HEAP_BYTES = 96 * 1024 * 1024
IMPORT_BATCH_MAX_SQLITE_BIND_BYTES = 48 * 1024 * 1024


def import_batch_resource_profile() -> dict[str, int]:
    """Return a fresh JSON-safe snapshot for CLI jobs and API documentation."""

    return {
        "max_conversations": IMPORT_BATCH_MAX_CONVERSATIONS,
        "max_nodes": IMPORT_BATCH_MAX_NODES,
        "max_input_bytes": IMPORT_BATCH_MAX_INPUT_BYTES,
        "max_decoded_chars": IMPORT_BATCH_MAX_DECODED_CHARS,
        "max_raw_bytes": IMPORT_BATCH_MAX_RAW_BYTES,
        "max_metadata_bytes": IMPORT_BATCH_MAX_METADATA_BYTES,
        "max_estimated_heap_bytes": IMPORT_BATCH_MAX_ESTIMATED_HEAP_BYTES,
        "max_sqlite_bind_bytes": IMPORT_BATCH_MAX_SQLITE_BIND_BYTES,
    }
