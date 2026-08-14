"""Precisão do L1 JS/TS: cada ``certain`` precisa apontar para um callable real.

O fixture mistura chamadas resolvíveis, callback dinâmico, import CommonJS e
função em object literal. Ele é pequeno o bastante para funcionar como oráculo
de call edges, não apenas como contador de promoções.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph, l1
from codegraph.l1 import tsjs_ls
from codegraph.l1.tsjs_ls import TsLsResolver


LIB = """
const helper = () => 1;
function plain() { return 2; }
class Svc {
  run(x) { return x; }
  static make() { return new Svc(); }
}
const obj = { m: function () { return 3; } };
module.exports = { helper, plain, Svc, obj };
"""

MAIN = """
const lib = require("./lib");
const local = (n) => n + 1;
function go(cb) {
  const s = new lib.Svc();
  s.run(1);
  lib.helper();
  lib.plain();
  lib.Svc.make();
  lib.obj.m();
  local(2);
  cb(3);
  return helper2();
}
function helper2() { return 4; }
module.exports = go;
"""


def _fixture(tmp_path):
    (tmp_path / "lib.js").write_text(textwrap.dedent(LIB).lstrip(),
                                      encoding="utf-8")
    (tmp_path / "main.js").write_text(textwrap.dedent(MAIN).lstrip(),
                                       encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    return g


def _edge_at(g, line: int):
    return g.indexer.conn.execute(
        "SELECT e.id, e.file_id, e.line, e.col, e.dst_name, e.confidence, "
        "e.resolver, s.fqn AS target_fqn, s.kind AS target_kind "
        "FROM edges e JOIN files f ON f.id=e.file_id "
        "LEFT JOIN symbols s ON s.id=e.dst "
        "WHERE f.path='main.js' AND e.kind='calls' AND e.line=? "
        "ORDER BY e.id LIMIT 1", (line,)
    ).fetchone()


def test_object_literal_function_has_own_symbol(tmp_path):
    g = _fixture(tmp_path)
    row = g.indexer.conn.execute(
        "SELECT kind, parent_id FROM symbols WHERE fqn='lib.obj.m'"
    ).fetchone()
    assert row is not None and row["kind"] == "method"
    g.close()


def test_typescript_discovery_uses_analyzed_repo_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    ts = repo / "node_modules" / "typescript"
    (ts / "lib").mkdir(parents=True)
    (ts / "lib" / "typescript.js").write_text("", encoding="utf-8")
    monkeypatch.delenv("CODEGRAPH_TS_DIR", raising=False)
    monkeypatch.setattr(tsjs_ls, "_DEV_ROOT", tmp_path / "sem-tools")
    assert tsjs_ls._find_ts(repo) == str(ts)


def test_ts_kind_guard_rejects_callback_parameter(tmp_path):
    g = _fixture(tmp_path)
    edge = _edge_at(g, 11)
    assert edge is not None and edge["dst_name"] == "cb"

    # Não precisa de node: simula exatamente a resposta do LanguageService.
    resolver = object.__new__(TsLsResolver)
    resolver._query = lambda _rel, line, _col: ({
        "defs": [{"file": "main.js", "line": 3, "col": 12,
                  "kind": "parameter"}]
    } if line == 11 else {})
    n = resolver.refine_file(g.indexer.conn, g.indexer.root,
                             "main.js", edge["file_id"])
    assert n == 0
    got = _edge_at(g, 11)
    assert got["resolver"] == "l0" and got["confidence"] != "certain"
    g.close()


def test_ts_definition_name_must_match_callee(tmp_path):
    g = _fixture(tmp_path)
    edge = _edge_at(g, 11)
    resolver = object.__new__(TsLsResolver)
    resolver._query = lambda _rel, line, _col: ({
        "defs": [{"file": "main.js", "line": 3, "col": 0,
                  "kind": "function", "name": "go"}]
    } if line == 11 else {})
    assert resolver.refine_file(g.indexer.conn, g.indexer.root,
                                "main.js", edge["file_id"]) == 0
    assert _edge_at(g, 11)["resolver"] == "l0"
    g.close()


@pytest.mark.skipif(not TsLsResolver.available(),
                    reason="node + módulo typescript não disponíveis")
def test_live_ts_resolver_matches_call_edge_oracle(tmp_path):
    g = _fixture(tmp_path)
    stats = l1.refine(g.indexer)
    assert stats["errors"] == 0

    expected = {
        5: "lib.Svc.run",
        6: "lib.helper",
        7: "lib.plain",
        8: "lib.Svc.make",
        9: "lib.obj.m",
        10: "main.local",
        12: "main.helper2",
    }
    for line, fqn in expected.items():
        row = _edge_at(g, line)
        assert row is not None, line
        assert (row["target_fqn"], row["confidence"], row["resolver"]) == (
            fqn, "certain", "l1")
        assert row["target_kind"] in {"function", "method", "class"}

    callback = _edge_at(g, 11)
    assert callback["resolver"] == "l0"
    assert callback["confidence"] != "certain"
    assert callback["target_fqn"] != "main.go"
    g.close()
