"""CLI for graphdb.

Usage:
    graphdb [--root PATH] index <path>
    graphdb [--root PATH] stats
    graphdb [--root PATH] query def <name>
    graphdb [--root PATH] query callers <name> [--depth N]
    graphdb [--root PATH] query refs <name>
    graphdb [--root PATH] query impact <name> [--max-depth N]
    graphdb [--root PATH] query module <path>
    graphdb [--root PATH] query routes <controller_or_action>
    graphdb [--root PATH] query associations <model>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import queries
from .graph import CodeGraph, default_db_path_for
from .indexer import index_path


def _open(root: str) -> CodeGraph:
    db_path = default_db_path_for(root)
    if not db_path.exists():
        sys.exit(
            f"No graph at {db_path} — run `graphdb index {root}` first."
        )
    return CodeGraph(root=root)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphdb")
    parser.add_argument("--root", default=".", help="Project root (default: cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="Build the graph for a Ruby on Rails project")
    p_index.add_argument("path", nargs="?", default=".")

    sub.add_parser("stats", help="Print graph statistics")

    p_query = sub.add_parser("query", help="Query the graph")
    qsub = p_query.add_subparsers(dest="qcmd", required=True)

    p_def = qsub.add_parser("def", help="Find definitions by name")
    p_def.add_argument("name")

    p_callers = qsub.add_parser("callers", help="Find callers of a function")
    p_callers.add_argument("name")
    p_callers.add_argument("--depth", type=int, default=1)

    p_refs = qsub.add_parser("refs", help="All references to a symbol")
    p_refs.add_argument("name")

    p_impact = qsub.add_parser("impact", help="Transitive blast radius of a change")
    p_impact.add_argument("name")
    p_impact.add_argument("--max-depth", type=int, default=3)

    p_mod = qsub.add_parser("module", help="Module overview (exports/imports/hotspots)")
    p_mod.add_argument("path")

    p_routes = qsub.add_parser("routes", help="Routes that hit a controller or action (Rails)")
    p_routes.add_argument("name", help="e.g. UsersController, Api::V1::UsersController, or Api::V1::UsersController#create")

    p_assoc = qsub.add_parser("associations", help="All ActiveRecord associations on a model (Rails)")
    p_assoc.add_argument("name", help="e.g. User")

    args = parser.parse_args(argv)

    if args.cmd == "index":
        graph = CodeGraph(root=args.path, reset=True)
        index_path(graph, args.path)
        print(f"Indexed {args.path} → {graph.db_path}")
        _print(graph.stats())
        return 0

    if args.cmd == "stats":
        _print(_open(args.root).stats())
        return 0

    if args.cmd == "query":
        graph = _open(args.root)
        if args.qcmd == "def":
            _print(queries.find_definition(graph, args.name))
        elif args.qcmd == "callers":
            _print(queries.find_callers(graph, args.name, depth=args.depth))
        elif args.qcmd == "refs":
            _print(queries.find_references(graph, args.name))
        elif args.qcmd == "impact":
            _print(queries.impact_of(graph, args.name, max_depth=args.max_depth))
        elif args.qcmd == "module":
            _print(queries.module_overview(graph, args.path))
        elif args.qcmd == "routes":
            _print(queries.routes_for(graph, args.name))
        elif args.qcmd == "associations":
            _print(queries.associations_of(graph, args.name))
        else:
            parser.error(f"unknown query {args.qcmd}")
        return 0

    parser.error(f"unknown command {args.cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
