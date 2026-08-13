"""Resolver L1 para PHP via intelephense. Config sobre LspResolver.

O intelephense é distribuído como pacote npm, não como binário nativo, e é aí
que mora a dificuldade: `npm i` publica no PATH um *shim* (`.cmd`/`.ps1` no
Windows, symlink com shebang no POSIX), e o shim `.cmd` não é lançável por
CreateProcess sem shell — era isso que mantinha o resolver inerte nesta máquina.
Por isso a descoberta prefere o ENTRYPOINT REAL (`lib/intelephense.js`) e o
lança com `node`, caindo no executável do PATH só quando o .js não aparece.

Instalação (qualquer uma serve, ver `_module_dirs`):
    npm install intelephense            # em <repo>/tools/php (convenção local,
                                        # mesma do tools/ts do resolver JS/TS)
    npm install -g intelephense         # global

Licença premium só é exigida por features avançadas; goto-definition — a única
que usamos — funciona no modo livre. A indexação do workspace é assíncrona: o
servidor responde `[]` enquanto indexa, e o `_warmup` da base é justamente
quem espera esse período (validado ao vivo no DVWA).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .lsp_base import LspResolver

_DEV_ROOT = Path(__file__).resolve().parents[3]  # layout src/: raiz do repo
_ENTRY_REL = Path("intelephense") / "lib" / "intelephense.js"


def _find_node() -> str | None:
    env = os.environ.get("CODEGRAPH_NODE")
    if env and Path(env).is_file():
        return env
    return shutil.which("node")


def _module_dirs() -> list[Path]:
    """`node_modules` onde um `npm install [-g] intelephense` costuma cair.

    A ordem é intencional: o toolchain do próprio repo (tools/php) primeiro,
    depois os prefixos globais do npm, para uma instalação local vencer uma
    global desatualizada."""
    dirs = [_DEV_ROOT / "tools" / "php" / "node_modules"]
    prefix = (os.environ.get("NPM_CONFIG_PREFIX")
              or os.environ.get("npm_config_prefix"))
    if prefix:
        dirs += [Path(prefix) / "node_modules",
                 Path(prefix) / "lib" / "node_modules"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "npm" / "node_modules")  # npm -g no Windows
    home = Path.home()
    dirs += [home / ".npm-global" / "lib" / "node_modules",
             home / "node_modules",
             Path("/usr/local/lib/node_modules"),
             Path("/usr/lib/node_modules")]
    return dirs


def _js_beside_shim(exe: str) -> Path | None:
    """Do shim `<...>/node_modules/.bin/intelephense` para o pacote irmão."""
    cand = Path(exe).resolve().parent.parent / _ENTRY_REL
    return cand if cand.is_file() else None


def _find_entry() -> tuple[str, bool] | None:
    """`(caminho, precisa_de_node)` do servidor, ou None se não instalado.

    `CODEGRAPH_INTELEPHENSE` aceita tanto o .js quanto um executável já pronto,
    para não amarrar o usuário à forma como ele instalou."""
    env = os.environ.get("CODEGRAPH_INTELEPHENSE")
    if env and Path(env).is_file():
        return env, env.endswith(".js")
    for d in _module_dirs():
        cand = d / _ENTRY_REL
        if cand.is_file():
            return str(cand), True
    exe = shutil.which("intelephense")
    if exe:
        js = _js_beside_shim(exe)
        return (str(js), True) if js else (exe, False)
    return None


class IntelephenseResolver(LspResolver):
    languages = ("php",)
    language_id = "php"
    cmd_name = "intelephense"
    cmd_env = "CODEGRAPH_INTELEPHENSE"
    root_markers = ("composer.json",)
    cmd_args = ("--stdio",)

    @classmethod
    def _binary(cls) -> str | None:
        entry = _find_entry()
        return None if entry is None else entry[0]

    @classmethod
    def available(cls) -> bool:
        entry = _find_entry()
        if entry is None:
            return False
        # entrypoint .js sem node é o mesmo que servidor ausente: o resolver
        # tem de continuar inerte em vez de estourar no Popen.
        return not entry[1] or _find_node() is not None

    def _popen_argv(self) -> list[str]:
        entry = _find_entry()
        if entry is None:                   # só chegaria aqui se o servidor
            return list(self.cmd_args)      # sumisse depois de `available()`
        caminho, precisa_node = entry
        head = [_find_node(), caminho] if precisa_node else [caminho]
        return [*head, *self.cmd_args]
