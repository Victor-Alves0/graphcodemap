# CodeGraph — Pesquisa: Estado da Arte em Code Graphs para Agentes de IA

> Pesquisa realizada em 2026-07-17. Objetivo: mapear o que existe (academia + indústria),
> o que funciona, onde falha, e derivar os princípios de design do CodeGraph.

## 1. As três linhas de evolução

### 1.1 Academia (papers principais)

| Sistema | Venue | Ideia central | Resultado |
|---|---|---|---|
| **RepoGraph** ([arXiv 2410.14684](https://arxiv.org/abs/2410.14684), ICLR 2025) | Grafo def/ref em nível de linha via tree-sitter; plug-in para frameworks procedurais (Agentless) e agênticos; recuperação por ego-graph em torno de keywords | +32.8% relativo de resolve rate no SWE-bench Lite; SOTA open-source na época (29.67% com Agentless) |
| **CodexGraph** ([arXiv 2408.03910](https://arxiv.org/abs/2408.03910), NAACL 2025) | Grafo de símbolos (MODULE/CLASS/FUNCTION; CONTAINS/INHERITS/USES) num graph DB; o agente escreve queries em linguagem natural e um segundo LLM traduz para Cypher | Bom em CrossCodeEval/SWE-bench/EvoCodeBench; mostrou que query language livre funciona mas é frágil e caro (2 LLMs por consulta) |
| **LocAgent** ([arXiv 2503.09089](https://arxiv.org/abs/2503.09089), ACL 2025) | Grafo heterogêneo dirigido (files/classes/functions; import/invoke/inherit) + poucas tools de travessia multi-hop, focado em **localização** de código | 92.7% acc em localização de arquivo; +12% em resolução de issues; Qwen-32B fine-tunado ≈ modelos proprietários com −86% de custo |
| **Codebase-Memory** ([arXiv 2603.27277](https://arxiv.org/abs/2603.27277), 2026) | KG tree-sitter via MCP, 66 linguagens, call-graph, impact analysis, community discovery | **10x menos tokens, 2.1x menos tool calls — mas 83% de qualidade de resposta vs 92% da exploração de arquivos.** O grafo sozinho perde em qualidade. |
| **Code Isn't Memory** ([arXiv 2606.22417](https://arxiv.org/pdf/2606.22417), 2026) | Índice estrutural *dentro* do agente; updates incrementais por arquivo | Ganhos em tarefas cross-file; lição: separar navegação (grafo) de detalhe de implementação (ler o arquivo) |
| **Code Property Graph / Joern** (Yamaguchi et al., S&P 2014) | AST + CFG + PDG unificados num grafo só; SOTA para análise de segurança/vulnerabilidades | Referência para "grafo rico"; custo alto de construção, foco em análise, não em agentes |

Contexto adicional: [Awesome-Repo-Level-Code-Generation](https://github.com/YerbaPage/Awesome-Repo-Level-Code-Generation) (curadoria de papers), [survey de RAG repo-level](https://arxiv.org/pdf/2510.04905).

### 1.2 Indústria madura (code intelligence em escala)

- **[SCIP](https://sourcegraph.com/blog/announcing-scip)** (Sourcegraph): formato protobuf tipado para índices de código, ~8x menor que LSIF, **indexação incremental por arquivo** — só re-indexa o que mudou no push. Ecossistema de indexers por linguagem já existe ([scip-code/scip](https://github.com/scip-code/scip)).
- **[Glean](https://engineering.fb.com/2024/12/19/developer-tools/glean-open-source-code-indexing/)** (Meta, open-source): fatos tipados com schema (defs, refs, tipos, calls, herança) num DB queryável; consome SCIP via conversor.
- **Kythe** (Google): o ancestral — grafo de código cross-language desde 2008 (Grok).
- **Stack Graphs** (GitHub): resolução de nomes **incremental por arquivo** — cada arquivo produz um sub-grafo independente do resto do repo, e a resolução acontece na query. É o design mais importante para o nosso problema de staleness: mudar 1 arquivo invalida só o sub-grafo daquele arquivo.
- **Cursor** ([como indexa](https://read.engineerscodex.com/p/how-cursor-indexes-codebases-fast), [indexação segura](https://cursor.com/blog/secure-codebase-indexing)): **Merkle tree** de content-hashes para detectar mudanças; sync periódico (~5 min) re-processa só arquivos com hash divergente; chunking AST-aware via tree-sitter; embeddings cacheados por content-hash (−90% de re-upload).

### 1.3 Ferramentas para agentes (2025–2026)

- **[Serena MCP](https://github.com/oraios/serena)**: LSP real por baixo → `find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, edição simbólica (`replace_symbol_body`). 40+ linguagens. Prova que LSP-as-tools funciona e é token-eficiente. Fraqueza: startup/config de language servers por linguagem, sem grafo persistente (consulta ao vivo).
- **[Aider repo map](https://aider.chat/2023/10/22/repomap.html)**: grafo def/ref via tree-sitter + **PageRank personalizado** para ranquear símbolos por importância + busca binária para caber no budget de tokens. O insight: nem todo símbolo importa igual — função chamada por 20 lugares > helper privado.
- **[codegraph](https://agentconn.com/blog/codegraph-pre-indexed-knowledge-graph-multi-agent-claude-code-codex-2026/)** (trending #2 GitHub, 2026): tree-sitter → SQLite + FTS local-first; **file watchers nativos com debounce de 2s**; 9 tools MCP (symbol lookup, call-graph traversal, impact analysis). Claims: −59% tokens, −70% tool calls, −49% latência. Limitações admitidas: sem snapshots por commit, sem cross-repo, arestas só sintáticas.
- **[DeepWiki](https://docs.devin.ai/work-with-devin/deepwiki)** (Cognition) e [CodeWiki](https://arxiv.org/pdf/2510.24428): a camada de "descrições/comportamento" — documentação hierárquica gerada por LLM sobre a estrutura. É o que dá o "entendimento" que o grafo sintático não tem.

## 2. O contraponto: por que Claude Code usa grep e não índice

Evidência forte de que **grafo não substitui busca agêntica** — complementa:

- Cursor (chat), Claude Code e Devin usam grep/read iterativo como primário ([análise](https://www.mindstudio.ai/blog/is-rag-dead-what-ai-agents-use-instead)). A Augment relatou que [grep venceu embeddings](https://567-labs.github.io/systematically-improving-rag/talks/colin-rag-agents/) no agente deles de SWE-bench.
- Motivos: **zero index lag** (edita o arquivo, lê os bytes novos 100ms depois), zero infra, e o LLM se auto-corrige iterando.
- Codebase-Memory confirmou o limite: grafo dá eficiência (10x menos tokens) mas perde 9pp de qualidade vs ler arquivos — porque para *entender* código o agente precisa do código, não do resumo do grafo.

**Conclusão de design: o grafo ganha em multi-hop e visão global (quem chama isso? o que quebra se eu mudar isso? qual a arquitetura?), grep/read ganha em detalhe local. O CodeGraph deve ser a camada estrutural AO LADO de grep/read, nunca no lugar.**

## 3. Onde o estado da arte falha (nossas oportunidades)

### 3.1 Staleness (o risco nº 1, corretamente identificado)

Ninguém resolve completo. Peças do SOTA:
- Merkle/content-hash por arquivo (Cursor) — detecção determinística de mudança.
- File watcher + debounce (codegraph) — tempo real durante a sessão.
- Incrementalidade por arquivo (SCIP, stack graphs) — re-indexar 1 arquivo, não o repo.
- tree-sitter parsing incremental — re-parse em ms.

**Princípio que ninguém formula explicitamente e nós vamos adotar: o grafo é um CACHE DERIVADO do código, nunca uma fonte de verdade.** Consequências:
1. Grafo = função pura e determinística do conteúdo dos arquivos. Nunca editado diretamente; sempre reconstruível do zero.
2. Cada fato no grafo carrega o content-hash do arquivo de origem. Na query, hash divergente ⇒ re-indexa aquele arquivo *antes de responder* (read-repair) ou marca o fato como `stale` na resposta.
3. Camadas têm frescor diferente: estrutura (tree-sitter) é barata ⇒ sempre fresca; resolução LSP é média; descrições LLM são caras ⇒ podem ficar stale, mas **declaradas** como stale, nunca servidas como verdade.
4. Startup: diff de Merkle root ⇒ re-indexa só o delta (cobre mudanças feitas com o watcher desligado — git pull, branch switch, outro editor).

### 3.2 Honestidade epistêmica do grafo

Call graphs estáticos em linguagens dinâmicas têm recall baixo: [PyCG](https://arxiv.org/pdf/2103.00587) = 99.2% precisão mas ~70% recall em Python; em Android, [ferramentas estáticas perdem 61% dos métodos executados dinamicamente](https://arxiv.org/pdf/2407.07804) ([Total Recall?, ISSTA 2024](https://dl.acm.org/doi/10.1145/3650212.3652114)). Dispatch dinâmico, reflection, metaprogramação, DI containers — tudo invisível.

Nenhuma tool atual comunica isso ao agente. O agente confia numa lista de callers "completa" que não é, e conclui errado ("ninguém chama essa função, posso deletar").

**Nós vamos: (a) anotar arestas com confiança (`certain`/`inferred`/`possible`); (b) toda resposta de call-graph declara o limite ("análise estática; chamadas via reflection/dispatch dinâmico podem faltar"); (c) opcionalmente enriquecer com evidência dinâmica (traces de testes) no futuro.**

### 3.3 Interface: poucas tools > query language livre

CodexGraph (Cypher livre) funciona mas exige 2 LLMs e quebra fácil. LocAgent e Serena mostram que **poucas tools focadas com respostas compactas** vencem. Tools candidatas:

- `overview` — mapa ranqueado do repo (estilo Aider PageRank, com budget de tokens)
- `find_symbol` / `symbol_info` — definição, assinatura, doc, localização
- `callers` / `callees` — com confiança por aresta
- `references` — todos os usos
- `impact` — fecho transitivo de dependentes (o que pode quebrar se eu mudar X)
- `ego_graph` — vizinhança de um símbolo (estilo RepoGraph)
- `describe` — camada semântica (resumo LLM do módulo/função, com flag de frescor)

### 3.4 Camada semântica (descrições/comportamento)

DeepWiki/CodeWiki geram wiki, mas desacoplada do loop do agente e sem invalidação. Nossa versão: descrições LLM **ancoradas em nós do grafo, invalidadas por content-hash**, geradas lazy (na primeira consulta) ou em batch, hierárquicas (função → módulo → domínio, estilo comunidades do GraphRAG). É o diferencial de "behavior" que o usuário quer — e a parte mais cara, então: opcional, plugável em qualquer provider (model-agnostic), e sempre com metadado de proveniência.

## 4. Arquitetura recomendada (síntese)

**Camadas (cada uma útil sozinha, frescor decrescente):**
- **L0 — Estrutura**: tree-sitter → arquivos, símbolos, imports, def/ref léxico. Sempre fresco (ms por arquivo). Funciona em qualquer repo sem configuração.
- **L1 — Resolução precisa**: LSP/SCIP quando disponível, async, refina as arestas de L0 (duas velocidades: aproximado imediato, preciso eventual).
- **L2 — Grafo derivado**: call graph, herança, impacto, PageRank de importância. SQLite local-first (+ FTS), validado pelo codegraph.
- **L3 — Semântica**: descrições LLM por nó, hash-invalidadas, provider-agnostic.

**Sincronização**: content-hash Merkle por arquivo + watcher com debounce + rescan por diff no startup + read-repair na query. Meta: **é impossível o CodeGraph servir um fato sem saber se ele está fresco.**

**Entrega model-agnostic**: core como lib + CLI; MCP server como adaptador primeiro (padrão de facto — funciona em Claude Code, Cursor, Codex, etc.); integração com [SIFT](https://github.com/Victor-Alves0/SIFT) via `connect_mcp_stdio`, com a hierarquia natural `codegraph.symbols.find`, `codegraph.graph.impact`, etc.

**Avaliação** (para não nos enganarmos): baseline = agente com grep/read puro (não "sem contexto"). Métricas: qualidade de resposta E tokens E tool calls. Benchmarks: SWE-bench Lite/Verified, LocBench (do LocAgent), CrossCodeEval. O gap a fechar: os 9pp de qualidade do Codebase-Memory — hipótese: fecha quando o grafo é usado para *localizar* e o agente ainda lê o código localizado.

## 5. Anti-metas (o que NÃO fazer)

- Não substituir grep/read — complementar.
- Não expor query language livre como interface primária.
- Não servir descrições LLM sem proveniência/frescor.
- Não apresentar call graph estático como completo.
- Não exigir configuração por linguagem para o nível básico funcionar (tree-sitter primeiro, LSP como upgrade).
- Não depender de nuvem: local-first (SQLite), como codegraph.

---

## 6. Dataflow / taint — pesquisa e decisão (2026-07-18)

Objetivo do Victor: mapear como os dados fluem entre funções (uma função recebe
X, passa para outra), útil para **segurança** (input não-confiável → sink
perigoso) e **refatoração** (mudar um tipo → quem é afetado de verdade).

### 6.1 Estado da arte

| Abordagem | Referência | Ideia | Custo/ajuste ao nosso caso |
|---|---|---|---|
| **Code Property Graph (CPG)** | Yamaguchi et al., S&P 2014; [Joern](https://cpg.joern.io/) | AST + CFG + **DDG** (data dependence graph) unificados; taint = alcançabilidade fonte→sink no grafo | É a estrutura de referência. Construção cara, por-linguagem; Joern é whole-program, não incremental. Recentes ligam CPG+LLM ([LLMxCPG](https://arxiv.org/pdf/2507.16585), Codebadger MCP) |
| **IFDS/IDE** | Reps-Horwitz-Sagiv 1995; [FlowDroid], IDEDroid | Dataflow interprocedural preciso reduzido a alcançabilidade em grafo; distributivo sobre união | Preciso e **sound**, mas exige pointer analysis + whole-program + por-linguagem. Pesado demais para multi-linguagem/incremental/barato |
| **Semgrep taint mode** | [semgrep.dev](https://semgrep.dev/docs/writing-rules/data-flow/taint-mode/overview) | tree-sitter → IL agnóstica → taint **intra-procedural** (sources/propagators/sanitizers/sinks); interprocedural só no Pro | **O análogo mais próximo do nosso caso.** Prioriza velocidade/praticidade sobre soundness — mesma filosofia honesta que já adotamos |
| **PDG / def-use / reaching definitions** | clássico (Ferrante 1987); [Cornell CS4120] | Dependências de dados/controle intra-procedurais; def-use chains via reaching definitions; construção O(n)–O(n log n) | São os blocos de base. def-use intra-procedural encaixa **perfeitamente** no nosso index incremental por-arquivo |

### 6.2 Decisão de design (ajustada à nossa arquitetura)

Não reimplementar Joern/IFDS (whole-program, por-linguagem, não incremental,
caro — contra tudo o que priorizamos). Em vez disso, **construir um CPG de forma
incremental e pragmática (estilo Semgrep), com os nossos diferenciais** (frescor
em query-time, confiança honesta em 3 níveis):

1. **Intra-procedural primeiro** (a base O(n) por função): dentro do corpo de
   uma função, def-use — parâmetros e atribuições propagam "taint" até (a) os
   argumentos de chamadas e (b) o valor de retorno. É o que responde
   diretamente "esta função recebe X e passa para qual função".
2. **Interprocedural por composição com o call graph** que já temos (e já é
   resolvido por L1 + fresco): o alcance entre funções vem de compor os
   sumários intra-procedurais ao longo das arestas `calls`. A confiança da
   propagação herda a confiança da aresta de chamada (`certain`/`inferred`/
   `possible`) — honestidade epistêmica de graça.
3. **Query-time, não novo índice persistente**: o fluxo intra-procedural é
   barato e é computado sob demanda a partir do span da função (sempre fresco,
   nunca stale, zero inchaço de storage) — mesma lógica lazy do L3. Só o call
   graph, que já é persistido, é reusado.
4. **Taint = alcançabilidade** sobre esse grafo, de sources configuráveis a
   sinks, com sanitizers cortando o fluxo. Incompletude (reflexão, dinâmico)
   **declarada**, como já fazemos no call graph.

Isso nos dá a espinha de um CPG sem o custo de um, cabendo no local-first/
incremental/barato — e é diferencial forte: Joern não é incremental nem
fresco; Semgrep OSS é só intra-procedural; Graphify não faz dataflow nenhum.

Sources: [Joern CPG](https://cpg.joern.io/) · [Code property graph (Wikipedia)](https://en.wikipedia.org/wiki/Code_property_graph) · [IFDS Taint with Access Paths (arXiv 2103.16240)](https://arxiv.org/pdf/2103.16240) · [Semgrep taint mode](https://semgrep.dev/docs/writing-rules/data-flow/taint-mode/overview) · [Scaling IFDS to large C/C++ (ECOOP 2024)](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.ECOOP.2024.36) · [LLMxCPG (arXiv 2507.16585)](https://arxiv.org/pdf/2507.16585)
