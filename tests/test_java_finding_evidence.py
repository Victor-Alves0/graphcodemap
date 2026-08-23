"""Exact and privacy-preserving provenance for Java security findings."""

from __future__ import annotations

from codegraph import CodeGraph


def _findings(tmp_path, source: str) -> list[dict]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        return data["findings"]
    finally:
        graph.close()


def test_java_finding_preserves_allowed_source_and_sink_evidence(tmp_path):
    findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String[] languages = request.getParameterValues("lang");
                new java.io.FileInputStream(languages[0]);
            }
        }
        """,
    )
    finding = next(
        finding for finding in findings
        if finding["sink"]["callee"] == "FileInputStream"
    )
    origin = finding["origin"]
    assert origin["what"] == "getParameterValues()"
    assert origin["line"] == 4
    assert isinstance(origin["column"], int)
    assert origin["byte_span"]["start"] < origin["byte_span"]["end"]
    assert origin["argument_literals"] == {0: "lang"}
    assert origin["parameter"] == "lang"

    sink = finding["sink"]
    assert sink["line"] == 5
    assert isinstance(sink["column"], int)
    assert sink["byte_span"]["start"] < sink["byte_span"]["end"]
    assert finding["steps"][-1]["column"] == sink["column"]
    assert finding["steps"][-1]["byte_span"] == sink["byte_span"]


def test_java_finding_does_not_publish_unapproved_source_literals(tmp_path):
    findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String value = request.getHeader("Authorization");
                new java.io.FileInputStream(value);
            }
        }
        """,
    )
    origin = next(
        finding["origin"] for finding in findings
        if finding["sink"]["callee"] == "FileInputStream"
    )
    assert origin["what"] == "getHeader()"
    assert "argument_literals" not in origin
    assert "parameter" not in origin


def test_java_finding_omits_oversized_parameter_literal(tmp_path):
    parameter = "p" * 129
    findings = _findings(
        tmp_path,
        f"""
        class App {{
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {{
                String value = request.getParameter("{parameter}");
                new java.io.FileInputStream(value);
            }}
        }}
        """,
    )
    origin = next(
        finding["origin"] for finding in findings
        if finding["sink"]["callee"] == "FileInputStream"
    )
    assert "argument_literals" not in origin
    assert "parameter" not in origin


def test_same_line_sink_sites_survive_finding_deduplication(tmp_path):
    findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String value = request.getParameter("path");
                new java.io.FileInputStream(value); new java.io.FileInputStream(value);
            }
        }
        """,
    )
    sinks = [
        finding["sink"] for finding in findings
        if finding["sink"]["callee"] == "FileInputStream"
    ]
    assert len(sinks) == 2
    assert len({sink["column"] for sink in sinks}) == 2
    assert len({tuple(sink["byte_span"].values()) for sink in sinks}) == 2


def test_line_only_call_resolution_fails_closed_for_multiple_sites(tmp_path):
    (tmp_path / "App.java").write_text(
        """
        class A { void run(String value) {} }
        class B { void run(String value) {} }
        class App { void call(A a, B b) { a.run("x"); b.run("y"); } }
        """.strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        conn = graph.indexer.conn
        row = conn.execute(
            "SELECT e.src, e.line FROM edges e JOIN symbols s ON e.src=s.id "
            "WHERE s.name='call' AND e.kind='calls' AND e.dst_name LIKE '%.run' "
            "LIMIT 1"
        ).fetchone()
        assert row is not None
        assert graph.query._df_resolve_call(
            row["src"], row["line"], "run") is None
    finally:
        graph.close()
