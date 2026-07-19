"""Extractor L0 dedicado para Ruby (tree-sitter).

Símbolos: module, class (com superclass), method, singleton_method (def self.x),
constantes de topo. Refs: calls (nome do método, no site do NOME), imports
(require/require_relative), inherits (superclass).
"""

from __future__ import annotations

from .base import BaseExtractor

_REQUIRE = {"require", "require_relative", "load", "autoload"}


class RubyExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t in ("module", "class"):
            self._container(node, "module" if t == "module" else "class")
            return
        if t == "method":
            self._method(node, node.child_by_field_name("name"))
            return
        if t == "singleton_method":
            # def self.x / def Foo.x — método de classe
            self._method(node, node.child_by_field_name("name"))
            return
        if t == "call":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        if t == "assignment":
            self._toplevel_const(node)
            for c in node.children:
                self.visit(c)
            return
        for c in node.children:
            self.visit(c)

    # -- defs ----------------------------------------------------------------

    def _container(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._const_name(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node))
        self.scope.append((name, "class"))
        sup = node.child_by_field_name("superclass")
        if sup is not None:
            for c in sup.named_children:
                if c.type in ("constant", "scope_resolution"):
                    self.add_ref(c, "inherits", self._const_name(c).rsplit(".", 1)[-1])
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _method(self, node, name_node) -> None:
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

    def _toplevel_const(self, node) -> None:
        if self.scope:
            return
        left = node.child_by_field_name("left")
        if left is not None and left.type == "constant":
            self.add_sym(left, "constant", self.text(left), signature=None, doc=None)

    def _const_name(self, node) -> str:
        # constant → 'Foo'; scope_resolution 'A::B' → 'A.B'
        return self.text(node).replace("::", ".")

    def _doc(self, node) -> str | None:
        prev = node.prev_sibling
        lines = []
        while prev is not None and prev.type == "comment":
            lines.append(self.text(prev).lstrip("# ").rstrip())
            prev = prev.prev_sibling
        return "\n".join(reversed(lines)).strip() or None if lines else None

    # -- refs ----------------------------------------------------------------

    def _call(self, node) -> None:
        method = node.child_by_field_name("method")
        if method is None:
            return
        name = self.text(method)
        receiver = node.child_by_field_name("receiver")
        if name in _REQUIRE and receiver is None:
            self._import(node)
            return
        self.add_ref(method, "calls", name)

    def _import(self, node) -> None:
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        for a in args.named_children:
            if a.type == "string":
                spec = self.text(a).strip("'\"")
                mod = spec.replace("/", ".").strip(".")
                if mod:
                    self.add_ref(node, "imports", mod)
