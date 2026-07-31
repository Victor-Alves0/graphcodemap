"""Bateria de robustez do extractor Kotlin (L0).

Mesmo método das baterias anteriores, com o checklist do padrão (membros viram
símbolo? visibilidade setada? herança/import sobre-qualifica?). Kotlin tem o
idioma do CONSTRUTOR PRIMÁRIO com val/var (que declara propriedades) e a
gramática não expõe field names (navegação por tipo de nó).

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
    tree = get_parser("kotlin").parse(src_b)
    return extract("kotlin", src_b, module, tree)


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

def test_function():
    assert ("function", "app.run") in _syms("fun run() {}")


def test_class():
    assert ("class", "app.Widget") in _syms("class Widget")


def test_interface():
    assert ("interface", "app.Shape") in _syms("interface Shape { fun area(): Int }")


def test_object():
    s = _syms("object Singleton {}")
    assert any(fqn == "app.Singleton" for _, fqn in s)


def test_enum_class():
    assert ("enum", "app.Color") in _syms("enum class Color { RED, GREEN }")


def test_data_class():
    assert ("class", "app.Point") in _syms("data class Point(val x: Int, val y: Int)")


def test_method():
    assert ("method", "app.C.run") in _syms("class C { fun run() {} }")


def test_top_level_val_is_constant_or_variable():
    s = _syms("val max = 3")
    assert ("variable", "app.max") in s or ("constant", "app.max") in s


def test_const_val():
    assert ("constant", "app.MAX") in _syms("const val MAX = 3")


def test_class_property_is_a_symbol():
    # propriedade dentro da classe (o guard `not self.scope` a descartava)
    s = _syms("class C {\n val count: Int = 0\n var name: String = \"\"\n}")
    assert any(fqn == "app.C.count" for _, fqn in s)
    assert any(fqn == "app.C.name" for _, fqn in s)


def test_primary_constructor_property_is_a_symbol():
    # class Point(val x: Int) — val/var no construtor primário é propriedade
    s = _syms("class Point(val x: Int, val y: Int)")
    assert any(fqn == "app.Point.x" for _, fqn in s)
    assert any(fqn == "app.Point.y" for _, fqn in s)


def test_enum_entries_are_symbols():
    s = _syms("enum class Color { RED, GREEN, BLUE }")
    assert any(fqn == "app.Color.RED" for _, fqn in s)
    assert any(fqn == "app.Color.BLUE" for _, fqn in s)


def test_interface_method_is_a_symbol():
    assert ("method", "app.Repo.save") in _syms(
        "interface Repo { fun save() }")


def test_companion_object_method():
    s = _syms("class C {\n companion object {\n  fun create() {}\n }\n}")
    assert any(fqn.endswith("create") and k == "method" for k, fqn in s)


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_class():
    assert ("method", "app.Service.handle") in _syms(
        "class Service { fun handle() {} }")


def test_nested_class():
    s = _syms("class Outer { class Inner }")
    assert ("class", "app.Outer.Inner") in s


def test_parent_fqn_for_property():
    syms, _ = _extract("class C {\n val x: Int = 0\n}")
    x = [s for s in syms if s.name == "x"]
    assert x and x[0].parent_fqn == "app.C"


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_private_method_visibility():
    syms = _sym_by_name("class C {\n private fun x() {}\n}", "x")
    assert syms and syms[0].visibility == "private"


def test_public_default_visibility():
    # Kotlin default é public
    syms = _sym_by_name("class C {\n fun x() {}\n}", "x")
    assert syms and syms[0].visibility == "public"


def test_protected_method_visibility():
    syms = _sym_by_name("class C {\n protected fun x() {}\n}", "x")
    assert syms and syms[0].visibility == "protected"


def test_internal_visibility():
    syms = _sym_by_name("class C {\n internal fun x() {}\n}", "x")
    assert syms and syms[0].visibility == "internal"


def test_private_property_visibility():
    syms = _sym_by_name("class C {\n private val secret: Int = 1\n}", "secret")
    assert syms and syms[0].visibility == "private"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_kdoc_comment():
    syms = _sym_by_name("/** Faz algo. */\nfun run() {}", "run")
    assert syms and syms[0].doc == "Faz algo."


def test_signature_excludes_body():
    syms = _sym_by_name("fun add(a: Int, b: Int): Int { return a + b }", "add")
    assert syms[0].signature.startswith("fun add(a: Int, b: Int)")


# ============================================================================
# E. Imports
# ============================================================================

def test_import_simple():
    assert "kotlin.collections.List" in _refs(
        "import kotlin.collections.List", "imports")


def test_import_wildcard():
    assert "kotlin.math.*" in _refs("import kotlin.math.*", "imports")


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("fun f() { helper() }", "calls")


def test_method_call_takes_name():
    assert "run" in _refs("fun f(x: T) { x.run() }", "calls")


def test_this_method_call():
    assert "helper" in _refs(
        "class C {\n fun a() { this.helper() }\n fun helper() {}\n}", "calls")


def test_imported_call_is_qualified():
    got = _refs("import a.foo\nfun f() { foo() }", "calls")
    assert "a.foo" in got


# ============================================================================
# G. Herança
# ============================================================================

def test_class_extends():
    assert "Base" in _refs("class C : Base()", "inherits")


def test_class_implements_interface():
    assert "Runnable" in _refs("class C : Runnable", "inherits")


def test_multiple_supertypes():
    got = _refs("class C : Base(), Runnable", "inherits")
    assert "Base" in got and "Runnable" in got


def test_generic_supertype_is_stripped():
    assert "Comparable" in _refs("class C : Comparable<C>", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.kt": "fun helper(): Int { return 1 }\nfun caller(): Int { return helper() }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_inheritance_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.kt": "open class Base\nclass Sub : Base()\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_class_property_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.kt": "class C {\n val retries: Int = 5\n}\n"})
    info, _ = g.symbol_info("retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


def test_primary_ctor_property_visible(tmp_path):
    g = _graph(tmp_path, {"a.kt": "class C(val retries: Int)\n"})
    info, _ = g.symbol_info("retries")
    assert info["symbol"]["kind"] in ("variable", "constant")
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_generic_class():
    assert ("class", "app.Box") in _syms("class Box<T>(val value: T)")


def test_generic_function():
    assert ("function", "app.identity") in _syms(
        "fun <T> identity(x: T): T = x")


def test_extension_function():
    # fun String.trimmed() — função de extensão não deve estourar
    syms, _ = _extract("fun String.trimmed(): String = this")
    assert any(s.name == "trimmed" for s in syms)


def test_sealed_class():
    assert ("class", "app.Result") in _syms("sealed class Result")


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("fun f( { }\nfun g() {}")
    assert isinstance(syms, list)
