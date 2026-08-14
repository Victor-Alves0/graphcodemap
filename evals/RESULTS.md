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

---

# Rodada 13 — sumário de retorno: TENTADO E REJEITADO (2026-08-13)

Registro de resultado negativo, porque ele vale tanto quanto os positivos.

## A hipótese

Diagnosticada na rodada 12: `x = f(sujo)` suja `x` sem perguntar se `f` devolve
o argumento. A máquina para responder já existia (`Flow.reaches_return`), usada
numa passada para achar *wrappers de fonte*. Bastaria usá-la no sentido inverso.

## O que foi construído

- Passada prévia calculando, por função, se algum parâmetro alcança o `return`.
- Resolução do callee do RHS por **FQN**, não por nome: o Benchmark tem 1.698
  métodos `doSomething` diferentes, e decidir pelo nome ou não decide nada (uma
  definição que propaga contamina todas) ou decide errado. A resolução preferiu
  a definição do MESMO ARQUIVO quando ela é única.
- Guardas: só funções COM parâmetros (senão `sb.toString()` seria morto, e ele
  devolve dado derivado do receptor), e wrappers de fonte fora.

## Por que foi rejeitado

| | TP | FP | precisão | recall | score |
|---|---|---|---|---|---|
| sem sumário | 666 | 354 | 65% | 74% | +0.29 |
| com sumário | 557 | **0** | **100%** | 62% | +0.62 |

O score dobra. E ainda assim não entra, por três motivos:

1. **Custa 109 verdadeiros positivos** — 12% do recall — por um motivo que não
   consegui isolar. Para uma ferramenta de bug-finding, perda de recall
   inexplicada é o pior defeito possível: some vulnerabilidade real, em
   silêncio, sem nada no relatório indicando que sumiu.
2. **Zero falso positivo em 796 casos seguros não é resultado, é alarme.**
   Nenhuma análise estática real acerta 796 de 796. O número seria bonito de
   publicar e eu não confiaria nele.
3. O mecanismo é **quase inerte em código real** — nos cinco apps vulneráveis
   ele mudou exatamente um achado — e **decisivo no Benchmark**, porque o
   artifício de segurança do Benchmark é justamente esse método auxiliar. Um
   ganho que só aparece no gabarito sintético é um ganho no gabarito, não no
   produto.

O único efeito em código real foi *correto*: removeu
`sqli/dao/review.py:36` do dvpwa, que é uma consulta PARAMETRIZADA
(`INSERT … VALUES (%(course_id)s, %(review_text)s)` com dict de params) —
falso positivo nosso, e por acaso um dos 10 casos daquela categoria.

## O que fica

A ideia está certa e é o que o `summaryModel` do CodeQL faz. O que falta não é
a análise, é a **resolução**: saber a qual definição uma chamada se refere exige
tipo, não nome. Isso é trabalho da camada L1 (LSP), que já existe no projeto
para outras consultas e ainda não alimenta o motor de taint. Enquanto essa
ponte não existir, o sumário decide sobre o alvo errado — e decidir errado aqui
apaga vulnerabilidade.

Revertido. A `_df_resolve_call` com filtro por nome (que desempata
`new Test().doSomething(x)`, onde a linha tem a aresta do `new` para a classe e
a do método) foi revertida junto por não ter mais uso.

---

# Rodada 14 — PHP e o argumento que importa (2026-08-13)

Os dois itens que faltavam da lista, com um defeito estrutural no meio.

## Só o argumento 0 é a consulta

`cur.execute(q, params)` com `q` literal e placeholders é a forma SEGURA de
consultar: o dado do usuário vai em `params` justamente para NÃO ser
interpretado como SQL. Sujeira chegando no argumento 1 não é injeção — é o
mecanismo que a impede. O motor acusava quem acertou.

A regra certa não era analisar como a string foi construída (que era a minha
hipótese desde a rodada 9), e sim restringir o sink ao argumento onde o perigo
mora. Os modelos do CodeQL trazem esse índice em cada linha (`Argument[0]`); nós
o descartamos na importação porque o motor ainda não o usava.

Resultado em app vulnerável real — **dvpwa: 5 achados → 1**, e o que sobrou é a
única injeção verdadeira do projeto:

```python
q = ("INSERT INTO students (name) VALUES ('%(name)s')" % {'name': name})
await cur.execute(q)                      # interpolação, não binding
```

Os quatro removidos eram todos parametrizados
(`cur.execute('… WHERE username = %s', (username,))`). O tutorial do Flask caiu
de 5 para 1 pelo mesmo motivo — era o falso positivo declarado na rodada 9.
**dvpwa e Flask passaram a ter 100% de precisão.**

## PHP: 0 → 51 num app vulnerável real

DVWA tem 102 `$_POST` e 99 `$_GET`, e o motor achava **zero**. Três causas
empilhadas:

1. **Superglobal não é caminho com receptor.** `$_GET['id']` produz o caminho
   `("_GET",)`, de UM segmento, e a regra de fonte de framework exigia dois
   (`req` + `query`). Nomes como `_GET` são seguros de casar nus — nenhuma
   outra linguagem tem variável com esse nome. `_SERVER` ficou de fora: metade
   dele é cabeçalho do usuário e metade é configuração do servidor, e sem
   distinguir a chave o resultado seria acusar todo `include` de app PHP.

2. **Não havia catálogo de PHP.** Nem o CodeQL publica MaD para PHP, nem o
   OpenTaint cobria. Levantado à mão a partir do que aparece no DVWA:
   `mysqli_query`, `shell_exec`, `unserialize`, `move_uploaded_file`,
   `file_get_contents`, `header`… e os sanitizers (`htmlspecialchars`,
   `mysqli_real_escape_string`, `escapeshellarg`, `intval`…).

3. **Código de nível de ARQUIVO era invisível.** Este é o defeito estrutural, e
   vale para qualquer linguagem de script. A varredura itera símbolos de função;
   em PHP o código perigoso mora fora de qualquer função — o DVWA inteiro é
   assim:

   ```php
   if( isset( $_REQUEST[ 'Submit' ] ) ) {
       $id = $_REQUEST[ 'id' ];
       $query = "SELECT … WHERE user_id = '$id';";
       $result = mysqli_query($GLOBALS["___mysqli_ston"], $query);
   }
   ```

   Agora o símbolo de ARQUIVO também é analisado, com a raiz da árvore fazendo
   o papel de corpo. Como a extração já para nas funções aninhadas, não há
   contagem dupla.

O oráculo de recall, estendido para as superglobais, cobrou mais cinco casos
que ainda faltavam: `header("location: " . $_GET['redirect'])` e
`move_uploaded_file($_FILES['uploaded']['tmp_name'], …)`. A causa era a
coleta de fontes dentro do argumento só olhar nós de acesso a MEMBRO — e a
superglobal é uma folha, sem receptor. Corrigido: **51 achados**.

## Sem efeito colateral

| repo | antes | agora |
|---|---|---|
| DVWA (PHP) | 0 | **51** |
| dvpwa | 5 | **1** (4 eram parametrizados) |
| Flask | 5 | **1** (4 eram parametrizados) |
| dvna / NodeGoat / nodejs-goof / pygoat | 6 / 4 / 2 / 10 | iguais |
| Express / gin | 73 / 10 | iguais |
| OWASP Benchmark | +0.29 | **+0.29** |

O Benchmark não se moveu, como esperado: é Java, sem superglobais e sem código
de nível de arquivo.

---

# Rodada 15 — Ruby/Rails, e o que os números significam (2026-08-13)

## Ruby: 0 → 2 no RailsGoat

Medido antes de escrever regra: o RailsGoat usa `params[:x]` **62 vezes**
contra 5 de `params.require`. O idioma dominante é justamente o ambíguo —
`params` também é nome de variável comum em Python e JS (o próprio dvpwa tem um
dicionário chamado assim), então marcá-lo como fonte global criaria falso
positivo nas outras linguagens.

A saída foi resolver **na extração**, que é o único ponto onde a linguagem já é
conhecida sem espalhá-la por todo o motor: `_LANG_BARE_SOURCES` declara fontes
que só valem naquela gramática, e a atribuição carrega o resultado num campo
(`rhs_framework_source`).

Os dois achados são vulnerabilidades documentadas do projeto:

```ruby
# controllers/benefit_forms_controller.rb:25  →  models/benefits.rb:15
file = params[:benefits][:upload]
  … system("cp #{full_file_name} #{data_path}/bak…_#{file.original_filename}")

# controllers/password_resets_controller.rb:6
user = Marshal.load(Base64.decode64(params[:user]))
```

O primeiro é **interprocedural**, atravessando controller → model.

## O que os números significam

Vale fixar, porque "recall alto" sozinho não quer dizer nada.

| medida | fórmula | lê-se |
|---|---|---|
| **TPR / recall** | TP / (TP + FN) | das vulnerabilidades REAIS, quantas achei |
| **FPR** | FP / (FP + TN) | do código SEGURO, quanto acusei à toa |
| **precisão** | TP / (TP + FP) | do que reportei, quanto era verdade |
| **score** | TPR − FPR | distância da linha do chute aleatório |

Recall alto é bom **só se o FPR não subir junto**. Uma ferramenta que reporta
tudo tem recall 100% e FPR 100% — score **zero**, igual a chutar. Uma que não
reporta nada também dá zero. O score existe exatamente para punir os dois
extremos, e é por isso que ele é a métrica oficial do Benchmark.

Precisão é o que a pessoa SENTE ao ler o relatório; recall é o que ela não vê.
Para pentest os dois doem de formas diferentes: recall baixo deixa passar o bug,
precisão baixa faz a ferramenta ser ignorada.

## Onde estamos frente aos placares oficiais

Números publicados no artefato oficial
`scorecard/OWASP_Benchmark_Home.html` do checkout externo pinado do Benchmark;
o corpus não é vendorizado aqui. Portanto são do gabarito deles, não nossos:

| ferramenta | TPR | FPR | score |
|---|---|---|---|
| FindBugs + FindSecBugs v1.4.6 | 96,8% | 57,7% | **39,1%** |
| FindBugs + FindSecBugs v1.4.5 | 95,2% | 57,7% | 37,5% |
| SonarQube Java v3.14 | 50,4% | 17,0% | **33,3%** |
| SAST-06 (comercial, anônimo) | 85,0% | 52,1% | 32,9% |
| SAST-04 (comercial, anônimo) | 61,5% | 28,8% | 32,6% |
| SAST-02 (comercial, anônimo) | 56,1% | 25,5% | 30,6% |
| **graphcodemap (hoje)** | **74%** | **44%** | **29%** |
| SAST-03 (comercial, anônimo) | 46,3% | 21,4% | 24,9% |
| OWASP ZAP (DAST) 2016 | 20,0% | 0,1% | 19,8% |
| SAST-01 (comercial, anônimo) | 29,0% | 12,2% | 16,7% |
| SAST-05 (comercial, anônimo) | 47,7% | 29,0% | 18,7% |

**Duas ressalvas que puxam para lados opostos, e nenhuma delas é pequena:**

- **A favor deles:** os placares cobrem as **11** categorias; nós pontuamos
  só as **7** de taint. As outras quatro são mau uso de API/configuração, que
  este motor não faz.
- **A favor nosso, indevidamente:** medimos por ARQUIVO, sem conferir se a
  categoria do achado bate com a do gabarito. Um scorecard oficial nosso seria
  MENOR que 29%.

Ou seja: o +0,29 **não é comparável linha a linha** com a tabela acima. O que
dá para dizer com segurança é a ordem de grandeza — estamos na faixa das
ferramentas comerciais medianas, acima do SAST-01/03/05 e abaixo do FindSecBugs
e do SonarQube. Para um motor de 14 mil linhas sem build, é um lugar honesto.

**O que seria "excelente":** ninguém publicado passa de 39%. O FindSecBugs
chega lá com 96,8% de recall e 57,7% de FPR — mais da metade do código seguro
acusado, que na prática é insuportável de usar. O SonarQube faz 33,3% com FPR
de 17%, que é o perfil mais utilizável da lista. **O alvo bom para nós não é
maximizar o score, é chegar perto do perfil do SonarQube: recall na casa dos
70% com FPR abaixo de 20%.** Hoje o recall já está lá; o FPR é o que falta.

---

# Rodada 16 — a ponte L1 → taint, e o gargalo que ela revelou (2026-08-13)

## O que foi construído

O sumário de retorno da rodada 13 foi rejeitado por um motivo específico:
resolvia a chamada por NOME e apagava 109 vulnerabilidades reais. A correção
não é analisar melhor — é **só decidir quando a resolução é confiável**.

O grafo já carrega essa informação: cada aresta `calls` tem uma `confidence`, e
`certain` significa resolvida SEMANTICAMENTE pela camada L1 (LSP). O sumário
agora só mata a propagação quando a chamada é `certain`. Sem L1 rodado, nada é
morto e o motor volta a over-aproximar — o lado seguro.

## O que entregou

**Segurança, comprovada:** rodando o mesmo repositório com e sem `refine`, os
achados são idênticos. Nenhuma vulnerabilidade some.

| app | sem L1 | com L1 | arestas promovidas |
|---|---|---|---|
| pygoat | 10 | 10 | 29 |
| dvpwa | 1 | 1 | 47 |
| dvna | 6 | 6 | 21 |

**E dispara** — não é código morto:

| app | funções que não propagam | atribuições com chamada `certain` | linhas mortas |
|---|---|---|---|
| pygoat | 35 | 13 | **3** |
| dvpwa | 195 | 3 | **1** |
| dvna | 33 | 0 | 0 |

## O que NÃO entregou, e por quê

O alvo era derrubar o FPR de 44% para a faixa dos 20%. **Não aconteceu**, e a
medição mostra exatamente onde trava: das centenas de atribuições com chamada
nesses repositórios, só **13** (pygoat) e **3** (dvpwa) têm resolução `certain`.
O jedi resolve o que alcança; o resto são chamadas a bibliotecas de terceiros e
despacho dinâmico.

**O gargalo não é o sumário — é a cobertura do L1.** E no OWASP Benchmark ele
nem pode disparar: os resolvers disponíveis nesta máquina são
`go, javascript, python, rust, tsx, typescript`. Não há **jdtls**, então Java
não tem uma única aresta `certain`, e o Benchmark segue em **+0.29**, sem
mover uma casa.

## Leitura

Esta rodada não melhora número nenhum, e isso está dito no topo em vez de
escondido no rodapé. O que ela faz é trocar um mecanismo *perigoso* por um
mecanismo *correto e ocioso*, e medir por que ele fica ocioso.

O caminho para o FPR agora tem nome e ordem:

1. **Instalar/embarcar resolvers L1 para Java e PHP** (jdtls, intelephense).
   É o que transforma o sumário de ocioso em eficaz, e é pré-requisito de tudo
   o mais em precisão.
2. Ampliar a cobertura do L1 nas linguagens que já têm resolver — hoje a
   maioria das chamadas continua `inferred`.
3. Só então voltar às demais causas de falso positivo.

Trocar a ordem seria repetir o erro da rodada 13: otimizar precisão sobre uma
resolução em que não se pode confiar.

---

# Rodada 17 — consolidação da confiança sem perda de achados (2026-08-13)

## Hipótese testada

A definição devolvida por um language server não é automaticamente um alvo de
chamada. Parâmetros, variáveis, propriedades e funções vizinhas podiam ser
promovidos a `certain`; em JavaScript minificado isso fabricava autoarestas em
escala. A correção passou a exigir kind chamável, identidade de nome, posição
exata quando disponível e reutilização segura do alvo em colisões.

Foi criado um oráculo executável com callback, alias, propriedade de objeto,
função vizinha e object literal. O serviço TypeScript agora devolve
`line/column/kind/name`, e object literals ganham símbolos de método em vez de
serem confundidos com o contêiner.

## A/B em aplicações vulneráveis reais

Achados foram normalizados por origem, sink, caminho, linha, argumento e
evidência de fluxo. O mesmo checkout foi analisado antes e depois de L1.

| app | sem L1 | com L1 | idêntico | promoções | autoarestas certas suspeitas |
|---|---:|---:|---|---:|---:|
| pygoat | 10 | 10 | sim | 48 | 0 |
| dvpwa | 1 | 1 | sim | 656 | 0 |
| dvna | 6 | 6 | sim | 2 | 0 |
| NodeGoat | 4 | 4 | sim | 209 | 0 |
| nodejs-goof | 2 | 2 | sim | 8 | 0 |

No dvpwa existem 10 autoarestas `certain`, todas auditadas como recursão real
em `materialize.js` (`extend`, `P`, `set`, `create`, `isDateExact` e `wrap`), por
isso não entram na coluna de suspeitas. No NodeGoat, as **36 autoarestas
fabricadas caíram para zero**. Nenhuma aresta `certain` terminou em kind não
chamável nos cinco projetos.

## Outros invariantes fechados

- JSON da visualização não permite breakout de `</script>`.
- Recalculo lazy de PageRank e comunidades usa uma transação consistente e
  retry de lock; um writer concorrente não perde a marca de dirty.
- Arquivos já indexados são removidos quando uma nova política de ignore passa
  a excluí-los.
- O comando `capabilities` separa adapter wired, live smoke, real-repo e
  evidência externa de segurança; cobertura de parser não vira mais paridade.

## Leitura

Esta rodada não tenta aumentar recall. Ela remove confiança inventada e prova
que isso não esconde as 23 vulnerabilidades encontradas nos cinco aplicativos.
O próximo ganho legítimo depende de medição comparável por categoria e de
fechar o Tier A definido em `docs/ROADMAP.md`.

---

# Rodada 18 — mesma régua, categoria correta e primeiro confronto completo (2026-08-13)

## O contrato

Foi criado um formato normalizado e versionado para GraphCodeMap, SARIF
(CodeQL/OpenTaint) e JSON do OpenGrep. Cada execução carrega versão da
ferramenta, commit do alvo, categoria/CWE, origem, sink, status, tempo, pico de
memória da árvore de processos, warnings e erros. `failed`, `partial` e
`unavailable` não viram execução bem-sucedida com zero achados.

O avaliador OWASP também foi corrigido: um alerta só é hit quando **arquivo e
categoria** coincidem com o gabarito. Antes, qualquer alerta no arquivo podia
receber crédito; por isso as rodadas anteriores permanecem históricas, mas não
são mais a linha de base.

## OWASP Benchmark v1.2

Mesmo checkout `f51bf36b8891`, 1.698 casos nas sete categorias de fluxo:

| ferramenta/configuração | status | TP | FP | FN | TN | precisão | recall | FPR | score | tempo | pico RSS |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| GraphCodeMap 0.1.0 | complete | 696 | 364 | 206 | 432 | 65,7% | 77,2% | 45,7% | **+0,314** | 43,29 s | 551 MB |
| OpenTaint `dev-7f7da63`, 323 regras de fluxo | complete | 819 | 454 | 83 | 342 | 64,3% | 90,8% | 57,0% | **+0,338** | 240,34 s | 5.120 MB |
| OpenGrep 1.22.0, fixture Java de 28 regras | complete | 74 | 64 | 828 | 732 | 53,6% | 8,2% | 8,0% | **+0,002** | 59,13 s | 547 MB |
| CodeQL CLI 2.26.2 / `java-queries` 1.11.7 `default` (80 queries) | complete | 776 | 292 | 126 | 504 | 72,7% | 86,0% | 36,7% | **+0,494** | - | - |
| CodeQL CLI 2.26.2 / `java-queries` 1.11.7 `security-extended` (124 queries) | complete | 902 | 471 | 0 | 325 | 65,7% | 100% | 59,2% | **+0,408** | - | - |

OpenTaint vence o score por +0,023 e encontra 123 vulnerabilidades a mais, ao
custo de 90 falsos positivos adicionais, 5,6x o tempo e 9,3x o pico de
memória. GraphCodeMap não é o líder ainda. A linha OpenGrep vale somente para
o ruleset local de 28 regras usado: ela valida o adaptador e a reprodutibilidade,
não representa todo o ecossistema de regras OpenGrep. As linhas CodeQL foram
preenchidas depois com as suites oficiais no mesmo alvo fixado e um mapeamento
explícito de query ID para categoria: `default` cobre seis das sete categorias,
sem trust-boundary, enquanto `security-extended` cobre 7/7 e maximiza recall ao
custo de mais ruído. A comparação mede esta matriz, não a superioridade total
de produto sobre CodeQL.

### GraphCodeMap por categoria

| categoria | TP | FP | FN | TN | precisão | recall | score |
|---|---:|---:|---:|---:|---:|---:|---:|
| cmdi | 90 | 53 | 36 | 72 | 63% | 71% | +0,29 |
| ldapi | 22 | 19 | 5 | 13 | 54% | 81% | +0,22 |
| pathtraver | 61 | 47 | 72 | 88 | 56% | 46% | +0,11 |
| sqli | 225 | 114 | 47 | 118 | 66% | 83% | +0,34 |
| trustbound | 66 | 19 | 17 | 24 | 78% | 80% | +0,35 |
| xpathi | 13 | 9 | 2 | 11 | 59% | 87% | +0,42 |
| xss | 219 | 103 | 27 | 106 | 68% | 89% | +0,40 |

Os micro-goals agora são objetivos: primeiro `pathtraver`, com 46% de recall;
depois o FPR de SQLi/XSS. Não há justificativa numérica para
abrir outra linguagem ou outra superfície antes disso.

## Correção guiada pela medição

A primeira execução GraphCodeMap produziu 2.522 candidatos em 94,68 s. A
normalização revelou APIs Java de arquivo, resposta HTTP, sessão e processo
caindo em `unknown`; o adaptador agora preserva suas categorias reais.

Também revelou uma perda de tipo na importação dos modelos CodeQL:
`String.getBytes`, `MessageDigest.update` e `String.valueOf` haviam herdado
papéis que só são válidos para tipos específicos. Como o motor atual casa pelo
nome final, esses nomes genéricos geravam centenas de candidatos redundantes.
Eles foram excluídos apenas no catálogo Java, com teste de regressão.

Depois: 2.085 candidatos em ~43 s no índice quente, 55% menos tempo, sem custo
no score. Uma execução fria isolada, incluindo o índice de 5.603 arquivos,
levou 113,66 s e produziu os mesmos 2.085 achados. A mudança grande de recall
aparente (52,7% para 77,2%) vem da categoria correta,
não de vulnerabilidades novas; essa distinção é justamente por que o contrato
normalizado passou a existir.

## JDTLS real: promoção certa não é resumo de retorno certo

JDTLS 1.60.0 foi instalado com SHA-256 oficial verificado e executado no mesmo
projeto Maven. Resultado estrutural: **12.937 arestas promovidas**, 2.770
arquivos processados, duas raízes e zero erro do resolver.

A primeira execução revelou um defeito semântico importante: o taint tratava
"alvo da chamada certo" como "fluxo do retorno conhecido". Com interfaces,
despacho virtual, estado do receptor e campos, isso não é verdade. O score
subia artificialmente para +0,58/+0,61 ao zerar FP, mas o recall caía para
57,9%/60,5%, apagando vulnerabilidades reais. A otimização de retorno foi
desativada apenas para Java até existir oráculo dispatch-aware; as promoções L1
continuam no grafo.

Prova final: banco limpo L0 versus índice JDTLS, ambos com a guarda, têm os
mesmos **2.071 sinks exatos** e **1.922 pares arquivo+categoria** (Jaccard 1,0,
zero exclusivo dos dois lados). O L1 Java agora melhora a estrutura sem mudar
o conjunto de vulnerabilidades reportado.

---

# Rodada 19 — recall de path traversal e FPR abaixo de 30% (2026-08-13)

## Micro-goal 1: path traversal >= 70% de recall

Os 72 FNs foram decompostos por sink e formato de fluxo. O maior grupo já
chegava tainted ao construtor `java.io.File`, mas ele não estava no catálogo;
o motor reportava o XSS posterior com `fileTarget` e ignorava a materialização
do caminho. Adicionar o construtor como ponto verificável produziu:

| path traversal | antes | depois |
|---|---:|---:|
| TP | 61 | **110** |
| FP | 47 | 75 |
| FN | 72 | **23** |
| recall | 46% | **83%** |
| score | +0,11 | **+0,27** |

O ganho passou pelo gate de recall, embora o custo de 28 FPs exigisse a etapa
seguinte. Os 23 FNs restantes envolvem principalmente origem perdida em
iteração de cookies/coleções e não foram atacados no escuro.

## Micro-goal 2: FPR < 30% sem devolver TP

Os 392 FPs após o ganho foram classificados. Todos atravessavam wrappers
`doSomething(...)`; 189 eram controle local constante (`if`, ternário ou
`switch`) que a CFG já conseguia decidir, 166 envolviam listas/mapas/builders ou
reflexão, e 37 envolviam sanitização HTML contextual.

O resumo de retorno Java foi reativado somente para funções call-free ou com
um conjunto mínimo de operações puras sobre constantes. Qualquer coleção,
despacho, reflexão ou sanitizer continua conservador. Testes cobrem tanto o
branch constante seguro quanto a lista que deve continuar propagando.

| total OWASP (7 categorias) | antes | depois |
|---|---:|---:|
| TP | 745 | **745** |
| FP | 392 | **203** |
| FN | 157 | **157** |
| TN | 404 | **593** |
| precisão | 65,5% | **78,6%** |
| recall | 82,6% | **82,6%** |
| FPR | 49,2% | **25,5%** |
| score | +0,33 | **+0,571** |

Na mesma matriz, OpenTaint permanece com mais recall (90,8%), mas score +0,338,
FPR 57,0%, 240 s e 5,12 GB. GraphCodeMap agora lidera o score sem fingir que
encontra mais vulnerabilidades: perde 74 TPs para o concorrente e ganha no
controle de ruído/custo.

## Invariantes reais

O A/B foi repetido em cinco aplicativos vulneráveis, comparando origem, sink,
caminho, linha e argumento antes/depois de L1:

| app | antes | depois | idêntico | promoções | erros |
|---|---:|---:|---|---:|---:|
| pygoat | 10 | 10 | sim | 17 | 0 |
| dvpwa | 1 | 1 | sim | 566 | 0 |
| DVNA | 6 | 6 | sim | 0 | 0 |
| NodeGoat | 4 | 4 | sim | 209 | 0 |
| nodejs-goof | 2 | 2 | sim | 8 | 0 |

Nenhum dos 23 achados reais desapareceu. O próximo gate é FPR <20% com os
745 TPs congelados; depois, recuperar os 23 FNs de path traversal restantes.

---

# Rodada 20 — lista Java por índice constante e FPR abaixo de 20% (2026-08-14)

Os 203 FPs da rodada 19 foram reclassificados antes de alterar o motor. Todos
pertenciam a seis templates, agrupados em quatro famílias disjuntas:

| família restante na entrada | casos |
|---|---:|
| `ArrayList.add/remove/get` com índice constante | 66 |
| `Map.put/get` com chave constante e overwrite | 48 |
| despacho/reflexão com argumento final constante | 52 |
| sanitizador HTML contextual | 37 |

A primeira família tinha um oráculo particularmente forte: 66 wrappers seguros
e 65 vulneráveis quase idênticos. Depois de inserir `param` entre dois literais
e remover o índice zero, a única diferença relevante é `get(1)` (seguro) contra
`get(0)` (sujo).

O extrator passou a preservar o texto curto dos argumentos de chamada. Um
domínio abstrato Java fechado aceita somente listas criadas localmente e as
operações `add`, `remove` e `get` com índices inteiros constantes. Argumento
com identificador é possivelmente sujo; literal é limpo. Chamada, receptor,
índice dinâmico ou alias fora desse subconjunto aborta a prova e conserva o
comportamento anterior.

Antes do benchmark, a prova foi aplicada aos 1.698 casos elegíveis: todas as
65 versões vulneráveis continuaram `tainted`, e **zero** foi classificada como
limpa. Os 66 FPs previstos foram exatamente os 66 removidos na execução final.

| total OWASP (7 categorias) | rodada 19 | rodada 20 |
|---|---:|---:|
| TP | 745 | **745** |
| FP | 203 | **137** |
| FN | 157 | **157** |
| TN | 593 | **659** |
| precisão | 78,6% | **84,5%** |
| recall | 82,6% | **82,6%** |
| FPR | 25,5% | **17,2%** |
| score | +0,571 | **+0,654** |

| categoria | TP | FP antes | FP depois | recall | score depois |
|---|---:|---:|---:|---:|---:|
| command injection | 90 | 18 | **11** | 71% | +0,63 |
| LDAP injection | 22 | 10 | **6** | 81% | +0,63 |
| path traversal | 110 | 43 | **27** | 83% | +0,63 |
| SQL injection | 225 | 55 | **32** | 83% | +0,69 |
| trust boundary | 66 | 8 | **2** | 80% | +0,75 |
| XPath injection | 13 | 3 | **1** | 87% | +0,82 |
| XSS | 219 | 66 | **58** | 89% | +0,61 |

O relatório normalizado caiu de 1.863 para 1.732 traces; a pontuação por
arquivo+categoria caiu exatamente 66 FPs. Na execução pareada, o passe levou
44,87 s e pico RSS de 587,6 MB. Variação de tempo/memória não foi usada como
critério de aceitação.

Os cinco aplicativos reais foram executados novamente e preservaram a mesma
distribuição: pygoat 10, dvpwa 1, DVNA 6, NodeGoat 4 e nodejs-goof 2. A suíte
completa fechou em **1.411 passed, 24 skipped**.

O gate FPR <20% está concluído. O próximo micro-goal é recuperar pelo menos 10
dos 23 FNs de path traversal, levando a categoria a recall >=90% sem devolver o
FPR total acima de 20%.

---

# Rodada 21 — enhanced-for Java e recall de path traversal (2026-08-14)

Dos 23 FNs de path traversal, 16 liam `Cookie[]` vindo de `getCookies()` e
perdiam a origem em `for (Cookie cookie : cookies)`. A fonte do array já era
reconhecida; o extrator não normalizava o binding elemento←iterável.

Foi adicionado um fato sintético geral para `enhanced_for_statement`. Não se
marcou `getValue` como fonte: isso teria transformado qualquer getter homônimo
em input externo. Testes cobrem iterável sujo, iterável seguro e rebind seguro
depois de um loop sujo.

| total OWASP | rodada 20 | rodada 21 |
|---|---:|---:|
| TP | 745 | **807** |
| FP | 137 | **145** |
| FN | 157 | **95** |
| TN | 659 | **651** |
| precisão | 84,5% | **84,8%** |
| recall | 82,6% | **89,5%** |
| FPR | 17,2% | **18,2%** |
| score | +0,654 | **+0,712** |

Path traversal passou de 110/27/23/108 para **125/30/8/105**: recall 94%.
A semântica geral também recuperou TPs em command, LDAP, SQL, trust-boundary e
XPath. Os oito FPs novos são o custo honesto de propagar o elemento de um
iterável externo; o FPR total permaneceu abaixo do gate de 20%.

---

# Rodada 22 — Map/List fechado e path traversal 100% (2026-08-14)

O domínio de coleções Java foi estendido para uso tanto no summary de retorno
quanto no trace intra-procedural:

- List local: `add/remove/get` com índice constante;
- HashMap, LinkedHashMap e TreeMap locais: `put/get/remove` com chave literal;
- overwrite por chave e valor anterior devolvido por `put/remove`;
- resultado contaminado em assignment, return ou chamada aninhada;
- fail-closed em alias, escape, chave dinâmica, método desconhecido, overload
  não modelado, reinicialização e mutação condicional.

A revisão root adicionou um hardening que o OWASP não exigia: List criada fora
de um branch não pode ser considerada limpa por overwrite que ocorre apenas em
um braço ou loop. Construtor e operações precisam compartilhar o mesmo caminho
estrutural; loop ou branch aninhado/diferente aborta a prova. Dois testes
adversariais congelam esse comportamento. O hardening não alterou as métricas.

| total OWASP | rodada 21 | rodada 22 |
|---|---:|---:|
| TP | 807 | **868** |
| FP | 145 | **92** |
| FN | 95 | **34** |
| TN | 651 | **704** |
| precisão | 84,8% | **90,4%** |
| recall | 89,5% | **96,2%** |
| FPR | 18,2% | **11,6%** |
| score | +0,712 | **+0,847** |

| categoria | TP | FP | FN | TN | recall | score |
|---|---:|---:|---:|---:|---:|---:|
| command injection | 107 | 8 | 19 | 117 | 85% | +0,79 |
| LDAP injection | 27 | 2 | 0 | 30 | 100% | +0,94 |
| path traversal | **133** | 15 | **0** | 120 | **100%** | +0,89 |
| SQL injection | 272 | 19 | 0 | 213 | 100% | +0,92 |
| trust boundary | 77 | 0 | 6 | 43 | 93% | +0,93 |
| XPath injection | 15 | 1 | 0 | 19 | 100% | +0,95 |
| XSS | 237 | 47 | 9 | 162 | 96% | +0,74 |

Nenhum dos 807 TPs da entrada foi perdido e nenhum FP novo foi criado. Os 53
FPs removidos eram Maps locais; os 61 TPs recuperados se dividem em 32 Map e 29
List. A execução final hardened levou 43,59 s e 575,54 MB de pico RSS.

## Holdout independente: NIST Juliet Java 1.3 CWE-23

Para testar overfitting, o corpus oficial SARD suite #111 foi baixado fora do
workspace e verificado pelo SHA-256 publicado:
`d985f4177c2bcd7b03455a05c1c8f2e755f55c9eb250accd052f05f877347e60`.
`evals/julietbench.py` usa o manifesto para enumerar 444 casos vulneráveis e
pontua os 444 companions `good()` como negativos.

| corpus | TP | FP | FN | TN | precisão | recall | FPR | score |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GraphCodeMap / Juliet CWE-23 | 242 | 0 | 202 | 444 | **100%** | **54,5%** | **0%** | +0,545 |
| CodeQL `default` / DB manual | 222 | 6 | 222 | 438 | 97,37% | 50,0% | 1,35% | +0,486 |
| CodeQL `security-extended` / DB manual | 222 | 6 | 222 | 438 | 97,37% | 50,0% | 1,35% | +0,486 |

O resultado GraphCodeMap atualizado cruza o gate de recall >=50% mantendo FPR
zero. As suites CodeQL foram executadas sobre uma base manual com 732 fontes e
744 classes compiladas; ambas produziram os mesmos números e assinaturas
idênticas às da base anterior `no-build`. Manifesto, arquivos e classificação
dos endpoints `bad`/`good` são compartilhados. A proximidade não é usada para
alegar vitória total de produto em nenhuma direção.

Os cinco aplicativos reais não-Java preservaram os mesmos 23 achados e
categorias: pygoat 10, dvpwa 1, DVNA 6, NodeGoat 4 e nodejs-goof 2. A suíte
completa fechou em **1.432 passed, 24 skipped**.

---

# Rodada 23 — fontes Java por tipo e holdouts externos (2026-08-14)

O ganho no Juliet veio de fontes reais de entrada que estavam ausentes, não de
regras específicas para nomes de testcases. O extrator agora preserva o tipo
declarado do receptor para parâmetros, variáveis locais, resources,
enhanced-for e campos Java não ambíguos. Assim,
`BufferedReader.readLine`, `Console.readLine`, `DataInputStream.readLine`,
`LineNumberReader.readLine` e `RandomAccessFile.readLine` são fontes somente
quando o receptor tem o tipo esperado; um método de domínio chamado `readLine`
continua limpo. `System.getProperty` exige a qualificação explícita.

O scorer também passou a classificar o dispatch abstrato `action` da variante
81 pelo nome da classe concreta `_bad`/`_good`. Isso corrige a atribuição do
endpoint sem transformar a fixture em fonte ou sink.

| holdout Juliet CWE-23 | TP | FP | FN | TN | precisão | recall | FPR | score |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GraphCodeMap | 228 | 0 | 216 | 444 | **100%** | **51,4%** | **0%** | **+0,514** |
| CodeQL `default`, DB manual | 222 | 6 | 222 | 438 | 97,37% | 50,0% | 1,35% | +0,486 |
| CodeQL `security-extended`, DB manual | 222 | 6 | 222 | 438 | 97,37% | 50,0% | 1,35% | +0,486 |

Como segundo holdout, três CVEs Java foram selecionados em pares vulnerável/corrigido
e executados com bancos L0 isolados:

| par real | vulnerabilidade detectada | patch eliminou o fluxo | baseline |
|---|---:|---:|---|
| OpenRefine CVE-2024-49760 | sim | não | containment normalizado não modelado |
| FitNesse CVE-2024-42499 | não | não | fluxo por maps, fields e helpers perdido |
| openHAB CVE-2024-42468 | sim | não | containment canônico não modelado |
| **agregado** | **2/3** | **0/3** | **1 miss; 2 patches indistinguíveis** |

Essa linha de base é evidência de descoberta, mas ainda não de sensibilidade a
patch. Resultados dos guards de contenção em desenvolvimento ficam fora desta
rodada até a suíte completa e os benchmarks confirmarem que eles não escondem
fluxos vulneráveis.

---

# Rodada 24 — guards de contenção e qualidade de release (2026-08-14)

O motor agora reconhece apenas guards Java de path traversal com prova local e
fail-closed: `Path.normalize()/toAbsolutePath().startsWith(...)` ou
`getCanonicalPath().startsWith(base.getCanonicalPath() + File.separator)`, em
um `if` negado cujo braço de rejeição termina. Base contaminada, prefixo textual,
uso antes da validação, alias, helper/construtor aninhado e base canônica não
comprovada continuam reportados.

| par real | matches vulnerável | matches corrigido | resultado atual |
|---|---:|---:|---|
| openHAB CVE-2024-42468 | 3 | **0** | patch distinguido |
| OpenRefine CVE-2024-49760 | 2 | 3 | base herdada não comprovável em L0 |
| FitNesse CVE-2024-42499 | 0 | 0 | transporte interprocedural ainda ausente |

No openHAB, os findings totais caíram de 45 para 26 na versão corrigida. O
OpenRefine não foi forçado a passar: `_modulesByName` vem de uma superclasse em
dependência externa, e afirmar que o lookup devolve um módulo confiável sem
solver de tipos seria introduzir falso negativo. O FitNesse permanece como
caracterização explícita de seis gaps `xfail`, não como sucesso artificial.

Os gates não se moveram: OWASP ficou em **868 TP / 92 FP / 34 FN / 704 TN**
(score **+0,847**) e Juliet em **228 / 0 / 216 / 444**. A suíte completa fechou
em **1.470 passed, 24 skipped, 6 xfailed**.

A entrega também ganhou Ruff, mypy progressivo, branch coverage mínima de 75%,
matriz Linux/Windows em Python 3.10–3.12, build/twine e smoke do wheel instalado
em ambiente limpo. O warmup LSP passou a esperar preferencialmente por uma
referência qualificada cross-file; o teste real do `rust-analyzer`, antes
intermitente, passou cinco execuções consecutivas após a correção.

---

# Rodada 25 — transporte Java no mesmo receiver (2026-08-14)

O micro-goal partiu do miss real FitNesse CVE-2024-42499 e foi fechado como
semântica geral, sem nomes do corpus. Fontes `fitnesse.http.Request` exigem o
tipo qualificado; wrappers de fonte são guardados por FQN e resolvidos apenas
para o mesmo `this`, uma aresta única, ou o tipo concreto de
`new Type().wrapper()`. Fontes aninhadas em construtores/containers propagam
para o alvo da atribuição e respeitam sanitizers ancestrais.

O primeiro transporte de heap é deliberadamente estreito: campos de instância
diretos, sem shadow local, são capturados no ponto da chamada e entram apenas
num helper posterior da mesma classe e do mesmo receiver. Outro objeto, chamada
antes da escrita, overwrite limpo e ambiguidade de overload falham fechados.
Efeitos escritos pelo callee e devolvidos ao caller continuam fora do domínio e
permanecem em `xfail` estrito como próximo `HeapSummary`.

Uma revisão adversarial independente encontrou e bloqueou sete regressões antes
do fechamento: marcador de wrapper compartilhado por linha, ordem incorreta da
chamada no RHS, estado de `this` vazando para `new App()`, perda do marcador no
domínio List/Map e `java.io.*` não resolvido. Cada reprodução virou teste.

| gate | TP | FP | FN | TN | precisão | recall | FPR | score |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| OWASP Benchmark | 868 | 92 | 34 | 704 | 90,4% | 96,2% | 11,6% | +0,847 |
| NIST Juliet CWE-23 | 242 | 0 | 202 | 444 | 100% | 54,5% | 0% | +0,545 |

O OWASP ficou bit a bit nas métricas. No Juliet, o ganho 228 → 242 TP não veio
de reabrir nomes globais: o antigo resultado incluía colisões entre wrappers
homônimos; a rodada recupera os fluxos por tipo concreto e adiciona
`getQueryString` aninhado. A família `PropertiesFile` continua ausente até
existir prova de `Properties.load` no mesmo receiver, em vez de transformar
todo `Properties.getProperty` em fonte.

Nos pares reais, o FitNesse passa de miss para **2 matches vulneráveis**. A
revisão corrigida ainda conserva **1 match**: o containment canônico está dentro
do wrapper `composeFileName`, e o motor ainda não exporta um sumário
sanitizante desse helper. O agregado, portanto, é **3/3 vulnerabilidades
detectadas e 1/3 patches distinguidos**; o resultado não é promovido a
`detected-and-cleared` artificialmente.

O gate final fechou em **1.501 passed, 24 skipped, 3 xfailed**, Ruff e mypy
limpos, cobertura branch-aware total de **81%**, além de wheel/sdist aprovados
por `twine check`. O resultado reproduzível dos seis scans está em
`evals/java-real-pairs-round25-results.json`, com hash SHA-256 de cada relatório.

---

# Rodada 26 — contratos Java ponta a ponta e HeapSummary (2026-08-14)

A rodada não foi dirigida a um caso do benchmark. Cinco auditorias paralelas
partiram dos contratos do extrator L0, promoção L1/JDTLS, unidade de função,
fluxo sensível e incrementalidade até o relatório normalizado. Reproduções
adversariais viraram contratos antes da correção.

No L0, overloads ambíguos agora falham fechados e receivers locais com nome em
maiúscula não são fabricados como chamadas estáticas. No L1, posições são
convertidas corretamente entre bytes UTF-8 do tree-sitter e unidades UTF-16 do
LSP; seleção de definição preserva coluna, respostas/configuração e deadlines
têm contrato, e falhas de discovery/spawn ficam isoladas. O cache de fatos e o
lookup de função passaram a usar linha **e coluna**, impedindo duas unidades na
mesma linha de compartilharem facts.

Na incrementalidade, caminhos vindos de diff/query são contidos na raiz,
symlinks externos não entram, `mtime_ns` substitui segundos, queries não vazias
descobrem arquivos novos e proveniência L1 é invalidada antes da re-resolução
L0. Rename/delete preserva o impacto do snapshot antigo sem devolver uma aresta
`certain` que deixou de ser semanticamente válida.

O primeiro HeapSummary Java exporta efeitos dirty/clean de campos do mesmo
receiver. A contenção é propositalmente conservadora:

- kill virtual exige conjunto de despacho fechado comprovado;
- alias de `this` ou escape para mutator bloqueia kills;
- escrita em subcampo marca a raiz dirty;
- fan-out une todos os efeitos dirty e só preserva kill comum comprovado;
- fontes qualificadas aninhadas diretamente no sink continuam respeitando
  sanitizer;
- lambdas não invocadas são unidades deferred e não executam por acidente no
  método envolvente.

Também foi introduzido um catálogo declarativo de papéis de argumentos de sink.
Para Java, `Runtime.exec(command, envp, dir)` considera os índices `{0,1}`:
comando e ambiente continuam sendo risco, mas `File dir` não é command
injection. A origem `new File(source)` continua aparecendo como path traversal.
No OWASP isso removeu exatamente 36 FP de cmdi, sem alterar TP ou FN. O literal
`System.getProperty("user.dir")` passou a ser tratado como configuração confiável;
chaves dinâmicas e outros literais continuam fontes.

## Gates externos finais

| gate | TP | FP | FN | TN | precisão | recall | FPR | score |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| OWASP Benchmark v1.2 | 868 | 92 | 34 | 704 | 90,4% | 96,2% | 11,6% | +0,847 |
| NIST Juliet Java 1.3 CWE-23 | 308 | 0 | 136 | 444 | 100% | 69,4% | 0% | +0,694 |

O relatório OWASP final tem **1.942 findings** e **378 wrong-category**. O
resultado pontuado volta exatamente ao gate 868/92/34/704 depois do refinamento
semântico; o ganho é remover explicações redundantes/incorretas, não ajustar o
gabarito. O full run com refine levou **1.549,856 s**, pico de **1.680,26 MB**
na árvore de processos e promoveu **8.838** arestas em 2.770 arquivos, com zero
erro, usando JDTLS 1.60.0 e Oracle JDK 21.0.11. O rescore final no mesmo banco,
sem repetir refine, levou **73,258 s** e **640,51 MB**.
As duas regras finais são query-only: o rescore reutiliza exatamente o mesmo
banco/grafo construído pelo refine. Não existe nesta rodada uma única invocação
que combine custo end-to-end e métricas finais; por isso
1.549,856 s / 1.680,26 MB é publicado apenas como fase refine-inclusive, e
73,258 s / 640,51 MB como rescore final. Wall time end-to-end final combinado
não foi medido.

No holdout Juliet, 242 → **308 TP** recupera transporte interprocedural sem
adicionar FP; precisão permanece 100% e recall sobe de 54,5% para 69,4%. Essa
linha é independente do OWASP e continua expondo 136 FNs.

Nos pares reais, as três revisões vulneráveis permanecem detectadas:

| par | vulnerável → corrigido | resultado Round 26 |
|---|---:|---|
| FitNesse CVE-2024-42499 | 2 → 0 | patch distinguido pelo HeapSummary |
| openHAB CVE-2024-42468 | 3 → 0 | patch continua distinguido |
| OpenRefine CVE-2024-49760 | 2 → 3 | permanece aberto; base confiável herdada não é provada em L0 |
| **agregado** | **3/3 detectadas; 2/3 corrigidas limpas** | sem promover OpenRefine artificialmente |

## Reprodutibilidade e gate local

O [manifest incluído/versionado nesta rodada](round26-external-gates-manifest.json)
será adicionado com o commit e preserva os valores
necessários para reproduzir e auditar o snapshot; eles também são transcritos
aqui: commit-fonte
`da5b1610f15bb39ea4f13c0853a67fe105bbd83a`, diff de `src` SHA-256
`48361e2eaaad50748a4a697dee677a5d7264b538124e2d2f4367d018b0a9490f` e
arquivo JDTLS SHA-256
`e94c303d8198f977930803582738771fd18c52c5492878410bf222b1aa81ef1d`.
Os hashes finais são:

| artefato | SHA-256 |
|---|---|
| OWASP report | `d5bc5ee056a350fb17efdc901ba04a8a403a04c19a11092f7d3c1fc98496f111` |
| OWASP score | `146b279755db707d9a7d1f33683215bf09dae3a8a911a07029613ef9dc9d71e1` |
| Juliet report | `1a9206f37fca08eb9857a7d56a4538664a950b79795fd4ab905c8740c0cd2730` |
| Juliet score | `4f013deaea6566f2099fbe016cf44aa7bc1e03cd2750e9d74f43aebf20196d5b` |

O gate completo fechou em **158,60 s**, com **1.576 passed, 25 skipped,
1 xfailed**, **82% de cobertura total branch-aware**, Ruff e diff-check verdes.
O único `xfail` estrito caracteriza a lacuna global de
`System.setProperty("user.dir", valorTainted)` no próprio processo.

## Dívida residual

Os próximos micro-goals medidos são OpenRefine, lambdas Java realmente
invocadas, fan-out mais largo e fechamento de hierarquias de tipo. Os
**34 FN / 92 FP** restantes no OWASP e os 136 FN do Juliet continuam publicados.
Esta rodada melhora o resultado nesta matriz oficial e nos holdouts fixados; não
estabelece superioridade global sobre CodeQL, cujo conjunto de linguagens,
frameworks, queries e integrações operacionais é mais amplo que o experimento.
