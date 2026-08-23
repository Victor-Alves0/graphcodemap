"""L1 para Java via Eclipse JDT Language Server (jdtls).

Diferente dos resolvers "binário no PATH", o jdtls é uma aplicação Eclipse
lançada por `java -jar <equinox-launcher> -configuration <cfg> -data <ws>` —
o primeiro servidor com *launcher* neste projeto, provando que o cliente
genérico (lsp_base) não presume um único executável.

Ativação: aponte `CODEGRAPH_JDTLS` para a pasta de instalação do JDT LS (a que
contém `plugins/` e `config_*`). `CODEGRAPH_JDTLS_JAVA` pode apontar para o
executável/JDK usado só pelo servidor; `JAVA_HOME` continua sendo o toolchain
do projeto importado. O bytecode do próprio JDTLS determina a versão mínima.
Download: https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .lsp_base import LspResolver


class JdtlsResolver(LspResolver):
    languages = ("java",)
    language_id = "java"
    cmd_name = "java"
    root_markers = ("pom.xml", "build.gradle", "build.gradle.kts",
                    "settings.gradle", ".project")
    # jdtls importa o projeto de forma assíncrona (autobuild); pode demorar.
    ready_timeout = 120.0
    # A primeira textDocument/definition pode ficar retida enquanto o JDTLS
    # importa Gradle/Maven. Um timeout menor que ready_timeout matava o processo
    # aos 30 s mesmo quando a importacao valida terminava logo depois; o warmup
    # entao passava os 90 s restantes consultando um servidor ja morto e a
    # passada acabava com 0 promocoes. O deadline total de _request continua
    # limitado, agora coerente com o contrato de readiness desta subclasse.
    io_timeout = 120.0
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
        jars = list(plugins.glob("org.eclipse.equinox.launcher_*.jar"))
        if not jars:
            return None

        def version_key(path: Path):
            suffix = path.stem.removeprefix("org.eclipse.equinox.launcher_")
            return tuple((0, int(part)) if part.isdigit() else (1, part)
                         for part in re.split(r"[._-]", suffix))

        return max(jars, key=version_key)

    @staticmethod
    def _config_dir(home: Path) -> Path | None:
        name = {"win32": "config_win",
                "darwin": "config_mac"}.get(sys.platform, "config_linux")
        d = home / name
        return d if d.is_dir() else None

    @staticmethod
    def _java_from(value: str | None) -> str | None:
        """Resolve executável ou JAVA_HOME sem alterar o ambiente do projeto."""
        if not value:
            return None
        path = Path(value)
        if path.is_dir():
            path = path / "bin" / "java"
        if path.is_file():
            return str(path)
        exe = path.with_suffix(".exe")
        if exe.is_file():
            return str(exe)
        return shutil.which(value)

    @classmethod
    def _java_bin(cls) -> str | None:
        # O runtime dedicado tem prioridade. Não reescrevemos JAVA_HOME: Gradle
        # e Maven lançados pelo JDTLS continuam vendo o toolchain do projeto.
        dedicated = os.environ.get("CODEGRAPH_JDTLS_JAVA")
        if dedicated:
            return cls._java_from(dedicated)
        return (cls._java_from(os.environ.get("JAVA_HOME"))
                or shutil.which("java"))

    @staticmethod
    def _required_java_major(home: Path) -> int | None:
        """Infere o Java mínimo pelo classfile do plugin central do JDTLS.

        Isto acompanha releases futuras sem congelar ``17``/``21`` no código.
        Se uma distribuição não tiver o layout oficial, a descoberta continua
        utilizável e o launcher produzirá o erro concreto ao iniciar.
        """
        plugins = home / "plugins"
        jars = list(plugins.glob("org.eclipse.jdt.ls.core_*.jar"))
        for jar in sorted(jars, reverse=True):
            try:
                with zipfile.ZipFile(jar) as archive:
                    raw = archive.read(
                        "org/eclipse/jdt/ls/core/internal/"
                        "JavaLanguageServerPlugin.class")
            except (OSError, KeyError, zipfile.BadZipFile):
                continue
            if len(raw) >= 8 and raw[:4] == b"\xca\xfe\xba\xbe":
                class_major = int.from_bytes(raw[6:8], "big")
                if class_major >= 45:
                    return class_major - 44
        return None

    @staticmethod
    def _runtime_java_major(java: str) -> int | None:
        try:
            result = subprocess.run(
                [java, "-XshowSettings:properties", "-version"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = f"{result.stdout}\n{result.stderr}"
        match = re.search(r"java\.specification\.version\s*=\s*(?:1\.)?(\d+)",
                          output)
        if match is None:
            match = re.search(r"\bversion\s+\"(?:1\.)?(\d+)(?:[._\"])",
                              output)
        return int(match.group(1)) if match else None

    @classmethod
    def _runtime_compatible(cls, home: Path, java: str) -> bool:
        required = cls._required_java_major(home)
        if required is None:
            return True
        current = cls._runtime_java_major(java)
        return current is not None and current >= required

    @classmethod
    def available(cls) -> bool:
        home = cls._home()
        if home is None:
            return False
        if cls._launcher_jar(home) is None or cls._config_dir(home) is None:
            return False
        java = cls._java_bin()
        return bool(java and cls._runtime_compatible(home, java))

    # -- launch ---------------------------------------------------------------

    def __init__(self, root: Path, project_root: Path | None = None) -> None:
        try:
            super().__init__(root, project_root)
        except Exception:
            data = getattr(self, "_data", None)
            if data is not None:
                shutil.rmtree(data, ignore_errors=True)
            raise

    def _popen_argv(self) -> list[str]:
        home = self._home()
        if home is None:
            raise RuntimeError("CODEGRAPH_JDTLS não aponta para uma instalação")
        jar = self._launcher_jar(home)
        cfg = self._config_dir(home)
        java = self._java_bin()
        if java is None:
            raise RuntimeError("runtime Java do JDTLS não encontrado")
        required = self._required_java_major(home)
        current = self._runtime_java_major(java) if required is not None else None
        if required is not None and (current is None or current < required):
            found = "desconhecido" if current is None else str(current)
            raise RuntimeError(
                f"JDTLS requer Java {required}+, runtime encontrado: {found}")
        # workspace `-data` isolado por instância (evita lock entre execuções).
        self._data = Path(tempfile.mkdtemp(prefix="cg-jdtls-"))
        return [java,
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
