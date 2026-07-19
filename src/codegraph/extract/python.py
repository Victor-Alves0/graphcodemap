"""Extractor L0 para Python (tree-sitter).

Símbolos: funções, métodos, classes, atribuições de módulo (constant/variable).
Refs: calls (com rastreio de import → dst_name qualificado), imports, inherits.
"""

from __future__ import annotations

from .base import BaseExtractor

# builtins: ruído puro como aresta de call (nunca são símbolos do repo);
# só ignorados quando não há import/definição local com o mesmo nome
_BUILTINS = {
    "print", "len", "range", "int", "str", "float", "bool", "bytes", "list",
    "dict", "set", "tuple", "frozenset", "isinstance", "issubclass", "super",
    "hash", "abs", "min", "max", "sum", "sorted", "reversed", "iter", "next",
    "enumerate", "zip", "map", "filter", "getattr", "setattr", "hasattr",
    "repr", "type", "id", "open", "vars", "format", "round", "any", "all",
    "callable", "exec", "eval", "input", "divmod", "ord", "chr",
}


class PythonExtractor(BaseExtractor):
    def visit(self, node) -> None:
        t = node.type
        if t == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None:
                self.visit(inner)
            return
        if t == "function_definition":
            self._function(node)
            return
        if t == "class_definition":
            self._class(node)
            return
        if t == "import_statement":
            self._import(node)
            return
        if t == "import_from_statement":
            self._import_from(node)
            return
        if t == "call":
            self._call(node)
            # argumentos podem conter outras calls/lambdas
            for c in node.children:
                self.visit(c)
            return
        if t == "expression_statement" and not self.scope:
            self._module_assignment(node)
        for c in node.children:
            self.visit(c)

    # -- defs ----------------------------------------------------------------

    def _function(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        kind = "method" if self.in_class() else "function"
        self.add_sym(
            node, kind, name,
            signature=self.sig_of(node, body),
            doc=self._docstring(body),
            visibility="private" if name.startswith("_") else "public",
        )
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _class(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(
            node, "class", name,
            signature=self.sig_of(node, body),
            doc=self._docstring(body),
            visibility="private" if name.startswith("_") else "public",
        )
        supers = node.child_by_field_name("superclasses")
        self.scope.append((name, "class"))
        if supers is not None:
            for c in supers.named_children:
                if c.type in ("identifier", "attribute"):
                    self.add_ref(c, "inherits", self._qualify(self.text(c)))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _module_assignment(self, node) -> None:
        for child in node.named_children:
            if child.type != "assignment":
                continue
            left = child.child_by_field_name("left")
            if left is None or left.type != "identifier":
                continue
            name = self.text(left)
            kind = "constant" if name.isupper() else "variable"
            self.add_sym(
                child, kind, name,
                signature=None, doc=None,
                visibility="private" if name.startswith("_") else "public",
            )

    def _docstring(self, body) -> str | None:
        if body is None or not body.named_children:
            return None
        s = body.named_children[0]
        if s.type == "expression_statement" and s.named_children:
            s = s.named_children[0]
        if s.type != "string":
            return None
        raw = self.text(s)
        for q in ('"""', "'''", '"', "'"):
            if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                return raw[len(q) : -len(q)].strip()
        return raw.strip()

    # -- imports -------------------------------------------------------------

    def _import(self, node) -> None:
        # import a.b.c [as d]
        for c in node.named_children:
            if c.type == "dotted_name":
                dotted = self.text(c)
                self.aliases[dotted.split(".", 1)[0]] = dotted.split(".", 1)[0]
                self.aliases[dotted] = dotted
                self.add_ref(node, "imports", dotted)
            elif c.type == "aliased_import":
                target = c.child_by_field_name("name")
                alias = c.child_by_field_name("alias")
                if target is not None and alias is not None:
                    dotted = self.text(target)
                    self.aliases[self.text(alias)] = dotted
                    self.add_ref(node, "imports", dotted)

    def _import_from(self, node) -> None:
        # from <module> import x [as y], ...
        module_node = node.child_by_field_name("module_name")
        if module_node is None:
            return
        module = self._resolve_module(module_node)
        for c in node.named_children:
            if c == module_node:
                continue
            if c.type == "dotted_name":
                name = self.text(c)
                self.aliases[name] = f"{module}.{name}" if module else name
                self.add_ref(node, "imports", self.aliases[name])
            elif c.type == "aliased_import":
                target = c.child_by_field_name("name")
                alias = c.child_by_field_name("alias")
                if target is not None and alias is not None:
                    name = self.text(target)
                    guess = f"{module}.{name}" if module else name
                    self.aliases[self.text(alias)] = guess
                    self.add_ref(node, "imports", guess)
            elif c.type == "wildcard_import":
                self.add_ref(node, "imports", f"{module}.*" if module else "*")

    def _resolve_module(self, module_node) -> str:
        if module_node.type == "dotted_name":
            return self.text(module_node)
        if module_node.type == "relative_import":
            raw = self.text(module_node)
            dots = len(raw) - len(raw.lstrip("."))
            rest = raw.lstrip(".")
            parts = self.module_fqn.split(".")
            base = parts[: max(len(parts) - dots, 0)]
            return ".".join([*base, rest] if rest else base)
        return self.text(module_node)

    # -- calls ---------------------------------------------------------------

    def _call(self, node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            name = self.text(fn)
            if name in _BUILTINS and name not in self.aliases:
                return
            # ref na posição do NOME (resolvers L1 resolvem por linha+coluna)
            self.add_ref(fn, "calls", self._qualify(name))
        elif fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            site = attr if attr is not None else fn
            dotted = self.text(fn)
            if "\n" in dotted or "(" in dotted or "[" in dotted:
                # receptor é expressão (ex.: foo().bar()): só o nome do atributo
                if attr is not None:
                    self.add_ref(attr, "calls", self.text(attr))
                return
            base, _, rest = dotted.partition(".")
            if base in ("self", "cls"):
                self.add_ref(site, "calls", rest.rsplit(".", 1)[-1] if rest else dotted)
            elif base in self.aliases:
                self.add_ref(site, "calls", f"{self.aliases[base]}.{rest}" if rest else self.aliases[base])
            else:
                # receptor desconhecido: só o nome do método (resolução vira 'possible')
                self.add_ref(site, "calls", dotted.rsplit(".", 1)[-1])
