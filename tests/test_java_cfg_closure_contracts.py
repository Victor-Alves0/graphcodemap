"""Executable contracts for the remaining Java CFG/call-site boundary.

The positive and inverse cases are paired deliberately: recall fixes must not
turn into line-wide call resolution, unconditional ``finally`` union, loop
body execution, or eager lambda execution.  Strict xfails describe gaps in the
current engine and must be promoted to ordinary regressions when implemented.
"""

from __future__ import annotations

from codegraph import CodeGraph
from codegraph.l1 import promote


def _scan(tmp_path, source: str):
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def _path_findings(graph: CodeGraph) -> tuple[list[dict], object]:
    data, env = graph.taint(max_findings=100)
    return [
        finding
        for finding in data["findings"]
        if finding["sink"]["callee"] in {"File", "FileInputStream"}
    ], env


def _promote_typed_run(graph: CodeGraph, owner: str) -> None:
    """Promote exactly the syntactic ``owner.run`` site, as a partial L1 run."""
    conn = graph.indexer.conn
    edge = conn.execute(
        "SELECT id, file_id, line, col FROM edges "
        "WHERE dst_name=? LIMIT 1",
        (f"{owner}.run",),
    ).fetchone()
    target = conn.execute(
        "SELECT m.id FROM symbols m JOIN symbols c ON m.parent_id=c.id "
        "WHERE c.name=? AND m.name='run' LIMIT 1",
        (owner,),
    ).fetchone()
    assert edge is not None and target is not None
    assert promote.apply(conn, edge["file_id"], edge, [target["id"]]) == 1
    conn.commit()


def _assert_distinct_same_line_run_sites(graph: CodeGraph) -> None:
    """Guard the fixture: both calls share a line but not a call-site column."""
    rows = graph.indexer.conn.execute(
        "SELECT dst_name, line, col FROM edges "
        "WHERE dst_name IN ('SafeHandler.run', 'DangerousHandler.run')"
    ).fetchall()
    assert {row["dst_name"] for row in rows} == {
        "SafeHandler.run", "DangerousHandler.run",
    }
    assert len({row["line"] for row in rows}) == 1
    assert len({row["col"] for row in rows}) == 2


_SAME_LINE_TEMPLATE = """
    import java.io.FileInputStream;

    class SafeHandler {{
        void run(String value) {{ }}
    }}

    class DangerousHandler {{
        void run(String value) throws Exception {{
            new FileInputStream(value);
        }}
    }}

    class App {{
        void handle(javax.servlet.http.HttpServletRequest request)
                throws Exception {{
            String path = request.getParameter("path");
            SafeHandler safe = new SafeHandler();
            DangerousHandler dangerous = new DangerousHandler();
            {calls}
        }}
    }}
"""


def test_same_line_call_site_keeps_dangerous_target_by_column(tmp_path):
    """A certain safe site must not suppress a later dangerous site."""
    graph = _scan(
        tmp_path,
        _SAME_LINE_TEMPLATE.format(
            calls='safe.run("constant"); dangerous.run(path);'
        ),
    )
    try:
        _assert_distinct_same_line_run_sites(graph)
        _promote_typed_run(graph, "SafeHandler")
        findings, _env = _path_findings(graph)
        assert findings
    finally:
        graph.close()


def test_same_line_call_site_does_not_borrow_dangerous_target(tmp_path):
    """A certain dangerous site must not create a finding at a safe site."""
    graph = _scan(
        tmp_path,
        _SAME_LINE_TEMPLATE.format(
            calls='safe.run(path); dangerous.run("constant");'
        ),
    )
    try:
        _assert_distinct_same_line_run_sites(graph)
        _promote_typed_run(graph, "DangerousHandler")
        findings, _env = _path_findings(graph)
        assert not findings
    finally:
        graph.close()


def test_finally_runs_after_taint_generated_on_normal_try_exit(tmp_path):
    """At least the normal try exit reaches the mandatory finally block."""
    graph = _scan(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = "safe";
                try {
                    path = request.getParameter("path");
                } catch (IllegalArgumentException ignored) {
                    path = "safe";
                } finally {
                    new FileInputStream(path);
                }
            }
        }
        """,
    )
    try:
        findings, _env = _path_findings(graph)
        assert findings
    finally:
        graph.close()


def test_finally_does_not_skip_definite_clean_try_exit(tmp_path):
    """A non-throwing clean assignment precedes finally on every real path."""
    graph = _scan(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = request.getParameter("path");
                try {
                    path = "safe";
                } finally {
                    new FileInputStream(path);
                }
            }
        }
        """,
    )
    try:
        findings, _env = _path_findings(graph)
        assert not findings
    finally:
        graph.close()


def test_long_loop_chain_reaches_fixpoint_or_reports_partial(tmp_path):
    """A missed lattice state is acceptable only with an explicit partial result."""
    graph = _scan(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String a0 = request.getParameter("path");
                String a1 = "safe", a2 = "safe", a3 = "safe";
                String a4 = "safe", a5 = "safe", a6 = "safe";
                String a7 = "safe", a8 = "safe", a9 = "safe";
                String a10 = "safe", a11 = "safe", a12 = "safe";
                while (request != null) {
                    a12 = a11; a11 = a10; a10 = a9; a9 = a8;
                    a8 = a7; a7 = a6; a6 = a5; a5 = a4;
                    a4 = a3; a3 = a2; a2 = a1; a1 = a0;
                }
                new FileInputStream(a12);
            }
        }
        """,
    )
    try:
        findings, env = _path_findings(graph)
        assert findings or env.truncated
    finally:
        graph.close()


def test_statically_false_loop_does_not_execute_source_body(tmp_path):
    """Zero iterations means a body-only source cannot reach the later sink."""
    graph = _scan(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String path = "safe";
                while (false) {
                    path = request.getParameter("path");
                }
                new FileInputStream(path);
            }
        }
        """,
    )
    try:
        findings, _env = _path_findings(graph)
        assert not findings
    finally:
        graph.close()


def test_invoked_lambda_reads_its_captured_taint(tmp_path):
    """Construction is deferred, but invocation executes the captured sink."""
    graph = _scan(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String path = request.getParameter("path");
                Runnable task = () -> {
                    try { new FileInputStream(path); }
                    catch (Exception ignored) { }
                };
                task.run();
            }
        }
        """,
    )
    try:
        findings, _env = _path_findings(graph)
        assert findings
    finally:
        graph.close()


def test_invoked_lambda_parameter_receives_tainted_argument(tmp_path):
    """A functional-interface argument is a normal parameter-flow boundary."""
    graph = _scan(
        tmp_path,
        """
        import java.io.FileInputStream;
        import java.util.function.Consumer;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                Consumer<String> task = value -> {
                    try { new FileInputStream(value); }
                    catch (Exception ignored) { }
                };
                task.accept(request.getParameter("path"));
            }
        }
        """,
    )
    try:
        findings, _env = _path_findings(graph)
        assert findings
    finally:
        graph.close()


def test_uninvoked_lambda_is_not_eagerly_executed(tmp_path):
    """Making lambda bodies visible must not inline them at construction time."""
    graph = _scan(
        tmp_path,
        """
        import java.io.FileInputStream;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String path = request.getParameter("path");
                Runnable task = () -> {
                    try { new FileInputStream(path); }
                    catch (Exception ignored) { }
                };
            }
        }
        """,
    )
    try:
        findings, _env = _path_findings(graph)
        assert not findings
    finally:
        graph.close()


def test_lambda_parameter_shadows_tainted_outer_local(tmp_path):
    """An invoked lambda must bind its own safe parameter, not capture its name."""
    graph = _scan(
        tmp_path,
        """
        import java.io.FileInputStream;
        import java.util.function.Consumer;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String value = request.getParameter("path");
                Consumer<String> task = value -> {
                    try { new FileInputStream(value); }
                    catch (Exception ignored) { }
                };
                task.accept("constant");
            }
        }
        """,
    )
    try:
        findings, _env = _path_findings(graph)
        assert not findings
    finally:
        graph.close()
