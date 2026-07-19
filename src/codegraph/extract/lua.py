"""Extractor L0 dedicado para Lua/Luau (tree-sitter).

Símbolos: function_declaration em três formas — `function f`, `function M.f`
(método de tabela/módulo M) e `function obj:m` (método de instância). Refs:
calls (nome no site do NOME), imports (require).
"""

from __future__ import annotations

from .base import BaseExtractor


class LuaExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "function_declaration":
            self._function(node)
            return
        if t == "function_call":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        for c in node.children:
            self.visit(c)

    def _function(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        table, name = self._split_name(name_node)
        if name is None:
            return
        body = node.child_by_field_name("body")
        is_method = table is not None or bool(self.scope)
        if table:
            self.scope.append((table, "class"))
        self.add_sym(node, "method" if is_method else "function", name,
                     signature=self.sig_of(node, body), doc=None)
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()
        if table:
            self.scope.pop()

    def _split_name(self, name_node):
        """Retorna (tabela|None, nome). dot_index M.f e method_index obj:m."""
        t = name_node.type
        if t == "identifier":
            return None, self.text(name_node)
        if t == "dot_index_expression":
            tbl = name_node.child_by_field_name("table")
            fld = name_node.child_by_field_name("field")
            return (self.text(tbl) if tbl else None,
                    self.text(fld) if fld else None)
        if t == "method_index_expression":
            tbl = name_node.child_by_field_name("table")
            m = name_node.child_by_field_name("method")
            return (self.text(tbl) if tbl else None,
                    self.text(m) if m else None)
        return None, None

    def _call(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        t = name_node.type
        if t == "identifier":
            fn = self.text(name_node)
            if fn == "require":
                self._import(node)
                return
            self.add_ref(name_node, "calls", self._qualify(fn))
        elif t == "dot_index_expression":
            fld = name_node.child_by_field_name("field")
            if fld is not None:
                self.add_ref(fld, "calls", self.text(fld))
        elif t == "method_index_expression":
            m = name_node.child_by_field_name("method")
            if m is not None:
                self.add_ref(m, "calls", self.text(m))

    def _import(self, node) -> None:
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        for a in args.named_children:
            if a.type == "string":
                content = a.child_by_field_name("content")
                spec = self.text(content) if content is not None else self.text(a).strip("'\"[]")
                mod = spec.replace("/", ".").replace("\\", ".").strip(".")
                if mod:
                    self.aliases[mod.rsplit(".", 1)[-1]] = mod
                    self.add_ref(node, "imports", mod)
