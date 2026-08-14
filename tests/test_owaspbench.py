from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "evals" / "owaspbench.py"
    spec = importlib.util.spec_from_file_location("owaspbench_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_wrong_category_is_not_scored_as_hit():
    bench = _module()
    cases = ["vuln", "safe"]
    gt = {"vuln": ("sqli", True), "safe": ("sqli", False)}
    flagged = {"vuln": {"xss"}, "safe": {"xss"}}
    per_cat, total, wrong = bench.score_cases(cases, gt, flagged)
    assert per_cat["sqli"] == {"tp": 0, "fp": 0, "tn": 1, "fn": 1}
    assert total == {"tp": 0, "fp": 0, "tn": 1, "fn": 1}
    assert wrong == 2


def test_matching_category_counts_normally():
    bench = _module()
    cases = ["vuln", "safe"]
    gt = {"vuln": ("cmdi", True), "safe": ("cmdi", False)}
    flagged = {"vuln": {"cmdi"}, "safe": {"cmdi"}}
    _, total, wrong = bench.score_cases(cases, gt, flagged)
    assert total == {"tp": 1, "fp": 1, "tn": 0, "fn": 0}
    assert wrong == 0


def test_normalized_report_requires_complete_status_for_scoreboard(tmp_path):
    bench = _module()
    (tmp_path / "expectedresults-1.2.csv").write_text(
        "BenchmarkTest00001,sqli,true\nBenchmarkTest00002,sqli,false\n",
        encoding="utf-8")
    report = {
        "tool": {"name": "competitor", "version": "1"},
        "target": {"git_commit": "abc"},
        "invocation": {"status": "partial", "duration_s": 2.5},
        "findings": [{"category": "sql-injection",
                      "sink": {"path": "x/BenchmarkTest00001.java", "line": 1}}],
    }
    scored = bench.score_normalized_report(tmp_path, report)
    assert scored["total"] == {"tp": 1, "fp": 0, "tn": 1, "fn": 0}
    assert scored["eligible"] is False
    assert scored["tool"] == "competitor"
