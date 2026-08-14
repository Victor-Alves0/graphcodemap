# Contributing to GraphCodeMap

Thanks for your interest. This project has a strong point of view about
correctness — contributions are very welcome as long as they hold the same bar.

## The core principle

**Epistemic honesty.** The value of this product is in its *invariants*, not its
parser: a graph an agent can trust because it never serves a stale fact without
warning, and never presents a guess as a certainty. Every change should keep that
true.

## Development setup

```bash
git clone https://github.com/Victor-Alves0/graphcodemap
cd graphcodemap
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,mcp,l1]"
```

Run the suite:

```bash
pytest -q --timeout=180
```

Run the same production quality gates used by CI and releases:

```bash
python -m ruff check src
python -m mypy
python -m coverage run -m pytest -q --timeout=180
python -m coverage report
python -m build
python -m twine check dist/*
```

Install them with `pip install -e ".[dev,quality,mcp,l1]"`. Coverage is
branch-aware and currently fails below 75%; typing is intentionally progressive
and its checked module list lives in `pyproject.toml`.

CI runs the same suite on a matrix of **Linux + Windows × Python 3.10/3.11/3.12**.
Tests that need an external language server (L1/LSP) skip themselves when the
server isn't installed, so a green local run without every toolchain is expected.

> On Windows, set `PYTHONIOENCODING=utf-8` if console output chokes on the `⚠`
> character.

## The workflow that is non-negotiable: tests first

When you change how a language is parsed, or touch the graph's behavior, the order
is **tests before code**:

1. **Write a large test battery first** — including cases you *suspect are broken*,
   specifically to expose gaps. A battery that only covers what already works is
   not doing its job.
2. Watch the relevant ones fail.
3. Then implement, until they pass and nothing else regresses.

This is how real bugs have been found here (the new-file read-repair gap, the
overload fan-out collision, inheritance edges in several languages). A green diff
that skipped step 1 will be asked to redo it.

## What "correct" means here

- **Never fabricate a `certain` edge.** If semantic resolution isn't available,
  the edge stays `inferred`/`possible`. Honest uncertainty beats confident error.
- **Never let a query return stale data silently.** If you add a code path that
  reads the graph, it must go through read-repair (or explicitly justify why the
  data is already fresh).
- **The invariants are locked by tests.** See
  [`tests/test_contract_invariants.py`](tests/test_contract_invariants.py) — ten
  contract tests for reindex idempotency, symbol-identity stability, dangling
  edges, freshness↔hash, confidence honesty, minimum-confidence propagation,
  exclusion, and path non-leakage. Don't break these; if you change one, explain
  why in the PR.

## Adding a language

- **Generic tier** (structural symbols over any tree-sitter grammar): usually a
  grammar mapping in [`src/codegraph/languages.py`](src/codegraph/languages.py).
- **Dedicated extractor** (refined fqn/imports/calls/inheritance): a new module in
  [`src/codegraph/extract/`](src/codegraph/extract/) plus a full test battery
  under `tests/` (see the existing `test_*_robust.py` files for the expected
  depth).
- **L1 resolver** (promote edges to `certain`): often a **~10-line config** on
  [`src/codegraph/l1/lsp_base.py`](src/codegraph/l1/lsp_base.py) — the server
  command, the project root markers, and the language id. See
  [docs/languages.md](docs/languages.md).

Bump `INDEXER_VERSION` in [`src/codegraph/indexer.py`](src/codegraph/indexer.py)
whenever the on-disk graph shape changes — it forces a clean rebuild (the derived
cache never asks the user for manual migration).

## Pull requests

- Keep the change focused and match the surrounding code's style, naming, and
  comment density.
- Add or update tests; update the relevant page under [`docs/`](docs/) and the
  [CHANGELOG](CHANGELOG.md).
- Make sure `pytest` is green and `codegraph doctor` on a sample repo is clean.
- Describe *what invariant your change preserves or improves* — that's the review
  lens.

## Reporting bugs

Open an issue: <https://github.com/Victor-Alves0/graphcodemap/issues>. A repro
(even a tiny snippet in the affected language) and the output of `codegraph doctor`
go a long way.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
