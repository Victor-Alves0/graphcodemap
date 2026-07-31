"""Extractor L0 dedicado para Lua/Luau (tree-sitter).

Símbolos: function_declaration em três formas — `function f`, `function M.f`
(método de tabela/módulo M) e `function obj:m` (método de instância). Refs:
calls (nome no site do NOME), imports (require).
"""

from __future__ import annotations

from .base import BaseExtractor


def _first(node, *types):
    for c in node.named_children:
        if c.type in types:
            return c
    return None


class LuaExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "function_declaration":
            self._function(node)
            return
        if t == "variable_declaration":
            # `local x = …` — declaração LOCAL (privada ao arquivo)
            self._assignment(node, is_local=True)
            return
        if t == "assignment_statement":
            # atribuição GLOBAL (pública) ou a campo (`M.x = …`). A versão
            # embrulhada em variable_declaration é tratada acima (não recursa).
            self._assignment(node, is_local=False)
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
        # visibilidade Lua: `local function` é privado ao arquivo; global é público.
        # Método de tabela (function M.f) é acessível → público.
        is_local = self.text(node).lstrip().startswith("local")
        vis = "private" if (is_local and not is_method) else "public"
        if table:
            self.scope.append((table, "class"))
        self.add_sym(node, "method" if is_method else "function", name,
                     signature=self.sig_of(node, body), doc=None, visibility=vis)
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()
        if table:
            self.scope.pop()

    # -- atribuições: variáveis de módulo, tabelas-módulo, campos -------------

    def _assignment(self, node, is_local: bool) -> None:
        assign = node if node.type == "assignment_statement" else \
            _first(node, "assignment_statement")
        if assign is None:                       # `local x` sem valor
            vl = _first(node, "variable_list")
            if vl is not None and not self.scope:
                for tgt in vl.named_children:
                    self._declare(tgt, None, "private")
            return
        var_list = _first(assign, "variable_list")
        expr_list = _first(assign, "expression_list")
        targets = list(var_list.named_children) if var_list is not None else []
        values = list(expr_list.named_children) if expr_list is not None else []
        vis = "private" if is_local else "public"
        for i, tgt in enumerate(targets):
            val = values[i] if i < len(values) else None
            if not self.scope:                   # símbolo só no nível de módulo
                self._declare(tgt, val, vis)
            elif val is not None:
                self.visit(val)                  # dentro de função: só os calls
        for j in range(len(targets), len(values)):
            self.visit(values[j])

    def _declare(self, tgt, val, vis: str) -> None:
        if tgt.type == "identifier":
            name = self.text(tgt)
            if val is not None and val.type == "function_definition":
                self._func_value(tgt, name, val, "function", vis)
            elif val is not None and val.type == "table_constructor":
                self.add_sym(tgt, "variable", name, signature=None,
                             doc=None, visibility=vis)   # tabela-módulo
                self._table_fields(val, name)
            else:
                kind = "constant" if name.isupper() else "variable"
                self.add_sym(tgt, kind, name, signature=None, doc=None,
                             visibility=vis)
                if val is not None:
                    self.visit(val)
        elif tgt.type == "dot_index_expression":
            tbl = tgt.child_by_field_name("table")
            fld = tgt.child_by_field_name("field")
            if fld is None:
                return
            tname = self.text(tbl) if tbl is not None else None
            if tname:
                self.scope.append((tname, "class"))
            if val is not None and val.type == "function_definition":
                self._func_value(fld, self.text(fld), val, "method", "public")
            else:
                self.add_sym(fld, "variable", self.text(fld), signature=None,
                             doc=None, visibility="public")
                if val is not None:
                    self.visit(val)
            if tname:
                self.scope.pop()

    def _func_value(self, anchor, name, val, kind, vis) -> None:
        """Função atribuída a um nome/campo (`f = function() … end`)."""
        body = val.child_by_field_name("body") or _first(val, "block")
        self.add_sym(anchor, kind, name, signature=self.sig_of(val, body),
                     doc=None, visibility=vis)
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _table_fields(self, table, owner: str) -> None:
        self.scope.append((owner, "class"))
        for f in table.named_children:
            if f.type != "field":
                continue
            key = f.child_by_field_name("name")
            val = f.child_by_field_name("value")
            if key is None or key.type != "identifier":
                if val is not None:              # [expr]=v / posicional: só calls
                    self.visit(val)
                continue
            fname = self.text(key)
            if val is not None and val.type == "function_definition":
                self._func_value(key, fname, val, "method", "public")
            else:
                self.add_sym(key, "variable", fname, signature=None, doc=None,
                             visibility="public")
                if val is not None:
                    self.visit(val)
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
