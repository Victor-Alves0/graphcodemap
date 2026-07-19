"""Resolver L1 para Python via jedi.

Para cada aresta de call L0 do arquivo, roda `goto` na posição do nome do
callee. Exatamente UMA definição, dentro do repo → promove a aresta:
dst = símbolo da definição, confidence = 'certain', resolver = 'l1',
e remove os clones 'possible' do mesmo call site.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class JediResolver:
    languages = ("python",)

    @staticmethod
    def available() -> bool:
        try:
            import jedi  # noqa: F401
            return True
        except ImportError:
            return False

    def __init__(self, root: Path) -> None:
        import jedi

        self.root = root
        self.project = jedi.Project(str(root))
        # in-process: ~10x mais rápido e sem subprocess (funciona em sandbox)
        self.environment = jedi.api.environment.InterpreterEnvironment()

    def refine_file(self, conn: sqlite3.Connection, root: Path,
                    rel: str, file_id: int) -> int:
        import jedi

        edges = conn.execute(
            "SELECT id, line, col, dst_name FROM edges "
            "WHERE file_id=? AND kind='calls' AND resolver='l0' AND col IS NOT NULL",
            (file_id,)).fetchall()
        if not edges:
            return 0
        path = root / rel
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        script = jedi.Script(source, path=str(path), project=self.project,
                             environment=self.environment)
        promoted = 0
        seen_sites: set[tuple[int, int]] = set()
        for e in edges:
            site = (e["line"], e["col"])
            if site in seen_sites:  # clones 'possible' compartilham o site
                continue
            seen_sites.add(site)
            try:
                defs = script.goto(e["line"], e["col"], follow_imports=True,
                                   follow_builtin_imports=False)
            except Exception:
                continue
            defs = [d for d in defs if d.module_path is not None and d.line]
            if len(defs) != 1:
                continue
            try:
                drel = Path(defs[0].module_path).resolve() \
                    .relative_to(root).as_posix()
            except ValueError:
                continue  # definição fora do repo (stdlib/site-packages)
            drow = conn.execute("SELECT id FROM files WHERE path=?", (drel,)).fetchone()
            if drow is None:
                continue
            srow = conn.execute(
                "SELECT id FROM symbols WHERE file_id=? AND start_line<=? "
                "AND end_line>=? ORDER BY (end_line-start_line) LIMIT 1",
                (drow["id"], defs[0].line, defs[0].line)).fetchone()
            if srow is None:
                continue
            conn.execute(
                "UPDATE edges SET dst=?, confidence='certain', resolver='l1' "
                "WHERE id=?", (srow["id"], e["id"]))
            # clones 'possible' do mesmo site viraram redundância
            conn.execute(
                "DELETE FROM edges WHERE kind='calls' AND file_id=? AND line=? "
                "AND col=? AND id!=? AND resolver='l0' AND confidence='possible'",
                (file_id, e["line"], e["col"], e["id"]))
            promoted += 1
        return promoted
