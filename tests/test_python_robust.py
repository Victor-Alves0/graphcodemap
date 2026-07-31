"""Bateria de robustez do extractor Python (L0).

Objetivo: cobrir toda a superfície do que o extractor DEVE produzir — símbolos,
fqn, imports, chamadas, herança, resolução/confiança — e os casos de borda que
costumam quebrar. Escrita para EXPOR lacunas antes de corrigir o código; onde um
caso é fora de escopo por decisão, o teste diz isso explicitamente.

Convenção: `_syms`/`_refs` rodam o extractor direto (superfície pura, rápido);
os testes de resolução/confiança montam um CodeGraph real.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(src: str, module="m"):
    src_b = textwrap.dedent(src).encode("utf-8")
    tree = get_parser("python").parse(src_b)
    return extract("python", src_b, module, tree)


def _syms(src: str, module="m"):
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

def test_module_function():
    assert ("function", "m.foo") in _syms("def foo(): pass")


def test_async_function():
    assert ("function", "m.foo") in _syms("async def foo(): pass")


def test_class():
    assert ("class", "m.C") in _syms("class C: pass")


def test_method():
    assert ("method", "m.C.run") in _syms("class C:\n def run(self): pass")


def test_classmethod_and_staticmethod_are_methods():
    s = _syms("""
        class C:
            @classmethod
            def a(cls): pass
            @staticmethod
            def b(): pass
    """)
    assert ("method", "m.C.a") in s
    assert ("method", "m.C.b") in s


def test_nested_function():
    s = _syms("""
        def outer():
            def inner(): pass
    """)
    assert ("function", "m.outer.inner") in s


def test_nested_class():
    s = _syms("""
        class Outer:
            class Inner: pass
    """)
    assert ("class", "m.Outer.Inner") in s


def test_module_constant_and_variable():
    s = _syms("MAX = 3\nname = 'x'\n")
    assert ("constant", "m.MAX") in s
    assert ("variable", "m.name") in s


def test_annotated_assignment_with_value():
    assert ("constant", "m.MAX") in _syms("MAX: int = 3")


def test_annotated_assignment_without_value():
    # `timeout: int` (sem valor) é uma declaração de módulo — vira símbolo
    assert ("variable", "m.timeout") in _syms("timeout: int")


def test_decorated_function_keeps_name():
    s = _syms("""
        import functools
        @functools.cache
        def compute(): pass
    """)
    assert ("function", "m.compute") in s


def test_decorated_class_keeps_name():
    s = _syms("""
        @register
        class Handler: pass
    """)
    assert ("class", "m.Handler") in s


def test_property_is_method():
    s = _syms("""
        class C:
            @property
            def value(self): return 1
    """)
    assert ("method", "m.C.value") in s


def test_class_level_attribute_is_a_symbol():
    # atributo de classe (`count = 0` no corpo da classe) é parte da API do tipo
    s = _syms("""
        class C:
            count = 0
            LIMIT = 100
    """)
    assert ("variable", "m.C.count") in s
    assert ("constant", "m.C.LIMIT") in s


def test_overloaded_functions_do_not_collapse_or_crash():
    # typing.overload: várias defs mesmo nome — ao menos a implementação existe
    s = _syms("""
        from typing import overload
        @overload
        def f(x: int) -> int: ...
        @overload
        def f(x: str) -> str: ...
        def f(x): return x
    """)
    assert ("function", "m.f") in s


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_class():
    s = _syms("class Service:\n def handle(self): pass", module="app.svc")
    assert ("method", "app.svc.Service.handle") in s


def test_deeply_nested_fqn():
    s = _syms("""
        class A:
            class B:
                def m(self): pass
    """)
    assert ("method", "m.A.B.m") in s


def test_parent_fqn_is_set_for_method():
    syms, _ = _extract("class C:\n def run(self): pass")
    run = next(s for s in syms if s.name == "run")
    assert run.parent_fqn == "m.C"


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_underscore_is_private():
    syms = _sym_by_name("def _helper(): pass", "_helper")
    assert syms and syms[0].visibility == "private"


def test_public_function_visibility():
    syms = _sym_by_name("def helper(): pass", "helper")
    assert syms and syms[0].visibility == "public"


# ============================================================================
# D. Docstring e assinatura
# ============================================================================

def test_function_docstring():
    syms = _sym_by_name('def f():\n    """Faz algo."""\n    pass', "f")
    assert syms[0].doc == "Faz algo."


def test_class_docstring():
    syms = _sym_by_name('class C:\n    """Um tipo."""\n    pass', "C")
    assert syms[0].doc == "Um tipo."


def test_signature_excludes_body():
    # a assinatura remove o `:` final por convenção (sig_of tira `:{`)
    syms = _sym_by_name("def add(a, b):\n    return a + b", "add")
    assert syms[0].signature == "def add(a, b)"


def test_async_signature():
    syms = _sym_by_name("async def fetch(url):\n    return url", "fetch")
    assert syms[0].signature.startswith("async def fetch")


# ============================================================================
# E. Imports
# ============================================================================

def test_import_simple():
    assert "os" in _refs("import os", "imports")


def test_import_dotted():
    assert "os.path" in _refs("import os.path", "imports")


def test_import_aliased():
    assert "numpy" in _refs("import numpy as np", "imports")


def test_from_import():
    assert "a.b.foo" in _refs("from a.b import foo", "imports")


def test_from_import_aliased():
    assert "a.b.foo" in _refs("from a.b import foo as f", "imports")


def test_from_import_multiple():
    got = _refs("from a import b, c", "imports")
    assert "a.b" in got and "a.c" in got


def test_wildcard_import():
    assert "a.b.*" in _refs("from a.b import *", "imports")


def test_relative_import_one_dot():
    # from .mod import x  dentro de pkg.sub → pkg.mod.x
    _, refs = _extract("from .mod import x", module="pkg.sub")
    imports = {r.dst_name for r in refs if r.kind == "imports"}
    assert "pkg.mod.x" in imports


def test_relative_import_bare():
    # from . import sibling  dentro de pkg.sub → pkg.sibling
    _, refs = _extract("from . import sibling", module="pkg.sub")
    imports = {r.dst_name for r in refs if r.kind == "imports"}
    assert "pkg.sibling" in imports


def test_relative_import_two_dots():
    # from ..other import x  dentro de pkg.sub.mod → pkg.other.x
    _, refs = _extract("from ..other import x", module="pkg.sub.mod")
    imports = {r.dst_name for r in refs if r.kind == "imports"}
    assert "pkg.other.x" in imports


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("def f():\n    helper()", "calls")


def test_builtin_call_is_ignored():
    # print/len etc. são ruído: não viram aresta
    assert _refs("def f():\n    print(1)\n    len([])", "calls") == set()


def test_shadowed_builtin_is_kept():
    # se há import/def local com nome de builtin, a chamada conta
    got = _refs("from mod import len\ndef f():\n    len(x)", "calls")
    assert any("len" in g for g in got)


def test_self_method_call():
    assert "run" in _refs("class C:\n def a(self):\n  self.run()", "calls")


def test_constructor_call():
    assert "Widget" in _refs("def f():\n    Widget()", "calls")


def test_chained_call_takes_method_name():
    # a().b() : receptor é expressão → só o nome do método
    assert "b" in _refs("def f():\n    a().b()", "calls")


def test_imported_call_is_qualified():
    # from a import foo; foo() → guess qualificado a.foo
    assert "a.foo" in _refs("from a import foo\ndef f():\n    foo()", "calls")


# ============================================================================
# G. Herança
# ============================================================================

def test_simple_inheritance():
    assert "Base" in _refs("class C(Base): pass", "inherits")


def test_multiple_inheritance():
    got = _refs("class C(A, B): pass", "inherits")
    assert "A" in got and "B" in got


def test_qualified_inheritance():
    assert "mod.Base" in _refs("import mod\nclass C(mod.Base): pass", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_import_traced_call_is_inferred(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "def foo():\n    return 1\n",
        "b.py": "from a import foo\n\ndef use():\n    return foo()\n",
    })
    calls = [e for e in _edges(g, "calls") if e["src"] == "b.use"]
    assert any(e["dst"] == "a.foo" and e["conf"] == "inferred" for e in calls)
    g.close()


def test_local_unique_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "def helper():\n    return 1\n\ndef caller():\n    return helper()\n",
    })
    calls = [e for e in _edges(g, "calls") if e["src"] == "a.caller"]
    assert any(e["dst"] == "a.helper" for e in calls)
    g.close()


def test_homonym_method_is_possible_not_inferred(tmp_path):
    # dois métodos `save`, receptor desconhecido → ambíguo → possible
    g = _graph(tmp_path, {
        "a.py": "class A:\n    def save(self): pass\n",
        "b.py": "class B:\n    def save(self): pass\n",
        "c.py": "def go(x):\n    x.save()\n",
    })
    calls = [e for e in _edges(g, "calls")
             if e["src"] == "c.go" and e["dst_name"] == "save"]
    assert calls and all(e["conf"] == "possible" for e in calls)
    g.close()


def test_self_call_resolves_within_class(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "class C:\n    def a(self):\n        self.b()\n    def b(self):\n        pass\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "b"]
    assert any(e["dst"] == "a.C.b" for e in calls)
    g.close()


def test_module_constant_reference_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"cfg.py": "RETRIES = 5\n"})
    info, _ = g.symbol_info("RETRIES")
    assert info["symbol"]["kind"] == "constant"
    assert info["symbol"]["start_line"] == 1
    g.close()


def test_inheritance_edge_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "class Base:\n    pass\n",
        "b.py": "from a import Base\n\nclass Sub(Base):\n    pass\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["src"] == "b.Sub"]
    assert any(e["dst"] == "a.Base" for e in inh)
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_multiple_assignment_captures_all_targets():
    # a = b = 0  → duas constantes/variáveis
    s = _syms("a = b = 0")
    assert ("variable", "m.a") in s
    assert ("variable", "m.b") in s


def test_tuple_unpacking_captures_names():
    # x, y = 1, 2  → dois símbolos de módulo
    s = _syms("x, y = 1, 2")
    assert ("variable", "m.x") in s
    assert ("variable", "m.y") in s


def test_assignment_inside_main_guard_is_captured():
    s = _syms("""
        if __name__ == "__main__":
            CONFIG = load()
    """)
    assert ("constant", "m.CONFIG") in s


def test_dunder_all_is_captured():
    # `__all__` não é UPPER (tem minúsculas) → variable, e é privado (começa _)
    assert ("variable", "m.__all__") in _syms("__all__ = ['a', 'b']")


def test_local_variable_is_not_a_symbol():
    # variável local NÃO é símbolo (só existe no dataflow) — comportamento correto
    s = _syms("def f():\n    tmp = 1\n    return tmp")
    assert not any(fqn == "m.f.tmp" for _, fqn in s)


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    # arquivo com erro de sintaxe: extrai o que der, sem exceção
    syms, _ = _extract("def f(:\n    pass\n\ndef g():\n    pass")
    assert any(s.name == "g" for s in syms)


def test_enum_members_become_class_constants():
    # ganho do fix de atributo de classe: membros de enum viram símbolos
    s = _syms("""
        from enum import Enum
        class Color(Enum):
            RED = 1
            GREEN = 2
    """)
    assert ("constant", "m.Color.RED") in s
    assert ("constant", "m.Color.GREEN") in s


def test_dataclass_fields_become_class_symbols():
    s = _syms("""
        from dataclasses import dataclass
        @dataclass
        class Point:
            x: int
            y: int
    """)
    assert ("variable", "m.Point.x") in s
    assert ("variable", "m.Point.y") in s


def test_self_attribute_assignment_is_not_a_symbol():
    # `self.x = 1` é atributo de INSTÂNCIA, não declaração de símbolo — fronteira
    s = _syms("""
        class C:
            def __init__(self):
                self.x = 1
    """)
    assert not any(fqn.endswith(".x") for _, fqn in s)


def test_subscript_assignment_is_not_a_symbol():
    # `d[k] = v` no módulo não declara símbolo
    s = _syms("d = {}\nd['k'] = 1\n")
    assert ("variable", "m.d") in s
    assert not any(fqn == "m.k" for _, fqn in s)


def test_decorator_call_is_not_an_edge_by_design():
    # fronteira deliberada: `@app.route(...)` NÃO vira aresta de call — a maioria
    # dos decoradores é fiação de framework/stdlib e poluiria o grafo. Só o que
    # o corpo chama conta. (Se quisermos wiring de framework, é feature à parte.)
    got = _refs("""
        @app.route("/x")
        def handler():
            do_work()
    """, "calls")
    assert "do_work" in got
    assert not any("route" in g for g in got)
