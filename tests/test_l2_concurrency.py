"""Lazy L2 recomputation must coexist with watcher/MCP writers."""

from __future__ import annotations

import threading
import time

import pytest

from codegraph import CodeGraph
from codegraph import community, rank
from codegraph.db import connect


@pytest.mark.parametrize(
    ("mark", "ensure"),
    [(rank.mark_dirty, rank.ensure_ranks),
     (community.mark_dirty, community.ensure_communities)],
)
def test_lazy_l2_retries_when_another_writer_temporarily_holds_db(
        tmp_path, mark, ensure):
    (tmp_path / "a.py").write_text(
        "def helper():\n    return 1\n\ndef run():\n    return helper()\n",
        encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    mark(g.indexer.conn)
    g.indexer.conn.commit()
    # Faz a falha aparecer rápido; o retry da aplicação, não o busy_timeout de
    # 10 s, deve absorver a contenção curta.
    g.indexer.conn.execute("PRAGMA busy_timeout=1")

    other = connect(g.indexer.db_path)
    other.execute("BEGIN IMMEDIATE")
    other.execute("UPDATE meta SET value=value WHERE key='schema_version'")

    def release():
        time.sleep(0.12)
        other.rollback()

    t = threading.Thread(target=release)
    t.start()
    try:
        assert ensure(g.indexer.conn) is True
    finally:
        t.join(timeout=2)
        other.close()
        g.close()
