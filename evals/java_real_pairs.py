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
import re
from pathlib import Path, PurePosixPath, PureWindowsPath


SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0", "1.1"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_COMMON_MATCH_KEYS = frozenset({
    "source_path_suffix", "sink_path_suffix",
    "source_label", "sink_label",
    "source_line", "sink_line",
    "source_parameter",
    "source_argument_literals", "sink_argument_literals",
})
_MATCH_KEYS = {
    # v1.0 keeps the historical substring contract explicitly for old manifests.
    "1.0": _COMMON_MATCH_KEYS | {
        "source_symbol_contains", "sink_symbol_contains",
    },
    # v1.1 requires a terminal symbol boundary; near-collisions do not match.
    "1.1": _COMMON_MATCH_KEYS | {
        "source_symbol_suffix", "sink_symbol_suffix",
    },
}


def _ends_with(value: object, suffix: str) -> bool:
    actual = str(value or "").replace("\\", "/").rstrip("/")
    expected = suffix.replace("\\", "/").strip("/")
    return bool(expected and (actual == expected
                              or actual.endswith("/" + expected)))


def _symbol_ends_with(value: object, suffix: str) -> bool:
    actual = str(value or "")
    expected = suffix.strip()
    if not expected:
        return False
    if expected.startswith("."):
        return actual.endswith(expected)
    return actual == expected or actual.endswith("." + expected)


def _is_safe_relative_path(value: object, *, allow_dot: bool = False) -> bool:
    """Return whether *value* is a portable, root-bound relative path."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    normalized = value.strip().replace("\\", "/")
    if normalized == ".":
        return allow_dot
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(value.strip())
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        return False
    return all(part not in {"", ".", ".."} for part in posix.parts)


def _safe_report_path(root: Path, name: str) -> Path:
    """Resolve a manifest report below *root*, rejecting traversal/symlinks."""
    if not _is_safe_relative_path(name):
        raise ValueError("report path must be relative and cannot contain '..'")
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError("reports root must be a directory")
    candidate = (resolved_root / Path(name.replace("\\", "/"))).resolve(
        strict=True)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("report path escapes reports root") from error
    if not candidate.is_file():
        raise ValueError("report path must identify a regular file")
    return candidate


def _normalize_literals(raw: object) -> dict[int, str] | None:
    if not isinstance(raw, (dict, list, tuple)):
        return None
    items = raw.items() if isinstance(raw, dict) else raw
    normalized: dict[int, str] = {}
    try:
        for index, value in items:
            if isinstance(index, bool) or value is None:
                return None
            numeric = int(index)
            if numeric < 0 or numeric in normalized:
                return None
            normalized[numeric] = str(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized


def _argument_literals(endpoint: dict) -> dict[int, str] | None:
    """Return normalized literal arguments, or ``None`` when not reported.

    Normalized reports may serialize an index-to-literal mapping with JSON
    string keys or the extractor's sequence of ``(index, literal)`` pairs.
    Invalid or absent evidence deliberately does not match: the scorer must
    never reconstruct an argument from a line number or source text.
    """
    raw = endpoint.get("argument_literals")
    if raw is None:
        return None
    return _normalize_literals(raw)


def _matches_argument_literals(endpoint: dict, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    actual = _argument_literals(endpoint)
    if actual is None:
        return False
    normalized = _normalize_literals(expected)
    if normalized is None:
        return False
    return all(actual.get(index) == value for index, value in normalized.items())


def _matches_source_parameter(finding: dict, source: dict,
                              expected: object) -> bool:
    """Require every present encoding of source-parameter evidence to agree.

    Report versions have represented this evidence at finding level, on the
    source location, or as the source call's literal argument zero. Conflicting
    encodings are invalid evidence; precedence would let stale metadata hide a
    contradictory literal from the actual source endpoint.
    """
    evidence: list[str] = []
    for value in (finding.get("source_parameter"), source.get("parameter")):
        if value is not None:
            evidence.append(str(value))
    arguments = _argument_literals(source)
    if "argument_literals" in source and arguments is None:
        return False
    if arguments is not None and 0 in arguments:
        evidence.append(arguments[0])
    wanted = str(expected)
    return bool(evidence) and all(value == wanted for value in evidence)


def _validate_match(match: object, schema_version: str) -> dict:
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported schema_version: {schema_version}")
    if not isinstance(match, dict) or not match:
        raise ValueError("case.match must be a non-empty object")
    unknown = sorted(set(match).difference(_MATCH_KEYS[schema_version]))
    if unknown:
        raise ValueError(f"unknown match key(s): {', '.join(unknown)}")
    for key, value in match.items():
        if value is None:
            raise ValueError(f"match.{key} cannot be null")
        if key.endswith(("_line",)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"match.{key} must be a positive integer")
        elif key.endswith("_argument_literals"):
            normalized = _normalize_literals(value)
            if normalized is None or not normalized:
                raise ValueError(f"match.{key} must contain valid literals")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"match.{key} must be a non-empty string")
    return match


def validate_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    version = str(manifest.get("schema_version") or "")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported schema_version: {version or '<missing>'}")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest.cases must be a non-empty array")
    contract = manifest.get("report_contract", {})
    if not isinstance(contract, dict):
        raise ValueError("manifest.report_contract must be an object")
    unknown_contract = set(contract).difference({"require_subject",
                                                 "subject_fields"})
    if unknown_contract:
        raise ValueError("unknown report_contract key(s): "
                         + ", ".join(sorted(unknown_contract)))
    require_subject = contract.get("require_subject", False)
    if not isinstance(require_subject, bool):
        raise ValueError("report_contract.require_subject must be boolean")
    subject_fields = contract.get("subject_fields")
    expected_subject_fields = ["repository", "commit", "scan_subdir"]
    if require_subject and subject_fields != expected_subject_fields:
        raise ValueError(
            "report_contract.subject_fields must explicitly declare "
            "repository, commit and scan_subdir")
    if not require_subject and subject_fields is not None:
        raise ValueError(
            "report_contract.subject_fields requires require_subject=true")
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("every manifest case must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("case ids must be non-empty and unique")
        ids.add(case_id)
        if (not isinstance(case.get("expected_category"), str)
                or not case["expected_category"].strip()):
            raise ValueError(f"case {case_id}: expected_category is required")
        _validate_match(case.get("match"), version)
        for key in ("vulnerable_report", "fixed_report"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"case {case_id}: {key} is required")
            if not _is_safe_relative_path(case[key]):
                raise ValueError(
                    f"case {case_id}: {key} must stay below reports root")
        if require_subject:
            for key in ("repository", "vulnerable_commit", "fixed_commit",
                        "scan_subdir"):
                if (not isinstance(case.get(key), str)
                        or not case[key].strip()):
                    raise ValueError(
                        f"case {case_id}: {key} required by report_contract")
            if not _is_safe_relative_path(case["scan_subdir"], allow_dot=True):
                raise ValueError(
                    f"case {case_id}: scan_subdir must be repository-relative")
    return manifest


def finding_matches(finding: dict, case: dict,
                    schema_version: str = "1.0") -> bool:
    if not isinstance(finding, dict):
        return False
    try:
        match = _validate_match(case.get("match"), schema_version)
    except (AttributeError, ValueError):
        return False
    if finding.get("category") != case.get("expected_category"):
        return False
    source = finding.get("source") or {}
    sink = finding.get("sink") or {}
    if not isinstance(source, dict) or not isinstance(sink, dict):
        return False
    checks = (
        ("source_path_suffix", _ends_with(source.get("path"), match.get("source_path_suffix", ""))),
        ("sink_path_suffix", _ends_with(sink.get("path"), match.get("sink_path_suffix", ""))),
        ("source_label", source.get("label") == match.get("source_label")),
        ("sink_label", sink.get("label") == match.get("sink_label")),
        ("source_symbol_contains", match.get("source_symbol_contains", "")
         in str(source.get("symbol") or "")),
        ("sink_symbol_contains", match.get("sink_symbol_contains", "")
         in str(sink.get("symbol") or "")),
        ("source_symbol_suffix", _symbol_ends_with(
            source.get("symbol"), match.get("source_symbol_suffix", ""))),
        ("sink_symbol_suffix", _symbol_ends_with(
            sink.get("symbol"), match.get("sink_symbol_suffix", ""))),
        ("source_line", source.get("line") == match.get("source_line")),
        ("sink_line", sink.get("line") == match.get("sink_line")),
        ("source_parameter", _matches_source_parameter(
            finding, source, match.get("source_parameter"))),
        ("source_argument_literals", _matches_argument_literals(
            source, match.get("source_argument_literals"))),
        ("sink_argument_literals", _matches_argument_literals(
            sink, match.get("sink_argument_literals"))),
    )
    return all(ok for key, ok in checks if key in match)


def _no_errors(value: object) -> bool:
    if isinstance(value, bool):
        return False
    return value == 0 or value == [] or value == {}


def _target_subject_errors(report: dict) -> list[str]:
    subject = report.get("subject")
    if not isinstance(subject, dict):
        return ["subject must be an object"]
    errors = []
    target = report.get("target")
    if not isinstance(target, dict):
        return ["target must be an object when subject is present"]
    if target.get("git_commit") != subject.get("commit"):
        errors.append("target.git_commit must equal subject.commit")
    target_remote = str(target.get("git_remote") or "").rstrip("/")
    subject_remote = str(subject.get("repository") or "").rstrip("/")
    if target_remote != subject_remote:
        errors.append("target.git_remote must equal subject.repository")
    if target.get("git_dirty") is not False:
        errors.append("target.git_dirty must be false for competitive evidence")
    return errors


def _subject_errors(report: dict, case: dict, side: str) -> list[str]:
    subject = report.get("subject")
    if not isinstance(subject, dict):
        return ["subject must be an object"]
    expected = {
        "repository": case["repository"],
        "commit": case[f"{side}_commit"],
        "scan_subdir": case["scan_subdir"],
    }
    errors = []
    for key, value in expected.items():
        if key not in subject:
            errors.append(f"subject.{key} is required")
            continue
        actual = subject.get(key)
        if key == "scan_subdir":
            actual = str(actual or "").replace("\\", "/").rstrip("/") or "."
            value = value.replace("\\", "/").rstrip("/") or "."
        if actual != value:
            errors.append(
                f"subject.{key} expected {value!r}, got {actual!r}")
    errors.extend(_target_subject_errors(report))
    return errors


def _engine_identity(report: object) -> dict | None:
    if not isinstance(report, dict):
        return None
    analysis = report.get("analysis")
    engine = analysis.get("engine") if isinstance(analysis, dict) else None
    if not isinstance(engine, dict):
        return None
    commit = engine.get("git_commit")
    dirty = engine.get("git_dirty")
    tree_hash = engine.get("source_tree_sha256")
    if (not isinstance(commit, str) or not _GIT_COMMIT_RE.fullmatch(commit)
            or not isinstance(dirty, bool)):
        return None
    if (tree_hash is not None
            and (not isinstance(tree_hash, str)
                 or not _SHA256_RE.fullmatch(tree_hash))):
        return None
    if dirty and tree_hash is None:
        return None
    return {
        "git_commit": commit,
        "git_dirty": dirty,
        "source_tree_sha256": tree_hash if isinstance(tree_hash, str) else None,
    }


def _engine_errors(report: dict) -> list[str]:
    analysis = report.get("analysis")
    engine = analysis.get("engine") if isinstance(analysis, dict) else None
    if not isinstance(engine, dict):
        return []
    provenance_keys = {"git_commit", "git_dirty", "source_tree_sha256"}
    if not provenance_keys.intersection(engine):
        return []  # Legacy reports did not publish engine provenance.
    return ([] if _engine_identity(report) is not None else
            ["analysis.engine provenance is incomplete or invalid"])


def _report_errors(report: object, case: dict, side: str,
                   require_subject: bool) -> list[str]:
    if not isinstance(report, dict):
        return ["report must be an object"]
    errors = []
    if report.get("status") != "complete":
        errors.append("status must be 'complete'")
    invocation = report.get("invocation")
    if invocation is not None:
        if not isinstance(invocation, dict):
            errors.append("invocation must be an object")
        else:
            if invocation.get("status") != "complete":
                errors.append("invocation.status must be 'complete'")
            exit_code = invocation.get("exit_code")
            if isinstance(exit_code, bool) or exit_code != 0:
                errors.append("invocation.exit_code must be zero")
    if report.get("truncated") is not False:
        errors.append("truncated must be false")
    if "errors" not in report or not _no_errors(report.get("errors")):
        errors.append("errors must be present and empty/zero")
    findings = report.get("findings")
    if not isinstance(findings, list) or not all(
            isinstance(finding, dict) for finding in findings):
        errors.append("findings must be an array of objects")
    if require_subject:
        errors.extend(_subject_errors(report, case, side))
    elif "subject" in report:
        errors.extend(_target_subject_errors(report))
    errors.extend(_engine_errors(report))
    return errors


def score_case(case: dict, reports: Path, schema_version: str = "1.0",
               require_subject: bool = False) -> dict:
    def load(name: str) -> tuple[object, str | None]:
        try:
            raw = _safe_report_path(reports, name).read_text(encoding="utf-8")
            return json.loads(raw), None
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            return None, f"cannot load {name!r}: {type(error).__name__}: {error}"

    vulnerable, vulnerable_load_error = load(case["vulnerable_report"])
    fixed, fixed_load_error = load(case["fixed_report"])
    evidence_errors = [error for error in (
        (f"vulnerable: {vulnerable_load_error}"
         if vulnerable_load_error else None),
        f"fixed: {fixed_load_error}" if fixed_load_error else None,
    ) if error]
    evidence_errors.extend([
        *(f"vulnerable: {error}" for error in _report_errors(
            vulnerable, case, "vulnerable", require_subject)),
        *(f"fixed: {error}" for error in _report_errors(
            fixed, case, "fixed", require_subject)),
    ])
    vulnerable_findings = (vulnerable.get("findings", [])
                            if isinstance(vulnerable, dict) else [])
    fixed_findings = (fixed.get("findings", [])
                      if isinstance(fixed, dict) else [])
    if evidence_errors:
        return {
            "id": case["id"],
            "cve": case.get("cve"),
            "outcome": "invalid-evidence",
            "evidence_errors": evidence_errors,
            "vulnerable_matches": 0,
            "fixed_matches": 0,
            "vulnerable_total_findings": (
                len(vulnerable_findings)
                if isinstance(vulnerable_findings, list) else 0),
            "fixed_total_findings": (
                len(fixed_findings) if isinstance(fixed_findings, list) else 0),
            "vulnerable_duration_s": None,
            "fixed_duration_s": None,
        }
    vulnerable_matches = [
        finding for finding in vulnerable_findings
        if finding_matches(finding, case, schema_version)
    ]
    fixed_matches = [
        finding for finding in fixed_findings
        if finding_matches(finding, case, schema_version)
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
        "vulnerable_duration_s": (
            vulnerable.get("invocation", {}).get("duration_s")
            if isinstance(vulnerable.get("invocation"), dict) else None),
        "fixed_duration_s": (
            fixed.get("invocation", {}).get("duration_s")
            if isinstance(fixed.get("invocation"), dict) else None),
    }


def score_manifest(manifest: dict, reports: Path) -> dict:
    validate_manifest(manifest)
    schema_version = str(manifest["schema_version"])
    require_subject = bool(
        (manifest.get("report_contract") or {}).get("require_subject", False))
    cases = [score_case(case, reports, schema_version, require_subject)
             for case in manifest["cases"]]
    identities: list[dict | None] = []
    for case in manifest["cases"]:
        for key in ("vulnerable_report", "fixed_report"):
            try:
                report = json.loads(
                    _safe_report_path(reports, case[key]).read_text(
                        encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                identities.append(None)
            else:
                identities.append(_engine_identity(report))
    claimed = [identity for identity in identities if identity is not None]
    if not claimed:
        engine_provenance = {"status": "unverified"}
    elif len(claimed) != len(identities) or any(
            identity != claimed[0] for identity in claimed[1:]):
        engine_provenance = {"status": "conflicting-or-incomplete"}
    else:
        engine_provenance = {"status": "verified", **claimed[0]}
    expected_commit = manifest.get("engine_commit")
    if (engine_provenance["status"] == "verified" and expected_commit
            and engine_provenance["git_commit"] != expected_commit):
        engine_provenance["status"] = "manifest-mismatch"
    if engine_provenance["status"] in {
            "conflicting-or-incomplete", "manifest-mismatch"}:
        message = "engine provenance is inconsistent with the report set or manifest"
        for case in cases:
            prior = list(case.get("evidence_errors") or [])
            case["outcome"] = "invalid-evidence"
            case["evidence_errors"] = [*prior, message]
            case["vulnerable_matches"] = 0
            case["fixed_matches"] = 0
    outcomes: dict[str, int] = {}
    for case in cases:
        outcomes[case["outcome"]] = outcomes.get(case["outcome"], 0) + 1
    verified_commit = (engine_provenance.get("git_commit")
                       if engine_provenance["status"] == "verified" else None)
    return {
        "schema_version": manifest.get("schema_version"),
        "engine_commit": verified_commit,
        "manifest_engine_commit": manifest.get("engine_commit"),
        "engine_provenance": engine_provenance,
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
    scored = score_manifest(manifest, args.reports)
    print(json.dumps(scored, indent=2))
    return 2 if scored["outcomes"].get("invalid-evidence") else 0


if __name__ == "__main__":
    raise SystemExit(main())
