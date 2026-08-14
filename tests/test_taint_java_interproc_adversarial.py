"""Adversarial contracts for Java source wrappers and receiver-field flow."""

from __future__ import annotations

from codegraph import CodeGraph


def _file_input_findings(tmp_path, source: str) -> list[dict]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        return [
            finding for finding in data["findings"]
            if finding["sink"]["callee"] == "FileInputStream"
        ]
    finally:
        graph.close()


def test_source_wrapper_survives_java_collection_refinement(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;
        import java.util.ArrayList;

        class Source {
            String read(javax.servlet.http.HttpServletRequest request) {
                return request.getParameter("path");
            }
        }

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = new Source().read(request);
                ArrayList<String> values = new ArrayList<>();
                values.add("safe");
                new FileInputStream(path);
            }
        }
        """,
    )
    assert findings


def test_rhs_call_sees_field_state_before_source_assignment(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            String dirtyAndRead(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                new FileInputStream(this.path);
                return request.getParameter("path");
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = dirtyAndRead(request);
            }
        }
        """,
    )
    assert not findings


def test_rhs_call_sees_field_state_before_clean_assignment(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            String readThenClean() throws Exception {
                new FileInputStream(this.path);
                return "safe";
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = request.getParameter("path");
                this.path = readThenClean();
            }
        }
        """,
    )
    assert findings


def test_new_same_class_instance_does_not_inherit_current_receiver_state(
        tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            App() throws Exception {
                new FileInputStream(this.path);
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = request.getParameter("path");
                new App();
            }
        }
        """,
    )
    assert not findings


def test_source_wrapper_marker_is_scoped_to_exact_assignment(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class Source {
            String read(javax.servlet.http.HttpServletRequest request) {
                return request.getParameter("path");
            }
        }

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String dirty = new Source().read(request); String safe = "safe";
                new FileInputStream(safe);
            }
        }
        """,
    )
    assert not findings


def test_same_line_call_does_not_change_assignment_receiver_identity(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class CleanComposer {
            String compose(javax.servlet.http.HttpServletRequest request) {
                return "safe";
            }
        }

        class App {
            String compose(javax.servlet.http.HttpServletRequest request) {
                return request.getParameter("path");
            }

            void handle(CleanComposer clean,
                        javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = clean.compose(request); this.compose(request);
                new FileInputStream(path);
            }
        }
        """,
    )
    assert not findings


def test_java_io_wildcard_import_resolves_typed_input_source(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.*;

        class App {
            void handle(BufferedReader reader) throws Exception {
                String path = reader.readLine();
                new FileInputStream(path);
            }
        }
        """,
    )
    assert findings


def test_cleaning_helper_updates_caller_receiver_state(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            void clear() { this.path = "safe"; }
            void consume() throws Exception {
                new FileInputStream(this.path);
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = request.getParameter("path");
                clear();
                consume();
            }
        }
        """,
    )
    assert not findings


def test_cleaning_helper_does_not_retroactively_hide_earlier_sink(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            void clear() { this.path = "safe"; }
            void consume() throws Exception {
                new FileInputStream(this.path);
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = request.getParameter("path");
                consume();
                clear();
            }
        }
        """,
    )
    assert findings


def test_conditional_cleaning_helper_does_not_claim_definite_kill(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            void maybeClear(boolean allowed) {
                if (allowed) this.path = "safe";
            }
            void consume() throws Exception {
                new FileInputStream(this.path);
            }

            void handle(javax.servlet.http.HttpServletRequest request,
                        boolean allowed) throws Exception {
                this.path = request.getParameter("path");
                maybeClear(allowed);
                consume();
            }
        }
        """,
    )
    assert findings


def test_unresolved_same_receiver_call_keeps_clean_summary_fail_closed(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            void clearThenUnknown() {
                this.path = "safe";
                extensionHook();
            }
            void consume() throws Exception {
                new FileInputStream(this.path);
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = request.getParameter("path");
                clearThenUnknown();
                consume();
            }
        }
        """,
    )
    assert findings


def test_nested_cleaning_summary_composes_on_same_receiver(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            void reset() { this.path = "safe"; }
            void clear() { reset(); }
            void consume() throws Exception {
                new FileInputStream(this.path);
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = request.getParameter("path");
                clear();
                consume();
            }
        }
        """,
    )
    assert not findings


def test_other_receiver_cleaning_helper_does_not_clear_this(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            void clear() { this.path = "safe"; }
            void consume() throws Exception {
                new FileInputStream(this.path);
            }

            void handle(App other,
                        javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = request.getParameter("path");
                other.clear();
                consume();
            }
        }
        """,
    )
    assert findings


def test_parameter_written_by_helper_updates_caller_receiver_state(tmp_path):
    findings = _file_input_findings(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            String path;

            void capture(String value) { this.path = value; }
            void consume() throws Exception {
                new FileInputStream(this.path);
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                capture(request.getParameter("path"));
                consume();
            }
        }
        """,
    )
    assert findings


def test_data_flow_reports_parameter_stored_in_receiver_field(tmp_path):
    (tmp_path / "App.java").write_text(
        """
        class App {
            String path;
            void consume() { sink(this.path); }
            void sink(String value) {}
            void handle(String input) {
                this.path = input;
                consume();
            }
        }
        """.strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.data_flow("App.handle", depth=3)
        input_flow = next(param for param in data["params"]
                          if param["name"] == "input")
        assert any(sink["callee_name"] == "sink"
                   for sink in input_flow["sinks"])
    finally:
        graph.close()
