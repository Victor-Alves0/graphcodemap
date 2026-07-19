"""Extractor L0 para PHP (tree-sitter)."""

from __future__ import annotations

from .base import BaseExtractor


class PhpExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "namespace_use_declaration":
            self._use(node)
            return
        if t == "class_declaration":
            self._class(node, "class")
            return
        if t == "interface_declaration":
            self._class(node, "interface")
            return
        if t == "trait_declaration":
            self._class(node, "class")
            return
        if t == "enum_declaration":
            self._class(node, "enum")
            return
        if t in ("function_definition", "method_declaration"):
            self._function(node)
            return
        if t == "const_declaration" and not self.scope:
            for c in node.named_children:
                if c.type == "const_element" and c.named_children:
                    self.add_sym(c, "constant", self.text(c.named_children[0]),
                                 signature=self.text(c), doc=None)
            return
        if t == "function_call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type in ("name", "qualified_name"):
                self.add_ref(node, "calls",
                             self._qualify(self.text(fn).replace("\\", ".").lstrip(".")))
            for c in node.children:
                self.visit(c)
            return
        if t in ("member_call_expression", "scoped_call_expression",
                 "nullsafe_member_call_expression"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self.add_ref(node, "calls", self.text(name_node))
            for c in node.children:
                self.visit(c)
            return
        if t == "object_creation_expression":
            target = next((c for c in node.named_children
                           if c.type in ("name", "qualified_name")), None)
            if target is not None:
                self.add_ref(node, "calls",
                             self._qualify(self.text(target).replace("\\", ".").lstrip(".")))
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
        for c in node.named_children:
            if c.type == "base_clause" or c.type == "class_interface_clause":
                for b in c.named_children:
                    if b.type in ("name", "qualified_name"):
                        self.add_ref(c, "inherits",
                                     self._qualify(self.text(b).replace("\\", ".").lstrip(".")))
        if body is not None:
            for c in body.children:
                if c.type == "use_declaration":  # trait use
                    for b in c.named_children:
                        if b.type in ("name", "qualified_name"):
                            self.add_ref(c, "inherits",
                                         self._qualify(self.text(b).replace("\\", ".")))
                else:
                    self.visit(c)
        self.scope.pop()

    def _function(self, node) -> None:
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
        if prev is not None and prev.type == "comment":
            raw = self.text(prev)
            if raw.startswith("/**"):
                lines = [ln.strip().lstrip("*").strip() for ln in raw[3:-2].splitlines()]
                return "\n".join(ln for ln in lines if ln) or None
        return None

    def _use(self, node) -> None:
        for c in node.named_children:
            if c.type == "namespace_use_clause":
                target = next((n for n in c.named_children
                               if n.type == "qualified_name"), None)
                if target is None:
                    target = next((n for n in c.named_children if n.type == "name"), None)
                if target is None:
                    continue
                dotted = self.text(target).replace("\\", ".").lstrip(".")
                alias_node = next((n for n in c.named_children
                                   if n.type == "namespace_aliasing_clause"), None)
                local = (self.text(alias_node.named_children[0])
                         if alias_node is not None and alias_node.named_children
                         else dotted.rsplit(".", 1)[-1])
                self.aliases[local] = dotted
                self.add_ref(node, "imports", dotted)
