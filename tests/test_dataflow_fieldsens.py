"""Field-sensitivity do dataflow (docs/RESEARCH.md §6).

Um FATO tainted é um *caminho de acesso* (`("obj","campo")`), não um nome nu.
Ganhos provados aqui:
  - PRECISÃO: marcar um campo NÃO contamina os campos irmãos.
  - RECALL: atribuições a um campo (`obj.f = x`) agora são rastreadas (o
    extractor Python antes as descartava por completo).
O motor antigo (baseado em nomes) NÃO conseguia distinguir `o.x` de `o.y` —
os dois viravam `o`. Estes testes semeiam `o.x` vs `o.y` e exigem resultados
diferentes: a prova direta de que a análise passou a ser field-sensitive.
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph import dataflow as df
from codegraph.languages import get_parser


# -- prova direta no motor: prefixo distingue campos irmãos -------------------

def _facts(src: bytes, lang: str, line: int = 1):
    tree = get_parser(lang).parse(src)
    fn = df.find_function_node(tree.root_node, line, lang)
    assert fn is not None
    return df.extract_facts(src, fn, lang)


def test_prefix_rule_distinguishes_sibling_fields():
    src = b"def f(o):\n    a = o.x\n    b = o.y\n    return a\n"
    facts = _facts(src, "python")
    # semeando só o.x: a (=o.x) fica sujo, b (=o.y) não -> retorna a => alcança
    assert df.analyze_facts(facts, {("o", "x")}).reaches_return is True
    # semeando só o.y: a não é sujo -> retorno (a) não alcança
    assert df.analyze_facts(facts, {("o", "y")}).reaches_return is False


def test_whole_object_taints_all_fields():
    # regra de prefixo: marcar o objeto inteiro contamina qualquer campo
    src = b"def f(o):\n    a = o.x\n    return a\n"
    facts = _facts(src, "python")
    assert df.analyze_facts(facts, {("o",)}).reaches_return is True


def test_deep_path_capped_but_safe():
    # caminho mais fundo que o cap é truncado ao prefixo (super-aproxima, seguro)
    src = b"def f(o):\n    a = o.b.c.d.e\n    return a\n"
    facts = _facts(src, "python")
    # semear o prefixo dentro do cap contamina a leitura funda
    assert df.analyze_facts(facts, {("o", "b", "c")}).reaches_return is True


# -- ponta a ponta (Python): recall + precisão via data_flow ------------------

PY_MEMBER = '''
def store(user, evil):
    user.name = evil
    log(user.email)
    save(user.name)
'''


@pytest.fixture()
def cgpy(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent(PY_MEMBER), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    yield g
    g.close()


def test_py_member_target_recall_and_field_precision(cgpy):
    data, _ = cgpy.data_flow("m.store")
    evil = next(p for p in data["params"] if p["name"] == "evil")
    callees = {s["callee_name"] for s in evil["sinks"]}
    # RECALL: user.name = evil -> save(user.name) é detectado (antes: perdido)
    assert "save" in callees
    # PRECISÃO: user.email é campo irmão, não recebe taint de evil
    assert "log" not in callees


# -- ponta a ponta (JavaScript): alvo-membro também é rastreado ---------------

JS_MEMBER = '''
function store(user, evil) {
  user.token = evil;
  send(user.token);
  other(user.session);
}
'''


@pytest.fixture()
def cgjs(tmp_path):
    (tmp_path / "m.js").write_text(textwrap.dedent(JS_MEMBER), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    yield g
    g.close()


def test_js_member_target_recall_and_precision(cgjs):
    data, _ = cgjs.data_flow("m.store")
    evil = next(p for p in data["params"] if p["name"] == "evil")
    callees = {s["callee_name"] for s in evil["sinks"]}
    assert "send" in callees          # user.token = evil -> send(user.token)
    assert "other" not in callees     # user.session é irmão, não contaminado


def test_via_renders_access_path(cgjs):
    # o campo `via` agora carrega o caminho pontilhado (não um nome nu)
    data, _ = cgjs.data_flow("m.store")
    evil = next(p for p in data["params"] if p["name"] == "evil")
    vias = {s["via"] for s in evil["sinks"] if s["callee_name"] == "send"}
    assert "user.token" in vias
