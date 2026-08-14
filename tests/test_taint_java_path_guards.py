"""Relational Java path-containment guards, including adversarial lookalikes."""

from codegraph import CodeGraph


def _path_findings(tmp_path, body: str) -> list[dict]:
    (tmp_path / "App.java").write_text(
        ("""
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        java.nio.file.Path base) throws Exception {
        """ + body + """
            }
        }
        """).strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        return [finding for finding in data["findings"]
                if finding["sink"]["callee"] in {"File", "FileInputStream"}]
    finally:
        graph.close()


def _has_path_finding(tmp_path, body: str) -> bool:
    return any(finding["sink"]["callee"] == "FileInputStream"
               for finding in _path_findings(tmp_path, body))


def test_unguarded_path_remains_vulnerable(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        new java.io.FileInputStream(candidate.toFile());
    """)


def test_normalized_path_guard_clears_same_candidate(tmp_path):
    assert not _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        if (!candidate.normalize().toAbsolutePath().startsWith(
                base.normalize().toAbsolutePath())) {
            return;
        }
        new java.io.FileInputStream(candidate.toFile());
    """)


def test_canonical_guard_with_separator_clears_same_candidate(tmp_path):
    assert not _path_findings(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File baseFile = base.toFile();
        java.io.File candidate = new java.io.File(baseFile, raw);
        if (!candidate.getCanonicalPath().startsWith(
                baseFile.getCanonicalPath() + java.io.File.separator)) {
            throw new SecurityException();
        }
        new java.io.FileInputStream(candidate);
    """)


def test_canonical_guard_clears_constructor_in_trusted_base_ternary(tmp_path):
    assert not _path_findings(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File baseFile = base.toFile();
        java.io.File candidate = raw != null
                ? new java.io.File(baseFile, raw)
                : baseFile;
        if (!candidate.getCanonicalPath().startsWith(
                baseFile.getCanonicalPath() + java.io.File.separator)) {
            return;
        }
    """)


def test_guard_does_not_retract_another_file_constructor_on_same_line(tmp_path):
    findings = _path_findings(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File escaped = new java.io.File(raw); java.io.File candidate = new java.io.File(base.toFile(), raw);
        if (!candidate.getCanonicalPath().startsWith(
                base.toFile().getCanonicalPath() + java.io.File.separator)) {
            return;
        }
    """)
    assert any(finding["sink"]["callee"] == "File" for finding in findings)


def test_use_before_guard_keeps_file_constructor_surrogate(tmp_path):
    findings = _path_findings(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File candidate = new java.io.File(base.toFile(), raw);
        audit(candidate);
        if (!candidate.getCanonicalPath().startsWith(
                base.toFile().getCanonicalPath() + java.io.File.separator)) {
            return;
        }
    """)
    assert any(finding["sink"]["callee"] == "File" for finding in findings)


def test_nested_file_passed_to_helper_is_not_retracted(tmp_path):
    findings = _path_findings(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File candidate = wrap(new java.io.File(raw));
        if (!candidate.getCanonicalPath().startsWith(
                base.toFile().getCanonicalPath() + java.io.File.separator)) {
            return;
        }
    """)
    assert any(finding["sink"]["callee"] == "File" for finding in findings)


def test_nested_constructor_inside_validated_constructor_stays_provisional(tmp_path):
    findings = _path_findings(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File candidate = new java.io.File(new java.io.File(raw), "child");
        if (!candidate.getCanonicalPath().startsWith(
                base.toFile().getCanonicalPath() + java.io.File.separator)) {
            return;
        }
    """)
    assert any(finding["sink"]["callee"] == "File" for finding in findings)


def test_multiple_declarators_fail_closed(tmp_path):
    findings = _path_findings(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File escaped = new java.io.File(raw), candidate = new java.io.File(base.toFile(), raw);
        if (!candidate.getCanonicalPath().startsWith(
                base.toFile().getCanonicalPath() + java.io.File.separator)) {
            return;
        }
    """)
    assert any(finding["sink"]["callee"] == "File" for finding in findings)


def test_canonical_string_prefix_without_separator_is_not_a_guard(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File baseFile = base.toFile();
        java.io.File candidate = new java.io.File(baseFile, raw);
        if (!candidate.getCanonicalPath().startsWith(baseFile.getCanonicalPath())) {
            return;
        }
        new java.io.FileInputStream(candidate);
    """)


def test_unproven_precomputed_canonical_base_is_not_a_guard(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.io.File baseFile = base.toFile();
        String claimedCanonicalBase = baseFile.toString();
        java.io.File candidate = new java.io.File(baseFile, raw);
        if (!candidate.getCanonicalPath().startsWith(
                claimedCanonicalBase + java.io.File.separator)) {
            return;
        }
        new java.io.FileInputStream(candidate);
    """)


def test_ignored_containment_result_is_not_a_guard(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        candidate.normalize().toAbsolutePath().startsWith(
                base.normalize().toAbsolutePath());
        new java.io.FileInputStream(candidate.toFile());
    """)


def test_guard_does_not_clear_a_different_path(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        java.nio.file.Path other = base.resolve(raw);
        if (!candidate.normalize().toAbsolutePath().startsWith(
                base.normalize().toAbsolutePath())) {
            return;
        }
        new java.io.FileInputStream(other.toFile());
    """)


def test_inverted_guard_does_not_clear_candidate(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        if (candidate.normalize().toAbsolutePath().startsWith(
                base.normalize().toAbsolutePath())) {
            return;
        }
        new java.io.FileInputStream(candidate.toFile());
    """)


def test_rejection_branch_must_terminate(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        if (!candidate.normalize().toAbsolutePath().startsWith(
                base.normalize().toAbsolutePath())) {
            System.err.println("rejected");
        }
        new java.io.FileInputStream(candidate.toFile());
    """)


def test_sink_in_rejection_arm_remains_visible(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        if (!candidate.normalize().toAbsolutePath().startsWith(
                base.normalize().toAbsolutePath())) {
            new java.io.FileInputStream(candidate.toFile());
            return;
        }
    """)


def test_return_nested_in_non_exhaustive_if_does_not_terminate_arm(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        if (!candidate.normalize().toAbsolutePath().startsWith(
                base.normalize().toAbsolutePath())) {
            if (raw.isEmpty()) {
                return;
            }
        }
        new java.io.FileInputStream(candidate.toFile());
    """)


def test_mismatched_normalization_shapes_are_not_a_guard(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        java.nio.file.Path candidate = base.resolve(raw);
        if (!candidate.normalize().toAbsolutePath().startsWith(
                base.toAbsolutePath().normalize())) {
            return;
        }
        new java.io.FileInputStream(candidate.toFile());
    """)


def test_string_starts_with_is_not_path_containment(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        String candidate = base.resolve(raw).toString();
        String baseText = base.toString();
        if (!candidate.startsWith(baseText)) {
            return;
        }
        new java.io.FileInputStream(candidate);
    """)


def test_tainted_base_does_not_validate_candidate(tmp_path):
    assert _has_path_finding(tmp_path, """
        String raw = request.getParameter("path");
        String attackerRoot = request.getParameter("base");
        java.nio.file.Path attackerBase = java.nio.file.Path.of(attackerRoot);
        java.nio.file.Path candidate = base.resolve(raw);
        if (!candidate.normalize().toAbsolutePath().startsWith(
                attackerBase.normalize().toAbsolutePath())) {
            return;
        }
        new java.io.FileInputStream(candidate.toFile());
    """)
