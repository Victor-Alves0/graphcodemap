"""Export de visualização: dados do grafo + HTML autocontido."""

from __future__ import annotations


def test_visualize_file_level(cg):
    data, env = cg.visualize(level="file")
    assert data["level"] == "file"
    assert data["nodes"]
    # nós carregam rótulo (caminho) e tamanho
    n = data["nodes"][0]
    assert "label" in n and "weight" in n
    # há pelo menos uma aresta inter-arquivo (routes importa/chama auth)
    assert data["links"]


def test_visualize_symbol_level(cg):
    data, env = cg.visualize(level="symbol", top=50)
    assert data["level"] == "symbol"
    labels = {n["label"] for n in data["nodes"]}
    assert any("TokenService" in l for l in labels)


def test_visualize_scope_filters(cg):
    data, _ = cg.visualize(level="file", scope="app")
    assert all(n["label"].startswith("app/") for n in data["nodes"])


def test_render_html_is_self_contained(cg):
    from codegraph.viz import render_html

    data, _ = cg.visualize(level="file")
    html = render_html(data)
    assert html.startswith("<!doctype html>")
    # sem recursos externos (offline de verdade)
    assert "http://" not in html and "https://" not in html
    assert "src=" not in html  # nenhum script/img externo
    # os dados foram embutidos
    assert "const DATA =" in html


def test_visualize_top_limits_nodes(cg):
    data, _ = cg.visualize(level="symbol", top=3)
    assert len(data["nodes"]) <= 3
