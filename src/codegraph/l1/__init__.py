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


def all_resolvers() -> list[type]:
    """TODAS as classes de resolver (disponíveis ou não), na ordem canônica.

    Cada linguagem dedicada tem um resolver wired; ele só ATIVA quando seu LSP
    está no PATH (`available()`), inerte caso contrário. Validados ao vivo:
    Python (jedi), JS/TS (tsserver), Go (gopls), Rust (rust-analyzer), Lua
    (lua-language-server), Clojure (clojure-lsp), Java (jdtls), PHP
    (intelephense) — 8 famílias, incluindo a primeira por *launcher* (jdtls) e a
    primeira distribuída como pacote npm (intelephense). Wired via lsp_base
    genérico, ativam quando o servidor/toolchain existe (não validados ao vivo
    aqui): C/C++ (clangd), Ruby (solargraph), Kotlin (kotlin-language-server),
    C# (csharp-ls), Scala (metals), Swift (sourcekit)."""
    from .clangd import ClangdResolver
    from .clojure_lsp import ClojureLspResolver
    from .csharp_ls import CSharpLsResolver
    from .go_gopls import GoplsResolver
    from .jdtls import JdtlsResolver
    from .kotlin_ls import KotlinLsResolver
    from .lua_ls import LuaLsResolver
    from .metals import MetalsResolver
    from .php_intelephense import IntelephenseResolver
    from .python_jedi import JediResolver
    from .ruby_solargraph import SolargraphResolver
    from .rust_analyzer import RustAnalyzerResolver
    from .sourcekit_lsp import SourceKitLspResolver
    from .tsjs_ls import TsLsResolver

    return [JediResolver, TsLsResolver, GoplsResolver, RustAnalyzerResolver,
            ClangdResolver, LuaLsResolver, ClojureLspResolver,
            IntelephenseResolver, SolargraphResolver, KotlinLsResolver,
            JdtlsResolver, CSharpLsResolver, MetalsResolver,
            SourceKitLspResolver]


def _available(cls, root=None) -> bool:
    hook = getattr(cls, "available_for_root", None)
    return hook(root) if root is not None and hook is not None else cls.available()


def available_resolvers(root=None) -> list[type]:
    out = []
    for cls in all_resolvers():
        try:
            if _available(cls, root):
                out.append(cls)
        except Exception as e:
            log.debug("discovery do resolver %s falhou: %s: %s",
                      cls.__name__, type(e).__name__, e, exc_info=True)
    return out


def missing_resolvers(languages, is_available=None, root=None) -> list[dict]:
    """Resolvers cujo LSP FALTA, restrito às linguagens presentes no repo.

    Torna a degradação visível: se o repo tem Go mas `gopls` não está no PATH,
    as arestas Go ficam em `inferred`/`possible` em vez de `certain` — e o
    usuário merece saber por quê. `is_available` é injetável (testes).

    Retorna ``[{"languages": [...], "server": "gopls", "env": "GOPLS_BIN"}]``."""
    langs = set(languages)
    avail = is_available or (lambda cls: _available(cls, root))
    out: list[dict] = []
    for cls in all_resolvers():
        hit = sorted(l for l in cls.languages if l in langs)
        if hit and not avail(cls):
            # JediResolver é in-process (sem cmd_name/cmd_env); os demais são LSP
            # no PATH. getattr tolera ambos.
            out.append({"languages": hit,
                        "server": getattr(cls, "cmd_name", "") or "jedi",
                        "env": getattr(cls, "cmd_env", None)})
    return out


def refine(indexer: Indexer, rels: list[str] | None = None) -> dict:
    """Roda os resolvers disponíveis. `rels` restringe a arquivos específicos.

    Monorepo: os arquivos de cada linguagem são AGRUPADOS pela raiz de projeto
    detectada (go.mod, Cargo.toml, pom.xml…) e um servidor é aberto POR raiz —
    aberto na raiz certa, o LSP resolve; aberto na raiz do repo, não. Sem
    marcadores (ou repo não-monorepo), há um único grupo = a raiz do repo, e o
    comportamento é o de antes. `roots` conta as raízes distintas usadas."""
    from .roots import group_by_root

    resolvers = available_resolvers(indexer.root)
    stats = {"files": 0, "promoted": 0, "errors": 0, "roots": 0,
             "servers": 0,
             "resolvers": sorted(lang for cls in resolvers
                                 for lang in cls.languages)}
    if not resolvers:
        return stats
    conn = indexer.conn
    roots_used = set()
    for cls in resolvers:
        ph = ",".join("?" * len(cls.languages))
        where, args = f"language IN ({ph})", list(cls.languages)
        if rels is not None:
            phr = ",".join("?" * len(rels))
            where += f" AND path IN ({phr})"
            args += list(rels)
        files = conn.execute(
            f"SELECT id, path FROM files WHERE {where} ORDER BY path", args).fetchall()
        if not files:
            continue
        id_of = {f["path"]: f["id"] for f in files}
        groups = group_by_root(id_of.keys(), indexer.root,
                               getattr(cls, "root_markers", ()))
        roots_used.update(groups)
        for proj_root, group_rels in groups.items():
            stats["servers"] += 1
            try:
                resolver = cls(indexer.root, project_root=proj_root)
            except Exception as e:
                stats["errors"] += 1
                log.debug("resolver %s não iniciou em %s: %s: %s",
                          cls.__name__, proj_root, type(e).__name__, e,
                          exc_info=True)
                continue
            try:
                for rel in group_rels:
                    stats["files"] += 1
                    try:
                        stats["promoted"] += resolver.refine_file(
                            conn, indexer.root, rel, id_of[rel])
                    except Exception as e:
                        stats["errors"] += 1
                        log.debug("resolver %s falhou em %s: %s: %s",
                                  cls.__name__, rel, type(e).__name__, e,
                                  exc_info=True)
                        continue
            finally:
                close = getattr(resolver, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception as e:
                        stats["errors"] += 1
                        log.debug("resolver %s não fechou em %s: %s: %s",
                                  cls.__name__, proj_root, type(e).__name__, e,
                                  exc_info=True)
    stats["roots"] = len(roots_used)
    if stats["promoted"]:
        mark_dirty(conn)
        mark_community_dirty(conn)
    conn.commit()
    return stats
