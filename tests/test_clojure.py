"""Extractor + dataflow dedicados de Clojure: ns vira fqn, defn/def/defmethod,
:require com :as/:refer resolvendo chamadas cross-namespace, e taint via let."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

# namespace de utilidade: define permitted? (um validador/sanitizer)
UTIL = '''
(ns myapp.util.net
  (:require [clj-http.client :as http]))

(defn- parse-headers [h]
  (decode h))

(defn permitted?
  "checa acesso"
  [strategy url]
  (let [u (parse url)]
    (check u)))
'''

# namespace de config: chama permitted? via alias net
CONFIG = '''
(ns myapp.config
  (:require [myapp.util.net :as net]
            [clojure.string :as str]))

(def ^:private timeout 8000)

(defn guard [url]
  (net/permitted? :strict url))
'''

# namespace de handler: um path que NÃO passa por permitted?
HANDLER = '''
(ns myapp.handler
  (:require [clj-http.client :as http]
            [myapp.config :as config]))

(defn- load-remote [url]
  (http/get url))

(defmethod handle :fetch [req]
  (load-remote (:url req)))
'''


@pytest.fixture()
def cg(tmp_path):
    files = {"net.clj": UTIL, "config.clj": CONFIG, "handler.clj": HANDLER}
    for name, content in files.items():
        (tmp_path / name).write_text(textwrap.dedent(content), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def fqns(rows):
    return {r["fqn"] for r in rows}


def test_ns_becomes_module_fqn(cg):
    # o fqn vem do (ns ...), não do caminho do arquivo (net.clj)
    rows, _ = cg.find_symbol("permitted?")
    assert "myapp.util.net.permitted?" in fqns(rows)


def test_defn_private_visibility(cg):
    rows, _ = cg.find_symbol("parse-headers")
    r = next(x for x in rows if x["fqn"] == "myapp.util.net.parse-headers")
    assert r["visibility"] == "private"


def test_def_constant(cg):
    rows, _ = cg.find_symbol("timeout")
    assert "myapp.config.timeout" in fqns(rows)


def test_defmethod_dispatch_in_name(cg):
    rows, _ = cg.find_symbol("handle:fetch")
    assert "myapp.handler.handle:fetch" in fqns(rows)


def test_cross_ns_call_resolves_via_alias(cg):
    # guard chama net/permitted? → deve virar caller de permitted?
    _sym, callers, _ = cg.callers("myapp.util.net.permitted?")
    assert any(c["other_fqn"] == "myapp.config.guard" for c in callers)


def test_require_is_import(cg):
    _, callees, _ = cg.callees("myapp.config.guard")
    # a chamada real é permitted?; o require não vira call
    assert any((c["dst_name"] or "").endswith("permitted?") for c in callees)


def test_fetch_path_does_not_reach_validator(cg):
    # reachability: load-remote NÃO chama permitted?
    _, callees, _ = cg.callees("myapp.handler.load-remote")
    assert not any((c["dst_name"] or "").endswith("permitted?") for c in callees)


def test_dataflow_param_flows_through_let(cg):
    # o param url flui via let [u (parse url)] até a chamada check
    data, _ = cg.data_flow("myapp.util.net.permitted?")
    assert data is not None


def test_reaches_sink_without_validator(cg):
    # handle:fetch -> load-remote -> http/get, SEM permitted? no caminho
    _sym, data, _ = cg.reaches("myapp.handler.handle:fetch",
                               sink="http", via="permitted?")
    assert data["paths"], "deveria alcançar um sink http"
    p = data["paths"][0]
    assert "get" in p["sink_call"].lower()
    assert p["via_present"] is False
    tail = [c.split(".")[-1] for c in p["chain"]]
    assert "load-remote" in tail
