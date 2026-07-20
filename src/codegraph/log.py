"""Observabilidade opt-in: logging leve via stdlib, zero dependência nova.

Uma biblioteca não deve poluir a saída do usuário: por padrão é SILENCIOSO
(NullHandler). Liga por variável de ambiente, com saída em ``stderr``:

- ``CODEGRAPH_LOG=debug|info|warning|error`` — nível explícito;
- ``CODEGRAPH_DEBUG=1`` — atalho para ``debug``.

Todos os módulos pegam um logger via ``log.get(__name__)`` e chamam
``.debug()/.warning()`` normalmente. Sem env var, nada é escrito — mas os
eventos ainda podem ser inspecionados por quem anexar um handler ao logger
raiz ``codegraph`` (ex.: um servidor MCP que queira encaminhar diagnósticos).

Chamar ``get()`` é barato e idempotente: a configuração roda uma vez só.
"""

from __future__ import annotations

import logging
import os
import sys

_ROOT = "codegraph"
_configured = False

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


def _env_level() -> int | None:
    dbg = os.environ.get("CODEGRAPH_DEBUG", "").strip().lower()
    if dbg not in ("", "0", "false", "no", "off"):
        return logging.DEBUG
    name = os.environ.get("CODEGRAPH_LOG", "").strip().lower()
    return _LEVELS.get(name) if name else None


def _configure() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger(_ROOT)
    root.propagate = False  # não vaza para o root logging do app hospedeiro
    level = _env_level()
    if level is None:
        # silencioso: NullHandler evita o "lastResort" da stdlib (que
        # imprimiria WARNING+ em stderr sem o usuário ter pedido).
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.WARNING)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                          "%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(level)


def get(name: str) -> logging.Logger:
    """Logger filho de ``codegraph``. ``name`` costuma ser ``__name__``."""
    _configure()
    short = name.split(".")[-1] if name else _ROOT
    return logging.getLogger(f"{_ROOT}.{short}")


def enabled() -> bool:
    """True se alguma saída de log está ativa (env var setada)."""
    _configure()
    return _env_level() is not None
