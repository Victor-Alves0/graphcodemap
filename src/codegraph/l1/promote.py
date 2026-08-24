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

Cada nova passada primeiro devolve provas L1 anteriores ao fallback L0. Isso é
necessário porque o universo semântico pode mudar sem o arquivo chamador mudar
(novo override, classpath/build model diferente). Uma falha parcial do resolver
não pode conservar uma certeza produzida para outro universo.

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

# Declarações de tipo podem ser destinos legítimos de construção, mas não
# de uma chamada de método comum. O chamador semântico precisa provar que o site
# é uma construção antes de aceitar um desses contêineres amplos.
TYPE_DECLARATION_KINDS = frozenset({
    "class", "interface", "enum", "record", "struct", "trait",
})


def reset_sites(conn: sqlite3.Connection, languages, rels=None) -> int:
    """Colapsa sites L1 do escopo em um representante dangling ``possible/l0``.

    A operação inteira usa um savepoint: leitores nunca recebem um fan-out pela
    metade e uma exceção não deixa clones parcialmente removidos. O chamador deve
    rodar a resolução L0 depois, antes de iniciar os servidores semânticos.
    Retorna o número de callsites cuja prova anterior foi invalidada.
    """
    languages = tuple(dict.fromkeys(languages))
    if not languages:
        return 0
    where = ["e.kind='calls'", "e.resolver='l1'",
             f"f.language IN ({','.join('?' * len(languages))})"]
    args = list(languages)
    if rels is not None:
        rels = tuple(dict.fromkeys(rels))
        if not rels:
            return 0
        where.append(f"f.path IN ({','.join('?' * len(rels))})")
        args.extend(rels)
    sites = conn.execute(
        "SELECT DISTINCT e.file_id, e.line, e.col FROM edges e "
        "JOIN files f ON f.id=e.file_id WHERE " + " AND ".join(where)
        + " ORDER BY e.file_id, e.line, e.col",
        args,
    ).fetchall()
    if not sites:
        return 0

    conn.execute("SAVEPOINT l1_reset_sites")
    try:
        for site in sites:
            rows = conn.execute(
                "SELECT id FROM edges WHERE kind='calls' AND file_id=? "
                "AND line IS ? AND col IS ? ORDER BY id",
                (site["file_id"], site["line"], site["col"]),
            ).fetchall()
            if not rows:
                continue
            keep = rows[0]["id"]
            conn.execute(
                "UPDATE edges SET dst=NULL, confidence='possible', resolver='l0' "
                "WHERE id=?", (keep,),
            )
            if len(rows) > 1:
                conn.executemany(
                    "DELETE FROM edges WHERE id=?",
                    ((row["id"],) for row in rows[1:]),
                )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT l1_reset_sites")
        conn.execute("RELEASE SAVEPOINT l1_reset_sites")
        raise
    conn.execute("RELEASE SAVEPOINT l1_reset_sites")
    return len(sites)


def target_symbol(conn: sqlite3.Connection, drel: str, dline: int,
                  dcol: int | None = None, dname: str | None = None,
                  *, allow_type: bool = True):
    """Menor símbolo chamável que contém a posição devolvida pelo L1.

    ``dcol`` é opcional porque LSPs genéricos antigos só entregam linha. JS/TS
    fornece a coluna exata, reduzindo colisões quando várias definições ocupam a
    mesma linha. Retorna ``None`` para defs externas, ausentes ou cujo menor
    contêiner seja dado/configuração em vez de código chamável. Quando
    ``allow_type`` é falso, uma definição que cai apenas no span de uma classe
    ou tipo falha fechada em vez de fabricar uma chamada ao contêiner.
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
    if (srow is None or srow["kind"] in NON_CALLABLE_KINDS
            or (not allow_type and srow["kind"] in TYPE_DECLARATION_KINDS)):
        return None
    return srow["id"]


def apply(conn: sqlite3.Connection, file_id: int, edge, target_ids,
          resolver: str = "l1") -> int:
    """Promove o call site de `edge` para o(s) símbolo(s)-alvo do L1.

    `edge` precisa expor id, line, col. `target_ids` é a lista de símbolos que o
    servidor resolveu (duplicatas e ordem toleradas). Retorna 1 se promoveu o
    site, 0 caso contrário. O índice único + INSERT OR IGNORE impedem clones
    duplicados; um savepoint torna a substituição do fan-out atômica."""
    ids = list(dict.fromkeys(t for t in target_ids if t is not None))
    if not ids or len(ids) > MAX_L1_TARGETS:
        return 0
    conf = "certain" if len(ids) == 1 else "inferred"
    # O fan-out L0 pode já ter um clone com o dst escolhido pelo L1. Limpar
    # antes do UPDATE evita colisão no índice único e é idempotente: o alvo é
    # recriado abaixo com a confiança semântica correta.
    ph = ",".join("?" * len(ids))
    conn.execute("SAVEPOINT l1_apply_site")
    try:
        conn.execute(
            f"DELETE FROM edges WHERE kind='calls' AND file_id=? AND line=? AND col=? "
            f"AND id!=? AND ((resolver='l0' AND confidence='possible') "
            f"OR dst IN ({ph}))",
            (file_id, edge["line"], edge["col"], edge["id"], *ids))
        conn.execute(
            "UPDATE edges SET dst=?, confidence=?, resolver=? WHERE id=?",
            (ids[0], conf, resolver, edge["id"]))
        # clones para os alvos extras (overloads): copia kind/src/dst_name/site
        # da original, trocando só o dst. dst distinto passa no índice único.
        for sid in ids[1:]:
            conn.execute(
                "INSERT OR IGNORE INTO edges(kind, src, dst, dst_name, file_id, "
                "line, col, confidence, resolver) SELECT kind, src, ?, dst_name, "
                "file_id, line, col, ?, ? FROM edges WHERE id=?",
                (sid, conf, resolver, edge["id"]))
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT l1_apply_site")
        conn.execute("RELEASE SAVEPOINT l1_apply_site")
        raise
    conn.execute("RELEASE SAVEPOINT l1_apply_site")
    return 1
