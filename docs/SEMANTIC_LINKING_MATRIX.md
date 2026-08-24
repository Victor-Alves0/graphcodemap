# Java/Python semantic linking matrix

Status: focused real-resolver contract, 2026-08-24.

This matrix answers a narrow question: when a callsite has ordinary static
language evidence, does GraphCodeMap publish the correct target and confidence?
It is a product contract fixture, not a claim about arbitrary frameworks or
whole-repository coverage.

## Results

| Call category | Python + Jedi | Java + JDTLS |
|---|---:|---:|
| Direct/local call | `certain` | `certain` |
| Imported cross-file call | `certain` | `certain` |
| Typed receiver | `certain` | `certain` |
| Inherited method | `certain` | `certain` |
| Interface/Protocol declaration | `certain` | `certain` |
| Overload selected by argument type | n/a | `certain` (`int`) |
| Method reference (`Type::method`) | n/a | `certain` |
| **Total** | **5/5** | **7/7** |

The Python contract uses the real in-process Jedi resolver. The Java contract
uses the configured JDTLS 1.60.0 installation with JDK 21 against a small Maven
project. The measured Java live run completed in 74.73 seconds on the local
Windows development machine; it is opt-in so ordinary unit tests do not pay the
language-server startup cost.

Fixtures live in `tests/fixtures/semantic_link_matrix/`. The executable contract
is `tests/test_semantic_link_matrix.py`:

```bash
pytest -q tests/test_semantic_link_matrix.py

# Include the real JDTLS integration after `codegraph setup java --install`:
CODEGRAPH_RUN_SEMANTIC_LIVE=1 pytest -q tests/test_semantic_link_matrix.py
```

## Coverage outcomes

`codegraph semantic-coverage` and the equivalent library/MCP APIs classify each
persisted callsite without inventing a runtime explanation:

- `l1_certain`: one local semantic target;
- `l1_multiple_targets`: semantic fan-out, retained as `inferred`;
- `l1_no_promotion_l0_unique` / `l1_no_unique_target`: L1 did not publish a
  target, but L0 retains an explicit fallback;
- `resolver_unavailable`: the applicable semantic tool was absent;
- `l1_no_local_target`: the resolver pass found no publishable in-repository
  callable target;
- `*_not_refined`: the current L0 revision has not run L1 yet.

`l1_no_local_target` deliberately combines cases static analysis cannot safely
distinguish from the persisted graph alone, such as external definitions,
callable parameters and dynamic/reflection-based dispatch.

The report includes two denominators. Total-callsite coverage answers “what
fraction of every syntactic call has a local semantic target?” Local-candidate
coverage answers “when L0 already persisted at least one in-repository target,
what fraction did L1 refine?” The second denominator excludes calls for which
the graph contains no local target; it does not relabel them as successful.

## Ordinary-repository canaries

These runs used unmodified local clones and separate temporary graph databases:

| Repository | Focus calls | L1 certain | Local candidates refined | L0 fallback | No local graph candidate | Index / refine |
|---|---:|---:|---:|---:|---:|---:|
| Flask `36e4a824` | 3,022 Python | 406 (13.4%) | 406/515 (78.8%) | 109 | 2,507 | 8.31s / 53.86s |
| Spring PetClinic `f182358d` | 1,529 Java | 340 (22.2%) | 340/351 (96.9%) | 11 | 1,178 | 2.04s / 60.65s |

The PetClinic replay exposed two missed method-reference sites
(`NamedEntity::getName` and `Visit::getDate`). They are now covered by a narrow
hybrid rule: only explicit `receiver::method` syntax with exactly one persisted
local target is promoted when JDTLS returns no definition. Ambiguous overloads
remain L0. The structured evidence is in
`evals/semantic-link-canaries-2026-08-24.json`.

## What this does not prove

- framework-generated or reflection-driven calls;
- Python monkey-patching, arbitrary decorator behavior or runtime protocols;
- Java dependency/classpath failures and generated sources across diverse build
  systems;
- framework/dynamic reason precision beyond “no local graph candidate”;
- acceptable JDTLS latency at portfolio scale.

The next G2 gate is request batching/cache, measured against these canaries,
followed by a second ordinary repository per focus language at product
acceptance.
