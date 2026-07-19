"""Tipos intermediários da extração L0 e utilidades comuns aos extractors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..util import content_hash


@dataclass
class Sym:
    kind: str          # function|method|class|interface|struct|enum|variable|constant|module|type_alias
    name: str
    fqn: str
    parent_fqn: str | None
    signature: str | None
    doc: str | None
    start_line: int    # 1-based
    start_col: int
    end_line: int
    end_col: int
    body_hash: str
    visibility: str | None = None


@dataclass
class Ref:
    kind: str          # calls|imports|inherits|references
    src_fqn: str | None  # símbolo que contém o site da referência; None = nível de módulo
    dst_name: str      # alvo textual (guess) — SEMPRE preenchido
    line: int          # 1-based
    col: int = 0       # 0-based; posição do NOME do alvo (para resolvers L1)


_WS = re.compile(r"\s+")


class BaseExtractor:
    def __init__(self, source: bytes, module_fqn: str) -> None:
        self.source = source
        self.module_fqn = module_fqn
        self.syms: list[Sym] = []
        self.refs: list[Ref] = []
        self.scope: list[tuple[str, str]] = []  # (name, kind)
        self.aliases: dict[str, str] = {}       # nome local -> fqn guess importado

    # -- helpers -------------------------------------------------------------

    def text(self, node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def fqn_here(self, name: str) -> str:
        parts = [self.module_fqn, *(n for n, _ in self.scope), name]
        return ".".join(p for p in parts if p)

    def enclosing_fqn(self) -> str | None:
        if not self.scope:
            return None
        return ".".join([self.module_fqn, *(n for n, _ in self.scope)])

    def in_class(self) -> bool:
        return bool(self.scope) and self.scope[-1][1] == "class"

    def sig_of(self, node, body) -> str:
        end = body.start_byte if body is not None else node.end_byte
        raw = self.source[node.start_byte : end].decode("utf-8", "replace")
        return _WS.sub(" ", raw).strip().rstrip(":{").strip()

    def add_sym(self, node, kind: str, name: str, *, signature: str | None = None,
                doc: str | None = None, visibility: str | None = None) -> Sym:
        s = Sym(
            kind=kind,
            name=name,
            fqn=self.fqn_here(name),
            parent_fqn=self.enclosing_fqn(),
            signature=signature,
            doc=doc,
            start_line=node.start_point[0] + 1,
            start_col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            end_col=node.end_point[1],
            body_hash=content_hash(self.source[node.start_byte : node.end_byte]),
            visibility=visibility,
        )
        self.syms.append(s)
        return s

    def add_ref(self, node, kind: str, dst_name: str) -> None:
        if not dst_name:
            return
        self.refs.append(
            Ref(kind=kind, src_fqn=self.enclosing_fqn(), dst_name=dst_name,
                line=node.start_point[0] + 1, col=node.start_point[1])
        )

    def _qualify(self, name: str) -> str:
        """Reescreve o primeiro segmento via mapa de imports (ex.: alias → fqn)."""
        base, _, rest = name.partition(".")
        if base in self.aliases:
            mapped = self.aliases[base]
            return f"{mapped}.{rest}" if rest else mapped
        return name

    def run(self, tree) -> tuple[list[Sym], list[Ref]]:
        self.visit(tree.root_node)
        return self.syms, self.refs

    def visit(self, node) -> None:  # pragma: no cover - abstrato
        raise NotImplementedError
