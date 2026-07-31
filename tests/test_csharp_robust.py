"""Bateria de robustez do extractor C# (L0).

Mesmo método das baterias anteriores, com o checklist do padrão (membros viram
símbolo? visibilidade setada? herança/import sobre-qualifica?). C# tem namespace
(entra no fqn) e o idioma central de PROPERTY em vez de campo público.

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
    tree = get_parser("csharp").parse(src_b)
    return extract("csharp", src_b, module, tree)


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


def test_record():
    assert ("class", "app.Point") in _syms("record Point(int X, int Y);")


def test_interface():
    assert ("interface", "app.IShape") in _syms(
        "interface IShape { int Area(); }")


def test_struct():
    assert ("struct", "app.Vec") in _syms("struct Vec { int X; }")


def test_enum():
    assert ("enum", "app.Color") in _syms("enum Color { Red, Green }")


def test_method():
    assert ("method", "app.C.Run") in _syms("class C { void Run() {} }")


def test_static_method():
    assert ("method", "app.C.Main") in _syms(
        "class C { static void Main() {} }")


def test_constructor():
    s = _syms("class C { public C() {} }")
    assert any(fqn == "app.C.C" for _, fqn in s)


def test_property_is_a_symbol():
    # PROPERTY é o idioma central de C# — deveria ser navegável
    s = _syms("class C { public string Name { get; set; } }")
    assert any(fqn == "app.C.Name" for _, fqn in s)


def test_readonly_auto_property():
    s = _syms("class C { public int Id { get; } }")
    assert any(fqn == "app.C.Id" for _, fqn in s)


def test_field_is_a_symbol():
    s = _syms("class C { private int count; }")
    assert any(fqn == "app.C.count" for _, fqn in s)


def test_const_field_is_a_symbol():
    s = _syms("class C { public const int MAX = 3; }")
    assert any(fqn == "app.C.MAX" for _, fqn in s)


def test_multiple_fields_one_declaration():
    s = _syms("class C { private int a, b; }")
    assert any(fqn == "app.C.a" for _, fqn in s)
    assert any(fqn == "app.C.b" for _, fqn in s)


def test_enum_members_are_symbols():
    s = _syms("enum Color { Red, Green, Blue }")
    assert any(fqn == "app.Color.Red" for _, fqn in s)
    assert any(fqn == "app.Color.Blue" for _, fqn in s)


def test_interface_method_is_a_symbol():
    assert ("method", "app.IRepo.Save") in _syms(
        "interface IRepo { void Save(); }")


# ============================================================================
# B. FQN, namespace e contenção
# ============================================================================

def test_namespace_in_fqn():
    assert ("class", "app.MyApp.Widget") in _syms(
        "namespace MyApp { class Widget {} }")


def test_file_scoped_namespace():
    assert ("class", "app.MyApp.Widget") in _syms(
        "namespace MyApp;\nclass Widget {}")


def test_method_fqn_includes_class_and_namespace():
    assert ("method", "app.N.Service.Handle") in _syms(
        "namespace N { class Service { void Handle() {} } }")


def test_nested_class():
    s = _syms("class Outer { class Inner {} }")
    assert ("class", "app.Outer.Inner") in s


def test_parent_fqn_for_property():
    syms, _ = _extract("class C { public int X { get; set; } }")
    x = [s for s in syms if s.name == "X"]
    assert x and x[0].parent_fqn == "app.C"


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_private_method_visibility():
    syms = _sym_by_name("class C { private void X() {} }", "X")
    assert syms and syms[0].visibility == "private"


def test_public_method_visibility():
    syms = _sym_by_name("class C { public void X() {} }", "X")
    assert syms and syms[0].visibility == "public"


def test_protected_method_visibility():
    syms = _sym_by_name("class C { protected void X() {} }", "X")
    assert syms and syms[0].visibility == "protected"


def test_internal_visibility():
    syms = _sym_by_name("class C { internal void X() {} }", "X")
    assert syms and syms[0].visibility == "internal"


def test_property_visibility():
    syms = _sym_by_name("class C { public int X { get; set; } }", "X")
    assert syms and syms[0].visibility == "public"


def test_field_visibility():
    syms = _sym_by_name("class C { private int x; }", "x")
    assert syms and syms[0].visibility == "private"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_xml_doc_comment():
    syms = _sym_by_name(
        "class C {\n /// Faz algo.\n void Run() {}\n}", "Run")
    assert syms and syms[0].doc == "Faz algo."


def test_signature_excludes_body():
    syms = _sym_by_name("class C { int Add(int a, int b) { return a + b; } }", "Add")
    assert syms[0].signature == "int Add(int a, int b)"


# ============================================================================
# E. Imports (using)
# ============================================================================

def test_using_simple():
    assert "System" in _refs("using System;", "imports")


def test_using_qualified():
    assert "System.Collections.Generic" in _refs(
        "using System.Collections.Generic;", "imports")


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "Helper" in _refs("class C { void F() { Helper(); } }", "calls")


def test_member_call_takes_name():
    assert "Run" in _refs("class C { void F(T x) { x.Run(); } }", "calls")


def test_static_call_via_using_is_qualified():
    got = _refs(
        "using System;\nclass C { void F() { Console.WriteLine(\"x\"); } }", "calls")
    # Console não está em using diretamente; ao menos WriteLine é capturado
    assert "WriteLine" in got or any("WriteLine" in g for g in got)


def test_object_creation_is_a_call():
    assert "Widget" in _refs("class C { void F() { new Widget(); } }", "calls")


# ============================================================================
# G. Herança
# ============================================================================

def test_class_extends():
    assert "Base" in _refs("class C : Base {}", "inherits")


def test_class_implements_interface():
    got = _refs("class C : IDisposable {}", "inherits")
    assert "IDisposable" in got


def test_multiple_base_types():
    got = _refs("class C : Base, IFoo {}", "inherits")
    assert "Base" in got and "IFoo" in got


def test_generic_base_is_stripped():
    assert "List" in _refs("class C : List<int> {}", "inherits")


def test_interface_extends():
    assert "IBase" in _refs("interface I : IBase {}", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.cs": "class C { int Helper() { return 1; } "
                "int Caller() { return Helper(); } }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "Helper"]
    assert any(e["dst"] and e["dst"].endswith("Helper") for e in calls)
    g.close()


def test_inheritance_resolves_same_file(tmp_path):
    g = _graph(tmp_path, {
        "a.cs": "class Base {}\nclass Sub : Base {}\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_inheritance_resolves_across_files_with_namespace(tmp_path):
    # caso real: classes em namespace, arquivos separados, using entre eles
    g = _graph(tmp_path, {
        "base.cs": "namespace App { public class Base {} }\n",
        "sub.cs": "using App;\nnamespace App { public class Sub : Base {} }\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_new_resolves_to_class(tmp_path):
    g = _graph(tmp_path, {
        "a.cs": "class Widget {}\nclass C { void F() { new Widget(); } }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "Widget"]
    assert any(e["dst"] and e["dst"].endswith("Widget") for e in calls)
    g.close()


def test_property_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.cs": "class C { public int Retries { get; set; } }\n"})
    info, _ = g.symbol_info("Retries")
    assert info["symbol"]["kind"] in ("variable", "property")
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_generic_class():
    assert ("class", "app.Box") in _syms("class Box<T> { }")


def test_generic_method():
    assert ("method", "app.C.Map") in _syms(
        "class C { T Map<T>(T x) { return x; } }")


def test_expression_bodied_method():
    assert ("method", "app.C.Get") in _syms(
        "class C { int Get() => 42; }")


def test_expression_bodied_property():
    s = _syms("class C { public int X => 42; }")
    assert any(fqn == "app.C.X" for _, fqn in s)


def test_abstract_class_and_method():
    s = _syms("abstract class C { public abstract void Run(); }")
    assert ("class", "app.C") in s
    assert ("method", "app.C.Run") in s


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("class C { void F( { } void G() {} }")
    assert isinstance(syms, list)
