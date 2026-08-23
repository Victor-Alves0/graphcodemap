# Java analysis behavioral contract

Status: **Round 27 reference contract, verified by the final release gates.**
Semantic, health, incremental-L1 and reporting invariants below have executable
evidence. The result is bounded evidence, not a claim of whole-JVM completeness.

## Required behavior

Java is acceptable only when all stages agree. A benchmark score cannot waive
a red semantic or health contract.

1. **L0 identity:** declarations retain lexical ownership; overloads and
   same-line calls remain distinct by file, line, column and byte span.
2. **L1 identity and health:** JDTLS promotion preserves the L0 site, converts
   UTF-8/UTF-16 positions correctly, invalidates stale provenance and reports
   handshake, server status and project diagnostics honestly.
3. **Flow semantics:** branches join by may-taint, loops reach the finite
   fixpoint, `finally` is mandatory and ordered, and invoked lambdas execute
   without eagerly executing deferred lambdas.
4. **Heap semantics:** dirty effects union across possible targets; clean kills
   require closed dispatch, complete summaries and no relevant escape.
5. **Rule semantics:** source/sink identity includes receiver and argument role;
   sanitization is compatible with the vulnerability/output context.
6. **Reporting semantics:** findings retain exact source/sink sites, explicit
   source literals allowed by policy, call and flow confidence, provenance,
   truncation and resolver health. Dedupe may collapse equivalent paths, never
   distinct sites or sources.

## Requirement-to-evidence matrix

| Area | Current implementation | Executable evidence | Current gap |
|---|---|---|---|
| L0 identity | Java extraction retains owners, overload IDs, receiver types, line/column and byte spans. Lexical block scopes and same-line resolution are carried through flow lookup. | Former same-line and disjoint-scope regressions are ordinary passing tests; the final project suite is 1,778 passed. | Factory/reflection typing still degrades conservatively without semantic proof. |
| L1 identity | JDTLS uses project roots, UTF-16 conversion, target selection ranges, semantic fan-out and incremental provenance invalidation. Build markers trigger project-root revalidation, including creation/removal in monorepos. | Corrected Juliet overlay: 732/732 files, 4,408 `certain`, complete with zero warnings/errors. Incremental override, marker and late-diagnostic contracts pass. | Language-server availability and project build health remain operational prerequisites. |
| CFG and provenance | Structured flow models ordered branches, finite loop convergence, mandatory `finally`, invoked/deferred lambdas and source provenance through multiple tainted argument candidates. | Former strict characterizations for `finally`, loop convergence, invoked lambdas, source attribution and source/sink same-line identity now pass. OpenRefine reports the exact `getParameterValues("lang")` source at line 83. | Unsupported reflection/native behavior still degrades conservatively; broader real-project control-flow diversity remains necessary. |
| Heap and dispatch | Receiver summaries separate may-dirty effects from must-clean overwrites; fan-out unions dirty state and intersects kills; aliases/escape block unsafe kills. Static/process state and ordered property reads/writes are modeled by the current Java contracts. | Focused heap, alias, dispatch and global-state contracts are green; FitNesse and openHAB fixed revisions clear their oracles. | This is not a whole-JVM points-to analysis: arbitrary object graphs, reflection, concurrency and framework-managed state remain conservative. |
| Rules | Rules are receiver/type/category/argument-role aware. Sanitizer effects are context-compatible instead of one universal kill set. | The semantic OWASP gate scores 902/0/0/796; Juliet CWE-23 scores 444/0/0/444. | These corpora cover a bounded category and coding-style surface; independent projects and more vulnerability families are still required. |
| Reporting | Source, sink and steps preserve column/span; allowed request-parameter literals are explicit; fingerprints and dedupe retain site/provenance distinctions; subject, engine and resolver health participate in report validity. Contradictory/partial/SARIF-aborted evidence fails closed. | Hardened scorer reports 3/3 vulnerable revisions detected and 3/3 fixes clear; path, subject and invocation adversarials pass. | Dirty engine worktrees are identified by a source-tree hash; release evidence is repinned after commit. |

## Verified Round 27 evidence

### Local contracts

- The final project gate is **1,778 passed, 27 skipped** in 148.12 s.
- The focused P1/contract regressions pass inside the broad project gate.
- No previously documented strict Java characterization remains an advertised
  feature gap. A future regression must be recorded as a new executable
  contract, not copied from the Round 26 list.

### External semantic gates

| Gate | GraphCodeMap Round 27 profile | Final pre-report time | Status |
|---|---:|---:|---|
| OWASP Benchmark v1.2 | 902 TP / 0 FP / 0 FN / 796 TN | 280.036 s | Complete, target clean |
| NIST Juliet Java 1.3 CWE-23 | 444 TP / 0 FP / 0 FN / 444 TN | 23.095 s | Complete; healthy `INDEXER_VERSION=35` overlay |
| Java vulnerable/fixed pairs | 3/3 vulnerable detected; 3/3 fixes clear | Per-report timings in `evals/java-real-pairs-round27-results.json` | Valid hardened-oracle evidence |

The first fresh indexing plus JDTLS phases exposed two genuine operational
problems: post-shutdown health was sampled incorrectly, and Juliet was opened
above its actual source root without its bundled classpath. The final overlay
fixes both without suppressing diagnostics and reports complete/clean health.

### Fair CodeQL comparison

The following CodeQL rows are existing versioned measurements using CodeQL CLI
2.26.2 and `java-queries` 1.11.7. They are useful corpus comparisons, not a
whole-product ranking.

| Corpus | GraphCodeMap Round 27 profile | CodeQL `default` | CodeQL `security-extended` |
|---|---:|---:|---:|
| OWASP, same 1,698 category-scored cases | 902/0/0/796 | 776/292/126/504 | 902/471/0/325 |
| Juliet CWE-23, 444 bad + 444 good | 444/0/0/444 | 222/6/222/438 | 222/6/222/438 |

GraphCodeMap has the stronger result on these pinned rows. CodeQL has a much
broader query ecosystem, framework coverage, language coverage and mature
operational tooling. Neither table justifies a universal superiority claim.

## Current gaps and acceptance status

| Gate | Status |
|---|---|
| Exact L0/L1 site identity | Implemented and covered by focused contracts |
| CFG, loop, lambda, sanitizer and global-state characterizations formerly listed in Round 26 | Promoted to passing contracts |
| Hardened real-pair oracle | Complete: 3/3 vulnerable, 3/3 fixes clear |
| Semantic OWASP/Juliet score | Complete for the measured `INDEXER_VERSION=35` profile |
| Broad final project regression | Complete: 1,778 passed, 27 skipped |
| JDTLS overlay health | Complete: 732/732, 4,408 certain, zero warnings/errors |
| Broader independent Java evidence | Pending additional CVEs, frameworks and vulnerability families |
| Whole-product parity with CodeQL | Not claimed |

Canonical details live in [Security Benchmark](SECURITY_BENCHMARK.md), the
[Round 27 gate manifest](../evals/round27-java-gates-manifest.json), the
[real-pair result](../evals/java-real-pairs-round27-results.json), and the
historical log in [evals/RESULTS.md](../evals/RESULTS.md).
