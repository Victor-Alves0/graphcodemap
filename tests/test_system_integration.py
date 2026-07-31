"""Integração do sistema como um todo: várias linguagens no MESMO índice, o grafo
Terraform ponta a ponta, transparência de resolução em todas as consultas,
frescor (read-repair) e consistência de stats/doctor.

Diferente das baterias por linguagem (extractor isolado), aqui o índice real é
montado com `CodeGraph`, exercitando indexação + resolução + query + render
juntos — onde os bugs de integração aparecem."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph, render


def _graph(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    return g


MULTI = {
    "svc.py": "def helper():\n    return 1\n\ndef run():\n    return helper()\n",
    "api.go": "package api\ntype S struct { X int }\nfunc (s S) Do() { helper() }\n",
    "main.tf": 'variable "region" {}\n'
               'resource "aws_instance" "web" {\n  region = var.region\n}\n',
    "app.tsx": "export function Card() {\n  return <div className='card'/>;\n}\n",
    "app.css": ".card { color: red }\n",
    "readme.md": "# Título\n## Seção\n",
}


# ============================================================================
# A. Índice multi-linguagem coeso
# ============================================================================

def test_multi_language_index_covers_all(tmp_path):
    g = _graph(tmp_path, MULTI)
    langs = set(g.stats()["by_language"])
    assert {"python", "go", "terraform", "tsx", "css", "markdown"} <= langs
    g.close()


def test_stats_are_consistent(tmp_path):
    g = _graph(tmp_path, MULTI)
    s = g.stats()
    assert s["edges"] == s["edges_resolved"] + s["edges_dangling"]
    assert s["symbols"] > 0 and s["files"] == len(MULTI)
    g.close()


def test_find_symbol_across_languages(tmp_path):
    g = _graph(tmp_path, MULTI)
    for q in ("helper", "aws_instance.web", "Card", "region"):
        rows, _ = g.find_symbol(q)
        assert rows, f"find_symbol('{q}') não achou nada num índice multi-lang"
    g.close()


def test_overview_runs_on_multi_language(tmp_path):
    g = _graph(tmp_path, MULTI)
    entries, _ = g.overview()
    assert isinstance(entries, list)
    g.close()


# ============================================================================
# B. Terraform ponta a ponta no grafo
# ============================================================================

def test_terraform_dependency_resolves_and_is_queryable(tmp_path):
    g = _graph(tmp_path, {
        "main.tf": 'variable "region" {}\n'
                   'resource "aws_instance" "a" {\n  region = var.region\n}\n'
                   'resource "aws_instance" "b" {\n  region = var.region\n}\n',
    })
    sym, rows, _ = g.query.references("var.region")
    assert len({r["src_fqn"] for r in rows}) == 2      # a e b dependem da var
    assert all(r["kind"] == "references" for r in rows)
    g.close()


def test_terraform_impact_follows_references(tmp_path):
    g = _graph(tmp_path, {
        "main.tf": 'variable "region" {}\n'
                   'resource "aws_instance" "web" {\n  region = var.region\n}\n',
    })
    _sym, rows, _ = g.query.impact("var.region")
    assert any(r["fqn"].endswith("aws_instance.web") for r in rows)
    g.close()


# ============================================================================
# C. Transparência de resolução em TODAS as consultas de aresta
# ============================================================================

def test_all_edge_queries_carry_resolver(tmp_path):
    g = _graph(tmp_path, MULTI)
    _s, refs, _ = g.query.references("helper")
    _s2, callers, _ = g.query.callers("helper")
    ego, _ = g.query.ego_graph("run")
    _s3, imp, _ = g.query.impact("helper")
    for rows, label in ((refs, "references"), (callers, "callers"),
                        (ego["out"], "ego.out"), (imp, "impact")):
        assert all("resolver" in r for r in rows), f"{label}: sem resolver"
        assert all("confidence" in r for r in rows), f"{label}: sem confidence"
    g.close()


def test_render_has_legend_across_methods(tmp_path):
    g = _graph(tmp_path, MULTI)
    refs_out = render.refs(*g.query.references("helper"))
    callers_out = render.calls(*g.query.callers("helper"), "callers de", "in")
    assert "como ler" in refs_out.lower()
    assert "como ler" in callers_out.lower()
    g.close()


# ============================================================================
# D. Frescor / read-repair (garantia anti-staleness)
# ============================================================================

def test_edit_on_disk_triggers_reindex(tmp_path):
    g = _graph(tmp_path, {"a.py": "def alpha():\n    return 1\n"})
    rows, _ = g.find_symbol("alpha")
    assert rows
    # renomeia no disco; a próxima query deve reindexar e refletir
    (tmp_path / "a.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    rows2, env = g.find_symbol("beta")
    assert rows2, "read-repair não reindexou o arquivo alterado"
    assert any("freshness" in w for w in env.warnings)
    g.close()


def test_delete_on_disk_removes_from_index(tmp_path):
    g = _graph(tmp_path, {"a.py": "def gone():\n    return 1\n",
                          "b.py": "def stay():\n    return 2\n"})
    (tmp_path / "a.py").unlink()
    rows, _ = g.find_symbol("gone")
    assert not rows, "símbolo de arquivo deletado ainda aparece"
    assert g.find_symbol("stay")[0]
    g.close()


# ============================================================================
# E. Doctor / diagnóstico
# ============================================================================

def test_doctor_reports_languages_and_l1_missing(tmp_path):
    g = _graph(tmp_path, MULTI)
    d = g.query.doctor()
    assert "by_language" in d and "l1_missing" in d
    assert isinstance(d["l1_missing"], list)
    assert 0 <= d["certain_pct"] <= 100
    g.close()


def test_doctor_render_smoke(tmp_path):
    g = _graph(tmp_path, MULTI)
    out = render.doctor(g.query.doctor())
    assert "saúde do índice" in out
    g.close()


# ============================================================================
# F. Reindexação idempotente (re-index sem mudança = mesmo grafo)
# ============================================================================

def test_reindex_without_changes_is_stable(tmp_path):
    g = _graph(tmp_path, MULTI)
    s1 = g.stats()
    g.index()  # de novo, sem mudança no disco
    s2 = g.stats()
    assert (s1["symbols"], s1["edges"]) == (s2["symbols"], s2["edges"])
    g.close()


def test_force_reindex_matches_incremental(tmp_path):
    g = _graph(tmp_path, MULTI)
    s1 = g.stats()
    g.index(force=True)
    s2 = g.stats()
    assert (s1["symbols"], s1["edges"]) == (s2["symbols"], s2["edges"])
    g.close()
