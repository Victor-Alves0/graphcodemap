"""Executable findings from the end-to-end Java contract review.

These tests describe required security behavior.  They intentionally remain
red until the corresponding core contract is implemented; the review does
not patch production code.
"""

from __future__ import annotations

from codegraph import CodeGraph
from codegraph.l1 import promote


def test_same_line_homonymous_receiver_calls_keep_exact_target(tmp_path):
    """Each call site must recurse into its own receiver-resolved target."""
    (tmp_path / "App.java").write_text(
        """
        import java.io.FileInputStream;

        class SafeHandler {
            void run(String value) { }
        }

        class DangerousHandler {
            void run(String value) throws Exception {
                new FileInputStream(value);
            }
        }

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = request.getParameter("path");
                SafeHandler safe = new SafeHandler();
                DangerousHandler dangerous = new DangerousHandler();
                safe.run(path); dangerous.run(path);
            }
        }
        """.strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        # Model a partial but valid L1 run: the first call site has been
        # resolved with semantic certainty while the second remains L0.  The
        # certain edge must not be reused for another column on the same line.
        conn = graph.indexer.conn
        edge = conn.execute(
            "SELECT e.id, e.file_id, e.line, e.col "
            "FROM edges e WHERE e.dst_name='SafeHandler.run' LIMIT 1"
        ).fetchone()
        target = conn.execute(
            "SELECT m.id FROM symbols m JOIN symbols c ON m.parent_id=c.id "
            "WHERE c.name='SafeHandler' AND m.name='run' LIMIT 1"
        ).fetchone()
        assert edge is not None and target is not None
        assert promote.apply(conn, edge["file_id"], edge, [target["id"]]) == 1
        conn.commit()
        data, _env = graph.taint(max_findings=100)
        assert any(
            finding["sink"]["callee"] == "FileInputStream"
            for finding in data["findings"]
        )
    finally:
        graph.close()


def test_finally_observes_taint_generated_in_try(tmp_path):
    """A Java finally block executes after a normal or exceptional try exit."""
    (tmp_path / "App.java").write_text(
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = null;
                try {
                    path = request.getParameter("path");
                } finally {
                    new FileInputStream(path);
                }
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


def test_disjoint_block_receiver_types_do_not_rewrite_earlier_call(tmp_path):
    """Receiver typing must follow Java lexical scope, not traversal order."""
    (tmp_path / "App.java").write_text(
        """
        import java.io.FileInputStream;

        class DangerousHandler {
            void run(String value) throws Exception {
                new FileInputStream(value);
            }
        }

        class SafeHandler {
            void run(String value) { }
        }

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = request.getParameter("path");
                {
                    DangerousHandler handler = new DangerousHandler();
                    handler.run(path);
                }
                {
                    SafeHandler handler = new SafeHandler();
                    handler.run("constant");
                }
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


def test_loop_fixpoint_is_not_silently_truncated_after_eight_rounds(tmp_path):
    """May-taint must cover every reachable loop iteration in its finite domain."""
    (tmp_path / "App.java").write_text(
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String a0 = request.getParameter("path");
                String a1 = "safe", a2 = "safe", a3 = "safe";
                String a4 = "safe", a5 = "safe", a6 = "safe";
                String a7 = "safe", a8 = "safe", a9 = "safe";
                String a10 = "safe";
                while (request != null) {
                    a10 = a9; a9 = a8; a8 = a7; a7 = a6; a6 = a5;
                    a5 = a4; a4 = a3; a3 = a2; a2 = a1; a1 = a0;
                }
                new FileInputStream(a10);
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


def test_html_encoder_does_not_sanitize_sql_query(tmp_path):
    """A sanitizer is valid only for compatible sink semantics/context."""
    (tmp_path / "App.java").write_text(
        """
        class App {
            String escapeHtml(String value) { return value; }

            void handle(javax.servlet.http.HttpServletRequest request,
                        java.sql.Statement statement) throws Exception {
                String value = request.getParameter("name");
                String html = escapeHtml(value);
                statement.executeQuery("SELECT * FROM users WHERE name='" +
                                       html + "'");
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
            finding["sink"]["callee"] == "executeQuery"
            for finding in data["findings"]
        )
    finally:
        graph.close()
