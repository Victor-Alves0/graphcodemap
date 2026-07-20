"""L1 via LSP para linguagens adicionais (Lua, Clojure, PHP, Ruby, Kotlin).

Cada teste PULA se o servidor não estiver no PATH — como jedi/gopls pulam sem
sua dependência. São o harness de validação: quando o binário existe, provam
que uma chamada cross-file é promovida a 'certain'. Fixtures minimalistas e
idiomáticas; a resolução real depende do servidor carregar o projeto.
"""

from __future__ import annotations

import pytest

from codegraph.l1.clojure_lsp import ClojureLspResolver
from codegraph.l1.kotlin_ls import KotlinLsResolver
from codegraph.l1.lua_ls import LuaLsResolver
from codegraph.l1.php_intelephense import IntelephenseResolver
from codegraph.l1.ruby_solargraph import SolargraphResolver


def _promoted_cross_file(tmp_path, files, lang, caller="helper", callee="compute"):
    from codegraph import CodeGraph, l1

    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    cg = CodeGraph(tmp_path)
    cg.index()
    stats = l1.refine(cg.indexer)
    row = cg.indexer.conn.execute(
        "SELECT e.confidence, e.dst FROM edges e JOIN symbols s ON e.src=s.id "
        f"WHERE s.name=? AND e.kind='calls' AND e.dst_name LIKE '%{callee}%'",
        (caller,)).fetchone()
    cg.close()
    return lang in stats["resolvers"], stats["promoted"], row


@pytest.mark.skipif(not LuaLsResolver.available(),
                    reason="lua-language-server não disponível")
def test_lua_l1_promotes_cross_file_call(tmp_path):
    files = {
        "calc.lua": "local M = {}\nfunction M.compute(x) return x * x end\nreturn M\n",
        "main.lua": ("local calc = require('calc')\n"
                     "local function helper() return calc.compute(2) end\n"
                     "return helper\n"),
    }
    active, promoted, row = _promoted_cross_file(tmp_path, files, "lua")
    assert active and promoted > 0
    assert row is not None and row["dst"] is not None
    assert row["confidence"] == "certain"


@pytest.mark.skipif(not ClojureLspResolver.available(),
                    reason="clojure-lsp não disponível")
def test_clojure_l1_promotes_cross_file_call(tmp_path):
    files = {
        "calc.clj": "(ns calc)\n(defn compute [x] (* x x))\n",
        "main.clj": "(ns main (:require [calc]))\n(defn helper [] (calc/compute 2))\n",
    }
    active, promoted, row = _promoted_cross_file(tmp_path, files, "clojure")
    assert active and promoted > 0
    assert row is not None and row["dst"] is not None
    assert row["confidence"] == "certain"


@pytest.mark.skipif(not IntelephenseResolver.available(),
                    reason="intelephense não disponível")
def test_php_l1_promotes_cross_file_call(tmp_path):
    files = {
        "calc.php": "<?php\nfunction compute($x) { return $x * $x; }\n",
        "main.php": ("<?php\nrequire_once 'calc.php';\n"
                     "function helper() { return compute(2); }\n"),
    }
    active, promoted, row = _promoted_cross_file(tmp_path, files, "php")
    assert active and promoted > 0
    assert row is not None and row["dst"] is not None
    assert row["confidence"] == "certain"


@pytest.mark.skipif(not SolargraphResolver.available(),
                    reason="solargraph não disponível")
def test_ruby_l1_promotes_cross_file_call(tmp_path):
    files = {
        "calc.rb": "def compute(x)\n  x * x\nend\n",
        "main.rb": "require_relative 'calc'\n\ndef helper\n  compute(2)\nend\n",
    }
    active, promoted, row = _promoted_cross_file(tmp_path, files, "ruby")
    assert active and promoted > 0
    assert row is not None and row["dst"] is not None
    assert row["confidence"] == "certain"


@pytest.mark.skipif(not KotlinLsResolver.available(),
                    reason="kotlin-language-server não disponível")
def test_kotlin_l1_promotes_cross_file_call(tmp_path):
    files = {
        "Calc.kt": "package app\n\nfun compute(x: Int): Int = x * x\n",
        "Main.kt": "package app\n\nfun helper(): Int = compute(2)\n",
    }
    active, promoted, row = _promoted_cross_file(tmp_path, files, "kotlin")
    assert active and promoted > 0
    assert row is not None and row["dst"] is not None
    assert row["confidence"] == "certain"
