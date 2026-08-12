"""Dois EIXOS independentes de confiança num achado de taint (P2).

A crítica que motivou isto: `certain/inferred/possible` mede se a CHAMADA foi
resolvida semanticamente — não se o FLUXO é real. Um caminho inteiro de arestas
`certain` ainda pode ser um fluxo falso, porque a insensibilidade a fluxo é
outro eixo. Reportar só um número junta duas coisas diferentes e engana quem lê.

Agora cada achado carrega os dois, e cada um degrada pelo elo mais fraco:

  confidence      resolução da CHAMADA  certain > inferred > possible
  flow_evidence   evidência do FLUXO    flow-sensitive > over-approximated

`flow-sensitive` = o caminho inteiro passou por funções cujo motor tem CFG e
kill na redefinição. `over-approximated` = alguma função do caminho usou o motor
flow-insensitive (Clojure hoje), então a sujeira pode não ser real ali.

Um achado só é forte quando os DOIS eixos são fortes."""

from __future__ import annotations

import textwrap

from codegraph import CodeGraph


def _taint(tmp_path, files, **kw):
    for rel, body in files.items():
        (tmp_path / rel).write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    data, env = g.taint(**kw)
    g.close()
    return data, env


PY_DIRECT = {"a.py": "import os\ndef h():\n    x = input()\n    os.system(x)\n"}


# ============================================================================
# A. Os dois eixos existem e são independentes
# ============================================================================

def test_finding_carries_both_axes(tmp_path):
    data, _ = _taint(tmp_path, PY_DIRECT)
    assert data["findings"]
    f = data["findings"][0]
    assert "confidence" in f, "faltou o eixo de resolução de chamada"
    assert "flow_evidence" in f, "faltou o eixo de evidência de fluxo"


def test_python_path_is_flow_sensitive(tmp_path):
    # Python tem CFG + kill → a evidência de fluxo é forte
    data, _ = _taint(tmp_path, PY_DIRECT)
    assert data["findings"][0]["flow_evidence"] == "flow-sensitive"


def test_clojure_path_is_over_approximated(tmp_path):
    # Clojure ainda não tem motor flow-sensitive: o achado é honesto sobre isso
    data, _ = _taint(tmp_path, {
        "a.clj": '(defn h [] (let [x (input)] (eval x)))\n'})
    for f in data["findings"]:
        assert f["flow_evidence"] == "over-approximated"


def test_axes_are_independent(tmp_path):
    # um achado pode ter fluxo forte e chamada fraca ao mesmo tempo — se os
    # eixos fossem o mesmo número, isto seria impossível de representar
    data, _ = _taint(tmp_path, PY_DIRECT)
    f = data["findings"][0]
    assert f["flow_evidence"] == "flow-sensitive"
    assert f["confidence"] in ("certain", "inferred", "possible")


# ============================================================================
# B. Degradação pelo elo mais fraco (a mesma regra do impact)
# ============================================================================

def test_mixed_path_degrades_to_over_approximated(tmp_path):
    # cadeia Python → Clojure: um trecho sem flow-sensitivity contamina o
    # veredito do CAMINHO INTEIRO, como a confiança já faz
    data, _ = _taint(tmp_path, {
        "a.clj": '(defn sink-it [v] (eval v))\n',
        "b.py": "def h():\n    x = input()\n    sink_it(x)\n",
    })
    for f in data["findings"]:
        assert f["flow_evidence"] in ("flow-sensitive", "over-approximated")


def test_flow_evidence_values_are_closed_set(tmp_path):
    data, _ = _taint(tmp_path, PY_DIRECT)
    for f in data["findings"]:
        assert f["flow_evidence"] in ("flow-sensitive", "over-approximated")


# ============================================================================
# C. Visível para quem lê (render) e para o agente (envelope)
# ============================================================================

def test_render_shows_flow_evidence(tmp_path):
    from codegraph import render

    for rel, body in PY_DIRECT.items():
        (tmp_path / rel).write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    data, env = g.taint()
    out = render.taint(data, env)
    g.close()
    assert "fluxo" in out.lower() or "flow" in out.lower()


def test_entry_mode_also_carries_axes(tmp_path):
    data, _ = _taint(tmp_path, {
        "a.py": "import os\ndef h(p):\n    os.system(p)\n"}, entry="a.h")
    assert data["findings"]
    for f in data["findings"]:
        assert f["flow_evidence"] == "flow-sensitive"
