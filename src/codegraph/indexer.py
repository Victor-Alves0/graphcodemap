"""Indexação L0: walk do repo, transação por arquivo, resolução de arestas.

Invariantes (docs/DESIGN.md §1.2 e §2):
- re-indexar o arquivo F só toca linhas com file_id=F (transação por arquivo);
- arestas nunca perdem `dst_name`; alvo que sumiu vira dangling (dst=NULL)
  e é religado pela resolução na próxima passada;
- confiança L0: 'inferred' (alvo único via fqn/import ou mesmo arquivo),
  'possible' (match por nome, até MAX_CANDIDATES candidatos). 'certain' é L1.
"""

from __future__ import annotations

import json
import os
import posixpath
import time
from pathlib import Path

import pathspec

from . import community, extract, rank
from .db import connect, default_db_path, retry_on_locked
from .extract.base import Sym
from .languages import get_parser, language_for
from .log import get as _get_log
from .util import content_hash, symbol_uid

log = _get_log(__name__)

MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_CANDIDATES = 5
# escrita em lote no scan completo: commit a cada N arquivos (o commit por
# arquivo dominava o tempo de indexação). Savepoint por arquivo preserva o
# isolamento de erro. Só afeta index_repo — o caminho incremental é per-arquivo.
BULK_BATCH = 500
# Indexação paralela: só o PREPARE (ler+parsear+extrair, que solta o GIL no I/O
# e no tree-sitter) roda em threads; a ESCRITA no SQLite continua serial (writer
# único). Gera resultados na ordem de entrada → ordem de escrita idêntica à
# serial → grafo bit-a-bit igual. Ganho limitado pelo teto de escrita (~1,3x).
PARALLEL_MIN_FILES = 1000        # abaixo disso o overhead de thread não compensa
INDEX_WORKERS_MAX = 4            # ponto ótimo medido (acima, contenção de GIL piora)
# WAL sob controle em repos ENORMES: escrever milhões de linhas numa transação
# única faz o WAL crescer sem limite (não dá p/ checkpointar frames não-commitados)
# até o commit final disparar um checkpoint gigante que trava — foi o que travou
# o índice do kernel Linux inteiro. Commit em blocos + checkpoint(TRUNCATE)
# mantêm o WAL pequeno e tornam o índice resumível se interrompido.
WRITE_CHUNK = 50_000             # linhas por commit nas escritas em massa
CHECKPOINT_EVERY_BATCHES = 20    # a cada N commits de arquivo, encolhe o WAL
MAX_TRACKED_CHANGES = 1000       # teto das listas de mudança (contadores são exatos)


class ChangeSet:
    """Símbolos que entraram, saíram ou mudaram de ASSINATURA numa passada.

    Fecha o loop de quem edita código: reindexa → sabe que `save_user` mudou de
    assinatura → chama `impact()` e avisa antes de commitar, sem precisar de
    diff de git nem adivinhar o símbolo. O indexador já sabia disso (apaga os
    símbolos antigos e insere os novos); só não contava.

    As listas são limitadas (`truncated`) para não estourar num índice inicial
    de repo inteiro; os CONTADORES são sempre exatos."""

    def __init__(self, cap: int = MAX_TRACKED_CHANGES) -> None:
        self.cap = cap
        self.added: list[str] = []
        self.removed: list[str] = []
        self.signature_changed: list[dict] = []
        self.counts = {"added": 0, "removed": 0, "signature_changed": 0}
        self.truncated = False

    def _push(self, bucket: list, key: str, item) -> None:
        self.counts[key] += 1
        if len(bucket) < self.cap:
            bucket.append(item)
        else:
            self.truncated = True

    def add(self, fqn: str) -> None:
        self._push(self.added, "added", fqn)

    def remove(self, fqn: str) -> None:
        self._push(self.removed, "removed", fqn)

    def signature(self, fqn: str, before: str | None, after: str | None) -> None:
        self._push(self.signature_changed, "signature_changed",
                   {"fqn": fqn, "before": before, "after": after})

    def as_dict(self) -> dict:
        return {"added": self.added, "removed": self.removed,
                "signature_changed": self.signature_changed,
                "counts": dict(self.counts), "truncated": self.truncated}
CALLABLE_KINDS = ("function", "method", "class")
# Alvos válidos de uma aresta `references` (uso de estilo). Cross-language por
# natureza — o TSX/HTML usa a classe que o CSS define, e o CSS estiliza o id que
# o HTML declara —, então aqui NÃO se filtra por língua; o kind é o que protege
# contra religar numa função homônima.
STYLE_DEF_KINDS = ("css_class", "html_id")

# Versão da lógica de extração/resolução: mudou → força re-index completo,
# mesmo com content-hashes iguais (o índice é derivado de código+extractor).
INDEXER_VERSION = "15"

DEFAULT_IGNORES = [
    ".git/", ".codegraph/", "__pycache__/", ".venv/", "venv/", "node_modules/",
    "dist/", "build/", ".next/", "coverage/", "target/", ".mypy_cache/",
    ".pytest_cache/", ".ruff_cache/", "*.min.js",
]


def _ignore_lines(root: Path, extra: list[str] | None = None) -> list[str]:
    lines = list(DEFAULT_IGNORES)
    for name in (".gitignore", ".codegraphignore"):
        p = root / name
        if p.is_file():
            lines += p.read_text(encoding="utf-8", errors="replace").splitlines()
    if extra:
        lines += list(extra)          # política do host (meta), sem tocar o repo
    return lines


def load_ignore_spec(root: Path,
                     extra: list[str] | None = None) -> pathspec.PathSpec:
    return pathspec.GitIgnoreSpec.from_lines(_ignore_lines(root, extra))


def _file_ignore_spec(lines: list[str]) -> pathspec.PathSpec:
    """Spec para checar ARQUIVOS: descarta padrões terminados em '/' — no
    gitignore eles casam SÓ diretórios, então nunca alteram o status de um
    arquivo (cujos ancestrais já passaram na poda de diretório). Remover essas
    linhas é exato (independe de ordem) e corta o custo por arquivo: dos ~15
    ignores default, só `*.min.js` casa arquivo — 15x menos regex por arquivo."""
    file_lines = [ln for ln in lines if not ln.rstrip().endswith("/")]
    return pathspec.GitIgnoreSpec.from_lines(file_lines)


# -- escopo de indexação parcial ---------------------------------------------
# Um índice pode cobrir só subárvores (para monorepos grandes demais p/ indexar
# inteiros — ver evals/RESULTS.md). O escopo é persistido em meta['index_scopes']
# (lista JSON de prefixos relativos; vazio = repo inteiro). iter/scan/freshness
# respeitam o escopo; a remoção só apaga arquivos sumidos DENTRO do escopo.

def _norm_scope(s: str) -> str:
    return s.strip().replace("\\", "/").strip("/")


def in_scope(rel: str, scopes: list[str] | None) -> bool:
    if not scopes:
        return True
    return any(rel == s or rel.startswith(s + "/") for s in scopes)


def _get_meta_list(conn, key: str) -> list[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None or not row["value"]:
        return []
    try:
        return list(json.loads(row["value"]))
    except (ValueError, TypeError):
        return []


def _set_meta_list(conn, key: str, values: list[str]) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(values)))
    conn.commit()


def get_index_scopes(conn) -> list[str]:
    return _get_meta_list(conn, "index_scopes")


def get_index_excludes(conn) -> list[str]:
    """Padrões de exclusão (estilo gitignore) guardados no índice — política do
    HOST, sem exigir escrever `.codegraphignore` na working copy do usuário.
    Complemento negativo do escopo."""
    return _get_meta_list(conn, "index_excludes")


def _scope_roots(root: Path, scopes: list[str] | None) -> list[tuple[Path, str]]:
    """(dir absoluto, prefixo relativo) por onde iniciar o walk. Sem escopo, o
    próprio root; com escopo, cada subárvore (existente)."""
    if not scopes:
        return [(root, "")]
    return [(root / s, s) for s in scopes if (root / s).exists()]


def iter_source_files(root: Path, spec: pathspec.PathSpec | None = None,
                      scopes: list[str] | None = None):
    spec = spec or load_ignore_spec(root)
    for base, _ in _scope_roots(root, scopes):
        it = [base] if base.is_file() else sorted(base.rglob("*"))
        for p in it:
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if spec.match_file(rel) or language_for(rel) is None:
                continue
            yield rel


def scan_source_stats(root: Path,
                      spec: pathspec.PathSpec | None = None,
                      scopes: list[str] | None = None,
                      excludes: list[str] | None = None) -> dict[str, tuple[int, int]]:
    """``{rel: (size, int(mtime))}`` de todos os arquivos-fonte, via ``os.scandir``.

    size/mtime vêm da leitura do diretório (no Windows, sem syscall extra por
    arquivo) — ~60x mais rápido que ``stat()`` individual em repos grandes. Usado
    pela varredura de frescor (read-repair de resultado vazio) para que ela possa
    rodar a CADA query sem custo proibitivo, preservando a garantia anti-staleness
    em escala. Mesmo conjunto de arquivos que ``iter_source_files``."""
    lines = _ignore_lines(root, excludes)
    dir_spec = spec or pathspec.GitIgnoreSpec.from_lines(lines)  # poda de diretório
    file_spec = _file_ignore_spec(lines)                        # check de arquivo (barato)
    out: dict[str, tuple[int, int]] = {}
    # Constrói o caminho relativo por concatenação enquanto desce (o prefixo do
    # diretório vem na pilha), em vez de os.path.relpath por entrada — relpath
    # chama normcase/LCMapStringEx e dominava a varredura (~72% em 100k arquivos).
    # Barra "/" direto, sem replace. Mesmo conjunto/paths que iter_source_files.
    # Com escopo, o walk começa só nas subárvores indexadas (varredura barata em
    # monorepo grande onde só uma parte está indexada).
    stack: list[tuple[str, str]] = [(str(base), rel)
                                    for base, rel in _scope_roots(root, scopes)]
    while stack:
        abs_dir, rel_dir = stack.pop()
        try:
            it = os.scandir(abs_dir)
        except OSError:
            continue
        with it:
            for e in it:
                rel = e.name if not rel_dir else f"{rel_dir}/{e.name}"
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not dir_spec.match_file(rel + "/"):   # poda dir ignorado
                            stack.append((e.path, rel))
                    elif e.is_file(follow_symlinks=False):
                        # language_for (lookup de extensão) primeiro: descarta
                        # não-fontes sem pagar o match do gitignore; file_spec só
                        # tem os padrões que podem casar arquivo (sem os de dir).
                        if language_for(rel) is None or file_spec.match_file(rel):
                            continue
                        st = e.stat()
                        out[rel] = (st.st_size, int(st.st_mtime))
                except OSError:
                    continue
    return out


def module_fqn_for(rel: str) -> str:
    dot = rel.rfind(".")
    stem = rel[:dot] if dot != -1 else rel
    parts = stem.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# extensões tentadas ao resolver um caminho sem extensão (`./utils`, `@use
# "buttons"`). Ordem = precedência quando várias existem.
PATH_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".scss", ".sass",
             ".css", ".html", ".py")


def _file_symbol(rel: str, data: bytes, h: str) -> Sym:
    """O ARQUIVO como símbolo — o alvo que faltava para `imports` locais.

    Sem isto, `<script src="./main.tsx">`, `<link href>` e `@import` ficavam
    permanentemente dangling: o guess é um caminho, e não havia nada no grafo
    para apontar. Um símbolo por arquivo é barato (1 por arquivo, contra 596
    classes CSS num único .css do repo de teste) e é o alvo natural.

    `name` é o ÚLTIMO segmento do fqn (não o basename com extensão): a
    resolução por guess qualificado procura por `fqn.rsplit('.')[-1]` no índice
    de nomes, então `main.tsx` como nome tornaria o símbolo inalcançável.
    """
    fqn = module_fqn_for(rel)
    return Sym(kind="file", name=fqn.rsplit(".", 1)[-1], fqn=fqn,
               parent_fqn=None, signature=rel, doc=None,
               start_line=1, start_col=0, end_line=data.count(b"\n") + 1,
               end_col=0, body_hash=h, visibility=None)


def _extract_file(lang: str, data: bytes, rel: str, tree, h: str):
    """extract.extract + o símbolo do arquivo. Ponto único: os dois caminhos de
    prepare (serial e paralelo) passam por aqui, senão o grafo dependeria de
    quantos workers rodaram."""
    syms, refs = extract.extract(lang, data, module_fqn_for(rel), tree)
    syms.append(_file_symbol(rel, data, h))   # append: fqn de símbolo declarado
    return syms, refs                         # continua ganhando no fqn_to_uid


class Indexer:
    def __init__(self, root: str | Path, db_path: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.db_path = Path(db_path) if db_path else default_db_path(self.root)
        self.conn = connect(self.db_path)
        # coletor da passada corrente (escrita é sempre single-thread) e o
        # resultado da última — ver ChangeSet.
        self._changes: ChangeSet | None = None
        self.last_changes: dict | None = None

    def close(self) -> None:
        self.conn.close()

    # -- manutenção ----------------------------------------------------------

    def compact(self) -> dict:
        """Reconstrói o índice do zero e recupera espaço: re-indexa tudo
        (limpa marcadores 'failed' obsoletos e arestas órfãs), remove descrições
        órfãs e roda VACUUM. As descrições L3 de símbolos vivos são preservadas
        (ids estáveis). Retorna tamanhos antes/depois e contagens."""
        size_before = self.db_path.stat().st_size if self.db_path.exists() else 0
        edges_before = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        stats = self.index_repo(force=True)
        # descrições cujo símbolo/arquivo sumiu (defensivo — CASCADE já cobre o
        # caminho normal, mas estado legado pode ter sobrado)
        self.conn.execute("DELETE FROM descriptions WHERE symbol_id NOT IN "
                          "(SELECT id FROM symbols)")
        self.conn.execute("DELETE FROM module_descriptions WHERE file_id NOT IN "
                          "(SELECT id FROM files)")
        self.conn.commit()
        self.conn.execute("VACUUM")
        edges_after = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        size_after = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {"size_before": size_before, "size_after": size_after,
                "edges_before": edges_before, "edges_after": edges_after,
                "indexed": stats["indexed"], "errors": stats["errors"]}

    # -- indexação -----------------------------------------------------------

    def index_repo(self, force: bool = False, scope: str | None = None,
                   workers: int | None = None,
                   exclude: list[str] | None = None) -> dict:
        self._changes = ChangeSet()      # ver stats["changes"] no retorno
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='indexer_version'").fetchone()
        if row is None or row["value"] != INDEXER_VERSION:
            force = True
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('indexer_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (INDEXER_VERSION,))
            self.conn.commit()
        # escopo parcial: acumula o prefixo pedido no escopo persistido; sem
        # `scope`, respeita o que já estiver salvo (vazio = repo inteiro).
        scopes = get_index_scopes(self.conn)
        if scope is not None:
            ns = _norm_scope(scope)
            if ns and ns not in scopes:
                scopes = sorted(scopes + [ns])
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('index_scopes', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(scopes),))
            self.conn.commit()
        scope_arg = scopes or None
        # exclusões: política do host guardada no índice. `exclude=None` mantém
        # a salva; uma lista SUBSTITUI (é política, não acumulável como o
        # escopo); `[]` limpa. Nunca exige escrever arquivo no repo do usuário.
        if exclude is not None:
            _set_meta_list(self.conn, "index_excludes",
                           [str(p) for p in exclude if str(p).strip()])
        excludes = get_index_excludes(self.conn)
        spec = load_ignore_spec(self.root, excludes)
        files = list(iter_source_files(self.root, spec, scope_arg))
        if workers is None:
            workers = min(INDEX_WORKERS_MAX, os.cpu_count() or 1)
        stats = {"scanned": len(files), "indexed": 0, "removed": 0, "errors": 0}
        if workers > 1 and len(files) >= PARALLEL_MIN_FILES:
            seen = self._index_files_parallel(files, force, workers, stats)
        else:
            seen = self._index_files_serial(files, force, stats)
        # arquivos que sumiram do disco (delete/rename/branch switch) — só
        # dentro do escopo, para não apagar o que outra subárvore indexou.
        for row in self.conn.execute("SELECT path FROM files").fetchall():
            if row["path"] not in seen and in_scope(row["path"], scope_arg):
                self.remove_file(row["path"])
                stats["removed"] += 1
        rank.mark_dirty(self.conn)
        community.mark_dirty(self.conn)
        self.conn.commit()
        self.resolve_edges()
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_full_scan', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(int(time.time())),),
        )
        self.conn.commit()
        stats["changes"] = self._changes.as_dict()
        self.last_changes = stats["changes"]
        self._changes = None
        return stats

    def diagnose_file(self, rel: str) -> str | None:
        """Re-tenta parse+extract SEM tocar o banco e devolve o motivo da falha
        (ou None se hoje parseia). Usado pelo `doctor --why`."""
        path = self.root / rel
        lang = language_for(rel)
        if lang is None:
            return "sem linguagem dedicada (extensão não reconhecida)"
        try:
            data = path.read_bytes()
        except OSError as e:
            return f"leitura falhou: {e}"
        if len(data) > MAX_FILE_SIZE:
            return f"arquivo grande demais ({len(data)} > {MAX_FILE_SIZE} bytes)"
        try:
            tree = get_parser(lang).parse(data)
            extract.extract(lang, data, module_fqn_for(rel), tree)
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        return None

    def index_file(self, rel: str, force: bool = False, data: bytes | None = None) -> bool:
        """Re-indexa um arquivo (com retry-on-locked p/ escrita concorrente).

        Popula `self.last_changes` com os símbolos que entraram/saíram/mudaram
        de assinatura — a menos que já haja uma passada maior coletando (ex.:
        index_repo), caso em que contribui para ela."""
        own = self._changes is None
        if own:
            self._changes = ChangeSet()
        try:
            return retry_on_locked(
                lambda: self._index_file(rel, force=force, data=data))
        finally:
            if own:
                self.last_changes = self._changes.as_dict()
                self._changes = None

    def _index_file(self, rel: str, force: bool = False, data: bytes | None = None) -> bool:
        """Re-indexa um arquivo em transação PRÓPRIA (caminho incremental:
        watcher/read-repair). Retorna True se o índice mudou."""
        prep = self._prepare(rel, force, data)
        if prep is None:
            return False
        if prep[0] == "unchanged":
            _, row, st = prep
            self.conn.execute(
                "UPDATE files SET mtime=?, size=? WHERE id=?",
                (int(st.st_mtime), st.st_size, row["id"]))
            self.conn.commit()
            return False
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN")
            self._write_parsed(cur, rel, prep)
            rank.mark_dirty(self.conn)
            community.mark_dirty(self.conn)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return True

    def _flush_wal(self) -> None:
        """Checkpoint(TRUNCATE): grava o WAL no .db e ENCOLHE o arquivo -wal. O
        autocheckpoint (passivo) reaproveita espaço mas não encolhe, e pode ser
        bloqueado — este é explícito. Não-fatal se um leitor concorrente segurar."""
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            log.debug("wal_checkpoint adiado: %s", e)

    def _executemany_chunked(self, cur, sql: str, rows: list) -> None:
        """executemany em blocos com commit (+ checkpoint periódico) entre eles,
        para não acumular um WAL gigante numa transação única — a origem do stall
        ao indexar repos enormes. Cada bloco commitado é durável: resolve_edges
        vira resumível (re-rodar religa só o que faltou; é idempotente)."""
        for i in range(0, len(rows), WRITE_CHUNK):
            cur.executemany(sql, rows[i:i + WRITE_CHUNK])
            self.conn.commit()
            if (i // WRITE_CHUNK) % 4 == 3:
                self._flush_wal()

    def _mark_failed(self, cur, rel: str) -> None:
        cur.execute(
            "INSERT INTO files(path, language, content_hash, size, mtime, "
            "parse_status, indexed_at) VALUES(?,?,?,?,?,'failed',?) "
            "ON CONFLICT(path) DO UPDATE SET parse_status='failed'",
            (rel, language_for(rel), "", 0, 0, int(time.time())))

    def _index_files_serial(self, files, force, stats) -> set[str]:
        seen: set[str] = set()
        cur = self.conn.cursor()
        cur.execute("BEGIN")
        pending = batches = 0
        for rel in files:
            seen.add(rel)
            cur.execute("SAVEPOINT f")           # isolamento de erro por arquivo
            try:
                prep = self._prepare(rel, force)
                if prep is None:                 # sem linguagem / grande demais
                    cur.execute("RELEASE f")
                elif prep[0] == "unchanged":
                    _, frow, st = prep
                    cur.execute("UPDATE files SET mtime=?, size=? WHERE id=?",
                                (int(st.st_mtime), st.st_size, frow["id"]))
                    cur.execute("RELEASE f")
                else:
                    self._write_parsed(cur, rel, prep)
                    cur.execute("RELEASE f")
                    stats["indexed"] += 1
            except Exception as e:
                cur.execute("ROLLBACK TO f")
                cur.execute("RELEASE f")
                stats["errors"] += 1
                log.warning("falha ao indexar %s: %s: %s",
                            rel, type(e).__name__, e)
                log.debug("traceback de %s", rel, exc_info=True)
                self._mark_failed(cur, rel)
            pending += 1
            if pending >= BULK_BATCH:            # flush do lote
                cur.execute("COMMIT")
                batches += 1
                if batches % CHECKPOINT_EVERY_BATCHES == 0:
                    self._flush_wal()            # encolhe o WAL em índice enorme
                cur.execute("BEGIN")
                pending = 0
        cur.execute("COMMIT")
        return seen

    def _prepare_pure(self, rel: str, force: bool, known: dict):
        """Versão thread-safe de _prepare: NÃO toca self.conn. `known`
        (path→content_hash, lido 1x na main thread) permite pular inalterados sem
        parsear. Roda em worker; qualquer exceção vira ('error', rel, msg)."""
        try:
            path = self.root / rel
            lang = language_for(rel)
            if lang is None:
                return ("skip", rel)
            data = path.read_bytes()
            if len(data) > MAX_FILE_SIZE:
                return ("skip", rel)
            h = content_hash(data)
            st = path.stat()
            if not force and known.get(rel) == h:
                return ("unchanged", rel, st, h)
            tree = get_parser(lang).parse(data)
            if lang == "cpp" and rel.endswith(".h") and tree.root_node.has_error:
                c_tree = get_parser("c").parse(data)
                if not c_tree.root_node.has_error:
                    lang, tree = "c", c_tree
            syms, refs = _extract_file(lang, data, rel, tree, h)
            status = "partial" if tree.root_node.has_error else "ok"
            return ("changed", rel, st, h, lang, syms, refs, status)
        except Exception as e:
            return ("error", rel, f"{type(e).__name__}: {e}")

    def _index_files_parallel(self, files, force, workers, stats) -> set[str]:
        """Prepare em threads (I/O + tree-sitter soltam o GIL), escrita serial no
        writer único. `ex.map` devolve na ORDEM de entrada → a ordem de escrita é
        idêntica à serial → mesmos ids e mesmo grafo. Memória limitada por chunk."""
        from concurrent.futures import ThreadPoolExecutor

        known = {r["path"]: r["content_hash"]
                 for r in self.conn.execute("SELECT path, content_hash FROM files")}
        id_map = {r["path"]: r["id"]
                  for r in self.conn.execute("SELECT path, id FROM files")}
        seen: set[str] = set(files)
        cur = self.conn.cursor()
        cur.execute("BEGIN")
        pending = batches = 0
        chunk = max(BULK_BATCH, workers * 16)     # limita prepares em voo (memória)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i in range(0, len(files), chunk):
                batch = files[i:i + chunk]
                for res in ex.map(lambda r: self._prepare_pure(r, force, known), batch):
                    rel = res[1]
                    cur.execute("SAVEPOINT f")
                    try:
                        tag = res[0]
                        if tag == "skip":
                            cur.execute("RELEASE f")
                        elif tag == "unchanged":
                            _, r, st, h = res
                            cur.execute("UPDATE files SET mtime=?, size=? WHERE path=?",
                                        (int(st.st_mtime), st.st_size, r))
                            cur.execute("RELEASE f")
                        elif tag == "changed":
                            _, r, st, h, lang, syms, refs, status = res
                            row = {"id": id_map[r]} if r in id_map else None
                            self._write_parsed(
                                cur, r, ("changed", row, st, lang, h, syms, refs, status))
                            cur.execute("RELEASE f")
                            stats["indexed"] += 1
                        else:                     # ('error', rel, msg)
                            cur.execute("ROLLBACK TO f")
                            cur.execute("RELEASE f")
                            stats["errors"] += 1
                            log.warning("falha ao indexar %s: %s", rel, res[2])
                            self._mark_failed(cur, rel)
                    except Exception as e:
                        cur.execute("ROLLBACK TO f")
                        cur.execute("RELEASE f")
                        stats["errors"] += 1
                        log.warning("falha ao escrever %s: %s: %s",
                                    rel, type(e).__name__, e)
                        self._mark_failed(cur, rel)
                    pending += 1
                    if pending >= BULK_BATCH:
                        cur.execute("COMMIT")
                        batches += 1
                        if batches % CHECKPOINT_EVERY_BATCHES == 0:
                            self._flush_wal()
                        cur.execute("BEGIN")
                        pending = 0
        cur.execute("COMMIT")
        return seen

    def _prepare(self, rel: str, force: bool, data: bytes | None = None):
        """Lê+parseia um arquivo, SEM tocar o banco (além de 1 SELECT de frescor).
        Retorna: None (pular) | ('unchanged', row, st) | ('changed', row, st,
        lang, h, syms, refs, status). Separado de _write_parsed para o modo em
        lote do index_repo poder agrupar as escritas."""
        path = self.root / rel
        lang = language_for(rel)
        if lang is None:
            return None
        if data is None:
            data = path.read_bytes()
        if len(data) > MAX_FILE_SIZE:
            return None
        h = content_hash(data)
        st = path.stat()
        row = self.conn.execute(
            "SELECT id, content_hash FROM files WHERE path=?", (rel,)).fetchone()
        if row is not None and row["content_hash"] == h and not force:
            return ("unchanged", row, st)
        tree = get_parser(lang).parse(data)
        if lang == "cpp" and rel.endswith(".h") and tree.root_node.has_error:
            # .h é ambíguo: header C parseado como C++ pode falhar — tenta C
            c_tree = get_parser("c").parse(data)
            if not c_tree.root_node.has_error:
                lang, tree = "c", c_tree
        syms, refs = _extract_file(lang, data, rel, tree, h)
        status = "partial" if tree.root_node.has_error else "ok"
        return ("changed", row, st, lang, h, syms, refs, status)

    def _write_parsed(self, cur, rel: str, prep) -> None:
        """Escreve símbolos+arestas de um arquivo já parseado (tupla 'changed' de
        _prepare). NÃO gerencia transação — o chamador faz BEGIN/COMMIT (incre-
        mental) ou SAVEPOINT (lote). Inserts em `executemany` em vez de execute
        por linha. (No index completo o custo é dominado por resolve_edges e pela
        manutenção dos índices de edges, não por esta escrita.)"""
        _, row, st, lang, h, syms, refs, status = prep
        saved_descriptions: list = []
        track = self._changes
        old_sigs: dict[str, str | None] = {}
        if row is not None:
            file_id = row["id"]
            if track is not None:
                # só para arquivo PRÉ-EXISTENTE: no índice inicial não há
                # consulta extra (tudo é 'added', derivado dos próprios syms)
                # kind='file' fora: o host quer saber que símbolo DECLARADO
                # mudou; "o arquivo existe" já está em counts/files e só
                # inflaria o diff de toda integração
                old_sigs = {r["fqn"]: r["signature"] for r in cur.execute(
                    "SELECT fqn, signature FROM symbols WHERE file_id=? "
                    "AND kind<>'file'", (file_id,))}
            # descrições L3 sobrevivem ao re-index (ids de símbolo são estáveis;
            # source_hash antigo preservado → stale detectável)
            saved_descriptions = cur.execute(
                "SELECT d.symbol_id, d.scope, d.content, d.source_hash, "
                "d.model, d.generated_at FROM descriptions d "
                "JOIN symbols s ON d.symbol_id=s.id WHERE s.file_id=?",
                (file_id,)).fetchall()
            cur.execute(
                "DELETE FROM symbols_fts WHERE symbol_id IN "
                "(SELECT id FROM symbols WHERE file_id=?)", (file_id,))
            cur.execute("DELETE FROM edges WHERE file_id=?", (file_id,))
            cur.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
            cur.execute(
                "UPDATE files SET language=?, content_hash=?, size=?, mtime=?, "
                "parse_status=?, indexed_at=? WHERE id=?",
                (lang, h, st.st_size, int(st.st_mtime), status,
                 int(time.time()), file_id))
        else:
            cur.execute(
                "INSERT INTO files(path, language, content_hash, size, mtime, "
                "parse_status, indexed_at) VALUES(?,?,?,?,?,?,?)",
                (rel, lang, h, st.st_size, int(st.st_mtime), status,
                 int(time.time())))
            file_id = cur.lastrowid

        if track is not None:
            new_sigs: dict[str, str | None] = {}
            for s in syms:
                if s.kind != "file":          # ver old_sigs acima
                    new_sigs.setdefault(s.fqn, s.signature)
            for fqn, sig in new_sigs.items():
                if fqn not in old_sigs:
                    track.add(fqn)
                elif (old_sigs[fqn] or "") != (sig or ""):
                    track.signature(fqn, old_sigs[fqn], sig)
            for fqn in old_sigs:
                if fqn not in new_sigs:
                    track.remove(fqn)

        ordinals: dict[tuple[str, str], int] = {}
        fqn_to_uid: dict[str, str] = {}
        all_uids: set[str] = set()
        sym_rows: list = []
        fts_rows: list = []
        for s in syms:
            ordinal = ordinals.get((s.fqn, s.kind), 0)
            ordinals[(s.fqn, s.kind)] = ordinal + 1
            uid = symbol_uid(rel, s.fqn, s.kind, ordinal)
            fqn_to_uid.setdefault(s.fqn, uid)
            all_uids.add(uid)
            sym_rows.append((uid, file_id, s.kind, s.name, s.fqn, s.signature,
                             s.doc, s.start_line, s.start_col, s.end_line,
                             s.end_col, s.body_hash, s.visibility))
            fts_rows.append((uid, s.name, s.fqn, s.doc or ""))
        if sym_rows:
            cur.executemany(
                "INSERT INTO symbols(id, file_id, kind, name, fqn, signature, doc, "
                "start_line, start_col, end_line, end_col, body_hash, visibility) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", sym_rows)
            cur.executemany(
                "INSERT INTO symbols_fts(symbol_id, name, fqn, doc) VALUES(?,?,?,?)",
                fts_rows)
        for d in saved_descriptions:
            if d["symbol_id"] in all_uids:
                cur.execute(
                    "INSERT INTO descriptions(symbol_id, scope, content, "
                    "source_hash, model, generated_at) VALUES(?,?,?,?,?,?)",
                    (d["symbol_id"], d["scope"], d["content"],
                     d["source_hash"], d["model"], d["generated_at"]))
        parent_updates = [(fqn_to_uid[s.parent_fqn], fqn_to_uid[s.fqn])
                          for s in syms
                          if s.parent_fqn and s.parent_fqn in fqn_to_uid]
        if parent_updates:
            cur.executemany("UPDATE symbols SET parent_id=? WHERE id=?",
                            parent_updates)
        seen_refs: set[tuple] = set()
        edge_rows: list = []
        for r in refs:
            src_id = fqn_to_uid.get(r.src_fqn) if r.src_fqn else None
            # refs idênticas no mesmo site são redundantes; deduplicar aqui
            # garante ≤1 aresta resolvida por site (casando com o índice único)
            key = (r.kind, src_id, r.dst_name, r.line, r.col)
            if key in seen_refs:
                continue
            seen_refs.add(key)
            edge_rows.append((r.kind, src_id, r.dst_name, file_id, r.line, r.col))
        if edge_rows:
            cur.executemany(
                "INSERT INTO edges(kind, src, dst, dst_name, file_id, line, col, "
                "confidence, resolver) VALUES(?,?,NULL,?,?,?,?,'possible','l0')",
                edge_rows)

    def remove_file(self, rel: str) -> None:
        retry_on_locked(lambda: self._remove_file(rel))

    def _remove_file(self, rel: str) -> None:
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()
        if row is None:
            return
        if self._changes is not None:      # arquivo sumiu → símbolos saíram
            for r in self.conn.execute(                 # kind='file' fora:
                    "SELECT fqn FROM symbols WHERE file_id=? "   # só símbolo
                    "AND kind<>'file'", (row["id"],)):          # DECLARADO
                self._changes.remove(r["fqn"])
        self.conn.execute(
            "DELETE FROM symbols_fts WHERE symbol_id IN "
            "(SELECT id FROM symbols WHERE file_id=?)", (row["id"],))
        self.conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
        rank.mark_dirty(self.conn)
        community.mark_dirty(self.conn)
        self.conn.commit()

    # -- resolução de arestas (docs/DESIGN.md §1.3) ---------------------------

    def resolve_edges(self) -> None:
        danglings = self.conn.execute(
            "SELECT id, kind, src, dst_name, file_id, line, col "
            "FROM edges WHERE dst IS NULL"
        ).fetchall()
        if not danglings:
            return
        cur = self.conn.cursor()
        lang_of: dict[int, str] = {}
        path_of: dict[int, str] = {}
        for r in cur.execute("SELECT id, language, path FROM files"):
            lang_of[r["id"]] = r["language"]
            path_of[r["id"]] = r["path"]
        # caminho do repo -> símbolo `file` daquele arquivo (alvo dos imports
        # que são caminho e não nome: `<script src>`, `<link href>`, `@import`)
        file_sym: dict[str, str] = {
            path_of[r["file_id"]]: r["id"] for r in cur.execute(
                "SELECT id, file_id FROM symbols WHERE kind='file'")
            if r["file_id"] in path_of}
        # Índice de símbolos EM MEMÓRIA (um único SELECT). O cache-por-guess
        # degradava para ~1 SELECT por dangling quando os guesses são quase únicos
        # (o grosso do custo de resolução em repos grandes): 20k+ queries. Os
        # símbolos cabem em memória; resolver por dict elimina essas queries.
        by_name: dict[str, list] = {}
        for r in cur.execute(
                "SELECT s.id, s.file_id, s.fqn, s.name, s.kind, f.language "
                "FROM symbols s JOIN files f ON s.file_id=f.id ORDER BY s.id"):
            by_name.setdefault(r["name"], []).append(r)

        class_kinds = ("class", "interface", "struct")

        def _by_path(guess: str, from_rel: str) -> str | None:
            """Import que é CAMINHO (não nome) → símbolo `file` do alvo.

            Tenta o caminho como está e, quando não traz extensão, as extensões
            usuais — incluindo o parcial do Sass (`@use "buttons"` →
            `_buttons.scss`) e o `index.*` de diretório. Só casa com arquivo que
            está no índice: caminho para fora do repo ou pacote externo
            (`react`, `node:fs`) não casa e segue dangling, que é o correto.
            """
            if guess.startswith("/"):
                base = guess.lstrip("/")            # relativo à raiz do repo
            else:
                d = posixpath.dirname(from_rel)
                base = posixpath.normpath(posixpath.join(d, guess))
            if base.startswith("..") or base in (".", ""):
                return None                          # escapou da raiz
            cands = [base]
            head, _, tail = base.rpartition("/")
            prefix = f"{head}/" if head else ""
            if "." not in tail:
                cands += [base + e for e in PATH_EXTS]
                cands += [f"{prefix}_{tail}{e}" for e in (".scss", ".sass")]
            cands += [f"{base}/index{e}" for e in PATH_EXTS]
            for c in cands:
                sid = file_sym.get(c)
                if sid is not None:
                    return sid
            return None

        def _dedup_cap(rows) -> list:
            # candidatos distintos por fqn (decl+def de C/C++ = 1 candidato),
            # parando em MAX+1 para detectar ambiguidade (>MAX) sem varrer tudo
            out: dict[str, object] = {}
            for c in rows:
                if c["fqn"] not in out:
                    out[c["fqn"]] = c
                    if len(out) > MAX_CANDIDATES:
                        break
            return list(out.values())

        # decisões coletadas no loop e escritas em lote no fim (executemany):
        # nenhum lookup depende de uma escrita, então diferir é seguro.
        inferred: list = []   # (dst_id, edge_id)
        possible: list = []   # (dst_id, edge_id) — representante do ambíguo
        fanout: list = []     # clones de candidatos extras
        for e in danglings:
            guess = e["dst_name"]
            if not guess or "*" in guess:
                continue
            if e["kind"] == "imports":
                # caminho é evidência mais forte que nome — tenta primeiro
                sid = _by_path(guess, path_of.get(e["file_id"], ""))
                if sid is not None:
                    inferred.append((sid, e["id"]))
                    continue
            if "." in guess:
                # guess qualificado (via import/escopo): match por fqn exato/sufixo
                seg, suffix = guess.rsplit(".", 1)[-1], "." + guess
                cands = _dedup_cap(
                    c for c in by_name.get(seg, ())
                    if c["fqn"] == guess or c["fqn"].endswith(suffix))
            elif e["kind"] in ("calls", "inherits"):
                # nome puro (receptor desconhecido): por nome + kind + MESMA língua
                kinds = CALLABLE_KINDS if e["kind"] == "calls" else class_kinds
                lang = lang_of.get(e["file_id"])
                cands = _dedup_cap(
                    c for c in by_name.get(guess, ())
                    if c["kind"] in kinds and c["language"] == lang)
            elif e["kind"] == "references":
                # uso de estilo: `className="card"` no TSX/HTML → `.card` no
                # CSS. Sem filtro de língua (é justamente o vínculo entre elas);
                # o kind restrito impede casar com uma função chamada `card`.
                cands = _dedup_cap(
                    c for c in by_name.get(guess, ())
                    if c["kind"] in STYLE_DEF_KINDS)
            else:
                continue
            if not cands or len(cands) > MAX_CANDIDATES:
                continue  # permanece dangling — contado na completeness
            if len(cands) == 1:
                inferred.append((cands[0]["id"], e["id"]))
                continue
            same_file = [c for c in cands if c["file_id"] == e["file_id"]]
            if len(same_file) == 1:
                inferred.append((same_file[0]["id"], e["id"]))
                continue
            # ambíguo (2..MAX candidatos): representante na aresta original +
            # um clone por candidato extra (recall p/ callers/impact). INSERT OR
            # IGNORE + índice único garantem idempotência: re-resolver nunca
            # duplica (foi a causa do bloat histórico, não o fan-out em si).
            possible.append((cands[0]["id"], e["id"]))
            for c in cands[1:]:
                fanout.append((e["kind"], e["src"], c["id"], guess,
                               e["file_id"], e["line"], e["col"]))
        # escrita em blocos: em repos enormes estas listas têm milhões de linhas;
        # uma transação única faria o WAL explodir e o commit final travar.
        self._executemany_chunked(
            cur, "UPDATE edges SET dst=?, confidence='inferred' WHERE id=?", inferred)
        self._executemany_chunked(
            cur, "UPDATE edges SET dst=?, confidence='possible' WHERE id=?", possible)
        self._executemany_chunked(
            cur, "INSERT OR IGNORE INTO edges(kind, src, dst, dst_name, "
                 "file_id, line, col, confidence, resolver) "
                 "VALUES(?,?,?,?,?,?,?,'possible','l0')", fanout)
        self.conn.commit()
        self._flush_wal()
