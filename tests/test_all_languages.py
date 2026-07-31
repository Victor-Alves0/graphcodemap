"""Invariantes cross-language: toda linguagem dedicada respeita o mesmo contrato.

Em vez de repetir os mesmos asserts básicos em 18 baterias, esta trava as
INVARIANTES que valem para QUALQUER extractor — nome/fqn não-vazios, kinds e
visibilidades do vocabulário conhecido, fqn ancorado no módulo, spans válidos,
refs sempre com dst_name, e robustez a entrada vazia/malformada (nunca estourar).
Uma amostra representativa por linguagem exercita a superfície comum (tipo com
membro, herança, chamada, import)."""

from __future__ import annotations

import textwrap

import pytest

from codegraph.extract import extract
from codegraph.languages import DEDICATED, get_parser

# amostra por linguagem: define um tipo com membro, herança e uma chamada.
SAMPLES = {
    "python": "import os\n\nclass A(B):\n    x = 1\n    def m(self):\n        return helper()\n",
    "typescript": "import {B} from './b';\nexport class A extends B {\n  x = 1;\n  m(): void { helper(); }\n}\n",
    "tsx": "import React from 'react';\nexport function C() {\n  return <div className='card'>{helper()}</div>;\n}\n",
    "javascript": "import B from './b';\nclass A extends B { m() { helper(); } }\nexport default A;\n",
    "rust": "use crate::b::B;\npub struct S { pub x: i32 }\nimpl B for S { fn m(&self) { helper(); } }\n",
    "go": "package p\nimport \"fmt\"\ntype S struct { X int }\nfunc (s S) M() { helper(); fmt.Println() }\n",
    "java": "package app;\nclass A extends B {\n  private int x;\n  void m() { helper(); }\n}\n",
    "kotlin": "package app\nclass A(val x: Int) : B {\n  fun m() { helper() }\n}\n",
    "csharp": "namespace App;\nclass A : B {\n  private int x;\n  void M() { Helper(); }\n}\n",
    "c": "#include <stdio.h>\nstruct S { int x; };\nvoid m(void) { helper(); }\n",
    "cpp": "#include <vector>\nclass A : public B {\n  int x;\npublic:\n  void m() { helper(); }\n};\n",
    "cuda": "__global__ void k(int* p) { helper(); }\nstruct S { int x; };\n",
    "php": "<?php\nnamespace App;\nuse App\\B;\nclass A extends B {\n  private $x;\n  function m() { helper(); }\n}\n",
    "ruby": "class A < B\n  attr_accessor :x\n  def m\n    helper\n  end\nend\n",
    "lua": "local M = {}\nfunction M.f()\n  return helper()\nend\nreturn M\n",
    "luau": "local M = {}\nfunction M.f(): number\n  return helper()\nend\nreturn M\n",
    "swift": "class A: B {\n  var x = 1\n  func m() { helper() }\n}\n",
    "scala": "class A(val x: Int) extends B {\n  def m() = helper()\n}\n",
    "clojure": "(ns app.core (:require [app.b :as b]))\n(defrecord R [x] P (m [this] (helper)))\n",
    "terraform": 'variable "r" {}\nresource "aws_x" "n" {\n  v = var.r\n}\n',
    "html": "<html><body><div id='a' class='card'>"
            "<script src='./x.js'></script></div></body></html>\n",
    "css": ".card { color: red }\n#a { color: blue }\n",
    "scss": "$c: red;\n.card {\n  .nested { color: $c }\n}\n",
}

# vocabulário fechado — um kind/visibilidade fora disto é bug (ou algo novo a
# registrar conscientemente aqui).
KINDS = {
    "function", "method", "class", "interface", "struct", "enum", "variable",
    "constant", "module", "type_alias", "file", "css_class", "css_id",
    "html_id", "key", "section",
    "resource", "data", "output", "local", "provider",  # terraform
}
VIS = {None, "public", "private", "protected", "internal", "crate", "package"}
REF_KINDS = {"calls", "imports", "inherits", "references"}


def _extract(lang, src, module="app"):
    b = textwrap.dedent(src).encode("utf-8")
    return extract(lang, b, module, get_parser(lang).parse(b))


def test_every_dedicated_language_has_a_sample():
    # se uma linguagem dedicada nova entrar sem amostra, este teste avisa
    assert DEDICATED <= set(SAMPLES), f"faltam amostras: {DEDICATED - set(SAMPLES)}"


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_sample_produces_symbols(lang):
    syms, _ = _extract(lang, SAMPLES[lang])
    assert syms, f"{lang}: a amostra não produziu nenhum símbolo"


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_symbol_invariants(lang):
    syms, _ = _extract(lang, SAMPLES[lang])
    for s in syms:
        assert s.name, f"{lang}: nome vazio (fqn={s.fqn!r}, kind={s.kind})"
        assert s.fqn, f"{lang}: fqn vazio (name={s.name!r})"
        assert s.kind in KINDS, f"{lang}: kind fora do vocabulário: {s.kind!r}"
        assert s.visibility in VIS, f"{lang}: visibilidade inválida: {s.visibility!r}"
        assert s.fqn.startswith("app"), f"{lang}: fqn não ancorado no módulo: {s.fqn!r}"
        assert s.parent_fqn is None or s.parent_fqn.startswith("app"), \
            f"{lang}: parent_fqn solto: {s.parent_fqn!r}"
        assert 1 <= s.start_line <= s.end_line, \
            f"{lang}: span inválido em {s.name}: {s.start_line}-{s.end_line}"
        assert s.body_hash, f"{lang}: body_hash vazio em {s.name}"


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_ref_invariants(lang):
    _, refs = _extract(lang, SAMPLES[lang])
    for r in refs:
        assert r.dst_name, f"{lang}: ref com dst_name vazio (kind={r.kind})"
        assert r.kind in REF_KINDS, f"{lang}: ref kind inválido: {r.kind!r}"
        assert r.src_fqn is None or r.src_fqn.startswith("app"), \
            f"{lang}: src_fqn solto: {r.src_fqn!r}"
        assert r.line >= 1, f"{lang}: linha de ref inválida: {r.line}"


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_empty_input_is_empty(lang):
    syms, refs = _extract(lang, "")
    assert syms == [] and refs == [], f"{lang}: entrada vazia produziu saída"


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_malformed_input_never_crashes(lang):
    src = SAMPLES[lang]
    fragments = [
        src[: len(src) // 2],            # truncado no meio
        src[len(src) // 3:],             # começo cortado
        "{{{{{{ ", "))))))", "<<<<", "@@@@",
        "\x00\x01\x02 garbage",
    ]
    for bad in fragments:
        try:
            _extract(lang, bad)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"{lang}: crash em entrada malformada {bad[:10]!r}: "
                        f"{type(e).__name__}: {e}")


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_containment_fqn_extends_parent(lang):
    # todo símbolo com pai declarado tem fqn que ESTENDE o fqn do pai (o modelo
    # de contenção do grafo depende disso para montar a árvore de símbolos).
    syms, _ = _extract(lang, SAMPLES[lang])
    by_fqn = {s.fqn for s in syms}
    for s in syms:
        if s.parent_fqn and s.parent_fqn in by_fqn:
            assert s.fqn.startswith(s.parent_fqn + "."), \
                f"{lang}: {s.fqn} não estende o pai {s.parent_fqn}"


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_get_parser_available(lang):
    # a gramática de toda linguagem dedicada tem que carregar
    assert get_parser(lang) is not None
