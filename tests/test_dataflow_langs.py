"""Dataflow multi-linguagem: fluxo param→call→return em cada linguagem dedicada.

Valida ponta a ponta (indexação real + query.data_flow) que a extração de
fatos dirigida por config funciona para as 17 linguagens suportadas.
"""

from __future__ import annotations

import pytest

from codegraph import CodeGraph

# cada caso: (arquivo com stem ÚNICO, fqn da função, nome do primeiro parâmetro)
CASES = {
    "fjava.java": ("class C { String m(String p) { String x = p; sink(x); "
                   "return x; } }", "fjava.C.m", "p"),
    "fcs.cs": ("class C { string M(string p) { var x = p; Sink(x); return x; } }",
               "fcs.C.M", "p"),
    "fc.c": ("char* m(char* p) { char* x = p; sink(x); return x; }", "fc.m", "p"),
    "fcpp.cpp": ("int m(int p) { int x = p; sink(x); return x; }", "fcpp.m", "p"),
    "fphp.php": ("<?php function m($p) { $x = $p; sink($x); return $x; }",
                 "fphp.m", "p"),
    "frs.rs": ("fn m(p: String) -> String { let x = p; sink(x); x }", "frs.m", "p"),
    "fgo.go": ("package m\nfunc m(p string) string { x := p; sink(x); return x }",
               "fgo.m", "p"),
    "frb.rb": ("def m(p)\n x = p\n sink(x)\n x\nend", "frb.m", "p"),
    "fkt.kt": ("fun m(p: String): String { val x = p; sink(x); return x }",
               "fkt.m", "p"),
    "fswift.swift": ("func m(_ p: String) -> String { let x = p; sink(x); return x }",
                     "fswift.m", "p"),
    "fscala.scala": ("def m(p: String): String = { val x = p; sink(x); x }",
                     "fscala.m", "p"),
    "flua.lua": ("function m(p)\n local x = p\n sink(x)\n return x\nend",
                 "flua.m", "p"),
}


@pytest.fixture()
def cgml(tmp_path):
    for fname, (code, _, _) in CASES.items():
        (tmp_path / fname).write_text(code, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


@pytest.mark.parametrize("fname", list(CASES))
def test_dataflow_param_flows(cgml, fname):
    _, fqn, param = CASES[fname]
    data, env = cgml.data_flow(fqn)
    assert data["supported"], f"{fname}: dataflow não suportado"
    names = {p["name"] for p in data["params"]}
    assert param in names, f"{fname}: parâmetro '{param}' não extraído (got {names})"
    p = next(pp for pp in data["params"] if pp["name"] == param)
    callees = {s["callee_name"].lower() for s in p["sinks"]}
    assert "sink" in callees, f"{fname}: fluxo até sink não detectado ({callees})"
    assert p["reaches_return"], f"{fname}: retorno não alcançado"


def test_all_dedicated_have_dataflow():
    """Paridade: toda linguagem de PROGRAMAÇÃO dedicada tem dataflow.

    Marcação/estilo (HTML/CSS/SCSS) tem extractor dedicado mas não tem fluxo de
    dados — não faz sentido, e por isso fica fora da paridade (languages.MARKUP).
    """
    from codegraph import dataflow as df
    from codegraph.languages import DEDICATED, MARKUP

    missing = {lang for lang in DEDICATED - MARKUP if not df.supported(lang)}
    assert not missing, f"linguagens dedicadas sem dataflow: {missing}"
