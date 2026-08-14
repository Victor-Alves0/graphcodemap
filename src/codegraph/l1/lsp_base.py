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
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ..log import get as _get_log
from . import promote

log = _get_log(__name__)

_EOF = object()  # sentinela: stream do servidor fechou/quebrou


def _uri_to_path(uri: str) -> Path | None:
    """Converte somente ``file:`` URI, decodificando escapes uma única vez.

    A conversão padrão do Windows não reconhece ``c%3A`` como drive antes de
    decodificar. Decodificamos explicitamente uma única vez e não aplicamos um
    segundo conversor (que transformaria um nome literal ``%20`` em espaço).
    O netloc também participa para preservar caminhos UNC.
    """
    try:
        parsed = urlparse(uri)
        if parsed.scheme.lower() != "file":
            return None
        netloc = "" if parsed.netloc.lower() == "localhost" else parsed.netloc
        raw = f"//{netloc}{parsed.path}" if netloc else parsed.path
        decoded = unquote(raw)
        # url2pathname reconhece /C:/ e UNC, mas também faz unquote. Escapar os
        # percentuais já decodificados impede a segunda decodificação.
        return Path(url2pathname(decoded.replace("%", "%25")))
    except Exception:
        return None


def _lsp_character(text: str, py_index: int) -> int:
    """Índice Python → unidade UTF-16 usada pelo LSP."""
    return len(text[:max(0, py_index)].encode("utf-16-le")) // 2


def _py_index_from_byte_col(text: str, byte_col: int) -> int:
    """Coluna UTF-8 do tree-sitter → índice Python, tolerando corte inválido."""
    raw = text.encode("utf-8")[:max(0, byte_col)]
    return len(raw.decode("utf-8", errors="ignore"))


def _byte_col_from_lsp(text: str, character: int) -> int:
    """Unidade UTF-16 do LSP → coluna UTF-8 persistida no índice."""
    used = 0
    py_index = 0
    for py_index, char in enumerate(text):
        width = len(char.encode("utf-16-le")) // 2
        if used + width > character:
            break
        used += width
    else:
        return len(text.encode("utf-8"))
    return len(text[:py_index].encode("utf-8"))


class LspResolver:
    languages: tuple[str, ...] = ()
    language_id: str = ""
    cmd_name: str = ""
    cmd_env: str | None = None
    cmd_args: tuple[str, ...] = ()
    # arquivos que marcam a raiz de um subprojeto (monorepo). O servidor é aberto
    # nessa raiz, não na do repo. Vazio = sempre a raiz do repo (ver roots.py).
    root_markers: tuple[str, ...] = ()
    # opções passadas em `initialize` (jdtls/metals usam para configurar o
    # projeto); None = omitir. Neutro para os servidores simples.
    init_options: dict | None = None
    # servidores que carregam o projeto de forma assíncrona (rust-analyzer,
    # clangd) só respondem `definition` depois de indexar — espera até isto.
    ready_timeout: float = 40.0
    # limite de I/O por leitura: servidor que trava (aceita didOpen mas nunca
    # responde) não pode congelar a indexação — estourou, mata e desiste.
    io_timeout: float = 20.0

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

    def _popen_argv(self) -> list[str]:
        """Argv para lançar o servidor. Servidores simples = binário no PATH;
        subclasses com launcher (jdtls: java -jar equinox…) sobrescrevem."""
        return [self._binary(), *self.cmd_args]

    def __init__(self, root: Path, project_root: Path | None = None) -> None:
        # `root` é a raiz do REPO — usada para abrir arquivos repo-relativos e
        # relativizar as definições de volta (o índice usa caminhos repo-relativos).
        # `project_root` é a raiz do SUBPROJETO anunciada ao servidor (rootUri);
        # os URIs de arquivo são absolutos, então isto não afeta o casamento.
        self.root = Path(root).resolve()
        self.project_root = (Path(project_root).resolve() if project_root
                             else self.root)
        self._defcache: dict[tuple[str, int, int], list] = {}
        self.proc = subprocess.Popen(
            self._popen_argv(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self._seq = 0
        self._opened: set[str] = set()
        self._lines: dict[str, list[str]] = {}
        self._ready = False
        self._dead = False
        # thread leitora dedicada + fila: dá timeout a cada leitura sem depender
        # de select() (indisponível em pipe no Windows). Um servidor travado é
        # detectado pelo timeout da fila, não bloqueia a thread principal.
        self._q: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self._ok = self._initialize()

    # -- framing --------------------------------------------------------------

    def _write(self, msg: dict) -> None:
        if self._dead or self.proc.poll() is not None:
            return
        data = json.dumps(msg).encode("utf-8")
        try:
            self.proc.stdin.write(
                f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
            self.proc.stdin.flush()
        except OSError as e:
            log.debug("%s: stdin quebrado: %s", self.cmd_name, e)
            self._kill()

    def _read_frame(self):
        """Lê UMA mensagem do stdout (bloqueante). ``_EOF`` = stream fechou."""
        headers: dict[str, str] = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return _EOF
            s = line.decode("ascii", "replace").strip()
            if s == "":
                break
            if ":" in s:
                k, v = s.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        try:
            n = int(headers.get("content-length", 0))
        except ValueError:
            return _EOF
        if n <= 0:
            return _EOF
        buf = b""
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                return _EOF
            buf += chunk
        try:
            return json.loads(buf.decode("utf-8"))
        except ValueError:
            return _EOF  # framing quebrou: stream não é mais confiável

    def _reader_loop(self) -> None:
        try:
            while True:
                msg = self._read_frame()
                self._q.put(msg)
                if msg is _EOF:
                    return
        except Exception:
            self._q.put(_EOF)

    def _read(self, timeout: float | None = None) -> dict | None:
        """Próxima mensagem, com timeout de I/O. None = EOF ou servidor travou."""
        wait = self.io_timeout if timeout is None else max(0.0, timeout)
        try:
            msg = self._q.get(timeout=wait)
        except queue.Empty:
            log.warning("%s: sem resposta em %.0fs — matando servidor LSP",
                        self.cmd_name, wait)
            self._kill()
            return None
        return None if msg is _EOF else msg

    def _kill(self) -> None:
        self._dead = True
        self._ok = False
        try:
            self.proc.kill()
        except Exception:
            pass

    def _request(self, method: str, params, timeout_msgs: int = 2000):
        if self._dead or self.proc.poll() is not None:
            return None
        self._seq += 1
        rid = self._seq
        self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                     "params": params})
        # Limite TOTAL: notificações de progresso não podem manter viva para
        # sempre uma requisição cuja resposta nunca chega.
        deadline = time.monotonic() + self.io_timeout
        for _ in range(timeout_msgs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                return None
            msg = self._read(remaining)
            if msg is None:
                return None
            if msg.get("id") == rid and "method" not in msg:
                return msg.get("result")
            if "id" in msg and "method" in msg:
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "result": self._server_request_result(msg)})
        return None

    def _server_request_result(self, msg: dict):
        """Resposta mínima correta às requisições usuais servidor→cliente."""
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "workspace/workspaceFolders":
            return [{"uri": self.project_root.as_uri(),
                     "name": self.project_root.name}]
        if method == "workspace/configuration":
            settings = (self.init_options or {}).get("settings", {})
            out = []
            for item in params.get("items", []):
                value = settings
                section = item.get("section") if isinstance(item, dict) else None
                for part in (section or "").split("."):
                    if part:
                        value = value.get(part) if isinstance(value, dict) else None
                out.append(value)
            return out
        # registerCapability, workDoneProgress/create e showMessageRequest
        # aceitam resposta nula no cliente sem UI.
        return None

    def _notify(self, method: str, params) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _initialize(self) -> bool:
        try:
            params = {
                "processId": os.getpid(),
                "rootUri": self.project_root.as_uri(),
                "workspaceFolders": [{"uri": self.project_root.as_uri(),
                                      "name": self.project_root.name}],
                "capabilities": {
                    "textDocument": {"definition": {}},
                    "workspace": {"configuration": True,
                                  "workspaceFolders": True},
                },
            }
            if self.init_options is not None:
                params["initializationOptions"] = self.init_options
            result = self._request("initialize", params)
            if not isinstance(result, dict) or self._dead:
                return False
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
        try:
            text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self._opened.add(rel)
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
        if not lines or not (1 <= line1 <= len(lines)):
            return col
        src = lines[line1 - 1]
        start = _py_index_from_byte_col(src, col)
        if not seg:
            return _lsp_character(src, start)
        idx = src.find(seg, start)
        if idx < 0:
            idx = src.find(seg)
        if idx >= 0:
            return _lsp_character(src, idx)
        return _lsp_character(src, start)

    def _warmup(self, rel: str, edges) -> None:
        """Espera o servidor ficar pronto (indexação assíncrona) consultando a
        aresta mais representativa até responder ou estourar ready_timeout.

        Uma chamada local pode ficar resolvível antes de o servidor terminar de
        indexar o workspace. Preferir um callee qualificado (``pkg::fn``,
        ``pkg.fn`` ou ``obj->fn``) evita declarar o servidor pronto cedo demais
        e perder justamente as referências cross-file que o L1 deve promover.
        """
        if not edges:
            return
        e = next((edge for edge in edges
                  if any(sep in (edge["dst_name"] or "")
                         for sep in ("::", ".", "->"))), edges[0])
        col = self._query_col(rel, e["line"], e["col"], e["dst_name"])
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if self._definition(rel, e["line"] - 1, col):
                break
            # resposta VAZIA durante o warmup é "ainda indexando", não "não
            # existe" — e não pode ficar no memo, senão a própria espera relê o
            # "ainda não" cacheado e nunca vê o servidor ficar pronto. Servidores
            # que seguram a requisição até indexar (gopls) nunca caem aqui; o
            # intelephense responde [] na hora, e era isso que zerava o L1 de PHP.
            self._defcache.pop((rel, e["line"] - 1, col), None)
            time.sleep(1.0)
        self._ready = True

    def _definition(self, rel: str, line0: int, char0: int):
        # cache por instância: o warmup consulta a 1ª aresta para esperar o
        # servidor ficar pronto, e o laço principal a consultaria de novo — o
        # memo elimina esse round-trip repetido (e quaisquer outros no mesmo run,
        # que roda sobre um snapshot consistente do repo).
        key = (rel, line0, char0)
        if key in self._defcache:
            return self._defcache[key]
        res = self._request("textDocument/definition", {
            "textDocument": {"uri": (self.root / rel).as_uri()},
            "position": {"line": line0, "character": char0}})
        locs = res if isinstance(res, list) else ([res] if res else [])
        out = []
        for loc in locs:
            uri = loc.get("uri") or loc.get("targetUri")
            rng = (loc.get("targetSelectionRange") or loc.get("range")
                   or loc.get("targetRange"))
            if uri and rng:
                start = rng["start"]
                out.append((uri, start["line"], start.get("character", 0)))
        self._defcache[key] = out
        return out

    def _definition_byte_col(self, drel: str, line0: int,
                             char0: int) -> int | None:
        cache = getattr(self, "_target_lines", None)
        if cache is None:
            cache = self._target_lines = {}
        if drel not in cache:
            try:
                cache[drel] = (self.root / drel).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                cache[drel] = []
        lines = cache[drel]
        if not (0 <= line0 < len(lines)):
            # Fallback seguro: target_symbol volta ao casamento apenas por
            # linha, equivalente ao contrato anterior, se o arquivo sumiu.
            return None
        return _byte_col_from_lsp(lines[line0], char0)

    def refine_file(self, conn: sqlite3.Connection, root: Path,
                    rel: str, file_id: int) -> int:
        if not self._ok:
            return 0
        edges = conn.execute(
            "SELECT id, line, col, dst_name FROM edges "
            "WHERE file_id=? AND kind='calls' AND resolver='l0' AND col IS NOT NULL "
            "ORDER BY line, col, id",
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
            # multi-def (overloads / interface+impls / decl+def) NÃO é descartado:
            # cada definição no repo vira um alvo; promote.apply decide certain
            # (1 alvo) vs fan-out inferred (2..MAX).
            targets = []
            for uri, line0, char0 in locs:
                dpath = _uri_to_path(uri)
                if dpath is None:
                    continue
                try:
                    drel = dpath.resolve().relative_to(self.root).as_posix()
                except ValueError:
                    continue  # definição fora do repo (stdlib/módulo externo)
                dcol = self._definition_byte_col(drel, line0, char0)
                sid = promote.target_symbol(conn, drel, line0 + 1, dcol)
                if sid is not None:
                    targets.append(sid)
            promoted += promote.apply(conn, file_id, e, targets)
        return promoted
