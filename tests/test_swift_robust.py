"""Bateria de robustez do extractor Swift (L0).

Mesmo método das baterias anteriores, com o checklist do padrão (membros viram
símbolo? visibilidade setada? herança/import sobre-qualifica?). Swift funde
class/struct/enum/actor/extension num class_declaration distinguido por
declaration_kind; protocol é interface.

`_syms`/`_refs` rodam o extractor direto (module="app"); resolução monta grafo.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(src: str, module="app"):
    src_b = textwrap.dedent(src).encode("utf-8")
    tree = get_parser("swift").parse(src_b)
    return extract("swift", src_b, module, tree)


def _syms(src: str, module="app"):
    syms, _ = _extract(src, module)
    return {(s.kind, s.fqn) for s in syms}


def _sym_by_name(src: str, name: str):
    syms, _ = _extract(src)
    return [s for s in syms if s.name == name]


def _refs(src: str, kind: str):
    _, refs = _extract(src)
    return {r.dst_name for r in refs if r.kind == kind}


def _graph(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    return g


def _edges(g, kind=None):
    sql = ("SELECT e.kind, ss.fqn src, e.dst_name, sd.fqn dst, e.confidence conf "
           "FROM edges e LEFT JOIN symbols ss ON e.src=ss.id "
           "LEFT JOIN symbols sd ON e.dst=sd.id")
    rows = g.indexer.conn.execute(sql).fetchall()
    return [dict(r) for r in rows if kind is None or r["kind"] == kind]


# ============================================================================
# A. Símbolos — superfície
# ============================================================================

def test_class():
    assert ("class", "app.Widget") in _syms("class Widget {}")


def test_struct():
    assert ("struct", "app.Point") in _syms("struct Point {}")


def test_enum():
    assert ("enum", "app.Color") in _syms("enum Color {}")


def test_actor():
    assert ("class", "app.Worker") in _syms("actor Worker {}")


def test_protocol_is_interface():
    assert ("interface", "app.Drawable") in _syms(
        "protocol Drawable { func draw() }")


def test_free_function():
    assert ("function", "app.run") in _syms("func run() {}")


def test_method():
    assert ("method", "app.C.run") in _syms("class C { func run() {} }")


def test_init():
    s = _syms("class C { init() {} }")
    assert any(fqn == "app.C.init" for _, fqn in s)


def test_static_method():
    assert ("method", "app.C.make") in _syms(
        "class C { static func make() {} }")


def test_stored_property_var():
    s = _syms("class C { var name: String = \"\" }")
    assert any(fqn == "app.C.name" for _, fqn in s)


def test_stored_property_let():
    s = _syms("struct P { let id: Int }")
    assert any(fqn == "app.P.id" for _, fqn in s)


def test_computed_property():
    s = _syms("class C { var area: Int { return 1 } }")
    assert any(fqn == "app.C.area" for _, fqn in s)


def test_static_property():
    s = _syms("class C { static let shared = 1 }")
    assert any(fqn == "app.C.shared" for _, fqn in s)


def test_enum_cases_are_symbols():
    s = _syms("enum Direction { case north; case south }")
    assert any(fqn == "app.Direction.north" for _, fqn in s)
    assert any(fqn == "app.Direction.south" for _, fqn in s)


def test_enum_case_with_associated_value():
    s = _syms("enum Result { case success(Int); case failure }")
    assert any(fqn == "app.Result.success" for _, fqn in s)
    assert any(fqn == "app.Result.failure" for _, fqn in s)


def test_protocol_method_is_a_symbol():
    assert ("method", "app.Repo.save") in _syms(
        "protocol Repo { func save() }")


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_type():
    assert ("method", "app.Service.handle") in _syms(
        "class Service { func handle() {} }")


def test_nested_type():
    s = _syms("struct Outer { struct Inner {} }")
    assert ("struct", "app.Outer.Inner") in s


def test_parent_fqn_for_property():
    syms, _ = _extract("class C { var x: Int = 0 }")
    x = [s for s in syms if s.name == "x"]
    assert x and x[0].parent_fqn == "app.C"


def test_extension_members_scope_to_type():
    # extension reabre o tipo — o método vai para C, sem duplicar C
    s = _syms("class C {}\nextension C { func extra() {} }")
    assert ("method", "app.C.extra") in s
    assert len([1 for k, f in s if k == "class" and f == "app.C"]) == 1


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_private_method_visibility():
    syms = _sym_by_name("class C { private func x() {} }", "x")
    assert syms and syms[0].visibility == "private"


def test_public_method_visibility():
    syms = _sym_by_name("class C { public func x() {} }", "x")
    assert syms and syms[0].visibility == "public"


def test_fileprivate_visibility():
    syms = _sym_by_name("class C { fileprivate func x() {} }", "x")
    assert syms and syms[0].visibility == "fileprivate"


def test_property_visibility():
    syms = _sym_by_name("class C { private var secret: Int = 0 }", "secret")
    assert syms and syms[0].visibility == "private"


def test_open_visibility():
    syms = _sym_by_name("open class C { open func x() {} }", "x")
    assert syms and syms[0].visibility == "open"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_doc_comment():
    syms = _sym_by_name("/// Faz algo.\nfunc run() {}", "run")
    assert syms and syms[0].doc == "Faz algo."


# ============================================================================
# E. Imports
# ============================================================================

def test_import_simple():
    assert "Foundation" in _refs("import Foundation", "imports")


def test_import_submodule():
    got = _refs("import UIKit.UIView", "imports")
    assert any("UIKit" in g for g in got)


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("func f() { helper() }", "calls")


def test_method_call_takes_name():
    assert "run" in _refs("func f(x: T) { x.run() }", "calls")


def test_constructor_call():
    assert "Widget" in _refs("func f() { let w = Widget() }", "calls")


# ============================================================================
# G. Herança / conformidade
# ============================================================================

def test_class_superclass():
    assert "Base" in _refs("class C: Base {}", "inherits")


def test_protocol_conformance():
    got = _refs("class C: Equatable {}", "inherits")
    assert "Equatable" in got


def test_multiple_conformances():
    got = _refs("class C: Base, Codable, Equatable {}", "inherits")
    assert "Base" in got and "Codable" in got


def test_struct_conformance():
    assert "Codable" in _refs("struct P: Codable {}", "inherits")


def test_protocol_inheritance():
    assert "Base" in _refs("protocol P: Base {}", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.swift": "func helper() -> Int { return 1 }\n"
                   "func caller() -> Int { return helper() }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_inheritance_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.swift": "class Base {}\nclass Sub: Base {}\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_property_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.swift": "class C { var retries: Int = 5 }\n"})
    info, _ = g.symbol_info("retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_generic_class():
    assert ("class", "app.Box") in _syms("class Box<T> { var value: T? }")


def test_generic_function():
    assert ("function", "app.identity") in _syms(
        "func identity<T>(_ x: T) -> T { return x }")


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("func f( { }\nfunc g() {}")
    assert isinstance(syms, list)
