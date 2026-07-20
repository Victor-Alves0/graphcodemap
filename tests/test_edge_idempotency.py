"""Resolução de arestas: fan-out preserva recall (N candidatos → N arestas) e a
garantia estrutural de que re-indexar NUNCA infla o grafo (bloat histórico 800x
via clones duplicados) — índice único + INSERT OR IGNORE + dedup de refs."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

# 4 funções 'run' em módulos distintos + um chamador com receptor desconhecido
# (x.run()) → resolução por nome é ambígua → antes explodia em N arestas.
A = "def run():\n    return 1\n"
B = "def run():\n    return 2\n"
C = "def run():\n    return 3\n"
CALLER = "def go(x):\n    return x.run()\n"


@pytest.fixture()
def cg(tmp_path):
    for name, src in {"a.py": A, "b.py": B, "c.py": C, "caller.py": CALLER}.items():
        (tmp_path / name).write_text(textwrap.dedent(src), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def _count(cg, where="1=1"):
    return cg.indexer.conn.execute(
        f"SELECT COUNT(*) FROM edges WHERE {where}").fetchone()[0]


def test_ambiguous_call_fans_out_to_all_candidates(cg):
    # x.run() casa 3 funções 'run' (a/b/c) → 3 arestas 'possible' (recall
    # preservado para callers/impact), todas distintas por dst
    rows = cg.indexer.conn.execute(
        "SELECT dst, confidence FROM edges "
        "WHERE dst_name='run' AND kind='calls' AND dst IS NOT NULL").fetchall()
    assert len(rows) == 3
    assert all(r["confidence"] == "possible" for r in rows)
    assert len({r["dst"] for r in rows}) == 3     # candidatos distintos


def test_reindex_force_does_not_inflate(cg):
    before = _count(cg)
    for _ in range(5):
        cg.index(force=True)
    assert _count(cg) == before          # idempotente: zero acúmulo


def test_unique_index_blocks_duplicate_resolved_edge(cg):
    # a guarda estrutural: inserir uma aresta resolvida idêntica é rejeitada
    import sqlite3

    row = cg.indexer.conn.execute(
        "SELECT kind, src, dst, dst_name, file_id, line, col FROM edges "
        "WHERE dst IS NOT NULL AND src IS NOT NULL LIMIT 1").fetchone()
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError):
        cg.indexer.conn.execute(
            "INSERT INTO edges(kind, src, dst, dst_name, file_id, line, col, "
            "confidence, resolver) VALUES(?,?,?,?,?,?,?,'possible','l0')",
            (row["kind"], row["src"], row["dst"], row["dst_name"],
             row["file_id"], row["line"], row["col"]))
    cg.indexer.conn.rollback()
