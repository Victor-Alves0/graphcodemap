"""Flow-sensitivity do taint: o alvo de precisão (P1).

Trava as DUAS direções ao mesmo tempo, que é o que torna a promessa
"sem caminho, sem achado" falseável:

  A. FALSOS POSITIVOS que a insensibilidade a fluxo produzia. O motor antigo
     mantinha um conjunto `tainted` monotônico SEM kill-set: uma vez que `x`
     entrava, nunca saía — reatribuir com valor limpo/sanitizado não cortava o
     fluxo e a ordem era ignorada. RESOLVIDO pelo motor flow-sensitive
     (`flowsens.py`): CFG estruturada + `out = gen ∪ (in − kill)`.

  B. VERDADEIROS POSITIVOS que NÃO podem se perder ao ganhar precisão. Um
     kill-set implementado com a mão pesada mata recall — e recall perdido em
     segurança é pior que ruído. Estes são a guarda contra a super-correção.

Referências de arquitetura (estudadas, reimplementação limpa):
Joern (Apache-2.0) `passes/reachingdef` — `out = gen ∪ (in − kill)` com kill
ciente de campos; Opengrep (LGPL, só estudo) `Taint_lval_env` — ambiente
por-lvalue propagado no CFG com `add`/`clean`, sem análise de alias.
Ver docs/RESEARCH.md §7."""

from __future__ import annotations

import textwrap

from codegraph import CodeGraph


def _findings(tmp_path, src: str, *, entry: str | None = None):
    (tmp_path / "a.py").write_text(textwrap.dedent(src), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    data, _env = g.taint(entry=entry) if entry else g.taint()
    hits = [f["sink"]["callee"] for f in data["findings"]]
    g.close()
    return hits


# ============================================================================
# A. Falsos positivos da insensibilidade a fluxo (alvo do P1)
# ============================================================================

def test_reassignment_by_sanitizer_kills_taint(tmp_path):
    # x = input(); x = escape(x); system(x)  → o sink recebe o dado LIMPO.
    # É o caso canônico citado na crítica de engenharia.
    hits = _findings(tmp_path, """
        import os
        def h():
            x = input()
            x = escape(x)
            os.system(x)
    """)
    assert hits == []


def test_reassignment_by_literal_kills_taint(tmp_path):
    # redefinir com constante limpa mata a sujeira anterior
    hits = _findings(tmp_path, """
        import os
        def h():
            x = input()
            x = "constante"
            os.system(x)
    """)
    assert hits == []


def test_sink_before_taint_is_not_a_finding(tmp_path):
    # o sink executa ANTES da variável ficar suja → não há fluxo
    hits = _findings(tmp_path, """
        import os
        def h():
            x = "ok"
            os.system(x)
            x = input()
    """)
    assert hits == []


def test_reassigning_object_kills_field_taint(tmp_path):
    # reatribuir o objeto mata a sujeira dos CAMPOS dele (regra do Joern:
    # `x = Box()` mata `x.value`, `x.length()`, …)
    hits = _findings(tmp_path, """
        import os
        def h():
            box = Box()
            box.value = input()
            box = Box()
            os.system(box.value)
    """)
    assert hits == []


# ============================================================================
# B. Verdadeiros positivos — a guarda de recall (não podem se perder)
# ============================================================================

def test_direct_source_to_sink_still_found(tmp_path):
    hits = _findings(tmp_path, """
        import os
        def h():
            x = input()
            os.system(x)
    """)
    assert "system" in hits


def test_sanitizer_into_other_var_does_not_clean_original(tmp_path):
    # y = escape(x) limpa Y, não X — o sink em x continua vulnerável
    hits = _findings(tmp_path, """
        import os
        def h():
            x = input()
            y = escape(x)
            os.system(x)
    """)
    assert "system" in hits


def test_taint_then_sink_in_order_still_found(tmp_path):
    hits = _findings(tmp_path, """
        import os
        def h():
            x = "ok"
            x = input()
            os.system(x)
    """)
    assert "system" in hits


def test_field_taint_still_found(tmp_path):
    hits = _findings(tmp_path, """
        import os
        def h():
            box = Box()
            box.value = input()
            os.system(box.value)
    """)
    assert "system" in hits


def test_interprocedural_still_found(tmp_path):
    hits = _findings(tmp_path, """
        import os
        def run_cmd(c):
            os.system(c)
        def h():
            x = input()
            run_cmd(x)
    """)
    assert "system" in hits


def test_reassignment_with_tainted_value_keeps_taint(tmp_path):
    # kill não pode ser cego: reatribuir com outro valor SUJO mantém sujo
    hits = _findings(tmp_path, """
        import os
        def h():
            x = input()
            y = input()
            x = y
            os.system(x)
    """)
    assert "system" in hits


def test_taint_in_one_branch_is_still_a_finding(tmp_path):
    # may-taint: sujo em ALGUM caminho basta (o meet é união). Uma
    # implementação "must" mataria este achado — não é o que queremos.
    hits = _findings(tmp_path, """
        import os
        def h(cond):
            x = "ok"
            if cond:
                x = input()
            os.system(x)
    """)
    assert "system" in hits


def test_sanitized_only_in_one_branch_is_still_a_finding(tmp_path):
    # limpo num ramo, sujo no outro → o meet (união) mantém o achado
    hits = _findings(tmp_path, """
        import os
        def h(cond):
            x = input()
            if cond:
                x = escape(x)
            os.system(x)
    """)
    assert "system" in hits


def test_loop_carried_taint_is_found(tmp_path):
    # o fixpoint tem que convergir COM o back-edge do laço
    hits = _findings(tmp_path, """
        import os
        def h(items):
            x = "ok"
            for i in items:
                os.system(x)
                x = input()
    """)
    assert "system" in hits
