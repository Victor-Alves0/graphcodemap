import json
from pathlib import Path

import pytest

from evals.java_real_pairs import (
    finding_matches,
    score_manifest,
    validate_manifest,
)


_MANIFEST = Path(__file__).parents[1] / "evals" / "java-real-pairs.json"


def _finding(category="path-traversal", source="src/Input.java", sink="src/Sink.java"):
    return {
        "category": category,
        "source": {
            "path": source,
            "line": 10,
            "label": "getParameter()",
            "symbol": "app.Input.handle",
            "argument_literals": {"0": "path"},
        },
        "sink": {
            "path": sink,
            "line": 20,
            "label": "File",
            "symbol": "app.Sink.open",
        },
    }


def test_oracle_requires_category_and_requested_endpoints():
    case = {
        "expected_category": "path-traversal",
        "match": {
            "source_path_suffix": "Input.java",
            "source_label": "getParameter()",
            "source_symbol_contains": ".Input.handle",
            "sink_path_suffix": "Sink.java",
            "sink_label": "File",
        },
    }
    assert finding_matches(_finding(), case)
    assert not finding_matches(_finding(category="xss"), case)
    assert not finding_matches(_finding(sink="src/Other.java"), case)


def test_oracle_matches_reported_line_and_literal_argument_strictly():
    case = {
        "expected_category": "path-traversal",
        "match": {
            "source_line": 10,
            "source_argument_literals": {"0": "path"},
            "sink_line": 20,
        },
    }
    assert finding_matches(_finding(), case)

    wrong_line = _finding()
    wrong_line["source"]["line"] = 11
    assert not finding_matches(wrong_line, case)

    wrong_literal = _finding()
    wrong_literal["source"]["argument_literals"] = {"0": "module"}
    assert not finding_matches(wrong_literal, case)


def test_oracle_does_not_guess_an_argument_literal_when_report_omits_it():
    case = {
        "expected_category": "path-traversal",
        "match": {"source_argument_literals": {"0": "lang"}},
    }
    finding = _finding()
    finding["source"].pop("argument_literals")
    assert not finding_matches(finding, case)


def test_oracle_accepts_extractor_pair_encoding_for_argument_literals():
    case = {
        "expected_category": "path-traversal",
        "match": {"source_argument_literals": {"0": "lang"}},
    }
    finding = _finding()
    finding["source"]["argument_literals"] = [[0, "lang"], [1, "fallback"]]
    assert finding_matches(finding, case)


def test_source_parameter_accepts_only_explicit_report_encodings():
    case = {
        "expected_category": "path-traversal",
        "match": {"source_parameter": "lang"},
    }

    finding_level = _finding()
    finding_level["source_parameter"] = "lang"
    finding_level["source"].pop("argument_literals")
    assert finding_matches(finding_level, case)

    source_level = _finding()
    source_level["source"]["parameter"] = "lang"
    source_level["source"].pop("argument_literals")
    assert finding_matches(source_level, case)

    argument_level = _finding()
    argument_level["source"]["argument_literals"] = [[0, "lang"]]
    assert finding_matches(argument_level, case)

    missing = _finding()
    missing["source"].pop("argument_literals")
    assert not finding_matches(missing, case)

    wrong = _finding()
    wrong["source_parameter"] = "project"
    assert not finding_matches(wrong, case)

    conflicting = _finding()
    conflicting["source_parameter"] = "lang"
    conflicting["source"]["argument_literals"] = {"0": "project"}
    assert not finding_matches(conflicting, case)

    malformed = _finding()
    malformed["source_parameter"] = "lang"
    malformed["source"]["argument_literals"] = [[0, "lang"], [0, "lang"]]
    assert not finding_matches(malformed, case)


def test_openrefine_oracle_excludes_provisional_file_constructors():
    case = {
        "expected_category": "path-traversal",
        "match": {
            "source_path_suffix": "LoadLanguageCommand.java",
            "source_label": "getParameterValues()",
            "source_symbol_suffix": ".LoadLanguageCommand.doPost",
            "source_line": 83,
            "source_parameter": "lang",
            "sink_path_suffix": "LoadLanguageCommand.java",
            "sink_label": "FileInputStream",
            "sink_symbol_suffix": ".LoadLanguageCommand.loadLanguage",
        },
    }
    finding = _finding(
        source="main/src/com/google/refine/commands/lang/LoadLanguageCommand.java",
        sink="main/src/com/google/refine/commands/lang/LoadLanguageCommand.java",
    )
    finding["source"].update({
        "line": 83,
        "label": "getParameterValues()",
        "symbol": "com.google.refine.commands.lang.LoadLanguageCommand.doPost",
        "argument_literals": {"0": "lang"},
    })
    finding["source_parameter"] = "lang"
    finding["sink"].update({
        "label": "FileInputStream",
        "symbol": "com.google.refine.commands.lang.LoadLanguageCommand.loadLanguage",
    })
    assert finding_matches(finding, case, "1.1")

    mutations = (
        ("source", "label", "getParameter()"),
        ("source", "symbol", "example.Other.doPost"),
        ("source", "line", 84),
        ("sink", "label", "File"),
        ("sink", "symbol", "example.Other.loadLanguage"),
    )
    for endpoint, key, value in mutations:
        mismatch = _finding(
            source="main/src/com/google/refine/commands/lang/LoadLanguageCommand.java",
            sink="main/src/com/google/refine/commands/lang/LoadLanguageCommand.java",
        )
        mismatch.update(finding)
        mismatch["source"] = finding["source"].copy()
        mismatch["sink"] = finding["sink"].copy()
        mismatch[endpoint][key] = value
        assert not finding_matches(mismatch, case, "1.1")

    missing_parameter = finding.copy()
    missing_parameter.pop("source_parameter")
    missing_parameter["source"] = finding["source"].copy()
    missing_parameter["source"].pop("argument_literals", None)
    assert not finding_matches(missing_parameter, case, "1.1")


def test_manifest_pins_openrefine_to_the_patch_derived_flow():
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    case = next(
        case for case in manifest["cases"]
        if case["id"] == "openrefine-cve-2024-49760"
    )
    assert case["match"] == {
        "source_path_suffix": (
            "main/src/com/google/refine/commands/lang/LoadLanguageCommand.java"
        ),
        "source_label": "getParameterValues()",
        "source_symbol_suffix": ".LoadLanguageCommand.doPost",
        "source_line": 83,
        "source_parameter": "lang",
        "sink_path_suffix": (
            "main/src/com/google/refine/commands/lang/LoadLanguageCommand.java"
        ),
        "sink_label": "FileInputStream",
        "sink_symbol_suffix": ".LoadLanguageCommand.loadLanguage",
    }


def test_new_oracle_constraints_do_not_change_other_pair_matching():
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    legacy_cases = [
        case for case in manifest["cases"]
        if case["id"] != "openrefine-cve-2024-49760"
    ]
    for case in legacy_cases:
        match = case["match"]
        finding = {
            "category": case["expected_category"],
            "source": {
                "path": match["source_path_suffix"],
                "label": match["source_label"],
                "symbol": f"example{match.get('source_symbol_suffix', '')}",
            },
            "sink": {
                "path": match["sink_path_suffix"],
                "label": match["sink_label"],
                "symbol": f"example{match.get('sink_symbol_suffix', '')}",
            },
        }
        assert finding_matches(
            finding, case, manifest["schema_version"]), case["id"]


def test_score_preserves_a_miss_instead_of_dropping_case(tmp_path):
    empty = {"status": "complete", "truncated": False,
             "errors": 0, "findings": []}
    (tmp_path / "vuln.json").write_text(json.dumps(empty), encoding="utf-8")
    (tmp_path / "fixed.json").write_text(json.dumps(empty), encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "engine_commit": "abc",
        "cases": [{
            "id": "honest-miss",
            "expected_category": "path-traversal",
            "match": {"sink_path_suffix": "Affected.java"},
            "vulnerable_report": "vuln.json",
            "fixed_report": "fixed.json",
        }],
    }
    scored = score_manifest(manifest, tmp_path)
    assert scored["outcomes"] == {"missed": 1}
    assert scored["cases"][0]["outcome"] == "missed"


def test_manifest_rejects_unsupported_schema_unknown_keys_and_nulls():
    base = {
        "schema_version": "1.1",
        "cases": [{
            "id": "strict", "expected_category": "path-traversal",
            "match": {"sink_label": "File"},
            "vulnerable_report": "v.json", "fixed_report": "f.json",
        }],
    }
    validate_manifest(base)

    unsupported = {**base, "schema_version": "999"}
    with pytest.raises(ValueError, match="unsupported schema_version"):
        validate_manifest(unsupported)

    typo = json.loads(json.dumps(base))
    typo["cases"][0]["match"] = {"sink_lable": "File"}
    with pytest.raises(ValueError, match="unknown match key"):
        validate_manifest(typo)
    assert not finding_matches(_finding(), typo["cases"][0], "1.1")

    null_value = json.loads(json.dumps(base))
    null_value["cases"][0]["match"] = {"sink_label": None}
    with pytest.raises(ValueError, match="cannot be null"):
        validate_manifest(null_value)


def test_schema_11_path_and_symbol_suffix_require_boundaries():
    case = {
        "expected_category": "path-traversal",
        "match": {
            "source_path_suffix": "main/src/A.java",
            "source_symbol_suffix": ".Controller.doPost",
        },
    }
    finding = _finding(source="repo/main/src/A.java")
    finding["source"]["symbol"] = "app.Controller.doPost"
    assert finding_matches(finding, case, "1.1")

    prefixed_path = _finding(source="repo/evilmain/src/A.java")
    prefixed_path["source"]["symbol"] = "app.Controller.doPost"
    assert not finding_matches(prefixed_path, case, "1.1")

    near_symbol = _finding(source="repo/main/src/A.java")
    near_symbol["source"]["symbol"] = "app.Controller.doPostHelper"
    assert not finding_matches(near_symbol, case, "1.1")


def test_schema_10_contains_is_explicit_legacy_compatibility():
    case = {
        "expected_category": "path-traversal",
        "match": {"source_symbol_contains": ".Controller.doPost"},
    }
    finding = _finding()
    finding["source"]["symbol"] = "app.Controller.doPostHelper"
    assert finding_matches(finding, case, "1.0")
    assert not finding_matches(finding, case, "1.1")


def _report(findings, *, status="complete", truncated=False, errors=0,
            subject=None, invocation=None, target=None, analysis=None):
    report = {"status": status, "truncated": truncated,
              "errors": errors, "findings": findings}
    if invocation is not None:
        report["invocation"] = invocation
    if subject is not None:
        report["subject"] = subject
        if target is None:
            target = {
                "git_remote": subject.get("repository"),
                "git_commit": subject.get("commit"),
                "git_dirty": False,
            }
    if target is not None:
        report["target"] = target
    if analysis is not None:
        report["analysis"] = analysis
    return report


def _write_pair(tmp_path, vulnerable, fixed):
    (tmp_path / "v.json").write_text(json.dumps(vulnerable), encoding="utf-8")
    (tmp_path / "f.json").write_text(json.dumps(fixed), encoding="utf-8")


def _scored_manifest(*, require_subject=False):
    manifest = {
        "schema_version": "1.1",
        "cases": [{
            "id": "pair", "expected_category": "path-traversal",
            "match": {"sink_label": "File"},
            "vulnerable_report": "v.json", "fixed_report": "f.json",
        }],
    }
    if require_subject:
        manifest["report_contract"] = {
            "require_subject": True,
            "subject_fields": ["repository", "commit", "scan_subdir"],
        }
        manifest["cases"][0].update({
            "repository": "https://example.test/repo.git",
            "vulnerable_commit": "a" * 40,
            "fixed_commit": "b" * 40,
            "scan_subdir": ".",
        })
    return manifest


@pytest.mark.parametrize("fixed", [
    _report([], status="partial"),
    _report([], truncated=True),
    _report([], errors=1),
    _report([], errors=None),
    {"findings": []},
])
def test_incomplete_fixed_report_is_invalid_evidence_not_clearance(
        tmp_path, fixed):
    _write_pair(tmp_path, _report([_finding()]), fixed)
    scored = score_manifest(_scored_manifest(), tmp_path)
    case = scored["cases"][0]
    assert case["outcome"] == "invalid-evidence"
    assert scored["outcomes"] == {"invalid-evidence": 1}
    assert any(error.startswith("fixed:") for error in case["evidence_errors"])


@pytest.mark.parametrize("invocation", [
    {"status": "partial", "exit_code": 0},
    {"status": "complete", "exit_code": 1},
    {"status": "complete", "exit_code": None},
])
def test_invocation_failure_invalidates_complete_top_level_report(
        tmp_path, invocation):
    _write_pair(
        tmp_path,
        _report([_finding()], invocation={"status": "complete", "exit_code": 0}),
        _report([], invocation=invocation),
    )
    case = score_manifest(_scored_manifest(), tmp_path)["cases"][0]
    assert case["outcome"] == "invalid-evidence"
    assert any("fixed: invocation." in error for error in case["evidence_errors"])


def test_unreadable_or_malformed_report_is_invalid_evidence(tmp_path):
    (tmp_path / "v.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "f.json").write_text(
        json.dumps(_report([])), encoding="utf-8")
    scored = score_manifest(_scored_manifest(), tmp_path)["cases"][0]
    assert scored["outcome"] == "invalid-evidence"
    assert any("JSONDecodeError" in error for error in scored["evidence_errors"])


def test_required_subject_must_be_present_and_match_each_revision(tmp_path):
    manifest = _scored_manifest(require_subject=True)
    _write_pair(tmp_path, _report([_finding()]), _report([]))
    missing = score_manifest(manifest, tmp_path)["cases"][0]
    assert missing["outcome"] == "invalid-evidence"
    assert any("subject must be an object" in error
               for error in missing["evidence_errors"])

    case = manifest["cases"][0]
    common = {"repository": case["repository"], "scan_subdir": "."}
    vulnerable_subject = {**common, "commit": case["vulnerable_commit"]}
    missing_subdir = {"repository": case["repository"],
                      "commit": case["fixed_commit"]}
    _write_pair(tmp_path, _report([_finding()], subject=vulnerable_subject),
                _report([], subject=missing_subdir))
    absent = score_manifest(manifest, tmp_path)["cases"][0]
    assert absent["outcome"] == "invalid-evidence"
    assert any("fixed: subject.scan_subdir is required" in error
               for error in absent["evidence_errors"])

    wrong_fixed_subject = {**common, "commit": case["vulnerable_commit"]}
    _write_pair(tmp_path, _report([_finding()], subject=vulnerable_subject),
                _report([], subject=wrong_fixed_subject))
    wrong = score_manifest(manifest, tmp_path)["cases"][0]
    assert wrong["outcome"] == "invalid-evidence"
    assert any("fixed: subject.commit" in error
               for error in wrong["evidence_errors"])

    fixed_subject = {**common, "commit": case["fixed_commit"]}
    _write_pair(tmp_path, _report([_finding()], subject=vulnerable_subject),
                _report([], subject=fixed_subject))
    valid = score_manifest(manifest, tmp_path)["cases"][0]
    assert valid["outcome"] == "detected-and-cleared"


@pytest.mark.parametrize(("field", "value", "message"), [
    ("git_commit", "c" * 40, "target.git_commit"),
    ("git_remote", "https://example.test/other.git", "target.git_remote"),
    ("git_dirty", True, "target.git_dirty"),
])
def test_required_subject_rejects_target_mismatch_or_dirty_tree(
        tmp_path, field, value, message):
    manifest = _scored_manifest(require_subject=True)
    case = manifest["cases"][0]
    common = {"repository": case["repository"], "scan_subdir": "."}
    vulnerable_subject = {**common, "commit": case["vulnerable_commit"]}
    fixed_subject = {**common, "commit": case["fixed_commit"]}
    fixed = _report([], subject=fixed_subject)
    fixed["target"][field] = value
    _write_pair(tmp_path, _report([_finding()], subject=vulnerable_subject), fixed)
    scored = score_manifest(manifest, tmp_path)["cases"][0]
    assert scored["outcome"] == "invalid-evidence"
    assert any(message in error for error in scored["evidence_errors"])


def test_present_subject_is_cross_checked_even_when_manifest_does_not_require_it(
        tmp_path):
    subject = {
        "repository": "https://example.test/repo.git",
        "commit": "a" * 40,
        "scan_subdir": ".",
    }
    vulnerable = _report([_finding()], subject=subject)
    fixed = _report([], subject=subject)
    fixed["target"]["git_commit"] = "b" * 40
    _write_pair(tmp_path, vulnerable, fixed)
    scored = score_manifest(_scored_manifest(), tmp_path)["cases"][0]
    assert scored["outcome"] == "invalid-evidence"
    assert any("target.git_commit" in error for error in scored["evidence_errors"])


def test_manifest_report_paths_cannot_escape_reports_root(tmp_path):
    manifest = _scored_manifest()
    for unsafe in ("../v.json", str((tmp_path / "v.json").resolve()),
                   "nested/../v.json"):
        candidate = json.loads(json.dumps(manifest))
        candidate["cases"][0]["vulnerable_report"] = unsafe
        with pytest.raises(ValueError, match="reports root"):
            validate_manifest(candidate)


def test_report_symlink_outside_root_is_invalid_evidence(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text(json.dumps(_report([_finding()])), encoding="utf-8")
    try:
        (tmp_path / "v.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    (tmp_path / "f.json").write_text(json.dumps(_report([])), encoding="utf-8")
    scored = score_manifest(_scored_manifest(), tmp_path)["cases"][0]
    assert scored["outcome"] == "invalid-evidence"
    assert any("escapes reports root" in error
               for error in scored["evidence_errors"])


def test_engine_commit_is_reported_only_from_consistent_report_proof(tmp_path):
    manifest = _scored_manifest()
    manifest["engine_commit"] = "manifest-only"
    _write_pair(tmp_path, _report([_finding()]), _report([]))
    unverified = score_manifest(manifest, tmp_path)
    assert unverified["engine_commit"] is None
    assert unverified["manifest_engine_commit"] == "manifest-only"
    assert unverified["engine_provenance"] == {"status": "unverified"}

    identity = {
        "git_commit": "a" * 40,
        "git_dirty": True,
        "source_tree_sha256": "b" * 64,
    }
    manifest["engine_commit"] = identity["git_commit"]
    analysis = {"engine": identity}
    _write_pair(
        tmp_path,
        _report([_finding()], analysis=analysis),
        _report([], analysis=analysis),
    )
    verified = score_manifest(manifest, tmp_path)
    assert verified["engine_commit"] == "a" * 40
    assert verified["engine_provenance"] == {"status": "verified", **identity}

    conflicting = {"engine": {**identity, "source_tree_sha256": "c" * 64}}
    _write_pair(
        tmp_path,
        _report([_finding()], analysis=analysis),
        _report([], analysis=conflicting),
    )
    invalid = score_manifest(manifest, tmp_path)
    assert invalid["outcomes"] == {"invalid-evidence": 1}
    assert invalid["engine_provenance"]["status"] == "conflicting-or-incomplete"


def test_published_manifest_is_strict_and_all_three_oracles_still_match():
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    assert manifest["report_contract"] == {
        "require_subject": True,
        "subject_fields": ["repository", "commit", "scan_subdir"],
    }
    assert len(manifest["cases"]) == 3
    for case in manifest["cases"]:
        match = case["match"]
        finding = {
            "category": case["expected_category"],
            "source": {
                "path": match.get("source_path_suffix"),
                "line": match.get("source_line"),
                "label": match.get("source_label"),
                "symbol": f"pkg{match.get('source_symbol_suffix', '')}",
                "argument_literals": {"0": match.get("source_parameter")},
            },
            "sink": {
                "path": match.get("sink_path_suffix"),
                "line": match.get("sink_line"),
                "label": match.get("sink_label"),
                "symbol": f"pkg{match.get('sink_symbol_suffix', '')}",
            },
        }
        finding["source_parameter"] = match.get("source_parameter")
        assert finding_matches(finding, case, "1.1"), case["id"]
