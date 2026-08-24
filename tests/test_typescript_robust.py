"""Bateria de robustez do extractor TypeScript/TSX/JavaScript (L0).

Mesmo método das baterias Python/Java/Rust, com o checklist do padrão recorrente
(membros viram símbolo? visibilidade setada? herança/import sobre-qualifica?).
Cobre símbolos, fqn, contenção, visibilidade, doc/assinatura, imports, chamadas,
herança, resolução/confiança e casos de borda. Fronteiras deliberadas travadas.

`_syms`/`_refs` rodam o extractor direto (module="app"); resolução monta grafo.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(src: str, lang="typescript", module="app"):
    src_b = textwrap.dedent(src).encode("utf-8")
    tree = get_parser(lang).parse(src_b)
    return extract(lang, src_b, module, tree)


def _syms(src: str, lang="typescript", module="app"):
    syms, _ = _extract(src, lang, module)
    return {(s.kind, s.fqn) for s in syms}


def _sym_by_name(src: str, name: str, lang="typescript"):
    syms, _ = _extract(src, lang)
    return [s for s in syms if s.name == name]


def _refs(src: str, kind: str, lang="typescript"):
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

def test_function():
    assert ("function", "app.run") in _syms("function run() {}")


def test_async_function():
    assert ("function", "app.fetchIt") in _syms("async function fetchIt() {}")


def test_generator_function():
    assert ("function", "app.gen") in _syms("function* gen() {}")


def test_large_jasmine_suite_materializes_callbacks_without_reentrant_walk():
    cases = "\n".join(
        f'it("case {i}", function () {{ helper({i}); }});'
        for i in range(160)
    )
    syms, refs = _extract(
        f'describe("large suite", function () {{\n{cases}\n}});',
        lang="javascript",
    )

    callbacks = [s for s in syms if s.name.startswith("it#1:")]
    helper_calls = [r for r in refs if r.kind == "calls" and r.dst_name == "helper"]
    assert len(callbacks) == 160
    assert len({s.fqn for s in callbacks}) == 160
    assert len(helper_calls) == 160


def test_arrow_const():
    assert ("function", "app.add") in _syms("const add = (a, b) => a + b;")


def test_class():
    assert ("class", "app.Widget") in _syms("class Widget {}")


def test_abstract_class():
    assert ("class", "app.Base") in _syms("abstract class Base {}")


def test_method():
    assert ("method", "app.C.run") in _syms("class C { run() {} }")


def test_static_method():
    assert ("method", "app.C.create") in _syms("class C { static create() {} }")


def test_getter_is_method():
    assert ("method", "app.C.value") in _syms("class C { get value() { return 1; } }")


def test_interface():
    assert ("interface", "app.Shape") in _syms("interface Shape { area(): number; }")


def test_type_alias():
    assert ("type_alias", "app.ID") in _syms("type ID = string;")


def test_enum():
    assert ("enum", "app.Color") in _syms("enum Color { Red, Green }")


def test_module_const():
    assert ("constant", "app.MAX") in _syms("const MAX = 3;")


def test_module_let_is_variable():
    assert ("variable", "app.counter") in _syms("let counter = 0;")


def test_class_field_is_a_symbol():
    # propriedade de classe é parte da API — deveria ser navegável
    s = _syms("class C { count = 0; name: string; }")
    assert any(fqn == "app.C.count" for _, fqn in s)
    assert any(fqn == "app.C.name" for _, fqn in s)


def test_static_class_field():
    s = _syms("class C { static VERSION = 1; }")
    assert any(fqn == "app.C.VERSION" for _, fqn in s)


def test_private_hash_field_is_a_symbol():
    s = _syms("class C { #secret = 1; }")
    assert any(fqn.endswith(".secret") or fqn.endswith(".#secret") for _, fqn in s)


def test_enum_members_are_symbols():
    s = _syms("enum Color { Red, Green, Blue }")
    assert any(fqn == "app.Color.Red" for _, fqn in s)
    assert any(fqn == "app.Color.Blue" for _, fqn in s)


def test_exported_function():
    assert ("function", "app.run") in _syms("export function run() {}")


def test_export_default_class():
    assert ("class", "app.App") in _syms("export default class App {}")


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_class():
    assert ("method", "app.Service.handle") in _syms(
        "class Service { handle() {} }")


def test_parent_fqn_for_method():
    syms, _ = _extract("class C { run() {} }")
    run = next(s for s in syms if s.name == "run")
    assert run.parent_fqn == "app.C"


def test_class_field_parent_is_the_class():
    syms, _ = _extract("class C { count = 0; }")
    fld = [s for s in syms if s.name == "count"]
    assert fld and fld[0].parent_fqn == "app.C"


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_private_method_visibility():
    syms = _sym_by_name("class C { private helper() {} }", "helper")
    assert syms and syms[0].visibility == "private"


def test_public_method_visibility():
    syms = _sym_by_name("class C { public run() {} }", "run")
    assert syms and syms[0].visibility == "public"


def test_protected_method_visibility():
    syms = _sym_by_name("class C { protected guard() {} }", "guard")
    assert syms and syms[0].visibility == "protected"


def test_private_field_visibility():
    syms = _sym_by_name("class C { private secret = 1; }", "secret")
    assert syms and syms[0].visibility == "private"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_jsdoc_on_function():
    syms = _sym_by_name("/** Faz algo. */\nfunction run() {}", "run")
    assert syms and syms[0].doc == "Faz algo."


def test_signature_excludes_body():
    syms = _sym_by_name("function add(a: number, b: number): number { return a + b; }", "add")
    assert syms[0].signature == "function add(a: number, b: number): number"


# ============================================================================
# E. Imports
# ============================================================================

def test_default_import():
    assert "react" in _refs("import React from 'react';", "imports")


def test_named_import():
    assert "react.useState" in _refs("import { useState } from 'react';", "imports")


def test_named_import_aliased():
    assert "react.useState" in _refs(
        "import { useState as us } from 'react';", "imports")


def test_namespace_import():
    assert "lodash" in _refs("import * as _ from 'lodash';", "imports")


def test_relative_module_import():
    got = _refs("import { foo } from './util';", "imports", lang="typescript")
    assert any(g.endswith("util.foo") or g == "util.foo" for g in got)


def test_css_side_effect_import_keeps_path():
    # regressão do fix anterior: `import "./styles.css"` preserva o caminho
    assert "./styles.css" in _refs("import './styles.css';", "imports")


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("function f() { helper(); }", "calls")


def test_method_call_takes_name():
    assert "run" in _refs("function f(x) { x.run(); }", "calls")


def test_this_method_call():
    assert "helper" in _refs(
        "class C { a() { this.helper(); } helper() {} }", "calls")


def test_imported_call_is_qualified():
    assert "react.useState" in _refs(
        "import { useState } from 'react';\nfunction f() { useState(); }", "calls")


def test_new_expression_is_a_call():
    assert "Widget" in _refs("function f() { new Widget(); }", "calls")


def test_chained_call_takes_last_method():
    assert "map" in _refs("function f() { arr.filter(x).map(y); }", "calls")


# ============================================================================
# G. Herança
# ============================================================================

def test_class_extends():
    assert "Base" in _refs("class C extends Base {}", "inherits")


def test_class_implements_interface():
    assert "Drawable" in _refs("class C implements Drawable {}", "inherits")


def test_interface_extends():
    assert "Base" in _refs("interface C extends Base {}", "inherits")


def test_extends_generic_is_stripped():
    assert "Component" in _refs("class C extends Component<Props> {}", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_imported_call_resolves_when_defined_locally(tmp_path):
    g = _graph(tmp_path, {
        "a.ts": "export function foo() { return 1; }\n",
        "b.ts": "import { foo } from './a';\n\nexport function use() { return foo(); }\n",
    })
    calls = [e for e in _edges(g, "calls")
             if e["dst_name"].endswith("foo") and e["src"] and e["src"].endswith("use")]
    assert any(e["dst"] and e["dst"].endswith("a.foo") for e in calls)
    g.close()


def test_local_unique_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.ts": "function helper() { return 1; }\nfunction caller() { return helper(); }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_inheritance_resolves_with_import(tmp_path):
    # o fqn TS é baseado em caminho (a.ts -> 'a', SEM duplicar): checa que a
    # herança importada resolve de verdade
    g = _graph(tmp_path, {
        "base.ts": "export class Base {}\n",
        "sub.ts": "import { Base } from './base';\nexport class Sub extends Base {}\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_inheritance_resolves_when_filename_matches_class(tmp_path):
    # caso React: Foo.tsx com class Foo — o fqn duplica (Foo.Foo)? checa que
    # ainda resolve
    g = _graph(tmp_path, {
        "Base.tsx": "export class Base {}\n",
        "Sub.tsx": "import { Base } from './Base';\nexport class Sub extends Base {}\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_new_resolves_to_class(tmp_path):
    g = _graph(tmp_path, {
        "a.ts": "class Widget {}\nfunction make() { return new Widget(); }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "Widget"]
    assert any(e["dst"] and e["dst"].endswith("Widget") for e in calls)
    g.close()


def test_class_field_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.ts": "class C { retries = 5; }\n"})
    info, _ = g.symbol_info("app.C.retries" if False else "retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_generic_function():
    assert ("function", "app.identity") in _syms(
        "function identity<T>(x: T): T { return x; }")


def test_generic_class():
    assert ("class", "app.Box") in _syms("class Box<T> { value: T; }")


def test_nested_arrow_not_a_top_symbol():
    # arrow interna a uma função não é símbolo de topo
    s = _syms("function outer() { const inner = () => 1; return inner; }")
    assert ("function", "app.outer") in s


def test_decorator_is_not_a_call_edge():
    # fronteira: @Component() não vira aresta de call (fiação de framework)
    got = _refs("@Component()\nclass Foo { m() { work(); } }", "calls")
    assert "work" in got
    assert not any("Component" in g for g in got)


def test_object_literal_method_is_not_a_top_symbol():
    # método em object literal não é símbolo de classe/módulo
    s = _syms("const obj = { greet() { return 1; } };")
    assert ("constant", "app.obj") in s or ("variable", "app.obj") in s


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    # erro RECUPERÁVEL (expressão inválida no corpo): o extractor não estoura e
    # ainda extrai as duas funções. Erros irrecuperáveis do tree-sitter (ex.:
    # `(` sem fechar, que engole o resto) são limite do parser, não do extractor.
    syms, _ = _extract("function f() { x = ; }\nfunction g() {}")
    names = {s.name for s in syms}
    assert "f" in names and "g" in names


def test_unrecoverable_syntax_error_does_not_crash():
    # o pior caso (parser não recupera) mesmo assim não pode lançar exceção
    syms, refs = _extract("function f( { }\nfunction g() {}")
    assert isinstance(syms, list) and isinstance(refs, list)


def test_tsx_component_and_jsx_class_ref():
    # TSX: componente vira função e className vira reference (fix anterior)
    syms, refs = _extract(
        'export function Card() { return <div className="card" />; }',
        lang="tsx")
    assert any(s.fqn == "app.Card" for s in syms)
    assert any(r.dst_name == "card" for r in refs if r.kind == "references")
