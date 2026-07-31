# GraphCodeMap vs Graphify — análise honesta (2026-07-18, atualizado 2026-07-31)

> Nota (2026-07-31): documento vivo. As colunas "nós" abaixo foram atualizadas
> para o estado atual — o projeto agora se chama **GraphCodeMap**, tem 23
> extractors dedicados, visualização investigativa e dataflow em 18 linguagens.
> A análise do concorrente permanece a do levantamento original.

[Graphify](https://github.com/Graphify-Labs/graphify) (Graphify Labs, YC S26, ~90k stars, MIT)
é o projeto mais popular do espaço. Análise do que eles fazem melhor, do que
fazemos melhor, e do que falta para superá-los.

## Onde o Graphify está à frente

| Área | Graphify | GraphCodeMap hoje |
|---|---|---|
| Linguagens | ~40 (36 gramáticas tree-sitter + regex p/ 4) | **23 dedicadas** (18 código + web HTML/CSS + Terraform) + tier genérico p/ dezenas de gramáticas |
| Além de código | docs, PDFs, imagens, áudio/vídeo via LLM | só código (por tese) + Markdown/JSON/YAML/TOML |
| Comunidades | Leiden + labels por LLM (camada "domínio") | **temos** (Louvain próprio + labels L3 opcionais, invalidados por assinatura de membros); Leiden é refinamento futuro |
| Visualização | HTML interativo | **temos** — subgrafos de investigação semeados (neighborhood/callers/callees/impact/domains), arestas por confiança, destaque de git-diff; HTML autocontido |
| Time/CI | PR tools, triage, HTTP multi-user, merge driver | local-first single-user; `change-impact`/`affected-modules` já respondem a um diff |
| Ecossistema | 20+ plataformas, skills por IDE, 164 releases | MCP (20 tools) + CLI + lib |

## Onde o CodeGraph é tecnicamente superior

1. **Frescor com garantia no query time — o diferencial central.** Graphify
   sincroniza por `--update` manual e git hooks *pós-commit*: edições não
   commitadas — o estado real durante uma sessão de agente — são invisíveis
   ao grafo deles até alguém re-rodar. Nosso read-repair confere content-hash
   dos arquivos envolvidos EM CADA consulta e re-indexa antes de responder,
   com watcher + boot scan por cima. No problema nº 1 do domínio (staleness),
   somos estritamente mais fortes.
2. **Resolução semântica (L1).** Graphify declara "no full LSP-style type
   resolution". Nós promovemos arestas a `certain` via jedi (inferência de
   tipos real; chamada de método em instância resolvida). Confiança em 3
   níveis com semântica de *caminho* (impact propaga o mínimo).
2b. **Dataflow / taint (CPG-lite).** Graphify NÃO faz análise de fluxo de
   dados. Nós temos `dataflow` (para onde vão os parâmetros) e `taint`
   (fonte→sink de segurança, com sanitizers que cortam o fluxo, sources/sinks
   configuráveis em `.codegraph/taint.json`, modo `--entry` para revisar um
   handler): intra-procedural may-taint composto ao longo do call graph, sob
   demanda (sempre fresco), confiança herdada — o esqueleto de um Code Property
   Graph sem o custo whole-program do Joern, e incremental/fresco (que Joern
   não é). **18 linguagens dedicadas** (py, js/ts, java, c#, c/c++, go, rust,
   ruby, php, kotlin, swift, scala, lua, clojure) — paridade total com o
   grafo. Ver docs/RESEARCH.md §6.
3. **Honestidade epistêmica na resposta.** Ambos etiquetam arestas
   (EXTRACTED/INFERRED ≈ nosso inferred/possible), mas só nós declaramos
   incompletude na resposta ("análise estática — chamadas dinâmicas podem
   faltar; N não resolvidas") e servimos descrições STALE marcadas.
4. **Armazenamento.** SQLite WAL + FTS5 + transação por arquivo vs um
   `graph.json` (com merge driver para conflitos; a própria doc admite
   limites >5k nós na visualização). Escala e concorrência favorecem SQLite.
5. **Descrições L3 hash-invalidadas por símbolo**, sobrevivendo a re-index,
   lazy e hub-first — deles é extração de docs/mídia, não comportamento de
   código com invalidação.
6. **Benchmark da coisa certa.** Os números publicados deles (LOCOMO,
   LongMemEval) são benchmarks de *memória conversacional*, não de agente
   resolvendo tarefas de código. Nosso M6 mede o valor marginal do grafo
   para um agente de código — qualidade E tokens E tool calls contra
   baseline de busca agêntica.

## O que falta para superá-los de fato

Curto prazo (engenharia direta):
- ~~**Cobertura de linguagens**~~ **MUITO AVANÇADO** (2026-07-31): tier genérico
  cobre qualquer gramática; extractors DEDICADOS agora em **23 linguagens**
  (18 código — incl. Clojure/ClojureScript — mais web HTML/CSS/SCSS e
  Terraform/HCL), com fqn com escopo, herança, imports e calls no site do nome;
  dataflow/taint cobre as 18 de código. Cada linguagem dedicada tem um resolver
  L1/LSP conectado (7 validados ao vivo). Falta ainda ampliar além disso
  (Graphify ~40).
- ~~**Camada domínio**: detecção de comunidades~~ **FEITO** (2026-07-18):
  Louvain próprio sobre o grafo de símbolos (calls/imports/inherits),
  recompute lazy, `communities` na CLI/MCP; labels via L3 opcionais e
  preservados por assinatura de membros. Leiden (garante comunidades
  bem-conectadas) fica como refinamento futuro sobre a mesma interface.
- ~~**Visualização**: export JSON/HTML do grafo~~ **FEITO + EVOLUÍDO**
  (2026-07-31): de hairball para **ferramenta de investigação** — subgrafos
  semeados por query (neighborhood/callers/callees/impact/domains), filtros
  ortogonais (confiança mínima, linguagem, arquivos alterados no git), arestas
  direcionais estilizadas por confiança, anel vermelho=changed, anel
  branco=seed, toggles in-page; HTML autocontido ou `--json`.

Médio prazo (produto):
- ~~PR/CI tooling~~ **PARCIAL** (2026-07-31): `change-impact`/`affected-modules`
  já operam a partir de um diff; falta o wrapper de PR/CI propriamente dito.
- Multi-repo/HTTP para times.
- ~~Distribuição~~ **EM ANDAMENTO** (2026-07-31): documentação completa
  (`docs/`), CONTRIBUTING; publicação no PyPI e skills por IDE pendentes.

Não copiar: ingestão de PDF/mídia (fora da tese; SIFT/RAG cobrem isso melhor).

## Estado da arte?

- No **eixo frescor/correção** (staleness, confiança, honestidade): já
  estamos além do que qualquer sistema publicado que conhecemos faz — é
  arquitetura, não promessa; coberta por testes (gate anti-staleness).
- No **eixo amplitude/ecossistema**: Graphify está anos-luz à frente em
  adoção e superfícies; alcançável, mas é trabalho de produto, não pesquisa.
- **Alegação de SOTA honesta** exige números. Primeiro benchmark em dataset
  padrão rodado (2026-07-18): **SWE-bench-Lite, localização** (15 tarefas
  reais de flask/requests/pytest) — grafo **93% vs 80%** de acerto do arquivo
  a editar (RESULTS.md rodada 5). É sinal DIRECIONAL a favor da tese, não
  prova: n=15, +2 tarefas está dentro do ruído; e é localização, não resolução
  completa (patch+testes, que exige Docker, inviável aqui). Alegação pública
  forte pede escala (300 tarefas, 3× cada, repos grandes) e, idealmente,
  resolução completa. O piloto valida a harness (`evals/locbench.py`) e dá o
  primeiro dado real.
