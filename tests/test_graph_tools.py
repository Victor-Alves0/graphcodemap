"""M1 (PageRank/impact/ego/overview) e M3 (servidor MCP)."""

from __future__ import annotations

import asyncio

import pytest


def test_pagerank_hub_ranks_higher(cg):
    # validate é chamado por issue_token, login e formatUser → hub
    entries, _ = cg.overview()
    assert entries  # PageRank computado sem erro
    conn = cg.indexer.conn
    rank = {r["fqn"]: r["rank"] for r in conn.execute("SELECT fqn, rank FROM symbols")}
    assert rank["app.auth.TokenService.validate"] > rank["app.db.close_session"]


def test_impact_transitive(cg):
    sym, rows, env = cg.impact("app.db.get_session", depth=4)
    fqn_depth = {r["fqn"]: r["depth"] for r in rows}
    # cadeia: get_session ← _check(d1) ← validate(d2) ← issue_token/login(d3)
    assert fqn_depth.get("app.auth.TokenService._check") == 1
    assert fqn_depth.get("app.auth.TokenService.validate") == 2
    assert "app.routes.login" in fqn_depth
    assert any("completeness" in w for w in env.warnings)


def test_impact_confidence_propagates_min(cg):
    _, rows, _ = cg.impact("app.db.get_session", depth=4)
    by_fqn = {r["fqn"]: r for r in rows}
    # nenhuma confiança de caminho pode ser maior que a mínima das arestas
    assert by_fqn["app.auth.TokenService.validate"]["confidence"] in ("inferred", "possible")


def test_ego_graph(cg):
    data, _ = cg.ego_graph("app.auth.TokenService.validate")
    assert data["parent"] == "app.auth.TokenService"
    in_fqns = {r["other_fqn"] for r in data["in"]}
    assert "app.auth.issue_token" in in_fqns
    out_names = {r["other_fqn"] or r["dst_name"] for r in data["out"]}
    assert any("_check" in (n or "") for n in out_names)


def test_overview_budget_truncates(cg):
    entries, env = cg.overview(token_budget=30)
    assert len(entries) >= 1
    assert any("truncated" in w for w in env.warnings)


def test_overview_scope(cg):
    entries, _ = cg.overview(scope="app")
    assert entries and all(e["path"].startswith("app/") for e in entries)


def test_rank_recomputed_after_edit(cg, repo):
    cg.overview()  # computa ranks
    row = cg.indexer.conn.execute(
        "SELECT value FROM meta WHERE key='rank_dirty'").fetchone()
    assert row["value"] == "0"
    (repo / "app" / "extra.py").write_text(
        "from app.db import get_session\n\ndef extra():\n    return get_session()\n",
        encoding="utf-8")
    cg.index()
    row = cg.indexer.conn.execute(
        "SELECT value FROM meta WHERE key='rank_dirty'").fetchone()
    assert row["value"] == "1"  # dirty → próxima consulta recomputa


# -- MCP ----------------------------------------------------------------------

def test_mcp_server_tools(repo):
    pytest.importorskip("mcp")
    from codegraph.mcp_server import build_server

    server = build_server(repo)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"overview", "find_symbol", "symbol_info", "references",
                     "callers", "callees", "impact", "ego_graph", "dataflow",
                     "taint", "reaches", "communities", "describe", "index_status"}


def test_mcp_tool_call_roundtrip(repo):
    pytest.importorskip("mcp")
    from codegraph.mcp_server import build_server

    server = build_server(repo)
    result = asyncio.run(server.call_tool("callers", {"symbol": "issue_token"}))
    text = "".join(block.text for block in result[0])
    assert "app.routes.login" in text
    assert "completeness" in text  # honestidade sempre presente

    result = asyncio.run(server.call_tool("find_symbol", {"query": "inexistente_xyz"}))
    text = "".join(block.text for block in result[0])
    assert "nenhum símbolo" in text
