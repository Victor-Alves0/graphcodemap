"""Camada de dataflow (CPG-lite): intra-procedural + inter via call graph."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

# app com um caminho claro de fonte→sink cruzando funções:
# handler(request) -> build_query(user) -> run(sql) -> db.execute(sql)
APP = '''
def db_execute(sql):
    return sql


def run(sql):
    return db_execute(sql)


def build_query(user):
    q = "SELECT * FROM t WHERE u = " + user
    return run(q)


def sanitize(x):
    return x


def handler(request, unused):
    uid = request
    name = uid
    result = build_query(name)
    return result
'''

INTRA = '''
def process(data, count):
    tmp = data
    logged = transform(tmp)
    store(logged, count)
    return tmp
'''


@pytest.fixture()
def cgdf(tmp_path):
    (tmp_path / "app.py").write_text(textwrap.dedent(APP), encoding="utf-8")
    (tmp_path / "intra.py").write_text(textwrap.dedent(INTRA), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def _param(data, name):
    return next(p for p in data["params"] if p["name"] == name)


def test_intra_param_reaches_calls_and_return(cgdf):
    data, env = cgdf.data_flow("intra.process")
    assert data["supported"]
    p = _param(data, "data")
    callees = {(s["callee_name"], s["arg_index"]) for s in p["sinks"]}
    # data -> tmp -> transform(tmp) e store(logged); e alcança o retorno
    assert ("transform", 0) in callees
    assert ("store", 0) in callees
    assert p["reaches_return"] is True


def test_intra_second_param_isolated(cgdf):
    data, _ = cgdf.data_flow("intra.process")
    p = _param(data, "count")
    callees = {s["callee_name"] for s in p["sinks"]}
    assert "store" in callees          # store(logged, count) — count é arg#1
    assert "transform" not in callees  # count não flui para transform
    assert p["reaches_return"] is False


def test_interprocedural_sql_path(cgdf):
    """request deve alcançar db_execute atravessando build_query -> run."""
    data, _ = cgdf.data_flow("app.handler", depth=3)
    p = _param(data, "request")
    reached = {s["callee_fqn"] for s in p["sinks"] if s["resolved"]}
    assert "app.build_query" in reached
    # composição inter-procedural leva até run e db_execute
    assert any(f and f.endswith("run") for f in reached)
    assert "app.db_execute" in reached


def test_interprocedural_depth_limit(cgdf):
    shallow, _ = cgdf.data_flow("app.handler", depth=1)
    p = _param(shallow, "request")
    depths = {s["depth"] for s in p["sinks"]}
    assert depths == {1}  # só o primeiro salto


def test_confidence_is_carried(cgdf):
    data, _ = cgdf.data_flow("app.handler", depth=3)
    p = _param(data, "request")
    resolved = [s for s in p["sinks"] if s["resolved"]]
    assert resolved and all(s["confidence"] in ("certain", "inferred", "possible")
                            for s in resolved)


def test_unsupported_language_is_honest(tmp_path):
    # bash é extraído como função (tier genérico) mas não tem análise de fluxo
    (tmp_path / "deploy.sh").write_text("deploy() {\n  compile_all\n}\n",
                                        encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    try:
        data, env = graph.data_flow("deploy.deploy")
        assert data["supported"] is False
        assert any("fluxo" in w.lower() for w in env.warnings)
    finally:
        graph.close()


def test_completeness_warning_present(cgdf):
    _, env = cgdf.data_flow("intra.process")
    assert any("taint" in w.lower() or "sanitiz" in w.lower() for w in env.warnings)


# -- JavaScript/TypeScript ----------------------------------------------------

JS = '''
function outer(req, opts) {
  const a = req;
  inner(a);
  return a;
}

function inner(x) {
  sink(x);
}
'''


@pytest.fixture()
def cgjs(tmp_path):
    (tmp_path / "w.js").write_text(textwrap.dedent(JS), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def test_js_dataflow_param_to_call_and_return(cgjs):
    data, _ = cgjs.data_flow("w.outer")
    assert data["supported"]
    p = _param(data, "req")
    callees = {s["callee_name"] for s in p["sinks"]}
    assert "inner" in callees
    assert p["reaches_return"] is True


def test_js_interprocedural(cgjs):
    data, _ = cgjs.data_flow("w.outer", depth=2)
    p = _param(data, "req")
    # req -> inner(x) -> sink(x)
    assert any(s["callee_name"] == "sink" for s in p["sinks"])
