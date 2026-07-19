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
from ..rank import mark_dirty


def available_resolvers() -> list[type]:
    out: list[type] = []
    from .clangd import ClangdResolver
    from .go_gopls import GoplsResolver
    from .python_jedi import JediResolver
    from .rust_analyzer import RustAnalyzerResolver
    from .tsjs_ls import TsLsResolver

    # Python/JS-TS/Go validados; Rust/C-C++ ativam quando o LSP está no PATH
    # (protocolo idêntico ao gopls, que foi validado).
    for cls in (JediResolver, TsLsResolver, GoplsResolver,
                RustAnalyzerResolver, ClangdResolver):
        if cls.available():
            out.append(cls)
    return out


def refine(indexer: Indexer, rels: list[str] | None = None) -> dict:
    """Roda os resolvers disponíveis. `rels` restringe a arquivos específicos."""
    resolvers = available_resolvers()
    stats = {"files": 0, "promoted": 0,
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
                except Exception:
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
