from __future__ import annotations

import textwrap

import pytest

from codegraph import AmbiguousSymbol, CodeGraph


def _write(root, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


def test_java_canonical_fqn_locates_find_info_and_callers(tmp_path):
    _write(
        tmp_path,
        "src/main/java/com/acme/orders/OrderService.java",
        """
        package com.acme.orders;

        public class OrderService {
            public void save() {}
        }
        """,
    )
    _write(
        tmp_path,
        "src/main/java/com/acme/orders/OrderController.java",
        """
        package com.acme.orders;

        public class OrderController {
            private OrderService service;

            public void create() {
                service.save();
            }
        }
        """,
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        canonical = "com.acme.orders.OrderService.save"

        stored = graph.indexer.conn.execute(
            "SELECT fqn FROM symbols WHERE name='save'"
        ).fetchone()["fqn"]
        assert stored == canonical

        found, _env = graph.find_symbol(canonical)
        assert [row["fqn"] for row in found if row["name"] == "save"] == [stored]

        info, _env = graph.symbol_info(canonical)
        assert info["symbol"]["fqn"] == stored

        target, callers, _env = graph.callers(canonical)
        assert target["fqn"] == stored
        assert any(row["other_fqn"].endswith(".OrderController.create")
                   for row in callers)
    finally:
        graph.close()


def test_java_canonical_fqn_preserves_overload_ambiguity(tmp_path):
    _write(
        tmp_path,
        "src/main/java/com/acme/orders/OrderService.java",
        """
        package com.acme.orders;

        public class OrderService {
            public void save(String value) {}
            public void save(int value) {}
        }
        """,
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        with pytest.raises(AmbiguousSymbol) as error:
            graph.symbol_info("com.acme.orders.OrderService.save")
        assert len(error.value.candidates) == 2
    finally:
        graph.close()


def test_java_alias_does_not_override_exact_identity_from_other_language(tmp_path):
    _write(
        tmp_path,
        "com/acme/orders/OrderService.py",
        """
        def save():
            return None
        """,
    )
    _write(
        tmp_path,
        "src/main/java/com/acme/orders/OrderService.java",
        """
        package com.acme.orders;

        public class OrderService {
            public void save() {}
        }
        """,
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        info, _env = graph.symbol_info("com.acme.orders.OrderService.save")
        assert info["symbol"]["fqn"] == "com.acme.orders.OrderService.save"
        assert info["symbol"]["path"].endswith("OrderService.py")
    finally:
        graph.close()


def test_java_canonical_identity_uses_declared_package_not_source_root(tmp_path):
    _write(
        tmp_path,
        "unusual/generated/location/WrongName.java",
        """
        package actual.api;
        class Outer {
            class Inner {
                Inner() {}
                void run() {}
                void run(String value) {}
            }
        }
        """,
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        rows = graph.indexer.conn.execute(
            "SELECT fqn FROM symbols WHERE kind != 'file' ORDER BY fqn"
        ).fetchall()
        fqns = [row["fqn"] for row in rows]
        assert "actual.api.Outer" in fqns
        assert "actual.api.Outer.Inner" in fqns
        assert fqns.count("actual.api.Outer.Inner.run") == 2
        assert all("unusual.generated" not in fqn for fqn in fqns)
        with pytest.raises(AmbiguousSymbol):
            graph.symbol_info("actual.api.Outer.Inner.run")
    finally:
        graph.close()


def test_java_default_package_identity_has_no_path_prefix(tmp_path):
    _write(tmp_path, "generated/source/Main.java", "class Main { void run() {} }")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        rows = graph.indexer.conn.execute(
            "SELECT fqn FROM symbols WHERE kind != 'file' ORDER BY fqn"
        ).fetchall()
        assert [row["fqn"] for row in rows] == ["Main", "Main.run"]
    finally:
        graph.close()
