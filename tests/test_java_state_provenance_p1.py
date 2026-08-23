"""P1 contracts for branch state and interprocedural source provenance."""

from __future__ import annotations

import textwrap

from codegraph import CodeGraph


def _taint(tmp_path, source: str):
    (tmp_path / "App.java").write_text(
        textwrap.dedent(source), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        result, _env = graph.taint(max_findings=100)
        return result
    finally:
        graph.close()


def _file_findings(result):
    return [
        finding for finding in result["findings"]
        if finding["sink"]["callee"] in {"File", "FileInputStream"}
    ]


def test_system_property_branch_join_preserves_may_taint(tmp_path):
    result = _taint(tmp_path, """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        boolean chooseExternal) throws Exception {
                String external = request.getParameter("path");
                if (chooseExternal) {
                    System.setProperty("user.dir", external);
                } else {
                    System.setProperty("user.dir", "fixed");
                }
                new java.io.FileInputStream(
                    System.getProperty("user.dir"));
            }
        }
    """)

    assert _file_findings(result)


def test_system_property_state_does_not_leak_between_exclusive_arms(tmp_path):
    result = _taint(tmp_path, """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        boolean chooseExternal) throws Exception {
                String external = request.getParameter("path");
                if (chooseExternal) {
                    System.setProperty("user.dir", external);
                } else {
                    new java.io.FileInputStream(
                        System.getProperty("user.dir"));
                }
            }
        }
    """)

    assert not _file_findings(result)


def test_shared_subtree_keeps_each_distinct_source_origin(tmp_path):
    result = _taint(tmp_path, """
        class App {
            void first(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String value = request.getParameter("path");
                shared(value);
            }

            void second(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String value = request.getHeader("X-Path");
                shared(value);
            }

            void shared(String value) throws Exception { leaf(value); }

            void leaf(String value) throws Exception {
                new java.io.FileInputStream(value);
            }
        }
    """)

    origins = {
        finding["origin"]["what"] for finding in _file_findings(result)
    }
    assert origins == {"getParameter()", "getHeader()"}


def test_shared_subtree_still_collapses_duplicate_same_origin(tmp_path):
    result = _taint(tmp_path, """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String value = request.getParameter("path");
                shared(value);
                shared(value);
            }

            void shared(String value) throws Exception { leaf(value); }

            void leaf(String value) throws Exception {
                new java.io.FileInputStream(value);
            }
        }
    """)

    findings = _file_findings(result)
    assert len(findings) == 1
    assert findings[0]["origin"]["what"] == "getParameter()"
    assert result["explored"] < 10
