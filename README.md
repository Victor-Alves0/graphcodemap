<div align="center">

# GraphCodeMap

**A live Java/Python code map for developers and AI agents.**

Symbols, call graph, references, impact, dataflow and taint with explicit
coverage and uncertainty — *local-first, staleness-aware, model-agnostic.*

[![CI](https://github.com/Victor-Alves0/graphcodemap/actions/workflows/tests.yml/badge.svg)](https://github.com/Victor-Alves0/graphcodemap/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha%20v0.1-orange)](#status)

[Quick start](#quick-start) ·
[Why it exists](#the-problem) ·
[Product contract](docs/PRODUCT_CONTRACT.md) ·
[Documentation](docs/README.md) ·
[When to use it](#when-to-use-it-and-when-not-to) ·
[Benchmarks](evals/RESULTS.md) ·
[Maturity](docs/MATURITY.md) ·
[Roadmap](docs/ROADMAP.md)

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

- **Freshness is checked at answer time.** Each row carries the exact content
  hash of the file it came from. Relevant paths use read-repair, while watcher
  and full-sweep backstops discover new/deleted files. Size and mtime are hints;
  unchanged metadata never substitutes for content verification.
- **Every fact declares its confidence.** Call edges are labeled `certain`,
  `inferred`, or `possible`. Static-analysis limits are stated, never hidden.
  Transitive queries propagate the *minimum* confidence along the path.

This is **epistemic honesty** as an engineering principle — and it is the whole
point. An agent that can trust a `certain` answer stops re-verifying by reading
files, which is where the graph turns into both a correctness win and a token
win.

> **Product reset:** Java and Python are the phase-one focus. Other extractors
> remain experimental compatibility surfaces until they pass the same product
> gates. The canonical scope, graph vocabulary, acceptance criteria and current
> gaps are in the [Product Contract](docs/PRODUCT_CONTRACT.md).

## Quick start

```bash
git clone https://github.com/Victor-Alves0/graphcodemap.git
cd graphcodemap
python -m pip install -e ".[mcp,l1]"

codegraph setup --install         # detect languages; show + confirm a pinned plan
codegraph index . --l1            # build the index and promote semantic edges
codegraph doctor                  # verify resolver health and % certain
codegraph overview                # ranked map of the repo (PageRank)
codegraph tree                    # physical folders/files, hashes and index state
codegraph history                 # Git-aware graph and analysis-stage revisions
codegraph semantic-coverage       # certain/fallback/unresolved callsites and why
codegraph find validate_token     # locate symbols
codegraph impact auth.TokenService.validate   # what breaks if I change this?
codegraph callers auth.TokenService.validate  # who calls it (with confidence)
codegraph taint --entry handle_request         # untrusted input → dangerous sink
codegraph visualize --mode impact --symbol validate_token   # investigate as HTML
```

Point any MCP-capable agent at your repo:

```bash
codegraph mcp --install           # prepares MCP + repo languages, then starts
```

The distribution is not published on PyPI yet, so the documented path is an
editable install from this checkout. The core remains useful without L1, but
semantic call edges cannot become `certain` until their resolver is ready.
`codegraph setup` first reuses installed/repo-local tools, then offers an
explicit versioned installation; it never downloads merely because a repo was
opened. `python -m pip install -e ".[l1]"` is the direct Python/Jedi shortcut.
See [Languages & Resolvers](docs/languages.md#activating-a-resolver).

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
| *Where is every file, and is its graph current?* | `tree` |
| *Which repository/analysis revision produced this graph?* | `history` |
| *Which calls are semantically proven, and why not the rest?* | `semantic-coverage` |
| *What should I read for this task?* | `suggest`, `explain` |
| *Show me the neighborhood* | `visualize` — seeded, interactive HTML |

Full reference: **[CLI](docs/cli.md)** · **[MCP tools](docs/mcp.md)** · **[Library API](docs/library.md)** · **[Java analysis contract](docs/JAVA_ANALYSIS_CONTRACT.md)**.

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
  → [Concepts](docs/concepts.md#the-layers-l0l3)
- **Semantic L1 via LSP.** Promotes edges to `certain` through one generic LSP
  client; every dedicated language has a resolver wired.
  → [Languages & Resolvers](docs/languages.md)
- **Agent-oriented MCP layer.** 23 tools returning a structured freshness/
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
  The Round 27 Java profile scores **902/0/0/796** on the pinned
  OWASP matrix and **444/0/0/444** on Juliet CWE-23; all three pinned vulnerable
  revisions are detected and all three fixes clear. A corrected Juliet project
  overlay completed with 732/732 files, 4,408 `certain` promotions and zero
  resolver warnings/errors. Existing versioned
  CodeQL rows remain the fair comparison: OWASP `default` is
  776/292/126/504 and `security-extended` is 902/471/0/325; Juliet is
  222/6/222/438 for both suites. These pinned rows do not establish universal
  superiority over CodeQL's broader languages, queries, framework models and
  operational tooling. See [Security Benchmark](docs/SECURITY_BENCHMARK.md).

Full, quantified limitations and benchmark methodology: **[FAQ & Limitations](docs/faq.md)**
and **[evals/RESULTS.md](evals/RESULTS.md)**. What is being worked on next, and
why in that order: **[docs/ROADMAP.md](docs/ROADMAP.md)**.

## Languages

**Phase-one product languages: Java and Python.** The repository also contains
23 dedicated extractors (refined fqn / imports / calls / inheritance):
Python, TypeScript/TSX, JavaScript, Rust, Go, Java, Kotlin, C#, C, C++/CUDA/Metal,
PHP, Ruby, Lua/Luau, Swift, Scala, Clojure/ClojureScript, **Terraform/HCL**, and
the web tier HTML + CSS/SCSS. These additional languages are experimental until
they pass the same end-to-end gates as Java/Python. A **generic tier** gives
structural L0 to dozens more grammars (Zig, Elixir, Vue, Svelte, SQL, Bash,
Dart…). Implementation presence is not a claim of product parity.
→ **[Languages & Resolvers](docs/languages.md)**

## Documentation

| Guide | What's inside |
|---|---|
| **[Getting Started](docs/getting-started.md)** | Install, index, your first queries |
| **[Product Contract](docs/PRODUCT_CONTRACT.md)** | Canonical scope, required graph and acceptance gates |
| **[Core Concepts](docs/concepts.md)** | The graph model, confidence tiers, the freshness guarantee, layers L0–L3 |
| **[CLI Reference](docs/cli.md)** | Every command, flag, and output format |
| **[Agents & MCP](docs/mcp.md)** | The 23 MCP tools and the response envelope |
| **[Library / Host API](docs/library.md)** | Embedding GraphCodeMap in a service |
| **[Languages & Resolvers](docs/languages.md)** | Language tiers and L1/LSP resolution |
| **[Semantic Linking Matrix](docs/SEMANTIC_LINKING_MATRIX.md)** | Real Jedi/JDTLS call contracts and coverage outcomes |
| **[Product Maturity](docs/MATURITY.md)** | Evidence levels, current gaps and honest parity boundaries |
| **[Language Maturity Playbook](docs/LANGUAGE_MATURITY_PLAYBOOK.md)** | Reusable Java lessons and gates for adapting each next language |
| **[Round 28 Real-world Feedback](docs/REAL_WORLD_FEEDBACK_ROUND28.md)** | Aethros/PetClinic findings and their Round 29 operational closure |
| **[Architecture](docs/architecture.md)** | Pipeline, SQLite schema, incremental indexing |
| **[FAQ & Limitations](docs/faq.md)** | Honest answers, benchmarks, scope |
| [Design](docs/DESIGN.md) · [Research](docs/RESEARCH.md) | Original design contract and research notes |

## Status

**Alpha (v0.1.0), undergoing a product reset.** Java/Python declarations,
parameters, locals, fields/properties and persistent `contains`/`defines`/
`reads`/`writes`/simple `returns` now share one structural model. A separate
physical repository graph records every non-ignored folder/file, exact hashes,
index state and Git-aware graph-stage revisions. L1 lifecycle publication is
atomic and observable; persistent interprocedural `flows_to`, semantic-link
coverage across multiple ordinary repositories and broad real-world onboarding
remain open. Focused real-resolver matrices currently pass 5/5 Python categories
and 7/7 Java categories (including overload and method-reference resolution).
Flask/PetClinic canaries refine 78.8%/96.9% of persisted local call candidates.
Bounded JDTLS pipelining reduced PetClinic warm revalidation from 54.75s to
37.61s, while unchanged `index --l1` runs reuse the snapshot in about 0.10s.
Historical benchmark results are retained
as bounded subsystem evidence, not as a declaration that the product is ready.
See the [Product Contract](docs/PRODUCT_CONTRACT.md) and
[maturity matrix](docs/MATURITY.md).

## Contributing

Issues and pull requests are welcome — see **[CONTRIBUTING.md](CONTRIBUTING.md)**.
Wiring a basic language-server adapter is often a ~10-line config. Promoting a
language profile requires the structural, semantic, operational and evidence
gates in the [maturity playbook](docs/LANGUAGE_MATURITY_PLAYBOOK.md).

## License

[MIT](LICENSE) © Victor Alves

Parts of the taint rule catalog are derived from MIT-licensed data published by
other projects (GitHub CodeQL's `*.model.yml` models and OpenTaint's `rules/`).
See [NOTICE](NOTICE.md) for the required copyright notices and for exactly what
was — and was not — used.
