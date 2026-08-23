"""M2: file watcher com debounce (docs/DESIGN.md §2.2).

Mantém o índice quente durante a sessão; a garantia final continua sendo o
read-repair na query (§2.3) — o watcher é otimização, não fonte de verdade.
Mudanças em .git/HEAD ou .git/refs (troca de branch, pull) disparam rescan
completo em vez de re-index por arquivo.

Usa conexão SQLite própria (thread separada); WAL permite leitor+escritor.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .indexer import (Indexer, get_index_excludes, get_index_scopes, in_scope,
                      load_ignore_spec)
from .languages import language_for
from .log import get as _get_log

log = _get_log(__name__)


class Watcher:
    def __init__(self, root: str | Path, db_path: str | Path | None = None,
                 debounce: float = 1.0) -> None:
        self.root = Path(root).resolve()
        self._db_path = db_path
        self.debounce = debounce
        self.ix: Indexer | None = None  # criado na thread do drain
        self._scopes, self._excludes = self._load_policy()
        # spec inclui as exclusões do host (meta), não só os ignores do repo
        self.spec = load_ignore_spec(self.root, self._excludes)
        self._pending: set[str] = set()
        self._full_rescan = False
        self._draining = False
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._observer = None

    def _load_policy(self) -> tuple[list[str], list[str]]:
        """(escopos, exclusões) persistidos no índice — o watcher respeita a
        mesma política do index_repo, senão editar um arquivo fora do escopo (ou
        excluído) o traria de volta ao grafo pelas costas."""
        from .db import connect, default_db_path

        try:
            conn = connect(self._db_path or default_db_path(self.root))
            try:
                return get_index_scopes(conn), get_index_excludes(conn)
            finally:
                conn.close()
        except Exception:
            return [], []

    # -- eventos -------------------------------------------------------------

    def _note(self, path: str) -> None:
        try:
            rel = Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return
        if rel.startswith(".git/"):
            if rel == ".git/HEAD" or rel.startswith(".git/refs"):
                with self._lock:
                    self._full_rescan = True
                self._schedule()
            return
        if rel.startswith(".codegraph/"):
            return
        from . import l1

        marker = l1.is_project_marker(rel)
        if self.spec.match_file(rel) and not marker:
            return
        if language_for(rel) is None and not marker:
            if rel in (".gitignore", ".codegraphignore"):
                self.spec = load_ignore_spec(self.root)
                with self._lock:
                    self._full_rescan = True
                self._schedule()
            return
        # Marker pode ficar acima do diretório indexado e ainda mudar a raiz do
        # language server daquele escopo. Arquivos-fonte continuam obedecendo à
        # política persistida normalmente.
        if not marker and not in_scope(rel, self._scopes):
            return
        with self._lock:
            self._pending.add(rel)
        self._schedule()

    def _schedule(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.debounce, self.drain)
        self._timer.daemon = True
        self._timer.start()

    # -- aplicação -----------------------------------------------------------

    def drain(self) -> dict:
        """Aplica o lote pendente. Retorna estatísticas (também usado em testes)."""
        with self._drain_lock:
            with self._lock:
                self._draining = True
            try:
                return self._drain()
            finally:
                with self._lock:
                    self._draining = False

    def _drain(self) -> dict:
        if self.ix is None:
            self.ix = Indexer(self.root, self._db_path)
        with self._lock:
            batch, self._pending = self._pending, set()
            full, self._full_rescan = self._full_rescan, False
        stats = {"indexed": 0, "removed": 0, "errors": 0, "full": full}
        if full:
            repo_stats = self.ix.index_repo()
            stats["indexed"] = repo_stats["indexed"]
            stats["removed"] = repo_stats["removed"]
            return stats
        from . import l1

        marker_batch = {rel for rel in batch if l1.is_project_marker(rel)}
        changed = False
        for rel in sorted(batch):
            if rel in marker_batch:
                continue  # evento semântico; marker não entra na tabela files
            path = self.root / rel
            try:
                if path.is_file():
                    if self.ix.index_file(rel):
                        stats["indexed"] += 1
                        changed = True
                else:
                    self.ix.remove_file(rel)
                    stats["removed"] += 1
                    changed = True
            except Exception as e:
                stats["errors"] += 1
                log.warning("watcher: falha ao re-indexar %s: %s: %s",
                            rel, type(e).__name__, e)
                log.debug("traceback de %s", rel, exc_info=True)
        if changed:
            self.ix.resolve_edges()
        if changed or marker_batch:
            try:
                l1.refine(self.ix, rels=sorted(batch))
            except Exception as e:
                log.debug("watcher: refine L1 falhou no lote: %s: %s",
                          type(e).__name__, e, exc_info=True)
        return stats

    def is_current(self) -> bool:
        """True se o watcher está vivo E drenado — sem eventos pendentes nem
        rescan pendente. Nesse estado o índice reflete tudo que o watcher
        observou, então a varredura de frescor O(N) da query é redundante e pode
        ser pulada. Durante o debounce (evento anotado, ainda não aplicado)
        retorna False, e a query cai na varredura — a garantia é preservada.
        Não cobre eventos que o watchdog tenha PERDIDO (overflow); por isso o
        chamador mantém uma varredura-backstop periódica."""
        obs = self._observer
        if obs is None or not obs.is_alive():
            return False
        with self._lock:
            return (not self._pending and not self._full_rescan
                    and not self._draining)

    # -- ciclo de vida -------------------------------------------------------

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory:
                    return
                watcher._note(event.src_path)
                dest = getattr(event, "dest_path", None)
                if dest:
                    watcher._note(dest)

        self._observer = Observer()
        self._observer.schedule(Handler(), str(self.root), recursive=True)
        self._observer.daemon = True
        self._observer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
        if self.ix is not None:
            self.ix.close()
