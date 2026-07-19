"""Cliente LSP genérico para resolvers L1.

O protocolo LSP (`textDocument/definition`) é o mesmo para qualquer servidor —
gopls, rust-analyzer, clangd, jdtls… Esta base implementa o cliente stdio
(framing Content-Length, initialize/didOpen/definition) e a promoção de arestas
a `certain`. Cada linguagem vira uma subclasse trivial declarando:

    languages   : tupla de linguagens (as do L0)
    language_id : languageId LSP (ex.: 'go', 'rust', 'cpp')
    cmd_name    : nome do executável no PATH (ex.: 'gopls')
    cmd_env     : env var opcional que aponta o executável
    cmd_args    : args extras para lançar o servidor (raro)

`available()` só confere se o binário existe; a *qualidade* da resolução ainda
depende do servidor achar o projeto (go.mod / Cargo.toml / compile_commands).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


def _uri_to_path(uri: str) -> Path | None:
    try:
        return Path(url2pathname(unquote(urlparse(uri).path)))
    except Exception:
        return None


class LspResolver:
    languages: tuple[str, ...] = ()
    language_id: str = ""
    cmd_name: str = ""
    cmd_env: str | None = None
    cmd_args: tuple[str, ...] = ()
    # servidores que carregam o projeto de forma assíncrona (rust-analyzer,
    # clangd) só respondem `definition` depois de indexar — espera até isto.
    ready_timeout: float = 40.0

    # -- descoberta / disponibilidade ----------------------------------------

    @classmethod
    def _binary(cls) -> str | None:
        if cls.cmd_env:
            env = os.environ.get(cls.cmd_env)
            if env and Path(env).is_file():
                return env
        return shutil.which(cls.cmd_name)

    @classmethod
    def available(cls) -> bool:
        return cls._binary() is not None

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.proc = subprocess.Popen(
            [self._binary(), *self.cmd_args], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self._seq = 0
        self._opened: set[str] = set()
        self._lines: dict[str, list[str]] = {}
        self._ready = False
        self._ok = self._initialize()

    # -- framing --------------------------------------------------------------

    def _write(self, msg: dict) -> None:
        data = json.dumps(msg).encode("utf-8")
        self.proc.stdin.write(
            f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
        self.proc.stdin.flush()

    def _read(self) -> dict | None:
        headers: dict[str, str] = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            s = line.decode("ascii", "replace").strip()
            if s == "":
                break
            if ":" in s:
                k, v = s.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", 0))
        if n <= 0:
            return None
        buf = b""
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        try:
            return json.loads(buf.decode("utf-8"))
        except ValueError:
            return None

    def _request(self, method: str, params, timeout_msgs: int = 2000):
        if self.proc.poll() is not None:
            return None
        self._seq += 1
        rid = self._seq
        self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                     "params": params})
        for _ in range(timeout_msgs):
            msg = self._read()
            if msg is None:
                return None
            if msg.get("id") == rid and "method" not in msg:
                return msg.get("result")
            if "id" in msg and "method" in msg:  # req server→client → responde vazio
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        return None

    def _notify(self, method: str, params) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _initialize(self) -> bool:
        try:
            self._request("initialize", {
                "processId": os.getpid(),
                "rootUri": self.root.as_uri(),
                "capabilities": {"textDocument": {"definition": {}}},
            })
            self._notify("initialized", {})
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._request("shutdown", None, timeout_msgs=50)
            self._notify("exit", None)
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    # -- resolução ------------------------------------------------------------

    def _open(self, rel: str) -> None:
        if rel in self._opened:
            return
        self._opened.add(rel)
        try:
            text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self._lines[rel] = text.splitlines()
        self._notify("textDocument/didOpen", {"textDocument": {
            "uri": (self.root / rel).as_uri(), "languageId": self.language_id,
            "version": 1, "text": text}})

    def _query_col(self, rel: str, line1: int, col: int, dst_name: str) -> int:
        """Coluna a consultar: início do ÚLTIMO segmento do nome do callee.
        Ex.: aresta em `calc::compute` aponta col de `calc`, mas o servidor só
        resolve a função na posição de `compute`. Neutro p/ chamadas simples."""
        seg = (dst_name or "").replace("::", ".").replace("->", ".").split(".")[-1].strip()
        lines = self._lines.get(rel)
        if not seg or not lines or not (1 <= line1 <= len(lines)):
            return col
        src = lines[line1 - 1]
        idx = src.find(seg, max(0, col))
        if idx < 0:
            idx = src.find(seg)
        return idx if idx >= 0 else col

    def _warmup(self, rel: str, edges) -> None:
        """Espera o servidor ficar pronto (indexação assíncrona) consultando a
        primeira aresta até responder ou estourar ready_timeout."""
        if not edges:
            return
        e = edges[0]
        col = self._query_col(rel, e["line"], e["col"], e["dst_name"])
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if self._definition(rel, e["line"] - 1, col):
                break
            time.sleep(1.0)
        self._ready = True

    def _definition(self, rel: str, line0: int, char0: int):
        res = self._request("textDocument/definition", {
            "textDocument": {"uri": (self.root / rel).as_uri()},
            "position": {"line": line0, "character": char0}})
        locs = res if isinstance(res, list) else ([res] if res else [])
        out = []
        for loc in locs:
            uri = loc.get("uri") or loc.get("targetUri")
            rng = (loc.get("range") or loc.get("targetSelectionRange")
                   or loc.get("targetRange"))
            if uri and rng:
                out.append((uri, rng["start"]["line"]))
        return out

    def refine_file(self, conn: sqlite3.Connection, root: Path,
                    rel: str, file_id: int) -> int:
        if not self._ok:
            return 0
        edges = conn.execute(
            "SELECT id, line, col, dst_name FROM edges "
            "WHERE file_id=? AND kind='calls' AND resolver='l0' AND col IS NOT NULL",
            (file_id,)).fetchall()
        if not edges:
            return 0
        self._open(rel)
        if not self._ready:
            self._warmup(rel, edges)
        promoted = 0
        seen_sites: set[tuple[int, int]] = set()
        for e in edges:
            site = (e["line"], e["col"])
            if site in seen_sites:
                continue
            seen_sites.add(site)
            col = self._query_col(rel, e["line"], e["col"], e["dst_name"])
            locs = self._definition(rel, e["line"] - 1, col)
            if len(locs) != 1:
                continue
            dpath = _uri_to_path(locs[0][0])
            if dpath is None:
                continue
            try:
                drel = dpath.resolve().relative_to(self.root).as_posix()
            except ValueError:
                continue  # definição fora do repo (stdlib/módulo externo)
            drow = conn.execute("SELECT id FROM files WHERE path=?",
                                (drel,)).fetchone()
            if drow is None:
                continue
            dline = locs[0][1] + 1
            srow = conn.execute(
                "SELECT id FROM symbols WHERE file_id=? AND start_line<=? "
                "AND end_line>=? ORDER BY (end_line-start_line) LIMIT 1",
                (drow["id"], dline, dline)).fetchone()
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
