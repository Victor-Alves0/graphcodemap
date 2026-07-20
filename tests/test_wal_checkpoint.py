"""Controle de WAL no índice de repos enormes.

Escrever milhões de linhas numa transação única faz o WAL crescer sem limite
(frames não-commitados não podem ser checkpointados) até o commit final disparar
um checkpoint gigante que trava — foi o que travou o índice do kernel Linux
inteiro. resolve_edges e os loops de índice agora commitam em blocos + fazem
checkpoint(TRUNCATE), mantendo o WAL pequeno. Estes testes provam que:
  - a escrita em blocos dá o MESMO grafo que a escrita única (equivalência);
  - o arquivo -wal fica pequeno após o índice (foi truncado).
"""

from __future__ import annotations

from codegraph import CodeGraph
from codegraph import indexer as ix_mod
from codegraph.indexer import Indexer


def _mkrepo(root, n=30):
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        nxt = (i + 1) % n
        (root / f"m{i}.py").write_text(
            f"import m{nxt}\n\n\ndef f{i}(x):\n    return m{nxt}.f{nxt}(x)\n",
            encoding="utf-8")


def _edge_counts(root, db):
    ix = Indexer(root, db_path=db)
    ix.index_repo(force=True)
    c = ix.conn
    counts = (
        c.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        c.execute("SELECT COUNT(*) FROM edges WHERE dst IS NOT NULL").fetchone()[0],
        c.execute("SELECT COUNT(*) FROM edges WHERE confidence='inferred'").fetchone()[0],
    )
    ix.close()
    return counts


def test_chunked_resolve_matches_unchunked(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _mkrepo(root, n=30)
    big = _edge_counts(root, tmp_path / "big.db")        # WRITE_CHUNK default (1 bloco)
    monkeypatch.setattr(ix_mod, "WRITE_CHUNK", 2)        # força muitos blocos
    small = _edge_counts(root, tmp_path / "small.db")
    assert big == small                                  # grafo idêntico
    assert big[1] > 0                                    # de fato resolveu arestas


def test_wal_small_after_index(tmp_path, monkeypatch):
    # com checkpoint agressivo, o -wal é truncado ao longo do índice e no fim
    monkeypatch.setattr(ix_mod, "WRITE_CHUNK", 2)
    monkeypatch.setattr(ix_mod, "CHECKPOINT_EVERY_BATCHES", 1)
    root = tmp_path / "repo"
    _mkrepo(root, n=30)
    cg = CodeGraph(root)
    cg.index(force=True)
    wal = root / ".codegraph" / "graph.db-wal"
    # resolve_edges termina com _flush_wal(TRUNCATE) → -wal ~vazio
    assert (not wal.exists()) or wal.stat().st_size < 200_000
    cg.close()


def test_flush_wal_is_nonfatal(tmp_path):
    # _flush_wal nunca deve derrubar o índice, mesmo se o checkpoint não puder rodar
    ix = Indexer(tmp_path, db_path=tmp_path / "g.db")
    ix._flush_wal()          # sem crash em banco recém-criado
    ix.close()
