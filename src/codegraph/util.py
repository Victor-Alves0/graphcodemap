"""Hashing e identidade de símbolos.

Identidade (docs/DESIGN.md §1.1): symbol_id = hash(path, fqn, kind, ordinal).
Estável sob edições de corpo/linhas; mover de arquivo quebra a identidade (v1).
"""

from __future__ import annotations

import hashlib


def content_hash(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def symbol_uid(path: str, fqn: str, kind: str, ordinal: int) -> str:
    key = f"{path}\x00{fqn}\x00{kind}\x00{ordinal}".encode("utf-8")
    return hashlib.blake2b(key, digest_size=10).hexdigest()


def like_escape(s: str) -> str:
    """Escapa curingas de LIKE (usar com ESCAPE '\\\\'). Identificadores têm '_'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
