"""Extractor L0 para C# (tree-sitter)."""

from __future__ import annotations

from .base import BaseExtractor

_ACCESS = ("public", "private", "protected", "internal")


class CSharpExtractor(BaseExtractor):
    def _visibility(self, node, default: str = "private") -> str:
        """Acessibilidade a partir dos `modifier`. Default: membro é private,
        tipo top-level é internal (passado por quem chama)."""
        mods = {self.text(c) for c in node.children if c.type == "modifier"}
        for a in _ACCESS:
            if a in mods:
                return a
        return default

    def visit(self, node) -> None:
        t = node.type
        if t == "using_directive":
            self._using(node)
            return
        if t == "namespace_declaration":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is not None:
                self.scope.append((self.text(name_node), "module"))
            targets = body.children if body is not None else node.children
            for c in targets:
                self.visit(c)
            if name_node is not None:
                self.scope.pop()
            return
        if t == "file_scoped_namespace_declaration":
            # sem corpo: o namespace vale para o RESTO do arquivo (as declarações
            # são IRMÃS, não filhas). Empilha o escopo e não desempilha — o
            # extractor é por arquivo, então vazar até o fim é o correto.
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self.scope.append((self.text(name_node), "module"))
            return
        if t == "property_declaration":
            self._property(node)
            return
        if t == "field_declaration":
            self._field(node)
            return
        if t == "enum_member_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self.add_sym(node, "constant", self.text(name_node),
                             signature=None, doc=None, visibility="public")
            return
        if t in ("class_declaration", "record_declaration"):
            self._type(node, "class")
            return
        if t == "interface_declaration":
            self._type(node, "interface")
            return
        if t == "struct_declaration":
            self._type(node, "struct")
            return
        if t == "enum_declaration":
            self._type(node, "enum")
            return
        if t in ("method_declaration", "constructor_declaration",
                 "local_function_statement"):
            self._method(node)
            return
        if t == "invocation_expression":
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

    def _type(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node), visibility=self._visibility(node, "internal"))
        self.scope.append((name, "class"))
        base = next((c for c in node.named_children if c.type == "base_list"), None)
        if base is not None:
            for c in base.named_children:
                if c.type in ("identifier", "qualified_name", "generic_name"):
                    # nome SIMPLES: C# tem namespace no fqn (baseado em caminho +
                    # escopo), então o `using`-qualificado não alinharia; o nome
                    # nu resolve por nome+kind, como Java/Rust/Go
                    simple = self.text(c).split("<", 1)[0].rsplit(".", 1)[-1]
                    self.add_ref(base, "inherits", simple)
        # o corpo pode ser declaration_list (class/struct/iface) ou
        # enum_member_declaration_list (enum) — visitar ambos
        containers = [body] if body is not None else [
            c for c in node.named_children
            if c.type in ("declaration_list", "enum_member_declaration_list")]
        for cont in containers:
            if cont is not None:
                for c in cont.children:
                    self.visit(c)
        self.scope.pop()

    def _method(self, node) -> None:
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

    def _property(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        self.add_sym(node, "variable", self.text(name_node),
                     signature=None, doc=self._doc(node),
                     visibility=self._visibility(node))
        # o corpo (accessor com get/set custom, ou `=> expr`) pode conter chamadas
        for c in node.children:
            if c.type in ("accessor_list", "arrow_expression_clause"):
                self.visit(c)

    def _field(self, node) -> None:
        mods = {self.text(c) for c in node.children if c.type == "modifier"}
        kind = "constant" if "const" in mods else "variable"
        vis = self._visibility(node)
        vardecl = next((c for c in node.named_children
                        if c.type == "variable_declaration"), None)
        if vardecl is None:
            return
        for d in vardecl.named_children:
            if d.type != "variable_declarator":
                continue
            n = d.child_by_field_name("name") or next(
                (g for g in d.named_children if g.type == "identifier"), None)
            if n is not None:
                self.add_sym(d, kind, self.text(n), signature=None,
                             doc=None, visibility=vis)

    def _doc(self, node) -> str | None:
        lines: list[str] = []
        prev = node.prev_sibling
        while prev is not None and prev.type == "comment":
            txt = self.text(prev)
            if not txt.startswith("///"):
                break
            lines.append(txt.lstrip("/").strip())
            prev = prev.prev_sibling
        return "\n".join(reversed(lines)) or None

    def _using(self, node) -> None:
        for c in node.named_children:
            if c.type in ("identifier", "qualified_name"):
                dotted = self.text(c)
                self.aliases[dotted.rsplit(".", 1)[-1]] = dotted
                self.add_ref(node, "imports", dotted)

    def _invocation(self, node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type in ("identifier", "generic_name"):
            self.add_ref(node, "calls",
                         self._qualify(self.text(fn).split("<", 1)[0]))
        elif fn.type == "member_access_expression":
            name_node = fn.child_by_field_name("name")
            expr = fn.child_by_field_name("expression")
            if name_node is None:
                return
            name = self.text(name_node).split("<", 1)[0]
            if expr is not None and expr.type == "identifier" \
                    and self.text(expr) in self.aliases:
                self.add_ref(node, "calls", f"{self.aliases[self.text(expr)]}.{name}")
            else:
                self.add_ref(node, "calls", name)
