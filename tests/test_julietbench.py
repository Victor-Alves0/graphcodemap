from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from evals import julietbench


_CASE = "CWE23_Relative_Path_Traversal__Environment_01.java"


def _java_root(tmp_path: Path, *, with_case: bool = True) -> Path:
    root = tmp_path / "Java"
    root.mkdir()
    case = (
        f'<testcase><file path="{_CASE}">'
        '<flaw line="10" name="CWE-023"/>'
        "</file></testcase>"
        if with_case else ""
    )
    (root / "manifest.xml").write_text(
        f"<container>{case}</container>", encoding="utf-8")
    return root


def _finding(source_method: str, sink_method: str,
             *, category: str = "path-traversal") -> dict:
    path = f"testcases/CWE23_Relative_Path_Traversal/{_CASE}"
    return {
        "category": category,
        "source": {"path": path, "line": 10,
                   "symbol": f"example.Case.{source_method}"},
        "sink": {"path": path, "line": 20,
                 "symbol": f"example.Case.{sink_method}"},
    }


def _report(*findings: dict, status: str = "complete") -> dict:
    return {
        "tool": {"name": "test-sast", "version": "1"},
        "invocation": {"status": status},
        "findings": list(findings),
    }


def test_bad_flow_is_tp_and_good_flow_is_fp(tmp_path: Path) -> None:
    result = julietbench.score(
        _java_root(tmp_path),
        _report(
            _finding("badSource", "badSink"),
            _finding("goodG2BSource", "goodG2BSink"),
        ),
    )

    assert result["total"] == {"tp": 1, "fp": 1, "fn": 0, "tn": 0}
    assert result["flow_findings"] == {
        "bad-bad": 1, "contains-good": 1, "unknown": 0,
    }


def test_non_path_category_is_ignored(tmp_path: Path) -> None:
    result = julietbench.score(
        _java_root(tmp_path),
        _report(_finding("badSource", "badSink", category="sql-injection")),
    )

    assert result["relevant_findings"] == 0
    assert result["total"] == {"tp": 0, "fp": 0, "fn": 1, "tn": 1}


def test_partial_report_is_ineligible_and_main_exits_two(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    java_root = _java_root(tmp_path)
    report_path = tmp_path / "partial.json"
    report_path.write_text(
        json.dumps(_report(status="partial")), encoding="utf-8")
    result = julietbench.score(java_root, _report(status="partial"))

    assert result["eligible"] is False
    monkeypatch.setattr(
        sys, "argv",
        ["julietbench.py", str(java_root), "--report", str(report_path)],
    )
    assert julietbench.main() == 2


def test_empty_manifest_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Juliet CWE-23 cases"):
        julietbench.score(_java_root(tmp_path, with_case=False), _report())


def test_variant_81_uses_concrete_class_label_for_abstract_action(
        tmp_path: Path) -> None:
    endpoint = {
        "path": "testcases/CWE23_Relative_Path_Traversal/Case_81_bad.java",
        "line": 20,
        "symbol": "example.Case_81_bad.action",
    }

    assert julietbench._endpoint_kind(_java_root(tmp_path), endpoint) == "bad"
