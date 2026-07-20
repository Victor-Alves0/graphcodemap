"""Indexação paralela: prepare (ler+parsear+extrair) em threads, escrita serial.

O gargalo do índice é a escrita no SQLite (writer único) — o prepare é que solta
o GIL (I/O + tree-sitter). `ex.map` devolve na ORDEM de entrada, então a escrita
sai na mesma ordem do serial → o grafo é bit-a-bit idêntico. Estes testes provam
essa equivalência (mesmos símbolos/arestas) e que erros por arquivo são isolados.
"""

from __future__ import annotations

import pytest

from codegraph import indexer as ix_mod
from codegraph.indexer import Indexer


def _mkrepo(root, n=40):
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        nxt = (i + 1) % n
        (root / f"m{i}.py").write_text(
            f"import m{nxt}\n\n\ndef f{i}(x):\n    return m{nxt}.f{nxt}(x)\n",
            encoding="utf-8")


def _index_counts(root, db, workers):
    ix = Indexer(root, db_path=db)
    stats = ix.index_repo(force=True, workers=workers)
    c = ix.conn
    counts = {
        "symbols": c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        "edges": c.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "resolved": c.execute(
            "SELECT COUNT(*) FROM edges WHERE dst IS NOT NULL").fetchone()[0],
        "fts": c.execute("SELECT COUNT(*) FROM symbols_fts").fetchone()[0],
    }
    ix.close()
    return stats, counts


def test_parallel_matches_serial(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _mkrepo(root, n=40)
    # baixa o limiar p/ o caminho paralelo engatar num repo pequeno de teste
    monkeypatch.setattr(ix_mod, "PARALLEL_MIN_FILES", 2)

    s_stats, s_counts = _index_counts(root, tmp_path / "serial.db", workers=1)
    p_stats, p_counts = _index_counts(root, tmp_path / "par.db", workers=4)

    assert s_stats["indexed"] == p_stats["indexed"] == 40
    assert s_stats["errors"] == p_stats["errors"] == 0
    assert s_counts == p_counts            # grafo idêntico (símbolos, arestas, FTS)


def test_parallel_isolates_file_errors(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _mkrepo(root, n=40)
    (root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(ix_mod, "PARALLEL_MIN_FILES", 2)

    ix = Indexer(root, db_path=tmp_path / "p.db")
    stats = ix.index_repo(force=True, workers=4)
    # o arquivo quebrado é marcado, os outros indexam normalmente (isolamento)
    assert stats["indexed"] >= 40
    row = ix.conn.execute(
        "SELECT parse_status FROM files WHERE path='broken.py'").fetchone()
    assert row is not None and row["parse_status"] in ("failed", "partial")
    ix.close()


def test_small_repo_uses_serial_path(tmp_path, monkeypatch):
    # abaixo do limiar → caminho serial mesmo pedindo workers>1 (sem overhead)
    root = tmp_path / "repo"
    _mkrepo(root, n=5)
    called = {"parallel": False}
    real = Indexer._index_files_parallel

    def spy(self, *a, **k):
        called["parallel"] = True
        return real(self, *a, **k)

    monkeypatch.setattr(Indexer, "_index_files_parallel", spy)
    ix = Indexer(root, db_path=tmp_path / "s.db")
    ix.index_repo(force=True, workers=4)     # 5 arquivos < PARALLEL_MIN_FILES
    ix.close()
    assert called["parallel"] is False
