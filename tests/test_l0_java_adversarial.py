"""Regressões adversariais do extractor/resolver Java L0."""

from __future__ import annotations

import textwrap

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(source: str, module: str = "app"):
    data = textwrap.dedent(source).encode()
    return extract("java", data, module, get_parser("java").parse(data))


def _calls(source: str):
    return [r for r in _extract(source)[1] if r.kind == "calls"]


def _graph(tmp_path, files: dict[str, str]) -> CodeGraph:
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def test_wildcard_import_does_not_create_package_alias():
    calls = _calls("""
        import java.util.*;
        class C { void f() { util.run(); } }
    """)
    assert {r.dst_name for r in calls} == {"run"}


def test_calls_on_constructor_static_type_and_fqn_are_qualified():
    calls = _calls("""
        class C { void f() {
          new Widget().run();
          Helper.start();
          a.b.Tools.stop();
        } }
    """)
    names = {r.dst_name for r in calls}
    assert {"Widget", "Widget.run", "Helper.start", "a.b.Tools.stop"} <= names


def test_syntactically_proven_local_types_are_used():
    names = {r.dst_name for r in _calls("""
        class C {
          void f(java.util.List<Svc> values) {
            var made = new Svc(); made.run();
            for (Svc item : values) item.run();
            try (Reader reader = open()) { reader.read(); }
            catch (Problem problem) { problem.report(); }
          }
        }
    """)}
    assert {"Svc.run", "Reader.read", "Problem.report"} <= names


def test_method_and_constructor_references_are_calls():
    names = {r.dst_name for r in _calls("""
        import a.b.Factory;
        class C {
          Worker worker;
          void f() {
            use(Factory::new);
            use(Factory::build);
            use(worker::run);
            use(this::finish);
          }
          void finish() {}
        }
    """)}
    assert {"a.b.Factory", "a.b.Factory.build", "Worker.run", "finish"} <= names


def test_compact_record_constructor_is_a_method_with_parent():
    syms, refs = _extract("record R(int x) { R { check(x); } }")
    ctor = next(s for s in syms if s.kind == "method" and s.name == "R")
    assert ctor.fqn == "app.R.R"
    assert ctor.parent_fqn == "app.R"
    assert any(r.src_fqn == ctor.fqn and r.dst_name == "check" for r in refs)


def test_reference_span_points_at_target_name_not_receiver():
    calls = _calls("""
        class C {
          void f() {
            receiver
              .execute();
          }
        }
    """)
    call = next(r for r in calls if r.dst_name == "execute")
    assert (call.line, call.col) == (5, 7)


def test_imported_java_targets_resolve_through_source_root(tmp_path):
    graph = _graph(tmp_path, {
        "src/main/java/a/b/Widget.java": """
            package a.b;
            public class Widget {
              public Widget() {}
              public static void build() {}
              public void run() {}
            }
        """,
        "src/main/java/client/Use.java": """
            package client;
            import a.b.Widget;
            import static a.b.Widget.build;
            class Use { void go() { new Widget().run(); build(); } }
        """,
    })
    rows = graph.indexer.conn.execute(
        "SELECT e.dst_name, d.kind, d.fqn, e.confidence "
        "FROM edges e LEFT JOIN symbols d ON d.id=e.dst "
        "WHERE e.kind='calls' AND e.dst_name IN "
        "('a.b.Widget','a.b.Widget.run','a.b.Widget.build')"
    ).fetchall()
    by_name = {r["dst_name"]: dict(r) for r in rows}
    assert by_name["a.b.Widget"]["kind"] == "class"
    assert by_name["a.b.Widget.build"]["fqn"].endswith(".Widget.build")
    assert by_name["a.b.Widget.run"]["fqn"].endswith(".Widget.run")
    assert all(r["confidence"] == "inferred" for r in by_name.values())
    graph.close()


def test_overloads_keep_parent_and_reference_ownership(tmp_path):
    graph = _graph(tmp_path, {"C.java": """
        class C {
          void first() {}
          void second() {}
          void f() { first(); class Local {} }
          void f(int value) { second(); class Local {} }
        }
    """})
    methods = graph.indexer.conn.execute(
        "SELECT id, signature, parent_id FROM symbols "
        "WHERE kind='method' AND name='f' ORDER BY start_line"
    ).fetchall()
    assert len(methods) == 2
    assert all(r["parent_id"] for r in methods)

    owners = graph.indexer.conn.execute(
        "SELECT e.dst_name, s.signature FROM edges e "
        "JOIN symbols s ON s.id=e.src "
        "WHERE e.kind='calls' AND e.dst_name IN ('first','second')"
    ).fetchall()
    assert {(r["dst_name"], r["signature"]) for r in owners} == {
        ("first", "void f()"), ("second", "void f(int value)"),
    }

    locals_ = graph.indexer.conn.execute(
        "SELECT l.start_line, p.signature parent_signature "
        "FROM symbols l JOIN symbols p ON p.id=l.parent_id "
        "WHERE l.kind='class' AND l.name='Local' ORDER BY l.start_line"
    ).fetchall()
    assert [r["parent_signature"] for r in locals_] == [
        "void f()", "void f(int value)",
    ]
    graph.close()
