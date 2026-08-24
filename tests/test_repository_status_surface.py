import pytest

from codegraph import CodeGraph
from codegraph import render


def test_stats_renders_physical_snapshot_and_graph_revision(tmp_path):
    (tmp_path / "app.py").write_text(
        "def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "asset.bin").write_bytes(b"fixture")
    graph = CodeGraph(tmp_path)
    graph.index()

    stats = graph.stats()
    text = render.stats(stats)

    assert stats["repository_files"] == 2
    assert stats["current_revision_id"] == 1
    assert "snapshot físico: 2 arquivos /" in text
    assert "revisão: 1 (1 registradas)" in text
    assert "código indexado: 1 arquivos" in text
    graph.close()


def test_symlink_target_changes_physical_snapshot_without_following(tmp_path):
    (tmp_path / "one.txt").write_text("same", encoding="utf-8")
    (tmp_path / "two.txt").write_text("same", encoding="utf-8")
    link = tmp_path / "current.txt"
    try:
        link.symlink_to("one.txt")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    graph = CodeGraph(tmp_path)
    first = graph.index()["repository"]["snapshot_hash"]
    first_link_hash = graph.indexer.conn.execute(
        "SELECT content_hash FROM repository_nodes WHERE path='current.txt'"
    ).fetchone()[0]

    link.unlink()
    link.symlink_to("two.txt")
    second = graph.index()["repository"]["snapshot_hash"]
    second_link_hash = graph.indexer.conn.execute(
        "SELECT content_hash FROM repository_nodes WHERE path='current.txt'"
    ).fetchone()[0]

    assert first_link_hash != second_link_hash
    assert first != second
    graph.close()
