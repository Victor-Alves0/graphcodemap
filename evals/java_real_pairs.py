"""Score vulnerable/fixed Java reports against patch-derived oracles.

The corpus check is intentionally narrower than a benchmark score: an advisory
identifies one vulnerable flow, so a hit must agree on category and on the
source/sink constraints recorded in ``java-real-pairs.json``.  Merely finding
some other issue in the repository does not count.

Usage:
    python evals/java_real_pairs.py [--manifest FILE] --reports DIR
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _ends_with(value: object, suffix: str) -> bool:
    return str(value or "").replace("\\", "/").endswith(suffix.replace("\\", "/"))


def finding_matches(finding: dict, case: dict) -> bool:
    if finding.get("category") != case["expected_category"]:
        return False
    match = case.get("match", {})
    source = finding.get("source") or {}
    sink = finding.get("sink") or {}
    checks = (
        ("source_path_suffix", _ends_with(source.get("path"), match.get("source_path_suffix", ""))),
        ("sink_path_suffix", _ends_with(sink.get("path"), match.get("sink_path_suffix", ""))),
        ("source_label", source.get("label") == match.get("source_label")),
        ("sink_label", sink.get("label") == match.get("sink_label")),
        ("source_symbol_contains", match.get("source_symbol_contains", "")
         in str(source.get("symbol") or "")),
        ("sink_symbol_contains", match.get("sink_symbol_contains", "")
         in str(sink.get("symbol") or "")),
    )
    return all(ok for key, ok in checks if key in match)


def score_case(case: dict, reports: Path) -> dict:
    def load(name: str) -> dict:
        return json.loads((reports / name).read_text(encoding="utf-8"))

    vulnerable = load(case["vulnerable_report"])
    fixed = load(case["fixed_report"])
    vulnerable_matches = [
        finding for finding in vulnerable.get("findings", [])
        if finding_matches(finding, case)
    ]
    fixed_matches = [
        finding for finding in fixed.get("findings", [])
        if finding_matches(finding, case)
    ]
    vulnerable_hit = bool(vulnerable_matches)
    fixed_hit = bool(fixed_matches)
    if vulnerable_hit and not fixed_hit:
        outcome = "detected-and-cleared"
    elif vulnerable_hit and fixed_hit:
        outcome = "detected-but-not-cleared"
    elif not vulnerable_hit and fixed_hit:
        outcome = "fixed-only-anomaly"
    else:
        outcome = "missed"
    return {
        "id": case["id"],
        "cve": case.get("cve"),
        "outcome": outcome,
        "vulnerable_matches": len(vulnerable_matches),
        "fixed_matches": len(fixed_matches),
        "vulnerable_total_findings": len(vulnerable.get("findings", [])),
        "fixed_total_findings": len(fixed.get("findings", [])),
        "vulnerable_duration_s": (vulnerable.get("invocation") or {}).get("duration_s"),
        "fixed_duration_s": (fixed.get("invocation") or {}).get("duration_s"),
    }


def score_manifest(manifest: dict, reports: Path) -> dict:
    cases = [score_case(case, reports) for case in manifest["cases"]]
    outcomes: dict[str, int] = {}
    for case in cases:
        outcomes[case["outcome"]] = outcomes.get(case["outcome"], 0) + 1
    return {
        "schema_version": manifest.get("schema_version"),
        "engine_commit": manifest.get("engine_commit"),
        "outcomes": outcomes,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("java-real-pairs.json"),
    )
    parser.add_argument("--reports", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    print(json.dumps(score_manifest(manifest, args.reports), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
