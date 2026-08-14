"""Contrato do benchmark competitivo: ferramentas diferentes, mesma evidência."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

from codegraph.eval.security_bench import (
    build_report,
    compare_reports,
    infer_category,
    infer_graphcodemap_category,
    infer_cwes,
    normalize_opengrep,
    normalize_path,
    normalize_sarif,
    execute,
    run_graphcodemap,
)


def test_cwe_and_category_normalization():
    assert infer_cwes("CWE-089", {"tags": ["security/CWE-79"]}) == ["79", "89"]
    assert infer_category(["89"], "anything") == "sql-injection"
    assert infer_category([], "LDAP Injection") == "ldap-injection"
    assert infer_graphcodemap_category("db.sequelize.query") == "sql-injection"
    assert infer_graphcodemap_category("getWriter.println") == "xss"
    assert infer_graphcodemap_category("FileOutputStream") == "path-traversal"
    assert infer_graphcodemap_category("Files.newInputStream") == "path-traversal"
    assert infer_graphcodemap_category("getSession.putValue") == "trust-boundary"
    assert infer_graphcodemap_category("ProcessBuilder.command") == "command-injection"
    assert infer_graphcodemap_category(
        "compile", {"sink": {"qualified": "xpath.compile"}}) == "xpath-injection"
    assert infer_graphcodemap_category(
        "compile", {"sink": {"qualified": "xp.compile"}}) == "xpath-injection"


def test_paths_are_repo_relative_and_uri_decoded(tmp_path):
    file = tmp_path / "src" / "a.py"
    assert normalize_path(str(file), tmp_path) == "src/a.py"
    assert normalize_path("file:///C:/repo/a%20b.py") == "C:/repo/a b.py"


def _sarif():
    return {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "CodeQL", "semanticVersion": "2.20.0",
                "rules": [{"id": "py/sql-injection", "properties": {
                    "tags": ["external/cwe/cwe-089"], "precision": "high"}}],
            }},
            "results": [{
                "ruleId": "py/sql-injection", "level": "error",
                "message": {"text": "SQL query built from user input"},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": "src/db.py"},
                    "region": {"startLine": 20, "startColumn": 5}}}],
                "codeFlows": [{"threadFlows": [{"locations": [
                    {"location": {"physicalLocation": {
                        "artifactLocation": {"uri": "src/web.py"},
                        "region": {"startLine": 4}}}},
                    {"location": {"physicalLocation": {
                        "artifactLocation": {"uri": "src/db.py"},
                        "region": {"startLine": 20, "startColumn": 5}}}},
                ]}]}],
            }],
        }],
    }


def test_sarif_normalizes_rule_cwe_category_and_flow():
    findings, tool, warnings = normalize_sarif(_sarif())
    assert warnings == []
    assert tool == {"name": "CodeQL", "version": "2.20.0",
                    "adapter_version": "1.0"}
    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "sql-injection"
    assert finding["cwes"] == ["89"]
    assert finding["source"]["path"] == "src/web.py"
    assert finding["sink"] == {"path": "src/db.py", "line": 20, "column": 5}
    assert finding["evidence"] == "sarif-code-flow"


def test_opengrep_normalizes_dataflow_and_deduplicates():
    result = {
        "check_id": "python.lang.security.audit.sqli",
        "path": "src/db.py", "start": {"line": 20, "col": 5},
        "extra": {
            "message": "SQL injection", "severity": "ERROR",
            "metadata": {"cwe": ["CWE-89"], "confidence": "HIGH"},
            "dataflow_trace": {"taint_source": [{
                "path": "src/web.py", "start": {"line": 4, "col": 2}}]},
        },
    }
    findings, tool, warnings = normalize_opengrep(
        {"version": "1.16.0", "results": [result, result], "errors": []})
    assert warnings == [] and tool["version"] == "1.16.0"
    assert len(findings) == 1
    assert findings[0]["source"]["path"] == "src/web.py"
    assert findings[0]["category"] == "sql-injection"


def test_report_matches_published_json_schema(tmp_path):
    findings, tool, _ = normalize_sarif(_sarif())
    report = build_report(tool=tool, root=tmp_path, findings=findings)
    schema_path = Path(__file__).resolve().parents[1] / "evals" / "security-report.schema.json"
    jsonschema.validate(report, json.loads(schema_path.read_text(encoding="utf-8")))
    assert report["summary"]["findings"] == 1
    assert report["summary"]["by_category"] == {"sql-injection": 1}
    assert report["target"]["git_commit"] is None


def test_comparison_distinguishes_exact_sink_from_same_file_category(tmp_path):
    findings, tool, _ = normalize_sarif(_sarif())
    left = build_report(tool=tool, root=tmp_path, findings=findings)
    moved = json.loads(json.dumps(findings))
    moved[0]["sink"]["line"] = 19
    # Fingerprint não é usado como equivalência cross-tool.
    right = build_report(tool={**tool, "name": "other"}, root=tmp_path,
                         findings=moved)
    comparison = compare_reports(left, right)
    assert comparison["exact_sink"]["common"] == 0
    assert comparison["file_category"]["common"] == 1


def test_comparison_rejects_different_commits(tmp_path):
    left = build_report(tool={"name": "a", "version": "1",
                              "adapter_version": "1"}, root=tmp_path, findings=[])
    right = json.loads(json.dumps(left))
    right["target"]["git_commit"] = "different"
    import pytest
    with pytest.raises(ValueError, match="commits diferentes"):
        compare_reports(left, right)


def test_command_runner_measures_child_process_tree():
    child = "x=bytearray(30*1024*1024); import time; time.sleep(.5)"
    parent = ("import subprocess,sys,time; "
              f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
              "time.sleep(.7)")
    result = execute([sys.executable, "-c", parent], timeout_s=5)
    assert result["exit_code"] == 0 and not result["timed_out"]
    assert result["peak_rss_mb"] is not None
    # Pai + filho de 30 MiB; protege contra medir apenas o launcher magro.
    assert result["peak_rss_mb"] > 35


def test_graphcodemap_report_measures_process_tree(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    isolated = tmp_path / ".state" / "graph.db"
    report = run_graphcodemap(tmp_path, db_path=isolated)
    assert report["invocation"]["status"] == "complete"
    assert report["invocation"]["memory_scope"] == "process-tree"
    assert report["invocation"]["peak_rss_mb"] is not None
    assert Path(report["extra"]["db_path"]) == isolated.resolve()


def test_opentaint_rule_ids_are_resolved_and_validated(tmp_path):
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "evals" / "securitybench.py"
    spec = importlib.util.spec_from_file_location("securitybench_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    rules = tmp_path / "security"
    rules.mkdir()
    (rules / "sqli.yaml").write_text(
        "rules:\n  - id: sql-injection\n    languages: [java]\n",
        encoding="utf-8")
    assert module._resolve_rule_ids(str(tmp_path), ["sql-injection"]) == [
        "security/sqli.yaml:sql-injection"]
    assert module._resolve_rule_ids(
        str(tmp_path), [r"security\sqli.yaml#sql-injection"]) == [
            "security/sqli.yaml:sql-injection"]
    import pytest
    with pytest.raises(ValueError, match="não encontrado"):
        module._resolve_rule_ids(str(tmp_path), ["typo"])
