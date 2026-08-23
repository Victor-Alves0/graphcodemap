"""Contratos de saude/observabilidade das passadas L1."""

from __future__ import annotations

import os
import queue
import zipfile
from pathlib import Path

from codegraph.l1 import lsp_base


def _resolver(tmp_path):
    resolver = object.__new__(lsp_base.LspResolver)
    resolver.root = tmp_path
    resolver.project_root = tmp_path
    resolver._ok = True
    resolver._dead = False
    resolver._health_errors = []
    resolver._health_warnings = []
    resolver._diagnostics_by_uri = {}
    resolver._semantic_sites = 0
    resolver._semantic_hits = 0
    resolver._warmup_timed_out = False
    return resolver


def test_json_rpc_error_marks_resolver_partial(tmp_path):
    resolver = _resolver(tmp_path)
    resolver._seq = 0
    resolver.io_timeout = 1.0
    resolver.proc = type("Proc", (), {"poll": lambda self: None})()
    resolver._write = lambda *_a: None
    resolver._read = lambda _timeout: {
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32603, "message": "project import failed"},
    }

    assert resolver._request("textDocument/definition", {}) is None
    health = resolver.health_report()
    assert health["status"] == "partial"
    assert any("project import failed" in error for error in health["errors"])


def test_jdtls_error_status_and_diagnostics_are_not_discarded(tmp_path):
    resolver = _resolver(tmp_path)
    resolver._observe_message({
        "method": "language/status",
        "params": {"type": "Error", "message": "Gradle import failed"},
    })
    resolver._observe_message({
        "method": "textDocument/publishDiagnostics",
        "params": {"diagnostics": [{
            "severity": 1, "message": "Java 11 toolchain was not found",
        }]},
    })

    health = resolver.health_report()
    assert health["status"] == "partial"
    assert any("Gradle import failed" in error for error in health["errors"])
    assert any("Java 11 toolchain" in error for error in health["errors"])


def test_diagnostics_replace_previous_snapshot_per_uri(tmp_path):
    resolver = _resolver(tmp_path)
    resolver._observe_message({
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": "file:///A.java", "diagnostics": [
            {"severity": 1, "message": "old A"},
        ]},
    })
    resolver._observe_message({
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": "file:///B.java", "diagnostics": [
            {"severity": 1, "message": "current B"},
        ]},
    })
    resolver._observe_message({
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": "file:///A.java", "diagnostics": []},
    })

    health = resolver.health_report()
    assert health["status"] == "partial"
    assert health["errors"] == ["current B"]


def test_diagnostics_new_publication_replaces_old_errors(tmp_path):
    resolver = _resolver(tmp_path)
    for message in ("old", "new"):
        resolver._observe_message({
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": "file:///A.java", "diagnostics": [
                {"severity": 1, "message": message},
            ]},
        })
    assert resolver.health_report()["errors"] == ["new"]


def test_zero_semantic_hits_is_warning_not_error_without_failure_signal(tmp_path):
    resolver = _resolver(tmp_path)
    resolver._semantic_sites = 3
    resolver._warmup_timed_out = True

    health = resolver.health_report()
    assert health["status"] == "complete"
    assert health["errors"] == []
    assert any("nao comprovada" in warning for warning in health["warnings"])


def test_shutdown_timeout_does_not_rewrite_successful_run_as_no_handshake(
        tmp_path):
    resolver = _resolver(tmp_path)
    resolver._semantic_sites = 4
    resolver._semantic_hits = 3

    class Proc:
        def poll(self):
            return None

        def kill(self):
            pass

    resolver.proc = Proc()
    resolver._request = lambda *_a, **_k: resolver._kill()

    resolver.close()
    health = resolver.health_report()

    assert resolver._dead is True
    assert health["status"] == "complete"
    assert health["errors"] == []
    assert health["resolved_sites"] == 3


def test_failure_before_shutdown_remains_partial(tmp_path):
    resolver = _resolver(tmp_path)
    resolver._semantic_sites = 4
    resolver._semantic_hits = 1

    class Proc:
        def poll(self):
            return 1

    resolver.proc = Proc()
    resolver._dead = True
    resolver._ok = False

    resolver.close()
    health = resolver.health_report()

    assert health["status"] == "partial"
    assert health["errors"] == [
        "servidor LSP indisponivel ou sem handshake"]


def test_real_diagnostic_during_shutdown_remains_partial(tmp_path):
    resolver = _resolver(tmp_path)

    class Proc:
        def poll(self):
            return None

        def kill(self):
            pass

    resolver.proc = Proc()

    def shutdown(*_a, **_k):
        resolver._observe_message({
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": "file:///Broken.java", "diagnostics": [{
                "severity": 1, "message": "cannot resolve project type",
            }]},
        })
        resolver._kill()

    resolver._request = shutdown
    resolver.close()
    health = resolver.health_report()

    assert health["status"] == "partial"
    assert health["errors"] == ["cannot resolve project type"]


def test_shutdown_drains_response_then_late_diagnostic_and_eof(tmp_path):
    resolver = _resolver(tmp_path)
    resolver._seq = 0
    resolver.io_timeout = resolver.shutdown_timeout = 1.0
    resolver._q = queue.Queue()
    resolver._q.put({"jsonrpc": "2.0", "id": 1, "result": None})
    resolver._q.put({
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": "file:///Late.java", "diagnostics": [{
            "severity": 1, "message": "late compile failure",
        }]},
    })
    resolver._q.put(lsp_base._EOF)

    class Reader:
        def is_alive(self):
            return False

        def join(self, timeout):
            assert timeout >= 0

    class Proc:
        def poll(self):
            return None

        def wait(self, timeout):
            assert 0 <= timeout <= 1.0
            return 0

        def kill(self):
            pass

    resolver._reader = Reader()
    resolver.proc = Proc()
    resolver._write = lambda *_a: None
    resolver._notify = lambda *_a: None

    resolver.close()
    health = resolver.health_report()

    assert health["status"] == "partial"
    assert health["errors"] == ["late compile failure"]


def test_unexpected_process_exit_before_close_is_partial(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.shutdown_timeout = 1.0
    resolver._q = queue.Queue()
    resolver._q.put(lsp_base._EOF)
    resolver.proc = type("Proc", (), {"poll": lambda self: 7})()

    resolver.close()

    assert resolver.health_report()["status"] == "partial"


def test_jdtls_io_deadline_covers_its_gradle_import_window():
    from codegraph.l1.jdtls import JdtlsResolver

    assert JdtlsResolver.io_timeout >= JdtlsResolver.ready_timeout


def _fake_jdtls_home(tmp_path, class_major=65):
    home = tmp_path / "jdtls"
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "org.eclipse.equinox.launcher_1.0.0.jar").touch()
    (home / ("config_win" if os.name == "nt" else "config_linux")).mkdir()
    with zipfile.ZipFile(plugins / "org.eclipse.jdt.ls.core_1.0.0.jar", "w") as jar:
        jar.writestr(
            "org/eclipse/jdt/ls/core/internal/JavaLanguageServerPlugin.class",
            b"\xca\xfe\xba\xbe\x00\x00" + class_major.to_bytes(2, "big"),
        )
    return home


def test_jdtls_dedicated_java_precedes_project_java_home(tmp_path, monkeypatch):
    from codegraph.l1.jdtls import JdtlsResolver

    project_jdk = tmp_path / "project-jdk"
    runtime_jdk = tmp_path / "runtime-jdk"
    suffix = ".exe" if os.name == "nt" else ""
    for jdk in (project_jdk, runtime_jdk):
        (jdk / "bin").mkdir(parents=True)
        (jdk / "bin" / f"java{suffix}").touch()
    monkeypatch.setenv("JAVA_HOME", str(project_jdk))
    monkeypatch.setenv("CODEGRAPH_JDTLS_JAVA", str(runtime_jdk))

    assert Path(JdtlsResolver._java_bin()).resolve() == (
        runtime_jdk / "bin" / f"java{suffix}").resolve()
    assert os.environ["JAVA_HOME"] == str(project_jdk)


def test_jdtls_runtime_requirement_comes_from_plugin_bytecode(tmp_path,
                                                              monkeypatch):
    from codegraph.l1.jdtls import JdtlsResolver

    home = _fake_jdtls_home(tmp_path, class_major=65)
    monkeypatch.setenv("CODEGRAPH_JDTLS", str(home))
    monkeypatch.setattr(JdtlsResolver, "_java_bin", classmethod(lambda cls: "java"))
    monkeypatch.setattr(JdtlsResolver, "_runtime_java_major",
                        staticmethod(lambda _java: 17))
    assert JdtlsResolver._required_java_major(home) == 21
    assert JdtlsResolver.available() is False
    monkeypatch.setattr(JdtlsResolver, "_runtime_java_major",
                        staticmethod(lambda _java: 21))
    assert JdtlsResolver.available() is True


def test_refine_propagates_explicit_resolver_health(tmp_path, monkeypatch):
    from codegraph import CodeGraph
    from codegraph import l1

    (tmp_path / "Main.java").write_text(
        "class Main { void run() { helper(); } void helper() {} }\n",
        encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    class FailedImport:
        languages = ("java",)
        root_markers = ()

        def __init__(self, *_a, **_k):
            pass

        def refine_file(self, *_a):
            return 0

        def health_report(self):
            return {
                "status": "partial", "errors": ["Gradle import failed"],
                "warnings": [], "sites": 1, "resolved_sites": 0,
                "warmup_timed_out": False,
            }

        def close(self):
            pass

    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [FailedImport])
    stats = l1.refine(graph.indexer)
    graph.close()

    assert stats["status"] == "partial" and stats["partial"] is True
    assert stats["errors"] == 1
    assert stats["promoted"] == 0
    assert stats["runs"][0]["status"] == "partial"
    assert any("Gradle import failed" in warning for warning in stats["warnings"])


def test_refine_zero_promotions_can_still_be_complete(tmp_path, monkeypatch):
    from codegraph import CodeGraph
    from codegraph import l1

    (tmp_path / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    class EmptyButHealthy:
        languages = ("java",)
        root_markers = ()

        def __init__(self, *_a, **_k):
            pass

        def refine_file(self, *_a):
            return 0

        def health_report(self):
            return {"status": "complete", "errors": [], "warnings": [],
                    "sites": 0, "resolved_sites": 0,
                    "warmup_timed_out": False}

        def close(self):
            pass

    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [EmptyButHealthy])
    stats = l1.refine(graph.indexer)
    graph.close()

    assert stats["status"] == "complete" and stats["partial"] is False
    assert stats["errors"] == stats["promoted"] == 0
    assert stats["applicable"] == stats["attempted"] == ["java"]
    assert stats["unavailable"] == []


def test_refine_is_partial_when_present_language_has_no_resolver(tmp_path,
                                                                 monkeypatch):
    from codegraph import CodeGraph
    from codegraph import l1

    (tmp_path / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    class MissingJava:
        languages = ("java",)
        cmd_name = "jdtls"
        cmd_env = "CODEGRAPH_JDTLS"

    monkeypatch.setattr(l1, "all_resolvers", lambda: [MissingJava])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [])
    stats = l1.refine(graph.indexer)
    graph.close()

    assert stats["status"] == "partial" and stats["partial"] is True
    assert stats["applicable"] == ["java"]
    assert stats["attempted"] == []
    assert stats["unavailable"] == [{
        "languages": ["java"], "resolver": "MissingJava",
        "server": "jdtls", "env": "CODEGRAPH_JDTLS",
    }]


def test_refine_keeps_other_languages_but_reports_java_unavailable(
        tmp_path, monkeypatch):
    from codegraph import CodeGraph
    from codegraph import l1

    (tmp_path / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    class MissingJava:
        languages = ("java",)
        cmd_name = "jdtls"
        cmd_env = "CODEGRAPH_JDTLS"

    class HealthyPython:
        languages = ("python",)
        root_markers = ()

        def __init__(self, *_a, **_k):
            pass

        def refine_file(self, *_a):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(l1, "all_resolvers",
                        lambda: [MissingJava, HealthyPython])
    monkeypatch.setattr(l1, "available_resolvers",
                        lambda _root: [HealthyPython])
    stats = l1.refine(graph.indexer)
    graph.close()

    assert stats["status"] == "partial"
    assert stats["applicable"] == ["java", "python"]
    assert stats["attempted"] == ["python"]
    assert stats["resolvers"] == ["python"]
