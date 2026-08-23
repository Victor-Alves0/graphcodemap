"""Freshness contracts for semantic L1 callsite proofs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codegraph import CodeGraph
from codegraph import l1
from codegraph.l1 import promote
from codegraph.l1.lsp_base import LspResolver


class _AliveProc:
    def poll(self):
        return None


class _UniverseResolver(LspResolver):
    languages = ("java",)
    root_markers = ()

    @classmethod
    def available(cls):
        return True

    def __init__(self, root, project_root=None):
        self.root = Path(root).resolve()
        self.project_root = Path(project_root or root).resolve()
        self.proc = _AliveProc()
        self._ok = True
        self._dead = False
        self._ready = True
        self._opened = set()
        self._lines = {}
        self._defcache = {}
        self._health_errors = []
        self._health_warnings = []
        self._diagnostics_by_uri = {}
        self._semantic_sites = 0
        self._semantic_hits = 0
        self._warmup_timed_out = False
        self._shutdown_started_healthy = False
        self._notify = lambda *_a: None

    def _definition(self, *_args):
        if (self.project_root / "semantic-universe-empty").exists():
            return []
        locations = []
        for name in ("A.java", "B.java"):
            path = self.project_root / name
            if not path.exists():
                continue
            line = path.read_text(encoding="utf-8").splitlines()[0]
            locations.append((path.as_uri(), 0, line.index("target")))
        return locations

    def close(self):
        pass


class _MavenUniverseResolver(_UniverseResolver):
    root_markers = ("pom.xml",)

    def _definition(self, *_args):
        pom = self.project_root / "pom.xml"
        if pom.exists() and "empty" in pom.read_text(encoding="utf-8"):
            return []
        path = self.project_root / "A.java"
        if not path.exists():
            return []
        line = path.read_text(encoding="utf-8").splitlines()[0]
        return [(path.as_uri(), 0, line.index("target"))]


def _write_initial_program(root: Path) -> None:
    (root / "A.java").write_text(
        "class A { static void target() {} }\n", encoding="utf-8")
    (root / "Main.java").write_text(
        "class Main { void run() { A.target(); } }\n", encoding="utf-8")


def _write_maven_project(root: Path, name: str, *, marker=True) -> Path:
    project = root / name
    project.mkdir(parents=True)
    if marker:
        (project / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    _write_initial_program(project)
    return project


def _site_rows(graph: CodeGraph):
    return graph.indexer.conn.execute(
        "SELECT e.confidence, e.resolver, s.fqn FROM edges e "
        "LEFT JOIN symbols s ON s.id=e.dst JOIN files f ON f.id=e.file_id "
        "WHERE f.path='Main.java' AND e.kind='calls' "
        "AND e.dst_name LIKE '%target' ORDER BY s.fqn"
    ).fetchall()


def _project_site_rows(conn, project: str):
    return conn.execute(
        "SELECT e.confidence, e.resolver, s.fqn FROM edges e "
        "LEFT JOIN symbols s ON s.id=e.dst JOIN files f ON f.id=e.file_id "
        "WHERE f.path=? AND e.kind='calls' "
        "AND e.dst_name LIKE '%target' ORDER BY s.fqn",
        (f"{project}/Main.java",),
    ).fetchall()


def _install_resolver(monkeypatch, resolver=_UniverseResolver):
    monkeypatch.setattr(l1, "all_resolvers", lambda: [resolver])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [resolver])


def test_new_override_replaces_previous_l1_fanout(tmp_path, monkeypatch):
    _write_initial_program(tmp_path)
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch)
        first = l1.refine(graph.indexer)
        assert first["promoted"] == 1
        assert [(row["confidence"], row["resolver"])
                for row in _site_rows(graph)] == [("certain", "l1")]

        (tmp_path / "B.java").write_text(
            "class B { static void target() {} }\n", encoding="utf-8")
        graph.index()
        second = l1.refine(graph.indexer, rels=["B.java"])
        rows = _site_rows(graph)

        assert second["revalidated"] == second["promoted"] == 1
        assert second["files"] == 3
        assert len(rows) == 2
        assert {row["resolver"] for row in rows} == {"l1"}
        assert {row["confidence"] for row in rows} == {"inferred"}
        assert {row["fqn"].split(".")[0] for row in rows} == {"A", "B"}
    finally:
        graph.close()


def test_changed_semantic_universe_drops_old_certainty(tmp_path, monkeypatch):
    _write_initial_program(tmp_path)
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch)
        assert l1.refine(graph.indexer)["promoted"] == 1

        # Deliberately not indexed: build/classpath state may change while both
        # source files and their content hashes remain identical.
        (tmp_path / "semantic-universe-empty").touch()
        stats = l1.refine(graph.indexer)
        rows = _site_rows(graph)

        assert stats["status"] == "complete"
        assert stats["revalidated"] == 1 and stats["promoted"] == 0
        assert rows
        assert all(row["resolver"] == "l0" for row in rows)
        assert all(row["confidence"] != "certain" for row in rows)
    finally:
        graph.close()


def test_partial_resolver_start_cannot_preserve_old_certainty(
        tmp_path, monkeypatch):
    _write_initial_program(tmp_path)
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch)
        assert l1.refine(graph.indexer)["promoted"] == 1

        (tmp_path / "B.java").write_text(
            "class B { static void target() {} }\n", encoding="utf-8")
        graph.index()

        class BrokenResolver:
            languages = ("java",)
            root_markers = ()

            def __init__(self, *_a, **_k):
                raise OSError("semantic server failed")

        _install_resolver(monkeypatch, BrokenResolver)
        stats = l1.refine(graph.indexer, rels=["B.java"])
        rows = _site_rows(graph)

        assert stats["status"] == "partial" and stats["errors"] == 1
        assert stats["revalidated"] == 1 and stats["promoted"] == 0
        assert rows
        assert all(row["resolver"] == "l0" for row in rows)
        assert all(row["confidence"] != "certain" for row in rows)
    finally:
        graph.close()


def test_watcher_new_override_revalidates_unchanged_caller(tmp_path,
                                                            monkeypatch):
    from codegraph.watcher import Watcher

    _write_initial_program(tmp_path)
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch)
        assert l1.refine(graph.indexer)["promoted"] == 1
    finally:
        graph.close()

    (tmp_path / "B.java").write_text(
        "class B { static void target() {} }\n", encoding="utf-8")
    watcher = Watcher(tmp_path)
    try:
        watcher._pending = {"B.java"}
        stats = watcher.drain()
        assert stats["indexed"] == 1
        rows = watcher.ix.conn.execute(
            "SELECT e.confidence, e.resolver, s.fqn FROM edges e "
            "LEFT JOIN symbols s ON s.id=e.dst JOIN files f ON f.id=e.file_id "
            "WHERE f.path='Main.java' AND e.kind='calls' "
            "AND e.dst_name LIKE '%target' ORDER BY s.fqn"
        ).fetchall()
        assert len(rows) == 2
        assert {row["resolver"] for row in rows} == {"l1"}
        assert {row["confidence"] for row in rows} == {"inferred"}
    finally:
        watcher.stop()


def test_watcher_deleted_java_revalidates_unchanged_caller(tmp_path,
                                                            monkeypatch):
    from codegraph.watcher import Watcher

    _write_initial_program(tmp_path)
    (tmp_path / "B.java").write_text(
        "class B { static void target() {} }\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch)
        assert l1.refine(graph.indexer)["promoted"] == 1
        rows = _site_rows(graph)
        assert len(rows) == 2
        assert {row["confidence"] for row in rows} == {"inferred"}
    finally:
        graph.close()

    (tmp_path / "B.java").unlink()
    watcher = Watcher(tmp_path)
    try:
        watcher._pending = {"B.java"}
        stats = watcher.drain()
        rows = watcher.ix.conn.execute(
            "SELECT e.confidence, e.resolver, s.fqn FROM edges e "
            "LEFT JOIN symbols s ON s.id=e.dst JOIN files f ON f.id=e.file_id "
            "WHERE f.path='Main.java' AND e.kind='calls' "
            "AND e.dst_name LIKE '%target' ORDER BY s.fqn"
        ).fetchall()

        assert stats["removed"] == 1
        assert [(row["confidence"], row["resolver"])
                for row in rows] == [("certain", "l1")]
        assert rows[0]["fqn"].endswith("A.target")
    finally:
        watcher.stop()


def test_direct_refine_marker_modification_drops_stale_certainty(
        tmp_path, monkeypatch):
    project = _write_maven_project(tmp_path, "one")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch, _MavenUniverseResolver)
        assert l1.refine(graph.indexer)["promoted"] == 1
        assert _project_site_rows(
            graph.indexer.conn, "one")[0]["confidence"] == "certain"

        (project / "pom.xml").write_text(
            "<project>empty</project>\n", encoding="utf-8")
        stats = l1.refine(graph.indexer, rels=["one/pom.xml"])
        rows = _project_site_rows(graph.indexer.conn, "one")

        assert stats["revalidated"] == 1 and stats["promoted"] == 0
        assert stats["files"] == 2 and stats["roots"] == 1
        assert rows and all(row["resolver"] == "l0" for row in rows)
        assert all(row["confidence"] != "certain" for row in rows)
    finally:
        graph.close()


def test_unavailable_resolver_on_marker_change_cannot_keep_certainty(
        tmp_path, monkeypatch):
    project = _write_maven_project(tmp_path, "one")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch, _MavenUniverseResolver)
        assert l1.refine(graph.indexer)["promoted"] == 1

        (project / "pom.xml").write_text(
            "<project>changed</project>\n", encoding="utf-8")
        monkeypatch.setattr(l1, "all_resolvers",
                            lambda: [_MavenUniverseResolver])
        monkeypatch.setattr(l1, "available_resolvers", lambda _root: [])
        stats = l1.refine(graph.indexer, rels=["one/pom.xml"])
        rows = _project_site_rows(graph.indexer.conn, "one")

        assert stats["status"] == "partial" and stats["unavailable"]
        assert stats["revalidated"] == 1 and stats["promoted"] == 0
        assert rows and all(row["resolver"] == "l0" for row in rows)
        assert all(row["confidence"] != "certain" for row in rows)
    finally:
        graph.close()


def test_marker_create_delete_recomputes_root_and_isolates_sibling(
        tmp_path, monkeypatch):
    one = _write_maven_project(tmp_path, "one")
    _write_maven_project(tmp_path, "two")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch, _MavenUniverseResolver)
        assert l1.refine(graph.indexer)["promoted"] == 2

        (one / "pom.xml").unlink()
        deleted = l1.refine(graph.indexer, rels=["one/pom.xml"])
        one_rows = _project_site_rows(graph.indexer.conn, "one")
        two_rows = _project_site_rows(graph.indexer.conn, "two")
        assert deleted["revalidated"] == 1 and deleted["promoted"] == 0
        assert one_rows and all(row["resolver"] == "l0" for row in one_rows)
        assert [(row["confidence"], row["resolver"])
                for row in two_rows] == [("certain", "l1")]

        (one / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        created = l1.refine(graph.indexer, rels=["one/pom.xml"])
        one_rows = _project_site_rows(graph.indexer.conn, "one")
        two_rows = _project_site_rows(graph.indexer.conn, "two")
        assert created["promoted"] == 1 and created["roots"] == 1
        assert [(row["confidence"], row["resolver"])
                for row in one_rows] == [("certain", "l1")]
        assert [(row["confidence"], row["resolver"])
                for row in two_rows] == [("certain", "l1")]
    finally:
        graph.close()


def test_watcher_marker_event_revalidates_without_indexing_marker(
        tmp_path, monkeypatch):
    project = _write_maven_project(tmp_path, "one")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        _install_resolver(monkeypatch, _MavenUniverseResolver)
        assert l1.refine(graph.indexer)["promoted"] == 1
    finally:
        graph.close()

    (project / "pom.xml").write_text(
        "<project>empty</project>\n", encoding="utf-8")
    from codegraph.watcher import Watcher

    watcher = Watcher(tmp_path)
    watcher._schedule = lambda: None
    try:
        watcher._note(str(project / "pom.xml"))
        assert watcher._pending == {"one/pom.xml"}
        stats = watcher.drain()
        rows = _project_site_rows(watcher.ix.conn, "one")

        assert stats["indexed"] == stats["removed"] == stats["errors"] == 0
        assert rows and all(row["resolver"] == "l0" for row in rows)
        assert all(row["confidence"] != "certain" for row in rows)
    finally:
        watcher.stop()


def test_l1_fanout_replacement_rolls_back_as_one_site(tmp_path):
    _write_initial_program(tmp_path)
    (tmp_path / "B.java").write_text(
        "class B { static void target() {} }\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        conn = graph.indexer.conn
        edge = conn.execute(
            "SELECT e.id, e.file_id, e.line, e.col FROM edges e JOIN files f "
            "ON f.id=e.file_id WHERE f.path='Main.java' "
            "AND e.kind='calls' AND e.dst_name LIKE '%target' ORDER BY e.id"
        ).fetchone()
        targets = [row["id"] for row in conn.execute(
            "SELECT s.id FROM symbols s JOIN files f ON f.id=s.file_id "
            "WHERE f.path IN ('A.java','B.java') AND s.name='target' "
            "ORDER BY f.path"
        ).fetchall()]
        before = [tuple(row) for row in _site_rows(graph)]
        conn.execute(
            "CREATE TEMP TRIGGER reject_l1_clone BEFORE INSERT ON edges "
            "WHEN NEW.resolver='l1' BEGIN "
            "SELECT RAISE(ABORT, 'injected clone failure'); END"
        )

        with pytest.raises(sqlite3.IntegrityError,
                           match="injected clone failure"):
            promote.apply(conn, edge["file_id"], edge, targets)

        assert [tuple(row) for row in _site_rows(graph)] == before
    finally:
        graph.close()
