"""Servidor MCP (stdio): expõe as tools do grafo para qualquer agente.

As tools são a camada estrutural AO LADO de grep/read do agente (DESIGN §0.4):
localizam e navegam; o agente lê o código nos spans retornados.
"""

from __future__ import annotations

from pathlib import Path

from . import agent, render
from .indexer import Indexer
from .query import AmbiguousSymbol, QueryEngine, SymbolNotFound

INSTRUCTIONS = """\
CodeGraph: grafo estrutural do repositório (símbolos, call graph, impacto),
sempre fresco — cada consulta confere content-hashes e re-indexa o que mudou.

Como usar bem (e barato):
- Para achar texto/definição simples, grep/read direto costuma bastar — use o
  grafo quando a pergunta é ESTRUTURAL: quem chama, o que quebra, arquitetura.
- `overview` primeiro num repo desconhecido; `callers`/`impact` antes de
  modificar algo.
- Reachability "input entra aqui → chega a operação perigosa?": use `reaches`
  (uma resposta com a cadeia + veredito de validador) em vez de montar o caminho
  salto a salto lendo arquivos.
- As tools LOCALIZAM; leia o código nos spans `path:linha` retornados.
- CONFIE na confiança: [certain] = relação resolvida semanticamente (L1) — é um
  fato, NÃO precisa reler o código para confirmar; pare e responda. [inferred] =
  provável (nome único), confira só se for crítico. [possible] = palpite, aí sim
  verifique. Não gaste tokens re-verificando o que já veio [certain].
- Avisos ⚠ de completeness: análise estática, chamadas dinâmicas podem faltar.

Formato da resposta: cada tool devolve um envelope estável — `text` (o resumo
compacto, para você ler), `results` (as linhas estruturadas), e os sinais
`confidence` (certain/inferred/possible/mixed), `fresh`, `truncated` e
`completeness` (static_analysis, unresolved_edges, dynamic_dispatch_possible).

Fluxo do agente (tools de alto nível): `change_impact(paths_ou_diff)` e
`find_affected_modules(...)` para “o que essa mudança quebra/toca”;
`find_related_tests(symbol)` para “o que já testa isto”;
`explain_symbol(symbol)` para uma ficha rica sem reler o código;
`suggest_files_to_read(task)` para começar uma tarefa.
"""


def build_server(root: str | Path, db_path: str | Path | None = None,
                 watch: bool = False):
    from mcp.server.fastmcp import FastMCP

    indexer = Indexer(root, db_path)
    # varredura de boot (DESIGN §2.1): incremental — barata se nada mudou,
    # e garante que o servidor nunca nasce com índice vazio/velho
    indexer.index_repo()
    engine = QueryEngine(indexer)

    # M4 (§4): refinamento L1 assíncrono — o grafo L0 já responde; arestas
    # vão sendo promovidas a 'certain' em background (conexão própria)
    import threading

    def _refine_async() -> None:
        from . import l1

        own = Indexer(root, db_path)
        try:
            l1.refine(own)
        finally:
            own.close()

    threading.Thread(target=_refine_async, daemon=True).start()
    if watch:
        # M2 (§2.2): mantém o índice quente em background; conexão própria.
        # A garantia continua sendo o read-repair na query — mas com o watcher
        # ligado ao engine, uma query só paga a varredura O(N) quando o watcher
        # NÃO está drenado (ou a cada backstop), não a cada miss.
        from .watcher import Watcher

        _watcher = Watcher(root, db_path)
        _watcher.start()
        engine.attach_watcher(_watcher)
    mcp = FastMCP("codegraph", instructions=INSTRUCTIONS)

    # o engine é UMA conexão SQLite; FastMCP pode despachar tools em paralelo
    # (threadpool). Uma conexão sqlite não é thread-safe → serializa o acesso.
    # Barato: queries são ms; a correção do read-repair (que escreve) exige
    # exclusão mútua de qualquer forma. Escritores de background (watcher/refine)
    # usam conexões próprias e contam com o retry-on-locked do db.
    _engine_lock = threading.RLock()

    def guard(fn) -> agent.Response:
        with _engine_lock:
            try:
                return fn()
            except (AmbiguousSymbol, SymbolNotFound) as e:
                return agent.error(str(e))

    @mcp.tool()
    def overview(scope: str | None = None, token_budget: int = 1200) -> agent.Response:
        """Mapa ranqueado do repo (PageRank). Primeiro passo em repo novo."""
        def run():
            entries, env = engine.overview(scope=scope, token_budget=token_budget)
            return agent.build(render.overview(entries, env), env, results=entries)
        return guard(run)

    @mcp.tool()
    def find_symbol(query: str, kind: str | None = None,
                    limit: int = 10) -> agent.Response:
        """Localiza símbolos por nome/fqn (kind: function|method|class|…)."""
        def run():
            rows, env = engine.find_symbol(query, kind=kind, limit=limit)
            return agent.build(render.find(query, rows, env), env, results=rows)
        return guard(run)

    @mcp.tool()
    def symbol_info(symbol: str) -> agent.Response:
        """Ficha do símbolo: assinatura, doc, span, contagens."""
        def run():
            info, env = engine.symbol_info(symbol)
            return agent.build(render.info(info, env), env, results=[info])
        return guard(run)

    @mcp.tool()
    def references(symbol: str, kind: str | None = None) -> agent.Response:
        """Usos do símbolo (kind: calls|imports|inherits)."""
        def run():
            sym, rows, env = engine.references(symbol, kind=kind)
            return agent.build(render.refs(sym, rows, env), env, results=rows)
        return guard(run)

    @mcp.tool()
    def callers(symbol: str, depth: int = 1) -> agent.Response:
        """Quem chama o símbolo. Use antes de mudar assinatura/comportamento."""
        def run():
            sym, rows, env = engine.callers(symbol, depth=depth)
            return agent.build(
                render.calls(sym, rows, env, "callers de", "in"), env, results=rows)
        return guard(run)

    @mcp.tool()
    def callees(symbol: str, depth: int = 1) -> agent.Response:
        """O que o símbolo chama."""
        def run():
            sym, rows, env = engine.callees(symbol, depth=depth)
            return agent.build(
                render.calls(sym, rows, env, "callees de", "out"), env, results=rows)
        return guard(run)

    @mcp.tool()
    def impact(symbol: str, depth: int = 3) -> agent.Response:
        """Dependentes transitivos: o que pode quebrar se o símbolo mudar."""
        def run():
            sym, rows, env = engine.impact(symbol, depth=depth)
            return agent.build(render.impact(sym, rows, env), env, results=rows)
        return guard(run)

    @mcp.tool()
    def ego_graph(symbol: str) -> agent.Response:
        """Vizinhança imediata do símbolo no grafo (in/out/containment)."""
        def run():
            data, env = engine.ego_graph(symbol)
            return agent.build(render.ego(data, env), env,
                               results=data["in"] + data["out"])
        return guard(run)

    # -- tools de alto nível (fluxo do agente) --------------------------------

    @mcp.tool()
    def change_impact(paths_or_diff: str, depth: int = 3) -> agent.Response:
        """Impacto de uma mudança: dados CAMINHOS (vírgula/espaço) ou um DIFF
        unificado, quais símbolos alterados têm dependentes e o fecho transitivo
        deles — o que revisar/re-testar. Comece por aqui ao avaliar um patch."""
        def run():
            data, env = engine.change_impact(paths_or_diff, depth=depth)
            return agent.build(render.change_impact(data, env), env,
                               results=data["impacted"])
        return guard(run)

    @mcp.tool()
    def find_affected_modules(paths_or_diff: str, depth: int = 3) -> agent.Response:
        """`change_impact` agregado por ARQUIVO: quais módulos uma mudança toca e
        com que profundidade — visão de alto nível do que abrir."""
        def run():
            data, env = engine.find_affected_modules(paths_or_diff, depth=depth)
            return agent.build(render.affected_modules(data, env), env,
                               results=data["modules"])
        return guard(run)

    @mcp.tool()
    def find_related_tests(symbol: str, depth: int = 3) -> agent.Response:
        """Testes que exercitam um símbolo: callers transitivos em arquivos de
        teste (test_*, *_test, *Test, *Spec, tests/…). O que já cobre isto."""
        def run():
            data, env = engine.find_related_tests(symbol, depth=depth)
            return agent.build(render.related_tests(data, env), env,
                               results=data["tests"])
        return guard(run)

    @mcp.tool()
    def explain_symbol(symbol: str) -> agent.Response:
        """Ficha rica de um símbolo (assinatura, doc, usos, vizinhança, domínio)
        para decidir sem reler o código. Sem custo de LLM (use `describe` para
        uma explicação de comportamento gerada por LLM)."""
        def run():
            data, env = engine.explain_symbol(symbol)
            return agent.build(render.explain_symbol(data, env), env,
                               results=data["callers"] + data["callees"])
        return guard(run)

    @mcp.tool()
    def suggest_files_to_read(task: str, limit: int = 8) -> agent.Response:
        """Arquivos mais relevantes para uma TAREFA em linguagem natural
        (casa termos → símbolos → ranqueia arquivos por importância no grafo).
        Ponto de partida ao começar uma tarefa num repo desconhecido."""
        def run():
            data, env = engine.suggest_files_to_read(task, limit=limit)
            return agent.build(render.suggest_files(data, env), env,
                               results=data["files"])
        return guard(run)

    # -- segurança / arquitetura ----------------------------------------------

    @mcp.tool()
    def dataflow(symbol: str, depth: int = 2) -> agent.Response:
        """Fluxo de dados de uma função: para onde vão os dados de cada
        parâmetro (quais chamadas recebem, se alcançam o retorno), seguindo o
        call graph até `depth` saltos. Use para segurança (input não-confiável
        → sink) e refatoração (impacto real de mudar um argumento/tipo).
        may-taint intra-procedural (over-aproxima) — trate os fluxos como
        candidatos a verificar. 17 linguagens (py, js/ts, java, c#, c/c++, go,
        rust, ruby, php, kotlin, swift, scala, lua)."""
        def run():
            data, env = engine.data_flow(symbol, depth=depth)
            return agent.build(render.dataflow(data, env), env,
                               results=data.get("params", []))
        return guard(run)

    @mcp.tool()
    def taint(scope: str | None = None, entry: str | None = None,
              depth: int = 4) -> agent.Response:
        """Análise de taint (segurança): rastreia input não-confiável (sources)
        até operações perigosas (sinks: eval/exec/execute/system/...), com
        sanitizers cortando o fluxo, interprocedural pelo call graph. Sem args =
        varre o repo; `entry=fqn` assume os parâmetros dessa função como
        não-confiáveis. Regras ajustáveis em .codegraph/taint.json. Achados são
        candidatos (may-taint, over-aproxima) — confirme lendo o código.
        17 linguagens (mesmas do dataflow)."""
        def run():
            data, env = engine.taint(scope=scope, entry=entry, depth=depth)
            return agent.build(render.taint(data, env), env,
                               results=data.get("findings", []))
        return guard(run)

    @mcp.tool()
    def reaches(symbol: str, sink: str = "http", via: str | None = None,
                depth: int = 8) -> agent.Response:
        """Reachability endpoint→sink numa resposta só: seguindo o call graph a
        partir de `symbol`, quais caminhos chegam a um sink perigoso, e um
        validador aparece no meio? `sink`: preset ('http', 'sql', 'exec',
        'file') ou regex sobre o nome da chamada. `via`: nome do validador/
        sanitizer a checar no caminho (ex.: 'sanitize'). Devolve a cadeia de
        funções entry→sink + veredito de validação — evita o agente montar a
        travessia salto a salto lendo código. Estático (arestas 'calls'):
        chamadas dinâmicas podem faltar; confiança = mínima do caminho."""
        def run():
            sym, data, env = engine.reaches(symbol, sink=sink, via=via, depth=depth)
            return agent.build(render.reaches(sym, data, env), env,
                               results=data.get("paths", []))
        return guard(run)

    @mcp.tool()
    def communities(limit: int = 20, min_size: int = 3) -> agent.Response:
        """Domínios/subsistemas do repo (clustering do grafo) com seus hubs e
        arquivos. Mapa de alto nível que não está escrito em arquivo nenhum —
        bom depois de `overview` para entender a arquitetura. Rotule um domínio
        com describe('domain:N')."""
        def run():
            items, meta, env = engine.communities(limit=limit, min_size=min_size)
            return agent.build(render.communities(items, meta, env), env,
                               results=items)
        return guard(run)

    @mcp.tool()
    def describe(target: str, refresh: bool = False) -> agent.Response:
        """Descrição LLM do COMPORTAMENTO de um símbolo (fqn), módulo
        (caminho de arquivo) ou domínio (`domain:N` de communities). Cacheada e
        invalidada por hash do código; respostas STALE vêm marcadas.
        `refresh=True` re-gera agora."""
        from .l3 import L3Unavailable

        def run():
            try:
                data, env = engine.describe(target, refresh=refresh)
                return agent.build(render.describe(data, env), env)
            except L3Unavailable as e:
                return agent.error(str(e))

        return guard(run)

    @mcp.tool()
    def index_status() -> agent.Response:
        """Estatísticas do índice: arquivos, símbolos, arestas resolvidas/
        pendentes, linguagens."""
        def run():
            s = engine.stats()
            from .query import Envelope
            return agent.build(render.stats(s), Envelope(), results=[s])
        return guard(run)

    @mcp.tool()
    def doctor() -> agent.Response:
        """Diagnóstico de saúde do índice: parse (arquivos ok/falhos),
        distribuição de confiança das chamadas (certain/inferred/possible), %
        certain, resolvers L1 ativos, staleness (idade do último scan) e
        arquivos que falharam no parse. Use para decidir o quanto confiar nas
        respostas do grafo, ou para diagnosticar por que algo não aparece."""
        def run():
            d = engine.doctor()
            from .query import Envelope
            return agent.build(render.doctor(d), Envelope(), results=[d])
        return guard(run)

    return mcp


def serve(root: str | Path, db_path: str | Path | None = None,
          watch: bool = True) -> None:
    build_server(root, db_path, watch=watch).run()


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="codegraph-mcp")
    p.add_argument("--root", default=".", help="raiz do repo")
    p.add_argument("--db", default=None)
    p.add_argument("--no-watch", action="store_true",
                   help="desliga o watcher em background")
    args = p.parse_args()
    serve(str(Path(args.root).resolve()), args.db, watch=not args.no_watch)


if __name__ == "__main__":
    main()
