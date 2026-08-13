<div align="center">

# GraphCodeMap

**A code-to-graph engine that AI agents can actually trust.**

Symbols, call graph, references, impact, dataflow and taint over any codebase —
*local-first, staleness-aware, model-agnostic.*

[![CI](https://github.com/Victor-Alves0/graphcodemap/actions/workflows/tests.yml/badge.svg)](https://github.com/Victor-Alves0/graphcodemap/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1373%20passing-brightgreen)](tests/)
[![Status](https://img.shields.io/badge/status-alpha%20v0.1-orange)](#status)

[Quick start](#quick-start) ·
[Why it exists](#why-it-exists) ·
[Documentation](docs/README.md) ·
[When to use it](#when-to-use-it-and-when-not-to) ·
[Benchmarks](evals/RESULTS.md)

</div>

---

## The problem

An AI coding agent asked *"what breaks if I change this function?"* has two bad
options: read dozens of files (slow, expensive, easy to miss a caller) or trust
an index that was built at some point in the past and may already be wrong. In a
live editing session — the moment the answer matters most — most code indexes are
**stale**, and a stale graph answers *confidently and incorrectly*. That single
failure mode is the number-one risk in this space.

## The approach

GraphCodeMap parses your repository with [tree-sitter](https://tree-sitter.github.io/)
into a SQLite-backed graph and exposes it as focused tools — a **library**, a
**CLI**, and an **MCP server** any agent can call. It is built around one
invariant:

> **The code is the source of truth; the graph is a derived cache.**

Two consequences make it trustworthy where other indexes are not:

- **Every fact is fresh at answer time.** Each row carries the content-hash of
  the file it came from, and every query verifies those hashes against disk
  *before* answering — re-indexing on the spot if the file drifted
  (**read-repair**). You cannot get a stale answer without an explicit warning.
- **Every fact declares its confidence.** Call edges are labeled `certain`,
  `inferred`, or `possible`. Static-analysis limits are stated, never hidden.
  Transitive queries propagate the *minimum* confidence along the path.

This is **epistemic honesty** as an engineering principle — and it is the whole
point. An agent that can trust a `certain` answer stops re-verifying by reading
files, which is where the graph turns into both a correctness win and a token
win.

## Quick start

```bash
pip install graphcodemap

codegraph index .                 # build .codegraph/graph.db
codegraph overview                # ranked map of the repo (PageRank)
codegraph find validate_token     # locate symbols
codegraph impact auth.TokenService.validate   # what breaks if I change this?
codegraph callers auth.TokenService.validate  # who calls it (with confidence)
codegraph taint --entry handle_request         # untrusted input → dangerous sink
codegraph visualize --mode impact --symbol validate_token   # investigate as HTML
```

Point any MCP-capable agent at your repo:

```bash
pip install "graphcodemap[mcp]"
graphcodemap-mcp --root .         # stdio server; indexes/refreshes on boot
```

```jsonc
// .mcp.json at the repo root (Claude Code, Cursor, Codex…)
{ "mcpServers": {
    "codegraph": { "command": "graphcodemap-mcp", "args": ["--root", "."] }
} }
```

Or embed it as a library — the importable package is `codegraph` (like `pillow`→`PIL`):

```python
from codegraph import CodeGraph

cg = CodeGraph(".")
cg.index()
rows, env = cg.find_symbol("validate")
```

New here? Start with **[Getting Started](docs/getting-started.md)**.

## What you can ask

| Question | Tool |
|---|---|
| *Where does this repo begin?* | `overview` — ranked map by PageRank |
| *Where is this symbol?* | `find`, `info` |
| *Who calls this / what does it call?* | `callers`, `callees` |
| *What breaks if I change this?* | `impact`, `change-impact`, `affected-modules` |
| *Which tests cover this?* | `related-tests` |
| *Where does untrusted input flow?* | `dataflow`, `taint`, `reaches` |
| *What are the subsystems here?* | `communities` |
| *What should I read for this task?* | `suggest`, `explain` |
| *Show me the neighborhood* | `visualize` — seeded, interactive HTML |

Full reference: **[CLI](docs/cli.md)** · **[MCP tools](docs/mcp.md)** · **[Library API](docs/library.md)**.

## Highlights

- **Read-repair freshness guarantee.** A watcher keeps the index hot, a boot scan
  catches offline changes, and every query verifies content-hashes before
  answering. Measured to **100k+ files**. → [Concepts](docs/concepts.md#freshness)
- **Confidence-typed edges.** `certain` / `inferred` / `possible`, with `certain`
  coming from real semantic resolution (L1). → [Concepts](docs/concepts.md#confidence)
- **Impact & change analysis.** Transitive reverse-reachability, ranked by
  PageRank × path-confidence; feed it a git diff to ask *"what does my branch
  break?"* → [CLI](docs/cli.md#impact)
- **Dataflow & taint (CPG-lite).** Source→sink reachability with sanitizers,
  interprocedural and computed on demand. **Flow-sensitive** in 18 of the 19
  dedicated code languages: a redefinition kills the taint, so
  `x = input(); x = escape(x); sink(x)` is correctly reported clean.
  → [Concepts](docs/concepts.md#dataflow--taint)
- **Semantic L1 via LSP.** Promotes edges to `certain` through one generic LSP
  client; every dedicated language has a resolver wired.
  → [Languages & Resolvers](docs/languages.md)
- **Agent-oriented MCP layer.** 20 tools returning a structured freshness/
  completeness envelope, plus high-level tools (`change_impact`,
  `find_related_tests`, `explain_symbol`…). → [MCP](docs/mcp.md)
- **Investigative visualization.** Seeded subgraphs (neighborhood/callers/
  callees/impact/domains) with confidence-styled edges and git-diff highlighting
  — not a decorative hairball. → [CLI](docs/cli.md#visualize)

## When to use it (and when not to)

Trust is built by being honest about the boundaries:

- ✅ **Use it for structural questions** — impact, multi-hop call chains,
  dataflow/taint — and in large or unfamiliar codebases, where reading files by
  hand is slow and error-prone.
- ✅ **Use it when an agent must be *sure*** — a `certain` L1 edge is a semantic
  fact, so the agent can answer and stop instead of re-reading.
- ⚠️ **Reach for grep first when you just want to *find* a string.** For plain
  text search grep is often enough and cheaper; the graph earns its cost on
  structure, not substring matching.
- ⚠️ **Treat dataflow/taint findings as candidates.** It is *may-taint* — it
  over-approximates on purpose, so a finding is a lead to verify, not a verdict.
  Measured on the OWASP Benchmark: **64% precision, 31% recall** (see
  [evals/RESULTS.md](evals/RESULTS.md)). That is not competitive with a mature
  SAST, and we publish it rather than claim otherwise.

Full, quantified limitations and benchmark methodology: **[FAQ & Limitations](docs/faq.md)**
and **[evals/RESULTS.md](evals/RESULTS.md)**.

## Languages

**23 dedicated extractors** (refined fqn / imports / calls / inheritance):
Python, TypeScript/TSX, JavaScript, Rust, Go, Java, Kotlin, C#, C, C++/CUDA/Metal,
PHP, Ruby, Lua/Luau, Swift, Scala, Clojure/ClojureScript, **Terraform/HCL**, and
the web tier HTML + CSS/SCSS. A **generic tier** gives structural L0 to dozens
more grammars (Zig, Elixir, Vue, Svelte, SQL, Bash, Dart…). Dataflow & taint
cover all 18 dedicated *code* languages. → **[Languages & Resolvers](docs/languages.md)**

## Documentation

| Guide | What's inside |
|---|---|
| **[Getting Started](docs/getting-started.md)** | Install, index, your first queries |
| **[Core Concepts](docs/concepts.md)** | The graph model, confidence tiers, the freshness guarantee, layers L0–L3 |
| **[CLI Reference](docs/cli.md)** | Every command, flag, and output format |
| **[Agents & MCP](docs/mcp.md)** | The 20 MCP tools and the response envelope |
| **[Library / Host API](docs/library.md)** | Embedding GraphCodeMap in a service |
| **[Languages & Resolvers](docs/languages.md)** | Language tiers and L1/LSP resolution |
| **[Architecture](docs/architecture.md)** | Pipeline, SQLite schema, incremental indexing |
| **[FAQ & Limitations](docs/faq.md)** | Honest answers, benchmarks, scope |
| [Design](docs/DESIGN.md) · [Research](docs/RESEARCH.md) | Original design contract and research notes |

## Status

**Alpha (v0.1.0).** The core is feature-complete and covered by **1373 tests**
(including a contract-test suite that locks the graph's ten load-bearing
invariants), across a CI matrix of Linux + Windows on Python 3.10–3.12. It has
not yet been battle-tested by broad real-world usage — expect rough edges, and
please [open an issue](https://github.com/Victor-Alves0/graphcodemap/issues).

## Contributing

Issues and pull requests are welcome — see **[CONTRIBUTING.md](CONTRIBUTING.md)**.
Adding a language resolver is often a ~10-line config.

## License

[MIT](LICENSE) © Victor Alves

Parts of the taint rule catalog are derived from MIT-licensed data published by
other projects (GitHub CodeQL's `*.model.yml` models and OpenTaint's `rules/`).
See [NOTICE](NOTICE.md) for the required copyright notices and for exactly what
was — and was not — used.
