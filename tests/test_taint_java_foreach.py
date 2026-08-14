"""Java enhanced-for bindings must preserve element-level taint."""

from codegraph import CodeGraph


def _sink_names(tmp_path, source: str) -> set[str]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        return {finding["sink"]["callee"] for finding in data["findings"]}
    finally:
        graph.close()


def test_request_derived_iterable_taints_enhanced_for_element(tmp_path):
    sinks = _sink_names(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                javax.servlet.http.Cookie[] cookies = request.getCookies();
                for (javax.servlet.http.Cookie cookie : cookies) {
                    String path = cookie.getValue();
                    new java.io.FileInputStream(path);
                }
            }
        }
        """,
    )
    assert "FileInputStream" in sinks


def test_safe_iterable_keeps_enhanced_for_element_clean(tmp_path):
    sinks = _sink_names(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                // Keep an unrelated source in the same method: only the
                // iterable selected by the loop may taint its element.
                javax.servlet.http.Cookie[] ignored = request.getCookies();
                javax.servlet.http.Cookie[] cookies = {
                    new javax.servlet.http.Cookie("name", "safe.txt")
                };
                for (javax.servlet.http.Cookie cookie : cookies) {
                    String path = cookie.getValue();
                    new java.io.FileInputStream(path);
                }
            }
        }
        """,
    )
    assert "FileInputStream" not in sinks


def test_later_safe_foreach_binding_kills_prior_element_taint(tmp_path):
    sinks = _sink_names(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                javax.servlet.http.Cookie[] dirty = request.getCookies();
                for (javax.servlet.http.Cookie cookie : dirty) {
                    audit(cookie.getValue());
                }

                javax.servlet.http.Cookie[] safe = {
                    new javax.servlet.http.Cookie("name", "safe.txt")
                };
                for (javax.servlet.http.Cookie cookie : safe) {
                    String path = cookie.getValue();
                    new java.io.FileInputStream(path);
                }
            }

            void audit(String value) {}
        }
        """,
    )
    assert "FileInputStream" not in sinks
