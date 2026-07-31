"""Extractor L0 para Kotlin (tree-sitter).

A gramática Kotlin não expõe field names: navegação por tipo de nó.
`interface`/`enum class` também são class_declaration — distinguidos pelo
token de palavra-chave e pelo tipo do body.
"""

from __future__ import annotations

from .base import BaseExtractor


def _first(node, *types):
    for c in node.named_children:
        if c.type in types:
            return c
    return None


_ACCESS = ("public", "private", "protected", "internal")


class KotlinExtractor(BaseExtractor):
    def _visibility(self, node, default: str = "public") -> str:
        mods = _first(node, "modifiers")
        if mods is not None:
            toks = self.text(mods).split()
            for a in _ACCESS:
                if a in toks:
                    return a
        return default

    def visit(self, node) -> None:
        t = node.type
        if t == "import_header":
            self._import(node)
            return
        if t in ("class_declaration", "object_declaration"):
            self._class(node)
            return
        if t == "function_declaration":
            self._function(node)
            return
        if t == "property_declaration" and (not self.scope or self.in_class()):
            # o guard antigo `not self.scope` descartava TODA propriedade de
            # classe. Dentro de função (val local) o escopo é function → não cai.
            self._property(node)
            for c in node.children:
                self.visit(c)
            return
        if t == "enum_entry":
            nm = _first(node, "simple_identifier")
            if nm is not None:
                self.add_sym(node, "constant", self.text(nm),
                             signature=None, doc=None, visibility="public")
            return
        if t == "call_expression":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        for c in node.children:
            self.visit(c)

    def _kind_of(self, node) -> str:
        tokens = {c.type for c in node.children}
        if "interface" in tokens:
            return "interface"
        if _first(node, "enum_class_body") is not None or "enum" in tokens:
            return "enum"
        return "class"

    def _class(self, node) -> None:
        name_node = _first(node, "type_identifier", "simple_identifier")
        if name_node is None:
            return
        name = self.text(name_node)
        body = _first(node, "class_body", "enum_class_body")
        self.add_sym(node, self._kind_of(node), name,
                     signature=self.sig_of(node, body), doc=self._doc(node),
                     visibility=self._visibility(node))
        self.scope.append((name, "class"))
        # propriedades do construtor primário: só as class_parameter COM val/var
        # (binding_pattern_kind) declaram propriedade; as sem são só argumentos
        pc = _first(node, "primary_constructor")
        if pc is not None:
            for cp in pc.named_children:
                if cp.type == "class_parameter" and \
                        _first(cp, "binding_pattern_kind") is not None:
                    nm = _first(cp, "simple_identifier")
                    if nm is not None:
                        pname = self.text(nm)
                        self.add_sym(cp, "constant" if pname.isupper() else "variable",
                                     pname, signature=None, doc=None,
                                     visibility=self._visibility(cp))
        for spec in (c for c in node.named_children if c.type == "delegation_specifier"):
            base = spec
            inv = _first(spec, "constructor_invocation")
            if inv is not None:
                base = inv
            ut = _first(base, "user_type")
            target = ut if ut is not None else _first(base, "type_identifier")
            if target is not None:
                # NOME SIMPLES (o fqn Kotlin é por caminho; o qualificado do
                # import não alinharia): resolve por nome+kind
                simple = self.text(target).split("<", 1)[0].rsplit(".", 1)[-1]
                self.add_ref(spec, "inherits", simple)
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _function(self, node) -> None:
        name_node = _first(node, "simple_identifier")
        if name_node is None:
            return
        name = self.text(name_node)
        body = _first(node, "function_body")
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
        var = _first(node, "variable_declaration")
        if var is None:
            return
        name_node = _first(var, "simple_identifier")
        if name_node is None:
            return
        name = self.text(name_node)
        is_const = any(self.text(c) == "const" for c in node.children
                       if c.type == "modifiers")
        self.add_sym(node, "constant" if is_const or name.isupper() else "variable",
                     name, signature=None, doc=None,
                     visibility=self._visibility(node) if self.in_class() else None)

    def _doc(self, node) -> str | None:
        prev = node.prev_sibling
        if prev is not None and prev.type in ("multiline_comment", "block_comment"):
            raw = self.text(prev)
            if raw.startswith("/**"):
                lines = [ln.strip().lstrip("*").strip() for ln in raw[3:-2].splitlines()]
                return "\n".join(ln for ln in lines if ln) or None
        return None

    def _import(self, node) -> None:
        ident = _first(node, "identifier")
        if ident is None:
            return
        parts = [self.text(c) for c in ident.named_children
                 if c.type == "simple_identifier"]
        if not parts:
            return
        dotted = ".".join(parts)
        wildcard = "*" in self.text(node)
        if not wildcard:
            self.aliases[parts[-1]] = dotted
        self.add_ref(node, "imports", dotted + (".*" if wildcard else ""))

    def _call(self, node) -> None:
        head = node.named_children[0] if node.named_children else None
        if head is None:
            return
        if head.type == "simple_identifier":
            self.add_ref(node, "calls", self._qualify(self.text(head)))
        elif head.type == "navigation_expression":
            dotted = self.text(head)
            if any(ch in dotted for ch in "\n(["):
                parts = dotted.rsplit(".", 1)
                self.add_ref(node, "calls", parts[-1] if len(parts) > 1 else dotted)
                return
            base, _, rest = dotted.partition(".")
            if base == "this":
                self.add_ref(node, "calls", rest.rsplit(".", 1)[-1] if rest else dotted)
            elif base in self.aliases and rest:
                self.add_ref(node, "calls", f"{self.aliases[base]}.{rest}")
            else:
                self.add_ref(node, "calls", dotted.rsplit(".", 1)[-1])
