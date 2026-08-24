"""Atomic publication and observable lifecycle for semantic L1 snapshots."""

from __future__ import annotations

import threading

import pytest

from codegraph import CodeGraph, cli, l1, render
from codegraph.db import write_l1_lifecycle


SOURCE = (
    "def helper():\n"
    "    return 1\n\n"
    "def run():\n"
    "    return helper()\n"
)


def _graph(tmp_path) -> CodeGraph:
    (tmp_path / "svc.py").write_text(SOURCE, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def _seed_published_certain(graph: CodeGraph) -> None:
    edge = graph.indexer.conn.execute(
        "SELECT id FROM edges WHERE kind='calls' AND dst_name='helper'"
    ).fetchone()
    assert edge is not None
    graph.indexer.conn.execute(
        "UPDATE edges SET confidence='certain', resolver='l1' WHERE id=?",
        (edge["id"],),
    )
    write_l1_lifecycle(graph.indexer.conn, {
        "status": "complete", "published": True, "finished_at": 1,
    })
    graph.indexer.conn.commit()


def _call_edge(graph: CodeGraph) -> dict:
    return dict(graph.indexer.conn.execute(
        "SELECT confidence, resolver, dst FROM edges "
        "WHERE kind='calls' AND dst_name='helper' ORDER BY id LIMIT 1"
    ).fetchone())


def _l1_stage(graph: CodeGraph):
    row = graph.indexer.conn.execute(
        "SELECT status, details_json FROM graph_stage_runs "
        "WHERE stage='l1' ORDER BY revision_id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else dict(row)


def test_library_and_human_status_start_not_started(tmp_path):
    graph = _graph(tmp_path)

    assert graph.l1_status() == {"status": "not_started"}
    assert graph.stats()["l1"]["status"] == "not_started"
    assert graph.doctor()["l1"]["status"] == "not_started"
    assert "L1: not_started" in render.stats(graph.stats())
    assert "L1 lifecycle: not_started" in render.doctor(graph.doctor())
    graph.close()


def test_running_refine_keeps_previous_snapshot_visible_until_publish(
        tmp_path, monkeypatch):
    writer = _graph(tmp_path)
    _seed_published_certain(writer)
    reader = CodeGraph(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class BlockingPython:
        languages = ("python",)
        root_markers = ()

        def __init__(self, *_args, **_kwargs):
            pass

        def refine_file(self, *_args):
            entered.set()
            assert release.wait(5), "test did not release the resolver"
            return 0

        def close(self):
            pass

    monkeypatch.setattr(l1, "all_resolvers", lambda: [BlockingPython])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [BlockingPython])
    failures: list[BaseException] = []

    def run_refine():
        try:
            l1.refine(writer.indexer)
        except BaseException as error:  # surfaced by the assertion below
            failures.append(error)

    thread = threading.Thread(target=run_refine)
    thread.start()
    try:
        assert entered.wait(5), "resolver did not start"
        assert reader.l1_status()["status"] == "running"
        visible = _call_edge(reader)
        assert visible["confidence"] == "certain"
        assert visible["resolver"] == "l1"
        assert visible["dst"] is not None
        # The candidate stage receipt is no more visible than its candidate
        # edges: the reader still has the previous published revision only.
        assert _l1_stage(reader)["status"] == "not_started"
    finally:
        release.set()
        thread.join(5)

    assert not thread.is_alive()
    assert failures == []
    assert reader.l1_status()["status"] == "complete"
    published = _call_edge(reader)
    assert published["confidence"] == "inferred"
    assert published["resolver"] == "l0"
    assert _l1_stage(reader)["status"] == "complete"
    writer.close()
    reader.close()


def test_expected_partial_pass_publishes_fallback_and_status_together(
        tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    _seed_published_certain(graph)

    class MissingPython:
        languages = ("python",)
        cmd_name = "missing-python-ls"
        cmd_env = "CODEGRAPH_MISSING_PYTHON"

    monkeypatch.setattr(l1, "all_resolvers", lambda: [MissingPython])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [])

    stats = l1.refine(graph.indexer)

    assert stats["status"] == "partial"
    assert graph.l1_status()["status"] == "partial"
    assert graph.l1_status()["published"] is True
    edge = _call_edge(graph)
    assert edge["confidence"] == "inferred" and edge["resolver"] == "l0"
    assert _l1_stage(graph)["status"] == "partial"
    graph.close()


def test_fatal_candidate_failure_rolls_back_and_marks_previous_preserved(
        tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    _seed_published_certain(graph)

    class HealthyPython:
        languages = ("python",)
        root_markers = ()

    monkeypatch.setattr(l1, "all_resolvers", lambda: [HealthyPython])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [HealthyPython])

    def fail_resolution(*_args, **_kwargs):
        raise RuntimeError("candidate publication failed")

    monkeypatch.setattr(graph.indexer, "resolve_edges", fail_resolution)

    with pytest.raises(RuntimeError, match="candidate publication failed"):
        l1.refine(graph.indexer)

    assert _call_edge(graph)["confidence"] == "certain"
    status = graph.l1_status()
    assert status["status"] == "partial"
    assert status["published"] is False
    assert status["preserved_previous"] is True
    assert status["error_type"] == "RuntimeError"
    assert _l1_stage(graph)["status"] == "not_started"
    graph.close()


def test_cli_doctor_observes_running_state(tmp_path, capsys):
    graph = _graph(tmp_path)
    write_l1_lifecycle(graph.indexer.conn, {
        "status": "running", "started_at": 1, "published": False,
    })
    graph.indexer.conn.commit()
    graph.close()

    assert cli.main(["--root", str(tmp_path), "doctor"]) == 0
    output = capsys.readouterr().out
    assert "L1 lifecycle: running" in output
    assert "consultas leem o último snapshot publicado" in output


def test_l0_code_revision_invalidates_complete_l1_but_asset_edit_does_not(
        tmp_path):
    graph = _graph(tmp_path)
    _seed_published_certain(graph)
    asset = tmp_path / "logo.bin"
    asset.write_bytes(b"one")
    graph.indexer.index_file("logo.bin")
    assert graph.l1_status()["status"] == "complete"

    source = tmp_path / "svc.py"
    source.write_text(SOURCE.replace("return 1", "return 2"), encoding="utf-8")
    graph.indexer.index_file("svc.py")

    status = graph.l1_status()
    assert status["status"] == "not_started"
    assert status["published"] is False
    assert status["previous_status"] == "complete"
    assert status["reason"] == "l0_revision_changed"
    assert status["revision_id"] == graph.stats()["current_revision_id"]
    graph.close()
