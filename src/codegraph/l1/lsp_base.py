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
import re
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
    # Encerrar um servidor já degradado não pode repetir o timeout de análise.
    shutdown_timeout: float = 2.0
    # Resolvers cujo import de projeto e parte do contrato semantico podem
    # optar por falhar fechado quando o warmup/I/O excede o orçamento.
    timeout_is_partial: bool = False

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
        binary = self._binary()
        # Launchers de ecossistema no Windows (gem/coursier/Gradle) costumam
        # ser .cmd/.bat. CreateProcess não os executa diretamente; use o cmd
        # apenas para esse launcher já descoberto, mantendo shell=False e um
        # argv construído, sem interpolar conteúdo do repositório analisado.
        if (os.name == "nt" and binary
                and Path(binary).suffix.lower() in {".cmd", ".bat"}):
            command = subprocess.list2cmdline([binary, *self.cmd_args])
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                    command]
        return [binary, *self.cmd_args]

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
        # Observabilidade do protocolo. Language servers costumam comunicar
        # falhas de import/build por notificacoes (e nao levantando uma excecao
        # no processo cliente). Sem guardar esses sinais, um workspace sem
        # modelo semantico podia parecer uma execucao limpa com 0 promocoes.
        self._health_errors: list[str] = []
        self._health_warnings: list[str] = []
        self._diagnostics_by_uri: dict[str, list[str]] = {}
        self._semantic_sites = 0
        self._semantic_hits = 0
        self._semantic_request_errors = 0
        self._warmup_timed_out = False
        self._io_timed_out = False
        self._active_method: str | None = None
        # O timeout curto de ``shutdown`` usa o mesmo caminho de kill das
        # falhas operacionais. Preserve se o servidor chegou saudável ao
        # encerramento para que o teardown não reescreva retrospectivamente a
        # saúde da análise; falhas ocorridas antes de ``close`` continuam
        # visíveis por ``_ok``/``_dead`` e pelos diagnósticos observados.
        self._shutdown_started_healthy = False
        self._shutdown_completed = False
        self._active_deadline: float | None = None
        self._last_message_at = time.monotonic()
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
        if self._dead or self.proc.poll() is not None:
            self._kill()
            return None
        wait = self.io_timeout if timeout is None else max(0.0, timeout)
        try:
            msg = self._q.get(timeout=wait)
        except queue.Empty:
            log.warning("%s: sem resposta em %.0fs — matando servidor LSP",
                        self.cmd_name, wait)
            if getattr(self, "_active_method", None) != "shutdown":
                self._io_timed_out = True
            self._kill()
            return None
        if msg is _EOF:
            self._kill()
            return None
        return msg

    def _kill(self) -> None:
        self._dead = True
        self._ok = False
        try:
            self.proc.kill()
        except Exception:
            pass

    def _request(self, method: str, params, timeout_msgs: int = 2000,
                 *, timeout: float | None = None):
        if self._dead or self.proc.poll() is not None:
            return None
        self._seq += 1
        rid = self._seq
        self._active_method = method
        self._active_params = params
        self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                     "params": params})
        # Limite TOTAL: notificações de progresso não podem manter viva para
        # sempre uma requisição cuja resposta nunca chega.
        now = time.monotonic()
        budget = self.io_timeout if timeout is None else min(self.io_timeout,
                                                              max(0.0, timeout))
        deadline = now + budget
        active = getattr(self, "_active_deadline", None)
        if active is not None:
            deadline = min(deadline, active)
        for _ in range(timeout_msgs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if method != "shutdown":
                    self._io_timed_out = True
                self._kill()
                return self._finish_request(None)
            msg = self._read(remaining)
            if msg is None:
                return self._finish_request(None)
            self._observe_message(msg)
            if msg.get("id") == rid and "method" not in msg:
                if msg.get("error"):
                    return self._finish_request(None)
                return self._finish_request(msg.get("result"))
            if "id" in msg and "method" in msg:
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "result": self._server_request_result(msg)})
        if method != "shutdown":
            self._io_timed_out = True
        self._kill()
        return self._finish_request(None)

    def _finish_request(self, result):
        """Limpa o contexto usado para classificar mensagens site-locais.

        ``shutdown`` permanece ativo de propósito: notificações inequívocas de
        teardown ainda precisam do contexto para não virar falso erro. Para as
        demais requisições, uma notificação tardia não pode herdar o método e o
        arquivo da consulta anterior.
        """
        if getattr(self, "_active_method", None) != "shutdown":
            self._active_method = None
            self._active_params = None
        return result

    @staticmethod
    def _health_text(value) -> str:
        """Texto curto e estavel para diagnosticos retornados no relatorio."""
        if isinstance(value, dict):
            value = value.get("message") or value.get("detail") or value
        text = " ".join(str(value or "falha LSP sem detalhe").split())
        return text[:500]

    def _record_health(self, severity: str, value) -> None:
        text = self._health_text(value)
        # gopls pode terminar um job assíncrono depois de aceitarmos shutdown e
        # reportar "while diagnosing orphaned files: session is shut down" como
        # Error. Isso descreve o teardown solicitado pelo cliente, não a análise.
        # O filtro exige shutdown iniciado saudável + mensagem inequívoca; NPEs,
        # erros de build e qualquer falha observada antes continuam fail-closed.
        if (severity == "error"
                and getattr(self, "_shutdown_started_healthy", False)
                and getattr(self, "_active_method", None) == "shutdown"
                and "session is shut down" in text.lower()):
            severity = "warning"
        target = (self._health_errors if severity == "error"
                  else self._health_warnings)
        if text not in target and len(target) < 20:
            target.append(text)

    def _logged_java_file_exists(self, text: str) -> bool:
        """Confirma um basename Java único antes de tolerar log stale do JDTLS."""
        match = re.search(r"([^/\\\s\[\]]+\.java).*?does not exist", text,
                          re.IGNORECASE)
        if match is None:
            return False
        name = match.group(1)
        cache = getattr(self, "_java_basename_counts", None)
        if cache is None:
            cache = self._java_basename_counts = {}
        if name not in cache:
            count = 0
            for path in self.root.rglob(name):
                if path.is_file() and not any(
                        part in {".git", "build", "out", "target"}
                        for part in path.parts):
                    count += 1
                    if count > 1:
                        break
            cache[name] = count
        return cache[name] == 1

    def _observe_message(self, msg: dict) -> None:
        """Preserva sinais de saude que antes eram descartados pelo cliente.

        O protocolo base cobre respostas JSON-RPC de erro e mensagens LSP
        padrao. JDTLS tambem usa ``language/status`` e
        ``language/actionableNotification`` para falhas de import/build.
        Diagnostico de compilacao ``Error`` torna o resultado parcial: o
        servidor pode continuar resolvendo parte do projeto, mas nao ha modelo
        completo para declarar a passada saudavel.
        """
        if hasattr(self, "_last_message_at"):
            self._last_message_at = time.monotonic()
        if msg.get("error"):
            error = msg["error"]
            method = str(getattr(self, "_active_method", "LSP") or "LSP")
            detail = self._health_text(error)
            params = getattr(self, "_active_params", None) or {}
            if method == "textDocument/definition":
                uri = ((params.get("textDocument") or {}).get("uri") or "?")
                pos = params.get("position") or {}
                detail = (f"{method} {uri}:{int(pos.get('line', 0)) + 1}:"
                          f"{int(pos.get('character', 0))}: {detail}")
            data = error.get("data") if isinstance(error, dict) else None
            if data:
                data_text = self._health_text(data)
                if data_text and data_text not in detail:
                    detail = f"{detail}; {data_text}"
            # Um erro interno de UMA consulta de definição não prova que o
            # modelo inteiro está inválido. O site recebe zero targets e fica
            # no fallback L0; milhares de outras provas continuam publicáveis.
            # Erros de initialize/import/build e demais códigos permanecem
            # fail-closed. JDTLS pode lançar -32603 em construções válidas com
            # classes anônimas aninhadas (ArrayStoreException interna).
            site_local = (self.cmd_name == "jdtls"
                          and method == "textDocument/definition"
                          and isinstance(error, dict)
                          and error.get("code") == -32603
                          and str(error.get("message", "")).strip().lower()
                          == "internal error.")
            if site_local:
                self._semantic_request_errors = (
                    int(getattr(self, "_semantic_request_errors", 0)) + 1)
                self._record_health("warning", detail)
            else:
                self._record_health("error", detail)
            return

        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "textDocument/publishDiagnostics":
            # publishDiagnostics contém o snapshot completo atual daquele URI.
            # Uma publicação vazia limpa erros antigos; outro arquivo não deve
            # apagar diagnósticos ainda ativos deste URI.
            uri = str(params.get("uri") or "<unknown>")
            errors = []
            for diagnostic in params.get("diagnostics", []):
                severity = diagnostic.get("severity")
                if severity == 1:
                    text = self._health_text(diagnostic)
                    if text not in errors and len(errors) < 20:
                        errors.append(text)
            diagnostics = getattr(self, "_diagnostics_by_uri", None)
            if diagnostics is None:
                diagnostics = self._diagnostics_by_uri = {}
            diagnostics[uri] = errors
            return

        if method in {"window/showMessage", "window/logMessage"}:
            severity = params.get("type")
            if severity == 1:
                text = self._health_text(params)
                lowered = text.lower()
                normalized = lowered.replace("\\", "/")
                # m2e-apt tenta adicionar a saída opcional de annotations sob
                # um source root pai. Essa colisão não remove fontes reais do
                # classpath e GraphCodeMap deliberadamente não indexa target/.
                # Erros de tipos/imports gerados continuam vindo por diagnostics
                # e permanecem fail-closed.
                apt_nested = (self.cmd_name == "jdtls"
                              and "failed to add classpath entry for generated source folder annotations"
                              in lowered
                              and "cannot nest" in lowered
                              and "target/generated-sources/annotations"
                              in normalized)
                # Notificação assíncrona e site-local observada no JDTLS ao
                # consultar arquivos que existem. Fora da consulta, só aceite
                # como stale quando o basename é único e existe no repo.
                stale_site = (self.cmd_name == "jdtls"
                              and method == "window/logMessage"
                              and ".java" in lowered
                              and "does not exist" in lowered
                              and self._logged_java_file_exists(text))
                if apt_nested:
                    self._workspace_tainted = True
                    nested = re.search(r"cannot nest '([^']+annotations)'",
                                       normalized)
                    folder = nested.group(1) if nested else (
                        "target/generated-sources/annotations")
                    self._record_health(
                        "warning",
                        "JDTLS/m2e-apt não adicionou a saída opcional aninhada "
                        f"{folder}; fontes e diagnostics reais permanecem ativos")
                else:
                    self._record_health(
                        "warning" if stale_site else "error", params)
            elif severity == 2:
                self._record_health("warning", params)
            return

        if method in {"language/status", "language/actionableNotification"}:
            raw = params.get("type", params.get("severity", ""))
            severity = str(raw).lower()
            if raw == 1 or "error" in severity:
                self._record_health("error", params)
            elif raw == 2 or "warn" in severity:
                self._record_health("warning", params)

    def _drain_pending(self, timeout: float = 0.0) -> None:
        """Observa mensagens já recebidas pelo reader sem exceder ``timeout``.

        A resposta de shutdown pode anteceder um ``publishDiagnostics`` que o
        reader já colocou (ou está prestes a colocar) na fila. Consumir somente
        até a resposta esconderia esse erro do health report. O EOF é terminal,
        mas depois de um shutdown iniciado saudável não reclassifica o teardown
        normal como falha da análise.
        """
        pending = getattr(self, "_q", None)
        if pending is None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        reader = getattr(self, "_reader", None)
        while True:
            try:
                msg = pending.get_nowait()
            except queue.Empty:
                remaining = deadline - time.monotonic()
                is_alive = (reader is not None
                            and getattr(reader, "is_alive", lambda: False)())
                if remaining <= 0 or not is_alive:
                    break
                try:
                    msg = pending.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
            if msg is _EOF:
                self._dead = True
                self._ok = False
                break
            self._observe_message(msg)
        if reader is not None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                join = getattr(reader, "join", None)
                if join is not None:
                    join(timeout=remaining)
        # Uma mensagem pode ter sido colocada entre o último get e o join.
        while True:
            try:
                msg = pending.get_nowait()
            except queue.Empty:
                break
            if msg is _EOF:
                self._dead = True
                self._ok = False
                break
            self._observe_message(msg)

    def health_report(self) -> dict:
        """Saude observada da instancia, independente do numero de promocoes.

        Zero promocoes e valido (por exemplo, chamadas apenas externas). Ele so
        vira ``partial`` quando ha evidencia positiva de falha: inicializacao,
        transporte/protocolo, diagnostico Error ou status Error do servidor.
        Timeout de warmup sem definicao e mantido como aviso ``unverified``.
        """
        # Normalmente ``refine`` chama health depois de ``close``. O drain
        # adicional torna a API direta segura para mensagens que já chegaram,
        # sem esperar nem roubar tempo de uma requisição ativa.
        self._drain_pending()
        errors = list(getattr(self, "_health_errors", ()))
        for current in getattr(self, "_diagnostics_by_uri", {}).values():
            errors.extend(current)
        errors = list(dict.fromkeys(errors))
        warnings = list(getattr(self, "_health_warnings", ()))
        ok = ((bool(getattr(self, "_ok", False))
               and not getattr(self, "_dead", False))
              or bool(getattr(self, "_shutdown_started_healthy", False)))
        if not ok and "servidor LSP indisponivel ou sem handshake" not in errors:
            errors.append("servidor LSP indisponivel ou sem handshake")
        timed_out = bool(getattr(self, "_warmup_timed_out", False))
        sites = int(getattr(self, "_semantic_sites", 0))
        hits = int(getattr(self, "_semantic_hits", 0))
        io_timed_out = bool(getattr(self, "_io_timed_out", False))
        request_errors = int(getattr(self, "_semantic_request_errors", 0))
        if timed_out:
            # O warmup consulta UMA aresta representativa. Ela pode ser externa,
            # dinâmica ou simplesmente irresolúvel; exigir que esse probe tenha
            # definição produz falso partial mesmo quando centenas de requests
            # posteriores provaram que o servidor está semanticamente pronto.
            # Só falhe fechado quando não existe nenhuma prova positiva no run.
            if hits:
                warnings.append(
                    f"probe de readiness nao resolveu em {self.ready_timeout:g}s, "
                    f"mas {hits}/{sites} site(s) obtiveram definicao na passada")
            else:
                message = (
                    f"readiness semantica nao comprovada em "
                    f"{self.ready_timeout:g}s: 0/{sites} site(s) obtiveram "
                    "definicao na passada")
                if self.timeout_is_partial:
                    message += ("; aumente --jdtls-ready-timeout/"
                                "CODEGRAPH_JDTLS_READY_TIMEOUT e o orçamento de I/O")
                    errors.append(message)
                else:
                    warnings.append(message)
        if io_timed_out and self.timeout_is_partial:
            errors.append(
                f"JDTLS excedeu o timeout de I/O de {self.io_timeout:g}s; "
                "aumente --jdtls-io-timeout/CODEGRAPH_JDTLS_IO_TIMEOUT")
        errors = list(dict.fromkeys(errors))
        return {
            "status": "partial" if errors else "complete",
            "errors": errors,
            "warnings": list(dict.fromkeys(warnings)),
            "sites": sites,
            "resolved_sites": hits,
            "semantic_request_errors": request_errors,
            "warmup_timed_out": timed_out,
            "io_timed_out": io_timed_out,
            "ready_timeout_s": self.ready_timeout,
            "io_timeout_s": self.io_timeout,
        }

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
            settings = (self.init_options or {}).get("settings")
            if settings:
                self._notify("workspace/didChangeConfiguration",
                             {"settings": settings})
            return True
        except Exception:
            return False

    def close(self) -> None:
        proc = getattr(self, "proc", None)
        if proc is None:
            self._drain_pending()
            return
        running = not self._dead and proc.poll() is None
        self._shutdown_started_healthy = bool(
            running and getattr(self, "_ok", False))
        deadline = time.monotonic() + max(0.0, self.shutdown_timeout)
        if not running:
            if proc.poll() is not None:
                self._dead = True
                self._ok = False
            self._drain_pending()
            return
        try:
            # didOpen cria working copies e pode agendar diagnósticos. Fechá-las
            # antes do workspace evita que jobs atrasados tentem publicar depois
            # de shutdown/exit (observado no JDTLS como Publish Diagnostics).
            opened = getattr(self, "_opened", None)
            for rel in sorted(opened or ()):
                self._notify("textDocument/didClose", {"textDocument": {
                    "uri": (self.root / rel).as_uri()}})
            if opened is not None:
                opened.clear()
            self._before_shutdown()
            remaining = max(0.0, deadline - time.monotonic())
            self._request("shutdown", None, timeout_msgs=50,
                          timeout=remaining)
            if not self._dead and self.proc.poll() is None:
                self._notify("exit", None)
                remaining = max(0.0, deadline - time.monotonic())
                self.proc.wait(timeout=remaining)
            self._shutdown_completed = self.proc.poll() is not None
        except Exception:
            self._kill()
        finally:
            self._drain_pending(max(0.0, deadline - time.monotonic()))

    def _before_shutdown(self) -> None:
        """Hook para servidores que precisam assentar jobs após didClose."""

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
        def score(edge) -> tuple[bool, bool]:
            keys = edge.keys() if hasattr(edge, "keys") else edge
            internal = "dst" in keys and edge["dst"] is not None
            qualified = any(sep in (edge["dst_name"] or "")
                            for sep in ("::", ".", "->"))
            return internal, qualified

        # Readiness não pode depender de uma única chamada externa ou de um
        # guess L0 incorreto. Tente primeiro até oito sites já ligados dentro do
        # repo, mantendo qualificação como desempate, e depois os demais.
        probes = sorted(edges, key=score, reverse=True)[:8]
        deadline = time.monotonic() + self.ready_timeout
        ready = False
        previous_deadline = getattr(self, "_active_deadline", None)
        self._active_deadline = (deadline if previous_deadline is None
                                 else min(deadline, previous_deadline))
        try:
            while True:
                remaining = deadline - time.monotonic()
                proc = getattr(self, "proc", None)
                if (remaining <= 0 or getattr(self, "_dead", False)
                        or (proc is not None and proc.poll() is not None)):
                    break
                for probe in probes:
                    col = self._query_col(
                        rel, probe["line"], probe["col"], probe["dst_name"])
                    if self._definition(rel, probe["line"] - 1, col):
                        ready = True
                        break
                    # resposta VAZIA durante o warmup é "ainda indexando", não
                    # "não existe" — não memorize o resultado provisório.
                    self._defcache.pop((rel, probe["line"] - 1, col), None)
                if ready:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or getattr(self, "_dead", False):
                    break
                time.sleep(min(1.0, remaining))
        finally:
            self._active_deadline = previous_deadline
        self._warmup_timed_out = not ready
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

    def _project_readiness_probe(self, conn: sqlite3.Connection):
        """Uma chamada cross-file interna na mesma raiz é o melhor probe LSP.

        O primeiro arquivo em ordem lexical pode conter somente framework calls;
        esperar uma delas por todo o timeout confundia "externa" com "servidor
        importando". O destino L0 serve apenas para escolher o site — a resposta
        semântica continua vindo exclusivamente do language server.
        """
        try:
            prefix = self.project_root.relative_to(self.root).as_posix().strip("/")
        except ValueError:
            prefix = ""
        if prefix == ".":
            prefix = ""
        placeholders = ",".join("?" * len(self.languages))
        rows = conn.execute(
            "SELECT e.id, e.line, e.col, e.dst_name, e.dst, f.path AS rel "
            "FROM edges e JOIN files f ON f.id=e.file_id "
            "JOIN symbols target ON target.id=e.dst "
            "WHERE e.kind='calls' AND e.resolver='l0' AND e.col IS NOT NULL "
            f"AND f.language IN ({placeholders}) AND target.file_id!=f.id "
            "ORDER BY (instr(e.dst_name, '.') > 0) DESC, f.path, e.line, e.col",
            list(self.languages),
        )
        for row in rows:
            rel = row["rel"]
            if not prefix or rel == prefix or rel.startswith(prefix + "/"):
                return row
        return None

    def refine_file(self, conn: sqlite3.Connection, root: Path,
                    rel: str, file_id: int) -> int:
        if not self._ok:
            return 0
        edges = conn.execute(
            "SELECT id, line, col, dst_name, dst FROM edges "
            "WHERE file_id=? AND kind='calls' AND resolver='l0' AND col IS NOT NULL "
            "ORDER BY line, col, id",
            (file_id,)).fetchall()
        if not edges:
            return 0
        self._open(rel)
        if not self._ready:
            probe = self._project_readiness_probe(conn)
            if probe is not None:
                probe_rel = probe["rel"]
                self._open(probe_rel)
                self._warmup(probe_rel, [probe])
            else:
                self._warmup(rel, edges)
        if self._dead or self.proc.poll() is not None:
            return 0
        promoted = 0
        seen_sites: set[tuple[int, int]] = set()
        for e in edges:
            site = (e["line"], e["col"])
            if site in seen_sites:
                continue
            seen_sites.add(site)
            col = self._query_col(rel, e["line"], e["col"], e["dst_name"])
            locs = self._definition(rel, e["line"] - 1, col)
            self._semantic_sites += 1
            if locs:
                self._semantic_hits += 1
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
