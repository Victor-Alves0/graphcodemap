"""Indexação parcial/escopada (evals/RESULTS.md — teto de escala do C denso).

Um índice pode cobrir só subárvores: torna tratável um monorepo grande demais
p/ indexar inteiro. O escopo é persistido (meta['index_scopes']), acumula entre
execuções, e é respeitado por iter/scan/freshness/watcher. A remoção só apaga
arquivos sumidos DENTRO do escopo — indexar a subárvore A não apaga a B.
"""

from __future__ import annotations

import pytest

from codegraph import CodeGraph
from codegraph.indexer import (Indexer, get_index_scopes, in_scope,
                               scan_source_stats)


def _mkrepo(root):
    for pkg in ("pkg_a", "pkg_b"):
        d = root / pkg
        d.mkdir()
        (d / "m.py").write_text(
            f"def fn_{pkg}():\n    return 1\n", encoding="utf-8")
    (root / "top.py").write_text("def fn_top():\n    return 1\n", encoding="utf-8")


def _paths(cg):
    return {r["path"] for r in cg.indexer.conn.execute("SELECT path FROM files")}


def test_scope_indexes_only_subtree(tmp_path):
    _mkrepo(tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index(scope="pkg_a")
    paths = _paths(cg)
    assert paths == {"pkg_a/m.py"}
    assert get_index_scopes(cg.indexer.conn) == ["pkg_a"]
    cg.close()


def test_scope_persisted_on_reindex(tmp_path):
    _mkrepo(tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index(scope="pkg_a")
    cg.index()                       # sem scope: respeita o escopo salvo
    assert _paths(cg) == {"pkg_a/m.py"}
    cg.close()


def test_scope_is_additive(tmp_path):
    _mkrepo(tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index(scope="pkg_a")
    cg.index(scope="pkg_b")
    assert _paths(cg) == {"pkg_a/m.py", "pkg_b/m.py"}
    assert get_index_scopes(cg.indexer.conn) == ["pkg_a", "pkg_b"]
    cg.close()


def test_reindex_of_one_scope_keeps_the_other(tmp_path):
    _mkrepo(tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index(scope="pkg_a")
    cg.index(scope="pkg_b")
    (tmp_path / "pkg_a" / "m.py").unlink()      # some um arquivo do escopo A
    cg.index()                                  # reindex escopado
    assert _paths(cg) == {"pkg_b/m.py"}         # A/m removido, B intacto
    cg.close()


def test_scoped_freshness_sweep_only_walks_scope(tmp_path):
    _mkrepo(tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index(scope="pkg_a")
    scanned = set(scan_source_stats(tmp_path, scopes=["pkg_a"]))
    assert scanned == {"pkg_a/m.py"}            # não anda em pkg_b/ nem top.py
    cg.close()


def test_scoped_read_repair_catches_edit_in_scope(tmp_path):
    _mkrepo(tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index(scope="pkg_a")
    p = tmp_path / "pkg_a" / "m.py"
    import os
    p.write_text(p.read_text(encoding="utf-8") +
                 "\ndef fn_nova():\n    return 2\n", encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime + 2, st.st_mtime + 2))
    rows, env = cg.find_symbol("fn_nova")       # miss → varredura escopada repara
    assert any(r["fqn"] == "pkg_a.m.fn_nova" for r in rows)
    cg.close()


def test_no_scope_is_whole_repo(tmp_path):
    _mkrepo(tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index()
    assert get_index_scopes(cg.indexer.conn) == []
    assert _paths(cg) == {"pkg_a/m.py", "pkg_b/m.py", "top.py"}
    cg.close()


def test_in_scope_predicate():
    assert in_scope("a/b.py", None) is True          # sem escopo = tudo
    assert in_scope("a/b.py", []) is True
    assert in_scope("a/b.py", ["a"]) is True
    assert in_scope("a/b.py", ["a/b.py"]) is True
    assert in_scope("ab/c.py", ["a"]) is False       # não é prefixo de diretório
    assert in_scope("b/c.py", ["a"]) is False
