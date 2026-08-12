"""Extrai um catálogo de sources/sinks/sanitizers das regras do OpenTaint.

Ferramenta OFFLINE de manutenção: roda à mão quando quisermos re-sincronizar o
catálogo. Gera `src/codegraph/taint_catalog.py` (Python puro), portanto o
runtime NÃO ganha dependência de YAML nem das regras de terceiros.

Por que isto existe: o motor de taint casa chamadas pelo ÚLTIMO SEGMENTO do
nome (`exec.Command` → `Command`). Sem um catálogo por framework, o usuário
teria que escrever `.codegraph/taint.json` à mão — a diferença entre funcionar
em código de exemplo e funcionar no repositório do cliente. As regras do
OpenTaint são **MIT** (`rules/LICENSE`), então o conhecimento nelas é
legalmente reutilizável num projeto MIT, com atribuição.

Uso:
    python scripts/import_taint_catalog.py <caminho-para/opentaint/rules/ruleset>

O que NÃO fazemos: importar a DSL de regras do Semgrep (patterns, metavariáveis,
`pattern-inside`). Extraímos só os NOMES DE API e sua categoria — o que o nosso
motor consegue usar. É deliberadamente menos poderoso e muito mais simples.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:                                     # pragma: no cover
    sys.exit("precisa de pyyaml: pip install pyyaml (só para esta ferramenta)")

# tag da regra → papel no nosso motor
_ROLE = {
    "source": ("sources", re.compile(r"untrusted-data-source|-source$")),
    "sink": ("sinks", re.compile(r"-sink$")),
    "sanitizer": ("sanitizers", re.compile(r"saniti[sz]er|-validator$")),
}

# chamadas dentro de um pattern Semgrep: `pkg.Fn(...)`, `Fn(...)`, `$X.method(...)`
_CALL = re.compile(r"([A-Za-z_][\w.]*)\s*\(")

# ruído: construções da própria DSL e palavras-chave de linguagem
_NOISE = {
    "if", "for", "while", "switch", "return", "new", "catch", "import",
    "function", "func", "class", "print", "println", "String", "Integer",
    "int", "var", "let", "const", "public", "private", "static", "void",
}

# NOMES GENÉRICOS DEMAIS para casar pelo último segmento.
#
# Esta é a decisão de engenharia mais importante do arquivo. Nosso motor casa
# `exec.Command` pelo segmento final (`Command`), sem saber o pacote nem o tipo
# do receptor. Para nomes distintivos (`CommandContext`, `LookPath`, `ForkExec`)
# isso funciona bem. Para `Write`, `Run`, `New`, `Open`, `copy`, `list` — que
# aparecem em qualquer código — tratá-los como sink dispararia em massa e
# desfaria o ganho de precisão do motor flow-sensitive.
#
# Um nome descartado aqui NÃO é perda definitiva: volta quando o motor souber
# casar nome QUALIFICADO (pacote/receptor). Enquanto isso, preferimos recall
# menor a um catálogo que enche o usuário de ruído — ruído destrói a confiança
# na ferramenta, que é o ativo principal.
_TOO_GENERIC = {
    "get", "set", "new", "run", "start", "stop", "open", "close", "read",
    "write", "parse", "post", "head", "body", "query", "values", "value",
    "header", "headers", "param", "params", "input", "output", "bind",
    "create", "encode", "decode", "json", "url", "uri", "link", "stat",
    "copy", "find", "list", "walk", "move", "touch", "mkdir", "html",
    "text", "data", "name", "path", "file", "send", "call", "load", "save",
    "add", "put", "push", "next", "done", "must", "check", "test", "init",
    # getters genéricos (Java/Kotlin): casam com qualquer POJO
    "getname", "getvalue", "gettext", "getdata", "getbody", "getpath",
    "getfile", "getinputstream", "iterator", "readfrom", "tostring",
}


def _is_metavar(name: str) -> bool:
    """Placeholder da DSL do Semgrep, não uma API real.

    A convenção deles é CAIXA_ALTA (`$METHOD`, `CLASS_FUNC`,
    `FILE_COPY_UTILS_METHOD`). Sem este filtro, entrariam no catálogo como se
    fossem nomes de função — e nunca casariam com código nenhum."""
    return name.isupper() or ("_" in name and name.upper() == name)


def _last_segment(name: str) -> str:
    """`exec.Command` → `Command`; é assim que o motor casa nomes."""
    return name.rsplit(".", 1)[-1]


def _walk_strings(node):
    """Todas as strings de um YAML aninhado (os patterns vivem em folhas)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_strings(v)


def _role_of(tags: list[str]) -> str | None:
    for tag in tags:
        for role, (_bucket, rx) in _ROLE.items():
            if rx.search(tag):
                return role
    return None


def harvest(ruleset: Path) -> dict[str, dict[str, set[str]]]:
    """{linguagem: {sources|sinks|sanitizers: {nomes}}}"""
    out: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set))
    for path in sorted(ruleset.rglob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:                        # pragma: no cover
            print(f"  ! {path.name}: {exc}", file=sys.stderr)
            continue
        for rule in (doc or {}).get("rules", []) or []:
            role = _role_of(rule.get("tags", []) or [])
            if role is None:
                continue
            bucket = _ROLE[role][0]
            langs = rule.get("languages") or ["*"]
            # só os blocos do papel certo (um arquivo de sinks pode citar
            # sources no contexto, e vice-versa)
            key = {"source": "pattern-sources", "sink": "pattern-sinks",
                   "sanitizer": "pattern-sanitizers"}[role]
            block = rule.get(key)
            if block is None and role == "source":
                # regras de source do Java põem os patterns direto no topo
                # (pattern-either), sem o wrapper `pattern-sources`
                block = rule.get("pattern-either") or rule.get("patterns")
            for text in _walk_strings(block):
                for m in _CALL.finditer(text):
                    name = _last_segment(m.group(1))
                    if (len(name) < 3 or name in _NOISE
                            or name.lower() in _TOO_GENERIC
                            or _is_metavar(name)
                            or name.startswith("$") or not name[0].isalpha()):
                        continue
                    for lang in langs:
                        out[lang][bucket].add(name)
    # AMBIGUIDADE: um nome que é source E sink na mesma linguagem (ex.: Go tem
    # `c.Query()` como fonte HTTP e `db.Query()` como sink SQL) não pode ser
    # decidido pelo último segmento. Descartado dos dois lados até existir
    # casamento qualificado — chutar o papel seria pior que não saber.
    for lang, buckets in out.items():
        clash = buckets.get("sources", set()) & buckets.get("sinks", set())
        if clash:
            print(f"  ~ {lang}: {len(clash)} ambíguos descartados: "
                  f"{sorted(clash)}", file=sys.stderr)
            for b in ("sources", "sinks"):
                buckets[b] -= clash
    return out


_HEADER = '''"""Catálogo de sources/sinks/sanitizers por linguagem (GERADO).

NÃO EDITE À MÃO — gerado por `scripts/import_taint_catalog.py`.

Semeado a partir das regras do OpenTaint (github.com/seqra/opentaint,
`rules/` sob licença **MIT**, Copyright 2026 Seqra Team), das quais extraímos
apenas os NOMES DE API e sua categoria — não a DSL de regras do Semgrep.

O motor casa chamadas pelo ÚLTIMO SEGMENTO do nome (`exec.Command` → `Command`),
então o catálogo é um conjunto de nomes por linguagem. É intencionalmente menos
expressivo que uma regra Semgrep (sem tipos, sem contexto de import) — a
contrapartida é que roda no nosso motor incremental e sempre fresco.

O usuário continua podendo ajustar tudo em `.codegraph/taint.json`.
"""

from __future__ import annotations

'''


def emit(catalog: dict[str, dict[str, set[str]]], dest: Path) -> None:
    lines = [_HEADER, "CATALOG: dict[str, dict[str, frozenset[str]]] = {"]
    for lang in sorted(catalog):
        buckets = catalog[lang]
        if not any(buckets.values()):
            continue
        lines.append(f'    "{lang}": {{')
        for bucket in ("sources", "sinks", "sanitizers"):
            names = sorted(buckets.get(bucket, ()))
            if not names:
                continue
            lines.append(f'        "{bucket}": frozenset({{')
            for i in range(0, len(names), 4):
                chunk = ", ".join(f'"{n}"' for n in names[i:i + 4])
                lines.append(f"            {chunk},")
            lines.append("        }),")
        lines.append("    },")
    lines.append("}")
    lines.append("")
    dest.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        return int(bool(sys.stderr.write(__doc__)))
    ruleset = Path(sys.argv[1])
    if not ruleset.is_dir():
        return int(bool(sys.stderr.write(f"não é diretório: {ruleset}\n")))
    catalog = harvest(ruleset)
    dest = Path(__file__).resolve().parents[1] / "src" / "codegraph" / "taint_catalog.py"
    emit(catalog, dest)
    for lang in sorted(catalog):
        b = catalog[lang]
        print(f"  {lang:8s} sources={len(b.get('sources', ()))} "
              f"sinks={len(b.get('sinks', ()))} "
              f"sanitizers={len(b.get('sanitizers', ()))}")
    print(f"→ {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
