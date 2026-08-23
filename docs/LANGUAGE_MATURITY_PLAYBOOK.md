# Language maturity playbook

This document is the reusable engineering process for taking a GraphCodeMap
language from grammar support to a release-quality analysis profile. It records
what the Java Round 27 work taught us, including the failures that were found
only after testing installation and a real Spring repository.

The goal is not to reproduce Java-specific code. The goal is to reproduce the
discipline that made the Java profile trustworthy.

## The outcome we are optimizing for

A language is not ready because it parses files, has an LSP adapter, or scores
well on one benchmark. It is ready when a new user can install GraphCodeMap,
point it at a representative repository and understand all of the following:

- what was analyzed;
- which facts are semantic and which are guesses;
- where untrusted data came from and where it arrived;
- whether the semantic resolver completed, degraded or failed;
- what evidence supports the advertised precision and recall;
- what important language or framework behavior remains unsupported.

The acceptance unit is therefore a **language profile**, not an extractor.

Terms used below: **L0** is structural extraction; **L1** is semantic resolution
through a language server or equivalent engine; **FQN** is a fully qualified
symbol name; **CFG** is a control-flow graph; **CWE** is a vulnerability
category; **MCP** is the agent-tool protocol. JDTLS is Java's L1 server and is a
worked example, not a dependency of other languages.

## Starting an existing language profile

Use this bootstrap before choosing individual bug fixes:

1. Run `codegraph capabilities`, locate the language row, and record the current
   structural, engine, security and L1 evidence separately.
2. Copy the [profile template](#profile-template), choose an engineering target
   (`E0`–`E4`) and a validation target (`V0`–`V4`), and name the representative
   framework/build layouts.
3. Freeze a baseline commit and record the current focused/full test commands,
   known failures and external target commits. A dirty exploratory run may find
   work, but is not release evidence.
4. Map every missing requirement to one executable positive and one negative
   case. Assign stable IDs such as `KT-L0-001` or `CS-L1-004`.
5. Select the lowest prerequisite that blocks the target profile. That becomes
   the first micro-goal; later red cells stay named but do not compete for the
   same iteration.
6. Follow the micro-goal loop below, update the profile matrix, then choose the
   next blocking cell.

## Non-negotiable principles

1. **Correctness before breadth.** Finish one declared micro-goal and its
   evidence before opening another language or vulnerability family.
2. **Uncertainty is data.** Never convert an unresolved target into `certain` or
   an unhealthy LSP run into `complete`. A definition being absent is not, by
   itself, an unhealthy run; the edge remains unresolved at its honest L0
   confidence.
3. **Benchmarks are oracles, not product requirements.** A fix must be stated as
   a general semantic invariant and tested with a minimal non-benchmark fixture.
4. **Safe kills require stronger proof than dirty propagation.** May-dirty
   effects union across possible paths and targets; clean kills require proof on
   every relevant path and target.
5. **Identity precedes dataflow.** Wrong ownership, overload identity, source
   location or call target makes every later analysis confidently wrong.
6. **Operation is part of semantics.** A resolver that times out silently or is
   impossible to install does not provide semantic resolution to the user.
7. **Evidence fails closed.** Dirty targets, dirty engines, truncated output,
   command failures, contradictory health and external paths make a report
   ineligible instead of merely adding a warning.

## Definition of done by layer

### L0: structural identity

The extractor must preserve the identity a developer actually uses:

- lexical owner and nested scope;
- package/module plus type plus member identity;
- overload/signature identity where the language supports overloads;
- constructor, static, instance and external-call distinctions;
- exact file, line, column and byte span for declarations and call sites;
- multiple calls on one line without site collision;
- separate symbols for anonymous functions, lambdas and test blocks;
- references to functions passed as values, not only direct calls;
- imports, inheritance and implementation relationships;
- canonical user-facing names, including stack-trace/import FQNs.

Required adversarial fixtures include duplicate short names, nested functions,
test doubles, overloads, disjoint lexical blocks, same-line calls, callbacks and
production/test symbols with identical member names.

The Java lesson was decisive: overload and site identity had to be fixed before
heap summaries, taint provenance or L1 promotion could be trusted. A later
review still found an overload summary keyed by a non-unique FQN; declaration
order changed the result. Every downstream cache and summary must use the same
unique identity as the graph.

### L1: semantic resolution and health

An adapter being importable or present on `PATH` means **wired**, not healthy.
A release-quality resolver must cover:

- actionable discovery of the server/runtime already installed;
- correct project-root detection for single projects and monorepos;
- build markers, source roots, classpaths/workspaces and project toolchains;
- a runtime for the language server that may be distinct from the project
  runtime;
- encoding conversion such as UTF-8 bytes versus UTF-16 LSP columns;
- exact promotion of the existing L0 call site rather than a new approximate
  site;
- configurable readiness and request budgets;
- explicit timeout, crash, diagnostics, warning and partial status;
- late diagnostic draining and truthful shutdown health;
- incremental invalidation when a file, override or build marker is created,
  changed or deleted;
- revalidation of untouched callers in the affected semantic project;
- no stale L1 proof surviving an unavailable or failed revalidation.

The first user journey must be tested from a clean installation to the first
`certain` edge. The test must verify error messages and remediation commands,
not just the happy-path protocol handshake.

Java exposed four operational traps that apply to every future resolver:

1. opening above the real source root produced misleading diagnostics;
2. sampling health after shutdown produced a false handshake result;
3. a fixed readiness budget could return zero promotions on a real Spring
   import while still appearing active;
4. a temporary workspace made every run pay the full cold-import cost.

The first three are correctness issues. Workspace reuse is an optimization and
must not weaken isolation, freshness or cleanup.

#### L1 health truth table

| Observed state | Public status / exit | Existing L1 proof | Required action |
|---|---|---|---|
| Applicable resolver unavailable | `partial`, nonzero when L1 is requested | Reset for the applicable revalidation universe | Name the missing component and exact remediation |
| Handshake/process/request I/O failure | `partial`, nonzero | Reset; never preserve stale `certain` proof | Persist sanitized failure and fail closed |
| Readiness probe unresolved and no semantic hit anywhere | `partial`, nonzero | Reset | Report readiness budget and how to increase it |
| Representative probe unresolved but another site returned a definition | `complete` with informational warning, zero if there is no other error | New resolved sites may be promoted | Do not require external/unresolvable calls to have repo definitions |
| Warning diagnostics only | `complete` with warnings, zero | New proof is usable | Surface warnings and completeness |
| Error diagnostic, even after semantic hits | `partial`, nonzero | Do not present the pass as complete | Preserve the error unless timing proves it is teardown-only noise |
| Healthy server, zero definitions after all sites respond | `complete`, zero | Sites remain L0/possible; no `certain` is invented | Report zero honestly; this is not a timeout by itself |

### Control flow and local state

The flow engine must have executable contracts for:

- branch join as may-taint;
- finite loop convergence and zero-iteration paths;
- mandatory and ordered `finally` behavior;
- early return, break, continue and exception paths where applicable;
- invoked callbacks/lambdas without eagerly executing deferred ones;
- assignment kills only where domination/path proof permits them;
- container reads/writes with constant and dynamic keys/indices;
- aliasing and escape that prevent an unsafe clean kill;
- exact provenance when multiple tainted arguments or sources coexist.

The Java work showed why textual order is not control flow. A conditional
`Properties.setProperty(key, clean)` originally cleaned the key globally even
when the branch was not taken. State analysis must explicitly distinguish
may-loaded from must-clean and join them through the structured CFG.

### Heap, summaries and dispatch

Interprocedural summaries must be conservative across dispatch:

- dirty effects union across every possible target;
- clean effects intersect across a closed, complete target set;
- any missing body, reflection, alias or escape blocks the clean kill;
- summaries are keyed by unique symbol/signature identity;
- receivers, fields, elements, static state and process state are not collapsed;
- an explored-state cache retains distinct origins and relevant state;
- recursion and cycles converge without silently dropping paths.

This asymmetric lattice was the core reusable Java result: propagation may be
broad, but pruning must be proven. It fixed real vulnerable/fixed pairs without
teaching the engine their filenames or benchmark labels.

### Sources, sinks and sanitizers

Rules must be semantic enough to avoid short-name collisions:

- qualify by language, receiver/type, member and argument role where possible;
- distinguish receiver taint from individual argument taint;
- keep source literals and source parameter names as evidence;
- model sinks by the dangerous argument, not every argument;
- make sanitization compatible with vulnerability and output context;
- never treat a generic short name such as `loads`, `get` or `execute` as proof
  of a dangerous API;
- preserve safe framework primitives without assuming an entire framework is
  safe.

Java's contextual sanitizers demonstrated the rule: HTML escaping can kill XSS
without killing SQL injection or path traversal. The same standard must be used
for each new language and framework.

### Query, ranking and presentation

The graph can be internally correct and still mislead through its query layer.
Contracts must verify that:

- canonical FQNs, short names and documented aliases locate the same symbol;
- fallback targets respect lexical visibility, module boundaries and language;
- nested test doubles cannot become cross-module production targets;
- test paths are a tie-break disadvantage, not an unconditional exclusion;
- aggregate labels never call `inferred`/`possible` facts “trusted”;
- ranking is not dominated by generic-name fallback edges;
- truncation is visible and totals describe the returned confidence classes;
- natural-language suggestion handles the documented query languages or tells
  the user to query in the code's language;
- overview budgets produce a bounded, explainable result.

### Reporting and evidence

Every security finding should preserve:

- exact source and sink sites;
- source/sink symbols, receiver and argument role;
- vulnerability category/CWE;
- path steps and their confidence/provenance;
- resolver health and report completeness;
- stable fingerprint without collapsing distinct sites or origins.

Every external gate must record:

- target repository, commit, scan subdirectory and dirty state;
- engine repository, commit, dirty state and source-tree hash;
- tool and adapter versions;
- invocation status, exit code, duration and memory scope;
- truncation, errors, warnings and resolver health;
- byte hashes of reports/scores and the exact scorer contract.

Paths in manifests and reports must stay within their declared roots. Absolute
or parent-traversal paths and external symlinks are rejected.

## Evidence axes and promotion levels

Evidence can be acquired in a useful non-linear order. Ruby may have real
security evidence before an operational L1; Go may have real-repository L1
before a labeled security corpus. Record two axes instead of hiding that fact.

| Engineering | Required evidence | What it proves |
|---|---|---|
| E0 Recognized | Grammar/file detection | Files are classified, nothing more |
| E1 Structural | Adversarial L0 identity battery | Declarations and sites have usable identity |
| E2 Engine | Flow/heap/rule contracts with vulnerable and safe fixtures | Shared analysis has language semantics |
| E3 L1 wired | Adapter discovery and protocol fixture | Integration exists, not that users can rely on it |
| E4 L1 operational | Clean-install smoke plus representative real repository | Users can obtain honest `certain` edges |

| Validation | Required evidence | What it proves |
|---|---|---|
| V0 Local | Minimal positive/negative contracts | The implementation matches its local specification |
| V1 Security oracle | Category-correct labeled corpus or pinned vulnerable real application with explicit expected findings | Security behavior on one bounded distribution |
| V2 Independent oracle | Different corpus, generator, project or coding style | Reduced risk of oracle-specific fixes |
| V3 Real pairs | Pinned vulnerable/fixed commits and patch-derived oracle | Findings track real security fixes |
| V4 Release evidence | Clean engine/targets, manifests, package smoke, full suite and fresh review | Evidence is reproducible and publication-eligible |

Product labels are promotion gates, not the order in which experiments must be
run: `recognized=E0/V0`, `structural=E1/V0`, `engine=E2/V0`,
`security-validated=E2/V1+`, `semantic-validated=E4/V1+`, and
`reference=E4/V4` with no open P1. “Perfect” on one axis never waives the other.
Validation levels are cumulative for promotion: V4 publication requires the
applicable V0–V3 evidence, even though teams may discover or run those gates in
a non-linear order.
Java's perfect OWASP/Juliet rows did not expose the Spring readiness timeout,
canonical-FQN lookup or misleading installation message; the real-repository
journey did.

Severity means: **P1** can make a supported answer wrong, incomplete without
disclosure, unsafe or unusable in the target journey; **P2** materially harms
precision, operability or maintainability with a workaround; **P3** is bounded
quality/debt. A reference profile has no open P1 in its declared scope.

## Required test portfolio

Each language profile owns these suites:

1. **Minimal semantics:** tiny fixtures isolating one invariant each.
2. **Adversarial identity:** homonyms, nesting, overloads, same-line sites,
   cross-module collisions and test doubles.
3. **Flow closure:** branch, loop, exception/finally, callback and container
   behavior with vulnerable and safe companions.
4. **L1 protocol:** positions, multiple definitions, diagnostics, timeout,
   shutdown and unavailable-server behavior.
5. **Incremental L1:** create/change/delete source and project marker; untouched
   caller must be revalidated.
6. **Clean-install journey:** documented install command through index/refine,
   doctor and first `certain` edge.
7. **Representative repository:** a framework/build layout users actually run.
8. **Labeled security corpus:** category-correct scoring with negatives.
9. **Independent holdout:** different origin and coding style.
10. **Pinned CVE pairs:** vulnerable found, fixed cleared for the patch-derived
    oracle without requiring all unrelated findings to disappear.
11. **Release evidence:** clean engine/targets, full suite, coverage, build,
    installed-wheel CLI/MCP smoke and fresh-reader documentation review.

## Micro-goal workflow

Use this loop for every gap:

1. Write the user-visible failure in one sentence.
2. Reduce it to the smallest fixture that still fails.
3. State the general invariant; do not mention the benchmark filename in the
   implementation requirement.
4. Add a strict regression and observe the failure.
5. Implement the smallest architectural fix.
6. Run adjacent contracts, then the full project suite.
7. Re-run the external gate and check both positive and negative movement.
8. Test a different repository or holdout before calling the behavior general.
9. Update the profile's known gaps and evidence manifest.
10. Freeze code before generating release reports.

If the score improves but the minimal invariant, safe companion or independent
holdout regresses, reject the change.

Every requirement row should be executable rather than interpretive. Record an
ID, precondition, expected positive result, expected negative result, command or
test, and threshold. Terms such as “closed dispatch” or “relevant state” must be
defined for that language in its profile's known-boundaries section.

## Parallel work without losing coherence

Agents can work independently on extractor identity, L1 health, flow/heap and
scoring/reporting because those areas usually touch different modules. They
must share these rules:

- one bounded invariant per agent;
- tests accompany every implementation;
- no agent commits or stages while integration is in progress;
- the integrator re-runs overlapping suites after all edits land;
- final code review compares required behavior with the implementation;
- code is frozen in a clean commit before external evidence is regenerated;
- evidence/docs are published in a second commit if necessary, so reports can
  prove a clean engine tree.

Java benefited from this parallelism, but the final independent review was what
found three remaining P1s outside the headline benchmarks. Parallel speed does
not replace an adversarial integration review.

## Operational and onboarding contract

For every language targeting `E4` or a semantic/reference product label,
document and test:

- the one recommended install command;
- optional dependencies and why they matter;
- server/runtime discovery order and exact environment-variable shape;
- monorepo/workspace discovery behavior;
- project prerequisites such as build metadata or compilation;
- configurable timeouts and resource limits;
- actionable `doctor` output for unavailable, partial and failed states;
- explicit consent for any download or external code/LLM transmission;
- pinned versions and checksums for setup helpers;
- behavior behind proxies, offline environments and missing egress;
- expected time and steps from clone to first `certain` edge.

Discovery should prefer tools already on the machine. Failure must name the
missing component and the exact remediation. Explicit, checksum-pinned setup is
preferred over silent downloads; automatic setup is opt-in.

## Profile template

Copy this section into `docs/<LANGUAGE>_ANALYSIS_CONTRACT.md`.

```markdown
# <Language> analysis behavioral contract

Status: <recognized|structural|engine|security-validated|semantic-validated|reference>
Current evidence: E<0-4>/V<0-4>
Target evidence: E<0-4>/V<0-4>
Open P1/P2/P3: <counts and links>

## Required behavior
- L0 identity:
- L1 identity and health:
- Flow semantics:
- Heap/dispatch semantics:
- Rule semantics:
- Query/reporting semantics:
- Operational onboarding:

## Requirement-to-evidence matrix
| ID | Precondition | Required positive behavior | Required negative behavior | Implementation | Executable evidence/threshold | Current gap |
|---|---|---|---|---|---|---|

## External gates
| Gate | Target/commit | Result | Health | Artifact |
|---|---|---|---|---|

## Known boundaries
- Framework behavior:
- Reflection/metaprogramming:
- Build/project layouts:
- Concurrency/native/FFI:
- Unsupported vulnerability families:

## Promotion decision
- Current engineering/validation axes:
- Target product label:
- Blocking contracts:
- Next micro-goal:
```

## Worked Java profile

Use Java as an example of the completed artifacts, not as a source-code
template:

- [Java behavioral contract](JAVA_ANALYSIS_CONTRACT.md) maps required behavior
  to implementation, tests, external results and known ceilings.
- [Round 27 gate manifest](../evals/round27-java-gates-manifest.json) pins engine,
  target and artifact identity for release evidence.
- [Round 27 real vulnerable/fixed results](../evals/java-real-pairs-round27-results.json)
  show the patch-derived oracle.
- [Security benchmark contract](SECURITY_BENCHMARK.md) defines category-correct
  scoring and fair comparison boundaries.
- [Round 28 feedback ledger](REAL_WORLD_FEEDBACK_ROUND28.md) shows how a strong
  semantic profile can still fail its first-run and real-framework journey.

## Questions a release reviewer must answer

Before promoting a language, a reviewer unfamiliar with its implementation
must be able to answer from its contract and artifacts:

1. What exact user behavior is promised?
2. What happens when L1 is absent, slow, unhealthy or partially successful?
3. Can a canonical symbol name from a stack trace be queried?
4. Which identities prevent overload, scope, test-double and same-line
   collisions?
5. Which state changes are may-dirty and which are proven must-clean?
6. Are sources, sinks and sanitizers type/role/context aware?
7. Which labeled corpus, independent holdout and real CVE pairs were used?
8. Are targets and engine clean and cryptographically identified?
9. What remains unsupported, and can the product say so at runtime?
10. Can a new user reach the first trustworthy result using only public docs?

If the document or artifacts cannot answer one of these, the profile is not
finished even if its current benchmark score is perfect.
