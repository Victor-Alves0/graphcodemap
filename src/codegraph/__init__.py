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

    def __init__(self, root: str | Path, db_path: str | Path | None = None,
                 llm=None) -> None:
        """`llm`: provider L3 injetado — callable `(system, user) -> str` ou a
        própria chave de API. Evita depender de os.environ/.env (essencial para
        host multi-usuário, onde a chave é do usuário e o custo precisa ficar
        atribuível). Também pode ser passado por chamada em `describe(llm=...)`."""
        from .l3.provider import coerce_provider

        self.indexer = Indexer(root, db_path)
        self.query = QueryEngine(self.indexer)
        self.query.l3_provider = coerce_provider(llm)

    def index(self, force: bool = False, scope: str | None = None,
              workers: int | None = None, exclude: list[str] | None = None) -> dict:
        """Indexa. O retorno inclui `changes` (símbolos que entraram/saíram/
        mudaram de assinatura). `exclude` é a política de exclusão do host
        (padrões estilo gitignore), guardada no índice — sem escrever arquivo
        no repo do usuário; `None` mantém a salva, `[]` limpa."""
        return self.indexer.index_repo(force=force, scope=scope, workers=workers,
                                       exclude=exclude)

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

    def change_impact(self, target: str, depth: int = 3):
        return self.query.change_impact(target, depth=depth)

    def find_affected_modules(self, target: str, depth: int = 3):
        return self.query.find_affected_modules(target, depth=depth)

    def find_related_tests(self, selector: str, depth: int = 3):
        return self.query.find_related_tests(selector, depth=depth)

    def explain_symbol(self, selector: str):
        return self.query.explain_symbol(selector)

    def suggest_files_to_read(self, task: str, limit: int = 8):
        return self.query.suggest_files_to_read(task, limit=limit)

    def overview(self, scope: str | None = None, token_budget: int = 2000):
        return self.query.overview(scope=scope, token_budget=token_budget)

    def communities(self, limit: int = 20, min_size: int = 3):
        return self.query.communities(limit=limit, min_size=min_size)

    def repository_tree(self, path: str = "", depth: int = 4,
                        refresh: bool = True):
        return self.query.repository_tree(path=path, depth=depth, refresh=refresh)

    def graph_history(self, limit: int = 20, git_commit: str | None = None):
        return self.query.graph_history(limit=limit, git_commit=git_commit)

    def visualize(self, mode: str | None = None, *, level: str | None = None,
                  scope: str | None = None, top: int = 250,
                  symbol: str | None = None, depth: int = 3,
                  min_confidence: str | None = None, language: str | None = None,
                  changed: str | None = None, git: bool = False,
                  git_ref: str | None = None, staged: bool = False):
        return self.query.visualize(
            mode, level=level, scope=scope, top=top, symbol=symbol, depth=depth,
            min_confidence=min_confidence, language=language, changed=changed,
            git=git, git_ref=git_ref, staged=staged)

    def data_flow(self, selector: str, depth: int = 2):
        return self.query.data_flow(selector, depth=depth)

    def taint(self, scope: str | None = None, entry: str | None = None,
              depth: int | None = None, max_findings: int = 100,
              deadline_ms: int | None = None, max_steps: int | None = None,
              should_cancel=None):
        return self.query.taint(scope=scope, entry=entry, depth=depth,
                                max_findings=max_findings, deadline_ms=deadline_ms,
                                max_steps=max_steps, should_cancel=should_cancel)

    def reaches(self, selector: str, sink: str = "http", via: str | None = None,
                depth: int = 8, max_paths: int = 20, deadline_ms: int | None = None,
                max_steps: int | None = None, should_cancel=None):
        return self.query.reaches(selector, sink=sink, via=via, depth=depth,
                                  max_paths=max_paths, deadline_ms=deadline_ms,
                                  max_steps=max_steps, should_cancel=should_cancel)

    def describe(self, target: str, refresh: bool = False, llm=None):
        return self.query.describe(target, refresh=refresh, llm=llm)

    def stats(self) -> dict:
        return self.query.stats()

    def l1_status(self) -> dict:
        """Semantic lifecycle: not_started/running/complete/partial."""
        return self.query.l1_status()

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
