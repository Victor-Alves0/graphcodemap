"""Extrai um catálogo de sources/sinks dos modelos "Models as Data" do CodeQL.

Ferramenta OFFLINE de manutenção, irmã de `import_taint_catalog.py`. Gera
`src/codegraph/taint_catalog_codeql.py` (Python puro), então o runtime NÃO
ganha dependência de YAML nem do CodeQL.

POR QUE ISTO EXISTE, E O QUE **NÃO** É
--------------------------------------
O repositório `github/codeql` é **MIT** (Copyright 2006-2025 GitHub, Inc.).
O que é MIT são as *queries* e as *bibliotecas QL*; o motor/CLI do CodeQL é
proprietário ("GitHub CodeQL Terms and Conditions") e não entra em nada aqui.

Mesmo com a licença permitindo, não dá para "portar as queries": uma query como
`SqlTainted.ql` tem ~20 linhas e só declara três conjuntos — a substância mora
em 250 mil linhas de biblioteca QL escritas contra o modelo de AST/IR que os
extractors proprietários produzem, avaliadas por um motor Datalog. E o banco do
CodeQL exige COMPILAR o projeto, que é o oposto da premissa deste aqui (sem
build, incremental, sempre fresco).

O que É portável, e é o que este script faz: os arquivos `*.model.yml` são
DADOS TABULARES — "este método, deste tipo, neste argumento, é sink desta
categoria". Conhecimento de modelagem acumulado por anos, em formato que não
depende do motor deles. Extraímos os NOMES DE API e a categoria; jogamos fora
tipo, assinatura e índice de argumento, que o nosso motor ainda não usa.

Uso:
    python scripts/import_codeql_models.py <caminho-para/github/codeql>
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:                                     # pragma: no cover
    sys.exit("precisa de pyyaml: pip install pyyaml (só para esta ferramenta)")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_taint_catalog import _TOO_GENERIC, _is_metavar   # noqa: E402

# diretório da linguagem no repo do CodeQL → linguagens do nosso índice
_LANGS = {
    "java": ("java",),
    "csharp": ("csharp",),
    "go": ("go",),
    "javascript": ("javascript", "typescript", "tsx"),
}

# CATEGORIAS DE SINK QUE IMPORTAMOS.
#
# Lista de INCLUSÃO, não de exclusão, e a diferença importa: os modelos do
# CodeQL cobrem categorias que o nosso motor não reporta, e a maior delas de
# longe é `log-injection` (851 linhas só em Java, 359 em Go). Logar dado do
# usuário é achado de severidade baixa e frequência altíssima — importá-lo
# encheria todo relatório de ruído e enterraria a injeção de comando embaixo.
#
# `credentials-*`, `encryption-*`, `environment`, `windows-registry` e afins
# são mau uso de API/configuração, que este motor não faz. Ficar de fora é a
# mesma decisão já tomada no harness do OWASP Benchmark, pelo mesmo motivo.
_SINK_KINDS = {
    "sql-injection", "nosql-injection", "command-injection", "code-injection",
    "path-injection", "path-injection[read]", "request-forgery",
    "jndi-injection", "ognl-injection", "xpath-injection", "ldap-injection",
    "xslt-injection", "groovy-injection", "template-injection",
    "url-redirection", "html-injection", "xss", "unsafe-deserialization",
    "file-write",
}

# Fonte só quando o dado vem da REDE. O CodeQL também modela `file`,
# `database`, `environment` e `commandargs` como fontes, mas eles só entram
# quando o usuário liga o "threat model" correspondente; tratá-los como
# não-confiáveis por padrão é o que transforma leitura de config em achado.
_SOURCE_KINDS = {"remote"}


def _name_from_row(row) -> tuple[str | None, str | None]:
    """(nome do método, categoria) de uma linha de modelo.

    Duas formas convivem no repo deles:
      9 colunas (Java/C#/Go): [pacote, tipo, subtipos, NOME, assinatura, …,
                               acesso, CATEGORIA, procedência]
      3 colunas (JavaScript): [pacote, caminho-de-acesso, CATEGORIA] com o
                               método dentro de `Member[…]`
    """
    if not isinstance(row, list):
        return None, None
    if len(row) >= 8:
        nome, categoria = row[3], row[7]
    elif len(row) == 3:
        categoria = row[2]
        # `Member[messages].Member[create].Argument[0]` → o último Member antes
        # do argumento é o método efetivamente chamado
        segs = [s for s in str(row[1]).split(".") if s.startswith("Member[")]
        nome = segs[-1][len("Member["):-1] if segs else None
    else:
        return None, None
    if not isinstance(nome, str) or not isinstance(categoria, str):
        return None, None
    return nome, categoria


def _keep(nome: str) -> bool:
    """Mesma régua do catálogo do OpenTaint, pelo mesmo motivo: casamos pelo
    ÚLTIMO SEGMENTO, sem tipo nem pacote. `executeQuery` é distintivo;
    `write`, `get`, `open`, `query` aparecem em qualquer código."""
    return not (len(nome) < 4
                or nome.lower() in _TOO_GENERIC
                or _is_metavar(nome)
                or not nome[0].isalpha())


def harvest(raiz: Path) -> dict[str, dict[str, set[str]]]:
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for dir_lang, nossas in _LANGS.items():
        ext = raiz / dir_lang / "ql" / "lib" / "ext"
        if not ext.is_dir():
            print(f"  ! sem modelos para {dir_lang} em {ext}", file=sys.stderr)
            continue
        n = 0
        for p in sorted(ext.rglob("*.model.yml")):
            try:
                doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            except Exception as exc:                    # pragma: no cover
                print(f"  ! {p.name}: {exc}", file=sys.stderr)
                continue
            for bloco in (doc or {}).get("extensions", []) or []:
                tipo = bloco.get("addsTo", {}).get("extensible")
                # `summaryModel` descreve PROPAGAÇÃO (arg→retorno) e
                # `neutralModel` diz "esta assinatura não flui". Os dois são
                # por ASSINATURA: `PreparedStatement.executeQuery()` é neutro
                # e `Statement.executeQuery(sql)` é sink, e o mesmo nome serve
                # aos dois. Sem tipo no casamento, usar o neutro para subtrair
                # apagaria sinks reais — então não usamos nenhum dos dois.
                if tipo == "sinkModel":
                    balde, aceitas = "sinks", _SINK_KINDS
                elif tipo == "sourceModel":
                    balde, aceitas = "sources", _SOURCE_KINDS
                else:
                    continue
                for row in bloco.get("data", []) or []:
                    nome, categoria = _name_from_row(row)
                    if nome is None or categoria not in aceitas or not _keep(nome):
                        continue
                    for lang in nossas:
                        out[lang][balde].add(nome)
                        n += 1
        print(f"  {dir_lang}: {n} entradas aceitas", file=sys.stderr)
    # AMBIGUIDADE: mesmo nome como fonte E sink na mesma linguagem não é
    # decidível pelo último segmento. Descartado dos dois lados — chutar o
    # papel é pior que não saber. (Mesma regra do catálogo do OpenTaint.)
    for lang, baldes in out.items():
        choque = baldes.get("sources", set()) & baldes.get("sinks", set())
        if choque:
            print(f"  ~ {lang}: {len(choque)} ambíguos descartados: "
                  f"{sorted(choque)[:12]}", file=sys.stderr)
            for b in ("sources", "sinks"):
                baldes[b] -= choque
    return out


_HEADER = '''"""Catálogo de sources/sinks por linguagem, dos modelos do CodeQL (GERADO).

NÃO EDITE À MÃO — gerado por `scripts/import_codeql_models.py`.

Semeado a partir dos arquivos `*.model.yml` ("Models as Data") de
github.com/github/codeql, sob licença **MIT**, Copyright (c) 2006-2025
GitHub, Inc. Deles extraímos apenas NOMES DE API e a categoria da regra —
não as queries, não as bibliotecas QL, e nada do motor/CLI do CodeQL, que é
proprietário e não foi usado.

O que fica de fora, e por quê: `log-injection` (a maior categoria deles) e as
famílias `credentials-*`/`encryption-*` cobrem defeitos que este motor não
reporta; importá-las só encheria o relatório. Fontes só de categoria `remote`.

Como o motor casa chamadas pelo ÚLTIMO SEGMENTO do nome, nomes genéricos
(`write`, `query`, `open`) são descartados na importação: sem tipo nem pacote
eles disparariam em qualquer código.

O usuário continua podendo ajustar tudo em `.codegraph/taint.json`.
"""

from __future__ import annotations

'''


def emit(catalogo: dict[str, dict[str, set[str]]], destino: Path) -> None:
    linhas = [_HEADER,
              "CATALOG_CODEQL: dict[str, dict[str, frozenset[str]]] = {"]
    for lang in sorted(catalogo):
        baldes = catalogo[lang]
        if not any(baldes.values()):
            continue
        linhas.append(f'    "{lang}": {{')
        for balde in ("sources", "sinks"):
            nomes = sorted(baldes.get(balde, ()))
            if not nomes:
                continue
            linhas.append(f'        "{balde}": frozenset({{')
            for i in range(0, len(nomes), 4):
                pedaco = ", ".join(f'"{n}"' for n in nomes[i:i + 4])
                linhas.append(f"            {pedaco},")
            linhas.append("        }),")
        linhas.append("    },")
    linhas += ["}", ""]
    destino.write_text("\n".join(linhas), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    raiz = Path(sys.argv[1])
    if not raiz.is_dir():
        return print(f"não encontrei {raiz}") or 1
    catalogo = harvest(raiz)
    destino = Path(__file__).resolve().parents[1] / "src" / "codegraph" / \
        "taint_catalog_codeql.py"
    emit(catalogo, destino)
    total = sum(len(v) for b in catalogo.values() for v in b.values())
    print(f"{destino}: {total} nomes em {len(catalogo)} linguagens")
    for lang in sorted(catalogo):
        b = catalogo[lang]
        print(f"  {lang}: {len(b.get('sources', ()))} sources, "
              f"{len(b.get('sinks', ()))} sinks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
