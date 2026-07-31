"""Extractor L0 para Rust (tree-sitter).

Símbolos: fn (livres e em impl/trait), struct, enum, trait (→ interface),
const/static, type alias, mod.
Refs: calls (identifier, Path::to::fn, method calls), imports (use), e
`impl Trait for Type` → inherits (Type → Trait).
"""

from __future__ import annotations

from .base import BaseExtractor


def _simple_trait(text: str) -> str:
    """Nome simples de um trait: strip de genérico e de caminho.
    `From<i32>` → `From`; `std::fmt::Display` → `Display`."""
    return text.split("<", 1)[0].replace("::", ".").strip().rsplit(".", 1)[-1]


class RustExtractor(BaseExtractor):
    def _visibility(self, node) -> str:
        """`pub` → public; `pub(crate)`/`pub(super)`/`pub(in …)` → crate
        (restrito, mas não privado); sem modificador → private (default Rust)."""
        v = next((c for c in node.children
                  if c.type == "visibility_modifier"), None)
        if v is None:
            return "private"
        return "public" if v.text.decode("utf-8", "replace") == "pub" else "crate"

    def visit(self, node) -> None:
        t = node.type
        if t in ("function_item", "function_signature_item"):
            self._function(node)
            return
        if t == "struct_item":
            self._struct(node)
            return
        if t == "enum_item":
            self._enum(node)
            return
        if t == "trait_item":
            self._trait(node)
            return
        if t == "impl_item":
            self._impl(node)
            return
        if t == "mod_item":
            self._mod(node)
            return
        if t in ("const_item", "static_item"):
            self._named(node, "constant")
            return
        if t == "type_item":
            self._named(node, "type_alias")
            return
        if t == "use_declaration":
            for c in node.named_children:
                self._use_walk(c, "")
            return
        if t == "call_expression":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        if t == "macro_invocation":
            for c in node.children:
                if c.type == "token_tree":
                    self._macro_calls(c)
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
        kind = "method" if self.in_class() else "function"
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node), visibility=self._visibility(node))
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _named(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        self.add_sym(node, kind, self.text(name_node),
                     signature=self.sig_of(node, node.child_by_field_name("body")),
                     doc=self._doc(node), visibility=self._visibility(node))

    def _struct(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "struct", name, signature=self.sig_of(node, body),
                     doc=self._doc(node), visibility=self._visibility(node))
        # campos NOMEADOS viram símbolos (API do tipo). Tuple struct
        # (`Wrapper(i32)`) e unit (`Marker;`) não têm nomes → nada a capturar.
        flist = next((c for c in node.named_children
                      if c.type == "field_declaration_list"), None)
        if flist is None:
            return
        self.scope.append((name, "class"))
        for f in flist.named_children:
            if f.type != "field_declaration":
                continue
            fn = f.child_by_field_name("name")
            if fn is not None:
                self.add_sym(f, "variable", self.text(fn), signature=None,
                             doc=self._doc(f), visibility=self._visibility(f))
        self.scope.pop()

    def _enum(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "enum", name, signature=self.sig_of(node, body),
                     doc=self._doc(node), visibility=self._visibility(node))
        vlist = next((c for c in node.named_children
                      if c.type == "enum_variant_list"), None)
        if vlist is None:
            return
        self.scope.append((name, "class"))
        for v in vlist.named_children:
            if v.type != "enum_variant":
                continue
            vn = v.child_by_field_name("name")
            if vn is not None:
                self.add_sym(v, "constant", self.text(vn), signature=None,
                             doc=self._doc(v), visibility="public")
        self.scope.pop()

    def _trait(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        self.add_sym(node, "interface", name,
                     signature=self.sig_of(node, node.child_by_field_name("body")),
                     doc=self._doc(node), visibility=self._visibility(node))
        self.scope.append((name, "class"))
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _impl(self, node) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is None:
            return
        type_name = self.text(type_node).split("<", 1)[0].strip()
        trait_node = node.child_by_field_name("trait")
        self.scope.append((type_name, "class"))
        if trait_node is not None:
            # NOME SIMPLES do trait, não o caminho qualificado: o fqn de símbolo é
            # baseado em caminho de arquivo (`a.Draw`), então o `use crate::a::Draw`
            # qualificado (`crate.a.Draw`) nunca casaria — o nome nu resolve por
            # nome+kind, como as chamadas e a herança do Java.
            self.add_ref(node, "inherits", _simple_trait(self.text(trait_node)))
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _mod(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        self.add_sym(node, "module", name, signature=None, doc=self._doc(node))
        self.scope.append((name, "module"))
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _doc(self, node) -> str | None:
        lines: list[str] = []
        prev = node.prev_sibling
        while prev is not None and prev.type == "attribute_item":
            prev = prev.prev_sibling
        while prev is not None and prev.type == "line_comment":
            txt = self.text(prev)
            if not txt.startswith("///"):
                break
            lines.append(txt[3:].strip())
            prev = prev.prev_sibling
        return "\n".join(reversed(lines)) or None

    # -- use (imports) --------------------------------------------------------

    def _use_walk(self, n, prefix: str) -> None:
        t = n.type
        join = (prefix + ".") if prefix else ""
        if t == "identifier":
            full = join + self.text(n)
            self.aliases[self.text(n)] = full
            self.add_ref(n, "imports", full)
        elif t == "scoped_identifier":
            full = join + self.text(n).replace("::", ".")
            self.aliases[full.rsplit(".", 1)[-1]] = full
            self.add_ref(n, "imports", full)
        elif t == "use_as_clause":
            path = n.child_by_field_name("path")
            alias = n.child_by_field_name("alias")
            if path is not None and alias is not None:
                full = join + self.text(path).replace("::", ".")
                self.aliases[self.text(alias)] = full
                self.add_ref(n, "imports", full)
        elif t == "scoped_use_list":
            path = n.child_by_field_name("path")
            lst = n.child_by_field_name("list")
            new_prefix = join + self.text(path).replace("::", ".") if path is not None else prefix
            if lst is not None:
                for c in lst.named_children:
                    self._use_walk(c, new_prefix)
        elif t == "use_list":
            for c in n.named_children:
                self._use_walk(c, prefix)
        elif t == "use_wildcard":
            # `use std::prelude::*` traz o caminho DENTRO do wildcard; já
            # `use a::{b::*}` traz o caminho no prefixo herdado (join). Cobrir os
            # dois: caminho interno se houver, senão o prefixo acumulado.
            inner = next((c for c in n.named_children
                          if c.type in ("scoped_identifier", "identifier")), None)
            if inner is not None:
                path = join + self.text(inner).replace("::", ".")
            else:
                path = join
            self.add_ref(n, "imports", (path + ".*") if path else "*")

    def _macro_calls(self, token_tree) -> None:
        """Macros (println!, format!…) não são parseadas como expressões pelo
        tree-sitter; heurística: identifier [:: identifier]* seguido de (…)
        dentro do token_tree vira aresta de call."""
        kids = list(token_tree.children)
        for idx, n in enumerate(kids):
            if n.type == "token_tree":
                self._macro_calls(n)
            elif n.type == "identifier":
                nxt = kids[idx + 1] if idx + 1 < len(kids) else None
                if nxt is None or nxt.type != "token_tree" or not self.text(nxt).startswith("("):
                    continue
                parts = [self.text(n)]
                k = idx - 1
                while k - 1 >= 0 and kids[k].type == "::" and kids[k - 1].type == "identifier":
                    parts.insert(0, self.text(kids[k - 1]))
                    k -= 2
                self.add_ref(n, "calls", self._qualify(".".join(parts)))

    # -- calls ----------------------------------------------------------------

    def _call(self, node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "generic_function":
            fn = fn.child_by_field_name("function") or fn
        if fn.type == "identifier":
            self.add_ref(node, "calls", self._qualify(self.text(fn)))
        elif fn.type == "scoped_identifier":
            self.add_ref(node, "calls", self._qualify(self.text(fn).replace("::", ".")))
        elif fn.type == "field_expression":
            f = fn.child_by_field_name("field")
            if f is not None:
                self.add_ref(node, "calls", self.text(f))
