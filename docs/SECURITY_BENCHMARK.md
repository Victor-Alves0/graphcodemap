# Reproducible security benchmark

`evals/securitybench.py` runs or imports security tools into one versioned JSON
contract. It exists to prevent a tool from receiving credit because it ran on a
different commit, reported another vulnerability category, stopped early, or
could not parse part of the target.

## Contract

Every report records:

- tool, version and adapter version;
- target Git commit, dirty state and remote;
- command, status, exit code, duration and peak RSS when measurable;
- normalized rule, category, CWE, severity, source, sink and evidence;
- warnings, parser failures, timeouts and other errors.

The machine-readable contract is
[`evals/security-report.schema.json`](../evals/security-report.schema.json).
Statuses are `complete`, `partial`, `failed` and `unavailable`. An unavailable
tool is never represented as a successful scan with zero findings.

## Commands

Inspect the local toolchain:

```powershell
python evals/securitybench.py doctor
```

Run GraphCodeMap:

```powershell
python evals/securitybench.py graphcodemap D:\repo --refine `
  --out .codegraph\evals\repo-graphcodemap.json
```

Run OpenGrep with a local, commit-pinned ruleset:

```powershell
python evals/securitybench.py run-opengrep --root D:\repo `
  --config D:\rules\security.yml --timeout-s 300 `
  --out .codegraph\evals\repo-opengrep.json
```

Run OpenTaint with a pinned ruleset:

```powershell
python evals/securitybench.py run-opentaint --root D:\repo `
  --config D:\opentaint\rules\ruleset\java --rule-id sql-injection `
  --timeout-s 900 --out .codegraph\evals\repo-opentaint.json
```

Run CodeQL against a pre-created database and explicit query suite:

```powershell
python evals/securitybench.py run-codeql --root D:\repo `
  --database D:\codeql-db --queries codeql\java-queries:codeql-suites\java-security-extended.qls `
  --out .codegraph\evals\repo-codeql.json
```

Existing SARIF from CodeQL/OpenTaint and OpenGrep JSON can be normalized without
rerunning the tool:

```powershell
python evals/securitybench.py sarif result.sarif --root D:\repo `
  --tool-name codeql --out normalized.json
python evals/securitybench.py opengrep result.json --root D:\repo `
  --out normalized.json
```

Compare two reports from the same commit:

```powershell
python evals/securitybench.py compare left.json right.json --out overlap.json
```

Score a normalized report against the independent Juliet CWE-23 holdout:

```powershell
python evals/julietbench.py D:\corpora\juliet-java-1.3\Java `
  --report juliet-graphcodemap.json --json juliet-score.json
```

The comparison reports both exact sink overlap and file+category overlap.
Static analyzers legitimately disagree whether the primary line is a tainted
assignment or the sink call one line later; those two notions must not be
silently conflated.

## Fairness rules

1. Target commits must match and dirty targets must be disclosed.
2. Tool and ruleset commits/versions must be pinned in the experiment notes.
3. Remote `auto` rules are forbidden in published comparisons.
4. A hit on labeled data requires the same file **and vulnerability category**.
5. `partial`, `failed` and `unavailable` runs are never ranked as complete.
6. Findings on vulnerable applications demonstrate utility, not precision;
   precision/FPR require safe negatives or fixed revisions.
7. Memory uses the sampled process tree. Imported reports keep memory as
   unavailable rather than borrowing unverifiable numbers.

## First smoke comparison

On DVNA commit `9ba473add536`, with OpenGrep 1.22.0 and the NodeJsScan rules
vendored in the OpenGrep repository at commit `3bd8e95fea89`:

| tool | status | findings/categories | time | peak RSS |
|---|---|---:|---:|---:|
| GraphCodeMap 0.1.0 | complete | 6 | 3.16 s | 48.63 MB |
| OpenGrep 1.22.0 | partial | 4 | 9.51 s | 259.36 MB |

The four OpenGrep file+category pairs were also found by GraphCodeMap. Its two
additional categories were command injection and code injection. This is not a
precision claim: DVNA lacks exhaustive safe negatives, and OpenGrep reported a
parser error plus a Windows console traceback, so the run is correctly marked
partial.

## First labeled competitive run

OWASP Benchmark v1.2 commit `f51bf36b8891`, restricted to the 1,698 cases in
the seven source-to-sink categories and scored only when **file and category**
both agree with the ground truth:

| tool / pinned configuration | status | TP / FP / FN / TN | precision | recall | FPR | score | time | peak RSS |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| GraphCodeMap 0.1.0 (Round 26 final warm rescore¹) | complete | 868 / 92 / 34 / 704 | 90.4% | 96.2% | 11.6% | **+0.847** | 73.258 s | 640.51 MB |
| OpenTaint `dev-7f7da63` + 323 selected flow rules | complete | 819 / 454 / 83 / 342 | 64.3% | 90.8% | 57.0% | **+0.338** | 240.34 s | 5,120 MB |
| OpenGrep 1.22.0 + repository `perf/r2c-rules/java.yml` (28 rules) | complete | 74 / 64 / 828 / 732 | 53.6% | 8.2% | 8.0% | **+0.002** | 59.13 s | 547 MB |
| CodeQL CLI 2.26.2 / `java-queries` 1.11.7 `default` (80 queries) | complete | 776 / 292 / 126 / 504 | 72.7% | 86.0% | 36.7% | **+0.494** | - | - |
| CodeQL CLI 2.26.2 / `java-queries` 1.11.7 `security-extended` (124 queries) | complete | 902 / 471 / 0 / 325 | 65.7% | 100% | 59.2% | **+0.408** | - | - |

¹ The final time/RSS is a warm rescore. The refine-inclusive phase built the
same database/graph in 1,549.856 s / 1,680.26 MB before two query-only rule
changes; the final 868 / 92 / 34 / 704 was then measured by rescoring it in
73.258 s / 640.51 MB. Final combined end-to-end wall time was not measured, so
the precursor cost is not presented as final-run performance.

On this pinned seven-category matrix, GraphCodeMap finds 49 more vulnerable
cases than OpenTaint and reports 362 fewer safe cases. Runtime and memory rows
are configuration-specific; GraphCodeMap's row is explicitly a warm rescore,
not end-to-end performance. These are not direct whole-product comparisons.
The OpenGrep row measures that exact pinned
28-rule fixture, **not OpenGrep's full registry or an equivalently broad Java
suite**, so it is useful for adapter reproducibility, not a product ranking.

The CodeQL rows use the official query suites and an explicit mapping from
query IDs to the benchmark categories. The `default` suite covers six of the
seven scored categories and has no trust-boundary query in this mapping;
`security-extended` covers all seven, reaches maximum recall, and also reports
substantially more safe cases. These rows compare detection on this pinned
matrix. They do **not** establish that GraphCodeMap is a better overall product
than CodeQL, whose language coverage, framework models, query ecosystem and
operational integrations are outside this experiment.

The run also exposed type information lost by GraphCodeMap's name-only catalog.
`String.getBytes`, `MessageDigest.update` and `String.valueOf` had inherited
type-specific CodeQL roles and produced hundreds of redundant candidates. The
Java catalog now excludes those ambiguous names. Together with complete Java
sink categorization, findings fell from 2,522 to 2,085 and runtime from 94.68 s
to about 43 s on the warm index; category-correct recall reached 77.2% because
path traversal, trust-boundary, response-format, process-command and XPath
compile sinks no longer appeared under a wrong category. A cold isolated run,
including indexing 5,603 files, took 113.66 s and produced the exact same 2,085
findings.

## Java semantic A/B

JDTLS 1.60.0 on Oracle JDK 21.0.11 was run against the same Maven checkout and
promoted 8,838 call edges with zero resolver errors in the Round 26 snapshot.
The first taint pass in the earlier semantic A/B incorrectly used
"certain call target" as if it also meant "certain return flow": recall fell
to 57.9%. Java interface dispatch, receiver state and field flow make that
implication unsound. Target-only return pruning remains disabled for Java;
structurally proven local/receiver summaries and structural L1 promotion remain
enabled.

After the guard, the initial clean-L0 and JDTLS indexes produced identical
normalized findings. The next precision step re-enabled Java return summaries
only for call-free, alias-free control flow that the CFG can fold. Round 20
added a closed abstract domain for locally-created Java lists using only
`add`, `remove` and `get` with constant indices; arbitrary collection aliasing,
reflection, dispatch and context-specific sanitizers remain conservative.
This removed 189 false positives with **zero TP loss**. The same change
preserved the exact 23 normalized findings across pygoat, dvpwa, DVNA, NodeGoat
and nodejs-goof before/after their available L1 resolvers.

The constant-list domain removed another 66 false positives while all 65
equivalent vulnerable list wrappers remained tainted. Total FPR fell from
25.5% to 17.2%, with TP and recall unchanged. The five real-application scans
again retained their 10 / 1 / 6 / 4 / 2 finding counts and categories.

Round 21 added the general Java enhanced-for binding `element <- iterable`.
Round 22 extended the closed collection domain to local HashMap, LinkedHashMap
and TreeMap operations with literal keys. Alias, escape, dynamic keys, unknown
methods, conditional mutation, reinitialization and loops fail closed. Together
these changes reached 868 TP / 92 FP / 34 FN / 704 TN; path traversal reached
133/133 vulnerable cases. Two extra adversarial tests ensure a conditional or
loop-only safe List overwrite cannot be linearized into a false proof.

## Round 26 verified Java gate

Round 26 reran the complete Java gate after hardening L0 overload ownership and
receiver typing, L1 UTF-8/UTF-16 spans and same-line symbol identity,
nanosecond incremental freshness and L1 provenance invalidation, and Java
receiver heap summaries. Heap kills now require closed dispatch and no
receiver alias/escape; dirty subfields and ambiguous fan-out are unioned
conservatively. Uninvoked lambdas are deferred instead of being inlined into
their enclosing method.

The full OWASP semantic run scanned 2,770 files, promoted 8,838 edges and
reported zero resolver errors. It took 1,549.856 s with a 1,680.26 MB peak
process-tree RSS. After adding semantic argument roles for
`Runtime.exec(command, envp, dir)` and treating the literal
`System.getProperty("user.dir")` as trusted, the existing database was rescored
without another refine: 1,942 findings, 378 wrong-category findings, 73.258 s
and 640.51 MB. The scored result remained 868 / 92 / 34 / 704. In particular,
the working-directory role removed 36 command-injection false positives while
preserving command/environment flows and all TP/FN counts.

The reproducibility details are included/versioned in Round 26 in the
[Round 26 external-gates manifest](../evals/round26-external-gates-manifest.json)
and mirrored here. The manifest records source commit
`da5b1610f15bb39ea4f13c0853a67fe105bbd83a`, source-diff SHA-256
`48361e2eaaad50748a4a697dee677a5d7264b538124e2d2f4367d018b0a9490f`,
JDTLS archive SHA-256
`e94c303d8198f977930803582738771fd18c52c5492878410bf222b1aa81ef1d`,
final OWASP report SHA-256
`d5bc5ee056a350fb17efdc901ba04a8a403a04c19a11092f7d3c1fc98496f111`
and score SHA-256
`146b279755db707d9a7d1f33683215bf09dae3a8a911a07029613ef9dc9d71e1`.
The local quality gate closed in 158.60 s at 1,576 passed, 25 skipped and one
strict expected failure, with 82% total branch-aware coverage.

## Independent Java holdout

NIST SARD [Juliet Java 1.3 suite #111](https://samate.nist.gov/SARD/test-suites/111)
is kept outside the development corpus.
The pinned ZIP SHA-256 is
`d985f4177c2bcd7b03455a05c1c8f2e755f55c9eb250accd052f05f877347e60`.
`evals/julietbench.py` scores CWE-23 using 444 vulnerable testcases and their
444 co-located `good()` companions:

| tool / corpus | TP / FP / FN / TN | precision | recall | FPR | score |
|---|---:|---:|---:|---:|---:|
| GraphCodeMap 0.1.0 / Juliet CWE-23 (Round 26) | 308 / 0 / 136 / 444 | 100% | 69.4% | 0% | +0.694 |
| CodeQL `default` / manual database | 222 / 6 / 222 / 438 | 97.37% | 50.0% | 1.35% | +0.486 |
| CodeQL `security-extended` / manual database | 222 / 6 / 222 / 438 | 97.37% | 50.0% | 1.35% | +0.486 |

GraphCodeMap crosses the original recall gate after adding type-aware Java
sources, concrete `new Type().wrapper()` resolution, sanitizer-aware sources
nested inside assignments and Round 26 heap/interprocedural transport. Recall
rose from 54.5% (242 TP) to 69.4% (308 TP) without an FP. Type qualification is
intentionally fail-closed:
standard input readers such as `BufferedReader.readLine` are sources, while an
unrelated domain object's same-named method is not. The remaining
`PropertiesFile` family is not promoted merely because another class has a
homonymous source wrapper.

The Round 26 Juliet report and score SHA-256 values are respectively
`1a9206f37fca08eb9857a7d56a4538664a950b79795fd4ab905c8740c0cd2730`
and `4f013deaea6566f2099fbe016cf44aa7bc1e03cd2750e9d74f43aebf20196d5b`.
The CodeQL database was built manually from 732 source files and 744 compiled
classes. Both official suites produced the same score, and the finding
signatures were identical to the earlier no-build database. The shared corpus,
fixed manifest and file+bad/good endpoint scoring make the rows comparable;
the small difference does not imply whole-product superiority in either
direction.

## Real vulnerable/fixed Java holdout (Round 26 historical)

Three published Java vulnerabilities were scanned at both the vulnerable and
fixed revisions with isolated L0 databases. This is a deliberately small
behavioral holdout: the vulnerable revision tests discovery, while disappearance
at the fixed revision tests whether the engine understands the security guard.

| pair | vulnerable flow | baseline cleared | current cleared | current outcome |
|---|---:|---:|---:|---|
| OpenRefine CVE-2024-49760 | yes | no | no | 2 vulnerable matches → 3 fixed; trusted inherited module base is still unproved |
| FitNesse CVE-2024-42499 | **yes** | no | **yes** | **2 vulnerable matches → 0 fixed** after receiver heap summaries |
| openHAB CVE-2024-42468 | yes | no | **yes** | **3 vulnerable matches → 0 fixed** |
| **aggregate** | **3 / 3** | **0 / 3** | **2 / 3** | **all vulnerable flows found; two patches distinguished** |

Round 24 recognizes narrow, rejecting Java path-containment guards using
normalized `Path.startsWith` or canonical paths plus `File.separator`.
The base must be untainted and the rejecting arm must definitely terminate;
text-prefix checks, use before validation, aliases, nested constructors and
unproved canonical bases fail closed. This clears the openHAB patch while the
vulnerable revision retains all three oracle matches. OpenRefine remains open:
its base comes from a map field inherited from a dependency, so L0 cannot prove
that a tainted lookup key does not taint the selected module path.

Round 25 closes the FitNesse discovery miss with qualified
`fitnesse.http.Request` sources, source wrappers resolved by concrete symbol,
and call-order-sensitive transport of direct `this` fields into a later helper
on the same receiver. Homonymous request types, other receivers, local field
shadows and calls before the write remain clean. Callee-written field effects
are deliberately deferred to an explicit heap summary.

Round 26 exports receiver dirty/clean effects through proven same-receiver
calls, which clears the fixed FitNesse revision without hiding either
vulnerable match. The final change is neutral on the OWASP gate at
868 / 92 / 34 / 704, while Juliet improves to 308 / 0 / 136 / 444. The
real-pair aggregate remains 3/3 vulnerable flows detected and advances from
1/3 to 2/3 patches distinguished. OpenRefine remains deliberately open because
the trusted base is inherited dependency state that L0 cannot prove.

The remaining Round 26 Java risks were explicit: 34 OWASP false negatives and 92 false
positives, invoked lambda/callback units, wider virtual fan-out and incomplete
type-hierarchy closure. A process-local tainted
`System.setProperty("user.dir", value)` is not propagated globally; its strict
characterization remains the single expected failure rather than weakening the
trusted-literal rule.

## Round 27 Java semantic profile

Round 27 closes the previously published Java semantic characterizations and
the OpenRefine oracle. These values supersede Round 26 as semantic evidence;
the Round 26 sections above remain unchanged as historical reproduction data.

| gate | GraphCodeMap Round 27 | precision | recall | FPR | final pre-report time |
|---|---:|---:|---:|---:|---:|
| OWASP Benchmark v1.2 | 902 / 0 / 0 / 796 | 100% | 100% | 0% | 280.036 s |
| NIST Juliet Java 1.3 CWE-23 | 444 / 0 / 0 / 444 | 100% | 100% | 0% | 23.095 s |

The hardened real-pair scorer also reports:

| pair | vulnerable → fixed | outcome |
|---|---:|---|
| OpenRefine CVE-2024-49760 | 1 → 0 | exact line-83 `getParameterValues("lang")` provenance retained |
| FitNesse CVE-2024-42499 | 2 → 0 | preserved |
| openHAB CVE-2024-42468 | 3 → 0 | preserved |
| **aggregate** | **3/3 detected; 3/3 clear** | **all pinned patch oracles distinguished** |

The final local project gate has **1,778 passing tests and 27 skips**, with no
xfail. Focused P1/contract regressions also pass inside that project gate.

### Operational health validation

The first fresh runs exposed a false post-shutdown handshake result and Juliet
project-root diagnostics. The final validation fixed both instead of filtering
them: JDTLS health drains late diagnostics, and the isolated Juliet overlay uses
source root `src`, its four official bundled JARs and the official `antbuild`
exclusion. It completed 732/732 files with **4,408** `certain` promotions,
`status=complete` and zero warnings/errors.

### Existing CodeQL comparison, unchanged

The versioned CodeQL CLI 2.26.2 / `java-queries` 1.11.7 rows remain the fair
comparison on these pinned corpora:

| corpus | GraphCodeMap Round 27 | CodeQL `default` | CodeQL `security-extended` |
|---|---:|---:|---:|
| OWASP | 902/0/0/796 | 776/292/126/504 | 902/471/0/325 |
| Juliet CWE-23 | 444/0/0/444 | 222/6/222/438 | 222/6/222/438 |

GraphCodeMap is stronger on these measured rows. CodeQL remains broader in
languages, queries, framework models, integrations and operational maturity;
this document does not claim whole-product superiority. Round 27 real-pair
timings and byte hashes are versioned in
[`evals/java-real-pairs-round27-results.json`](../evals/java-real-pairs-round27-results.json).
Engine/target identities, package hashes, OWASP/Juliet report hashes and the
overlay-health record are pinned in the
[`Round 27 Java gate manifest`](../evals/round27-java-gates-manifest.json).
