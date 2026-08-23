"""Descoberta e instalação explícita dos toolchains semânticos.

Contrato de segurança:

* nada é instalado sem ``--install`` e consentimento do chamador;
* versões são fixadas no código;
* downloads diretos exigem SHA-256 e extração confinada;
* o que já existe na máquina ou no monorepo sempre vence;
* caminhos descobertos ficam numa configuração do usuário, nunca no repo.

Os gerenciadores de pacote (pip/npm/go/rustup/gem/dotnet/winget) continuam
responsáveis por assinatura/integridade dos seus artefatos. Archives que o
GraphCodeMap baixa por conta própria são verificados aqui antes da extração.
"""

from __future__ import annotations

import glob
import hashlib
import importlib
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from .indexer import iter_source_files
from .languages import language_for
from .tool_config import (apply_saved_environment, config_path, save,
                          tools_dir as default_tools_dir)


VERSIONS = {
    "jedi": "0.20.0",
    "typescript": "7.0.2",
    "intelephense": "1.18.5",
    "gopls": "v0.23.0",
    "rust": "1.89.0",
    "solargraph": "0.60.3",
    "csharp-ls": "0.26.0",
    "jdtls": "1.60.0",
    "jdtls_build": "202606262232",
    "clojure-lsp": "2026.07.06-14.34.19",
    "kotlin-language-server": "1.3.13",
    "coursier": "2.1.24",
    "metals": "1.6.8",
    "mcp": "1.29.0",
    "mcp_min": "1.2",
    "mcp_max": "2",
}

_WINGET = {
    "java21": ("EclipseAdoptium.Temurin.21.JDK", "21.0.12.101"),
    "node": ("OpenJS.NodeJS.LTS", "24.19.0"),
    "go": ("GoLang.Go", "1.26.7"),
    "rustup": ("Rustlang.Rustup", "1.29.0"),
    "dotnet": ("Microsoft.DotNet.SDK.8", "8.0.424"),
    "ruby": ("RubyInstallerTeam.RubyWithDevKit.3.3", "3.3.12-1"),
    "clangd": ("LLVM.LLVM", "22.1.8"),
    "lua-language-server": ("LuaLS.lua-language-server", "3.18.2"),
    "swift": ("Swift.Toolchain", "6.3.3"),
}

_ARCHIVES = {
    "jdtls": {
        "url": ("https://download.eclipse.org/jdtls/milestones/1.60.0/"
                "jdt-language-server-1.60.0-202606262232.tar.gz"),
        "sha256": "e94c303d8198f977930803582738771fd18c52c5492878410bf222b1aa81ef1d",
        "archive": "tar.gz",
    },
    "clojure-windows": {
        "url": ("https://github.com/clojure-lsp/clojure-lsp/releases/download/"
                "2026.07.06-14.34.19/clojure-lsp-native-windows-amd64.zip"),
        "sha256": "7b978ab266f7aa0ecf48b7484fc0aa6d3b3b7b395c27c47c949d6ce93174599d",
        "archive": "zip",
    },
    "kotlin-windows": {
        "url": ("https://github.com/fwcd/kotlin-language-server/releases/"
                "download/1.3.13/server.zip"),
        "sha256": "4fe7d71d087b307c7869036171bd9d8c6a4284cd7c25b89098b0a24eb2d9b6d2",
        "archive": "zip",
    },
    "coursier-windows": {
        "url": ("https://github.com/coursier/coursier/releases/download/"
                "v2.1.24/cs-x86_64-pc-win32.zip"),
        "sha256": "c16b4f95b59fbe035cdda4353cfca78befcbbef5a20a3b151c679717c1504c9f",
        "archive": "zip",
    },
}
_MAX_DIRECT_DOWNLOAD = 512 * 1024 * 1024


@dataclass(frozen=True)
class Step:
    kind: str
    label: str
    argv: tuple[str, ...] = ()
    url: str | None = None
    sha256: str | None = None
    destination: Path | None = None
    archive: str | None = None
    marker: str | None = None
    environment: tuple[tuple[str, str], ...] = ()

    def display(self) -> str:
        if self.kind == "download":
            return (f"{self.label}: {self.url} → {self.destination} "
                    f"[sha256:{(self.sha256 or '')[:12]}…]")
        if self.kind == "manual":
            return self.label
        return f"{self.label}: {_display_argv(self.argv)}"


@dataclass
class Plan:
    target: str
    languages: tuple[str, ...]
    ready: bool
    reason: str
    steps: list[Step]


TARGET_LANGUAGES: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "javascript": ("javascript", "typescript", "tsx"),
    "go": ("go",),
    "rust": ("rust",),
    "cpp": ("c", "cpp", "cuda"),
    "lua": ("lua", "luau"),
    "clojure": ("clojure",),
    "php": ("php",),
    "ruby": ("ruby",),
    "kotlin": ("kotlin",),
    "java": ("java",),
    "csharp": ("csharp",),
    "scala": ("scala",),
    "swift": ("swift",),
    "mcp": (),
}

ALIASES = {
    "py": "python",
    "js": "javascript", "ts": "javascript", "tsx": "javascript",
    "typescript": "javascript",
    "golang": "go",
    "rs": "rust",
    "c": "cpp", "c++": "cpp", "cuda": "cpp", "clangd": "cpp",
    "luau": "lua",
    "clj": "clojure",
    "rb": "ruby",
    "kt": "kotlin",
    "jvm": "java",
    "c#": "csharp", "cs": "csharp", ".net": "csharp",
}


def normalize_targets(values: Iterable[str]) -> list[str]:
    targets = []
    for raw in values:
        name = raw.strip().lower()
        name = ALIASES.get(name, name)
        if name not in TARGET_LANGUAGES:
            choices = ", ".join(TARGET_LANGUAGES)
            raise ValueError(f"alvo de setup desconhecido: {raw!r}; use {choices}")
        if name not in targets:
            targets.append(name)
    return targets


def detect_targets(root: str | Path) -> list[str]:
    root = Path(root).resolve()
    languages = {
        language_for(rel) for rel in iter_source_files(root)
    }
    out = []
    for target, family in TARGET_LANGUAGES.items():
        if family and languages.intersection(family):
            out.append(target)
    return out


def _resolver_map() -> dict[str, Any]:
    from . import l1

    out = {}
    for resolver_type in l1.all_resolvers():
        resolver: Any = resolver_type
        for language in resolver.languages:
            target = next((name for name, family in TARGET_LANGUAGES.items()
                           if language in family), None)
            if target:
                out[target] = resolver
    return out


def _resolver_ready(resolver: Any, root: Path) -> bool:
    try:
        hook = getattr(resolver, "available_for_root", None)
        return bool(hook(root) if hook is not None else resolver.available())
    except Exception:
        return False


def _mcp_ready() -> bool:
    # ``pip install`` pode ter acabado de rodar neste mesmo processo. Tanto os
    # finders de import quanto ``importlib.metadata`` cacheiam o miss anterior
    # em alguns ambientes. Um subprocesso curto com o MESMO interpretador é a
    # verificação fiel do próximo boot real do servidor e não depende desses
    # caches internos.
    importlib.invalidate_caches()
    probe = (
        "import importlib.metadata as m; "
        "v=m.version('mcp'); major=int(v.split('.',1)[0]); "
        "assert 1 <= major < 2; "
        "from mcp.server.fastmcp import FastMCP"
    )
    try:
        result = subprocess.run([sys.executable, "-c", probe],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _unavailable_reason(resolver: Any) -> str:
    details = getattr(resolver, "unavailable_details", None)
    if details is not None:
        try:
            data = details()
            if data.get("reason"):
                return str(data["reason"])
        except Exception:
            pass
    server = getattr(resolver, "cmd_name", "") or "dependência semântica"
    return f"{server} não foi localizado"


def build_plans(root: str | Path, targets: Iterable[str] | None = None,
                *, all_languages: bool = False,
                base_dir: str | Path | None = None) -> list[Plan]:
    apply_saved_environment()
    root = Path(root).resolve()
    base = Path(base_dir or default_tools_dir()).expanduser().resolve()
    if all_languages:
        names = [name for name, family in TARGET_LANGUAGES.items() if family]
    elif targets:
        names = normalize_targets(targets)
    else:
        names = detect_targets(root)
    resolvers = _resolver_map()
    plans = []
    for name in names:
        if name == "mcp":
            ready = _mcp_ready()
            plans.append(Plan(name, (), ready,
                              "MCP 1.x com FastMCP disponível" if ready else
                              "MCP 1.x/FastMCP não está disponível",
                              [] if ready else _steps_for(name, base)))
            continue
        resolver = resolvers[name]
        ready = _resolver_ready(resolver, root)
        plans.append(Plan(
            name, TARGET_LANGUAGES[name], ready,
            "resolver semântico localizado" if ready else
            _unavailable_reason(resolver),
            [] if ready else _steps_for(name, base),
        ))
    return plans


def _command(name: str, label: str, *args: str,
             environment: dict[str, str] | None = None) -> Step:
    return Step("command", label, (f"{{{name}}}", *args),
                environment=tuple(sorted((environment or {}).items())))


def _winget(name: str) -> Step:
    package, version = _WINGET[name]
    return Step("command", f"instalar {name} {version} via winget", (
        "{winget}", "install", "--exact", "--id", package,
        "--version", version, "--silent", "--accept-source-agreements",
        "--accept-package-agreements", "--disable-interactivity",
    ))


def _system_runtime(name: str) -> list[Step]:
    if _find_runtime(name):
        return []
    if os.name == "nt":
        return [_winget(name)]
    hints = {
        "java21": "instale um JDK 21+ assinado pela distribuição/Adoptium",
        "node": "instale Node.js 24 LTS e npm pelo gerenciador do sistema",
        "go": "instale Go 1.26 pelo gerenciador do sistema",
        "rustup": "instale rustup 1.29 pelo pacote oficial da plataforma",
        "dotnet": "instale o .NET SDK 8 pelo repositório Microsoft",
        "ruby": "instale Ruby 3.3 e RubyGems pelo gerenciador do sistema",
        "clangd": "instale LLVM/clangd 22 pelo gerenciador do sistema",
        "lua-language-server": "instale LuaLS 3.18.2 pelo gerenciador do sistema",
        "swift": "instale Swift 6.3.3 pelo toolchain oficial",
    }
    return [Step("manual", hints[name])]


def _archive_step(key: str, label: str, destination: Path,
                  marker: str) -> Step:
    spec = _ARCHIVES[key]
    return Step("download", label, url=spec["url"],
                sha256=spec["sha256"], destination=destination,
                archive=spec["archive"], marker=marker)


def _steps_for(target: str, base: Path) -> list[Step]:
    bin_dir = base / "bin"
    if target == "mcp":
        return [_command("python", "instalar MCP compatível",
                         "-m", "pip", "install", f"mcp=={VERSIONS['mcp']}")]
    if target == "python":
        return [_command("python", "instalar Jedi fixado", "-m", "pip",
                         "install", f"jedi=={VERSIONS['jedi']}")]
    if target == "javascript":
        prefix = base / "typescript"
        return [*_system_runtime("node"),
                _command("npm", "instalar TypeScript fixado", "install",
                         "--ignore-scripts", "--no-audit", "--no-fund",
                         "--prefix", str(prefix),
                         f"typescript@{VERSIONS['typescript']}")]
    if target == "php":
        prefix = base / "intelephense"
        return [*_system_runtime("node"),
                _command("npm", "instalar Intelephense fixado", "install",
                         "--ignore-scripts", "--no-audit", "--no-fund",
                         "--prefix", str(prefix),
                         f"intelephense@{VERSIONS['intelephense']}")]
    if target == "go":
        return [*_system_runtime("go"),
                _command("go", "instalar gopls fixado", "install",
                         f"golang.org/x/tools/gopls@{VERSIONS['gopls']}",
                         environment={"GOBIN": str(bin_dir)})]
    if target == "rust":
        return [*_system_runtime("rustup"),
                _command("rustup", "instalar toolchain Rust fixado", "toolchain",
                         "install", VERSIONS["rust"], "--profile", "minimal"),
                _command("rustup", "instalar rust-analyzer fixado", "component",
                         "add", "rust-analyzer", "--toolchain", VERSIONS["rust"])]
    if target == "cpp":
        return _system_runtime("clangd")
    if target == "lua":
        return _system_runtime("lua-language-server")
    if target == "clojure":
        if os.name != "nt":
            return [Step("manual", "instale clojure-lsp 2026.07.06-14.34.19 "
                         "a partir do release oficial com o checksum publicado")]
        dest = base / "clojure" / VERSIONS["clojure-lsp"]
        return [_archive_step("clojure-windows",
                              "baixar clojure-lsp fixado", dest,
                              "clojure-lsp.exe")]
    if target == "ruby":
        gem_home = base / "ruby" / "gems"
        return [*_system_runtime("ruby"),
                _command("gem", "instalar Solargraph fixado", "install",
                         "solargraph", "--version", VERSIONS["solargraph"],
                         "--no-document", "--install-dir", str(gem_home))]
    if target == "kotlin":
        if os.name != "nt":
            return [*_system_runtime("java21"), Step(
                "manual", "instale kotlin-language-server 1.3.13 pelo release oficial")]
        dest = base / "kotlin" / VERSIONS["kotlin-language-server"]
        return [*_system_runtime("java21"),
                _archive_step("kotlin-windows",
                              "baixar kotlin-language-server fixado", dest,
                              "server/bin/kotlin-language-server.bat")]
    if target == "java":
        dest = base / "java" / f"jdtls-{VERSIONS['jdtls']}"
        return [*_system_runtime("java21"),
                _archive_step("jdtls", "baixar Eclipse JDTLS fixado", dest,
                              "plugins")]
    if target == "csharp":
        dest = base / "csharp"
        return [*_system_runtime("dotnet"),
                _command("dotnet", "instalar csharp-ls fixado", "tool", "install",
                         "--tool-path", str(dest), "--version",
                         VERSIONS["csharp-ls"], "csharp-ls")]
    if target == "scala":
        if os.name != "nt":
            return [*_system_runtime("java21"), Step(
                "manual", "instale Coursier 2.1.24 e Metals 1.6.8 pelos releases oficiais")]
        cs_dest = base / "coursier" / VERSIONS["coursier"]
        metals_dest = base / "scala"
        return [*_system_runtime("java21"),
                _archive_step("coursier-windows", "baixar Coursier fixado",
                              cs_dest, "cs-x86_64-pc-win32.exe"),
                _command("coursier", "instalar Metals fixado", "install",
                         f"metals:{VERSIONS['metals']}", "--install-dir",
                         str(metals_dest))]
    if target == "swift":
        return _system_runtime("swift")
    raise AssertionError(target)


def _display_argv(argv: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in argv)


def _candidate_paths(name: str) -> list[str]:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    candidates = {
        "winget": [local / "Microsoft/WindowsApps/winget.exe"],
        "node": [program_files / "nodejs/node.exe"],
        "npm": [program_files / "nodejs/npm.cmd"],
        "go": [program_files / "Go/bin/go.exe"],
        "rustup": [Path.home() / ".cargo/bin/rustup.exe"],
        "rust-analyzer": [Path.home() / ".cargo/bin/rust-analyzer.exe"],
        "dotnet": [program_files / "dotnet/dotnet.exe"],
        "clangd": [program_files / "LLVM/bin/clangd.exe"],
        "lua-language-server": [
            local / "Microsoft/WinGet/Links/lua-language-server.exe"],
        "swift": [local / "Microsoft/WinGet/Links/swift.exe"],
        "sourcekit-lsp": [local / "Microsoft/WinGet/Links/sourcekit-lsp.exe"],
        "ruby": [],
        "gem": [],
        "java21": [],
    }
    if name in {"ruby", "gem"}:
        suffix = "ruby.exe" if name == "ruby" else "gem.cmd"
        candidates[name] += [Path(p) for p in glob.glob(f"C:/Ruby33*/bin/{suffix}")]
    if name == "java21":
        candidates[name] += [Path(p) for p in glob.glob(
            str(program_files / "Eclipse Adoptium/jdk-21*/bin/java.exe"))]
        candidates[name] += [Path(p) for p in glob.glob(
            str(program_files / "Java/jdk-21*/bin/java.exe"))]
    return [str(path) for path in candidates.get(name, ())]


def _java_major(executable: str) -> int | None:
    try:
        result = subprocess.run([executable, "-XshowSettings:properties", "-version"],
                                capture_output=True, text=True, timeout=8,
                                check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    import re
    match = re.search(r"java\.specification\.version\s*=\s*(\d+)",
                      result.stdout + result.stderr)
    return int(match.group(1)) if match else None


def _find_runtime(name: str) -> str | None:
    command = "java" if name == "java21" else (
        "sourcekit-lsp" if name == "swift" else name)
    values = [shutil.which(command), *_candidate_paths(name)]
    for value in values:
        if not value or not Path(value).is_file():
            continue
        if name == "java21" and (_java_major(value) or 0) < 21:
            continue
        if name == "dotnet":
            try:
                result = subprocess.run([value, "--list-sdks"], capture_output=True,
                                        text=True, timeout=8, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode or not any(
                    line.strip().split(".", 1)[0].isdigit()
                    and int(line.strip().split(".", 1)[0]) >= 8
                    for line in result.stdout.splitlines()):
                continue
        return str(Path(value).resolve())
    return None


def _resolve_token(token: str, base: Path) -> str:
    if not (token.startswith("{") and token.endswith("}")):
        return token
    name = token[1:-1]
    if name == "python":
        return sys.executable
    if name == "coursier":
        path = base / "coursier" / VERSIONS["coursier"] / "cs-x86_64-pc-win32.exe"
        if path.is_file():
            return str(path)
    runtime = _find_runtime(name)
    if runtime:
        return runtime
    raise RuntimeError(f"pré-requisito {name!r} não foi localizado após a instalação")


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return bool(name and not path.is_absolute() and ".." not in path.parts)


def _zip_symlink(item: zipfile.ZipInfo) -> bool:
    # Unix mode is stored in the high 16 bits when the producer supplies it.
    return ((item.external_attr >> 16) & 0o170000) == 0o120000


def _download_verified(step: Step, base: Path,
                       opener: Callable = urllib.request.urlopen) -> None:
    assert step.url and step.sha256 and step.destination and step.archive
    marker = step.destination / (step.marker or "")
    if marker.exists():
        return
    cache = base / ".downloads"
    cache.mkdir(parents=True, exist_ok=True)
    suffix = ".tar.gz" if step.archive == "tar.gz" else ".zip"
    archive = cache / (step.sha256 + suffix)
    if not archive.is_file() or _sha256(archive) != step.sha256:
        temporary = archive.with_suffix(archive.suffix + ".part")
        try:
            with opener(step.url, timeout=60) as response, temporary.open("wb") as dst:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_DIRECT_DOWNLOAD:
                        raise RuntimeError("download recusado: excede o limite de 512 MiB")
                    dst.write(chunk)
            actual = _sha256(temporary)
            if actual != step.sha256:
                raise RuntimeError(
                    f"checksum inválido para {step.label}: esperado {step.sha256}, "
                    f"recebido {actual}")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    parent = step.destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=".extract-", dir=parent))
    try:
        if step.archive == "zip":
            with zipfile.ZipFile(archive) as package:
                if any((not _safe_member(item.filename) or _zip_symlink(item))
                       for item in package.infolist()):
                    raise RuntimeError(
                        "archive recusado: link/caminho sai do diretório de destino")
                package.extractall(temporary_dir)
        else:
            with tarfile.open(archive, "r:gz") as package:
                members = package.getmembers()
                if any((not _safe_member(item.name) or item.issym() or item.islnk()
                        or not (item.isfile() or item.isdir()))
                       for item in members):
                    raise RuntimeError("archive recusado: tipo/link/caminho inseguro")
                package.extractall(temporary_dir)
        if not (temporary_dir / (step.marker or "")).exists():
            raise RuntimeError(f"archive não contém marcador esperado: {step.marker}")
        if step.destination.exists():
            shutil.rmtree(step.destination)
        temporary_dir.replace(step.destination)
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_step(step: Step, base: Path,
              runner: Callable = subprocess.run,
              opener: Callable = urllib.request.urlopen) -> None:
    if step.kind == "manual":
        raise RuntimeError(step.label)
    if step.kind == "download":
        _download_verified(step, base, opener=opener)
        return
    argv = [_resolve_token(value, base) for value in step.argv]
    env = os.environ.copy()
    env.update(dict(step.environment))
    result = runner(argv, env=env, check=False)
    if result.returncode:
        raise RuntimeError(f"comando falhou ({result.returncode}): {_display_argv(argv)}")


def _target_environment(target: str, base: Path) -> dict[str, str]:
    if target == "javascript":
        return {
            "CODEGRAPH_NODE": _find_runtime("node") or "",
            "CODEGRAPH_TS_DIR": str(base / "typescript/node_modules/typescript"),
        }
    if target == "php":
        return {
            "CODEGRAPH_NODE": _find_runtime("node") or "",
            "CODEGRAPH_INTELEPHENSE": str(
                base / "intelephense/node_modules/intelephense/lib/intelephense.js"),
        }
    if target == "go":
        return {"CODEGRAPH_GOPLS": str(base / "bin" / (
            "gopls.exe" if os.name == "nt" else "gopls"))}
    if target == "rust":
        executable = _find_runtime("rust-analyzer") or ""
        return {"CODEGRAPH_RUST_ANALYZER": executable}
    if target == "cpp":
        return {"CODEGRAPH_CLANGD": _find_runtime("clangd") or ""}
    if target == "lua":
        return {"CODEGRAPH_LUA_LS": _find_runtime("lua-language-server") or ""}
    if target == "clojure":
        return {"CODEGRAPH_CLOJURE_LSP": str(
            base / "clojure" / VERSIONS["clojure-lsp"] / "clojure-lsp.exe")}
    if target == "ruby":
        suffix = "solargraph.bat" if os.name == "nt" else "solargraph"
        return {"CODEGRAPH_SOLARGRAPH": str(base / "ruby/gems/bin" / suffix)}
    if target == "kotlin":
        suffix = "kotlin-language-server.bat" if os.name == "nt" else \
                 "kotlin-language-server"
        return {"CODEGRAPH_KOTLIN_LS": str(
            base / "kotlin" / VERSIONS["kotlin-language-server"] /
            "server/bin" / suffix)}
    if target == "java":
        return {
            "CODEGRAPH_JDTLS": str(base / "java" / f"jdtls-{VERSIONS['jdtls']}"),
            "CODEGRAPH_JDTLS_JAVA": _find_runtime("java21") or "",
        }
    if target == "csharp":
        suffix = "csharp-ls.exe" if os.name == "nt" else "csharp-ls"
        return {"CODEGRAPH_CSHARP_LS": str(base / "csharp" / suffix)}
    if target == "scala":
        suffix = "metals.bat" if os.name == "nt" else "metals"
        return {"CODEGRAPH_METALS": str(base / "scala" / suffix)}
    if target == "swift":
        return {"CODEGRAPH_SOURCEKIT_LSP": _find_runtime("sourcekit-lsp") or ""}
    return {}


def install(plans: Iterable[Plan], *, base_dir: str | Path | None = None,
            runner: Callable = subprocess.run,
            opener: Callable = urllib.request.urlopen,
            progress: Callable[[str], None] | None = None) -> list[dict]:
    """Executa planos já consentidos e retorna um resultado por alvo."""
    base = Path(base_dir or default_tools_dir()).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda _message: None)
    completed: set[Step] = set()
    results: list[dict[str, object]] = []
    for plan in plans:
        if plan.ready:
            results.append({"target": plan.target, "status": "ready"})
            continue
        try:
            for step in plan.steps:
                if step in completed:
                    continue
                progress(step.display())
                _run_step(step, base, runner=runner, opener=opener)
                importlib.invalidate_caches()
                completed.add(step)
            updates = {key: value for key, value in
                       _target_environment(plan.target, base).items() if value}
            if updates:
                save(updates)
                for key, value in updates.items():
                    os.environ.setdefault(key, value)
            results.append({"target": plan.target, "status": "installed",
                            "environment": updates})
        except Exception as error:
            results.append({"target": plan.target, "status": "failed",
                            "error": f"{type(error).__name__}: {error}"})
    return results


def render(plans: Iterable[Plan], results: Iterable[dict] | None = None) -> str:
    plans = list(plans)
    if results is not None:
        lines = ["resultado do setup"]
        for result in results:
            mark = "✓" if result["status"] in {"ready", "installed"} else "✗"
            detail = f": {result['error']}" if result.get("error") else ""
            lines.append(f"  {mark} {result['target']}: {result['status']}{detail}")
        lines.append(f"  configuração: {config_path()}")
        return "\n".join(lines)
    lines = ["preparação semântica do ambiente"]
    if not plans:
        return "\n".join(lines + [
            "  nenhum alvo detectado; informe uma linguagem ou use --all"])
    for plan in plans:
        mark = "✓" if plan.ready else "⚠"
        langs = f" ({', '.join(plan.languages)})" if plan.languages else ""
        lines.append(f"  {mark} {plan.target}{langs}: {plan.reason}")
        for step in plan.steps:
            lines.append(f"      → {step.display()}")
    if any(not plan.ready for plan in plans):
        names = " ".join(plan.target for plan in plans if not plan.ready)
        lines += ["", f"para instalar explicitamente: codegraph setup {names} --install"]
    return "\n".join(lines)
