"""Tier 3 da resolução L1: multi-definição (overloads) não é mais descartada.

Antes, cada resolver dava `continue` quando o servidor devolvia != 1 definição —
overloads, interface+impls, decl+def viravam o `possible` por nome do L0. Agora
`promote` promove: 1 alvo → certain; 2..MAX → fan-out `inferred` com resolver l1
(semântico, mais forte que o L0). Estes testes travam a lógica de `promote` sobre
um grafo real + a explicação (Tier 1) do caso overload."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph, explain, render
from codegraph.l1 import promote


def _graph(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    return g


def _homonym_repo(tmp_path):
    """main.run() chama process(), homônimo em a.py e b.py → L0 gera fan-out
    'possible' (2 candidatos). Simula o que o L1 resolveria semanticamente."""
    return _graph(tmp_path, {
        "a.py": "def process():\n    return 1\n",
        "b.py": "def process():\n    return 2\n",
        "main.py": "def run():\n    return process()\n",
    })


def _call_edge(g, dst_name="process"):
    return g.indexer.conn.execute(
        "SELECT e.id, e.line, e.col, e.file_id FROM edges e "
        "WHERE e.kind='calls' AND e.dst_name=? LIMIT 1", (dst_name,)).fetchone()


def _process_ids(g):
    return [r["id"] for r in g.indexer.conn.execute(
        "SELECT id FROM symbols WHERE name='process' AND kind='function' "
        "ORDER BY fqn").fetchall()]


# ============================================================================
# A. promote.target_symbol
# ============================================================================

def test_target_symbol_resolves_enclosing(tmp_path):
    g = _homonym_repo(tmp_path)
    sid = promote.target_symbol(g.indexer.conn, "a.py", 1)  # linha do def process
    got = g.indexer.conn.execute(
        "SELECT fqn FROM symbols WHERE id=?", (sid,)).fetchone()
    assert got["fqn"] == "a.process"
    g.close()


def test_target_symbol_unknown_file_is_none(tmp_path):
    g = _homonym_repo(tmp_path)
    assert promote.target_symbol(g.indexer.conn, "nope.py", 1) is None
    g.close()


def test_target_symbol_definition_name_must_match(tmp_path):
    g = _homonym_repo(tmp_path)
    assert promote.target_symbol(
        g.indexer.conn, "a.py", 1, dname="not_process") is None
    assert promote.target_symbol(
        g.indexer.conn, "a.py", 1, dname="process") is not None
    g.close()


def test_target_symbol_rejects_non_callable_container(tmp_path):
    """Uma definição LSP dentro de uma constante não torna a constante chamável.

    JS object literals/IIFEs eram o caso real: sem símbolo para a função interna,
    o menor span que cobria a linha era ``constant``/``file`` e recebia uma
    aresta ``certain`` fabricada.
    """
    g = _graph(tmp_path, {
        "a.js": "const obj = { value: 1 };\n",
    })
    assert promote.target_symbol(g.indexer.conn, "a.js", 1) is None
    g.close()


# ============================================================================
# B. promote.apply — 1 alvo = certain
# ============================================================================

def test_apply_single_target_is_certain(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    ids = _process_ids(g)
    n = promote.apply(conn, edge["file_id"], edge, [ids[0]])
    conn.commit()
    assert n == 1
    row = conn.execute(
        "SELECT dst, confidence, resolver FROM edges WHERE id=?",
        (edge["id"],)).fetchone()
    assert row["dst"] == ids[0]
    assert row["confidence"] == "certain" and row["resolver"] == "l1"
    g.close()


def test_apply_single_target_removes_l0_possible_clones(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    ids = _process_ids(g)
    promote.apply(conn, edge["file_id"], edge, [ids[0]])
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) c FROM edges WHERE kind='calls' AND dst_name='process' "
        "AND resolver='l0' AND confidence='possible'").fetchone()["c"]
    assert left == 0
    g.close()


def test_apply_single_target_can_reuse_sibling_clone_target(tmp_path):
    """O alvo L1 pode já estar ocupado por outro clone ``possible`` do L0.

    A limpeza precisa acontecer antes do UPDATE; do contrário o índice único
    rejeita a promoção e o resolver aborta o restante do arquivo.
    """
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    rows = conn.execute(
        "SELECT id, dst, line, col, file_id FROM edges WHERE kind='calls' "
        "AND dst_name='process' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2 and rows[0]["dst"] != rows[1]["dst"]
    n = promote.apply(conn, rows[0]["file_id"], rows[0], [rows[1]["dst"]])
    conn.commit()
    assert n == 1
    left = conn.execute(
        "SELECT dst, confidence, resolver FROM edges WHERE kind='calls' "
        "AND dst_name='process'"
    ).fetchall()
    assert [(r["dst"], r["confidence"], r["resolver"]) for r in left] == [
        (rows[1]["dst"], "certain", "l1")
    ]
    g.close()


# ============================================================================
# C. promote.apply — 2..MAX alvos = fan-out inferred
# ============================================================================

def test_apply_multi_target_is_inferred_fanout(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    ids = _process_ids(g)
    assert len(ids) == 2
    n = promote.apply(conn, edge["file_id"], edge, ids)
    conn.commit()
    assert n == 1
    rows = conn.execute(
        "SELECT dst, confidence, resolver FROM edges WHERE kind='calls' "
        "AND dst_name='process' AND resolver='l1'").fetchall()
    assert {r["dst"] for r in rows} == set(ids)         # uma aresta por overload
    assert all(r["confidence"] == "inferred" for r in rows)
    g.close()


def test_apply_multi_target_clears_l0_possible(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    promote.apply(conn, edge["file_id"], edge, _process_ids(g))
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) c FROM edges WHERE dst_name='process' "
        "AND resolver='l0' AND confidence='possible'").fetchone()["c"]
    assert left == 0
    g.close()


def test_apply_is_idempotent(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    ids = _process_ids(g)
    promote.apply(conn, edge["file_id"], edge, ids)
    conn.commit()
    before = conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    promote.apply(conn, edge["file_id"], edge, ids)   # de novo
    conn.commit()
    after = conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    assert after == before                             # índice único: sem duplicar
    g.close()


# ============================================================================
# D. Guardas: 0 alvos e > MAX alvos não mexem no grafo
# ============================================================================

def test_apply_zero_targets_noop(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    before = conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    assert promote.apply(conn, edge["file_id"], edge, []) == 0
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"] == before
    g.close()


def test_apply_over_max_targets_noop(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    fake = list(range(promote.MAX_L1_TARGETS + 1))   # ids inexistentes, mas > MAX
    assert promote.apply(conn, edge["file_id"], edge, fake) == 0
    g.close()


def test_apply_dedups_target_ids(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    ids = _process_ids(g)
    # mesmo alvo repetido (decl+def na mesma def) → 1 alvo → certain
    promote.apply(conn, edge["file_id"], edge, [ids[0], ids[0], ids[0]])
    conn.commit()
    row = conn.execute("SELECT confidence FROM edges WHERE id=?",
                       (edge["id"],)).fetchone()
    assert row["confidence"] == "certain"
    g.close()


# ============================================================================
# E. Transparência (Tier 1) do caso overload
# ============================================================================

def test_reason_l1_inferred_is_overloads():
    r = explain.reason("l1/typescript", "inferred")
    assert "overload" in r.lower() or "várias defini" in r.lower()


def test_reason_l1_inferred_differs_from_l0_inferred():
    assert explain.reason("l1/go", "inferred") != explain.reason("l0", "inferred")


def test_overload_shows_in_callers_render(tmp_path):
    g = _homonym_repo(tmp_path)
    conn = g.indexer.conn
    edge = _call_edge(g)
    promote.apply(conn, edge["file_id"], edge, _process_ids(g))
    conn.commit()
    # callers de a.process deve mostrar o site com selo l1 e legenda de overloads
    out = render.calls(*g.query.callers("a.process"), "callers de", "in")
    assert "l1/python" in out
    assert "overload" in out.lower() or "várias defini" in out.lower()
    g.close()
