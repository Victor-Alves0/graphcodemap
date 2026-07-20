# M6 — Resultados da primeira avaliação (2026-07-18)

Setup: mesmo agente/modelo (deepseek/deepseek-v4-flash via OpenRouter, temp 0,
max 12 passos), 8 tasks sobre este repo com gabarito, juiz LLM com referência.
Braços: baseline (list_files/grep/read_file) vs codegraph (baseline + 8 tools
do grafo). Relatório bruto: report-1784373922.json.

| braço | nota juiz (0-10) | acerto objetivo | tokens/task | tool calls | seg/task |
|---|---|---|---|---|---|
| baseline | 8.25 | 75% | 23.234 | 9.6 | 52.5 |
| **codegraph** | **9.38** | **88%** | 43.320 | 9.8 | 74.8 |

## Leitura honesta

**Qualidade: o grafo vence com folga.** +1.13 na nota do juiz e 75%→88% no
acerto objetivo. O caso decisivo foi `dangling-edges` (pergunta multi-hop
sobre semântica de arestas): baseline marcou **0** após 18 tool calls sem
chegar lá; codegraph marcou **10**. Multi-hop estrutural é exatamente onde a
pesquisa previa vantagem (RESEARCH.md §2).

**Tokens: o grafo custou ~86% MAIS, não menos.** Contradiz os claims de
"−59% tokens" do ecossistema — e provavelmente diz mais sobre os baselines
fracos usados nesses claims do que sobre grafos. Causas identificadas aqui:
(1) 11 schemas de tool reenviados a cada rodada vs 3 do baseline;
(2) saídas verbosas de `impact`/`overview`; (3) o agente usa o grafo E ainda
lê os arquivos (comportamento correto — DESIGN §0.4). Alavancas de
otimização: saídas mais compactas, caps de budget por tool, menos schemas.

**Vieses deste setup (contra o grafo):** repo pequeno (43 arquivos) e
extremamente bem documentado — várias respostas existem literalmente em
docstrings/comentários, o que favorece grep. Em repos grandes/legados a
vantagem de qualidade deve crescer e a de custo pode inverter. Próximo passo
para alegação forte: rodar em repos externos maiores + LocBench/SWE-bench-Lite.

## Conclusão

O valor do grafo neste primeiro corte é **qualidade e confiabilidade em
perguntas estruturais**, não economia de tokens. É o resultado que o design
previa (complemento de grep, não substituto) e dá o alvo de engenharia da
próxima iteração: manter a qualidade cortando o custo por resposta.

---

# Rodada 2 — após compactação de custo (mesmo dia)

Mudanças: saídas das tools compactadas (externas agregadas `nome×N`, cap de
25 no impact, refs limit 60, overview budget 1200/6 símbolos), schemas
telegráficos, resultado de tool truncado em 4k chars, completeness curta.

| braço | nota juiz | objetivo | tokens/task | Δ custo vs r1 |
|---|---|---|---|---|
| baseline | 8.38 | 75% | 24.186 | +4% (ruído) |
| **codegraph** | 8.75 | **88%** | **36.034** | **−17%** |

- Overhead do grafo sobre o baseline: **+86% → +49%**. Acerto objetivo
  estável (88% vs 75%).
- Nota do juiz tem variância alta com n=8 e 1 execução por task: cada braço
  teve um zero-outlier nesta rodada (l3-stale no codegraph, confidence-layers
  no baseline). Para comparações finas, rodar 3× por task e usar mediana.
- Custo restante do braço grafo é majoritariamente comportamento correto:
  o agente consulta o grafo E lê os arquivos. Próximas alavancas: menos
  rodadas (respostas mais diretas ao agente), cache de prompt do provider.

## Escala em repos externos (benchrepos/, clones rasos)

| repo | linguagem | arquivos | símbolos | arestas | tempo index |
|---|---|---|---|---|---|
| flask | Python | 83 | 1.620 | 4.212 | 3,5s (+L1 472 promoções em 56s) |
| express | JavaScript | 141 | 1.918 | 11.692 | 4,6s |
| gin | Go | 99 | 1.750 | 11.601 | 7,1s |
| ripgrep | Rust | 101 | 3.491 | 25.293 | 19s |
| redis | C/C++ | 839 | 18.488 | 94.024 | ~134s |
| spring-petclinic | Java | 49 | 234 | 2.043 | 1,2s |

Achados: (1) headers `.h` de projetos C caíam na gramática C++ → fallback
para C implementado (INDEXER_VERSION 8); restantes ~349 parciais do redis
são macros pesadas/deps vendorizadas — indexados parcialmente com aviso,
comportamento honesto. (2) express com 97% de arestas pendentes: JS
idiomático (encadeamento `app.get(...)`) é quase todo receptor-dinâmico —
candidato natural para um resolver L1 de tsserver. (3) impact do Flask
retorna dependentes reais com `[certain]` após L1.

---

# Rodada 3 — express fix, prompt-cache e eval em repo externo (Flask)

**Extractor JS por atribuição** (`res.send = fn`, `Router.prototype.x`,
`exports.y`): arestas resolvidas do express saltaram de 302 para 3.990
(3%→34% — 13x). Era a lacuna dominante em JS clássico.

**Prompt caching medido** (prefixo estável: mensagens append-only + schemas
fixos): 20–42% dos tokens vêm do cache (~1/10 do preço). O relatório agora
mostra `cached` e `efetivo`.

**Eval no Flask** (repo desconhecido pelo agente, 6 tasks de localização,
após correção de bug do harness*):

| braço | nota juiz | objetivo | tokens | efetivo |
|---|---|---|---|---|
| baseline | 9.83 | 100% | 19.997 | 16.464 |
| codegraph | 10.0 | 100% | 36.259 | 22.589 |

\* o modelo vazava sintaxe interna de tool-call como texto na rodada final —
zerava tasks nos DOIS braços; o runner agora instrui resposta final explícita
e re-pede em texto se detectar o vazamento.

**Leitura consolidada das 3 rodadas:** em tasks de *localização greppável*
(Flask), o grafo empata em qualidade e custa mais — grep resolve. Em tasks
*estruturais/multi-hop* (repo CodeGraph: dangling-edges, impact), o grafo
decide: 88% vs 75% no objetivo, com baseline zerando a task mais difícil.
Conclusão operacional (refletida nas instructions do MCP): grep para achar,
grafo para entender estrutura — e o custo do grafo é pago só quando ele é a
ferramenta certa.

---

# Rodada 4 — redis: repo grande + tasks estruturais (tasks-redis.json)

A tese das rodadas anteriores previa: em repo grande, a vantagem de
qualidade cresce e a de custo inverte. Testado no redis (1.459 arquivos,
19.346 símbolos, 95k arestas) com 6 tasks multi-hop de gabarito verificado
manualmente (callers de rdbSaveRio/performEvictions/activeExpireCycle,
subsistemas do serverCron, cadeias AOF-rewrite e BGSAVE). Relatório:
report-1784390721-redis.json.

| braço | nota juiz | objetivo | tokens | efetivo | calls | seg |
|---|---|---|---|---|---|---|
| baseline | 7.5 | 83% | 36.598 | 25.913 | 12.2 | 74.0 |
| **codegraph** | **8.83** | **100%** | **32.009** | **22.639** | **11.3** | **62.6** |

**Primeira rodada em que o grafo vence em qualidade E custo simultaneamente:**
−13% tokens efetivos, −15% tempo, 100% vs 83% no objetivo. Em arquivos de
7k linhas (server.c), o baseline queima rodadas de grep+read para descobrir
a função que envolve cada call site; `callers` responde isso em 1 chamada.
O baseline zerou `redis-bgsave-chain`: estourou os 12 passos sem produzir
resposta final. A pior task do grafo (nota 5, evictions) foi erro de
conteúdo do agente (disse "depois do comando"; é antes) com os callers
certos — variância de n=1, não falha da ferramenta.

Nota de indexação: o boot do redis subiu de 839 arq/134s (11 linguagens)
para 1.459 arq/770s com os 3 níveis. Perfilado em seguida — a culpa NÃO era
dos extractors (parse+extract do repo inteiro: 8,2s): eram dois gargalos de
SQLite no caminho de *re-index sobre banco populado*:
(1) `symbols.parent_id ON DELETE SET NULL` sem índice → cada DELETE de
símbolo escaneava a tabela inteira (re-index de 1 arquivo grande: 2,7s);
(2) `resolve_edges` com `LIKE '%.x'` (scan de 19k símbolos) por aresta ×
38,5k pendentes, re-executado a cada read-repair (60s/passada).
Correções: índice em parent_id + resolução por nome indexado com memoização
por guess. Resultado (mesmos números de resolução, byte-idênticos):
reindex forçado 770s→251s (3x); resolve_edges 60s→0,5s (120x); re-index de
arquivo 2,7s→0,2s; boot sem mudanças: 2,7s. O caminho quente (read-repair
por query, watcher) era o mais beneficiado — era ele que inflava a latência
das tools do grafo no eval em repo grande.

**Quadro final das 4 rodadas:** repo pequeno greppável → empate com grafo
mais caro; repo próprio estrutural → grafo vence qualidade pagando mais;
repo grande estrutural → grafo vence tudo. A recomendação das instructions
do MCP ("grep para achar, grafo para estrutura") está validada nas três
condições.

---

# Rodada 5 — Benchmark acadêmico: SWE-bench-Lite (localização) (2026-07-18)

Primeiro benchmark com **dataset padrão e reconhecido**. Nota de honestidade
sobre o método: a harness OFICIAL do SWE-bench (gerar patch + rodar a suíte de
testes de cada repo) exige Docker + imagens por-tarefa + setup por projeto —
inviável neste ambiente. O que É viável e é exatamente o eixo da nossa tese:
**localização**. Cada tarefa do SWE-bench-Lite traz o *gold patch* (o diff que
resolveu a issue de verdade); os arquivos que ele edita são ground truth de
localização — o que o LocBench/LocAgent medem. Harness: `evals/locbench.py`.

Setup: 15 tarefas reais do SWE-bench-Lite (flask 3, requests 6, pytest 6 —
repos pequenos p/ custo baixo), todas com fix em **um único arquivo** (ground
truth limpo). Repo clonado e posto no `base_commit` de cada issue. Mesmo
agente/modelo (deepseek-v4-flash)/prompt; dois braços — baseline (grep/read/
list) vs +grafo (tools do CodeGraph). Extração final do JSON de resposta é
dedicada e idêntica nos dois braços (a variável é só o conjunto de tools).
Métrica: o braço encontrou o arquivo que o gold patch edita?

| braço | achou o arquivo | recall | símbolo | tokens | tool calls | seg |
|---|---|---|---|---|---|---|
| baseline | 80% (12/15) | 0.80 | 29% | 26.803 | 9.0 | 34.4 |
| **codegraph** | **93% (14/15)** | **0.93** | 29% | 41.333 | 9.6 | 40.1 |

Por tarefa: **11 empates, 3 vitórias do grafo** (baseline errou requests-2317,
requests-863, pytest-5413), **1 derrota** (requests-2674). Saldo: +2 tarefas.

## Leitura honesta (o que este número é e o que NÃO é)

- **É** evidência direcional a favor da tese, em dataset padrão: o grafo
  localizou 2 arquivos que o grep puro não achou, custando as mesmas
  ferramentas. Consistente com a pesquisa (RepoGraph/LocAgent: grafo ajuda em
  localização estrutural).
- **NÃO é** prova de SOTA. n=15, execução única, 3 repos pequenos, fixes de
  arquivo único. +13pp = 2 tarefas → **dentro do ruído** com n=15. Um intervalo
  honesto não exclui empate real.
- **NÃO é** o SWE-bench completo: medimos localização, não resolução de issue
  (gerar patch que passa nos testes). Localização é condição necessária, não
  suficiente.
- **Custo**: grafo +54% tokens (41k vs 27k) — o mesmo padrão de todas as
  rodadas. O grafo paga mais para acertar mais na localização.
- **Símbolo**: empate em 29% (fraco nos dois; a extração de símbolos-alvo do
  gold patch é heurística e o alvo é mais difícil que arquivo).

## Para virar alegação pública forte

Rodar em escala: as 300 tarefas do SWE-bench-Lite (ou o subconjunto
LocBench), 3× por tarefa para diluir variância de execução única, incluindo os
repos grandes (django/sympy) onde a vantagem estrutural deve crescer — e,
idealmente, a resolução completa (patch+testes) num ambiente com Docker. Este
piloto valida a harness e dá o primeiro sinal em dado real; a escala é
trabalho de compute, não de design.

---

# Rodada 6 — Reachability em Python (arestas certain + confiar): o ponteiro vira

Duas mudanças no sistema + um teste no terreno onde o grafo deve brilhar:
1. `reaches` agora SURFACE a confiança com veredito ("[certain] = pode confiar
   sem reler o código").
2. INSTRUCTIONS do MCP + prompt do eval dizem ao agente: se vier [certain], PARE.
3. Teste em **Python (flask)** com L1/jedi (472 arestas promovidas a `certain`),
   3 tarefas de reachability grep-hard com cadeias `certain` de 3–5 saltos, gold
   computado pelo grafo e verificado. deepseek-v4-pro. `evals/reachbench.py`.

| braço | correto | recall cadeia | tokens | chamadas |
|---|---:|---:|---:|---:|
| baseline (grep/read) | 67% | 0.58 | 47.954 | 16,0 |
| **codegraph** | **100%** | **1.00** | **19.947** | **5,7** |

Por tarefa (codegraph): tarefas 1 e 2 resolvidas em **1 chamada** (`reaches`),
~4k tokens cada — o modelo viu `[certain]`, confiou e parou. Baseline nas mesmas:
20 chamadas/59k e 13/23k → **~12× mais tokens**. Tarefa 3 (caminho não-óbvio via
sessão): **baseline ERROU** (grep não montou a cadeia), grafo acertou.

## Leitura honesta

- **Aqui o grafo ganhou nas duas frentes: correção (100% vs 67%) E custo (−58%
  tokens, −64% chamadas).** É a rodada com vantagem grande e limpa — e não por
  acaso: é onde as três condições se alinham (pergunta de travessia + arestas
  `certain` + agente instruído a confiar).
- **O mecanismo do `reaches` funcionou como projetado:** confiança alta →
  1 chamada → ~12× menos tokens. Sem L1 (arestas `possible`) o modelo re-verifica;
  com `certain` ele para. A diferença é a confiança, exatamente a hipótese.
- **Grep não é só mais caro aqui — erra:** na tarefa 3, a cadeia não-óbvia
  (wsgi_app→push→_get_session) o baseline não reconstruiu. É o "melhor recall em
  relações profundas" que o grafo deveria dar.
- **Ressalvas:** n=3, um repo, um modelo, temp 0 não-determinístico. Direcional,
  não prova de escala.

## Síntese: quando o graphcodemap é a melhor escolha

| Condição da pergunta | Melhor | Evidência |
|---|---|---|
| Texto exato / alvo já nomeado | grep | r3 |
| Localização de arquivo a editar | grafo (recall), + caro | r5 |
| Estrutural / repo grande | grafo (custo+qualidade) | r4 redis |
| **Reachability profunda + arestas certain** | **grafo (−58% tokens E +correção)** | **r6** |

O valor do graphcodemap não é uniforme — é **condicional e demonstrado**: em
perguntas de travessia/estrutura, com resolução semântica (L1) que dá confiança
alta, ele entrega a resposta pronta numa chamada, mais barato E mais correto que
grep. A alavanca: acoplar CONFIANÇA (certain) a um primitivo que ENTREGA a
resposta (`reaches`) e instruir o agente a confiar.

---

# Rodada 7 — A vitória generaliza: Go via gopls (L1 por LSP)

Prova de que a alavanca do L1 não é específica de Python/jedi: adicionamos o
resolver Go (`gopls` via LSP) e repetimos a rodada anterior no `benchrepos/gin`
(0→4705 arestas `certain`), 3 tarefas de reachability com cadeias certain de 2–4
saltos. deepseek-v4-pro. `evals/reachbench.py` + `evals/reach-gin.json`.

| braço | correto | recall cadeia | tokens | chamadas |
|---|---:|---:|---:|---:|
| baseline | 100% | 0.83 | 23.421 | 11,7 |
| **codegraph** | 100% | **1.00** | **17.530** | **6,7** |

Por tarefa: mapForm (4 saltos) → grafo em **2 chamadas** via `reaches` (7k tok)
vs baseline 9/14k; handleHTTPRequest → grafo 9/21k vs baseline 23/50k; serve-
error (2 saltos, raso) → **baseline venceu** (3/5k vs grafo 9/24k). Padrão
mantido: fundo=grafo, raso=grep.

## Leitura

- **O ganho do L1 generaliza para uma linguagem nova, por um caminho novo (LSP):**
  −25% tokens, −43% chamadas, recall melhor, mesma correção. `reaches` +
  confiança `certain` funcionam com qualquer resolver que produza `certain`.
- **Menor que no flask (−58%)** porque o gin é mais greppável (baseline 100% aqui
  vs 67% no flask) — quando o grep dá conta, a margem encolhe.
- **A tarefa rasa o grafo ainda sobre-explora** — o teto de "confiar e parar" é
  comportamento do agente, não do grafo.

## Implicação de produto

A receita está validada e replicável: **linguagem + L1 (resolução semântica) =
grafo passa a ganhar de grep em estrutura/travessia, mais barato E mais correto.**
Hoje há L1 para Python (jedi), JS/TS (tsserver), Go (gopls), Rust (rust-analyzer)
e C/C++ (clangd); cada nova linguagem com um LSP entra na mesma receita.

---

# Escala — prova em 100k+ arquivos (2026-07-20)

Harness reproduzível: [`evals/scalebench.py`](scalebench.py). Gera um repo
sintético com **densidade de grafo real** (imports + chamadas cross-file, ~2N
símbolos, ~N arestas), não arquivos isolados — assim os caminhos O(N) que
importam são de fato exercitados. Mede, por N crescente: tempo de índice frio,
**pico de memória** (working set do processo), tamanho do `.db`, varredura de
frescor (`scan_source_stats`), custo de um "miss" de query (dispara
read-repair), re-index incremental e latência de query. Hardware: Windows 11,
1 máquina, SQLite em disco local (números têm ruído de ±, não são um benchmark
de laboratório).

## Braço 1 — sintético (código namespaced, estilo Python)

| N | index | arq/s | pico RAM | DB | frescor | miss query | find | impact* | re-index† |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 5.000 | 11,6s | 430 | 49 MB | 7,3 MB | 0,22s | 0,24s | 9 ms | 0,35s | 4,5s |
| 20.000 | 128s | 156 | 94 MB | 35 MB | 0,86s | 0,94s | 40 ms | 2,5s | 19s |
| **100.000** | **484s** | 207 | **324 MB** | 181 MB | **4,4s** | **4,9s** | 197 ms | 16,7s | 102s |

\* `impact` no sintético é o **pior caso**: o grafo é uma cadeia linear de 500
níveis, então a travessia transitiva percorre a cadeia inteira. Código real
raramente é assim.  † `re-index` = `index()` completo após 1 edição (boot-scan
diff O(N), re-hash de tudo) — **não** é o caminho incremental do watcher (O(1)
por evento).

**Leitura honesta:**
- **✅ Não quebra.** 100k arquivos, **zero erro, zero OOM**, 324 MB de pico. O
  `by_name` em memória do `resolve_edges` (200k símbolos) — o suspeito nº 1 de
  estouro — escalou ~linear e aguentou.
- **Índice frio é ~linear** (150–430 arq/s, ruidoso): ~8 min p/ 100k. Custo
  único; o watcher mantém quente depois.
- **⚠️ A garantia forte de frescor custa ~5s por "miss" a 100k.** A varredura
  `scan_source_stats` (scandir, size/mtime) é O(N): barata até ~20-30k (<1s),
  limítrofe a 100k. Acima disso precisa de throttle/estratégia em camadas
  (trabalho futuro). Antes documentado como "~250ms a 8k" — a 100k são ~5s.
- **⚠️ `index()` completo é O(N) re-hash** (102s a 100k). Incremental de verdade
  é via watcher, não chamando `index()` de novo.

## Braço 2 — C real (kernel Linux, `git clone --depth 1`)

Corroboração em código de verdade. Duas medições:

| repo | arquivos | index | arq/s | símbolos | símbolos/arq | bytes/arq | resultado |
|---|---:|---:|---:|---:|---:|---:|---|
| `kernel/` | 641 | 35s | 18 | 23.764 | 37 | **55 KB** | ✅ completa |
| kernel inteiro | 72.428 | — | — | — | — | — | **🧱 não completa** |

O kernel **inteiro travou no L0 a 38.445/72.428 arquivos** (~53%), após ~19 min
de CPU, com **2,69 milhões de símbolos** (~70/arq) e **2,5 milhões de arestas de
chamada**, gerando um DB de 2,4 GB + 1,2 GB de WAL — antes mesmo do resolve.

**Por que o C é o muro (e por que o L1 deixa de ser luxo):**
- **C é 30× mais denso em disco** (55 KB/arq vs 1,8 KB do sintético) e **11× mais
  lento de indexar** (18 vs 207 arq/s): macros + headers geram dezenas de
  símbolos por arquivo.
- **Resolução por nome é patológica em C** (sem namespaces): os alvos de chamada
  mais frequentes são macros/funções ubíquas — `dev_err` ×35.395, `BIT` ×34.123,
  `ARRAY_SIZE` ×31.517, `kfree` ×20.074. Sem L1, o `resolve_edges` casa esses
  nomes por texto e o fan-out explode.
- **É exatamente o caso que o L1/clangd resolve** — arestas `certain` semânticas
  em vez de adivinhação por nome. Mas clangd não está ativo nesta máquina, então
  o kernel bate no muro. **Conclusão: em C-escala, L1 não é qualidade, é
  requisito de viabilidade.**

## Veredito de escala

- **Código bem-estruturado (namespaced) escala limpo até 100k+** no modelo
  "índice único + watcher quente": ~8 min, 324 MB, sem OOM.
- **Dois tetos reais, medidos, não escondidos:** (1) a varredura de frescor O(N)
  chega a ~5s/miss a 100k — precisa de camadas acima de ~30k; (2) C denso em
  escala (kernel) exige L1 ativo para não explodir por fan-out de nomes.
- **Não validado como pronto para monorepo de 100k+ em C sem L1.** Indexação
  incremental/parcial e throttle da varredura de frescor são o próximo trabalho
  de escala. Números honestos > alegação de SOTA.
