"""Resolver L1 para JS/TS via TypeScript LanguageService (processo node).

Requer node + módulo typescript. Descoberta:
- node: env CODEGRAPH_NODE → PATH → <repo-dev>/tools/node/node.exe
- typescript: env CODEGRAPH_TS_DIR → <repo-dev>/tools/ts/node_modules/typescript
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

_DEV_ROOT = Path(__file__).resolve().parents[3]  # layout src/: raiz do repo


def _find_node() -> str | None:
    env = os.environ.get("CODEGRAPH_NODE")
    if env and Path(env).is_file():
        return env
    which = shutil.which("node")
    if which:
        return which
    dev = _DEV_ROOT / "tools" / "node" / "node.exe"
    return str(dev) if dev.is_file() else None


def _find_ts() -> str | None:
    env = os.environ.get("CODEGRAPH_TS_DIR")
    if env and Path(env).is_dir():
        return env
    dev = _DEV_ROOT / "tools" / "ts" / "node_modules" / "typescript"
    return str(dev) if dev.is_dir() else None


class TsLsResolver:
    languages = ("javascript", "typescript", "tsx")

    @staticmethod
    def available() -> bool:
        return _find_node() is not None and _find_ts() is not None

    def __init__(self, root: Path) -> None:
        self.root = root
        service = Path(__file__).with_name("ts_service.js")
        self.proc = subprocess.Popen(
            [_find_node(), str(service), _find_ts(), str(root)],
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
        if self.proc.poll() is not None:
            return None
        self._seq += 1
        req = {"id": self._seq, "file": rel, "line": line, "col": col}
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            raw = self.proc.stdout.readline()
            return json.loads(raw) if raw else None
        except Exception:
            return None

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
            if not resp or "file" not in resp:
                continue
            drow = conn.execute("SELECT id FROM files WHERE path=?",
                                (resp["file"],)).fetchone()
            if drow is None:
                continue
            srow = conn.execute(
                "SELECT id FROM symbols WHERE file_id=? AND start_line<=? "
                "AND end_line>=? ORDER BY (end_line-start_line) LIMIT 1",
                (drow["id"], resp["line"], resp["line"])).fetchone()
            if srow is None:
                continue
            conn.execute(
                "UPDATE edges SET dst=?, confidence='certain', resolver='l1' "
                "WHERE id=?", (srow["id"], e["id"]))
            conn.execute(
                "DELETE FROM edges WHERE kind='calls' AND file_id=? AND line=? "
                "AND col=? AND id!=? AND resolver='l0' AND confidence='possible'",
                (file_id, e["line"], e["col"], e["id"]))
            promoted += 1
        return promoted
