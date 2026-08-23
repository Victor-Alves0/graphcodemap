"""Deterministic performance contracts for the Java taint closure.

These tests assert work counts and cache reuse instead of machine-dependent
wall-clock limits.  The external benchmark remains the end-to-end time gate.
"""

from __future__ import annotations

from codegraph import CodeGraph
from codegraph import dataflow as df
from codegraph.flowsens import _Eval
from codegraph.languages import get_parser


def _scan(tmp_path, source: str):
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        return graph.taint(max_findings=100)[0]
    finally:
        graph.close()


def _sinks(result: dict) -> set[str]:
    return {finding["sink"]["callee"] for finding in result["findings"]}


def test_void_methods_do_not_run_parametric_return_flow(tmp_path):
    helpers = " ".join(
        f"void helper{index}() {{ int local = {index}; }}"
        for index in range(40)
    )
    result = _scan(
        tmp_path,
        f"""
        class App {{
            {helpers}
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {{
                String path = request.getParameter("path");
                new java.io.FileInputStream(path);
            }}
        }}
        """,
    )

    assert "FileInputStream" in _sinks(result)
    assert result["analysis"] == {
        "profiles": ["all"],
        "summary_flow_runs": 0,
    }


def test_contextual_sanitizer_without_xss_uses_only_non_xss_profile(tmp_path):
    result = _scan(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        java.sql.Statement statement) throws Exception {
                String external = request.getParameter("name");
                String htmlSafe =
                    org.owasp.esapi.ESAPI.encoder().encodeForHTML(external);
                statement.executeQuery(
                    "select * from users where name='" + htmlSafe + "'");
            }
        }
        """,
    )

    assert result["analysis"]["profiles"] == ["non-xss"]
    assert "executeQuery" in _sinks(result)


def test_lambda_contextual_sanitizer_and_xss_sink_activate_dual_profiles(
        tmp_path):
    result = _scan(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        javax.servlet.http.HttpServletResponse response) {
                Runnable render = () -> {
                    try {
                        String external = request.getParameter("message");
                        String encoded = org.springframework.web.util.HtmlUtils
                            .htmlEscape(external);
                        response.getWriter().println(encoded);
                    } catch (Exception ignored) { }
                };
                render.run();
            }
        }
        """,
    )

    assert result["analysis"]["profiles"] == ["xss", "non-xss"]
    assert "println" not in _sinks(result)


def _long_loop_facts(length: int):
    declarations = "; ".join(
        ['String a0 = request.getParameter("path")']
        + [f'String a{index} = "safe"' for index in range(1, length + 1)]
    )
    shifts = "; ".join(
        f"a{index} = a{index - 1}" for index in range(length, 0, -1)
    )
    source = f"""
        class App {{
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {{
                {declarations};
                while (request != null) {{ {shifts}; }}
                new java.io.FileInputStream(a{length});
            }}
        }}
    """.encode()
    tree = get_parser("java").parse(source)
    owner = next(
        node for node in tree.root_node.named_children
        if node.type == "class_declaration"
    )
    function = next(
        node for node in owner.child_by_field_name("body").named_children
        if node.type == "method_declaration"
    )
    return df.extract_facts(source, function, "java")


def test_loop_fixpoint_reuses_indexed_span_events():
    facts = _long_loop_facts(32)
    flow = df.Flow()
    evaluator = _Eval(facts, frozenset(), flow, sources={"getParameter"})
    calls = 0
    original = evaluator._events_for_span

    def counted(start, end):
        nonlocal calls
        calls += 1
        return original(start, end)

    evaluator._events_for_span = counted
    evaluator.run(facts.regions, set())

    assert any(arg.callee == "FileInputStream" for arg in flow.arg_flows)
    assert len(evaluator._events_by_start) == (
        len(facts.assigns) + len(facts.calls) + len(facts.returns)
    )
    # The loop revisits regions through many lattice rounds, while each exact
    # region materializes and sorts its event slice only once.
    assert calls > len(evaluator._span_events) * 4
