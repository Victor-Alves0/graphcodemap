# Roadmap — consolidation toward v0.2

## North star

**GraphCodeMap v0.2 is a trustworthy structural and security context engine for
AI coding agents on five Tier-A ecosystems: Python, JavaScript/TypeScript, Java,
PHP and Go.** The other recognized languages remain useful, but v0.2 does not
claim equal depth across all 46. See [Product Maturity](MATURITY.md).

## Release gates

1. No known fabricated `certain` edge in the adversarial L1 oracle.
2. Every Tier-A ecosystem has a dedicated extractor, applicable flow analysis,
   a live L1 integration test and external security evidence.
3. OWASP results are scored by the correct vulnerability category on the same
   source commit, not merely by whether a file was flagged.
4. Each Tier-A security claim includes a vulnerable-versus-fixed real-application
   comparison.
5. The full test, package and release checks pass in CI.

## Ordered execution gates

### G0 — Trust the graph

- [x] Reject callbacks, aliases and non-callable containers as semantic call targets.
- [x] Prevent L1 promotion collisions and fabricated JavaScript self-edges.
- [x] Add an executable JavaScript definition oracle, including object literals.
- [x] Prevent visualization data from breaking out of its script container.
- [x] Serialize lazy PageRank/community recomputation against concurrent writers.
- [x] Remove indexed files when ignore policy changes.

### G1 — Make measurements comparable

- [x] Normalize every finding as tool/version/commit/category/CWE/source/sink/path.
- [x] Require category agreement in OWASP scoring.
- [x] Add repeatable CodeQL, OpenGrep and OpenTaint adapters; missing CLIs are
      recorded as `unavailable`, not zero findings.
- [x] Record time, peak memory scope, errors and unsupported cases.
- [x] Keep the current security matrix deterministic; any future LLM-dependent
      row is ineligible until it reports repeated runs and confidence intervals.

### G2 — Finish Tier A

- [x] Prove Java JDTLS on a real Maven repository and rerun OWASP; Round 26
      promoted 8,838 edges with JDTLS 1.60.0 on JDK 21, with zero resolver
      errors and taint invariant after the Java
      return-summary safety guard. Gradle evidence remains a follow-up.
- [ ] Add a TypeScript vulnerable-versus-fixed real-app corpus.
- [ ] Add an externally labeled Go security corpus.
- [ ] Pin vulnerable and fixed revisions for Python, JavaScript and PHP apps.
- [ ] Per-category target: recall >= 70%, then FPR < 30%, then FPR < 20%.

### G3 — Harden the product surface

- [x] Add Ruff, branch-coverage thresholds and progressive typing to CI.
- [x] Make package/release validation depend on the complete quality gate,
      including Linux/Windows on Python 3.10–3.12 and a clean wheel smoke test.
- [ ] Version and contract-test CLI, library and MCP response schemas.
- [ ] Split large query/dataflow modules only after behavior is characterized.

### G4 — Expand only with evidence

- [ ] Prove Ruby/Solargraph against RailsGoat.
- [ ] Select the next language by user demand plus available labeled corpus.
- [ ] Never convert generic parser coverage into a parity claim.

---

## Security precision track (historical detail)

Documento vivo. Existe para que a próxima rodada **não redescubra** o que já foi
medido, e para que a ordem das ações seja justificada por número, não por
intuição. Cada item cita a medição que o coloca onde está.

Histórico completo, com as rodadas e os números de cada mudança:
[`evals/RESULTS.md`](../evals/RESULTS.md).

---

## Onde estamos

OWASP Benchmark v1.2, 1.698 casos das 7 categorias de taint:

| | valor |
|---|---|
| precisão | 90,4% |
| recall (TPR) | 96,2% |
| FPR | 11,6% |
| score (TPR − FPR) | **+0.847** |

Esta é a primeira linha medida com concordância obrigatória de **arquivo e
categoria**, no mesmo commit do alvo. Na mesma régua, OpenTaint marcou +0.338
(recall 90,8%, FPR 57,0%). Nesta matriz fixada, GraphCodeMap tem 49 TPs a mais
e 362 FPs a menos. Isso não é uma alegação de superioridade global sobre
CodeQL ou outro produto; ecossistema de queries, frameworks, operações e
linguagens ficam fora desta régua. Path traversal chegou a 100% de recall e o
FPR total ficou abaixo de 12%.

O holdout independente NIST Juliet CWE-23 impede transformar isso em uma
declaração universal: nele o GraphCodeMap teve precisão 100% e FPR 0%, mas
recall de **69,4%**. O primeiro gate externo (recall >=50%, FPR <=5%) foi
atingido por modelagem de fontes Java qualificada por tipo, sem regra específica
para nomes do corpus. O transporte atual encontra 3/3 vulnerabilidades Java
reais e dois dos três patches eliminam o oráculo. O próximo gate é ampliar os
efeitos de heap já existentes para lambdas invocadas, fan-out e hierarquias de
tipo, além de levar a mesma evidência às demais
linguagens Tier A, não continuar ajustando o corpus OWASP.
Veja [Security Benchmark](SECURITY_BENCHMARK.md).

### Checkpoint Round 26

- Gate local: **1.576 passed, 25 skipped, 1 xfailed**.
- OWASP: 868 TP / 92 FP / 34 FN / 704 TN; 1.942 findings e 378 descartados por
  categoria incorreta no rescore final.
- Juliet CWE-23: 308 / 0 / 136 / 444, contra 242 TP na rodada anterior.
- Pares reais: FitNesse 2 → 0 e openHAB 3 → 0; OpenRefine permanece 2 → 3.
- Residuais priorizados: OpenRefine, lambdas Java invocadas, propagação global
  de `System.setProperty` (xfail estrito) e fan-out/hierarquia de tipos mais
  largos. Os 34 FN e 92 FP do OWASP continuam sendo dívida medida.

Em aplicações vulneráveis REAIS (não gabarito sintético):

| app | linguagem | achados |
|---|---|---|
| DVWA | PHP | 51 |
| pygoat | Python/Django | 10 |
| dvna | Node/Express | 6 |
| NodeGoat | Node/Express | 4 |
| RailsGoat | Ruby/Rails | 2 |
| nodejs-goof | Node/Express | 2 |
| dvpwa | Python/aiohttp | 1 (100% de precisão) |

## Qual é o alvo, e por que não é "score máximo"

Placares oficiais publicados no artefato `scorecard/OWASP_Benchmark_Home.html`
do checkout externo pinado do OWASP Benchmark (o corpus não é vendorizado neste
repositório):

| ferramenta | TPR | FPR | score |
|---|---|---|---|
| FindBugs + FindSecBugs 1.4.6 | 96,8% | 57,7% | 39,1% |
| SonarQube Java 3.14 | 50,4% | 17,0% | 33,3% |
| **graphcodemap (7 categorias, category-correct)** | **96,2%** | **11,6%** | **84,7%** |
| OWASP ZAP (DAST) | 20,0% | 0,1% | 19,8% |

O líder em score tem **57,7% de FPR** — mais da metade do código seguro
acusado. Ninguém revisa um relatório assim; o score alto não se traduz em
ferramenta usável.

> **Os gates OWASP, Juliet e descoberta nos primeiros pares reais foram
> atingidos. O próximo gate é distinguir mais patches e elevar as demais
> linguagens Tier A à mesma evidência externa.**

Ressalva da comparação: os placares cobrem 11 categorias e nós pontuamos 7.
Nossa medição agora exige arquivo **e categoria**, mas ainda não é comparação
linha a linha com scorecards que incluem quatro classes de mau uso de API.

---

## Ordem das ações

A ordem não é negociável por preferência: cada item depende do anterior.
Trocá-la já causou um erro registrado (ver "Rejeitados").

### 1. Cobertura do L1 — o gargalo

O motor tem uma otimização de precisão pronta (o **sumário de retorno**:
`x = f(sujo)` só suja `x` se `f` devolver o argumento). Ela é correta e segura,
e **quase nunca dispara**, porque só age sobre chamadas resolvidas
semanticamente (`confidence='certain'`).

Medido: das centenas de atribuições com chamada, só **13** em pygoat e **3** em
dvpwa têm resolução `certain`. No OWASP Benchmark, o JDTLS agora promove
**8.838** arestas sem erro no snapshot Round 26. A poda de retorno Java
permanece fechada para despacho virtual/reflexão, mas aceita controle local
comprovado, enhanced-for e domínios fechados de List/Map local com índice/chave
constante.

| ação | estado |
|---|---|
| resolver L1 de Java (jdtls) | Maven real + A/B OWASP concluídos; falta Gradle |
| resolver L1 de PHP (intelephense) | DVWA real concluído: 659 arestas `certain`, achados de taint invariantes |
| aumentar taxa de promoção em Python/JS | em investigação |

Path traversal passou de 46% para **100% de recall**. O micro-goal de FPR <20%
foi superado em 11,6%, com 868 TPs totais. No Juliet CWE-23 ainda há 136 FNs,
concentrados em lambdas invocadas, fan-out mais largo, hierarquias de tipo,
containers, serialização e dispatch abstrato/virtual.

### 2. Causas de falso positivo ainda abertas

Classificação medida dos falsos positivos (a distribuição muda a cada rodada;
reclassificar antes de escolher):

- **Lista/Map local com índice/chave constante** — resolvidos por domínio
  fechado, com alias, escape, branch incerto e chave dinâmica fail-closed.
- **Despacho/reflexão com argumento constante** — 52 FPs restantes; alvo de
  chamada certo ainda não prova o valor devolvido pela implementação runtime.
- **Sanitizador HTML contextual** — 37 FPs restantes (ESAPI, Spring, Commons
  Lang, OWASP Java Encoder). Custa recall: escape para HTML não protege uso em
  contexto de URL, e tratá-lo como universal apaga esse bug.
- **Enhanced-for seguro conservador** — 3 FPs restantes; a propagação correta
  do elemento iterado recuperou 62 TPs totais e não deve ser desfeita.
- **Propagação pelo RECEPTOR** — `String s = objSujo.toString()`. Hoje o motor
  não distingue receptor de argumento, o que impede endurecer o sumário.

### 3. Modelagem que falta

- **Ruby**: só `params`/`cookies` são fontes. Faltam sinks de Rails
  (`find_by_sql`, `constantize`, `render inline:`).
- **Python/JS**: catálogo curado à mão; o CodeQL não publica modelos MaD para
  essas linguagens, então não há atalho.
- **Índice de argumento nos sinks**: Round 26 introduziu papéis declarativos e
  modelou `Runtime.exec(command, envp, dir)` como `{0,1}`. Isso removeu os 36
  FPs de working directory sem perder TP; o catálogo restante ainda deve ser
  migrado conforme houver oráculo semântico.

### 4. Cobertura estrutural

- **Callbacks anônimos**: feito para JS (`get#2`). Java agora não executa uma
  lambda não invocada por acidente, mas lambdas efetivamente invocadas ainda
  precisam de unidade deferred e aresta de chamada próprias. Python (`lambda`)
  e Ruby (blocos) não têm equivalente completo.
- **Código de nível de arquivo**: feito. Era invisível e é a norma em PHP.
- **`switch`/`if` sem chaves**: feito. Os dois avaliavam ramos em SEQUÊNCIA e
  apagavam recall em silêncio.

---

## Abordagens rejeitadas — não repetir

### Sumário de retorno resolvido por NOME

Construído e medido: score subia de +0.29 para **+0.62** com **zero falso
positivo** em 796 casos seguros. Rejeitado mesmo assim:

1. Custava **109 verdadeiros positivos** por motivo não isolado.
2. Zero FP em 796 casos não é resultado, é alarme — nenhuma análise estática
   real acerta 796 de 796.
3. Era quase inerte em código real e decisivo no Benchmark, porque o artifício
   de segurança do Benchmark é justamente um método auxiliar. Ganho que só
   aparece no gabarito é ganho no gabarito.

A versão atual (gated em `certain`) é a mesma ideia feita de forma segura.

### Portar as queries do CodeQL

As queries e bibliotecas de `github/codeql` são **MIT** e mesmo assim não são
portáveis: `SqlTainted.ql` tem ~20 linhas e só declara três conjuntos; a
substância está em **250.342 linhas** de biblioteca QL (contra 13.853 do
graphcodemap inteiro) escritas contra um IR que exige **compilar o projeto**.
O que É portável são os **dados** (`*.model.yml`), e isso já foi feito: recall
saltou de 31% para 60% numa rodada.

### `exec` restrito a chamadas sem receptor, globalmente

Derrubou o recall de cmdi de 26% para 3%: em Java a forma real é
`Runtime r = …; r.exec(cmd)`, com receptor em variável local. A régua passou a
ser por linguagem.

### `HttpResponse` como sink de XSS

Acusava código correto — toda view Django devolve uma, em geral com conteúdo já
escapado pelo template.

---

## Como se mede aqui

- **Nada entra sem medição antes e depois.** A escolha do item também é medida:
  classificar os falsos positivos por causa já contrariou o roadmap duas vezes
  (o item que eu tinha como "próximo" era 2,9% do problema).
- **Testes dinâmicos contra repositórios reais**, afirmando invariantes e nunca
  contagens fixas (`tests/test_real_repos.py`). Rodam com
  `CODEGRAPH_REAL_REPOS=<caminhos>`.
- **Dois oráculos, um contra o outro**: "sem caminho, sem achado" (precisão) e
  "sem silêncio diante do óbvio" (recall). Só o primeiro deixaria passar um
  scanner que não reporta nada.
- **Resultado negativo é resultado.** Ele fica registrado em `RESULTS.md` com o
  mesmo cuidado dos positivos.
