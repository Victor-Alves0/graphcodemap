"""Ciclos no call graph (recursão mútua a↔b, auto-recursão c→c): toda travessia
tem conjunto de visitados + profundidade limitada, então termina sem loop nem
explosão. Regressão para 'como o sistema lida com a chama b e b chama a'."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

SRC = '''
def a(x):
    return b(x)          # a -> b
def b(x):
    return a(x)          # b -> a  (ciclo mútuo)
def c(x):
    return c(x)          # c -> c  (auto-recursão)
def entry(x):
    return a(x)
'''


@pytest.fixture()
def cg(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent(SRC), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


@pytest.mark.timeout(30)
def test_mutual_recursion_callers_callees(cg):
    callees = {r["other_fqn"] for r in cg.callees("m.a")[1]}
    callers = {r["other_fqn"] for r in cg.callers("m.a")[1]}
    assert "m.b" in callees
    assert {"m.b", "m.entry"} <= callers


@pytest.mark.timeout(30)
def test_deep_traversal_over_cycle_terminates(cg):
    # profundidade alta sobre um ciclo não pode explodir nem travar
    _sym, rows, _ = cg.callees("m.a", depth=8)
    assert len(rows) <= 4                # a→b→a…, poucos nós únicos


@pytest.mark.timeout(30)
def test_self_recursion(cg):
    callers = {r["other_fqn"] for r in cg.callers("m.c")[1]}
    assert "m.c" in callers              # c chama a si mesma, resolvido


@pytest.mark.timeout(30)
def test_impact_over_cycle_terminates(cg):
    _sym, rows, _ = cg.impact("m.a", depth=8)
    fqns = {r["fqn"] for r in rows}
    assert "m.entry" in fqns             # alcança o topo sem loop


@pytest.mark.timeout(30)
def test_reaches_over_cycle_terminates(cg):
    # entry → a → b → a … alcançando um 'sink' regex; não pode travar
    _sym, data, _ = cg.reaches("m.entry", sink="b", depth=8)
    assert isinstance(data["paths"], list)


@pytest.mark.timeout(30)
def test_dataflow_over_cycle_terminates(cg):
    data, _ = cg.data_flow("m.a", depth=8)
    assert data is not None
