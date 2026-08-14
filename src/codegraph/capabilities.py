"""Mapa de capacidades por linguagem: o que está PRONTO e o que FALTA.

Existe por um motivo operacional: com 20+ linguagens e camadas independentes
(extractor, dataflow, taint, flow-sensitivity, resolver L1), é fácil endurecer
duas linguagens e passar batido numa terceira. Este módulo é a fonte única da
verdade — e é DERIVADO DO CÓDIGO, nunca escrito à mão: se uma linguagem ganha
um extractor mas não ganha dataflow, o mapa mostra o buraco sozinho.

O teste `tests/test_capabilities.py` falha quando uma linguagem nova aparece e
não é classificada aqui — é a trava contra "esquecer a linguagem Z".

Eixos (do mais básico ao mais avançado):

  parse    L0 — tem parser tree-sitter (todas têm)
  extract  L0 — extractor: 'dedicated' (fqn/imports/calls refinados) ou 'generic'
  dataflow L2 — fatos de fluxo intra-procedural (para onde vai um parâmetro)
  taint    L2 — fonte→sink com sanitizers (requer dataflow)
  flow     L2+ — taint FLOW-SENSITIVE (CFG + kill na redefinição). Sem isto o
                 motor over-aproxima: reatribuir com valor limpo não corta o
                 fluxo. Ver docs/RESEARCH.md §7.
  l1       L1 — resolver semântico que promove arestas a 'certain'
"""

from __future__ import annotations

from . import dataflow as df
from .languages import CONFIG, DEDICATED, EXT_TO_LANG, MARKUP

# Formatos de DADOS/DOCS: têm estrutura navegável (chaves de topo, headings) mas
# não têm função/parâmetro — dataflow/taint é N/A, como em MARKUP/CONFIG. Sem
# esta distinção o mapa acusaria 'falta dataflow' em YAML, que é ruído puro.
DATA_FORMATS = frozenset({"json", "yaml", "toml", "xml", "markdown"})

# Linguagens cujo taint é FLOW-SENSITIVE (CFG + reaching-defs com kill).
# Rollout por etapas e declarado: o resto usa o motor flow-insensitive, que
# over-aproxima — honesto, mas menos preciso. Fonte: dataflow.FLOW_SENSITIVE.
FLOW_SENSITIVE: frozenset[str] = getattr(df, "FLOW_SENSITIVE", frozenset())

# Resolvers L1 VALIDADOS contra um servidor vivo (o resto está wired e inerte
# até o toolchain existir). Curado à mão porque "foi testado ao vivo" é um fato
# do mundo, não do código.
L1_VALIDATED: frozenset[str] = frozenset({
    "python", "typescript", "tsx", "javascript", "go", "rust", "lua", "luau",
    "clojure", "java", "php",
})

# Validação em repo real é mais forte que um smoke test de duas funções. Só
# entram linguagens com medição registrada em evals/RESULTS.md.
L1_REAL_REPO: frozenset[str] = frozenset({
    "python", "javascript", "go", "java", "php",
})

# Evidência de segurança não é derivável do parser: exige corpus/oráculo. Apps
# reais provam utilidade, mas não dão TN/FN exaustivos; o OWASP Java é rotulado
# e permite matriz de confusão completa. Manter a distinção evita chamar todas
# as linguagens com o mesmo motor de "igualmente prontas".
SECURITY_EVIDENCE: dict[str, str] = {
    "python": "real-app",
    "javascript": "real-app",
    "php": "real-app",
    "ruby": "real-app",
    "java": "labeled-benchmark",
}

# Rótulo de cada eixo para renderização.
AXES = ("extract", "dataflow", "taint", "flow", "l1")


def _l1_languages() -> dict[str, str]:
    """{linguagem: motor L1} para todo resolver wired.

    O nome do motor sai de `explain._L1_ENGINE` (a fonte canônica, já usada nas
    respostas), não de `cmd_name`: jedi e o tsserver rodam in-process/embutidos e
    não têm binário no PATH — cair no `cmd_name` rotularia JS/TS como 'jedi'."""
    from . import l1
    from .explain import _L1_ENGINE

    out: dict[str, str] = {}
    for cls in l1.all_resolvers():
        for lang in getattr(cls, "languages", ()):
            out[lang] = _L1_ENGINE.get(lang) or getattr(cls, "cmd_name", "") or "?"
    return out


def all_languages() -> list[str]:
    """Toda linguagem que o sistema reconhece por extensão."""
    return sorted(set(EXT_TO_LANG.values()))


def tier(lang: str) -> str:
    """Camada do extractor: dedicated | generic. (markup/config são dedicated
    com dataflow N/A — a distinção fica em `applicable`.)"""
    return "dedicated" if lang in DEDICATED else "generic"


def dataflow_applicable(lang: str) -> bool:
    """Dataflow/taint fazem sentido nesta linguagem? Marcação (HTML/CSS), config
    declarativa (Terraform) e formatos de dados (JSON/YAML/…) não têm função ou
    parâmetro a rastrear — não é lacuna, é N/A. Manter a distinção é o que faz
    'paridade de dataflow' significar alguma coisa."""
    return lang not in MARKUP and lang not in CONFIG and lang not in DATA_FORMATS


def is_code(lang: str) -> bool:
    """É linguagem de PROGRAMAÇÃO? Marcação/config/dados têm estrutura, mas não
    têm semântica de chamada — nem dataflow nem resolver L1 se aplicam a elas."""
    return dataflow_applicable(lang)


def for_language(lang: str) -> dict:
    """Capacidades de UMA linguagem."""
    l1map = _l1_languages()
    code = is_code(lang)
    has_df = df.supported(lang)
    if not code or lang not in l1map:
        l1_evidence = "none"
    elif lang in L1_REAL_REPO:
        l1_evidence = "real-repo"
    elif lang in L1_VALIDATED:
        l1_evidence = "live-smoke"
    else:
        l1_evidence = "wired"
    row = {
        "language": lang,
        "extract": tier(lang),
        "dataflow_applicable": code,
        "l1_applicable": code,
        "dataflow": has_df,
        "taint": has_df,                       # taint monta sobre dataflow
        "flow": lang in FLOW_SENSITIVE,
        "l1_wired": lang in l1map,
        "l1_server": l1map.get(lang),
        "l1_validated": lang in L1_VALIDATED,
        "l1_evidence": l1_evidence,
        "security_evidence": SECURITY_EVIDENCE.get(lang, "none"),
    }
    row["product_level"] = _product_level(row)
    return row


def _product_level(row: dict) -> str:
    """Nível de produto, não apenas presença de implementação.

    ``engine`` significa que os eixos existem; ``*-validated`` exige evidência
    externa. Esta é a diferença entre "o código suporta" e "sabemos que funciona".
    """
    if row["extract"] != "dedicated":
        return "recognized"
    if not row["dataflow_applicable"]:
        return "structural"
    if row["security_evidence"] == "none":
        return "engine"
    if row["l1_evidence"] == "real-repo":
        return "semantic-validated"
    return "security-validated"


def matrix() -> list[dict]:
    """Mapa completo, uma linha por linguagem, ordenado por maturidade."""
    rows = [for_language(l) for l in all_languages()]
    rows.sort(key=lambda r: (-_score(r), r["language"]))
    return rows


def _score(row: dict) -> int:
    """Maturidade: quanto mais alto, mais pronta."""
    return (
        (4 if row["extract"] == "dedicated" else 0)
        + (2 if row["dataflow"] else 0)
        + (4 if row["flow"] else 0)
        + (2 if row["l1_validated"] else 1 if row["l1_wired"] else 0)
    )


def gaps() -> list[dict]:
    """As LACUNAS reais, já filtrando o que é N/A. É a lista de trabalho."""
    out = []
    for r in matrix():
        missing = []
        if r["extract"] != "dedicated":
            missing.append("extractor dedicado")
        if r["dataflow_applicable"] and not r["dataflow"]:
            missing.append("dataflow/taint")
        if r["dataflow_applicable"] and r["dataflow"] and not r["flow"]:
            missing.append("flow-sensitivity")
        if r["l1_applicable"]:
            if not r["l1_wired"]:
                missing.append("resolver L1")
            elif not r["l1_validated"]:
                missing.append("validar L1 ao vivo")
            elif r["l1_evidence"] != "real-repo":
                missing.append("validar L1 em repo real")
        if (r["extract"] == "dedicated" and r["dataflow_applicable"]
                and r["security_evidence"] == "none"):
            missing.append("validar segurança em corpus")
        if missing:
            out.append({"language": r["language"], "missing": missing})
    return out


def summary() -> dict:
    """Contagens para o cabeçalho do relatório."""
    rows = matrix()
    df_app = [r for r in rows if r["dataflow_applicable"]]
    return {
        "languages": len(rows),
        "dedicated": sum(1 for r in rows if r["extract"] == "dedicated"),
        "dataflow": sum(1 for r in rows if r["dataflow"]),
        "dataflow_applicable": len(df_app),
        "flow_sensitive": sum(1 for r in rows if r["flow"]),
        "l1_wired": sum(1 for r in rows if r["l1_wired"]),
        "l1_validated": sum(1 for r in rows if r["l1_validated"]),
        "l1_real_repo": sum(1 for r in rows if r["l1_evidence"] == "real-repo"),
        "security_validated": sum(1 for r in rows
                                  if r["security_evidence"] != "none"),
    }
