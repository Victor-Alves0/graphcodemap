"""Extractor L0 para Go (tree-sitter).

Símbolos: func, métodos com receiver (→ Type.Method), type (struct/interface/
alias) com métodos de interface, const/var de pacote.
Refs: calls (identifier, pkg.Fn via alias de import), imports, e embedding de
struct → inherits (ComplexProcessor{BaseProcessor} → BaseProcessor).
"""

from __future__ import annotations

from .base import BaseExtractor

_BUILTINS = {
    "make", "append", "len", "cap", "close", "panic", "print", "println",
    "new", "delete", "copy", "recover", "min", "max", "clear",
}


class GoExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "import_declaration":
            self._import(node)
            return
        if t == "function_declaration":
            self._function(node)
            return
        if t == "method_declaration":
            self._method(node)
            return
        if t == "type_declaration":
            self._types(node)
            return
        if t in ("const_declaration", "var_declaration") and not self.scope:
            self._vars(node, "constant" if t == "const_declaration" else "variable")
            return
        if t == "call_expression":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        for c in node.children:
            self.visit(c)

    # -- defs ----------------------------------------------------------------

    def _function(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "function", name, signature=self.sig_of(node, body),
                     doc=self._doc(node),
                     visibility="public" if name[:1].isupper() else "private")
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _method(self, node) -> None:
        name_node = node.child_by_field_name("name")
        recv = node.child_by_field_name("receiver")
        if name_node is None or recv is None:
            return
        recv_type = self._receiver_type(recv)
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        if recv_type:
            self.scope.append((recv_type, "class"))
        self.add_sym(node, "method", name, signature=self.sig_of(node, body),
                     doc=self._doc(node),
                     visibility="public" if name[:1].isupper() else "private")
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()
        if recv_type:
            self.scope.pop()

    def _receiver_type(self, recv) -> str | None:
        for c in recv.named_children:  # parameter_declaration
            type_node = c.child_by_field_name("type")
            if type_node is None:
                continue
            if type_node.type == "pointer_type" and type_node.named_children:
                type_node = type_node.named_children[0]
            if type_node.type in ("type_identifier", "generic_type"):
                return self.text(type_node).split("[", 1)[0]
        return None

    def _types(self, node) -> None:
        for spec in node.named_children:
            if spec.type != "type_spec":
                continue
            name_node = spec.child_by_field_name("name")
            type_node = spec.child_by_field_name("type")
            if name_node is None or type_node is None:
                continue
            name = self.text(name_node)
            if type_node.type == "struct_type":
                self.add_sym(spec, "struct", name,
                             signature=f"type {name} struct", doc=self._doc(node))
                self.scope.append((name, "class"))
                self._struct_embeds(type_node)
                self.scope.pop()
            elif type_node.type == "interface_type":
                self.add_sym(spec, "interface", name,
                             signature=f"type {name} interface", doc=self._doc(node))
                self.scope.append((name, "class"))
                for m in type_node.named_children:
                    if m.type in ("method_spec", "method_elem"):
                        mn = m.child_by_field_name("name")
                        if mn is not None:
                            self.add_sym(m, "method", self.text(mn),
                                         signature=self.text(m), doc=None)
                self.scope.pop()
            else:
                self.add_sym(spec, "type_alias", name,
                             signature=self.text(spec), doc=self._doc(node))

    def _struct_embeds(self, struct_type) -> None:
        body = next((c for c in struct_type.named_children
                     if c.type == "field_declaration_list"), None)
        if body is None:
            return
        for fd in body.named_children:
            if fd.type != "field_declaration":
                continue
            has_name = any(c.type == "field_identifier"
                           for c in fd.children if c.is_named)
            type_node = fd.child_by_field_name("type")
            if not has_name and type_node is not None and \
                    type_node.type in ("type_identifier", "qualified_type"):
                self.add_ref(fd, "inherits",
                             self._qualify(self.text(type_node).replace("/", ".")))

    def _vars(self, node, kind: str) -> None:
        for spec in node.named_children:
            if spec.type not in ("const_spec", "var_spec"):
                continue
            for c in spec.named_children:
                if c.type == "identifier":
                    name = self.text(c)
                    self.add_sym(spec, kind, name, signature=self.text(spec),
                                 doc=None,
                                 visibility="public" if name[:1].isupper() else "private")
            value = spec.child_by_field_name("value")
            if value is not None:
                self.visit(value)

    def _doc(self, node) -> str | None:
        lines: list[str] = []
        prev = node.prev_sibling
        while prev is not None and prev.type == "comment":
            txt = self.text(prev)
            if not txt.startswith("//") or txt.startswith("////"):
                break
            lines.append(txt[2:].strip())
            prev = prev.prev_sibling
        return "\n".join(reversed(lines)) or None

    # -- imports --------------------------------------------------------------

    def _import(self, node) -> None:
        specs = [c for c in node.named_children if c.type == "import_spec"]
        for lst in (c for c in node.named_children if c.type == "import_spec_list"):
            specs += [c for c in lst.named_children if c.type == "import_spec"]
        for spec in specs:
            path_node = spec.child_by_field_name("path")
            if path_node is None:
                continue
            path = self.text(path_node).strip("\"'")
            dotted = path.replace("/", ".").replace("-", "_")
            name_node = spec.child_by_field_name("name")
            local = self.text(name_node) if name_node is not None else dotted.rsplit(".", 1)[-1]
            if local not in ("_", "."):
                self.aliases[local] = dotted
            self.add_ref(spec, "imports", dotted)

    # -- calls ----------------------------------------------------------------

    def _call(self, node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            name = self.text(fn)
            if name in _BUILTINS and name not in self.aliases:
                return
            self.add_ref(node, "calls", self._qualify(name))
        elif fn.type == "selector_expression":
            operand = fn.child_by_field_name("operand")
            field = fn.child_by_field_name("field")
            if field is None:
                return
            if operand is not None and operand.type == "identifier" \
                    and self.text(operand) in self.aliases:
                self.add_ref(node, "calls",
                             f"{self.aliases[self.text(operand)]}.{self.text(field)}")
            else:
                self.add_ref(node, "calls", self.text(field))
