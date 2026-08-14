"""Adversarial contracts found while reviewing Java heap/source summaries."""

import json

from codegraph import CodeGraph


def _findings(tmp_path, source: str) -> list[dict]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    config_dir = tmp_path / ".codegraph"
    config_dir.mkdir()
    (config_dir / "taint.json").write_text(
        json.dumps({"sources": ["Request.getResource"]}),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        result, _env = graph.taint(max_findings=100)
        return result["findings"]
    finally:
        graph.close()


def test_qualified_named_source_directly_nested_in_sink_is_reported(tmp_path):
    findings = _findings(
        tmp_path,
        """
        interface Request { String getResource(); }

        class App {
            void handle(Request request) throws Exception {
                new java.io.FileInputStream(request.getResource());
            }
        }
        """,
    )

    assert any(
        finding["sink"]["callee"] == "FileInputStream"
        for finding in findings
    )


def test_sanitizer_still_cuts_named_source_directly_nested_in_sink(tmp_path):
    findings = _findings(
        tmp_path,
        """
        interface Request { String getResource(); }

        class App {
            void handle(Request request) throws Exception {
                new java.io.FileInputStream(escape(request.getResource()));
            }
        }
        """,
    )

    assert not findings


def test_receiver_kill_does_not_ignore_retaint_through_this_alias(tmp_path):
    findings = _findings(
        tmp_path,
        """
        interface Request { String getResource(); }

        class App {
            private String filePath;

            private void store(String value) {
                this.filePath = value;
            }

            private void rewrite(String value) {
                this.filePath = "safe.txt";
                App alias = this;
                alias.filePath = value;
            }

            private void consume() throws Exception {
                new java.io.FileInputStream(this.filePath);
            }

            void handle(Request request) throws Exception {
                String value = request.getResource();
                store(value);
                rewrite(value);
                consume();
            }
        }
        """,
    )

    assert any(
        finding["sink"]["callee"] == "FileInputStream"
        for finding in findings
    )


def test_receiver_summary_preserves_taint_written_to_field_subpath(tmp_path):
    findings = _findings(
        tmp_path,
        """
        interface Request { String getResource(); }

        class Holder {
            String path;
        }

        class App {
            private Holder holder = new Holder();

            private void store(String value) {
                this.holder.path = value;
            }

            private void consume() throws Exception {
                new java.io.FileInputStream(this.holder.path);
            }

            void handle(Request request) throws Exception {
                String value = request.getResource();
                store(value);
                consume();
            }
        }
        """,
    )

    assert any(
        finding["sink"]["callee"] == "FileInputStream"
        for finding in findings
    )


def test_receiver_kill_is_blocked_when_this_escapes_to_mutator(tmp_path):
    findings = _findings(
        tmp_path,
        """
        interface Request { String getResource(); }

        class Mutator {
            static void retaint(App target, String value) {
                target.filePath = value;
            }
        }

        class App {
            String filePath;

            private void store(String value) {
                this.filePath = value;
            }

            private void rewrite(String value) {
                this.filePath = "safe.txt";
                Mutator.retaint(this, value);
            }

            private void consume() throws Exception {
                new java.io.FileInputStream(this.filePath);
            }

            void handle(Request request) throws Exception {
                String value = request.getResource();
                store(value);
                rewrite(value);
                consume();
            }
        }
        """,
    )

    assert any(
        finding["sink"]["callee"] == "FileInputStream"
        for finding in findings
    )


def test_receiver_dirty_effect_unions_ambiguous_overload_fanout(tmp_path):
    findings = _findings(
        tmp_path,
        """
        interface Request { String getResource(); }

        class App {
            private String filePath;

            private void dispatch(String value) {
                this.filePath = value;
            }

            private void dispatch(int ignored) {
            }

            private void consume() throws Exception {
                new java.io.FileInputStream(this.filePath);
            }

            void handle(Request request) throws Exception {
                String value = request.getResource();
                dispatch(value);
                dispatch(0);
                consume();
            }
        }
        """,
    )

    assert any(
        finding["sink"]["callee"] == "FileInputStream"
        for finding in findings
    )
