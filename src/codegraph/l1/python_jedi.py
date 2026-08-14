"""Resolver L1 para Python via jedi.

Para cada aresta de call L0 do arquivo, roda `goto` na posição do nome do
callee. Exatamente UMA definição, dentro do repo → promove a aresta:
dst = símbolo da definição, confidence = 'certain', resolver = 'l1',
e remove os clones 'possible' do mesmo call site.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import promote


# ``param``/``statement`` apontam para um nome que contém um callable, não para
# a implementação chamada. Ex.: ``def run(cb): cb()``; mapear a linha do
# parâmetro pelo menor símbolo envolvente fabricava ``run -> run [certain]``.
_CALLABLE_TYPES = frozenset({"function", "class"})


class JediResolver:
    languages = ("python",)
    # raiz de projeto Python (jedi.Project infere sys.path a partir dela).
    root_markers = ("pyproject.toml", "setup.py", "setup.cfg")

    @staticmethod
    def available() -> bool:
        try:
            import jedi  # noqa: F401
            return True
        except ImportError:
            return False

    def __init__(self, root: Path, project_root: Path | None = None) -> None:
        import jedi

        # root = raiz do repo (paths repo-relativos); project_root = subprojeto
        # (a raiz que o jedi.Project usa p/ inferir sys.path). Ver l1/roots.py.
        self.root = Path(root)
        self.project = jedi.Project(str(project_root or root))
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
            # multi-def não é descartado: cada def no repo vira um alvo;
            # promote decide certain (1) vs fan-out inferred (2..MAX).
            targets = []
            for d in defs:
                if d.module_path is None or not d.line:
                    continue
                if d.type not in _CALLABLE_TYPES:
                    continue
                try:
                    drel = Path(d.module_path).resolve().relative_to(root).as_posix()
                except ValueError:
                    continue  # definição fora do repo (stdlib/site-packages)
                sid = promote.target_symbol(conn, drel, d.line, dname=d.name)
                if sid is not None:
                    targets.append(sid)
            promoted += promote.apply(conn, file_id, e, targets)
        return promoted
