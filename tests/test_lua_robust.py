"""Bateria de robustez do extractor Lua/Luau (L0).

Lua é minimalista e dinâmica: sem classes ou keywords de visibilidade. O
checklist do padrão se adapta — visibilidade = `local` (privado ao arquivo) vs
global (público); "membro" = campo de tabela e variável de módulo; tabela faz
papel de módulo/objeto. Mesmo método: expor lacunas antes de corrigir.

`_syms`/`_refs` rodam o extractor direto (module="app"); resolução monta grafo.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(src: str, lang="lua", module="app"):
    src_b = textwrap.dedent(src).encode("utf-8")
    tree = get_parser(lang).parse(src_b)
    return extract(lang, src_b, module, tree)


def _syms(src: str, lang="lua", module="app"):
    syms, _ = _extract(src, lang, module)
    return {(s.kind, s.fqn) for s in syms}


def _sym_by_name(src: str, name: str, lang="lua"):
    syms, _ = _extract(src, lang)
    return [s for s in syms if s.name == name]


def _refs(src: str, kind: str, lang="lua"):
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

def test_global_function():
    assert ("function", "app.run") in _syms("function run() end")


def test_local_function():
    assert ("function", "app.helper") in _syms("local function helper() end")


def test_table_method_dot():
    assert ("method", "app.M.foo") in _syms(
        "local M = {}\nfunction M.foo() end")


def test_table_method_colon():
    assert ("method", "app.Obj.greet") in _syms(
        "local Obj = {}\nfunction Obj:greet() end")


def test_module_table_is_a_symbol():
    # local M = {} — a tabela-módulo deveria ser navegável
    s = _syms("local M = {}\nfunction M.foo() end")
    assert any(fqn == "app.M" for _, fqn in s)


def test_local_variable_is_a_symbol():
    assert ("variable", "app.count") in _syms("local count = 0")


def test_global_variable_is_a_symbol():
    s = _syms("Config = 5")
    assert any(fqn == "app.Config" for _, fqn in s)


def test_uppercase_local_is_constant():
    s = _syms("local MAX = 100")
    assert ("constant", "app.MAX") in s


def test_table_field_is_a_symbol():
    # campo em construtor de tabela
    s = _syms("local M = {\n name = 'x',\n count = 0,\n}")
    assert any(fqn == "app.M.name" for _, fqn in s)
    assert any(fqn == "app.M.count" for _, fqn in s)


def test_table_field_function_is_a_method():
    s = _syms("local M = {\n foo = function() end,\n}")
    assert any(fqn == "app.M.foo" for _, fqn in s)


def test_field_assignment_function():
    # M.bar = function() end — função atribuída a campo
    s = _syms("local M = {}\nM.bar = function() end")
    assert any(fqn == "app.M.bar" for _, fqn in s)


def test_multiple_locals_one_line():
    s = _syms("local a, b = 1, 2")
    assert any(fqn == "app.a" for _, fqn in s)
    assert any(fqn == "app.b" for _, fqn in s)


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_table():
    assert ("method", "app.Service.handle") in _syms(
        "local Service = {}\nfunction Service.handle() end")


def test_parent_fqn_for_table_method():
    syms, _ = _extract("local M = {}\nfunction M.foo() end")
    foo = [s for s in syms if s.name == "foo"]
    assert foo and foo[0].parent_fqn == "app.M"


def test_nested_function():
    s = _syms("function outer()\n local function inner() end\nend")
    assert ("function", "app.outer") in s


# ============================================================================
# C. Visibilidade (local vs global)
# ============================================================================

def test_local_function_is_private():
    syms = _sym_by_name("local function helper() end", "helper")
    assert syms and syms[0].visibility == "private"


def test_global_function_is_public():
    syms = _sym_by_name("function run() end", "run")
    assert syms and syms[0].visibility == "public"


def test_local_variable_is_private():
    syms = _sym_by_name("local x = 1", "x")
    assert syms and syms[0].visibility == "private"


def test_global_variable_is_public():
    syms = _sym_by_name("Config = 1", "Config")
    assert syms and syms[0].visibility == "public"


# ============================================================================
# D. Imports (require)
# ============================================================================

def test_require():
    assert "socket" in _refs('local s = require("socket")', "imports")


def test_require_path():
    assert "app.utils" in _refs('require("app/utils")', "imports")


# ============================================================================
# E. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("function f()\n helper()\nend", "calls")


def test_table_call_takes_field():
    assert "foo" in _refs("function f()\n M.foo()\nend", "calls")


def test_method_call_takes_name():
    assert "greet" in _refs("function f()\n obj:greet()\nend", "calls")


def test_require_is_not_a_call():
    assert "require" not in _refs('require("x")', "calls")


# ============================================================================
# F. Resolução (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.lua": "local function helper() return 1 end\n"
                 "function caller() return helper() end\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_table_method_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.lua": "local M = {}\nfunction M.foo() end\n"
                 "function M.bar() M.foo() end\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "foo"]
    assert any(e["dst"] and e["dst"].endswith("M.foo") for e in calls)
    g.close()


def test_local_var_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.lua": "local retries = 5\n"})
    info, _ = g.symbol_info("retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


# ============================================================================
# G. Casos de borda
# ============================================================================

def test_luau_typed_local():
    # Luau: local x: number = 5
    s = _syms("local x: number = 5", lang="luau")
    assert any(fqn == "app.x" for _, fqn in s)


def test_closure_call_is_captured():
    got = _refs("function f()\n local g = function() work() end\nend", "calls")
    assert "work" in got


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("function f( end\nfunction g() end")
    assert isinstance(syms, list)
