"""Benchmark graphdb vs grep on smart-hub-backend.

For each navigation task, measures wall-clock time and tiktoken (cl100k_base)
token counts for (a) the graphdb query and (b) the equivalent grep approach
a programmer or Claude would otherwise run.

Two grep variants are measured where it matters:
- "minimum": the raw grep output — sometimes that already answers the question.
- "realistic": grep + the file reads required to actually understand the matches.

Assumes the kuzu DB has been snapshot to /tmp/graphdb-bench.kuzu (so the running
MCP server's lock on ~/.cache/graphdb doesn't block read access).
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/samagrapathak/Desktop/Graph_DB/src")

import tiktoken

from graphdb import queries
from graphdb.graph import CodeGraph

REPO = "/Users/samagrapathak/Desktop/smart-hub-backend"
DB_PATH = "/tmp/graphdb-bench.kuzu"
RUNS = 5

graph = CodeGraph(root=REPO, db_path=DB_PATH)
enc = tiktoken.get_encoding("cl100k_base")


def _stats(samples_ms: list[float]) -> dict:
    return {
        "median_ms": round(statistics.median(samples_ms), 2),
        "min_ms": round(min(samples_ms), 2),
    }


def measure_graph(fn, *args, **kwargs) -> dict:
    samples = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        result = fn(graph, *args, **kwargs)
        samples.append((time.perf_counter() - t0) * 1000)
    text = json.dumps(result, indent=2, default=str)
    return {
        **_stats(samples),
        "bytes": len(text),
        "tokens": len(enc.encode(text)),
        "records": len(result) if isinstance(result, list) else None,
    }


def measure_shell(cmd: str) -> dict:
    samples = []
    out = ""
    for _ in range(RUNS):
        t0 = time.perf_counter()
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, cwd=REPO
        )
        samples.append((time.perf_counter() - t0) * 1000)
        out = proc.stdout
    return {
        **_stats(samples),
        "bytes": len(out),
        "tokens": len(enc.encode(out)),
        "lines": out.count("\n"),
    }


def measure_grep_then_read(grep_cmd: str, max_files: int | None = None) -> dict:
    """Cost of grep + reading each unique file it returned (realistic Claude flow).

    `grep_cmd` must produce `path:line:content` lines (i.e. use `-rn`).
    `max_files` caps the reads (a real Claude would stop somewhere).
    """
    samples = []
    total_text = ""
    for _ in range(RUNS):
        t0 = time.perf_counter()
        proc = subprocess.run(
            grep_cmd, shell=True, capture_output=True, text=True, cwd=REPO
        )
        grep_out = proc.stdout
        files = []
        seen = set()
        for line in grep_out.splitlines():
            if ":" not in line:
                continue
            path = line.split(":", 1)[0]
            if path not in seen:
                seen.add(path)
                files.append(path)
            if max_files and len(files) >= max_files:
                break
        read_text = ""
        for p in files:
            try:
                read_text += Path(REPO, p).read_text(errors="ignore")
            except (FileNotFoundError, IsADirectoryError):
                pass
        samples.append((time.perf_counter() - t0) * 1000)
        total_text = grep_out + read_text
    return {
        **_stats(samples),
        "bytes": len(total_text),
        "tokens": len(enc.encode(total_text)),
        "files_read": len(files),
    }


TASKS = [
    {
        "id": "routes",
        "question": "What routes hit Api::V1::CampaignsController?",
        "graph": ("routes_for", "Api::V1::CampaignsController"),
        "grep_min": "grep -n -i campaigns config/routes.rb",
        # Realistic: you need the surrounding namespace context, so read the file.
        "grep_real": "grep -n -i campaigns config/routes.rb && cat config/routes.rb",
    },
    {
        "id": "callers",
        "question": "Who delivers UserInviteMailer?",
        "graph": ("find_callers", "UserInviteMailer", 2),
        "grep_min": "grep -rn UserInviteMailer app/ lib/ 2>/dev/null",
        "grep_real_files": "grep -rn UserInviteMailer app/ lib/ 2>/dev/null",
    },
    {
        "id": "assoc",
        "question": "What associations does Account have (incl. reverse refs)?",
        "graph": ("associations_of", "Account"),
        "grep_min": "grep -nE '^\\s*(has_many|has_one|belongs_to|has_and_belongs_to_many)' app/models/account.rb",
        # Realistic: also need reverse-refs from other models.
        "grep_real": "grep -nE '^\\s*(has_many|has_one|belongs_to)' app/models/account.rb && grep -rnE 'belongs_to :account|has_many :accounts|has_one :account' app/models/",
    },
    {
        "id": "impact",
        "question": "Blast radius (depth 3) of changing ScheduleExecutionJob",
        "graph": ("impact_of", "ScheduleExecutionJob", 3),
        "grep_min": "grep -rn ScheduleExecutionJob app/ lib/ 2>/dev/null",
        # depth-N grep would require iterative re-greping; the upper-bound
        # cost is reading every file that mentions it, then doing it again
        # for each method found there. We approximate with a single-level read.
        "grep_real_files": "grep -rn ScheduleExecutionJob app/ lib/ 2>/dev/null",
    },
    {
        "id": "def",
        "question": "Where is the method `notify` defined?",
        "graph": ("find_definition", "notify"),
        "grep_min": "grep -rn 'def notify' app/ lib/ 2>/dev/null",
    },
]


def run() -> list[dict]:
    results = []
    for t in TASKS:
        graph_fn = getattr(queries, t["graph"][0])
        graph_args = t["graph"][1:]
        g = measure_graph(graph_fn, *graph_args)

        gmin = measure_shell(t["grep_min"])

        greal = None
        if "grep_real" in t:
            greal = measure_shell(t["grep_real"])
        elif "grep_real_files" in t:
            greal = measure_grep_then_read(t["grep_real_files"], max_files=20)

        results.append({"task": t, "graph": g, "grep_min": gmin, "grep_real": greal})
    return results


def main() -> None:
    results = run()
    for r in results:
        t = r["task"]
        print(f"\n=== {t['question']}")
        print(f"  graph    {r['graph']}")
        print(f"  grep-min {r['grep_min']}")
        if r["grep_real"]:
            print(f"  grep-real {r['grep_real']}")
        gt = r["graph"]["tokens"]
        if r["grep_real"]:
            ratio = r["grep_real"]["tokens"] / max(gt, 1)
            print(f"  realistic-token ratio: grep {ratio:.1f}x graph")
    out_path = Path(__file__).parent / "results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
