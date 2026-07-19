"""Detecção de comunidades (domínios) sobre o grafo de símbolos.

Louvain (otimização gulosa de modularidade), implementação própria — mesma
filosofia do PageRank em rank.py: sem dependência pesada, determinístico
(ordem fixa de nós → comunidades estáveis entre execuções, menos churn de
label). Recomputado lazy: re-index marca `community_dirty`; a próxima consulta
de domínios recomputa.

O grafo é tratado como não-direcionado e ponderado: peso(u,v) = nº de arestas
resolvidas (calls/imports/inherits) entre os dois símbolos, somando as duas
direções. Símbolos sem nenhuma aresta resolvida ficam fora (community=NULL):
não pertencem a nenhum domínio estrutural.
"""

from __future__ import annotations

import sqlite3

from .util import content_hash

_MIN_IMPROVEMENT = 1e-7
_STRUCTURAL_KINDS = ("calls", "imports", "inherits")


def mark_dirty(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('community_dirty','1') "
        "ON CONFLICT(key) DO UPDATE SET value='1'"
    )


def ensure_communities(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='community_dirty'").fetchone()
    if row is not None and row["value"] == "0":
        return False
    recompute(conn)
    return True


# -- construção do grafo ------------------------------------------------------

def _build_graph(conn: sqlite3.Connection):
    """Retorna (ids, adj): ids = lista de símbolos com aresta; adj = dict-de-dict
    simétrico de pesos (sem laços) sobre índices inteiros 0..N-1."""
    kinds_ph = ",".join("?" * len(_STRUCTURAL_KINDS))
    pair_w: dict[tuple[str, str], int] = {}
    for r in conn.execute(
        f"SELECT src, dst FROM edges WHERE src IS NOT NULL AND dst IS NOT NULL "
        f"AND src != dst AND kind IN ({kinds_ph})", _STRUCTURAL_KINDS
    ).fetchall():
        a, b = r["src"], r["dst"]
        key = (a, b) if a < b else (b, a)
        pair_w[key] = pair_w.get(key, 0) + 1

    idx: dict[str, int] = {}
    ids: list[str] = []

    def node(sid: str) -> int:
        i = idx.get(sid)
        if i is None:
            i = len(ids)
            idx[sid] = i
            ids.append(sid)
        return i

    adj: dict[int, dict[int, float]] = {}
    for (a, b), w in pair_w.items():
        ia, ib = node(a), node(b)
        adj.setdefault(ia, {})[ib] = float(w)
        adj.setdefault(ib, {})[ia] = float(w)
    return ids, adj


# -- Louvain ------------------------------------------------------------------

def _degrees(adj: dict[int, dict[int, float]]) -> dict[int, float]:
    # laço conta em dobro (convenção da modularidade); grafo base não tem laços,
    # mas o grafo induzido dos níveis seguintes tem
    return {n: sum(nbrs.values()) + nbrs.get(n, 0.0) for n, nbrs in adj.items()}


def _one_level(adj, node2com, gdeg, m) -> bool:
    """Move nós entre comunidades enquanto a modularidade sobe. True se mudou algo."""
    com_deg: dict[int, float] = {}
    for n, c in node2com.items():
        com_deg[c] = com_deg.get(c, 0.0) + gdeg[n]

    modified = True
    changed_any = False
    nodes = sorted(adj.keys())
    while modified:
        modified = False
        for n in nodes:
            cur = node2com[n]
            ki = gdeg[n]
            # pesos de n para cada comunidade vizinha (exclui laço)
            neigh: dict[int, float] = {}
            for v, w in adj[n].items():
                if v == n:
                    continue
                neigh[node2com[v]] = neigh.get(node2com[v], 0.0) + w
            # remove n da comunidade atual
            com_deg[cur] -= ki
            best_com, best_gain = cur, 0.0
            base = neigh.get(cur, 0.0)  # ganho de re-inserir na origem = referência
            for c, w_to_c in neigh.items():
                # ΔQ relativo de mover n para c (constante ki²/… some fora)
                gain = (w_to_c - base) - ki * (com_deg.get(c, 0.0)
                                               - com_deg.get(cur, 0.0)) / (2.0 * m)
                if gain > best_gain + _MIN_IMPROVEMENT:
                    best_gain, best_com = gain, c
            com_deg[best_com] = com_deg.get(best_com, 0.0) + ki
            node2com[n] = best_com
            if best_com != cur:
                modified = True
                changed_any = True
    return changed_any


def _induce(adj, node2com):
    """Grafo induzido: cada comunidade vira um nó; arestas somam pesos."""
    ind: dict[int, dict[int, float]] = {}
    seen = set()
    for u, nbrs in adj.items():
        cu = node2com[u]
        for v, w in nbrs.items():
            if v < u:
                continue  # cada par não-ordenado uma vez (laços u==v incluídos)
            key = (u, v)
            if key in seen:
                continue
            seen.add(key)
            cv = node2com[v]
            ind.setdefault(cu, {})
            ind.setdefault(cv, {})
            ind[cu][cv] = ind[cu].get(cv, 0.0) + w
            if cu != cv:
                ind[cv][cu] = ind[cv].get(cu, 0.0) + w
    return ind


def louvain(adj: dict[int, dict[int, float]]) -> dict[int, int]:
    """Particiona os nós; retorna node_index -> community_id (inteiros compactos)."""
    m = sum(_degrees(adj).values()) / 2.0
    if m == 0:
        return {n: i for i, n in enumerate(adj)}

    # partition[n] = comunidade final do nó ORIGINAL n
    partition = {n: n for n in adj}
    cur_adj = adj
    node2com = {n: n for n in cur_adj}

    while True:
        gdeg = _degrees(cur_adj)
        _one_level(cur_adj, node2com, gdeg, m)
        # renumera comunidades usadas para 0..k-1
        used = sorted(set(node2com.values()))
        renum = {c: i for i, c in enumerate(used)}
        node2com = {n: renum[c] for n, c in node2com.items()}
        # propaga para os nós originais
        partition = {orig: node2com[c] for orig, c in partition.items()}
        if len(used) == len(cur_adj):
            break  # nada agregou → convergiu
        cur_adj = _induce(cur_adj, node2com)
        node2com = {n: n for n in cur_adj}
    return partition


# -- persistência -------------------------------------------------------------

def recompute(conn: sqlite3.Connection) -> None:
    ids, adj = _build_graph(conn)
    conn.execute("UPDATE symbols SET community=NULL")
    if not adj:
        _finish(conn, {})
        return
    part = louvain(adj)
    # agrupa símbolos por comunidade
    members: dict[int, list[str]] = {}
    for i, sid in enumerate(ids):
        c = part.get(i)
        if c is None:
            continue
        members.setdefault(c, []).append(sid)
    _finish(conn, members)


def _finish(conn: sqlite3.Connection, members: dict[int, list[str]]) -> None:
    # preserva labels LLM por assinatura de membros (mesma ideia do L3 stale)
    old = {r["signature"]: r for r in conn.execute(
        "SELECT signature, label, summary, model, generated_at FROM communities")}
    conn.execute("DELETE FROM communities")

    # ids compactos e determinísticos: comunidades maiores primeiro
    ordered = sorted(members.items(), key=lambda kv: (-len(kv[1]), kv[1][0]))
    for new_id, (_, sids) in enumerate(ordered):
        sig = content_hash("\n".join(sorted(sids)).encode())
        prev = old.get(sig)
        conn.execute(
            "INSERT INTO communities(id, size, signature, label, summary, "
            "model, generated_at) VALUES(?,?,?,?,?,?,?)",
            (new_id, len(sids), sig,
             prev["label"] if prev else None,
             prev["summary"] if prev else None,
             prev["model"] if prev else None,
             prev["generated_at"] if prev else None))
        conn.executemany(
            "UPDATE symbols SET community=? WHERE id=?",
            [(new_id, sid) for sid in sids])
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('community_dirty','0') "
        "ON CONFLICT(key) DO UPDATE SET value='0'")
    conn.commit()
