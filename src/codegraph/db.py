"""SQLite local-first: `.codegraph/graph.db`. Schema em docs/DESIGN.md §1.2.

Regras de propriedade que tornam o incremental correto:
- símbolos pertencem ao arquivo que os define (ON DELETE CASCADE);
- arestas pertencem ao arquivo do *site da referência* (file_id, CASCADE);
- alvo de aresta (dst) usa ON DELETE SET NULL preservando dst_name,
  para re-resolução posterior — o grafo nunca perde informação em silêncio.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = "6"
L1_LIFECYCLE_STATUSES = frozenset({
    "not_started", "running", "complete", "partial",
})


def read_l1_lifecycle(conn: sqlite3.Connection) -> dict:
    """Return the semantic-refinement lifecycle with a stable default.

    The lifecycle lives in ``meta`` rather than the derived edge tables so a
    reader can distinguish "L1 has never run" from a completed L0-only graph.
    Malformed legacy/user-edited metadata fails closed as ``not_started``.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key='l1_lifecycle'"
    ).fetchone()
    if row is None:
        return {"status": "not_started"}
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError):
        return {"status": "not_started"}
    if (not isinstance(value, dict)
            or value.get("status") not in L1_LIFECYCLE_STATUSES):
        return {"status": "not_started"}
    return value


def write_l1_lifecycle(conn: sqlite3.Connection, value: dict) -> None:
    """Write lifecycle metadata in the caller's transaction.

    Deliberately does not commit: final ``complete``/``partial`` must publish in
    the same SQLite commit as the corresponding semantic edge snapshot.
    """
    status = value.get("status")
    if status not in L1_LIFECYCLE_STATUSES:
        raise ValueError(f"status L1 inválido: {status!r}")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('l1_lifecycle', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(value, ensure_ascii=False, sort_keys=True),),
    )


def retry_on_locked(fn, tries: int = 6, base_delay: float = 0.05):
    """Roda `fn`, repetindo em 'database is locked'/SQLITE_BUSY_SNAPSHOT com
    backoff exponencial. Para escritas concorrentes entre conexões (watcher +
    read-repair + refine) que o busy_timeout do SQLite não cobre. `fn` deve ser
    idempotente — é o caso das escritas do indexer (transação por arquivo)."""
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == tries - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def record_current_stage(conn: sqlite3.Connection, stage: str,
                         stage_version: str, status: str,
                         details: dict | None = None, *,
                         commit: bool = True) -> int | None:
    """Version a derived stage against the current repository revision.

    ``commit=False`` lets a producer publish the stage receipt in the same
    transaction as its derived artifacts and readiness metadata.  Existing
    callers retain the historical auto-commit behavior.
    """
    row = conn.execute(
        "SELECT r.id, r.source_snapshot_hash FROM graph_revisions r "
        "JOIN meta m ON m.key='current_graph_revision' "
        "AND CAST(m.value AS INTEGER)=r.id"
    ).fetchone()
    if row is None:
        return None
    payload = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
    artifact = hashlib.blake2b(
        f"{row['source_snapshot_hash']}\0{stage}\0{stage_version}\0{payload}"
        .encode("utf-8"), digest_size=16).hexdigest()
    now = int(time.time())
    conn.execute(
        "INSERT INTO graph_stage_runs(revision_id,stage,stage_version,status,"
        "artifact_hash,details_json,started_at,completed_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(revision_id,stage) DO UPDATE SET "
        "stage_version=excluded.stage_version,status=excluded.status,"
        "artifact_hash=excluded.artifact_hash,details_json=excluded.details_json,"
        "completed_at=excluded.completed_at",
        (row["id"], stage, stage_version, status, artifact, payload, now, now),
    )
    if commit:
        conn.commit()
    return int(row["id"])

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id           INTEGER PRIMARY KEY,
  path         TEXT UNIQUE NOT NULL,
  language     TEXT,
  content_hash TEXT NOT NULL,
  size         INTEGER,
  mtime        INTEGER, -- nanosegundos desde epoch (stat.st_mtime_ns)
  parse_status TEXT NOT NULL DEFAULT 'ok'
               CHECK(parse_status IN ('ok','partial','failed')),
  indexed_at   INTEGER NOT NULL
);

-- Snapshot físico do repositório. Inclui diretórios e arquivos que não são
-- código (README, configs, assets etc.); ``files`` continua sendo a projeção
-- parseável usada pelo grafo de símbolos.
CREATE TABLE IF NOT EXISTS repository_nodes (
  id            TEXT PRIMARY KEY,
  parent_id     TEXT REFERENCES repository_nodes(id) ON DELETE CASCADE,
  path          TEXT UNIQUE NOT NULL,
  kind          TEXT NOT NULL CHECK(kind IN ('repository','directory','file','symlink')),
  content_hash  TEXT,
  size          INTEGER,
  mtime         INTEGER,
  language      TEXT,
  index_state   TEXT CHECK(index_state IS NULL OR index_state IN
                  ('pending','indexed','partial','skipped','failed','not_applicable')),
  state_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_repository_nodes_parent
  ON repository_nodes(parent_id, path);

-- Uma revisão identifica o snapshot observado, mesmo quando o worktree está
-- dirty. O commit Git dá o ancestral reproduzível; source_snapshot_hash
-- distingue mudanças ainda não commitadas.
CREATE TABLE IF NOT EXISTS graph_revisions (
  id                   INTEGER PRIMARY KEY,
  parent_revision_id   INTEGER REFERENCES graph_revisions(id),
  trigger              TEXT NOT NULL,
  git_commit           TEXT,
  git_dirty            INTEGER NOT NULL DEFAULT 0,
  source_snapshot_hash TEXT NOT NULL,
  started_at           INTEGER NOT NULL,
  completed_at         INTEGER,
  status               TEXT NOT NULL,
  changed_files        INTEGER NOT NULL DEFAULT 0,
  removed_files        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_graph_revisions_git
  ON graph_revisions(git_commit, id DESC);
CREATE INDEX IF NOT EXISTS idx_graph_revisions_snapshot
  ON graph_revisions(source_snapshot_hash, id DESC);

-- Cada camada declara qual implementação rodou sobre qual snapshot e o hash
-- determinístico de seu artefato. Stages futuros entram sem migrar o schema.
CREATE TABLE IF NOT EXISTS graph_stage_runs (
  revision_id    INTEGER NOT NULL REFERENCES graph_revisions(id) ON DELETE CASCADE,
  stage          TEXT NOT NULL,
  stage_version  TEXT NOT NULL,
  status         TEXT NOT NULL,
  artifact_hash  TEXT,
  details_json   TEXT NOT NULL DEFAULT '{}',
  started_at     INTEGER NOT NULL,
  completed_at   INTEGER,
  PRIMARY KEY(revision_id, stage)
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
-- guarda estrutural: arestas resolvidas idênticas não podem coexistir. Torna o
-- re-acúmulo de clones impossível — a origem do bloat histórico (edges 800x) —
-- SEM perder recall: candidatos distintos (dst diferente) continuam permitidos.
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_resolved_uniq
  ON edges(kind, src, dst, dst_name, file_id, line, col) WHERE dst IS NOT NULL;

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
    # watcher/refine/queries podem concorrer: espera em vez de "database is locked".
    # busy_timeout cobre SQLITE_BUSY, mas NÃO SQLITE_BUSY_SNAPSHOT (ler-depois-
    # escrever entre conexões) — para esse há retry_on_locked na camada de escrita.
    conn.execute("PRAGMA busy_timeout=10000")
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
                      "graph_stage_runs", "graph_revisions",
                      "repository_nodes", "files", "meta"):
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
