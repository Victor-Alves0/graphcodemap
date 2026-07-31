"""Bateria de robustez do extractor Clojure/ClojureScript (L0).

Clojure é Lisp — sem classes no sentido usual. O checklist do padrão se adapta:
"membro" = campo de defrecord/deftype e assinatura de método de defprotocol;
"visibilidade" = ^:private / defn-; "herança" = protocolos implementados por um
defrecord. O significado vem da CABEÇA de cada forma (list_lit).

`_syms`/`_refs` rodam o extractor direto; o fqn usa o namespace declarado no
`(ns …)` quando presente, senão o module passado (default "app").
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(src: str, module="app"):
    src_b = textwrap.dedent(src).encode("utf-8")
    tree = get_parser("clojure").parse(src_b)
    return extract("clojure", src_b, module, tree)


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

def test_def_is_constant():
    assert ("constant", "app.max-retries") in _syms("(def max-retries 3)")


def test_defn_is_function():
    assert ("function", "app.run") in _syms("(defn run [] nil)")


def test_defn_private():
    assert ("function", "app.helper") in _syms("(defn- helper [] nil)")


def test_defmacro_is_function():
    assert ("function", "app.unless") in _syms("(defmacro unless [c body] nil)")


def test_defmulti_is_function():
    assert ("function", "app.area") in _syms("(defmulti area :type)")


def test_defprotocol_is_interface():
    assert ("interface", "app.Shape") in _syms(
        "(defprotocol Shape (area [this]))")


def test_defrecord_is_class():
    assert ("class", "app.Circle") in _syms(
        "(defrecord Circle [radius])")


def test_deftype_is_class():
    assert ("class", "app.Point") in _syms("(deftype Point [x y])")


def test_defmethod_is_method():
    s = _syms("(defmethod area :circle [c] nil)")
    assert any(fqn == "app.area:circle" for _, fqn in s)


def test_defrecord_fields_are_symbols():
    # campos de um defrecord são a "forma" dos dados — deveriam ser navegáveis
    s = _syms("(defrecord Point [x y])")
    assert any(fqn == "app.Point.x" for _, fqn in s)
    assert any(fqn == "app.Point.y" for _, fqn in s)


def test_deftype_fields_are_symbols():
    s = _syms("(deftype Vec [dx dy])")
    assert any(fqn == "app.Vec.dx" for _, fqn in s)


def test_defprotocol_methods_are_symbols():
    # assinaturas de método do protocolo devem virar method, não call
    s = _syms("(defprotocol Shape\n (area [this])\n (perimeter [this]))")
    assert ("method", "app.Shape.area") in s
    assert ("method", "app.Shape.perimeter") in s


# ============================================================================
# B. FQN e namespace
# ============================================================================

def test_ns_sets_fqn():
    # (ns …) define o module_fqn real das defs
    assert ("function", "myapp.core.run") in _syms(
        "(ns myapp.core)\n(defn run [] nil)")


def test_defrecord_field_parent_is_the_record():
    syms, _ = _extract("(defrecord Point [x y])")
    x = [s for s in syms if s.name == "x"]
    assert x and x[0].parent_fqn == "app.Point"


def test_protocol_method_fqn_includes_protocol():
    assert ("method", "app.Repo.save") in _syms(
        "(defprotocol Repo (save [this]))")


# ============================================================================
# C. Visibilidade
# ============================================================================

def test_defn_dash_is_private():
    syms = _sym_by_name("(defn- helper [] nil)", "helper")
    assert syms and syms[0].visibility == "private"


def test_meta_private_def():
    syms = _sym_by_name("(def ^:private secret 1)", "secret")
    assert syms and syms[0].visibility == "private"


def test_defn_default_has_no_private():
    syms = _sym_by_name("(defn run [] nil)", "run")
    assert syms and syms[0].visibility != "private"


# ============================================================================
# D. Imports (:require)
# ============================================================================

def test_require_simple():
    got = _refs("(ns app (:require [clojure.string]))", "imports")
    assert "clojure.string" in got


def test_require_with_alias():
    got = _refs("(ns app (:require [clojure.string :as str]))", "imports")
    assert "clojure.string" in got


def test_aliased_call_is_qualified():
    # str/upper-case → clojure.string.upper-case via o alias do :require
    got = _refs(
        "(ns app (:require [clojure.string :as str]))\n"
        "(defn f [] (str/upper-case \"x\"))", "calls")
    assert "clojure.string.upper-case" in got


# ============================================================================
# E. Chamadas
# ============================================================================

def test_call_head_is_captured():
    assert "helper" in _refs("(defn f [] (helper))", "calls")


def test_special_form_is_not_a_call():
    # if/let/when etc. são formas especiais, não chamadas
    calls = _refs("(defn f [] (if true 1 2))", "calls")
    assert "if" not in calls


def test_nested_call_is_captured():
    got = _refs("(defn f [] (outer (inner)))", "calls")
    assert "inner" in got and "outer" in got


# ============================================================================
# F. Resolução (grafo real)
# ============================================================================

def test_local_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "a.clj": "(defn helper [] 1)\n(defn caller [] (helper))\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"] == "helper"]
    assert any(e["dst"] and e["dst"].endswith("helper") for e in calls)
    g.close()


def test_defrecord_field_visible_via_symbol_info(tmp_path):
    g = _graph(tmp_path, {"a.clj": "(defrecord Config [retries])\n"})
    info, _ = g.symbol_info("retries")
    assert info["symbol"]["kind"] == "variable"
    g.close()


def test_cross_ns_call_resolves(tmp_path):
    g = _graph(tmp_path, {
        "util.clj": "(ns app.util)\n(defn check [] 1)\n",
        "core.clj": "(ns app.core (:require [app.util :as u]))\n"
                    "(defn run [] (u/check))\n",
    })
    calls = [e for e in _edges(g, "calls") if e["dst_name"].endswith("check")]
    assert any(e["dst"] and e["dst"].endswith("check") for e in calls)
    g.close()


# ============================================================================
# G. Casos de borda
# ============================================================================

def test_defrecord_with_protocol_impl():
    # (defrecord C [x] Shape (area [this] ...)) — o record e o campo existem
    s = _syms("(defrecord Circle [radius]\n Shape\n (area [this] radius))")
    assert ("class", "app.Circle") in s
    assert any(fqn == "app.Circle.radius" for _, fqn in s)


def test_multi_arity_defn():
    s = _syms("(defn f\n ([] 0)\n ([x] x))")
    assert ("function", "app.f") in s


def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_comment_form_does_not_crash():
    syms, _ = _extract("(comment (defn ignored [] nil))\n(defn real [] nil)")
    assert any(s.name == "real" for s in syms)
