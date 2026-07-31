"""Bateria de robustez do extractor Ruby (L0).

Ruby é dinâmica, então o checklist do padrão se adapta: propriedade = `attr_*`,
visibilidade = SECCIONAL (private/public são chamadas que alternam o estado dos
métodos seguintes, como os access specifiers do C++), herança inclui os MIXINS
(include/extend/prepend). Mesmo método: expor lacunas antes de corrigir.

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
    tree = get_parser("ruby").parse(src_b)
    return extract("ruby", src_b, module, tree)


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

def test_top_level_method_is_function():
    assert ("function", "app.run") in _syms("def run\nend")


def test_module():
    assert ("module", "app.Helpers") in _syms("module Helpers\nend")


def test_class():
    assert ("class", "app.Widget") in _syms("class Widget\nend")


def test_method():
    assert ("method", "app.C.run") in _syms("class C\n def run\n end\nend")


def test_singleton_method():
    assert ("method", "app.C.create") in _syms(
        "class C\n def self.create\n end\nend")


def test_initialize_method():
    assert ("method", "app.C.initialize") in _syms(
        "class C\n def initialize\n end\nend")


def test_top_level_constant():
    assert ("constant", "app.MAX") in _syms("MAX = 3")


def test_class_constant_is_a_symbol():
    # constante dentro da classe (o guard `if self.scope: return` a descartava)
    s = _syms("class C\n VERSION = 1\nend")
    assert any(fqn == "app.C.VERSION" for _, fqn in s)


def test_attr_accessor_is_a_symbol():
    # attr_accessor é o idioma de propriedade do Ruby
    s = _syms("class C\n attr_accessor :name, :age\nend")
    assert any(fqn == "app.C.name" for _, fqn in s)
    assert any(fqn == "app.C.age" for _, fqn in s)


def test_attr_reader_is_a_symbol():
    s = _syms("class C\n attr_reader :id\nend")
    assert any(fqn == "app.C.id" for _, fqn in s)


def test_attr_writer_is_a_symbol():
    s = _syms("class C\n attr_writer :value\nend")
    assert any(fqn == "app.C.value" for _, fqn in s)


def test_nested_module_and_class():
    s = _syms("module A\n class B\n  def m\n  end\n end\nend")
    assert ("module", "app.A") in s
    assert ("class", "app.A.B") in s
    assert ("method", "app.A.B.m") in s


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_class():
    assert ("method", "app.Service.handle") in _syms(
        "class Service\n def handle\n end\nend")


def test_parent_fqn_for_attr():
    syms, _ = _extract("class C\n attr_reader :x\nend")
    x = [s for s in syms if s.name == "x"]
    assert x and x[0].parent_fqn == "app.C"


def test_compact_nested_class():
    # class A::B  (definição compacta)
    s = _syms("class A::B\n def m\n end\nend")
    assert any(fqn.endswith("B.m") for _, fqn in s)


# ============================================================================
# C. Visibilidade (seccional)
# ============================================================================

def test_default_method_is_public():
    syms = _sym_by_name("class C\n def x\n end\nend", "x")
    assert syms and syms[0].visibility == "public"


def test_method_after_private_is_private():
    syms = _sym_by_name(
        "class C\n def a\n end\n private\n def b\n end\nend", "b")
    assert syms and syms[0].visibility == "private"


def test_method_before_private_stays_public():
    syms = _sym_by_name(
        "class C\n def a\n end\n private\n def b\n end\nend", "a")
    assert syms and syms[0].visibility == "public"


def test_protected_section():
    syms = _sym_by_name(
        "class C\n protected\n def guard\n end\nend", "guard")
    assert syms and syms[0].visibility == "protected"


def test_public_resets_after_private():
    syms = _sym_by_name(
        "class C\n private\n def a\n end\n public\n def b\n end\nend", "b")
    assert syms and syms[0].visibility == "public"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_doc_comment():
    syms = _sym_by_name("# Faz algo.\ndef run\nend", "run")
    assert syms and syms[0].doc == "Faz algo."


# ============================================================================
# E. Imports (require)
# ============================================================================

def test_require():
    assert "json" in _refs('require "json"', "imports")


def test_require_relative():
    assert "lib.helper" in _refs('require_relative "lib/helper"', "imports")


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call_with_parens():
    assert "helper" in _refs("def f\n helper()\nend", "calls")


def test_call_with_args():
    assert "render" in _refs("def f\n render :ok\nend", "calls")


def test_method_call_takes_name():
    assert "run" in _refs("def f(x)\n x.run\nend", "calls")


def test_bare_identifier_is_not_a_call():
    # fronteira: `helper` sozinho (sem parênteses/args) é ambíguo com variável
    # local — o tree-sitter o parseia como identifier, não call. Capturá-lo
    # cegamente inventaria arestas para toda leitura de variável. Precisa de
    # rastreio de locais (L1/solargraph). Com parênteses ou args, é captado.
    assert "helper" not in _refs("def f\n helper\nend", "calls")


def test_require_is_not_a_call():
    # require vira import, não call
    assert "require" not in _refs('require "json"', "calls")


# ============================================================================
# G. Herança
# ============================================================================

def test_superclass_is_inherits():
    assert "Base" in _refs("class C < Base\nend", "inherits")


def test_qualified_superclass():
    got = _refs("class C < App::Base\nend", "inherits")
    assert "Base" in got


def test_include_is_inherits():
    # include Module é o mixin do Ruby → herança/composição
    assert "Comparable" in _refs("class C\n include Comparable\nend", "inherits")


def test_extend_is_inherits():
    assert "Forwardable" in _refs("class C\n extend Forwardable\nend", "inherits")


def test_prepend_is_inherits():
    assert "Loggable" in _refs("class C\n prepend Loggable\nend", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.rb": "def helper\n 1\nend\ndef caller\n helper()\nend\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_inheritance_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.rb": "class Base\nend\nclass Sub < Base\nend\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_attr_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.rb": "class C\n attr_accessor :retries\nend\n"})
    info, _ = g.symbol_info("retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


def test_include_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.rb": "module Walkable\nend\nclass Dog\n include Walkable\nend\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Walkable")]
    assert any(e["dst"] and e["dst"].endswith("Walkable") for e in inh)
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_method_with_question_mark():
    assert ("method", "app.C.valid?") in _syms(
        "class C\n def valid?\n end\nend")


def test_method_with_bang():
    assert ("method", "app.C.save!") in _syms(
        "class C\n def save!\n end\nend")


def test_operator_method():
    s = _syms("class C\n def +(other)\n end\nend")
    assert any(fqn == "app.C.+" for _, fqn in s)


def test_block_call_is_captured():
    got = _refs("def f\n [1,2].each { |x| work(x) }\nend", "calls")
    assert "work" in got


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("def f(\n\ndef g\nend")
    assert isinstance(syms, list)
