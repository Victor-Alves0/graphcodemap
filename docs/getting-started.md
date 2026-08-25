# Getting Started

This guide takes you from an empty shell to answering real structural questions
about your codebase in about five minutes.

## Install

GraphCodeMap needs **Python 3.10+**. The parser is C (via tree-sitter), so the
hot path is not Python.

GraphCodeMap is not published on PyPI yet. Install it from a checkout:

```bash
git clone https://github.com/Victor-Alves0/graphcodemap.git
cd graphcodemap
python -m pip install -e "."
```

Then let GraphCodeMap detect the repository languages, reuse tools already
present and offer an explicit, versioned setup plan:

```bash
codegraph setup --install
```

Nothing is downloaded without `--install` and confirmation. For CI, use
`--install --yes`. Direct downloads have fixed URLs and SHA-256; package-manager
installs use fixed versions. Paths are stored in the user's local GraphCodeMap
configuration, never in the analyzed repository. The shortcut
`python -m pip install -e ".[l1]"` is available for Python/Jedi only.

Optional extras, installed only when you need them:

```bash
python -m pip install -e ".[mcp]"   # MCP server for AI agents
python -m pip install -e ".[dev]"   # test tooling (pytest)
```

The distribution is `graphcodemap`; the importable Python package is `codegraph`
(like `pillow` → `PIL`). The CLI is available as both `graphcodemap` and the
shorter alias `codegraph` — they are the same program.

## Build your first index

From the root of any repository:

```bash
codegraph index . --install       # setup + index + L1 in one explicit command
# or, after setup:
codegraph index . --l1
codegraph doctor
```

This parses every source file into `.codegraph/graph.db` (a local SQLite
database), runs available semantic resolvers and then reports their health.
`doctor` shows the confidence split and an actionable warning for each missing,
timed-out or partial resolver. Add `.codegraph/` to your `.gitignore` — the
index is a derived cache and should never be committed.

For very large monorepos, index only the subtree you care about:

```bash
codegraph index --scope services/payments
```

Scopes are **persisted and additive**: indexing another scope later adds to the
index rather than replacing it, and the freshness sweep then walks only the
indexed subtrees.

## Get oriented

Start with the ranked map of the repo — this is the first thing to run in an
unfamiliar codebase:

```bash
codegraph overview
```

It prints a module tree with the top symbols by PageRank, so the structurally
central code surfaces first.

## Find and inspect a symbol

```bash
codegraph find validate          # fuzzy/exact search, ranked
codegraph info auth.TokenService.validate   # signature, doc, containment, counts
```

Any command that takes a symbol accepts three forms:

- a fully-qualified name — `auth.TokenService.validate` (preferred, unambiguous)
- a bare name — `validate` (disambiguated interactively if it collides)
- a location — `src/auth.py:42`

## Ask the questions that matter

```bash
codegraph callers auth.TokenService.validate   # who calls it
codegraph callees auth.TokenService.validate   # what it calls
codegraph impact  auth.TokenService.validate   # everything that could break
```

Each call edge is labeled with its confidence — `certain`, `inferred`, or
`possible`. In `impact`, the confidence of a multi-hop path is the *minimum* of
the edges along it, so a path is never reported as more certain than its weakest
link. See [Core Concepts](concepts.md#confidence) for what the tiers mean.

## Trace data and find security issues

```bash
codegraph dataflow handle_request              # where each parameter's data goes
codegraph dataflow-build                       # persist Java/Python value flow
codegraph flow-path handler.request save.value # query a reusable value path
codegraph taint --entry handle_request         # untrusted input → dangerous sink
codegraph reaches handle_request --sink http   # does a path reach an HTTP call?
```

Java/Python value paths are persisted and incrementally refreshed; the richer
dataflow/taint compatibility engine remains on demand. Treat taint findings as
*candidates to verify* — the analysis
over-approximates by design. See [Concepts](concepts.md#dataflow--taint).

## See it

```bash
codegraph visualize --mode impact --symbol validate_token
```

This writes a self-contained, interactive HTML file to `.codegraph/graph.html`:
a **seeded subgraph** (not the whole repo), with directional arrows, edges styled
by confidence, and in-page toggles. Other modes: `neighborhood`, `callers`,
`callees`, `domains`. See [CLI › visualize](cli.md#visualize).

## Keep it fresh (and honest)

You never have to remember to re-index. Freshness is layered:

1. a **file watcher** keeps the index hot during a session (`codegraph watch`);
2. a **boot scan** catches anything that changed while the watcher was off;
3. the final guarantee — **every query verifies content-hashes** of the files in
   its answer and re-indexes them before responding (read-repair).

The practical result: you cannot get a stale answer without an explicit `⚠`
warning. Check index health at any time:

```bash
codegraph doctor        # parse status, confidence split, active L1 resolvers, staleness
codegraph stats         # counts
```

## Wire it into an AI agent

```bash
codegraph mcp --install
```

This prepares bounded MCP 1.x and semantic tooling for the detected repository
languages before starting stdio. Plain `codegraph mcp` never installs anything
and reports exact remediation when MCP is missing or incompatible.

Then register the stdio server with your agent. For Claude Code, add a
`.mcp.json` at the repo root:

```jsonc
{ "mcpServers": {
    "codegraph": { "command": "graphcodemap-mcp", "args": ["--root", "."] }
} }
```

The agent now has 25 graph tools, each returning a structured freshness/
completeness envelope. Full guide: [Agents & MCP](mcp.md).

## Next steps

- Understand the model → **[Core Concepts](concepts.md)**
- Every command and flag → **[CLI Reference](cli.md)**
- Enable `certain` edges for your language → **[Languages & Resolvers](languages.md)**
- Embed it in a service → **[Library / Host API](library.md)**
