"""Regras de taint: sources (entrada não-confiável), sinks (operações
perigosas) e sanitizers (limpam o dado). Casadas pelo ÚLTIMO segmento do nome
da chamada (é como o call graph resolve nomes), portanto heurísticas por
convenção — ponto de partida honesto, ajustável por repositório.

Override: um arquivo `.codegraph/taint.json` na raiz do repo, com listas que
são UNIDAS às defaults (e um bloco opcional `remove` para tirar entradas):

    {
      "sources":   ["my_input"],
      "sinks":     ["run_shell"],
      "sanitizers":["my_escape"],
      "remove":    {"sinks": ["call", "run"]}
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Entrada não-confiável: o RETORNO destas chamadas nasce tainted.
_SOURCES = {
    # Python
    "input", "raw_input", "getenv", "get_json", "recv", "recvfrom",
    # comuns a web frameworks (nomes de método frequentes). ATENÇÃO: o
    # casamento é case-sensitive e usa o nome COMO ESCRITO no código — havia
    # aqui "getparameter" em minúsculas, que nunca casou com o `getParameter`
    # real do servlet: regra morta por anos.
    "getParameter", "getParameterValues", "getHeader", "getQueryString",
    "getInputStream", "getReader", "getRequestURI", "getRequestURL",
    # JS/Node
    "prompt",
}

# Operações perigosas: se um dado tainted alcança um argumento aqui → achado.
_SINKS = {
    # execução de código / shell
    "eval", "exec", "system", "popen", "Popen", "spawn", "spawnSync",
    "execSync", "execFileSync", "check_output", "check_call", "compile",
    "__import__",
    # SQL
    "execute", "executemany", "executescript", "executeQuery", "query",
    # (des)serialização perigosa / templates
    "loads", "load", "render_template_string", "literal_eval",
    # JS DOM/eval-like
    "innerHTML", "insertAdjacentHTML", "writeln", "setTimeout", "Function",
}

# Limpam o dado: o RETORNO de uma chamada a estes é considerado seguro.
_SANITIZERS = {
    "escape", "quote", "quote_plus", "sanitize", "clean", "escape_string",
    "secure_filename", "int", "float", "bool", "escapeHtml", "encodeURIComponent",
    "parseInt", "parseFloat",
}


@dataclass(frozen=True)
class TaintRules:
    sources: frozenset[str]
    sinks: frozenset[str]
    sanitizers: frozenset[str]


def default_rules() -> TaintRules:
    return TaintRules(frozenset(_SOURCES), frozenset(_SINKS), frozenset(_SANITIZERS))


# Suplemento CURADO À MÃO, por linguagem. Complementa o catálogo gerado com
# APIs perigosas que as regras do OpenTaint não cobriam. Critério de inclusão:
# o nome tem que ser DISTINTIVO o bastante para casar pelo último segmento sem
# disparar em código comum (`queryForObject` sim; `println` não).
#
# Lacuna conhecida e declarada: os sinks de XSS em Java são `println`/`print`/
# `write` num PrintWriter de resposta, e de trust-boundary são `setAttribute`.
# São genéricos demais para nome-nu — ficam de fora até existir casamento
# QUALIFICADO por receptor/pacote. Enquanto isso o recall de XSS é ZERO, e é
# melhor dizer isso do que fingir cobertura.
_CURATED: dict[str, dict[str, set[str]]] = {
    "java": {
        "sinks": {
            # JDBC / Spring JdbcTemplate
            "executeUpdate", "executeLargeUpdate", "addBatch", "batchUpdate",
            "queryForObject", "queryForList", "queryForMap", "queryForRowSet",
            "queryForInt", "queryForLong", "createStatement",
            # processo
            "ProcessBuilder",
            # XPath / expressão
            "evaluate", "compileExpression", "getValue", "setValue",
        },
    },
}


def _curated(lang: str, bucket: str) -> set[str]:
    return set(_CURATED.get(lang, {}).get(bucket, ()))


def catalog_for(languages) -> TaintRules:
    """Regras default + o catálogo por FRAMEWORK das linguagens presentes.

    O catálogo (`taint_catalog.py`, semeado das regras MIT do OpenTaint) é o que
    separa "funciona em código de exemplo" de "funciona no repo do cliente":
    sem ele, `exec.Command` do Go ou `FileOutputStream` do Java não são sinks
    conhecidos e a vulnerabilidade passa batido.

    Aplicado só às linguagens que o repo REALMENTE tem — carregar os 124 sinks
    de Java num repo Go só aumentaria a chance de colisão de nome à toa."""
    src, snk, san = set(_SOURCES), set(_SINKS), set(_SANITIZERS)
    try:
        from .taint_catalog import CATALOG
    except ImportError:                       # catálogo é opcional
        return TaintRules(frozenset(src), frozenset(snk), frozenset(san))
    for lang in languages or ():
        b = CATALOG.get(lang)
        if not b:
            continue
        src |= set(b.get("sources", ()))
        snk |= set(b.get("sinks", ()))
        san |= set(b.get("sanitizers", ()))
    for lang in languages or ():
        src |= _curated(lang, "sources")
        snk |= _curated(lang, "sinks")
        san |= _curated(lang, "sanitizers")
    return TaintRules(frozenset(src), frozenset(snk), frozenset(san))


def load_rules(root: Path, languages=None) -> TaintRules:
    base = catalog_for(languages)
    src, snk, san = set(base.sources), set(base.sinks), set(base.sanitizers)
    cfg = root / ".codegraph" / "taint.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        src |= set(data.get("sources", []))
        snk |= set(data.get("sinks", []))
        san |= set(data.get("sanitizers", []))
        rem = data.get("remove", {}) or {}
        src -= set(rem.get("sources", []))
        snk -= set(rem.get("sinks", []))
        san -= set(rem.get("sanitizers", []))
    return TaintRules(frozenset(src), frozenset(snk), frozenset(san))
