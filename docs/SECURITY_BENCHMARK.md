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
| GraphCodeMap 0.1.0 | complete | 745 / 203 / 157 / 593 | 78.6% | 82.6% | 25.5% | **+0.571** | 40.83 s | 551 MB |
| OpenTaint `dev-7f7da63` + 323 selected flow rules | complete | 819 / 454 / 83 / 342 | 64.3% | 90.8% | 57.0% | **+0.338** | 240.34 s | 5,120 MB |
| OpenGrep 1.22.0 + repository `perf/r2c-rules/java.yml` (28 rules) | complete | 74 / 64 / 828 / 732 | 53.6% | 8.2% | 8.0% | **+0.002** | 59.13 s | 547 MB |
| CodeQL | unavailable | - | - | - | - | - | - | - |

GraphCodeMap now leads this seven-category matrix: it finds 74 fewer vulnerable
cases than OpenTaint, but reports 251 fewer safe cases, is 5.9x faster and uses
about one ninth of its peak memory. The OpenGrep row measures that exact pinned
28-rule fixture, **not OpenGrep's full registry or an equivalently broad Java
suite**, so it is useful for adapter reproducibility, not a product ranking.
CodeQL was not installed and is explicitly recorded as `unavailable`; no score
was fabricated.

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

JDTLS 1.60.0 was run against the same Maven checkout and promoted 12,937 call
edges with zero resolver errors. The first taint pass incorrectly used
"certain call target" as if it also meant "certain return flow": recall fell
to 57.9%. Java interface dispatch, receiver state and field flow make that
implication unsound. Return-summary pruning is therefore disabled for Java
until it has a dispatch-aware oracle; structural L1 promotion remains enabled.

After the guard, the initial clean-L0 and JDTLS indexes produced identical
normalized findings. The next precision step re-enabled Java return summaries
only for call-free, alias-free control flow that the CFG can fold; collection,
reflection, dispatch and context-specific sanitizers remain conservative.
This removed 189 false positives with **zero TP loss**. The same change
preserved the exact 23 normalized findings across pygoat, dvpwa, DVNA, NodeGoat
and nodejs-goof before/after their available L1 resolvers.
