"""G4 repo-wide sources and sanitizer cuts use canonical call-result nodes."""

from __future__ import annotations

import json

from codegraph import CodeGraph


def _graph(tmp_path, files: dict[str, str]) -> CodeGraph:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def test_python_external_source_result_seeds_repo_scan(tmp_path):
    graph = _graph(tmp_path, {"app.py": """
def download():
    selected = input()
    open(selected)
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["mode"] == "scan"
    assert result["verdict"] == "candidate"
    finding = result["findings"][0]
    assert finding["source"]["kind"] == "call_result"
    assert finding["source"]["details"]["callee"] == "input"
    assert finding["source"]["evidence"]["kind"] == (
        "persisted external source result")
    assert "input" in finding["source"]["evidence"]["matched_rules"]
    assert finding["sink"]["details"]["callee"] == "open"
    assert "input::$result" in [
        node["name"] for node in finding["path"]["nodes"]]
    graph.close()


def test_python_framework_source_result_seeds_repo_scan(tmp_path):
    graph = _graph(tmp_path, {"app.py": """
from flask import request
def download():
    selected = request.args.get("name")
    open(selected)
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "candidate"
    assert result["findings"][0]["source"]["details"]["callee"] == "get"
    graph.close()


def test_java_servlet_source_result_seeds_repo_scan(tmp_path):
    graph = _graph(tmp_path, {"App.java": """
import java.io.FileInputStream;
import javax.servlet.http.HttpServletRequest;
class App {
  void download(HttpServletRequest request) throws Exception {
    String selected = request.getParameter("name");
    new FileInputStream(selected);
  }
}
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "candidate"
    finding = result["findings"][0]
    assert finding["source"]["details"]["callee"] == "getParameter"
    assert finding["sink"]["details"]["callee"] == "FileInputStream"
    graph.close()


def test_trusted_java_source_literal_does_not_seed_repo_scan(tmp_path):
    graph = _graph(tmp_path, {"App.java": """
import java.io.FileInputStream;
class App {
  void load() throws Exception {
    String selected = System.getProperty("user.dir");
    new FileInputStream(selected);
  }
}
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "unknown"
    assert result["findings"] == []
    graph.close()


def test_nested_source_flows_through_persisted_sanitizer_and_is_cut(tmp_path):
    graph = _graph(tmp_path, {"app.py": """
import os
def download():
    selected = os.path.basename(input())
    open(selected)
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "unknown"
    assert result["findings"] == []
    assert result["completeness"]["sanitized_paths"] >= 1
    graph.close()


def test_java_path_sanitizer_result_cuts_source(tmp_path):
    graph = _graph(tmp_path, {"App.java": """
import java.io.FileInputStream;
import javax.servlet.http.HttpServletRequest;
import org.apache.commons.io.FilenameUtils;
class App {
  void download(HttpServletRequest request) throws Exception {
    String selected = FilenameUtils.getName(request.getParameter("name"));
    new FileInputStream(selected);
  }
}
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "unknown"
    assert result["completeness"]["sanitized_paths"] >= 1
    graph.close()


def test_qualified_sanitizer_rule_does_not_cut_domain_homonym(tmp_path):
    graph = _graph(tmp_path, {"app.py": """
class Domain:
    def basename(self, value):
        return value
domain = Domain()
def download(user_path):
    selected = domain.basename(user_path)
    open(selected)
"""})

    _entry, result, _envelope = graph.path_traversal("app.download")

    assert result["verdict"] == "candidate"
    graph.close()


def test_direct_nested_source_call_reaches_sink_argument(tmp_path):
    graph = _graph(tmp_path, {"app.py": """
def download():
    open(input())
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "candidate"
    names = [node["name"] for node in result["findings"][0]["path"]["nodes"]]
    assert names[0] == "input::$result"
    assert names[-1] == "open#0"
    graph.close()


def test_direct_sanitizer_result_cuts_nested_source(tmp_path):
    graph = _graph(tmp_path, {"app.py": """
import os
def download():
    open(os.path.basename(input()))
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "unknown"
    assert result["completeness"]["sanitized_paths"] >= 1
    graph.close()


def test_sanitizer_in_mixed_expression_does_not_hide_other_source(tmp_path):
    graph = _graph(tmp_path, {"app.py": """
import os
def download():
    open(input() + os.path.basename("fixed"))
"""})

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "candidate"
    graph.close()


def test_repo_source_result_composes_across_callee_parameter(tmp_path):
    graph = _graph(tmp_path, {
        "entry.py": """
from worker import consume
def download():
    selected = input()
    consume(selected)
""",
        "worker.py": """
def consume(value):
    open(value)
""",
    })

    _entry, result, _envelope = graph.path_traversal()

    assert result["verdict"] == "candidate"
    finding = result["findings"][0]
    assert any(edge["relation"] == "call_parameter"
               for edge in finding["path"]["edges"])
    assert finding["sink"]["path"] == "worker.py"
    graph.close()


def test_repo_source_rules_are_configurable_without_rebuilding_value_graph(
        tmp_path):
    graph = _graph(tmp_path, {"app.py": """
def download():
    selected = tenant_input()
    open(selected)
"""})
    graph.build_dataflow()
    _entry, before, _envelope = graph.path_traversal()
    assert before["verdict"] == "unknown"

    config = tmp_path / ".codegraph" / "taint.json"
    config.write_text(json.dumps({"sources": ["tenant_input"]}),
                      encoding="utf-8")
    _entry, after, _envelope = graph.path_traversal()

    assert after["verdict"] == "candidate"
    assert after["findings"][0]["source"]["details"]["callee"] == "tenant_input"
    graph.close()


def test_repo_sanitizer_rules_are_configurable_without_rebuilding_value_graph(
        tmp_path):
    graph = _graph(tmp_path, {"app.py": """
def download():
    selected = tenant_clean(input())
    open(selected)
"""})
    graph.build_dataflow()
    _entry, before, _envelope = graph.path_traversal()
    assert before["verdict"] == "candidate"

    config = tmp_path / ".codegraph" / "taint.json"
    config.write_text(json.dumps({"sanitizers": ["tenant_clean"]}),
                      encoding="utf-8")
    _entry, after, _envelope = graph.path_traversal()

    assert after["verdict"] == "unknown"
    assert after["completeness"]["sanitized_paths"] >= 1
    graph.close()


def test_repo_scan_scope_filters_source_files(tmp_path):
    graph = _graph(tmp_path, {
        "allowed/app.py": "def run():\n    open(input())\n",
        "other/app.py": "def run():\n    open(input())\n",
    })

    _entry, result, _envelope = graph.path_traversal(scope="allowed")

    assert result["verdict"] == "candidate"
    assert {finding["source"]["path"] for finding in result["findings"]} == {
        "allowed/app.py"}
    graph.close()
