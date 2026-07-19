"""Extractor L0 para Java (tree-sitter)."""

from __future__ import annotations

from .base import BaseExtractor


class JavaExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "import_declaration":
            self._import(node)
            return
        if t in ("class_declaration", "record_declaration"):
            self._class(node, "class")
            return
        if t == "interface_declaration":
            self._class(node, "interface")
            return
        if t == "enum_declaration":
            self._class(node, "enum")
            return
        if t in ("method_declaration", "constructor_declaration"):
            self._method(node)
            return
        if t == "method_invocation":
            self._invocation(node)
            for c in node.children:
                self.visit(c)
            return
        if t == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                self.add_ref(node, "calls",
                             self._qualify(self.text(type_node).split("<", 1)[0]))
            for c in node.children:
                self.visit(c)
            return
        for c in node.children:
            self.visit(c)

    def _class(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node))
        self.scope.append((name, "class"))
        sup = node.child_by_field_name("superclass")
        if sup is not None:
            for c in sup.named_children:
                self.add_ref(sup, "inherits", self._qualify(self.text(c).split("<", 1)[0]))
        ifaces = node.child_by_field_name("interfaces")
        if ifaces is not None:
            for lst in ifaces.named_children:
                for c in lst.named_children:
                    self.add_ref(ifaces, "inherits",
                                 self._qualify(self.text(c).split("<", 1)[0]))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _method(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "method" if self.in_class() else "function", name,
                     signature=self.sig_of(node, body), doc=self._doc(node))
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _doc(self, node) -> str | None:
        prev = node.prev_sibling
        if prev is not None and prev.type == "block_comment":
            raw = self.text(prev)
            if raw.startswith("/**"):
                lines = [ln.strip().lstrip("*").strip() for ln in raw[3:-2].splitlines()]
                return "\n".join(ln for ln in lines if ln) or None
        return None

    def _import(self, node) -> None:
        # import a.b.C; / import static a.b.C.m;
        for c in node.named_children:
            if c.type == "scoped_identifier":
                dotted = self.text(c)
                self.aliases[dotted.rsplit(".", 1)[-1]] = dotted
                self.add_ref(node, "imports", dotted)

    def _invocation(self, node) -> None:
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        if name_node is None:
            return
        name = self.text(name_node)
        if obj is None:
            self.add_ref(node, "calls", self._qualify(name))
        elif obj.type == "identifier" and self.text(obj) in self.aliases:
            self.add_ref(node, "calls", f"{self.aliases[self.text(obj)]}.{name}")
        elif obj.type == "this":
            self.add_ref(node, "calls", name)
        else:
            self.add_ref(node, "calls", name)
