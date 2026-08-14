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

# Kinds que representam contêineres/dados, nunca o destino de uma chamada.
# ``target_symbol`` é deliberadamente conservador: uma promoção ausente mantém
# o fallback L0; uma promoção ``certain`` para uma variável/arquivo fabrica uma
# relação semântica e pode fazer o agente ou o motor de taint confiar no alvo
# errado. A lista cobre os kinds emitidos pelos extractors dedicados e genéricos.
NON_CALLABLE_KINDS = frozenset({
    "variable", "constant", "field", "property", "file", "module",
    "key", "section", "html_id", "css_class", "css_id", "type_alias",
    "interface", "enum",
})


def target_symbol(conn: sqlite3.Connection, drel: str, dline: int,
                  dcol: int | None = None, dname: str | None = None):
    """Menor símbolo chamável que contém a posição devolvida pelo L1.

    ``dcol`` é opcional porque LSPs genéricos antigos só entregam linha. JS/TS
    fornece a coluna exata, reduzindo colisões quando várias definições ocupam a
    mesma linha. Retorna ``None`` para defs externas, ausentes ou cujo menor
    contêiner seja dado/configuração em vez de código chamável.
    """
    drow = conn.execute("SELECT id FROM files WHERE path=?", (drel,)).fetchone()
    if drow is None:
        return None
    name_sql = " AND name=?" if dname else ""
    name_args = (dname,) if dname else ()
    if dcol is None:
        srow = conn.execute(
            "SELECT id, kind FROM symbols WHERE file_id=? "
            "AND start_line<=? AND end_line>=? "
            + name_sql +
            "ORDER BY (end_line-start_line), (end_col-start_col) LIMIT 1",
            (drow["id"], dline, dline, *name_args)).fetchone()
    else:
        srow = conn.execute(
            "SELECT id, kind FROM symbols WHERE file_id=? "
            "AND (start_line<? OR (start_line=? AND start_col<=?)) "
            "AND (end_line>? OR (end_line=? AND end_col>=?)) "
            + name_sql +
            "ORDER BY (end_line-start_line), (end_col-start_col) LIMIT 1",
            (drow["id"], dline, dline, dcol,
             dline, dline, dcol, *name_args)).fetchone()
    if srow is None or srow["kind"] in NON_CALLABLE_KINDS:
        return None
    return srow["id"]


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
    # O fan-out L0 pode já ter um clone com o dst escolhido pelo L1. Limpar
    # antes do UPDATE evita colisão no índice único e é idempotente: o alvo é
    # recriado abaixo com a confiança semântica correta.
    ph = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM edges WHERE kind='calls' AND file_id=? AND line=? AND col=? "
        f"AND id!=? AND ((resolver='l0' AND confidence='possible') "
        f"OR dst IN ({ph}))",
        (file_id, edge["line"], edge["col"], edge["id"], *ids))
    conn.execute(
        "UPDATE edges SET dst=?, confidence=?, resolver=? WHERE id=?",
        (ids[0], conf, resolver, edge["id"]))
    # clones para os alvos extras (overloads): copia kind/src/dst_name/site da
    # aresta original, trocando só o dst. dst distinto → passa no índice único.
    for sid in ids[1:]:
        conn.execute(
            "INSERT OR IGNORE INTO edges(kind, src, dst, dst_name, file_id, line, "
            "col, confidence, resolver) SELECT kind, src, ?, dst_name, file_id, "
            "line, col, ?, ? FROM edges WHERE id=?",
            (sid, conf, resolver, edge["id"]))
    return 1
