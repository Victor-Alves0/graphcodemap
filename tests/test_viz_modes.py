"""Visualização como ferramenta de investigação (Prioridade 6).

Modos SEMEADOS (vizinhança/callers/callees/impacto) em vez do hairball do repo
inteiro, o modo domínios, e os filtros ortogonais (confiança, linguagem, arquivos
alterados no Git). Trava os DADOS de cada modo/filtro e o HTML autocontido."""

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.viz import git_changed_files, render_html

PKG = {
    "core.py": (
        "def base():\n    return 1\n\n"
        "def mid():\n    return base()\n\n"
        "def top():\n    return mid()\n"
    ),
    "helpers.py": "from core import base\n\ndef helper():\n    return base()\n",
    "app.ts": "export function widget() {\n  return 1;\n}\n",
}


def _graph(tmp_path, files=PKG, git=False):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    if git and shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index()
    return cg


def _labels(data):
    return {n["label"] for n in data["nodes"]}


# ============================================================================
# A. Modos semeados por um símbolo
# ============================================================================

def test_neighborhood_centers_on_symbol(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("neighborhood", symbol="mid")
    labs = _labels(data)
    assert "core.mid" in labs and "core.base" in labs and "core.top" in labs
    assert data["directed"] is True
    seed = [n for n in data["nodes"] if n.get("seed")]
    assert len(seed) == 1 and seed[0]["label"] == "core.mid"
    g.close()


def test_callees_are_outgoing(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("callees", symbol="top", depth=3)
    # top → mid → base
    assert {"core.top", "core.mid", "core.base"} <= _labels(data)
    assert "helpers.helper" not in _labels(data)   # helper não é chamado por top
    g.close()


def test_callers_are_incoming(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("callers", symbol="base", depth=1)
    # quem chama base direto: mid e helper
    assert {"core.base", "core.mid", "helpers.helper"} <= _labels(data)
    assert "core.top" not in _labels(data)         # top chama base só via mid
    g.close()


def test_impact_is_reverse_reachability(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("impact", symbol="base", depth=3)
    labs = _labels(data)
    assert {"core.mid", "core.top", "helpers.helper"} <= labs
    g.close()


def test_callees_of_leaf_is_just_seed(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("callees", symbol="base")
    assert _labels(data) == {"core.base"} and data["links"] == []
    g.close()


def test_seeded_links_are_directed_with_confidence(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("callers", symbol="base", depth=1)
    assert data["links"]
    for l in data["links"]:
        assert "confidence" in l and l["kind"] == "calls"


# ============================================================================
# B. Filtro por confiança
# ============================================================================

def test_min_confidence_filters_links(tmp_path):
    g = _graph(tmp_path)
    # L0: base←mid é 'inferred'; exigir 'certain' zera as arestas
    all_links = g.visualize("callers", symbol="base", depth=2)[0]["links"]
    certain = g.visualize("callers", symbol="base", depth=2,
                          min_confidence="certain")[0]["links"]
    assert all_links and certain == []


def test_min_confidence_inferred_keeps(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("callers", symbol="base", depth=1,
                          min_confidence="inferred")
    assert data["links"]
    g.close()


# ============================================================================
# C. Filtro por linguagem
# ============================================================================

def test_language_filter_restricts_nodes(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("symbol", language="python")
    assert data["nodes"]
    assert all(n["language"] == "python" for n in data["nodes"])
    assert "widget" not in " ".join(_labels(data))
    g.close()


def test_file_mode_language_filter(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("file", language="typescript")
    assert all(n["language"] == "typescript" for n in data["nodes"])
    g.close()


def test_languages_listed_in_payload(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("file")
    assert {"python", "typescript"} <= set(data["languages"])
    g.close()


# ============================================================================
# D. Modo domínios
# ============================================================================

def test_domains_mode_nodes_are_communities(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("domains")
    assert data["mode"] == "domains" and data["directed"] is True
    for n in data["nodes"]:
        assert n["label"].startswith("dom ")
    g.close()


# ============================================================================
# E. Back-compat: level legado
# ============================================================================

def test_level_file_still_works(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize(level="file")
    assert data["level"] == "file" and data["nodes"]
    g.close()


def test_modules_is_alias_of_file(tmp_path):
    g = _graph(tmp_path)
    a, _ = g.visualize("modules")
    b, _ = g.visualize("file")
    assert a["mode"] == b["mode"] == "file"
    g.close()


def test_top_limits_nodes(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("symbol", top=2)
    assert len(data["nodes"]) <= 2
    g.close()


# ============================================================================
# F. Filtro por arquivos alterados no Git
# ============================================================================

@pytest.mark.skipif(not shutil.which("git"), reason="git ausente")
def test_git_changed_files_detects_worktree(tmp_path):
    g = _graph(tmp_path, git=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                    "commit", "-qm", "x"], cwd=tmp_path)
    (tmp_path / "core.py").write_text(
        "def base():\n    return 999\n", encoding="utf-8")
    changed = git_changed_files(tmp_path)
    assert "core.py" in changed
    g.close()


@pytest.mark.skipif(not shutil.which("git"), reason="git ausente")
def test_git_flag_marks_changed_nodes(tmp_path):
    g = _graph(tmp_path, git=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                    "commit", "-qm", "x"], cwd=tmp_path)
    (tmp_path / "core.py").write_text(
        "def base():\n    return 999\n\ndef mid():\n    return base()\n",
        encoding="utf-8")
    data, _ = g.visualize("file", git=True)
    changed = {n["label"] for n in data["nodes"] if n["changed"]}
    assert "core.py" in changed
    assert data["filters"]["changed"] >= 1
    g.close()


def test_changed_paths_seed_impact(tmp_path):
    g = _graph(tmp_path)
    # sem git: passa os caminhos explicitamente
    data, _ = g.visualize("impact", changed="core.py")
    assert "helpers.helper" in _labels(data)     # helper depende de core
    g.close()


def test_changed_marks_file_node(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("file", changed="core.py,helpers.py")
    marked = {n["label"] for n in data["nodes"] if n["changed"]}
    assert {"core.py", "helpers.py"} <= marked
    g.close()


# ============================================================================
# G. HTML autocontido e investigativo
# ============================================================================

def test_html_self_contained_with_toggles(tmp_path):
    g = _graph(tmp_path)
    data, _ = g.visualize("neighborhood", symbol="base")
    html = render_html(data)
    assert html.startswith("<!doctype html>")
    assert "http://" not in html and "https://" not in html
    assert "src=" not in html
    assert "const DATA =" in html
    # elementos investigativos: toggles de confiança/linguagem e legenda
    assert "confbox" in html and "langbox" in html
    assert "arrow(" in html          # setas direcionais
    g.close()


def test_html_encodes_seed_and_mode(tmp_path):
    g = _graph(tmp_path)
    html = render_html(g.visualize("callers", symbol="base")[0])
    assert "modo callers" in html and "core.base" in html
    g.close()
