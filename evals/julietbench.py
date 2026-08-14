"""Score normalized SAST reports against NIST Juliet Java CWE-23.

This is an independent holdout for path traversal.  It intentionally does not
share fixtures or labels with OWASP Benchmark.  Juliet embeds one vulnerable
``bad`` flow and one or more safe ``good`` flows in each generated testcase.

The scorer uses the official ``manifest.xml`` only to enumerate vulnerable
testcases.  Findings are assigned to ``bad`` or ``good`` flows from their
source/sink symbols (or, as a fallback, from the containing generated Java
method).  A safe flow is never inferred from the absence of a manifest line.

Corpus pin:
  NIST SARD suite #111, Juliet Java 1.3 (2017-10-01)
  SHA-256 d985f4177c2bcd7b03455a05c1c8f2e755f55c9eb250accd052f05f877347e60
  https://samate.nist.gov/SARD/test-suites/111

Usage::

    python evals/julietbench.py <extracted Java dir> --report report.json
    python evals/julietbench.py <extracted Java dir> --report report.json \
        --json juliet-cwe23-score.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


SUITE_ID = 111
SUITE_VERSION = "Juliet Java 1.3"
SUITE_SHA256 = "d985f4177c2bcd7b03455a05c1c8f2e755f55c9eb250accd052f05f877347e60"
_PREFIX = "CWE23_Relative_Path_Traversal__"


def _manifest_cases(manifest: Path) -> list[list[str]]:
    """Return the files in every CWE-23 testcase.

    Juliet 1.3's full manifest is not well-formed XML (a mismatched tag exists
    outside CWE-23), so deliberately parse its small, regular testcase/file
    records rather than accepting a repaired copy of the ground truth.
    """
    text = manifest.read_text(encoding="utf-8", errors="replace")
    cases: list[list[str]] = []
    for block in re.findall(r"<testcase>(.*?)</testcase>", text, re.DOTALL):
        files = []
        for path, body in re.findall(
                r'<file path="([^"]+)">(.*?)</file>', block, re.DOTALL):
            if path.startswith(_PREFIX) and "<flaw " in body:
                files.append(path)
        if files:
            cases.append(files)
    if not cases:
        raise ValueError(f"no Juliet CWE-23 cases found in {manifest}")
    return cases


def _method_from_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    method = symbol.rsplit(".", 1)[-1]
    return method if method else None


def _method_at_line(source: Path, line: int) -> str | None:
    """Best-effort fallback for SARIF adapters that omit endpoint symbols."""
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    upto = min(max(line, 1), len(lines))
    method_re = re.compile(
        r"\b(?:public|protected|private)\s+(?:static\s+)?[\w<>\[\], ?]+\s+"
        r"(\w+)\s*\([^;]*$"
    )
    for candidate in reversed(lines[:upto]):
        match = method_re.search(candidate.strip())
        if match:
            return match.group(1)
    return None


def _flow_kind(method: str | None) -> str:
    if not method:
        return "unknown"
    lowered = method.lower()
    if "good" in lowered:
        return "good"
    if "bad" in lowered:
        return "bad"
    return "unknown"


def _source_path(java_root: Path, reported: str) -> Path:
    path = Path(reported.replace("\\", "/"))
    direct = java_root / "src" / path
    if direct.is_file():
        return direct
    return (java_root / "src" / "testcases" /
            "CWE23_Relative_Path_Traversal" / path.name)


def _endpoint_kind(java_root: Path, endpoint: dict) -> str:
    method = _method_from_symbol(endpoint.get("symbol"))
    if method is None:
        method = _method_at_line(
            _source_path(java_root, endpoint.get("path") or ""),
            int(endpoint.get("line") or 1),
        )
    return _flow_kind(method)


def _case_key(path: str) -> tuple[str, str]:
    stem = Path(path).stem
    rest = stem.split("__", 1)[1]
    match = re.search(r"_(\d\d)(?:[a-z]|_.*)?$", rest)
    if not match:
        return rest, "??"
    return rest[:match.start()], match.group(1)


def score(java_root: Path, report: dict) -> dict:
    cases = _manifest_cases(java_root / "manifest.xml")
    file_to_cases: dict[str, set[int]] = defaultdict(set)
    for case_id, files in enumerate(cases):
        for file in files:
            file_to_cases[file].add(case_id)

    bad_cases: set[int] = set()
    good_cases: set[int] = set()
    flow_counts = {"bad-bad": 0, "contains-good": 0, "unknown": 0}
    relevant_findings = 0
    for finding in report.get("findings", []):
        if finding.get("category") != "path-traversal":
            continue
        endpoints = [finding.get("source") or {}, finding.get("sink") or {}]
        case_ids: set[int] = set()
        for endpoint in endpoints:
            case_ids.update(file_to_cases[Path(endpoint.get("path") or "").name])
        if not case_ids:
            continue
        relevant_findings += 1
        kinds = [_endpoint_kind(java_root, endpoint) for endpoint in endpoints]
        if kinds == ["bad", "bad"]:
            flow_counts["bad-bad"] += 1
            bad_cases.update(case_ids)
        elif "good" in kinds:
            flow_counts["contains-good"] += 1
            good_cases.update(case_ids)
        else:
            flow_counts["unknown"] += 1

    tp = len(bad_cases)
    fn = len(cases) - tp
    # Each Juliet testcase exposes a `good()` safe companion.  Multiple good
    # helper flows in the same testcase are one negative unit, matching the
    # testcase-level positive unit above.
    fp = len(good_cases)
    tn = len(cases) - fp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0

    by_source: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cases": 0, "detected": 0})
    by_variant: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cases": 0, "detected": 0})
    for case_id, files in enumerate(cases):
        source, variant = _case_key(files[0])
        by_source[source]["cases"] += 1
        by_variant[variant]["cases"] += 1
        if case_id in bad_cases:
            by_source[source]["detected"] += 1
            by_variant[variant]["detected"] += 1

    invocation = report.get("invocation") or {}
    tool = report.get("tool") or {}
    return {
        "corpus": {
            "name": SUITE_VERSION,
            "sard_suite_id": SUITE_ID,
            "sha256": SUITE_SHA256,
            "cwe": "CWE-23",
        },
        "tool": tool,
        "source_status": invocation.get("status", "unknown"),
        "eligible": invocation.get("status") == "complete",
        "cases": len(cases),
        "safe_companions": len(cases),
        "relevant_findings": relevant_findings,
        "flow_findings": flow_counts,
        "total": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "metrics": {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "fpr": round(fpr, 6),
            "score": round(recall - fpr, 6),
        },
        "by_source": dict(sorted(by_source.items())),
        "by_variant": dict(sorted(by_variant.items())),
        "limitations": [
            "Juliet is generated synthetic code, not a real-repository corpus.",
            "One co-located good() flow per testcase is used as the negative unit.",
            "Findings with unknown endpoint methods are reported but not scored.",
        ],
    }


def _percent(value: float) -> str:
    return f"{value:.1%}"


def render(result: dict) -> str:
    total = result["total"]
    metrics = result["metrics"]
    tool = result.get("tool") or {}
    lines = [
        f"{SUITE_VERSION} / CWE-23 independent holdout",
        f"  tool={tool.get('name', 'unknown')} {tool.get('version', '')} "
        f"status={result['source_status']}",
        f"  cases={result['cases']} safe-companions={result['safe_companions']} "
        f"findings={result['relevant_findings']}",
        f"  TP={total['tp']} FP={total['fp']} FN={total['fn']} TN={total['tn']}",
        f"  precision={_percent(metrics['precision'])} "
        f"recall={_percent(metrics['recall'])} "
        f"FPR={_percent(metrics['fpr'])} score={metrics['score']:+.3f}",
        f"  flow findings: {result['flow_findings']}",
        "",
        "  variant  detected/cases",
    ]
    for variant, counts in result["by_variant"].items():
        lines.append(
            f"  {variant:>7}  {counts['detected']:>3}/{counts['cases']:<3}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("java_root", help="extracted Juliet Java directory")
    parser.add_argument("--report", required=True,
                        help="normalized security-report JSON")
    parser.add_argument("--json", help="write the score as JSON")
    args = parser.parse_args()

    java_root = Path(args.java_root).resolve()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    result = score(java_root, report)
    print(render(result))
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\noutput: {args.json}")
    return 0 if result["eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
