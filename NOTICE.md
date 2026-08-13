# Avisos de terceiros

O graphcodemap é distribuído sob a licença [MIT](LICENSE). Parte do seu
catálogo de regras de taint é **derivada de dados de terceiros**, também sob
MIT, cujos avisos de copyright são reproduzidos abaixo conforme exigido.

Nenhum código de terceiros é copiado para dentro do runtime: em ambos os casos
o que foi extraído são **nomes de API e a categoria da regra**, por ferramentas
offline em [`scripts/`](scripts/), gerando módulos Python puros. O runtime não
depende de nenhum destes projetos.

---

## GitHub CodeQL — modelos "Models as Data"

Origem: <https://github.com/github/codeql> (arquivos `*.model.yml`)
Deriva: [`src/codegraph/taint_catalog_codeql.py`](src/codegraph/taint_catalog_codeql.py)
Ferramenta: [`scripts/import_codeql_models.py`](scripts/import_codeql_models.py)

```
MIT License

Copyright (c) 2006-2025 GitHub, Inc.
```

**Escopo do que foi usado.** Apenas os arquivos de modelo (`*.model.yml`) do
repositório `github/codeql`, que está sob MIT. Deles extraímos o nome do método
e a categoria da regra, descartando tipo, assinatura e índice de argumento.

**O que NÃO foi usado.** Nada do CodeQL CLI, do motor de avaliação, dos
extractors ou dos binários distribuídos sob os "GitHub CodeQL Terms and
Conditions", que não são uma licença aprovada pela OSI. Nenhuma query `.ql` ou
biblioteca `.qll` foi copiada, traduzida ou portada.

---

## OpenTaint — regras de taint

Origem: <https://github.com/seqra/opentaint> (diretório `rules/`)
Deriva: [`src/codegraph/taint_catalog.py`](src/codegraph/taint_catalog.py)
Ferramenta: [`scripts/import_taint_catalog.py`](scripts/import_taint_catalog.py)

```
MIT License

Copyright (c) 2026 Seqra Team
```

**Escopo do que foi usado.** Apenas o diretório `rules/`, que está sob MIT.
Extraímos nomes de API e sua categoria — não a DSL de regras do Semgrep
(patterns, metavariáveis, `pattern-inside`).

---

## Projetos estudados, mas não usados

Registrados para deixar a fronteira explícita:

- **Joern** (Apache-2.0) — arquitetura de Code Property Graph estudada no
  código-fonte; nenhuma linha copiada.
- **Semgrep / Opengrep** (LGPL) — estudados apenas como referência de
  comportamento. Copiar código de qualquer um dos dois contaminaria a licença
  MIT deste projeto, então nada foi reaproveitado.
