# graphdb — Claude Code prompt cookbook

A set of natural-language prompts you can paste into Claude Code (with the
`graphdb` MCP server connected) to exercise every tool. Calibrated to the
`smart-hub-backend` codebase so you can verify the answers.

## Before you start

1. **Reindex** (always run after pulling new code in the target repo):
   ```bash
   /Users/samagrapathak/Desktop/Graph_DB/.venv/bin/graphdb \
     --root /Users/samagrapathak/Desktop/smart-hub-backend \
     index /Users/samagrapathak/Desktop/smart-hub-backend
   ```
2. **Restart Claude Code** so the MCP server reopens the fresh graph.
3. **Confirm connection** — in Claude Code, type `/mcp` and verify `graphdb`
   is listed and connected.
4. **Open a fresh conversation** so prior context doesn't bias tool choice.

A quick sanity-check prompt:

> List the graph stats for smart-hub-backend.

Claude should call `graph_stats` and return the node/edge counts. If it
instead reaches for `Bash` or `Read`, the MCP server isn't being surfaced —
restart Claude Code and try `/mcp` again.

---

## Category 1 — Routes and endpoints

These exercise `routes_for`. Best when grep would otherwise force a long scan
of `config/routes.rb` with mental namespace tracking.

### 1.1 Map a controller to its URLs

> What HTTP routes hit `Api::V1::CampaignsController`?

**Expected:** 5 records (GET/POST/GET-by-id/PATCH/DELETE) with verb, URL,
handler, file:line.

**Why graph wins:** `routes.rb` declares this inside
`namespace :api { namespace :v1 { resources :campaigns } }`. Grep finds the
`resources :campaigns` line but not the resolved URL — graphdb pre-computed
the namespace walk.

### 1.2 Find a single endpoint's handler

> Which controller action handles `POST /api/v1/users/sign_in`?

**Expected:** `Api::V1::Users::SessionsController#create` (Devise route).

### 1.3 Compare two related controllers

> What routes hit `Api::V1::MembershipsController`, and what about
> `Api::V1::AdminEmployeesController`?

**Expected:** Claude makes two `routes_for` calls and compares them. Useful
test for whether tool selection holds across follow-ups.

---

## Category 2 — Model relationships

These exercise `associations_of`. Useful for "how is this model connected to
everything else?" questions that grep struggles with because reverse-refs
live in other files.

### 2.1 Full association map for one model

> What are all the ActiveRecord associations on the `Account` model,
> including incoming references from other models?

**Expected:** Outgoing (`has_many :users`, etc.) **plus** reverse-refs (e.g.
`Membership BELONGS_TO :account`, `Campaign BELONGS_TO :account`, …).

### 2.2 Compare two models' relationships

> What's the difference between `User`'s associations and `AdminUser`'s
> associations?

**Expected:** Two `associations_of` calls. Claude should call out which one
has more reverse-refs (User does — it's referenced by many app-side tables).

### 2.3 Find a specific relationship type

> Which models have a `has_many :through` relationship?

**Expected:** Claude calls `find_references` or filters via Cypher. A pure
graph query — grep would miss `through:` declarations that span lines.

---

## Category 3 — Refactor and impact analysis

These exercise `impact_of` and `find_callers`. The "blast radius" questions
that justify a graph in the first place.

### 3.1 Blast radius of changing a job

> If I change the behavior of `ScheduleExecutionJob`, what code paths could
> be affected? Use depth 3.

**Expected:** Direct callers (services that call `perform_async`), then their
callers (controllers, other services), up to 3 hops.

**Why graph wins:** Depth-3 grep requires N iterations of grep-and-read-file.
graphdb does it in one Cypher BFS.

### 3.2 Who delivers a mailer

> Who delivers `UserInviteMailer`? Use depth 2.

**Expected:** Direct delivery callsites (4 of them in this codebase) plus the
controller actions that call those methods.

**Why graph wins:** `find_callers` walks `CALLS ∪ ENQUEUES ∪ DELIVERS` in one
query. Grep would match the mailer's class name in unrelated places (specs,
comments, the mailer file itself).

### 3.3 Renaming impact

> I want to rename `Membership#deliver_membership_invite!`. What code calls
> it, directly or indirectly?

**Expected:** Direct call from `MembershipsController#create`, plus any
service that calls the controller (rare, but graph will catch it).

### 3.4 Find every code path that ends in a Sidekiq job

> Trace all paths that enqueue any `*Job`. Group by the entry point.

**Expected:** Claude calls `find_callers` on each Job class, organizes by
controller/service. Tests cross-edge traversal at scale.

---

## Category 4 — Cross-cutting concerns

These exercise the Rails-specific edge types (`BEFORE_ACTION`, `INCLUDES`,
`HAS_MANY`, `HANDLES`) via `find_references`.

### 4.1 Which actions run a given filter

> Which controller actions have `authenticate_user!` as a before_action?

**Expected:** Many actions. Claude uses `find_references` on
`authenticate_user!` filtered to `BEFORE_ACTION` edges. The graph stores one
edge per (filter, action) pair, so this returns the exact action list.

**Why graph wins:** `before_action :authenticate_user!, only: [:show]` is one
line of grep but the answer is the *expanded* action set. The graph has the
expansion pre-computed.

### 4.2 Which models include a concern

> Which classes include the `AccountLevelIsolate` concern?

**Expected:** The list of controllers (or models) that include it. Edge type
`INCLUDES`. Grep would match the string but graphdb gives you the resolved
class names with file paths.

### 4.3 Validations on a field

> Which models validate the `:email` field, and with which rules?

**Expected:** Claude uses `find_references` or Cypher to find VALIDATES
edges with `field='email'`. Useful for compliance / audit questions.

---

## Category 5 — Architecture and orientation

These exercise `module_overview`, `find_definition`, `graph_stats`. Useful on
day one of working with an unfamiliar Rails app.

### 5.1 First look at a file

> Give me a high-level overview of `app/services/scheduled_reports/create_service.rb`.

**Expected:** `module_overview` is called. Returns the file's classes,
methods, and which methods are most-called from elsewhere.

### 5.2 Where is something defined

> Where is the `Membership` model defined? Give me file and line.

**Expected:** `find_definition` → `{file: 'app/models/membership.rb', line: 3}`.

### 5.3 Codebase-wide architecture overview

> Give me a high-level shape of this codebase — how many models, controllers,
> services, jobs, mailers, and routes?

**Expected:** `graph_stats` call. Should return the counts (60 models, 63
controllers, 121 services, 6 jobs, 7 mailers, 230 routes).

### 5.4 Onboarding scenario

> I'm new to this codebase. I'm about to work on the `Memberships` feature.
> What should I read first?

**Expected:** Claude combines `find_definition`, `module_overview`,
`routes_for`, and `associations_of`. Tests whether the tools compose
naturally on an open-ended question.

---

## Category 6 — Direct comparisons (for the doubters)

Useful when demoing to someone skeptical that this beats grep.

### 6.1 Side-by-side

> Two ways to answer this: first use grep to find the routes that hit
> `Api::V1::CampaignsController`, then use the graphdb tools. Compare the
> output and the token count.

**Expected:** Claude runs both. The grep approach forces reading
`config/routes.rb` (~218 lines). The graph approach returns 5 structured
records. Roughly 6× fewer tokens.

### 6.2 Coverage question grep struggles with

> Which models point at `User` via any association type? Don't read source
> files — answer from the graph.

**Expected:** 16+ reverse-refs returned in one `associations_of` call. Grep
equivalent: multiple invocations across different association keywords +
reading each match for context.

---

## How phrasing affects tool selection

Claude picks tools by matching your prompt's language to each tool's
description. Tip sheet:

| Says this in your prompt | Likely tool |
|---|---|
| "routes", "URLs", "endpoints", "hit a controller" | `routes_for` |
| "associations", "has_many", "belongs_to", "reverse references" | `associations_of` |
| "who calls", "callers", "callsites", "delivers", "enqueues" | `find_callers` |
| "blast radius", "impact", "what breaks if I change", "transitively" | `impact_of` |
| "where is X defined", "find the definition of X" | `find_definition` |
| "all references to X", "everywhere X is touched" | `find_references` |
| "overview of file", "what's in this file" | `module_overview` |
| "how big is the codebase", "counts", "stats" | `graph_stats` |

Phrasings that confuse tool selection (Claude may grep instead):

- *"How is X wired up?"* — too vague.
- *"Show me everything about User"* — too broad; combine multiple tools but
  unclear which to start with.
- *"What does this code do?"* (without naming a file or class) — Claude will
  reach for `Read`.

When that happens, just nudge: *"Use the graphdb tools."*

---

## Tool reference

| Tool | Args | Output shape |
|---|---|---|
| `graph_stats()` | — | `{nodes, edges, node_*: count, edge_*: count}` |
| `find_definition(name)` | `name: str` | `[{id, kind, name, qualname, file, line, signature, …}]` |
| `find_references(name)` | `name: str` | `[{... summary, edge_kind}]` for every inbound edge |
| `find_callers(name, depth)` | `name: str, depth: int=1` | `[{... summary, hops}]` over CALLS∪ENQUEUES∪DELIVERS |
| `impact_of(name, max_depth)` | `name: str, max_depth: int=3` | `{target, found, direct, total, callers[]}` |
| `module_overview(path)` | `path: str` (file path, relative to root) | `{module, file, exports[], most_called[]}` |
| `routes_for(controller_or_action)` | `name: str` (e.g. `Api::V1::UsersController` or `Api::V1::UsersController#create`) | `[{verb, url, handler, file, line}]` |
| `associations_of(model)` | `model: str` (e.g. `User`) | `{has_many[], has_one[], belongs_to[], habtm[], referenced_by[]}` |

All return JSON-serializable structures. The MCP server stringifies them with
`indent=2` for readability — Claude can still parse them as JSON when
chaining tools.

---

## Troubleshooting

**Claude reaches for grep instead of the tools.**
First check `/mcp` shows `graphdb` connected. If yes, your prompt may be too
generic — add "use the graphdb tools" or rephrase using the keywords in the
tool-selection table above.

**Tool returns "found": false.**
The name doesn't exist in the index. Check spelling (case-sensitive), check
the graph is up to date (reindex if you've edited code since), and try the
fully-qualified name (`Api::V1::UsersController` instead of just
`UsersController`).

**Tool errors out.**
Usually means the MCP server is running stale code. Restart Claude Code
fully (close and reopen, not just a new conversation).
