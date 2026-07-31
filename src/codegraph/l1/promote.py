"""Promoção de arestas resolvidas pelo L1 — compartilhada pelos três resolvers.

Antes, cada resolver descartava em silêncio quando o servidor devolvia != 1
definição (`if len(locs) != 1: continue`). Mas uma chamada resolver para VÁRIAS
definições é informação semântica real — overloads, uma interface e suas impls,
declarações múltiplas (partial class, decl+def em C++) — e não o palpite por nome
do L0. Jogar fora deixava a aresta como `possible` do L0, pior do que o servidor
já sabia.

Modelo de confiança:
- 1 alvo no repo  → `certain`  (resolução semântica única);
- 2..MAX alvos    → fan-out `inferred` (overloads/múltiplas defs — semânticas,
                    logo mais fortes que o `possible` por nome, mas não únicas);
- 0 alvos no repo → nada muda (externo/stdlib → segue como estava, fallback L0);
- > MAX alvos     → nada muda (ambíguo demais; L0 permanece).

A camada de transparência (explain) distingue `l1`+`inferred` (overloads) do
`l0`+`inferred` (alvo único por nome) pelo rótulo do resolver.
"""

from __future__ import annotations

import sqlite3

# teto de alvos L1 por site (espelha indexer.MAX_CANDIDATES): acima disso o
# resultado é ruído, não overload — deixa o L0 no comando.
MAX_L1_TARGETS = 5


def target_symbol(conn: sqlite3.Connection, drel: str, dline: int):
    """Símbolo do repo que CONTÉM (drel, dline) — o menor span que cobre a linha.
    None se o arquivo não está no índice (def fora do repo) ou nada cobre."""
    drow = conn.execute("SELECT id FROM files WHERE path=?", (drel,)).fetchone()
    if drow is None:
        return None
    srow = conn.execute(
        "SELECT id FROM symbols WHERE file_id=? AND start_line<=? AND end_line>=? "
        "ORDER BY (end_line-start_line) LIMIT 1",
        (drow["id"], dline, dline)).fetchone()
    return srow["id"] if srow is not None else None


def apply(conn: sqlite3.Connection, file_id: int, edge, target_ids,
          resolver: str = "l1") -> int:
    """Promove o call site de `edge` para o(s) símbolo(s)-alvo do L1.

    `edge` precisa expor id, line, col. `target_ids` é a lista de símbolos que o
    servidor resolveu (duplicatas e ordem toleradas). Retorna 1 se promoveu o
    site, 0 caso contrário. Idempotente: a aresta original vira `l1` e sai do
    filtro `resolver='l0'` das próximas passadas; o índice único + INSERT OR
    IGNORE impedem clones duplicados."""
    ids = list(dict.fromkeys(t for t in target_ids if t is not None))
    if not ids or len(ids) > MAX_L1_TARGETS:
        return 0
    conf = "certain" if len(ids) == 1 else "inferred"
    conn.execute(
        "UPDATE edges SET dst=?, confidence=?, resolver=? WHERE id=?",
        (ids[0], conf, resolver, edge["id"]))
    # Ordem importa: apaga os clones 'possible' do L0 no mesmo site ANTES de
    # inserir os L1. O fan-out do L0 pode já ter uma aresta com um dos dst dos
    # overloads; sem apagar primeiro, o INSERT OR IGNORE colidiria no índice
    # único e depois o DELETE levaria o alvo embora — perdendo o overload.
    conn.execute(
        "DELETE FROM edges WHERE kind='calls' AND file_id=? AND line=? AND col=? "
        "AND id!=? AND resolver='l0' AND confidence='possible'",
        (file_id, edge["line"], edge["col"], edge["id"]))
    # clones para os alvos extras (overloads): copia kind/src/dst_name/site da
    # aresta original, trocando só o dst. dst distinto → passa no índice único.
    for sid in ids[1:]:
        conn.execute(
            "INSERT OR IGNORE INTO edges(kind, src, dst, dst_name, file_id, line, "
            "col, confidence, resolver) SELECT kind, src, ?, dst_name, file_id, "
            "line, col, ?, ? FROM edges WHERE id=?",
            (sid, conf, resolver, edge["id"]))
    return 1
