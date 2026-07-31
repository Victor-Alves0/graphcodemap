"""Workflows e invariantes CRUZADOS entre os métodos.

As baterias por método já cobrem cada tool isolada. Esta cobre o que importa
quando o agente ENCADEIA chamadas: a saída de uma alimenta a outra, e os dados
têm que permanecer coerentes. Ex.: se `callees(A)` diz que A chama B, então
`callers(B)` tem que conter A; `impact(X)` tem que conter os callers diretos de
X; um fqn devolvido por qualquer tool tem que resolver em `symbol_info`; o
envelope estruturado tem que bater com as linhas que acompanha.

Fixture determinística (Python, cadeia de chamadas clara): base ← mid ← top,
base ← helper, e um teste chamando top."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph, agent, render
from codegraph.query import AmbiguousSymbol, SymbolNotFound

PKG = {
    "core.py": (
        "def base():\n    return 1\n\n"
        "def mid():\n    return base()\n\n"
        "def top():\n    return mid()\n"
    ),
    "helpers.py": (
        "from core import base\n\n"
        "def helper():\n    return base()\n"
    ),
    "tests/test_core.py": (
        "from core import top\n\n"
        "def test_top():\n    assert top() == 1\n"
    ),
}


@pytest.fixture
def g(tmp_path):
    for rel, body in PKG.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    cg = CodeGraph(tmp_path)
    cg.index()
    yield cg
    cg.close()


def _fqns(rows, key="other_fqn"):
    return {r[key] for r in rows if r.get(key)}


# ============================================================================
# A. Round-trip de seletor: um fqn de qualquer tool resolve nas outras
# ============================================================================

def test_find_then_info_roundtrip(g):
    rows, _ = g.find_symbol("base")
    fqn = next(r["fqn"] for r in rows if r["name"] == "base")
    info, _ = g.symbol_info(fqn)
    assert info["symbol"]["fqn"] == fqn


def test_info_by_name_equals_by_fqn(g):
    by_name, _ = g.symbol_info("base")
    by_fqn, _ = g.symbol_info(by_name["symbol"]["fqn"])
    assert by_name["symbol"]["id"] == by_fqn["symbol"]["id"]


def test_every_impacted_fqn_resolves(g):
    data, _ = g.change_impact("core.py")
    for r in data["impacted"]:
        info, _ = g.symbol_info(r["fqn"])       # não deve lançar
        assert info["symbol"]["fqn"] == r["fqn"]


def test_every_caller_fqn_resolves(g):
    _s, rows, _ = g.callers("base", depth=2)
    for fqn in _fqns(rows):
        info, _ = g.symbol_info(fqn)
        assert info["symbol"]["fqn"] == fqn


# ============================================================================
# B. Dualidade call graph: callees(A)∋B  ⇒  callers(B)∋A
# ============================================================================

def test_callees_callers_are_dual(g):
    _s, callees, _ = g.callees("mid", depth=1)
    assert "core.base" in _fqns(callees)
    _s2, callers, _ = g.callers("base", depth=1)
    assert "core.mid" in _fqns(callers)


def test_all_direct_callees_are_symmetric(g):
    for fn in ("mid", "top", "helper"):
        _s, callees, _ = g.callees(fn, depth=1)
        for callee in _fqns(callees):
            _s2, callers, _ = g.callers(callee, depth=1)
            assert f"core.{fn}" in _fqns(callers) or f"helpers.{fn}" in _fqns(callers), \
                f"{fn}->{callee} não é simétrico"


def test_references_calls_match_callers(g):
    # references(base, calls) traz os SITES; os src_fqn são exatamente os callers
    _s, refs, _ = g.references("base", kind="calls")
    _s2, callers, _ = g.callers("base", depth=1)
    assert _fqns(refs, "src_fqn") == _fqns(callers)


def test_symbol_info_counts_match_edges(g):
    info, _ = g.symbol_info("base")
    _s, callers, _ = g.callers("base", depth=1)
    # counts.callers conta arestas; callers(depth=1) traz as mesmas
    assert info["counts"]["callers"] == len(callers)


# ============================================================================
# C. ego_graph coerente com callers/callees
# ============================================================================

def test_ego_out_subset_of_callees(g):
    data, _ = g.ego_graph("mid")
    ego_out = {r["other_fqn"] for r in data["out"]
               if r["kind"] == "calls" and r.get("other_fqn")}
    _s, callees, _ = g.callees("mid", depth=1)
    assert ego_out <= _fqns(callees) | {"core.base"}
    assert "core.base" in ego_out


def test_ego_in_matches_callers(g):
    data, _ = g.ego_graph("base")
    ego_in = {r["other_fqn"] for r in data["in"]
              if r["kind"] == "calls" and r.get("other_fqn")}
    _s, callers, _ = g.callers("base", depth=1)
    assert ego_in == _fqns(callers)


# ============================================================================
# D. impact ⊇ callers diretos; e é transitivo
# ============================================================================

def test_impact_contains_direct_callers(g):
    _s, callers, _ = g.callers("base", depth=1)
    _s2, impacted, _ = g.impact("base", depth=1)
    assert _fqns(callers) <= _fqns(impacted, "fqn")


def test_impact_is_transitive(g):
    _s, impacted, _ = g.impact("base", depth=3)
    fqns = _fqns(impacted, "fqn")
    # base ← mid ← top ← test_top ; base ← helper
    assert {"core.mid", "core.top", "helpers.helper"} <= fqns
    assert any("test_top" in f for f in fqns)


def test_impact_depth_monotonic(g):
    _s1, d1, _ = g.impact("base", depth=1)
    _s3, d3, _ = g.impact("base", depth=3)
    assert _fqns(d1, "fqn") <= _fqns(d3, "fqn")   # profundidade maior só ADiciona


# ============================================================================
# E. change_impact == união dos impact() dos símbolos do arquivo
# ============================================================================

def test_change_impact_equals_union_of_impacts(g):
    data, _ = g.change_impact("core.py", depth=3)
    ci = _fqns(data["impacted"], "fqn")
    union = set()
    for fn in ("core.base", "core.mid", "core.top"):
        _s, rows, _ = g.impact(fn, depth=3)
        union |= _fqns(rows, "fqn")
    # change_impact não inclui os próprios símbolos alterados; a união pode
    # incluir uns aos outros (mid impacta top). ci ⊆ união e cobre os externos.
    assert ci <= union
    assert {"helpers.helper"} <= ci     # dependente fora do arquivo alterado


def test_affected_modules_group_matches_impacted(g):
    ci, _ = g.change_impact("core.py")
    am, _ = g.find_affected_modules("core.py")
    files_ci = {r["path"] for r in ci["impacted"]}
    files_am = {m["path"] for m in am["modules"]}
    assert files_ci == files_am
    total = sum(m["count"] for m in am["modules"])
    assert total == len(ci["impacted"])


# ============================================================================
# F. related_tests são callers em arquivos de teste
# ============================================================================

def test_related_tests_subset_of_transitive_callers(g):
    _s, callers, _ = g.callers("base", depth=3)
    rt, _ = g.find_related_tests("base", depth=3)
    caller_fqns = _fqns(callers)
    for t in rt["tests"]:
        assert t["test"] in caller_fqns              # é um caller transitivo
        assert "test" in t["path"].lower()           # num arquivo de teste


def test_related_tests_found_via_chain(g):
    # base não é chamado direto pelo teste — só via top; ainda assim aparece
    rt, _ = g.find_related_tests("base", depth=3)
    assert any("test_top" in t["test"] for t in rt["tests"])


# ============================================================================
# G. explain_symbol consistente com callers/callees/info
# ============================================================================

def test_explain_matches_primitives(g):
    data, _ = g.explain_symbol("mid")
    assert data["symbol"]["fqn"] == "core.mid"
    assert {c["fqn"] for c in data["callees"]} == {"core.base"}
    assert {c["fqn"] for c in data["callers"]} == {"core.top"}
    info, _ = g.symbol_info("mid")
    assert data["counts"] == info["counts"]


# ============================================================================
# H. suggest_files_to_read aponta o arquivo certo
# ============================================================================

def test_suggest_points_to_defining_file(g):
    data, _ = g.suggest_files_to_read("preciso mexer na função base do core")
    assert data["files"][0]["path"] == "core.py"


# ============================================================================
# I. Envelope estruturado bate com as linhas que acompanha
# ============================================================================

def test_envelope_confidence_matches_rows(g):
    _s, rows, env = g.callers("base", depth=2)
    resp = agent.build(render.calls(_s, rows, env, "callers de", "in"),
                       env, results=rows)
    assert resp.confidence == agent.aggregate_confidence(rows)
    assert resp.completeness.dynamic_dispatch_possible is True


def test_envelope_fields_present_on_all_edge_tools(g):
    calls = [
        ("references", g.references("base")),
        ("callers", g.callers("base")),
        ("callees", g.callees("top")),
        ("impact", g.impact("base")),
        ("change_impact", g.change_impact("core.py")),
    ]
    for name, res in calls:
        env = res[-1]
        assert env.static_analysis is True, name
        assert isinstance(env.unresolved_edges, int), name


# ============================================================================
# J. Frescor sequencial: editar/adicionar/remover reflete nas próximas chamadas
# ============================================================================

def test_sequential_edit_reflects(g):
    info, _ = g.symbol_info("core.base")
    assert info["symbol"]["fqn"] == "core.base"
    # remove base, adiciona novo símbolo — mesmo tamanho não bastaria, mudo tudo
    (g.indexer.root / "core.py").write_text(
        "def renamed_base():\n    return 42\n\ndef mid():\n"
        "    return renamed_base()\n", encoding="utf-8")
    # base sumiu
    rows, _ = g.find_symbol("base")
    assert not any(r["name"] == "base" for r in rows)
    # o novo aparece
    rows2, _ = g.find_symbol("renamed_base")
    assert any(r["name"] == "renamed_base" for r in rows2)


def test_sequential_new_file_becomes_queryable(g):
    (g.indexer.root / "extra.py").write_text(
        "from core import base\n\ndef extra():\n    return base()\n",
        encoding="utf-8")
    rows, _ = g.find_symbol("extra")
    assert any(r["fqn"] == "extra.extra" for r in rows)
    # e vira um novo caller de base
    _s, callers, _ = g.callers("base", depth=1)
    assert "extra.extra" in _fqns(callers)


def test_delete_then_query_removes(g):
    (g.indexer.root / "helpers.py").unlink()
    rows, _ = g.find_symbol("helper")
    assert not any(r["name"] == "helper" for r in rows)
    _s, callers, _ = g.callers("base", depth=1)
    assert "helpers.helper" not in _fqns(callers)


# ============================================================================
# K. Consistência de stats/doctor com o grafo real
# ============================================================================

def test_stats_internally_consistent(g):
    s = g.stats()
    assert s["edges"] == s["edges_resolved"] + s["edges_dangling"]
    n_files = g.indexer.conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    assert s["files"] == n_files
    assert sum(s["by_language"].values()) == n_files


def test_doctor_confidence_sums_to_call_edges(g):
    d = g.doctor()
    assert sum(d["confidence"].values()) == d["call_edges"]
    assert 0 <= d["certain_pct"] <= 100


# ============================================================================
# L. Workflow L1: refine promove e é idempotente (jedi resolve Python)
# ============================================================================

def test_refine_promotes_and_labels(g):
    pytest.importorskip("jedi")
    from codegraph import l1
    l1.refine(g.indexer)
    # mid→base deve estar certain/l1 após o refine
    _s, callees, _ = g.callees("mid", depth=1)
    base = [r for r in callees if r.get("other_fqn") == "core.base"]
    assert base and base[0]["confidence"] == "certain"
    assert base[0]["resolver"] == "l1/python"


def test_refine_is_idempotent(g):
    pytest.importorskip("jedi")
    from codegraph import l1
    l1.refine(g.indexer)
    s1 = g.stats()
    l1.refine(g.indexer)
    s2 = g.stats()
    assert (s1["edges"], s1["edges_resolved"]) == (s2["edges"], s2["edges_resolved"])


# ============================================================================
# M. Overview/communities apontam para símbolos reais
# ============================================================================

def test_overview_symbols_resolve(g):
    entries, _ = g.overview()
    for e in entries:
        for s in e["symbols"]:
            info, _ = g.symbol_info(s["fqn"])
            assert info["symbol"]["fqn"] == s["fqn"]


def test_communities_top_symbols_resolve(g):
    items, _meta, _ = g.communities(min_size=1)
    for c in items:
        for t in c["top_symbols"]:
            info, _ = g.symbol_info(t["fqn"])
            assert info["symbol"]["fqn"] == t["fqn"]
