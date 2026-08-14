"""Contratos L0 Java encontrados na revisão de integração.

Os casos cobrem falhas encontradas na fronteira entre extração e resolução.
"""

from __future__ import annotations

import textwrap

from codegraph import CodeGraph


def _graph(tmp_path, source: str) -> CodeGraph:
    (tmp_path / "C.java").write_text(textwrap.dedent(source), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def test_java_l0_overloads_fail_closed_instead_of_choosing_arbitrarily(tmp_path):
    graph = _graph(tmp_path, """
        class C {
          void f() {}
          void f(int value) {}
          void go() { new C().f(); new C().f(1); }
        }
    """)
    rows = graph.indexer.conn.execute(
        "SELECT e.col, e.confidence, d.signature "
        "FROM edges e JOIN symbols d ON d.id=e.dst "
        "WHERE e.kind='calls' AND e.dst_name='C.f' ORDER BY e.col, d.signature"
    ).fetchall()
    graph.close()

    by_site: dict[int, list] = {}
    for row in rows:
        by_site.setdefault(row["col"], []).append(row)
    assert len(by_site) == 2
    assert all({r["signature"] for r in site} == {"void f()", "void f(int value)"}
               for site in by_site.values())
    assert all(r["confidence"] == "possible" for r in rows)


def test_java_l0_uppercase_local_receiver_does_not_fabricate_static_target(tmp_path):
    graph = _graph(tmp_path, """
        class Service { void run() {} }
        class Worker { void run() {} }
        class Use {
          Worker make() { return new Worker(); }
          void go() { var Service = make(); Service.run(); }
          void ref() { var Service = make(); use(Service::run); }
        }
    """)
    rows = graph.indexer.conn.execute(
        "SELECT e.line, e.dst_name, e.confidence, d.fqn "
        "FROM edges e JOIN symbols d ON d.id=e.dst "
        "WHERE e.kind='calls' AND e.line IN (6, 7) "
        "AND e.dst_name IN ('Service.run', 'run') ORDER BY d.fqn"
    ).fetchall()
    graph.close()

    assert {r["line"] for r in rows} == {6, 7}
    assert {r["dst_name"] for r in rows} == {"run"}
    assert {r["fqn"].rsplit('.', 2)[-2] for r in rows} == {"Service", "Worker"}
    assert all(r["confidence"] == "possible" for r in rows)
