"""General contracts distilled from the residual Java OWASP taxonomy.

The fixtures deliberately avoid OWASP testcase names and constants.  Each
case describes a reusable language/runtime contract which should hold for any
Java repository.  Strict xfails keep the current gaps visible and force their
removal once the implementation starts satisfying the contract.
"""

from __future__ import annotations

import pytest

from codegraph import CodeGraph


def _findings(tmp_path, source: str) -> tuple[CodeGraph, list[dict]]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    result, _env = graph.taint(max_findings=100)
    return graph, result["findings"]


def _has_sink(findings: list[dict], callee: str) -> bool:
    return any(finding["sink"]["callee"] == callee for finding in findings)


def _promote_unique_call(graph: CodeGraph, caller: str, callee: str) -> None:
    """Model the semantic-resolution precondition of return summaries."""
    conn = graph.indexer.conn
    target = conn.execute(
        "SELECT id FROM symbols WHERE name=? AND kind='method'", (callee,)
    ).fetchall()
    source = conn.execute(
        "SELECT id FROM symbols WHERE name=? AND kind='method'", (caller,)
    ).fetchall()
    assert len(target) == len(source) == 1
    changed = conn.execute(
        "UPDATE edges SET dst=?, confidence='certain', resolver='l1' "
        "WHERE src=? AND kind='calls' "
        "AND (dst_name=? OR dst_name LIKE ?)",
        (target[0]["id"], source[0]["id"], callee, f"%.{callee}"),
    ).rowcount
    assert changed == 1
    conn.commit()
    graph.query._facts_cache.clear()


def test_process_builder_constructor_tracks_mutated_command_list(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                java.util.List<String> command = new java.util.ArrayList<>();
                command.add("sh");
                command.add("-c");
                command.add("echo " + external);
                new ProcessBuilder(command).start();
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "ProcessBuilder")
    finally:
        graph.close()


def test_assigned_process_builder_tracks_mutated_command_list(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                java.util.List<String> command = new java.util.ArrayList<>();
                if (System.getProperty("os.name").contains("Windows")) {
                    command.add("cmd.exe");
                    command.add("/c");
                } else {
                    command.add("sh");
                    command.add("-c");
                }
                command.add("echo " + external);
                ProcessBuilder builder = new ProcessBuilder(command);
                builder.start();
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "ProcessBuilder")
    finally:
        graph.close()


def test_assigned_process_builder_tracks_source_wrapper_value(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            String read(javax.servlet.http.HttpServletRequest request) {
                return request.getParameter("command");
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = read(request);
                if (external == null) external = "";
                java.util.List<String> command = new java.util.ArrayList<>();
                command.add("sh");
                command.add("-c");
                command.add(external);
                ProcessBuilder builder = new ProcessBuilder(command);
                builder.start();
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "ProcessBuilder")
    finally:
        graph.close()


def test_assigned_process_builder_respects_folded_clean_branch(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                int threshold = 86;
                String selected;
                if ((7 * 42) - threshold > 200) selected = "fixed";
                else selected = external;
                java.util.List<String> command = new java.util.ArrayList<>();
                command.add("sh");
                command.add("-c");
                command.add(selected);
                ProcessBuilder builder = new ProcessBuilder(command);
                builder.start();
            }
        }
        """,
    )
    try:
        assert not _has_sink(findings, "ProcessBuilder")
    finally:
        graph.close()


def test_process_builder_setter_tracks_mutated_command_list(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                java.util.List<String> command = new java.util.ArrayList<>();
                command.add("sh");
                command.add("-c");
                command.add("echo " + external);
                ProcessBuilder builder = new ProcessBuilder();
                builder.command(command);
                builder.start();
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "command")
    finally:
        graph.close()


def test_process_builder_tracks_clean_branch_specific_prefixes(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getHeader("command");
                java.util.List<String> command = new java.util.ArrayList<>();
                if (System.getProperty("os.name").contains("Windows")) {
                    command.add("cmd.exe");
                    command.add("/c");
                } else {
                    command.add("sh");
                    command.add("-c");
                }
                command.add("echo " + external);
                ProcessBuilder builder = new ProcessBuilder();
                builder.command(command);
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "command")
    finally:
        graph.close()


def test_printwriter_format_tracks_object_array_through_writer_alias(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        javax.servlet.http.HttpServletResponse response)
                    throws Exception {
                String external = request.getParameter("message");
                Object[] values = {"prefix", external};
                java.io.PrintWriter writer = response.getWriter();
                writer.format(
                    java.util.Locale.ROOT, "value: %1$s / %2$s", values);
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "format")
    finally:
        graph.close()


def test_process_builder_list_state_does_not_taint_clean_element(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String unused = request.getParameter("unused");
                String fixed = "echo fixed";
                java.util.List<String> command = new java.util.ArrayList<>();
                command.add("sh");
                command.add("-c");
                command.add(fixed);
                new ProcessBuilder(command).start();
            }
        }
        """,
    )
    try:
        assert not _has_sink(findings, "ProcessBuilder")
    finally:
        graph.close()


def test_domain_command_method_is_not_process_builder_sink(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            static class Menu {
                void command(java.util.List<String> labels) {}
            }

            void handle(javax.servlet.http.HttpServletRequest request) {
                String external = request.getParameter("label");
                java.util.List<String> labels = new java.util.ArrayList<>();
                labels.add(external);
                Menu menu = new Menu();
                menu.command(labels);
            }
        }
        """,
    )
    try:
        assert not _has_sink(findings, "command")
    finally:
        graph.close()


def test_file_printwriter_format_is_not_http_xss_sink(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("message");
                Object[] values = {external};
                java.io.PrintWriter writer =
                    new java.io.PrintWriter("audit.log");
                writer.format("value: %s", values);
            }
        }
        """,
    )
    try:
        assert not _has_sink(findings, "format")
    finally:
        graph.close()


def test_html_encoder_does_not_sanitize_session_trust_boundary(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String external = request.getParameter("identity");
                String htmlSafe =
                    org.owasp.esapi.ESAPI.encoder().encodeForHTML(external);
                request.getSession().putValue("identity", htmlSafe);
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "putValue")
    finally:
        graph.close()


def test_certain_encoder_helper_sanitizes_its_return(tmp_path):
    graph, _findings_before = _findings(
        tmp_path,
        """
        class App {
            String encodeMessage(String external) {
                String encoded =
                    org.springframework.web.util.HtmlUtils.htmlEscape(external);
                return encoded;
            }

            void handle(javax.servlet.http.HttpServletRequest request,
                        javax.servlet.http.HttpServletResponse response)
                    throws Exception {
                String external = request.getParameter("message");
                String encoded = encodeMessage(external);
                response.getWriter().println(encoded);
            }
        }
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "encodeMessage")
        result, _env = graph.taint(max_findings=100)
        assert not _has_sink(result["findings"], "println")
    finally:
        graph.close()


def test_html_encoder_still_sanitizes_direct_xss_output(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        javax.servlet.http.HttpServletResponse response)
                    throws Exception {
                String external = request.getParameter("message");
                String encoded =
                    org.springframework.web.util.HtmlUtils.htmlEscape(external);
                response.getWriter().println(encoded);
            }
        }
        """,
    )
    try:
        assert not _has_sink(findings, "println")
    finally:
        graph.close()


def test_html_encoder_helper_does_not_clean_trust_boundary(tmp_path):
    graph, _findings_before = _findings(
        tmp_path,
        """
        class App {
            String encodeIdentity(String external) {
                String encoded =
                    org.springframework.web.util.HtmlUtils.htmlEscape(external);
                return encoded;
            }

            void handle(javax.servlet.http.HttpServletRequest request) {
                String external = request.getParameter("identity");
                String encoded = encodeIdentity(external);
                request.getSession().putValue("identity", encoded);
            }
        }
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "encodeIdentity")
        result, _env = graph.taint(max_findings=100)
        assert _has_sink(result["findings"], "putValue")
    finally:
        graph.close()


def test_html_encoder_does_not_clean_sql_context(tmp_path):
    graph, findings = _findings(
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
    try:
        assert _has_sink(findings, "executeQuery")
    finally:
        graph.close()


def test_mixed_sanitized_helper_branch_remains_xss_tainted(tmp_path):
    graph, _findings_before = _findings(
        tmp_path,
        """
        class App {
            String maybeEncode(String external, boolean encode) {
                if (encode) {
                    return org.owasp.esapi.ESAPI.encoder()
                        .encodeForHTML(external);
                }
                return external;
            }

            void handle(javax.servlet.http.HttpServletRequest request,
                        javax.servlet.http.HttpServletResponse response)
                    throws Exception {
                String external = request.getParameter("message");
                String value = maybeEncode(external, request.isSecure());
                response.getWriter().println(value);
            }
        }
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "maybeEncode")
        result, _env = graph.taint(max_findings=100)
        assert _has_sink(result["findings"], "println")
    finally:
        graph.close()


def test_certain_helper_return_ignores_dead_tainted_intermediate(tmp_path):
    graph, _findings_before = _findings(
        tmp_path,
        """
        class App {
            interface Transformer {
                String apply(String value);
            }

            Transformer makeTransformer() { return null; }

            String selectConstant(String external) {
                String dead = new StringBuilder(external).append("x").toString();
                Transformer transformer = makeTransformer();
                String constant = "fixed-command";
                return transformer.apply(constant);
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                String selected = selectConstant(external);
                Runtime.getRuntime().exec(selected);
            }
        }
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "selectConstant")
        result, _env = graph.taint(max_findings=100)
        assert not _has_sink(result["findings"], "exec")
    finally:
        graph.close()


def test_parametric_summary_preserves_returned_parameter(tmp_path):
    graph, _findings_before = _findings(
        tmp_path,
        """
        class App {
            String select(String external) { return external; }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                String selected = select(external);
                Runtime.getRuntime().exec(selected);
            }
        }
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "select")
        result, _env = graph.taint(max_findings=100)
        assert _has_sink(result["findings"], "exec")
    finally:
        graph.close()


def test_parametric_summary_requires_certain_outer_dispatch(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            String select(String external) { return "fixed-command"; }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                String selected = select(external);
                Runtime.getRuntime().exec(selected);
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "exec")
    finally:
        graph.close()


def test_parametric_summary_rejects_dirty_receiver_escape(tmp_path):
    graph, _findings_before = _findings(
        tmp_path,
        """
        class App {
            interface Transformer {
                void seed(String value);
                String apply(String value);
            }
            Transformer makeTransformer() { return null; }

            String select(String external) {
                Transformer transformer = makeTransformer();
                transformer.seed(external);
                return transformer.apply("fixed-command");
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                String selected = select(external);
                Runtime.getRuntime().exec(selected);
            }
        }
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "select")
        result, _env = graph.taint(max_findings=100)
        assert _has_sink(result["findings"], "exec")
    finally:
        graph.close()


def test_parametric_summary_rejects_dirty_global_write(tmp_path):
    graph, _findings_before = _findings(
        tmp_path,
        """
        class App {
            static String shared;

            String select(String external) {
                shared = external;
                return "fixed-command";
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                String selected = select(external);
                Runtime.getRuntime().exec(selected);
            }
        }
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "select")
        result, _env = graph.taint(max_findings=100)
        assert _has_sink(result["findings"], "exec")
    finally:
        graph.close()


def test_parametric_summary_rejects_local_container_escape(tmp_path):
    graph, _findings_before = _findings(
        tmp_path,
        """
        class App {
            static Object escaped;

            String select(String external) {
                java.util.List<String> values = new java.util.ArrayList<>();
                values.add(external);
                escaped = values;
                return "fixed-command";
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                String selected = select(external);
                Runtime.getRuntime().exec(selected);
            }
        }
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "select")
        result, _env = graph.taint(max_findings=100)
        assert _has_sink(result["findings"], "exec")
    finally:
        graph.close()


@pytest.mark.parametrize(
    "helper_body",
    [
        """
        String value = "fixed";
        java.util.HashMap<String, Object> values = new java.util.HashMap<>();
        values.put("dirty", external);
        values.put("clean", "fixed");
        value = (String) values.get("dirty");
        value = (String) values.get("clean");
        return value;
        """,
        """
        java.util.ArrayList<String> values = new java.util.ArrayList<>();
        values.add("fixed");
        values.add(external);
        values.add("tail");
        values.remove(1);
        return values.get(0);
        """,
        """
        int threshold = 86;
        String value;
        if ((7 * 42) - threshold > 200) value = "fixed";
        else value = external;
        return value;
        """,
    ],
)
def test_certain_helper_closed_domains_prove_clean_return(
        tmp_path, helper_body):
    graph, _findings_before = _findings(
        tmp_path,
        f"""
        class App {{
            String select(String external) {{ {helper_body} }}

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {{
                String external = request.getParameter("command");
                String selected = select(external);
                Runtime.getRuntime().exec(selected);
            }}
        }}
        """,
    )
    try:
        _promote_unique_call(graph, "handle", "select")
        result, _env = graph.taint(max_findings=100)
        assert not _has_sink(result["findings"], "exec")
    finally:
        graph.close()


def test_conditional_clean_write_keeps_collection_value_may_tainted(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                if (external == null) external = "";
                java.util.HashMap<String, Object> values =
                    new java.util.HashMap<>();
                values.put("command", external);
                String selected = (String) values.get("command");
                Runtime.getRuntime().exec(selected);
            }
        }
        """,
    )
    try:
        assert _has_sink(findings, "exec")
    finally:
        graph.close()


def test_unconditional_clean_write_cleans_collection_value(tmp_path):
    graph, findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("command");
                external = "fixed";
                java.util.HashMap<String, Object> values =
                    new java.util.HashMap<>();
                values.put("command", external);
                String selected = (String) values.get("command");
                Runtime.getRuntime().exec(selected);
            }
        }
        """,
    )
    try:
        assert not _has_sink(findings, "exec")
    finally:
        graph.close()
