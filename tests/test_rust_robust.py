"""Bateria de robustez do extractor Rust (L0).

Mesmo método das baterias Python/Java: cobrir toda a superfície do extractor —
símbolos, fqn, contenção, visibilidade, doc/assinatura, use, chamadas, traits/
impl (herança), resolução/confiança — e os casos de borda. Escrita para EXPOR
lacunas antes de corrigir; fronteiras deliberadas ficam travadas com o motivo.

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
    tree = get_parser("rust").parse(src_b)
    return extract("rust", src_b, module, tree)


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

def test_free_function():
    assert ("function", "app.run") in _syms("fn run() {}")


def test_struct():
    assert ("struct", "app.Point") in _syms("struct Point { x: i32 }")


def test_tuple_struct():
    assert ("struct", "app.Wrapper") in _syms("struct Wrapper(i32);")


def test_unit_struct():
    assert ("struct", "app.Marker") in _syms("struct Marker;")


def test_enum():
    assert ("enum", "app.Color") in _syms("enum Color { Red, Green }")


def test_trait_is_interface():
    assert ("interface", "app.Draw") in _syms("trait Draw { fn draw(&self); }")


def test_const():
    assert ("constant", "app.MAX") in _syms("const MAX: i32 = 3;")


def test_static():
    assert ("constant", "app.COUNTER") in _syms("static COUNTER: i32 = 0;")


def test_type_alias():
    assert ("type_alias", "app.Vec2") in _syms("type Vec2 = (f32, f32);")


def test_module():
    assert ("module", "app.util") in _syms("mod util {}")


def test_method_in_impl():
    s = _syms("struct P; impl P { fn new() -> P { P } }")
    assert ("method", "app.P.new") in s


def test_method_with_self():
    s = _syms("struct P; impl P { fn area(&self) -> i32 { 0 } }")
    assert ("method", "app.P.area") in s


def test_trait_method_signature():
    # fn sem corpo dentro de trait (function_signature_item)
    s = _syms("trait T { fn required(&self); }")
    assert ("method", "app.T.required") in s


def test_trait_default_method():
    s = _syms("trait T { fn helper(&self) -> i32 { 1 } }")
    assert ("method", "app.T.helper") in s


def test_function_in_module():
    s = _syms("mod util { fn helper() {} }")
    assert ("function", "app.util.helper") in s


def test_associated_const_in_impl():
    s = _syms("struct P; impl P { const ID: i32 = 7; }")
    assert ("constant", "app.P.ID") in s


def test_struct_fields_are_symbols():
    # campo é parte da API do struct — deveria ser navegável
    s = _syms("struct Point { x: i32, y: i32 }")
    assert any(fqn == "app.Point.x" for _, fqn in s)
    assert any(fqn == "app.Point.y" for _, fqn in s)


def test_enum_variants_are_symbols():
    s = _syms("enum Color { Red, Green, Blue }")
    assert any(fqn == "app.Color.Red" for _, fqn in s)
    assert any(fqn == "app.Color.Blue" for _, fqn in s)


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_type():
    assert ("method", "app.Service.handle") in _syms(
        "struct Service; impl Service { fn handle(&self) {} }")


def test_nested_module_fqn():
    s = _syms("mod a { mod b { fn f() {} } }")
    assert ("function", "app.a.b.f") in s


def test_parent_fqn_for_method():
    syms, _ = _extract("struct P; impl P { fn run(&self) {} }")
    run = next(s for s in syms if s.name == "run")
    assert run.parent_fqn == "app.P"


def test_struct_field_parent_is_the_struct():
    syms, _ = _extract("struct Point { x: i32 }")
    fld = [s for s in syms if s.name == "x"]
    assert fld and fld[0].parent_fqn == "app.Point"


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_pub_function_is_public():
    syms = _sym_by_name("pub fn run() {}", "run")
    assert syms and syms[0].visibility == "public"


def test_private_function_is_private():
    syms = _sym_by_name("fn run() {}", "run")
    assert syms and syms[0].visibility == "private"


def test_pub_struct_is_public():
    syms = _sym_by_name("pub struct P {}", "P")
    assert syms and syms[0].visibility == "public"


def test_pub_crate_is_not_private():
    syms = _sym_by_name("pub(crate) fn run() {}", "run")
    assert syms and syms[0].visibility != "private"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_doc_comment():
    syms = _sym_by_name("/// Faz algo.\nfn run() {}", "run")
    assert syms and syms[0].doc == "Faz algo."


def test_multiline_doc_comment():
    syms = _sym_by_name("/// Linha um.\n/// Linha dois.\nfn run() {}", "run")
    assert syms and "Linha um." in syms[0].doc and "Linha dois." in syms[0].doc


def test_signature_excludes_body():
    syms = _sym_by_name("fn add(a: i32, b: i32) -> i32 { a + b }", "add")
    assert syms[0].signature == "fn add(a: i32, b: i32) -> i32"


# ============================================================================
# E. Use (imports)
# ============================================================================

def test_use_simple():
    assert "std.collections.HashMap" in _refs(
        "use std::collections::HashMap;", "imports")


def test_use_list():
    got = _refs("use std::collections::{HashMap, HashSet};", "imports")
    assert "std.collections.HashMap" in got
    assert "std.collections.HashSet" in got


def test_use_as_alias():
    assert "std.collections.HashMap" in _refs(
        "use std::collections::HashMap as Map;", "imports")


def test_use_wildcard():
    assert "std.prelude.*" in _refs("use std::prelude::*;", "imports")


def test_nested_use_list():
    got = _refs("use a::{b::C, d::E};", "imports")
    assert "a.b.C" in got and "a.d.E" in got


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("fn f() { helper(); }", "calls")


def test_path_call_is_qualified():
    assert "Foo.bar" in _refs("fn f() { Foo::bar(); }", "calls")


def test_method_call_takes_method_name():
    assert "run" in _refs("fn f(x: T) { x.run(); }", "calls")


def test_calls_inside_macro_args_are_captured():
    # fronteira: o NOME do macro (do_thing!) não vira call — a maioria é stdlib
    # (println!, vec!, format!) e poluiria. Mas chamadas DENTRO dos args contam:
    # println!("{}", compute()) → compute é uma aresta real.
    got = _refs('fn f() { println!("{}", compute()); }', "calls")
    assert "compute" in got


def test_use_traced_call_is_qualified():
    # use a::foo; foo() → foo() resolve para a.foo
    got = _refs("use a::foo;\nfn f() { foo(); }", "calls")
    assert "a.foo" in got


def test_associated_call_via_use():
    got = _refs("use a::Foo;\nfn f() { Foo::bar(); }", "calls")
    assert any("Foo.bar" in g for g in got)


# ============================================================================
# G. Traits e impl (herança)
# ============================================================================

def test_impl_trait_for_type_is_inherits():
    assert "Draw" in _refs("struct P; impl Draw for P {}", "inherits")


def test_impl_generic_trait_stripped():
    got = _refs("struct P; impl From<i32> for P {}", "inherits")
    assert "From" in got


def test_inherent_impl_has_no_inherits():
    # impl P {} (sem trait) não gera aresta de herança
    assert _refs("struct P; impl P { fn new() {} }", "inherits") == set()


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_use_traced_call_resolves_inferred(tmp_path):
    g = _graph(tmp_path, {
        "a.rs": "pub fn foo() {}\n",
        "b.rs": "use crate::a::foo;\n\nfn use_it() { foo(); }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"].endswith("foo")
             and e["src"] and e["src"].endswith("use_it")]
    assert calls
    g.close()


def test_local_unique_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.rs": "fn helper() -> i32 { 1 }\n\nfn caller() -> i32 { helper() }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_impl_method_resolves_to_type(tmp_path):
    g = _graph(tmp_path, {
        "a.rs": "struct P;\nimpl P { fn new() -> P { P } fn go() { P::new(); } }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"].endswith("new")]
    assert any(e["dst"] and e["dst"].endswith("P.new") for e in calls)
    g.close()


def test_trait_impl_inherits_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.rs": "pub trait Draw { fn draw(&self); }\n",
        "b.rs": "use crate::a::Draw;\nstruct P;\nimpl Draw for P { fn draw(&self) {} }\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Draw")]
    assert any(e["dst"] and e["dst"].endswith("Draw") for e in inh)
    g.close()


def test_struct_constant_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.rs": "const RETRIES: i32 = 5;\n"})
    info, _ = g.symbol_info("RETRIES")
    assert info["symbol"]["kind"] == "constant"
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_generic_function():
    assert ("function", "app.identity") in _syms("fn identity<T>(x: T) -> T { x }")


def test_generic_struct():
    assert ("struct", "app.Box") in _syms("struct Box<T> { value: T }")


def test_async_function():
    assert ("function", "app.fetch") in _syms("async fn fetch() {}")


def test_unsafe_function():
    assert ("function", "app.raw") in _syms("unsafe fn raw() {}")


def test_multiple_impl_blocks_same_type():
    s = _syms("struct P; impl P { fn a(&self) {} } impl P { fn b(&self) {} }")
    assert ("method", "app.P.a") in s
    assert ("method", "app.P.b") in s


def test_closure_call_inside_is_captured():
    got = _refs("fn f() { let g = |x: i32| do_work(x); }", "calls")
    assert "do_work" in got


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("fn f( { } fn g() {}")
    assert any(s.name == "g" for s in syms)
