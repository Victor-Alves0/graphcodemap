"""The first G4 family exposes identical evidence on all public surfaces."""

from __future__ import annotations

import asyncio
import json

import pytest

from codegraph import CodeGraph, cli


SOURCE = """\
def download(user_path):
    selected = user_path
    return open(selected)
"""

REPO_SOURCE = """\
def download():
    selected = input()
    return open(selected)
"""


def _facts(finding: dict) -> dict:
    return {
        "rule_id": finding["rule_id"],
        "cwe": finding["cwe"],
        "source": (finding["source"]["name"], finding["source"]["path"]),
        "sink": (
            finding["sink"]["details"]["callee"],
            finding["sink"]["evidence"]["argument_index"],
        ),
        "nodes": [node["name"] for node in finding["path"]["nodes"]],
        "relations": [edge["relation"] for edge in finding["path"]["edges"]],
    }


def test_library_cli_and_mcp_return_the_same_persistent_finding(
        tmp_path, capsys, monkeypatch):
    pytest.importorskip("mcp")
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")

    graph = CodeGraph(tmp_path)
    graph.index()
    _entry, library, _env = graph.path_traversal("app.download")
    expected = _facts(library["findings"][0])
    graph.close()

    assert cli.main([
        "--root", str(tmp_path), "path-traversal", "app.download", "--json",
    ]) == 1
    cli_result = json.loads(capsys.readouterr().out)
    assert _facts(cli_result["findings"][0]) == expected

    from codegraph import l1
    from codegraph.mcp_server import build_server

    # This surface test is about the response contract, not a background L1
    # resolver. Avoid an unrelated daemon changing lifecycle metadata mid-test.
    monkeypatch.setattr(l1, "refine", lambda _indexer: None)
    server = build_server(tmp_path, watch=False)
    _content, structured = asyncio.run(server.call_tool(
        "path_traversal", {"entry": "app.download"}))
    assert _facts(structured["results"][0]) == expected
    assert structured["fresh"] is True
    assert structured["truncated"] is False


def test_repo_wide_library_cli_and_mcp_return_the_same_source_result(
        tmp_path, capsys, monkeypatch):
    pytest.importorskip("mcp")
    (tmp_path / "app.py").write_text(REPO_SOURCE, encoding="utf-8")

    graph = CodeGraph(tmp_path)
    graph.index()
    _entry, library, _env = graph.path_traversal()
    expected = _facts(library["findings"][0])
    graph.close()

    assert cli.main([
        "--root", str(tmp_path), "path-traversal", "--json",
    ]) == 1
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["mode"] == "scan"
    assert _facts(cli_result["findings"][0]) == expected

    from codegraph import l1
    from codegraph.mcp_server import build_server

    monkeypatch.setattr(l1, "refine", lambda _indexer: None)
    server = build_server(tmp_path, watch=False)
    _content, structured = asyncio.run(server.call_tool("path_traversal", {}))
    assert _facts(structured["results"][0]) == expected
