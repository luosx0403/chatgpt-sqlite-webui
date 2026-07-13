from __future__ import annotations

"""Synthetic effective-current scope/chain benchmark for release verification."""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatgpt_export_archiver.current_path import (
    ensure_effective_current_views,
    invalidate_effective_current_cache,
)
from chatgpt_export_archiver.db import init_db


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _elapsed(call) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


def benchmark_scopes(conversation_count: int) -> None:
    conn = _connection()
    try:
        conn.executemany(
            "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES (?, 'synthetic', ?, ?)",
            ((f"c{index}", f"n{index}", f"h{index}") for index in range(conversation_count)),
        )
        conn.executemany(
            "INSERT INTO conversation_nodes(conversation_id, node_id, is_on_current_path) VALUES (?, ?, 0)",
            ((f"c{index}", f"n{index}") for index in range(conversation_count)),
        )
        conn.commit()
        scopes = (
            ("conversation_detail", ["c0"]),
            ("conversation_messages", ["c0"]),
            ("conversation_page_60", [f"c{index}" for index in range(min(60, conversation_count))]),
            ("scoped_message_search", ["c0"]),
            ("global_current_search", None),
        )
        for label, ids in scopes:
            invalidate_effective_current_cache(conn)
            seconds = _elapsed(lambda: ensure_effective_current_views(conn, ids))
            conversations = conn.execute("SELECT COUNT(*) FROM effective_current_scope").fetchone()[0]
            nodes = conn.execute("SELECT COUNT(*) FROM effective_current_nodes").fetchone()[0]
            print(
                f"scope {label} conversations {conversations} nodes {nodes} seconds {seconds:.6f}"
            )
    finally:
        conn.close()


def benchmark_chains(depths: list[int]) -> None:
    for depth in depths:
        conn = _connection()
        try:
            conn.execute(
                "INSERT INTO conversations(conversation_id, title, current_node, aggregate_hash) VALUES ('chain', 'synthetic', ?, 'chain')",
                (f"n{depth - 1}",),
            )
            conn.executemany(
                "INSERT INTO conversation_nodes(conversation_id, node_id, parent_node_id, is_on_current_path) VALUES ('chain', ?, ?, 0)",
                ((f"n{index}", f"n{index - 1}" if index else None) for index in range(depth)),
            )
            conn.commit()
            seconds = _elapsed(lambda: ensure_effective_current_views(conn, ["chain"]))
            nodes = conn.execute("SELECT COUNT(*) FROM effective_current_nodes").fetchone()[0]
            print(f"chain depth {depth} nodes {nodes} seconds {seconds:.6f}")
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversations", type=int, default=20_000)
    parser.add_argument("--chains", default="1000,5000,10000")
    args = parser.parse_args()
    depths = [int(value) for value in args.chains.split(",") if value]
    benchmark_scopes(max(1, args.conversations))
    benchmark_chains(depths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
