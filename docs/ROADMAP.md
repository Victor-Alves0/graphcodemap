# Roadmap — Java/Python product reset

## North star

**GraphCodeMap turns ordinary Java and Python repositories into a live,
queryable code graph that can answer change-impact, bug-path, call-reachability
and value-reachability questions with explicit coverage.** Other languages are
experimental until this phase-one contract is complete. See the canonical
[Product Contract](PRODUCT_CONTRACT.md).

## Release gates

1. Every recognized Java/Python file is indexed or explicitly skipped with a
   reason.
2. The persistent graph contains stable declarations, parameters, locals,
   containment, calls, reads, writes and value-flow facts.
3. Impact and reachability pass shared Java/Python product scenarios.
4. Semantic refinement has an atomic, observable lifecycle.
5. Fresh installed CLI, library and MCP journeys pass on ordinary repositories.

## Ordered execution gates

### G0 — Contract and observability

- [x] Make the Java/Python Product Contract canonical.
- [x] Separate experimental language breadth from phase-one support.
- [x] Fix the MCP `doctor` crash found by the installed-path audit.
- [x] Report physical/index states (`indexed`, `partial`, `failed`, `skipped`,
  `not_applicable`); ignored paths stay outside the snapshot by policy.
- [x] Expose semantic `not_started/running/complete/partial` to library, CLI
  and MCP, with atomic publication and previous-snapshot preservation.

### G1 — Complete and stable structural graph

- [x] Prune ignored directories before initial index/setup traversal.
- [x] Include nested methods/functions in file and diff impact.
- [x] Keep overload identities stable when siblings are inserted.
- [x] Resolve ordinary packaged Python `src/` identities.
- [x] Persist `contains`, parameters, locals, fields/properties, `defines`,
  `reads`, `writes` and structurally provable simple `returns`.
- [x] Verify exact content hashes during read-repair, including unchanged
  size/mtime.
- [x] Persist the physical folder/file graph and Git-aware per-stage revisions.
- [x] Pass shared Java/Python structural canaries, including Python `src/`
  packaging and Java lexical shadowing.

### G2 — Reliable semantic linking

- [x] Publish L1 edges, lifecycle and stage receipt atomically; readers keep the
  previous snapshot while refinement is running or fails fatally.
- [x] Cover direct/imported/typed/inherited/interface calls in focused real-
  resolver matrices for both languages, plus Java overload selection and
  explicit method references.
- [x] Report semantic coverage by persisted callsite, with explicit L1, L0
  fallback, resolver-unavailable, no-local-target and per-language/local-
  candidate outcomes.
- [x] Replay unmodified Flask and Spring PetClinic canaries; refine 406/515
  (78.8%) and 340/351 (96.9%) persisted local candidates respectively.
- [x] Pipeline bounded JDTLS definition requests and carry complete L1
  snapshots across non-semantic/no-op index revisions. PetClinic warm full
  revalidation improved 54.75s → 37.61s; no-change L1 takes 0.10s.

### G3 — Persistent dataflow graph

- [x] Persist def-use and flow facts keyed by content/stable node identity.
- [x] Compose parameter/local/field/call/return flow across functions.
- [x] Expose conservative CFG and separate Java/Python heap limitations in the
  versioned stage receipt. Atomic failure and concurrent-writer contracts pass.
- [x] Optimize the bounded dogfood baseline: current `src/codegraph` publishes
  28,171 nodes/37,959 edges for 1,165 callables in 11.446s; five cached runs are
  0.111–0.119s (historical cold run: 43.04s). Structural path-event mapping is
  80.7%; this is not a recall score.
- [x] Reject stale L1 inputs, keep value-node identities distinct, filter a
  directed target before path limits and index incremental file cleanup.

### G4 — Vulnerability analysis

- [x] Migrate path traversal as the first rule family consuming canonical
  persisted paths, with entry and bounded repo-wide candidate/unknown modes.
- [x] Persist external configured/framework source-call results, nested direct
  sources and sanitizer-return transformations as inspectable path nodes.
- [x] Apply source/sanitizer catalog changes at query time without rebuilding
  the value graph; qualified sanitizer rules do not cut domain homonyms.
- [ ] Migrate the remaining vulnerability families.
- [ ] Add labeled vulnerable/fixed and negative corpora for both focus languages.
- [x] Preserve source, sink, ordered path, sanitizer decision and completeness
  evidence identically in library, CLI JSON and MCP for the first family.

### G5 — Product acceptance

- [ ] Build and install a wheel outside the checkout; run public entrypoints.
- [ ] Pass the same golden journey through library, CLI and MCP.
- [ ] Pass two ordinary repositories per focus language without overlays.
- [ ] Execute expensive portfolio/benchmark gates once, on a frozen commit.

## Current position

G0's lifecycle surface and the shared Java/Python structural core of G1 are now
implemented. The product persists the physical repository, structural graph and
the G3 Java/Python interprocedural value graph with exact freshness, revision
metadata and focused golden canaries. Broader ordinary-repository proof remains.
Historical OWASP, Juliet and real-app
results remain valuable subsystem evidence, but they cannot promote the whole
product or either focus language by themselves.

The next micro-goal is evidence, not another engine front: build small labeled
vulnerable/fixed/negative Java and Python corpora for the persistent path-
traversal family, then compare it with the compatibility engine before any
expensive portfolio replay or migration of a second vulnerability family.

## Execution policy

- Prefer small contract tests and one ordinary-repository canary during
  development.
- A benchmark is an external oracle, not a target-specific specification.
- Do not run a test expected to exceed five minutes without recording its
  purpose, estimate and cheaper precursor.
- Promote a gate only from a clean commit with reproducible artifacts.
- Record negative results and known unsupported cases explicitly.

Historical measurements and rejected experiments remain in
[`evals/RESULTS.md`](../evals/RESULTS.md); they are not the current roadmap.
