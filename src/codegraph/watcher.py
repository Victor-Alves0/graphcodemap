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

from .indexer import Indexer, load_ignore_spec
from .languages import language_for


class Watcher:
    def __init__(self, root: str | Path, db_path: str | Path | None = None,
                 debounce: float = 1.0) -> None:
        self.root = Path(root).resolve()
        self._db_path = db_path
        self.debounce = debounce
        self.ix: Indexer | None = None  # criado na thread do drain
        self.spec = load_ignore_spec(self.root)
        self._pending: set[str] = set()
        self._full_rescan = False
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._observer = None

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
        if rel.startswith(".codegraph/") or self.spec.match_file(rel):
            return
        if language_for(rel) is None:
            if rel in (".gitignore", ".codegraphignore"):
                self.spec = load_ignore_spec(self.root)
                with self._lock:
                    self._full_rescan = True
                self._schedule()
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
            return self._drain()

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
        changed = False
        for rel in sorted(batch):
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
            except Exception:
                stats["errors"] += 1
        if changed:
            self.ix.resolve_edges()
            try:
                from . import l1

                l1.refine(self.ix, rels=sorted(batch))
            except Exception:
                pass
        return stats

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
