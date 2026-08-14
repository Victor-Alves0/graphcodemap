"""Return summaries must not turn semantic call certainty into flow certainty."""

from codegraph import CodeGraph


def _sinks(graph):
    return {finding["sink"]["callee"] for finding in graph.taint()[0]["findings"]}


def test_certain_java_interface_target_does_not_kill_taint(tmp_path):
    (tmp_path / "App.java").write_text(
        """
interface Filter {
    String apply(String value);
}
class App {
    void handle(javax.servlet.http.HttpServletRequest request,
                java.sql.Statement statement) throws Exception {
        String input = request.getParameter("x");
        Filter filter = null;
        String output = filter.apply(input);
        statement.execute(output);
    }
}
""".strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        assert "execute" in _sinks(graph)

        conn = graph.indexer.conn
        interface = conn.execute(
            "SELECT id FROM symbols WHERE name='apply' AND kind='method'"
        ).fetchone()
        caller = conn.execute(
            "SELECT id FROM symbols WHERE name='handle' AND kind='method'"
        ).fetchone()
        assert interface is not None and caller is not None
        # Simula a definição correta devolvida pelo JDTLS para uma chamada
        # via interface. A declaração é semanticamente certa, mas o corpo em
        # runtime pertence à implementação e não pode ser resumido daqui.
        conn.execute(
            "UPDATE edges SET dst=?, confidence='certain', resolver='l1' "
            "WHERE src=? AND kind='calls' AND line=9",
            (interface["id"], caller["id"]),
        )
        conn.commit()
        graph.query._facts_cache.clear()

        assert "execute" in _sinks(graph)
    finally:
        graph.close()


def _promote_call(graph, caller: str, target: str, line: int) -> None:
    conn = graph.indexer.conn
    dst = conn.execute(
        "SELECT child.id FROM symbols child JOIN symbols parent "
        "ON child.parent_id=parent.id WHERE child.name=? AND child.kind='method' "
        "AND parent.name='App'",
        (target,),
    ).fetchone()
    src = conn.execute(
        "SELECT id FROM symbols WHERE name=? AND kind='method'", (caller,)
    ).fetchone()
    assert dst is not None and src is not None
    conn.execute(
        "UPDATE edges SET dst=?, confidence='certain', resolver='l1' "
        "WHERE src=? AND kind='calls' AND line=?",
        (dst["id"], src["id"], line),
    )
    conn.commit()


def test_certain_java_constant_branch_can_prove_clean_return(tmp_path):
    (tmp_path / "App.java").write_text(
        """
class App {
    String chooseValue(String input) {
        int number = 86;
        String output;
        if ((7 * 42) - number > 200) output = "safe";
        else output = input;
        return output;
    }
    void handle(javax.servlet.http.HttpServletRequest request,
                java.sql.Statement statement) throws Exception {
        String input = request.getParameter("x");
        String output = chooseValue(input);
        statement.execute(output);
    }
}
""".strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        assert "execute" in _sinks(graph)
        _promote_call(graph, "handle", "chooseValue", 12)
        graph.query._facts_cache.clear()
        assert "execute" not in _sinks(graph)
    finally:
        graph.close()


def test_certain_java_collection_alias_stays_conservative(tmp_path):
    (tmp_path / "App.java").write_text(
        """
class App {
    String throughList(String input) {
        java.util.List<String> values = new java.util.ArrayList<String>();
        values.add("safe");
        values.add(input);
        return values.get(1);
    }
    void handle(javax.servlet.http.HttpServletRequest request,
                java.sql.Statement statement) throws Exception {
        String input = request.getParameter("x");
        String output = throughList(input);
        statement.execute(output);
    }
}
""".strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        assert "execute" in _sinks(graph)
        _promote_call(graph, "handle", "throughList", 11)
        graph.query._facts_cache.clear()
        assert "execute" in _sinks(graph)
    finally:
        graph.close()
