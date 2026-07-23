"""Ordenação de `find_symbol` (docs/DESIGN.md §2.4).

Motivação medida num app React real: `find_symbol("menu")` devolvia 10 de 10
resultados em classes CSS (`.menu-item`, `.title-menu`…) — o código com "menu"
no nome nem aparecia. Duas causas distintas, e as duas estão travadas aqui:

1. desempate DENTRO de um nível vinha arbitrário do SQLite (sem ORDER BY);
2. um símbolo camelCase (`changeModelMenuSubmenu`) só é alcançável no ÚLTIMO
   nível (substring), que nunca rodava porque os níveis anteriores já tinham
   enchido o limite — não era ordenação, era um nível que nem era consultado.

O que NÃO se quer é excluir marcação do resultado: buscar "menu" deve mostrar
`.menu` primeiro, porque é casamento exato.
"""

from __future__ import annotations

import pytest

from codegraph import CodeGraph
from codegraph.query import LOW_INFO_KINDS

# 12 classes casando "menu" contra 2 símbolos de código: sem piso, o código
# fica fora do limite de 10
CSS = "\n".join(f".menu-{i} {{ color: red; }}" for i in range(12)) + """
.menu { color: blue; }
"""

TSX = """\
export function openMenu() { return 1; }

export function changeModelMenuSubmenu() { return 2; }
"""


@pytest.fixture()
def cgrank(tmp_path):
    (tmp_path / "styles.css").write_text(CSS, encoding="utf-8")
    (tmp_path / "ui.tsx").write_text(TSX, encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    yield g
    g.close()


def _kinds(rows):
    return [r["kind"] for r in rows]


def test_markup_does_not_crowd_code_out(cgrank):
    rows, _ = cgrank.find_symbol("menu", limit=10)
    code = [r for r in rows if r["kind"] not in LOW_INFO_KINDS]
    assert len(code) >= 2, _kinds(rows)


def test_camelcase_symbol_is_reachable(cgrank):
    # só o nível de substring alcança isto; antes ele era starvado
    rows, _ = cgrank.find_symbol("menu", limit=10)
    assert any(r["name"] == "changeModelMenuSubmenu" for r in rows), _kinds(rows)


def test_exact_match_stays_first_even_being_css(cgrank):
    # o piso de código não pode virar exclusão de marcação: `.menu` é o
    # casamento exato e continua no topo
    rows, _ = cgrank.find_symbol("menu", limit=10)
    assert (rows[0]["kind"], rows[0]["name"]) == ("css_class", "menu")


def test_code_comes_before_fuzzy_markup(cgrank):
    rows, _ = cgrank.find_symbol("menu", limit=10)
    first_code = next(i for i, r in enumerate(rows) if r["kind"] not in LOW_INFO_KINDS)
    last_fuzzy = max(i for i, r in enumerate(rows)
                     if r["kind"] in LOW_INFO_KINDS and r["name"] != "menu")
    assert first_code < last_fuzzy, _kinds(rows)


def test_explicit_kind_filter_is_not_overridden(cgrank):
    # pedir kind=css_class é uma escolha do chamador: o piso não se aplica
    rows, _ = cgrank.find_symbol("menu", kind="css_class", limit=10)
    assert rows and all(r["kind"] == "css_class" for r in rows)


def test_pure_code_search_is_unaffected(cgrank):
    rows, _ = cgrank.find_symbol("openMenu", limit=10)
    assert rows[0]["name"] == "openMenu"
