"""Camada L1: refinamento semântico assíncrono (docs/DESIGN.md §4, M4).

Resolvers plugáveis por linguagem promovem arestas L0 (`inferred`/`possible`)
para `certain` quando a resolução semântica encontra exatamente uma definição
dentro do repo. Sem resolver disponível, nada muda — L0 continua correto
quanto à própria incerteza.

Resolvers: Python (jedi, in-process) e JS/TS (TypeScript LanguageService via
node; requer node + typescript instalados).
"""

from __future__ import annotations

import json
import time

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
    # Toolchains instalados por ``codegraph setup`` ficam numa configuração do
    # usuário. Aplicar aqui cobre CLI, biblioteca e MCP sem exigir que cada
    # superfície repita o bootstrap.
    from ..tool_config import apply_saved_environment

    apply_saved_environment()
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
    from ..tool_config import apply_saved_environment

    apply_saved_environment()
    langs = set(languages)
    avail = is_available or (lambda cls: _available(cls, root))
    out: list[dict] = []
    for cls in all_resolvers():
        hit = sorted(l for l in cls.languages if l in langs)
        if hit and not avail(cls):
            # JediResolver é in-process (sem cmd_name/cmd_env); os demais são LSP
            # no PATH. getattr tolera ambos.
            item = {"languages": hit,
                    "server": getattr(cls, "cmd_name", "") or "jedi",
                    "env": getattr(cls, "cmd_env", None)}
            details = getattr(cls, "unavailable_details", None)
            if details is not None:
                item.update(details())
            out.append(item)
    return out


def is_project_marker(rel: str) -> bool:
    """Reconhece somente markers declarados pelo catálogo de resolvers L1."""
    from .roots import matches_project_marker

    return any(matches_project_marker(
        rel, getattr(cls, "root_markers", ())) for cls in all_resolvers())


def refine(indexer: Indexer, rels: list[str] | None = None) -> dict:
    """Roda os resolvers disponíveis. `rels` identifica arquivos alterados.

    Monorepo: os arquivos de cada linguagem são AGRUPADOS pela raiz de projeto
    detectada (go.mod, Cargo.toml, pom.xml…) e um servidor é aberto POR raiz —
    aberto na raiz certa, o LSP resolve; aberto na raiz do repo, não. Sem
    marcadores (ou repo não-monorepo), há um único grupo = a raiz do repo, e o
    comportamento é o de antes. Incrementalmente, mudar um arquivo pode alterar
    a resolução de callers intocados; por isso a unidade processada é a raiz de
    projeto afetada, não apenas `rels`. `roots` conta as raízes usadas."""
    from .roots import (detect_project_root, group_by_root,
                        marker_affected_roots, matches_project_marker)
    from . import promote

    conn = indexer.conn
    changed_languages: dict[str, str] = {}
    if rels is not None:
        if not rels:
            present_languages: set[str] = set()
        else:
            placeholders = ",".join("?" * len(rels))
            changed_languages = {
                row["path"]: row["language"] for row in conn.execute(
                    f"SELECT path, language FROM files "
                    f"WHERE path IN ({placeholders})", list(rels))
            }
            # Um delete chega aqui depois de ``Indexer.remove_file`` e portanto
            # já não possui linha em ``files``. A extensão ainda preserva a
            # linguagem necessária para revalidar callers intocados.
            from ..languages import language_for

            for rel in rels:
                inferred = language_for(rel)
                if inferred is not None:
                    changed_languages.setdefault(rel, inferred)
            present_languages = set(changed_languages.values())
    else:
        present_languages = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT language FROM files")
        }

    resolvers = available_resolvers(indexer.root)
    # Inclui resolvers injetados em testes/extensões, sem perder o catálogo que
    # permite explicar por que uma linguagem presente ficou sem L1.
    catalog = all_resolvers()
    candidates = list(dict.fromkeys([*catalog, *resolvers]))

    # ``rels`` é um delta do universo semântico, não um conjunto seguro de
    # callers. Expanda cada resolver para todos os arquivos das raízes tocadas:
    # adicionar B.java pode mudar Main.java sem mudar o hash de Main.java.
    changed_rels = set(rels or ())
    scoped_files: dict[type, list] = {}

    def files_for(cls):
        cached = scoped_files.get(cls)
        if cached is not None:
            return cached
        ph = ",".join("?" * len(cls.languages))
        all_files = conn.execute(
            f"SELECT id, path, language FROM files "
            f"WHERE language IN ({ph}) ORDER BY path",
            list(cls.languages),
        ).fetchall()
        if rels is None:
            selected = all_files
        else:
            groups = group_by_root(
                (row["path"] for row in all_files), indexer.root,
                getattr(cls, "root_markers", ()))
            affected_roots = {
                root for root, paths in groups.items()
                if changed_rels.intersection(paths)
            }
            for changed in changed_rels:
                # Caminhos removidos não aparecem em ``groups``. O diretório e
                # os markers do projeto, porém, continuam suficientes para
                # recuperar a raiz semântica que precisa ser reaberta.
                if changed_languages.get(changed) in cls.languages:
                    affected_roots.add(detect_project_root(
                        changed, indexer.root,
                        getattr(cls, "root_markers", ())))
                affected_roots.update(marker_affected_roots(
                    changed, indexer.root,
                    getattr(cls, "root_markers", ())))
            affected = {
                path for root in affected_roots if root in groups
                for path in groups[root]
            }
            selected = [row for row in all_files if row["path"] in affected]
        scoped_files[cls] = selected
        return selected

    # Markers não entram em ``files`` e portanto não têm language_for. Derive
    # as linguagens apenas dos arquivos indexados nas raízes realmente afetadas.
    if rels is not None:
        for cls in candidates:
            if any(matches_project_marker(
                    changed, getattr(cls, "root_markers", ()))
                    for changed in changed_rels):
                present_languages.update(
                    row["language"] for row in files_for(cls))

    applicable = sorted(
        present_languages.intersection(
            lang for cls in candidates for lang in cls.languages))
    active_languages = {
        lang for cls in resolvers for lang in cls.languages
        if lang in present_languages
    }
    unavailable_languages = set(applicable).difference(active_languages)
    unavailable: list[dict] = []
    remaining = set(unavailable_languages)
    for cls in catalog:
        hit = sorted(remaining.intersection(cls.languages))
        if not hit:
            continue
        item = {
            "languages": hit,
            "resolver": cls.__name__,
            "server": getattr(cls, "cmd_name", "") or "jedi",
            "env": getattr(cls, "cmd_env", None),
        }
        details = getattr(cls, "unavailable_details", None)
        if details is not None:
            item.update(details())
        unavailable.append(item)
        remaining.difference_update(hit)

    missing_warnings = []
    for item in unavailable:
        warning = ("resolver L1 indisponível para "
                   + ", ".join(item["languages"])
                   + f" ({item['server']})")
        if item.get("reason"):
            warning += f": {item['reason']}"
        if item.get("action"):
            warning += f"; ação: {item['action']}"
        missing_warnings.append(warning)
    stats = {"files": 0, "promoted": 0, "errors": 0, "roots": 0,
             "servers": 0, "rolled_back": 0,
             "status": "partial" if unavailable else "complete",
             "partial": bool(unavailable),
             # Keep the public detailed list independent from the sanitized
             # persistence seed. Resolver diagnostics may contain absolute
             # paths and must never mutate ``missing_warnings`` by alias.
             "warnings": list(missing_warnings), "runs": [],
             "applicable": applicable, "attempted": [],
             "unavailable": unavailable,
             "revalidated": 0,
             "resolvers": sorted(lang for cls in resolvers
                                 for lang in cls.languages)}
    # Provas semânticas pertencem ao universo da passada que as produziu, não
    # apenas ao hash do caller. Novo override, build model ou classpath pode mudar
    # a resposta do LSP sem tocar o arquivo. Retorne primeiro ao fallback L0;
    # assim indisponibilidade/crash/resultado vazio nunca conserva certeza stale.
    revalidation_rels = None
    if rels is not None:
        revalidation_rels = sorted({
            row["path"] for cls in candidates
            if present_languages.intersection(cls.languages)
            for row in files_for(cls)
        })
    stats["revalidated"] = promote.reset_sites(
        conn, applicable, rels=revalidation_rels)
    if stats["revalidated"]:
        # ``resolve_edges`` faz commit do fallback L0. Marque caches derivados
        # antes para que nenhum leitor observe o grafo novo como rank/current.
        mark_dirty(conn)
        mark_community_dirty(conn)
        indexer.resolve_edges()
    def persist_last_run() -> None:
        safe_runs = [{key: run[key] for key in (
            "resolver", "files", "promoted", "status",
            "attempted_promotions", "rolled_back",
            "sites", "resolved_sites", "warmup_timed_out", "io_timed_out",
            "ready_timeout_s", "io_timeout_s", "workspace_reused",
            "workspace_recovered", "workspace_invalidated") if key in run}
            for run in stats["runs"]]
        safe_warnings = list(missing_warnings)
        for run in safe_runs:
            resolver_name = run["resolver"]
            unresolved_readiness = (run.get("warmup_timed_out")
                                    and not run.get("resolved_sites"))
            if unresolved_readiness:
                seconds = run.get("ready_timeout_s", "configurado")
                action = ("aumente --jdtls-ready-timeout/"
                          "CODEGRAPH_JDTLS_READY_TIMEOUT"
                          if resolver_name == "JdtlsResolver"
                          else "ajuste o timeout de readiness do resolver")
                safe_warnings.append(
                    f"{resolver_name}: readiness excedeu {seconds}s; {action}")
            if run.get("io_timed_out"):
                seconds = run.get("io_timeout_s", "configurado")
                action = ("aumente --jdtls-io-timeout/"
                          "CODEGRAPH_JDTLS_IO_TIMEOUT"
                          if resolver_name == "JdtlsResolver"
                          else "ajuste o timeout de I/O do resolver")
                safe_warnings.append(
                    f"{resolver_name}: I/O excedeu {seconds}s; {action}")
            if (run.get("status") == "partial"
                    and not unresolved_readiness
                    and not run.get("io_timed_out")):
                safe_warnings.append(
                    f"{resolver_name}: passada parcial por erro do resolver; "
                    "consulte a saída original de `refine` para detalhes")
        record = {key: stats[key] for key in (
            "status", "partial", "errors", "applicable",
            "attempted", "unavailable", "files", "promoted", "rolled_back")}
        # Diagnosticos do LSP podem conter caminhos absolutos. O doctor e a API
        # publica preservam o estado/acao, enquanto o detalhe fica no stdout da
        # passada que o produziu.
        record["warnings"] = list(dict.fromkeys(safe_warnings))
        record["runs"] = safe_runs
        record["finished_at"] = int(time.time())
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('l1_last_run', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(record, ensure_ascii=False),))
        conn.commit()

    if not resolvers:
        persist_last_run()
        return stats
    roots_used = set()
    attempted_languages: set[str] = set()
    for cls in resolvers:
        if not present_languages.intersection(cls.languages):
            continue
        files = files_for(cls)
        if not files:
            continue
        attempted_languages.update(
            present_languages.intersection(cls.languages))
        id_of = {f["path"]: f["id"] for f in files}
        groups = group_by_root(id_of.keys(), indexer.root,
                               getattr(cls, "root_markers", ()))
        roots_used.update(groups)
        for proj_root, group_rels in groups.items():
            stats["servers"] += 1
            run = {"resolver": cls.__name__, "root": str(proj_root),
                   "files": 0, "promoted": 0, "status": "complete",
                   "warnings": [], "errors": []}
            stats["runs"].append(run)
            try:
                resolver = cls(indexer.root, project_root=proj_root)
            except Exception as e:
                stats["errors"] += 1
                stats["partial"] = True
                stats["status"] = "partial"
                run["status"] = "partial"
                message = f"{type(e).__name__}: {e}"
                run["errors"] = [message]
                # Este caminho não chega ao agregador comum abaixo do finally.
                stats["warnings"].append(
                    f"{run['resolver']} ({run['root']}): ERROR: {message}")
                log.debug("resolver %s não iniciou em %s: %s: %s",
                          cls.__name__, proj_root, type(e).__name__, e,
                          exc_info=True)
                continue
            # Uma promoção só é publicável depois que close+health aprovam a
            # instância desta raiz. A savepoint permite rollback exato sem
            # apagar provas saudáveis aceitas por outra raiz/resolver.
            savepoint = f"l1_root_{stats['servers']}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                for rel in group_rels:
                    stats["files"] += 1
                    run["files"] += 1
                    try:
                        promoted = resolver.refine_file(
                            conn, indexer.root, rel, id_of[rel])
                        stats["promoted"] += promoted
                        run["promoted"] += promoted
                    except Exception as e:
                        stats["errors"] += 1
                        stats["partial"] = True
                        stats["status"] = "partial"
                        run["status"] = "partial"
                        run["errors"].append(f"{rel}: {type(e).__name__}: {e}")
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
                        stats["partial"] = True
                        stats["status"] = "partial"
                        run["status"] = "partial"
                        run["errors"].append(
                            f"close: {type(e).__name__}: {e}")
                        log.debug("resolver %s não fechou em %s: %s: %s",
                                  cls.__name__, proj_root, type(e).__name__, e,
                                  exc_info=True)
            health_fn = getattr(resolver, "health_report", None)
            if health_fn is not None:
                try:
                    health = health_fn()
                except Exception as e:
                    stats["errors"] += 1
                    stats["partial"] = True
                    stats["status"] = "partial"
                    run["status"] = "partial"
                    run["errors"].append(
                        f"health: {type(e).__name__}: {e}")
                else:
                    run.update({key: health[key] for key in (
                        "sites", "resolved_sites", "warmup_timed_out",
                        "semantic_request_errors",
                        "io_timed_out", "ready_timeout_s", "io_timeout_s",
                        "workspace_reused", "workspace_recovered",
                        "workspace_invalidated")
                                if key in health})
                    run["warnings"].extend(health.get("warnings", ()))
                    run["errors"].extend(health.get("errors", ()))
                    if health.get("status") == "partial":
                        # Uma falha de saude conta por passada/servidor, nao por
                        # cada diagnostico repetido publicado pelo LSP.
                        if run["status"] != "partial":
                            stats["errors"] += 1
                        run["status"] = "partial"
                        stats["partial"] = True
                        stats["status"] = "partial"
            if run["status"] == "partial":
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                attempted = run["promoted"]
                run["attempted_promotions"] = attempted
                run["rolled_back"] = attempted
                run["promoted"] = 0
                stats["rolled_back"] += attempted
                stats["promoted"] -= attempted
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            run["warnings"] = list(dict.fromkeys(run["warnings"]))
            run["errors"] = list(dict.fromkeys(run["errors"]))
            stats["warnings"].extend(
                f"{run['resolver']} ({run['root']}): {warning}"
                for warning in run["warnings"])
            stats["warnings"].extend(
                f"{run['resolver']} ({run['root']}): ERROR: {error}"
                for error in run["errors"])
    stats["roots"] = len(roots_used)
    stats["attempted"] = sorted(attempted_languages)
    stats["warnings"] = list(dict.fromkeys(stats["warnings"]))
    if stats["promoted"]:
        mark_dirty(conn)
        mark_community_dirty(conn)
    conn.commit()
    persist_last_run()
    return stats
