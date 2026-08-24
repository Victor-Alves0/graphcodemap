from __future__ import annotations

import os
import subprocess

import pytest

from codegraph import CodeGraph
from codegraph import cli
from codegraph.util import content_hash


def test_repository_graph_contains_directories_all_files_and_exact_hashes(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "app.py").write_text(
        "def run():\n    return 1\n", encoding="utf-8")
    binary = tmp_path / "assets" / "logo.bin"
    binary.parent.mkdir()
    binary.write_bytes(b"not-source-but-part-of-the-repository")

    graph = CodeGraph(tmp_path)
    stats = graph.index()
    rows = {row["path"]: dict(row) for row in graph.indexer.conn.execute(
        "SELECT n.path, n.kind, n.content_hash, n.index_state, "
        "p.path parent_path FROM repository_nodes n "
        "LEFT JOIN repository_nodes p ON p.id=n.parent_id"
    )}

    assert rows[""]["kind"] == "repository"
    assert rows["src/pkg"]["kind"] == "directory"
    assert rows["src/pkg/app.py"]["parent_path"] == "src/pkg"
    assert rows["src/pkg/app.py"]["index_state"] == "indexed"
    assert rows["assets/logo.bin"]["index_state"] == "not_applicable"
    assert rows["assets/logo.bin"]["content_hash"] == content_hash(binary.read_bytes())
    assert stats["repository"]["files"] == 2
    assert stats["revision_id"] == 1
    tree, envelope = graph.repository_tree(refresh=False)
    assert envelope.fresh is True
    assert {node["path"] for node in tree["nodes"]} >= {
        "", "src", "src/pkg", "src/pkg/app.py", "assets", "assets/logo.bin",
    }
    graph.close()


def test_unknown_file_edit_updates_only_physical_graph_and_creates_revision(tmp_path):
    asset = tmp_path / "assets.dat"
    asset.write_bytes(b"first")
    graph = CodeGraph(tmp_path)
    graph.index()
    before = graph.indexer.conn.execute(
        "SELECT content_hash FROM repository_nodes WHERE path='assets.dat'"
    ).fetchone()[0]

    asset.write_bytes(b"other")
    assert graph.indexer.index_file("assets.dat") is False
    after = graph.indexer.conn.execute(
        "SELECT content_hash FROM repository_nodes WHERE path='assets.dat'"
    ).fetchone()[0]
    revisions = graph.indexer.conn.execute(
        "SELECT trigger, source_snapshot_hash FROM graph_revisions ORDER BY id"
    ).fetchall()

    assert before != after == content_hash(b"other")
    assert [row["trigger"] for row in revisions] == ["full_index", "file_reindex"]
    assert revisions[0]["source_snapshot_hash"] != revisions[1]["source_snapshot_hash"]
    graph.close()


def test_single_file_scope_does_not_add_siblings_to_physical_graph(tmp_path):
    (tmp_path / "wanted.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "sibling.py").write_text("y = 2\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index(scope="wanted.py")

    paths = {row[0] for row in graph.indexer.conn.execute(
        "SELECT path FROM repository_nodes")}
    assert "wanted.py" in paths
    assert "sibling.py" not in paths
    graph.close()


def test_deleted_file_leaves_physical_and_semantic_graph(tmp_path):
    path = tmp_path / "gone.py"
    path.write_text("def gone():\n    return 1\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    path.unlink()

    graph.indexer.remove_file("gone.py")

    assert graph.indexer.conn.execute(
        "SELECT 1 FROM repository_nodes WHERE path='gone.py'").fetchone() is None
    assert graph.find_symbol("gone")[0] == []
    assert graph.graph_history()[0][0]["trigger"] == "file_remove"
    graph.close()


def test_read_repair_detects_same_size_same_mtime_content_change(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("def old():\n    return 1\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    original = path.stat()

    path.write_text("def new():\n    return 1\n", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_size == original.st_size

    rows, envelope = graph.find_symbol("new")

    assert [row["fqn"] for row in rows] == ["module.new"]
    assert envelope.fresh is False
    assert graph.find_symbol("old")[0] == []
    graph.close()


def test_graph_revision_is_linked_to_git_commit_and_stage_versions(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True).returncode:
        pytest.skip("git unavailable")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Graph Test"],
                   cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path,
        check=True, capture_output=True, text=True).stdout.strip()

    graph = CodeGraph(tmp_path)
    stats = graph.index()
    revision = graph.indexer.conn.execute(
        "SELECT * FROM graph_revisions WHERE id=?", (stats["revision_id"],)
    ).fetchone()
    stages = {row["stage"]: dict(row) for row in graph.indexer.conn.execute(
        "SELECT stage, stage_version, status, artifact_hash "
        "FROM graph_stage_runs WHERE revision_id=?", (stats["revision_id"],)
    )}

    assert revision["git_commit"] == head
    assert revision["git_dirty"] == 0
    assert set(stages) == {
        "filesystem", "l0", "l1", "l2_rank", "l2_communities", "l3", "dataflow",
    }
    assert stages["filesystem"]["artifact_hash"] == revision["source_snapshot_hash"]
    assert stages["l0"]["artifact_hash"]
    assert stages["l1"]["status"] == "not_started"
    assert stages["dataflow"]["status"] == "on_demand"
    graph.overview()
    graph.communities()
    graph.data_flow("app.run")
    updated = {row["stage"]: row["status"] for row in graph.indexer.conn.execute(
        "SELECT stage, status FROM graph_stage_runs WHERE revision_id=?",
        (stats["revision_id"],),
    )}
    assert updated["l2_rank"] == "complete"
    assert updated["l2_communities"] == "complete"
    assert updated["dataflow"] == "executed"
    history, envelope = graph.graph_history()
    assert envelope.fresh is True
    assert history[0]["git_commit"] == head
    assert {stage["stage"] for stage in history[0]["stages"]} == set(stages)
    graph.close()


def test_cli_exposes_repository_tree_and_graph_history(tmp_path, capsys):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text(
        "def run():\n    return 1\n", encoding="utf-8")
    assert cli.main(["--root", str(tmp_path), "index"]) == 0
    capsys.readouterr()

    assert cli.main([
        "--root", str(tmp_path), "tree", "pkg", "--no-refresh",
    ]) == 0
    tree_output = capsys.readouterr().out
    assert "pkg/" in tree_output
    assert "app.py [indexed]" in tree_output

    assert cli.main(["--root", str(tmp_path), "history", "--limit", "1"]) == 0
    history_output = capsys.readouterr().out
    assert "r1 complete full_index" in history_output
    assert "filesystem@1=complete" in history_output
