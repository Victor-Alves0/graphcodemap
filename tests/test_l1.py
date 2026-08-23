"""M4: refinamento L1 via jedi — promoção a 'certain'."""

from __future__ import annotations

import pytest

jedi = pytest.importorskip("jedi")

from codegraph import l1  # noqa: E402


def _refine(cg):
    return l1.refine(cg.indexer)


def test_refine_promotes_to_certain(cg):
    stats = _refine(cg)
    assert "python" in stats["resolvers"]
    assert stats["promoted"] > 0
    # login() chama issue_token importado — semanticamente inequívoco
    sym, rows, _ = cg.callers("app.auth.issue_token")
    confs = {r["other_fqn"]: r["confidence"] for r in rows}
    assert confs.get("app.routes.login") == "certain"


def test_refine_resolves_instance_method_call(cg):
    # service = TokenService(); service.validate(token) — L0 só chega a
    # 'possible'/'inferred' por nome; jedi infere o tipo do receptor
    _refine(cg)
    sym, rows, _ = cg.callers("app.auth.TokenService.validate")
    by_site = {(r["site_path"], r["line"]): r["confidence"] for r in rows}
    assert by_site.get(("app/routes.py", 9)) == "certain"


def test_refine_removes_possible_clones(cg):
    _refine(cg)
    # sites promovidos a certain não podem manter clones 'possible'
    n = cg.indexer.conn.execute(
        "SELECT COUNT(*) FROM edges e1 WHERE e1.confidence='possible' "
        "AND EXISTS (SELECT 1 FROM edges e2 WHERE e2.file_id=e1.file_id "
        "AND e2.line=e1.line AND e2.col=e1.col AND e2.resolver='l1')"
    ).fetchone()[0]
    assert n == 0


def test_external_calls_stay_unresolved(cg):
    _refine(cg)
    # hashlib.sha256: definição fora do repo → permanece dangling (honesto)
    row = cg.indexer.conn.execute(
        "SELECT dst, confidence FROM edges WHERE dst_name='hashlib.sha256'"
    ).fetchone()
    assert row is not None and row["dst"] is None


def test_callback_parameter_is_not_promoted_to_enclosing_function(tmp_path):
    """Jedi resolve ``cb()`` para o parâmetro na linha do ``def``.

    Mapear essa linha pelo menor símbolo envolvente fabricava uma auto-chamada
    ``run -> run`` com confiança ``certain``.
    """
    p = tmp_path / "callbacks.py"
    p.write_text("def run(cb):\n    return cb(1)\n", encoding="utf-8")
    from codegraph import CodeGraph

    g = CodeGraph(tmp_path)
    g.index()
    _refine(g)
    row = g.indexer.conn.execute(
        "SELECT e.confidence, e.resolver, e.src, e.dst FROM edges e "
        "WHERE e.kind='calls' AND e.dst_name='cb'"
    ).fetchone()
    assert row is not None
    assert row["resolver"] == "l0"
    assert row["confidence"] != "certain"
    assert row["src"] != row["dst"]
    g.close()


def test_refine_revalidates_without_accumulating_clones(cg):
    first = _refine(cg)
    before = cg.indexer.conn.execute(
        "SELECT kind, src, dst, dst_name, file_id, line, col, confidence, resolver "
        "FROM edges ORDER BY kind, src, dst, dst_name, file_id, line, col"
    ).fetchall()
    second = _refine(cg)
    after = cg.indexer.conn.execute(
        "SELECT kind, src, dst, dst_name, file_id, line, col, confidence, resolver "
        "FROM edges ORDER BY kind, src, dst, dst_name, file_id, line, col"
    ).fetchall()
    assert first["promoted"] > 0
    assert second["revalidated"] > 0
    assert second["promoted"] == first["promoted"]
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_watcher_drain_refines_changed_file(cg, repo):
    from codegraph.watcher import Watcher

    _refine(cg)
    routes = repo / "app" / "routes.py"
    routes.write_text(routes.read_text(encoding="utf-8") +
                      "\n\ndef logout(request):\n    return issue_token(request)\n",
                      encoding="utf-8")
    w = Watcher(repo, db_path=repo / ".codegraph" / "graph.db")
    w._pending = {"app/routes.py"}
    w.drain()
    row = w.ix.conn.execute(
        "SELECT e.confidence FROM edges e JOIN symbols s ON e.src=s.id "
        "WHERE s.name='logout' AND e.kind='calls'").fetchone()
    assert row is not None and row["confidence"] == "certain"
    w.stop()
