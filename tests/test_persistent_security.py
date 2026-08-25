"""First G4 rule family: path traversal over persisted value paths only."""

from __future__ import annotations

from codegraph import CodeGraph


PYTHON_VULNERABLE = """
def download(user_path):
    selected = user_path
    return open(selected)
"""


JAVA_VULNERABLE = """
import java.io.FileInputStream;

class App {
    void download(String userPath) throws Exception {
        String selected = userPath;
        new FileInputStream(selected);
    }
}
"""


def _graph(tmp_path, name: str, source: str) -> CodeGraph:
    (tmp_path / name).write_text(source, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()
    return graph


def test_python_entry_parameter_reaches_file_sink_with_inspectable_path(
        tmp_path):
    graph = _graph(tmp_path, "app.py", PYTHON_VULNERABLE)

    entry, result, envelope = graph.path_traversal("app.download")

    assert entry["fqn"] == "app.download"
    assert result["verdict"] == "candidate"
    assert result["rule_id"] == "path-traversal"
    assert result["cwe"] == "CWE-22"
    assert result["persistent"] is True
    assert result["engine"] == "persistent-dataflow"
    assert result["stage"]["stage_version"].startswith("persistent-v")
    assert result["freshness"] == {"fresh": True, "repaired": False}
    finding = result["findings"][0]
    assert finding["rule_id"] == "python.path.open"
    assert finding["confidence"] == "inferred"
    assert finding["source"]["name"] == "user_path"
    assert finding["source"]["path"] == "app.py"
    assert finding["sink"]["details"]["callee"] == "open"
    assert finding["sink"]["path"] == "app.py"
    assert [node["name"] for node in finding["path"]["nodes"]] == [
        "user_path", "selected", "open#0"]
    assert [edge["relation"] for edge in finding["path"]["edges"]] == [
        "assignment", "call_argument"]
    assert all(edge["kind"] == "flows_to"
               for edge in finding["path"]["edges"])
    assert finding["evidence"]["kind"] == "persistent-value-path"
    assert finding["sanitization"]["status"] == "not_proven"
    assert result["completeness"]["complete"] is False
    assert envelope.dynamic_dispatch is True
    graph.close()


def test_java_entry_parameter_reaches_path_constructor_deterministically(
        tmp_path):
    graph = _graph(tmp_path, "App.java", JAVA_VULNERABLE)

    _entry, first, _envelope = graph.path_traversal("App.download")
    _entry, second, _envelope = graph.path_traversal("App.download")

    assert first == second
    assert first["verdict"] == "candidate"
    finding = first["findings"][0]
    assert finding["rule_id"] == "java.path.file-input-stream"
    assert finding["cwe"] == "CWE-22"
    assert finding["source"]["name"] == "userPath"
    assert finding["sink"]["evidence"]["argument_index"] == 0
    assert finding["path"]["nodes"][-1]["name"] == "FileInputStream#0"
    graph.close()


def test_absence_of_persisted_path_is_unknown_not_safe(tmp_path):
    graph = _graph(tmp_path, "app.py", """
def download(user_path):
    return open("fixed.txt")
""")

    _entry, result, envelope = graph.path_traversal("app.download")

    assert result["findings"] == []
    assert result["verdict"] == "unknown"
    assert result["completeness"]["complete"] is False
    assert any("unknown" in warning for warning in envelope.warnings)
    graph.close()


def test_sanitizer_shaped_assignment_never_fabricates_safe_verdict(tmp_path):
    graph = _graph(tmp_path, "app.py", """
import os

def download(user_path):
    selected = os.path.basename(user_path)
    return open(selected)
""")

    _entry, result, _envelope = graph.path_traversal("app.download")

    # G3 currently collapses this RHS to an assignment flow without proving
    # that basename's return is sanitized. G4 must fail open, not hide it.
    assert result["verdict"] == "candidate"
    finding = result["findings"][0]
    assert finding["sanitization"]["status"] == "not_proven"
    assert "never suppress" in finding["sanitization"]["policy"]
    graph.close()


def test_path_traversal_read_repairs_and_rebuilds_persisted_stage(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("""
def download(user_path):
    return open("fixed.txt")
""", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()
    source.write_text(PYTHON_VULNERABLE, encoding="utf-8")

    _entry, result, envelope = graph.path_traversal("app.download")

    assert result["verdict"] == "candidate"
    assert envelope.fresh is False
    assert result["freshness"] == {"fresh": False, "repaired": True}
    assert result["stage"]["status"] == "complete"
    graph.close()


def test_path_traversal_repairs_changed_callee_in_another_file(tmp_path):
    (tmp_path / "entry.py").write_text("""
from worker import consume

def download(user_path):
    consume(user_path)
""", encoding="utf-8")
    worker = tmp_path / "worker.py"
    worker.write_text("""
def consume(value):
    audit(value)
""", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()
    _entry, before, _envelope = graph.path_traversal("entry.download")
    assert before["verdict"] == "unknown"

    worker.write_text("""
def consume(value):
    open(value)
""", encoding="utf-8")
    _entry, after, envelope = graph.path_traversal("entry.download")

    assert envelope.fresh is False
    assert after["verdict"] == "candidate"
    assert [node["path"] for node in after["findings"][0]["path"]["nodes"]][
        -2:] == ["worker.py", "worker.py"]
    graph.close()


def test_nonqueryable_stage_does_not_read_an_old_snapshot(tmp_path, monkeypatch):
    from codegraph import persistent_security

    graph = _graph(tmp_path, "app.py", PYTHON_VULNERABLE)
    monkeypatch.setattr(
        persistent_security, "ensure",
        lambda _engine: {"status": "partial", "queryable": False})

    def stale_read(*_args, **_kwargs):
        raise AssertionError("old dataflow snapshot was queried")

    monkeypatch.setattr(persistent_security, "_entry_sources", stale_read)

    _entry, result, envelope = graph.path_traversal("app.download")

    assert result["verdict"] == "unknown"
    assert result["findings"] == []
    assert result["completeness"]["complete"] is False
    assert any("não é consultável" in warning for warning in envelope.warnings)
    graph.close()


def test_path_traversal_enforces_resource_bounds(tmp_path):
    graph = _graph(tmp_path, "app.py", """
def download(user_path):
    open(user_path)
    open(user_path)
""")

    _entry, result, envelope = graph.path_traversal(
        "app.download", max_hops=4, max_findings=1)

    assert len(result["findings"]) == 1
    assert envelope.truncated is True
    assert result["completeness"]["max_hops"] == 4
    assert result["completeness"]["max_findings"] == 1
    graph.close()
