"""Extractor L0 para Java (tree-sitter)."""

from __future__ import annotations

from .base import BaseExtractor

# tipos de nó que abrem um escopo próprio: o coletor de locais não desce neles
# (senão um local de um método vazaria para outro, ou para um lambda/anônima)
_NESTED_SCOPE = {"method_declaration", "constructor_declaration",
                 "compact_constructor_declaration",
                 "class_declaration", "interface_declaration",
                 "enum_declaration", "record_declaration", "lambda_expression"}

_ACCESS = ("public", "private", "protected")


def _simple_name(text: str) -> str:
    """Nome de tipo cru → nome simples: strip de genérico e pacote.
    `Base<String>` → `Base`; `a.b.Base` → `Base`. (Diferente de _simple_type:
    aqui NÃO se descarta minúsculo — herança/uso quer o nome como está.)"""
    return text.split("<", 1)[0].strip().rsplit(".", 1)[-1]


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
        self._types: list[dict[str, str | None]] = []
        # marca se o tipo-recipiente atual é interface (membro sem modificador é
        # implicitamente public)
        self._iface: list[bool] = []

    def _lookup_type(self, var: str) -> str | None:
        for scope in reversed(self._types):
            if var in scope:
                return scope[var]
        return None

    def _is_declared(self, var: str) -> bool:
        """Se ``var`` existe em algum escopo, mesmo sem tipo L0 conhecido.

        A distinção entre "não declarado" e "declarado com tipo desconhecido"
        impede que a convenção de nomes Java transforme `var Service = ...`
        numa chamada estática fictícia a `Service`.
        """
        return any(var in scope for scope in reversed(self._types))

    def _declared_type(self, type_node) -> str | None:
        if type_node is None:
            return None
        raw = self.text(type_node).split("<", 1)[0].split("[", 1)[0].strip()
        simple = _simple_type(raw)
        if simple is None or "|" in raw:  # multi-catch não tem receptor único
            return None
        # Agora o resolver L0 entende o nome Java canônico contra FQNs baseados
        # em path; preservar import/FQN evita colisões entre tipos homônimos.
        if "." in raw:
            return raw
        return self.aliases.get(simple, simple)

    @staticmethod
    def _modifiers(node) -> list[str]:
        m = next((c for c in node.children if c.type == "modifiers"), None)
        return m.text.decode("utf-8", "replace").split() if m is not None else []

    def _visibility(self, node, default: str = "package") -> str:
        for a in _ACCESS:
            if a in self._modifiers(node):
                return a
        return default

    def _fields(self, body) -> dict[str, str | None]:
        """Adiciona um símbolo por campo E devolve o mapa nome->TipoSimples para
        o type-tracking. Campo é parte da API do tipo (navegável); `static final`
        é constant, o resto variable; a visibilidade vem dos modificadores."""
        types: dict[str, str | None] = {}
        if body is None:
            return types
        for c in body.children:
            if c.type != "field_declaration":
                continue
            ty = self._declared_type(c.child_by_field_name("type"))
            mods = self._modifiers(c)
            kind = "constant" if ("static" in mods and "final" in mods) else "variable"
            vis = next((a for a in _ACCESS if a in mods), "package")
            for d in c.named_children:
                if d.type != "variable_declarator":
                    continue
                n = d.child_by_field_name("name")
                if n is None:
                    continue
                fname = self.text(n)
                # nó = o declarator (nome+valor): body_hash detecta mudança de valor
                self.add_sym(d, kind, fname, signature=None, doc=None,
                             visibility=vis)
                types[fname] = ty
        return types

    def _enum_constants(self, body) -> None:
        if body is None:
            return
        for c in body.children:
            if c.type != "enum_constant":
                continue
            n = c.child_by_field_name("name") or next(
                (g for g in c.named_children if g.type == "identifier"), None)
            if n is not None:
                self.add_sym(c, "constant", self.text(n), signature=None,
                             doc=None, visibility="public")

    def _record_components(self, node) -> dict[str, str | None]:
        """Componentes de um record viram símbolos (campos implícitos) e entram
        no type-tracking. Devolve nome->TipoSimples."""
        types: dict[str, str | None] = {}
        params = next((c for c in node.children
                       if c.type == "formal_parameters"), None)
        if params is None:
            return types
        for p in params.named_children:
            if p.type != "formal_parameter":
                continue
            n = p.child_by_field_name("name")
            ty = self._declared_type(p.child_by_field_name("type"))
            if n is not None:
                self.add_sym(p, "variable", self.text(n), signature=None,
                             doc=None, visibility="private")
                types[self.text(n)] = ty
        return types

    def _collect_locals(self, node, into: dict[str, str | None]) -> None:
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
                        if n is not None:
                            into[self.text(n)] = ty
            elif t == "local_variable_declaration":
                ty = self._declared_type(c.child_by_field_name("type"))
                for d in c.named_children:
                    if d.type != "variable_declarator":
                        continue
                    n = d.child_by_field_name("name")
                    # `var x = new Service()` esconde a anotação, mas não o tipo
                    # sintático do initializer. Esta inferência é exata e barata
                    # o bastante para L0; factory calls continuam deliberadamente
                    # sem tipo até o L1.
                    dty = ty
                    if dty is None:
                        value = d.child_by_field_name("value")
                        if value is not None and value.type == "object_creation_expression":
                            dty = self._declared_type(value.child_by_field_name("type"))
                    if n is not None:
                        into[self.text(n)] = dty
            elif t in ("enhanced_for_statement", "resource"):
                ty = self._declared_type(c.child_by_field_name("type"))
                n = c.child_by_field_name("name")
                if n is not None:
                    into[self.text(n)] = ty
                # initializer/corpo ainda pode conter outras declarações.
                self._collect_locals(c, into)
            elif t == "catch_formal_parameter":
                n = c.child_by_field_name("name")
                type_node = next((g for g in c.named_children
                                  if g.type == "catch_type"), None)
                ty = self._declared_type(type_node)
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
        if t in ("method_declaration", "constructor_declaration",
                 "compact_constructor_declaration"):
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
                self.add_ref(type_node, "calls",
                             self._qualify(self.text(type_node).split("<", 1)[0]))
            for c in node.children:
                self.visit(c)
            return
        if t == "method_reference":
            self._method_reference(node)
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
                     doc=self._doc(node), visibility=self._visibility(node))
        self.scope.append((name, "class"))
        self._iface.append(node.type == "interface_declaration")
        # campos + componentes de record alimentam o type-tracking (topo da pilha)
        types = self._fields(body)
        if node.type == "record_declaration":
            types.update(self._record_components(node))
        self._types.append(types)
        if node.type == "enum_declaration":
            self._enum_constants(body)
        self._inherits(node)
        if body is not None:
            for c in body.children:
                self.visit(c)
        self._types.pop()
        self._iface.pop()
        self.scope.pop()

    def _inherits(self, node) -> None:
        """extends de classe (`superclass`), implements (`interfaces`) e extends
        de interface (`extends_interfaces`). Emite o NOME SIMPLES, não o fqn do
        import: o fqn de símbolo é baseado em caminho e duplica a classe
        (`app.Base.Base`), então o import qualificado `app.Base` nunca casaria —
        o nome nu resolve por nome+kind, como as chamadas."""
        sup = node.child_by_field_name("superclass")
        if sup is not None:
            for c in sup.named_children:
                self.add_ref(c, "inherits", _simple_name(self.text(c)))
        for child in node.children:
            if child.type not in ("interfaces", "super_interfaces",
                                  "extends_interfaces"):
                continue
            for lst in child.named_children:          # type_list
                targets = lst.named_children if lst.type == "type_list" else [lst]
                for c in targets:
                    self.add_ref(c, "inherits", _simple_name(self.text(c)))

    def _method(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        # membro de interface é implicitamente public quando sem modificador
        default = "public" if (self._iface and self._iface[-1]) else "package"
        self.add_sym(node, "method" if self.in_class() else "function", name,
                     signature=self.sig_of(node, body), doc=self._doc(node),
                     visibility=self._visibility(node, default))
        self.scope.append((name, "function"))
        locals_: dict[str, str | None] = {}
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
        wildcard = any(c.type == "asterisk" for c in node.named_children)
        for c in node.named_children:
            if c.type == "scoped_identifier":
                dotted = self.text(c)
                # `import java.util.*` importa membros, não uma variável/type
                # chamado `util`. Registrá-lo como alias fazia `util.run()` virar
                # a chamada fictícia `java.util.run`.
                if not wildcard:
                    self.aliases[dotted.rsplit(".", 1)[-1]] = dotted
                self.add_ref(c, "imports", dotted + (".*" if wildcard else ""))

    def _invocation(self, node) -> None:
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        if name_node is None:
            return
        name = self.text(name_node)
        if obj is None:
            self.add_ref(name_node, "calls", self._qualify(name))
            return
        if obj.type == "identifier":
            recv = self.text(obj)
            ty = self._lookup_type(recv)        # receptor de tipo DECLARADO
            if ty is not None:
                # Import/FQN é preservado quando conhecido; tipo local continua
                # simples. O resolver entende ambas as representações.
                self.add_ref(name_node, "calls", f"{ty}.{name}")
                return
            if self._is_declared(recv):
                # O identificador é uma variável real, mas seu tipo está além
                # do L0 (`var x = factory()`, por exemplo). Manter o nome nu é
                # conservador; tratá-lo como tipo por capitalização fabricaria
                # uma ligação estática para uma classe homônima.
                self.add_ref(name_node, "calls", name)
                return
            if recv in self.aliases:            # chamada estática: Tipo.metodo()
                self.add_ref(name_node, "calls", f"{self.aliases[recv]}.{name}")
                return
            # Tipos começam convencionalmente em maiúscula em Java e a
            # própria gramática não distingue `Helper.run()` de `obj.run()`.
            # O sinal é forte o bastante para qualificar tipos locais/nested;
            # identificadores minúsculos continuam no caminho conservador.
            if recv[:1].isupper():
                self.add_ref(name_node, "calls", f"{recv}.{name}")
                return
        elif obj.type == "scoped_identifier":
            # chamada estática plenamente qualificada: a.b.Helper.run().
            self.add_ref(name_node, "calls", f"{self.text(obj)}.{name}")
            return
        elif obj.type == "object_creation_expression":
            ty = self._declared_type(obj.child_by_field_name("type"))
            if ty is not None:
                self.add_ref(name_node, "calls", f"{ty}.{name}")
                return
        elif obj.type == "array_access":
            base = obj.child_by_field_name("array")
            if base is not None and base.type == "identifier":
                ty = self._lookup_type(self.text(base))
                if ty is not None:
                    self.add_ref(name_node, "calls", f"{ty}.{name}")
                    return
        elif obj.type == "field_access":
            # A gramática representa `a.b.Tools` como field_access (a mesma
            # forma de `obj.field`). O último segmento de tipo em maiúscula é a
            # evidência sintática disponível para a chamada estática FQN.
            obj_text = self.text(obj)
            if obj_text.rsplit(".", 1)[-1][:1].isupper():
                self.add_ref(name_node, "calls", f"{obj_text}.{name}")
                return
            # this.campo.metodo(): usa o tipo declarado do campo
            fld = obj.child_by_field_name("field")
            inner = obj.child_by_field_name("object")
            if (fld is not None and inner is not None and inner.type == "this"):
                ty = self._lookup_type(self.text(fld))
                if ty is not None:
                    self.add_ref(name_node, "calls", f"{ty}.{name}")
                    return
        # receptor sem tipo conhecido (var, expressão, cadeia): nome nu → possible
        self.add_ref(name_node, "calls", name)

    def _method_reference(self, node) -> None:
        """Extrai `Type::method`, `obj::method`, `this::method` e `Type::new`.

        Method references são chamadas adiadas no bytecode, mas representam a
        mesma dependência estrutural que uma invocation e eram completamente
        invisíveis no L0.
        """
        named = node.named_children
        if not named:
            return
        raw = self.text(node)
        lhs, sep, rhs = raw.partition("::")
        if not sep:
            return
        lhs, rhs = lhs.strip(), rhs.strip()
        target_node = named[-1]
        if rhs == "new":
            # `Type[]::new` aloca array, não invoca construtor da classe.
            if "[]" not in lhs:
                self.add_ref(named[0], "calls", self._qualify(lhs))
            return
        if not rhs:
            return
        if lhs in ("this", "super"):
            self.add_ref(target_node, "calls", rhs)
            return
        ty = self._lookup_type(lhs)
        if ty is not None:
            self.add_ref(target_node, "calls", f"{ty}.{rhs}")
            return
        if self._is_declared(lhs):
            self.add_ref(target_node, "calls", rhs)
            return
        if lhs in self.aliases:
            self.add_ref(target_node, "calls", f"{self.aliases[lhs]}.{rhs}")
            return
        if "." in lhs or lhs[:1].isupper():
            self.add_ref(target_node, "calls", f"{lhs}.{rhs}")
            return
        self.add_ref(target_node, "calls", rhs)
