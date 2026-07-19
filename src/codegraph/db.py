"""SQLite local-first: `.codegraph/graph.db`. Schema em docs/DESIGN.md §1.2.

Regras de propriedade que tornam o incremental correto:
- símbolos pertencem ao arquivo que os define (ON DELETE CASCADE);
- arestas pertencem ao arquivo do *site da referência* (file_id, CASCADE);
- alvo de aresta (dst) usa ON DELETE SET NULL preservando dst_name,
  para re-resolução posterior — o grafo nunca perde informação em silêncio.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "4"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id           INTEGER PRIMARY KEY,
  path         TEXT UNIQUE NOT NULL,
  language     TEXT,
  content_hash TEXT NOT NULL,
  size         INTEGER,
  mtime        INTEGER,
  parse_status TEXT NOT NULL DEFAULT 'ok'
               CHECK(parse_status IN ('ok','partial','failed')),
  indexed_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
  id         TEXT PRIMARY KEY,
  file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  parent_id  TEXT REFERENCES symbols(id) ON DELETE SET NULL,
  kind       TEXT NOT NULL,
  name       TEXT NOT NULL,
  fqn        TEXT NOT NULL,
  signature  TEXT,
  doc        TEXT,
  start_line INTEGER, start_col INTEGER, end_line INTEGER, end_col INTEGER,
  body_hash  TEXT NOT NULL,
  visibility TEXT,
  rank       REAL NOT NULL DEFAULT 0,
  community  INTEGER          -- domínio (Louvain), recomputado lazy; NULL = isolado
);
CREATE INDEX IF NOT EXISTS idx_symbols_fqn  ON symbols(fqn);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_community ON symbols(community);
-- sem este índice, cada DELETE de símbolo faz scan da tabela inteira para
-- honrar parent_id ON DELETE SET NULL (re-index de 1 arquivo custava ~s)
CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent_id);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
  symbol_id UNINDEXED, name, fqn, doc
);

CREATE TABLE IF NOT EXISTS edges (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  src        TEXT REFERENCES symbols(id) ON DELETE CASCADE,
  dst        TEXT REFERENCES symbols(id) ON DELETE SET NULL,
  dst_name   TEXT NOT NULL,
  file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  line       INTEGER,
  col        INTEGER,
  confidence TEXT NOT NULL CHECK(confidence IN ('certain','inferred','possible')),
  resolver   TEXT NOT NULL CHECK(resolver IN ('l0','l1'))
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, kind);
CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file_id);
CREATE INDEX IF NOT EXISTS idx_edges_dangling ON edges(dst_name) WHERE dst IS NULL;

CREATE TABLE IF NOT EXISTS descriptions (
  symbol_id    TEXT NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  scope        TEXT NOT NULL CHECK(scope IN ('symbol','module','domain')),
  content      TEXT NOT NULL,
  source_hash  TEXT NOT NULL,
  model        TEXT,
  generated_at INTEGER,
  PRIMARY KEY(symbol_id, scope)
);

CREATE TABLE IF NOT EXISTS module_descriptions (
  file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  content      TEXT NOT NULL,
  source_hash  TEXT NOT NULL,           -- content_hash do arquivo na geração
  model        TEXT,
  generated_at INTEGER
);

-- Domínios (comunidades do grafo): metadados recomputados a cada detecção.
-- `signature` = hash do conjunto de membros; permite reaproveitar o label LLM
-- quando a composição do domínio não mudou (mesma invalidação-por-hash do L3).
CREATE TABLE IF NOT EXISTS communities (
  id           INTEGER PRIMARY KEY,
  size         INTEGER NOT NULL,
  signature    TEXT NOT NULL,
  label        TEXT,
  summary      TEXT,
  model        TEXT,
  generated_at INTEGER
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def default_db_path(root: Path) -> Path:
    return root / ".codegraph" / "graph.db"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: o watcher drena em timer-threads; o acesso é
    # serializado por lock no chamador (Watcher._drain_lock) e WAL no arquivo
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    # watcher/refine/queries podem concorrer: espera em vez de "database is locked"
    conn.execute("PRAGMA busy_timeout=5000")
    # identificadores são case-sensitive; LIKE default do SQLite não é
    conn.execute("PRAGMA case_sensitive_like=ON")
    # Checar a versão ANTES de aplicar o schema: o _SCHEMA novo pode referenciar
    # colunas/índices que não existem no banco antigo (ex.: symbols.community),
    # e um CREATE INDEX sobre a tabela velha falharia antes do wipe. meta é
    # estável entre versões, então lê-la primeiro é seguro.
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    fresh = row is None
    if row is not None and row["value"] != SCHEMA_VERSION:
        # o grafo é cache derivado: schema mudou → apaga e reconstrói,
        # nunca exige intervenção manual (docs/DESIGN.md §0.1)
        for table in ("symbols_fts", "edges", "descriptions",
                      "module_descriptions", "communities", "symbols",
                      "files", "meta"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        fresh = True
    conn.executescript(_SCHEMA)
    if fresh:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,))
        conn.commit()
    return conn
