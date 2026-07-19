"""L1 C/C++ via clangd (LSP) — promoção a 'certain' de chamada cross-file.

Pula se clangd não estiver disponível (PATH ou CODEGRAPH_CLANGD). Cross-file em C
depende do clangd achar a unidade de compilação — incluímos um
`compile_commands.json` mínimo. clangd indexa de forma assíncrona; o cliente LSP
espera (ready_timeout).
"""

from __future__ import annotations

import json

import pytest

from codegraph.l1.clangd import ClangdResolver

pytestmark = pytest.mark.skipif(
    not ClangdResolver.available(), reason="clangd não disponível")


def test_clangd_l1_promotes_cross_file_call(tmp_path):
    (tmp_path / "calc.h").write_text("int compute(int x);\n", encoding="utf-8")
    (tmp_path / "calc.c").write_text(
        '#include "calc.h"\n\nint compute(int x) {\n    return x * x;\n}\n',
        encoding="utf-8")
    (tmp_path / "main.c").write_text(
        '#include "calc.h"\n\nint helper(void) {\n    return compute(2);\n}\n\n'
        'int main(void) {\n    return helper();\n}\n', encoding="utf-8")
    d = str(tmp_path)
    (tmp_path / "compile_commands.json").write_text(json.dumps([
        {"directory": d, "file": f"{d}/main.c", "command": "clang -c main.c"},
        {"directory": d, "file": f"{d}/calc.c", "command": "clang -c calc.c"},
    ]), encoding="utf-8")

    from codegraph import CodeGraph, l1

    cg = CodeGraph(tmp_path)
    cg.index()
    stats = l1.refine(cg.indexer)
    assert "c" in stats["resolvers"]
    row = cg.indexer.conn.execute(
        "SELECT e.confidence, e.dst FROM edges e JOIN symbols s ON e.src=s.id "
        "WHERE s.name='helper' AND e.kind='calls' AND e.dst_name LIKE '%compute%'"
    ).fetchone()
    cg.close()
    assert row is not None and row["dst"] is not None
    assert row["confidence"] == "certain"
