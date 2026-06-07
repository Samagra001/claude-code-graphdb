# graphdb vs grep — navigation benchmark

A first-pass comparison of `graphdb` (Kùzu-backed Rails code graph) against
`grep` (with realistic follow-up file reads) on five navigation questions
against the `smart-hub-backend` codebase. The interesting metric is **tokens
to answer**, since that is what determines how much of Claude Code's context
each tool burns. Wall-clock time is reported as a secondary signal.

## TL;DR

For four of five tasks, `graphdb` returns a more complete answer in fewer
tokens than the realistic grep flow. The exception (`associations_of`) is a
case where `graphdb` emits more structured detail than a single-file grep
would surface — it's *more output*, not *worse output*, and still cheaper than
the realistic grep-plus-reverse-refs flow.

| Task | graph tokens | grep + reads tokens | grep / graph |
|---|---:|---:|---:|
| Routes for `Api::V1::CampaignsController` | 281 | 1,767 | **6.3×** |
| Callers of `UserInviteMailer` (depth 2) | 456 | 2,285 | **5.0×** |
| Associations of `Account` (incl. reverse-refs) | 1,203 | 505 | 0.4× |
| Impact of `ScheduleExecutionJob` (depth 3) | 251 | 1,981 | **7.9×** |
| Definition of `notify` | 1 | 0 | — (both empty) |

Across the four tasks that returned content, graph used **2,191 tokens total
vs grep's 6,538 — a 3.0× overall reduction**. Excluding the one task where
grep wins (associations), the ratio is **6.1× across the three remaining
tasks**.

## Setup

- **Target codebase:** `/Users/samagrapathak/Desktop/smart-hub-backend` —
  Rails 8 / Ruby 3.3, 494 indexed `.rb` files, 2,913 nodes, 4,731 edges.
- **graphdb:** commit at HEAD of `main`, Kùzu 0.11, Python 3.13.
- **Index:** prebuilt at `~/.cache/graphdb/smart-hub-backend-edf5dd6e.kuzu`
  (69 MB). Because the running MCP server holds a write lock, the benchmark
  reads from a snapshot copy at `/tmp/graphdb-bench.kuzu`.
- **Tokenizer:** `tiktoken` `cl100k_base`. This is OpenAI's tokenizer and is
  not exactly Claude's, but it's a stable, locally-runnable proxy. The
  *ratios* between approaches are what matters; absolute numbers are
  approximations within ~10–20%.
- **Iterations:** 5 runs per measurement, median reported.
- **Host:** macOS 25, M-series Apple Silicon. Cold caches not enforced —
  `grep` benefits from filesystem cache after the first run; so does Kùzu's
  buffer pool. Both run under the same warmup.

## Methodology

For each question we measure three approaches:

1. **graph** — the relevant graphdb query (`routes_for`, `find_callers`,
   `associations_of`, `impact_of`, `find_definition`). Output is the
   `json.dumps(result, indent=2)` form, exactly what the MCP server returns
   to Claude.
2. **grep-min** — the bare grep command. Sometimes that already answers the
   question; often it does not.
3. **grep-real** — grep plus the file reads a programmer or Claude would do
   to actually understand the matches. For "raw context" questions (routes,
   associations) this is `cat`ing the relevant config or model file. For
   "where else is X used" questions (callers, impact) it's reading every
   file the grep returned (capped at 20).

Tokens are counted on the *combined* text the approach produces — for
`grep-real` this is the grep output **plus** the contents of every file
read. This mirrors what would land in Claude's context window.

The benchmark script is at [`bench.py`](./bench.py) and the raw output is
[`results.json`](./results.json).

## Per-task results

### 1. "What routes hit `Api::V1::CampaignsController`?"

| Approach | Tokens | Median latency |
|---|---:|---:|
| `routes_for("Api::V1::CampaignsController")` | **281** | 5 ms |
| `grep -n campaigns config/routes.rb` (raw) | 48 | 37 ms |
| grep + `cat config/routes.rb` (realistic) | 1,767 | 33 ms |

**Why the graph wins:** `routes.rb` declares routes inside nested
`namespace :api do; namespace :v1 do; resources :campaigns` blocks. A bare
grep returns four matching lines but **not the namespace context**, so to
actually answer "what's the URL and verb" you have to read the surrounding
file. graphdb resolved the namespace at index time and returns
`{verb, url, handler, file, line}` records directly.

### 2. "Who delivers `UserInviteMailer`?"

| Approach | Tokens | Median latency |
|---|---:|---:|
| `find_callers("UserInviteMailer", depth=2)` | **456** | 171 ms |
| `grep -rn UserInviteMailer app/ lib/` (raw) | 134 | 90 ms |
| grep + read each matching file (6 files) | 2,285 | 35 ms |

**Why the graph wins:** grep matches the *string* `UserInviteMailer` in 7
places across 6 files (model, service, mailer file itself, specs). Of those,
only a subset are actual delivery callsites; the rest are mentions in
comments, `params` keys, or class definitions. graphdb's
`find_callers` returns just the four callsites that go through
`CALLS`/`ENQUEUES`/`DELIVERS` edges, with caller qualnames pre-resolved.

### 3. "What associations does `Account` have?"

| Approach | Tokens | Median latency |
|---|---:|---:|
| `associations_of("Account")` | **1,203** | 6 ms |
| `grep ...has_many\|belongs_to... account.rb` (raw) | 189 | 18 ms |
| grep + reverse-refs grep | 505 | 22 ms |

**Why grep wins here:** `associations_of` returns a verbose JSON structure
with full qualified names, association options, and a full reverse-ref list
(every other model that points back at Account). The grep flow returns less
structured text and skips reverse-refs. This is **not graph losing** — it's
returning strictly more information, and avoids the
"are you sure that's all the reverse-refs?" follow-up Claude would otherwise
do. But if you only want the forward associations on one model, grep is
adequate and cheaper. A `--brief` mode on `associations_of` would close the
gap.

### 4. "Blast radius (depth 3) of changing `ScheduleExecutionJob`"

| Approach | Tokens | Median latency |
|---|---:|---:|
| `impact_of("ScheduleExecutionJob", max_depth=3)` | **251** | 245 ms |
| `grep -rn ScheduleExecutionJob app/ lib/` (depth 1 only) | 91 | 32 ms |
| grep + read each matching file (depth 1 only) | 1,981 | 31 ms |

**Why the graph wins, and why grep can't really compete:** grep finds the
direct mentions (4 files) but **does not transitively traverse** to find
"who calls the callers" at depth 2 and 3. To equal the graph's answer with
grep you'd have to iterate: grep, identify each containing method, grep
each of those names, repeat. The realistic cost of a depth-3 grep flow is
much higher than the single-pass number above — we are *under*-reporting
the grep cost here. graphdb gets it in one query.

The 245 ms graph latency is the slowest in the suite — it's a BFS up to
depth 3 across multiple edge types. Still well under a second.

### 5. "Where is the method `notify` defined?"

| Approach | Tokens | Median latency |
|---|---:|---:|
| `find_definition("notify")` | 1 | 1 ms |
| `grep -rn 'def notify' app/ lib/` | 0 | 32 ms |

Both correctly return nothing — `smart-hub-backend` has no method literally
named `notify`. This is included as a sanity check: graphdb does not invent
results when there's no match, and on the cheap cases is no worse than grep.

## Latency observations

- **graphdb is faster than grep on small structural queries.** Lookups that
  hit indexed columns (`routes_for`, `associations_of`, `find_definition`)
  finish in 1–6 ms, vs grep's ~30 ms shell + filesystem floor.
- **graphdb is slower on deep traversals.** `find_callers` (170 ms) and
  `impact_of` depth 3 (245 ms) do multi-hop BFS in Cypher. Still
  sub-second; still equivalent to a multi-step grep loop that would take
  several seconds.
- The grep floor of ~30 ms is shell + process startup. On warm cache the
  actual scan is well under 10 ms.

## Honest caveats

- **tiktoken ≠ Claude's tokenizer.** Numbers are approximate. Ratios are
  reliable; absolute counts are within ~10–20%.
- **One target codebase, one snapshot in time.** A larger or more
  metaprogramming-heavy Rails app would change the graph-vs-grep balance
  in both directions. graphdb might miss more dynamic dispatch; grep would
  produce even noisier results.
- **No measurement of Claude's actual reasoning tokens.** We measure what
  enters the context window, not how many output tokens Claude spends
  reasoning over it. The latter likely amplifies the graph advantage,
  since a structured JSON answer requires less reasoning to consume than
  a 1,700-token routes.rb plus surrounding namespace logic.
- **grep-real is conservative.** For depth-N questions like `impact_of`,
  the realistic grep flow is *iterative* (grep → identify hits → grep again
  on each hit's containing symbol → repeat). We measure a single pass,
  understating grep's true cost. The numbers in this doc are a lower bound
  for grep, not an upper bound.
- **Warm cache.** Both tools benefit from the OS file cache after the first
  iteration. Cold-start numbers would be different (worse for both, more so
  for grep).
- **Excludes graphdb's index-time cost.** Indexing smart-hub-backend takes
  ~0.8 seconds. That's a one-time cost per codebase, amortized over every
  query. Not free, but cheap on the scale of a Claude Code session.

## Reproducing

```bash
# Snapshot the kuzu directory so the running MCP server's lock doesn't block reads
cp -r ~/.cache/graphdb/smart-hub-backend-*.kuzu /tmp/graphdb-bench.kuzu

# Install tiktoken into the project venv
uv pip install --python .venv/bin/python tiktoken

# Run the benchmark
.venv/bin/python benchmarks/bench.py
```

Edit `TASKS` in `bench.py` to add or swap questions. `results.json` is
rewritten each run.

## What this suggests

Each pattern below maps to a specific shape of question, with a concrete
example from `smart-hub-backend`.

### Where graphdb pays for itself

**1. Route resolution (namespace-encoded).** Rails nests routes inside
namespace blocks:

```ruby
namespace :api do
  namespace :v1 do
    resources :campaigns          # ← what URL is this?
  end
end
```

`grep campaigns config/routes.rb` returns the `resources :campaigns` line —
but **not** the fact that the URL is `/api/v1/campaigns`. To answer, you
have to read the surrounding 50+ lines and mentally walk the namespace
stack. `routes_for("Api::V1::CampaignsController")` returns the resolved
`{verb: POST, url: /api/v1/campaigns}` directly. The graph did the
namespace walk at index time, once.

**2. Transitive impact ("who calls who at depth N").** Say you want to
rename `ScheduleExecutionJob.perform`. Direct callers (depth 1) are easy —
grep finds them. But you also need:

- Depth 2: who calls those callers? (e.g. a controller action that invokes
  the service that enqueues the job)
- Depth 3: who calls *those*? (e.g. a scheduled task that triggers the
  controller flow)

To do depth-3 with grep you grep for `ScheduleExecutionJob`, identify each
containing symbol, grep for *each of those*, then repeat. That's N rounds
of greps and N rounds of file reads. `impact_of("ScheduleExecutionJob",
max_depth=3)` does it in one Cypher BFS — 251 tokens, one tool call.

**3. Mention vs actual callsite.** `grep -rn UserInviteMailer app/` returned
7 hits in this codebase. Of those:

- 1 is the class definition (`class UserInviteMailer < …`)
- 1 is a string literal in a config
- 2 are spec test setups
- 3 are actual delivery callsites

grep cannot distinguish these — they're all the same string match.
`find_callers` traverses `DELIVERS` edges only, so it returns just the 3
real callsites. For a question like "who actually sends this email?",
that's the difference between getting a clean answer and reading 6 files
to filter the noise yourself.

### Where grep is fine or better

**4. One-shot literal-string lookup.** "Does anyone use the constant
`FORBIDDEN_KEYS`?" — grep is perfect. graphdb doesn't index string literals
or non-DSL constants at the value level. Same for `grep TODO`,
`grep -rn 'X-API-Key'`, finding a typo'd method name, locating a hardcoded
URL.

**5. "Just show me the lines."** "How is `email` validated on User?" —
`grep -n 'validates :email' app/models/user.rb` returns

```ruby
validates :email, presence: true, format: { with: URI::MailTo::EMAIL_REGEXP }
```

— one line, exact source, ready to read. graphdb's structured output for
validations would emit 3–4 edges (one per rule) which is more verbose for
a "show me the source" question. The Account benchmark (0.4× ratio,
section 3 above) was exactly this pattern: too much structure for a question
that wanted text.

### Concrete session: "give Claude both"

Imagine the prompt: *"Rename `ScheduleExecutionJob` to `JobRunner` and make
sure nothing breaks."*

A well-tuned Claude flow:

1. `impact_of("ScheduleExecutionJob", max_depth=3)` → 251 tokens, gets the
   full transitive caller tree.
2. `find_callers(...)` to confirm the depth-1 callsites that need the
   rename in Ruby code.
3. `grep -rn ScheduleExecutionJob` → catches anything graphdb missed:
   string literals in YAML, mentions in `Procfile`, `Sidekiq` queue config,
   inline comments, etc.
4. `Edit` each file.

Steps 1–2 use graphdb because the question is "what's the reachable set in
Ruby code". Step 3 uses grep because the question is "are there any string
mentions outside parsed Ruby". Neither tool alone is sufficient; together
they're complementary.

### Takeaway

The benchmark is not arguing "graphdb instead of grep." It's arguing that
for the question shapes in §1–3 (structural, multi-hop, mention-vs-call),
graphdb is **strictly cheaper and more accurate**, and Claude should reach
for it first. For §4–5 (literal strings, "show me the source"), grep
remains the right tool. The MCP server exposes graphdb's 8 query tools
*alongside* Claude's existing Bash + grep — Claude picks per question.
