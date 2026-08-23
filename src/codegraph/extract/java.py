"""Extractor L0 para Java (tree-sitter)."""

from __future__ import annotations

from .base import BaseExtractor

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

    def _bind_type(self, name: str, ty: str | None) -> None:
        """Registra uma declaração no escopo lexical corrente.

        Declarações duplicadas no mesmo escopo não são Java válido. Ainda
        assim, arquivos incompletos aparecem durante indexação incremental;
        nesses casos o L0 perde a qualificação em vez de escolher um tipo por
        ordem de travessia.
        """
        if not self._types:
            return
        scope = self._types[-1]
        if name in scope:
            scope[name] = None
        else:
            scope[name] = ty

    def _parameter_types(self, node) -> dict[str, str | None]:
        """Tipos dos parâmetros formais, cujo escopo cobre o corpo do método."""
        types: dict[str, str | None] = {}
        params = next((c for c in node.children
                       if c.type == "formal_parameters"), None)
        if params is None:
            return types
        for param in params.named_children:
            if param.type not in ("formal_parameter", "spread_parameter"):
                continue
            type_node = param.child_by_field_name("type")
            if type_node is None and param.type == "spread_parameter":
                type_node = next(
                    (child for child in param.named_children
                     if child.type != "variable_declarator"),
                    None,
                )
            ty = self._declared_type(type_node)
            name = param.child_by_field_name("name")
            if name is None:  # spread_parameter guarda o nome no declarator
                declarator = next(
                    (c for c in param.named_children
                     if c.type == "variable_declarator"),
                    None,
                )
                if declarator is not None:
                    name = declarator.child_by_field_name("name")
            if name is not None:
                text = self.text(name)
                types[text] = None if text in types else ty
        return types

    def _lexical_block(self, node) -> None:
        """Visita um bloco em ordem, descartando seus locais ao sair."""
        self._types.append({})
        for child in node.children:
            self.visit(child)
        self._types.pop()

    def _local_declaration(self, node) -> None:
        """Visita initializers antes de tornar cada declarador visível.

        Isso modela tanto o ponto da declaração quanto declarações múltiplas
        (`A a = make(), b = a`) sem o antigo mapa flat por método.
        """
        declared = self._declared_type(node.child_by_field_name("type"))
        for child in node.children:
            if child.type != "variable_declarator":
                self.visit(child)
                continue
            for part in child.children:
                self.visit(part)
            name = child.child_by_field_name("name")
            if name is None:
                continue
            ty = declared
            value = child.child_by_field_name("value")
            if (ty is None and value is not None
                    and value.type == "object_creation_expression"):
                ty = self._declared_type(value.child_by_field_name("type"))
            self._bind_type(self.text(name), ty)

    def _enhanced_for(self, node) -> None:
        """A variável do foreach existe no corpo, não no iterável."""
        value = node.child_by_field_name("value")
        if value is not None:
            self.visit(value)
        self._types.append({})
        name = node.child_by_field_name("name")
        if name is not None:
            self._bind_type(
                self.text(name),
                self._declared_type(node.child_by_field_name("type")),
            )
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(body)
        self._types.pop()

    def _catch_clause(self, node) -> None:
        """Limita o parâmetro de catch ao respectivo bloco."""
        self._types.append({})
        param = next((c for c in node.named_children
                      if c.type == "catch_formal_parameter"), None)
        if param is not None:
            name = param.child_by_field_name("name")
            type_node = next((c for c in param.named_children
                              if c.type == "catch_type"), None)
            if name is not None:
                self._bind_type(self.text(name), self._declared_type(type_node))
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(body)
        self._types.pop()

    def _resource(self, node) -> None:
        """Torna um recurso visível somente depois de seu initializer."""
        value = node.child_by_field_name("value")
        if value is not None:
            self.visit(value)
        name = node.child_by_field_name("name")
        if name is not None:
            self._bind_type(
                self.text(name),
                self._declared_type(node.child_by_field_name("type")),
            )

    def _try_with_resources(self, node) -> None:
        """Recursos vivem nos initializers seguintes e no corpo do try."""
        resources = next((c for c in node.named_children
                          if c.type == "resource_specification"), None)
        body = node.child_by_field_name("body")
        self._types.append({})
        if resources is not None:
            for resource in resources.named_children:
                self.visit(resource)
        if body is not None:
            self.visit(body)
        self._types.pop()
        # Recursos não estão no escopo dos catches/finally.
        for child in node.named_children:
            if child not in (resources, body):
                self.visit(child)

    def _lambda(self, node) -> None:
        """Parâmetros de lambda sombreiam receptores externos no seu corpo."""
        params = node.child_by_field_name("parameters")
        body = node.child_by_field_name("body")
        self._types.append({})
        if params is not None:
            candidates = (params.named_children
                          if params.type != "identifier" else [params])
            for param in candidates:
                if param.type in ("formal_parameter", "spread_parameter"):
                    name = param.child_by_field_name("name")
                    type_node = param.child_by_field_name("type")
                    if type_node is None and param.type == "spread_parameter":
                        type_node = next(
                            (child for child in param.named_children
                             if child.type != "variable_declarator"),
                            None,
                        )
                    ty = self._declared_type(type_node)
                    if name is None and param.type == "spread_parameter":
                        declarator = next(
                            (child for child in param.named_children
                             if child.type == "variable_declarator"),
                            None,
                        )
                        if declarator is not None:
                            name = declarator.child_by_field_name("name")
                elif param.type == "identifier":
                    name, ty = param, None
                else:
                    continue
                if name is not None:
                    self._bind_type(self.text(name), ty)
        if body is not None:
            self.visit(body)
        self._types.pop()

    def _instanceof_binding(self, condition) -> tuple[str, str | None] | None:
        """Binding do caso positivo simples ``value instanceof Type name``.

        O escopo dependente de fluxo de negações, ``&&``/``||`` e saídas
        abruptas pertence ao CFG. O L0 só qualifica o caso sintaticamente certo
        e deixa os demais sem tipo, evitando inventar disponibilidade.
        """
        current = condition
        while current is not None and current.type == "parenthesized_expression":
            current = next(iter(current.named_children), None)
        if current is None or current.type != "instanceof_expression":
            return None
        name = current.child_by_field_name("name")
        type_node = current.child_by_field_name("right")
        if name is None or type_node is None:
            return None
        return self.text(name), self._declared_type(type_node)

    def _if_statement(self, node) -> None:
        """Restringe pattern variable ao ramo positivo comprovado."""
        condition = node.child_by_field_name("condition")
        consequence = node.child_by_field_name("consequence")
        alternative = node.child_by_field_name("alternative")
        if condition is not None:
            self.visit(condition)
        binding = self._instanceof_binding(condition)
        self._types.append({})
        if binding is not None:
            self._bind_type(*binding)
        if consequence is not None:
            self.visit(consequence)
        self._types.pop()
        if alternative is not None:
            self.visit(alternative)

    def _type_patterns(self, node) -> list[tuple[str, str | None]]:
        """Bindings explícitos de um label de switch, sem inferência de fluxo."""
        out: list[tuple[str, str | None]] = []

        def collect(current) -> None:
            if current.type == "type_pattern":
                named = current.named_children
                if len(named) >= 2 and named[-1].type == "identifier":
                    out.append((self.text(named[-1]),
                                self._declared_type(named[0])))
                return
            for child in current.named_children:
                collect(child)

        collect(node)
        return out

    def _switch_arm(self, node) -> None:
        """Cada rule/grupo ganha os bindings apenas do próprio label."""
        label = next((child for child in node.named_children
                      if child.type == "switch_label"), None)
        self._types.append({})
        if label is not None:
            for binding in self._type_patterns(label):
                self._bind_type(*binding)
        for child in node.children:
            self.visit(child)
        self._types.pop()

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
        if t in ("block", "switch_block"):
            self._lexical_block(node)
            return
        if t == "local_variable_declaration":
            self._local_declaration(node)
            return
        if t == "for_statement":
            # O initializer do for permanece visível na condição, update e
            # corpo, mas não pode vazar para a instrução seguinte.
            self._types.append({})
            for child in node.children:
                self.visit(child)
            self._types.pop()
            return
        if t == "enhanced_for_statement":
            self._enhanced_for(node)
            return
        if t == "catch_clause":
            self._catch_clause(node)
            return
        if t == "try_with_resources_statement":
            self._try_with_resources(node)
            return
        if t == "resource":
            self._resource(node)
            return
        if t == "lambda_expression":
            self._lambda(node)
            return
        if t == "if_statement":
            self._if_statement(node)
            return
        if t in ("switch_rule", "switch_block_statement_group"):
            self._switch_arm(node)
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
        self._types.append(self._parameter_types(node))
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
