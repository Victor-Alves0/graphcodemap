"""CodeGraph: código → grafo para agentes de IA.

Uso como biblioteca:

    from codegraph import CodeGraph
    cg = CodeGraph(".")
    cg.index()
    rows, env = cg.find_symbol("validate")
"""

from __future__ import annotations

from pathlib import Path

from .indexer import Indexer
from .query import AmbiguousSymbol, Envelope, QueryEngine, SymbolNotFound

__version__ = "0.1.0"


class CodeGraph:
    """Fachada: indexação + consultas sobre um repositório."""

    def __init__(self, root: str | Path, db_path: str | Path | None = None) -> None:
        self.indexer = Indexer(root, db_path)
        self.query = QueryEngine(self.indexer)

    def index(self, force: bool = False, scope: str | None = None,
              workers: int | None = None) -> dict:
        return self.indexer.index_repo(force=force, scope=scope, workers=workers)

    def find_symbol(self, query: str, kind: str | None = None, limit: int = 10):
        return self.query.find_symbol(query, kind=kind, limit=limit)

    def symbol_info(self, selector: str):
        return self.query.symbol_info(selector)

    def references(self, selector: str, kind: str | None = None):
        return self.query.references(selector, kind=kind)

    def callers(self, selector: str, depth: int = 1):
        return self.query.callers(selector, depth=depth)

    def callees(self, selector: str, depth: int = 1):
        return self.query.callees(selector, depth=depth)

    def impact(self, selector: str, depth: int = 3):
        return self.query.impact(selector, depth=depth)

    def ego_graph(self, selector: str):
        return self.query.ego_graph(selector)

    def overview(self, scope: str | None = None, token_budget: int = 2000):
        return self.query.overview(scope=scope, token_budget=token_budget)

    def communities(self, limit: int = 20, min_size: int = 3):
        return self.query.communities(limit=limit, min_size=min_size)

    def visualize(self, level: str = "file", scope: str | None = None, top: int = 250):
        return self.query.visualize(level=level, scope=scope, top=top)

    def data_flow(self, selector: str, depth: int = 2):
        return self.query.data_flow(selector, depth=depth)

    def taint(self, scope: str | None = None, entry: str | None = None, depth: int = 4):
        return self.query.taint(scope=scope, entry=entry, depth=depth)

    def reaches(self, selector: str, sink: str = "http", via: str | None = None,
                depth: int = 8):
        return self.query.reaches(selector, sink=sink, via=via, depth=depth)

    def describe(self, target: str, refresh: bool = False):
        return self.query.describe(target, refresh=refresh)

    def stats(self) -> dict:
        return self.query.stats()

    def doctor(self, failed_limit: int = 20) -> dict:
        return self.query.doctor(failed_limit=failed_limit)

    def compact(self) -> dict:
        return self.indexer.compact()

    def close(self) -> None:
        self.indexer.close()

    def __enter__(self) -> "CodeGraph":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = [
    "CodeGraph", "Indexer", "QueryEngine", "Envelope",
    "AmbiguousSymbol", "SymbolNotFound", "__version__",
]
