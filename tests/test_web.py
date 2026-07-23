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


# -- vínculo cross-language: quem usa a classe que o CSS define ---------------
#
# Medido no Flowen (repo React real) ANTES desta religação: 597 símbolos web,
# 597 ilhas, 0 arestas resolvidas — o extractor via as definições e nenhum uso,
# porque num app React quem usa classe é `className=` no TSX, não HTML.

TSX = """\
import { clsx } from "clsx";

export function Card({ on, n }) {
  return <div className="container main">
    <b className={`btn ${on ? "is-on" : ""} tail`} />
    <u className={`col-${n} fixo`} />
    <i className={clsx("card", on && "is-on")} />
    <s className={styles.container} />
    <p class="btn" />
  </div>;
}
"""


@pytest.fixture()
def cgapp(tmp_path):
    (tmp_path / "index.html").write_text(HTML, encoding="utf-8")
    (tmp_path / "main.css").write_text(CSS, encoding="utf-8")
    (tmp_path / "theme.scss").write_text(SCSS, encoding="utf-8")
    (tmp_path / "Card.tsx").write_text(TSX, encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    yield g
    g.close()


def _resolved(cg, path, kind="references"):
    """dst_name -> kind do símbolo alvo, só das arestas RESOLVIDAS."""
    return {r["dst_name"]: r["k"] for r in cg.indexer.conn.execute(
        "SELECT e.dst_name, s.kind k FROM edges e JOIN files f ON e.file_id=f.id "
        "JOIN symbols s ON e.dst=s.id WHERE f.path=? AND e.kind=?", (path, kind))}


def test_tsx_classname_resolves_to_css_class(cgapp):
    got = _resolved(cgapp, "Card.tsx")
    assert got.get("container") == "css_class"      # string literal simples
    assert got.get("btn") == "css_class"            # estático dentro de template
    assert got.get("card") == "css_class"           # literal dentro de clsx(...)


def test_html_class_resolves_to_css_class(cgapp):
    # a modelagem original (HTML usa) agora fecha o ciclo de verdade
    assert _resolved(cgapp, "index.html").get("container") == "css_class"


def test_css_id_resolves_to_the_html_element(cgapp):
    # `#root` no CSS estiliza o `id="root"` do HTML — sem isto ficava ilhado
    assert _resolved(cgapp, "main.css").get("root") == "html_id"


def test_interpolated_prefix_is_not_a_class_name(cgapp):
    # `col-${n}` é PREFIXO, não classe: registrar 'col-' seria inventar nome
    names = _refs(cgapp, "Card.tsx", "references")
    assert "col-" not in names
    assert "fixo" in names                          # o vizinho completo entra


def test_css_modules_expression_yields_no_reference(cgapp):
    # `className={styles.container}` não tem literal: nada a afirmar.
    # 'container' entra pelos OUTROS usos; o que se checa é que não há ref
    # inventada a partir do acesso a propriedade (senão 'styles' apareceria).
    assert "styles" not in _refs(cgapp, "Card.tsx", "references")


def test_classname_does_not_swallow_the_call(cgapp):
    # a expressão do atributo continua sendo visitada: clsx(...) segue sendo
    # uma chamada (qualificada pelo import, como qualquer outra)
    calls = _refs(cgapp, "Card.tsx", "calls")
    assert any(c.rsplit(".", 1)[-1] == "clsx" for c in calls), calls


def test_reference_never_binds_to_a_homonym_function(tmp_path):
    # sem filtro de língua, o kind é a única proteção: uma função `btn` não
    # pode virar alvo de um `className="btn"`
    (tmp_path / "a.tsx").write_text(
        'export const A = () => <i className="btn" />;\n', encoding="utf-8")
    (tmp_path / "b.ts").write_text("export function btn() { return 1; }\n",
                                   encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    rows = list(g.indexer.conn.execute(
        "SELECT s.kind FROM edges e JOIN symbols s ON e.dst=s.id "
        "WHERE e.kind='references' AND e.dst_name='btn'"))
    assert [r["kind"] for r in rows] == []          # nenhum css_class existe
    g.close()


def test_escaped_selector_matches_the_raw_attribute(tmp_path):
    """`.mt-1\\.5` no CSS é a classe `mt-1.5` no atributo (padrão do Tailwind).

    Trava duas coisas de uma vez: desescapar o seletor, e o ramo `references`
    do resolver vir ANTES do ramo de guess qualificado — com o ponto no nome,
    aquele ramo procuraria por um símbolo chamado "5" e a classe seria
    permanentemente inalcançável.
    """
    (tmp_path / "a.css").write_text(
        ".mt-1\\.5 { margin: 4px; }\n.hover\\:bg { color: red; }\n",
        encoding="utf-8")
    (tmp_path / "a.tsx").write_text(
        'export const A = () => <i className="mt-1.5 hover:bg" />;\n',
        encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    got = _resolved(g, "a.tsx")
    assert got.get("mt-1.5") == "css_class"
    assert got.get("hover:bg") == "css_class"
    g.close()


# -- refs de asset agora têm alvo: o símbolo de ARQUIVO -----------------------
#
# Era o limite declarado do commit anterior: `<script src>` / `@import` ficavam
# dangling para sempre porque o guess é um CAMINHO e não havia nada no grafo
# para apontar. Um símbolo `file` por arquivo fecha isso.

@pytest.fixture()
def cgassets(tmp_path):
    (tmp_path / "styles").mkdir()
    (tmp_path / "js").mkdir()
    (tmp_path / "index.html").write_text(
        '<html><head><link rel="stylesheet" href="styles/main.css">\n'
        '<script src="js/app.js?v=2"></script>\n'
        '<script src="https://cdn.example.com/x.js"></script></head>\n'
        '<body><div id="root"></div></body></html>\n', encoding="utf-8")
    (tmp_path / "styles" / "main.css").write_text(
        '@import "base.css";\n.x { color: red; }\n', encoding="utf-8")
    # @use é sintaxe SCSS: o grammar de CSS não a reconhece
    (tmp_path / "styles" / "theme.scss").write_text(
        '@use "buttons";\n.w { color: gray; }\n', encoding="utf-8")
    (tmp_path / "styles" / "base.css").write_text(".y { color: blue; }\n",
                                                  encoding="utf-8")
    (tmp_path / "styles" / "_buttons.scss").write_text(".z { color: teal; }\n",
                                                       encoding="utf-8")
    (tmp_path / "js" / "app.js").write_text("export function go() { return 1; }\n",
                                            encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    yield g
    g.close()


def _import_targets(cg, path):
    """dst_name -> caminho do arquivo alvo, das arestas `imports` resolvidas."""
    return {r["dst_name"]: r["p"] for r in cg.indexer.conn.execute(
        "SELECT e.dst_name, fd.path p FROM edges e JOIN files f ON e.file_id=f.id "
        "JOIN symbols d ON e.dst=d.id JOIN files fd ON d.file_id=fd.id "
        "WHERE f.path=? AND e.kind='imports' AND d.kind='file'", (path,))}


def test_script_and_link_resolve_to_the_file(cgassets):
    got = _import_targets(cgassets, "index.html")
    assert got["styles/main.css"] == "styles/main.css"
    assert got["js/app.js"] == "js/app.js"        # query string já removida


def test_css_import_resolves_relative_to_the_importing_file(cgassets):
    # `@import "base.css"` dentro de styles/main.css é styles/base.css,
    # não base.css na raiz
    assert _import_targets(cgassets, "styles/main.css")["base.css"] == "styles/base.css"


def test_sass_partial_is_found_by_its_underscore_name(cgassets):
    # `@use "buttons"` → _buttons.scss (convenção do Sass)
    assert _import_targets(cgassets, "styles/theme.scss")["buttons"] == "styles/_buttons.scss"


def test_external_url_never_resolves(cgassets):
    # CDN não é arquivo do repo: continua sem alvo, e é o correto
    assert not any("cdn.example.com" in n
                   for n in _refs(cgassets, "index.html", "imports"))


def test_relative_asset_import_from_tsx_resolves(tmp_path):
    # `import "./styles.css"` é o padrão de todo app React. Virava fqn
    # pontilhado (`src.styles.css`), destruindo o caminho — e nunca resolvia.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "styles.css").write_text(".x { color: red; }\n",
                                                 encoding="utf-8")
    (tmp_path / "src" / "App.tsx").write_text(
        'import "./styles.css";\nexport const A = () => <i className="x" />;\n',
        encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    assert _import_targets(g, "src/App.tsx")["./styles.css"] == "src/styles.css"
    g.close()


def test_bare_specifier_never_binds_to_a_homonym_file(tmp_path):
    # `import "constants"` resolve para node_modules, NÃO para o irmão
    # constants.ts — resolver por caminho inventava aresta local para toda
    # dependência externa homônima de um arquivo do repo
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "constants.ts").write_text("export const X = 1;\n",
                                                   encoding="utf-8")
    (tmp_path / "src" / "a.ts").write_text('import "constants";\n',
                                           encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    assert _import_targets(g, "src/a.ts") == {}
    g.close()


def test_root_init_file_symbol_has_a_name(tmp_path):
    # `__init__.py` na raiz tem module fqn vazio: sem fallback o grafo ganhava
    # um símbolo de nome e fqn vazios, inalcançável e sujo no FTS
    (tmp_path / "__init__.py").write_text("def a():\n    return 1\n",
                                          encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    row = g.indexer.conn.execute(
        "SELECT name, fqn FROM symbols WHERE kind='file'").fetchone()
    assert row["name"] and row["fqn"]
    g.close()


def test_file_symbol_is_not_reported_as_a_code_change(tmp_path):
    # o host quer saber que símbolo DECLARADO mudou; "o arquivo existe" só
    # inflaria o diff de toda integração
    (tmp_path / "u.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    g = CodeGraph(tmp_path)
    ch = g.index()["changes"]
    assert ch["added"] == ["u.a"]
    (tmp_path / "u.py").unlink()
    assert g.index()["changes"]["removed"] == ["u.a"]
    g.close()


def test_unused_css_class_is_detectable_as_dead(tmp_path):
    """A ilha que SOBRA vira sinal: classe definida e nunca usada.

    Capacidade nova — antes toda classe era ilha, então "sem uso" não
    distinguia nada. No Flowen isto apontou 173 de 595 classes sem uso.
    """
    (tmp_path / "a.css").write_text(".viva { color: red; }\n"
                                    ".morta { color: blue; }\n", encoding="utf-8")
    (tmp_path / "a.tsx").write_text(
        'export const A = () => <i className="viva" />;\n', encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    dead = {r["name"] for r in g.indexer.conn.execute(
        "SELECT name FROM symbols WHERE kind='css_class' "
        "AND NOT EXISTS(SELECT 1 FROM edges e WHERE e.dst=symbols.id)")}
    assert dead == {"morta"}
    g.close()
