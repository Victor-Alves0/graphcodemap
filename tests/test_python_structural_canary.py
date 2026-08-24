from __future__ import annotations

import shutil
from pathlib import Path

from codegraph import CodeGraph
from codegraph.util import content_hash


FIXTURE = Path(__file__).parent / "fixtures" / "python_structural_canary"


def _copy_canary(tmp_path: Path) -> Path:
    root = tmp_path / "python-structural-canary"
    shutil.copytree(FIXTURE, root)
    return root


def _symbol_projection(graph: CodeGraph) -> set[tuple[str, str, str, str | None]]:
    return {
        (row["path"], row["kind"], row["fqn"], row["parent_fqn"])
        for row in graph.indexer.conn.execute(
            "SELECT f.path, s.kind, s.fqn, p.fqn parent_fqn "
            "FROM symbols s JOIN files f ON f.id=s.file_id "
            "LEFT JOIN symbols p ON p.id=s.parent_id "
            "WHERE f.language='python'"
        )
    }


def _resolved_edges(graph: CodeGraph) -> set[tuple[str, str, str]]:
    return {
        (row["kind"], row["src_fqn"], row["dst_fqn"])
        for row in graph.indexer.conn.execute(
            "SELECT e.kind, src.fqn src_fqn, dst.fqn dst_fqn "
            "FROM edges e JOIN symbols src ON src.id=e.src "
            "JOIN symbols dst ON dst.id=e.dst"
        )
    }


def test_python_canary_persists_exact_physical_and_structural_projection(tmp_path):
    root = _copy_canary(tmp_path)
    graph = CodeGraph(root)
    try:
        stats = graph.index()
        repository = {
            row["path"]: dict(row)
            for row in graph.indexer.conn.execute(
                "SELECT path, kind, content_hash, index_state, state_reason "
                "FROM repository_nodes"
            )
        }

        assert stats["scanned"] == 4
        assert stats["indexed"] == 4
        assert stats["errors"] == 0
        assert set(repository) == {
            "", "assets", "assets/badge.svg", "pyproject.toml", "src",
            "src/live_map", "src/live_map/__init__.py",
            "src/live_map/decorators.py", "src/live_map/service.py",
        }
        assert repository["assets/badge.svg"]["index_state"] == "not_applicable"
        assert repository["assets/badge.svg"]["state_reason"] == (
            "unrecognized_extension"
        )
        assert repository["assets/badge.svg"]["content_hash"] == content_hash(
            (root / "assets" / "badge.svg").read_bytes()
        )
        assert {
            row[0] for row in graph.indexer.conn.execute("SELECT path FROM files")
        } == {
            "pyproject.toml", "src/live_map/__init__.py",
            "src/live_map/decorators.py", "src/live_map/service.py",
        }

        assert _symbol_projection(graph) == {
            ("src/live_map/__init__.py", "file", "live_map", None),
            ("src/live_map/__init__.py", "variable", "live_map.__all__", None),
            ("src/live_map/decorators.py", "file", "live_map.decorators", None),
            ("src/live_map/decorators.py", "function",
             "live_map.decorators.traced", None),
            ("src/live_map/decorators.py", "parameter",
             "live_map.decorators.traced.fn", "live_map.decorators.traced"),
            ("src/live_map/decorators.py", "function",
             "live_map.decorators.traced.wrapper", "live_map.decorators.traced"),
            ("src/live_map/decorators.py", "parameter",
             "live_map.decorators.traced.wrapper.args",
             "live_map.decorators.traced.wrapper"),
            ("src/live_map/decorators.py", "parameter",
             "live_map.decorators.traced.wrapper.kwargs",
             "live_map.decorators.traced.wrapper"),
            ("src/live_map/service.py", "file", "live_map.service", None),
            ("src/live_map/service.py", "class", "live_map.service.Account", None),
            ("src/live_map/service.py", "method",
             "live_map.service.Account.__init__", "live_map.service.Account"),
            ("src/live_map/service.py", "parameter",
             "live_map.service.Account.__init__.self",
             "live_map.service.Account.__init__"),
            ("src/live_map/service.py", "parameter",
             "live_map.service.Account.__init__.owner",
             "live_map.service.Account.__init__"),
            ("src/live_map/service.py", "field", "live_map.service.Account._owner",
             "live_map.service.Account"),
            ("src/live_map/service.py", "property", "live_map.service.Account.owner",
             "live_map.service.Account"),
            ("src/live_map/service.py", "parameter",
             "live_map.service.Account.owner.self", "live_map.service.Account.owner"),
            ("src/live_map/service.py", "method", "live_map.service.Account.label",
             "live_map.service.Account"),
            ("src/live_map/service.py", "parameter",
             "live_map.service.Account.label.self", "live_map.service.Account.label"),
            ("src/live_map/service.py", "parameter",
             "live_map.service.Account.label.prefix", "live_map.service.Account.label"),
            ("src/live_map/service.py", "local",
             "live_map.service.Account.label.normalized",
             "live_map.service.Account.label"),
            ("src/live_map/service.py", "function",
             "live_map.service.Account.label.render",
             "live_map.service.Account.label"),
            ("src/live_map/service.py", "parameter",
             "live_map.service.Account.label.render.suffix",
             "live_map.service.Account.label.render"),
            ("src/live_map/service.py", "function",
             "live_map.service.build_label", None),
            ("src/live_map/service.py", "parameter",
             "live_map.service.build_label.owner", "live_map.service.build_label"),
            ("src/live_map/service.py", "local",
             "live_map.service.build_label.account", "live_map.service.build_label"),
        }

        edges = _resolved_edges(graph)
        assert {
            ("imports", "live_map", "live_map.service.Account"),
            ("imports", "live_map", "live_map.service.build_label"),
            ("references", "live_map.service.Account.label",
             "live_map.decorators.traced"),
            ("calls", "live_map.service.Account.label",
             "live_map.service.Account.label.render"),
            ("calls", "live_map.service.build_label", "live_map.service.Account"),
            ("calls", "live_map.service.build_label",
             "live_map.service.Account.label"),
            ("writes", "live_map.service.Account.__init__",
             "live_map.service.Account._owner"),
            ("reads", "live_map.service.Account.label.render",
             "live_map.service.Account.label.normalized"),
            ("reads", "live_map.service.Account.label.render",
             "live_map.service.Account.label.self"),
            ("reads", "live_map.service.Account.label.render",
             "live_map.service.Account.owner"),
        } <= edges
        assert (
            "calls", "live_map.service.Account.label", "live_map.decorators.traced"
        ) not in edges
        assert graph.indexer.conn.execute(
            "SELECT 1 FROM edges e JOIN symbols src ON src.id=e.src "
            "JOIN symbols dst ON dst.id=e.dst "
            "WHERE e.kind='writes' AND e.line=6 "
            "AND src.fqn='live_map.service.Account.__init__' "
            "AND dst.fqn='live_map.service.Account.__init__.self'"
        ).fetchone() is None
        assert (
            "references", "live_map.service.build_label",
            "live_map.service.Account.owner"
        ) not in edges

        # Every nested declaration is physically owned by the same source file
        # and structurally contained by its lexical parent.
        missing_contains = graph.indexer.conn.execute(
            "SELECT s.fqn FROM symbols s JOIN files f ON f.id=s.file_id "
            "JOIN symbols p ON p.id=s.parent_id "
            "WHERE f.language='python' "
            "AND NOT EXISTS ("
            " SELECT 1 FROM edges e "
            " WHERE e.kind='contains' AND e.dst=s.id AND e.src=p.id"
            ")"
        ).fetchall()
        assert missing_contains == []
    finally:
        graph.close()


def test_python_canary_add_edit_rename_delete_keeps_projections_in_sync(tmp_path):
    root = _copy_canary(tmp_path)
    graph = CodeGraph(root)
    try:
        first = graph.index()
        stable_fqns = {
            "live_map.service.Account",
            "live_map.service.Account.owner",
            "live_map.service.Account.label",
            "live_map.service.build_label",
        }
        stable_ids = {
            row["fqn"]: row["id"] for row in graph.indexer.conn.execute(
                "SELECT id, fqn FROM symbols WHERE fqn IN (?,?,?,?)",
                tuple(sorted(stable_fqns)),
            )
        }

        feature = root / "src" / "live_map" / "feature.py"
        feature.write_text(
            "from .service import build_label\n\n"
            "def present(owner: str) -> str:\n"
            "    return build_label(owner)\n",
            encoding="utf-8",
        )
        added = graph.index()
        assert added["indexed"] == 1
        assert added["removed"] == 0
        assert {row["fqn"] for row in graph.indexer.conn.execute(
            "SELECT fqn FROM symbols WHERE fqn LIKE 'live_map.feature%'"
        )} == {
            "live_map.feature", "live_map.feature.present",
            "live_map.feature.present.owner",
        }
        assert (
            "calls", "live_map.feature.present", "live_map.service.build_label"
        ) in _resolved_edges(graph)

        service = root / "src" / "live_map" / "service.py"
        old_hash = graph.indexer.conn.execute(
            "SELECT content_hash FROM repository_nodes "
            "WHERE path='src/live_map/service.py'"
        ).fetchone()[0]
        service.write_text(
            service.read_text(encoding="utf-8").replace("normalized", "cleaned"),
            encoding="utf-8",
        )
        edited = graph.index()
        assert edited["indexed"] == 1
        assert edited["removed"] == 0
        assert graph.indexer.conn.execute(
            "SELECT 1 FROM symbols "
            "WHERE fqn='live_map.service.Account.label.normalized'"
        ).fetchone() is None
        assert graph.indexer.conn.execute(
            "SELECT 1 FROM symbols "
            "WHERE fqn='live_map.service.Account.label.cleaned'"
        ).fetchone() is not None
        assert graph.indexer.conn.execute(
            "SELECT content_hash FROM repository_nodes "
            "WHERE path='src/live_map/service.py'"
        ).fetchone()[0] != old_hash
        assert {
            row["fqn"]: row["id"] for row in graph.indexer.conn.execute(
                "SELECT id, fqn FROM symbols WHERE fqn IN (?,?,?,?)",
                tuple(sorted(stable_fqns)),
            )
        } == stable_ids

        presenter = feature.with_name("presenter.py")
        feature.rename(presenter)
        renamed = graph.index()
        assert renamed["indexed"] == 1
        assert renamed["removed"] == 1
        assert graph.indexer.conn.execute(
            "SELECT 1 FROM repository_nodes "
            "WHERE path='src/live_map/feature.py'"
        ).fetchone() is None
        assert graph.indexer.conn.execute(
            "SELECT 1 FROM symbols WHERE fqn LIKE 'live_map.feature%'"
        ).fetchone() is None
        assert (
            "calls", "live_map.presenter.present", "live_map.service.build_label"
        ) in _resolved_edges(graph)

        presenter.unlink()
        deleted = graph.index()
        assert deleted["indexed"] == 0
        assert deleted["removed"] == 1
        assert graph.indexer.conn.execute(
            "SELECT 1 FROM repository_nodes "
            "WHERE path='src/live_map/presenter.py'"
        ).fetchone() is None
        assert graph.indexer.conn.execute(
            "SELECT 1 FROM symbols WHERE fqn LIKE 'live_map.presenter%'"
        ).fetchone() is None
        assert not any(
            edge[1] == "live_map.presenter.present"
            for edge in _resolved_edges(graph)
        )

        revisions = graph.indexer.conn.execute(
            "SELECT id, parent_revision_id, trigger, source_snapshot_hash "
            "FROM graph_revisions ORDER BY id"
        ).fetchall()
        assert [row["trigger"] for row in revisions] == ["full_index"] * 5
        assert [row["parent_revision_id"] for row in revisions] == [
            None, first["revision_id"], added["revision_id"],
            edited["revision_id"], renamed["revision_id"],
        ]
        assert len({row["source_snapshot_hash"] for row in revisions}) == 5
        assert {
            row[0] for row in graph.indexer.conn.execute(
                "SELECT path FROM repository_nodes WHERE kind='file'"
            )
        } == {
            "assets/badge.svg", "pyproject.toml", "src/live_map/__init__.py",
            "src/live_map/decorators.py", "src/live_map/service.py",
        }
    finally:
        graph.close()
