# GraphCodeMap Product Contract

Status: canonical reset contract, 2026-08-24.

This document defines what GraphCodeMap is, which claims the product may make,
and the order in which work is accepted. When a benchmark, feature proposal or
older roadmap conflicts with this contract, this contract wins.

## Mission

GraphCodeMap turns a source repository into a live, queryable code graph for
developers and AI agents. The graph must help answer questions such as:

- If I change X, what code and tests can be affected?
- A user reports a bug on page or endpoint X. Which code can participate?
- Can function X reach subsystem, state or dangerous operation Y?
- Where can an untrusted value enter, be transformed and reach a sink?

The graph is the product core. Dataflow, vulnerability analysis, visualization,
CLI and MCP are consumers of the same facts. They must not maintain conflicting
shadow models or claim maturity that the graph itself does not provide.

## Phase-one scope

Java and Python are the only phase-one product languages. Other extractors stay
available as experimental compatibility surfaces, but they do not block this
contract and must not be presented as having Java/Python parity.

The phase-one features are:

1. repository and code analysis;
2. dataflow and reachability;
3. vulnerability analysis with inspectable evidence;
4. identical facts through the library, CLI and MCP interfaces.

Universal framework behavior, every runtime dispatch target, every generated
source and parity with the whole CodeQL product are not phase-one claims.

## Required graph

### Nodes

Every supported Java/Python file must produce a file node or an explicit
`skipped`/`failed` record with a reason. The graph must represent:

- package/module and file;
- class, interface, enum and record where applicable;
- function, method and constructor;
- field, constant and property;
- parameter and local variable;
- framework entry points when supported by explicit evidence.

Node identity must be deterministic and stable under body edits, line changes,
sibling reorder and insertion of an unrelated overload. A signature change may
create a new callable identity. Moving a declaration between files may create a
new identity in the phase-one model.

### Edges

The minimum graph vocabulary is:

- `contains`;
- `imports`;
- `inherits` and `implements`;
- `calls`;
- `references`;
- `defines`;
- `reads` and `writes`;
- `returns`;
- `flows_to`;
- explicit framework wiring where supported.

Every edge carries its source site, provenance and confidence. An unresolved or
ambiguous fact remains unresolved/ambiguous; it must never be promoted merely
because one same-named declaration exists.

## Meaning of live

A live graph has all of these properties:

- initial indexing prunes ignored directories before traversing them;
- every recognized file is indexed or reported as omitted with a reason;
- editing, adding, deleting or renaming code updates the owned facts;
- inbound relationships are invalidated and re-linked after target changes;
- query freshness distinguishes content verification from a size/mtime hint;
- semantic refinement exposes `not_started`, `running`, `complete` or `partial`;
- readers never observe a refinement reset as a complete empty result;
- rerunning the same revision produces the same graph.

## Query acceptance contracts

### Change impact

`impact(symbol)` traverses callers and data uses with path confidence.
`change_impact(file-or-diff)` seeds every relevant declaration in changed files,
including class members, parameters/fields when relevant and removed symbols.
It returns affected production code and related tests without silently reducing
a changed class file to its top-level class node.

### Bug investigation

Given a route, controller, view, CLI command or named symbol, the product can
return a bounded subgraph containing entry points, callers/callees, state reads
and writes, relevant dataflow and tests. Each missing semantic boundary is
reported rather than hidden.

### Reachability

The product distinguishes call reachability from value reachability. “Can X
call Y?” and “Can a value from X flow into Y?” are separate queries and verdicts.
Reflection, dynamic dispatch and external libraries appear in completeness
metadata.

### Vulnerability analysis

A finding contains the source, sink, ordered path, transformations, sanitizer
decisions, exact locations, rule identifier and analysis completeness. A taint
candidate is not described as a confirmed vulnerability without the evidence
required by its rule.

## Current truth at reset

The existing product has a useful but smaller foundation:

- persistent physical folders/files, exact hashes and indexing states in SQLite;
- persistent Java/Python declarations, parameters, locals, fields/properties and
  structural `contains`, `defines`, `reads`, `writes` and simple `returns` edges;
- Git-aware repository snapshots and independently versioned analysis stages;
- Java/Python dedicated extraction for major declarations, imports, inheritance
  and many calls;
- basic incremental delete/relink contracts;
- callers, callees and symbol impact over the call graph;
- Java/Python flow-sensitive dataflow and taint computed on demand;
- optional semantic linking through JDTLS and Jedi.

It does **not** yet satisfy the entire required graph:

- `flows_to` is not yet a persistent whole-repository graph, and `returns`
  currently covers only structurally provable simple value returns;
- dataflow reparses files on demand and is not reusable graph state;
- common packaged Python `src/` identity was corrected during the reset, but
  still needs broader real-repository validation;
- semantic refinement can be slow and is not the default library path;
- user-facing readiness and coverage reporting are incomplete;
- security validation is much stronger for Java than Python.

Therefore the honest product label is **alpha structural graph**, not a complete
CPG and not universal SAST parity.

## Delivery order

### G0 — Contract and observability

- This document is canonical.
- README, maturity and capability output separate implemented, validated and
  experimental behavior.
- Index, refine and MCP expose progress, coverage, omissions and partial state.
- No long benchmark is a default development test.

### G1 — Complete and stable structural graph

- Fix stable callable identities and file/diff impact.
- Represent every recognized/skipped file.
- Add containment, parameters, locals, references, reads and writes for Java
  and Python.
- Add source-root/package discovery for normal Python layouts.
- Pass one shared Java/Python structural contract corpus.

### G2 — Reliable semantic linking

- Direct, imported, inherited, interface and typed-receiver calls have explicit
  acceptance cases.
- L1 lifecycle is visible and atomic to readers.
- Unresolved and ambiguous calls have actionable coverage summaries.
- Performance is improved before large portfolio replay, using batching/cache
  where the language server permits it.

### G3 — Persistent dataflow graph

- Persist def-use and flow facts keyed to stable nodes and content hashes.
- Support parameter → local/field → call argument → callee parameter → return.
- Model branch/loop/exception kills conservatively and expose uncertainty.
- Separate state/heap limitations for Java and Python.

### G4 — Vulnerability analysis

- Rules consume the persistent flow graph.
- Java and Python each get labeled vulnerable/fixed and negative corpora.
- Findings remain reproducible and inspectable through library, CLI and MCP.

### G5 — Product acceptance

- A clean installed package completes setup → index → refine → doctor → query
  without repository-specific manual knowledge.
- The same acceptance journey runs against at least two ordinary repositories
  per focus language, selected for diversity rather than benchmark scores.
- Only after focused contracts and canaries pass is the expensive portfolio run
  executed once on a frozen commit.

## Normative vocabulary

For phase one, a **recognized file** is a regular `.py` or `.java` file below
the selected repository root that is not excluded by the active ignore policy.
Python stubs/notebooks, generated-source discovery and an exact Java syntax
version matrix are not implied; each must be added explicitly before it becomes
supported. Files above the current 2 MiB parser limit are `skipped`, not absent.

Every recognized file has exactly one terminal indexing state:

- `indexed`: parsed without a known syntax error;
- `partial`: useful facts were emitted but parsing or enrichment was incomplete;
- `skipped`: deliberately not parsed, with a machine-readable reason;
- `failed`: an attempted parse/index operation failed, with an error category.

`ignored` files are outside the recognized-file denominator. Product coverage
reports the count in each state and never combines `partial`, `skipped` or
`failed` with `indexed`.

Edge direction is source-to-target. `contains` links a lexical container to its
member; `defines` links an executable definition site to the variable or value
it defines; `references` is a non-value-specific name use; `reads` and `writes`
are value-bearing accesses; `returns` links a return site/value to its callable;
and `flows_to` links an origin value/definition to a later value/use. Provenance
is one of `l0`, `l1`, `framework` or `dataflow`; confidence is `certain`,
`inferred` or `possible`. Facts without a natural syntax site must carry the
evidence sites from which they were inferred.

“Identical facts” across library, CLI and MCP means the same semantic projection:
stable node IDs, edge kind/direction, locations, provenance, confidence,
freshness and completeness. Ordering, transport envelopes and timestamps may
differ. Determinism compares that projection and excludes volatile timestamps.

For an edit made while the watcher is healthy, convergence must occur before a
subsequent query returns a `fresh` result. Without a watcher, the query must
perform or request verification before claiming freshness. The implementation
verifies exact bytes during read-repair, including same-size/same-mtime edits;
watcher events and metadata remain acceleration hints.

## Repository and graph revision truth

The physical repository graph is distinct from the semantic code graph. It
records every non-ignored directory, regular file and symbolic link, including
assets and unsupported source formats, with path, type, exact content hash and
index state. Editing one file replaces only facts owned by that file; deletion
removes its physical node and invalidates the semantic facts it owned.

Each indexing/refinement step records a graph revision tied to the current Git
commit when available. Dirty worktrees are identified by a deterministic
snapshot hash, so several graph states can coexist conceptually within one Git
commit. Stages (`filesystem`, `l0`, `l1`, L2 metrics, `l3`, `dataflow`) carry
their own version, status and artifact hash. Revisions store reproducibility
metadata and graph fingerprints, not historical copies of source bytes; Git or
another VCS remains responsible for reconstructing old clean source snapshots.

G0 is complete only when this state model appears in the stored schema and in
library, CLI and MCP responses. G1 must add versioned shared Java/Python golden
fixtures, their expected semantic projection and a single command that decides
pass/fail. G5 must pin the ordinary repository revisions and budgets before its
first acceptance run.

## Test strategy and cost controls

Development uses a pyramid:

1. sub-second extractor/unit reproductions;
2. small shared Java/Python product-contract repositories;
3. the GraphCodeMap repository as a Python dogfood smoke;
4. one ordinary canary repository per language;
5. large benchmarks only as release evidence.

Any test expected to exceed five minutes must state its purpose, estimated time
and whether prior evidence can be reused. A source change invalidates only the
smallest relevant gate; documentation-only changes never trigger semantic
portfolio replay. Agents receive bounded context and bounded subtasks.

## Definition of phase-one done

Phase one is complete only when all G0–G5 gates pass and a fresh reader can use
the documented CLI or MCP flow to answer the four core questions—change impact,
bug-path investigation, call/value reachability and an inspectable vulnerability
path—on ordinary Java and Python repositories, with explicit coverage and
without hidden setup or benchmark-only assumptions.
