# Core Concepts

Read this once and the rest of the system follows from it. GraphCodeMap is built
on five design invariants; everything else is a consequence.

## The five invariants

1. **The code is the source of truth; the graph is a derived cache.** Every fact
   carries the provenance (file + content-hash) of where it came from. The whole
   graph is reconstructible from scratch at any time and is never edited directly.
2. **No answer without a freshness check.** Every query verifies the hashes of
   the files involved before answering; a mismatch triggers read-repair.
3. **Epistemic honesty.** Edges carry confidence; call-graph answers declare the
   limits of static analysis. Partial recall is never presented as complete.
4. **Complement, don't replace.** The tools locate and navigate; the agent still
   reads the code. Answers point to spans (`path:line`), they don't dump function
   bodies.
5. **Each layer is useful alone.** L0 (tree-sitter) works on any repo with zero
   configuration; L1 (LSP) and L3 (LLM descriptions) are optional upgrades.

## The data model

The graph has two kinds of nodes-and-relationships, stored in SQLite.

### Symbols

A **symbol** is a named definition: a function, method, class, interface, struct,
enum, variable, constant, module, type alias — or a whole file (`kind='file'`,
used as the target of import/asset edges).

Symbol identity is deliberately stable:

```
symbol_id = hash(path, fqn, kind, discriminator)
```

- `fqn` is the fully-qualified name within the module (`auth.TokenService.validate`).
- callable signatures distinguish overloads without tying identity to sibling
  order; an ordinal remains the fallback for non-callable homonyms and exact
  duplicate signatures.
- **The body and line numbers are *not* part of the identity.** Editing a
  function's body or moving it down the file keeps the same `symbol_id` — so
  cached L3 descriptions and cross-file edges survive a routine edit.
- Moving a symbol to a *different file* breaks identity (it becomes a
  delete + add), because the path is part of the id. This is an accepted,
  documented v1 trade-off.

This is the invariant the contract test `test_body_edit_preserves_symbol_id`
locks down.

### Edges

An **edge** is a typed relationship: `calls`, `imports`, `inherits`, `implements`,
`references`, or conservative implicit `framework` wiring. Framework edges do
not claim that a runtime call occurred and therefore stay out of `callers()`.
The crucial ownership rules that make incremental re-indexing correct:

- **Symbols belong to the file that defines them; edges belong to the file where
  the *reference* occurs.**
- Re-indexing file `F` is one transaction: delete symbols and edges with
  `file_id = F`, re-parse, re-insert. Nothing outside `F` is touched.
- If a symbol in `F` disappears, edges from *other* files that pointed at it
  become **dangling** (`dst = NULL`) but keep their textual target `dst_name`, so
  no information is lost and the edge can be re-resolved later. Re-indexing the
  defining file reconnects them.

## Confidence

Not every edge is equally trustworthy, and pretending otherwise is how indexes
lie. Every call edge carries one of three levels:

| Confidence | Meaning | Origin |
|---|---|---|
| `certain` | Resolved by real semantics | **L1** — an LSP server / jedi |
| `inferred` | Traced through an import with a single target | **L0** — import heuristic |
| `possible` | Name match with ≥ 1 candidate | **L0** — lexical; up to 5 candidates become 5 `possible` edges |

Two rules keep this honest:

- **`possible` is never presented as `certain`.** No query and no renderer ever
  upgrades a confidence level (contract test: `test_possible_stays_possible…`).
- **Transitive queries propagate the *minimum*.** In `impact` or multi-hop
  `callers`, a path's confidence is the weakest edge on it — a chain is never
  more certain than its least-certain link (`test_impact_propagates_minimum_confidence`).

An ambiguous call (say `x.run()` where three `run` methods exist) fans out to
**all** candidates as separate `possible` edges. This preserves recall — the real
target is guaranteed to be in the set — while the label tells the agent to verify
before trusting.

## Freshness

Staleness is the number-one failure mode of code indexes, so freshness is
defended in **four layers**. The goal: it is impossible to serve a fact without
knowing whether it is current.

1. **Startup — boot scan.** Content-hashes (with mtime+size as a fast-path) catch
   everything that changed while the tool was off: a `git pull`, a branch switch,
   edits made by another process.
2. **Session — file watcher.** A native watcher (`codegraph watch`) queues an L0
   re-index of each changed file with a short debounce.
3. **Query — read-repair (the final guarantee).** Before answering, every tool
   verifies the `content_hash` of each file that appears in the answer. On drift
   it re-parses those files synchronously (milliseconds) and answers with fresh
   data, noting `freshness` in the envelope. New files that appeared after
   indexing are picked up here too.
4. **L1/L3 — freshness declared, never faked.** LSP refinement is asynchronous:
   until it arrives, edges stay honest L0 (`inferred`/`possible`). L3 descriptions
   compare their `source_hash` against the current body hash on read and are
   flagged `stale` if they diverge.

A response is only marked `fresh` if the stored content-hash of every involved
file matches disk. This is asserted directly by `test_fresh_response_matches_disk_hash`.

## The layers (L0–L3)

Each layer is independently useful; higher layers refine, never gate.

- **L0 — structural (tree-sitter).** Symbols, containment, and call/import/inherit
  edges from the syntax tree. Works on any repo, no configuration, no language
  server. Confidence: `inferred` / `possible`.
- **L1 — semantic (LSP / jedi).** A pluggable resolver runs after L0 and promotes
  call edges to `certain` when exactly one in-repo definition is found — including
  instance-method calls that name-based resolution can only mark `possible`. See
  [Languages & Resolvers](languages.md).
- **L2 — graph metrics.** PageRank (structural centrality, recomputed lazily) and
  Louvain community detection (subsystems/domains). Powers `overview`, `impact`
  ranking, and `communities`. Also the home of **dataflow/taint**, which is
  *flow-sensitive* in 18 of the 19 dedicated code languages: the taint
  environment travels a structured CFG, so a redefinition kills the old taint
  (`x = input(); x = escape(x); sink(x)` is clean). Findings carry two
  independent axes — `confidence` (was the call resolved?) and `flow_evidence`
  (was the flow verified, or over-approximated?).
- **L3 — descriptions (LLM).** On-demand, cached, provider-agnostic natural-
  language summaries of a symbol, module, or domain. Invalidated by body-hash, so
  a summary never silently describes old code. Entirely optional — nothing else
  depends on it.

## The response envelope

Answers are **compact text**, not verbose JSON — tokens matter. The envelope
appears only when there is something to declare; **silence means fresh and within
known limits.**

```
⚠ freshness: 2 files changed since indexing; re-indexed now (L0).
⚠ completeness: static analysis — dynamic/reflective dispatch may be missing;
  3 unresolved refs in this scope.
⚠ truncated: showing 20 of 143 (use offset/limit).
```

The **completeness** line is mandatory on `callers`, `callees`, `impact`, and
`references` whenever a `possible` edge or an unresolved reference exists in scope
— it is the materialization of invariant #3. For agents, the MCP layer also
exposes these as structured fields; see [Agents & MCP](mcp.md).

## Where to go next

- Put the concepts to work → **[CLI Reference](cli.md)**
- How `certain` edges are produced → **[Languages & Resolvers](languages.md)**
- The schema and pipeline behind all this → **[Architecture](architecture.md)**
