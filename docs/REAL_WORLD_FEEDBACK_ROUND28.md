# Real-world feedback disposition — Round 28

This ledger turns the Aethros and Spring PetClinic reports into product
contracts. It exists so a future maintainer can distinguish a fixed invariant
from an attractive benchmark-only change, and so deferred work does not become
an invisible half-feature.

The two reports covered different failure planes:

- Aethros exercised Python/FastAPI, TypeScript, MCP and the first-run journey;
- Spring PetClinic exercised Java/JDTLS project import, canonical identity and
  degraded-run reporting.

State labels in this ledger are deliberately strict:

- **implemented** means code and a minimal regression exist in the worktree;
- **locally verified** adds focused and project tests;
- **externally explored** records a real repository run, but is not release
  evidence when the engine or artifact set is dirty/unversioned;
- **release eligible** requires a clean commit, pinned target and manifest with
  commands, versions, health, duration and artifact hashes;
- **deferred** names an acceptance criterion and remains open.

Round 28 is implemented and locally verified by 1,821 passing tests, 28 skips,
Ruff, configured mypy, build, Twine and an installed-wheel smoke. The
Aethros and PetClinic numbers below are external exploratory observations, not
yet `V4` release evidence. Round 27 remains the latest clean Java release
manifest.

## Disposition

| Feedback | General product invariant | Disposition | Executable or real-repo evidence |
|---|---|---|---|
| Fresh `graphcodemap[mcp]` installed MCP 2 and crashed | Optional dependencies must be bounded to the API the package imports | Implemented and locally verified: `mcp>=1.2,<2` | [Packaging regression](../tests/test_operational_feedback.py); an exploratory clean venv imported `FastMCP` and the server with MCP 1.29 |
| Quick start produced 0% `certain` for Python/TS | The recommended journey must lead to semantic edges and run `doctor` | Implemented and locally verified in [Getting Started](getting-started.md) | Recommended install is `[l1]`, indexing uses `--l1`, and the next step is `doctor` |
| `CODEGRAPH_TS_DIR` shape was unclear | Remediation must name the exact file layout expected | Implemented and locally verified | [Resolver diagnostics and discovery regressions](../tests/test_l1_ts_precision.py) require the package root containing `lib/typescript.js` |
| TypeScript under a monorepo child was undiscoverable | L1 discovery must remain contained while understanding workspaces/subprojects | Implemented and locally verified | [Contained discovery fixtures](../tests/test_l1_ts_precision.py) cover manifest/workspace/code relevance, deterministic precedence and generated/symlink exclusion |
| `Depends(get_membership)` looked dead | Functions passed to frameworks/callbacks must be represented as references without fabricating calls | Implemented, locally verified and externally explored | [Framework-reference regressions](../tests/test_python_framework_precision.py); exploratory Aethros run reports the real reference |
| Production `.get()`/`.execute()` resolved to unrelated business functions or nested test doubles | Name fallback must respect binding, scope, module and production/test priority | Implemented, locally verified and externally explored | [Adversarial fallback regressions](../tests/test_python_framework_precision.py); exploratory Aethros result was 406→8 for `model_policy.get` and zero production→`FakeDb.execute` |
| Aggregate output called inferred edges “trusted” | Summary nouns must match the confidence classes being counted | Implemented and locally verified | [Rendering regression](../tests/test_python_framework_precision.py) separates `certain` from inferred candidates |
| `json.loads` was an unsafe-deserialization sink | Security rules cannot infer an API family from a generic short member name | Implemented and locally verified | [Identity-aware rule regressions](../tests/test_python_framework_precision.py) prove real JSON safe, `pickle as json` unsafe, `json as pickle` safe and shadowing fail-closed |
| Portuguese `suggest` queries ranked poorly | The documented CLI language must not become query noise | Implemented and locally verified | [Portuguese and English query regressions](../tests/test_workflows.py) plus Unicode normalization |
| TypeScript test blocks collapsed to `describe#1.it#1` | Anonymous/test identities must be unique within lexical parents | Implemented and locally verified | [Nested/sibling callback-block regressions](../tests/test_l1_ts_precision.py) |
| Foreground `watch` was silent | Long-running foreground work must expose initial, ready and failure states | Implemented and locally verified | [CLI lifecycle and cleanup regressions](../tests/test_cli_watch_status.py) |
| `reaches --entry` failed although `taint --entry` trained that syntax | Equivalent entry-point commands should accept the same explicit spelling | Implemented and locally verified | [CLI alias regressions](../tests/test_cli_watch_status.py) cover positional and `--entry` forms |
| `describe` consumed the target repository's `.env` and transmitted code | Repository data is never consent to external transmission or spending | Implemented and locally verified | [L3 consent regressions](../tests/test_l3.py) prove target/cwd dotenv files are ignored by default; process credentials or explicit `CODEGRAPH_ALLOW_REPO_ENV=1` are required |
| Spring JDTLS returned zero after a fixed timeout and `doctor` suggested repeating it | Resolver readiness, timeout and diagnostics are persisted facts; partial is non-success | Implemented and locally verified; externally explored | [Operational unit regressions](../tests/test_operational_feedback.py) cover configurable budgets, exit status, sanitized persisted health and actionable `doctor`; PetClinic observation is recorded below |
| Missing JDTLS was reported as missing `java` | Discovery must identify the actual missing/incompatible component | Implemented and locally verified | [Operational regressions](../tests/test_operational_feedback.py) distinguish JDTLS home/layout, runtime absence and incompatible Java version |
| Canonical Java FQN from a stack trace did not resolve | Query identity must accept the language's canonical user-facing name | Implemented, locally verified and externally explored | [Canonical-query regressions](../tests/test_java_canonical_queries.py); real PetClinic FQN resolved without rewriting persisted identity and overload ambiguity is preserved |
| Every JDTLS run imports into a fresh temporary workspace | Performance state reuse must not compromise freshness, locking or tool-version isolation | Deferred P2 | Acceptance: repo-keyed workspace includes an exclusive lock, JDTLS/build-model version key, create/change/delete invalidation, crash recovery and two-process regression |
| Setup required several manual discoveries | Discover installed tools, fail with exact remediation, then offer explicit pinned setup | Partially resolved; setup helper deferred P2 | Acceptance: `codegraph setup <language>` is explicit, version/checksum pinned, proxy/offline aware and never executes an unverified download |
| Overview was polluted and could exceed a useful reading budget | Ranking must not amplify false fallback edges; output remains explicitly bounded | Root cause implemented; broader UX deferred P2/P3 | Acceptance: entry-point ranking beats generic hubs on a labeled repo-navigation oracle; CLI/docs language policy is explicit and tested |

## Real Spring PetClinic validation

The operational fix was tested against a clean clone at commit
`88e37c15cf6fc8490b01bc3e8e2c800cec1ac272` (49 Java files).

The first run used the project's Java 11 environment. GraphCodeMap indexed all
85 recognized files and, instead of reporting a healthy zero, returned a
non-success `partial` result with the Gradle/toolchain failure and readiness
timeout. After supplying a checksum-verified portable Temurin 17 project
toolchain, `gradlew classes` completed and JDTLS promoted 345 call edges.
The pass still ended `partial` because JDTLS emitted an internal
`Publish Diagnostics` NPE; GraphCodeMap preserved that error instead of
declaring the 345 successful definitions a complete run.

That second exploratory run exposed a more subtle health question: resolving 345 of 1,575
queried sites proves that JDTLS answered semantically, while unresolved library
or framework calls do not by themselves prove a readiness failure. The final
Round 28 gate must keep genuine request/process timeouts fail-closed while not
requiring every queried call to have an in-repository definition. This case is
covered by a minimal operational regression. The
[clean replay manifest](../evals/round28-real-world-feedback-manifest.json)
records the engine/target commits, exact command, JDTLS/JDK versions, duration,
health, rollback and artifact hashes. It is valid evidence of fail-closed
operation, but not V4 semantic evidence because the resolver run is partial.

## What this feedback changed in the language process

The reports demonstrated that extractor and benchmark quality are necessary but
not sufficient. Every next Tier-A language now needs four independent reviews:

1. semantic identity and flow on minimal adversarial fixtures;
2. a clean-install journey to the first `certain` edge;
3. a representative framework/build repository;
4. query/reporting review using the names and commands a real developer knows.

The complete reusable process is the
[Language Maturity Playbook](LANGUAGE_MATURITY_PLAYBOOK.md). Java remains the
reference profile because its semantic contracts are the deepest; these
operational failures are recorded precisely so the next language inherits the
lessons instead of only copying the resolver code.
