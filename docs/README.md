# GraphCodeMap Documentation

Everything you need to index a codebase, query its structure, and wire it into an
AI agent — organized from first run to internals.

> New to the project? Read the [one-page overview](../README.md) first, then come
> back here.

## Start here

- **[Getting Started](getting-started.md)** — install, build your first index,
  run your first queries in five minutes.
- **[Core Concepts](concepts.md)** — the mental model: what a symbol and an edge
  are, the three confidence tiers, the freshness guarantee, and the L0–L3 layers.
  Read this once and everything else clicks.

## Reference

- **[CLI Reference](cli.md)** — every subcommand, its flags, and the exact output
  format it prints.
- **[Agents & MCP](mcp.md)** — the 20 MCP tools, how to register the server, and
  the freshness/completeness envelope every answer carries.
- **[Library / Host API](library.md)** — embedding GraphCodeMap in a service:
  change detection, per-call LLM credentials, exclusion policy, multi-tenant
  safety.

## Understanding the engine

- **[Languages & Resolvers](languages.md)** — the three language tiers and how L1
  (LSP) resolution promotes edges to `certain`.
- **[Architecture](architecture.md)** — the indexing pipeline, the SQLite schema,
  incremental re-indexing, and why the invariants hold.
- **[FAQ & Limitations](faq.md)** — honest, quantified answers about scope,
  accuracy, scale, and benchmarks.
- **[Roadmap](ROADMAP.md)** — where the taint engine stands against published
  SAST scorecards, what is being worked on next and in which order, and the
  approaches that were built, measured and *rejected* (so they are not retried).

## Design history

- **[DESIGN.md](DESIGN.md)** — the original design contract (schema + tool
  contract). Written before implementation; still the reference for *why* the
  data model looks the way it does.
- **[RESEARCH.md](RESEARCH.md)** — the research notes and prior art that shaped
  the design.
- **[COMPARISON.md](COMPARISON.md)** — an early competitive analysis.

> These three are historical design documents (partly in Portuguese) and may lag
> the current implementation. The guides above are the source of truth for how
> the system behaves today.

## Conventions used in these docs

- The CLI is invoked as `codegraph` throughout; `graphcodemap` is the same
  binary under its full name.
- A **symbol reference** in any command accepts a fully-qualified name
  (`auth.TokenService.validate`), a bare name (disambiguated if needed), or a
  `path:line` location.
- Command output examples show the **envelope** — the `⚠` lines that appear only
  when there is something to declare (drift, incompleteness, truncation). Silence
  means *fresh and within known limits*.
