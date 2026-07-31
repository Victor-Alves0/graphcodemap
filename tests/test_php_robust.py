"""Bateria de robustez do extractor PHP (L0).

Mesmo método das baterias anteriores, com o checklist do padrão (membros viram
símbolo? visibilidade setada? herança/import sobre-qualifica?) + o novo item
(namespace entra no fqn? nós irmãos inesperados?).

`_syms`/`_refs` rodam o extractor direto (module="app"); resolução monta grafo.
PHP exige a tag de abertura `<?php`.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser

OPEN = "<?php\n"


def _extract(src: str, module="app"):
    src_b = (OPEN + textwrap.dedent(src)).encode("utf-8")
    tree = get_parser("php").parse(src_b)
    return extract("php", src_b, module, tree)


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
        p.write_text(body, encoding="utf-8")
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


def test_class():
    assert ("class", "app.Widget") in _syms("class Widget {}")


def test_interface():
    assert ("interface", "app.Shape") in _syms("interface Shape { public function area(); }")


def test_trait():
    assert ("class", "app.Loggable") in _syms("trait Loggable {}")


def test_enum():
    assert ("enum", "app.Suit") in _syms("enum Suit { case Hearts; }")


def test_method():
    assert ("method", "app.C.run") in _syms("class C { function run() {} }")


def test_static_method():
    assert ("method", "app.C.create") in _syms(
        "class C { public static function create() {} }")


def test_constructor():
    s = _syms("class C { public function __construct() {} }")
    assert any(fqn == "app.C.__construct" for _, fqn in s)


def test_top_level_const():
    assert ("constant", "app.MAX") in _syms("const MAX = 3;")


def test_property_is_a_symbol():
    s = _syms("class C { private $count; }")
    assert any(fqn == "app.C.count" or fqn == "app.C.$count" for _, fqn in s)


def test_typed_property_is_a_symbol():
    s = _syms("class C { public string $name; }")
    assert any(fqn.endswith(".name") or fqn.endswith(".$name") for _, fqn in s)


def test_class_constant_is_a_symbol():
    s = _syms("class C { const VERSION = 1; }")
    assert any(fqn == "app.C.VERSION" for _, fqn in s)


def test_enum_cases_are_symbols():
    s = _syms("enum Suit { case Hearts; case Spades; }")
    assert any(fqn == "app.Suit.Hearts" for _, fqn in s)
    assert any(fqn == "app.Suit.Spades" for _, fqn in s)


def test_interface_method_is_a_symbol():
    assert ("method", "app.Repo.save") in _syms(
        "interface Repo { public function save(); }")


# ============================================================================
# B. FQN e contenção
# ============================================================================

def test_method_fqn_includes_class():
    assert ("method", "app.Service.handle") in _syms(
        "class Service { function handle() {} }")


def test_parent_fqn_for_property():
    syms, _ = _extract("class C { private $x; }")
    x = [s for s in syms if s.name in ("x", "$x")]
    assert x and x[0].parent_fqn == "app.C"


def test_namespace_in_fqn():
    # namespace App; class Widget {}  → deveria compor o fqn
    assert ("class", "app.App.Widget") in _syms(
        "namespace App;\nclass Widget {}")


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_private_method_visibility():
    syms = _sym_by_name("class C { private function x() {} }", "x")
    assert syms and syms[0].visibility == "private"


def test_public_method_visibility():
    syms = _sym_by_name("class C { public function x() {} }", "x")
    assert syms and syms[0].visibility == "public"


def test_protected_method_visibility():
    syms = _sym_by_name("class C { protected function x() {} }", "x")
    assert syms and syms[0].visibility == "protected"


def test_property_visibility():
    syms = [s for s in _sym_by_name("class C { private $secret; }", "secret")
            or _sym_by_name("class C { private $secret; }", "$secret")]
    assert syms and syms[0].visibility == "private"


# ============================================================================
# D. Doc e assinatura
# ============================================================================

def test_phpdoc_on_function():
    syms = _sym_by_name("/** Faz algo. */\nfunction run() {}", "run")
    assert syms and syms[0].doc == "Faz algo."


def test_signature_excludes_body():
    syms = _sym_by_name("function add($a, $b) { return $a + $b; }", "add")
    assert syms[0].signature == "function add($a, $b)"


# ============================================================================
# E. Imports (use)
# ============================================================================

def test_use_simple():
    assert "App.Models.User" in _refs("use App\\Models\\User;", "imports")


def test_use_aliased():
    assert "App.Models.User" in _refs("use App\\Models\\User as U;", "imports")


# ============================================================================
# F. Chamadas
# ============================================================================

def test_direct_call():
    assert "helper" in _refs("function f() { helper(); }", "calls")


def test_method_call_takes_name():
    assert "run" in _refs("function f($x) { $x->run(); }", "calls")


def test_static_call_takes_name():
    assert "create" in _refs("function f() { Factory::create(); }", "calls")


def test_new_expression_is_a_call():
    assert "Widget" in _refs("function f() { new Widget(); }", "calls")


# ============================================================================
# G. Herança
# ============================================================================

def test_class_extends():
    assert "Base" in _refs("class C extends Base {}", "inherits")


def test_class_implements():
    assert "Countable" in _refs("class C implements Countable {}", "inherits")


def test_implements_multiple():
    got = _refs("class C implements A, B {}", "inherits")
    assert "A" in got and "B" in got


def test_trait_use_is_inherits():
    assert "Loggable" in _refs("class C { use Loggable; }", "inherits")


def test_interface_extends():
    assert "Base" in _refs("interface C extends Base {}", "inherits")


# ============================================================================
# H. Resolução e confiança (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.php": "<?php\nfunction helper() { return 1; }\n"
                 "function caller() { return helper(); }\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_inheritance_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.php": "<?php\nclass Base {}\nclass Sub extends Base {}\n",
    })
    inh = [e for e in _edges(g, "inherits") if e["dst_name"].endswith("Base")]
    assert any(e["dst"] and e["dst"].endswith("Base") for e in inh)
    g.close()


def test_property_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.php": "<?php\nclass C { private $retries; }\n"})
    # o nome pode ou não incluir o $ — busca pelos dois
    try:
        info, _ = g.symbol_info("retries")
    except Exception:
        info, _ = g.symbol_info("$retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


# ============================================================================
# I. Casos de borda
# ============================================================================

def test_abstract_class_and_method():
    s = _syms("abstract class C { abstract public function run(); }")
    assert ("class", "app.C") in s
    assert ("method", "app.C.run") in s


def test_arrow_function_does_not_crash():
    syms, _ = _extract("function f() { $g = fn($x) => $x + 1; }")
    assert any(s.name == "f" for s in syms)


def test_multiple_properties_one_line():
    s = _syms("class C { public $a, $b; }")
    assert any(fqn.endswith(".a") or fqn.endswith(".$a") for _, fqn in s)
    assert any(fqn.endswith(".b") or fqn.endswith(".$b") for _, fqn in s)


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    syms, _ = _extract("function f( { }\nfunction g() {}")
    assert isinstance(syms, list)
