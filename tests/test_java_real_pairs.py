from evals.java_real_pairs import finding_matches, score_manifest


def _finding(category="path-traversal", source="src/Input.java", sink="src/Sink.java"):
    return {
        "category": category,
        "source": {
            "path": source,
            "label": "getParameter()",
            "symbol": "app.Input.handle",
        },
        "sink": {"path": sink, "label": "File", "symbol": "app.Sink.open"},
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


def test_score_preserves_a_miss_instead_of_dropping_case(tmp_path):
    (tmp_path / "vuln.json").write_text('{"findings": []}', encoding="utf-8")
    (tmp_path / "fixed.json").write_text('{"findings": []}', encoding="utf-8")
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
