from __future__ import annotations

import asyncio

from codegraph import CodeGraph, cli, l1


SOURCE = (
    "def helper():\n"
    "    return 1\n\n"
    "def run(callback):\n"
    "    helper()\n"
    "    external()\n"
    "    return callback()\n"
)


def _graph(tmp_path) -> CodeGraph:
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def test_real_jedi_reports_certain_and_honest_no_local_targets(tmp_path):
    graph = _graph(tmp_path)

    stats = l1.refine(graph.indexer)
    coverage, envelope = graph.semantic_coverage()

    assert stats["status"] == "complete"
    assert stats["coverage"] == coverage
    assert envelope.fresh is True
    assert coverage["total_sites"] == 3
    assert coverage["certain_sites"] == 1
    assert coverage["semantic_sites"] == 1
    assert coverage["unresolved_sites"] == 2
    assert coverage["local_candidate_sites"] == 1
    assert coverage["missed_local_candidate_sites"] == 0
    assert coverage["no_local_graph_candidate_sites"] == 2
    assert coverage["local_candidate_coverage_pct"] == 100.0
    assert coverage["by_language"]["python"]["semantic_sites"] == 1
    assert coverage["outcomes"] == {
        "l1_certain": 1,
        "l1_no_local_target": 2,
    }
    assert {item["callee"] for item in coverage["samples"]} == {
        "callback", "external",
    }
    stage = graph.graph_history(limit=1)[0][0]["stages"]
    l1_stage = next(item for item in stage if item["stage"] == "l1")
    assert l1_stage["details"]["coverage"] == coverage
    graph.close()


def test_missing_resolver_is_distinct_from_attempted_without_target(
        tmp_path, monkeypatch):
    graph = _graph(tmp_path)

    class MissingPython:
        languages = ("python",)
        cmd_name = "missing-python-ls"
        cmd_env = "CODEGRAPH_MISSING_PYTHON"

    monkeypatch.setattr(l1, "all_resolvers", lambda: [MissingPython])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [])
    stats = l1.refine(graph.indexer)

    assert stats["status"] == "partial"
    assert stats["coverage"]["outcomes"] == {"resolver_unavailable": 3}
    assert stats["coverage"]["fallback_sites"] == 1
    assert stats["coverage"]["unresolved_sites"] == 2
    assert stats["coverage"]["local_candidate_sites"] == 1
    assert stats["coverage"]["missed_local_candidate_sites"] == 1
    assert stats["coverage"]["local_candidate_coverage_pct"] == 0.0
    graph.close()


def test_semantic_coverage_is_exposed_by_cli_and_mcp(tmp_path, capsys):
    graph = _graph(tmp_path)
    l1.refine(graph.indexer)
    graph.close()

    assert cli.main([
        "--root", str(tmp_path), "semantic-coverage", "--samples", "2",
    ]) == 0
    output = capsys.readouterr().out
    assert "certain: 1/3" in output
    assert "l1_no_local_target=2" in output

    from codegraph.mcp_server import build_server

    server = build_server(tmp_path, watch=False)
    _content, payload = asyncio.new_event_loop().run_until_complete(
        server.call_tool("semantic_coverage", {"sample_limit": 2}))
    assert payload["confidence"] == "n/a"
    assert payload["results"][0]["certain_sites"] == 1


def test_new_l0_revision_does_not_reuse_previous_attempt_reasons(tmp_path):
    graph = _graph(tmp_path)
    l1.refine(graph.indexer)
    source = tmp_path / "app.py"
    source.write_text(SOURCE.replace("return 1", "return 2"), encoding="utf-8")
    graph.index()

    coverage = graph.semantic_coverage()[0]

    assert graph.l1_status()["status"] == "not_started"
    assert coverage["outcomes"] == {
        "l0_unique_not_refined": 1,
        "unresolved_not_refined": 2,
    }
    graph.close()
