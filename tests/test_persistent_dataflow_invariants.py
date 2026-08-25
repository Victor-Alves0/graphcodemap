"""Focused G3 persistence invariants found by an independent audit."""

from __future__ import annotations

from codegraph import CodeGraph
from codegraph.db import record_current_stage, write_l1_lifecycle


PYTHON_FLOW = """\
def consume(value):
    return value


def handle(request):
    local = request
    return consume(local)
"""


def _flow_ids(graph: CodeGraph) -> tuple[set[str], set[str]]:
    nodes = {
        row["id"]
        for row in graph.indexer.conn.execute("SELECT id FROM dataflow_nodes")
    }
    edges = {
        row["id"]
        for row in graph.indexer.conn.execute("SELECT id FROM dataflow_edges")
    }
    return nodes, edges


def test_persistent_ids_are_deterministic_for_the_same_revision(tmp_path):
    (tmp_path / "app.py").write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow(force=True)
    first = _flow_ids(graph)

    graph.build_dataflow(force=True)

    assert _flow_ids(graph) == first
    graph.close()


def test_persistent_path_cycle_does_not_revisit_a_node(tmp_path):
    (tmp_path / "app.py").write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()
    conn = graph.indexer.conn
    parameter = conn.execute(
        "SELECT n.id,n.file_id,s.parent_id FROM dataflow_nodes n "
        "JOIN symbols s ON s.id=n.symbol_id "
        "WHERE s.fqn='app.handle.request'"
    ).fetchone()
    local = conn.execute(
        "SELECT n.id FROM dataflow_nodes n JOIN symbols s ON s.id=n.symbol_id "
        "WHERE s.fqn='app.handle.local'"
    ).fetchone()
    conn.execute(
        "INSERT INTO dataflow_edges(id,owner_function_id,src_node_id,dst_node_id,"
        "file_id,relation,line,col,confidence,interprocedural,evidence_json) "
        "VALUES('audit-cycle',?,?,?,?, 'assignment',NULL,NULL,'certain',0,'{}')",
        (parameter["parent_id"], local["id"], parameter["id"],
         parameter["file_id"]),
    )
    conn.commit()

    _, result, _ = graph.flow_path(
        "app.handle.request", "app.handle.local", max_hops=64, max_paths=10,
    )

    assert result["paths"]
    assert all(
        len({node["id"] for node in path["nodes"]}) == len(path["nodes"])
        for path in result["paths"]
    )
    graph.close()


def test_homonymous_java_access_paths_do_not_collapse(tmp_path):
    (tmp_path / "App.java").write_text(
        "class App {\n"
        "  static class Box { String value; }\n"
        "  void use(String value) {}\n"
        "  void run() {\n"
        "    { Box item = new Box(); use(item.value); }\n"
        "    { Box item = new Box(); use(item.value); }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()

    rows = graph.indexer.conn.execute(
        "SELECT id,details_json FROM dataflow_nodes "
        "WHERE kind='value' AND access_path='item.value'"
    ).fetchall()

    assert len(rows) == 2
    graph.close()


def test_target_path_is_not_hidden_by_unrelated_path_prefixes(tmp_path):
    (tmp_path / "app.py").write_text(
        "def source_fn(source):\n"
        "    return source\n\n"
        "def target_fn(target):\n"
        "    return target\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()
    conn = graph.indexer.conn
    source = conn.execute(
        "SELECT n.id,n.file_id,s.parent_id FROM dataflow_nodes n "
        "JOIN symbols s ON s.id=n.symbol_id "
        "WHERE s.fqn='app.source_fn.source'"
    ).fetchone()
    target = conn.execute(
        "SELECT n.id FROM dataflow_nodes n JOIN symbols s ON s.id=n.symbol_id "
        "WHERE s.fqn='app.target_fn.target'"
    ).fetchone()

    previous = [source["id"]]
    edge_number = 0
    for level in range(13):
        current = [f"audit-node-{level}-{branch}" for branch in range(2)]
        conn.executemany(
            "INSERT INTO dataflow_nodes(id,function_id,symbol_id,file_id,kind,"
            "name,access_path,line,col,content_hash,details_json) "
            "VALUES(?,?,NULL,?,'value',?,NULL,NULL,NULL,'audit','{}')",
            [(node_id, source["parent_id"], source["file_id"], node_id)
             for node_id in current],
        )
        for src in previous:
            for dst in current:
                conn.execute(
                    "INSERT INTO dataflow_edges(id,owner_function_id,src_node_id,"
                    "dst_node_id,file_id,relation,line,col,confidence,"
                    "interprocedural,evidence_json) "
                    "VALUES(?,?,?,?,?,'assignment',NULL,NULL,'certain',0,'{}')",
                    (f"audit-edge-{edge_number}", source["parent_id"], src,
                     dst, source["file_id"]),
                )
                edge_number += 1
        previous = current
    for src in previous:
        conn.execute(
            "INSERT INTO dataflow_edges(id,owner_function_id,src_node_id,"
            "dst_node_id,file_id,relation,line,col,confidence,"
            "interprocedural,evidence_json) "
            "VALUES(?,?,?,?,?,'assignment',NULL,NULL,'certain',0,'{}')",
            (f"audit-edge-{edge_number}", source["parent_id"], src,
             target["id"], source["file_id"]),
        )
        edge_number += 1
    conn.commit()

    _, result, _ = graph.flow_path(
        "app.source_fn.source", "app.target_fn.target",
        max_hops=32, max_paths=1,
    )

    assert result["paths"]
    graph.close()


def test_build_marker_cannot_republish_stale_l1_flow_as_complete(tmp_path):
    (tmp_path / "pom.xml").write_text("<project>one</project>\n", encoding="utf-8")
    (tmp_path / "App.java").write_text(
        "class App {\n"
        "  String consume(String value) { return value; }\n"
        "  String handle(String request) { return consume(request); }\n"
        "}\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    conn = graph.indexer.conn
    conn.execute(
        "UPDATE edges SET resolver='l1',confidence='certain' "
        "WHERE kind='calls' AND dst IS NOT NULL"
    )
    write_l1_lifecycle(conn, {
        "status": "complete", "published": True,
    })
    record_current_stage(
        conn, "l1", "resolver-set", "complete", {}, commit=False)
    conn.commit()
    graph.build_dataflow(force=True)

    (tmp_path / "pom.xml").write_text(
        "<project>two</project>\n", encoding="utf-8",
    )
    graph.index()
    assert graph.l1_status()["status"] == "not_started"
    assert graph.doctor()["dataflow"]["status"] == "dirty"

    rebuilt, _ = graph.build_dataflow()

    assert rebuilt["status"] == "partial"
    assert rebuilt["queryable"] is False
    assert rebuilt["reason"] == "awaiting_l1_revalidation"
    graph.close()


def test_value_graph_foreign_key_cleanup_uses_file_indexes(tmp_path):
    (tmp_path / "app.py").write_text(PYTHON_FLOW, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.build_dataflow()

    plans = [
        row["detail"]
        for row in graph.indexer.conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM files WHERE id=-1")
    ]

    assert any("idx_dataflow_nodes_file" in detail for detail in plans)
    assert any("idx_dataflow_edges_file" in detail for detail in plans)
    assert any("idx_dataflow_function_state_file" in detail for detail in plans)
    graph.close()
