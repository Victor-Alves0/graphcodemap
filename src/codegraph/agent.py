"""Camada orientada a agentes: envelope estruturado estável sobre as respostas.

O texto (render.*) segue compacto para exibição; ao lado, cada resposta MCP
carrega campos CONCEITUAIS consistentes que um agente consome sem parsear texto:

    {results, text, confidence, fresh, completeness{...}, truncated}

`confidence` é agregado das arestas do resultado (certain/inferred/possible →
"mixed" quando misturam). `completeness`/`fresh`/`truncated` vêm da Envelope, que
os calcula no mesmo ponto em que emite os avisos ⚠ — os mesmos fatos, em campos
de máquina. Honesto por construção: nada aqui é decorativo."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_CONF_RANK = {"certain": 0, "inferred": 1, "possible": 2}


def aggregate_confidence(rows: list[dict]) -> str:
    """Confiança agregada de um conjunto de arestas: o rótulo único quando todas
    concordam, senão 'mixed'; 'n/a' quando não há aresta com confiança."""
    confs = {r.get("confidence") for r in rows if r.get("confidence")}
    confs.discard(None)
    if not confs:
        return "n/a"
    if len(confs) == 1:
        return next(iter(confs))
    return "mixed"


class Completeness(BaseModel):
    static_analysis: bool          # a resposta vem de análise estática
    unresolved_edges: int          # arestas relevantes ainda não resolvidas
    dynamic_dispatch_possible: bool  # chamadas dinâmicas/reflexivas podem faltar


class Response(BaseModel):
    """Envelope estável devolvido por toda tool MCP.

    `text` é o mesmo render compacto de sempre (exibição); `results` são as linhas
    estruturadas quando a tool as tem (navegáveis por máquina). Os demais campos
    são os sinais conceituais consistentes."""

    text: str
    results: list[dict[str, Any]] = []
    confidence: str = "n/a"
    fresh: bool = True
    semantic_status: str = "not_started"
    completeness: Completeness
    truncated: bool = False
    warnings: list[str] = []


def error(msg: str) -> Response:
    """Response de erro (símbolo não encontrado/ambíguo): texto do erro, envelope
    neutro. Mantém o contrato estável mesmo no caminho de falha."""
    return Response(
        text=f"erro: {msg}", results=[], confidence="n/a", fresh=True,
        truncated=False,
        completeness=Completeness(static_analysis=True, unresolved_edges=0,
                                  dynamic_dispatch_possible=False))


def build(text: str, env, results: list[dict] | None = None,
          confidence: str | None = None) -> Response:
    """Monta o Response a partir do texto renderizado + a Envelope da consulta.

    `confidence` pode ser passado explicitamente (agregado das arestas do
    resultado); senão infere de `results`."""
    results = results or []
    return Response(
        text=text,
        results=results,
        confidence=confidence if confidence is not None
        else aggregate_confidence(results),
        fresh=env.fresh,
        semantic_status=getattr(env, "semantic_status", "not_started"),
        truncated=env.truncated,
        warnings=list(env.warnings),
        completeness=Completeness(
            static_analysis=env.static_analysis,
            unresolved_edges=env.unresolved_edges,
            dynamic_dispatch_possible=env.dynamic_dispatch,
        ),
    )
