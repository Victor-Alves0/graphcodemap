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
from pathlib import Path, PurePosixPath

from . import promote

_DEV_ROOT = Path(__file__).resolve().parents[3]  # layout src/: raiz do repo

_CALLABLE_KINDS = frozenset({
    "function", "local function", "method", "class", "local class",
    "constructor", "getter", "setter",
})
_TS_DISCOVERY_MAX_DIRS = 1000
_TS_DISCOVERY_MAX_DEPTH = 6
_TS_DISCOVERY_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".codegraph", "dist", "build", "coverage",
    ".next", "vendor", "target", "out",
})
_TS_CODE_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})


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


def _workspace_patterns(root: Path) -> tuple[str, ...]:
    """Workspaces npm/yarn válidos, sem permitir escapes ou árvores geradas."""
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ()
    raw = data.get("workspaces", ()) if isinstance(data, dict) else ()
    if isinstance(raw, dict):
        raw = raw.get("packages", ())
    if not isinstance(raw, list):
        return ()
    valid = []
    for item in raw:
        if not isinstance(item, str):
            continue
        raw_pattern = item.strip().replace("\\", "/")
        if raw_pattern.startswith("/") or Path(raw_pattern).is_absolute():
            continue
        pattern = raw_pattern.strip("/")
        parts = PurePosixPath(pattern).parts
        if (not pattern or pattern.startswith("!") or ".." in parts or any(
                    part in _TS_DISCOVERY_SKIP_DIRS or part.startswith(".")
                    for part in parts)):
            continue
        valid.append(pattern)
    return tuple(sorted(set(valid)))


def _workspace_match(rel: str, patterns: tuple[str, ...]) -> bool:
    path = PurePosixPath(rel or ".")
    return any(path.match(pattern) for pattern in patterns)


def _subproject_ts_candidates(root: Path) -> list[Path]:
    """Descobre instalações por workspace/subprojeto, com busca confinada.

    Monorepos nem sempre fazem hoist do TypeScript para a raiz. A busca não
    segue symlinks, não entra em árvores geradas e tem limites explícitos de
    profundidade/diretórios para um checkout hostil não transformar discovery
    em uma varredura sem fim.
    """
    root = root.resolve()
    workspace_patterns = _workspace_patterns(root)
    queue = [(root, 0)]
    seen: set[Path] = set()
    candidates: list[tuple[Path, Path, int]] = []
    manifests: set[Path] = set()
    code_dirs: set[Path] = set()
    visited = 0
    while queue and visited < _TS_DISCOVERY_MAX_DIRS:
        current, depth = queue.pop(0)
        try:
            lexical = Path(os.path.abspath(current))
            resolved = current.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved != lexical:  # symlink/junction, ainda que aponte para dentro
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        visited += 1

        if (resolved / "package.json").is_file():
            manifests.add(resolved)

        candidate = resolved / "node_modules" / "typescript"
        try:
            package_root = candidate.resolve()
            package_root.relative_to(root)
        except (OSError, ValueError):
            package_root = None
        entrypoint = package_root / "lib" / "typescript.js" \
            if package_root is not None else None
        if (package_root is not None
                and package_root == Path(os.path.abspath(candidate))
                and not candidate.is_symlink()
                and not candidate.parent.is_symlink()
                and not entrypoint.parent.is_symlink()
                and not entrypoint.is_symlink()
                and entrypoint.is_file()):
            candidates.append((package_root, resolved, depth))

        if depth >= _TS_DISCOVERY_MAX_DEPTH:
            continue
        try:
            children = sorted(resolved.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for child in children:
            if (child.name in _TS_DISCOVERY_SKIP_DIRS
                    or child.name.startswith(".") or child.is_symlink()):
                continue
            try:
                if child.is_dir():
                    queue.append((child, depth + 1))
                elif child.is_file() and child.suffix.lower() in _TS_CODE_SUFFIXES:
                    code_dirs.add(resolved)
            except OSError:
                continue

    def relevance(item: tuple[Path, Path, int]):
        _package_root, owner, depth = item
        rel = owner.relative_to(root).as_posix()
        distances = [len(code.relative_to(owner).parts)
                     for code in code_dirs if code == owner or owner in code.parents]
        code_distance = min(distances) if distances else _TS_DISCOVERY_MAX_DEPTH + 1
        workspace = _workspace_match(rel, workspace_patterns)
        manifest = owner in manifests
        if workspace and distances:
            tier = 0
        elif workspace:
            tier = 1
        elif manifest and distances:
            tier = 2
        elif distances:
            tier = 3
        elif manifest:
            tier = 4
        else:
            tier = 5
        return tier, code_distance, depth, rel

    candidates.sort(key=relevance)
    return [package_root for package_root, _owner, _depth in candidates]


def _find_ts(root: Path | None = None) -> str | None:
    for candidate in _ts_candidates(root):
        if (candidate / "lib" / "typescript.js").is_file():
            return str(candidate)
    if root is not None:
        for candidate in _subproject_ts_candidates(Path(root)):
            return str(candidate)
    return None


class TsLsResolver:
    languages = ("javascript", "typescript", "tsx")
    cmd_name = "typescript"
    cmd_env = "CODEGRAPH_TS_DIR"
    install_hint = ("CODEGRAPH_TS_DIR deve apontar para a raiz do pacote "
                    "TypeScript que contém lib/typescript.js")
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

    @classmethod
    def unavailable_details(cls) -> dict[str, str]:
        return {
            "reason": "node ou pacote TypeScript não localizado",
            "action": cls.install_hint,
        }

    def __init__(self, root: Path, project_root: Path | None = None) -> None:
        self.root = root                       # sempre a raiz do repo (ver acima)
        self._cache: dict[tuple[str, int, int], dict | None] = {}
        service = Path(__file__).with_name("ts_service.js")
        node, ts_dir = _find_node(), _find_ts(root)
        if node is None or ts_dir is None:
            raise RuntimeError(
                "node/typescript indisponível para a raiz analisada; "
                + self.install_hint)
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
