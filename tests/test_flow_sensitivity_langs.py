"""Flow-sensitivity em TODAS as linguagens dedicadas (P1b).

Sem isto, `FLOW_SENSITIVE` seria uma promessa não verificada: uma linguagem
listada lá cujo `analyze_flow` cai no fallback continuaria over-aproximando —
e o mapa `codegraph capabilities` MENTIRIA, que é o pior resultado possível
para um projeto cuja tese é honestidade epistêmica.

Cada linguagem é checada nas duas direções, com um sink que existe no catálogo
padrão (`taint_rules`):

  FP: fonte → sanitizer → sink   deve ficar LIMPO (o kill funcionou)
  TP: fonte → sink               deve ACHAR    (não perdeu recall)

Este arquivo pegou duas lacunas reais de EXTRAÇÃO (anteriores ao fluxo):
Kotlin e Swift não transformavam reatribuição (`x = f(x)`) em fato — sem o
fato, não há o que matar. Kotlin não nomeia os campos do `assignment` (daí o
passo `poslr`) e Swift usa `target`/`result`, não `left`/`right`."""

from __future__ import annotations

import pytest

from codegraph import CodeGraph
from codegraph.dataflow import FLOW_SENSITIVE

# (linguagem, arquivo, código-FP, código-TP). O sink de cada um está no
# catálogo padrão; o source é `getenv`, também padrão.
CASES = [
    ("java", "A.java",
     'class A { void h(){ String x = System.getenv("Q"); x = escape(x);'
     ' Runtime.getRuntime().exec(x); } }',
     'class A { void h(){ String x = System.getenv("Q");'
     ' Runtime.getRuntime().exec(x); } }'),
    ("go", "m.go",
     'package m\nfunc h(){ x := getenv("Q"); x = escape(x); system(x) }',
     'package m\nfunc h(){ x := getenv("Q"); system(x) }'),
    ("c", "m.c",
     'void h(){ char* x = getenv("Q"); x = escape(x); system(x); }',
     'void h(){ char* x = getenv("Q"); system(x); }'),
    ("csharp", "A.cs",
     'class A { void h(){ var x = getenv("Q"); x = escape(x); eval(x); } }',
     'class A { void h(){ var x = getenv("Q"); eval(x); } }'),
    ("php", "a.php",
     '<?php function h(){ $x = getenv("Q"); $x = escape($x); system($x); }',
     '<?php function h(){ $x = getenv("Q"); system($x); }'),
    ("ruby", "a.rb",
     "def h\n x = getenv('Q')\n x = escape(x)\n system(x)\nend",
     "def h\n x = getenv('Q')\n system(x)\nend"),
    ("rust", "m.rs",
     'fn h(){ let mut x = getenv("Q"); x = escape(x); system(x); }',
     'fn h(){ let mut x = getenv("Q"); system(x); }'),
    ("kotlin", "A.kt",
     'fun h(){ var x = getenv("Q"); x = escape(x); eval(x) }',
     'fun h(){ var x = getenv("Q"); eval(x) }'),
    ("swift", "a.swift",
     'func h(){ var x = getenv("Q"); x = escape(x); system(x) }',
     'func h(){ var x = getenv("Q"); system(x) }'),
    ("scala", "A.scala",
     'object A { def h() = { var x = getenv("Q"); x = escape(x); eval(x) } }',
     'object A { def h() = { var x = getenv("Q"); eval(x) } }'),
    ("lua", "a.lua",
     'function h() local x = getenv("Q") x = escape(x) system(x) end',
     'function h() local x = getenv("Q") system(x) end'),
]


def _has_finding(tmp_path, fname, src) -> bool:
    (tmp_path / fname).write_text(src, encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    data, _env = g.taint()
    out = bool(data["findings"])
    g.close()
    return out


@pytest.mark.parametrize("lang,fname,fp_src,_tp", CASES,
                         ids=[c[0] for c in CASES])
def test_sanitizing_reassignment_is_clean(tmp_path, lang, fname, fp_src, _tp):
    """fonte → sanitizer → sink: o kill precisa cortar o fluxo."""
    assert not _has_finding(tmp_path, fname, fp_src), (
        f"{lang}: falso positivo — a reatribuição sanitizada não matou o taint")


@pytest.mark.parametrize("lang,fname,_fp,tp_src", CASES,
                         ids=[c[0] for c in CASES])
def test_direct_flow_is_still_found(tmp_path, lang, fname, _fp, tp_src):
    """fonte → sink direto: precisão não pode custar recall."""
    assert _has_finding(tmp_path, fname, tp_src), (
        f"{lang}: PERDEU o achado direto — regressão de recall")


def test_every_tested_language_is_declared_flow_sensitive():
    # o mapa de capacidades não pode prometer menos do que entregamos
    for lang, *_ in CASES:
        assert lang in FLOW_SENSITIVE, f"{lang} passa nos testes mas não está declarada"


def test_declared_flow_sensitive_languages_are_covered_by_tests():
    # …nem mais: toda linguagem declarada tem que ter prova aqui. Aliases que
    # compartilham a mesma config (cpp/cuda→c, luau→lua, tsx/ts→js) são cobertos
    # pelo representante da família.
    ALIASES = {"cpp", "cuda", "luau", "typescript", "tsx", "javascript", "python"}
    tested = {c[0] for c in CASES} | ALIASES
    faltando = sorted(set(FLOW_SENSITIVE) - tested)
    assert faltando == [], (
        f"declaradas flow-sensitive sem teste: {faltando} — ou testa, ou tira "
        f"de FLOW_SENSITIVE (o mapa não pode mentir)")
