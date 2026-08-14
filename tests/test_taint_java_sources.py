"""Precise Java input sources used by ordinary file/network applications."""

from codegraph import CodeGraph


def _path_sinks(tmp_path, source: str) -> set[str]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        return {finding["sink"]["callee"] for finding in data["findings"]}
    finally:
        graph.close()


def test_line_reader_return_is_external_input(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            void handle(java.io.BufferedReader reader) throws Exception {
                String path = reader.readLine();
                new java.io.FileInputStream(path);
            }
        }
        """,
    )
    assert "FileInputStream" in sinks


def test_line_reader_source_still_respects_sanitizer(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            void handle(java.io.BufferedReader reader) throws Exception {
                String path = escape(reader.readLine());
                new java.io.FileInputStream(path);
            }
        }
        """,
    )
    assert "FileInputStream" not in sinks


def test_line_reader_field_is_resolved_by_declared_type(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            private java.io.BufferedReader reader;

            void handle() throws Exception {
                String path = this.reader.readLine();
                new java.io.FileInputStream(path);
            }
        }
        """,
    )
    assert "FileInputStream" in sinks


def test_domain_object_read_line_is_not_external_input(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            void handle(Settings settings) throws Exception {
                String path = settings.readLine();
                new java.io.FileInputStream(path);
            }
        }
        class Settings {
            String readLine() { return "bundled.txt"; }
        }
        """,
    )
    assert "FileInputStream" not in sinks


def test_system_property_is_qualified_external_input(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            void handle() throws Exception {
                String path = System.getProperty("download.path");
                new java.io.FileInputStream(path);
            }
        }
        """,
    )
    assert "FileInputStream" in sinks


def test_domain_object_get_property_is_not_a_source(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            void handle(Settings settings) throws Exception {
                String path = settings.getProperty("bundled.path");
                new java.io.FileInputStream(path);
            }
        }
        class Settings {
            String getProperty(String name) { return "safe.txt"; }
        }
        """,
    )
    assert "FileInputStream" not in sinks


def test_constructor_receiver_resolves_the_exact_source_wrapper(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String path = new RequestSource().readPath(request);
                new java.io.FileInputStream(path);
            }
        }
        class RequestSource {
            String readPath(javax.servlet.http.HttpServletRequest request) {
                return request.getParameter("path");
            }
        }
        class HarmlessSource {
            String readPath(javax.servlet.http.HttpServletRequest request) {
                return "bundled.txt";
            }
        }
        """,
    )
    assert "FileInputStream" in sinks


def test_source_nested_in_constructor_taints_the_created_container(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                java.util.StringTokenizer tokens = new java.util.StringTokenizer(
                    request.getQueryString(), "&");
                String path = tokens.nextToken().substring(3);
                new java.io.FileInputStream(path);
            }
        }
        """,
    )
    assert "FileInputStream" in sinks


def test_sanitizer_cuts_a_source_nested_inside_constructor(tmp_path):
    sinks = _path_sinks(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                java.util.StringTokenizer tokens = new java.util.StringTokenizer(
                    escape(request.getQueryString()), "&");
                String path = tokens.nextToken().substring(3);
                new java.io.FileInputStream(path);
            }
        }
        """,
    )
    assert "FileInputStream" not in sinks
