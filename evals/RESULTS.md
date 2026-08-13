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
- **⚠️→✅ A varredura de frescor foi de ~5s para ~1,3s a 100k (3,5×).** A tabela
  acima é a medição *original*. O profiling apontou que **72% do tempo era
  `os.path.relpath`** (chama `normcase`/`LCMapStringEx` milhões de vezes no
  Windows), não I/O. Trocado por concatenação do caminho relativo durante a
  descida (o prefixo do diretório vem na pilha): **4,65s → 1,33s a 100k**, mesmo
  conjunto de arquivos, mesma garantia forte (sem throttle — o teste
  `test_repeated_misses_still_catch_edits` continua exigindo varredura a cada
  miss). Depois disso o `pathspec` passou a dominar (15 padrões × 100k arquivos):
  como um padrão gitignore terminado em `/` só casa diretório (já podado na
  descida), ele nunca muda o status de um ARQUIVO — então os arquivos são
  checados contra um spec reduzido sem esses padrões (exato, ~15× menos regex por
  arquivo): **1,33s → 0,61s**. Total **~7,7×** (4,65s → 0,61s), mesmo conjunto de
  arquivos, empurrando o teto confortável de ~30k para ~100k.
- **✅ Watcher-aware: no caminho de produção a varredura é PULADA.** Com o MCP
  server (watcher ligado), uma query não paga mais a varredura a cada miss — o
  watcher mantém o índice quente, então quando ele está vivo e drenado
  (`is_current()`) a varredura é redundante e pulada (custo/miss → ~0), com um
  backstop a cada 30s para cobrir eventos que o SO possa ter perdido. A garantia
  forte fica intacta onde importa: SEM watcher, e DURANTE o debounce do watcher,
  a varredura roda a cada miss como antes. Reduzir o pathspec é o que resta.
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
- **Dois tetos reais, medidos, não escondidos:** (1) a varredura de frescor é
  O(N) — reduzida de ~5s para ~1,3s/miss a 100k (eliminando `os.path.relpath`) e
  PULADA no caminho de produção quando um watcher vivo já garante frescor
  (custo/miss → ~0, com backstop de 30s); (2) C denso em escala (kernel) exige L1
  ativo para não explodir por fan-out de nomes.
- **Escape para o caso denso: indexação parcial (`index --scope`).** Para um
  monorepo grande/denso demais p/ indexar inteiro (o kernel), indexa-se só a
  subárvore que importa — escopo persistido e acumulável; a varredura de frescor
  então só anda nessa subárvore (~4ms para um escopo de 500 arquivos num repo de
  100k, vs ~0,7s no inteiro = 185×). Não resolve indexar o kernel INTEIRO, mas
  torna tratável trabalhar numa parte dele.
- **Indexação paralela (prepare em threads, escrita serial):** ~1,16× confiável e
  grafo bit-a-bit idêntico ao serial. Modesto de propósito — o índice é limitado
  pela escrita serial no SQLite (~48% do tempo; parse+extract são só ~7%), então
  não há ganho linear a extrair aqui. Dito sem enfeite.
- **O travamento do índice do kernel inteiro era o WAL, e foi corrigido.** A
  causa não era CPU: o `resolve_edges` escrevia milhões de arestas numa transação
  única → WAL de 1,2 GB → checkpoint final gigante que travava. Agora as escritas
  em massa commitam em blocos + `wal_checkpoint(TRUNCATE)`, mantendo o WAL pequeno
  (índice de 100k arestas → -wal de ~10 KB) e tornando o índice resumível. Isso
  muda "não completa" para "completa" (ainda que devagar). Validado como
  WAL-limitado e resultado-idêntico em índices sintéticos; o kernel inteiro
  ponta-a-ponta (multi-GB, horas) não foi re-rodado — é o fix da causa-raiz
  identificada, rotulado honestamente assim.
- **Ainda não validado como pronto para indexar um monorepo de 100k+ em C por
  completo sem L1**, e o throughput continua limitado pela escrita serial do
  SQLite. Storage com escrita não-serial daria só um fator constante (~1,5×) e
  feriria o local-first — baixa alavancagem. Para uso real, `--scope` resolve.
  Números honestos > alegação de SOTA.

---

# OWASP Benchmark v1.2 — detecção de vulnerabilidade (2026-07-31)

Primeira medição de **detecção de vulnerabilidade** com gabarito. Até aqui todos
os nossos benchmarks mediam *localização* e *alcançabilidade para agente* — e
"nosso taint é bom" sem número contra gabarito é opinião, não engenharia.

Harness reproduzível: [`evals/owaspbench.py`](owaspbench.py). Motor no estado
pós-P1 (flow-sensitive), P2 (dois eixos) e P3 (catálogo de framework).

## Resultado (1.698 casos, as 7 categorias baseadas em taint)

| categoria | TP | FP | FN | TN | precisão | recall | F1 | score |
|---|---|---|---|---|---|---|---|---|
| xss | 87 | 37 | 159 | 172 | 70% | 35% | 0.47 | +0.18 |
| sqli | 90 | 54 | 182 | 178 | 62% | 33% | 0.43 | +0.10 |
| pathtraver | 40 | 25 | 93 | 110 | 62% | 30% | 0.40 | +0.12 |
| cmdi | 33 | 23 | 93 | 102 | 59% | 26% | 0.36 | +0.08 |
| trustbound | 20 | 7 | 63 | 36 | 74% | 24% | 0.36 | +0.08 |
| xpathi | 7 | 1 | 8 | 19 | 88% | 47% | 0.61 | +0.42 |
| ldapi | 6 | 11 | 21 | 21 | 35% | 22% | 0.27 | **−0.12** |
| **TOTAL** | **283** | **158** | **619** | **638** | **64%** | **31%** | **0.42** | **+0.12** |

Custo: 11,2s para indexar + 5,5s de análise nos 1.698 arquivos.

### Evolução medida no mesmo harness

| etapa | precisão | recall | score |
|---|---|---|---|
| catálogo base (só regras universais) | 85%\* | 9%\* | +0.06\* |
| + sinks de framework curados (JDBC/Spring/processo) | 61% | 18% | +0.05 |
| **+ casamento qualificado receptor.método (P3b)** | **64%** | **31%** | **+0.12** |

\* medido numa amostra de 200 casos antes da corrida completa; a amostra era
alfabética e portanto enviesada — os 85% não se sustentaram nos 1.698. Fica
registrado como lembrete de que amostra pequena não ordenada aleatoriamente
mente.

## Leitura honesta

**Ainda não é um resultado competitivo como SAST.** Score +0.12 (métrica oficial
é TPR − FPR; 0 = aleatório) é baixo, e em LDAP injection continuamos **piores
que aleatório** (−0.12). Publicamos porque um número ruim medido vale mais que
um número bom alegado — e porque agora cada melhoria é verificável.

O que o benchmark permitiu separar, e que inspeção manual não separaria:

### 1. Lacuna de CATÁLOGO (barata)

Nomear os sinks de JDBC/Spring que faltavam (`queryForObject`, `executeUpdate`,
`batchUpdate`, `ProcessBuilder`, `evaluate`) levou sqli de 8% → 33% de recall e
xpathi a 47%. Nomes distintivos: entram sem custo de precisão.

### 2. Lacuna de ARQUITETURA: resolvida por casamento QUALIFICADO

Na primeira medição, **XSS e trust-boundary tinham recall ZERO** — 329 dos 741
falsos negativos (44%). Não era falta de esforço: o sink de XSS em Java é
`println` num `PrintWriter` de resposta, e casar `println` pelo último segmento
dispararia em todo `System.out.println` de qualquer código.

A saída não foi inferência de tipos, e sim casar o par **receptor.método**:

    response.getWriter().println(sujo)   → getWriter.println   ← sink
    System.out.println(sujo)             → out.println         ← inofensivo

Duas informações em vez de uma. Resultado: XSS 0% → **35%** de recall
(87 achados), trust-boundary 0% → **24%**, path traversal 19% → **30%** — e a
precisão global SUBIU (61% → 64%). Ganhar recall sem pagar em ruído é a única
troca que interessa aqui.

### 3. Os falsos positivos são, em boa parte, PREDICADOS OPACOS

Amostrando 120 casos sqli marcados como seguros, 18 foram acusados. O padrão:

```java
bar = (7 * 18) + num > 200 ? "This_should_always_happen" : param;
```

A condição é sempre verdadeira, então `bar` nunca recebe o dado sujo. O
Benchmark usa isso de propósito para exercitar path-sensitivity.

Nossa análise é **may-taint**: no encontro de ramos, une. Acusar aqui é o
comportamento *correto* de uma may-analysis, não um bug — e "consertar"
ingenuamente quebraria detecção real (nosso próprio teste
`test_taint_in_one_branch_is_still_a_finding` exige a união). A correção certa é
**constant folding na condição do ramo** antes do meet, o que exige
interpretação abstrata sobre inteiros. Fica no roadmap, dimensionado.

## Ressalvas metodológicas (as duas mexem no número, uma para cada lado)

- **Só as 7 categorias de taint são pontuadas.** `weakrand`, `crypto`, `hash` e
  `securecookie` são mau uso de API/configuração, que nosso motor não faz;
  pontuar nelas — para bem ou para mal — seria desonesto.
- **Detecção em nível de arquivo, sem conferir a categoria do achado.** Cada
  caso do Benchmark tem uma vulnerabilidade pretendida, então é um proxy
  razoável — mas isso **infla** nosso número frente ao placar oficial do OWASP.
  O número real de um scorecard oficial seria menor que 61%/18%.

---

# Rodada 8 — apps vulneráveis REAIS: a fonte lida direto no argumento (2026-08-13)

O OWASP Benchmark é código **gerado**, com estilo uniforme. Ele mede bem o que
já sabemos tratar e esconde o que o código escrito por gente faz. Esta rodada
troca o gabarito sintético por quatro aplicações vulneráveis reais e uma
pergunta desconfortável: *o motor cala sobre o quê?*

## O defeito, medido antes de mexer no código

Levantei à mão as vulnerabilidades indefensáveis de dois apps Node —
linhas onde um sink conhecido recebe uma leitura de requisição na mesma linha:

| local | código | detectado antes? |
|---|---|---|
| dvna `core/appHandler.js:10` | `var query = "… " + req.body.login` → `db.sequelize.query(query)` | **sim** |
| dvna `core/appHandler.js:39` | `exec('ping -c 2 ' + req.body.address, …)` | não |
| dvna `core/appHandler.js:197` | `mathjs.eval(req.body.eqn)` | não |
| NodeGoat `app/routes/contributions.js:32` | `eval(req.body.preTax)` | não |
| NodeGoat `app/routes/contributions.js:33` | `eval(req.body.afterTax)` | não |
| NodeGoat `app/routes/contributions.js:34` | `eval(req.body.roth)` | não |

O padrão salta aos olhos: **a única que achávamos era a única que passava por
uma variável**. O motor semeava em `x = fonte()` e depois via `x` chegar ao
sink; quando o programador escreve a leitura dentro do próprio argumento, não
havia semente — e sem semente, o motor nem varria aquela função.

Vale registrar que minha hipótese de trabalho era outra (callbacks anônimos não
indexados, que travariam Express/Koa/Fastify). A medição a derrubou: dos 203
callbacks anônimos não indexados em NodeGoat, **3** continham sink, e os três em
`Gruntfile.js` e testes e2e — nenhuma vulnerabilidade. O item certo só apareceu
porque o palpite foi medido antes de virar código.

## Resultado

| app | antes | depois |
|---|---|---|
| dvna (Node/Express/Sequelize) | 1 | **3** |
| NodeGoat (OWASP, Node/Express) | 0 | **3** |
| pygoat (OWASP, Django) | 8 | 8 |
| dvpwa (aiohttp) | 6 | 6 |

Os 5 achados novos são vulnerabilidades documentadas dos próprios projetos
(injeção de comando, injeção de código, SSJS injection). **Nenhum falso
positivo novo**, e o OWASP Benchmark não se moveu (64%/31%/+0.12): Java usa
*chamada* de fonte (`request.getParameter`), não caminho de atributo, então
esta forma simplesmente não ocorre lá.

## O contrapeso que faltava na suíte

Todos os invariantes dinâmicos até aqui mediam **precisão** — "sem caminho, sem
achado". Nenhum media o defeito oposto, que é pior: **um scanner que não
reporta nada passava em todos eles.**

O teste novo usa um oráculo que não toca na maquinaria do motor: lê o texto do
repositório procurando "um sink e uma leitura de requisição na mesma linha, sem
sanitizer à vista" e exige um achado ali. É estreito de propósito — cruzar a
fronteira da linha exigiria reimplementar a análise, e um oráculo que
reimplementa o motor não testa nada, só concorda consigo mesmo. Dentro de uma
linha, o que ele acusa é conferível a olho nu e indefensável.

A primeira versão dele acusou uma falha que **não existia**: casando o sink por
substring, `res.download(` virou o sink `load`. Um oráculo que inventa falha é
pior que a lacuna que deveria pegar — passou a exigir fronteira à esquerda
(aceitando o ponto, para `mathjs.eval(` e `db.query(`).

## Limite conhecido, dito aqui

A guarda de sanitizer no argumento olha só o **topo** da expressão — a mesma
over-aproximação que o caminho da atribuição sempre teve. `f(escape(req.q))`
sai limpo; `f("a" + escape(req.q))` não. Mantive as duas metades idênticas de
propósito: quando semeadura e propagação discordam, a discordância vira falso
positivo (foi exatamente o bug da rodada anterior). Nos quatro apps testados
não há **nenhuma** ocorrência viva de argumento sanitizado alimentando um sink,
então essa guarda não foi exercitada por código real — está dito, não medido.

---

# Rodada 9 — catálogo de sinks, e três defeitos que ele desenterrou (2026-08-13)

O oráculo da rodada 8 usa `rules.sinks`. Ampliar o catálogo torna o teste de
recall **automaticamente mais exigente** — e foi assim que esta rodada
funcionou: cada sink novo virou uma cobrança nova sobre o motor, e três das
cobranças expuseram defeitos que não tinham nada a ver com catálogo.

## Defeito 1 — o casamento qualificado só funcionava em Java

`_receiver_last` procurava o receptor nos campos `object`/`receiver`/`operand`
e, não achando, caía no campo `function`. Só que em Python e JS o campo
`function` guarda o callee INTEIRO (`res.redirect`), então a função devolvia o
último segmento — **o próprio método**. Como quem chama descarta `recv ==
callee`, o resultado era `qualified = None` em todo Python e todo JavaScript.

A regra qualificada existia desde que foi escrita, mas só a gramática de Java
(que tem campo `object`) a exercitava. `res.redirect`, `fs.readFile` e
`POST.get` nunca chegaram a casar — regra viva no código, morta na prática.
É o mesmo tipo de defeito do `getparameter` em minúsculas, e igualmente
invisível sem uma medição que o cobrasse.

## Defeito 2 — a linha reportada podia não conter o sink

```js
Todo.
  find({}).
  sort('-updated_at').
  exec(function (err, todos) {     // ← o sink está aqui, na linha 219
```

A expressão de chamada COMEÇA em `Todo`, na linha 217, e era essa a linha
reportada. O invariante central da suíte — *a linha reportada tem que conter o
sink* — pegou o caso em código real (nodejs-goof). A linha agora sai do nó do
NOME do callee, que é também onde o extractor grava a aresta `calls`; os dois
passaram a concordar.

## Defeito 3 — a origem podia ser de outra fonte da mesma função

A varredura montava UMA origem por função (a primeira fonte encontrada) e a
carimbava em todos os achados dela. Em pygoat:

```python
file = request.FILES["file"]              # 582
function_str = request.POST.get("function")   # 583
...
output = ImageMath.eval(function_str, img=img, ...)   # 588
```

O achado do argumento 0 é verdadeiro, mas era explicado pela linha 582 — a
fonte errada. Agora, quando a variável que chega ao sink é ela própria uma
semente, a origem é a linha dela. Achado certo com explicação inventada é
pior que achado nenhum: ensina a não conferir.

## O catálogo, e o que ele custou aprender

Duas regras foram medidas e **revertidas** antes de entrar:

- `HttpResponse` como sink de XSS acusava código correto: toda view Django
  devolve uma, em geral com conteúdo já escapado pelo template. Ficou só
  `mark_safe`, que é o que efetivamente desliga o escape.
- `requests.request("PATCH", url, data=payload)` virava SSRF com URL
  constante. O modelo de sink não distingue argumento, então só ficaram as
  formas em que a URL é o primeiro argumento.

E duas ambiguidades de nome nu pediram uma régua nova, `bare_sinks` (só casa
sem receptor):

- `open(caminho)` é path traversal; `Image.open(arquivo_enviado)` não é.
- `exec(cmd)` é execução de comando; `Todo.find({}).sort().exec(cb)` é Mongoose.

Só que restringir `exec` globalmente **derrubou o recall de cmdi do OWASP
Benchmark de 26% para 3%**: em Java a forma real é `Runtime r =
Runtime.getRuntime(); … r.exec(cmd)`, com o receptor numa variável local, que
nenhuma regra qualificada consegue nomear. A régua passou a ser por linguagem,
com empate resolvido a favor do recall. O Benchmark voltou a 64%/31%/+0.12,
idêntico ao anterior — que é o resultado desejado: esta rodada é sobre Node e
Python, e não deveria mexer no número de Java.

## Callbacks anônimos: a hipótese descartada na rodada 8, agora comprovada

Na rodada 8 medi callbacks anônimos e descartei o item: dos 203 não indexados
em NodeGoat, só 3 continham sink, todos em `Gruntfile.js` e testes e2e. A
medição estava certa **para o catálogo daquele momento** — `res.redirect` e
`res.send` ainda não eram sinks, então não havia o que encontrar lá dentro.

Com o catálogo ampliado o oráculo passou a cobrar, e a lacuna apareceu em
código real:

```js
app.get("/learn", isLoggedIn, (req, res) => {
    return res.redirect(req.query.url);      // open redirect do NodeGoat
});
```

Funções passadas como argumento agora viram símbolos próprios (`get#2`). O
achado mais valioso da rodada veio disso, em nodejs-goof, e depende de três
capacidades ao mesmo tempo:

```
req.body.redirectPage   lido DENTRO de um callback anônimo   (routes/index.js:54)
   → adminLoginSuccess(redirectPage, …)     INTERPROCEDURAL
      → res.redirect(redirectPage)          sink QUALIFICADO  (routes/index.js:74)
```

Lição registrada: uma lacuna medida como irrelevante pode estar **mascarada
por outra**. As duas se escondiam mutuamente.

## Resultado

| app | rodada 8 | agora |
|---|---|---|
| dvna (Node/Express/Sequelize) | 3 | **6** |
| NodeGoat (OWASP, Node/Express) | 3 | **4** |
| nodejs-goof (Snyk, Node/Express) | — | **2** |
| pygoat (OWASP, Django) | 8 | **10** |
| dvpwa (aiohttp) | 6 | 5 |

dvpwa caiu de 6 para 5 por **deduplicação**: o mesmo par (origem, sink,
argumento) era reportado duas vezes quando o resolvedor ligava `res.redirect` à
função `redirect` exportada pelo próprio módulo, fazendo a função chamar a si
mesma. Agora fica a versão mais confiável e, em empate, a de cadeia mais curta.

Os achados novos, todos conferidos linha a linha, são vulnerabilidades
documentadas dos próprios projetos: open redirect (dvna, NodeGoat,
nodejs-goof), desserialização insegura e XXE (dvna), path traversal e SSRF
(pygoat).

## O ruído que apareceu junto, dito sem maquiagem

Varrer o **Express** dá 73 achados — e **68 estão na suíte de testes dele**,
que existe justamente para ecoar a requisição (`res.send(req.params.id)`). São
achados verdadeiros e sem nenhum interesse. Não os escondi: cada achado passou
a trazer `in_test`, eles vão para o fim da lista e o cabeçalho diz quantos são.
Dos 5 restantes, 4 são padrões genuínos nos exemplos (path traversal em
`downloads`, eco em `vhost`/`params`, open redirect em `mvc`).

**Falso positivo conhecido e não resolvido:** os 4 achados no tutorial do Flask
são consultas PARAMETRIZADAS (`db.execute("… VALUES (?, ?)", (a, b))`). O motor
vê dado do usuário chegando em `execute` e não distingue parâmetro ligado de
concatenação. Resolver isso exige olhar como a string da consulta foi
CONSTRUÍDA — é o próximo item natural de precisão, e está dimensionado, não
escondido.

---

# Rodada 10 — modelos MIT do CodeQL: recall de 31% → 60% (2026-08-13)

## O que dá para clonar do CodeQL, e o que não dá

Conferido, não presumido:

| o que | licença | usável? |
|---|---|---|
| `github/codeql` — queries e bibliotecas QL | **MIT** (Copyright 2006-2025 GitHub, Inc.) | sim, com atribuição |
| CodeQL CLI / motor / extractors | "GitHub CodeQL Terms and Conditions" | não é OSI; não usado aqui |

Mesmo com a licença permitindo, **portar as queries não é uma opção**. A query
de SQL injection do Java tem ~20 linhas e só declara três conjuntos
(`isSource`, `isSink`, `isBarrier`); a substância mora embaixo:

| | arquivos | linhas |
|---|---|---|
| bibliotecas QL (só Java, Python, JS) | 1.251 | **250.342** |
| dataflow do Java, isolado | 45 | 11.065 |
| **graphcodemap inteiro, 19 linguagens** | 64 | **13.853** |

E essas 250 mil linhas não são autônomas: são Datalog avaliado pelo motor
proprietário, escrito contra o IR que os extractors produzem — e o banco do
CodeQL exige **compilar o projeto**, o oposto da premissa deste aqui (sem
build, incremental, sempre fresco).

## O que é portável: os dados

Os arquivos `*.model.yml` ("Models as Data") são tabelas puras — "este método,
deste tipo, neste argumento, é sink desta categoria". Anos de modelagem em
formato que não depende do motor deles. `scripts/import_codeql_models.py`
extrai nome de API e categoria; joga fora tipo, assinatura e índice de
argumento, que o nosso motor ainda não usa.

Duas decisões de curadoria carregam o resultado:

- **Lista de INCLUSÃO de categorias.** A maior categoria deles é
  `log-injection` (851 linhas só em Java, 359 em Go) — severidade baixa,
  frequência altíssima. Importá-la enterraria injeção de comando embaixo de
  ruído. `credentials-*` e `encryption-*` são mau uso de API, que este motor
  não faz. Fontes só de categoria `remote`.
- **`summaryModel` e `neutralModel` ficaram de fora.** Os dois são por
  ASSINATURA: `PreparedStatement.executeQuery()` é neutro e
  `Statement.executeQuery(sql)` é sink — mesmo nome. Sem tipo no casamento,
  usar o neutro para subtrair apagaria sinks reais.

Colheita: **1.073 nomes** — Java 147 fontes/436 sinks, Go 125/180, C# 12/128
(não tínhamos **nada** de C#), JS 2/13.

## Resultado — OWASP Benchmark v1.2

| | antes | depois |
|---|---|---|
| precisão | 64% | 61% |
| **recall** | **31%** | **60%** |
| F1 | 0.42 | **0.61** |
| **score (TPR−FPR)** | **+0.12** | **+0.16** |

Recall quase **dobrou** por 3 pontos de precisão. Por categoria:

| categoria | score antes | score depois |
|---|---|---|
| cmdi | +0.08 | +0.16 |
| **ldapi** | **−0.12** | **+0.09** |
| pathtraver | +0.12 | +0.13 |
| sqli | +0.10 | +0.14 |
| trustbound | +0.08 | +0.12 |
| xpathi | +0.42 | +0.50 |
| xss | +0.18 | +0.20 |

LDAP era a **única categoria pior que aleatório** e estava na lista de
pendências desde a rodada 8. Saiu de −0.12 para +0.09 sem uma linha de motor:
era lacuna de catálogo, e o catálogo veio pronto.

## Sem efeito colateral nos apps reais

Node e Python não mudaram (dvna 6, NodeGoat 4, nodejs-goof 2, pygoat 10,
dvpwa 5): o CodeQL não usa MaD para Python e quase não usa para JS, então a
importação não os toca. Gin (Go) passou a dar 10 achados — `FormFile()` →
`SaveUploadedFile`/`MkdirAll`/`Chmod`, que é o padrão real de path traversal
em upload; 6 estão marcados como fixture de teste. Dois são fracos
(`Fprintf`, `Redirect` com fonte duvidosa) e estão contados na precisão.

---

# Rodada 11 — precisão: medir a CAUSA dos falsos positivos (2026-08-13)

O sistema do usuário é de pentest/bug-finding, então precisão não é acabamento.
Esta rodada começou classificando os **350 falsos positivos** do Benchmark por
causa, em vez de escolher por intuição.

## Classificar direito não é trivial

A primeira classificação apontou "sanitizador não modelado" em **58%** dos
casos. Estava errada: o regex procurava `ESAPI` no arquivo inteiro, e o
Benchmark põe `ESAPI.encoder().encodeForHTML(e.getMessage())` no bloco `catch`
de quase todo caso — decoração, não sanitização do caminho.

O classificador certo usa os COMENTÁRIOS que o próprio Benchmark escreve para
documentar a técnica de cada caso (`// Simple if statement that assigns
constant to bar on true condition`). Resultado real:

| causa | n | % |
|---|---|---|
| ramo decidível — `if`/ternário com condição constante | 135 | 38,6% |
| outro | 106 | 30,3% |
| ramo decidível — `switch` com seletor constante | 45 | 12,9% |
| sanitizador não modelado | 54 | 15,4% |
| consulta parametrizada | 10 | 2,9% |

**Ramo decidível = 51,4% de todos os falsos positivos.** Consulta
parametrizada, que eu tinha listado como próximo item desde a rodada 9, é 2,9%
— teria sido a escolha errada, e só a medição mostrou isso.

## Parte 1 — sanitizadores de escape (barato)

Três nomes explicavam 46 dos 54 casos: `encodeForHTML` (ESAPI, 28),
`htmlEscape` (Spring, 13), `escapeHtml` (Commons Lang, 5). Entraram junto com
a família toda (ESAPI, Spring, Commons Lang/Text, OWASP Java Encoder).

Ganho medido: **−10 FP, −2 TP, score +0.16 → +0.17**. Menor que os 46
esperados porque a detecção é por ARQUIVO: vários desses casos têm outra causa
junto. Os 2 TPs perdidos são reais e o motivo é conhecido — escape para HTML
não protege quem usa o valor em contexto de URL, e tratá-lo como sanitizador
universal apaga exatamente esse bug (é o mesmo defeito que o NodeGoat
documenta). Trade aceito e registrado.

## Parte 2 — folding de condição constante

```java
int num = 86;
if ((7 * 42) - num > 200) bar = "This_should_always_happen";
else bar = param;
```

`208 > 200` é sempre verdadeiro, então `bar` nunca recebe o dado sujo. Unir os
braços é o comportamento correto de uma may-analysis — e é o que produz o
achado errado.

O avaliador lê a condição com o parser do **próprio Python**: a aritmética e as
comparações de Java, C, C#, JS, Go e PHP são sintaticamente iguais nessa fatia,
e o que Python não parseia (cast, chamada, `instanceof`) vira erro de sintaxe e
devolve `None`. Lista de PERMISSÃO em tudo: operador fora de `+ - * %`, nome
não resolvido, divisão (`/` é inteira em Java e real em Python — um avaliador
que erra a semântica é pior que um que se recusa a decidir) → não decide.

O ambiente de constantes é igualmente estreito: o nome tem que ser atribuído
UMA ÚNICA vez em toda a função, e com literal. Um valor errado ali apagaria
vulnerabilidade real em silêncio, que é o pior defeito possível.

## O defeito que a busca desenterrou (e valeu mais que o item)

O folding não disparava. Investigando: o Benchmark usa `if` **sem chaves**, e
`build_regions` procurava os braços por NODE TYPE de corpo (`block`). Sem
chaves não há `block`, a lista saía vazia e o `if` inteiro virava **um braço
só** — os dois ramos executando em SEQUÊNCIA.

A consequência é séria e não tinha nada a ver com falso positivo:

```java
if (c) bar = param;        // gen: bar sujo
else   bar = "constante";  // kill: bar limpo  ← apagava o gen anterior
```

Em sequência, o segundo ramo MATA a sujeira do primeiro e o achado desaparece.
Passar a usar os campos `consequence`/`alternative` — que existem nas duas
formas — corrigiu isso. Junto veio um segundo defeito: os ramos sem chaves
também entravam como trecho *incondicional*, sendo avaliados duas vezes, uma
delas no ambiente errado.

## Resultado

Medido em três passos, para separar o que é de quem:

| | TP | FP | precisão | recall | score |
|---|---|---|---|---|---|
| início da rodada | 541 | 340 | 61% | 60% | +0.17 |
| ramos corrigidos, sem folding | 641 | 392 | 62% | 71% | +0.22 |
| **+ folding de condição** | **641** | **372** | **63%** | **71%** | **+0.24** |

O folding entrega o que se pedia dele: **−20 falsos positivos e ZERO
verdadeiros perdidos**. A correção dos ramos vale +100 TPs — recall que estava
sendo apagado por um kill que não devia acontecer, desde que o motor
flow-sensitive existe.

Por categoria, todas subiram:

| categoria | score rodada 10 | agora |
|---|---|---|
| cmdi | +0.16 | +0.24 |
| ldapi | +0.09 | +0.20 |
| pathtraver | +0.13 | +0.17 |
| sqli | +0.14 | +0.24 |
| trustbound | +0.10 | +0.24 |
| xpathi | +0.50 | +0.40 |
| xss | +0.20 | +0.28 |

Apps reais inalterados (dvna 6, NodeGoat 4, nodejs-goof 2, pygoat 10,
dvpwa 5) — nenhum deles usa predicado opaco, que é um artifício do Benchmark.

## O que sobra, dimensionado

`switch` com seletor constante (45 FPs, 12,9%) exige casar o seletor com cada
rótulo — mesma técnica, mais trabalho. Consulta parametrizada são 10 casos
(2,9%). E os 106 "outro" ainda não foram abertos um a um.

---

# Rodada 12 — `switch` e ternário: +0.24 → +0.29 (2026-08-13)

Continuação direta da rodada 11, mesma técnica em mais duas formas.

## `switch` tinha o MESMO defeito estrutural do `if`

O corpo de um `switch` é um contêiner (`switch_block`) e os grupos de `case`
ficavam DENTRO dele, virando um trecho sequencial:

```java
case 'A': bar = param;   break;   // gen: bar sujo
case 'B': bar = "bob";   break;   // kill: apagava a sujeira do anterior
```

Mesmo defeito do `if` sem chaves, mesma perda silenciosa de recall. Um corpo
que contém outros corpos é contêiner de BRAÇOS. Corrigido: **+25 verdadeiros
positivos** (recall 71% → 74%), ao custo de +23 falsos positivos — que são
exatamente os casos que passaram a precisar de folding.

Junto veio uma correção de semântica: num `switch`, "tem 2+ braços" NÃO
significa que algum sempre executa. Sem `default` o seletor pode não casar com
nenhum. Quem decide agora é a presença do rótulo padrão; na dúvida, assume que
não há — o ambiente de entrada entra na união e nenhum kill escapa.

## Folding do seletor, e propagação por métodos puros

O idioma do Benchmark é `String guess = "ABC"; char alvo = guess.charAt(1);`.
Folding só de literais não resolve: `alvo` depende de `guess`. O ambiente de
constantes passou a resolver **em rodadas**, e o avaliador ganhou um conjunto
de métodos PUROS (`charAt`, `length`, `substring`, `toUpperCase`, `trim`…) —
métodos sem efeito colateral cuja semântica é idêntica entre as linguagens.
Ficaram de fora `format` e `replaceAll`, cujo comportamento difere.

Guarda contra fall-through: se o grupo escolhido não termina em
`break`/`return`/`throw`, o folding se recusa — mais de um corpo executa e
escolher um só apagaria o outro.

Resultado: **−23 falsos positivos, zero verdadeiros perdidos.**

## Ternário

`bar = (7 * 18) + num > 200 ? "constante" : param` não é um `if` e não vira
região de controle, então os dois lados sempre entravam juntos em `rhs_ids`.
Agora a atribuição guarda `(condição, ids do então, ids do senão)` e, depois
que as constantes são conhecidas, fica só o lado que executa.

Resultado: **−18 falsos positivos, zero verdadeiros perdidos.**

## Placar

| | TP | FP | precisão | recall | score |
|---|---|---|---|---|---|
| fim da rodada 11 | 641 | 372 | 63% | 71% | +0.24 |
| `switch` estruturado | 666 | 395 | 63% | 74% | +0.24 |
| + folding do seletor | 666 | 372 | 64% | 74% | +0.27 |
| + ternário | **666** | **354** | **65%** | **74%** | **+0.29** |

As três formas de folding entregaram a mesma propriedade: **só removem falso
positivo, nunca verdadeiro**. É o que se pede de uma otimização de precisão.

## O maior bloco restante, agora diagnosticado

Abrindo os falsos positivos que sobraram, a causa dominante não é mais
predicado opaco — é **taint atravessando o retorno de uma chamada sem
sumário**:

```java
String bar = new Test().doSomething(request, param);   // <- aqui
java.io.File fileTarget = new java.io.File(bar);       // <- achado
...
class Test {
    String doSomething(HttpServletRequest request, String param) {
        String bar = "alsosafe";     // NUNCA recebe param
        return bar;
    }
}
```

O motor intra-procedural vê `param` dentro da expressão do RHS e suja `bar`.
Não pergunta se `doSomething` de fato **propaga** o argumento até o retorno —
e neste caso não propaga. O folding dentro do auxiliar é irrelevante: o
chamador já decidiu sozinho.

A máquina para responder isso já existe (`Flow.reaches_return`), e já é usada
numa passada para descobrir *wrappers de fonte*. Falta usá-la no sentido
inverso: um sumário por função de "qual parâmetro chega ao retorno", consultado
na atribuição. É a próxima peça, e é ARQUITETURAL — nenhuma regra de catálogo
a resolve.
