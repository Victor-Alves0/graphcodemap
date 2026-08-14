"""End-to-end contracts across Java L0/L1 and both flow query surfaces."""

from __future__ import annotations

from codegraph import CodeGraph
from codegraph.l1 import promote


SOURCE = """
import java.io.FileInputStream;

class Noise { Noise() {} }

class App {
    void forward(String value) throws Exception {
        new FileInputStream(value);
    }

    void handle(javax.servlet.http.HttpServletRequest request) throws Exception {
        String path = request.getParameter("path");
        new Noise(); forward(path);
    }
}
"""


def _partially_refined_graph(tmp_path) -> CodeGraph:
    """Promote only the constructor on a line that also has a real data call.

    This models an incremental/partial L1 run: the constructor is semantic and
    ``certain`` while the adjacent ``forward`` call retains its valid L0 edge.
    Flow resolution must identify the call by both line and callee, otherwise
    the stronger but unrelated constructor steals ``forward``'s ArgFlow.
    """
    (tmp_path / "App.java").write_text(SOURCE.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    conn = graph.indexer.conn
    edge = conn.execute(
        "SELECT id, file_id, line, col FROM edges "
        "WHERE dst_name='Noise' ORDER BY id LIMIT 1"
    ).fetchone()
    constructor = conn.execute(
        "SELECT id FROM symbols WHERE name='Noise' AND kind='method'"
    ).fetchone()
    assert edge is not None and constructor is not None
    assert promote.apply(
        conn, edge["file_id"], edge, [constructor["id"]]
    ) == 1
    conn.commit()
    return graph


def test_data_flow_keeps_callee_identity_with_two_calls_on_same_line(tmp_path):
    graph = _partially_refined_graph(tmp_path)
    try:
        data, _env = graph.data_flow("App.handle", depth=3)
        request_flow = next(
            param for param in data["params"] if param["name"] == "request"
        )
        assert any(
            sink["callee_name"] == "FileInputStream"
            for sink in request_flow["sinks"]
        )
    finally:
        graph.close()


def test_taint_keeps_callee_identity_with_two_calls_on_same_line(tmp_path):
    graph = _partially_refined_graph(tmp_path)
    try:
        data, _env = graph.taint(max_findings=100)
        assert any(
            finding["sink"]["callee"] == "FileInputStream"
            for finding in data["findings"]
        )
    finally:
        graph.close()


def test_virtual_override_cannot_inherit_base_receiver_kill(tmp_path):
    """A virtual helper's base implementation is not a closed-world kill.

    ``handle`` may execute on ``Sub``; Java then dispatches ``clear`` to the
    override, which intentionally leaves the tainted receiver field intact.
    """
    (tmp_path / "App.java").write_text(
        """
        import java.io.FileInputStream;

        class Base {
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

        class Sub extends Base {
            @Override void clear() { }
        }
        """.strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        assert any(
            finding["sink"]["callee"] == "FileInputStream"
            for finding in data["findings"]
        )
    finally:
        graph.close()


def test_nonprop_summary_is_scoped_to_exact_assignment_span(tmp_path):
    """A clean RHS must not sanitize a second assignment on the same line."""
    (tmp_path / "App.java").write_text(
        """
        import java.io.FileInputStream;

        class App {
            String clean(String value) { return "safe"; }
            String identity(String value) { return value; }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = request.getParameter("path");
                String cleanValue = clean(path); String dirty = identity(path);
                new FileInputStream(dirty);
            }
        }
        """.strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        conn = graph.indexer.conn
        edge = conn.execute(
            "SELECT id, file_id, line, col FROM edges "
            "WHERE dst_name='clean' LIMIT 1"
        ).fetchone()
        target = conn.execute(
            "SELECT id FROM symbols WHERE name='clean' AND kind='method'"
        ).fetchone()
        assert edge is not None and target is not None
        assert promote.apply(
            conn, edge["file_id"], edge, [target["id"]]
        ) == 1
        conn.commit()

        data, _env = graph.taint(max_findings=100)
        assert any(
            finding["sink"]["callee"] == "FileInputStream"
            for finding in data["findings"]
        )
    finally:
        graph.close()


def test_deferred_lambda_write_does_not_clean_enclosing_method(tmp_path):
    """Constructing a lambda does not execute its receiver-field write."""
    (tmp_path / "App.java").write_text(
        """
        import java.io.FileInputStream;

        class App {
            String path;
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                this.path = request.getParameter("path");
                Runnable deferred = () -> { this.path = "safe"; };
                new FileInputStream(this.path);
            }
        }
        """.strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        assert any(
            finding["sink"]["callee"] == "FileInputStream"
            for finding in data["findings"]
        )
    finally:
        graph.close()


def test_uninvoked_lambda_sink_is_not_inlined_into_enclosing_method(tmp_path):
    """A sink in a lambda is not reached merely because the lambda is built."""
    (tmp_path / "App.java").write_text(
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String path = request.getParameter("path");
                Runnable deferred = () -> {
                    try { new FileInputStream(path); }
                    catch (Exception ignored) { }
                };
            }
        }
        """.strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        assert not any(
            finding["sink"]["callee"] == "FileInputStream"
            for finding in data["findings"]
        )
    finally:
        graph.close()


def test_fact_cache_distinguishes_java_methods_on_same_line(tmp_path):
    """Method start column is part of identity in compact/generated Java."""
    (tmp_path / "App.java").write_text(
        """import java.io.FileInputStream; class App {
        void safe(String value) {} void forward(String value) throws Exception {
        new FileInputStream(value); } void handle(
        javax.servlet.http.HttpServletRequest request) throws Exception {
        String path=request.getParameter("path"); forward(path); } }""",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        # ``safe`` and ``forward`` deliberately start on the same physical
        # line. Facts/cache lookup must still select ``forward`` by column/span.
        methods = graph.indexer.conn.execute(
            "SELECT name, start_line FROM symbols WHERE kind='method'"
        ).fetchall()
        starts = {row["name"]: row["start_line"] for row in methods}
        assert starts["safe"] == starts["forward"]

        data, _env = graph.taint(max_findings=100)
        assert any(
            finding["sink"]["callee"] == "FileInputStream"
            for finding in data["findings"]
        )
    finally:
        graph.close()
