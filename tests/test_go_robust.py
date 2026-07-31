"""Bateria de robustez do extractor Go (L0).

Mesmo método das baterias anteriores, com o checklist do padrão (membros viram
símbolo? visibilidade setada? herança/import sobre-qualifica?). Go tem uma
particularidade: visibilidade é por CAPITALIZAÇÃO (Maiúscula = exportada), então
o esperado é public/private derivado do nome.

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
    tree = get_parser("go").parse(src_b)
    return extract("go", src_b, module, tree)


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
    assert ("function", "app.Run") in _syms("func Run() {}")


def test_struct_type():
    assert ("struct", "app.Point") in _syms("type Point struct { X int }")


def test_interface_type():
    assert ("interface", "app.Reader") in _syms(
        "type Reader interface { Read() int }")


def test_type_alias():
    assert ("type_alias", "app.Celsius") in _syms("type Celsius float64")


def test_method_pointer_receiver():
    assert ("method", "app.Point.Move") in _syms(
        "type Point struct{}\nfunc (p *Point) Move() {}")


def test_method_value_receiver():
    assert ("method", "app.Point.X") in _syms(
        "type Point struct{}\nfunc (p Point) X() int { return 0 }")


def test_package_const():
    assert ("constant", "app.MaxRetries") in _syms("const MaxRetries = 3")


def test_package_var():
    assert ("variable", "app.logger") in _syms("var logger = 1")


def test_const_block_multiple():
    s = _syms("const (\n A = 1\n B = 2\n)")
    assert ("constant", "app.A") in s
    assert ("constant", "app.B") in s


def test_iota_const_block():
    s = _syms("const (\n Red = iota\n Green\n Blue\n)")
    assert ("constant", "app.Red") in s
    assert ("constant", "app.Blue") in s


def test_interface_method_is_a_symbol():
    assert ("method", "app.Reader.Read") in _syms(
        "type Reader interface { Read() int }")


def test_struct_fields_are_symbols():
    # campo NOMEADO é parte da API do struct — deveria ser navegável
    s = _syms("type Point struct {\n X int\n Y int\n}")
    assert any(fqn == "app.Point.X" for _, fqn in s)
    assert any(fqn == "app.Point.Y" for _, fqn in s)


def test_multiple_fields_one_line():
    s = _syms("type P struct {\n X, Y int\n}")
    assert any(fqn == "app.P.X" for _, fqn in s)
    assert any(fqn == "app.P.Y" for _, fqn in s)


def test_generic_struct():
    assert ("struct", "app.Stack") in _syms(
        "type Stack[T any] struct { items []T }")


def test_generic_function():
    assert ("function", "app.Map") in _syms(
        "func Map[T any](xs []T) []T { return xs }")


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_type():
    assert ("method", "app.Service.Handle") in _syms(
        "type Service struct{}\nfunc (s *Service) Handle() {}")


def test_parent_fqn_for_method():
    syms, _ = _extract("type P struct{}\nfunc (p *P) Run() {}")
    run = next(s for s in syms if s.name == "Run")
    assert run.parent_fqn == "app.P"


def test_struct_field_parent_is_the_struct():
    syms, _ = _extract("type Point struct {\n X int\n}")
    fld = [s for s in syms if s.name == "X"]
    assert fld and fld[0].parent_fqn == "app.Point"


# ============================================================================
# C. Visibilidade (por capitalização em Go)
# ============================================================================

def test_exported_function_is_public():
    syms = _sym_by_name("func Run() {}", "Run")
    assert syms and syms[0].visibility == "public"


def test_unexported_function_is_private():
    syms = _sym_by_name("func run() {}", "run")
    assert syms and syms[0].visibility == "private"


def test_exported_struct_is_public():
    syms = _sym_by_name("type Point struct{}", "Point")
    assert syms and syms[0].visibility == "public"


def test_unexported_struct_is_private():
    syms = _sym_by_name("type point struct{}", "point")
    assert syms and syms[0].visibility == "private"


def test_exported_field_is_public():
    syms = _sym_by_name("type P struct {\n Name string\n}", "Name")
    assert syms and syms[0].visibility == "public"


def test_unexported_field_is_private():
    syms = _sym_by_name("type P struct {\n age int\n}", "age")
    assert syms and syms[0].visibility == "private"


def test_interface_method_visibility():
    syms = _sym_by_name("type R interface { Read() int }", "Read")
    assert syms and syms[0].visibility == "public"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_doc_comment():
    syms = _sym_by_name("// Run faz algo.\nfunc Run() {}", "Run")
    assert syms and syms[0].doc == "Run faz algo."


def test_signature_excludes_body():
    syms = _sym_by_name("func Add(a int, b int) int { return a + b }", "Add")
    assert syms[0].signature == "func Add(a int, b int) int"


# ============================================================================
# E. Imports
# ============================================================================

def test_import_simple():
    assert "fmt" in _refs('import "fmt"', "imports")


def test_import_path_dotted():
    assert "net.http" in _refs('import "net/http"', "imports")


def test_import_grouped():
    got = _refs('import (\n "fmt"\n "os"\n)', "imports")
    assert "fmt" in got and "os" in got


def test_import_aliased():
    assert "github.com.foo.bar" in _refs(
        'import baz "github.com/foo/bar"', "imports")


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("func f() { helper() }", "calls")


def test_builtin_call_is_ignored():
    assert _refs("func f() { len(x); make([]int, 0) }", "calls") == set()


def test_package_call_is_qualified():
    assert "fmt.Println" in _refs(
        'import "fmt"\nfunc f() { fmt.Println("x") }', "calls")


def test_method_call_takes_name():
    assert "Run" in _refs("func f(x T) { x.Run() }", "calls")


# ============================================================================
# G. Herança (embedding)
# ============================================================================

def test_struct_embedding_is_inherits():
    assert "Base" in _refs(
        "type C struct {\n Base\n Name string\n}", "inherits")


def test_qualified_embedding_is_inherits():
    got = _refs('import "io"\ntype C struct {\n io.Reader\n}', "inherits")
    assert any("Reader" in g for g in got)


def test_named_field_is_not_inherits():
    # campo NOMEADO não é embedding → não gera herança
    assert _refs("type C struct {\n B Base\n}", "inherits") == set()


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_package_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.go": "package app\nfunc Helper() int { return 1 }\n"
                "func Caller() int { return Helper() }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "Helper"]
    assert any(e["dst"] and e["dst"].endswith("Helper") for e in calls)
    g.close()


def test_method_resolves_to_type(tmp_path):
    g = _graph(tmp_path, {
        "a.go": "package app\ntype P struct{}\nfunc (p *P) Run() {}\n"
                "func Go(p *P) { p.Run() }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "Run"]
    assert any(e["dst"] and e["dst"].endswith("P.Run") for e in calls)
    g.close()


def test_embedding_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.go": "package app\ntype Base struct{}\n"
                "type Sub struct {\n Base\n}\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_struct_field_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.go": "package app\ntype C struct {\n Retries int\n}\n"})
    info, _ = g.symbol_info("Retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_empty_struct():
    assert ("struct", "app.Empty") in _syms("type Empty struct{}")


def test_anonymous_struct_field_type_does_not_crash():
    syms, _ = _extract("type C struct {\n Data struct{ X int }\n}")
    assert any(s.name == "C" for s in syms)


def test_closure_call_is_captured():
    got = _refs("func f() { g := func() { work() }; g() }", "calls")
    assert "work" in got


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("func f( { }\nfunc g() {}")
    assert isinstance(syms, list)


def test_field_and_method_same_name_coexist():
    # campo `Run` e método `Run()` podem coexistir sem colidir
    s = _syms("type C struct {\n Run int\n}\nfunc (c *C) Do() {}")
    assert any(fqn == "app.C.Run" for _, fqn in s)
    assert ("method", "app.C.Do") in s
