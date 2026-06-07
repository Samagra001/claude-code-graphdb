"""Kùzu-backed code graph.

One `Node` table for all node kinds (Module, Class, Method, Model, Controller,
Action, Route, Job, Mailer, Concern, View, etc. — distinguished by the `kind`
column). One REL table per edge kind so Cypher patterns stay clean:
`MATCH (a)-[:CALLS]->(b)` instead of property filters.

Schema is created on first connect and reused thereafter. To rebuild from
scratch, construct CodeGraph(..., reset=True) which wipes the .kuzu directory.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

import kuzu


GRAPH_DIR_NAME = ".graphdb"
GRAPH_DB_NAME = "graph.kuzu"


def default_cache_dir() -> Path:
    """Where to store .kuzu databases by default.

    Lives OUTSIDE the indexed project so we never write into the user's repo.
    Override with the GRAPHDB_CACHE_DIR environment variable.
    """
    env = os.environ.get("GRAPHDB_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache" / "graphdb"


def default_db_path_for(root: str | Path) -> Path:
    """Stable per-target db path under the shared cache dir.

    Uses <name>-<8-char hash of abs path> so two repos with the same basename
    don't collide.
    """
    root = Path(root).resolve()
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:8]
    return default_cache_dir() / f"{root.name}-{digest}.kuzu"


# Node property schema. Kùzu requires all properties declared up front; new
# columns need ALTER TABLE. We declare a generous superset so every phase can
# write without touching the schema.
NODE_PROPS = """
    id STRING,
    kind STRING,
    name STRING,
    qualname STRING,
    file STRING,
    line INT64,
    signature STRING,
    docstring STRING,
    visibility STRING,
    table_name STRING,
    http_method STRING,
    url_pattern STRING,
    PRIMARY KEY (id)
"""


# Edge tables. Generic Ruby + Rails-specific. Declared up front; populated as
# phase extractors come online.
#
# NB: Avoid Kùzu reserved words as column names (e.g. `optional`, `except`,
# `from`, `to`). We use prefixed variants like `is_optional`, `except_actions`.
EDGE_TABLES: list[tuple[str, str]] = [
    # Generic Ruby
    ("CONTAINS",      ""),
    ("CALLS",         "line INT64"),
    ("INHERITS",      ""),
    ("INCLUDES",      "mode STRING"),  # include / extend / prepend
    # Rails models
    ("HAS_MANY",      "as_name STRING, through STRING, class_name STRING"),
    ("BELONGS_TO",    "as_name STRING, class_name STRING, is_optional BOOL"),
    ("HAS_ONE",       "as_name STRING, class_name STRING"),
    ("HABTM",         "as_name STRING"),
    ("VALIDATES",     "field STRING, rule STRING"),
    ("SCOPES",        "scope_name STRING"),
    # Rails routes / controllers
    ("HANDLES",       "verb STRING, url STRING"),
    ("BEFORE_ACTION", "only_actions STRING, except_actions STRING"),
    ("AFTER_ACTION",  "only_actions STRING, except_actions STRING"),
    ("SKIP_BEFORE",   "only_actions STRING, except_actions STRING"),
    ("RENDERS",       "template STRING"),
    # Async / messaging
    ("ENQUEUES",      "method STRING, line INT64"),
    ("DELIVERS",      "method STRING, line INT64"),
    ("MOUNTS",        "path STRING"),
]


# Node column list — must match NODE_PROPS keys. Used to normalize UNWIND rows
# so every row has the same shape (Kùzu requires uniform parameters).
_NODE_COLS = (
    "id", "kind", "name", "qualname", "file", "line",
    "signature", "docstring", "visibility",
    "table_name", "http_method", "url_pattern",
)


def _edge_attr_keys(kind: str) -> list[str]:
    """Extract the property column names declared for a given REL TABLE."""
    schema = dict(EDGE_TABLES).get(kind, "")
    schema = schema.strip()
    if not schema:
        return []
    return [piece.strip().split()[0] for piece in schema.split(",")]


class CodeGraph:
    """Wraps a Kùzu database with helpers for code-graph CRUD + Cypher queries.

    Writes are buffered in memory and bulk-flushed via UNWIND on the next
    query (auto-flush) or explicit `flush()`. This brings full-repo indexing
    of smart-hub-backend (~3-4k nodes) from minutes to a few seconds.
    """

    def __init__(
        self,
        root: str | Path,
        db_path: str | Path | None = None,
        reset: bool = False,
    ) -> None:
        self.root = str(Path(root).resolve())
        if db_path is None:
            db_path = default_db_path_for(self.root)
        db_path = Path(db_path)
        if reset and db_path.exists():
            shutil.rmtree(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._db = kuzu.Database(str(db_path))
        self._conn = kuzu.Connection(self._db)
        self._ensure_schema()
        # Buffered writes. Nodes dedup by id (last write wins); edges accumulate.
        self._node_buf: dict[str, dict[str, Any]] = {}
        self._edge_buf: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in EDGE_TABLES}

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        statements = [f"CREATE NODE TABLE IF NOT EXISTS Node({NODE_PROPS})"]
        for name, props in EDGE_TABLES:
            props_sql = f", {props}" if props else ""
            statements.append(
                f"CREATE REL TABLE IF NOT EXISTS {name}(FROM Node TO Node{props_sql})"
            )
        for stmt in statements:
            # Let real DDL errors propagate; IF NOT EXISTS handles dedup.
            self._conn.execute(stmt)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_node(self, id: str, kind: str, **attrs: Any) -> None:
        """Buffer an upsert of a node. Flushed lazily on the next query."""
        clean = {k: v for k, v in attrs.items() if v is not None}
        existing = self._node_buf.get(id, {})
        existing.update({"id": id, "kind": kind, **clean})
        self._node_buf[id] = existing

    def add_edge(self, src: str, dst: str, kind: str, **attrs: Any) -> None:
        """Buffer a new edge of the given kind. Endpoints must exist by flush time."""
        if kind not in self._edge_buf:
            raise ValueError(f"Unknown edge kind: {kind}")
        clean = {k: v for k, v in attrs.items() if v is not None}
        self._edge_buf[kind].append({"src": src, "dst": dst, **clean})

    def ensure_external(self, id: str, name: str | None = None) -> None:
        """Register a placeholder node for a referenced-but-undefined symbol.

        Won't overwrite a node already buffered with a real kind.
        """
        if id not in self._node_buf:
            self._node_buf[id] = {"id": id, "kind": "external", "name": name or id}

    def flush(self) -> None:
        """Write all buffered nodes/edges to Kùzu via UNWIND. Idempotent."""
        if not self._node_buf and not any(self._edge_buf.values()):
            return

        # Nodes: normalize each row to have every column (Kùzu UNWIND requires
        # uniform shape). Missing columns become NULL.
        if self._node_buf:
            rows = [{c: node.get(c) for c in _NODE_COLS} for node in self._node_buf.values()]
            set_clauses = ", ".join(f"n.{c} = row.{c}" for c in _NODE_COLS if c != "id")
            chunk = 1000
            for i in range(0, len(rows), chunk):
                self._conn.execute(
                    "UNWIND $rows AS row "
                    "MERGE (n:Node {id: row.id}) "
                    f"SET {set_clauses}",
                    {"rows": rows[i:i + chunk]},
                )
            self._node_buf.clear()

        # Edges: one bulk insert per edge kind.
        for kind, edges in self._edge_buf.items():
            if not edges:
                continue
            attr_keys = _edge_attr_keys(kind)
            rows = []
            for e in edges:
                row = {"src": e["src"], "dst": e["dst"]}
                for k in attr_keys:
                    row[k] = e.get(k)
                rows.append(row)
            if attr_keys:
                attr_props = ", ".join(f"{k}: row.{k}" for k in attr_keys)
                cypher = (
                    "UNWIND $rows AS row "
                    "MATCH (a:Node {id: row.src}), (b:Node {id: row.dst}) "
                    f"CREATE (a)-[:{kind} {{{attr_props}}}]->(b)"
                )
            else:
                cypher = (
                    "UNWIND $rows AS row "
                    "MATCH (a:Node {id: row.src}), (b:Node {id: row.dst}) "
                    f"CREATE (a)-[:{kind}]->(b)"
                )
            chunk = 1000
            for i in range(0, len(rows), chunk):
                self._conn.execute(cypher, {"rows": rows[i:i + chunk]})
            self._edge_buf[kind].clear()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def query(self, cypher: str, params: dict[str, Any] | None = None):
        self.flush()
        return self._conn.execute(cypher, params or {})

    def stats(self) -> dict[str, int]:
        self.flush()
        out: dict[str, int] = {}
        node_kinds: dict[str, int] = {}
        result = self.query("MATCH (n:Node) RETURN n.kind AS kind, count(*) AS c")
        while result.has_next():
            row = result.get_next()
            node_kinds[row[0] or "?"] = int(row[1])
        out["nodes"] = sum(node_kinds.values())
        for k, v in sorted(node_kinds.items(), key=lambda x: -x[1]):
            out[f"node_{k}"] = v
        edge_count_total = 0
        for ekind, _ in EDGE_TABLES:
            r = self.query(f"MATCH ()-[:{ekind}]->() RETURN count(*) AS c")
            if r.has_next():
                count = int(r.get_next()[0])
                if count:
                    out[f"edge_{ekind}"] = count
                    edge_count_total += count
        out["edges"] = edge_count_total
        return out

    def close(self) -> None:
        # Kùzu releases resources on GC; explicit close not required.
        pass
