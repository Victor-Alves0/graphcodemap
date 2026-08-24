"""Camada orientada a agentes: envelope estruturado + tools de alto nível + MCP.

Trava (1) o envelope estável (confidence/fresh/completeness/truncated) e sua
agregação honesta; (2) os campos estruturados da Envelope calculados no engine;
(3) as 5 tools de alto nível (change_impact, find_affected_modules,
find_related_tests, explain_symbol, suggest_files_to_read); (4) que o servidor
MCP registra tudo e devolve o envelope como structured content."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph, agent
from codegraph.query import Envelope, _is_test_path, _paths_from_target


def _graph(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    return g


REPO = {
    "svc.py": "def helper():\n    return 1\n\ndef run():\n    return helper()\n",
    "tests/test_svc.py": "from svc import run\n\n"
                         "def test_run():\n    assert run() == 1\n",
    "util.py": "def unrelated():\n    return 0\n",
}


# ============================================================================
# A. agent.aggregate_confidence
# ============================================================================

def test_confidence_empty_is_na():
    assert agent.aggregate_confidence([]) == "n/a"


def test_confidence_single_label():
    assert agent.aggregate_confidence(
        [{"confidence": "certain"}, {"confidence": "certain"}]) == "certain"


def test_confidence_mixed():
    assert agent.aggregate_confidence(
        [{"confidence": "certain"}, {"confidence": "possible"}]) == "mixed"


def test_confidence_ignores_missing():
    assert agent.aggregate_confidence(
        [{"x": 1}, {"confidence": "inferred"}]) == "inferred"


# ============================================================================
# B. agent.build / agent.error
# ============================================================================

def test_build_maps_envelope_fields():
    env = Envelope()
    env.fresh = False
    env.truncated = True
    env.dynamic_dispatch = True
    env.unresolved_edges = 3
    r = agent.build("txt", env, results=[{"confidence": "possible"}])
    assert r.text == "txt"
    assert r.fresh is False and r.truncated is True
    assert r.confidence == "possible"
    assert r.completeness.unresolved_edges == 3
    assert r.completeness.dynamic_dispatch_possible is True
    assert r.completeness.static_analysis is True


def test_build_explicit_confidence_wins():
    r = agent.build("t", Envelope(), results=[{"confidence": "certain"}],
                    confidence="mixed")
    assert r.confidence == "mixed"


def test_error_response_is_neutral():
    r = agent.error("símbolo não encontrado: x")
    assert r.text.startswith("erro:")
    assert r.confidence == "n/a" and r.fresh is True
    assert r.completeness.unresolved_edges == 0


def test_response_serializes_to_stable_schema():
    r = agent.build("t", Envelope(), results=[])
    d = r.model_dump()
    assert set(d) == {"text", "results", "confidence", "fresh",
                      "semantic_status", "completeness", "truncated",
                      "warnings"}
    assert d["semantic_status"] == "not_started"
    assert set(d["completeness"]) == {
        "static_analysis", "unresolved_edges", "dynamic_dispatch_possible"}


# ============================================================================
# C. Campos estruturados da Envelope calculados no engine
# ============================================================================

def test_references_env_has_completeness(tmp_path):
    g = _graph(tmp_path, REPO)
    _s, _rows, env = g.query.references("helper")
    assert env.dynamic_dispatch is True
    assert env.static_analysis is True
    g.close()


def test_env_fresh_false_after_edit(tmp_path):
    g = _graph(tmp_path, {"a.py": "def alpha():\n    return 1\n"})
    # edit que MUDA O TAMANHO (o fast-path de frescor é size+mtime; um edit de
    # mesmo tamanho no mesmo segundo é honestamente indetectável por stat)
    (tmp_path / "a.py").write_text(
        "def alpha():\n    return 1234567890\n\ndef gamma():\n    return 2\n",
        encoding="utf-8")
    _rows, env = g.query.find_symbol("alpha")
    assert env.fresh is False   # drift detectado e corrigido
    g.close()


def test_env_truncated_on_limit(tmp_path):
    # muitos símbolos com o mesmo prefixo → find_symbol atinge o limite
    src = "".join(f"def item{i}():\n    return {i}\n\n" for i in range(20))
    g = _graph(tmp_path, {"a.py": src})
    _rows, env = g.query.find_symbol("item", limit=5)
    assert env.truncated is True
    g.close()


# ============================================================================
# D. change_impact / find_affected_modules
# ============================================================================

def test_change_impact_from_paths(tmp_path):
    g = _graph(tmp_path, REPO)
    data, _env = g.change_impact("svc.py")
    fqns = {r["fqn"] for r in data["impacted"]}
    assert "svc.run" in fqns                       # depende de helper
    assert any("test_run" in f for f in fqns)      # o teste também
    assert data["n_changed"] == 2                  # helper + run
    g.close()


def test_change_impact_from_diff(tmp_path):
    g = _graph(tmp_path, REPO)
    diff = textwrap.dedent("""\
        diff --git a/svc.py b/svc.py
        index 111..222 100644
        --- a/svc.py
        +++ b/svc.py
        @@ -1 +1 @@
        -def helper():
        +def helper2():
        """)
    data, _env = g.change_impact(diff)
    assert data["changed_files"] == ["svc.py"]
    g.close()


def test_change_impact_unknown_path_is_empty(tmp_path):
    g = _graph(tmp_path, REPO)
    data, _env = g.change_impact("nope.py")
    assert data["n_changed"] == 0 and data["impacted"] == []
    g.close()


def test_affected_modules_groups_by_file(tmp_path):
    g = _graph(tmp_path, REPO)
    data, _env = g.find_affected_modules("svc.py")
    paths = {m["path"] for m in data["modules"]}
    assert any(p.endswith("test_svc.py") for p in paths)
    assert all("count" in m and "min_depth" in m for m in data["modules"])
    g.close()


# ============================================================================
# E. find_related_tests
# ============================================================================

def test_related_tests_finds_test_caller(tmp_path):
    g = _graph(tmp_path, REPO)
    data, _env = g.find_related_tests("helper")
    assert any("test_run" in t["test"] for t in data["tests"])
    g.close()


def test_related_tests_excludes_non_test(tmp_path):
    g = _graph(tmp_path, REPO)
    data, _env = g.find_related_tests("helper")
    # run() chama helper mas NÃO é teste → não entra
    assert all(_is_test_path(t["path"]) for t in data["tests"])
    g.close()


def test_related_tests_none_when_untested(tmp_path):
    g = _graph(tmp_path, REPO)
    data, _env = g.find_related_tests("unrelated")
    assert data["n"] == 0
    g.close()


# ============================================================================
# F. explain_symbol
# ============================================================================

def test_explain_symbol_has_neighbors(tmp_path):
    g = _graph(tmp_path, REPO)
    data, _env = g.explain_symbol("run")
    assert data["symbol"]["fqn"] == "svc.run"
    assert any(c["fqn"] == "svc.helper" for c in data["callees"])
    assert data["counts"]["callees"] >= 1
    g.close()


# ============================================================================
# G. suggest_files_to_read
# ============================================================================

def test_suggest_ranks_relevant_file_first(tmp_path):
    g = _graph(tmp_path, REPO)
    data, _env = g.suggest_files_to_read("mexer no helper e no run")
    assert data["files"], "nada sugerido"
    assert data["files"][0]["path"] == "svc.py"
    assert "helper" in " ".join(data["tokens"])
    g.close()


# ============================================================================
# H. helpers puros
# ============================================================================

@pytest.mark.parametrize("p", [
    "tests/test_x.py", "test_foo.py", "foo_test.go", "src/FooTest.java",
    "spec/foo_spec.rb", "a/b.test.ts", "x.spec.js", "__tests__/a.js",
    "FooSpec.scala",
])
def test_is_test_path_true(p):
    assert _is_test_path(p)


@pytest.mark.parametrize("p", [
    "src/foo.py", "tester.py", "contest.py", "latest.go", "main.rs",
    "attestation.py",
])
def test_is_test_path_false(p):
    assert not _is_test_path(p)


def test_paths_from_diff():
    diff = "diff --git a/x/y.py b/x/y.py\n--- a/x/y.py\n+++ b/x/y.py\n"
    assert _paths_from_target(diff) == ["x/y.py"]


def test_paths_from_list():
    assert _paths_from_target("a.py, b/c.py  d.go") == ["a.py", "b/c.py", "d.go"]


def test_paths_from_diff_skips_dev_null():
    diff = "--- a/gone.py\n+++ /dev/null\n"
    assert _paths_from_target(diff) == ["gone.py"] or _paths_from_target(diff) == []


# ============================================================================
# I. Servidor MCP: envelope estruturado ponta a ponta
# ============================================================================

def _call(srv, name, args):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(srv.call_tool(name, args))


def test_mcp_registers_new_tools(tmp_path):
    from codegraph.mcp_server import build_server
    import asyncio
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    srv = build_server(tmp_path, watch=False)
    tools = asyncio.new_event_loop().run_until_complete(srv.list_tools())
    names = {t.name for t in tools}
    assert {"change_impact", "find_affected_modules", "find_related_tests",
            "explain_symbol", "suggest_files_to_read"} <= names


def test_mcp_returns_structured_envelope(tmp_path):
    from codegraph.mcp_server import build_server
    (tmp_path / "svc.py").write_text(
        "def helper():\n    return 1\ndef run():\n    return helper()\n",
        encoding="utf-8")
    srv = build_server(tmp_path, watch=False)
    _content, structured = _call(srv, "change_impact", {"paths_or_diff": "svc.py"})
    assert set(structured) >= {"text", "results", "confidence", "fresh",
                               "semantic_status", "completeness", "truncated"}
    assert set(structured["completeness"]) == {
        "static_analysis", "unresolved_edges", "dynamic_dispatch_possible"}


def test_mcp_reports_current_l1_lifecycle(tmp_path, monkeypatch):
    from codegraph import l1
    from codegraph.db import write_l1_lifecycle
    from codegraph.indexer import Indexer
    from codegraph.mcp_server import build_server

    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(l1, "refine", lambda _indexer: None)
    srv = build_server(tmp_path, watch=False)
    writer = Indexer(tmp_path)
    write_l1_lifecycle(writer.conn, {
        "status": "running", "started_at": 1, "published": False,
    })
    writer.conn.commit()
    writer.close()

    _content, structured = _call(srv, "find_symbol", {"query": "f"})

    assert structured["semantic_status"] == "running"


def test_mcp_error_path_is_structured(tmp_path):
    from codegraph.mcp_server import build_server
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    srv = build_server(tmp_path, watch=False)
    _content, structured = _call(srv, "symbol_info", {"symbol": "does_not_exist"})
    assert structured["text"].startswith("erro:")
    assert structured["confidence"] == "n/a"


def test_mcp_doctor_returns_structured_status_without_aggregating_distribution(
        tmp_path):
    from codegraph.mcp_server import build_server

    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    srv = build_server(tmp_path, watch=False)

    _content, structured = _call(srv, "doctor", {})

    assert structured["confidence"] == "n/a"
    assert len(structured["results"]) == 1
    assert isinstance(structured["results"][0]["confidence"], dict)
    assert "saúde do índice" in structured["text"]


def test_mcp_exposes_physical_tree_and_versioned_graph_history(tmp_path):
    from codegraph.mcp_server import build_server

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text(
        "def run():\n    return 1\n", encoding="utf-8")
    srv = build_server(tmp_path, watch=False)

    _content, tree = _call(srv, "repository_tree", {
        "path": "pkg", "depth": 2, "refresh": False,
    })
    _content, history = _call(srv, "graph_history", {"limit": 2})

    assert tree["confidence"] == "certain"
    assert {row["path"] for row in tree["results"]} == {"pkg", "pkg/app.py"}
    assert history["confidence"] == "certain"
    assert history["results"][0]["stages"]
