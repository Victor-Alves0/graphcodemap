"""Argument-sensitive trust contracts for configured Java sources."""

from __future__ import annotations

import pytest

from codegraph import CodeGraph


def _findings(tmp_path, source: str) -> list[dict]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        result, _env = graph.taint(max_findings=100)
        return result["findings"]
    finally:
        graph.close()


def _sinks(findings: list[dict]) -> set[str]:
    return {finding["sink"]["callee"] for finding in findings}


@pytest.mark.parametrize(
    "members",
    [
        """
        void handle() {
            new java.io.File(System.getProperty("user.dir"));
        }
        """,
        """
        void handle() {
            String root = System.getProperty("user.dir");
            new java.io.File(root);
        }
        """,
        """
        String root() {
            return System.getProperty("user.dir");
        }
        void handle() {
            new java.io.File(root());
        }
        """,
        """
        private String root;
        void store() {
            this.root = System.getProperty("user.dir");
        }
        void consume() {
            new java.io.File(this.root);
        }
        void handle() {
            store();
            consume();
        }
        """,
    ],
    ids=["direct", "assignment", "return-wrapper", "receiver-heap"],
)
def test_user_dir_is_clean_across_java_source_paths(tmp_path, members):
    findings = _findings(tmp_path, f"class App {{ {members} }}")

    assert "File" not in _sinks(findings)


def test_user_dir_is_clean_for_file_nested_in_runtime_exec(tmp_path):
    findings = _findings(
        tmp_path,
        """
        class App {
            void handle() throws Exception {
                Runtime runtime = Runtime.getRuntime();
                runtime.exec(
                    "echo safe", null,
                    new java.io.File(System.getProperty("user.dir")));
            }
        }
        """,
    )

    assert not ({"exec", "File"} & _sinks(findings))


@pytest.mark.parametrize("key", ["user.home", "application.upload.root"])
def test_other_literal_system_properties_remain_sources(tmp_path, key):
    findings = _findings(
        tmp_path,
        f"""
        class App {{
            void handle() {{
                new java.io.File(System.getProperty("{key}"));
            }}
        }}
        """,
    )

    assert "File" in _sinks(findings)


def test_dynamic_system_property_key_remains_a_source(tmp_path):
    findings = _findings(
        tmp_path,
        """
        class App {
            void handle(String key) {
                new java.io.File(System.getProperty(key));
            }
        }
        """,
    )

    assert "File" in _sinks(findings)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "global System.setProperty state is not modeled yet; an in-process "
        "tainted write must eventually override literal trust"
    ),
)
def test_tainted_setproperty_eventually_overrides_user_dir_trust(tmp_path):
    findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String external = request.getParameter("directory");
                System.setProperty("user.dir", external);
                new java.io.File(System.getProperty("user.dir"));
            }
        }
        """,
    )

    assert "File" in _sinks(findings)
