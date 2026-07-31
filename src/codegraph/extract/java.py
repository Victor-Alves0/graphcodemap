"""Extractor L0 para Java (tree-sitter)."""

from __future__ import annotations

from .base import BaseExtractor

# tipos de nó que abrem um escopo próprio: o coletor de locais não desce neles
# (senão um local de um método vazaria para outro, ou para um lambda/anônima)
_NESTED_SCOPE = {"method_declaration", "constructor_declaration",
                 "class_declaration", "interface_declaration",
                 "enum_declaration", "record_declaration", "lambda_expression"}


def _simple_type(text: str) -> str | None:
    """Nome de classe simples de uma anotação de tipo, ou None quando não há um
    receptor de tipo de usuário útil (primitivo, `var`, parâmetro genérico).

    `List<Medico>` → `List`; `Medico[]` → `Medico`; `med.voll.Medico` → `Medico`.
    Primitivos e `var` começam com minúscula em Java — é o que os descarta, e é
    de propósito: `var` esconde o tipo (só o L1 o resolve)."""
    base = text.split("<", 1)[0].split("[", 1)[0].strip()
    simple = base.rsplit(".", 1)[-1]
    if not simple or simple[0].islower():   # primitivo, `var`, ou vazio
        return None
    return simple


class JavaExtractor(BaseExtractor):
    def __init__(self, source: bytes, module_fqn: str) -> None:
        super().__init__(source, module_fqn)
        # pilha de escopos varname->TipoSimples (classe: campos; método: params +
        # locais tipados). O topo é o mais interno, então local sombreia campo.
        self._types: list[dict[str, str]] = []

    def _lookup_type(self, var: str) -> str | None:
        for scope in reversed(self._types):
            t = scope.get(var)
            if t is not None:
                return t
        return None

    def _declared_type(self, type_node) -> str | None:
        return _simple_type(self.text(type_node)) if type_node is not None else None

    def _collect_fields(self, body, into: dict) -> None:
        if body is None:
            return
        for c in body.children:
            if c.type != "field_declaration":
                continue
            ty = self._declared_type(c.child_by_field_name("type"))
            if ty is None:
                continue
            for d in c.named_children:
                if d.type == "variable_declarator":
                    n = d.child_by_field_name("name")
                    if n is not None:
                        into[self.text(n)] = ty

    def _collect_locals(self, node, into: dict) -> None:
        """Params + locais tipados de UM método. Aproximação assumida: um local
        vale para o método todo (não do ponto da declaração em diante) — o
        deslize é raro (usar antes de declarar não compila) e sempre rebaixável."""
        for c in node.children:
            t = c.type
            if t == "formal_parameters":
                for p in c.named_children:
                    if p.type in ("formal_parameter", "spread_parameter"):
                        ty = self._declared_type(p.child_by_field_name("type"))
                        n = p.child_by_field_name("name")
                        if n is None:      # spread_parameter guarda o nome no declarator
                            n = next((g for g in p.named_children
                                      if g.type == "variable_declarator"), None)
                            if n is not None:
                                n = n.child_by_field_name("name")
                        if ty is not None and n is not None:
                            into[self.text(n)] = ty
            elif t == "local_variable_declaration":
                ty = self._declared_type(c.child_by_field_name("type"))
                if ty is not None:
                    for d in c.named_children:
                        if d.type == "variable_declarator":
                            n = d.child_by_field_name("name")
                            if n is not None:
                                into[self.text(n)] = ty
            elif t not in _NESTED_SCOPE:
                self._collect_locals(c, into)

    def visit(self, node) -> None:
        t = node.type
        if t == "import_declaration":
            self._import(node)
            return
        if t in ("class_declaration", "record_declaration"):
            self._class(node, "class")
            return
        if t == "interface_declaration":
            self._class(node, "interface")
            return
        if t == "enum_declaration":
            self._class(node, "enum")
            return
        if t in ("method_declaration", "constructor_declaration"):
            self._method(node)
            return
        if t == "method_invocation":
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

    def _class(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc(node))
        self.scope.append((name, "class"))
        fields: dict[str, str] = {}
        self._collect_fields(body, fields)
        self._types.append(fields)
        sup = node.child_by_field_name("superclass")
        if sup is not None:
            for c in sup.named_children:
                self.add_ref(sup, "inherits", self._qualify(self.text(c).split("<", 1)[0]))
        ifaces = node.child_by_field_name("interfaces")
        if ifaces is not None:
            for lst in ifaces.named_children:
                for c in lst.named_children:
                    self.add_ref(ifaces, "inherits",
                                 self._qualify(self.text(c).split("<", 1)[0]))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self._types.pop()
        self.scope.pop()

    def _method(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "method" if self.in_class() else "function", name,
                     signature=self.sig_of(node, body), doc=self._doc(node))
        self.scope.append((name, "function"))
        locals_: dict[str, str] = {}
        self._collect_locals(node, locals_)
        self._types.append(locals_)
        if body is not None:
            for c in body.children:
                self.visit(c)
        self._types.pop()
        self.scope.pop()

    def _doc(self, node) -> str | None:
        prev = node.prev_sibling
        if prev is not None and prev.type == "block_comment":
            raw = self.text(prev)
            if raw.startswith("/**"):
                lines = [ln.strip().lstrip("*").strip() for ln in raw[3:-2].splitlines()]
                return "\n".join(ln for ln in lines if ln) or None
        return None

    def _import(self, node) -> None:
        # import a.b.C; / import static a.b.C.m;
        for c in node.named_children:
            if c.type == "scoped_identifier":
                dotted = self.text(c)
                self.aliases[dotted.rsplit(".", 1)[-1]] = dotted
                self.add_ref(node, "imports", dotted)

    def _invocation(self, node) -> None:
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        if name_node is None:
            return
        name = self.text(name_node)
        if obj is None:
            self.add_ref(node, "calls", self._qualify(name))
            return
        if obj.type == "identifier":
            recv = self.text(obj)
            if recv in self.aliases:            # chamada estática: Tipo.metodo()
                self.add_ref(node, "calls", f"{self.aliases[recv]}.{name}")
                return
            ty = self._lookup_type(recv)        # receptor de tipo DECLARADO
            if ty is not None:
                # nome SIMPLES do tipo (não o fqn do import): o fqn do símbolo vem
                # do caminho do arquivo + escopo de classe, e o resolver casa por
                # SUFIXO — `Tipo.metodo` é o sufixo certo, o fqn expandido não
                # alinha. `var` não entra no mapa, então cai no nome nu → possible
                self.add_ref(node, "calls", f"{ty}.{name}")
                return
        elif obj.type == "field_access":
            # this.campo.metodo(): usa o tipo declarado do campo
            fld = obj.child_by_field_name("field")
            inner = obj.child_by_field_name("object")
            if (fld is not None and inner is not None and inner.type == "this"):
                ty = self._lookup_type(self.text(fld))
                if ty is not None:
                    self.add_ref(node, "calls", f"{ty}.{name}")
                    return
        # receptor sem tipo conhecido (var, expressão, cadeia): nome nu → possible
        self.add_ref(node, "calls", name)
