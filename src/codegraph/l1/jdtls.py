"""L1 para Java via Eclipse JDT Language Server (jdtls).

Diferente dos resolvers "binário no PATH", o jdtls é uma aplicação Eclipse
lançada por `java -jar <equinox-launcher> -configuration <cfg> -data <ws>` —
o primeiro servidor com *launcher* neste projeto, provando que o cliente
genérico (lsp_base) não presume um único executável.

Ativação: aponte `CODEGRAPH_JDTLS` para a pasta de instalação do JDT LS (a que
contém `plugins/` e `config_*`) e tenha um JDK 17+ (java no PATH ou JAVA_HOME).
Download: https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from .lsp_base import LspResolver


class JdtlsResolver(LspResolver):
    languages = ("java",)
    language_id = "java"
    cmd_name = "java"
    # jdtls importa o projeto de forma assíncrona (autobuild); pode demorar.
    ready_timeout = 120.0
    io_timeout = 30.0
    # habilita o autobuild do "invisible project" p/ arquivos sem build tool.
    init_options = {"settings": {"java": {"autobuild": {"enabled": True}}}}

    # -- localização da instalação -------------------------------------------

    @classmethod
    def _home(cls) -> Path | None:
        d = os.environ.get("CODEGRAPH_JDTLS")
        p = Path(d) if d else None
        return p if p and p.is_dir() else None

    @staticmethod
    def _launcher_jar(home: Path) -> Path | None:
        plugins = home / "plugins"
        if not plugins.is_dir():
            return None
        jars = sorted(plugins.glob("org.eclipse.equinox.launcher_*.jar"))
        return jars[0] if jars else None

    @staticmethod
    def _config_dir(home: Path) -> Path | None:
        name = {"win32": "config_win",
                "darwin": "config_mac"}.get(sys.platform, "config_linux")
        d = home / name
        return d if d.is_dir() else None

    @staticmethod
    def _java_bin() -> str:
        jh = os.environ.get("JAVA_HOME")
        if jh:
            cand = Path(jh) / "bin" / "java"
            if cand.exists() or cand.with_suffix(".exe").exists():
                return str(cand)
        return shutil.which("java") or "java"

    @classmethod
    def available(cls) -> bool:
        home = cls._home()
        if home is None:
            return False
        if cls._launcher_jar(home) is None or cls._config_dir(home) is None:
            return False
        return bool(os.environ.get("JAVA_HOME") or shutil.which("java"))

    # -- launch ---------------------------------------------------------------

    def _popen_argv(self) -> list[str]:
        home = self._home()
        jar = self._launcher_jar(home)
        cfg = self._config_dir(home)
        # workspace `-data` isolado por instância (evita lock entre execuções).
        self._data = Path(tempfile.mkdtemp(prefix="cg-jdtls-"))
        return [self._java_bin(),
                "-Declipse.application=org.eclipse.jdt.ls.core.id1",
                "-Dosgi.bundles.defaultStartLevel=4",
                "-Declipse.product=org.eclipse.jdt.ls.core.product",
                "-Dlog.level=OFF", "-Xmx1G",
                "--add-modules=ALL-SYSTEM",
                "--add-opens", "java.base/java.util=ALL-UNNAMED",
                "--add-opens", "java.base/java.lang=ALL-UNNAMED",
                "-jar", str(jar),
                "-configuration", str(cfg),
                "-data", str(self._data)]

    def close(self) -> None:
        super().close()
        data = getattr(self, "_data", None)
        if data is not None:
            shutil.rmtree(data, ignore_errors=True)
