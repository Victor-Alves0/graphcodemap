# Changelog

All notable changes to this project are documented here. Format loosely based
on [Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Clojure/ClojureScript** dedicated extractor + dataflow (18th dedicated
  language) — added for real-world security auditing of Clojure backends.
- **`reaches`** tool/CLI/MCP: reachability from an entry to a sink (preset
  `http`/`sql`/`exec`/`file` or regex) in a single answer — the call chain plus
  whether a given validator/sanitizer appears on the path. Lets an agent get the
  structural answer without assembling the traversal hop-by-hop.
- **Confidence surfaced on `reaches`** with an explicit trust verdict
  (`[certain]` = semantically resolved, trust without re-reading); MCP
  instructions now tell agents to stop re-verifying `certain` results.
- **Generic LSP-backed L1** (`l1/lsp_base.py`): one stdio LSP client drives any
  language server, waiting for async servers to finish indexing before querying
  and aiming the query at the callee's last name segment. Validated against a
  live server for Go (`gopls`, 0→4705 `certain` edges on gin) and Rust
  (`rust-analyzer`). Adding a language is now a ~10-line config (`languages`,
  `language_id`, `cmd_name`).
- **Six more L1 resolvers wired**, lifting `certain` coverage from 4 to 10 of the
  18 dedicated languages. **Validated live** (portable server, cross-file/-namespace
  promotion to `certain`): Lua (`lua-language-server`) and Clojure (`clojure-lsp`)
  — a 5th and 6th server family after gopls/rust-analyzer, proving the generic
  client generalizes. **Wired, inert until on `PATH`** (not yet validated live):
  C/C++ (`clangd`), PHP (`intelephense`), Ruby (`solargraph`), Kotlin
  (`kotlin-language-server`). Opt-in tests (`tests/test_l1_extra.py`) validate each
  once its binary is installed.
- **Reachability eval harness** (`evals/reachbench.py`) with an honest write-up
  of when the graph beats grep and when it doesn't (`evals/RESULTS.md`).
- **Observability layer** (`codegraph/log.py`): opt-in stdlib logging, silent by
  default, enabled via `CODEGRAPH_LOG=debug|info|warning` or `CODEGRAPH_DEBUG=1`
  (output to stderr). Wired into the previously-silent failure points — per-file
  index failures now log which file and why, L1 resolver failures are counted and
  logged, watcher errors are surfaced.
- **`doctor`** command (CLI + MCP): index health in one shot — parse status
  (ok/failed files, with the failed paths listed), call-edge confidence
  distribution (`certain`/`inferred`/`possible`) and `%certain`, active L1
  resolvers, and staleness (age of the last full scan). Flags actionable problems
  (files that failed to parse, no L1 resolver, low `%certain`). Non-zero exit when
  files failed to parse, for CI use. `doctor --why` re-parses the failed files
  and prints the actual exception per file.
- **`vacuum`** command: rebuilds the index from scratch (`index --force`), drops
  orphan L3 descriptions and runs `VACUUM` to reclaim space, preserving the L3
  descriptions of live symbols (stable ids). Recovers a bloated DB without losing
  cached work.
- **L3 cost visibility**: the OpenRouter provider now accounts token usage
  (`prompt`/`completion`/`total`, per-call and accumulated); `describe` reports
  the cost of a generation, and `describe --top N` reports the accumulated cost.

### Changed

- **Hardened the LSP client** (`l1/lsp_base.py`): a dedicated reader thread plus a
  bounded queue give every read an I/O timeout (no `select()` on pipes on Windows).
  A hung language server (accepts `didOpen` but never answers) is now killed after
  `io_timeout` instead of freezing the whole L1 pass; broken stdin and malformed
  framing are handled gracefully.

### Performance

- **Indexing restructured (batched writes + in-memory edge resolution).** The
  full-scan (`index_repo`) commits in batches (a per-file `SAVEPOINT` preserves
  error isolation) and inserts symbols/FTS/edges with `executemany`; the write
  path was split into `_prepare` (parse, no DB) + `_write_parsed` (batched
  writes), leaving the incremental path (watcher/read-repair) per-file
  transactional. `resolve_edges` now loads symbols into an in-memory index once
  (a dict) instead of running one SQL lookup per dangling edge — algorithmically
  better on large repos where the per-guess cache degrades (guesses are nearly
  unique). **Honesty on speed:** on the test hardware the wall-clock index time
  did **not** reliably improve — run-to-run variance (~±40%, thermal/load) dwarfs
  the change, and `cProfile` shows the cost is SQLite write throughput
  (`executemany` ~54%), which is inherently serial. The measured takeaway that
  *stuck*: parsing is only ~4% of index time, so **parallelizing the parser would
  not help** — the original goal was chasing the wrong bottleneck. These changes
  are kept for the cleaner structure and better resolution algorithm; all 176
  tests pass. Genuinely faster indexing would need a different storage strategy
  (out of scope).
- **Freshness sweep made scale-safe (the query-path landmine).** On an empty
  search result, read-repair re-checks every indexed file for staleness — but it
  did one `stat()` per file, so a single missed lookup cost O(N): ~2.7s on 8k
  files, ~34s projected on 100k, *on every miss* (agents miss constantly — typos,
  exploration). It now reads size/mtime via `os.scandir` (`scan_source_stats`),
  where the directory read carries the metadata with no per-file syscall — ~60x
  faster (21ms vs 1.3s for 8k stats), bringing a missed lookup to ~250ms on 8k.
  Fast enough to keep running on *every* empty result, so the strong
  anti-staleness guarantee is preserved at scale (no throttling, no weakened
  freshness). Covered by `tests/test_freshness.py`.

### Fixed

- **Concurrency safety.** Two failure modes were found by stress testing and
  fixed: (1) a single SQLite connection shared across threads corrupted state
  (`bad parameter or other API misuse`, `another row available`) — the MCP server
  now serializes every tool call on its engine connection with a lock (queries are
  ms-scale; read-repair writes need mutual exclusion anyway); (2) concurrent
  writers on separate connections (watcher + query read-repair) hit
  `database is locked` / `SQLITE_BUSY_SNAPSHOT`, which `busy_timeout` does not
  cover — the index write path (`index_file`/`remove_file`) now retries on lock
  with exponential backoff, and `busy_timeout` was raised to 10s. Both models now
  run a multi-threaded stress test with zero errors
  (`tests/test_concurrency.py`). Threading contract: one `CodeGraph`/`QueryEngine`
  instance is single-threaded — share it only under external serialization (as the
  MCP server does), or use one instance per thread.
- **Call-graph cycles** (`a→b→a`, self-recursion) are covered by a regression test
  (`tests/test_cycles.py`): every traversal (callers/callees/impact/reaches/
  dataflow) already guards with a visited set + depth bound, so cycles terminate
  without looping or blowing up — now locked in.
- **Edge accumulation / DB bloat.** A unique index on resolved edges
  (`kind, src, dst, dst_name, file_id, line, col`) plus `INSERT OR IGNORE` on
  ambiguous fan-out and de-duplication of identical refs at index time make the
  graph *idempotent by construction*: re-indexing can never duplicate an edge.
  On a real repo this turned a pathological `.codegraph` (4.6M call edges, 11-min
  index — all accumulated stale clones) back into ~6k edges and a 9s index, with
  no loss of recall (distinct candidates are still kept). `INDEXER_VERSION` bumped
  to 14 (forces a one-time clean rebuild). `doctor` surfaces the symptom (millions
  of edges / near-0 `%certain`); `vacuum` reclaims the space.
- **Ambiguous-call rendering.** `callers`/`callees` now aggregate the by-name
  candidates of a call site onto one line instead of one line per candidate —
  fewer tokens, same information.

## [0.1.0] — 2026-07-18

First public release. Alpha: the core is feature-complete and covered by tests,
but the project has not yet been battle-tested by real-world usage.

### Added

- **L0 structural graph** via tree-sitter → SQLite: symbols, call graph,
  imports, inheritance. 18 dedicated-extractor languages (Python, TypeScript/
  TSX, JavaScript, Rust, Go, Java, Kotlin, C#, C, C++/CUDA/Metal, PHP, Ruby,
  Lua/Luau, Swift, Scala, Clojure/ClojureScript) plus a generic heuristic tier
  for any tree-sitter grammar, and Markdown/JSON/YAML/TOML as data.
- **Anti-staleness by construction**: boot scan, file watcher, and query-time
  read-repair (every answer verifies content-hashes and re-indexes before
  responding). Edge confidence is labeled `certain` / `inferred` / `possible`,
  and static-analysis limits are declared, never hidden.
- **L1 semantic resolution**: Python via jedi (in-process); JS/TS via the
  TypeScript language service (optional, needs `node` + `typescript@5`); Go via
  `gopls` over LSP (optional, needs `gopls` + Go toolchain).
- **Graph queries**: `find_symbol`, `references`, `callers`, `callees`,
  `impact` (transitive dependents), `ego_graph`, `overview` (PageRank).
- **Domains**: community detection (hand-rolled Louvain) + optional LLM labels.
- **Dataflow & taint** (CPG-lite): intra-procedural may-taint composed along
  the call graph, on-demand (always fresh). `dataflow` (where each parameter
  flows) and `taint` (source→sink with sanitizers, configurable via
  `.codegraph/taint.json`). Covers all 18 dedicated languages.
- **L3 descriptions**: LLM behavior summaries, cached per code-hash, served
  `STALE`-flagged when the code changed. Provider-agnostic (OpenRouter).
- **Visualization**: `visualize` exports a self-contained interactive HTML
  graph (or JSON), colored by domain.
- **Interfaces**: Python library, CLI (`graphcodemap` / `codegraph`), and an
  MCP server (`graphcodemap-mcp`) for any agent (Claude Code, Cursor, Codex…).
- **Evaluation harness**: baseline (grep/read) vs +graph, and a SWE-bench-Lite
  localization benchmark (`evals/locbench.py`).

### Known limitations

- Dataflow/taint is *may-taint* (over-approximates — findings are candidates
  to verify), flow-insensitive; no field/alias sensitivity yet.
- Dataflow does not yet cover Dart/Elixir (grammar irregularities).
- L1 type resolution exists only for Python and JS/TS; other languages keep
  `inferred`/`possible` edges.
- Benchmarked at small scale (see `evals/RESULTS.md`) — directional evidence,
  not a proven SOTA claim.
- Developed on Windows; not yet CI-tested on Linux/macOS.
