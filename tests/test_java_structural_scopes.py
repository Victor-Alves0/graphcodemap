from __future__ import annotations

from codegraph import CodeGraph


def _line(source: str, marker: str) -> int:
    return next(
        number
        for number, text in enumerate(source.splitlines(), start=1)
        if marker in text
    )


def _read_target(graph: CodeGraph, line: int) -> str:
    rows = graph.indexer.conn.execute(
        "SELECT dst.fqn FROM edges e JOIN symbols dst ON dst.id=e.dst "
        "WHERE e.kind='reads' AND e.line=? AND dst.name='item'",
        (line,),
    ).fetchall()
    assert len(rows) == 1
    return rows[0]["fqn"]


def _write_target(graph: CodeGraph, line: int) -> str:
    rows = graph.indexer.conn.execute(
        "SELECT dst.fqn FROM edges e JOIN symbols dst ON dst.id=e.dst "
        "WHERE e.kind='writes' AND e.line=? AND dst.name='item'",
        (line,),
    ).fetchall()
    assert len(rows) == 1
    return rows[0]["fqn"]


def test_java_homonyms_keep_distinct_nodes_and_lexical_uses(tmp_path):
    source = """\
package sample;
class ScopeCases {
  Object item;
  void consume(Object item) {
    use(item); // parameter_use
    use(this.item); // field_use
  }
  void run(Iterable<String> items) {
    { Object item = seed();
      item = mutate(item); // first_block_update
      use(item); } // first_block_use
    use(item); // field_between_blocks
    { Object item = seed(); use(item); } // second_block_use
    for (String item : items) { use(item); } // enhanced_for_use
    try (InputStream item = open();
         InputStream wrapped = wrap(item)) { // later_resource_use
      use(item); // resource_body_use
    } catch (IOException item) {
      use(item); // catch_use
    }
  }
}
"""
    (tmp_path / "ScopeCases.java").write_text(source, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    run = "sample.ScopeCases.run"
    locals_ = graph.indexer.conn.execute(
        "SELECT id, fqn, start_line FROM symbols "
        "WHERE kind='local' AND name='item' AND fqn LIKE ? "
        "ORDER BY start_line",
        (f"{run}.%",),
    ).fetchall()
    assert len(locals_) == 5
    assert len({row["id"] for row in locals_}) == 5
    expected = [f"{run}.item", *[f"{run}.item#{i}" for i in range(2, 6)]]
    assert [row["fqn"] for row in locals_] == expected

    assert _read_target(graph, _line(source, "first_block_use")) == expected[0]
    assert _read_target(graph, _line(source, "first_block_update")) == expected[0]
    assert _write_target(graph, _line(source, "first_block_update")) == expected[0]
    assert _read_target(
        graph, _line(source, "field_between_blocks"),
    ) == "sample.ScopeCases.item"
    assert _read_target(graph, _line(source, "second_block_use")) == expected[1]
    assert _read_target(graph, _line(source, "enhanced_for_use")) == expected[2]
    assert _read_target(graph, _line(source, "later_resource_use")) == expected[3]
    assert _read_target(graph, _line(source, "resource_body_use")) == expected[3]
    assert _read_target(graph, _line(source, "catch_use")) == expected[4]

    # The enhanced-for binding starts at its body, not at the iterable: `items`
    # remains the method parameter.  A catch binding is outside resource scope.
    iterable_target = graph.indexer.conn.execute(
        "SELECT dst.fqn FROM edges e JOIN symbols dst ON dst.id=e.dst "
        "WHERE e.kind='reads' AND e.line=? AND dst.name='items'",
        (_line(source, "enhanced_for_use"),),
    ).fetchone()
    assert iterable_target["fqn"] == f"{run}.items"
    graph.close()


def test_java_field_shadowing_distinguishes_qualified_and_unqualified_uses(tmp_path):
    source = """\
package sample;
class ScopeCases {
  Object item;
  void consume(Object item) {
    use(item); // parameter_use
    use(this.item); // field_use
  }
}
"""
    (tmp_path / "ScopeCases.java").write_text(source, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    assert _read_target(
        graph, _line(source, "parameter_use"),
    ) == "sample.ScopeCases.consume.item"
    assert _read_target(
        graph, _line(source, "field_use"),
    ) == "sample.ScopeCases.item"
    graph.close()


def test_java_homonym_ids_survive_lines_and_unrelated_body_edits(tmp_path):
    path = tmp_path / "Stable.java"
    before = """\
package sample;
class Stable {
  void run() {
    { Object item = first(); use(item); }
    { Object item = second(); use(item); }
  }
}
"""
    after = """\
package sample;
class Stable {

  void run() {
    int unrelated = 1;
    unrelated++;
    { Object item = changedFirst(); use(item); }

    { Object item = changedSecond(); use(item); }
  }
}
"""
    path.write_text(before, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    def item_ids() -> dict[str, str]:
        return {
            row["fqn"]: row["id"]
            for row in graph.indexer.conn.execute(
                "SELECT id, fqn FROM symbols "
                "WHERE kind='local' AND name='item'"
            )
        }

    original = item_ids()
    path.write_text(after, encoding="utf-8")
    graph.indexer.index_file("Stable.java", force=True)
    graph.indexer.resolve_edges()

    assert item_ids() == original
    graph.close()


# L0 has no persistent lexical-scope nodes to anchor two otherwise identical
# declarations.  Consequently, inserting or reordering an *earlier homonym*
# can change the source-order ``#N`` identity.  Line changes, initializer/body
# edits and insertion of differently named declarations do not.  Java pattern
# variables are intentionally outside this small phase-one scope corpus.
