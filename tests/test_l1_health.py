"""Contratos de saude/observabilidade das passadas L1."""

from __future__ import annotations

import os
import queue
import zipfile
from pathlib import Path

import pytest

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

    params = {
        "textDocument": {"uri": "file:///src/Broken.java"},
        "position": {"line": 11, "character": 7},
    }
    assert resolver._request("textDocument/definition", params) is None
    health = resolver.health_report()
    assert health["status"] == "partial"
    assert any("project import failed" in error for error in health["errors"])
    assert any("Broken.java:12:7" in error for error in health["errors"])


def test_definition_internal_error_isolated_to_site(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.cmd_name = "jdtls"
    resolver._active_method = "textDocument/definition"
    resolver._active_params = {
        "textDocument": {"uri": "file:///src/Anonymous.java"},
        "position": {"line": 60, "character": 45},
    }
    resolver._observe_message({
        "error": {
            "code": -32603,
            "message": "Internal error.",
            "data": "java.lang.ArrayStoreException: element type mismatch",
        },
    })

    health = resolver.health_report()
    assert health["status"] == "complete"
    assert health["errors"] == []
    assert health["semantic_request_errors"] == 1
    assert any("Anonymous.java:61:45" in item for item in health["warnings"])


def test_non_jdtls_definition_internal_error_remains_partial(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.cmd_name = "another-lsp"
    resolver._active_method = "textDocument/definition"
    resolver._active_params = {
        "textDocument": {"uri": "file:///src/Broken.ts"},
        "position": {"line": 2, "character": 4},
    }
    resolver._observe_message({
        "error": {"code": -32603, "message": "Internal error."},
    })

    health = resolver.health_report()
    assert health["status"] == "partial"
    assert health["semantic_request_errors"] == 0


def test_definition_missing_file_log_is_site_warning(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.cmd_name = "jdtls"
    (tmp_path / "Example.java").write_text("class Example {}")
    resolver._active_method = "textDocument/definition"
    resolver._observe_message({
        "method": "window/logMessage",
        "params": {"type": 1, "message": "Example.java does not exist"},
    })

    health = resolver.health_report()
    assert health["status"] == "complete"
    assert health["errors"] == []
    assert health["warnings"] == ["Example.java does not exist"]


def test_late_missing_file_log_does_not_inherit_finished_definition(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.cmd_name = "jdtls"
    resolver._seq = 0
    resolver.io_timeout = 1.0
    resolver.proc = type("Proc", (), {"poll": lambda self: None})()
    resolver._write = lambda *_a: None
    resolver._read = lambda _timeout: {
        "jsonrpc": "2.0", "id": 1, "result": [],
    }
    params = {
        "textDocument": {"uri": "file:///src/Done.java"},
        "position": {"line": 1, "character": 2},
    }

    assert resolver._request("textDocument/definition", params) == []
    resolver._observe_message({
        "method": "window/logMessage",
        "params": {"type": 1, "message": "Late.java does not exist"},
    })

    health = resolver.health_report()
    assert health["status"] == "partial"
    assert health["errors"] == ["Late.java does not exist"]


def test_existing_unique_java_file_makes_late_missing_log_a_warning(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.cmd_name = "jdtls"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Present.java").write_text("class Present {}")

    resolver._observe_message({
        "method": "window/logMessage",
        "params": {"type": 1, "message": "Present.java does not exist"},
    })

    health = resolver.health_report()
    assert health["status"] == "complete"
    assert health["warnings"] == ["Present.java does not exist"]


def test_jdtls_optional_nested_annotation_output_is_warning(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.cmd_name = "jdtls"
    resolver._observe_message({
        "method": "window/logMessage",
        "params": {"type": 1, "message": (
            "Failed to add classpath entry for generated source folder "
            "annotations: Cannot nest 'server/target/generated-sources/annotations' "
            "inside 'server/target/generated-sources'"
        )},
    })

    health = resolver.health_report()
    assert health["status"] == "complete"
    assert health["errors"] == []
    assert len(health["warnings"]) == 1
    assert not getattr(resolver, "_workspace_tainted", False)


def test_jdtls_shutdown_only_failures_do_not_rollback_healthy_analysis(
        tmp_path):
    resolver = _resolver(tmp_path)
    resolver.cmd_name = "jdtls"
    resolver._shutdown_started_healthy = True
    resolver._active_method = "shutdown"

    resolver._record_health(
        "error", 'An internal error occurred during: "Register Watchers". '
        "JobManager is suspended; m2e is shut down!")

    health = resolver.health_report()
    assert health["status"] == "complete"
    assert health["errors"] == []
    assert len(health["warnings"]) == 1


def test_jdtls_unresolved_generated_type_stays_partial(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.cmd_name = "jdtls"
    resolver._observe_message({
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": "file:///GeneratedConsumer.java", "diagnostics": [{
            "severity": 1, "message": "GeneratedThing cannot be resolved",
        }]},
    })

    assert resolver.health_report()["status"] == "partial"


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


def test_jdtls_shutdown_waits_for_transient_diagnostic_to_clear(
        tmp_path, monkeypatch):
    from codegraph.l1 import jdtls

    resolver = object.__new__(jdtls.JdtlsResolver)
    uri = "file:///Transient.java"
    resolver._diagnostics_by_uri = {uri: ["missing during import"]}
    resolver.diagnostics_settle_timeout = 5.0
    resolver.diagnostics_quiet_period = 0.75
    clock = [10.0]
    waits = []
    cleared_at = []

    monkeypatch.setattr(jdtls.time, "monotonic", lambda: clock[0])

    def drain(timeout=0.0):
        waits.append(timeout)
        clock[0] += timeout
        # A implementação antiga encerrava após 0,75 s de quietude mesmo com
        # este erro ainda corrente. Publique o clear somente depois desse ponto
        # para caracterizar a regressão real, sem sleeps de parede.
        if len(waits) == 10:
            resolver._observe_message({
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            })
            cleared_at.append(clock[0])

    resolver._drain_pending = drain
    resolver._before_shutdown()

    assert resolver._diagnostics_by_uri[uri] == []
    assert clock[0] - cleared_at[0] >= resolver.diagnostics_quiet_period
    assert clock[0] < 15.0
    resolver._ok = True
    resolver._dead = False
    resolver._shutdown_started_healthy = True
    health = resolver.health_report()
    assert health["status"] == "complete"
    assert health["errors"] == []


def test_jdtls_shutdown_keeps_draining_while_diagnostic_is_current(
        tmp_path, monkeypatch):
    from codegraph.l1 import jdtls

    resolver = object.__new__(jdtls.JdtlsResolver)
    resolver._diagnostics_by_uri = {
        "file:///Broken.java": ["real compile failure"],
    }
    resolver.diagnostics_settle_timeout = 2.0
    resolver.diagnostics_quiet_period = 0.75
    clock = [20.0]
    waits = []

    monkeypatch.setattr(jdtls.time, "monotonic", lambda: clock[0])

    def drain(timeout=0.0):
        waits.append(timeout)
        clock[0] += timeout

    resolver._drain_pending = drain
    resolver._before_shutdown()

    assert resolver._diagnostics_by_uri["file:///Broken.java"] == [
        "real compile failure"]
    assert abs(clock[0] - 22.0) < 1e-9
    assert len(waits) > 1
    resolver._ok = True
    resolver._dead = False
    resolver._shutdown_started_healthy = True
    health = resolver.health_report()
    assert health["status"] == "partial"
    assert health["errors"] == ["real compile failure"]


def test_jdtls_shutdown_uses_short_quiet_period_without_diagnostics(
        tmp_path, monkeypatch):
    from codegraph.l1 import jdtls

    resolver = object.__new__(jdtls.JdtlsResolver)
    resolver._diagnostics_by_uri = {}
    resolver.diagnostics_settle_timeout = 30.0
    resolver.diagnostics_quiet_period = 0.75
    clock = [30.0]

    monkeypatch.setattr(jdtls.time, "monotonic", lambda: clock[0])
    resolver._drain_pending = lambda timeout=0.0: clock.__setitem__(
        0, clock[0] + timeout)

    resolver._before_shutdown()

    assert abs(clock[0] - 30.75) < 1e-9


def test_jdtls_diagnostic_timeout_extends_total_shutdown_budget(
        tmp_path, monkeypatch):
    from codegraph.l1 import jdtls

    monkeypatch.setenv("CODEGRAPH_JDTLS_DIAGNOSTICS_TIMEOUT", "12.5")
    monkeypatch.setattr(lsp_base.LspResolver, "__init__",
                        lambda self, root, project_root=None: None)

    resolver = jdtls.JdtlsResolver(tmp_path, tmp_path)

    assert resolver.diagnostics_settle_timeout == 12.5
    assert resolver.shutdown_timeout == (
        12.5 + resolver.shutdown_protocol_timeout)


def test_jdtls_close_reserves_protocol_budget_after_diagnostic_settle(
        tmp_path, monkeypatch):
    from codegraph.l1 import jdtls

    resolver = object.__new__(jdtls.JdtlsResolver)
    resolver.root = resolver.project_root = tmp_path
    resolver._ok = True
    resolver._dead = False
    resolver._opened = set()
    resolver._diagnostics_by_uri = {
        "file:///Broken.java": ["persistent compile failure"],
    }
    resolver._health_errors = []
    resolver._health_warnings = []
    resolver.diagnostics_settle_timeout = 2.0
    resolver.diagnostics_quiet_period = 0.75
    resolver.shutdown_protocol_timeout = 1.0
    resolver.shutdown_timeout = 3.0
    resolver._shutdown_completed = False
    clock = [40.0]
    request_timeouts = []
    wait_timeouts = []

    monkeypatch.setattr(jdtls.time, "monotonic", lambda: clock[0])

    def drain(timeout=0.0):
        clock[0] += timeout

    resolver._drain_pending = drain

    class Proc:
        done = False

        def poll(self):
            return 0 if self.done else None

        def wait(self, timeout):
            wait_timeouts.append(timeout)
            clock[0] += timeout
            self.done = True
            return 0

        def kill(self):
            self.done = True

    resolver.proc = Proc()

    def request(method, params, **kwargs):
        assert (method, params) == ("shutdown", None)
        request_timeouts.append(kwargs["timeout"])
        clock[0] += 0.4
        return None

    resolver._request = request
    resolver._notify = lambda *_args, **_kwargs: None

    resolver.close()

    assert request_timeouts[0] == pytest.approx(1.0)
    assert wait_timeouts[0] == pytest.approx(0.6)
    assert clock[0] == pytest.approx(43.0)
    health = resolver.health_report()
    assert health["status"] == "partial"
    assert health["errors"] == ["persistent compile failure"]


def test_jdtls_refine_closes_only_current_document_in_finally(
        tmp_path, monkeypatch):
    from codegraph.l1 import jdtls

    resolver = object.__new__(jdtls.JdtlsResolver)
    resolver.root = tmp_path
    resolver._dead = False
    resolver._opened = {"Main.java", "ReadinessProbe.java"}
    resolver._lines = {
        "Main.java": ["class Main {}"],
        "ReadinessProbe.java": ["class ReadinessProbe {}"],
    }
    resolver.proc = type("Proc", (), {"poll": lambda self: None})()
    notifications = []
    resolver._notify = lambda *args: notifications.append(args)

    def fail_refine(*_args, **_kwargs):
        raise RuntimeError("definition failed")

    monkeypatch.setattr(lsp_base.LspResolver, "refine_file", fail_refine)

    with pytest.raises(RuntimeError, match="definition failed"):
        resolver.refine_file(None, tmp_path, "Main.java", 1)

    assert notifications == [("textDocument/didClose", {"textDocument": {
        "uri": (tmp_path / "Main.java").as_uri(),
    }})]
    assert resolver._opened == {"ReadinessProbe.java"}
    assert set(resolver._lines) == {"ReadinessProbe.java"}


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


def test_explicit_session_shutdown_diagnostic_is_teardown_warning(tmp_path):
    resolver = _resolver(tmp_path)
    resolver._shutdown_started_healthy = True
    resolver._active_method = "shutdown"
    resolver._observe_message({
        "method": "window/logMessage",
        "params": {"type": 1, "message": (
            "warning: while diagnosing orphaned files: session is shut down")},
    })

    health = resolver.health_report()

    assert health["status"] == "complete"
    assert health["errors"] == []
    assert any("session is shut down" in item for item in health["warnings"])


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


def test_jdtls_disables_autobuild_for_declared_build_project(tmp_path,
                                                              monkeypatch):
    from codegraph.l1.jdtls import JdtlsResolver

    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    captured = {}

    def fake_init(self, root, project_root=None):
        captured.update(self.init_options)

    monkeypatch.setattr(lsp_base.LspResolver, "__init__", fake_init)
    JdtlsResolver(tmp_path, tmp_path)

    java = captured["settings"]["java"]
    assert java["autobuild"]["enabled"] is False
    assert java["configuration"]["maven"]["defaultMojoExecutionAction"] == "ignore"


def test_jdtls_keeps_autobuild_for_invisible_project(tmp_path, monkeypatch):
    from codegraph.l1.jdtls import JdtlsResolver

    captured = {}
    monkeypatch.setattr(
        lsp_base.LspResolver, "__init__",
        lambda self, root, project_root=None: captured.update(self.init_options))
    JdtlsResolver(tmp_path, tmp_path)

    assert captured["settings"]["java"]["autobuild"]["enabled"] is True


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


def test_jdtls_keeps_eclipse_metadata_out_of_project_root(tmp_path,
                                                           monkeypatch):
    from codegraph.l1 import jdtls

    home = _fake_jdtls_home(tmp_path)
    java = tmp_path / "jdk" / "bin" / "java.exe"
    java.parent.mkdir(parents=True)
    java.touch()
    data = tmp_path / "isolated-workspace" / "data"

    class FakeWorkspace:
        def __init__(self, *_args):
            self.data = data

        def acquire(self):
            return self

    monkeypatch.setenv("CODEGRAPH_JDTLS", str(home))
    monkeypatch.setattr(jdtls, "JdtlsWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        jdtls.JdtlsResolver, "_java_bin", classmethod(lambda _cls: str(java)))
    monkeypatch.setattr(
        jdtls.JdtlsResolver, "_runtime_java_major",
        staticmethod(lambda _java: 21))

    resolver = object.__new__(jdtls.JdtlsResolver)
    resolver.project_root = tmp_path
    argv = resolver._popen_argv()

    flag = "-Djava.import.generatesMetadataFilesAtProjectRoot=false"
    assert argv.count(flag) == 1
    assert argv.index(flag) < argv.index("-jar")
    assert argv[argv.index("-data") + 1] == str(data)


def test_jdtls_orderly_tainted_workspace_is_stale_not_crashed(tmp_path,
                                                               monkeypatch):
    from codegraph.l1 import jdtls

    released = []

    class Workspace:
        def release(self, **kwargs):
            released.append(kwargs)

    resolver = object.__new__(jdtls.JdtlsResolver)
    resolver._workspace = Workspace()
    resolver._ok = True
    resolver._dead = False
    resolver._workspace_tainted = True
    resolver._health_errors = []
    resolver._diagnostics_by_uri = {}
    resolver.proc = type("Proc", (), {"poll": lambda self: None})()

    def orderly_close(self):
        self._shutdown_completed = True

    monkeypatch.setattr(lsp_base.LspResolver, "close", orderly_close)
    resolver.close()

    assert released == [{"clean": True, "reusable": False}]


def test_jdtls_broken_transport_preserves_running_workspace(tmp_path,
                                                             monkeypatch):
    from codegraph.l1 import jdtls

    released = []

    class Workspace:
        def release(self, **kwargs):
            released.append(kwargs)

    resolver = object.__new__(jdtls.JdtlsResolver)
    resolver._workspace = Workspace()
    resolver._ok = True
    resolver._dead = False
    resolver._workspace_tainted = False
    resolver._health_errors = ["transport failed"]
    resolver._diagnostics_by_uri = {}
    resolver.proc = type("Proc", (), {"poll": lambda self: None})()

    def broken_close(self):
        self._shutdown_completed = False

    monkeypatch.setattr(lsp_base.LspResolver, "close", broken_close)
    resolver.close()

    assert released == [{"clean": False, "reusable": False}]


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
