"""Bateria de robustez do extractor Scala (L0).

Mesmo método das baterias anteriores, com o checklist do padrão (membros viram
símbolo? visibilidade setada? herança/import sobre-qualifica?). Scala tem o
idioma da CASE CLASS e do construtor primário com val/var (declara membros), e
`object` (singleton). Scala 3 traz `enum`.

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
    tree = get_parser("scala").parse(src_b)
    return extract("scala", src_b, module, tree)


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


def test_object():
    s = _syms("object Config {}")
    assert any(fqn == "app.Config" for _, fqn in s)


def test_trait_is_interface():
    assert ("interface", "app.Drawable") in _syms(
        "trait Drawable { def draw(): Unit }")


def test_case_class():
    assert ("class", "app.Point") in _syms("case class Point(x: Int, y: Int)")


def test_method():
    assert ("method", "app.C.run") in _syms("class C { def run(): Unit = {} }")


def test_top_level_val():
    assert ("constant", "app.MAX") in _syms("val MAX = 3")


def test_top_level_var():
    assert ("variable", "app.counter") in _syms("var counter = 0")


def test_class_val_member():
    s = _syms("class C {\n val name = \"x\"\n var age = 0\n}")
    assert any(fqn == "app.C.name" for _, fqn in s)
    assert any(fqn == "app.C.age" for _, fqn in s)


def test_case_class_params_are_members():
    # case class Point(x, y) — os parâmetros são vals públicos automáticos
    s = _syms("case class Point(x: Int, y: Int)")
    assert any(fqn == "app.Point.x" for _, fqn in s)
    assert any(fqn == "app.Point.y" for _, fqn in s)


def test_class_val_constructor_param_is_member():
    # class C(val x: Int) — val no construtor primário é membro
    s = _syms("class C(val x: Int)")
    assert any(fqn == "app.C.x" for _, fqn in s)


def test_plain_constructor_param_is_not_a_member():
    # class C(x: Int) — sem val/var/case, x é só parâmetro, não membro público
    s = _syms("class C(x: Int)")
    assert not any(fqn == "app.C.x" for _, fqn in s)


def test_scala3_enum():
    assert ("enum", "app.Color") in _syms("enum Color { case Red, Green }")


def test_scala3_enum_cases():
    s = _syms("enum Color { case Red, Green, Blue }")
    assert any(fqn == "app.Color.Red" for _, fqn in s)
    assert any(fqn == "app.Color.Blue" for _, fqn in s)


def test_trait_method_is_a_symbol():
    assert ("method", "app.Repo.save") in _syms(
        "trait Repo { def save(): Unit }")


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_class():
    assert ("method", "app.Service.handle") in _syms(
        "class Service { def handle(): Unit = {} }")


def test_nested_object():
    s = _syms("class C {\n object Inner {}\n}")
    assert any(fqn == "app.C.Inner" for _, fqn in s)


def test_parent_fqn_for_member():
    syms, _ = _extract("class C {\n val x = 0\n}")
    x = [s for s in syms if s.name == "x"]
    assert x and x[0].parent_fqn == "app.C"


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_private_method_visibility():
    syms = _sym_by_name("class C {\n private def x(): Unit = {}\n}", "x")
    assert syms and syms[0].visibility == "private"


def test_protected_method_visibility():
    syms = _sym_by_name("class C {\n protected def x(): Unit = {}\n}", "x")
    assert syms and syms[0].visibility == "protected"


def test_public_default_visibility():
    syms = _sym_by_name("class C {\n def x(): Unit = {}\n}", "x")
    assert syms and syms[0].visibility == "public"


def test_private_val_visibility():
    syms = _sym_by_name("class C {\n private val secret = 1\n}", "secret")
    assert syms and syms[0].visibility == "private"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_scaladoc_comment():
    syms = _sym_by_name("/** Faz algo. */\ndef run(): Unit = {}", "run")
    assert syms and syms[0].doc == "Faz algo."


# ============================================================================
# E. Imports
# ============================================================================

def test_import_simple():
    assert "scala.collection.mutable" in _refs(
        "import scala.collection.mutable", "imports")


def test_import_selector():
    got = _refs("import scala.collection.{Map, Set}", "imports")
    assert any(g.endswith("Map") for g in got)


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("def f(): Unit = { helper() }", "calls")


def test_field_call_takes_name():
    assert "run" in _refs("def f(x: T): Unit = { x.run() }", "calls")


# ============================================================================
# G. Herança
# ============================================================================

def test_class_extends():
    assert "Base" in _refs("class C extends Base {}", "inherits")


def test_with_mixin():
    got = _refs("class C extends Base with Logging {}", "inherits")
    assert "Base" in got and "Logging" in got


def test_trait_extends():
    assert "Base" in _refs("trait T extends Base {}", "inherits")


def test_generic_supertype_is_stripped():
    assert "Seq" in _refs("class C extends Seq[Int] {}", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.scala": "def helper(): Int = 1\ndef caller(): Int = helper()\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_inheritance_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.scala": "class Base {}\nclass Sub extends Base {}\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_case_class_param_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.scala": "case class C(retries: Int)\n"})
    info, _ = g.symbol_info("retries")
    assert info["symbol"]["kind"] in ("variable", "constant")
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_generic_class():
    assert ("class", "app.Box") in _syms("class Box[T](value: T)")


def test_generic_method():
    assert ("method", "app.C.identity") in _syms(
        "class C { def identity[T](x: T): T = x }")


def test_companion_object():
    s = _syms("class C {}\nobject C {\n def apply(): C = new C\n}")
    assert ("class", "app.C") in s
    assert any(fqn == "app.C.apply" for _, fqn in s)


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("def f( = {}\ndef g(): Unit = {}")
    assert isinstance(syms, list)
