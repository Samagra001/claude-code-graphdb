# graphdb

[![tokens vs grep: 3.0x fewer](https://img.shields.io/badge/tokens%20vs%20grep-3.0x%20fewer-brightgreen)](./benchmarks/BENCHMARK.md)

A Kùzu-backed code-graph MCP plugin for Claude Code. Indexes Ruby on Rails
codebases into a queryable graph of associations, routes, callbacks, jobs, and
mailers — a token-efficient alternative to grep for navigating Rails code.

![graphdb demo](docs/demo.gif)

> The animation above shows the CLI. The same eight tools are exposed to Claude
> Code via MCP — see [docs/PROMPTS.md](docs/PROMPTS.md) for natural-language
> prompts you can try directly in a Claude Code session.

## Why

Rails encodes most of its architecture in **DSL declarations** (`has_many`,
`before_action`, `resources`, `include`) rather than direct method calls. Grep
matches the strings but cannot turn them into relationships:

| Question | Grep | graphdb |
|---|---|---|
| "What models reference User?" | 200+ matches across `_id` columns, validators, params | `query associations User` — 4 outgoing assocs + 16 reverse-refs |
| "What routes hit `Api::V1::CampaignsController`?" | Scan 218-line `routes.rb` mentally | `query routes Api::V1::CampaignsController` — 5 records |
| "Who delivers `UserInviteMailer`?" | `grep -rn UserInviteMailer` then read each callsite | `query callers UserInviteMailer --depth 2` — 4 invokers |
| "What jobs do `ScheduledReports::*` services enqueue?" | Multi-step grep + read | `query impact ScheduleExecutionJob` |

## Status

Proof of concept, validated against a real Rails 8 / Ruby 3.3 codebase
(`smart-hub-backend`, 494 indexed `.rb` files):

```
nodes: 2913    edges: 4731    index time: ~0.8s
60 models   63 controllers   25 concerns   121 services   67 serializers
7 mailers   6 jobs           109 migrations   230 routes   421 actions
```

Validation against grep ground truth on smart-hub-backend:

| Pattern | Grep | Graph | |
|---|---:|---:|---|
| `has_many` | 52 | 52 | exact |
| `belongs_to` | 79 | 79 | exact |
| `has_one` | 15 | 15 | exact |
| `scope` | 66 | 66 | exact |
| `deliver_later` + `deliver_now` | 11 | 11 | exact |
| `validates` | 48 lines | 58 edges | each call's rules expand to one edge per rule |
| `before_action` | 46 lines | 377 edges | each filter × N actions in `only:`/all |

## Quickstart

```bash
# Set up a venv with uv (sidesteps Homebrew's broken ensurepip on macOS)
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -e .

# Index a Rails project (writes to ~/.cache/graphdb/<repo>-<hash>.kuzu/;
# nothing is written to the target repo)
.venv/bin/graphdb --root /path/to/rails_app index /path/to/rails_app

# CLI queries
.venv/bin/graphdb --root /path/to/rails_app query def User
.venv/bin/graphdb --root /path/to/rails_app query associations User
.venv/bin/graphdb --root /path/to/rails_app query routes Api::V1::UsersController
.venv/bin/graphdb --root /path/to/rails_app query callers UserInviteMailer --depth 2
.venv/bin/graphdb --root /path/to/rails_app query impact ScheduleExecutionJob
.venv/bin/graphdb --root /path/to/rails_app query refs Account
.venv/bin/graphdb --root /path/to/rails_app stats
```

## MCP integration with Claude Code

Add to your Claude Code MCP config (`~/.claude.json` or project-level):

```json
{
  "mcpServers": {
    "graphdb": {
      "command": "/path/to/Graph_DB/.venv/bin/graphdb-mcp",
      "env": { "GRAPHDB_ROOT": "/path/to/your/rails_app" }
    }
  }
}
```

Tools exposed to Claude:

| Tool | Answers |
|---|---|
| `find_definition(name)` | Where is `name` defined? (model, controller, action, …) |
| `find_callers(name, depth)` | Who calls/enqueues/delivers `name`? (CALLS ∪ ENQUEUES ∪ DELIVERS) |
| `find_references(name)` | All inbound edges to `name` — associations, includes, calls, etc. |
| `impact_of(name, max_depth)` | Transitive callers — predicted blast radius |
| `module_overview(path)` | File-level summary: exports, imports, hotspots |
| `routes_for(controller_or_action)` | HTTP routes that hit a controller / action |
| `associations_of(model)` | All `has_many`/`has_one`/`belongs_to`/HABTM on a model + reverse |
| `graph_stats()` | Counts of nodes/edges by kind |

## Graph schema

```
NODE KINDS
  file, module, class, method, class_method,
  model, controller, action, concern, service, serializer,
  job, mailer, validator, helper, channel, error_class,
  migration, route, external

EDGE KINDS
  Generic Ruby:    CONTAINS, CALLS, INHERITS, INCLUDES
  Model DSL:       HAS_MANY, BELONGS_TO, HAS_ONE, HABTM, VALIDATES, SCOPES
  Controllers:     BEFORE_ACTION, AFTER_ACTION, SKIP_BEFORE, RENDERS
  Routes:          HANDLES, MOUNTS
  Async:           ENQUEUES, DELIVERS
```

## How it works

1. **`indexer.py`** parses every `.rb` under `app/`, `lib/`, `config/`,
   `db/migrate/` with tree-sitter-ruby. Two passes:
   - Pass 1: register every class/module/method as a node, build a qualname
     index used for resolution.
   - Pass 2: walk again, emit `INHERITS`, `INCLUDES`, `CALLS`, and the
     Rails-specific edges (`HAS_MANY`, `BEFORE_ACTION`, `ENQUEUES`, etc.)
   - Pass 3: a separate walker handles `config/routes.rb` (DSL outside any
     class), producing `Route` nodes with `HANDLES` edges to controller actions.
2. **`graph.py`** buffers nodes/edges in memory and bulk-flushes via Cypher
   `UNWIND` at end of indexing. Drops full-repo indexing from ~8 minutes (individual
   inserts) to ~0.8 seconds.
3. **`queries.py`** issues Cypher patterns like
   `MATCH (caller)-[:CALLS|ENQUEUES|DELIVERS*1..3]->(target)` — the database
   does the BFS.
4. **`mcp_server.py`** exposes the queries as MCP tools for Claude Code.

## Known PoC limitations

- **Static parsing only.** Doesn't run Rails — no type inference, no resolution
  of `define_method` / `method_missing` / dynamic `class_eval`.
- **Relative imports / autoload constants:** resolved by unique-name fallback;
  ambiguous names are skipped silently.
- **Custom DSLs (acts_as_paranoid, paper_trail's `has_paper_trail`, amoeba):**
  not yet specialized — they emit generic `CALLS` edges at class scope.
- **Routes:** `scope`, member/collection custom routes, `constraints`,
  `concerns` (route concerns), nested resources past 1 level — partial or
  skipped. The 7 standard REST actions, `namespace`, `mount`, `devise_for`,
  and `get/post/...` with `to:` work fully.
- **Reindex is one-shot.** Edit a file, rerun `graphdb index`. No file watcher.

## Roadmap

- Specialize the major Gemfile DSLs in smart-hub-backend (paper_trail, discard,
  amoeba, flipper).
- Member / collection / nested resource routes.
- Incremental reindex via file-watcher.
- AI-generated per-method summaries stored on nodes (read summary first, file second).
- Benchmark harness: grep-only vs graph-only Claude sessions on identical tasks.
