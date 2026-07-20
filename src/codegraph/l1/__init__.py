"""Camada L1: refinamento semântico assíncrono (docs/DESIGN.md §4, M4).

Resolvers plugáveis por linguagem promovem arestas L0 (`inferred`/`possible`)
para `certain` quando a resolução semântica encontra exatamente uma definição
dentro do repo. Sem resolver disponível, nada muda — L0 continua correto
quanto à própria incerteza.

Resolvers: Python (jedi, in-process) e JS/TS (TypeScript LanguageService via
node; requer node + typescript instalados).
"""

from __future__ import annotations

from ..community import mark_dirty as mark_community_dirty
from ..indexer import Indexer
from ..log import get as _get_log
from ..rank import mark_dirty

log = _get_log(__name__)


def available_resolvers() -> list[type]:
    out: list[type] = []
    from .clangd import ClangdResolver
    from .clojure_lsp import ClojureLspResolver
    from .go_gopls import GoplsResolver
    from .kotlin_ls import KotlinLsResolver
    from .lua_ls import LuaLsResolver
    from .php_intelephense import IntelephenseResolver
    from .python_jedi import JediResolver
    from .ruby_solargraph import SolargraphResolver
    from .rust_analyzer import RustAnalyzerResolver
    from .tsjs_ls import TsLsResolver

    # Cada resolver ativa só quando seu LSP está no PATH — inerte caso contrário.
    # Validados ao vivo: Python (jedi), JS/TS (tsserver), Go (gopls),
    # Rust (rust-analyzer), Lua (lua-language-server), Clojure (clojure-lsp).
    # Wired via lsp_base genérico, ativam quando o binário existe (não validados
    # ao vivo aqui): C/C++ (clangd), PHP (intelephense), Ruby (solargraph),
    # Kotlin (kotlin-language-server).
    for cls in (JediResolver, TsLsResolver, GoplsResolver, RustAnalyzerResolver,
                ClangdResolver, LuaLsResolver, ClojureLspResolver,
                IntelephenseResolver, SolargraphResolver, KotlinLsResolver):
        if cls.available():
            out.append(cls)
    return out


def refine(indexer: Indexer, rels: list[str] | None = None) -> dict:
    """Roda os resolvers disponíveis. `rels` restringe a arquivos específicos."""
    resolvers = available_resolvers()
    stats = {"files": 0, "promoted": 0, "errors": 0,
             "resolvers": sorted(lang for cls in resolvers
                                 for lang in cls.languages)}
    if not resolvers:
        return stats
    conn = indexer.conn
    for cls in resolvers:
        ph = ",".join("?" * len(cls.languages))
        where, args = f"language IN ({ph})", list(cls.languages)
        if rels is not None:
            phr = ",".join("?" * len(rels))
            where += f" AND path IN ({phr})"
            args += list(rels)
        files = conn.execute(
            f"SELECT id, path FROM files WHERE {where}", args).fetchall()
        if not files:
            continue
        resolver = cls(indexer.root)
        try:
            for f in files:
                stats["files"] += 1
                try:
                    stats["promoted"] += resolver.refine_file(
                        conn, indexer.root, f["path"], f["id"])
                except Exception as e:
                    stats["errors"] += 1
                    log.debug("resolver %s falhou em %s: %s: %s",
                              cls.__name__, f["path"], type(e).__name__, e,
                              exc_info=True)
                    continue
        finally:
            close = getattr(resolver, "close", None)
            if close is not None:
                close()
    if stats["promoted"]:
        mark_dirty(conn)
        mark_community_dirty(conn)
    conn.commit()
    return stats
