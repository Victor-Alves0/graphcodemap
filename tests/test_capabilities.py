"""Trava do mapa de capacidades: impede que uma linguagem passe batido.

O problema operacional real: com 20+ linguagens e camadas independentes
(extractor → dataflow → taint → flow-sensitivity → L1), endurecer duas
linguagens e esquecer a terceira é o modo de falha natural. Estes testes fazem
o esquecimento FALHAR o build em vez de passar despercebido.

A invariante central é a de PARIDADE: toda linguagem de programação com
extractor dedicado tem dataflow/taint. Adicionar um extractor dedicado novo sem
dataflow quebra este teste — forçando a decisão a ser explícita (implementar, ou
declarar N/A em MARKUP/CONFIG/DATA_FORMATS) em vez de silenciosa."""

from __future__ import annotations

from codegraph import capabilities as caps
from codegraph import dataflow as df
from codegraph.languages import CONFIG, DEDICATED, EXT_TO_LANG, MARKUP


# ============================================================================
# A. Cobertura: nenhuma linguagem fica de fora do mapa
# ============================================================================

def test_every_known_language_is_in_the_matrix():
    mapped = {r["language"] for r in caps.matrix()}
    known = set(EXT_TO_LANG.values())
    assert known - mapped == set(), "linguagem reconhecida mas ausente do mapa"


def test_every_row_is_fully_classified():
    for r in caps.matrix():
        assert r["extract"] in ("dedicated", "generic")
        for k in ("dataflow", "taint", "flow", "l1_wired", "l1_validated",
                  "dataflow_applicable", "l1_applicable"):
            assert isinstance(r[k], bool), f"{r['language']}: {k} não classificado"
        assert r["product_level"] in {
            "recognized", "structural", "engine", "security-validated",
            "semantic-validated",
        }
        assert r["l1_evidence"] in {"none", "wired", "live-smoke", "real-repo"}
        assert r["security_evidence"] in {"none", "real-app", "labeled-benchmark"}


def test_dedicated_set_matches_matrix():
    ded = {r["language"] for r in caps.matrix() if r["extract"] == "dedicated"}
    assert ded == DEDICATED


# ============================================================================
# B. Paridade: dedicada de código ⇒ dataflow/taint
# ============================================================================

def test_dedicated_code_languages_all_have_dataflow():
    faltando = [r["language"] for r in caps.matrix()
                if r["extract"] == "dedicated" and r["dataflow_applicable"]
                and not r["dataflow"]]
    assert faltando == [], (
        f"extractor dedicado sem dataflow: {faltando}. Implemente o dataflow "
        f"ou declare N/A (MARKUP/CONFIG/DATA_FORMATS) — não deixe implícito.")


def test_non_code_languages_are_marked_not_applicable():
    # marcação/config/dados não podem aparecer como 'lacuna de dataflow'
    for lang in MARKUP | CONFIG | caps.DATA_FORMATS:
        r = caps.for_language(lang)
        assert not r["dataflow_applicable"], f"{lang} deveria ser N/A p/ dataflow"
        assert not r["l1_applicable"], f"{lang} deveria ser N/A p/ L1"


def test_taint_implies_dataflow():
    for r in caps.matrix():
        if r["taint"]:
            assert r["dataflow"], f"{r['language']}: taint sem dataflow"


# ============================================================================
# C. Flow-sensitivity: subconjunto honesto (P1 em rollout por etapas)
# ============================================================================

def test_flow_sensitive_requires_dataflow():
    for r in caps.matrix():
        if r["flow"]:
            assert r["dataflow"], (
                f"{r['language']}: marcada flow-sensitive sem ter dataflow")


def test_flow_sensitive_set_is_declared_in_dataflow_module():
    # a fonte da verdade é o módulo do motor, não uma lista paralela no mapa
    assert caps.FLOW_SENSITIVE == getattr(df, "FLOW_SENSITIVE", frozenset())


def test_flow_sensitive_languages_are_supported_by_engine():
    for lang in caps.FLOW_SENSITIVE:
        assert df.supported(lang), f"{lang} em FLOW_SENSITIVE mas sem dataflow"


# ============================================================================
# D. L1: só onde faz sentido, e validado é subconjunto de wired
# ============================================================================

def test_l1_validated_is_subset_of_wired():
    rows = {r["language"]: r for r in caps.matrix()}
    for lang in caps.L1_VALIDATED:
        assert rows[lang]["l1_wired"], f"{lang} validado mas não wired"


def test_php_live_validation_is_not_lost_from_capability_map():
    # DVWA: 659 certain via intelephense; docs/RESULTS registram a medição.
    php = caps.for_language("php")
    assert php["l1_validated"]
    assert php["l1_evidence"] == "real-repo"


def test_l1_only_on_code_languages():
    for r in caps.matrix():
        if r["l1_wired"]:
            assert r["l1_applicable"], f"{r['language']}: L1 wired mas não é código"


def test_l1_engine_name_is_resolved_for_every_wired_language():
    # regressão: o fallback antigo rotulava JS/TS como 'jedi' (ambos rodam
    # in-process e não têm binário no PATH)
    for r in caps.matrix():
        if r["l1_wired"]:
            assert r["l1_server"] and r["l1_server"] != "?", r["language"]
    js = caps.for_language("javascript")
    assert "jedi" not in (js["l1_server"] or "").lower()


# ============================================================================
# E. Lacunas: a lista de trabalho é coerente com o mapa
# ============================================================================

def test_gaps_never_report_non_applicable_axes():
    for g in caps.gaps():
        r = caps.for_language(g["language"])
        if not r["dataflow_applicable"]:
            assert "dataflow/taint" not in g["missing"]
            assert "flow-sensitivity" not in g["missing"]
        if not r["l1_applicable"]:
            assert "resolver L1" not in g["missing"]


def test_unvalidated_dedicated_language_surfaces_security_evidence_gap():
    gaps = {g["language"]: g["missing"] for g in caps.gaps()}
    assert "validar segurança em corpus" in gaps["c"]
    assert "validar segurança em corpus" not in gaps.get("python", [])


def test_fully_ready_language_has_no_gaps():
    # quando uma linguagem tiver tudo, ela some da lista de lacunas
    faltantes = {g["language"] for g in caps.gaps()}
    for r in caps.matrix():
        completa = r["product_level"] == "semantic-validated"
        if completa:
            assert r["language"] not in faltantes


def test_summary_counts_match_matrix():
    s, rows = caps.summary(), caps.matrix()
    assert s["languages"] == len(rows)
    assert s["dedicated"] == sum(1 for r in rows if r["extract"] == "dedicated")
    assert s["flow_sensitive"] == sum(1 for r in rows if r["flow"])
    assert s["dataflow"] == sum(1 for r in rows if r["dataflow"])


def test_render_smoke():
    from codegraph import render

    out = render.capabilities(caps.matrix(), caps.summary(), caps.gaps())
    assert "capacidades por linguagem" in out
    assert "flow-sens" in out
