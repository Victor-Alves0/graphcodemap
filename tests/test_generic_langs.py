"""Nível genérico: heurística estrutural + markdown/config."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

RB = '''
require "json"

class Scheduler
  def execute(task)
    process(task)
  end

  def process(task)
    task * 2
  end
end

def top_helper(x)
  Scheduler.new
end
'''

LUA = '''
local M = {}

function M.process(task)
  return helper(task)
end

function helper(x)
  return x * 2
end

return M
'''

SH = '''
#!/bin/bash

build() {
  compile_all
}

compile_all() {
  echo ok
}

build
'''

SCALA = '''
package app

trait Processor {
  def process(t: Int): Int
}

class Scheduler extends Processor {
  def process(t: Int): Int = helper(t)
  def helper(t: Int): Int = t * 2
}
'''

MD = """# Guia

## Instalação

texto

## Uso

### Avançado
"""

CFG = '{"name": "demo", "version": "1.0", "scripts": {"build": "x"}}'

YML = """name: ci
on: push
jobs:
  test:
    runs-on: ubuntu
"""


@pytest.fixture()
def cg4(tmp_path):
    files = {"sched.rb": RB, "mod.lua": LUA, "build.sh": SH,
             "app.scala": SCALA, "GUIA.md": MD, "package.json": CFG,
             "ci.yml": YML}
    for name, content in files.items():
        (tmp_path / name).write_text(textwrap.dedent(content), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def fqns(rows):
    return {r["fqn"] for r in rows}


def test_ruby_symbols_and_call(cg4):
    rows, _ = cg4.find_symbol("Scheduler", kind="class")
    assert "sched.Scheduler" in fqns(rows)
    sym, rows, _ = cg4.callers("sched.Scheduler.process")
    assert any(r["other_fqn"] == "sched.Scheduler.execute" for r in rows)


def test_lua_function_call(cg4):
    sym, rows, _ = cg4.callers("mod.helper")
    assert any((r["other_fqn"] or "").endswith("process") for r in rows)


def test_bash_functions(cg4):
    rows, _ = cg4.find_symbol("compile_all")
    assert "build.compile_all" in fqns(rows)
    sym, rows, _ = cg4.callers("build.compile_all")
    assert any(r["other_fqn"] == "build.build" for r in rows)


def test_scala_trait_and_methods(cg4):
    rows, _ = cg4.find_symbol("Processor", kind="interface")
    assert "app.Processor" in fqns(rows)
    rows, _ = cg4.find_symbol("app.Scheduler.helper")
    assert "app.Scheduler.helper" in fqns(rows)


def test_markdown_headings(cg4):
    rows, _ = cg4.find_symbol("Instalação", kind="section")
    assert "GUIA.Guia.Instalação" in fqns(rows)
    rows, _ = cg4.find_symbol("Avançado", kind="section")
    assert any("Uso.Avançado" in f for f in fqns(rows))


def test_config_keys(cg4):
    rows, _ = cg4.find_symbol("version", kind="key")
    assert "package.version" in fqns(rows)
    rows, _ = cg4.find_symbol("jobs", kind="key")
    assert "ci.jobs" in fqns(rows)


def test_languages_registered(cg4):
    langs = set(cg4.stats()["by_language"])
    assert {"ruby", "lua", "bash", "scala", "markdown", "json", "yaml"} <= langs
