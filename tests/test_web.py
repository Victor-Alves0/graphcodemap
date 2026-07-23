"""Extractors dedicados de HTML e CSS/SCSS.

Modelagem: o CSS DEFINE os seletores (símbolos); o HTML USA (referências) e
declara dependências de arquivo (`<script src>`, `<link href>`). O tier
genérico não dava nada disso: HTML rendia 0 símbolos e CSS rendia falso
positivo (`:hover` virava classe) — estes testes travam o comportamento certo.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

HTML = """\
<!DOCTYPE html>
<html><head>
  <link rel="stylesheet" href="styles/main.css">
  <link rel="icon" href="https://cdn.example.com/f.ico">
  <script src="js/app.js?v=2"></script>
</head>
<body>
  <div id="root" class="container main">
    <button id="go" class="btn">Go</button>
  </div>
</body></html>
"""

CSS = """\
.container { color: red; }
#root { margin: 0; }
.btn:hover { color: blue; }
@media (max-width: 600px) { .container { display: none; } }
@import "base.css";
"""

SCSS = """\
@use "buttons";
@mixin flexy($dir) { display: flex; }
@function double($n) { @return $n * 2; }
.card { color: red; }
"""


@pytest.fixture()
def cgweb(tmp_path):
    (tmp_path / "index.html").write_text(HTML, encoding="utf-8")
    (tmp_path / "main.css").write_text(CSS, encoding="utf-8")
    (tmp_path / "theme.scss").write_text(SCSS, encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    yield g
    g.close()


def _syms(cg, path):
    return {(r["kind"], r["name"]) for r in cg.indexer.conn.execute(
        "SELECT s.kind, s.name FROM symbols s JOIN files f ON s.file_id=f.id "
        "WHERE f.path=?", (path,))}


def _refs(cg, path, kind):
    return {r["dst_name"] for r in cg.indexer.conn.execute(
        "SELECT e.dst_name FROM edges e JOIN files f ON e.file_id=f.id "
        "WHERE f.path=? AND e.kind=?", (path, kind))}


# -- CSS: seletores são definições -------------------------------------------

def test_css_defines_class_and_id_selectors(cgweb):
    syms = _syms(cgweb, "main.css")
    assert ("css_class", "container") in syms
    assert ("css_id", "root") in syms            # id era PERDIDO no tier genérico
    assert ("css_class", "btn") in syms


def test_css_skips_pseudo_class_false_positive(cgweb):
    # `.btn:hover` define .btn — 'hover' NÃO é um seletor definido aqui
    assert ("css_class", "hover") not in _syms(cgweb, "main.css")


def test_css_dedupes_repeated_selector(cgweb):
    # .container aparece 2x (topo + @media) → um símbolo só
    n = cgweb.indexer.conn.execute(
        "SELECT COUNT(*) FROM symbols s JOIN files f ON s.file_id=f.id "
        "WHERE f.path='main.css' AND s.name='container'").fetchone()[0]
    assert n == 1


def test_css_import_is_recorded(cgweb):
    assert "base.css" in _refs(cgweb, "main.css", "imports")


# -- SCSS: mixin/function além dos seletores ---------------------------------

def test_scss_mixin_function_and_use(cgweb):
    syms = _syms(cgweb, "theme.scss")
    assert ("mixin", "flexy") in syms
    assert ("function", "double") in syms
    assert ("css_class", "card") in syms
    assert "buttons" in _refs(cgweb, "theme.scss", "imports")


# -- HTML: ids definem, classes usam, assets são dependências ----------------

def test_html_ids_become_symbols(cgweb):
    syms = _syms(cgweb, "index.html")
    assert ("html_id", "root") in syms
    assert ("html_id", "go") in syms


def test_html_classes_are_references_not_definitions(cgweb):
    refs = _refs(cgweb, "index.html", "references")
    assert {"container", "main", "btn"} <= refs
    # e NÃO viraram símbolos (quem define é o CSS)
    assert not {k for k, n in _syms(cgweb, "index.html") if k == "css_class"}


def test_html_local_assets_become_imports(cgweb):
    imports = _refs(cgweb, "index.html", "imports")
    assert "styles/main.css" in imports
    assert "js/app.js" in imports              # query string removida
    # externo (CDN) não vira dependência do repo
    assert not any("cdn.example.com" in i for i in imports)


def test_html_is_dedicated_not_generic(cgweb):
    # regressão do diagnóstico: no tier genérico, HTML rendia ZERO símbolos
    assert _syms(cgweb, "index.html")
