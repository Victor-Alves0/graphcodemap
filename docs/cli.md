# CLI Reference

The command-line interface exposes the full graph. It is invoked as `codegraph`
(or the identical `graphcodemap`).

```
codegraph [--root PATH] [--db PATH] <command> [options]
```

Global options:

| Option | Default | Meaning |
|---|---|---|
| `--root PATH` | `.` | Repository root. |
| `--db PATH` | `<root>/.codegraph/graph.db` | Index database location. |

A **symbol reference** accepts a fully-qualified name (`auth.TokenService.validate`),
a bare name, or a `path:line` location.

Commands are grouped below by what you're trying to do.

---

## Indexing & maintenance

### `index`

```
codegraph index [path] [--force] [--l1] [--install] [--yes]
                [--scope SCOPE] [--workers N]
                [--exclude PATTERN] [--jdtls-ready-timeout SEC]
                [--jdtls-io-timeout SEC] [--jdtls-diagnostics-timeout SEC]
```

Builds or incrementally updates the index. Only changed files (by content-hash)
are re-parsed.

With `--l1`, the exact physical delta scopes semantic work to affected project
roots. If no supported source or build marker changed and the previous L1
snapshot completed, it is carried forward without starting a resolver. Run
`codegraph refine` explicitly to force revalidation of external toolchain or
classpath state.

| Option | Meaning |
|---|---|
| `--force` | Re-index everything, even unchanged files. |
| `--l1` | Run L1 semantic refinement right after indexing. |
| `--install` | Detect and explicitly install missing pinned toolchains before indexing; implies `--l1`. |
| `--yes` | Confirm `--install` without a prompt (CI/non-interactive use). |
| `--scope SCOPE` | Index only this subtree. **Persisted and additive** across runs; the freshness sweep then walks only indexed scopes. |
| `--workers N` | Threads for the prepare phase (read+parse+extract). Default `min(4, CPUs)`; auto for repos ≥ 1000 files. The SQLite writer is always serial. |
| `--exclude PATTERN` | gitignore-style exclusion, repeatable. Stored *in the index* (nothing written to the repo); replaces the prior policy. Use `--exclude ''` to clear. |
| `--jdtls-ready-timeout SEC` | Java project-import/readiness budget; environment equivalent: `CODEGRAPH_JDTLS_READY_TIMEOUT`. |
| `--jdtls-io-timeout SEC` | JDTLS request budget, at least readiness; environment equivalent: `CODEGRAPH_JDTLS_IO_TIMEOUT`. |
| `--jdtls-diagnostics-timeout SEC` | Budget for transient JDTLS diagnostics to settle before shutdown; environment equivalent: `CODEGRAPH_JDTLS_DIAGNOSTICS_TIMEOUT`. |

### `refine`

```
codegraph refine [--install] [--yes] [--jdtls-ready-timeout SEC]
                 [--jdtls-io-timeout SEC] [--jdtls-diagnostics-timeout SEC]
```

Runs L1 semantic resolution over the index, promoting call edges to `certain`
where a language server (or jedi for Python) resolves a single definition. See
[Languages & Resolvers](languages.md).

### `setup`

```
codegraph setup [LANGUAGE ...] [--all] [--install] [--yes]
                [--tools-dir PATH]
```

Without targets, detects the code languages present in `--root`. Without
`--install`, it is read-only and prints the exact plan. With `--install`, it
reuses existing tools first, asks for consent, installs fixed versions and
persists only verified tool paths in the user's local configuration. `--yes`
is required for non-interactive installation. Aliases such as `ts`, `c++`,
`c#`, `jvm` and `clj` are accepted; `mcp` is an operational target too.

`index --install` is the one-command form of setup + index + refine. Automatic
installation is opt-in: opening/indexing a repository without that flag never
downloads or executes third-party tooling.

### `watch`

```
codegraph watch
```

Indexes, then watches the repository and keeps the index hot as files change.
It prints its initial, ready and failure states when run in the foreground.

### `vacuum`

```
codegraph vacuum
```

Rebuilds the index (`index --force`) and reclaims disk space (`VACUUM`), while
preserving cached L3 descriptions. Use it when the DB looks bloated or stale.

### `stats` / `doctor`

```
codegraph stats
codegraph doctor [--why]
```

`stats` prints counts. `doctor` is a one-shot health check: parse status (with the
paths of any files that failed), the call-edge confidence split with `%certain`,
which L1 resolvers are active, and index staleness. It **exits non-zero when files
failed to parse** (handy in CI). `--why` re-parses the failed files and prints the
reason.

---

## Navigation

### `tree`

```
codegraph tree [path] [--depth N] [--no-refresh]
```

Shows the physical repository graph: directories, files and symbolic links,
including non-code assets. Each file carries its exact content hash, detected
language and indexing state. By default it refreshes the repository snapshot;
`--no-refresh` reads the last recorded snapshot.

### `history`

```
codegraph history [--limit N] [--git-commit SHA]
```

Shows Git-aware graph revisions and the independently versioned analysis stages
that produced them. Dirty worktrees are distinguished by repository snapshot
hash; history records graph metadata/fingerprints, not copies of source bytes.

### `semantic-coverage`

```
codegraph semantic-coverage [--samples N]
```

Reports every applicable persisted callsite as an exact L1 target, semantic
multi-target, L0 fallback, unavailable resolver or no local semantic target.
The last category deliberately does not guess whether the runtime cause is an
external dependency, reflection or dynamic dispatch. `--samples` bounds the
non-`certain` examples printed; the structured library/MCP result retains the
aggregate counts. Output is split by language and also reports the fraction of
persisted local graph candidates refined by L1, so build-script and external-
library calls do not masquerade as missed local resolution.

### `overview`

```
codegraph overview [--scope SCOPE] [--budget TOKENS]
```

A ranked map of the repo (or a subtree): a module tree with the top symbols by
PageRank, sized to roughly `--budget` tokens. The first thing to run in an
unfamiliar codebase.

### `find`

```
codegraph find <query> [--kind KIND] [--limit N]
```

Locates symbols: exact fqn → prefix → full-text (name+doc) → fuzzy, in that order,
with a score. Returns `fqn | kind | signature | path:line | rank`.

### `info`

```
codegraph info <symbol>
```

The symbol card: signature, doc, containment (parent/children), counts
(callers/callees/references), the span to read, and a fresh L3 description if one
exists.

### `refs`

```
codegraph refs <symbol> [--kind KIND]
```

All uses of a symbol, grouped by file, each with `path:line [confidence]`.

### `ego`

```
codegraph ego <symbol>
```

The immediate graph neighborhood — every typed edge in and out at radius 1 — so
an agent can build a local mental model before editing.

---

## Call graph & impact

### `callers` / `callees`

```
codegraph callers <symbol> [--depth N]
codegraph callees <symbol> [--depth N]
```

The incoming / outgoing call tree, each edge labeled with its confidence.
`possible` (name-only) matches are grouped separately so they don't pollute the
strong signal.

### `impact`

```
codegraph impact <symbol> [--depth N]
```

The transitive set of dependents — *what could break if I change this* — ranked
by PageRank × path-confidence. A path's confidence is the **minimum** of the edges
along it. The completeness line is never omitted here.

### `change-impact`

```
codegraph change-impact <target> [--depth N]
```

Like `impact`, but seeded by a **change**: `target` is a set of paths
(comma/space-separated) or a unified diff. Answers *"what does this change
affect?"* — feed it `git diff` to reason about a branch.

### `affected-modules`

```
codegraph affected-modules <target> [--depth N]
```

The modules (not individual symbols) affected by a change — a coarser, higher-
signal view of `change-impact`.

### `related-tests`

```
codegraph related-tests <symbol> [--depth N]
```

The tests that exercise a symbol, found by walking the call graph to test files.

---

## Dataflow & security

### `dataflow`

```
codegraph dataflow <symbol> [--depth N]
```

Traces where each parameter's data flows — the calls it reaches and the returns it
feeds — interprocedurally along the call graph, computed on demand (always fresh).

### `taint`

```
codegraph taint [--scope SCOPE] [--entry FUNC] [--depth N]
                [--max-findings N] [--deadline-ms MS] [--max-steps N]
```

Follows untrusted input (sources) to dangerous operations (sinks); sanitizers cut
the flow. Two modes: a repo-wide scan, or `--entry FUNC` which treats that
function's parameters as untrusted (ideal for reviewing a request handler).
Sources/sinks/sanitizers are configurable in `.codegraph/taint.json`. Findings are
*may-taint* candidates to verify.

Inter-procedural reachability is worst-case exponential (`O(sources × branch^depth)`),
so **prefer `--entry` over scanning the whole base**. `--depth` defaults per mode
(shallow for scan, deeper for `--entry`). `--deadline-ms` (wall-clock) and
`--max-steps` (deterministic) return a **partial** result — the answer is marked
`truncated` and reports `limit_hit` — instead of running too long. See
[RESEARCH.md §6.3](RESEARCH.md) for the cost model and the truncation contract.

### `reaches`

```
codegraph reaches <symbol> [--sink SINK] [--via VALIDATOR] [--depth N]
                   [--max-paths N] [--deadline-ms MS] [--max-steps N]
codegraph reaches --entry <symbol> [--sink SINK] [--via VALIDATOR]
```

Answers *"does a path from this entry point reach a dangerous sink?"* in one shot:
it returns the call chain plus a validation verdict. `--sink` is a preset
(`http`/`sql`/`exec`/`file`) or a regex over the call name; `--via` names a
sanitizer to check for along the path. Like `taint`, `--deadline-ms`/`--max-steps`
return a `truncated` partial result, and `--max-paths` caps the enumeration.
The positional form and `--entry` are equivalent; the explicit alias makes the
transition from `taint --entry` predictable.

---

## Understanding & investigation

### `communities`

```
codegraph communities [--limit N] [--min-size N]
```

The repo's subsystems/domains, via graph clustering (Louvain). `--min-size`
ignores domains smaller than N symbols.

### `explain`

```
codegraph explain <symbol>
```

A rich, no-LLM fact sheet for a symbol: signature, location, containment, key
callers/callees, and how its edges were resolved.

### `suggest`

```
codegraph suggest "<task>" [--limit N]
```

Given a natural-language task, suggests the files most worth reading to do it —
ranked by graph structure, no LLM required.

### `describe`

```
codegraph describe [target] [--refresh] [--top N]
```

The L3 layer: an LLM-generated behavior summary of a symbol, a file, or a domain
(`domain:N`), cached and invalidated by body-hash. `--refresh` regenerates now;
`--top N` pre-warms summaries for the N highest-PageRank hubs. Requires an LLM
provider (see [Library](library.md#l3-provider)); without one, the command reports
that L3 is disabled.

### `visualize`

```
codegraph visualize [--mode MODE] [--symbol SYMBOL] [--depth N]
                    [--min-confidence LEVEL] [--language LANG]
                    [--changed PATHS] [--git] [--git-ref REF] [--staged]
                    [--scope SCOPE] [--top N] [--json] [--out PATH]
```

Exports a **seeded subgraph for investigation** — not a whole-repo hairball — as a
self-contained interactive HTML file (or `--json`).

| Option | Meaning |
|---|---|
| `--mode` | `neighborhood` · `callers` · `callees` · `impact` · `domains` · `file` · `symbol` (aliases `modules`/`symbols`). Default `file`. |
| `--symbol` | Seed for the `neighborhood`/`callers`/`callees`/`impact` modes. |
| `--depth` | Hops for seeded modes. |
| `--min-confidence` | Drop edges below `certain`/`inferred`/`possible`. |
| `--language` | Restrict nodes to one language. |
| `--changed` | Paths or a diff to highlight — or to seed the impact/neighborhood modes. |
| `--git` / `--git-ref REF` / `--staged` | Highlight files changed in the worktree / versus a ref / staged only. Answers *"what does my branch touch?"* |
| `--scope` | Restrict to a directory. |
| `--top N` | Cap the number of (most-connected) nodes. |
| `--json` / `--out PATH` | Emit JSON instead of HTML / choose the output path. |

The HTML has directional arrows, edges styled by confidence (solid/dashed/dotted),
a red ring on changed nodes, a white ring on the seed, and in-page toggles for
confidence and language. Default output: `.codegraph/graph.html`.

---

## Agent server

### `mcp`

```
codegraph mcp [--no-watch]
```

Starts the MCP server over stdio (equivalent to the `graphcodemap-mcp` entry
point). `--no-watch` disables the background watcher (read-repair still guarantees
freshness on every query). See [Agents & MCP](mcp.md).

---

## Observability

Logging is off by default — a library shouldn't spam your output. Turn it on to
see *why* something happened:

```bash
CODEGRAPH_LOG=warning codegraph index .   # which file failed to index, and why
CODEGRAPH_DEBUG=1     codegraph refine     # full debug: LSP activity, L3 token cost
```

L3 (`describe`) reports token usage per generation, so cost stays visible.
