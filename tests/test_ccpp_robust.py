"""Bateria de robustez do extractor C/C++ (L0).

Mesmo método das baterias anteriores, com o checklist do padrão (membros viram
símbolo? visibilidade setada? herança/import sobre-qualifica?). C++ é o mais
complexo: access specifiers SECCIONAIS (public:/private:), métodos fora da classe
(Type::method), namespaces, templates, typedef.

`_syms`/`_refs` rodam o extractor direto (module="app"); resolução monta grafo.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(src: str, lang="cpp", module="app"):
    src_b = textwrap.dedent(src).encode("utf-8")
    tree = get_parser(lang).parse(src_b)
    return extract(lang, src_b, module, tree)


def _syms(src: str, lang="cpp", module="app"):
    syms, _ = _extract(src, lang, module)
    return {(s.kind, s.fqn) for s in syms}


def _sym_by_name(src: str, name: str, lang="cpp"):
    syms, _ = _extract(src, lang)
    return [s for s in syms if s.name == name]


def _refs(src: str, kind: str, lang="cpp"):
    _, refs = _extract(src, lang)
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

def test_free_function():
    assert ("function", "app.run") in _syms("void run() {}")


def test_c_function():
    assert ("function", "app.add") in _syms("int add(int a, int b) { return a+b; }", lang="c")


def test_struct():
    assert ("struct", "app.Point") in _syms("struct Point { int x; };")


def test_class():
    assert ("class", "app.Widget") in _syms("class Widget { };")


def test_enum():
    assert ("enum", "app.Color") in _syms("enum Color { Red, Green };")


def test_inline_method():
    assert ("method", "app.C.run") in _syms("class C { void run() {} };")


def test_declared_method():
    # método declarado no corpo, definido fora
    assert ("method", "app.C.run") in _syms("class C { void run(); };")


def test_out_of_line_method_definition():
    s = _syms("class C { void run(); };\nvoid C::run() {}")
    assert ("method", "app.C.run") in s


def test_constructor():
    s = _syms("class C { C(); };")
    assert any(fqn == "app.C.C" for _, fqn in s)


def test_define_macro_is_constant():
    assert ("constant", "app.MAX") in _syms("#define MAX 100\n", lang="c")


def test_function_macro():
    assert ("function", "app.SQUARE") in _syms("#define SQUARE(x) ((x)*(x))\n", lang="c")


def test_struct_fields_are_symbols():
    # data member é parte da API do tipo — deveria ser navegável
    s = _syms("struct Point { int x; int y; };")
    assert any(fqn == "app.Point.x" for _, fqn in s)
    assert any(fqn == "app.Point.y" for _, fqn in s)


def test_class_data_members_are_symbols():
    s = _syms("class C {\n int count;\n float ratio;\n};")
    assert any(fqn == "app.C.count" for _, fqn in s)
    assert any(fqn == "app.C.ratio" for _, fqn in s)


def test_multiple_fields_one_declaration():
    s = _syms("struct P { int x, y; };")
    assert any(fqn == "app.P.x" for _, fqn in s)
    assert any(fqn == "app.P.y" for _, fqn in s)


def test_enum_members_are_symbols():
    s = _syms("enum Color { Red, Green, Blue };")
    assert any(fqn == "app.Color.Red" for _, fqn in s)
    assert any(fqn == "app.Color.Blue" for _, fqn in s)


def test_enum_class_members():
    s = _syms("enum class Direction { North, South };")
    assert any(fqn.endswith("Direction.North") for _, fqn in s)


def test_typedef_is_a_symbol():
    assert ("type_alias", "app.Handle") in _syms("typedef void* Handle;", lang="c")


# ============================================================================
# B. FQN, namespace e contenção
# ============================================================================

def test_namespace_in_fqn():
    assert ("function", "app.ns.helper") in _syms(
        "namespace ns { void helper() {} }")


def test_nested_namespace():
    assert ("function", "app.a.b.f") in _syms(
        "namespace a { namespace b { void f() {} } }")


def test_method_fqn_includes_class():
    assert ("method", "app.Service.handle") in _syms(
        "class Service { void handle() {} };")


def test_field_parent_is_the_struct():
    syms, _ = _extract("struct Point { int x; };")
    fld = [s for s in syms if s.name == "x"]
    assert fld and fld[0].parent_fqn == "app.Point"


# ============================================================================
# C. Visibilidade (access specifiers seccionais)
# ============================================================================

def test_class_member_default_is_private():
    syms = _sym_by_name("class C { int hidden; };", "hidden")
    assert syms and syms[0].visibility == "private"


def test_struct_member_default_is_public():
    syms = _sym_by_name("struct S { int open; };", "open")
    assert syms and syms[0].visibility == "public"


def test_public_section_member():
    syms = _sym_by_name("class C {\npublic:\n int shown;\n};", "shown")
    assert syms and syms[0].visibility == "public"


def test_private_section_after_public():
    syms = _sym_by_name(
        "class C {\npublic:\n int a;\nprivate:\n int b;\n};", "b")
    assert syms and syms[0].visibility == "private"


def test_protected_method_visibility():
    syms = _sym_by_name("class C {\nprotected:\n void guard() {}\n};", "guard")
    assert syms and syms[0].visibility == "protected"


# ============================================================================
# D. Assinatura
# ============================================================================

def test_signature_excludes_body():
    syms = _sym_by_name("int add(int a, int b) { return a + b; }", "add")
    assert syms[0].signature == "int add(int a, int b)"


# ============================================================================
# E. Imports (#include)
# ============================================================================

def test_include_angle():
    assert "vector" in _refs("#include <vector>\n", "imports")


def test_include_quoted_path():
    assert "foo.bar.h" in _refs('#include "foo/bar.h"\n', "imports")


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("void f() { helper(); }", "calls")


def test_qualified_call():
    assert "ns.helper" in _refs("void f() { ns::helper(); }", "calls")


def test_method_call_takes_name():
    assert "run" in _refs("void f(T x) { x.run(); }", "calls")


def test_template_function_call():
    got = _refs("void f() { make<int>(); }", "calls")
    assert "make" in got


# ============================================================================
# G. Herança
# ============================================================================

def test_public_inheritance():
    assert "Base" in _refs("class D : public Base { };", "inherits")


def test_multiple_inheritance():
    got = _refs("class D : public A, public B { };", "inherits")
    assert "A" in got and "B" in got


def test_template_base_is_stripped():
    assert "Vector" in _refs("class D : public Vector<int> { };", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.cpp": "int helper() { return 1; }\nint caller() { return helper(); }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_out_of_line_method_resolves_to_class(tmp_path):
    g = _graph(tmp_path, {
        "a.cpp": "class C {\npublic:\n void run();\n void go();\n};\n"
                 "void C::run() {}\nvoid C::go() { run(); }\n",
    })
    methods = [f for f in {r["fqn"] for r in g.indexer.conn.execute("SELECT fqn FROM symbols")}
               if f.endswith("C.run")]
    assert methods
    g.close()


def test_inheritance_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.cpp": "class Base { };\nclass Sub : public Base { };\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_struct_field_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.cpp": "struct C { int retries; };\n"})
    info, _ = g.symbol_info("retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_template_class():
    assert ("class", "app.Stack") in _syms(
        "template<typename T> class Stack { T* data; };")


def test_template_function():
    assert ("function", "app.identity") in _syms(
        "template<typename T> T identity(T x) { return x; }")


def test_forward_declaration_is_not_a_symbol():
    # `class C;` sem corpo é forward decl, não define o tipo
    s = _syms("class C;\nvoid f() {}")
    assert not any(k == "class" and fqn == "app.C" for k, fqn in s)


def test_anonymous_struct_does_not_crash():
    syms, _ = _extract("struct { int x; } global;")
    assert isinstance(syms, list)


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("void f( { }\nvoid g() {}")
    assert isinstance(syms, list)


def test_static_member_and_method_coexist():
    s = _syms("class C {\n int value;\npublic:\n int getValue() { return value; }\n};")
    assert any(fqn == "app.C.value" for _, fqn in s)
    assert ("method", "app.C.getValue") in s
