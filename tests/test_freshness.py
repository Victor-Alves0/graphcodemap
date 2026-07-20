"""Gate anti-staleness (docs/DESIGN.md §7 M2): editar/deletar/renomear arquivos
e provar que nenhuma query retorna dado velho sem aviso."""

from __future__ import annotations

import os
import time


def _touch_newer(path):
    st = path.stat()
    os.utime(path, (st.st_atime + 2, st.st_mtime + 2))


def test_edit_triggers_read_repair(cg, repo):
    auth = repo / "app" / "auth.py"
    src = auth.read_text(encoding="utf-8")
    auth.write_text(src.replace("def validate", "def validate_token"), encoding="utf-8")
    _touch_newer(auth)

    rows, env = cg.find_symbol("validate_token")
    assert any(r["fqn"] == "app.auth.TokenService.validate_token" for r in rows)
    assert any("re-indexado agora" in w for w in env.warnings)

    # o símbolo antigo não existe mais
    rows, _ = cg.find_symbol("app.auth.TokenService.validate")
    assert all(r["fqn"] != "app.auth.TokenService.validate" for r in rows)


def test_delete_removes_from_index(cg, repo):
    (repo / "app" / "db.py").unlink()
    sym, rows, env = cg.references("get_session")
    # ou o símbolo saiu do índice via repair de find, ou o aviso apareceu
    assert any("sumiu do disco" in w for w in env.warnings) or rows == []


def test_dangling_edge_after_target_removed(cg, repo):
    # remover o alvo transforma arestas de OUTROS arquivos em dangling,
    # preservando dst_name (nunca perder informação em silêncio)
    conn = cg.indexer.conn
    before = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE dst_name='app.db.get_session' AND dst IS NOT NULL"
    ).fetchone()[0]
    assert before >= 1
    cg.indexer.remove_file("app/db.py")
    row = conn.execute(
        "SELECT dst FROM edges WHERE dst_name='app.db.get_session'").fetchone()
    assert row is not None and row["dst"] is None


def test_rename_file(cg, repo):
    old = repo / "app" / "db.py"
    new = repo / "app" / "storage.py"
    old.rename(new)
    stats = cg.index()
    assert stats["removed"] == 1
    rows, _ = cg.find_symbol("get_session")
    assert any(r["fqn"] == "app.storage.get_session" for r in rows)


def test_completeness_warning_always_present_on_callers(cg):
    _, _, env = cg.callers("app.auth.TokenService.validate")
    assert any("completeness" in w for w in env.warnings)


def test_syntax_error_marks_partial(cg, repo):
    bad = repo / "app" / "broken.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
    cg.index()
    assert cg.stats()["parse_partial"] >= 1


def test_scan_source_stats_matches_iter(cg, repo):
    # a varredura de frescor (scandir, rápida em escala) deve cobrir exatamente
    # o mesmo conjunto de arquivos que a indexação (iter_source_files)
    from codegraph.indexer import iter_source_files, scan_source_stats

    indexed = set(iter_source_files(repo))
    scanned = set(scan_source_stats(repo))
    assert scanned == indexed


def test_scan_matches_iter_with_gitignore(tmp_path):
    # a varredura de arquivo usa um spec reduzido (sem padrões de diretório);
    # deve casar EXATAMENTE o conjunto de iter_source_files (spec completo),
    # inclusive com um .gitignore que mistura padrão de dir e de arquivo.
    from codegraph.indexer import iter_source_files, scan_source_stats

    (tmp_path / ".gitignore").write_text("build/\n*.skip.py\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.skip.py").write_text("x = 1\n", encoding="utf-8")   # padrão de arquivo
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "c.py").write_text("x = 1\n", encoding="utf-8")  # dir ignorado
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "d.py").write_text("x = 1\n", encoding="utf-8")

    scanned = set(scan_source_stats(tmp_path))
    assert scanned == set(iter_source_files(tmp_path))
    assert {"a.py", "src/d.py"} <= scanned
    assert "b.skip.py" not in scanned     # padrão de arquivo respeitado
    assert "build/c.py" not in scanned    # padrão de diretório respeitado


def test_repeated_misses_still_catch_edits(cg, repo):
    # garantia FORTE preservada em escala: sem throttle, cada resultado vazio
    # faz a varredura barata — uma edição é vista mesmo após outras queries.
    cg.find_symbol("inexistente_um")                 # miss 1 (varre)
    cg.find_symbol("inexistente_dois")               # miss 2 (varre de novo)

    auth = repo / "app" / "auth.py"
    auth.write_text(auth.read_text(encoding="utf-8") +
                    "\n\ndef recem_criada():\n    return 1\n", encoding="utf-8")
    _touch_newer(auth)

    rows, env = cg.find_symbol("recem_criada")       # varredura pega na hora
    assert any(r["fqn"] == "app.auth.recem_criada" for r in rows)
    assert any("re-indexado agora" in w for w in env.warnings)
