"""Extractor L0 para PHP (tree-sitter)."""

from __future__ import annotations

from .base import BaseExtractor


class PhpExtractor(BaseExtractor):
    @staticmethod
    def _simple(text: str) -> str:
        """Nome simples: strip de namespace (\\), genérico e caminho.
        `App\\Models\\Base` → `Base`."""
        return text.replace("\\", ".").split("<", 1)[0].strip().rsplit(".", 1)[-1]

    def _visibility(self, node, default: str = "public") -> str:
        v = next((c for c in node.children
                  if c.type == "visibility_modifier"), None)
        return self.text(v) if v is not None else default

    def visit(self, node) -> None:
        t = node.type
        if t == "namespace_use_declaration":
            self._use(node)
            return
        if t == "namespace_definition":
            self._namespace(node)
            return
        if t == "property_declaration" and self.in_class():
            self._property(node)
            return
        if t == "enum_case":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self.add_sym(node, "constant", self.text(name_node),
                             signature=None, doc=None, visibility="public")
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
        if t == "const_declaration" and (not self.scope or self.in_class()):
            # const de módulo OU de classe (o guard antigo `not self.scope`
            # descartava toda constante de classe). Dentro de função não há const.
            vis = self._visibility(node) if self.in_class() else None
            for c in node.named_children:
                if c.type == "const_element" and c.named_children:
                    n = c.child_by_field_name("name") or c.named_children[0]
                    self.add_sym(c, "constant", self.text(n),
                                 signature=self.text(c), doc=None, visibility=vis)
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
        if body is None:
            body = next((c for c in node.named_children
                         if c.type in ("declaration_list", "enum_declaration_list")),
                        None)
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node))
        self.scope.append((name, "class"))
        # herança/implements: NOME SIMPLES (o fqn PHP inclui namespace por
        # escopo, o qualificado do `use` não alinharia); resolve por nome+kind
        for c in node.named_children:
            if c.type in ("base_clause", "class_interface_clause"):
                for b in c.named_children:
                    if b.type in ("name", "qualified_name"):
                        self.add_ref(c, "inherits", self._simple(self.text(b)))
        if body is not None:
            for c in body.children:
                if c.type == "use_declaration":  # trait use
                    for b in c.named_children:
                        if b.type in ("name", "qualified_name"):
                            self.add_ref(c, "inherits", self._simple(self.text(b)))
                else:
                    self.visit(c)
        self.scope.pop()

    def _property(self, node) -> None:
        vis = self._visibility(node)
        for el in node.named_children:
            if el.type != "property_element":
                continue
            n = el.child_by_field_name("name") or next(
                (g for g in el.named_children if g.type == "variable_name"), None)
            if n is not None:
                # `$count` → `count`: o `$` é sintaxe da variável, não do símbolo
                self.add_sym(el, "variable", self.text(n).lstrip("$"),
                             signature=None, doc=None, visibility=vis)

    def _namespace(self, node) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        ns = self.text(name_node).replace("\\", ".") if name_node is not None else None
        if ns:
            self.scope.append((ns, "module"))
        if body is not None:
            for c in body.children:
                self.visit(c)
            if ns:
                self.scope.pop()
        # `namespace App;` sem corpo: file-scoped, vale até o fim do arquivo →
        # NÃO desempilha (o extractor é por arquivo). Igual ao C#.

    def _function(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        in_class = self.in_class()
        self.add_sym(node, "method" if in_class else "function", name,
                     signature=self.sig_of(node, body), doc=self._doc(node),
                     visibility=self._visibility(node) if in_class else None)
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
