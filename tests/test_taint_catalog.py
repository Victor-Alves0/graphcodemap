"""Modelagem de framework: catálogo de sources/sinks por linguagem (P3).

Sem catálogo, o taint só enxerga um punhado de nomes universais (`eval`,
`system`, `input`) e passa batido nas APIs que causam vulnerabilidade de
verdade: `exec.Command` no Go, `FileOutputStream` no Java, o `getParameter` de
um servlet. É a diferença entre funcionar em código de exemplo e funcionar no
repositório do cliente.

O catálogo é GERADO (`scripts/import_taint_catalog.py`) a partir das regras do
OpenTaint, que são MIT — o conhecimento é legalmente reutilizável aqui.

Os testes cobrem as duas metades do problema:

  A. O catálogo ACHA o que antes era invisível.
  B. O catálogo não vira canhão de falso positivo: nomes genéricos demais
     (`Write`, `Run`, `New`) e nomes AMBÍGUOS (source num contexto, sink em
     outro) ficam de fora enquanto o motor casar só pelo último segmento.
"""

from __future__ import annotations

import pytest

from codegraph import CodeGraph
from codegraph.taint_rules import catalog_for, default_rules

try:
    from codegraph.taint_catalog import CATALOG
except ImportError:                                      # pragma: no cover
    CATALOG = {}


def _findings(tmp_path, fname, src):
    (tmp_path / fname).write_text(src, encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    data, _env = g.taint()
    out = [f["sink"]["callee"] for f in data["findings"]]
    g.close()
    return out


# ============================================================================
# A. Acha o que era invisível
# ============================================================================

def test_go_exec_command_is_now_a_sink(tmp_path):
    # `exec.Command` não estava em nenhuma regra default — a injeção de comando
    # mais comum em Go passava despercebida.
    hits = _findings(tmp_path, "m.go",
                     'package m\nimport "os/exec"\n'
                     'func h(){ x := os.Getenv("Q"); exec.Command(x) }')
    assert "Command" in hits


def test_java_file_stream_is_now_a_sink(tmp_path):
    hits = _findings(tmp_path, "A.java",
                     'class A { void h(){ String x = req.getParameter("q");'
                     ' new FileOutputStream(x); } }')
    assert "FileOutputStream" in hits


def test_servlet_get_parameter_is_a_source(tmp_path):
    # regressão: a regra default trazia "getparameter" em MINÚSCULAS e o
    # casamento é case-sensitive — nunca casou com o `getParameter` real.
    assert "getParameter" in default_rules().sources


def test_catalog_composes_with_flow_sensitivity(tmp_path):
    # P3 (sabe que é sink) + P1 (sabe que foi sanitizado) têm que compor:
    # o sink do catálogo não pode ressuscitar um fluxo já morto.
    hits = _findings(tmp_path, "m.go",
                     'package m\nimport "os/exec"\n'
                     'func h(){ x := os.Getenv("Q"); x = escape(x);'
                     ' exec.Command(x) }')
    assert hits == []


# ============================================================================
# B. Não vira canhão de falso positivo
# ============================================================================

@pytest.mark.skipif(not CATALOG, reason="catálogo não gerado")
def test_no_overly_generic_names_in_catalog():
    # casar `Write`/`Run`/`New`/`Open` pelo último segmento dispararia em
    # qualquer código. Ficam de fora até existir casamento QUALIFICADO.
    proibidos = {"get", "set", "new", "run", "write", "read", "open", "close",
                 "parse", "query", "body", "name", "value", "data", "path"}
    for lang, buckets in CATALOG.items():
        for bucket, names in buckets.items():
            ruins = sorted(n for n in names if n.lower() in proibidos)
            assert ruins == [], f"{lang}/{bucket}: nomes genéricos demais: {ruins}"


@pytest.mark.skipif(not CATALOG, reason="catálogo não gerado")
def test_no_semgrep_metavariables_leaked():
    # `$METHOD`, `CLASS_FUNC`, `FILE_COPY_UTILS_METHOD` são placeholders da DSL
    # do Semgrep, não APIs — entrariam como nomes que nunca casam com nada.
    for lang, buckets in CATALOG.items():
        for bucket, names in buckets.items():
            lixo = sorted(n for n in names
                          if n.isupper() or (("_" in n) and n.upper() == n))
            assert lixo == [], f"{lang}/{bucket}: metavariáveis vazaram: {lixo}"


@pytest.mark.skipif(not CATALOG, reason="catálogo não gerado")
def test_no_name_is_both_source_and_sink():
    # `Query` é fonte HTTP (`c.Query()`) e sink SQL (`db.Query()`) no Go —
    # indecidível pelo último segmento, então sai dos dois lados.
    for lang, b in CATALOG.items():
        clash = sorted(b.get("sources", frozenset()) & b.get("sinks", frozenset()))
        assert clash == [], f"{lang}: ambíguos source+sink: {clash}"


def test_catalog_is_scoped_to_languages_present():
    # carregar os sinks de Java num repo Go só aumentaria colisão de nome
    go_only = catalog_for({"go"})
    java_only = catalog_for({"java"})
    # Compare the observable composed rules, not just one input catalog. A
    # Java catalog name may also be universal, curated for Go, or supplied by
    # the CodeQL-derived catalog. Picking an arbitrary set element made this
    # test depend on PYTHONHASHSEED and occasionally selected such an overlap.
    exclusivos = java_only.sinks - go_only.sinks
    assert exclusivos
    assert exclusivos <= java_only.sinks
    assert exclusivos.isdisjoint(go_only.sinks)


def test_type_specific_codeql_names_are_removed_when_name_only_is_ambiguous():
    java = catalog_for({"java"})
    assert "File" in java.sinks
    assert "getBytes" not in java.sources
    assert "update" not in java.sinks
    assert "valueOf" not in java.sinks


def test_defaults_survive_without_catalog():
    # o catálogo é aditivo: as regras universais continuam valendo
    base = default_rules()
    com = catalog_for({"go"})
    assert base.sinks <= com.sinks
    assert base.sources <= com.sources
