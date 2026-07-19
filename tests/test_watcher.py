"""M2: watcher com debounce — drain síncrono + integração com eventos reais."""

from __future__ import annotations

import time

import pytest

from codegraph import CodeGraph
from codegraph.watcher import Watcher


def test_drain_applies_edit(cg, repo):
    auth = repo / "app" / "auth.py"
    auth.write_text(auth.read_text(encoding="utf-8")
                    .replace("def issue_token", "def issue_token_v2"),
                    encoding="utf-8")
    w = Watcher(repo, db_path=repo / ".codegraph" / "graph.db")
    w._pending = {"app/auth.py"}
    stats = w.drain()
    assert stats["indexed"] == 1
    row = w.ix.conn.execute(
        "SELECT 1 FROM symbols WHERE name='issue_token_v2'").fetchone()
    assert row is not None
    w.stop()


def test_drain_removes_deleted(cg, repo):
    (repo / "app" / "db.py").unlink()
    w = Watcher(repo, db_path=repo / ".codegraph" / "graph.db")
    w._pending = {"app/db.py"}
    stats = w.drain()
    assert stats["removed"] == 1
    row = w.ix.conn.execute(
        "SELECT 1 FROM files WHERE path='app/db.py'").fetchone()
    assert row is None
    w.stop()


def test_full_rescan_flag(cg, repo):
    (repo / "app" / "novo.py").write_text("def novo():\n    pass\n", encoding="utf-8")
    w = Watcher(repo, db_path=repo / ".codegraph" / "graph.db")
    w._full_rescan = True
    stats = w.drain()
    assert stats["full"] and stats["indexed"] >= 1
    row = w.ix.conn.execute("SELECT 1 FROM symbols WHERE name='novo'").fetchone()
    assert row is not None
    w.stop()


def test_note_filters(repo):
    w = Watcher(repo)
    w._note(str(repo / ".codegraph" / "graph.db"))
    w._note(str(repo / "logo.png"))           # formato não suportado
    assert w._pending == set()
    w._note(str(repo / "app" / "auth.py"))
    assert w._pending == {"app/auth.py"}
    if w._timer is not None:
        w._timer.cancel()


def test_git_head_triggers_full_rescan(repo):
    w = Watcher(repo)
    (repo / ".git").mkdir(exist_ok=True)
    w._note(str(repo / ".git" / "HEAD"))
    assert w._full_rescan is True
    assert w._pending == set()
    if w._timer is not None:
        w._timer.cancel()


@pytest.mark.timeout(20)
def test_watcher_end_to_end(cg, repo):
    w = Watcher(repo, db_path=repo / ".codegraph" / "graph.db", debounce=0.3)
    w.start()
    try:
        (repo / "app" / "watched.py").write_text(
            "def watched_fn():\n    return 1\n", encoding="utf-8")
        deadline = time.time() + 15
        found = False
        while time.time() < deadline:
            if w.ix is not None and w.ix.conn.execute(
                    "SELECT 1 FROM symbols WHERE name='watched_fn'").fetchone():
                found = True
                break
            time.sleep(0.2)
        assert found, "watcher não indexou o arquivo novo a tempo"
    finally:
        w.stop()
