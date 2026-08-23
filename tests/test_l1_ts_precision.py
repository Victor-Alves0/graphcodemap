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


def test_typescript_discovery_finds_bounded_monorepo_subproject(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    package = repo / "packages" / "web"
    ts = package / "node_modules" / "typescript"
    (ts / "lib").mkdir(parents=True)
    (ts / "lib" / "typescript.js").write_text("", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"private":true,"workspaces":["packages/*"]}', encoding="utf-8")
    monkeypatch.delenv("CODEGRAPH_TS_DIR", raising=False)
    monkeypatch.setattr(tsjs_ls, "_DEV_ROOT", tmp_path / "sem-tools")
    monkeypatch.setattr(tsjs_ls, "_find_node", lambda: None)

    assert tsjs_ls._find_ts(repo) == str(ts.resolve())


def test_typescript_discovery_prefers_declared_workspace_over_fixture(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    fixture_ts = repo / "aaa_fixture" / "node_modules" / "typescript"
    workspace = repo / "packages" / "app"
    workspace_ts = workspace / "node_modules" / "typescript"
    for ts in (fixture_ts, workspace_ts):
        (ts / "lib").mkdir(parents=True)
        (ts / "lib" / "typescript.js").write_text("", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"private":true,"workspaces":["packages/*"]}', encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "index.ts").write_text("export {};", encoding="utf-8")
    monkeypatch.delenv("CODEGRAPH_TS_DIR", raising=False)
    monkeypatch.setattr(tsjs_ls, "_DEV_ROOT", tmp_path / "sem-tools")
    monkeypatch.setattr(tsjs_ls, "_find_node", lambda: None)

    assert tsjs_ls._find_ts(repo) == str(workspace_ts.resolve())


def test_typescript_discovery_prefers_manifest_and_code_over_fixture(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    fixture_ts = repo / "aaa_fixture" / "node_modules" / "typescript"
    project = repo / "packages" / "app"
    project_ts = project / "node_modules" / "typescript"
    for ts in (fixture_ts, project_ts):
        (ts / "lib").mkdir(parents=True)
        (ts / "lib" / "typescript.js").write_text("", encoding="utf-8")
    (project / "package.json").write_text('{"private":true}', encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "index.ts").write_text("export {};", encoding="utf-8")
    monkeypatch.delenv("CODEGRAPH_TS_DIR", raising=False)
    monkeypatch.setattr(tsjs_ls, "_DEV_ROOT", tmp_path / "sem-tools")
    monkeypatch.setattr(tsjs_ls, "_find_node", lambda: None)

    assert tsjs_ls._find_ts(repo) == str(project_ts.resolve())


def test_typescript_workspace_candidate_tie_is_deterministic(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    # Criação em ordem inversa não pode influenciar a escolha.
    for name in ("zeta", "alpha"):
        package = repo / "packages" / name
        ts = package / "node_modules" / "typescript"
        (ts / "lib").mkdir(parents=True)
        (ts / "lib" / "typescript.js").write_text("", encoding="utf-8")
        (package / "index.ts").write_text("export {};", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"private":true,"workspaces":["packages/*"]}', encoding="utf-8")
    monkeypatch.delenv("CODEGRAPH_TS_DIR", raising=False)
    monkeypatch.setattr(tsjs_ls, "_DEV_ROOT", tmp_path / "sem-tools")
    monkeypatch.setattr(tsjs_ls, "_find_node", lambda: None)

    expected = repo / "packages" / "alpha" / "node_modules" / "typescript"
    assert tsjs_ls._find_ts(repo) == str(expected.resolve())


def test_typescript_subproject_discovery_obeys_directory_limit(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    ts = repo / "packages" / "web" / "node_modules" / "typescript"
    (ts / "lib").mkdir(parents=True)
    (ts / "lib" / "typescript.js").write_text("", encoding="utf-8")
    monkeypatch.delenv("CODEGRAPH_TS_DIR", raising=False)
    monkeypatch.setattr(tsjs_ls, "_DEV_ROOT", tmp_path / "sem-tools")
    monkeypatch.setattr(tsjs_ls, "_find_node", lambda: None)
    monkeypatch.setattr(tsjs_ls, "_TS_DISCOVERY_MAX_DIRS", 1)

    assert tsjs_ls._find_ts(repo) is None


def test_typescript_subproject_discovery_rejects_symlink_escape(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    external = tmp_path / "external-typescript"
    (external / "lib").mkdir(parents=True)
    (external / "lib" / "typescript.js").write_text("", encoding="utf-8")
    link = repo / "packages" / "web" / "node_modules" / "typescript"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink indisponível: {error}")
    monkeypatch.delenv("CODEGRAPH_TS_DIR", raising=False)
    monkeypatch.setattr(tsjs_ls, "_DEV_ROOT", tmp_path / "sem-tools")
    monkeypatch.setattr(tsjs_ls, "_find_node", lambda: None)

    assert tsjs_ls._find_ts(repo) is None


def test_typescript_unavailable_message_explains_env_contract():
    details = TsLsResolver.unavailable_details()
    assert "CODEGRAPH_TS_DIR" in details["action"]
    assert "lib/typescript.js" in details["action"]

    missing = l1.missing_resolvers(
        {"typescript"}, is_available=lambda _resolver: False)
    assert missing[0]["action"] == details["action"]


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


def test_describe_and_it_callbacks_have_distinct_human_names(tmp_path):
    source = """
        describe("authentication", () => {
          it("accepts a valid token", () => {});
          it("rejects an invalid token", () => {});
        });
        describe("authorization", () => {
          it("rejects a guest", () => {});
        });
    """
    (tmp_path / "auth.spec.ts").write_text(
        textwrap.dedent(source), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        rows = graph.indexer.conn.execute(
            "SELECT name, fqn FROM symbols WHERE name LIKE 'describe#%' "
            "OR name LIKE 'it#%' ORDER BY start_line"
        ).fetchall()
        assert len(rows) == 5
        assert len({row["fqn"] for row in rows}) == 5
        assert any("authentication" in row["name"] for row in rows)
        assert any("valid_token" in row["name"] for row in rows)
    finally:
        graph.close()


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
