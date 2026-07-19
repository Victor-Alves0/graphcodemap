"""Extractor L0 dedicado para Swift (tree-sitter).

Símbolos: class/struct/enum/actor/extension (via class_declaration +
declaration_kind), protocol, func, init. Refs: calls (nome no site do NOME),
imports, inherits (inheritance_specifier — superclasse e conformidade a
protocolos, indistinguíveis estaticamente em Swift).
"""

from __future__ import annotations

from .base import BaseExtractor

_KIND_MAP = {"struct": "struct", "enum": "enum", "extension": "class",
             "actor": "class", "class": "class"}


class SwiftExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "class_declaration":
            self._type_decl(node)
            return
        if t == "protocol_declaration":
            self._protocol(node)
            return
        if t in ("function_declaration", "protocol_function_declaration"):
            self._function(node, "func")
            return
        if t == "init_declaration":
            self._function(node, "init")
            return
        if t == "import_declaration":
            self._import(node)
            return
        if t == "call_expression":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        for c in node.children:
            self.visit(c)

    # -- defs ----------------------------------------------------------------

    def _type_decl(self, node) -> None:
        dk_node = node.child_by_field_name("declaration_kind")
        dk = self.text(dk_node) if dk_node is not None else "class"
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._type_name(name_node)
        body = node.child_by_field_name("body")
        # extension reabre o escopo de um tipo existente — não declara um novo
        # símbolo (senão vira fqn duplicado); só adiciona os membros ao tipo
        is_extension = dk == "extension"
        if not is_extension:
            self.add_sym(node, _KIND_MAP.get(dk, "class"), name,
                         signature=self.sig_of(node, body), doc=self._doc(node))
        self.scope.append((name, "class"))
        for c in node.named_children:
            if c.type == "inheritance_specifier":
                self._inherit(c)
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _protocol(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._type_name(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "interface", name, signature=self.sig_of(node, body),
                     doc=self._doc(node))
        self.scope.append((name, "class"))
        for c in node.named_children:
            if c.type == "inheritance_specifier":
                self._inherit(c)
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _function(self, node, which: str) -> None:
        if which == "init":
            name = "init"
        else:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = self.text(name_node)
        kind = "method" if self.scope else "function"
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node))
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _inherit(self, spec) -> None:
        tgt = spec.child_by_field_name("inherits_from")
        if tgt is not None:
            self.add_ref(tgt, "inherits", self._type_name(tgt))

    def _type_name(self, node) -> str:
        # type_identifier direto, ou user_type que embrulha um type_identifier
        if node.type == "user_type":
            for c in node.named_children:
                if c.type == "type_identifier":
                    return self.text(c)
        return self.text(node).split("<", 1)[0].strip()

    def _doc(self, node) -> str | None:
        prev = node.prev_sibling
        if prev is not None and prev.type in ("comment", "multiline_comment"):
            raw = self.text(prev)
            if raw.startswith("///") or raw.startswith("/**"):
                return raw.lstrip("/*").strip().splitlines()[0] or None
        return None

    # -- refs ----------------------------------------------------------------

    def _call(self, node) -> None:
        if not node.named_children:
            return
        callee = node.named_children[0]
        if callee.type == "simple_identifier":
            self.add_ref(callee, "calls", self._qualify(self.text(callee)))
        elif callee.type == "navigation_expression":
            suffix = callee.child_by_field_name("suffix")
            name_node = (suffix.child_by_field_name("suffix")
                         if suffix is not None else None)
            if name_node is not None:
                self.add_ref(name_node, "calls", self.text(name_node))

    def _import(self, node) -> None:
        for c in node.named_children:
            name = self._first_identifier(c)
            if name:
                self.aliases[name] = name
                self.add_ref(node, "imports", name)
                return

    def _first_identifier(self, node) -> str | None:
        if node.type in ("simple_identifier", "identifier", "type_identifier"):
            return self.text(node).split(".", 1)[0]
        for c in node.named_children:
            r = self._first_identifier(c)
            if r:
                return r
        return None
