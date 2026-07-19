"""L1 Rust via rust-analyzer (LSP) — promoção a 'certain' de chamada cross-file.

Pula se rust-analyzer não estiver disponível (via PATH ou CODEGRAPH_RUST_ANALYZER)
— como os testes de jedi/tsserver/gopls pulam sem sua dependência. Rust-analyzer
carrega o crate de forma assíncrona (~20-40s no cold start); o cliente LSP espera
a indexação (ready_timeout).
"""

from __future__ import annotations

import pytest

from codegraph.l1.rust_analyzer import RustAnalyzerResolver

pytestmark = pytest.mark.skipif(
    not RustAnalyzerResolver.available(),
    reason="rust-analyzer não disponível")


def test_rust_l1_promotes_cross_file_call(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "m"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text(
        "mod calc;\n\nfn main() {\n    let _ = helper();\n}\n\n"
        "fn helper() -> i32 {\n    calc::compute(2)\n}\n", encoding="utf-8")
    (src / "calc.rs").write_text(
        "pub fn compute(x: i32) -> i32 {\n    x * x\n}\n", encoding="utf-8")

    from codegraph import CodeGraph, l1

    cg = CodeGraph(tmp_path)
    cg.index()
    stats = l1.refine(cg.indexer)
    assert "rust" in stats["resolvers"]
    row = cg.indexer.conn.execute(
        "SELECT e.confidence, e.dst FROM edges e JOIN symbols s ON e.src=s.id "
        "WHERE s.name='helper' AND e.kind='calls' AND e.dst_name LIKE '%compute%'"
    ).fetchone()
    cg.close()
    assert row is not None and row["dst"] is not None
    assert row["confidence"] == "certain"
