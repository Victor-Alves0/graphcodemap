# Changelog

All notable changes to this project are documented here. Format loosely based
on [Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Dedicated HTML and CSS/SCSS extractors.** Modelled the way the languages
  actually work: **CSS defines, HTML uses.** CSS/SCSS emit a symbol per class
  (`css_class`) and id (`css_id`) selector, plus SCSS `@mixin`/`@function`, and
  `@import`/`@use` become import refs. HTML emits `html_id` symbols for `id=`
  anchors, turns `class="a b"` into *references* (the definition lives in the
  stylesheet, not the markup), and records `<script src>` / `<link href>` /
  `<img src>` as imports — skipping external URLs and stripping query strings.
  Measured motivation: routing these through the generic tier yielded **0 symbols
  for HTML** and, for CSS, missed every id while inventing a false `hover`
  "class" out of `.btn:hover`. HTML/CSS/SCSS join `DEDICATED` but are excluded
  from the dataflow parity invariant via the new `MARKUP` set — stylesheets have
  no data flow. Declared limit: *asset* refs (`<script src>`, `@import`) stay
  *dangling* (`dst_name` preserved) because the graph has no file-level symbol
  to resolve to.
- **Style usage is linked across languages.** Measuring the extractor above on a
  real React app exposed that it only delivered half the model: 2 web files
  produced **597 symbols (24% of the graph) and 597 islands — 0 resolved edges**,
  because in a modern codebase the consumer of a CSS class is `className=` in
  TSX, not HTML. The TS/JS extractor now emits `references` for `className=` /
  `class=` (string literals, static chunks of template literals, and literals
  inside `clsx(...)`-style calls), and the resolver relinks `references` to
  `css_class`/`html_id` **without a language filter** — the kind is what prevents
  binding to a homonymous function (`indexer.STYLE_DEF_KINDS`). An interpolated
  chunk (`col-${n}`) is a *prefix*, not a class name, and is discarded rather
  than invented; `className={styles.card}` (CSS Modules) has no literal and
  yields nothing. Same repo after: **islands 597 → 174 (29%)**, **`references`
  edges 0 → 689 of 721 resolved**, 688 of them `tsx → css`. The islands that
  remain are now *signal* — 173 of 595 classes with no use anywhere, i.e. dead
  CSS, which nothing in the toolchain reported before. CSS `#id` selectors also
  reference the `html_id` they style, so they stop being islands too. Checked
  for the obvious regression: zero CSS classes reach the top-30 by rank.
- **Files are symbols, so local `imports` finally have a target.** This was the
  previous release's declared limit: `<script src>`, `<link href>` and `@import`
  carry a *path* as their guess, and the graph had nothing path-shaped to point
  at, so they were dangling by construction. Every indexed file now gets one
  `kind='file'` symbol (`fqn` = module fqn, `name` = its last segment so the
  qualified-guess resolver can reach it), and `imports` are resolved by path
  first — relative to the importing file, with the usual extension candidates,
  Sass partials (`@use "buttons"` → `_buttons.scss`) and `index.*`. External
  packages (`react`, `node:fs`) match nothing and stay dangling, which is
  correct. On the same repo: **0 of 158 distinct dangling imports still look
  like a local path.** Cost is 1 symbol per file (5.6% of that graph, against
  the 596 CSS classes a single stylesheet contributes) and no measurable
  indexing time (12.39s → 12.30s). `file` symbols are deliberately excluded from
  `index()["changes"]`: a host wants to know which *declared* symbol changed.

### Fixed

- **Markup no longer crowds code out of `find_symbol`.** Searching `menu` in a
  React app returned **10 of 10 results as CSS classes**. Three independent
  causes, all fixed: (1) the match tiers had no `ORDER BY`, so ties inside a
  tier came out in whatever order SQLite chose — now rank first, `file` last;
  (2) a camelCase symbol like `openMenu` was *unreachable*, because the fuzzy
  substring tier inherited the connection's `case_sensitive_like=ON` (correct
  for the exact tiers, wrong for a fallback) — that tier now folds case, which
  costs no query plan since `%x%` never used an index anyway; (3) even when
  reachable, that last tier never ran, because the earlier tiers had already
  filled the limit with markup — so a second pass restricted to code now
  guarantees markup takes at most half the results. Ordering keeps an *exact*
  match on top whatever its kind (searching `menu` still shows `.menu` first),
  then code, then the rest. Same query now returns both matching functions in
  the top 3. `find_symbol(kind=…)` is untouched — the caller already chose.
  Costs one extra query pass only when markup dominates (~+3ms per query).
- **`import "./styles.css"` now links the component to its stylesheet.** The
  single most common asset import in a React codebase was silently lost:
  relative specifiers were rewritten to a dotted module fqn, which for a
  non-JS extension (`src.styles.css`) destroyed the only useful thing in them —
  the path. Relative *asset* imports now keep their path so the file resolver
  can reach them; `module` is still the fqn for call aliases, a different job.
- **A bare specifier no longer binds to a same-named local file.**
  `import "constants"` resolves to node_modules in Node and every bundler, but
  path resolution was happily linking it to a sibling `constants.ts` — inventing
  a local edge for any external dependency sharing a name with a file in the
  repo. Path resolution now requires evidence of an actual path: a slash, an
  explicit `./`, or a stylesheet/markup source, where every import *is* a path.
- **Escaped CSS selectors match the class actually written in the markup.**
  `.mt-1\.5` and `.hover\:bg-blue` (how Tailwind, and any non-identifier class,
  must be written in CSS) never matched `className="mt-1.5"`, because the
  selector keeps the escapes and the attribute does not. Related: `references`
  is now resolved *before* the qualified-guess branch — a dot inside a class
  name is not scope qualification, and falling into that branch made such a
  class permanently unreachable.
- **A root-level `__init__.py` no longer produces a nameless symbol.** Its
  module fqn is empty by convention (the package is the directory, and there is
  no directory), which gave the new file symbol an empty `name` and `fqn` —
  unreachable, and noise in the FTS index.
- **`resolve_edges` no longer scans `symbols` twice.** The path→file-symbol map
  is built from the scan that already builds the name index; a second full scan
  would be paid on every read-repair.
- **`find_symbol` ordering no longer depends on how much markup the repo has.**
  The final sort was only applied on the branch where the code floor kicked in,
  so identical queries ordered differently across repos. It is applied always,
  and the code floor is skipped for an empty result (the restricted pass is a
  subset of the first, so it can only be empty too — and that is the expensive
  path, the one that triggers the freshness sweep).
- **Host-integration API** (for embedding GraphCodeMap as a service, not the CLI):
  - **`index()` now reports which symbols changed** — `stats["changes"]` carries
    `added` / `removed` / `signature_changed` (with `before`/`after` signatures)
    plus exact `counts` and a `truncated` flag. This closes the edit loop: reindex
    → see that `save_user` changed signature → run `impact()` and warn before
    committing, with no git diff and no guessing the symbol. The indexer already
    knew this (it deletes the old symbols and inserts the new); it just never
    reported it. Costs one indexed lookup per *pre-existing* file, so a first
    index pays nothing. Also on `Indexer.index_file` via `last_changes`.
  - **Injectable L3 credential**: `CodeGraph(root, llm=...)` and
    `describe(target, llm=...)` accept a callable `(system, user) -> str` **or an
    API key string**, bypassing `os.environ`/`.env` entirely. Multi-tenant hosts
    hold a per-user key and can't mutate global env per request (race) nor lose
    the cost from their ledger — the provider exposes `.usage`.
  - **`index(exclude=[...])`**: gitignore-style exclusion patterns stored in the
    index (`meta['index_excludes']`), so the policy belongs to the host without
    writing `.codegraphignore` into the user's working copy. The negative
    complement of `scope`. Honoured by indexing, the freshness sweep and the
    watcher. `None` keeps the stored policy, a list replaces it, `[]` clears.
  - **`doctor()` no longer returns the absolute server path** — `root` was
    replaced by `root_name` (directory name only), so MCP/API payloads don't
    expose the server's filesystem layout and hosts don't have to strip it.

- **Partial / scoped indexing** (`index --scope <subtree>`, `CodeGraph.index(scope=...)`).
  Index only the subtree(s) you care about — the escape hatch for monorepos too
  big or too dense to index whole (the scale proof showed the full Linux kernel,
  72k C files, doesn't complete on a dev box). The scope is **persisted**
  (`meta['index_scopes']`), **accumulates** across runs (index `drivers/gpu`, then
  `drivers/net`), and is respected everywhere: re-index, the freshness sweep, and
  the watcher all stay within scope, and removal only prunes vanished files
  *inside* scope (indexing subtree A never deletes B). The freshness sweep walks
  only the indexed subtrees, so a 500-file scope of a 100k-file repo sweeps in
  ~4ms instead of ~0.7s (185×). No scope = whole repo (unchanged default).

- **Scale proof to 100k+ files** (`evals/scalebench.py`, results in
  `evals/RESULTS.md`). A reproducible harness generates a synthetic repo with
  real graph density (cross-file imports/calls) and measures cold index time,
  peak memory, DB size, the O(files) freshness sweep, incremental re-index, and
  query latency at growing N. Findings, unvarnished: well-structured (namespaced)
  code scales cleanly to 100k (~8 min, **324 MB peak, no OOM** — the in-memory
  `resolve_edges` index held). Two real ceilings surfaced and are now documented:
  the strong freshness sweep costs ~5s per missed query at 100k (needs tiering
  above ~30k), and the full Linux kernel (72k C files) did not complete — dense C
  (~30× the on-disk size, name-based fan-out like `dev_err`×35k) makes active L1
  (clangd) a feasibility requirement, not a nicety.

- **Field-sensitive dataflow/taint.** A tainted fact is now an *access path*
  (`("user", "password")`), not a bare name. Reading a path is tainted if it or
  any prefix is tainted (marking the whole object taints its fields; marking one
  field does **not** taint its siblings — the prefix rule). This is a *precision*
  win (a tainted object's unrelated field is no longer flagged) **and** a *recall*
  win (member-target assignments like `obj.field = evil` are now tracked — the
  Python extractor previously dropped them entirely). Validated for Python and
  JS/TS with a direct proof — seeding `o.x` vs `o.y` yields different results,
  impossible in the old name-based engine (`tests/test_dataflow_fieldsens.py`).
  The generic tier reconstructs paths best-effort and falls back to base-name
  (depth-1) collection when a grammar's member node is unfamiliar, so no language
  loses recall. Path depth is capped (truncation keeps the prefix = safe
  over-approximation). Alias/flow sensitivity remain out of scope.
- **L1 coverage: every dedicated language now has a resolver wired.** Added the
  four that had none — Java (`jdtls`), C# (`csharp-ls`), Scala (`metals`), Swift
  (`sourcekit-lsp`). **Java validated live** (JDK 21 + Eclipse JDT LS): a
  cross-file call promoted to `certain` in ~14s — the **7th** server family and
  the **first launcher-based** one, proving the generic client is not limited to
  a single bare executable on `PATH`. The generic LSP client (`l1/lsp_base.py`)
  gained a `_popen_argv()` hook (subclasses build a full launch command — jdtls
  runs `java -jar <equinox-launcher> -configuration <cfg> -data <workspace>`),
  `initializationOptions`, and `workspaceFolders`. C#/Scala/Swift are wired and
  inert until their toolchain is present (no .NET/Swift/coursier on the dev box),
  with opt-in tests (`tests/test_l1_extra.py`) that validate each once installed.

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

- **Bounded WAL on huge indexes (fixes the full-repo index stall).** Indexing a
  very large repo (the full Linux kernel, 72k C files) stalled: the L0 graph wrote
  fine (~2.4GB) but `resolve_edges` then wrote millions of edges in a **single
  transaction**, so the write-ahead log grew unbounded (~1.2GB) — uncommitted
  frames can't be checkpointed — until the final commit triggered one giant
  checkpoint that hung. Fixed by committing the bulk edge writes in chunks
  (`WRITE_CHUNK` rows) with a periodic `wal_checkpoint(TRUNCATE)`, and by
  checkpointing the L0 batch loop every N commits. The WAL now stays small (a
  100k-edge index truncates it to ~10KB) and the index becomes resumable if
  interrupted (each committed chunk is durable; `resolve_edges` is idempotent).
  Verified WAL-bounded + result-identical on synthetic indexes; the full-kernel
  end-to-end was not re-run (multi-GB, hours), so this is the identified
  root-cause fix, honestly labeled as such.
- **Parallel indexing (prepare in threads, writes stay serial).** The index is
  write-bound — profiling puts SQLite `executemany`/`execute` at ~48% and
  parse+extract at only ~7% — so the honest lever is overlapping the parallelizable
  *prepare* (read + tree-sitter parse + extract, all of which release the GIL) with
  the serial writer. `index_repo(workers=N)` (auto `min(4, cpus)`, CLI `--workers`)
  runs prepare in a small thread pool for repos ≥1000 files; the single-connection
  writer consumes results **in input order**, so the graph is **bit-for-bit
  identical to the serial index** (proven by an equivalence test). Measured a
  reliable **~1.16×** on the synthetic 100k-style workload (tiny files, so writes
  dominate even more); larger real files parallelize more of the per-file cost.
  Not a linear speedup — the SQLite single-writer is the ceiling, and this is
  stated plainly rather than oversold. Serial path unchanged below the threshold.
- **Watcher-aware freshness: the O(files) sweep is skipped when a live watcher
  already guarantees freshness.** In the production path (MCP server with the file
  watcher on), a query no longer pays the freshness sweep on every empty result —
  the watcher keeps the index hot, so when it is alive and drained
  (`Watcher.is_current()`) the sweep is redundant and skipped, driving the
  per-miss cost toward zero. A periodic backstop sweep (default 30s) still runs to
  cover events the OS watcher may have dropped. The strong guarantee is untouched
  where it matters: **without** a watcher, and **during** the watcher's debounce
  window (event noted but not yet applied), the full sweep runs on every miss as
  before — so `test_repeated_misses_still_catch_edits` and the no-watcher path are
  unchanged. Wired via `QueryEngine.attach_watcher()`; covered by
  `tests/test_watcher_freshness.py`.
- **Freshness sweep ~7.7× faster (`scan_source_stats`, ~4.65s → ~0.6s at 100k).**
  The O(files) staleness sweep that runs on every empty query result was, in two
  successive profiles, dominated by pure overhead — not I/O. (1) `os.path.relpath`
  was 72% (millions of `normcase`/`LCMapStringEx` calls on Windows); replaced by
  building the relative path via concatenation during the directory descent (the
  parent's prefix rides the walk stack) → ~4.65s→~1.33s. (2) `pathspec` matching
  then dominated (15 default patterns × 100k files); since a gitignore pattern
  ending in `/` matches only directories (already pruned during descent) it can
  never change a *file's* status, so files are now checked against a reduced spec
  with those dir-only patterns dropped — exact, order-independent, ~15× fewer
  regex evals per file → ~1.33s→~0.6s. Same file set (guarded by a scan==iter
  test with a mixed dir/file gitignore), same strong anti-staleness guarantee (no
  throttle; the every-miss sweep test still holds).

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
