"""Resolver L1 para JS/TS via TypeScript LanguageService (processo node).

Requer node + módulo typescript. Descoberta:
- node: env CODEGRAPH_NODE → PATH → <repo-dev>/tools/node/node.exe
- typescript: env CODEGRAPH_TS_DIR → <repo-dev>/tools/ts → node_modules do repo
  analisado/ancestrais → instalações globais usuais
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import promote

_DEV_ROOT = Path(__file__).resolve().parents[3]  # layout src/: raiz do repo

_CALLABLE_KINDS = frozenset({
    "function", "local function", "method", "class", "local class",
    "constructor", "getter", "setter",
})


def _find_node() -> str | None:
    env = os.environ.get("CODEGRAPH_NODE")
    if env and Path(env).is_file():
        return env
    which = shutil.which("node")
    if which:
        return which
    dev = _DEV_ROOT / "tools" / "node" / "node.exe"
    return str(dev) if dev.is_file() else None


def _ts_candidates(root: Path | None = None) -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("CODEGRAPH_TS_DIR")
    if env:
        out.append(Path(env))
    out.append(_DEV_ROOT / "tools" / "ts" / "node_modules" / "typescript")
    base = Path(root or Path.cwd()).resolve()
    out += [p / "node_modules" / "typescript" for p in (base, *base.parents)]
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "npm" / "node_modules" / "typescript")
    out += [Path("/usr/local/lib/node_modules/typescript"),
            Path("/usr/lib/node_modules/typescript")]
    node = _find_node()
    if node:
        nb = Path(node).resolve().parent
        out += [nb / "node_modules" / "typescript",
                nb.parent / "lib" / "node_modules" / "typescript"]
    return out


def _find_ts(root: Path | None = None) -> str | None:
    for candidate in _ts_candidates(root):
        if (candidate / "lib" / "typescript.js").is_file():
            return str(candidate)
    return None


class TsLsResolver:
    languages = ("javascript", "typescript", "tsx")
    cmd_name = "typescript"
    cmd_env = "CODEGRAPH_TS_DIR"
    # o ts_service relativiza req/resp à raiz de spawn E varre a partir dela;
    # mudá-la quebraria o casamento com os caminhos repo-relativos do índice.
    # Por isso o TS é sempre aberto na raiz do repo (root_markers vazio); tornar
    # a resolução tsconfig-aware por pacote é trabalho de profundidade (Tier 3).
    root_markers: tuple[str, ...] = ()

    @staticmethod
    def available() -> bool:
        return _find_node() is not None and _find_ts() is not None

    @staticmethod
    def available_for_root(root: Path) -> bool:
        return _find_node() is not None and _find_ts(root) is not None

    def __init__(self, root: Path, project_root: Path | None = None) -> None:
        self.root = root                       # sempre a raiz do repo (ver acima)
        self._cache: dict[tuple[str, int, int], dict | None] = {}
        service = Path(__file__).with_name("ts_service.js")
        node, ts_dir = _find_node(), _find_ts(root)
        if node is None or ts_dir is None:
            raise RuntimeError("node/typescript indisponível para a raiz analisada")
        self.proc = subprocess.Popen(
            [node, str(service), ts_dir, str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        self._seq = 0

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def _query(self, rel: str, line: int, col: int) -> dict | None:
        key = (rel, line, col)
        if key in self._cache:                 # memo por run (snapshot consistente)
            return self._cache[key]
        if self.proc.poll() is not None:
            return None
        self._seq += 1
        req = {"id": self._seq, "file": rel, "line": line, "col": col}
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            raw = self.proc.stdout.readline()
            resp = json.loads(raw) if raw else None
        except Exception:
            resp = None
        self._cache[key] = resp
        return resp

    def refine_file(self, conn: sqlite3.Connection, root: Path,
                    rel: str, file_id: int) -> int:
        edges = conn.execute(
            "SELECT id, line, col, dst_name FROM edges "
            "WHERE file_id=? AND kind='calls' AND resolver='l0' AND col IS NOT NULL",
            (file_id,)).fetchall()
        promoted = 0
        seen_sites: set[tuple[int, int]] = set()
        for e in edges:
            site = (e["line"], e["col"])
            if site in seen_sites:
                continue
            seen_sites.add(site)
            resp = self._query(rel, e["line"], e["col"])
            if not resp or "defs" not in resp:
                continue
            # multi-def (overloads) não é descartado: cada def vira um alvo;
            # promote decide certain (1) vs fan-out inferred (2..MAX).
            targets = []
            expected_name = e["dst_name"].rsplit(".", 1)[-1]
            for d in resp["defs"]:
                if d.get("kind") not in _CALLABLE_KINDS:
                    continue
                # O LanguageService às vezes devolve o tipo/valor retornado em
                # vez do método invocado (ex.: ``array.pop()`` → uma função que
                # pode sair do array). Isso NÃO é o callee deste site.
                if d.get("name") != expected_name:
                    continue
                sid = promote.target_symbol(conn, d["file"], d["line"],
                                            d.get("col"), d.get("name"))
                if sid is not None:
                    targets.append(sid)
            promoted += promote.apply(conn, file_id, e, targets)
        return promoted
