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
    def __init__(self, source: bytes, module_fqn: str, *,
                 is_package: bool = False) -> None:
        super().__init__(source, module_fqn)
        self.is_package = is_package
        # Module singleton/factory bindings provide real receiver evidence:
        # ``service = TokenService(); service.validate()``.  This lets L0 keep
        # useful cross-module dispatch without returning to name-only matches.
        self.receiver_types: dict[tuple[tuple[str, ...], str], str] = {}
        self.known_callable_names: set[str] = set()
        self.callable_bindings: set[tuple[tuple[str, ...], str]] = set()
        self.shadowed_names: dict[tuple[str, ...], set[str]] = {}
        self.local_aliases: dict[tuple[tuple[str, ...], str], str] = {}

    def run(self, tree):
        # References are emitted only for names that could denote repository
        # callables (local definitions or imports), not for every ordinary data
        # argument.  Pre-collecting handles forward definitions without adding
        # a second extraction pass.
        stack = [(tree.root_node, ())]
        while stack:
            node, scope = stack.pop()
            if node.type in {"function_definition", "class_definition"}:
                name = node.child_by_field_name("name")
                if name is not None:
                    text = self.text(name)
                    self.known_callable_names.add(text)
                    self.callable_bindings.add((scope, text))
                    scope = (*scope, text)
            stack.extend((child, scope) for child in node.named_children)
        return super().run(tree)

    def _scope_key(self) -> tuple[str, ...]:
        return tuple(name for name, _kind in self.scope)

    def _is_shadowed(self, name: str) -> bool:
        scope = self._scope_key()
        for size in range(len(scope), -1, -1):
            here = scope[:size]
            if (here, name) in self.local_aliases:
                return False
            if name in self.shadowed_names.get(here, ()):
                return True
            if (here, name) in self.callable_bindings:
                return False
        return False

    def _visible_alias(self, name: str) -> bool:
        return self._alias_target(name) is not None

    def _alias_target(self, name: str) -> str | None:
        scope = self._scope_key()
        for size in range(len(scope), -1, -1):
            here = scope[:size]
            target = self.local_aliases.get((here, name))
            if target is not None:
                return target
            if name in self.shadowed_names.get(here, ()):
                return None
        return self.aliases.get(name)

    def _bind_alias(self, name: str, target: str) -> None:
        scope = self._scope_key()
        if scope:
            self.local_aliases[(scope, name)] = target
        else:
            self.aliases[name] = target

    def _qualify(self, name: str) -> str:
        base, _, rest = name.partition(".")
        if (mapped := self._alias_target(base)) is not None:
            return f"{mapped}.{rest}" if rest else mapped
        return name

    def visit(self, node) -> None:
        t = node.type
        if t == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None:
                self.visit(inner)
                self._decorator_callback_refs(node, inner)
            return
        if t in {"list_comprehension", "set_comprehension",
                 "dictionary_comprehension", "generator_expression"}:
            self._comprehension(node)
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
        if t == "assignment":
            self._record_receiver_assignment(node)
        if not self.scope or self.in_class():
            # Atribuição que é DECLARAÇÃO de símbolo: nível de módulo (constante/
            # variável) ou corpo de classe (atributo de classe — parte da API do
            # tipo). Dentro de função o escopo é 'function' → NÃO captura (é local).
            # A gramática emite `assignment` CRU (não embrulhado em
            # expression_statement, como se supunha — o que escondia que TODO
            # símbolo de módulo Python sumia); `MAX: int = 3` e `x, y = ...` idem.
            if t == "assignment":
                self._record_assignment(node)
            elif t == "expression_statement":
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
        # Defaults and ``Annotated`` metadata execute at definition time and
        # are also where Python frameworks declare dependency callbacks:
        # ``Depends(get_membership)``.  The old extractor visited only the
        # body, making that wiring completely invisible.
        parameters = node.child_by_field_name("parameters")
        if parameters is not None:
            for c in parameters.named_children:
                self.visit(c)
        local_names = self._parameter_names(parameters)
        if body is not None:
            local_names |= self._local_bindings(body)
        self.shadowed_names.setdefault(self._scope_key(), set()).update(local_names)
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
        # caminho legado: assignment embrulhado em expression_statement (algumas
        # versões da gramática). O caminho cru é _record_assignment direto.
        for child in node.named_children:
            if child.type == "assignment":
                self._record_assignment(child)

    def _record_assignment(self, child) -> None:
        left = child.child_by_field_name("left")
        if left is None:
            return
        for ident in self._target_identifiers(left):
            name = self.text(ident)
            kind = "constant" if name.isupper() else "variable"
            # nó = a atribuição inteira (não só o nome): assim o body_hash cobre
            # o VALOR, e mudar `RETRIES = 3` → `RETRIES = 5` é detectado como
            # mudança do símbolo (o ChangeSet do host reporta)
            self.add_sym(
                child, kind, name,
                signature=None, doc=None,
                visibility="private" if name.startswith("_") else "public",
            )

    def _record_receiver_assignment(self, node) -> None:
        left = node.child_by_field_name("left")
        if left is None:
            return
        right = node.child_by_field_name("right")
        scope = self._scope_key()
        for ident in self._target_identifiers(left):
            name = self.text(ident)
            key = (scope, name)
            self.shadowed_names.setdefault(scope, set()).add(name)
            if right is None or right.type != "call":
                self.receiver_types.pop(key, None)
                continue
            constructor = right.child_by_field_name("function")
            if constructor is not None and constructor.type in {
                    "identifier", "attribute",
            }:
                self.receiver_types[key] = self._qualify(self.text(constructor))

    def _receiver_type(self, name: str) -> str | None:
        scope = self._scope_key()
        for size in range(len(scope), -1, -1):
            here = scope[:size]
            found = self.receiver_types.get((here, name))
            if found is not None:
                return found
            if name in self.shadowed_names.get(here, ()):
                return None
        return None

    def _parameter_names(self, parameters) -> set[str]:
        if parameters is None:
            return set()
        out: set[str] = set()
        for parameter in parameters.named_children:
            name = parameter.child_by_field_name("name")
            binding = name
            if binding is None and parameter.type == "typed_parameter":
                type_node = parameter.child_by_field_name("type")
                binding = next(
                    (child for child in parameter.named_children
                     if child is not type_node), None)
            if binding is None and parameter.type in {
                    "identifier", "list_splat_pattern",
                    "dictionary_splat_pattern",
            }:
                binding = parameter
            if binding is not None:
                out.update(self.text(target)
                           for target in self._target_identifiers(binding))
        return out

    def _local_bindings(self, body) -> set[str]:
        """Names Python binds in this lexical function, excluding nested bodies."""
        out: set[str] = set()
        stack = list(body.named_children)
        while stack:
            node = stack.pop()
            if node.type in {"function_definition", "class_definition"}:
                continue
            if node.type == "lambda":
                continue
            if node.type in {"assignment", "augmented_assignment"}:
                left = node.child_by_field_name("left")
                if left is not None:
                    out.update(self.text(target)
                               for target in self._target_identifiers(left))
            elif node.type == "for_statement":
                left = node.child_by_field_name("left")
                if left is not None:
                    out.update(self.text(target)
                               for target in self._target_identifiers(left))
            elif node.type in {"with_item", "except_clause"}:
                value = node.child_by_field_name("value")
                alias = (value.child_by_field_name("alias")
                         if value is not None else None)
                if alias is not None:
                    out.update(self.text(child) for child in alias.named_children
                               if child.type == "identifier")
            elif node.type in {"import_statement", "import_from_statement"}:
                out.update(self._import_binding_names(node))
            if node.type in {"list_comprehension", "set_comprehension",
                             "dictionary_comprehension", "generator_expression"}:
                continue
            stack.extend(node.named_children)
        return out

    def _import_binding_names(self, node) -> set[str]:
        out: set[str] = set()
        module = (node.child_by_field_name("module_name")
                  if node.type == "import_from_statement" else None)
        for child in node.named_children:
            if child == module:
                continue
            if child.type == "aliased_import":
                alias = child.child_by_field_name("alias")
                if alias is not None:
                    out.add(self.text(alias))
            elif child.type == "dotted_name":
                text = self.text(child)
                out.add(text.rsplit(".", 1)[-1] if module is not None
                        else text.split(".", 1)[0])
        return out

    def _comprehension(self, node) -> None:
        scope = self._scope_key()
        original = set(self.shadowed_names.get(scope, ()))
        clauses = [child for child in node.named_children
                   if child.type in {"for_in_clause", "if_clause"}]
        bodies = [child for child in node.named_children if child not in clauses]
        try:
            for clause in clauses:
                if clause.type == "for_in_clause":
                    right = clause.child_by_field_name("right")
                    if right is not None:
                        self.visit(right)
                    left = clause.child_by_field_name("left")
                    if left is not None:
                        names = {self.text(target)
                                 for target in self._target_identifiers(left)}
                        self.shadowed_names.setdefault(scope, set()).update(names)
                else:
                    for child in clause.named_children:
                        self.visit(child)
            for body in bodies:
                self.visit(body)
        finally:
            self.shadowed_names[scope] = original

    @classmethod
    def _target_identifiers(cls, left) -> list:
        """Identificadores-alvo de uma atribuição. `a = …` → [a]; `x, y = …` e
        `[a, b] = …` → cada nome (desempacotamento). Subscrito/atributo
        (`d[k] = …`, `self.x = …`) não são declarações de símbolo → ignorados."""
        if left.type == "identifier":
            return [left]
        if left.type in (
                "pattern_list", "tuple_pattern", "list_pattern",
                "list_splat_pattern", "dictionary_splat_pattern",
        ):
            out: list = []
            for c in left.named_children:
                out.extend(cls._target_identifiers(c))   # aninhado: (a, (b, c))
            return out
        return []

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
                base = dotted.split(".", 1)[0]
                self._bind_alias(base, base)
                self._bind_alias(dotted, dotted)
                self.add_ref(node, "imports", dotted)
            elif c.type == "aliased_import":
                target = c.child_by_field_name("name")
                alias = c.child_by_field_name("alias")
                if target is not None and alias is not None:
                    dotted = self.text(target)
                    self._bind_alias(self.text(alias), dotted)
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
                target = f"{module}.{name}" if module else name
                self._bind_alias(name, target)
                self.add_ref(node, "imports", target)
            elif c.type == "aliased_import":
                target = c.child_by_field_name("name")
                alias = c.child_by_field_name("alias")
                if target is not None and alias is not None:
                    name = self.text(target)
                    guess = f"{module}.{name}" if module else name
                    self._bind_alias(self.text(alias), guess)
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
            parts = [part for part in self.module_fqn.split(".") if part]
            # For ``pkg/__init__.py``, module_fqn is the package itself.  Add a
            # synthetic leaf so ``from .service`` stays inside ``pkg`` just as
            # it does for a normal module ``pkg.current``.
            if self.is_package:
                parts.append("__init__")
            base = parts[: max(len(parts) - dots, 0)]
            return ".".join([*base, rest] if rest else base)
        return self.text(module_node)

    # -- calls ---------------------------------------------------------------

    def _decorator_callback_refs(self, decorated, definition) -> None:
        """Record callback values inside decorators without inventing calls.

        ``@router.get(..., dependencies=[Depends(auth)])`` wires ``auth`` into
        the endpoint, but it does not directly call ``auth``.  Keeping this as
        a ``references`` edge makes dead-code/impact queries conservative while
        leaving the call graph truthful.
        """
        name_node = definition.child_by_field_name("name")
        if name_node is None:
            return
        kind = "class" if definition.type == "class_definition" else "function"
        self.scope.append((self.text(name_node), kind))
        scope_key = self._scope_key()
        inner_shadows = self.shadowed_names.pop(scope_key, None)
        try:
            for child in decorated.named_children:
                if child.type != "decorator":
                    continue
                expression = next(iter(child.named_children), None)
                target = (expression.child_by_field_name("function")
                          if expression is not None
                          and expression.type == "call" else expression)
                if target is not None and target.type in {
                        "identifier", "attribute",
                }:
                    # Applying a decorator is a structural dependency, but not
                    # a direct runtime call from the decorated body.
                    self.add_ref(
                        target, "references", self._qualify(self.text(target)))
                for call in self._calls_in(child):
                    self._callable_argument_refs(call)
        finally:
            if inner_shadows is not None:
                self.shadowed_names[scope_key] = inner_shadows
            self.scope.pop()

    @staticmethod
    def _calls_in(node):
        if node.type == "call":
            yield node
        for child in node.named_children:
            yield from PythonExtractor._calls_in(child)

    def _callable_argument_refs(self, call) -> None:
        args = call.child_by_field_name("arguments")
        if args is None:
            return
        for arg in args.named_children:
            self._callable_value_ref(arg)

    def _callable_value_ref(self, node) -> None:
        """Capture a value that may be a callback/class passed to a call.

        Resolution later restricts these references to callable symbols.  We
        deliberately do not emit ``calls`` here: passing ``handler`` to
        Depends/Celery/pytest/registration APIs is a use, not an invocation.
        """
        if node.type == "identifier":
            name = self.text(node)
            if (not self._is_shadowed(name)
                    and (name in self.known_callable_names
                         or self._visible_alias(name))):
                self.add_ref(node, "references", self._qualify(name))
            return
        if node.type == "attribute":
            dotted = self.text(node)
            base, _, rest = dotted.partition(".")
            member = rest.rsplit(".", 1)[-1] if rest else dotted
            if (self._visible_alias(base)
                    or (not self._is_shadowed(member)
                        and member in self.known_callable_names)):
                if base in ("self", "cls"):
                    self.add_ref(node, "references", member)
                else:
                    self.add_ref(node, "references", self._qualify(dotted))
            return
        if node.type == "keyword_argument":
            value = node.child_by_field_name("value")
            if value is not None:
                self._callable_value_ref(value)
            return
        if node.type == "call":
            # The callee is an invocation (and is extracted separately when
            # this call is part of ordinary code).  Only values passed through
            # the wrapper are callback references.
            self._callable_argument_refs(node)
            return
        if node.type == "pair":
            value = node.child_by_field_name("value")
            if value is not None:
                self._callable_value_ref(value)
            return
        if node.type in {
                "argument_list", "list", "list_splat", "tuple", "set",
                "dictionary", "dictionary_splat", "parenthesized_expression",
        }:
            for child in node.named_children:
                self._callable_value_ref(child)

    def _call(self, node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            name = self.text(fn)
            if name in _BUILTINS and not self._visible_alias(name):
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
            elif (alias_target := self._alias_target(base)) is not None:
                self.add_ref(site, "calls", f"{alias_target}.{rest}"
                             if rest else alias_target)
            elif (receiver_type := self._receiver_type(base)) is not None:
                method = rest.rsplit(".", 1)[-1] if rest else dotted
                self.add_ref(site, "calls", f"{receiver_type}.{method}")
            else:
                # receptor desconhecido: só o nome do método (resolução vira 'possible')
                self.add_ref(site, "calls", dotted.rsplit(".", 1)[-1])
        self._callable_argument_refs(node)
