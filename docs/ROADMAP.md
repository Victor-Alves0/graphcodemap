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
- [ ] Expose semantic `not_started/running/complete/partial` to every interface.

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

### G2 — Reliable semantic linking

- [ ] Make L1 publication atomic or return an explicit non-ready result.
- [ ] Cover direct/imported/typed/inherited/interface calls in both languages.
- [ ] Report unresolved reasons and semantic coverage by applicable language.
- [ ] Improve request batching/cache before another large portfolio replay.

### G3 — Persistent dataflow graph

- [ ] Persist def-use and flow facts keyed by content/stable node identity.
- [ ] Compose parameter/local/field/call/return flow across functions.
- [ ] Expose CFG/heap limitations separately for Java and Python.

### G4 — Vulnerability analysis

- [ ] Make rules consume the canonical persistent flow graph.
- [ ] Add labeled vulnerable/fixed and negative corpora for both focus languages.
- [ ] Preserve source, sink, path, sanitizer decision and completeness evidence.

### G5 — Product acceptance

- [ ] Build and install a wheel outside the checkout; run public entrypoints.
- [ ] Pass the same golden journey through library, CLI and MCP.
- [ ] Pass two ordinary repositories per focus language without overlays.
- [ ] Execute expensive portfolio/benchmark gates once, on a frozen commit.

## Current position

G0 is partially complete; the shared Java/Python structural core of G1 is now
implemented. The product persists the physical repository and structural
declaration/variable graph with exact freshness and revision metadata. G1 still
needs broader golden/canary proof, while persistent interprocedural value flow
belongs to G3. Historical OWASP, Juliet and real-app
results remain valuable subsystem evidence, but they cannot promote the whole
product or either focus language by themselves.

The next micro-goal is deliberately singular: validate the shared structural
contract on ordinary Java/Python canaries, then close G2 semantic lifecycle and
linking before implementing persistent `flows_to` in G3.

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
