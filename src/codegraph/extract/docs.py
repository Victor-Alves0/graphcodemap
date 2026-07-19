"""Extractors de dados/docs: Markdown (headings) e JSON/YAML/TOML (chaves).

Docs entram no grafo como estrutura navegável (seções), não como texto —
o agente lê o arquivo no span. Configs expõem só chaves de topo (âncoras
para find_symbol), sem valores.
"""

from __future__ import annotations

import re

from .base import BaseExtractor

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


class MarkdownExtractor(BaseExtractor):
    def run(self, tree):  # heading por regex é mais robusto que a gramática
        stack: list[tuple[int, str]] = []
        for i, line in enumerate(
                self.source.decode("utf-8", "replace").splitlines(), 1):
            m = _HEADING.match(line)
            if m is None:
                continue
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            self.scope = [(name, "class") for _, name in stack]
            self.add_sym(_FakeNode(i, len(line)), "section", title,
                         signature=None, doc=None)
            stack.append((level, title))
        self.scope = []
        return self.syms, self.refs

    def visit(self, node) -> None:  # pragma: no cover
        pass


class _FakeNode:
    """Span sintético (linha única) para símbolos extraídos por regex."""

    def __init__(self, line: int, width: int) -> None:
        self.start_point = (line - 1, 0)
        self.end_point = (line - 1, width)
        self.start_byte = 0
        self.end_byte = 0


class ConfigExtractor(BaseExtractor):
    """Chaves de topo de JSON/YAML/TOML como símbolos kind='key'."""

    _KEY_NODES = {"pair": "key", "block_mapping_pair": "key", "table": "key",
                  "flow_pair": "key"}

    def visit(self, node, depth: int = 0) -> None:
        t = node.type
        if t in ("pair", "block_mapping_pair", "flow_pair"):
            if depth <= 4:  # documento → objeto/mapping de topo
                key = node.child_by_field_name("key")
                if key is not None:
                    name = self.text(key).strip("\"' ")
                    if name and len(name) <= 80 and not self.scope:
                        self.add_sym(node, "key", name, signature=None, doc=None)
            return  # não desce em valores (só topo)
        if t == "table":  # toml [section]
            for c in node.named_children:
                if c.type in ("bare_key", "dotted_key", "quoted_key"):
                    self.add_sym(node, "key", self.text(c), signature=None,
                                 doc=None)
                    break
            return
        for c in node.children:
            self.visit(c, depth + 1)
