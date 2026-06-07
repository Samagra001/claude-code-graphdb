"""Cypher-based queries over the Kùzu-backed code graph.

Each function returns plain Python dicts/lists shaped for direct JSON output
(MCP-friendly). Cypher patterns assume the schema declared in `graph.py`:
one `Node` table, one REL TABLE per edge kind.
"""
from __future__ import annotations

from typing import Any

from .graph import EDGE_TABLES, CodeGraph


# Columns we always return for a node summary. Order matters — must match the
# RETURN clauses below.
_SUMMARY_COLS = (
    "id", "kind", "name", "qualname", "file", "line", "signature", "docstring",
)


def _summary_return(alias: str) -> str:
    return ", ".join(f"{alias}.{c} AS {c}" for c in _SUMMARY_COLS)


def _row_to_summary(row: list[Any]) -> dict[str, Any]:
    s: dict[str, Any] = {c: row[i] for i, c in enumerate(_SUMMARY_COLS)}
    doc = s.get("docstring") or ""
    s["docstring_snippet"] = doc.splitlines()[0][:120] if doc else None
    s["docstring"] = None  # Don't echo full docstring in summaries.
    return s


def _iter(result):
    while result.has_next():
        yield result.get_next()


def find_definition(graph: CodeGraph, name: str) -> list[dict[str, Any]]:
    """All definitions whose simple name equals `name`."""
    cypher = f"MATCH (n:Node) WHERE n.name = $name RETURN {_summary_return('n')}"
    return [_row_to_summary(row) for row in _iter(graph.query(cypher, {"name": name}))]


_INVOKE_EDGES = "CALLS|ENQUEUES|DELIVERS"


def find_callers(graph: CodeGraph, name: str, depth: int = 1) -> list[dict[str, Any]]:
    """Functions/methods that invoke `name` via CALLS, ENQUEUES, or DELIVERS, up
    to `depth` hops away.

    Rails-aware: a controller that enqueues a job, or a service that delivers a
    mailer, counts as a caller of that job/mailer.
    """
    depth = max(1, int(depth))
    cypher = (
        f"MATCH path = (caller:Node)-[:{_INVOKE_EDGES}*1..{depth}]->(target:Node) "
        "WHERE target.name = $name "
        f"RETURN DISTINCT {_summary_return('caller')}, length(path) AS hops "
        "ORDER BY hops"
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _iter(graph.query(cypher, {"name": name})):
        cid = row[0]
        if cid in seen:
            continue
        seen.add(cid)
        s = _row_to_summary(row)
        s["hops"] = row[len(_SUMMARY_COLS)]
        out.append(s)
    return out


def find_references(graph: CodeGraph, name: str) -> list[dict[str, Any]]:
    """All inbound edges of any kind to definitions named `name`."""
    edge_union = "|".join(k for k, _ in EDGE_TABLES)
    cypher = (
        f"MATCH (src:Node)-[r:{edge_union}]->(target:Node) "
        "WHERE target.name = $name "
        f"RETURN {_summary_return('src')}, label(r) AS edge_kind"
    )
    out = []
    for row in _iter(graph.query(cypher, {"name": name})):
        s = _row_to_summary(row)
        s["edge_kind"] = row[len(_SUMMARY_COLS)]
        out.append(s)
    return out


def impact_of(graph: CodeGraph, name: str, max_depth: int = 3) -> dict[str, Any]:
    """Transitive callers of `name` — predicted blast radius of a change."""
    max_depth = max(1, int(max_depth))
    cypher = (
        f"MATCH path = (caller:Node)-[:{_INVOKE_EDGES}*1..{max_depth}]->(target:Node) "
        "WHERE target.name = $name "
        f"RETURN {_summary_return('caller')}, length(path) AS depth "
        "ORDER BY depth"
    )
    callers: list[dict[str, Any]] = []
    seen_min_depth: dict[str, int] = {}
    for row in _iter(graph.query(cypher, {"name": name})):
        cid = row[0]
        depth = int(row[len(_SUMMARY_COLS)])
        prior = seen_min_depth.get(cid)
        if prior is not None and prior <= depth:
            continue
        seen_min_depth[cid] = depth
    # Second pass to collect summaries at each node's min depth — we run the
    # same query but materialize results in order. Simpler: rebuild from seen.
    for row in _iter(
        graph.query(
            f"MATCH path = (caller:Node)-[:{_INVOKE_EDGES}*1..{max_depth}]->(target:Node) "
            "WHERE target.name = $name "
            f"RETURN DISTINCT {_summary_return('caller')}, length(path) AS depth "
            "ORDER BY depth",
            {"name": name},
        )
    ):
        cid = row[0]
        depth = int(row[len(_SUMMARY_COLS)])
        if seen_min_depth.get(cid) != depth:
            continue
        s = _row_to_summary(row)
        s["depth"] = depth
        callers.append(s)
        seen_min_depth[cid] = -1  # Mark consumed so duplicates skip.
    return {
        "target": name,
        "found": bool(callers),
        "direct": sum(1 for c in callers if c["depth"] == 1),
        "total": len(callers),
        "callers": callers,
    }


def routes_for(graph: CodeGraph, controller_or_action: str) -> list[dict[str, Any]]:
    """Rails-specific: find HTTP routes that hit a controller or controller#action.

    Accepts:
      - bare controller name ("UsersController")
      - fully qualified ("Api::V1::UsersController")
      - qualified action ("Api::V1::UsersController#create")
    """
    cypher = (
        "MATCH (route:Node)-[h:HANDLES]->(action:Node) "
        "WHERE action.qualname = $q OR action.qualname STARTS WITH ($q + '#') "
        "RETURN h.verb AS verb, h.url AS url, action.qualname AS handler, "
        "route.file AS file, route.line AS line "
        "ORDER BY url"
    )
    out: list[dict[str, Any]] = []
    for row in _iter(graph.query(cypher, {"q": controller_or_action})):
        out.append({
            "verb": row[0], "url": row[1], "handler": row[2],
            "file": row[3], "line": row[4],
        })
    return out


def associations_of(graph: CodeGraph, model: str) -> dict[str, Any]:
    """Rails-specific: all ActiveRecord associations on a model (outgoing + incoming)."""
    out: dict[str, list] = {
        "has_many": [], "has_one": [], "belongs_to": [], "habtm": [],
        "referenced_by": [],
    }
    pairs = [("HAS_MANY", "has_many"), ("HAS_ONE", "has_one"),
             ("BELONGS_TO", "belongs_to"), ("HABTM", "habtm")]
    for edge, key in pairs:
        cypher = (
            f"MATCH (m:Node {{name: $name}})-[e:{edge}]->(t:Node) "
            "RETURN e.as_name AS as_name, t.name AS target, t.file AS file"
        )
        for row in _iter(graph.query(cypher, {"name": model})):
            out[key].append({"as_name": row[0], "target": row[1], "file": row[2]})
    # Reverse: what other models point at this one?
    cypher = (
        "MATCH (other:Node)-[e]->(m:Node {name: $name}) "
        "WHERE label(e) IN ['HAS_MANY','HAS_ONE','BELONGS_TO','HABTM'] "
        "RETURN other.name AS other, label(e) AS edge, e.as_name AS as_name"
    )
    for row in _iter(graph.query(cypher, {"name": model})):
        out["referenced_by"].append({"model": row[0], "via": row[1], "as_name": row[2]})
    return out


def module_overview(graph: CodeGraph, path: str) -> dict[str, Any]:
    """Summary of a single file/module: exports, imports, most-called symbols."""
    result = graph.query(
        "MATCH (m:Node) WHERE m.kind = 'module' AND (m.file = $path OR m.id = $path) "
        "RETURN m.id AS id, m.file AS file LIMIT 1",
        {"path": path},
    )
    if not result.has_next():
        return {"module": path, "found": False}
    row = result.get_next()
    mod_id = row[0]
    mod_file = row[1]

    exports = []
    for r in _iter(graph.query(
        "MATCH (m:Node {id: $id})-[:CONTAINS]->(c:Node) "
        "RETURN c.name AS name, c.kind AS kind, c.line AS line, c.signature AS signature",
        {"id": mod_id},
    )):
        exports.append({"name": r[0], "kind": r[1], "line": r[2], "signature": r[3]})

    by_calls = []
    for r in _iter(graph.query(
        "MATCH (m:Node {id: $id})-[:CONTAINS]->(c:Node) "
        "OPTIONAL MATCH (c)<-[r:CALLS]-() "
        "WITH c, count(r) AS n WHERE n > 0 "
        "RETURN c.name AS name, n ORDER BY n DESC LIMIT 10",
        {"id": mod_id},
    )):
        by_calls.append({"name": r[0], "incoming_calls": int(r[1])})

    return {
        "module": mod_id,
        "file": mod_file,
        "found": True,
        "exports": exports,
        "most_called": by_calls,
    }
