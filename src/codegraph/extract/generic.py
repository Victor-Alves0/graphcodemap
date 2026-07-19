"""Extractor genérico: heurística estrutural sobre QUALQUER gramática tree-sitter.

Nível de fallback (docs/DESIGN.md §6.2): menos preciso que os dedicados, mas
dá símbolos/calls/imports razoáveis para dezenas de linguagens sem código
específico. Convenções exploradas: node types de definição contêm
class/function/method/…; o nome vem do field `name` ou do primeiro filho
identifier-like; calls contêm "call"/"invocation".
"""

from __future__ import annotations

import re

from .base import BaseExtractor

_NAME_TYPES = {
    "identifier", "name", "type_identifier", "simple_identifier", "constant",
    "word", "field_identifier", "property_identifier", "method_name",
    "class_name", "variable_name", "tag_name", "command_name", "sym",
    "namespace_identifier", "module_name",
}

_KIND_RULES = (
    ("interface", "interface"), ("trait", "interface"), ("protocol", "interface"),
    ("class", "class"),
    ("struct", "struct"),
    ("enum", "enum"),
    ("module", "module"), ("namespace", "module"), ("package_decl", "module"),
    ("method", "method"),
    ("function", "function"), ("subroutine", "function"), ("subprogram", "function"),
    ("procedure", "function"), ("constructor", "method"),
)

_EXCLUDE_SUBSTR = ("call", "argument", "parameter", "variable", "reference",
                   "expression", "comment", "string", "import", "body", "block",
                   "type_arguments", "pointer", "access", "selector", "literal",
                   "modifier", "operator", "pattern", "signature_help")

_IMPORT_KEYWORDS = {"import", "include", "use", "using", "require", "from",
                    "static", "public", "private", "unqualified"}

_IDENT_RE = re.compile(r"[A-Za-z_][\w./:$-]*")


def _kind_for(node_type: str) -> str | None:
    if any(x in node_type for x in _EXCLUDE_SUBSTR):
        return None
    for substr, kind in _KIND_RULES:
        if substr in node_type:
            return kind
    return None


class GenericExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if "import" in t or "include" in t:
            self._import(node)
            return
        if "call" in t or "invocation" in t or t == "command":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        kind = _kind_for(t)
        if kind is not None and node.named_child_count > 0:
            name_node = self._name_of(node)
            if name_node is not None:
                self._definition(node, kind, name_node)
                return
        for c in node.children:
            self.visit(c)

    def _name_of(self, node):
        direct = node.child_by_field_name("name")
        if direct is not None:
            if direct.type in _NAME_TYPES or direct.named_child_count == 0:
                return direct
            # nome pontuado (ex.: lua `function M.process`): último segmento
            for c in reversed(direct.named_children):
                if c.type in _NAME_TYPES:
                    return c
        for c in node.named_children[:4]:
            if c.type in _NAME_TYPES:
                return c
        return None

    def _definition(self, node, kind: str, name_node) -> None:
        name = self.text(name_node).strip()
        if not name or len(name) > 120 or "\n" in name:
            return
        if kind == "function" and self.in_class():
            kind = "method"
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=None)
        scope_kind = "class" if kind in ("class", "interface", "struct", "enum",
                                         "module") else "function"
        self.scope.append((name, scope_kind))
        for c in (body.children if body is not None else node.children):
            self.visit(c)
        self.scope.pop()

    def _call(self, node) -> None:
        target = None
        for field in ("function", "name", "method", "target", "command"):
            target = node.child_by_field_name(field)
            if target is not None:
                break
        if target is None and node.named_children:
            target = node.named_children[0]
        if target is None:
            return
        raw = self.text(target).strip().replace("::", ".")
        if any(ch in raw for ch in "\n(["):
            raw = raw.rsplit(".", 1)[-1] if "." in raw else ""
        if not raw or not _IDENT_RE.fullmatch(raw.replace("?", "").replace("!", "")):
            return
        base = raw.split(".", 1)[0]
        if base in self.aliases:
            rest = raw.partition(".")[2]
            raw = f"{self.aliases[base]}.{rest}" if rest else self.aliases[base]
        self.add_ref(node, "calls", raw)

    def _import(self, node) -> None:
        text = self.text(node).split("\n", 1)[0][:200]
        tokens = [t for t in _IDENT_RE.findall(text)
                  if t.lower() not in _IMPORT_KEYWORDS]
        if not tokens:
            return
        dotted = tokens[0].replace("/", ".").strip(".")
        if dotted:
            self.aliases[dotted.rsplit(".", 1)[-1]] = dotted
            self.add_ref(node, "imports", dotted)
