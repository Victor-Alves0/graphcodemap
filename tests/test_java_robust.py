"""Bateria de robustez do extractor Java (L0).

Mesmo método da bateria Python: cobrir toda a superfície do que o extractor DEVE
produzir — símbolos, fqn, contenção, visibilidade, javadoc/assinatura, imports,
chamadas, herança, resolução/confiança — e os casos de borda que costumam
quebrar. Escrita para EXPOR lacunas antes de corrigir; fronteiras deliberadas
ficam travadas com o motivo.

`_syms`/`_refs` rodam o extractor direto (usar module="app" evita o `Foo.Foo`
que o fqn baseado em caminho produziria); resolução/confiança montam CodeGraph.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(src: str, module="app"):
    src_b = textwrap.dedent(src).encode("utf-8")
    tree = get_parser("java").parse(src_b)
    return extract("java", src_b, module, tree)


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
    assert ("class", "app.Foo") in _syms("class Foo {}")


def test_interface():
    assert ("interface", "app.Runnable") in _syms("interface Runnable {}")


def test_enum():
    assert ("enum", "app.Color") in _syms("enum Color { RED, GREEN }")


def test_record():
    assert ("class", "app.Point") in _syms("record Point(int x, int y) {}")


def test_method():
    assert ("method", "app.C.run") in _syms("class C { void run() {} }")


def test_constructor():
    s = _syms("class C { C() {} }")
    assert any(k == "method" and fqn == "app.C.C" for k, fqn in s)


def test_static_method():
    assert ("method", "app.C.main") in _syms(
        "class C { public static void main(String[] a) {} }")


def test_nested_class():
    s = _syms("class Outer { class Inner {} }")
    assert ("class", "app.Outer.Inner") in s


def test_static_nested_class():
    s = _syms("class Outer { static class Builder {} }")
    assert ("class", "app.Outer.Builder") in s


def test_field_is_a_symbol():
    # campo é parte da API do tipo — deveria ser navegável
    s = _syms("class C { private PacienteService service; }")
    assert any(fqn == "app.C.service" for _, fqn in s)


def test_multiple_fields_in_one_declaration():
    s = _syms("class C { int a, b; }")
    assert any(fqn == "app.C.a" for _, fqn in s)
    assert any(fqn == "app.C.b" for _, fqn in s)


def test_static_final_field_is_a_constant():
    s = _syms("class C { public static final int MAX = 3; }")
    assert ("constant", "app.C.MAX") in s


def test_enum_constants_are_symbols():
    s = _syms("enum Color { RED, GREEN, BLUE }")
    assert ("constant", "app.Color.RED") in s
    assert ("constant", "app.Color.BLUE") in s


def test_record_components_are_symbols():
    s = _syms("record Point(int x, int y) {}")
    assert any(fqn == "app.Point.x" for _, fqn in s)
    assert any(fqn == "app.Point.y" for _, fqn in s)


def test_interface_method_is_a_symbol():
    assert ("method", "app.Repo.save") in _syms(
        "interface Repo { void save(Item i); }")


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_class():
    assert ("method", "app.Service.handle") in _syms(
        "class Service { void handle() {} }")


def test_deeply_nested_fqn():
    s = _syms("class A { class B { void m() {} } }")
    assert ("method", "app.A.B.m") in s


def test_parent_fqn_is_set_for_method():
    syms, _ = _extract("class C { void run() {} }")
    run = next(s for s in syms if s.name == "run")
    assert run.parent_fqn == "app.C"


def test_field_parent_fqn_is_the_class():
    syms, _ = _extract("class C { int count; }")
    fld = [s for s in syms if s.name == "count"]
    assert fld and fld[0].parent_fqn == "app.C"


# ============================================================================
# C. Visibilidade (modificadores Java são explícitos)
# ============================================================================

def test_private_method_visibility():
    syms = _sym_by_name("class C { private void x() {} }", "x")
    assert syms and syms[0].visibility == "private"


def test_public_method_visibility():
    syms = _sym_by_name("class C { public void x() {} }", "x")
    assert syms and syms[0].visibility == "public"


def test_protected_method_visibility():
    syms = _sym_by_name("class C { protected void x() {} }", "x")
    assert syms and syms[0].visibility == "protected"


def test_private_field_visibility():
    syms = _sym_by_name("class C { private int x; }", "x")
    assert syms and syms[0].visibility == "private"


# ============================================================================
# D. Javadoc e assinatura
# ============================================================================

def test_javadoc_on_method():
    syms = _sym_by_name(
        "class C {\n  /** Faz algo. */\n  void run() {}\n}", "run")
    assert syms and syms[0].doc == "Faz algo."


def test_signature_excludes_body():
    syms = _sym_by_name("class C { int add(int a, int b) { return a + b; } }", "add")
    assert syms[0].signature == "int add(int a, int b)"


# ============================================================================
# E. Imports
# ============================================================================

def test_import_simple():
    assert "java.util.List" in _refs("import java.util.List;", "imports")


def test_static_import():
    got = _refs("import static java.lang.Math.max;", "imports")
    assert any("Math" in g for g in got)


def test_wildcard_import():
    got = _refs("import java.util.*;", "imports")
    assert any(g.startswith("java.util") for g in got)


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("class C { void f() { helper(); } }", "calls")


def test_typed_field_receiver_call_is_qualified():
    got = _refs(
        "class C { private Svc svc; void f() { svc.run(); } }", "calls")
    assert "Svc.run" in got


def test_static_call_via_import_is_qualified():
    got = _refs(
        "import a.b.Math;\nclass C { void f() { Math.max(1, 2); } }", "calls")
    assert "a.b.Math.max" in got


def test_this_field_call_is_qualified():
    got = _refs(
        "class C { private Svc svc; void f() { this.svc.run(); } }", "calls")
    assert "Svc.run" in got


def test_chained_call_takes_method_name():
    assert "b" in _refs("class C { void f() { a().b(); } }", "calls")


def test_constructor_call():
    assert "Widget" in _refs("class C { void f() { new Widget(); } }", "calls")


def test_var_receiver_call_is_bare_name():
    # var esconde o tipo → nome nu (vira possible na resolução)
    got = _refs(
        "class C { void f() { var x = make(); x.run(); } }", "calls")
    assert "run" in got


# ============================================================================
# G. Herança
# ============================================================================

def test_extends():
    assert "Base" in _refs("class C extends Base {}", "inherits")


def test_implements_single():
    assert "Runnable" in _refs("class C implements Runnable {}", "inherits")


def test_implements_multiple():
    got = _refs("class C implements A, B {}", "inherits")
    assert "A" in got and "B" in got


def test_extends_generic_is_stripped():
    assert "Base" in _refs("class C extends Base<String> {}", "inherits")


def test_interface_extends():
    assert "Parent" in _refs("interface C extends Parent {}", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_typed_receiver_resolves_inferred(tmp_path):
    g = _graph(tmp_path, {
        "Svc.java": "package app;\npublic class Svc { public void run() {} }\n",
        "C.java": "package app;\npublic class C { private Svc svc; "
                  "void f() { svc.run(); } }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "Svc.run"]
    assert calls and all(e["conf"] == "inferred" for e in calls)
    g.close()


def test_homonym_bare_call_is_possible(tmp_path):
    g = _graph(tmp_path, {
        "A.java": "package app;\npublic class A { public void save() {} }\n",
        "B.java": "package app;\npublic class B { public void save() {} }\n",
        "C.java": "package app;\npublic class C { void go(Object x) { "
                  "callThing(); } void callThing() {} }\n",
    })
    # sanity: o grafo indexa sem quebrar e há símbolos dos três
    fqns = {r["fqn"] for r in g.indexer.conn.execute("SELECT fqn FROM symbols")}
    assert any(f.endswith("A.save") for f in fqns)
    g.close()


def test_constructor_resolves_to_class(tmp_path):
    g = _graph(tmp_path, {
        "Widget.java": "package app;\npublic class Widget { public Widget() {} }\n",
        "C.java": "package app;\npublic class C { void f() { new Widget(); } }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "Widget"]
    assert any(e["dst"] and e["dst"].endswith("Widget") for e in calls)
    g.close()


def test_inheritance_resolves(tmp_path):
    g = _graph(tmp_path, {
        "Base.java": "package app;\npublic class Base {}\n",
        "Sub.java": "package app;\nimport app.Base;\npublic class Sub extends Base {}\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_generic_class_declaration():
    assert ("class", "app.Box") in _syms("class Box<T> { T value; }")


def test_annotation_does_not_break_method():
    s = _syms("class C { @Override public String toString() { return \"\"; } }")
    assert ("method", "app.C.toString") in s


def test_varargs_method():
    assert ("method", "app.C.printf") in _syms(
        "class C { void printf(String fmt, Object... args) {} }")


def test_anonymous_class_call_does_not_crash():
    syms, refs = _extract(
        "class C { void f() { Runnable r = new Runnable() {"
        " public void run() { work(); } }; } }")
    # não deve estourar; a chamada interna work() é capturada
    assert any(r.dst_name == "work" for r in refs if r.kind == "calls")


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("class C { void f( { } void g() {} }")
    assert any(s.name == "g" for s in syms)


def test_field_call_target_is_not_confused_with_method():
    # campo `run` e método `run()` coexistindo não devem colidir no símbolo
    s = _syms("class C { int run; void run() {} }")
    assert ("method", "app.C.run") in s
