"""CLI: codegraph index|find|info|refs|callers|callees|impact|ego|overview|stats|mcp."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import render
from .indexer import Indexer
from .query import AmbiguousSymbol, QueryEngine, SymbolNotFound


def _engine(args) -> QueryEngine:
    return QueryEngine(Indexer(args.root, args.db))


def _l1_partial(stats: dict) -> bool:
    """Compatível também com relatórios anteriores à telemetria de status."""
    return bool(stats.get("partial") or stats.get("status") == "partial"
                or stats.get("errors", 0))


def _print_l1_report(stats: dict, elapsed: float) -> None:
    """Saída humana com campos estruturados estáveis para logs/CI."""
    partial = _l1_partial(stats)
    status = "partial" if partial else (stats.get("status") or "complete")
    resolvers = stats.get("resolvers", [])
    print(f"L1: {stats.get('promoted', 0)} aresta(s) promovidas a 'certain' em "
          f"{stats.get('files', 0)} arquivo(s) "
          f"({', '.join(resolvers) or 'nenhum resolver'}) em {elapsed:.2f}s")
    print(f"  status: {status}; partial: {str(partial).lower()}")
    # JSON válido preserva listas/objetos e acentos sem depender de repr Python.
    for key in ("applicable", "attempted", "unavailable", "warnings"):
        value = stats.get(key, [])
        print(f"  {key}: {json.dumps(value, ensure_ascii=False, default=str)}")


def cmd_index(args) -> int:
    if args.path:
        args.root = str(Path(args.path).resolve())
    ix = Indexer(args.root, args.db)
    t0 = time.perf_counter()
    stats = ix.index_repo(force=args.force, scope=getattr(args, "scope", None),
                          workers=getattr(args, "workers", None),
                          exclude=getattr(args, "exclude", None))
    dt = time.perf_counter() - t0
    from .indexer import get_index_scopes

    scopes = get_index_scopes(ix.conn)
    scope_note = f" [escopo: {', '.join(scopes)}]" if scopes else ""
    print(f"indexados {stats['indexed']}/{stats['scanned']} arquivos "
          f"({stats['removed']} removidos, {stats['errors']} erros) em {dt:.2f}s{scope_note}")
    if args.l1:
        from . import l1

        t0 = time.perf_counter()
        r = l1.refine(ix)
        dt = time.perf_counter() - t0
        _print_l1_report(r, dt)
        return 1 if _l1_partial(r) else 0
    return 0


def cmd_refine(args) -> int:
    from . import l1

    ix = Indexer(args.root, args.db)
    t0 = time.perf_counter()
    r = l1.refine(ix)
    dt = time.perf_counter() - t0
    _print_l1_report(r, dt)
    return 1 if _l1_partial(r) else 0


def cmd_find(args) -> int:
    rows, env = _engine(args).find_symbol(args.query, kind=args.kind, limit=args.limit)
    print(render.find(args.query, rows, env))
    return 0 if rows else 1


def cmd_info(args) -> int:
    data, env = _engine(args).symbol_info(args.symbol)
    print(render.info(data, env))
    return 0


def cmd_refs(args) -> int:
    sym, rows, env = _engine(args).references(args.symbol, kind=args.kind)
    print(render.refs(sym, rows, env))
    return 0


def cmd_callers(args) -> int:
    sym, rows, env = _engine(args).callers(args.symbol, depth=args.depth)
    print(render.calls(sym, rows, env, "callers de", "in"))
    return 0


def cmd_callees(args) -> int:
    sym, rows, env = _engine(args).callees(args.symbol, depth=args.depth)
    print(render.calls(sym, rows, env, "callees de", "out"))
    return 0


def cmd_impact(args) -> int:
    sym, rows, env = _engine(args).impact(args.symbol, depth=args.depth)
    print(render.impact(sym, rows, env))
    return 0


def cmd_ego(args) -> int:
    data, env = _engine(args).ego_graph(args.symbol)
    print(render.ego(data, env))
    return 0


def cmd_change_impact(args) -> int:
    data, env = _engine(args).change_impact(args.target, depth=args.depth)
    print(render.change_impact(data, env))
    return 0


def cmd_affected_modules(args) -> int:
    data, env = _engine(args).find_affected_modules(args.target, depth=args.depth)
    print(render.affected_modules(data, env))
    return 0


def cmd_related_tests(args) -> int:
    data, env = _engine(args).find_related_tests(args.symbol, depth=args.depth)
    print(render.related_tests(data, env))
    return 0 if data["tests"] else 1


def cmd_explain(args) -> int:
    data, env = _engine(args).explain_symbol(args.symbol)
    print(render.explain_symbol(data, env))
    return 0


def cmd_suggest(args) -> int:
    data, env = _engine(args).suggest_files_to_read(args.task, limit=args.limit)
    print(render.suggest_files(data, env))
    return 0 if data["files"] else 1


def cmd_overview(args) -> int:
    entries, env = _engine(args).overview(scope=args.scope, token_budget=args.budget)
    print(render.overview(entries, env))
    return 0


def cmd_communities(args) -> int:
    items, meta, env = _engine(args).communities(limit=args.limit, min_size=args.min_size)
    print(render.communities(items, meta, env))
    return 0


def cmd_dataflow(args) -> int:
    data, env = _engine(args).data_flow(args.symbol, depth=args.depth)
    print(render.dataflow(data, env))
    return 0


def cmd_taint(args) -> int:
    data, env = _engine(args).taint(scope=args.scope, entry=args.entry,
                                    depth=args.depth, max_findings=args.max_findings,
                                    deadline_ms=args.deadline_ms, max_steps=args.max_steps)
    print(render.taint(data, env))
    return 0 if not data["findings"] else 1


def cmd_reaches(args) -> int:
    sym, data, env = _engine(args).reaches(args.symbol, sink=args.sink,
                                           via=args.via, depth=args.depth,
                                           max_paths=args.max_paths,
                                           deadline_ms=args.deadline_ms,
                                           max_steps=args.max_steps)
    print(render.reaches(sym, data, env))
    return 0


def cmd_visualize(args) -> int:
    import json as _json

    from .viz import render_html

    data, env = _engine(args).visualize(
        args.mode, level=args.level, scope=args.scope, top=args.top,
        symbol=args.symbol, depth=args.depth, min_confidence=args.min_confidence,
        language=args.language, changed=args.changed, git=args.git,
        git_ref=args.git_ref, staged=args.staged)
    if env.warnings:
        print(render.warnings(env).rstrip(), file=sys.stderr)
    default = ".codegraph/graph.json" if args.json else ".codegraph/graph.html"
    out = Path(args.out) if args.out else Path(args.root) / default
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.json:
        out.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        out.write_text(render_html(data), encoding="utf-8")
    print(f"{len(data['nodes'])} nós, {len(data['links'])} arestas, "
          f"{len(data['domains'])} domínios → {out}")
    return 0


def cmd_describe(args) -> int:
    from .l3 import Describer, L3Unavailable

    if not args.target and not args.top:
        print("erro: informe um símbolo/arquivo ou use --top N", file=sys.stderr)
        return 2
    engine = _engine(args)
    try:
        if args.top:
            describer = Describer(engine.root, engine.conn)
            rows = engine.conn.execute(
                "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id=f.id "
                "ORDER BY s.rank DESC LIMIT ?", (args.top,)).fetchall()
            for r in rows:
                d = describer.describe_symbol(dict(r), refresh=args.refresh)
                mark = "gerada" if d["generated_now"] else "cache"
                print(f"[{mark}] {r['fqn']}")
            u = getattr(describer._provider, "usage", None)
            if u and u["calls"]:
                print(f"custo: {u['total_tokens']} tokens em {u['calls']} "
                      f"chamada(s) ({u['model']})")
            return 0
        data, env = engine.describe(args.target, refresh=args.refresh)
        print(render.describe(data, env))
        return 0
    except L3Unavailable as e:
        print(f"erro: {e}", file=sys.stderr)
        return 3


def cmd_eval(args) -> int:
    import json as _json

    from .eval import render_report, run_eval

    report = run_eval(args.root, args.tasks,
                      arms=args.arms.split(",") if args.arms else None,
                      max_steps=args.max_steps, model=args.model)
    out = Path(args.root) / "evals" / f"report-{report['timestamp']}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(_json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print()
    print(render_report(report))
    print(f"\nrelatório completo: {out}")
    return 0


def cmd_stats(args) -> int:
    print(render.stats(_engine(args).stats()))
    return 0


def cmd_vacuum(args) -> int:
    ix = Indexer(args.root, args.db)
    t0 = time.perf_counter()
    r = ix.compact()
    dt = time.perf_counter() - t0
    mb = lambda b: f"{b / 1024 / 1024:.1f} MB"  # noqa: E731
    print(f"compactado em {dt:.2f}s: arestas {r['edges_before']} → "
          f"{r['edges_after']}, tamanho {mb(r['size_before'])} → "
          f"{mb(r['size_after'])} ({r['indexed']} arquivos, {r['errors']} erros)")
    return 0


def cmd_capabilities(args) -> int:
    """Mapa 'pronto/falta' por linguagem. Não toca no índice — é sobre o MOTOR,
    não sobre um repo (por isso não abre engine)."""
    from . import capabilities as caps

    print(render.capabilities(caps.matrix(), caps.summary(), caps.gaps()))
    return 0


def cmd_doctor(args) -> int:
    engine = _engine(args)
    d = engine.doctor()
    print(render.doctor(d))
    if getattr(args, "why", False) and d["parse_failed_sample"]:
        print("\nmotivo das falhas de parse:")
        for rel in d["parse_failed_sample"]:
            reason = (engine.ix.diagnose_file(rel)
                      or "hoje parseia — falha obsoleta (rode `vacuum`)")
            print(f"  {rel}: {reason}")
    # exit != 0 se há sinal de problema, para uso em scripts/CI
    unhealthy = d["parse_failed_total"] > 0
    return 1 if unhealthy else 0


def cmd_watch(args) -> int:
    import time as _time

    from .watcher import Watcher

    ix = Indexer(args.root, args.db)
    stats = ix.index_repo()
    ix.close()
    print(f"índice atualizado ({stats['indexed']} arquivos); observando {args.root} "
          f"(Ctrl+C para sair)")
    w = Watcher(args.root, args.db)
    w.start()
    try:
        while True:
            _time.sleep(1)
    except KeyboardInterrupt:
        w.stop()
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import serve

    serve(args.root, args.db, watch=not args.no_watch)
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="codegraph",
                                description="Code-to-graph engine para agentes de IA")
    p.add_argument("--root", default=".", help="raiz do repo (default: .)")
    p.add_argument("--db", default=None,
                   help="caminho do banco (default: <root>/.codegraph/graph.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("index", help="indexa/atualiza o repo")
    sp.add_argument("path", nargs="?", default=None, help="raiz do repo (equivale a --root)")
    sp.add_argument("--force", action="store_true", help="re-indexa tudo mesmo sem mudanças")
    sp.add_argument("--l1", "--refine", dest="l1", action="store_true",
                    help="roda refinamento L1 após indexar")
    sp.add_argument("--scope", default=None,
                    help="indexa só esta subárvore do repo (parcial; acumula "
                         "entre execuções e é lembrado nas próximas)")
    sp.add_argument("--workers", type=int, default=None,
                    help="threads de prepare no índice (default: min(4, CPUs); "
                         "1 = serial). A escrita no SQLite é sempre serial.")
    sp.add_argument("--exclude", action="append", default=None, metavar="PADRAO",
                    help="padrão estilo gitignore a excluir (repetível). Fica "
                         "guardado no índice — não escreve nada no repo. "
                         "Substitui a política anterior; use --exclude '' p/ limpar")
    sp.set_defaults(fn=cmd_index)

    sp = sub.add_parser("refine", help="refinamento L1: promove arestas a 'certain'")
    sp.set_defaults(fn=cmd_refine)

    sp = sub.add_parser("find", help="localiza símbolos")
    sp.add_argument("query")
    sp.add_argument("--kind", default=None)
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(fn=cmd_find)

    sp = sub.add_parser("info", help="cartão de um símbolo")
    sp.add_argument("symbol")
    sp.set_defaults(fn=cmd_info)

    sp = sub.add_parser("refs", help="referências a um símbolo")
    sp.add_argument("symbol")
    sp.add_argument("--kind", default=None)
    sp.set_defaults(fn=cmd_refs)

    sp = sub.add_parser("callers", help="quem chama o símbolo")
    sp.add_argument("symbol")
    sp.add_argument("--depth", type=int, default=1)
    sp.set_defaults(fn=cmd_callers)

    sp = sub.add_parser("callees", help="o que o símbolo chama")
    sp.add_argument("symbol")
    sp.add_argument("--depth", type=int, default=1)
    sp.set_defaults(fn=cmd_callees)

    sp = sub.add_parser("impact", help="o que pode quebrar se eu mudar o símbolo")
    sp.add_argument("symbol")
    sp.add_argument("--depth", type=int, default=3)
    sp.set_defaults(fn=cmd_impact)

    sp = sub.add_parser("ego", help="vizinhança imediata do símbolo no grafo")
    sp.add_argument("symbol")
    sp.set_defaults(fn=cmd_ego)

    sp = sub.add_parser("change-impact",
                        help="impacto de uma mudança (caminhos ou diff)")
    sp.add_argument("target", help="caminhos (vírgula/espaço) ou um diff unificado")
    sp.add_argument("--depth", type=int, default=3)
    sp.set_defaults(fn=cmd_change_impact)

    sp = sub.add_parser("affected-modules",
                        help="módulos afetados por uma mudança (caminhos ou diff)")
    sp.add_argument("target")
    sp.add_argument("--depth", type=int, default=3)
    sp.set_defaults(fn=cmd_affected_modules)

    sp = sub.add_parser("related-tests",
                        help="testes que exercitam um símbolo")
    sp.add_argument("symbol")
    sp.add_argument("--depth", type=int, default=3)
    sp.set_defaults(fn=cmd_related_tests)

    sp = sub.add_parser("explain", help="ficha rica de um símbolo (sem LLM)")
    sp.add_argument("symbol")
    sp.set_defaults(fn=cmd_explain)

    sp = sub.add_parser("suggest",
                        help="arquivos a ler para uma tarefa em linguagem natural")
    sp.add_argument("task")
    sp.add_argument("--limit", type=int, default=8)
    sp.set_defaults(fn=cmd_suggest)

    sp = sub.add_parser("overview", help="mapa ranqueado do repo")
    sp.add_argument("--scope", default=None, help="restringe a um diretório")
    sp.add_argument("--budget", type=int, default=2000, help="budget aprox. de tokens")
    sp.set_defaults(fn=cmd_overview)

    sp = sub.add_parser("communities", help="domínios do repo (comunidades do grafo)")
    sp.add_argument("--limit", type=int, default=20, help="máx. de domínios listados")
    sp.add_argument("--min-size", type=int, default=3,
                    help="ignora domínios menores que N símbolos")
    sp.set_defaults(fn=cmd_communities)

    sp = sub.add_parser("dataflow", help="fluxo de dados: para onde vão os parâmetros")
    sp.add_argument("symbol", help="fqn da função")
    sp.add_argument("--depth", type=int, default=2, help="saltos inter-procedurais")
    sp.set_defaults(fn=cmd_dataflow)

    sp = sub.add_parser("taint", help="análise de taint: input não-confiável → sink perigoso")
    sp.add_argument("--scope", default=None, help="restringe a um diretório")
    sp.add_argument("--entry", default=None,
                    help="função-entrada: assume seus parâmetros como não-confiáveis")
    sp.add_argument("--depth", type=int, default=None,
                    help="saltos inter-procedurais (default por modo: scan raso, entry fundo)")
    sp.add_argument("--max-findings", type=int, default=100, help="teto de achados")
    sp.add_argument("--deadline-ms", type=int, default=None,
                    help="teto de tempo (ms): devolve PARCIAL + truncated ao estourar")
    sp.add_argument("--max-steps", type=int, default=None,
                    help="teto DETERMINÍSTICO de passos: devolve PARCIAL + truncated")
    sp.set_defaults(fn=cmd_taint)

    sp = sub.add_parser("reaches", help="reachability entry→sink numa resposta só "
                        "(cadeia + verdict de validação)")
    sp.add_argument("symbol", help="fqn da função-entrada")
    sp.add_argument("--sink", default="http",
                    help="preset (http|sql|exec|file) ou regex sobre o nome da chamada")
    sp.add_argument("--via", default=None,
                    help="validador/sanitizer a checar no caminho (ex.: sanitize)")
    sp.add_argument("--depth", type=int, default=8, help="máx. de saltos")
    sp.add_argument("--max-paths", type=int, default=20, help="teto de caminhos")
    sp.add_argument("--deadline-ms", type=int, default=None,
                    help="teto de tempo (ms): devolve PARCIAL + truncated ao estourar")
    sp.add_argument("--max-steps", type=int, default=None,
                    help="teto DETERMINÍSTICO de passos: devolve PARCIAL + truncated")
    sp.set_defaults(fn=cmd_reaches)

    sp = sub.add_parser("visualize",
                        help="exporta um subgrafo de investigação (HTML/JSON)")
    sp.add_argument("--mode", default=None,
                    choices=("file", "symbol", "modules", "symbols",
                             "neighborhood", "callers", "callees", "impact",
                             "domains"),
                    help="modo do subgrafo (default: file)")
    sp.add_argument("--level", choices=("file", "symbol"), default=None,
                    help="legado: alias de --mode file/symbol")
    sp.add_argument("--symbol", default=None,
                    help="semente dos modos neighborhood/callers/callees/impact")
    sp.add_argument("--depth", type=int, default=3, help="saltos nos modos semeados")
    sp.add_argument("--min-confidence", dest="min_confidence", default=None,
                    choices=("certain", "inferred", "possible"),
                    help="filtra arestas por confiança mínima")
    sp.add_argument("--language", default=None, help="filtra nós por linguagem")
    sp.add_argument("--changed", default=None,
                    help="caminhos/diff a destacar (ou semear o impacto)")
    sp.add_argument("--git", action="store_true",
                    help="destaca os arquivos alterados no worktree git")
    sp.add_argument("--git-ref", dest="git_ref", default=None,
                    help="destaca arquivos alterados vs. este ref git (ex.: main)")
    sp.add_argument("--staged", action="store_true",
                    help="usa só os arquivos staged do git")
    sp.add_argument("--scope", default=None, help="restringe a um diretório")
    sp.add_argument("--top", type=int, default=250, help="máx. de nós (mais conectados)")
    sp.add_argument("--json", action="store_true", help="exporta dados JSON em vez de HTML")
    sp.add_argument("--out", default=None, help="caminho de saída")
    sp.set_defaults(fn=cmd_visualize)

    sp = sub.add_parser("describe", help="descrição LLM de símbolo, módulo ou domínio (L3)")
    sp.add_argument("target", nargs="?", default=None,
                    help="fqn de símbolo, caminho de arquivo, ou domain:N")
    sp.add_argument("--refresh", action="store_true", help="re-gera mesmo com cache")
    sp.add_argument("--top", type=int, default=0,
                    help="pré-gera para os N símbolos de maior PageRank (hubs)")
    sp.set_defaults(fn=cmd_describe)

    sp = sub.add_parser("eval", help="avaliação: baseline grep/read vs grafo (M6)")
    sp.add_argument("--tasks", default="evals/tasks.json")
    sp.add_argument("--arms", default=None, help="ex.: baseline,codegraph")
    sp.add_argument("--max-steps", type=int, default=12)
    sp.add_argument("--model", default=None)
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("stats", help="estatísticas do índice")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("doctor", help="diagnóstico de saúde do índice "
                        "(parse, confiança, resolvers L1, staleness)")
    sp.add_argument("--why", action="store_true",
                    help="re-parseia os arquivos 'failed' e mostra o motivo")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("capabilities",
                        help="mapa por linguagem: o que está pronto e o que falta")
    sp.set_defaults(fn=cmd_capabilities)

    sp = sub.add_parser("vacuum", help="reconstrói o índice e recupera espaço "
                        "(re-index --force + VACUUM; preserva descrições L3)")
    sp.set_defaults(fn=cmd_vacuum)

    sp = sub.add_parser("watch", help="indexa e observa mudanças continuamente")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("mcp", help="inicia o servidor MCP (stdio)")
    sp.add_argument("--no-watch", action="store_true",
                    help="desliga o watcher em background (read-repair continua)")
    sp.set_defaults(fn=cmd_mcp)

    args = p.parse_args(argv)
    args.root = str(Path(args.root).resolve())
    try:
        return args.fn(args)
    except (AmbiguousSymbol, SymbolNotFound) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
