# Product maturity

The [Product Contract](PRODUCT_CONTRACT.md) is the canonical source of truth.
GraphCodeMap contains many extractors, but phase one focuses only on Java and
Python. Implementation presence does not mean product support or parity.

## Language levels

| Level | Promise |
|---|---|
| Recognized | The grammar is detected and generic L0 symbols may be produced. |
| Structural | A dedicated extractor covers the language's main declarations and relationships. |
| Engine | Structural extraction plus the applicable dataflow/taint machinery exists. External precision has not yet been demonstrated. |
| Security-validated | Security behavior is exercised against a labeled corpus or a vulnerable real application. |
| Semantic-validated | Security-validated and L1 resolution has been exercised on a real repository. |

L1 has its own evidence ladder: `wired` means an adapter exists,
`live-smoke` means the real server has passed an integration fixture, and
`real-repo` means it has been measured on an external repository. A wired
resolver is not advertised as proven semantic resolution.

Security and L1 evidence are independent axes: useful corpus work may precede
an operational resolver, and real-repository L1 may precede a labeled security
corpus. The product labels above are promotion gates, not a mandatory
chronological experiment order. The precise `E0`–`E4` and `V0`–`V4` mapping is
defined in the [language maturity playbook](LANGUAGE_MATURITY_PLAYBOOK.md#evidence-axes-and-promotion-levels).

## Current reset snapshot

Generated from code inspection, focused contracts and black-box dogfooding on
2026-08-24:

- 46 recognized languages;
- 23 dedicated extractors;
- dataflow for 19 of 37 applicable recognized languages;
- flow-sensitive analysis for 18 languages;
- 19 L1 adapters: 11 with a live smoke test and 5 with real-repository evidence;
- 5 languages with external security evidence.

Those counts describe code breadth, not a completed product. Java and Python
both have dedicated extraction, optional semantic linking and on-demand
dataflow. Neither yet has the required persistent graph of parameters, locals,
reads, writes and value flows. Java has stronger bounded security evidence;
Python has simpler semantic setup through Jedi. Neither is promoted as a
complete phase-one language until G0–G5 of the Product Contract pass.

Run `codegraph capabilities` and locate the language row for the current human-
readable view.

## Subsystem audit

| Subsystem | Current state | Exit criterion |
|---|---|---|
| Graph and freshness | Files/declarations/basic calls and incremental relinking work; same-size/same-mtime edits can evade the current fast path | Required nodes/edges, explicit skips, strict freshness and stable identities |
| L0 extraction | Useful Java/Python declarations/imports/calls; parameters, locals and reads/writes absent | Shared Java/Python golden structural graph |
| L1 resolution | Jedi/JDTLS materially improve impact; lifecycle/readiness is not yet atomic to readers | Explicit ready/running/partial state and real-repo canaries |
| Dataflow and taint | Flow-sensitive engine exists on demand; Java has bounded benchmark evidence | Persist def-use/flow graph, then validate Java and Python separately |
| CLI, library and MCP | Main surfaces exist; MCP doctor crash fixed during reset, other readiness/coverage gaps remain | Installed acceptance journey and equivalent result sets |
| Visualization | Functional; script-breakout regression covered | Large-graph performance budget and browser smoke suite |
| L3 descriptions | Experimental and optional | Provider-neutral quality/cost evaluation; not a v0.2 blocker |
| Delivery | Ruff, progressive mypy, branch coverage >=75%, a six-platform test matrix and built-wheel smoke checks gate releases | Run the complete gate on the release branch and version the public response schemas |
| Competitive evaluation | Category-correct OWASP compares pinned GraphCodeMap/OpenTaint/OpenGrep and both official CodeQL Java suites; Juliet uses a manually compiled, validated CodeQL database | Add independent labeled corpora and equivalent official suites for the remaining Tier-A languages |

Round 26's complete local gate ran in 158.60 s: 1,576 passed, 25 skipped, one
strict expected failure and 82% total branch-aware coverage. The expected
failure records the missing global propagation from a tainted
`System.setProperty("user.dir", value)`; it is not counted as a supported
behavior. The verified external snapshot is 868 / 92 / 34 / 704 on OWASP,
308 / 0 / 136 / 444 on Juliet, and 3/3 vulnerable plus 2/3 fixed Java real-pair
outcomes. Exact environment, resource use and SHA-256 values are in the
[Round 26 manifest](../evals/round26-external-gates-manifest.json) and summarized
in [Security Benchmark](SECURITY_BENCHMARK.md#round-26-verified-java-gate).

Round 27 supersedes Round 26 for Java: 902/0/0/796 on OWASP, 444/0/0/444 on
Juliet and 3/3 vulnerable plus 3/3 fixed real-pair outcomes. The final local
project gate is **1,778 passed, 27 skipped**. The corrected Juliet overlay
distinguished shutdown bookkeeping from real diagnostics and completed with
732/732 files, 4,408 `certain` promotions and no warnings/errors. Final
pre-report analysis took 280.036 s on OWASP and 23.095 s on Juliet. The
[Round 27 manifest](../evals/round27-java-gates-manifest.json) pins the engine,
targets, package/report hashes and overlay-health record.

Round 29 closes Java's three operational gates without changing the Round 27
semantic scores. `INDEXER_VERSION=37` persists declared-package FQNs with legacy
bridges. PetClinic completes 345 `certain` edges twice with zero warning/error;
the locked persistent JDTLS workspace reduces 73.114 s cold to 60.829 s warm
with an identical edge hash. The local gate is **1,845 passed, 28 skipped** and
the [Round 29 manifest](../evals/round29-java-operational-gates-manifest.json)
records the bounded evidence.

Round 30 closes the penultimate Java gate with conservative Spring semantics.
Explicit controller/bean/callback declarations are navigable without becoming
fabricated call edges, MVC-bound parameters seed taint, and typed
injection/repository dispatch fails closed when the target is undeclared. The
historical Round 30 gate was **1,852 passed, 28 skipped**. The only remaining
Java promotion gate at that checkpoint was the pinned four-repository
Maven/Gradle/multi-module/Spring portfolio.

The attempted Round 31 portfolio remains diagnostic evidence, not a promoted
product gate. It found genuine parser, Maven-root, JDTLS isolation and target
mapping defects, but the final same-commit replay and auditable sample artifact
were not completed. The product reset also found core graph gaps that portfolio
call-edge sampling did not cover. Do not describe Round 31 as closing Java.

## Rule for adding scope

A new language or major feature must declare a target maturity level, an oracle,
a vulnerable and a safe test where security applies, a before/after measurement,
and its known limitations. No additional generic language is a priority while a
Tier-A release gate remains open. This keeps breadth from masking unfinished
depth. The required workflow and evidence portfolio are defined in the
[language maturity playbook](LANGUAGE_MATURITY_PLAYBOOK.md).
