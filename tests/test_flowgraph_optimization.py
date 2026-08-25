from __future__ import annotations

from codegraph import CodeGraph
from codegraph import flowgraph


def test_batched_callable_locator_matches_legacy_facts(tmp_path):
    (tmp_path / "app.py").write_text(
        "def outer(value):\n"
        "    def inner(item):\n"
        "        return item\n"
        "    return inner(value)\n",
        encoding="utf-8",
    )
    (tmp_path / "App.java").write_text(
        "class App { "
        "String first(String value) { return value; } "
        "String second(String value) { return first(value); } "
        "}\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    engine = graph.query
    functions = flowgraph._callable_rows(engine.conn)

    optimized = []
    parse_cache: dict = {}
    locator_cache: dict = {}
    for function in functions:
        optimized.append(flowgraph._function_facts(
            engine, function, parse_cache, locator_cache))

    engine._facts_cache.clear()
    legacy = []
    parse_cache = {}
    for function in functions:
        legacy.append(engine._df_facts(function, parse_cache))

    assert optimized == legacy
    graph.close()


def test_function_builder_flushes_nodes_before_edges():
    class RecordingConnection:
        def __init__(self):
            self.calls = []

        def executemany(self, sql, rows):
            self.calls.append((sql, list(rows)))

    builder = object.__new__(flowgraph._FunctionBuilder)
    builder.conn = RecordingConnection()
    builder._node_rows = [("node",)]
    builder._edge_rows = [("edge",)]

    builder._flush()

    assert len(builder.conn.calls) == 2
    assert "INSERT INTO dataflow_nodes" in builder.conn.calls[0][0]
    assert builder.conn.calls[0][1] == [("node",)]
    assert "INSERT INTO dataflow_edges" in builder.conn.calls[1][0]
    assert builder.conn.calls[1][1] == [("edge",)]


def test_dataflow_file_foreign_keys_are_indexed(tmp_path):
    graph = CodeGraph(tmp_path)
    expected = {
        "dataflow_nodes": "idx_dataflow_nodes_file",
        "dataflow_edges": "idx_dataflow_edges_file",
        "dataflow_function_state": "idx_dataflow_function_state_file",
    }

    for table, index in expected.items():
        indexes = {
            row["name"] for row in graph.indexer.conn.execute(
                f"PRAGMA index_list({table})")
        }
        assert index in indexes
    graph.close()
