"""Regressoes do feedback operacional: MCP instalavel e JDTLS observavel."""

from __future__ import annotations

import os
import json
from pathlib import Path

from codegraph import CodeGraph, cli, l1, render
from codegraph.l1 import lsp_base
from codegraph.l1 import promote
from codegraph.l1.jdtls import JdtlsResolver


def _java_graph(tmp_path) -> CodeGraph:
    (tmp_path / "Main.java").write_text(
        "class Main { void run() { helper(); } void helper() {} }\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def test_mcp_extra_caps_sdk_before_removed_fastmcp_api():
    project = (Path(__file__).parents[1] / "pyproject.toml").read_text(
        encoding="utf-8")
    assert 'mcp = ["mcp>=1.2,<2"]' in project


def test_jdtls_timeouts_are_configurable_and_validated(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGRAPH_JDTLS_READY_TIMEOUT", "300")
    monkeypatch.setenv("CODEGRAPH_JDTLS_IO_TIMEOUT", "360")
    monkeypatch.setattr(lsp_base.LspResolver, "__init__", lambda *_a, **_k: None)

    resolver = JdtlsResolver(tmp_path)
    assert resolver.ready_timeout == 300
    assert resolver.io_timeout == 360

    monkeypatch.delenv("CODEGRAPH_JDTLS_IO_TIMEOUT")
    resolver = JdtlsResolver(tmp_path)
    assert resolver.io_timeout == 300

    monkeypatch.setenv("CODEGRAPH_JDTLS_IO_TIMEOUT", "299")
    try:
        JdtlsResolver(tmp_path)
    except ValueError as exc:
        assert "maior ou igual" in str(exc)
    else:  # pragma: no cover - contrato defensivo
        raise AssertionError("timeout incoerente foi aceito")


def test_cli_timeout_flags_reach_refine_without_leaking_environment(
        monkeypatch, tmp_path, capsys):
    graph = _java_graph(tmp_path)
    graph.close()
    observed = {}

    def fake_refine(_indexer):
        observed["ready"] = os.environ.get("CODEGRAPH_JDTLS_READY_TIMEOUT")
        observed["io"] = os.environ.get("CODEGRAPH_JDTLS_IO_TIMEOUT")
        return {"promoted": 0, "files": 1, "status": "complete",
                "partial": False, "errors": 0, "resolvers": ["java"]}

    monkeypatch.setattr(l1, "refine", fake_refine)
    monkeypatch.delenv("CODEGRAPH_JDTLS_READY_TIMEOUT", raising=False)
    monkeypatch.delenv("CODEGRAPH_JDTLS_IO_TIMEOUT", raising=False)

    code = cli.main([
        "--root", str(tmp_path), "refine",
        "--jdtls-ready-timeout", "300",
        "--jdtls-io-timeout", "360",
    ])
    capsys.readouterr()

    assert code == 0
    assert observed == {"ready": "300.0", "io": "360.0"}
    assert "CODEGRAPH_JDTLS_READY_TIMEOUT" not in os.environ
    assert "CODEGRAPH_JDTLS_IO_TIMEOUT" not in os.environ


def test_jdtls_readiness_timeout_is_partial_and_actionable(tmp_path):
    resolver = object.__new__(JdtlsResolver)
    resolver.root = resolver.project_root = tmp_path
    resolver._ok = True
    resolver._dead = False
    resolver._shutdown_started_healthy = False
    resolver._health_errors = []
    resolver._health_warnings = []
    resolver._diagnostics_by_uri = {}
    resolver._semantic_sites = 7
    resolver._semantic_hits = 0
    resolver._warmup_timed_out = True
    resolver._io_timed_out = True
    resolver.ready_timeout = 300
    resolver.io_timeout = 360

    health = resolver.health_report()

    assert health["status"] == "partial"
    assert health["warmup_timed_out"] is True
    assert health["io_timed_out"] is True
    assert any("--jdtls-ready-timeout" in item for item in health["errors"])
    assert any("--jdtls-io-timeout" in item for item in health["errors"])


def test_jdtls_semantic_hits_prove_readiness_despite_unresolved_probe(tmp_path):
    resolver = object.__new__(JdtlsResolver)
    resolver.root = resolver.project_root = tmp_path
    resolver._ok = True
    resolver._dead = False
    resolver._shutdown_started_healthy = False
    resolver._health_errors = []
    resolver._health_warnings = []
    resolver._diagnostics_by_uri = {}
    resolver._semantic_sites = 1575
    resolver._semantic_hits = 345
    resolver._warmup_timed_out = True
    resolver._io_timed_out = False
    resolver.ready_timeout = 300
    resolver.io_timeout = 360

    health = resolver.health_report()

    assert health["status"] == "complete"
    assert health["errors"] == []
    assert health["warmup_timed_out"] is True
    assert any("345/1575" in item for item in health["warnings"])


def test_semantic_hits_do_not_mask_transport_or_jdtls_diagnostic(tmp_path):
    resolver = object.__new__(JdtlsResolver)
    resolver.root = resolver.project_root = tmp_path
    resolver._ok = True
    resolver._dead = False
    resolver._shutdown_started_healthy = False
    resolver._health_errors = [
        "An internal error occurred during: Publish Diagnostics",
    ]
    resolver._health_warnings = []
    resolver._diagnostics_by_uri = {}
    resolver._semantic_sites = 1575
    resolver._semantic_hits = 345
    resolver._warmup_timed_out = True
    resolver._io_timed_out = True
    resolver.ready_timeout = 300
    resolver.io_timeout = 360

    health = resolver.health_report()

    assert health["status"] == "partial"
    assert any("Publish Diagnostics" in item for item in health["errors"])
    assert any("timeout de I/O" in item for item in health["errors"])


def test_doctor_preserves_last_partial_refine_and_does_not_suggest_retry(
        tmp_path, monkeypatch):
    graph = _java_graph(tmp_path)

    class TimedOutJava:
        languages = ("java",)
        root_markers = ()

        def __init__(self, *_a, **_k):
            pass

        def refine_file(self, *_a):
            return 0

        def close(self):
            pass

        def health_report(self):
            return {
                "status": "partial",
                "errors": [f"falha em {tmp_path}: JDTLS não ficou pronto"],
                "warnings": [],
                "sites": 1,
                "resolved_sites": 0,
                "warmup_timed_out": True,
                "io_timed_out": True,
                "ready_timeout_s": 120,
                "io_timeout_s": 120,
            }

    monkeypatch.setattr(l1, "all_resolvers", lambda: [TimedOutJava])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [TimedOutJava])
    stats = l1.refine(graph.indexer)
    doctor = graph.doctor()
    output = render.doctor(doctor)
    graph.close()

    assert stats["partial"] is True
    assert doctor["l1_last_run"]["runs"][0]["warmup_timed_out"] is True
    assert str(tmp_path) not in json.dumps(doctor)
    assert "última passada L1 terminou parcial" in output
    assert "considere rodar `refine`" not in output


def test_java_unavailable_message_distinguishes_jdtls_from_runtime(
        tmp_path, monkeypatch):
    monkeypatch.delenv("CODEGRAPH_JDTLS", raising=False)
    missing_home = JdtlsResolver.unavailable_details()
    assert "CODEGRAPH_JDTLS" in missing_home["reason"]
    assert "plugins/" in missing_home["action"]

    home = tmp_path / "jdtls"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "org.eclipse.equinox.launcher_1.jar").touch()
    (home / ("config_win" if os.name == "nt" else "config_linux")).mkdir()
    monkeypatch.setenv("CODEGRAPH_JDTLS", str(home))
    monkeypatch.setattr(JdtlsResolver, "_java_bin", classmethod(lambda cls: None))

    missing_java = JdtlsResolver.unavailable_details()
    assert "runtime Java" in missing_java["reason"]
    assert "CODEGRAPH_JDTLS_JAVA" in missing_java["action"]


def test_partial_root_rolls_back_only_its_own_promotions(tmp_path, monkeypatch):
    for root, suffix in (("bad", "Bad"), ("good", "Good")):
        project = tmp_path / root
        project.mkdir()
        (project / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        (project / "Main.java").write_text(
            f"class Main{suffix} {{\n"
            f"  void run() {{ Target{suffix}.hit(); }}\n"
            "}\n",
            encoding="utf-8",
        )
        (project / "Target.java").write_text(
            f"class Target{suffix} {{\n"
            "  static void hit() {}\n"
            "}\n",
            encoding="utf-8",
        )
    graph = CodeGraph(tmp_path)
    graph.index()

    class TwoRoots:
        languages = ("java",)
        root_markers = ("pom.xml",)

        def __init__(self, _root, project_root=None):
            self.partial = project_root.name == "bad"

        def refine_file(self, conn, _root, rel, file_id):
            if not rel.endswith("Main.java"):
                return 0
            edge = conn.execute(
                "SELECT id, line, col FROM edges WHERE file_id=? "
                "AND kind='calls' AND dst_name LIKE '%hit' LIMIT 1",
                (file_id,),
            ).fetchone()
            target_rel = rel.replace("Main.java", "Target.java")
            target = conn.execute(
                "SELECT s.id FROM symbols s JOIN files f ON f.id=s.file_id "
                "WHERE f.path=? AND s.name='hit' LIMIT 1",
                (target_rel,),
            ).fetchone()
            return promote.apply(conn, file_id, edge, [target["id"]])

        def close(self):
            pass

        def health_report(self):
            if self.partial:
                return {"status": "partial", "errors": ["late timeout"],
                        "warnings": []}
            return {"status": "complete", "errors": [], "warnings": []}

    monkeypatch.setattr(l1, "all_resolvers", lambda: [TwoRoots])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [TwoRoots])
    stats = l1.refine(graph.indexer)
    rows = graph.indexer.conn.execute(
        "SELECT f.path, e.confidence, e.resolver FROM edges e "
        "JOIN files f ON f.id=e.file_id WHERE e.kind='calls' "
        "AND f.path LIKE '%/Main.java' AND e.dst_name LIKE '%hit' "
        "ORDER BY f.path",
    ).fetchall()
    graph.close()

    assert stats["status"] == "partial"
    assert stats["promoted"] == 1
    assert stats["rolled_back"] == 1
    assert [(run["promoted"], run.get("rolled_back", 0))
            for run in stats["runs"]] == [(0, 1), (1, 0)]
    by_path = {row["path"]: (row["confidence"], row["resolver"])
               for row in rows}
    assert by_path["bad/Main.java"][1] == "l0"
    assert by_path["bad/Main.java"][0] != "certain"
    assert by_path["good/Main.java"] == ("certain", "l1")
