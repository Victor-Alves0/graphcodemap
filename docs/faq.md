# FAQ & Limitations

This project's design principle is **epistemic honesty** — so are its claims. This
page is deliberately candid about what GraphCodeMap does *not* do, and what the
numbers really say.

## Does the graph replace grep?

**No — it complements it.** For simply *finding* code, grep is often enough and
cheaper. The graph earns its cost on **structural** questions — impact ("what
breaks if I change this"), multi-hop call chains, dataflow/taint — and in large or
unfamiliar codebases. Use the right tool for the question.

## How accurate are the benchmarks?

They are **small-scale and directional, not proof of SOTA.** We report them
because hiding the caveats would violate the whole point.

- **Localization (SWE-bench-Lite pilot, n=15).** The graph arm found the file to
  edit in 93% of cases vs 80% for a grep/read baseline — but +2 tasks at n=15 is
  within noise, and it measures localization, not full issue resolution.
- **Reachability (flask, n=3, entry→sink chain).** The graph arm scored 100%
  correct vs 67% baseline, and 0.92 vs 0.58 chain recall, with fewer tool calls
  (14.7 vs 17.3) — but roughly **token parity** (43.6k vs 41.2k avg; one task where
  the graph over-explored dragged the average up).

The lesson we keep re-learning: the graph buys **correctness and completeness on
structural questions**, not a universal token discount. Where it *also* saves
tokens is when `certain` L1 edges let the agent trust an answer and stop.

Full methodology and caveats: [evals/RESULTS.md](../evals/RESULTS.md).

## How reliable is dataflow/taint?

Treat findings as **candidates to verify, not verdicts.** The analysis is:

- **may-taint** — it over-approximates, so it can report a flow that a stricter
  analysis would rule out (false positives are expected; the goal is not to miss).
- **flow-sensitive** in 18 of the 19 dedicated code languages (everything but
  Clojure) — statement order and redefinition ARE modelled: reassigning a
  variable kills the old taint. The `codegraph capabilities` map says which
  languages have it; the ones that don't fall back to the over-approximating
  engine, and each finding declares which was used (`flow_evidence`).
- **field-sensitive** — access paths with a prefix rule: a tainted object taints
  its fields, but tainting one field does not taint its siblings (validated for
  Python and JS/TS; the generic tier applies a safe base-name fallback).
- **alias-insensitive** — aliasing is out of scope.

It is a pragmatic, incremental
[Code Property Graph](RESEARCH.md)-*lite* — not a whole-program engine like Joern
— and its advantage is being fresh and interprocedural on demand.

## What are `certain` / `inferred` / `possible`?

The confidence of a call edge. `certain` = resolved by a real language server (L1);
`inferred` = traced through a single-target import (L0); `possible` = a name match
with one or more candidates (L0). No query ever upgrades a level, and transitive
queries propagate the *minimum* along a path. See
[Concepts › Confidence](concepts.md#confidence).

## Can it miss calls?

Yes — **static analysis can miss dynamic or reflective dispatch** (a call made
through a string name, a registry, runtime metaprogramming). Every call-graph
answer *says so* via the completeness line / `dynamic_dispatch_possible` field
when relevant. It never presents partial recall as complete.

## What about symbol identity when I move code?

Editing a function's body or moving it within a file **preserves** its identity
(the id is `hash(path, fqn, kind, discriminator)`, with callable signatures
independent of the body and sibling order). Moving it to
a *different file* breaks identity — it becomes delete + add — because the path is
part of the id. This is an accepted, documented v1 trade-off.

## Scale

Measured to **100k+ files** with a reproducible harness
([`evals/scalebench.py`](../evals/scalebench.py)). Well-structured (namespaced)
code scales cleanly: 100k files index in ~8 min at **324 MB peak, no OOM**, on the
one-time-index + hot-watcher model. Two honest ceilings surfaced:

1. **The strong freshness guarantee is an O(files) sweep** on every empty result.
   Profiling found 72% of it was `os.path.relpath` (millions of `normcase` calls on
   Windows) plus `pathspec` matching; removing that (relative path built during
   descent, dir-only ignore spec) took it from **~5s → ~0.6s per missed query at
   100k** (~7.7×), same guarantee. In production (MCP + watcher) the sweep is
   skipped while a live watcher already guarantees freshness (30s backstop for
   dropped events).
2. **Dense C at scale needs active L1.** The full Linux kernel (72k C files) did
   *not* complete on the dev box — C is ~30× denser on disk (55 KB/file) and
   name-based resolution fans out pathologically without namespaces (`dev_err`
   called 35k×, `ARRAY_SIZE` 31k×), so `certain` L1 resolution (clangd) becomes a
   **feasibility** requirement there, not a nicety.

For repos too big or dense to index whole, **`index --scope <subtree>`** indexes
only the part you care about (persisted, additive; the freshness sweep then walks
only that subtree — ~4ms for a 500-file scope of a 100k repo vs ~0.7s for the
whole).

## Concurrency & multi-tenancy

One `CodeGraph`/`QueryEngine` instance is single-threaded — share it only under
external serialization (the MCP server uses a lock) or use one instance per thread.
Writes retry on `database is locked`; call-graph cycles terminate safely.
Credentials and exclusion policy are injected, never read from global env, so a
host serving many users stays safe and cost stays attributable. `doctor()` never
leaks the absolute server path.

## Configuration

- **L3 / eval**: set `OPENROUTER_API_KEY` explicitly in the process environment;
  GraphCodeMap does not consume credentials from the analyzed repository's
  `.env` unless `CODEGRAPH_ALLOW_REPO_ENV=1` is explicitly set. `describe`
  sends selected source context to the external provider. Pick the model with
  `CODEGRAPH_L3_MODEL`. In a host, inject the credential via `llm=` instead — see
  [Library](library.md#l3-provider).
- **Logging**: off by default. `CODEGRAPH_LOG=warning` for warnings,
  `CODEGRAPH_DEBUG=1` for full debug.
- **Taint rules**: `.codegraph/taint.json` (sources/sinks/sanitizers).
- **Exclusions**: `.gitignore` / `.codegraphignore`, or the host's `exclude=`
  policy stored in the index.

## How do I report a bug or ask for a language?

Open an issue: <https://github.com/Victor-Alves0/graphcodemap/issues>. For a new
language resolver, see [CONTRIBUTING.md](../CONTRIBUTING.md) — it's often a
~10-line config.
