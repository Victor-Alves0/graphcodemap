"""Extractor L0 dedicado para Scala (tree-sitter).

Símbolos: trait (interface), object/class/case class, def (method/function),
val/var de topo. Refs: calls (no site do NOME), imports, inherits (extends_clause,
incluindo mixins `with` — indistinguíveis de superclasse estaticamente).
"""

from __future__ import annotations

from .base import BaseExtractor


class ScalaExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "import_declaration":
            self._import(node)
            return
        if t == "trait_definition":
            self._container(node, "interface")
            return
        if t in ("class_definition", "object_definition"):
            self._container(node, "class")
            return
        if t in ("function_definition", "function_declaration"):
            self._function(node)
            return
        if t in ("val_definition", "var_definition"):
            self._value(node, "constant" if t == "val_definition" else "variable")
            return
        if t == "call_expression":
            self._call(node)
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
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node))
        self.scope.append((name, "class"))
        ext = node.child_by_field_name("extend")
        if ext is not None:
            for c in ext.named_children:
                if c.type in ("type_identifier", "generic_type", "stable_type_identifier"):
                    self.add_ref(c, "inherits", self.text(c).split("[", 1)[0])
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _function(self, node) -> None:
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
            self.visit(body)
        self.scope.pop()

    def _value(self, node, kind: str) -> None:
        if self.scope and self.scope[-1][1] == "function":
            return  # variável local — não é símbolo de topo/membro relevante
        pat = node.child_by_field_name("pattern")
        if pat is not None and pat.type == "identifier":
            self.add_sym(pat, kind, self.text(pat), signature=None, doc=None)
        value = node.child_by_field_name("value")
        if value is not None:
            self.visit(value)

    def _doc(self, node) -> str | None:
        prev = node.prev_sibling
        if prev is not None and prev.type in ("comment", "block_comment"):
            raw = self.text(prev)
            if raw.startswith("/**"):
                lines = [ln.strip().lstrip("*").strip() for ln in raw[3:-2].splitlines()]
                return "\n".join(ln for ln in lines if ln).strip() or None
        return None

    # -- refs ----------------------------------------------------------------

    def _call(self, node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            self.add_ref(fn, "calls", self._qualify(self.text(fn)))
        elif fn.type == "field_expression":
            field = fn.child_by_field_name("field")
            if field is not None:
                self.add_ref(field, "calls", self.text(field))
        elif fn.type == "operator_identifier":
            return

    def _import(self, node) -> None:
        parts = []
        selectors = None
        for c in node.named_children:
            if c.type == "identifier":
                parts.append(self.text(c))
            elif c.type == "namespace_selectors":
                selectors = c
            elif c.type == "stable_identifier":
                parts.extend(self.text(x) for x in c.named_children
                             if x.type == "identifier")
        base = ".".join(parts)
        if selectors is not None:
            for s in selectors.named_children:
                if s.type == "identifier":
                    name = self.text(s)
                    full = f"{base}.{name}" if base else name
                    self.aliases[name] = full
                    self.add_ref(node, "imports", full)
        elif base:
            self.aliases[parts[-1]] = base
            self.add_ref(node, "imports", base)
