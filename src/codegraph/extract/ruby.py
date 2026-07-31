"""Extractor L0 dedicado para Ruby (tree-sitter).

Símbolos: module, class (com superclass), method, singleton_method (def self.x),
constantes de topo. Refs: calls (nome do método, no site do NOME), imports
(require/require_relative), inherits (superclass).
"""

from __future__ import annotations

from .base import BaseExtractor

_REQUIRE = {"require", "require_relative", "load", "autoload"}
_ATTR = {"attr_accessor", "attr_reader", "attr_writer"}
_MIXIN = {"include", "extend", "prepend"}      # composição do Ruby → inherits
_VIS_KW = {"private", "public", "protected"}


class RubyExtractor(BaseExtractor):
    def __init__(self, source: bytes, module_fqn: str) -> None:
        super().__init__(source, module_fqn)
        # acesso corrente por container: `private`/`public`/`protected` (bare) são
        # SEÇÕES que valem para os métodos seguintes, como no C++.
        self._access: list[str] = []

    def _cur_access(self) -> str | None:
        return self._access[-1] if self._access else None

    def visit(self, node) -> None:
        t = node.type
        if t in ("module", "class"):
            self._container(node, "module" if t == "module" else "class")
            return
        if t == "method":
            self._method(node, node.child_by_field_name("name"))
            return
        if t == "singleton_method":
            # def self.x / def Foo.x — método de classe
            self._method(node, node.child_by_field_name("name"))
            return
        if t == "call":
            self._call(node)
            for c in node.children:
                self.visit(c)
            return
        if t == "assignment":
            self._const_assignment(node)
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
        name = self._const_name(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node))
        self.scope.append((name, "class"))
        self._access.append("public")
        sup = node.child_by_field_name("superclass")
        if sup is not None:
            for c in sup.named_children:
                if c.type in ("constant", "scope_resolution"):
                    self.add_ref(c, "inherits", self._const_name(c).rsplit(".", 1)[-1])
        if body is not None:
            for c in body.children:
                # `private`/`public`/`protected` sozinho = rótulo de seção
                if c.type == "identifier" and self.text(c) in _VIS_KW:
                    self._access[-1] = self.text(c)
                else:
                    self.visit(c)
        self._access.pop()
        self.scope.pop()

    def _method(self, node, name_node) -> None:
        if name_node is None:
            return
        name = self.text(name_node)
        in_container = bool(self.scope)
        kind = "method" if in_container else "function"
        body = node.child_by_field_name("body")
        # método de instância herda a seção corrente; singleton (def self.x) é de
        # classe e a seção `private` não o afeta → public
        vis = None
        if in_container:
            vis = "public" if node.type == "singleton_method" else self._cur_access()
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node), visibility=vis)
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _const_assignment(self, node) -> None:
        # constante de topo OU de classe/módulo; dentro de método não é símbolo
        if self.scope and not self.in_class():
            return
        left = node.child_by_field_name("left")
        if left is not None and left.type == "constant":
            self.add_sym(left, "constant", self.text(left), signature=None,
                         doc=None, visibility=self._cur_access())

    def _attrs(self, node) -> None:
        """attr_accessor/reader/writer :a, :b → membros (idioma de propriedade)."""
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        vis = self._cur_access()
        for a in args.named_children:
            if a.type in ("simple_symbol", "symbol"):
                nm = self.text(a).lstrip(":")
                if nm:
                    self.add_sym(a, "variable", nm, signature=None,
                                 doc=None, visibility=vis)

    def _mixin(self, node) -> None:
        """include/extend/prepend Mod → inherits (composição de módulo)."""
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        for a in args.named_children:
            if a.type in ("constant", "scope_resolution"):
                self.add_ref(a, "inherits", self._const_name(a).rsplit(".", 1)[-1])

    def _const_name(self, node) -> str:
        # constant → 'Foo'; scope_resolution 'A::B' → 'A.B'
        return self.text(node).replace("::", ".")

    def _doc(self, node) -> str | None:
        prev = node.prev_sibling
        lines = []
        while prev is not None and prev.type == "comment":
            lines.append(self.text(prev).lstrip("# ").rstrip())
            prev = prev.prev_sibling
        return "\n".join(reversed(lines)).strip() or None if lines else None

    # -- refs ----------------------------------------------------------------

    def _call(self, node) -> None:
        method = node.child_by_field_name("method")
        if method is None:
            return
        name = self.text(method)
        receiver = node.child_by_field_name("receiver")
        if receiver is None:
            if name in _REQUIRE:
                self._import(node)
                return
            if name in _ATTR and self.in_class():
                self._attrs(node)
                return
            if name in _MIXIN and self.in_class():
                self._mixin(node)
                return
        self.add_ref(method, "calls", name)

    def _import(self, node) -> None:
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        for a in args.named_children:
            if a.type == "string":
                spec = self.text(a).strip("'\"")
                mod = spec.replace("/", ".").strip(".")
                if mod:
                    self.add_ref(node, "imports", mod)
