"""Contrato do benchmark competitivo: ferramentas diferentes, mesma evidência."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from codegraph.eval.security_bench import (
    build_report,
    compare_reports,
    deduplicate,
    infer_category,
    infer_graphcodemap_category,
    infer_cwes,
    location,
    make_finding,
    normalize_opengrep,
    normalize_path,
    normalize_sarif,
    execute,
    run_graphcodemap,
    sarif_execution_health,
    validate_report,
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


def test_root_bound_paths_reject_traversal_absolute_escape_and_symlink(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "src" / "a.py"
    inside.parent.mkdir()
    inside.write_text("pass\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")

    assert normalize_path(str(inside), root) == "src/a.py"
    assert normalize_path("../outside.py", root) is None
    assert normalize_path(str(outside), root) is None

    link = root / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    assert normalize_path("linked.py", root) is None


def test_report_rejects_non_relative_finding_paths(tmp_path):
    finding = make_finding(
        rule_id="path", category="path-traversal",
        sink={"path": "../outside.py", "line": 1},
    )
    with pytest.raises(ValueError, match="path não relativo"):
        build_report(
            tool={"name": "test", "version": "1", "adapter_version": "1"},
            root=tmp_path, findings=[finding],
        )


def _sarif():
    return {
        "version": "2.1.0",
        "runs": [{
            "invocations": [{
                "executionSuccessful": True,
                "exitCode": 0,
                "toolExecutionNotifications": [],
            }],
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
                    "adapter_version": "1.1"}
    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "sql-injection"
    assert finding["cwes"] == ["89"]
    assert finding["source"]["path"] == "src/web.py"
    assert finding["sink"] == {"path": "src/db.py", "line": 20, "column": 5}
    assert finding["evidence"] == "sarif-code-flow"


def test_sarif_execution_health_accepts_explicit_success():
    partial, errors, exit_code = sarif_execution_health(_sarif())
    assert partial is False
    assert errors == []
    assert exit_code == 0


def test_sarif_error_notification_alone_forces_partial():
    data = _sarif()
    data["runs"][0]["invocations"][0]["toolExecutionNotifications"] = [{
        "level": "error",
        "message": {"text": "database incomplete"},
    }]
    partial, errors, exit_code = sarif_execution_health(data)
    assert partial is True
    assert errors == [
        "SARIF run 0 invocation 0 notification 0: database incomplete"]
    assert exit_code == 0


@pytest.mark.parametrize("success", [False, pytest.param(None, id="missing")])
def test_aborted_or_unproven_sarif_import_is_partial_and_cli_returns_two(
        tmp_path, monkeypatch, success):
    from evals import securitybench as cli

    data = _sarif()
    invocation = {
        "exitCode": 3,
        "toolExecutionNotifications": [{
            "level": "error",
            "message": {"text": "analysis aborted"},
        }],
    }
    if success is not None:
        invocation["executionSuccessful"] = success
    data["runs"][0]["invocations"] = [invocation]
    input_path = tmp_path / "aborted.sarif"
    input_path.write_text(json.dumps(data), encoding="utf-8")

    report = cli.import_report("sarif", input_path, tmp_path)
    assert report["status"] == "partial"
    assert report["invocation"]["status"] == "partial"
    assert report["invocation"]["exit_code"] == 3
    assert report["truncated"] is False
    assert any("executionSuccessful" in error for error in report["errors"])
    assert any("analysis aborted" in error for error in report["errors"])

    output = tmp_path / "normalized.json"
    monkeypatch.setattr(
        sys, "argv", ["securitybench", "sarif", str(input_path),
                      "--root", str(tmp_path), "--out", str(output)])
    assert cli.main() == 2
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["status"] == "partial"
    assert persisted["errors"] == report["errors"]


def test_healthy_sarif_import_remains_complete(tmp_path):
    from evals import securitybench as cli

    input_path = tmp_path / "healthy.sarif"
    input_path.write_text(json.dumps(_sarif()), encoding="utf-8")
    report = cli.import_report("sarif", input_path, tmp_path)
    assert report["status"] == "complete"
    assert report["invocation"]["exit_code"] == 0
    assert report["truncated"] is False
    assert report["errors"] == []


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

    bad_exit = json.loads(json.dumps(report))
    bad_exit["invocation"]["exit_code"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            bad_exit, json.loads(schema_path.read_text(encoding="utf-8")))

    escaped = json.loads(json.dumps(report))
    escaped["findings"][0]["sink"]["path"] = "../outside.py"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            escaped, json.loads(schema_path.read_text(encoding="utf-8")))

    for field, value in (("truncated", True), ("errors", ["failure"])):
        contradictory = json.loads(json.dumps(report))
        contradictory[field] = value
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                contradictory,
                json.loads(schema_path.read_text(encoding="utf-8")))


def test_report_runtime_rejects_complete_nonzero_and_unhashed_dirty_engine(
        tmp_path):
    tool = {"name": "test", "version": "1", "adapter_version": "1"}
    with pytest.raises(ValueError, match="exit_code zero"):
        build_report(
            tool=tool, root=tmp_path, findings=[], status="complete",
            exit_code=3,
        )
    with pytest.raises(ValueError, match="truncated=false"):
        build_report(
            tool=tool, root=tmp_path, findings=[], status="complete",
            truncated=True,
        )
    with pytest.raises(ValueError, match=r"errors=\[\]"):
        build_report(
            tool=tool, root=tmp_path, findings=[], status="complete",
            errors=["hidden failure"],
        )
    with pytest.raises(ValueError, match="source_tree_sha256"):
        build_report(
            tool=tool, root=tmp_path, findings=[],
            analysis={
                "engine": {"git_commit": "a" * 40, "git_dirty": True},
                "config": {},
            },
        )


def test_validate_report_rejects_complete_with_truncation_or_errors(tmp_path):
    tool = {"name": "test", "version": "1", "adapter_version": "1"}
    report = build_report(tool=tool, root=tmp_path, findings=[])
    truncated = json.loads(json.dumps(report))
    truncated["truncated"] = True
    with pytest.raises(ValueError, match="truncated=false"):
        validate_report(truncated, root=tmp_path)
    errored = json.loads(json.dumps(report))
    errored["errors"] = ["failure"]
    with pytest.raises(ValueError, match=r"errors=\[\]"):
        validate_report(errored, root=tmp_path)


def test_fingerprint_distinguishes_same_line_sites():
    source = location("src/App.java", 4, 20)
    left = make_finding(
        rule_id="path", category="path-traversal", source=source,
        sink=location("src/App.java", 5, 9, byte_span={"start": 80, "end": 90}),
    )
    right = make_finding(
        rule_id="path", category="path-traversal", source=source,
        sink=location("src/App.java", 5, 42, byte_span={"start": 113, "end": 123}),
    )
    assert left["fingerprint"] != right["fingerprint"]
    assert len(deduplicate([left, right])) == 2


def test_report_subject_identifies_git_subdirectory(tmp_path):
    repo = tmp_path / "repo"
    scan_root = repo / "services" / "api"
    scan_root.mkdir(parents=True)
    (scan_root / "App.java").write_text("class App {}", encoding="utf-8")
    commands = [
        ["git", "init"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Test"],
        ["git", "remote", "add", "origin", "git@github.com:owner/repo.git"],
        ["git", "add", "."],
        ["git", "commit", "-m", "fixture"],
    ]
    for command in commands:
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    report = build_report(
        tool={"name": "test", "version": "1", "adapter_version": "1"},
        root=scan_root, findings=[],
    )
    assert report["subject"] == {
        "repository": "https://github.com/owner/repo.git",
        "commit": commit,
        "scan_subdir": "services/api",
    }
    assert report["target"]["git_dirty"] is False

    # Dirtiness is scoped to the scan root: unrelated untracked files do not
    # taint this subject, while an untracked input below it does.
    (repo / "outside-scan.txt").write_text("x", encoding="utf-8")
    outside_report = build_report(
        tool={"name": "test", "version": "1", "adapter_version": "1"},
        root=scan_root, findings=[],
    )
    assert outside_report["target"]["git_dirty"] is False
    (scan_root / "Untracked.java").write_text("class Untracked {}", encoding="utf-8")
    dirty_report = build_report(
        tool={"name": "test", "version": "1", "adapter_version": "1"},
        root=scan_root, findings=[],
    )
    assert dirty_report["target"]["git_dirty"] is True


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
    engine = report["analysis"]["engine"]
    assert engine["git_commit"]
    assert isinstance(engine["git_dirty"], bool)
    assert len(engine["source_tree_sha256"]) == 64


@pytest.mark.parametrize(("status", "expected"), [
    ("complete", 0),
    ("partial", 2),
    ("failed", 1),
])
def test_securitybench_cli_exit_code_reflects_report_completeness(
        tmp_path, monkeypatch, status, expected):
    from evals import securitybench as cli

    report = {
        "status": status,
        "invocation": {
            "status": status, "duration_s": 0.0, "peak_rss_mb": None,
        },
        "tool": {"name": "test", "version": "1"},
        "target": {"name": "repo", "git_commit": None, "git_dirty": False},
        "summary": {"findings": 0, "with_source": 0, "by_category": {}},
        "warnings": [],
        "errors": [],
    }
    monkeypatch.setattr(cli, "run_graphcodemap", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        sys, "argv", ["securitybench", "graphcodemap", str(tmp_path),
                      "--out", str(tmp_path / f"{status}.json")])
    assert cli.main() == expected


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
