from __future__ import annotations

import sqlite3
from array import array
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence, TypeVar


RowT = TypeVar("RowT", bound=Mapping[str, Any])
MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION = 100_000
MAX_EFFECTIVE_CURRENT_GRAPH_BYTES_PER_CONVERSATION = 128 * 1024 * 1024
MAX_EFFECTIVE_CURRENT_SCOPE_NODES = 1_000_000
MAX_EFFECTIVE_CURRENT_SCOPE_INPUT_BYTES = 512 * 1024 * 1024
MAX_EFFECTIVE_CURRENT_TEMP_BYTES = 1024 * 1024 * 1024
MAX_EFFECTIVE_CURRENT_CONVERSATIONS = 100_000
EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS = 20_000
EFFECTIVE_CURRENT_SCOPE_BATCH_NODES = 20_000
EFFECTIVE_CURRENT_SCOPE_BATCH_INPUT_BYTES = 64 * 1024 * 1024


class EffectiveCurrentResourceLimitError(ValueError):
    def __init__(self, code: str = "effective_current_node_limit_exceeded"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EffectiveCurrentCollection:
    """Resolved reader collection without changing archived raw path flags."""

    node_ids: tuple[str, ...]
    source: str
    current_node_exists: bool
    current_path_fallback_to_all: bool
    effective_path: str
    cycle_detected: bool = False
    missing_parent: bool = False
    cross_conversation_parent: bool = False
    partial_chain: bool = False
    raw_flag_count: int = 0
    raw_flag_leaf_count: int = 0
    selected_chain_cycle_detected: bool = False
    raw_flag_cycle_detected: bool = False
    selected_chain_missing_parent: bool = False
    raw_flag_missing_parent: bool = False
    selected_chain_cross_conversation_parent: bool = False
    raw_flag_cross_conversation_parent: bool = False


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _walk_parent_chain(
    start: str,
    by_id: Mapping[str, Mapping[str, Any]],
    *,
    require_flag: bool,
) -> tuple[list[str], bool, bool, str | None]:
    reversed_ids: list[str] = []
    seen: set[str] = set()
    current: str | None = start
    cycle = False
    missing_parent = False
    terminal_parent: str | None = None
    while current:
        if current in seen:
            cycle = True
            break
        row = by_id.get(current)
        if row is None or (require_flag and not bool(_row_value(row, "is_on_current_path"))):
            missing_parent = bool(reversed_ids)
            terminal_parent = current
            break
        seen.add(current)
        reversed_ids.append(current)
        parent = _row_value(row, "parent_node_id")
        current = str(parent) if parent not in (None, "") else None
    reversed_ids.reverse()
    return reversed_ids, cycle, missing_parent, terminal_parent


def _raw_flag_topology_diagnostics(
    by_id: Mapping[str, Mapping[str, Any]],
    flag_ids: set[str],
    foreign_ids: set[str],
) -> tuple[bool, bool, bool]:
    """Diagnose every raw-flag component, including components with no leaf."""

    parents = {
        node_id: (
            str(parent)
            if (parent := _row_value(by_id[node_id], "parent_node_id")) not in (None, "")
            else None
        )
        for node_id in flag_ids
    }
    cycle = False
    state: dict[str, int] = {}
    for start in sorted(flag_ids):
        current: str | None = start
        trail: list[str] = []
        positions: dict[str, int] = {}
        while current in flag_ids and state.get(current, 0) == 0:
            if current in positions:
                cycle = True
                break
            positions[current] = len(trail)
            trail.append(current)
            current = parents.get(current)
        if current in positions:
            cycle = True
        for node_id in trail:
            state[node_id] = 2

    missing = False
    cross = False
    for parent in parents.values():
        if parent is None or parent in flag_ids:
            continue
        if parent not in by_id and parent in foreign_ids:
            cross = True
        else:
            # A local but unflagged parent is ineligible for a raw-flag chain;
            # a wholly absent parent is likewise incomplete.
            missing = True
    return cycle, missing, cross


def resolve_effective_current_collection(
    current_node: str | None,
    rows: Sequence[RowT],
    *,
    foreign_node_ids: Iterable[str] = (),
) -> EffectiveCurrentCollection:
    """Resolve the effective current collection using the documented precedence.

    The returned ``node_ids`` are in root-to-leaf display order. A valid
    conversation-owned current node is authoritative even if every raw flag is
    false. Raw flags are consulted only when that node is absent or invalid.
    """

    by_id: dict[str, RowT] = {str(row["node_id"]): row for row in rows}
    flag_ids = {node_id for node_id, row in by_id.items() if bool(_row_value(row, "is_on_current_path"))}
    flag_parents = {
        str(parent)
        for node_id in flag_ids
        if (parent := _row_value(by_id[node_id], "parent_node_id")) not in (None, "") and str(parent) in flag_ids
    }
    flag_leaves = sorted(flag_ids - flag_parents)
    current_key = str(current_node) if current_node not in (None, "") else None
    current_exists = bool(current_key and current_key in by_id)
    foreign_ids = {str(value) for value in foreign_node_ids}
    raw_cycle, raw_missing, raw_cross = _raw_flag_topology_diagnostics(by_id, flag_ids, foreign_ids)

    if current_exists and current_key is not None:
        node_ids, cycle, missing_parent, terminal_parent = _walk_parent_chain(
            current_key, by_id, require_flag=False
        )
        if node_ids:
            cross_parent = bool(terminal_parent and terminal_parent not in by_id and terminal_parent in foreign_ids)
            missing_parent = missing_parent and not cross_parent
            combined_cycle = cycle or raw_cycle
            combined_missing = missing_parent or raw_missing
            combined_cross = cross_parent or raw_cross
            return EffectiveCurrentCollection(
                tuple(node_ids),
                "current_node",
                True,
                False,
                "current",
                cycle_detected=combined_cycle,
                missing_parent=combined_missing,
                cross_conversation_parent=combined_cross,
                partial_chain=combined_cycle or combined_missing or combined_cross,
                raw_flag_count=len(flag_ids),
                raw_flag_leaf_count=len(flag_leaves),
                selected_chain_cycle_detected=cycle,
                raw_flag_cycle_detected=raw_cycle,
                selected_chain_missing_parent=missing_parent,
                raw_flag_missing_parent=raw_missing,
                selected_chain_cross_conversation_parent=cross_parent,
                raw_flag_cross_conversation_parent=raw_cross,
            )

    for leaf in flag_leaves:
        node_ids, cycle, missing_parent, terminal_parent = _walk_parent_chain(
            leaf, by_id, require_flag=True
        )
        if node_ids:
            cross_parent = bool(terminal_parent and terminal_parent not in by_id and terminal_parent in foreign_ids)
            missing_parent = missing_parent and not cross_parent
            combined_cycle = cycle or raw_cycle
            combined_missing = missing_parent or raw_missing
            combined_cross = cross_parent or raw_cross
            return EffectiveCurrentCollection(
                tuple(node_ids),
                "raw_flags",
                current_exists,
                False,
                "current",
                cycle_detected=combined_cycle,
                missing_parent=combined_missing,
                cross_conversation_parent=combined_cross,
                partial_chain=combined_cycle or combined_missing or combined_cross,
                raw_flag_count=len(flag_ids),
                raw_flag_leaf_count=len(flag_leaves),
                selected_chain_cycle_detected=cycle,
                raw_flag_cycle_detected=raw_cycle,
                selected_chain_missing_parent=missing_parent,
                raw_flag_missing_parent=raw_missing,
                selected_chain_cross_conversation_parent=cross_parent,
                raw_flag_cross_conversation_parent=raw_cross,
            )

    all_ids = tuple(
        str(row["node_id"])
        for row in sorted(
            rows,
            key=lambda row: (
                _row_value(row, "create_time") is None,
                _row_value(row, "create_time")
                if _row_value(row, "create_time") is not None
                else _row_value(row, "update_time")
                if _row_value(row, "update_time") is not None
                else 0,
                str(row["node_id"]),
            ),
        )
    )
    return EffectiveCurrentCollection(
        all_ids,
        "fallback_all",
        current_exists,
        bool(all_ids),
        "all" if all_ids else "current",
        raw_flag_count=len(flag_ids),
        raw_flag_leaf_count=len(flag_leaves),
        cycle_detected=raw_cycle,
        missing_parent=raw_missing,
        cross_conversation_parent=raw_cross,
        partial_chain=raw_cycle or raw_missing or raw_cross,
        raw_flag_cycle_detected=raw_cycle,
        raw_flag_missing_parent=raw_missing,
        raw_flag_cross_conversation_parent=raw_cross,
    )


_EFFECTIVE_CURRENT_TABLES_SQL = (
    "CREATE TEMP TABLE IF NOT EXISTS effective_current_scope (conversation_id TEXT PRIMARY KEY)",
    """CREATE TEMP TABLE IF NOT EXISTS effective_current_nodes (
           conversation_id TEXT NOT NULL,
           node_id TEXT NOT NULL,
           depth INTEGER,
           source TEXT NOT NULL,
           cycle_detected INTEGER NOT NULL DEFAULT 0,
           PRIMARY KEY (conversation_id, node_id)
       )""",
    """CREATE TEMP TABLE IF NOT EXISTS effective_current_meta (
           conversation_id TEXT PRIMARY KEY,
           node_count INTEGER NOT NULL,
           raw_flag_count INTEGER NOT NULL,
           raw_flag_leaf_count INTEGER NOT NULL,
           current_node_exists INTEGER NOT NULL,
           current_collection_source TEXT NOT NULL,
           current_path_fallback_to_all INTEGER NOT NULL,
           effective_path TEXT NOT NULL,
           cycle_detected INTEGER NOT NULL,
           missing_parent INTEGER NOT NULL,
           cross_conversation_parent INTEGER NOT NULL,
           partial_chain INTEGER NOT NULL
           ,selected_chain_cycle_detected INTEGER NOT NULL
           ,raw_flag_cycle_detected INTEGER NOT NULL
           ,selected_chain_missing_parent INTEGER NOT NULL
           ,raw_flag_missing_parent INTEGER NOT NULL
           ,selected_chain_cross_conversation_parent INTEGER NOT NULL
           ,raw_flag_cross_conversation_parent INTEGER NOT NULL
       )""",
    """CREATE TEMP TABLE IF NOT EXISTS effective_current_cache_state (
           scope_mode TEXT NOT NULL,
           total_changes INTEGER NOT NULL,
           data_version INTEGER NOT NULL
       )""",
)


_SCOPED_EFFECTIVE_CURRENT_SQL = """
WITH RECURSIVE
valid_current AS (
    SELECT c.conversation_id, n.node_id, n.parent_node_id, n.node_id AS leaf_node_id
    FROM effective_current_scope scope
    CROSS JOIN conversations c
    CROSS JOIN conversation_nodes n
    WHERE c.conversation_id = scope.conversation_id
      AND n.conversation_id = c.conversation_id
      AND n.node_id = c.current_node
      AND c.current_node IS NOT NULL AND c.current_node <> ''
),
current_walk(conversation_id, node_id, parent_node_id, leaf_node_id) AS (
    SELECT conversation_id, node_id, parent_node_id, leaf_node_id
    FROM valid_current
    UNION
    SELECT p.conversation_id, p.node_id, p.parent_node_id, w.leaf_node_id
    FROM current_walk w
    JOIN conversation_nodes p
      ON p.conversation_id = w.conversation_id
     AND p.node_id = w.parent_node_id
),
flag_leaf_candidates AS (
    SELECT n.conversation_id, n.node_id, n.parent_node_id,
           row_number() OVER (PARTITION BY n.conversation_id ORDER BY n.node_id) AS leaf_rank
    FROM effective_current_scope scope
    CROSS JOIN conversation_nodes n
    WHERE n.conversation_id = scope.conversation_id
      AND n.is_on_current_path = 1
      AND NOT EXISTS (
          SELECT 1 FROM valid_current vc WHERE vc.conversation_id = scope.conversation_id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM conversation_nodes child
          WHERE child.conversation_id = n.conversation_id
            AND child.is_on_current_path = 1
            AND child.parent_node_id = n.node_id
      )
),
flag_walk(conversation_id, node_id, parent_node_id, leaf_node_id) AS (
    SELECT conversation_id, node_id, parent_node_id, node_id
    FROM flag_leaf_candidates
    WHERE leaf_rank = 1
    UNION
    SELECT p.conversation_id, p.node_id, p.parent_node_id, w.leaf_node_id
    FROM flag_walk w
    JOIN conversation_nodes p
      ON p.conversation_id = w.conversation_id
     AND p.node_id = w.parent_node_id
     AND p.is_on_current_path = 1
),
chosen_chain AS (
    SELECT conversation_id, node_id, parent_node_id, leaf_node_id, 'current_node' AS source
    FROM current_walk
    UNION ALL
    SELECT conversation_id, node_id, parent_node_id, leaf_node_id, 'raw_flags' AS source
    FROM flag_walk
),
fallback_conversations AS (
    SELECT scope.conversation_id
    FROM effective_current_scope scope
    WHERE NOT EXISTS (
        SELECT 1 FROM chosen_chain chain WHERE chain.conversation_id = scope.conversation_id
    )
)
SELECT conversation_id, node_id, parent_node_id, leaf_node_id, source
FROM chosen_chain
UNION ALL
SELECT n.conversation_id, n.node_id, n.parent_node_id, NULL, 'fallback_all'
FROM fallback_conversations f
CROSS JOIN conversation_nodes n
WHERE n.conversation_id = f.conversation_id;
"""


def invalidate_effective_current_cache(conn: sqlite3.Connection) -> None:
    """Invalidate connection-local derived state after graph mutations."""

    for table in (
        "effective_current_cache_state",
        "effective_current_meta",
        "effective_current_nodes",
        "effective_current_scope",
    ):
        conn.execute(f"DROP TABLE IF EXISTS temp.{table}")


def _data_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA data_version").fetchone()[0])


def _scope_is_current(conn: sqlite3.Connection, ids: list[str] | None) -> bool:
    try:
        state = conn.execute(
            "SELECT scope_mode, total_changes, data_version FROM effective_current_cache_state"
        ).fetchone()
    except sqlite3.Error:
        return False
    if not state:
        return False
    if int(state[1]) != conn.total_changes or int(state[2]) != _data_version(conn):
        return False
    if ids is None:
        return state[0] == "all"
    if state[0] == "all":
        return True
    if not ids:
        return True
    count = 0
    for offset in range(0, len(ids), 400):
        batch = ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        count += int(
            conn.execute(
                f"SELECT COUNT(*) FROM effective_current_scope WHERE conversation_id IN ({placeholders})",
                batch,
            ).fetchone()[0]
        )
    return int(count) == len(ids)


def ensure_effective_current_views(
    conn: sqlite3.Connection,
    conversation_ids: Iterable[str] | None,
) -> None:
    """Materialize effective-current state only for the requested scope.

    ``conversation_ids=None`` is the explicit global-search mode. All ordinary
    detail, reader, selected-item, and page enrichment callers pass finite IDs.
    Recursive membership uses ``UNION`` deduplication, so cycles terminate
    without a growing visited-path string.
    """

    global_scope = conversation_ids is None
    if _scope_is_current(conn, None):
        return
    for statement in _EFFECTIVE_CURRENT_TABLES_SQL:
        conn.execute(statement)
    finite_scope_current = False
    if not global_scope:
        try:
            state = conn.execute(
                "SELECT scope_mode, total_changes, data_version FROM effective_current_cache_state"
            ).fetchone()
            finite_scope_current = bool(
                state
                and state[0] == "ids"
                and int(state[1]) == conn.total_changes
                and int(state[2]) == _data_version(conn)
            )
        except sqlite3.Error:
            finite_scope_current = False
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS effective_current_requested_scope("
            "conversation_id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        conn.execute("DELETE FROM effective_current_requested_scope")
        pending_ids: list[tuple[str]] = []
        requested_id_bytes = 0
        for value in conversation_ids:
            identifier = str(value)
            requested_id_bytes += len(identifier.encode("utf-8", errors="surrogatepass"))
            if requested_id_bytes > MAX_EFFECTIVE_CURRENT_SCOPE_INPUT_BYTES:
                raise EffectiveCurrentResourceLimitError("effective_current_scope_too_large")
            pending_ids.append((identifier,))
            if len(pending_ids) >= 400:
                conn.executemany(
                    "INSERT OR IGNORE INTO effective_current_requested_scope VALUES (?)",
                    pending_ids,
                )
                pending_ids.clear()
        if pending_ids:
            conn.executemany(
                "INSERT OR IGNORE INTO effective_current_requested_scope VALUES (?)",
                pending_ids,
            )
        requested_count = int(
            conn.execute("SELECT COUNT(*) FROM effective_current_requested_scope").fetchone()[0]
        )
        if requested_count > MAX_EFFECTIVE_CURRENT_CONVERSATIONS:
            raise EffectiveCurrentResourceLimitError("effective_current_scope_too_large")
        temp_page_size = int(conn.execute("PRAGMA temp.page_size").fetchone()[0])
        temp_page_count = int(conn.execute("PRAGMA temp.page_count").fetchone()[0])
        if temp_page_size * temp_page_count > MAX_EFFECTIVE_CURRENT_TEMP_BYTES:
            raise EffectiveCurrentResourceLimitError("effective_current_scope_too_large")
        if finite_scope_current:
            # A materialized superset is valid for a smaller page/enrichment
            # request.  Retaining it is essential for selected-item checks
            # that run after page metadata has been added.  Compare actual IDs
            # (not cardinality) so equal-sized but different scopes still
            # rebuild correctly.
            missing = conn.execute(
                """SELECT 1 FROM (
                       SELECT conversation_id FROM effective_current_requested_scope
                       EXCEPT SELECT conversation_id FROM effective_current_scope
                   )
                   LIMIT 1"""
            ).fetchone()
            if missing is None:
                conn.execute(
                    "UPDATE effective_current_cache_state SET total_changes = ?",
                    (conn.total_changes + 1,),
                )
                return
    conn.execute("DELETE FROM effective_current_cache_state")
    conn.execute("DELETE FROM effective_current_meta")
    conn.execute("DELETE FROM effective_current_nodes")
    conn.execute("DELETE FROM effective_current_scope")
    if global_scope:
        # Refuse an oversized global request before copying every identity into
        # TEMP.  Node and graph-byte limits are checked in the same main-schema
        # aggregate, so the rejection path creates no large scope table.
        scope_totals = conn.execute(
            """SELECT COUNT(DISTINCT c.conversation_id), COUNT(n.node_id),
                      COALESCE(SUM(
                          length(CAST(COALESCE(n.node_id, '') AS BLOB)) +
                          length(CAST(COALESCE(n.parent_node_id, '') AS BLOB))
                      ), 0)
               FROM conversations c
               LEFT JOIN conversation_nodes n
                 ON n.conversation_id = c.conversation_id"""
        ).fetchone()
        scope_conversations = int(scope_totals[0] or 0)
        scope_nodes = int(scope_totals[1] or 0)
        scope_bytes = int(scope_totals[2] or 0)
        estimated_temp_bytes = scope_bytes + scope_nodes * 96
        if (
            scope_conversations > MAX_EFFECTIVE_CURRENT_CONVERSATIONS
            or scope_nodes > MAX_EFFECTIVE_CURRENT_SCOPE_NODES
            or scope_bytes > MAX_EFFECTIVE_CURRENT_SCOPE_INPUT_BYTES
            or estimated_temp_bytes > MAX_EFFECTIVE_CURRENT_TEMP_BYTES
        ):
            raise EffectiveCurrentResourceLimitError("effective_current_scope_too_large")
        conn.execute("INSERT INTO effective_current_scope SELECT conversation_id FROM conversations")
    else:
        conn.execute(
            "INSERT INTO effective_current_scope "
            "SELECT conversation_id FROM effective_current_requested_scope"
        )

    if global_scope:
        _materialize_global_effective_current(conn)
        return

    scope_totals = conn.execute(
        """SELECT COUNT(DISTINCT scope.conversation_id), COUNT(n.node_id),
                  COALESCE(SUM(
                      length(CAST(COALESCE(n.node_id, '') AS BLOB)) +
                      length(CAST(COALESCE(n.parent_node_id, '') AS BLOB))
                  ), 0)
           FROM effective_current_scope scope
           LEFT JOIN conversation_nodes n
             ON n.conversation_id = scope.conversation_id"""
    ).fetchone()
    scope_conversations = int(scope_totals[0] or 0)
    scope_nodes = int(scope_totals[1] or 0)
    scope_bytes = int(scope_totals[2] or 0)
    if (
        scope_conversations > MAX_EFFECTIVE_CURRENT_CONVERSATIONS
        or scope_nodes > MAX_EFFECTIVE_CURRENT_SCOPE_NODES
        or scope_bytes > MAX_EFFECTIVE_CURRENT_SCOPE_INPUT_BYTES
        or scope_bytes + scope_nodes * 96 > MAX_EFFECTIVE_CURRENT_TEMP_BYTES
    ):
        raise EffectiveCurrentResourceLimitError("effective_current_scope_too_large")
    if (
        scope_conversations > 1
        and (
            scope_conversations > EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS
            or scope_nodes > EFFECTIVE_CURRENT_SCOPE_BATCH_NODES
            or scope_bytes > EFFECTIVE_CURRENT_SCOPE_BATCH_INPUT_BYTES
        )
    ):
        _materialize_finite_effective_current_in_batches(conn)
        return

    oversized = conn.execute(
        """WITH scope_stats AS (
               SELECT scope.conversation_id,
                      (SELECT COUNT(*)
                       FROM conversation_nodes n INDEXED BY idx_nodes_conversation_path
                       WHERE n.conversation_id = scope.conversation_id) AS node_count,
                      (SELECT COALESCE(SUM(
                           COALESCE(length(CAST(n.node_id AS BLOB)), 0) +
                           COALESCE(length(CAST(n.parent_node_id AS BLOB)), 0)
                       ), 0)
                       FROM conversation_nodes n INDEXED BY idx_nodes_conversation_path
                       WHERE n.conversation_id = scope.conversation_id) AS graph_bytes
               FROM effective_current_scope scope
           )
           SELECT conversation_id, node_count, graph_bytes
           FROM scope_stats
           WHERE node_count > ? OR graph_bytes > ?
           LIMIT 1""",
        (
            MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION,
            MAX_EFFECTIVE_CURRENT_GRAPH_BYTES_PER_CONVERSATION,
        ),
    ).fetchone()
    if oversized is not None:
        code = (
            "effective_current_node_limit_exceeded"
            if int(oversized[1]) > MAX_EFFECTIVE_CURRENT_NODES_PER_CONVERSATION
            else "effective_current_input_limit_exceeded"
        )
        raise EffectiveCurrentResourceLimitError(code)

    _materialize_scoped_effective_current_sql(conn)
    conn.execute(
        "INSERT INTO effective_current_cache_state VALUES (?, ?, ?)",
        ("all" if global_scope else "ids", conn.total_changes + 1, _data_version(conn)),
    )


def _raw_flag_cycle_conversations(conn: sqlite3.Connection) -> Iterable[str]:
    """Stream raw-flag cycles using compact integer arrays, not graph strings."""

    conn.execute("DROP TABLE IF EXISTS temp.effective_current_raw_graph")
    conn.execute(
        """CREATE TEMP TABLE effective_current_raw_graph(
               conversation_id TEXT NOT NULL,
               node_ordinal INTEGER NOT NULL,
               parent_ordinal INTEGER,
               PRIMARY KEY(conversation_id, node_ordinal)
           ) WITHOUT ROWID"""
    )
    conn.execute(
        """INSERT INTO effective_current_raw_graph
           WITH flags AS (
               SELECT n.conversation_id, n.node_id, n.parent_node_id,
                      row_number() OVER (
                          PARTITION BY n.conversation_id ORDER BY n.node_id
                      ) AS node_ordinal
               FROM effective_current_scope scope
               CROSS JOIN conversation_nodes n INDEXED BY idx_nodes_conversation_flag_parent
               WHERE n.conversation_id = scope.conversation_id
                 AND n.is_on_current_path = 1
           )
           SELECT child.conversation_id, child.node_ordinal, parent.node_ordinal
           FROM flags child
           LEFT JOIN flags parent
             ON parent.conversation_id = child.conversation_id
            AND parent.node_id = child.parent_node_id"""
    )
    current_id: str | None = None
    parents = array("i", [0])

    def has_cycle(parent_ordinals: array) -> bool:
        colors = bytearray(len(parent_ordinals))
        for start in range(1, len(parent_ordinals)):
            if colors[start]:
                continue
            trail = array("i")
            node = start
            while node > 0 and colors[node] == 0:
                colors[node] = 1
                trail.append(node)
                node = parent_ordinals[node]
            cycle = node > 0 and colors[node] == 1
            for visited in trail:
                colors[visited] = 2
            if cycle:
                return True
        return False

    for row in conn.execute(
        "SELECT conversation_id, node_ordinal, parent_ordinal "
        "FROM effective_current_raw_graph ORDER BY conversation_id, node_ordinal"
    ):
        conversation_id = str(row[0])
        if current_id is not None and conversation_id != current_id:
            if has_cycle(parents):
                yield current_id
            parents = array("i", [0])
        current_id = conversation_id
        ordinal = int(row[1])
        while len(parents) <= ordinal:
            parents.append(0)
        parents[ordinal] = int(row[2] or 0)
    if current_id is not None and has_cycle(parents):
        yield current_id


def _materialize_scoped_effective_current_sql(conn: sqlite3.Connection) -> None:
    """Build one finite scope with TEMP SQL plus compact raw-cycle state."""

    work_tables = (
        "effective_current_work_membership",
        "effective_current_work_stats",
        "effective_current_work_raw_diag",
        "effective_current_raw_graph",
    )
    for name in work_tables:
        conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
    try:
        conn.execute(
            """CREATE TEMP TABLE effective_current_work_membership(
                   conversation_id TEXT NOT NULL,
                   node_id TEXT NOT NULL,
                   parent_node_id TEXT,
                   leaf_node_id TEXT,
                   source TEXT NOT NULL,
                   PRIMARY KEY(conversation_id, node_id)
               ) WITHOUT ROWID"""
        )
        conn.execute(
            "INSERT INTO effective_current_work_membership "
            + _SCOPED_EFFECTIVE_CURRENT_SQL
        )
        conn.execute(
            """CREATE TEMP TABLE effective_current_work_stats(
                   conversation_id TEXT PRIMARY KEY,
                   node_count INTEGER NOT NULL,
                   raw_flag_count INTEGER NOT NULL,
                   raw_flag_leaf_count INTEGER NOT NULL,
                   current_node_exists INTEGER NOT NULL,
                   source TEXT NOT NULL,
                   selected_cycle INTEGER NOT NULL,
                   selected_missing INTEGER NOT NULL,
                   selected_cross INTEGER NOT NULL
               ) WITHOUT ROWID"""
        )
        conn.execute(
            """INSERT INTO effective_current_work_stats
               WITH node_stats AS (
                   SELECT scope.conversation_id, COUNT(n.node_id) AS node_count,
                          COALESCE(SUM(n.is_on_current_path = 1), 0) AS raw_flag_count,
                          COALESCE(MAX(n.node_id = c.current_node), 0) AS current_node_exists
                   FROM effective_current_scope scope
                   JOIN conversations c ON c.conversation_id = scope.conversation_id
                   LEFT JOIN conversation_nodes n
                     ON n.conversation_id = scope.conversation_id
                   GROUP BY scope.conversation_id
               ), membership_stats AS (
                   SELECT m.conversation_id, MIN(m.source) AS source,
                          COUNT(*) AS chain_count,
                          SUM(CASE WHEN parent.node_id IS NOT NULL THEN 1 ELSE 0 END)
                              AS internal_edges
                   FROM effective_current_work_membership m
                   LEFT JOIN effective_current_work_membership parent
                     ON parent.conversation_id = m.conversation_id
                    AND parent.node_id = m.parent_node_id
                   GROUP BY m.conversation_id
               )
               SELECT stats.conversation_id, stats.node_count, stats.raw_flag_count,
                      (SELECT COUNT(*) FROM conversation_nodes leaf
                       WHERE leaf.conversation_id = stats.conversation_id
                         AND leaf.is_on_current_path = 1
                         AND NOT EXISTS (
                             SELECT 1 FROM conversation_nodes child
                             WHERE child.conversation_id = leaf.conversation_id
                               AND child.is_on_current_path = 1
                               AND child.parent_node_id = leaf.node_id
                         )),
                      stats.current_node_exists,
                      COALESCE(membership.source, 'fallback_all'),
                      CASE WHEN membership.source <> 'fallback_all'
                                AND membership.internal_edges >= membership.chain_count
                           THEN 1 ELSE 0 END,
                      CASE WHEN membership.source <> 'fallback_all' AND EXISTS (
                               SELECT 1 FROM effective_current_work_membership terminal
                               WHERE terminal.conversation_id = stats.conversation_id
                                 AND terminal.parent_node_id IS NOT NULL
                                 AND terminal.parent_node_id <> ''
                                 AND NOT EXISTS (
                                     SELECT 1 FROM effective_current_work_membership parent
                                     WHERE parent.conversation_id = terminal.conversation_id
                                       AND parent.node_id = terminal.parent_node_id
                                 )
                                 AND (
                                     EXISTS (
                                         SELECT 1 FROM conversation_nodes local_parent
                                         WHERE local_parent.conversation_id = terminal.conversation_id
                                           AND local_parent.node_id = terminal.parent_node_id
                                     ) OR NOT EXISTS (
                                         SELECT 1 FROM conversation_nodes any_parent
                                         WHERE any_parent.node_id = terminal.parent_node_id
                                     )
                                 )
                           ) THEN 1 ELSE 0 END,
                      CASE WHEN membership.source <> 'fallback_all' AND EXISTS (
                               SELECT 1 FROM effective_current_work_membership terminal
                               WHERE terminal.conversation_id = stats.conversation_id
                                 AND terminal.parent_node_id IS NOT NULL
                                 AND terminal.parent_node_id <> ''
                                 AND NOT EXISTS (
                                     SELECT 1 FROM effective_current_work_membership parent
                                     WHERE parent.conversation_id = terminal.conversation_id
                                       AND parent.node_id = terminal.parent_node_id
                                 )
                                 AND NOT EXISTS (
                                     SELECT 1 FROM conversation_nodes local_parent
                                     WHERE local_parent.conversation_id = terminal.conversation_id
                                       AND local_parent.node_id = terminal.parent_node_id
                                 )
                                 AND EXISTS (
                                     SELECT 1 FROM conversation_nodes foreign_parent
                                     WHERE foreign_parent.conversation_id <> terminal.conversation_id
                                       AND foreign_parent.node_id = terminal.parent_node_id
                                 )
                           ) THEN 1 ELSE 0 END
               FROM node_stats stats
               LEFT JOIN membership_stats membership
                 ON membership.conversation_id = stats.conversation_id"""
        )
        conn.execute(
            """CREATE TEMP TABLE effective_current_work_raw_diag(
                   conversation_id TEXT PRIMARY KEY,
                   raw_cycle INTEGER NOT NULL DEFAULT 0,
                   raw_missing INTEGER NOT NULL,
                   raw_cross INTEGER NOT NULL
               ) WITHOUT ROWID"""
        )
        conn.execute(
            """INSERT INTO effective_current_work_raw_diag
               SELECT stats.conversation_id, 0,
                      CASE WHEN EXISTS (
                           SELECT 1 FROM conversation_nodes flagged
                           WHERE flagged.conversation_id = stats.conversation_id
                             AND flagged.is_on_current_path = 1
                             AND flagged.parent_node_id IS NOT NULL
                             AND flagged.parent_node_id <> ''
                             AND NOT EXISTS (
                                 SELECT 1 FROM conversation_nodes flag_parent
                                 WHERE flag_parent.conversation_id = flagged.conversation_id
                                   AND flag_parent.node_id = flagged.parent_node_id
                                   AND flag_parent.is_on_current_path = 1
                             )
                             AND (
                                 EXISTS (
                                     SELECT 1 FROM conversation_nodes local_parent
                                     WHERE local_parent.conversation_id = flagged.conversation_id
                                       AND local_parent.node_id = flagged.parent_node_id
                                 ) OR NOT EXISTS (
                                     SELECT 1 FROM conversation_nodes any_parent
                                     WHERE any_parent.node_id = flagged.parent_node_id
                                 )
                             )
                      ) THEN 1 ELSE 0 END,
                      CASE WHEN EXISTS (
                           SELECT 1 FROM conversation_nodes flagged
                           WHERE flagged.conversation_id = stats.conversation_id
                             AND flagged.is_on_current_path = 1
                             AND flagged.parent_node_id IS NOT NULL
                             AND flagged.parent_node_id <> ''
                             AND NOT EXISTS (
                                 SELECT 1 FROM conversation_nodes local_parent
                                 WHERE local_parent.conversation_id = flagged.conversation_id
                                   AND local_parent.node_id = flagged.parent_node_id
                             )
                             AND EXISTS (
                                 SELECT 1 FROM conversation_nodes foreign_parent
                                 WHERE foreign_parent.conversation_id <> flagged.conversation_id
                                   AND foreign_parent.node_id = flagged.parent_node_id
                             )
                      ) THEN 1 ELSE 0 END
               FROM effective_current_work_stats stats"""
        )
        conn.executemany(
            "UPDATE effective_current_work_raw_diag SET raw_cycle = 1 "
            "WHERE conversation_id = ?",
            ((conversation_id,) for conversation_id in _raw_flag_cycle_conversations(conn)),
        )
        conn.execute(
            """INSERT INTO effective_current_nodes(
                   conversation_id, node_id, depth, source, cycle_detected
               )
               SELECT membership.conversation_id, membership.node_id, NULL,
                      membership.source, 0
               FROM effective_current_work_membership membership
               WHERE membership.source = 'fallback_all'"""
        )
        conn.execute(
            """INSERT INTO effective_current_nodes(
                   conversation_id, node_id, depth, source, cycle_detected
               )
               WITH RECURSIVE chain(
                   conversation_id, node_id, parent_node_id, source, depth, max_depth
               ) AS (
                   SELECT membership.conversation_id, membership.node_id,
                          membership.parent_node_id, membership.source, 0,
                          stats.node_count
                   FROM effective_current_work_membership membership
                   JOIN effective_current_work_stats stats
                     ON stats.conversation_id = membership.conversation_id
                   WHERE membership.source <> 'fallback_all'
                     AND membership.node_id = membership.leaf_node_id
                   UNION ALL
                   SELECT parent.conversation_id, parent.node_id,
                          parent.parent_node_id, parent.source,
                          chain.depth + 1, chain.max_depth
                   FROM chain
                   JOIN effective_current_work_membership parent
                     ON parent.conversation_id = chain.conversation_id
                    AND parent.node_id = chain.parent_node_id
                   WHERE chain.depth + 1 < chain.max_depth
               )
               SELECT chain.conversation_id, chain.node_id, MIN(chain.depth),
                      chain.source, stats.selected_cycle
               FROM chain
               JOIN effective_current_work_stats stats
                 ON stats.conversation_id = chain.conversation_id
               GROUP BY chain.conversation_id, chain.node_id, chain.source"""
        )
        conn.execute(
            """INSERT INTO effective_current_meta
               SELECT stats.conversation_id, stats.node_count, stats.raw_flag_count,
                      stats.raw_flag_leaf_count, stats.current_node_exists, stats.source,
                      CASE WHEN stats.source = 'fallback_all' AND stats.node_count > 0
                           THEN 1 ELSE 0 END,
                      CASE WHEN stats.source = 'fallback_all' AND stats.node_count > 0
                           THEN 'all' ELSE 'current' END,
                      (stats.selected_cycle OR raw.raw_cycle),
                      (stats.selected_missing OR raw.raw_missing),
                      (stats.selected_cross OR raw.raw_cross),
                      (stats.selected_cycle OR raw.raw_cycle OR
                       stats.selected_missing OR raw.raw_missing OR
                       stats.selected_cross OR raw.raw_cross),
                      stats.selected_cycle, raw.raw_cycle,
                      stats.selected_missing, raw.raw_missing,
                      stats.selected_cross, raw.raw_cross
               FROM effective_current_work_stats stats
               JOIN effective_current_work_raw_diag raw
                 ON raw.conversation_id = stats.conversation_id"""
        )
    finally:
        for name in work_tables:
            conn.execute(f"DROP TABLE IF EXISTS temp.{name}")


def _materialize_global_effective_current(conn: sqlite3.Connection) -> None:
    """Build global state through bounded conversation/node batches.

    The ordinary finite-scope resolver remains the single semantics source.
    Global exclusion-only searches aggregate its results in TEMP SQLite rather
    than retaining all archive topology in Python dictionaries.
    """

    aggregate_nodes = "effective_current_nodes_global_build"
    aggregate_meta = "effective_current_meta_global_build"
    conn.execute(f"DROP TABLE IF EXISTS temp.{aggregate_nodes}")
    conn.execute(f"DROP TABLE IF EXISTS temp.{aggregate_meta}")
    conn.execute(
        f"CREATE TEMP TABLE {aggregate_nodes} AS SELECT * FROM effective_current_nodes WHERE 0"
    )
    conn.execute(
        f"CREATE TEMP TABLE {aggregate_meta} AS SELECT * FROM effective_current_meta WHERE 0"
    )

    def materialize_batch(batch: list[str]) -> None:
        if not batch:
            return
        ensure_effective_current_views(conn, batch)
        conn.execute(
            f"INSERT INTO {aggregate_nodes} SELECT * FROM effective_current_nodes"
        )
        conn.execute(
            f"INSERT INTO {aggregate_meta} SELECT * FROM effective_current_meta"
        )

    try:
        last_conversation_id = ""
        batch: list[str] = []
        batch_nodes = 0
        batch_bytes = 0
        while True:
            rows = conn.execute(
                """SELECT c.conversation_id, COUNT(n.node_id) AS node_count,
                          COALESCE(SUM(
                              COALESCE(length(CAST(n.node_id AS BLOB)), 0) +
                              COALESCE(length(CAST(n.parent_node_id AS BLOB)), 0)
                          ), 0) AS graph_bytes
                   FROM conversations AS c
                   LEFT JOIN conversation_nodes AS n
                     ON n.conversation_id = c.conversation_id
                   WHERE c.conversation_id > ?
                   GROUP BY c.conversation_id
                   ORDER BY c.conversation_id
                   LIMIT ?""",
                (last_conversation_id, EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                conversation_id = str(row[0])
                node_count = int(row[1] or 0)
                graph_bytes = int(row[2] or 0)
                if batch and (
                    len(batch) >= EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS
                    or batch_nodes + node_count > EFFECTIVE_CURRENT_SCOPE_BATCH_NODES
                    or batch_bytes + graph_bytes > EFFECTIVE_CURRENT_SCOPE_BATCH_INPUT_BYTES
                ):
                    materialize_batch(batch)
                    batch = []
                    batch_nodes = 0
                    batch_bytes = 0
                batch.append(conversation_id)
                batch_nodes += node_count
                batch_bytes += graph_bytes
                last_conversation_id = conversation_id
            if len(rows) < EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS:
                break
        materialize_batch(batch)

        conn.execute("DELETE FROM effective_current_cache_state")
        conn.execute("DELETE FROM effective_current_meta")
        conn.execute("DELETE FROM effective_current_nodes")
        conn.execute("DELETE FROM effective_current_scope")
        conn.execute(
            "INSERT INTO effective_current_scope SELECT conversation_id FROM conversations"
        )
        conn.execute(
            f"INSERT INTO effective_current_nodes SELECT * FROM {aggregate_nodes}"
        )
        conn.execute(
            f"INSERT INTO effective_current_meta SELECT * FROM {aggregate_meta}"
        )
        conn.execute(
            "INSERT INTO effective_current_cache_state VALUES (?, ?, ?)",
            ("all", conn.total_changes + 1, _data_version(conn)),
        )
    finally:
        conn.execute(f"DROP TABLE IF EXISTS temp.{aggregate_nodes}")
        conn.execute(f"DROP TABLE IF EXISTS temp.{aggregate_meta}")


def _materialize_finite_effective_current_in_batches(conn: sqlite3.Connection) -> None:
    """Materialize a large finite request without a full-scope Python graph."""

    outer_scope = "effective_current_outer_scope"
    aggregate_nodes = "effective_current_nodes_finite_build"
    aggregate_meta = "effective_current_meta_finite_build"
    for name in (outer_scope, aggregate_nodes, aggregate_meta):
        conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
    conn.execute(
        f"CREATE TEMP TABLE {outer_scope}(conversation_id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    conn.execute(
        f"INSERT INTO {outer_scope} SELECT conversation_id FROM effective_current_scope"
    )
    conn.execute(
        f"CREATE TEMP TABLE {aggregate_nodes} AS SELECT * FROM effective_current_nodes WHERE 0"
    )
    conn.execute(
        f"CREATE TEMP TABLE {aggregate_meta} AS SELECT * FROM effective_current_meta WHERE 0"
    )

    def materialize(batch: list[str]) -> None:
        if not batch:
            return
        ensure_effective_current_views(conn, batch)
        conn.execute(f"INSERT INTO {aggregate_nodes} SELECT * FROM effective_current_nodes")
        conn.execute(f"INSERT INTO {aggregate_meta} SELECT * FROM effective_current_meta")

    try:
        last_id = ""
        batch: list[str] = []
        batch_nodes = 0
        batch_bytes = 0
        while True:
            rows = conn.execute(
                f"""SELECT scope.conversation_id, COUNT(n.node_id),
                           COALESCE(SUM(
                               COALESCE(length(CAST(n.node_id AS BLOB)), 0) +
                               COALESCE(length(CAST(n.parent_node_id AS BLOB)), 0)
                           ), 0)
                    FROM {outer_scope} scope
                    LEFT JOIN conversation_nodes n
                      ON n.conversation_id = scope.conversation_id
                    WHERE scope.conversation_id > ?
                    GROUP BY scope.conversation_id
                    ORDER BY scope.conversation_id
                    LIMIT ?""",
                (last_id, EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                conversation_id = str(row[0])
                node_count = int(row[1] or 0)
                graph_bytes = int(row[2] or 0)
                if batch and (
                    len(batch) >= EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS
                    or batch_nodes + node_count > EFFECTIVE_CURRENT_SCOPE_BATCH_NODES
                    or batch_bytes + graph_bytes > EFFECTIVE_CURRENT_SCOPE_BATCH_INPUT_BYTES
                ):
                    materialize(batch)
                    batch = []
                    batch_nodes = 0
                    batch_bytes = 0
                batch.append(conversation_id)
                batch_nodes += node_count
                batch_bytes += graph_bytes
                last_id = conversation_id
            if len(rows) < EFFECTIVE_CURRENT_SCOPE_BATCH_ROWS:
                break
        materialize(batch)
        conn.execute("DELETE FROM effective_current_cache_state")
        conn.execute("DELETE FROM effective_current_meta")
        conn.execute("DELETE FROM effective_current_nodes")
        conn.execute("DELETE FROM effective_current_scope")
        conn.execute(f"INSERT INTO effective_current_scope SELECT * FROM {outer_scope}")
        conn.execute(f"INSERT INTO effective_current_nodes SELECT * FROM {aggregate_nodes}")
        conn.execute(f"INSERT INTO effective_current_meta SELECT * FROM {aggregate_meta}")
        conn.execute(
            "INSERT INTO effective_current_cache_state VALUES (?, ?, ?)",
            ("ids", conn.total_changes + 1, _data_version(conn)),
        )
    finally:
        for name in (outer_scope, aggregate_nodes, aggregate_meta):
            conn.execute(f"DROP TABLE IF EXISTS temp.{name}")


def effective_current_metadata(conn: sqlite3.Connection, conversation_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    # Spool and deduplicate the caller's iterable directly.  Do not first make
    # an unbounded list plus set plus sorted copy for large search scopes.
    changes_before_spool = conn.total_changes
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS effective_current_requested_meta("
        "conversation_id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    conn.execute("DELETE FROM effective_current_requested_meta")
    pending: list[tuple[str]] = []
    requested_bytes = 0
    for value in conversation_ids:
        conversation_id = str(value)
        requested_bytes += len(conversation_id.encode("utf-8", errors="surrogatepass"))
        if requested_bytes > MAX_EFFECTIVE_CURRENT_SCOPE_INPUT_BYTES:
            raise EffectiveCurrentResourceLimitError("effective_current_scope_too_large")
        pending.append((conversation_id,))
        if len(pending) >= 400:
            conn.executemany(
                "INSERT OR IGNORE INTO effective_current_requested_meta VALUES (?)",
                pending,
            )
            pending.clear()
    if pending:
        conn.executemany(
            "INSERT OR IGNORE INTO effective_current_requested_meta VALUES (?)",
            pending,
        )
    requested_count = int(
        conn.execute("SELECT COUNT(*) FROM effective_current_requested_meta").fetchone()[0]
    )
    if requested_count == 0:
        return {}
    if requested_count > MAX_EFFECTIVE_CURRENT_CONVERSATIONS:
        raise EffectiveCurrentResourceLimitError("effective_current_scope_too_large")
    # TEMP request-spool writes are not graph mutations. Preserve an existing
    # valid superset cache, but never mask canonical changes made since it was
    # published on this connection.
    try:
        state = conn.execute(
            "SELECT total_changes, data_version FROM effective_current_cache_state"
        ).fetchone()
    except sqlite3.Error:
        state = None
    if (
        state is not None
        and int(state[0]) == changes_before_spool
        and int(state[1]) == _data_version(conn)
    ):
        conn.execute(
            "UPDATE effective_current_cache_state SET total_changes = ?",
            (conn.total_changes + 1,),
        )
    ensure_effective_current_views(
        conn,
        (
            str(row[0])
            for row in conn.execute(
                "SELECT conversation_id FROM effective_current_requested_meta ORDER BY conversation_id"
            )
        ),
    )
    rows = conn.execute(
        """SELECT meta.*
           FROM effective_current_meta meta
           JOIN effective_current_requested_meta request
             ON request.conversation_id = meta.conversation_id"""
    ).fetchall()
    # Request-table maintenance is not canonical graph mutation. Keep the
    # materialized scope cache current after these TEMP writes.
    conn.execute(
        "UPDATE effective_current_cache_state SET total_changes = ?",
        (conn.total_changes + 1,),
    )
    return {
        row["conversation_id"]: {
            "node_count": int(row["node_count"] or 0),
            "current_path_nodes": int(row["raw_flag_count"] or 0),
            "current_node_exists": bool(row["current_node_exists"]),
            "current_collection_source": row["current_collection_source"],
            "current_path_fallback_to_all": bool(row["current_path_fallback_to_all"]),
            "effective_path": row["effective_path"],
            "raw_flag_leaf_count": int(row["raw_flag_leaf_count"] or 0),
            "cycle_detected": bool(row["cycle_detected"]),
            "missing_parent": bool(row["missing_parent"]),
            "cross_conversation_parent": bool(row["cross_conversation_parent"]),
            "partial_chain": bool(row["partial_chain"]),
            "selected_chain_cycle_detected": bool(row["selected_chain_cycle_detected"]),
            "raw_flag_cycle_detected": bool(row["raw_flag_cycle_detected"]),
            "selected_chain_missing_parent": bool(row["selected_chain_missing_parent"]),
            "raw_flag_missing_parent": bool(row["raw_flag_missing_parent"]),
            "selected_chain_cross_conversation_parent": bool(row["selected_chain_cross_conversation_parent"]),
            "raw_flag_cross_conversation_parent": bool(row["raw_flag_cross_conversation_parent"]),
        }
        for row in rows
    }
