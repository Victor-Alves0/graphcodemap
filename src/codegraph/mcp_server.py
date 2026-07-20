"""Servidor MCP (stdio): expõe as tools do grafo para qualquer agente.

As tools são a camada estrutural AO LADO de grep/read do agente (DESIGN §0.4):
localizam e navegam; o agente lê o código nos spans retornados.
"""

from __future__ import annotations

from pathlib import Path

from . import render
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
        # A garantia continua sendo o read-repair na query.
        from .watcher import Watcher

        Watcher(root, db_path).start()
    mcp = FastMCP("codegraph", instructions=INSTRUCTIONS)

    # o engine é UMA conexão SQLite; FastMCP pode despachar tools em paralelo
    # (threadpool). Uma conexão sqlite não é thread-safe → serializa o acesso.
    # Barato: queries são ms; a correção do read-repair (que escreve) exige
    # exclusão mútua de qualquer forma. Escritores de background (watcher/refine)
    # usam conexões próprias e contam com o retry-on-locked do db.
    _engine_lock = threading.RLock()

    def guard(fn):
        with _engine_lock:
            try:
                return fn()
            except (AmbiguousSymbol, SymbolNotFound) as e:
                return f"erro: {e}"

    @mcp.tool()
    def overview(scope: str | None = None, token_budget: int = 1200) -> str:
        """Mapa ranqueado do repo (PageRank). Primeiro passo em repo novo."""
        return guard(lambda: render.overview(
            *engine.overview(scope=scope, token_budget=token_budget)))

    @mcp.tool()
    def find_symbol(query: str, kind: str | None = None, limit: int = 10) -> str:
        """Localiza símbolos por nome/fqn (kind: function|method|class|…)."""
        return guard(lambda: render.find(
            query, *engine.find_symbol(query, kind=kind, limit=limit)))

    @mcp.tool()
    def symbol_info(symbol: str) -> str:
        """Ficha do símbolo: assinatura, doc, span, contagens."""
        return guard(lambda: render.info(*engine.symbol_info(symbol)))

    @mcp.tool()
    def references(symbol: str, kind: str | None = None) -> str:
        """Usos do símbolo (kind: calls|imports|inherits)."""
        return guard(lambda: render.refs(*engine.references(symbol, kind=kind)))

    @mcp.tool()
    def callers(symbol: str, depth: int = 1) -> str:
        """Quem chama o símbolo. Use antes de mudar assinatura/comportamento."""
        return guard(lambda: render.calls(*engine.callers(symbol, depth=depth),
                                          "callers de", "in"))

    @mcp.tool()
    def callees(symbol: str, depth: int = 1) -> str:
        """O que o símbolo chama."""
        return guard(lambda: render.calls(*engine.callees(symbol, depth=depth),
                                          "callees de", "out"))

    @mcp.tool()
    def impact(symbol: str, depth: int = 3) -> str:
        """Dependentes transitivos: o que pode quebrar se o símbolo mudar."""
        return guard(lambda: render.impact(*engine.impact(symbol, depth=depth)))

    @mcp.tool()
    def ego_graph(symbol: str) -> str:
        """Vizinhança imediata do símbolo no grafo (in/out/containment)."""
        return guard(lambda: render.ego(*engine.ego_graph(symbol)))

    @mcp.tool()
    def dataflow(symbol: str, depth: int = 2) -> str:
        """Fluxo de dados de uma função: para onde vão os dados de cada
        parâmetro (quais chamadas recebem, se alcançam o retorno), seguindo o
        call graph até `depth` saltos. Use para segurança (input não-confiável
        → sink) e refatoração (impacto real de mudar um argumento/tipo).
        may-taint intra-procedural (over-aproxima) — trate os fluxos como
        candidatos a verificar. 17 linguagens (py, js/ts, java, c#, c/c++, go,
        rust, ruby, php, kotlin, swift, scala, lua)."""
        return guard(lambda: render.dataflow(*engine.data_flow(symbol, depth=depth)))

    @mcp.tool()
    def taint(scope: str | None = None, entry: str | None = None, depth: int = 4) -> str:
        """Análise de taint (segurança): rastreia input não-confiável (sources)
        até operações perigosas (sinks: eval/exec/execute/system/...), com
        sanitizers cortando o fluxo, interprocedural pelo call graph. Sem args =
        varre o repo; `entry=fqn` assume os parâmetros dessa função como
        não-confiáveis. Regras ajustáveis em .codegraph/taint.json. Achados são
        candidatos (may-taint, over-aproxima) — confirme lendo o código.
        17 linguagens (mesmas do dataflow)."""
        return guard(lambda: render.taint(
            *engine.taint(scope=scope, entry=entry, depth=depth)))

    @mcp.tool()
    def reaches(symbol: str, sink: str = "http", via: str | None = None,
                depth: int = 8) -> str:
        """Reachability endpoint→sink numa resposta só: seguindo o call graph a
        partir de `symbol`, quais caminhos chegam a um sink perigoso, e um
        validador aparece no meio? `sink`: preset ('http', 'sql', 'exec',
        'file') ou regex sobre o nome da chamada. `via`: nome do validador/
        sanitizer a checar no caminho (ex.: 'sanitize'). Devolve a cadeia de
        funções entry→sink + veredito de validação — evita o agente montar a
        travessia salto a salto lendo código. Estático (arestas 'calls'):
        chamadas dinâmicas podem faltar; confiança = mínima do caminho."""
        return guard(lambda: render.reaches(
            *engine.reaches(symbol, sink=sink, via=via, depth=depth)))

    @mcp.tool()
    def communities(limit: int = 20, min_size: int = 3) -> str:
        """Domínios/subsistemas do repo (clustering do grafo) com seus hubs e
        arquivos. Mapa de alto nível que não está escrito em arquivo nenhum —
        bom depois de `overview` para entender a arquitetura. Rotule um domínio
        com describe('domain:N')."""
        return guard(lambda: render.communities(
            *engine.communities(limit=limit, min_size=min_size)))

    @mcp.tool()
    def describe(target: str, refresh: bool = False) -> str:
        """Descrição LLM do COMPORTAMENTO de um símbolo (fqn), módulo
        (caminho de arquivo) ou domínio (`domain:N` de communities). Cacheada e
        invalidada por hash do código; respostas STALE vêm marcadas.
        `refresh=True` re-gera agora."""
        from .l3 import L3Unavailable

        def run():
            try:
                return render.describe(*engine.describe(target, refresh=refresh))
            except L3Unavailable as e:
                return f"erro: {e}"

        return guard(run)

    @mcp.tool()
    def index_status() -> str:
        """Estatísticas do índice: arquivos, símbolos, arestas resolvidas/
        pendentes, linguagens."""
        return guard(lambda: render.stats(engine.stats()))

    @mcp.tool()
    def doctor() -> str:
        """Diagnóstico de saúde do índice: parse (arquivos ok/falhos),
        distribuição de confiança das chamadas (certain/inferred/possible), %
        certain, resolvers L1 ativos, staleness (idade do último scan) e
        arquivos que falharam no parse. Use para decidir o quanto confiar nas
        respostas do grafo, ou para diagnosticar por que algo não aparece."""
        return guard(lambda: render.doctor(engine.doctor()))

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
