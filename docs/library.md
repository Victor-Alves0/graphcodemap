# Library / Host API

Beyond the CLI and the MCP server, GraphCodeMap is designed to be **embedded** by
a service — a code-review bot, an IDE backend, a multi-tenant platform. The
importable package is `codegraph` (the distribution is `graphcodemap`, like
`pillow` → `PIL`).

## The basics

```python
from codegraph import CodeGraph

cg = CodeGraph(".")          # repo root (or a Path)
cg.index()                   # build/update .codegraph/graph.db

rows, env = cg.find_symbol("validate")
for r in rows:
    print(r["fqn"], r["kind"], f'{r["path"]}:{r["start_line"]}')

cg.close()                   # or use CodeGraph(...) as a context manager
```

`CodeGraph` is a thin facade over the indexer and the query engine. Every query
method returns its structured result **plus an `Envelope`** carrying the
freshness/completeness signals — the same facts the CLI renders as `⚠` lines.

```python
with CodeGraph(repo) as cg:
    cg.index()
    sym, rows, env = cg.callers("auth.TokenService.validate")
    if not env.fresh:
        ...  # the index was repaired mid-query; act on fresh data
```

## Query surface

The facade mirrors the CLI. Selected methods:

```python
cg.find_symbol(query, kind=None, limit=10)
cg.symbol_info(selector)
cg.references(selector, kind=None)
cg.callers(selector, depth=1)
cg.callees(selector, depth=1)
cg.impact(selector, depth=3)
cg.ego_graph(selector)
cg.change_impact(target, depth=3)          # target: paths or a diff
cg.find_affected_modules(target, depth=3)
cg.find_related_tests(selector, depth=3)
cg.explain_symbol(selector)
cg.suggest_files_to_read(task, limit=8)
cg.overview(scope=None, token_budget=2000)
cg.communities(limit=20, min_size=3)
cg.repository_tree(path="", depth=4, refresh=True)
cg.graph_history(limit=20, git_commit=None)
cg.l1_status()                              # semantic snapshot lifecycle
cg.semantic_coverage(sample_limit=20)       # callsite outcomes/reasons
cg.data_flow(selector, depth=2)
cg.taint(scope=None, entry=None, depth=4)
cg.reaches(selector, sink="http", via=None, depth=8)
cg.visualize(mode=None, symbol=None, depth=3, ...)   # returns (data, envelope)
cg.describe(target, refresh=False, llm=None)
cg.stats()
cg.doctor(failed_limit=20)
cg.compact()                                # rebuild + reclaim (VACUUM)
```

See [CLI Reference](cli.md) for the semantics of each — the arguments line up.

## Closing the edit loop: `index()["changes"]`

The index tells you what changed on each run, so a host can react to edits
*without diffing git*:

```python
result  = cg.index(exclude=["vendor/", "*.min.css"])
changes = result["changes"]

for c in changes["signature_changed"]:
    print(c["fqn"], c["before"], "->", c["after"])
    cg.impact(c["fqn"])            # who breaks if this ships?

for fqn in changes["added"]:   ...
for fqn in changes["removed"]: ...
```

`changes` contains `added`, `removed`, and `signature_changed` (each with
`before`/`after`), plus exact `counts` and a `truncated` flag.

## Exclusion policy: `exclude=`

```python
cg.index(exclude=["vendor/", "*.min.css", "generated/"])
```

gitignore-style patterns, stored **in the index** — the host's policy without
writing a `.codegraphignore` into the user's working copy. `None` keeps the saved
policy; `[]` clears it. (`.gitignore` and `.codegraphignore` in the repo are
honored too.)

## L3 provider

L3 descriptions are provider-agnostic. Inject the credential — never read from a
global environment — which keeps a multi-tenant host safe and cost attributable:

```python
cg = CodeGraph(repo, llm=user_api_key)            # constructor default
cg.describe("auth.TokenService", llm=other_key)   # or per call
```

`llm` accepts a callable `(system, user) -> str` or an API key. The provider
exposes `.usage`, so token cost stays attributable per tenant. Without a provider,
`describe` reports that L3 is disabled and the rest of the system is unaffected.

## Multi-tenant & concurrency notes

- **One `CodeGraph`/`QueryEngine` instance is single-threaded.** Share it only
  under external serialization (the MCP server does this with a lock), or use one
  instance per thread. Writes retry on `database is locked`.
- **No global env mutation.** Credentials and exclusion policy are passed in, not
  read from the process environment — safe for a host serving many users.
- **`doctor()` never leaks the absolute server path** — it returns `root_name`,
  not the full filesystem location. (Contract test:
  `test_no_absolute_path_leaks_across_responses`.)
- Call-graph cycles (mutual/self recursion) terminate safely.

## A minimal review-bot sketch

```python
from codegraph import CodeGraph

def review(repo: str, diff_paths: list[str], api_key: str) -> list[str]:
    findings = []
    with CodeGraph(repo, llm=api_key) as cg:
        cg.index()
        # what does this change put at risk?
        data, env = cg.change_impact(",".join(diff_paths))
        for row in data["impacted"]:
            findings.append(f'{row["fqn"]} may be affected [{row["confidence"]}]')
        # any affected callable reaching a SQL sink without a sanitizer?
        # reaches() accepts a symbol selector, not a filesystem path.
        for changed in data["impacted"]:
            _sym, res, _env = cg.reaches(changed["fqn"], sink="sql")
            for path in res["paths"]:
                if not path["via_present"]:      # no sanitizer on the chain
                    findings.append(
                        f'unsanitized → {path["sink_call"]} '
                        f'via {" → ".join(path["chain"])} [{path["confidence"]}]')
    return findings
```
