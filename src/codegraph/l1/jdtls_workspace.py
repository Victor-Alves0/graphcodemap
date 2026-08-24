"""Workspace persistente, exclusivo e recuperável para o Eclipse JDTLS.

O diretório passado em ``-data`` é estado operacional do servidor, não um
temporário por análise. Reutilizá-lo reduz o custo de importar Maven/Gradle,
mas o Eclipse exige exclusividade e caches antigos não podem sobreviver a uma
mudança de build model, runtime ou distribuição do JDTLS.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path


# v3: settings de importação Maven/Gradle não executam autobuild do Eclipse.
# Caches v2 podem conter classpath/diagnostics produzidos pelo modelo antigo.
WORKSPACE_SCHEMA = 3
_BUILD_FILES = {
    ".classpath", ".project", "build.gradle", "build.gradle.kts",
    "gradle.properties", "gradle-wrapper.properties", "gradlew", "gradlew.bat",
    "libs.versions.toml", "maven-wrapper.properties", "mvnw", "mvnw.cmd",
    "pom.xml", "settings.gradle", "settings.gradle.kts",
}
_SKIP_DIRS = {".git", ".gradle", ".idea", ".metadata", "build", "out", "target"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _cache_root() -> Path:
    override = os.environ.get("CODEGRAPH_JDTLS_WORKSPACES")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get(
            "XDG_CACHE_HOME", Path.home() / ".cache"))
    return (base / "GraphCodeMap" / "jdtls").resolve()


def _canonical(path: Path) -> str:
    value = str(path.resolve())
    return os.path.normcase(value) if os.name == "nt" else value


def _build_fingerprint(root: Path) -> str:
    """Hash de existência, caminho e conteúdo dos arquivos que formam o build.

    Diretórios de saída e metadata são podados para que um build não invalide a
    própria cache. A lista vazia também é um estado: criar o primeiro ``pom`` ou
    ``build.gradle`` muda o fingerprint e força nova importação.
    """
    root = root.resolve()
    records: list[tuple[str, str]] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            if name not in _BUILD_FILES:
                continue
            path = Path(current) / name
            try:
                rel = path.relative_to(root).as_posix()
                content = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            records.append((rel, content))
    return _digest(json.dumps(records, separators=(",", ":")))


class WorkspaceBusy(RuntimeError):
    pass


class _FileLock:
    """Lock exclusivo advisory que funciona com handles do Windows e POSIX."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self, timeout: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                return
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise WorkspaceBusy(
                        "workspace JDTLS em uso por outra análise; aguarde a "
                        "execução concorrente ou configure "
                        "CODEGRAPH_JDTLS_WORKSPACE_LOCK_TIMEOUT")
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class JdtlsWorkspace:
    """Lease de um workspace persistente, versionado e exclusivo."""

    def __init__(self, project_root: Path, home: Path, launcher: Path,
                 config: Path, java: str, java_major: int | None) -> None:
        self.project_root = project_root.resolve()
        self.cache_root = _cache_root()
        runtime_record = {
            "schema": WORKSPACE_SCHEMA,
            "home": _canonical(home),
            "launcher": launcher.name,
            "launcher_size": launcher.stat().st_size,
            "launcher_mtime_ns": launcher.stat().st_mtime_ns,
            "config": config.name,
            "config_mtime_ns": config.stat().st_mtime_ns,
            "java": _canonical(Path(java)),
            "java_major": java_major,
            "java_size": Path(java).stat().st_size,
            "java_mtime_ns": Path(java).stat().st_mtime_ns,
            "jdtls_core": [
                (item.name, item.stat().st_size, item.stat().st_mtime_ns)
                for item in sorted((home / "plugins").glob(
                    "org.eclipse.jdt.ls.core_*.jar"))
            ],
        }
        self.runtime_key = _digest(json.dumps(
            runtime_record, sort_keys=True, separators=(",", ":")))
        self.project_key = _digest(_canonical(self.project_root))
        self.name = f"ws-{self.project_key[:16]}-{self.runtime_key[:16]}"
        self.path = self.cache_root / "workspaces" / self.name
        self.data = self.path / "data"
        self.metadata_path = self.path / "state.json"
        self.lock = _FileLock(self.cache_root / "locks" / f"{self.name}.lock")
        self.reused = False
        self.recovered = False
        self.invalidated = False
        self.build_fingerprint = ""
        self._acquired = False

    @staticmethod
    def _positive_float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} deve ser um número positivo") from exc
        if value < 0 or value == float("inf"):
            raise ValueError(f"{name} deve ser um número positivo")
        return value

    @staticmethod
    def _positive_int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} deve ser um inteiro positivo") from exc
        if value < 1:
            raise ValueError(f"{name} deve ser um inteiro positivo")
        return value

    def _read_metadata(self) -> dict:
        try:
            value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_metadata(self, status: str, fingerprint: str) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        value = {
            "schema": WORKSPACE_SCHEMA,
            "project_root": _canonical(self.project_root),
            "project_key": self.project_key,
            "runtime_key": self.runtime_key,
            "build_fingerprint": fingerprint,
            "status": status,
            "pid": os.getpid() if status == "running" else None,
            "updated_at": int(time.time()),
        }
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.metadata_path)

    def _reset_data(self) -> None:
        expected_parent = (self.cache_root / "workspaces").resolve()
        if self.path.parent.resolve() != expected_parent:
            raise RuntimeError("workspace JDTLS calculado fora da cache esperada")
        shutil.rmtree(self.path, ignore_errors=True)
        self.data.mkdir(parents=True, exist_ok=True)

    def acquire(self) -> "JdtlsWorkspace":
        timeout = self._positive_float(
            "CODEGRAPH_JDTLS_WORKSPACE_LOCK_TIMEOUT", 30.0)
        self.lock.acquire(timeout)
        self._acquired = True
        try:
            fingerprint = _build_fingerprint(self.project_root)
            self.build_fingerprint = fingerprint
            metadata = self._read_metadata()
            valid_identity = (
                metadata.get("schema") == WORKSPACE_SCHEMA
                and metadata.get("project_root") == _canonical(self.project_root)
                and metadata.get("runtime_key") == self.runtime_key
            )
            data_exists = self.data.is_dir()
            if metadata.get("status") == "running" and valid_identity:
                self.recovered = True
            elif (metadata and (not valid_identity
                                or metadata.get("build_fingerprint") != fingerprint)):
                self.invalidated = True
            elif data_exists and valid_identity and metadata.get("status") == "clean":
                self.reused = True

            if not self.reused:
                self._reset_data()
            self._write_metadata("running", fingerprint)
            self._cleanup()
            return self
        except Exception:
            self.lock.release()
            self._acquired = False
            raise

    def release(self, *, clean: bool) -> None:
        if not self._acquired:
            return
        try:
            if clean:
                current = _build_fingerprint(self.project_root)
                if current == self.build_fingerprint:
                    self._write_metadata("clean", current)
                else:
                    # O modelo mudou enquanto o servidor estava aberto. Não
                    # rotule o índice importado antes da mudança como quente.
                    self._write_metadata("stale", self.build_fingerprint)
            # Em crash/transporte quebrado, preserve ``running``. A próxima
            # aquisição sob lock reconhecerá a execução interrompida e zerará.
            self._cleanup()
        finally:
            self.lock.release()
            self._acquired = False

    def _cleanup(self) -> None:
        """Mantém apenas os workspaces mais recentes sem tocar leases ativos."""
        limit = self._positive_int("CODEGRAPH_JDTLS_WORKSPACE_LIMIT", 8)
        parent = self.cache_root / "workspaces"
        try:
            candidates = [p for p in parent.iterdir()
                          if p.is_dir() and p.name.startswith("ws-")]
        except OSError:
            return
        if len(candidates) <= limit:
            return
        candidates.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for candidate in candidates[limit:]:
            if candidate == self.path:
                continue
            lock = _FileLock(self.cache_root / "locks" / f"{candidate.name}.lock")
            try:
                lock.acquire(0.0)
            except WorkspaceBusy:
                continue
            try:
                if candidate.parent.resolve() == parent.resolve():
                    shutil.rmtree(candidate, ignore_errors=True)
            finally:
                lock.release()
