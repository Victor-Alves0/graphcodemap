# Product maturity

GraphCodeMap supports many languages, but **support does not mean parity**. This
page defines what each level promises and is the source of truth for deciding
whether a language or subsystem is ready.

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

## Current snapshot

Generated from `codegraph capabilities` after the August 2026 consolidation:

- 46 recognized languages;
- 23 dedicated extractors;
- dataflow for 19 of 37 applicable recognized languages;
- flow-sensitive analysis for 18 languages;
- 19 L1 adapters: 11 with a live smoke test and 5 with real-repository evidence;
- 5 languages with external security evidence.

The strongest current language profiles are Python, JavaScript, PHP and Java.
Java's Round 27 profile is operationally eligible: its corrected Juliet JDTLS
overlay completed with 732/732 files, 4,408 `certain` promotions and zero
warnings/errors. Ruby has
real-application security evidence but its L1 adapter is only
wired. Go has real-repository L1 evidence but still needs a labeled security
corpus. The remaining languages must not be described as equivalent to those
profiles.

Run `codegraph capabilities` and locate the language row for the current human-
readable view.

## Subsystem audit

| Subsystem | Current state | Exit criterion |
|---|---|---|
| Graph and freshness | Strong incremental core; policy-change cleanup now covered | Multiprocess stress and no known stale-read path |
| L0 extraction | Broad and well unit-tested | Real call-edge oracles for every Tier-A language |
| L1 resolution | Broadly wired; Java now has locked warm workspace and clean PetClinic evidence | Live integration plus a multi-repository portfolio for every Tier A language |
| Dataflow and taint | Round 27 Java: OWASP 902/0/0/796, Juliet CWE-23 444/0/0/444; all three pinned Java vulnerabilities are found and all three fixes clear | Add independent CVEs/frameworks rather than treating two perfect corpora as universal proof |
| CLI, library and MCP | Main surfaces implemented | Versioned response schemas and parity contract tests |
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
local gate is **1,852 passed, 28 skipped**. The only final Java promotion gate
is now the pinned four-repository Maven/Gradle/multi-module/Spring portfolio.

## Rule for adding scope

A new language or major feature must declare a target maturity level, an oracle,
a vulnerable and a safe test where security applies, a before/after measurement,
and its known limitations. No additional generic language is a priority while a
Tier-A release gate remains open. This keeps breadth from masking unfinished
depth. The required workflow and evidence portfolio are defined in the
[language maturity playbook](LANGUAGE_MATURITY_PLAYBOOK.md).
