from __future__ import annotations

from codegraph import CodeGraph


def _edge_projection(graph: CodeGraph) -> set[tuple[str, str | None, str | None, str]]:
    return {
        (row["kind"], row["src_fqn"], row["dst_fqn"], row["confidence"])
        for row in graph.indexer.conn.execute(
            "SELECT e.kind, src.fqn src_fqn, dst.fqn dst_fqn, e.confidence "
            "FROM edges e LEFT JOIN symbols src ON src.id=e.src "
            "LEFT JOIN symbols dst ON dst.id=e.dst"
        )
    }


def test_python_persists_parameters_locals_and_value_accesses(tmp_path):
    (tmp_path / "service.py").write_text(
        "def calculate(a: int, b=1):\n"
        "    total = a\n"
        "    total += b\n"
        "    return total\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()

    variables = [dict(row) for row in graph.indexer.conn.execute(
        "SELECT child.kind, child.name, parent.fqn parent_fqn "
        "FROM symbols child JOIN symbols parent ON parent.id=child.parent_id "
        "WHERE child.kind IN ('parameter','local') ORDER BY child.start_line, child.start_col"
    )]
    edges = _edge_projection(graph)

    assert variables == [
        {"kind": "parameter", "name": "a", "parent_fqn": "service.calculate"},
        {"kind": "parameter", "name": "b", "parent_fqn": "service.calculate"},
        {"kind": "local", "name": "total", "parent_fqn": "service.calculate"},
    ]
    for name in ("a", "b", "total"):
        target = f"service.calculate.{name}"
        assert ("contains", "service.calculate", target, "certain") in edges
        assert ("defines", "service.calculate", target, "certain") in edges
        assert ("writes", "service.calculate", target, "certain") in edges
        assert ("reads", "service.calculate", target, "certain") in edges
    assert ("returns", "service.calculate.total", "service.calculate", "certain") in edges
    graph.close()


def test_java_persists_parameters_locals_fields_and_value_accesses(tmp_path):
    (tmp_path / "Calculator.java").write_text(
        "package app;\n"
        "class Calculator {\n"
        "  int offset;\n"
        "  int calculate(int value) {\n"
        "    int total = value;\n"
        "    total += offset;\n"
        "    return total;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()

    variables = [dict(row) for row in graph.indexer.conn.execute(
        "SELECT child.kind, child.name, parent.fqn parent_fqn "
        "FROM symbols child JOIN symbols parent ON parent.id=child.parent_id "
        "WHERE child.kind IN ('parameter','local') ORDER BY child.start_line, child.start_col"
    )]
    edges = _edge_projection(graph)

    assert variables == [
        {"kind": "parameter", "name": "value",
         "parent_fqn": "app.Calculator.calculate"},
        {"kind": "local", "name": "total",
         "parent_fqn": "app.Calculator.calculate"},
    ]
    method = "app.Calculator.calculate"
    for name in ("value", "total"):
        target = f"{method}.{name}"
        assert ("contains", method, target, "certain") in edges
        assert ("defines", method, target, "certain") in edges
        assert ("writes", method, target, "certain") in edges
        assert ("reads", method, target, "certain") in edges
    assert ("reads", method, "app.Calculator.offset", "certain") in edges
    assert ("returns", f"{method}.total", method, "certain") in edges
    graph.close()


def test_structural_edges_are_idempotent_after_single_file_reindex(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("def run(value):\n    copy = value\n    return copy\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    before = graph.indexer.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    graph.indexer.index_file("module.py", force=True)
    graph.indexer.resolve_edges()
    after = graph.indexer.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    assert after == before
    graph.close()


def test_python_instance_attribute_is_a_persistent_field(tmp_path):
    (tmp_path / "service.py").write_text(
        "class Service:\n"
        "    def store(self, value):\n"
        "        self.item = value\n"
        "        return self.item\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    field = graph.indexer.conn.execute(
        "SELECT child.fqn, parent.fqn parent_fqn FROM symbols child "
        "JOIN symbols parent ON parent.id=child.parent_id "
        "WHERE child.kind='field' AND child.name='item'"
    ).fetchone()
    edges = _edge_projection(graph)

    assert dict(field) == {
        "fqn": "service.Service.item", "parent_fqn": "service.Service"}
    assert ("writes", "service.Service.store", "service.Service.item", "certain") in edges
    assert ("reads", "service.Service.store", "service.Service.item", "certain") in edges
    graph.close()


def test_python_property_is_normalized_in_persistent_graph(tmp_path):
    (tmp_path / "model.py").write_text(
        "class Model:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return self._value\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()

    prop = graph.indexer.conn.execute(
        "SELECT kind, fqn FROM symbols WHERE name='value'"
    ).fetchone()
    self_param = graph.indexer.conn.execute(
        "SELECT kind, fqn FROM symbols WHERE name='self'"
    ).fetchone()

    assert tuple(prop) == ("property", "model.Model.value")
    assert tuple(self_param) == ("parameter", "model.Model.value.self")
    graph.close()
