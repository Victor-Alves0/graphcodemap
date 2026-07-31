# Architecture

How the engine is put together, and *why* — the pieces that make incremental
re-indexing correct and the freshness guarantee real.

> This is the operational view. For the original design contract (with the full
> rationale) see [DESIGN.md](DESIGN.md); for the mental model see
> [Core Concepts](concepts.md).

## The pipeline

```
        ┌─ startup: boot scan (content-hash) ─┐
 fs ──► │  watcher: file events               ├─► re-index queue ─► L0 indexer
        └─ query: read-repair ────────────────┘        │            (tree-sitter,
                                                        ▼             workers)
                              L1 resolver (LSP, async) ─► SQLite ◄─ dangling re-resolve
                                                            ▲
                              L3 enricher (LLM, lazy) ──────┘
                                                            │
                    query engine (envelope + read-repair + lazy PageRank)
                                                            │
                       ┌──────────────┬────────────────┬────┴─────────────┐
                     library         CLI              MCP server (stdio)
```

Three independent triggers feed the same re-index queue — boot scan, watcher, and
read-repair — so no single mechanism is load-bearing for freshness. The L0 indexer
is the only writer.

## The layers

- **L0 — structural.** tree-sitter parses each file; extractors emit symbols and
  `calls`/`imports`/`inherits` edges. Runs on any repo, no configuration.
- **L1 — semantic.** An async LSP/jedi resolver promotes edges to `certain`.
  Optional; see [Languages & Resolvers](languages.md).
- **L2 — graph metrics.** PageRank (centrality) and Louvain (communities),
  recomputed lazily.
- **L3 — descriptions.** On-demand LLM summaries, cached and body-hash invalidated.

Each layer is useful alone; higher layers refine, they never gate.

## The SQLite schema (L0–L3)

Local-first: `.codegraph/graph.db` (SQLite in WAL mode, with FTS5 for search).
The essential tables:

- **`files`** — `path` (relative to root), `language`, `content_hash`, `size`,
  `mtime`, `parse_status` (`ok`/`partial`/`failed`), `indexed_at`. The
  `content_hash` is the anchor of the whole freshness guarantee.
- **`symbols`** — `id` = `hash(path, fqn, kind, ordinal)`, `file_id`, `parent_id`
  (containment), `kind`, `name`, `fqn`, `signature`, `doc`, span, `body_hash`
  (invalidates L3), `visibility`, `rank` (PageRank). Plus a `symbols_fts` FTS5
  index over name/fqn/doc.
- **`edges`** — `kind` (`calls`/`imports`/`inherits`/`implements`/`references`/
  `reads`/`writes`), `src`, `dst` (nullable — `NULL` = dangling), `dst_name`
  (the textual target, **always** filled so an edge can be re-resolved), `file_id`
  (the file where the *reference* occurs — the edge's owner), `line`, `confidence`,
  `resolver` (`l0`/`l1`).
- **`descriptions`** — L3 cache keyed by `(symbol_id, scope)`, with `source_hash`
  for freshness.
- **`meta`** — schema version, repo root, exclusion policy, sweep bookkeeping.

A **unique index** on the resolved-edge shape is a structural guard against the
duplicate-clone bloat that name-based fan-out could otherwise cause; a partial
index on dangling edges makes re-resolution cheap.

## Incremental re-indexing (why it's correct)

The ownership rules are the whole trick:

- **Symbols belong to the file that defines them; edges belong to the file where
  the reference occurs.**
- Re-indexing file `F` is one transaction: `DELETE` everything with `file_id = F`,
  re-parse, re-`INSERT`. **Nothing outside `F` is touched.**
- When a symbol in `F` disappears, edges from *other* files that targeted it become
  dangling (`dst = NULL`) but keep `dst_name` — no silent loss. A re-resolution
  pass relinks danglings after re-index (cheap thanks to the partial index).

These properties are not aspirational — they are locked by the contract-test suite
([`tests/test_contract_invariants.py`](../tests/test_contract_invariants.py)):
reindex-is-idempotent, identity-survives-body-edit, removal-creates-dangling,
reindex-reconnects, and more.

## Freshness, in the code path

1. **Boot scan** recomputes content-hashes (mtime+size fast-path) and re-indexes
   the delta — catching branch switches and offline edits.
2. **Watcher** queues L0 re-index on file events with a debounce.
3. **Read-repair** runs inside every query: verify the hashes of the files in the
   answer, re-parse on drift, then answer. New files are indexed here too.

In the production path (MCP server with the watcher on), the O(files) freshness
sweep is **skipped while a live watcher already guarantees freshness** — a 30s
backstop sweep covers dropped OS events. Without a watcher, the every-miss sweep
runs, so the strong guarantee holds either way. Profiling drove the sweep from
~5s to ~0.6s per missed query at 100k files by building relative paths during
descent and checking against a reduced (dir-only) ignore spec.

## Performance & scale

- **Indexing parallelizes the prepare phase** (read + parse + extract) across a
  small thread pool while keeping the SQLite writer serial — bit-for-bit identical
  to the serial graph. The single writer is the ceiling, so the speedup is modest
  (~1.16×); parse is only ~7% of index time, and we say so.
- **PageRank is lazy** — marked dirty on re-index, recomputed on the next
  `overview`/`impact` with a frequency cap; the graph is held in memory only for
  the computation.
- **Measured to 100k+ files** with a reproducible harness
  ([`evals/scalebench.py`](../evals/scalebench.py)): ~8 min to index at ~324 MB
  peak, no OOM, on the one-time-index + hot-watcher model. Full numbers and the
  two honest ceilings (the freshness sweep; dense C needing active L1) are in
  [evals/RESULTS.md](../evals/RESULTS.md) and [FAQ](faq.md#scale).

## Concurrency

SQLite WAL with a single writer; a re-index queue serializes writes and reads
never block. One `CodeGraph`/`QueryEngine` instance is single-threaded — the MCP
server shares one under a lock; a library host uses one instance per thread. Writes
retry on `database is locked`. Call-graph cycles terminate safely. Both are
covered by regression tests and the CI matrix (Linux + Windows, Python 3.10–3.12).
