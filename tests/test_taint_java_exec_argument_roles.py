"""Semantic argument-role contracts for Java ``Runtime.exec`` sinks."""

from __future__ import annotations

from codegraph import CodeGraph


def _taint_findings(tmp_path, source: str) -> list[dict]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        return data["findings"]
    finally:
        graph.close()


def _for_sink(findings: list[dict], callee: str) -> list[dict]:
    return [finding for finding in findings
            if finding["sink"]["callee"] == callee]


def test_runtime_exec_reports_tainted_command_argument_zero(tmp_path):
    findings = _taint_findings(
        tmp_path,
        """
        import java.io.File;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String command = request.getParameter("command");
                Runtime.getRuntime().exec(command, null, new File("/tmp"));
            }
        }
        """,
    )

    exec_findings = _for_sink(findings, "exec")
    assert exec_findings
    assert {finding["sink"]["arg_index"] for finding in exec_findings} == {0}


def test_runtime_exec_ignores_tainted_working_directory_but_keeps_file_sink(
        tmp_path):
    findings = _taint_findings(
        tmp_path,
        """
        import java.io.File;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String directory = request.getParameter("directory");
                Runtime.getRuntime().exec(
                    "echo safe", null, new File(directory));
            }
        }
        """,
    )

    assert not _for_sink(findings, "exec")
    file_findings = _for_sink(findings, "File")
    assert file_findings
    assert {finding["sink"]["arg_index"] for finding in file_findings} == {0}


def test_runtime_variable_receiver_keeps_tainted_command_detection(tmp_path):
    findings = _taint_findings(
        tmp_path,
        """
        import java.io.File;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                Runtime runtime = Runtime.getRuntime();
                String command = request.getParameter("command");
                runtime.exec(command, null, new File("/tmp"));
            }
        }
        """,
    )

    exec_findings = _for_sink(findings, "exec")
    assert exec_findings
    assert {finding["sink"]["arg_index"] for finding in exec_findings} == {0}


def test_runtime_exec_array_overload_uses_command_array_at_argument_zero(
        tmp_path):
    findings = _taint_findings(
        tmp_path,
        """
        import java.io.File;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String command = request.getParameter("command");
                String[] commandArray = new String[] {command, "safe"};
                Runtime.getRuntime().exec(
                    commandArray, new String[] {"SAFE=1"}, new File("/tmp"));
            }
        }
        """,
    )

    exec_findings = _for_sink(findings, "exec")
    assert exec_findings
    assert {finding["sink"]["arg_index"] for finding in exec_findings} == {0}


def test_runtime_exec_treats_external_environment_as_execution_risk(tmp_path):
    findings = _taint_findings(
        tmp_path,
        """
        import java.io.File;

        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String external = request.getParameter("external");
                String[] environment = new String[] {"VALUE=" + external};
                Runtime runtime = Runtime.getRuntime();
                runtime.exec("echo safe", environment, new File("/tmp"));
            }
        }
        """,
    )

    exec_findings = _for_sink(findings, "exec")
    assert exec_findings
    assert {finding["sink"]["arg_index"] for finding in exec_findings} == {1}
