# Agents & MCP

GraphCodeMap is built to be an agent's structural sense of a codebase. It speaks
[MCP](https://modelcontextprotocol.io/) (Model Context Protocol), so any
MCP-capable agent — Claude Code, Cursor, Codex, and others — can call it.

## Run the server

```bash
git clone https://github.com/Victor-Alves0/graphcodemap.git
cd graphcodemap
python -m pip install -e ".[mcp]"
graphcodemap-mcp --root /path/to/repo
```

Or start from the core install and prepare MCP plus the repository's semantic
toolchains explicitly:

```bash
codegraph mcp --install          # asks before installing
# CI/non-interactive:
codegraph mcp --install --yes
```

Setup pins MCP to the tested 1.x FastMCP-compatible release. Plain
`codegraph mcp` performs no installation and prints an actionable command if
the optional dependency is missing or incompatible.

The server speaks stdio, indexes/refreshes on boot, and runs a background watcher
so the index stays hot. Pass `--no-watch` to disable the watcher. Read-repair
checks exact content hashes; size and mtime are only discovery/performance hints.

### Register it

**Claude Code** — add a `.mcp.json` at the repo root:

```jsonc
{
  "mcpServers": {
    "codegraph": { "command": "graphcodemap-mcp", "args": ["--root", "."] }
  }
}
```

Any other MCP client works the same way: run `graphcodemap-mcp --root <repo>` as a
stdio server.

## The response envelope

This is what makes the graph *trustworthy* to an agent. Alongside the compact
display text, every tool returns a **stable structured envelope** the agent can
consume without parsing prose:

```jsonc
{
  "text": "...",              // the compact human-readable render
  "results": [ /* rows */ ],  // structured, machine-navigable result rows
  "confidence": "certain",    // aggregate over the result's edges (or "mixed"/"n/a")
  "fresh": true,              // the index matched disk at answer time
  "semantic_status": "complete", // not_started/running/complete/partial
  "completeness": {
    "static_analysis": true,          // answer comes from static analysis
    "unresolved_edges": 0,            // relevant edges not yet resolved
    "dynamic_dispatch_possible": false // dynamic/reflective calls may be missing
  },
  "truncated": false,
  "warnings": [ /* the same ⚠ facts, as strings */ ]
}
```

These are not decorative. They are the machine-readable form of the exact same
facts the CLI prints as `⚠` lines, computed at the same point:

- **`fresh`** is only `true` when every file in the answer still matches disk.
- **`confidence`** aggregates the edges: a single label when they agree, `"mixed"`
  when they don't, `"n/a"` when there are no confidence-bearing edges.
- **`semantic_status`** identifies the L1 snapshot. During `running`, queries
  continue to read the previous published snapshot; a fatal attempt reports
  `partial` without exposing its candidate edges.
- **`completeness`** tells the agent when to keep looking — e.g. unresolved edges
  or the possibility of dynamic dispatch.

The design intent: an agent reads `confidence: "certain"` and `fresh: true` and
can **stop** — no re-reading files to double-check. That is where the graph turns
into both a correctness win and a token win.

## The 26 tools

### Core navigation

| Tool | Answers |
|---|---|
| `overview` | A ranked map of the repo (PageRank). Call this first. |
| `find_symbol` | Locate a symbol by name/fqn. |
| `symbol_info` | The card for one symbol: signature, doc, containment, counts. |
| `references` | All uses of a symbol, with confidence. |
| `ego_graph` | The immediate typed neighborhood of a symbol. |
| `repository_tree` | Physical folders/files, exact hashes, language and index state. |
| `graph_history` | Git-aware repository snapshots and per-stage graph versions. |
| `semantic_coverage` | Per-callsite L1 coverage, L0 fallback and unresolved reasons. |

### Call graph & impact

| Tool | Answers |
|---|---|
| `callers` / `callees` | Who calls this / what it calls, with confidence. |
| `impact` | What could break if I change this (ranked, min-confidence paths). |
| `change_impact` | Impact seeded by a change (paths or a diff). |
| `find_affected_modules` | The modules a change affects (coarser than `impact`). |
| `find_related_tests` | The tests that exercise a symbol. |

### Dataflow & security

| Tool | Answers |
|---|---|
| `dataflow` | Where each parameter's data flows. |
| `build_dataflow` | Atomically materialize/reuse Java/Python `flows_to`. |
| `flow_path` | Query persisted value reachability between stable nodes. |
| `path_traversal` | Entry parameters → path/file APIs over persisted `flows_to`; candidate or unknown. |
| `taint` | Untrusted input → dangerous sink (with sanitizers). |
| `reaches` | Does a path from an entry point reach a sink? (chain + verdict) |

### Understanding

| Tool | Answers |
|---|---|
| `communities` | The repo's subsystems/domains. |
| `explain_symbol` | A rich, no-LLM fact sheet for a symbol. |
| `suggest_files_to_read` | Given a task, the files most worth reading. |
| `describe` | An LLM behavior summary (L3), cached and body-hash invalidated. |

### Status

| Tool | Answers |
|---|---|
| `index_status` | Freshness/coverage of the index. |
| `doctor` | Index health: parse status, confidence split, active resolvers, staleness. |

## A suggested agent flow

1. **Orient** with `overview` (and `communities` for the subsystem map).
2. **Locate** the relevant symbol with `find_symbol` / `suggest_files_to_read`.
3. **Understand** it with `symbol_info` / `explain_symbol` / `ego_graph`.
4. **Reason about change** with `impact` / `change_impact` / `find_related_tests`
   — and trust `certain` + `fresh` answers instead of re-reading.
5. **Check security-sensitive flows** with `path_traversal` for entry-scoped
   CWE-22 evidence, or `taint` / `reaches` for the broader compatibility engine.

## Notes

- The server serializes access with a lock, so a single instance is safe to share
  across an agent's concurrent tool calls.
- L3 (`describe`) needs an LLM provider; without one the tool still exists and
  reports that L3 is disabled — nothing else depends on it. See
  [Library › L3 provider](library.md#l3-provider).
