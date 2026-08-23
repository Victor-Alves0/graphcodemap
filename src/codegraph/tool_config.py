"""Configuração local dos toolchains semânticos instalados pelo GraphCodeMap.

O arquivo é do usuário, nunca do repositório analisado. Valores definidos pelo
processo têm precedência: uma configuração local não pode sequestrar um
``CODEGRAPH_*`` explicitamente fornecido pelo host/CI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


_ALLOWED_ENV = frozenset({
    "CODEGRAPH_NODE",
    "CODEGRAPH_TS_DIR",
    "CODEGRAPH_GOPLS",
    "CODEGRAPH_RUST_ANALYZER",
    "CODEGRAPH_CLANGD",
    "CODEGRAPH_LUA_LS",
    "CODEGRAPH_CLOJURE_LSP",
    "CODEGRAPH_INTELEPHENSE",
    "CODEGRAPH_SOLARGRAPH",
    "CODEGRAPH_KOTLIN_LS",
    "CODEGRAPH_JDTLS",
    "CODEGRAPH_JDTLS_JAVA",
    "CODEGRAPH_CSHARP_LS",
    "CODEGRAPH_METALS",
    "CODEGRAPH_SOURCEKIT_LSP",
})


def config_path() -> Path:
    override = os.environ.get("CODEGRAPH_TOOL_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "GraphCodeMap" / "toolchains.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "graphcodemap" / "toolchains.json"


def tools_dir() -> Path:
    override = os.environ.get("CODEGRAPH_TOOLS_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "GraphCodeMap" / "tools"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "graphcodemap" / "tools"


def load() -> dict[str, str]:
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    env = raw.get("environment", {}) if isinstance(raw, dict) else {}
    if not isinstance(env, dict):
        return {}
    return {
        key: value for key, value in env.items()
        if key in _ALLOWED_ENV and isinstance(value, str) and value.strip()
    }


def apply_saved_environment() -> dict[str, str]:
    """Aplica somente chaves ausentes e devolve as que foram aplicadas."""
    applied = {}
    for key, value in load().items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def save(updates: dict[str, str]) -> Path:
    """Mescla caminhos verificados e grava atomicamente a configuração."""
    clean = {
        key: str(value) for key, value in updates.items()
        if key in _ALLOWED_ENV and str(value).strip()
    }
    current = load()
    current.update(clean)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({
        "schema_version": 1,
        "environment": dict(sorted(current.items())),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
