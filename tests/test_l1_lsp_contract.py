"""Contratos de robustez do cliente LSP compartilhado pelos resolvers L1."""

from __future__ import annotations

import os

from codegraph.l1 import lsp_base


def _bare_resolver(tmp_path):
    r = object.__new__(lsp_base.LspResolver)
    r.root = tmp_path
    r.project_root = tmp_path
    r.init_options = {"settings": {"java": {"autobuild": {"enabled": True}}}}
    r._dead = False
    r._defcache = {}
    r._lines = {}
    return r


def test_uri_conversion_rejects_non_file_and_decodes_once():
    assert lsp_base._uri_to_path("https://example.test/A.java") is None
    got = lsp_base._uri_to_path("file:///C:/literal%2520name/A.java")
    assert got is not None
    assert "%20" in str(got)


def test_uri_conversion_preserves_unc_host_on_windows():
    got = lsp_base._uri_to_path("file://server/share/A.java")
    assert got is not None
    if os.name == "nt":
        assert str(got).lower().startswith(r"\\server\share")


def test_query_column_converts_utf8_tree_sitter_to_utf16_lsp(tmp_path):
    r = _bare_resolver(tmp_path)
    source = "😀 café.service.compute()"
    r._lines = {"Main.java": [source]}
    byte_col = len("😀 café.service".encode("utf-8"))
    got = r._query_col("Main.java", 1, byte_col, "service.compute")
    expected = len("😀 café.service.".encode("utf-16-le")) // 2
    assert got == expected


def test_definition_prefers_narrow_selection_range_and_keeps_column(tmp_path):
    r = _bare_resolver(tmp_path)
    uri = (tmp_path / "Calc.java").as_uri()
    r._request = lambda *_a, **_k: [{
        "targetUri": uri,
        "targetRange": {"start": {"line": 2, "character": 0}},
        "targetSelectionRange": {"start": {"line": 4, "character": 13}},
    }]
    assert r._definition("Main.java", 0, 0) == [(uri, 4, 13)]


def test_definition_column_converts_utf16_back_to_utf8(tmp_path):
    (tmp_path / "Calc.java").write_text("😀 void compute() {}\n", encoding="utf-8")
    r = _bare_resolver(tmp_path)
    char0 = len("😀 void ".encode("utf-16-le")) // 2
    assert r._definition_byte_col("Calc.java", 0, char0) == len(
        "😀 void ".encode("utf-8"))


def test_workspace_configuration_has_one_result_per_requested_item(tmp_path):
    r = _bare_resolver(tmp_path)
    got = r._server_request_result({
        "method": "workspace/configuration",
        "params": {"items": [
            {"section": "java.autobuild.enabled"},
            {"section": "java"},
            {"section": "missing"},
        ]},
    })
    assert got == [True, {"autobuild": {"enabled": True}}, None]


def test_initialize_does_not_claim_success_without_response(tmp_path):
    r = _bare_resolver(tmp_path)
    r._request = lambda *_a, **_k: None
    notifications = []
    r._notify = lambda *args: notifications.append(args)
    assert r._initialize() is False
    assert notifications == []


def test_initialize_advertises_workspace_configuration(tmp_path):
    r = _bare_resolver(tmp_path)
    captured = {}

    def request(_method, params):
        captured.update(params)
        return {}

    r._request = request
    r._notify = lambda *_a: None
    assert r._initialize() is True
    workspace = captured["capabilities"]["workspace"]
    assert workspace == {"configuration": True, "workspaceFolders": True}


def test_request_has_total_deadline_even_with_progress_messages(tmp_path,
                                                               monkeypatch):
    r = _bare_resolver(tmp_path)
    r._seq = 0
    r.io_timeout = 1.0
    r.proc = type("Proc", (), {"poll": lambda self: None})()
    r._write = lambda *_a: None
    r._read = lambda _timeout: {"jsonrpc": "2.0", "method": "$/progress"}
    killed = []
    r._kill = lambda: killed.append(True)
    ticks = iter((0.0, 0.5, 1.1))
    monkeypatch.setattr(lsp_base.time, "monotonic", lambda: next(ticks))
    assert r._request("example", {}) is None
    assert killed == [True]


def test_available_resolvers_isolates_discovery_failure(monkeypatch):
    import codegraph.l1 as l1

    class Broken:
        @staticmethod
        def available():
            raise RuntimeError("broken discovery")

    class Healthy:
        @staticmethod
        def available():
            return True

    monkeypatch.setattr(l1, "all_resolvers", lambda: [Broken, Healthy])
    assert l1.available_resolvers() == [Healthy]


def test_refine_isolates_resolver_start_failure(tmp_path, monkeypatch):
    from codegraph import CodeGraph
    import codegraph.l1 as l1

    (tmp_path / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()

    class Broken:
        languages = ("python",)
        root_markers = ()

        def __init__(self, *_a, **_k):
            raise OSError("spawn failed")

    class Healthy:
        languages = ("python",)
        root_markers = ()

        def __init__(self, *_a, **_k):
            pass

        def refine_file(self, *_a):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [Broken, Healthy])
    stats = l1.refine(graph.indexer)
    graph.close()
    assert stats["errors"] == 1
    assert stats["servers"] == 2
    assert stats["files"] == 1
    assert stats["roots"] == 1


def test_jdtls_removes_workspace_when_spawn_fails(tmp_path, monkeypatch):
    from codegraph.l1 import jdtls

    home = tmp_path / "jdtls"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "org.eclipse.equinox.launcher_1.0.0.jar").touch()
    (home / ("config_win" if os.name == "nt" else "config_linux")).mkdir()
    data = tmp_path / "workspace"
    monkeypatch.setenv("CODEGRAPH_JDTLS", str(home))
    monkeypatch.setattr(jdtls.tempfile, "mkdtemp", lambda **_k: str(data))
    monkeypatch.setattr(lsp_base.subprocess, "Popen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
    try:
        jdtls.JdtlsResolver(tmp_path)
    except OSError:
        pass
    else:
        raise AssertionError("spawn deveria falhar")
    assert not data.exists()


def test_jdtls_chooses_newest_launcher_version(tmp_path):
    from codegraph.l1.jdtls import JdtlsResolver

    plugins = tmp_path / "plugins"
    plugins.mkdir()
    old = plugins / "org.eclipse.equinox.launcher_1.9.0.jar"
    new = plugins / "org.eclipse.equinox.launcher_1.10.0.jar"
    old.touch(); new.touch()
    assert JdtlsResolver._launcher_jar(tmp_path) == new
