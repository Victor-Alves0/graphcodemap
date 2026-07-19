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
  and aiming the query at the callee's last name segment. Validated for Go
  (`gopls`, 0→4705 `certain` edges on gin), Rust (`rust-analyzer`) and C/C++
  (`clangd`) — each promoting a cross-file call to `certain`. Adding a language
  is now a ~10-line config (`languages`, `language_id`, `cmd_name`).
- **Security/reachability eval harnesses** (`evals/secbench*.py`,
  `evals/sectrace.py`, `evals/reachbench.py`) with an honest write-up of when the
  graph beats grep and when it doesn't (`evals/RESULTS.md`).

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
