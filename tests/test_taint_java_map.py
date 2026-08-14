"""Closed Java Map semantics: literal keys, overwrite and fail-closed edges."""

import pytest

from codegraph import CodeGraph


def _graph(tmp_path, helper_body: str, call_args: str = "input") -> CodeGraph:
    (tmp_path / "App.java").write_text(
        f"""
class App {{
    String select(String input, String... extra) {{
        {helper_body}
    }}

    void handle(javax.servlet.http.HttpServletRequest request) throws Exception {{
        String input = request.getParameter("path");
        String output = select({call_args});
        new java.io.FileInputStream(output);
    }}
}}
""".strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def _has_path_sink(graph: CodeGraph) -> bool:
    data, _env = graph.taint(max_findings=100)
    return any(finding["sink"]["callee"] == "FileInputStream"
               for finding in data["findings"])


def _promote_select(graph: CodeGraph) -> None:
    conn = graph.indexer.conn
    target = conn.execute(
        "SELECT id FROM symbols WHERE name='select' AND kind='method'"
    ).fetchone()
    caller = conn.execute(
        "SELECT id FROM symbols WHERE name='handle' AND kind='method'"
    ).fetchone()
    assert target is not None and caller is not None
    changed = conn.execute(
        "UPDATE edges SET dst=?, confidence='certain', resolver='l1' "
        "WHERE src=? AND kind='calls' "
        "AND (dst_name='select' OR dst_name LIKE '%.select')",
        (target["id"], caller["id"]),
    ).rowcount
    assert changed == 1
    conn.commit()
    graph.query._facts_cache.clear()


@pytest.mark.parametrize("map_type", ["HashMap", "LinkedHashMap", "TreeMap"])
def test_literal_key_safe_overwrite_proves_clean_return(tmp_path, map_type):
    graph = _graph(
        tmp_path,
        f"""
        java.util.Map<String, Object> values =
                new java.util.{map_type}<String, Object>();
        values.put("slot", input);
        values.put("slot", "safe.txt");
        return (String) values.get("slot");
        """,
    )
    try:
        assert _has_path_sink(graph)  # unresolved calls remain conservative
        _promote_select(graph)
        assert not _has_path_sink(graph)
    finally:
        graph.close()


def test_literal_key_dirty_overwrite_preserves_vulnerability(tmp_path):
    graph = _graph(
        tmp_path,
        """
        java.util.Map<String, Object> values = new java.util.HashMap<>();
        values.put("slot", "safe.txt");
        values.put("slot", input);
        return (String) values.get("slot");
        """,
    )
    try:
        _promote_select(graph)
        assert _has_path_sink(graph)
    finally:
        graph.close()


def test_literal_key_remove_clears_entry(tmp_path):
    graph = _graph(
        tmp_path,
        """
        java.util.Map<String, Object> values = new java.util.HashMap<>();
        values.put("slot", input);
        values.remove("slot");
        return (String) values.get("slot");
        """,
    )
    try:
        _promote_select(graph)
        assert not _has_path_sink(graph)
    finally:
        graph.close()


def test_dynamic_key_fails_closed(tmp_path):
    graph = _graph(
        tmp_path,
        """
        java.util.Map<String, Object> values = new java.util.HashMap<>();
        values.put("dirty", input);
        values.put("safe", "safe.txt");
        return (String) values.get(extra[0]);
        """,
        'input, "safe"',
    )
    try:
        _promote_select(graph)
        # Runtime key is safe, but the summary deliberately refuses to prove
        # a dynamic lookup clean.
        assert _has_path_sink(graph)
    finally:
        graph.close()


def test_map_alias_fails_closed(tmp_path):
    graph = _graph(
        tmp_path,
        """
        java.util.Map<String, Object> values = new java.util.HashMap<>();
        values.put("slot", input);
        java.util.Map<String, Object> alias = values;
        alias.put("slot", "safe.txt");
        return (String) alias.get("slot");
        """,
    )
    try:
        _promote_select(graph)
        assert _has_path_sink(graph)
    finally:
        graph.close()


def test_conditional_safe_overwrite_fails_closed(tmp_path):
    graph = _graph(
        tmp_path,
        """
        java.util.Map<String, Object> values = new java.util.HashMap<>();
        values.put("slot", input);
        if (extra.length > 0) {
            values.put("slot", "safe.txt");
        }
        return (String) values.get("slot");
        """,
        'input, "overwrite"',
    )
    try:
        _promote_select(graph)
        assert _has_path_sink(graph)
    finally:
        graph.close()


@pytest.mark.parametrize(
    "uncertain_step",
    [
        'values.replace("slot", "safe.txt");',
        "normalize(values);",
        'normalize(values, values.get("slot"));',
    ],
    ids=["unknown-method", "escape", "mixed-escape-and-read"],
)
def test_unknown_operation_or_escape_fails_closed(tmp_path, uncertain_step):
    graph = _graph(
        tmp_path,
        f"""
        java.util.Map<String, Object> values = new java.util.HashMap<>();
        values.put("slot", input);
        {uncertain_step}
        return (String) values.get("slot");
        """,
    )
    try:
        _promote_select(graph)
        assert _has_path_sink(graph)
    finally:
        graph.close()


def test_direct_nested_get_propagates_taint(tmp_path):
    (tmp_path / "App.java").write_text(
        """
class App {
    void handle(javax.servlet.http.HttpServletRequest request) throws Exception {
        String input = request.getParameter("path");
        java.util.Map<String, Object> values = new java.util.HashMap<>();
        values.put("slot", input);
        new java.io.FileInputStream((String) values.get("slot"));
    }
}
""".strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        assert _has_path_sink(graph)
    finally:
        graph.close()
