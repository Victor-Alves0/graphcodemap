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

## Current snapshot

Generated from `codegraph capabilities` after the August 2026 consolidation:

- 46 recognized languages;
- 23 dedicated extractors;
- dataflow for 19 of 37 applicable recognized languages;
- flow-sensitive analysis for 18 languages;
- 19 L1 adapters: 11 with a live smoke test and 4 with real-repository evidence;
- 5 languages with external security evidence.

The strongest current language profiles are Python, JavaScript and PHP.
Java now has labeled-benchmark security evidence plus real Maven/JDTLS evidence;
Gradle remains unproven. Ruby has real-application security evidence but its L1 adapter is only
wired. Go has real-repository L1 evidence but still needs a labeled security
corpus. The remaining languages must not be described as equivalent to those
profiles.

Run `codegraph capabilities [language]` for the machine-readable current view.

## Subsystem audit

| Subsystem | Current state | Exit criterion |
|---|---|---|
| Graph and freshness | Strong incremental core; policy-change cleanup now covered | Multiprocess stress and no known stale-read path |
| L0 extraction | Broad and well unit-tested | Real call-edge oracles for every Tier-A language |
| L1 resolution | Broadly wired, unevenly proven | Live integration plus real-repo evidence for Tier A |
| Dataflow and taint | Broad engine, narrow external evidence | Category-correct benchmark and vulnerable/fixed comparisons |
| CLI, library and MCP | Main surfaces implemented | Versioned response schemas and parity contract tests |
| Visualization | Functional; script-breakout regression covered | Large-graph performance budget and browser smoke suite |
| L3 descriptions | Experimental and optional | Provider-neutral quality/cost evaluation; not a v0.2 blocker |
| Delivery | Large test suite, limited quality gates | Lint, progressive typing, coverage and release/package checks in CI |
| Competitive evaluation | Category-correct OWASP runs now compare GraphCodeMap/OpenTaint/OpenGrep on one commit; CodeQL and equivalent-width suites remain incomplete | Same commits, categories and complete equivalent suites across Tier A |

## Rule for adding scope

A new language or major feature must declare a target maturity level, an oracle,
a vulnerable and a safe test where security applies, a before/after measurement,
and its known limitations. No additional generic language is a priority while a
Tier-A release gate remains open. This keeps breadth from masking unfinished
depth.
