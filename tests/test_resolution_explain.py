"""Transparência de resolução (Tier 1): toda aresta explica COMO foi resolvida.

O grafo separa resolução heurística (L0) de semântica (L1/LSP). Estes testes
travam: (1) o helper puro `explain` (rótulo + frase de motivo); (2) que as saídas
de query carregam `resolver` por aresta; (3) que o `render` mostra o rótulo e uma
legenda com o motivo; (4) que o `doctor` denuncia L1 indisponível para as
linguagens presentes no repo (degradação visível)."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph import explain, render


def _graph(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    return g


# ============================================================================
# A. Helper puro `explain`
# ============================================================================

def test_resolver_label_l0():
    assert explain.resolver_label("l0", "python") == "l0"


def test_resolver_label_l1_qualified():
    assert explain.resolver_label("l1", "typescript") == "l1/typescript"


def test_resolver_label_none_is_none():
    assert explain.resolver_label(None) == "none"


def test_reason_l1_names_the_engine():
    r = explain.reason("l1/typescript", "certain")
    assert "TypeScript language service" in r and "1 defini" in r


def test_reason_l1_python_is_jedi():
    assert "jedi" in explain.reason("l1/python", "certain")


def test_reason_inferred_is_l0_unique():
    r = explain.reason("l0", "inferred")
    assert "nico" in r  # "único" — alvo único


def test_reason_possible_flags_ambiguity():
    r = explain.reason("l0", "possible")
    assert "verificar" in r.lower()


def test_reason_unresolved():
    r = explain.reason("none", None, resolved=False)
    assert "não resolvido" in r or "externa" in r


def test_annotate_consumes_site_language():
    row = {"resolver": "l1", "site_language": "go", "confidence": "certain"}
    out = explain.annotate(row)
    assert out["resolver"] == "l1/go"
    assert "site_language" not in out


def test_annotate_tolerates_missing_columns():
    out = explain.annotate({"confidence": "possible"})
    assert out["resolver"] == "none"


# ============================================================================
# B. Registro L1 — missing_resolvers
# ============================================================================

def test_missing_resolvers_when_none_available():
    miss = explain_missing({"go", "rust", "python"}, available=False)
    langs = {l for m in miss for l in m["languages"]}
    assert {"go", "rust", "python"} <= langs
    assert all("server" in m for m in miss)


def test_missing_resolvers_empty_when_all_available():
    assert explain_missing({"go", "rust"}, available=True) == []


def test_missing_resolvers_ignores_absent_languages():
    # linguagem sem resolver ou fora do repo não entra
    miss = explain_missing({"markdown"}, available=False)
    assert miss == []


def explain_missing(langs, available):
    from codegraph.l1 import missing_resolvers
    return missing_resolvers(langs, is_available=lambda cls: available)


# ============================================================================
# C. Saídas de query carregam `resolver` por aresta
# ============================================================================

def test_references_rows_have_resolver(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "def helper():\n    return 1\n\n"
                "def caller():\n    return helper()\n",
    })
    _sym, rows, _env = g.query.references("helper")
    assert rows and all("resolver" in r for r in rows)
    assert all(r["resolver"] in ("l0", "l1/python") for r in rows)
    g.close()


def test_callers_rows_have_resolver(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "def helper():\n    return 1\n\n"
                "def caller():\n    return helper()\n",
    })
    _sym, rows, _env = g.query.callers("helper")
    assert rows and all("resolver" in r for r in rows)
    g.close()


def test_ego_rows_have_resolver(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "def helper():\n    return 1\n\n"
                "def caller():\n    return helper()\n",
    })
    data, _env = g.query.ego_graph("caller")
    assert all("resolver" in r for r in data["out"])
    g.close()


def test_impact_rows_have_resolver(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "def helper():\n    return 1\n\n"
                "def caller():\n    return helper()\n",
    })
    _sym, rows, _env = g.query.impact("helper")
    assert rows and all("resolver" in r for r in rows)
    g.close()


# ============================================================================
# D. Render mostra rótulo e legenda com o motivo
# ============================================================================

def test_render_refs_shows_resolver_label(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "def helper():\n    return 1\n\n"
                "def caller():\n    return helper()\n",
    })
    out = render.refs(*g.query.references("helper"))
    assert "l0" in out or "l1/python" in out
    g.close()


def test_render_refs_has_reason_legend(tmp_path):
    g = _graph(tmp_path, {
        "a.py": "def helper():\n    return 1\n\n"
                "def caller():\n    return helper()\n",
    })
    out = render.refs(*g.query.references("helper"))
    # a legenda "como ler" traduz cada rótulo·confiança presente numa frase
    assert "como ler" in out.lower()
    g.close()


# ============================================================================
# E. Doctor denuncia L1 indisponível para linguagens do repo
# ============================================================================

def test_doctor_reports_l1_missing_field(tmp_path):
    g = _graph(tmp_path, {"a.go": "package main\nfunc main() {}\n"})
    d = g.query.doctor()
    assert "l1_missing" in d
    assert isinstance(d["l1_missing"], list)
    g.close()


def test_render_doctor_warns_on_missing_l1(tmp_path, monkeypatch):
    # força gopls ausente e confirma que o doctor renderizado avisa sobre Go
    import codegraph.l1 as l1
    monkeypatch.setattr(l1, "missing_resolvers",
                        lambda langs, is_available=None, root=None: (
                            [{"languages": ["go"], "server": "gopls",
                              "env": "GOPLS_BIN"}]
                            if "go" in set(langs) else []))
    g = _graph(tmp_path, {"a.go": "package main\nfunc main() {}\n"})
    out = render.doctor(g.query.doctor())
    assert "gopls" in out and "go" in out.lower()
    g.close()
