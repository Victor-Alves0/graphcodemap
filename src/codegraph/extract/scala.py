"""Extractor L0 dedicado para Scala (tree-sitter).

Símbolos: trait (interface), object/class/case class, def (method/function),
val/var de topo. Refs: calls (no site do NOME), imports, inherits (extends_clause,
incluindo mixins `with` — indistinguíveis de superclasse estaticamente).
"""

from __future__ import annotations

from .base import BaseExtractor

_ACCESS = ("private", "protected")     # default Scala é public


class ScalaExtractor(BaseExtractor):
    def _visibility(self, node, default: str = "public") -> str:
        mods = next((c for c in node.children if c.type == "modifiers"), None)
        if mods is not None:
            toks = self.text(mods).split()
            for a in _ACCESS:
                if a in toks:
                    return a
        return default

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
        if t == "enum_definition":
            self._enum(node)
            return
        if t in ("simple_enum_case", "full_enum_case"):
            nm = node.child_by_field_name("name")
            if nm is not None:
                self.add_sym(node, "constant", self.text(nm), signature=None,
                             doc=None, visibility="public")
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
                     doc=self._doc(node), visibility=self._visibility(node))
        self.scope.append((name, "class"))
        self._ctor_params(node)
        ext = node.child_by_field_name("extend")
        if ext is not None:
            for c in ext.named_children:
                if c.type in ("type_identifier", "generic_type", "stable_type_identifier"):
                    # NOME SIMPLES (stable_type_identifier `pkg.Base` → `Base`);
                    # resolve por nome+kind como as demais
                    simple = self.text(c).split("[", 1)[0].strip().rsplit(".", 1)[-1]
                    self.add_ref(c, "inherits", simple)
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _ctor_params(self, node) -> None:
        """Parâmetros do construtor primário que declaram MEMBRO: case class
        (todos os params viram val público) ou `val`/`var` explícito. Parâmetro
        simples (`class C(x: Int)`) é só argumento, não membro."""
        is_case = any(c.type == "case" for c in node.children)
        for cpl in node.named_children:
            if cpl.type != "class_parameters":
                continue
            for cp in cpl.named_children:
                if cp.type != "class_parameter":
                    continue
                binding = next((c.type for c in cp.children
                                if c.type in ("val", "var")), None)
                if not (is_case or binding):
                    continue                 # param simples: não é membro
                nm = cp.child_by_field_name("name")
                if nm is None:
                    continue
                kind = "variable" if binding == "var" else "constant"
                self.add_sym(cp, kind, self.text(nm), signature=None,
                             doc=None, visibility=self._visibility(cp))

    def _enum(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "enum", name, signature=self.sig_of(node, body),
                     doc=self._doc(node), visibility=self._visibility(node))
        self.scope.append((name, "class"))
        self._ctor_params(node)
        if body is not None:
            for c in body.children:
                self.visit(c)     # enum_case_definitions → simple/full_enum_case
        self.scope.pop()

    def _function(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        in_type = bool(self.scope)
        kind = "method" if in_type else "function"
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node),
                     visibility=self._visibility(node) if in_type else None)
        self.scope.append((name, "function"))
        if body is not None:
            self.visit(body)
        self.scope.pop()

    def _value(self, node, kind: str) -> None:
        if self.scope and self.scope[-1][1] == "function":
            return  # variável local — não é símbolo de topo/membro relevante
        pat = node.child_by_field_name("pattern")
        if pat is not None and pat.type == "identifier":
            vis = self._visibility(node) if self.scope else None
            self.add_sym(pat, kind, self.text(pat), signature=None, doc=None,
                         visibility=vis)
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
