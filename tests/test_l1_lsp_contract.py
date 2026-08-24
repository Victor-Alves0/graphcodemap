"""Contratos de robustez do cliente LSP compartilhado pelos resolvers L1."""

from __future__ import annotations

import os

from codegraph.l1 import lsp_base
from codegraph import CodeGraph


def _bare_resolver(tmp_path):
    r = object.__new__(lsp_base.LspResolver)
    r.root = tmp_path
    r.project_root = tmp_path
    r.init_options = {"settings": {"java": {"autobuild": {"enabled": True}}}}
    r._dead = False
    r._defcache = {}
    r._lines = {}
    return r


def test_readiness_probe_uses_cross_file_edge_when_project_is_repo_root(tmp_path):
    (tmp_path / "Calc.java").write_text(
        "class Calc { static int compute() { return 1; } }", encoding="utf-8")
    (tmp_path / "Main.java").write_text(
        "class Main { int run() { return Calc.compute(); } }", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        resolver = _bare_resolver(tmp_path)
        resolver.languages = ("java",)
        probe = resolver._project_readiness_probe(graph.indexer.conn)
        assert probe is not None
        assert probe["rel"] == "Main.java"
        assert probe["dst_name"] == "Calc.compute"
    finally:
        graph.close()


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


def _refine_java_edge_to_definition(graph, source_rel, target_rel,
                                    target_line0, target_char0):
    root = graph.indexer.root
    resolver = _bare_resolver(root)
    resolver.languages = ("java",)
    resolver.language_id = "java"
    resolver.cmd_name = "jdtls"
    resolver._ok = True
    resolver._ready = True
    resolver._opened = set()
    resolver._semantic_sites = 0
    resolver._semantic_hits = 0
    resolver._notify = lambda *_a, **_k: None
    resolver.proc = type("Proc", (), {"poll": lambda self: None})()
    resolver._definition = lambda *_a: [(
        (root / target_rel).as_uri(), target_line0, target_char0)]
    file_row = graph.indexer.conn.execute(
        "SELECT id FROM files WHERE path=?", (source_rel,)).fetchone()
    assert file_row is not None
    return resolver.refine_file(
        graph.indexer.conn, root, source_rel, file_row["id"])


def test_jdtls_method_call_cannot_promote_enclosing_class(tmp_path):
    """Uma definition inexata na classe não fabrica um alvo de método."""
    (tmp_path / "ArrayOps.java").write_text(
        "class ArrayOps {\n"
        "  static boolean sameType(Object a, Object b) { return true; }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "Check.java").write_text(
        "class Check { void run() { ArrayOps.sameType(null, null); } }\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        promoted = _refine_java_edge_to_definition(
            graph, "Check.java", "ArrayOps.java", 0, len("class "))
        edge = graph.indexer.conn.execute(
            "SELECT e.confidence, e.resolver, s.kind, s.name "
            "FROM edges e LEFT JOIN symbols s ON s.id=e.dst "
            "WHERE e.kind='calls' AND e.dst_name='ArrayOps.sameType'"
        ).fetchone()
        assert promoted == 0
        assert edge is not None
        assert (edge["resolver"], edge["confidence"]) == ("l0", "inferred")
        assert (edge["kind"], edge["name"]) == ("method", "sameType")
    finally:
        graph.close()


def test_jdtls_constructor_definition_can_promote_class(tmp_path):
    """O filtro de contêiner preserva construtor implícito resolvido à classe."""
    (tmp_path / "Widget.java").write_text(
        "class Widget {}\n", encoding="utf-8")
    (tmp_path / "App.java").write_text(
        "class App { void run() { new Widget(); } }\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        promoted = _refine_java_edge_to_definition(
            graph, "App.java", "Widget.java", 0, len("class "))
        edge = graph.indexer.conn.execute(
            "SELECT e.confidence, e.resolver, s.kind, s.name "
            "FROM edges e JOIN symbols s ON s.id=e.dst "
            "WHERE e.kind='calls' AND e.dst_name='Widget'"
        ).fetchone()
        assert promoted == 1
        assert edge is not None
        assert (edge["resolver"], edge["confidence"]) == ("l1", "certain")
        assert (edge["kind"], edge["name"]) == ("class", "Widget")
    finally:
        graph.close()


def test_jdtls_constructor_reference_can_promote_class(tmp_path):
    """`Widget::new` é construção mesmo sem o token `new` antes do tipo."""
    (tmp_path / "Widget.java").write_text(
        "class Widget {}\n", encoding="utf-8")
    (tmp_path / "App.java").write_text(
        "class App { java.util.function.Supplier<Widget> maker = Widget::new; }\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        promoted = _refine_java_edge_to_definition(
            graph, "App.java", "Widget.java", 0, len("class "))
        edge = graph.indexer.conn.execute(
            "SELECT e.confidence, e.resolver, s.kind, s.name "
            "FROM edges e JOIN symbols s ON s.id=e.dst "
            "WHERE e.kind='calls' AND e.dst_name='Widget'"
        ).fetchone()
        assert promoted == 1
        assert edge is not None
        assert (edge["resolver"], edge["confidence"]) == ("l1", "certain")
        assert (edge["kind"], edge["name"]) == ("class", "Widget")
    finally:
        graph.close()


def test_jdtls_method_reference_cannot_promote_class(tmp_path):
    """`Widget::label` não herda a exceção reservada a `::new`."""
    (tmp_path / "Widget.java").write_text(
        "class Widget { static String label() { return \"ok\"; } }\n",
        encoding="utf-8",
    )
    (tmp_path / "App.java").write_text(
        "class App { java.util.function.Supplier<String> maker = Widget::label; }\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        promoted = _refine_java_edge_to_definition(
            graph, "App.java", "Widget.java", 0, len("class "))
        edge = graph.indexer.conn.execute(
            "SELECT e.confidence, e.resolver, s.kind, s.name "
            "FROM edges e LEFT JOIN symbols s ON s.id=e.dst "
            "WHERE e.kind='calls' AND e.dst_name='Widget.label'"
        ).fetchone()
        assert promoted == 0
        assert edge is not None
        assert (edge["resolver"], edge["confidence"]) == ("l0", "inferred")
        assert (edge["kind"], edge["name"]) == ("method", "label")
    finally:
        graph.close()


def test_jdtls_empty_definition_promotes_unique_java_method_reference(tmp_path):
    """JDTLS vazio usa somente a referência explícita com alvo L0 único."""
    (tmp_path / "Widget.java").write_text(
        "class Widget { static String label() { return \"ok\"; } }\n",
        encoding="utf-8",
    )
    (tmp_path / "App.java").write_text(
        "class App { java.util.function.Supplier<String> maker = "
        "Widget::label; }\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        root = graph.indexer.root
        resolver = _bare_resolver(root)
        resolver.languages = ("java",)
        resolver.language_id = "java"
        resolver.cmd_name = "jdtls"
        resolver._ok = resolver._ready = True
        resolver._opened = set()
        resolver._semantic_sites = resolver._semantic_hits = 0
        resolver._notify = lambda *_a, **_k: None
        resolver.proc = type("Proc", (), {"poll": lambda self: None})()
        resolver._definition = lambda *_a: []
        file_id = graph.indexer.conn.execute(
            "SELECT id FROM files WHERE path='App.java'").fetchone()["id"]

        promoted = resolver.refine_file(
            graph.indexer.conn, root, "App.java", file_id)

        edge = graph.indexer.conn.execute(
            "SELECT e.confidence, e.resolver, s.fqn FROM edges e "
            "JOIN symbols s ON s.id=e.dst WHERE e.kind='calls'"
        ).fetchone()
        assert promoted == 1
        assert dict(edge) == {
            "confidence": "certain", "resolver": "l1",
            "fqn": "Widget.label",
        }
    finally:
        graph.close()


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
    notifications = []

    def request(_method, params):
        captured.update(params)
        return {}

    r._request = request
    r._notify = lambda *args: notifications.append(args)
    assert r._initialize() is True
    workspace = captured["capabilities"]["workspace"]
    assert workspace == {"configuration": True, "workspaceFolders": True}
    assert notifications == [
        ("initialized", {}),
        ("workspace/didChangeConfiguration", {
            "settings": {"java": {"autobuild": {"enabled": True}}},
        }),
    ]


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


def test_request_is_capped_by_outer_readiness_deadline(tmp_path, monkeypatch):
    r = _bare_resolver(tmp_path)
    r._seq = 0
    r.io_timeout = 30.0
    r._active_deadline = 0.25
    r.proc = type("Proc", (), {"poll": lambda self: None})()
    r._write = lambda *_a: None
    waits = []

    def read(timeout):
        waits.append(timeout)
        return {"jsonrpc": "2.0", "method": "$/progress"}

    r._read = read
    killed = []
    r._kill = lambda: killed.append(True)
    ticks = iter((0.0, 0.1, 0.3))
    monkeypatch.setattr(lsp_base.time, "monotonic", lambda: next(ticks))

    assert r._request("example", {}) is None
    assert len(waits) == 1 and abs(waits[0] - 0.15) < 1e-9
    assert killed == [True]


def test_warmup_uses_one_monotonic_deadline_and_caps_sleep(tmp_path,
                                                          monkeypatch):
    r = _bare_resolver(tmp_path)
    r.ready_timeout = 3.0
    r._ready = False
    r.proc = type("Proc", (), {"poll": lambda self: None})()
    r._lines = {"Main.java": ["service.run();"]}
    r._definition = lambda *_a: []
    sleeps = []
    ticks = iter((10.0, 10.0, 12.6, 13.0))
    monkeypatch.setattr(lsp_base.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(lsp_base.time, "sleep", sleeps.append)

    r._warmup("Main.java", [{"line": 1, "col": 0,
                              "dst_name": "service.run"}])

    assert r._ready and r._warmup_timed_out
    assert len(sleeps) == 1
    assert abs(sleeps[0] - 0.4) < 1e-9
    assert r._active_deadline is None


def test_warmup_aborts_before_request_when_process_exited(tmp_path):
    r = _bare_resolver(tmp_path)
    r.ready_timeout = 10.0
    r._ready = False
    r.proc = type("Proc", (), {"poll": lambda self: 1})()
    r._lines = {"Main.java": ["run();"]}
    r._definition = lambda *_a: (_ for _ in ()).throw(
        AssertionError("não deve consultar processo encerrado"))

    r._warmup("Main.java", [{"line": 1, "col": 0, "dst_name": "run"}])
    assert r._ready and r._warmup_timed_out


def test_close_uses_short_shutdown_budget(tmp_path):
    r = _bare_resolver(tmp_path)
    r.shutdown_timeout = 1.5
    requests = []
    notifications = []

    class Proc:
        def poll(self):
            return None

        def wait(self, timeout):
            assert 0 <= timeout <= 1.5
            return 0

    r.proc = Proc()
    r._request = lambda *args, **kwargs: requests.append((args, kwargs))
    r._notify = lambda *args: notifications.append(args)
    r.close()

    assert requests[0][0][:2] == ("shutdown", None)
    assert requests[0][1]["timeout"] == 1.5
    assert notifications == [("exit", None)]


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
    assert any("spawn failed" in warning for warning in stats["warnings"])
    assert stats["runs"][0]["errors"] == ["OSError: spawn failed"]


def test_jdtls_marks_workspace_interrupted_when_spawn_fails(tmp_path, monkeypatch):
    from codegraph.l1 import jdtls

    home = tmp_path / "jdtls"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "org.eclipse.equinox.launcher_1.0.0.jar").touch()
    (home / ("config_win" if os.name == "nt" else "config_linux")).mkdir()
    cache = tmp_path / "cache"
    monkeypatch.setenv("CODEGRAPH_JDTLS", str(home))
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(cache))
    monkeypatch.setattr(lsp_base.subprocess, "Popen",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
    try:
        jdtls.JdtlsResolver(tmp_path)
    except OSError:
        pass
    else:
        raise AssertionError("spawn deveria falhar")
    states = list((cache / "workspaces").glob("*/state.json"))
    assert len(states) == 1
    assert '"status": "running"' in states[0].read_text(encoding="utf-8")


def test_jdtls_chooses_newest_launcher_version(tmp_path):
    from codegraph.l1.jdtls import JdtlsResolver

    plugins = tmp_path / "plugins"
    plugins.mkdir()
    old = plugins / "org.eclipse.equinox.launcher_1.9.0.jar"
    new = plugins / "org.eclipse.equinox.launcher_1.10.0.jar"
    old.touch(); new.touch()
    assert JdtlsResolver._launcher_jar(tmp_path) == new
