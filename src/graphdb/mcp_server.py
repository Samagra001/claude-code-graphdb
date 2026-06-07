"""MCP server exposing graphdb queries to Claude Code.

Run as `graphdb-mcp`. Set GRAPHDB_ROOT to the indexed project root.
"""
from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

from . import queries
from .graph import CodeGraph

app = FastMCP("graphdb")


_GRAPH_CACHE: CodeGraph | None = None


def _graph() -> CodeGraph:
    """Open (or reuse) the Kùzu graph for the project at GRAPHDB_ROOT.

    Caches the connection between tool calls — opening Kùzu on every query
    would re-load schema + indexes from disk needlessly.
    """
    global _GRAPH_CACHE
    if _GRAPH_CACHE is None:
        root = os.environ.get("GRAPHDB_ROOT", os.getcwd())
        _GRAPH_CACHE = CodeGraph(root=root)
    return _GRAPH_CACHE


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


@app.tool()
def find_definition(name: str) -> str:
    """Find all definitions matching a simple name (functions/classes/methods)."""
    return _dump(queries.find_definition(_graph(), name))


@app.tool()
def find_callers(name: str, depth: int = 1) -> str:
    """Functions that call `name`, transitively up to `depth` hops."""
    return _dump(queries.find_callers(_graph(), name, depth=depth))


@app.tool()
def find_references(name: str) -> str:
    """All inbound references to `name` (calls, imports, inheritance)."""
    return _dump(queries.find_references(_graph(), name))


@app.tool()
def impact_of(name: str, max_depth: int = 3) -> str:
    """Transitive callers — predict what may break if `name` changes."""
    return _dump(queries.impact_of(_graph(), name, max_depth=max_depth))


@app.tool()
def module_overview(path: str) -> str:
    """Summary of a single module: exports, imports, most-called internal symbols.

    `path` is the file path relative to the indexed root.
    """
    return _dump(queries.module_overview(_graph(), path))


@app.tool()
def routes_for(controller_or_action: str) -> str:
    """Rails-specific: HTTP routes that hit a controller (or controller#action).

    Accepts a bare class ("UsersController"), fully qualified ("Api::V1::UsersController"),
    or qualified action ("Api::V1::UsersController#create"). Returns verb/url/handler.
    """
    return _dump(queries.routes_for(_graph(), controller_or_action))


@app.tool()
def associations_of(model: str) -> str:
    """Rails-specific: all ActiveRecord associations on a model.

    Returns outgoing has_many/has_one/belongs_to/HABTM plus reverse-references
    (other models that point at this one).
    """
    return _dump(queries.associations_of(_graph(), model))


@app.tool()
def graph_stats() -> str:
    """Counts of nodes/edges by kind — quick health check."""
    return _dump(_graph().stats())


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
