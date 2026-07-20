# GraphCodeMap

Code-to-graph engine for AI coding agents: symbols, call graph, references and impact analysis over any codebase — **local-first, staleness-aware, model-agnostic**.

GraphCodeMap parses your repository with tree-sitter into a SQLite-backed graph and exposes it as focused tools (library, CLI, and soon MCP). It is designed around one invariant: **the code is the source of truth; the graph is a derived cache** — every fact carries the content-hash of the file it came from, and every query verifies freshness before answering (read-repair). Static-analysis limits are declared, never hidden: call edges carry a confidence level (`certain` / `inferred` / `possible`).

> Design rationale and research: [docs/RESEARCH.md](docs/RESEARCH.md) · [docs/DESIGN.md](docs/DESIGN.md)

## Quick start

```bash
pip install graphcodemap

codegraph index .                 # build .codegraph/graph.db
codegraph overview                # ranked map of the repo (PageRank)
codegraph find validate_token     # locate symbols
codegraph callers auth.TokenService.validate
codegraph impact auth.TokenService.validate   # what breaks if I change this
codegraph dataflow handle_request             # where each parameter's data flows
codegraph taint --entry handle_request        # untrusted input -> dangerous sink (security)
codegraph ego auth.TokenService               # immediate graph neighborhood
codegraph communities                         # domains/subsystems (graph clustering)
codegraph describe domain:0                   # L3: name a domain (LLM, cached)
codegraph visualize                           # interactive HTML map (.codegraph/graph.html)
codegraph watch                               # keep the index hot (file watcher)
codegraph refine                              # L1: promote edges to [certain] (jedi)
codegraph describe auth.TokenService.validate # L3: LLM behavior summary (cached)
codegraph describe src/auth.py                # L3: module-level summary
codegraph describe --top 20                   # pre-warm summaries for hub symbols
codegraph stats
codegraph doctor                              # index health: parse, confidence, L1, staleness
codegraph vacuum                              # rebuild + reclaim space (keeps L3 cache)
```

### Observability

`doctor` gives a one-shot health check: parse status (and the paths of any files
that failed to parse), the call-edge confidence split (`certain`/`inferred`/
`possible`) with `%certain`, which L1 resolvers are active, and how stale the
index is. It flags actionable problems and exits non-zero when files failed to
parse (handy in CI). `doctor --why` re-parses the failed files and prints the
reason. Also available as an MCP tool. When the DB looks bloated or stale,
`vacuum` rebuilds it and reclaims space while keeping the L3 cache.

Logging is off by default (a library shouldn't spam your output). Turn it on to
see *why* a file failed to index, resolver activity, or L3 token cost:

```bash
CODEGRAPH_LOG=warning codegraph index .   # warnings (e.g. which file failed and why)
CODEGRAPH_DEBUG=1 codegraph refine        # full debug (LSP activity, token accounting)
```

L3 (`describe`) reports token usage per generation, so the cost is visible.

The `graphcodemap` and `codegraph` commands are the same CLI (the short name
is kept as an alias). Install `graphcodemap[l1]` to enable semantic refinement: a pluggable resolver
layer (Python via jedi today) that runs after L0 and promotes call edges to
`certain` when exactly one in-repo definition is found — including instance
method calls that name-based resolution can only mark `possible`.

Freshness is layered: a file watcher keeps the index hot during a session,
a boot scan catches anything that changed while it was off, and — the final
guarantee — every query verifies content-hashes of the files involved and
re-indexes them before answering (read-repair).

## MCP server (any agent: Claude Code, Cursor, Codex…)

```bash
pip install "graphcodemap[mcp]"
graphcodemap-mcp --root /path/to/repo   # stdio server; indexes/refreshes on boot
```

Claude Code — add to `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "codegraph": { "command": "graphcodemap-mcp", "args": ["--root", "."] }
  }
}
```

Tools exposed: `overview`, `find_symbol`, `symbol_info`, `references`,
`callers`, `callees`, `impact`, `ego_graph`, `dataflow`, `taint`, `communities`,
`describe`, `index_status`. Every answer carries the freshness/completeness envelope —
edges are labeled `certain`/`inferred`/`possible`, and static-analysis limits
are declared, never hidden.

As a library (the importable package is `codegraph`, like `pillow`→`PIL`):

```python
from codegraph import CodeGraph

cg = CodeGraph(".")
cg.index()
for s in cg.find_symbol("validate"):
    print(s.fqn, s.kind, f"{s.path}:{s.start_line}")
```

## Languages

Three tiers:

- **Dedicated extractors** (refined fqn/imports/calls): Python, TypeScript/TSX, JavaScript, Rust, Go, Java, Kotlin, C#, C, C++/CUDA/Metal, PHP, Ruby, Lua/Luau, Swift, Scala, Clojure/ClojureScript.
- **Generic tier** (structural heuristics over any tree-sitter grammar): Zig, PowerShell, Elixir, Objective-C, Julia, Vue, Svelte, Astro, Groovy/Gradle, Dart, Verilog/SystemVerilog, SQL, Fortran, Pascal/Delphi, Bash, Apex, Razor, XML project files.

Dataflow & taint analysis covers all 18 dedicated languages (Python, JS/TS,
Java, C#, C/C++, Go, Rust, Ruby, PHP, Kotlin, Swift, Scala, Lua, Clojure).
- **Docs/data**: Markdown (headings as sections), JSON/YAML/TOML (top-level keys).

Binary/document formats (.pdf, .docx) and structureless formats (.sln, .dfm, BYOND) stay out of the structural graph by design.

Semantic L1 resolvers (promote edges to `certain`): Python via jedi; JS/TS via the TypeScript language service (needs `node` + `typescript@5` — set `CODEGRAPH_NODE`/`CODEGRAPH_TS_DIR` or have them on PATH); and any LSP server via a generic client — Go via `gopls`, Rust via `rust-analyzer`, C/C++ via `clangd` (all validated). An LSP-backed language activates automatically when its server is on `PATH` (or pointed at by `CODEGRAPH_GOPLS`/`CODEGRAPH_RUST_ANALYZER`/`CODEGRAPH_CLANGD`); resolution quality depends on the server finding the project (`go.mod` / `Cargo.toml` / `compile_commands.json`). The generic client waits for async servers (rust-analyzer/clangd) to finish indexing before querying. Adding another language is a ~10-line config on `l1/lsp_base.py`.

Why L1 matters: `certain` edges are semantic facts, not name guesses — so an agent can trust a `reaches`/`impact`/`callers` answer and stop, instead of re-verifying by reading files. In our reachability benchmark this made the graph arm both more correct and ~2.4× cheaper in tokens than a grep/read baseline (see [evals/RESULTS.md](evals/RESULTS.md#rodada-9)). Adding L1 to a language is what turns "graph is sometimes worth it" into "graph wins" there.

## Dataflow & taint

Beyond the call graph, GraphCodeMap answers *"if data enters here, where does it
go?"* — the foundation for security and safe refactoring. `dataflow` traces each
parameter to the calls and returns it reaches; `taint` follows untrusted input
(sources) to dangerous operations (sinks), with sanitizers cutting the flow.
Interprocedural via the call graph, computed on-demand (always fresh), and
configurable via `.codegraph/taint.json`. It is a pragmatic, incremental
[Code Property Graph](docs/RESEARCH.md#6-dataflow--taint--pesquisa-e-decis%C3%A3o-2026-07-18)
— not a whole-program engine. Covers all 18 dedicated languages.

## Status

**Alpha (v0.1.0).** The core is feature-complete and covered by ~165 tests, but
the project has not yet been battle-tested by real usage. Roadmap M0–M12 done
(see [docs/DESIGN.md](docs/DESIGN.md#7-roadmap)): L0 indexing, read-repair +
watcher, PageRank/impact/ego/overview, MCP server, L1 (Python/jedi, JS/TS/
tsserver), L3 descriptions, community detection, visualization, dataflow/taint.

## Honest limitations & benchmarks

This project's design principle is **epistemic honesty** — so are its claims:

- **The graph complements grep; it does not replace it.** For simply *finding*
  code, grep is often enough and cheaper. The graph earns its cost on
  *structural* questions — impact ("what breaks if I change this"), multi-hop
  call chains, dataflow/taint — and in large or unfamiliar codebases.
- **Benchmarks are small-scale and directional, not proof of SOTA.** On a
  15-task SWE-bench-Lite *localization* pilot, the graph arm found the file to
  edit in 93% of cases vs 80% for a grep/read baseline — but +2 tasks at n=15 is
  within noise, and it measures localization, not full issue resolution. On
  large structural tasks (redis) the graph won on both quality and token cost.
  Full methodology and caveats: [evals/RESULTS.md](evals/RESULTS.md).
- **Reachability isn't a token win by itself — it's an accuracy win.** On a
  3-task flask reachability set (entry→sink chain), the graph arm scored 100%
  correct vs 67% baseline and 0.92 vs 0.58 chain recall, with fewer tool calls
  (14.7 vs 17.3) — but roughly *token parity* (43.6k vs 41.2k avg; one task where
  the graph over-explored dragged the average up). The lesson we keep re-learning:
  the graph buys **correctness and completeness on structural questions**, not a
  universal token discount. Where it also saves tokens is when `certain` L1 edges
  let the agent trust `reaches` and stop.
- **Dataflow/taint is *may-taint*** (over-approximates — findings are candidates
  to verify), flow-insensitive, and does not model field/alias sensitivity yet.
- **L1 semantic resolution** promotes call edges to `certain` via one generic
  LSP client. *Validated* against a live server: Python (jedi), JS/TS (tsserver),
  Go (gopls), Rust (`rust-analyzer`), Lua (`lua-language-server`), Clojure
  (`clojure-lsp`) — four distinct server families proving the generic client
  generalizes. *Wired and inert until the server is on `PATH`* (same protocol,
  not yet validated on a live server here): C/C++ (`clangd`), PHP
  (`intelephense`), Ruby (`solargraph`), Kotlin (`kotlin-language-server`). Each
  activates only when its binary exists; languages without an active resolver
  keep honest `inferred`/`possible` edges. JVM/toolchain-launched servers (jdtls,
  metals, sourcekit-lsp, Roslyn) need a custom launcher and are future work.
- Static analysis can miss dynamic/reflective calls — every answer says so.
- **Concurrency:** one `CodeGraph`/`QueryEngine` instance is single-threaded —
  share it only under external serialization (the MCP server does this with a
  lock), or use one instance per thread. Writes retry on `database is locked`.
  Call-graph cycles (mutual/self recursion) terminate safely. Both are covered by
  regression tests and the CI matrix (Linux + Windows, Python 3.10–3.12).
- **Scale:** measured up to the low tens of thousands of files. Indexing is
  single-threaded (a one-time cost; the watcher keeps it warm after) and the
  freshness sweep is O(files) but cheap via `os.scandir` (~250ms for a missed
  lookup at 8k files, strong-consistency preserved). Not yet validated on 100k+
  monorepos; parallel indexing and lazy/partial indexing are future work.

Configuration: set `OPENROUTER_API_KEY` (env or `.env`) to enable L3/eval;
model via `CODEGRAPH_L3_MODEL`. Contributions and issue reports welcome.
