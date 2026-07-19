# CodeGraph — Design v0.1: Schema do Grafo + Contrato das Tools

> Deriva de [RESEARCH.md](RESEARCH.md). Decisões aqui são o contrato; implementação vem depois.
> Status: proposta inicial (2026-07-17), aberta a revisão.

## 0. Princípios (invariantes de design)

1. **O código é a fonte de verdade; o grafo é cache derivado.** Todo fato no grafo carrega a proveniência (arquivo + content-hash) de onde foi derivado. O grafo inteiro é reconstruível do zero a qualquer momento, e nunca é editado diretamente.
2. **Nenhuma resposta sem checagem de frescor.** Toda query verifica os hashes dos arquivos envolvidos antes de responder; divergência dispara read-repair (re-parse L0, milissegundos) ou marcação explícita de `stale`.
3. **Honestidade epistêmica.** Arestas têm confiança (`certain`/`inferred`/`possible`); respostas de call-graph declaram os limites da análise estática. Nunca apresentar recall parcial como completo.
4. **Complementar, não substituir.** As tools localizam e navegam; o agente continua lendo o código com as ferramentas dele. Respostas apontam para spans (`path:linha`), não despejam corpos de função.
5. **Cada camada é útil sozinha.** L0 (tree-sitter) funciona em qualquer repo sem configuração; L1 (LSP), L3 (descrições LLM) são upgrades opcionais.

## 1. Modelo de dados

### 1.1 Identidade de símbolo

`symbol_id = hash(path, fqn, kind, ordinal)` — estilo moniker do SCIP.

- `fqn` = nome totalmente qualificado dentro do arquivo/módulo (ex.: `auth.TokenService.validate`).
- `ordinal` distingue overloads/redefinições com mesma fqn+kind no mesmo arquivo.
- Estável sob edições no corpo e mudanças de linha (spans não entram na identidade).
- Mover símbolo de arquivo **quebra a identidade** (vira delete+add). Aceito na v1; documentado.

### 1.2 Schema SQLite (L0–L2)

Local-first: `.codegraph/graph.db` (SQLite WAL + FTS5).

```sql
CREATE TABLE files (
  id           INTEGER PRIMARY KEY,
  path         TEXT UNIQUE NOT NULL,          -- relativo à raiz do repo
  language     TEXT,
  content_hash TEXT NOT NULL,                 -- xxhash64 do conteúdo
  size         INTEGER,
  mtime        INTEGER,
  parse_status TEXT NOT NULL DEFAULT 'ok'
               CHECK(parse_status IN ('ok','partial','failed')),  -- partial = ERROR nodes no tree-sitter
  indexed_at   INTEGER NOT NULL
);

CREATE TABLE symbols (
  id        TEXT PRIMARY KEY,                 -- hash(path, fqn, kind, ordinal)
  file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  parent_id TEXT REFERENCES symbols(id) ON DELETE CASCADE,  -- containment (classe→método)
  kind      TEXT NOT NULL,                    -- function|method|class|interface|struct|enum|variable|constant|module|type_alias
  name      TEXT NOT NULL,
  fqn       TEXT NOT NULL,
  signature TEXT,
  doc       TEXT,                             -- doc comment extraído
  start_line INTEGER, start_col INTEGER, end_line INTEGER, end_col INTEGER,
  body_hash TEXT NOT NULL,                    -- hash do texto do corpo (invalida L3)
  visibility TEXT,                            -- public|private|... quando a linguagem expressa
  rank      REAL NOT NULL DEFAULT 0           -- PageRank, recomputado lazy
);
CREATE INDEX idx_symbols_fqn  ON symbols(fqn);
CREATE INDEX idx_symbols_file ON symbols(file_id);
CREATE VIRTUAL TABLE symbols_fts USING fts5(name, fqn, doc, content='symbols', content_rowid='rowid');

CREATE TABLE edges (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,                   -- calls|imports|inherits|implements|references|reads|writes
  src        TEXT REFERENCES symbols(id) ON DELETE CASCADE,
  dst        TEXT REFERENCES symbols(id) ON DELETE SET NULL,  -- NULL = não resolvido/alvo sumiu
  dst_name   TEXT NOT NULL,                   -- alvo textual, SEMPRE preenchido (permite re-resolução)
  file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,  -- arquivo do site da referência (dono da aresta)
  line       INTEGER,
  confidence TEXT NOT NULL CHECK(confidence IN ('certain','inferred','possible')),
  resolver   TEXT NOT NULL CHECK(resolver IN ('l0','l1'))
);
CREATE INDEX idx_edges_src ON edges(src, kind);
CREATE INDEX idx_edges_dst ON edges(dst, kind);
CREATE INDEX idx_edges_dangling ON edges(dst_name) WHERE dst IS NULL;

CREATE TABLE descriptions (                    -- camada L3
  symbol_id    TEXT NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  scope        TEXT NOT NULL CHECK(scope IN ('symbol','module','domain')),
  content      TEXT NOT NULL,
  source_hash  TEXT NOT NULL,                  -- body_hash no momento da geração → frescor = (source_hash == body_hash atual)
  model        TEXT,
  generated_at INTEGER,
  PRIMARY KEY(symbol_id, scope)
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- meta: schema_version, repo_root, merkle_root, last_full_scan, lsp_status
```

**Regras de propriedade (o que torna o incremental correto):**

- Símbolos pertencem ao arquivo que os define; arestas pertencem ao arquivo onde a **referência** ocorre.
- Re-indexar arquivo F = transação: `DELETE` símbolos e arestas com `file_id=F` → re-parse → `INSERT`. Nada fora de F é tocado.
- Se um símbolo de F morre, arestas de *outros* arquivos que apontavam para ele viram `dst=NULL` (dangling), preservando `dst_name`. Um passo assíncrono de re-resolução tenta religar danglings após cada re-index (índice parcial `idx_edges_dangling` torna isso barato).

### 1.3 Confiança das arestas

| Confiança | Significado | Origem |
|---|---|---|
| `certain` | Resolvido por semântica real | L1 (LSP/SCIP) |
| `inferred` | Rastreado por import com alvo único | L0 (heurística de import) |
| `possible` | Match por nome, ≥1 candidato | L0 (léxico); até 5 candidatos viram 5 arestas `possible` |

Consultas transitivas (impact, callers com depth>1) propagam a **mínima** confiança do caminho.

## 2. Sincronização (o mecanismo anti-staleness)

Quatro defesas em camadas; a meta é que seja impossível servir um fato sem saber seu frescor:

1. **Startup — diff de Merkle.** Árvore de content-hashes por diretório persistida em `meta`. No boot: recomputa (mtime+size como fast-path; hash só quando divergem), re-indexa o delta. Cobre git pull, troca de branch, edições com o daemon desligado.
2. **Sessão — file watcher.** Watcher nativo com debounce de 500ms–2s; eventos enfileiram re-index L0 do arquivo. Watch em `.git/HEAD` e `.git/refs` dispara diff de Merkle completo (troca de branch muda muitos arquivos de uma vez).
3. **Query — read-repair (a garantia final).** Toda tool, antes de responder: verifica `content_hash` dos arquivos que aparecem na resposta (fast-path mtime+size). Divergiu → re-parse L0 síncrono desses arquivos (ms) e responde com dado fresco, anotando `read_repaired`. Se >20 arquivos sujos (caso patológico), responde com os frescos + aviso `stale` explícito em vez de bloquear.
4. **L1/L3 — frescor declarado, nunca fingido.** Refinamento LSP é assíncrono: enquanto não chega, as arestas são L0 (`inferred`/`possible`) — corretas quanto à própria incerteza. Descrições L3 comparam `source_hash` vs `body_hash` na leitura: divergiu → servida com flag `stale: true` (e opcionalmente re-gerada sob demanda).

## 3. Contrato das tools

### 3.1 Envelope de resposta

Respostas são **texto compacto** (não JSON verboso — tokens importam). O envelope só aparece quando há desvio; silêncio = fresco e dentro dos limites conhecidos:

```
⚠ freshness: 2 arquivos mudaram desde a indexação; re-indexados agora (L0). Refinamento LSP pendente.
⚠ completeness: análise estática — chamadas via reflection/dispatch dinâmico podem faltar; 3 refs não resolvidas neste escopo.
⚠ truncated: mostrando 20 de 143 (use offset/limit).
```

Linha de completeness é **obrigatória** em `callers`, `callees`, `impact` e `references` sempre que existir aresta `possible` ou ref não resolvida no escopo — é a materialização do princípio 3.

### 3.2 As oito tools

Referência a símbolo aceita: `fqn` (preferido), `nome` (desambiguado se necessário) ou `path:linha`.

**`overview(scope?, token_budget=2000)`** — Mapa ranqueado do repo (ou de `scope`, um diretório/módulo): árvore de módulos com os top símbolos por PageRank, assinaturas incluídas. Busca binária no número de entradas para caber no budget (estilo Aider). Primeira tool que um agente chama num repo desconhecido.

**`find_symbol(query, kind?, limit=10)`** — Match exato por fqn → prefixo → FTS5 (nome+doc) → fuzzy, nessa ordem, com score. Retorna `fqn | kind | assinatura | path:linha | rank`.

**`symbol_info(symbol)`** — Cartão do símbolo: assinatura, doc, containment (pai/filhos), contadores (nº callers/callees/refs), span para o agente ler o corpo, e a descrição L3 se existir e estiver fresca (stale → incluída com flag).

**`references(symbol, kind?, limit=50)`** — Todos os usos, agrupados por arquivo, cada um com `path:linha [confiança]`.

**`callers(symbol, depth=1)` / `callees(symbol, depth=1)`** — Árvore de chamadas com confiança por aresta. `possible` agrupados numa seção separada ("candidatos por nome") para não poluir o sinal forte.

**`impact(symbol|path, depth=3, direction='upstream')`** — Fecho transitivo de dependentes (quem quebra se eu mudar isto), ranqueado por `rank × confiança-do-caminho`. `direction='downstream'` responde "do que isto depende". É a tool de "posso mudar isso com segurança?" — a linha de completeness aqui nunca é omitida.

**`ego_graph(symbol, radius=1)`** — Vizinhança compacta (todas as arestas tipadas de entrada/saída até `radius`), para o agente montar o modelo mental local antes de editar (estilo RepoGraph).

**`describe(symbol|module, refresh=false)`** — Camada L3: resumo de comportamento gerado por LLM, hierárquico (symbol→module→domain), sempre com proveniência (`modelo, gerado_em, fresh: bool`). `refresh=true` re-gera agora. Sem provider configurado, a tool existe mas responde que L3 está desabilitada — o resto do sistema não depende dela.

### 3.3 Exemplo (formato-alvo)

```
> callers("auth.TokenService.validate", depth=1)

⚠ completeness: análise estática (L0+L1); chamadas dinâmicas podem faltar.

callers de auth.TokenService.validate — 6 diretos:
  api/routes.py:42   handlers.login            [certain]
  api/routes.py:88   handlers.refresh          [certain]
  api/middleware.py:31 AuthMiddleware.__call__ [certain]
  cli/admin.py:120   verify_cmd                [inferred]
candidatos por nome (verificar antes de confiar):
  jobs/cleanup.py:17 purge_sessions            [possible]
  tests/legacy.py:9  test_validate             [possible]
```

## 4. Pipeline

```
        ┌─ startup: Merkle diff ─┐
fs ──►  │  watcher: eventos      ├──► fila de re-index ──► L0 indexer (tree-sitter, workers paralelos)
        └─ query: read-repair ───┘            │                    │ (tx por arquivo)
                                              ▼                    ▼
                                   L1 resolver (LSP, async) ──► SQLite ◄── re-resolução de danglings
                                                                   ▲
                                   L3 enricher (LLM, lazy/batch) ──┘
                                                                   │
                              query engine (envelope + read-repair + PageRank lazy)
                                                                   │
                        ┌──────────────┬───────────────┬───────────┴────┐
                        lib (API)      CLI             MCP server (stdio)  → SIFT via connect_mcp_stdio
```

- **PageRank**: recomputado lazy (marcado dirty a cada re-index; recalculado na próxima query de `overview`/`impact`, com cap de frequência). Grafo em memória só para o cálculo.
- **MCP primeiro** como adaptador (funciona em Claude Code, Cursor, Codex...); lib e CLI saem do mesmo core. No SIFT, a hierarquia natural: `codegraph.symbols.find`, `codegraph.graph.callers`, `codegraph.graph.impact`, `codegraph.semantic.describe`.

## 5. Casos de borda (decisões explícitas)

- **Parse com erro** (arquivo no meio de edição): tree-sitter produz nós ERROR → indexa o que der, `parse_status='partial'`, envelope avisa quando símbolos desse arquivo aparecem.
- **Arquivos gerados/vendored**: respeita `.gitignore` + `.codegraphignore`; default exclui `node_modules`, `dist`, lockfiles etc.
- **Rename de arquivo**: delete+add; conteúdo igual (mesmo hash) → re-parse barato, identidades mudam (contêm o path). Aceito na v1.
- **Repo gigante / primeira indexação**: L0 é paralelo por arquivo e tolera interrupção (transação por arquivo); o sistema já responde queries durante a indexação inicial, com aviso de cobertura parcial.
- **Concorrência**: SQLite WAL, um writer (fila serializa re-index); leituras nunca bloqueiam.

## 6. Decisões em aberto (com recomendação)

1. **Linguagem de implementação** — *Recomendo Python 3.11+*: bindings maduros de tree-sitter (o parser é C, então o hot path não é Python), MCP SDK oficial, e o SIFT já é Python (reuso de padrões e eventual integração de código). Portar o core para Rust depois é possível porque o contrato (schema + tools) é agnóstico. Stack sugerida: `uv`, `tree-sitter` + `tree-sitter-language-pack`, `watchdog`, `mcp`, `xxhash`, PageRank próprio (~50 linhas, evita dependência pesada).
2. **Linguagens-alvo da v1** — Python + TypeScript/JavaScript (cobrem os benchmarks e o SIFT); a gramática tree-sitter dá L0 para dezenas de outras "de graça", só sem extração de fqn refinada.
3. **L1 via LSP direto (estilo Serena) vs. índices SCIP** — LSP dá cobertura ampla mas exige servidor rodando; SCIP é offline mas exige indexer por linguagem. Recomendo LSP primeiro (pyright/tsserver), interface de resolver plugável.

**Decisões tomadas (2026-07-18):** implementação em Python 3.10+ (layout `src/`, distribuição provisória `codegraph-ai` — o nome `codegraph` já existe no PyPI); LLM de teste para L3/avaliação: **DeepSeek v4 Flash via OpenRouter** (a camada continua provider-agnostic). **L1 (M4)**: interface de resolver plugável; primeiro resolver = Python via **jedi in-process** (InterpreterEnvironment — sem subprocess, ~10x mais rápido) em vez de driver LSP; pyright/tsserver entram depois pela mesma interface. Schema mudou → wipe e rebuild automático do banco (cache derivado nunca pede intervenção manual). **Nome oficial do projeto (decisão do Victor, 2026-07-18): `graphcodemap`** — distribuição PyPI `graphcodemap` (livre, verificado), CLIs `graphcodemap`/`graphcodemap-mcp` com aliases `codegraph`/`codegraph-mcp`, pacote importável segue `codegraph`.

## 7. Roadmap

- **M0 — núcleo L0**: indexer tree-sitter → SQLite, `find_symbol`, `references`, `symbol_info`. Já útil.
- **M1 — grafo**: `calls/imports/inherits` L0, `callers/callees`, `impact`, `ego_graph`, envelope de completeness, PageRank + `overview`.
- **M2 — anti-staleness completo**: Merkle no startup, watcher, read-repair na query. *(Gate: teste automatizado que edita/deleta/renomeia arquivos e prova que nenhuma query retorna dado velho sem aviso.)*
- **M3 — MCP server** + integração SIFT.
- **M4 — L1**: refinamento LSP assíncrono, promoção de confiança, re-resolução de danglings.
- **M5 — L3**: `describe` com invalidação por hash, provider-agnostic.
- **M6 — avaliação**: harness contra baseline grep/read (LocBench + SWE-bench Lite), medindo qualidade E tokens E tool calls.
- **M7 — camada domínio**: detecção de comunidades (Louvain próprio sobre o grafo de símbolos, recompute lazy) + `communities` na CLI/MCP + labels via L3 (`describe domain:N`), preservados por assinatura de membros. *(Feito 2026-07-18.)*
- **M8 — visualização**: `visualize` exporta HTML autocontido (canvas force-directed, cor por domínio, tamanho por PageRank) ou JSON; corte declarado aos N nós mais conectados. *(Feito 2026-07-18; Leiden é refinamento futuro.)*
- **M9 — profundidade de linguagens**: extractors dedicados (fqn com escopo, herança, imports, calls no site do nome) para Ruby, Lua/Luau e Swift — 16 dedicados no total. *(Feito 2026-07-18; próximos candidatos Scala/Dart/Elixir.)*
- **M10 — dataflow (CPG-lite)**: análise intra-procedural may-taint por função (params → argumentos de chamadas / retorno), composta ao longo do call graph (inter-procedural), computada sob demanda (sempre fresca), confiança herdada das arestas. `dataflow` na CLI/MCP. Base para segurança (fonte→sink) e refatoração. *(Feito 2026-07-18. Pesquisa em RESEARCH.md §6.)*
- **M11 — taint (segurança)**: `taint` fonte→sink com sources/sinks/**sanitizers** (que cortam o fluxo), configuráveis em `.codegraph/taint.json`. Dois modos: varredura do repo (fontes = chamadas a `sources` + funções que retornam dado de fonte) e `--entry FUNC` (assume os parâmetros da função como não-confiáveis). Analisador refatorado em fatos-por-linguagem + motor compartilhado. *(Feito 2026-07-18; may-taint over-aproxima — achados são candidatos.)*
- **M12 — paridade grafo↔dataflow + Scala**: extração de fatos do dataflow dirigida por config por-linguagem (as irregularidades de gramática ficam declarativas; motor de taint continua compartilhado). Dataflow/taint agora cobre **todas as 17 linguagens dedicadas** (py, js/ts, java, c#, c/c++/cuda, go, rust, ruby, php, kotlin, swift, scala, lua/luau). Novo extractor de grafo dedicado: **Scala** (trait→interface, object/class, mixins `with`→inherits, imports com selectors). *(Feito 2026-07-18; Dart/Elixir adiados — grammar signature/body split e macro-based exigem cuidado extra, e taint meio-certo é pior que ausente.)*
