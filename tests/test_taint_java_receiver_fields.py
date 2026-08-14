"""Interprocedural Java field flow must preserve object and call order."""

from __future__ import annotations

from codegraph import CodeGraph


def _file_findings(tmp_path, source: str) -> list[dict]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        return [
            finding for finding in data["findings"]
            if finding["sink"]["callee"] == "File"
        ]
    finally:
        graph.close()


def test_dirty_field_reaches_later_same_receiver_method(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path;

            void consume() { new java.io.File(this.path); }

            void handle(javax.servlet.http.HttpServletRequest request) {
                this.path = request.getParameter("path");
                consume();
            }
        }
        """,
    )
    assert findings


def test_explicit_this_receiver_preserves_field_state(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path;

            void consume() { new java.io.File(path); }

            void handle(javax.servlet.http.HttpServletRequest request) {
                path = request.getParameter("path");
                this.consume();
            }
        }
        """,
    )
    assert findings


def test_call_before_field_write_does_not_time_travel(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path = "safe.txt";

            void consume() { new java.io.File(path); }

            void handle(javax.servlet.http.HttpServletRequest request) {
                consume();
                path = request.getParameter("path");
            }
        }
        """,
    )
    assert not findings


def test_clean_overwrite_before_call_kills_field_taint(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path;

            void consume() { new java.io.File(path); }

            void handle(javax.servlet.http.HttpServletRequest request) {
                path = request.getParameter("path");
                path = "safe.txt";
                consume();
            }
        }
        """,
    )
    assert not findings


def test_field_state_does_not_jump_to_another_receiver(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path;

            void consume() { new java.io.File(path); }

            void handle(javax.servlet.http.HttpServletRequest request,
                        App other) {
                path = request.getParameter("path");
                other.consume();
            }
        }
        """,
    )
    assert not findings


def test_local_shadow_does_not_inherit_instance_field_taint(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path;

            void consume(String path) { new java.io.File(path); }

            void handle(javax.servlet.http.HttpServletRequest request) {
                this.path = request.getParameter("path");
                consume("safe.txt");
            }
        }
        """,
    )
    assert not findings


def test_explicit_this_field_survives_local_shadow(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path;

            void consume(String path) { new java.io.File(this.path); }

            void handle(javax.servlet.http.HttpServletRequest request) {
                this.path = request.getParameter("path");
                consume("safe.txt");
            }
        }
        """,
    )
    assert findings
