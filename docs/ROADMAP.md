# Roadmap — precisão do motor de taint

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
| precisão | 65% |
| recall (TPR) | 74% |
| FPR | 44% |
| score (TPR − FPR) | **+0.29** |

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

Placares oficiais publicados no repositório do Benchmark
(`scorecard/OWASP_Benchmark_Home.html`):

| ferramenta | TPR | FPR | score |
|---|---|---|---|
| FindBugs + FindSecBugs 1.4.6 | 96,8% | 57,7% | 39,1% |
| SonarQube Java 3.14 | 50,4% | 17,0% | 33,3% |
| **graphcodemap** | **74%** | **44%** | **29%** |
| OWASP ZAP (DAST) | 20,0% | 0,1% | 19,8% |

O líder em score tem **57,7% de FPR** — mais da metade do código seguro
acusado. Ninguém revisa um relatório assim; o score alto não se traduz em
ferramenta usável.

> **Alvo: o perfil do SonarQube — recall na casa dos 70% com FPR abaixo de 20%.
> O recall já está lá. O FPR é a única coisa que falta.**

Ressalvas da comparação, que puxam para lados opostos: os placares cobrem 11
categorias e nós pontuamos 7 (a favor deles); nós medimos por ARQUIVO sem
conferir a categoria do achado (a favor nosso, indevidamente). Não é comparação
linha a linha — é ordem de grandeza.

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
dvpwa têm resolução `certain`. No OWASP Benchmark, **zero** — não há resolver
de Java instalado.

| ação | estado |
|---|---|
| resolver L1 de Java (jdtls) | em investigação |
| resolver L1 de PHP (intelephense) | em investigação |
| aumentar taxa de promoção em Python/JS | em investigação |

Enquanto isso não andar, **nenhum outro trabalho de precisão vale a pena** —
todos dependem de saber para qual definição uma chamada aponta.

### 2. Causas de falso positivo ainda abertas

Classificação medida dos falsos positivos (a distribuição muda a cada rodada;
reclassificar antes de escolher):

- **Taint atravessando o retorno sem sumário** — a maior. Resolvida pelo item 1.
- **Sanitizador não modelado** — parcialmente feito (ESAPI, Spring, Commons
  Lang, OWASP Java Encoder). Custa recall: escape para HTML não protege uso em
  contexto de URL, e tratá-lo como universal apaga esse bug.
- **Propagação pelo RECEPTOR** — `String s = objSujo.toString()`. Hoje o motor
  não distingue receptor de argumento, o que impede endurecer o sumário.

### 3. Modelagem que falta

- **Ruby**: só `params`/`cookies` são fontes. Faltam sinks de Rails
  (`find_by_sql`, `constantize`, `render inline:`).
- **Python/JS**: catálogo curado à mão; o CodeQL não publica modelos MaD para
  essas linguagens, então não há atalho.
- **Índice de argumento nos sinks**: os modelos do CodeQL trazem `Argument[N]`
  em cada linha e nós usamos isso em apenas um punhado de sinks
  (`_ARG0_ONLY`). Estender é barato e reduz FP.

### 4. Cobertura estrutural

- **Callbacks anônimos**: feito para JS (`get#2`). Python (`lambda`) e Ruby
  (blocos) não têm equivalente.
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
