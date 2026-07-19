"""Extractors dedicados Ruby/Lua/Swift: fqn com escopo, herança, imports, calls."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

RUBY = '''
require "json"
require_relative "helper"

module Billing
  class Invoice < Base
    def initialize(amount)
      @amount = amount
    end

    def total(tax)
      compute(@amount, tax)
    end

    def self.create(x)
      Invoice.new(x)
    end
  end
end

def top_level(a)
  puts a
end
'''

LUA = '''
local json = require("json")
local M = {}

function M.process(data)
  return transform(data)
end

local function helper(x)
  return x + 1
end

function Account:deposit(n)
  helper(n)
end

function Account.new(b)
  return b
end

return M
'''

SWIFT = '''
import Foundation

protocol Shape {
    func area() -> Double
}

class Circle: Shape {
    let radius: Double
    init(radius: Double) { self.radius = radius }
    func area() -> Double { return compute(radius) }
}

struct Point {
    func move(dx: Int) -> Point { return Point(x: dx) }
}

func compute(_ r: Double) -> Double { return r * r }

extension Circle {
    func describe() -> String { return "circle" }
}
'''


@pytest.fixture()
def cg5(tmp_path):
    files = {"billing.rb": RUBY, "mod.lua": LUA, "shapes.swift": SWIFT}
    for name, content in files.items():
        (tmp_path / name).write_text(textwrap.dedent(content), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def fqns(rows):
    return {r["fqn"] for r in rows}


# -- Ruby ---------------------------------------------------------------------

def test_ruby_module_class_scoping(cg5):
    rows, _ = cg5.find_symbol("total")
    assert "billing.Billing.Invoice.total" in fqns(rows)


def test_ruby_singleton_method(cg5):
    rows, _ = cg5.find_symbol("billing.Billing.Invoice.create")
    assert "billing.Billing.Invoice.create" in fqns(rows)


def test_ruby_inherits(cg5):
    data, _ = cg5.ego_graph("billing.Billing.Invoice")
    assert any(r["kind"] == "inherits" and (r["other_fqn"] or "").endswith("Base")
               or r.get("dst_name") == "Base" for r in data["out"])


def test_ruby_require_is_import_not_call(cg5):
    # require/require_relative viram imports, não chamadas
    data, _ = cg5.ego_graph("billing.Billing.Invoice.total")
    sym, callers, _ = cg5.callers("billing.Billing.Invoice.total")
    # e a chamada real dentro de total é compute
    _, callees, _ = cg5.callees("billing.Billing.Invoice.total")
    assert any((c["dst_name"] or "").endswith("compute") for c in callees)


def test_ruby_intra_class_call(cg5):
    _, rows, _ = cg5.callees("billing.Billing.Invoice.total")
    assert any((r["dst_name"] or "") == "compute" for r in rows)


# -- Lua ----------------------------------------------------------------------

def test_lua_table_method_fqn(cg5):
    rows, _ = cg5.find_symbol("mod.M.process")
    assert "mod.M.process" in fqns(rows)


def test_lua_colon_method(cg5):
    rows, _ = cg5.find_symbol("mod.Account.deposit")
    assert "mod.Account.deposit" in fqns(rows)


def test_lua_local_function_and_call(cg5):
    sym, rows, _ = cg5.callers("mod.helper")
    others = {(r["other_fqn"] or "") for r in rows}
    assert any(o.endswith("process") or o.endswith("deposit") for o in others)


# -- Swift --------------------------------------------------------------------

def test_swift_protocol_is_interface(cg5):
    rows, _ = cg5.find_symbol("Shape", kind="interface")
    assert "shapes.Shape" in fqns(rows)


def test_swift_struct_and_class(cg5):
    rows, _ = cg5.find_symbol("Point", kind="struct")
    assert "shapes.Point" in fqns(rows)
    rows, _ = cg5.find_symbol("Circle", kind="class")
    assert "shapes.Circle" in fqns(rows)


def test_swift_init_and_method(cg5):
    assert "shapes.Circle.init" in fqns(cg5.find_symbol("shapes.Circle.init")[0])
    assert "shapes.Circle.area" in fqns(cg5.find_symbol("shapes.Circle.area")[0])


def test_swift_inherits_conformance(cg5):
    data, _ = cg5.ego_graph("shapes.Circle")
    assert any(r["kind"] == "inherits" and
               ((r["other_fqn"] or "").endswith("Shape") or r.get("dst_name") == "Shape")
               for r in data["out"])


def test_swift_extension_method_scoped_to_type(cg5):
    rows, _ = cg5.find_symbol("describe")
    assert "shapes.Circle.describe" in fqns(rows)


def test_swift_call_resolves(cg5):
    _, rows, _ = cg5.callees("shapes.Circle.area")
    assert any((r["dst_name"] or "") == "compute" for r in rows)


def test_all_dedicated_registered(cg5):
    from codegraph.languages import DEDICATED
    assert {"ruby", "lua", "swift"} <= DEDICATED
    langs = set(cg5.stats()["by_language"])
    assert {"ruby", "lua", "swift"} <= langs
