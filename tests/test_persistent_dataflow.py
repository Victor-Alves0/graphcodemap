from __future__ import annotations

import threading
import time

import pytest

from codegraph import CodeGraph
from codegraph import cli
from codegraph.db import connect


PYTHON_FLOW = """
def consume(value):
    sink(value)
    return value


def handle(request):
    local = request
    result = consume(local)
    return result
"""


def test_persistent_flow_composes_assignment_argument_parameter_and_return(
        tmp_path):
    (tmp_path / "app.py").write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    built, envelope = graph.build_dataflow()

    assert envelope.fresh is True
    assert built["status"] == "complete"
    assert built["functions"] == 2
    assert built["nodes"] > 0
    assert built["edges"] > 0
    source, result, path_envelope = graph.flow_path(
        "app.handle.request", "app.consume.value")
    assert source["kind"] == "parameter"
    assert path_envelope.fresh is True
    assert result["persistent"] is True
    assert result["paths"]
    names = [node["access_path"] or node["name"]
             for node in result["paths"][0]["nodes"]]
    assert names[0] == "request"
    assert "local" in names
    assert "consume#0" in names
    assert names[-1] == "value"
    assert [edge["relation"] for edge in result["paths"][0]["edges"]] == [
        "assignment", "call_argument", "call_parameter"]
    assert all(edge["kind"] == "flows_to"
               for edge in result["paths"][0]["edges"])

    _, reachable, _ = graph.flow_path("app.handle.request")
    assert any(
        path["nodes"][-1]["details"].get("callee") == "sink"
        for path in reachable["paths"]
        if path["nodes"][-1]["kind"] == "call_argument"
    )
    graph.close()


def test_persistent_flow_reuses_hashes_and_invalidates_transitive_callers(
        tmp_path):
    source_path = tmp_path / "app.py"
    source_path.write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    first, _ = graph.build_dataflow()
    second, _ = graph.build_dataflow()

    assert first["rebuilt"] == 2
    assert second["rebuilt"] == 0
    assert second["reused"] == 2

    source_path.write_text(PYTHON_FLOW.replace(
        "sink(value)", "audit(value)\n    sink(value)"), encoding="utf-8")
    graph.index()
    dirty = graph.doctor()["dataflow"]
    assert dirty["status"] == "dirty"

    refreshed, _ = graph.build_dataflow()
    # consume changed; handle's call input includes the target body hash and is
    # rebuilt too, so stale interprocedural return edges cannot survive.
    assert refreshed["rebuilt"] == 2
    assert refreshed["status"] == "complete"
    graph.close()


def test_nonsemantic_revision_carries_persistent_flow_snapshot(tmp_path):
    (tmp_path / "app.py").write_text(PYTHON_FLOW, encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("first", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()

    readme.write_text("second", encoding="utf-8")
    graph.index()

    stage = graph.doctor()["dataflow"]
    assert stage["status"] == "complete"
    assert stage["details"]["cached"] is True
    cached, _ = graph.build_dataflow()
    assert cached["cached"] is True
    graph.close()


def test_java_fields_and_parameters_have_persistent_flow_paths(tmp_path):
    (tmp_path / "App.java").write_text(
        "class App {\n"
        "  String saved;\n"
        "  void store(String value) { this.saved = value; emit(this.saved); }\n"
        "}\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    built, _ = graph.build_dataflow()

    assert built["status"] == "complete"
    _, result, _ = graph.flow_path("App.store.value", "App.saved")
    assert result["paths"]
    assert result["paths"][0]["nodes"][-1]["access_path"] == "saved"
    graph.close()


def test_flow_query_repairs_an_unindexed_callee_change(tmp_path):
    source_path = tmp_path / "app.py"
    source_path.write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()

    source_path.write_text(PYTHON_FLOW.replace(
        "    sink(value)\n", ""), encoding="utf-8")
    _, reachable, envelope = graph.flow_path("app.handle.request")

    assert envelope.fresh is False
    assert not any(
        path["nodes"][-1]["details"].get("callee") == "sink"
        for path in reachable["paths"]
        if path["nodes"][-1]["kind"] == "call_argument"
    )
    assert graph.doctor()["dataflow"]["status"] == "complete"
    graph.close()


def test_cli_build_and_query_persistent_flow(tmp_path, capsys):
    (tmp_path / "app.py").write_text(PYTHON_FLOW, encoding="utf-8")
    assert cli.main(["--root", str(tmp_path), "index"]) == 0
    capsys.readouterr()

    assert cli.main(["--root", str(tmp_path), "dataflow-build"]) == 0
    assert "dataflow persistente [complete]" in capsys.readouterr().out
    assert cli.main([
        "--root", str(tmp_path), "flow-path",
        "app.handle.request", "app.consume.value",
    ]) == 0
    assert "consume#0" in capsys.readouterr().out


def test_taint_query_does_not_replace_persistent_dataflow_receipt(tmp_path):
    (tmp_path / "app.py").write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()

    graph.taint(entry="app.handle")

    assert graph.doctor()["dataflow"]["stage_version"] == "persistent-v1"
    stages = {row["stage"]: row["status"] for row in
              graph.indexer.conn.execute(
                  "SELECT stage,status FROM graph_stage_runs")}
    assert stages["dataflow"] == "complete"
    assert stages["taint"] == "executed"
    graph.close()


def test_persistent_build_is_atomic_on_materializer_failure(tmp_path, monkeypatch):
    from codegraph import flowgraph

    source_path = tmp_path / "app.py"
    source_path.write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()
    old_edges = graph.stats()["dataflow_edges"]

    source_path.write_text(PYTHON_FLOW.replace("sink(value)", "audit(value)"),
                           encoding="utf-8")
    graph.index()

    def fail(_self):
        raise RuntimeError("injected materializer failure")

    monkeypatch.setattr(flowgraph._FunctionBuilder, "build", fail)
    with pytest.raises(RuntimeError, match="injected materializer failure"):
        graph.build_dataflow()

    assert graph.stats()["dataflow_edges"] == old_edges
    assert graph.doctor()["dataflow"]["status"] == "dirty"
    graph.close()


def test_persistent_build_retries_a_short_concurrent_writer(tmp_path):
    source_path = tmp_path / "app.py"
    source_path.write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()
    source_path.write_text(PYTHON_FLOW.replace("sink(value)", "audit(value)"),
                           encoding="utf-8")
    graph.index()
    graph.indexer.conn.execute("PRAGMA busy_timeout=1")

    other = connect(graph.indexer.db_path)
    other.execute("BEGIN IMMEDIATE")
    other.execute("UPDATE meta SET value=value WHERE key='schema_version'")

    def release():
        time.sleep(0.12)
        other.rollback()

    thread = threading.Thread(target=release)
    thread.start()
    try:
        built, _ = graph.build_dataflow()
    finally:
        thread.join(timeout=2)
        other.close()
        graph.close()
    assert built["status"] == "complete"
