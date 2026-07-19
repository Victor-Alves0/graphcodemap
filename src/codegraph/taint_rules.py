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
    # comuns a web frameworks (nomes de método frequentes)
    "getparameter",
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


def load_rules(root: Path) -> TaintRules:
    src, snk, san = set(_SOURCES), set(_SINKS), set(_SANITIZERS)
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
