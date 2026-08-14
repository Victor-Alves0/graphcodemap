"""Tier 2 da resolução L1: detecção de root de projeto (monorepo) + decoupling.

Um language server precisa ser aberto na raiz do SUBPROJETO (go.mod, Cargo.toml,
pom.xml…), não na raiz do repo. Estes testes travam: (1) `roots` puro (detecção e
agrupamento); (2) que todo resolver declara `root_markers` como tupla; (3) que o
`lsp_base` desacopla repo_root (paths) de project_root (rootUri) sem spawnar um
servidor de verdade; (4) que o jedi aceita a nova assinatura."""

from __future__ import annotations

import textwrap

import pytest

from codegraph.l1 import roots


# ============================================================================
# A. roots.detect_project_root / group_by_root (puro)
# ============================================================================

def test_no_markers_returns_repo_root(tmp_path):
    assert roots.detect_project_root("a/b/x.go", tmp_path, ()) == tmp_path.resolve()


def test_marker_at_repo_root(tmp_path):
    (tmp_path / "go.mod").write_text("module m\n")
    assert roots.detect_project_root("x.go", tmp_path, ("go.mod",)) == tmp_path.resolve()


def test_nested_project_root_detected(tmp_path):
    svc = tmp_path / "services" / "api"
    svc.mkdir(parents=True)
    (svc / "go.mod").write_text("module api\n")
    got = roots.detect_project_root("services/api/main.go", tmp_path, ("go.mod",))
    assert got == svc.resolve()


def test_finds_nearest_marker_not_outermost(tmp_path):
    outer = tmp_path / "a"
    inner = outer / "b" / "c"
    inner.mkdir(parents=True)
    (outer / "Cargo.toml").write_text("[package]\n")
    (inner / "Cargo.toml").write_text("[package]\n")
    got = roots.detect_project_root("a/b/c/lib.rs", tmp_path, ("Cargo.toml",))
    assert got == inner.resolve()   # o crate mais próximo, não o de cima


def test_file_without_marker_falls_back_to_repo_root(tmp_path):
    (tmp_path / "x").mkdir()
    got = roots.detect_project_root("x/orphan.go", tmp_path, ("go.mod",))
    assert got == tmp_path.resolve()


def test_glob_marker_matches(tmp_path):
    proj = tmp_path / "App"
    proj.mkdir()
    (proj / "App.csproj").write_text("<Project/>\n")
    got = roots.detect_project_root("App/Program.cs", tmp_path, ("*.csproj",))
    assert got == proj.resolve()


def test_does_not_walk_above_repo_root(tmp_path):
    # marcador ACIMA do repo não deve ser considerado
    (tmp_path.parent / "go.mod").write_text("module outer\n")
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    got = roots.detect_project_root("pkg/x.go", repo, ("go.mod",))
    assert got == repo.resolve()


def test_relative_path_escape_cannot_select_external_marker(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pom.xml").write_text("<project/>\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    got = roots.detect_project_root("../outside/Main.java", repo, ("pom.xml",))
    assert got == repo.resolve()


def test_group_by_root_splits_monorepo(tmp_path):
    a = tmp_path / "svc-a"
    b = tmp_path / "svc-b"
    a.mkdir(); b.mkdir()
    (a / "go.mod").write_text("module a\n")
    (b / "go.mod").write_text("module b\n")
    groups = roots.group_by_root(
        ["svc-a/main.go", "svc-a/util.go", "svc-b/main.go"],
        tmp_path, ("go.mod",))
    assert set(groups) == {a.resolve(), b.resolve()}
    assert sorted(groups[a.resolve()]) == ["svc-a/main.go", "svc-a/util.go"]


def test_group_by_root_single_group_without_markers(tmp_path):
    groups = roots.group_by_root(["a.go", "b/c.go"], tmp_path, ())
    assert list(groups) == [tmp_path.resolve()]
    assert len(groups[tmp_path.resolve()]) == 2


# ============================================================================
# B. Contrato: todo resolver declara root_markers como tupla
# ============================================================================

def test_all_resolvers_declare_tuple_root_markers():
    from codegraph.l1 import all_resolvers
    for cls in all_resolvers():
        markers = getattr(cls, "root_markers", None)
        assert isinstance(markers, tuple), f"{cls.__name__}.root_markers"


def test_ts_has_empty_markers_stays_repo_root():
    # ts_service relativiza à raiz de spawn → TS fica sempre na raiz do repo
    from codegraph.l1.tsjs_ls import TsLsResolver
    assert TsLsResolver.root_markers == ()


# ============================================================================
# C. lsp_base desacopla repo_root (paths) de project_root (rootUri)
# ============================================================================

class _FakeProc:
    def __init__(self):
        self.stdin = None
        self.stdout = None

    def poll(self):
        return None


def test_lsp_base_decouples_project_root(tmp_path, monkeypatch):
    from codegraph.l1 import lsp_base

    monkeypatch.setattr(lsp_base.subprocess, "Popen",
                        lambda *a, **k: _FakeProc())
    monkeypatch.setattr(lsp_base.LspResolver, "_initialize", lambda self: True)

    class _FakeThread:
        def __init__(self, *a, **k): pass
        def start(self): pass

    monkeypatch.setattr(lsp_base.threading, "Thread",
                        lambda *a, **k: _FakeThread())

    class _Dummy(lsp_base.LspResolver):
        cmd_name = "dummy"

    sub = tmp_path / "services" / "api"
    sub.mkdir(parents=True)
    r = _Dummy(tmp_path, project_root=sub)
    assert r.root == tmp_path.resolve()          # paths repo-relativos
    assert r.project_root == sub.resolve()        # rootUri do LSP
    # sem project_root → cai na raiz do repo (comportamento de sempre)
    r2 = _Dummy(tmp_path)
    assert r2.project_root == tmp_path.resolve()


def test_lsp_base_definition_is_memoized(tmp_path, monkeypatch):
    from codegraph.l1 import lsp_base

    monkeypatch.setattr(lsp_base.subprocess, "Popen",
                        lambda *a, **k: _FakeProc())
    monkeypatch.setattr(lsp_base.LspResolver, "_initialize", lambda self: True)

    class _FakeThread:
        def __init__(self, *a, **k): pass
        def start(self): pass

    monkeypatch.setattr(lsp_base.threading, "Thread",
                        lambda *a, **k: _FakeThread())

    calls = {"n": 0}

    def fake_request(self, method, params, timeout_msgs=2000):
        calls["n"] += 1
        return None  # sem definição

    monkeypatch.setattr(lsp_base.LspResolver, "_request", fake_request)

    class _Dummy(lsp_base.LspResolver):
        cmd_name = "dummy"

    r = _Dummy(tmp_path)
    r._definition("a.go", 10, 4)
    r._definition("a.go", 10, 4)   # mesma posição → cache, sem novo _request
    assert calls["n"] == 1


# ============================================================================
# D. jedi aceita a nova assinatura (project_root)
# ============================================================================

@pytest.mark.skipif(
    __import__("importlib").util.find_spec("jedi") is None,
    reason="jedi não instalado")
def test_jedi_accepts_project_root(tmp_path):
    from codegraph.l1.python_jedi import JediResolver
    sub = tmp_path / "pkg"
    sub.mkdir()
    r = JediResolver(tmp_path, project_root=sub)
    assert r.root == tmp_path
    assert JediResolver.root_markers  # declara marcadores Python


# ============================================================================
# E. refine reporta as raízes usadas
# ============================================================================

def test_refine_stats_reports_roots(tmp_path):
    from codegraph import CodeGraph
    from codegraph import l1

    (tmp_path / "a.py").write_text(textwrap.dedent(
        "def helper():\n    return 1\n\ndef caller():\n    return helper()\n"))
    g = CodeGraph(tmp_path)
    g.index()
    stats = l1.refine(g.indexer)
    assert "roots" in stats and isinstance(stats["roots"], int)
    g.close()
