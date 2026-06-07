"""Ruby/Rails indexer built on tree-sitter-ruby.

Two-pass design:
  Pass 1 (definitions): walk every .rb file, register modules/classes/methods
    as graph nodes. Tracks namespace nesting so `module Api; module V1; class X`
    becomes qualname `Api::V1::X`. Builds a name index used by pass 2.
  Pass 2 (references): walk again. Resolve `class < Super`, `include Mod`, and
    method calls against the name index, emitting INHERITS / INCLUDES / CALLS.

Phase 3 extractors (Rails-specific) layer on top — they read the same AST and
emit specialized edges (HAS_MANY, HANDLES, BEFORE_ACTION, ENQUEUES, ...).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_ruby as tsruby
from tree_sitter import Language, Node, Parser

from .graph import CodeGraph

_RUBY = Language(tsruby.language())


def _new_parser() -> Parser:
    p = Parser(_RUBY)
    return p


# ----------------------------------------------------------------------
# Index state
# ----------------------------------------------------------------------


@dataclass
class IndexState:
    root: Path
    # qualname -> node id (often the same string, but kept distinct for future
    # flexibility)
    qualname_to_id: dict[str, str] = field(default_factory=dict)
    # simple basename -> [node ids] for fallback resolution
    simple_to_ids: dict[str, list[str]] = field(default_factory=dict)
    # cached parse trees so pass 2 doesn't re-parse: rel_path -> (source, root_node)
    asts: dict[str, tuple[bytes, Node]] = field(default_factory=dict)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


RAILS_SOURCE_DIRS = ("app", "lib", "config", "db/migrate")


def index_path(graph: CodeGraph, root: str | Path) -> None:
    root = Path(root).resolve()
    state = IndexState(root=root)
    parser = _new_parser()

    files = _gather_ruby_files(root)
    print(f"[indexer] parsing {len(files)} Ruby files…")

    # Pass 1: definitions
    for file in files:
        try:
            source = file.read_bytes()
        except OSError:
            continue
        tree = parser.parse(source)
        rel = str(file.relative_to(root))
        state.asts[rel] = (source, tree.root_node)
        _collect_defs(rel, source, tree.root_node, graph, state)

    print(f"[indexer] pass-1 defs: {len(state.qualname_to_id)} named entities")

    # Pass 2: refs (INHERITS / INCLUDES / CALLS / model+controller+job DSL)
    for rel, (source, root_node) in state.asts.items():
        _collect_refs(rel, source, root_node, graph, state)

    # Pass 3: routes.rb — DSL outside class bodies, needs its own walker.
    _index_routes(root, parser, graph, state)

    print("[indexer] pass-2 references done; flushing to Kùzu…")
    graph.flush()
    print("[indexer] flush complete")


# ----------------------------------------------------------------------
# File discovery
# ----------------------------------------------------------------------


def _gather_ruby_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for d in RAILS_SOURCE_DIRS:
        target = root / d
        if not target.exists():
            continue
        for p in target.rglob("*.rb"):
            # Skip the typical noise
            if any(part in (".bundle", "tmp", "vendor", "node_modules", "log") for part in p.parts):
                continue
            files.append(p)
    return sorted(files)


# ----------------------------------------------------------------------
# Node-walking primitives
# ----------------------------------------------------------------------


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_by_type(node: Node, type_: str) -> Node | None:
    for c in node.children:
        if c.type == type_:
            return c
    return None


def _children_by_type(node: Node, type_: str) -> list[Node]:
    return [c for c in node.children if c.type == type_]


def _flatten_constant(node: Node, source: bytes) -> str | None:
    """Resolve `Foo`, `Foo::Bar::Baz`, or a superclass wrapper to a dotted string."""
    if node.type == "constant":
        return _text(node, source)
    if node.type == "scope_resolution":
        parts: list[str] = []
        def walk(n: Node) -> None:
            for c in n.children:
                if c.type == "constant":
                    parts.append(_text(c, source))
                elif c.type == "scope_resolution":
                    walk(c)
        walk(node)
        return "::".join(parts) if parts else None
    if node.type == "superclass":
        for c in node.children:
            if c.type in ("constant", "scope_resolution"):
                return _flatten_constant(c, source)
    return None


def _class_or_module_name(node: Node, source: bytes) -> str | None:
    """Extract the leading name from `class Foo` or `module Foo::Bar`."""
    for c in node.children:
        if c.type in ("constant", "scope_resolution"):
            return _flatten_constant(c, source)
    return None


def _method_signature_line(method_node: Node, source: bytes) -> str:
    text = _text(method_node, source)
    first = text.split("\n", 1)[0]
    return first.strip()


# ----------------------------------------------------------------------
# Pass 1: definitions
# ----------------------------------------------------------------------


def _classify(qualname: str, rel: str) -> str:
    """Classify a class by its directory under app/."""
    if rel.startswith("app/models/"):
        if "/concerns/" in rel:
            return "concern"
        return "model"
    if rel.startswith("app/controllers/"):
        if "/concerns/" in rel:
            return "concern"
        return "controller"
    if rel.startswith("app/jobs/"):
        return "job"
    if rel.startswith("app/mailers/"):
        return "mailer"
    if rel.startswith("app/serializers/"):
        return "serializer"
    if rel.startswith("app/services/"):
        return "service"
    if rel.startswith("app/validators/"):
        return "validator"
    if rel.startswith("app/helpers/"):
        return "helper"
    if rel.startswith("app/channels/"):
        return "channel"
    if rel.startswith("app/errors/"):
        return "error_class"
    if rel.startswith("db/migrate/"):
        return "migration"
    return "class"


def _register(graph: CodeGraph, state: IndexState, node_id: str, kind: str,
              name: str, qualname: str, rel: str, line: int, **extra) -> None:
    graph.add_node(node_id, kind, name=name, qualname=qualname, file=rel, line=line, **extra)
    state.qualname_to_id[qualname] = node_id
    state.simple_to_ids.setdefault(name, []).append(node_id)


def _collect_defs(rel: str, source: bytes, root_node: Node,
                  graph: CodeGraph, state: IndexState) -> None:
    # File-level node so every file is reachable in the graph.
    file_id = f"file::{rel}"
    file_name = Path(rel).name
    graph.add_node(file_id, "file", name=file_name, qualname=rel, file=rel, line=1)

    _walk_defs(root_node, source, rel, file_id, [], graph, state)


def _walk_defs(node: Node, source: bytes, rel: str, parent_id: str,
               ns_stack: list[str], graph: CodeGraph, state: IndexState) -> None:
    for child in node.children:
        t = child.type
        if t == "module":
            name = _class_or_module_name(child, source)
            if not name:
                _walk_defs(child, source, rel, parent_id, ns_stack, graph, state)
                continue
            ns_stack.append(name)
            qualname = "::".join(ns_stack)
            simple = name.split("::")[-1]
            mod_kind = "concern" if "/concerns/" in rel else "module"
            _register(graph, state, qualname, mod_kind,
                      name=simple, qualname=qualname,
                      rel=rel, line=child.start_point[0] + 1)
            graph.add_edge(parent_id, qualname, "CONTAINS")
            body = _child_by_type(child, "body_statement")
            if body:
                _walk_defs(body, source, rel, qualname, ns_stack, graph, state)
            ns_stack.pop()
        elif t == "class":
            name = _class_or_module_name(child, source)
            if not name:
                _walk_defs(child, source, rel, parent_id, ns_stack, graph, state)
                continue
            ns_stack.append(name)
            qualname = "::".join(ns_stack)
            simple = name.split("::")[-1]
            kind = _classify(qualname, rel)
            _register(graph, state, qualname, kind,
                      name=simple, qualname=qualname,
                      rel=rel, line=child.start_point[0] + 1)
            graph.add_edge(parent_id, qualname, "CONTAINS")
            body = _child_by_type(child, "body_statement")
            if body:
                _walk_defs(body, source, rel, qualname, ns_stack, graph, state)
            ns_stack.pop()
        elif t == "method":
            name_node = _child_by_type(child, "identifier")
            if not name_node:
                continue
            mname = _text(name_node, source)
            owner = "::".join(ns_stack)
            qualname = f"{owner}#{mname}" if owner else mname
            kind = "action" if owner.endswith("Controller") and rel.startswith("app/controllers/") else "method"
            _register(graph, state, qualname, kind,
                      name=mname, qualname=qualname,
                      rel=rel, line=child.start_point[0] + 1,
                      signature=_method_signature_line(child, source))
            graph.add_edge(parent_id, qualname, "CONTAINS")
        elif t == "singleton_method":
            name_node = _child_by_type(child, "identifier")
            if not name_node:
                continue
            mname = _text(name_node, source)
            owner = "::".join(ns_stack)
            qualname = f"{owner}.{mname}" if owner else f".{mname}"
            _register(graph, state, qualname, "class_method",
                      name=mname, qualname=qualname,
                      rel=rel, line=child.start_point[0] + 1,
                      signature=_method_signature_line(child, source))
            graph.add_edge(parent_id, qualname, "CONTAINS")
        else:
            _walk_defs(child, source, rel, parent_id, ns_stack, graph, state)


# ----------------------------------------------------------------------
# Pass 2: references — INHERITS, INCLUDES, CALLS
# ----------------------------------------------------------------------


def _resolve(name: str, ns_stack: list[str], state: IndexState) -> str | None:
    """Resolve a (possibly qualified) name to a node id.

    Lookup order:
      1. exact qualname match
      2. enclosing namespace + name (innermost first)
      3. unique simple-name fallback (only if exactly one match)
    """
    if not name:
        return None
    if name in state.qualname_to_id:
        return state.qualname_to_id[name]
    for i in range(len(ns_stack), 0, -1):
        scoped = "::".join(ns_stack[:i]) + "::" + name
        if scoped in state.qualname_to_id:
            return state.qualname_to_id[scoped]
    simple = name.split("::")[-1]
    candidates = state.simple_to_ids.get(simple, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _collect_refs(rel: str, source: bytes, root_node: Node,
                  graph: CodeGraph, state: IndexState) -> None:
    _walk_refs(root_node, source, rel, [], [], graph, state)


def _walk_refs(node: Node, source: bytes, rel: str,
               ns_stack: list[str], method_stack: list[str],
               graph: CodeGraph, state: IndexState) -> None:
    for child in node.children:
        t = child.type
        if t in ("module", "class"):
            name = _class_or_module_name(child, source)
            if not name:
                _walk_refs(child, source, rel, ns_stack, method_stack, graph, state)
                continue
            ns_stack.append(name)
            qualname = "::".join(ns_stack)
            if t == "class":
                sc = _child_by_type(child, "superclass")
                if sc:
                    sup = _flatten_constant(sc, source)
                    if sup:
                        tid = _resolve(sup, ns_stack[:-1], state)
                        if tid is None:
                            tid = sup
                            graph.ensure_external(tid, name=sup.split("::")[-1])
                        graph.add_edge(qualname, tid, "INHERITS")
            body = _child_by_type(child, "body_statement")
            if body:
                _walk_refs(body, source, rel, ns_stack, method_stack, graph, state)
            ns_stack.pop()
        elif t in ("method", "singleton_method"):
            name_node = _child_by_type(child, "identifier")
            if not name_node:
                continue
            mname = _text(name_node, source)
            owner = "::".join(ns_stack)
            sep = "#" if t == "method" else "."
            qualname = f"{owner}{sep}{mname}" if owner else f"{sep}{mname}" if sep == "." else mname
            method_stack.append(qualname)
            body = _child_by_type(child, "body_statement")
            if body:
                _walk_refs(body, source, rel, ns_stack, method_stack, graph, state)
            method_stack.pop()
        elif t == "call":
            _handle_call(child, source, ns_stack, method_stack, graph, state)
            _walk_refs(child, source, rel, ns_stack, method_stack, graph, state)
        else:
            _walk_refs(child, source, rel, ns_stack, method_stack, graph, state)


def _handle_call(call_node: Node, source: bytes,
                 ns_stack: list[str], method_stack: list[str],
                 graph: CodeGraph, state: IndexState) -> None:
    callee = None
    for c in call_node.children:
        if c.type == "identifier":
            callee = _text(c, source)
            break
    if not callee:
        return

    at_class_scope = bool(ns_stack) and not method_stack
    inside_method = bool(method_stack)

    # ---- include / extend / prepend  -> INCLUDES edge -------------------
    if callee in ("include", "extend", "prepend") and at_class_scope:
        args = _child_by_type(call_node, "argument_list")
        if not args:
            return
        for arg in args.children:
            if arg.type in ("constant", "scope_resolution"):
                tgt_name = _flatten_constant(arg, source)
                if not tgt_name:
                    continue
                tid = _resolve(tgt_name, ns_stack, state)
                if tid is None:
                    tid = tgt_name
                    graph.ensure_external(tid, name=tgt_name.split("::")[-1])
                graph.add_edge("::".join(ns_stack), tid, "INCLUDES", mode=callee)
        return

    # ---- Rails model DSL: associations / validates / scope --------------
    if at_class_scope and callee in (
        "has_many", "has_one", "belongs_to", "has_and_belongs_to_many",
        "validates", "validates_presence_of", "validates_uniqueness_of", "scope",
    ):
        _handle_model_dsl(call_node, source, ns_stack, callee, graph, state)
        return

    # ---- Rails controller DSL: before_action / after_action / skip_* ----
    if at_class_scope and callee in (
        "before_action", "after_action", "around_action",
        "skip_before_action", "skip_after_action",
    ):
        _handle_controller_filter(call_node, source, ns_stack, callee, graph, state)
        return

    # ---- Job enqueue: *.perform_later / perform_async / perform_in -----
    if inside_method and callee in (
        "perform_later", "perform_async", "perform_in", "perform_at", "set",
    ):
        target = _root_constant(call_node, source)
        if target and callee != "set":
            _emit_chain_edge(call_node, source, ns_stack, method_stack,
                             target, callee, "ENQUEUES", graph, state)

    # ---- Mailer dispatch: *.deliver_later / deliver_now ---------------
    if inside_method and callee in ("deliver_later", "deliver_now", "deliver_now!"):
        target = _root_constant(call_node, source)
        if target:
            _emit_chain_edge(call_node, source, ns_stack, method_stack,
                             target, callee, "DELIVERS", graph, state)

    # ---- Generic CALLS edge from the enclosing method ------------------
    if not inside_method:
        return
    caller_id = method_stack[-1]
    tid = _resolve(callee, ns_stack, state)
    if tid:
        graph.add_edge(caller_id, tid, "CALLS", line=call_node.start_point[0] + 1)


# ----------------------------------------------------------------------
# Argument parsing helpers (Phase 3)
# ----------------------------------------------------------------------


def _strip_sym(s: str) -> str:
    return s.lstrip(":").strip()


def _arg_value(node: Node, source: bytes):
    """Best-effort literal extraction for `pair` values."""
    t = node.type
    if t == "true":
        return True
    if t == "false":
        return False
    if t == "nil":
        return None
    if t == "integer":
        try:
            return int(_text(node, source))
        except ValueError:
            return _text(node, source)
    if t == "string":
        content = _child_by_type(node, "string_content")
        return _text(content, source) if content else _text(node, source).strip("\"'")
    if t == "simple_symbol":
        return _strip_sym(_text(node, source))
    if t in ("constant", "scope_resolution"):
        return _flatten_constant(node, source)
    if t == "array":
        out = []
        for c in node.children:
            v = _arg_value(c, source)
            if v is not None or c.type == "nil":
                out.append(v)
        return out
    return None


def _parse_call_args(call_node: Node, source: bytes) -> tuple[list, dict]:
    """Return (positional_args, kwargs) for a call's argument_list."""
    args = _child_by_type(call_node, "argument_list")
    positional: list = []
    kwargs: dict = {}
    if not args:
        return positional, kwargs
    for child in args.children:
        if child.type == "pair":
            key = None
            val = None
            for sub in child.children:
                if sub.type == "hash_key_symbol":
                    key = _strip_sym(_text(sub, source)).rstrip(":")
                elif key is not None and val is None:
                    val = _arg_value(sub, source)
            if key:
                kwargs[key] = val
        else:
            v = _arg_value(child, source)
            if v is not None or child.type == "nil":
                positional.append(v)
    return positional, kwargs


# ----------------------------------------------------------------------
# Rails model DSL extractors (Phase 3a)
# ----------------------------------------------------------------------


def _singularize(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("ches") or word.endswith("shes") or word.endswith("xes") or word.endswith("sses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _camelize(snake: str) -> str:
    return "".join(p.capitalize() for p in snake.split("_"))


def _resolve_class_name(name: str, ns_stack: list[str], state: IndexState,
                       graph: CodeGraph) -> str:
    """Resolve a candidate Ruby class name to a node id; create external if unknown."""
    tid = _resolve(name, ns_stack, state)
    if tid is not None:
        return tid
    graph.ensure_external(name, name=name.split("::")[-1])
    return name


def _handle_model_dsl(call_node: Node, source: bytes, ns_stack: list[str],
                     callee: str, graph: CodeGraph, state: IndexState) -> None:
    src_id = "::".join(ns_stack)
    positional, kwargs = _parse_call_args(call_node, source)
    if not positional:
        return

    if callee == "has_many" or callee == "has_one" or callee == "belongs_to" \
            or callee == "has_and_belongs_to_many":
        assoc_name = positional[0]
        if not isinstance(assoc_name, str):
            return
        explicit = kwargs.get("class_name")
        if explicit:
            target_simple = explicit
        else:
            base = assoc_name if callee in ("belongs_to", "has_one") else _singularize(assoc_name)
            target_simple = _camelize(base)
        target_id = _resolve_class_name(target_simple, ns_stack[:-1], state, graph)

        if callee == "has_many":
            graph.add_edge(src_id, target_id, "HAS_MANY",
                          as_name=assoc_name, through=kwargs.get("through"),
                          class_name=explicit)
        elif callee == "has_one":
            graph.add_edge(src_id, target_id, "HAS_ONE",
                          as_name=assoc_name, class_name=explicit)
        elif callee == "belongs_to":
            graph.add_edge(src_id, target_id, "BELONGS_TO",
                          as_name=assoc_name, class_name=explicit,
                          is_optional=bool(kwargs.get("optional")))
        else:  # HABTM
            graph.add_edge(src_id, target_id, "HABTM", as_name=assoc_name)
        return

    if callee == "validates":
        fields = [a for a in positional if isinstance(a, str)]
        validator_kinds = list(kwargs.keys())
        for field in fields:
            for vkind in validator_kinds or ["unknown"]:
                graph.add_edge(src_id, src_id, "VALIDATES",
                              field=field, rule=vkind)
        return

    if callee in ("validates_presence_of", "validates_uniqueness_of"):
        rule_name = "presence" if callee.endswith("presence_of") else "uniqueness"
        for f in positional:
            if isinstance(f, str):
                graph.add_edge(src_id, src_id, "VALIDATES", field=f, rule=rule_name)
        return

    if callee == "scope":
        if positional and isinstance(positional[0], str):
            graph.add_edge(src_id, src_id, "SCOPES", scope_name=positional[0])
        return


# ----------------------------------------------------------------------
# Controller filter extractors (Phase 3c)
# ----------------------------------------------------------------------


def _actions_in_controller(controller_qualname: str, state: IndexState) -> list[str]:
    prefix = f"{controller_qualname}#"
    return [qn for qn in state.qualname_to_id if qn.startswith(prefix)]


def _handle_controller_filter(call_node: Node, source: bytes, ns_stack: list[str],
                              callee: str, graph: CodeGraph,
                              state: IndexState) -> None:
    controller = "::".join(ns_stack)
    positional, kwargs = _parse_call_args(call_node, source)
    filter_names = [a for a in positional if isinstance(a, str)]
    if not filter_names:
        return

    only = kwargs.get("only")
    except_ = kwargs.get("except")

    only_list = only if isinstance(only, list) else ([only] if isinstance(only, str) else [])
    except_list = except_ if isinstance(except_, list) else ([except_] if isinstance(except_, str) else [])

    actions = _actions_in_controller(controller, state)
    if only_list:
        actions = [a for a in actions if a.split("#", 1)[1] in only_list]
    elif except_list:
        actions = [a for a in actions if a.split("#", 1)[1] not in except_list]
    # else: applies to all actions

    if callee.startswith("skip_"):
        edge_kind = "SKIP_BEFORE"
    elif callee.startswith("after"):
        edge_kind = "AFTER_ACTION"
    else:
        edge_kind = "BEFORE_ACTION"

    only_s = ",".join(only_list) if only_list else None
    except_s = ",".join(except_list) if except_list else None

    for fname in filter_names:
        # Filter method node: prefer this controller's own definition.
        filter_qn = f"{controller}#{fname}"
        if filter_qn not in state.qualname_to_id:
            # Filter might be inherited / from a concern. Try simple-name resolution.
            candidates = [qn for qn in state.qualname_to_id if qn.endswith(f"#{fname}")]
            if len(candidates) == 1:
                filter_qn = candidates[0]
            else:
                # Couldn't resolve. Synthesize a placeholder so the edge has both ends.
                graph.ensure_external(filter_qn, name=fname)
                state.qualname_to_id[filter_qn] = filter_qn
        for action_qn in actions:
            graph.add_edge(filter_qn, action_qn, edge_kind,
                          only_actions=only_s, except_actions=except_s)


# ----------------------------------------------------------------------
# Job / mailer chain edges (Phase 3d)
# ----------------------------------------------------------------------


def _root_constant(call_node: Node, source: bytes) -> str | None:
    """Walk down a possibly-chained call to find the root receiver constant."""
    for c in call_node.children:
        if c.type in ("constant", "scope_resolution"):
            return _flatten_constant(c, source)
        if c.type == "call":
            inner = _root_constant(c, source)
            if inner:
                return inner
    return None


def _emit_chain_edge(call_node: Node, source: bytes, ns_stack: list[str],
                    method_stack: list[str], target_name: str, method: str,
                    edge_kind: str, graph: CodeGraph, state: IndexState) -> None:
    target_id = _resolve(target_name, ns_stack, state)
    if target_id is None:
        target_id = target_name
        graph.ensure_external(target_id, name=target_name.split("::")[-1])
    caller_id = method_stack[-1] if method_stack else "::".join(ns_stack)
    if not caller_id:
        return
    graph.add_edge(caller_id, target_id, edge_kind,
                  method=method, line=call_node.start_point[0] + 1)


# ----------------------------------------------------------------------
# Routes parser (Phase 3b) — config/routes.rb is a DSL outside any class.
# ----------------------------------------------------------------------


def _pluralize_simple(word: str) -> str:
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    return word + "s"


_RESTFUL_PLURAL = [
    ("GET",    "",          "index"),
    ("POST",   "",          "create"),
    ("GET",    "/new",      "new"),
    ("GET",    "/:id",      "show"),
    ("GET",    "/:id/edit", "edit"),
    ("PATCH",  "/:id",      "update"),
    ("DELETE", "/:id",      "destroy"),
]
_RESTFUL_SINGULAR = [
    ("POST",   "",       "create"),
    ("GET",    "/new",   "new"),
    ("GET",    "",       "show"),
    ("GET",    "/edit",  "edit"),
    ("PATCH",  "",       "update"),
    ("DELETE", "",       "destroy"),
]
# Devise default routes per resource. (verb, path_suffix, controller_part, action)
_DEVISE_ROUTES = [
    ("POST",   "/sign_in",  "sessions",       "create"),
    ("DELETE", "/sign_out", "sessions",       "destroy"),
    ("POST",   "",          "registrations",  "create"),
    ("PUT",    "",          "registrations",  "update"),
    ("DELETE", "",          "registrations",  "destroy"),
    ("POST",   "/password", "passwords",      "create"),
    ("PUT",    "/password", "passwords",      "update"),
]


def _ns_path(ns_stack: list[str]) -> str:
    return "/" + "/".join(ns_stack) if ns_stack else ""


def _ns_class_prefix(ns_stack: list[str]) -> list[str]:
    return [_camelize(p) for p in ns_stack]


def _controller_qualname(ns_stack: list[str], controller_simple: str) -> str:
    parts = _ns_class_prefix(ns_stack) + [_camelize(controller_simple) + "Controller"]
    return "::".join(parts)


def _emit_route(graph: CodeGraph, state: IndexState, rel: str, line: int,
                verb: str, url: str, controller_qn: str, action: str) -> None:
    action_qn = f"{controller_qn}#{action}"
    route_id = f"route::{verb}_{url}"
    graph.add_node(route_id, "route",
                  name=f"{verb} {url}", url_pattern=url,
                  http_method=verb, file=rel, line=line)
    if action_qn not in state.qualname_to_id:
        graph.ensure_external(action_qn, name=action)
    target = state.qualname_to_id.get(action_qn, action_qn)
    graph.add_edge(route_id, target, "HANDLES", verb=verb, url=url)


def _index_routes(root: Path, parser: Parser, graph: CodeGraph,
                  state: IndexState) -> None:
    routes_file = root / "config" / "routes.rb"
    if not routes_file.exists():
        return
    source = routes_file.read_bytes()
    rel = "config/routes.rb"
    tree = parser.parse(source)

    # Find Rails.application.routes.draw do ... end
    draw_block = None
    def find_draw(node: Node) -> Node | None:
        if node.type == "call":
            for c in node.children:
                if c.type == "identifier" and _text(c, source) == "draw":
                    block = _child_by_type(node, "do_block") or _child_by_type(node, "block")
                    if block:
                        return block
        for c in node.children:
            r = find_draw(c)
            if r is not None:
                return r
        return None

    draw_block = find_draw(tree.root_node)
    if draw_block is None:
        print("[indexer] no `routes.draw` block found in config/routes.rb")
        return
    body = _child_by_type(draw_block, "body_statement") or draw_block
    _walk_routes(body, source, rel, [], graph, state)
    print(f"[indexer] routes pass: parsed {rel}")


def _walk_routes(body: Node, source: bytes, rel: str, ns_stack: list[str],
                 graph: CodeGraph, state: IndexState) -> None:
    for child in body.children:
        if child.type != "call":
            _walk_routes(child, source, rel, ns_stack, graph, state)
            continue
        callee = None
        for c in child.children:
            if c.type == "identifier":
                callee = _text(c, source)
                break
        if callee is None:
            continue
        if callee == "namespace":
            _handle_route_namespace(child, source, rel, ns_stack, graph, state)
        elif callee in ("resources", "resource"):
            _handle_route_resources(child, source, rel, ns_stack,
                                    plural=(callee == "resources"), graph=graph, state=state)
        elif callee in ("get", "post", "put", "patch", "delete"):
            _handle_route_verb(child, source, rel, ns_stack, callee.upper(), graph, state)
        elif callee == "mount":
            _handle_route_mount(child, source, rel, ns_stack, graph, state)
        elif callee == "devise_for":
            _handle_devise_for(child, source, rel, ns_stack, graph, state)
        # other DSL (scope, root, concerns, etc.) — skipped for PoC


def _handle_route_namespace(call: Node, source: bytes, rel: str,
                            ns_stack: list[str], graph: CodeGraph,
                            state: IndexState) -> None:
    pos, _ = _parse_call_args(call, source)
    if not pos or not isinstance(pos[0], str):
        return
    ns_stack.append(pos[0])
    block = _child_by_type(call, "do_block") or _child_by_type(call, "block")
    if block:
        body = _child_by_type(block, "body_statement") or block
        _walk_routes(body, source, rel, ns_stack, graph, state)
    ns_stack.pop()


def _handle_route_resources(call: Node, source: bytes, rel: str,
                             ns_stack: list[str], plural: bool,
                             graph: CodeGraph, state: IndexState) -> None:
    pos, kwargs = _parse_call_args(call, source)
    if not pos or not isinstance(pos[0], str):
        return
    name = pos[0]
    controller_simple = name if plural else _pluralize_simple(name)
    controller_qn = _controller_qualname(ns_stack, controller_simple)
    line = call.start_point[0] + 1
    path_base = _ns_path(ns_stack) + "/" + name

    only = kwargs.get("only")
    except_ = kwargs.get("except")
    only_list = only if isinstance(only, list) else ([only] if isinstance(only, str) else None)
    except_list = except_ if isinstance(except_, list) else ([except_] if isinstance(except_, str) else None)

    template = _RESTFUL_PLURAL if plural else _RESTFUL_SINGULAR
    for verb, suffix, action in template:
        if only_list is not None and action not in only_list:
            continue
        if except_list is not None and action in except_list:
            continue
        url = path_base + suffix
        _emit_route(graph, state, rel, line, verb, url, controller_qn, action)

    # nested block: may have member/collection routes; recurse so simple
    # `post :foo, on: :member` style routes can at least register controller#foo
    block = _child_by_type(call, "do_block") or _child_by_type(call, "block")
    if block:
        body = _child_by_type(block, "body_statement") or block
        for sub in body.children:
            if sub.type != "call":
                continue
            sub_callee = None
            for c in sub.children:
                if c.type == "identifier":
                    sub_callee = _text(c, source)
                    break
            if sub_callee in ("get", "post", "put", "patch", "delete"):
                spos, skwargs = _parse_call_args(sub, source)
                if not spos:
                    continue
                action_name = spos[0]
                if not isinstance(action_name, str):
                    continue
                on = skwargs.get("on")
                if on == "member":
                    url = f"{path_base}/:id/{action_name}"
                else:
                    url = f"{path_base}/{action_name}"
                _emit_route(graph, state, rel, sub.start_point[0] + 1,
                           sub_callee.upper(), url, controller_qn, action_name)


def _handle_route_verb(call: Node, source: bytes, rel: str,
                       ns_stack: list[str], verb: str,
                       graph: CodeGraph, state: IndexState) -> None:
    pos, kwargs = _parse_call_args(call, source)
    if not pos:
        return
    path = pos[0]
    if not isinstance(path, str):
        return
    to = kwargs.get("to")
    if not isinstance(to, str) or "#" not in to:
        return
    ctrl_part, action = to.split("#", 1)
    parts = ctrl_part.split("/")
    controller_simple = parts[-1]
    extra_ns = parts[:-1]
    full_ns = ns_stack + extra_ns
    controller_qn = _controller_qualname(full_ns, controller_simple)
    url = _ns_path(ns_stack) + "/" + path.lstrip("/")
    _emit_route(graph, state, rel, call.start_point[0] + 1,
               verb, url, controller_qn, action)


def _handle_route_mount(call: Node, source: bytes, rel: str,
                        ns_stack: list[str], graph: CodeGraph,
                        state: IndexState) -> None:
    # `mount Foo::Engine => '/foo'` — argument_list contains a hash association.
    args = _child_by_type(call, "argument_list")
    if not args:
        return
    engine = None
    path = None
    for arg in args.children:
        if arg.type in ("constant", "scope_resolution"):
            if engine is None:
                engine = _flatten_constant(arg, source)
        elif arg.type == "string":
            sc = _child_by_type(arg, "string_content")
            if sc and path is None:
                path = _text(sc, source)
        elif arg.type == "hash":
            for pair in arg.children:
                if pair.type == "pair":
                    pass  # not the common form
        elif arg.type == "pair":
            for c in arg.children:
                if c.type in ("constant", "scope_resolution") and engine is None:
                    engine = _flatten_constant(c, source)
                elif c.type == "string":
                    sc = _child_by_type(c, "string_content")
                    if sc and path is None:
                        path = _text(sc, source)
    if not engine or not path:
        return
    url = _ns_path(ns_stack) + path
    route_id = f"route::MOUNT_{url}"
    graph.add_node(route_id, "route", name=f"MOUNT {url}",
                  url_pattern=url, http_method="MOUNT",
                  file=rel, line=call.start_point[0] + 1)
    if engine not in state.qualname_to_id:
        graph.ensure_external(engine, name=engine.split("::")[-1])
    target = state.qualname_to_id.get(engine, engine)
    graph.add_edge(route_id, target, "MOUNTS", path=url)


def _handle_devise_for(call: Node, source: bytes, rel: str,
                       ns_stack: list[str], graph: CodeGraph,
                       state: IndexState) -> None:
    pos, _ = _parse_call_args(call, source)
    if not pos or not isinstance(pos[0], str):
        return
    resource = pos[0]
    base = _ns_path(ns_stack) + "/" + resource
    line = call.start_point[0] + 1
    for verb, suffix, ctrl_part, action in _DEVISE_ROUTES:
        url = base + suffix
        ctrl_qn = "::".join(_ns_class_prefix(ns_stack) + [
            _camelize(resource), _camelize(ctrl_part) + "Controller"
        ])
        # Default devise controllers may also exist as project-defined classes;
        # fall back to a Devise:: synthetic when neither exists.
        action_qn = f"{ctrl_qn}#{action}"
        if action_qn not in state.qualname_to_id:
            action_qn = f"Devise::{_camelize(ctrl_part)}Controller#{action}"
            graph.ensure_external(action_qn, name=action)
        route_id = f"route::{verb}_{url}"
        graph.add_node(route_id, "route", name=f"{verb} {url}",
                      url_pattern=url, http_method=verb, file=rel, line=line)
        graph.add_edge(route_id, action_qn, "HANDLES", verb=verb, url=url)
