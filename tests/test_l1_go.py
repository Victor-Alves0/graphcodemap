"""L1 Go via gopls — promoção a 'certain' de chamada cross-file.

Pula se gopls (ou o toolchain Go) não estiver disponível — como os testes de
jedi/tsserver pulam sem sua dependência.
"""

from __future__ import annotations

import pytest

from codegraph.l1.go_gopls import GoplsResolver

pytestmark = pytest.mark.skipif(
    not GoplsResolver.available(), reason="gopls não disponível")


def test_go_l1_promotes_cross_file_call(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n",
                                     encoding="utf-8")
    (tmp_path / "main.go").write_text(
        "package main\n\nfunc main() { helper() }\n\n"
        "func helper() int { return compute(2) }\n", encoding="utf-8")
    (tmp_path / "calc.go").write_text(
        "package main\n\nfunc compute(x int) int { return x * x }\n",
        encoding="utf-8")

    from codegraph import CodeGraph, l1

    cg = CodeGraph(tmp_path)
    cg.index()
    stats = l1.refine(cg.indexer)
    assert "go" in stats["resolvers"]
    assert stats["promoted"] > 0
    # helper() chama compute() definido em outro arquivo → deve ser 'certain'
    row = cg.indexer.conn.execute(
        "SELECT e.confidence, e.dst FROM edges e JOIN symbols s ON e.src=s.id "
        "WHERE s.name='helper' AND e.kind='calls' AND e.dst_name LIKE '%compute%'"
    ).fetchone()
    cg.close()
    assert row is not None and row["dst"] is not None
    assert row["confidence"] == "certain"
