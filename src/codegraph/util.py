"""Hashing e identidade de símbolos.

Identidade (docs/DESIGN.md §1.1): symbol_id = hash(path, fqn, kind,
discriminador). Callables usam a assinatura como discriminador; os demais
símbolos usam ordinal. Assim inserir um overload não troca a identidade dos
overloads existentes. Mover de arquivo continua quebrando a identidade (v1).
"""

from __future__ import annotations

import hashlib


def content_hash(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def byte_column(source: bytes, offset: int) -> int:
    """Coluna 0-based a partir de um offset de bytes tree-sitter.

    Evita ler ``Point.column``/``point[1]``. Essa leitura corrompe o heap em
    combinações reais do py-tree-sitter 0.26.0; ``start_byte``/``end_byte`` são
    estáveis e a coluna do tree-sitter também é medida em bytes.
    """
    line_start = source.rfind(b"\n", 0, offset) + 1
    return offset - line_start


def symbol_uid(path: str, fqn: str, kind: str, discriminator: str | int) -> str:
    key = f"{path}\x00{fqn}\x00{kind}\x00{discriminator}".encode("utf-8")
    return hashlib.blake2b(key, digest_size=10).hexdigest()


def like_escape(s: str) -> str:
    """Escapa curingas de LIKE (usar com ESCAPE '\\\\'). Identificadores têm '_'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
