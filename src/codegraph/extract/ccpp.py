"""Extractor L0 para C e C++ (tree-sitter).

Cobre: funções (incl. definição fora da classe via Qualified::name), structs/
classes/enums (typedef incluso), macros (#define), namespaces, herança C++,
métodos inline e declarados em class body. Protótipos top-level são ignorados
(a definição é o símbolo).
"""

from __future__ import annotations

from .base import BaseExtractor


class CCppExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "preproc_include":
            path = node.child_by_field_name("path")
            if path is not None:
                spec = self.text(path).strip('<>"')
                self.add_ref(node, "imports", spec.replace("/", "."))
            return
        if t == "preproc_def":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self.add_sym(node, "constant", self.text(name_node),
                             signature=self.text(node).strip(), doc=None)
            return
        if t == "preproc_function_def":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self.add_sym(node, "function", self.text(name_node),
                             signature=self.text(node).splitlines()[0], doc=None)
            return
        if t == "function_definition":
            self._function(node)
            return
        if t == "field_declaration" and self.in_class():
            # declaração de método no class body (definição pode estar fora)
            decl = self._find_function_declarator(node)
            if decl is not None:
                self._declared_method(node, decl)
                return
            for c in node.children:
                self.visit(c)
            return
        if t in ("class_specifier", "struct_specifier"):
            self._class(node, "class" if t == "class_specifier" else "struct")
            return
        if t == "enum_specifier":
            name_node = node.child_by_field_name("name")
            if name_node is not None and node.child_by_field_name("body") is not None:
                self.add_sym(node, "enum", self.text(name_node),
                             signature=f"enum {self.text(name_node)}", doc=None)
            return
        if t == "type_definition":
            for c in node.named_children:
                self.visit(c)
            return
        if t == "namespace_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is not None:
                self.scope.append((self.text(name_node), "module"))
            if body is not None:
                for c in body.children:
                    self.visit(c)
            if name_node is not None:
                self.scope.pop()
            return
        if t == "template_declaration":
            for c in node.named_children:
                self.visit(c)
            return
        if t == "call_expression":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        for c in node.children:
            self.visit(c)

    # -- helpers -------------------------------------------------------------

    def _find_function_declarator(self, node):
        d = node.child_by_field_name("declarator")
        while d is not None and d.type in ("pointer_declarator", "reference_declarator",
                                           "parenthesized_declarator"):
            d = d.child_by_field_name("declarator") or next(
                iter(d.named_children), None)
        return d if d is not None and d.type == "function_declarator" else None

    def _declarator_name(self, fn_decl):
        d = fn_decl.child_by_field_name("declarator")
        if d is None:
            return None, None
        if d.type == "qualified_identifier":
            scope = d.child_by_field_name("scope")
            name = d.child_by_field_name("name")
            return (self.text(scope) if scope is not None else None,
                    self.text(name) if name is not None else None)
        if d.type in ("identifier", "field_identifier", "destructor_name",
                      "operator_name", "type_identifier"):
            return None, self.text(d)
        return None, None

    # -- defs ----------------------------------------------------------------

    def _function(self, node) -> None:
        fn_decl = self._find_function_declarator(node)
        if fn_decl is None:
            return
        qual, name = self._declarator_name(fn_decl)
        if not name:
            return
        body = node.child_by_field_name("body")
        pushed = 0
        if qual:  # definição fora da classe: Type::method
            for part in qual.split("::"):
                if part:
                    self.scope.append((part, "class"))
                    pushed += 1
        kind = "method" if self.in_class() else "function"
        self.add_sym(node, kind, name, signature=self.sig_of(node, body), doc=None)
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()
        for _ in range(pushed):
            self.scope.pop()

    def _declared_method(self, node, fn_decl) -> None:
        _, name = self._declarator_name(fn_decl)
        if name:
            self.add_sym(node, "method", name,
                         signature=self.text(node).split("{", 1)[0].strip().rstrip(";"),
                         doc=None)

    def _class(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            return  # forward declaration / uso como tipo
        name = self.text(name_node)
        self.add_sym(node, kind, name, signature=f"{node.type.split('_')[0]} {name}",
                     doc=None)
        self.scope.append((name, "class"))
        base = next((c for c in node.named_children
                     if c.type == "base_class_clause"), None)
        if base is not None:
            for c in base.named_children:
                if c.type in ("type_identifier", "qualified_identifier", "template_type"):
                    self.add_ref(base, "inherits",
                                 self.text(c).split("<", 1)[0].replace("::", "."))
        for c in body.children:
            self.visit(c)
        self.scope.pop()

    # -- calls ----------------------------------------------------------------

    def _call(self, node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            self.add_ref(node, "calls", self.text(fn))
        elif fn.type == "qualified_identifier":
            self.add_ref(node, "calls", self.text(fn).replace("::", "."))
        elif fn.type == "field_expression":
            f = fn.child_by_field_name("field")
            if f is not None:
                self.add_ref(node, "calls", self.text(f))
        elif fn.type == "template_function":
            name = fn.child_by_field_name("name")
            if name is not None:
                self.add_ref(node, "calls", self.text(name).replace("::", "."))
