"""PageRank sobre o grafo de símbolos (docs/DESIGN.md §4).

Recomputado lazy: qualquer re-index marca `rank_dirty`; a próxima consulta
que precisa de ranking (overview/impact) recomputa. Importância flui do site
da referência para o alvo: símbolo muito referenciado → rank alto.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

DAMPING = 0.85
ITERATIONS = 20


def mark_dirty(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('rank_dirty','1') "
        "ON CONFLICT(key) DO UPDATE SET value='1'"
    )


def ensure_ranks(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='rank_dirty'").fetchone()
    if row is not None and row["value"] == "0":
        return False
    recompute(conn)
    return True


def recompute(conn: sqlite3.Connection) -> None:
    ids = [r["id"] for r in conn.execute("SELECT id FROM symbols").fetchall()]
    n = len(ids)
    if n:
        out: dict[str, set[str]] = defaultdict(set)
        for r in conn.execute(
            "SELECT src, dst FROM edges "
            "WHERE src IS NOT NULL AND dst IS NOT NULL AND src != dst"
        ).fetchall():
            out[r["src"]].add(r["dst"])
        pr = dict.fromkeys(ids, 1.0 / n)
        base = (1.0 - DAMPING) / n
        for _ in range(ITERATIONS):
            nxt = dict.fromkeys(ids, base)
            for s, dsts in out.items():
                p = pr.get(s)
                if p is None:
                    continue
                share = DAMPING * p / len(dsts)
                for t in dsts:
                    if t in nxt:
                        nxt[t] += share
            pr = nxt
        conn.executemany(
            "UPDATE symbols SET rank=? WHERE id=?", [(v, k) for k, v in pr.items()]
        )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('rank_dirty','0') "
        "ON CONFLICT(key) DO UPDATE SET value='0'"
    )
    conn.commit()
