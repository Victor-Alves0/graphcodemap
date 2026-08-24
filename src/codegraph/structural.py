"""Persistent structural facts shared by the Java and Python frontends.

Dedicated extractors remain responsible for language-specific declarations and
call guesses.  This pass adds the common phase-one vocabulary that must have the
same semantics in both languages: parameters, locals, lexical containment,
definitions, reads, writes and simple return-value relations.

The pass is deliberately syntax-backed.  It emits ``certain/l0`` only when the
declaration and use are present in the parsed tree; it does not guess runtime
aliasing or heap identity.

Java locals in disjoint lexical scopes are distinct bindings.  Their stable L0
identity uses same-name source order because the graph does not yet persist
block/scope nodes: line changes, body edits and unrelated declarations are
stable, while inserting or reordering an earlier homonym can change ``#N``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .extract.base import Ref, Sym
from .util import byte_column, content_hash

_CALLABLE_KINDS = {"function", "method", "property"}
_VARIABLE_KINDS = {
    "parameter", "local", "field", "property", "variable", "constant",
}
_PY_CALLABLE_NODES = {"function_definition"}
_JAVA_CALLABLE_NODES = {
    "method_declaration", "constructor_declaration",
    "compact_constructor_declaration",
}
_PY_NESTED_BOUNDARIES = {
    "function_definition", "class_definition", "lambda",
}
_JAVA_NESTED_BOUNDARIES = {
    *_JAVA_CALLABLE_NODES, "class_declaration", "interface_declaration",
    "enum_declaration", "record_declaration", "lambda_expression",
}


@dataclass
class _JavaBinding:
    """One Java lexical binding and the exact region where its name is visible.

    Java permits a field to be shadowed and permits the same local name in
    disjoint scopes.  A flat ``name -> symbol`` map therefore loses facts.  The
    byte offsets here are only used to associate uses during this extraction;
    they are deliberately not part of persistent identity.
    """

    name_node: object
    scope_node: object
    visible_from: int
    visible_to: int
    kind: str
    sym: Sym | None = None


def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _span(source: bytes, node) -> tuple[int, int, int, int]:
    return (
        node.start_point[0] + 1,
        byte_column(source, node.start_byte),
        node.end_point[0] + 1,
        byte_column(source, node.end_byte),
    )


def _contains(outer, inner) -> bool:
    return outer.start_byte <= inner.start_byte and inner.end_byte <= outer.end_byte


def _walk(node, *, boundaries: set[str] | None = None,
          root=None) -> Iterable:
    """Depth-first walk, optionally excluding nested lexical boundaries."""
    yield node
    for child in node.named_children:
        if boundaries and child is not root and child.type in boundaries:
            continue
        yield from _walk(child, boundaries=boundaries, root=root)


def _target_identifiers(node, language: str) -> list:
    """Identifiers that a syntactic binding pattern defines."""
    if node is None:
        return []
    if node.type == "identifier":
        return [node]
    if language == "python":
        if node.type in {
            "pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern",
            "dictionary_splat_pattern", "as_pattern",
        }:
            out: list = []
            for child in node.named_children:
                out.extend(_target_identifiers(child, language))
            return out
        return []
    if node.type in {"variable_declarator", "formal_parameter", "spread_parameter"}:
        name = node.child_by_field_name("name")
        if name is not None:
            return [name]
        declarator = next(
            (child for child in node.named_children
             if child.type == "variable_declarator"), None)
        return _target_identifiers(declarator, language)
    return []


class _Augmenter:
    def __init__(self, language: str, source: bytes, tree, syms: list[Sym],
                 refs: list[Ref]) -> None:
        self.language = language
        self.source = source
        self.tree = tree
        self.syms = syms
        self.refs = refs
        self.file_sym = next(sym for sym in syms if sym.kind == "file")
        self._keys = {(sym.fqn, sym.kind) for sym in syms}
        self._declaration_spans: set[tuple[int, int]] = set()

    def _sym_for_node(self, node, kinds: set[str]) -> Sym | None:
        wanted = _span(self.source, node)
        exact = [sym for sym in self.syms if sym.kind in kinds and (
            sym.start_line, sym.start_col, sym.end_line, sym.end_col) == wanted]
        if exact:
            return exact[0]
        # Error-tolerant trees can make an extractor choose a child span.  A
        # uniquely-contained declaration is still safe.
        contained = [sym for sym in self.syms if sym.kind in kinds and
                     (wanted[0], wanted[1]) <= (sym.start_line, sym.start_col) and
                     (sym.end_line, sym.end_col) <= (wanted[2], wanted[3])]
        return contained[0] if len(contained) == 1 else None

    def _add_ref(self, kind: str, src_fqn: str | None, dst_fqn: str,
                 site) -> None:
        self.refs.append(Ref(
            kind=kind,
            src_fqn=src_fqn,
            dst_name=dst_fqn,
            line=site.start_point[0] + 1,
            col=byte_column(self.source, site.start_byte),
            confidence="certain",
            resolver="l0",
        ))

    def _add_variable(self, owner: Sym, name_node, kind: str,
                       signature: str | None = None,
                       definition_owner: Sym | None = None,
                       fqn: str | None = None) -> Sym:
        name = _text(self.source, name_node)
        fqn = fqn or f"{owner.fqn}.{name}"
        key = (fqn, kind)
        if key in self._keys:
            return next(sym for sym in self.syms
                        if sym.fqn == fqn and sym.kind == kind)
        start_line, start_col, end_line, end_col = _span(self.source, name_node)
        sym = Sym(
            kind=kind,
            name=name,
            fqn=fqn,
            parent_fqn=owner.fqn,
            signature=signature,
            doc=None,
            start_line=start_line,
            start_col=start_col,
            end_line=end_line,
            end_col=end_col,
            body_hash=content_hash(
                self.source[name_node.start_byte:name_node.end_byte]),
            visibility=None,
        )
        self.syms.append(sym)
        self._keys.add(key)
        self._declaration_spans.add((name_node.start_byte, name_node.end_byte))
        source = definition_owner or owner
        self._add_ref("defines", source.fqn, sym.fqn, name_node)
        self._add_ref("writes", source.fqn, sym.fqn, name_node)
        return sym

    def _python_parameters(self, callable_node) -> list:
        params = callable_node.child_by_field_name("parameters")
        if params is None:
            return []
        out: list = []
        for param in params.named_children:
            name = param.child_by_field_name("name")
            if name is not None:
                out.extend(_target_identifiers(name, "python"))
                continue
            if param.type == "identifier":
                out.append(param)
                continue
            # typed_parameter has no `name` field in tree-sitter-python; its
            # first named child is the binding pattern and the `type` field is
            # explicitly excluded.
            type_node = param.child_by_field_name("type")
            candidate = next(
                (child for child in param.named_children
                 if child is not type_node and child.type != "default_parameter"),
                None,
            )
            out.extend(_target_identifiers(candidate, "python"))
        return out

    def _python_locals(self, callable_node) -> list:
        body = callable_node.child_by_field_name("body")
        if body is None:
            return []
        out: list = []
        for node in _walk(body, boundaries=_PY_NESTED_BOUNDARIES, root=body):
            target = None
            if node.type in {"assignment", "augmented_assignment",
                             "named_expression"}:
                target = node.child_by_field_name("left")
                if target is None:
                    target = node.child_by_field_name("name")
            elif node.type in {"for_statement", "for_in_clause"}:
                target = node.child_by_field_name("left")
            elif node.type == "with_item":
                target = node.child_by_field_name("alias")
            elif node.type == "except_clause":
                alias = next((child for child in node.named_children
                              if child.type == "as_pattern"), None)
                target = alias
            if target is not None:
                out.extend(_target_identifiers(target, "python"))
        return out

    def _java_parameters(self, callable_node) -> list:
        params = next((child for child in callable_node.named_children
                       if child.type == "formal_parameters"), None)
        if params is None:
            return []
        out: list = []
        for param in params.named_children:
            if param.type in {"formal_parameter", "spread_parameter"}:
                out.extend(_target_identifiers(param, "java"))
        return out

    def _java_bindings(self, callable_node) -> list[_JavaBinding]:
        body = callable_node.child_by_field_name("body")
        if body is None:
            return []
        out = [
            _JavaBinding(
                name_node=name_node,
                scope_node=body,
                visible_from=body.start_byte,
                visible_to=body.end_byte,
                kind="parameter",
            )
            for name_node in self._java_parameters(callable_node)
        ]
        for node in _walk(body, boundaries=_JAVA_NESTED_BOUNDARIES, root=body):
            if node.type == "local_variable_declaration":
                for child in node.named_children:
                    if child.type != "variable_declarator":
                        continue
                    names = _target_identifiers(child, "java")
                    if not names:
                        continue
                    name_node = names[0]
                    scope = node.parent
                    while (scope is not None and scope != callable_node
                           and scope.type not in {
                               "block", "constructor_body", "for_statement",
                               "switch_block", "switch_block_statement_group",
                           }):
                        scope = scope.parent
                    scope = scope or body
                    out.append(_JavaBinding(
                        name_node=name_node,
                        scope_node=scope,
                        # A local is in scope in its own initializer and through
                        # the remainder of its declaring block/for statement.
                        visible_from=name_node.end_byte,
                        visible_to=scope.end_byte,
                        kind="local",
                    ))
            elif node.type == "enhanced_for_statement":
                name = node.child_by_field_name("name")
                loop_body = node.child_by_field_name("body")
                if name is not None and loop_body is not None:
                    out.append(_JavaBinding(
                        name_node=name,
                        scope_node=loop_body,
                        # The iteration variable is not visible in the iterable.
                        visible_from=loop_body.start_byte,
                        visible_to=loop_body.end_byte,
                        kind="local",
                    ))
            elif node.type == "resource":
                name = node.child_by_field_name("name")
                statement = node.parent
                while (statement is not None and statement != callable_node
                       and statement.type != "try_with_resources_statement"):
                    statement = statement.parent
                try_body = (statement.child_by_field_name("body")
                            if statement is not None else None)
                if name is not None and statement is not None and try_body is not None:
                    out.append(_JavaBinding(
                        name_node=name,
                        scope_node=statement,
                        # Resources are visible to later resource initializers
                        # and the try body, but not to catch/finally clauses.
                        visible_from=name.end_byte,
                        visible_to=try_body.end_byte,
                        kind="local",
                    ))
            elif node.type == "catch_formal_parameter":
                name = node.child_by_field_name("name")
                catch_clause = node.parent
                catch_body = (catch_clause.child_by_field_name("body")
                              if catch_clause is not None else None)
                if name is not None and catch_body is not None:
                    out.append(_JavaBinding(
                        name_node=name,
                        scope_node=catch_body,
                        visible_from=catch_body.start_byte,
                        visible_to=catch_body.end_byte,
                        kind="local",
                    ))
        return out

    def _python_instance_fields(self, callable_node, owner: Sym) -> dict[str, Sym]:
        if not owner.parent_fqn:
            return {}
        class_sym = next((sym for sym in self.syms
                          if sym.fqn == owner.parent_fqn
                          and sym.kind in {"class", "interface"}), None)
        if class_sym is None:
            return {}
        body = callable_node.child_by_field_name("body")
        if body is None:
            return {}
        fields: dict[str, Sym] = {}
        for node in _walk(body, boundaries=_PY_NESTED_BOUNDARIES, root=body):
            if node.type not in {"assignment", "augmented_assignment"}:
                continue
            left = node.child_by_field_name("left")
            if left is None or left.type != "attribute":
                continue
            obj = left.child_by_field_name("object")
            attr = left.child_by_field_name("attribute")
            if (obj is None or attr is None
                    or _text(self.source, obj) not in {"self", "cls"}):
                continue
            field = self._add_variable(
                class_sym, attr, "field", definition_owner=owner)
            fields.setdefault(field.name, field)
        return fields

    def _mode(self, node) -> set[str]:
        parent = node.parent
        while parent is not None:
            if self.language == "python" and parent.type in {
                    "assignment", "augmented_assignment", "named_expression",
            }:
                left = (parent.child_by_field_name("left") or
                        parent.child_by_field_name("name"))
                if left is not None and _contains(left, node):
                    if left.type == "attribute":
                        attribute = left.child_by_field_name("attribute")
                        if node != attribute:
                            return {"reads"}
                    elif left.type == "subscript":
                        return {"reads"}
                    return ({"reads", "writes"}
                            if parent.type == "augmented_assignment" else {"writes"})
                return {"reads"}
            if self.language == "java" and parent.type == "assignment_expression":
                left = parent.child_by_field_name("left")
                if left is not None and _contains(left, node):
                    operator = next((child for child in parent.children
                                     if not child.is_named), None)
                    return {"reads", "writes"} if (
                        operator is not None and operator.text != b"=") else {"writes"}
                return {"reads"}
            if parent.type == "update_expression":
                return {"reads", "writes"}
            if parent.type in (_PY_NESTED_BOUNDARIES | _JAVA_NESTED_BOUNDARIES):
                break
            parent = parent.parent
        return {"reads"}

    def _nonlocal_visible(self, owner: Sym, name: str) -> Sym | None:
        """Nearest closure binding or class member visible from ``owner``."""
        parent_fqn = owner.parent_fqn
        seen: set[str] = set()
        while parent_fqn and parent_fqn not in seen:
            seen.add(parent_fqn)
            candidate = next((
                sym for sym in self.syms
                if sym.parent_fqn == parent_fqn and sym.name == name
                and sym.kind in _VARIABLE_KINDS
            ), None)
            if candidate is not None:
                return candidate
            parent = next((sym for sym in self.syms
                           if sym.fqn == parent_fqn), None)
            parent_fqn = parent.parent_fqn if parent is not None else None
        return None

    def _selected_variable(self, owner: Sym, variables: dict[str, Sym], node) -> Sym | None:
        name = _text(self.source, node)
        if name in variables:
            return variables[name]
        selected = self._nonlocal_visible(owner, name)
        if selected is None:
            return None
        parent = node.parent
        selected_parent = next((sym for sym in self.syms
                                if sym.fqn == selected.parent_fqn), None)
        if (self.language == "python" and selected_parent is not None
                and selected_parent.kind in {"class", "interface"}):
            if parent is None or parent.type != "attribute":
                return None
            attribute = parent.child_by_field_name("attribute")
            obj = parent.child_by_field_name("object")
            if (attribute != node or obj is None
                    or _text(self.source, obj) not in {"self", "cls"}):
                return None
        if self.language == "java" and parent is not None and parent.type == "field_access":
            field_node = parent.child_by_field_name("field")
            obj = parent.child_by_field_name("object")
            if field_node == node and (obj is None or _text(self.source, obj) != "this"):
                return None
        return selected

    def _selected_java_variable(self, owner: Sym,
                                bindings: list[_JavaBinding], node) -> Sym | None:
        name = _text(self.source, node)
        parent = node.parent
        if parent is not None and parent.type == "field_access":
            field_node = parent.child_by_field_name("field")
            if field_node == node:
                obj = parent.child_by_field_name("object")
                obj_text = _text(self.source, obj) if obj is not None else ""
                if obj_text == "this":
                    return self._nonlocal_visible(owner, name)
                return None

        # Method/constructor/type names are identifiers too, but are not value
        # uses even when a local with the same spelling exists.
        if parent is not None:
            if (parent.type == "method_invocation"
                    and parent.child_by_field_name("name") == node):
                return None
            if (parent.type in {"method_reference", "object_creation_expression"}
                    and parent.child_by_field_name("name") == node):
                return None

        point = node.start_byte
        candidates = [
            binding for binding in bindings
            if binding.sym is not None
            and _text(self.source, binding.name_node) == name
            and binding.visible_from <= point < binding.visible_to
            and _contains(binding.scope_node, node)
        ]
        if candidates:
            # Invalid/error-tolerant source may expose overlapping bindings.  In
            # that case the narrowest lexical scope is the honest Java choice.
            selected = min(
                candidates,
                key=lambda binding: (
                    binding.scope_node.end_byte - binding.scope_node.start_byte,
                    -binding.visible_from,
                ),
            )
            return selected.sym
        return self._nonlocal_visible(owner, name)

    def _usage_facts(self, callable_node, owner: Sym,
                     variables: dict[str, Sym],
                     java_bindings: list[_JavaBinding] | None = None) -> None:
        boundaries = (_PY_NESTED_BOUNDARIES if self.language == "python"
                      else _JAVA_NESTED_BOUNDARIES)
        for node in _walk(callable_node, boundaries=boundaries, root=callable_node):
            if node.type != "identifier":
                continue
            if (node.start_byte, node.end_byte) in self._declaration_spans:
                continue
            variable = (self._selected_java_variable(owner, java_bindings, node)
                        if java_bindings is not None
                        else self._selected_variable(owner, variables, node))
            if variable is None:
                continue
            for kind in self._mode(node):
                self._add_ref(kind, owner.fqn, variable.fqn, node)
            parent = node.parent
            while parent is not None and parent != callable_node:
                if parent.type == "return_statement":
                    self._add_ref("returns", variable.fqn, owner.fqn, node)
                    break
                if parent.type in boundaries:
                    break
                parent = parent.parent

    def _callable(self, node) -> None:
        owner = self._sym_for_node(node, _CALLABLE_KINDS)
        if owner is None:
            return
        variables: dict[str, Sym] = {}
        if self.language == "python":
            self._python_instance_fields(node, owner)
            for name_node in self._python_parameters(node):
                sym = self._add_variable(owner, name_node, "parameter")
                variables.setdefault(sym.name, sym)
            for name_node in self._python_locals(node):
                name = _text(self.source, name_node)
                if name in variables:
                    continue
                sym = self._add_variable(owner, name_node, "local")
                variables[name] = sym
            self._usage_facts(node, owner, variables)
            return

        java_bindings = self._java_bindings(node)
        totals: dict[str, int] = {}
        occurrences: dict[str, int] = {}
        for binding in java_bindings:
            name = _text(self.source, binding.name_node)
            totals[name] = totals.get(name, 0) + 1
        for binding in java_bindings:
            name = _text(self.source, binding.name_node)
            occurrence = occurrences.get(name, 0) + 1
            occurrences[name] = occurrence
            # Preserve the established FQN for the common unique case.  For
            # homonyms, a source-order occurrence suffix makes every edge
            # resolvable while remaining stable across line/body edits and
            # insertion of declarations with other names.  Without persistent
            # lexical-scope nodes, inserting/reordering an earlier homonym is
            # inherently identity-changing in this L0 model.
            fqn = f"{owner.fqn}.{name}"
            if totals[name] > 1 and occurrence > 1:
                fqn += f"#{occurrence}"
            binding.sym = self._add_variable(
                owner, binding.name_node, binding.kind, fqn=fqn)
        self._usage_facts(node, owner, variables, java_bindings)

    def run(self) -> None:
        if self.language == "python":
            for decorated in _walk(self.tree.root_node):
                if decorated.type != "decorated_definition":
                    continue
                definition = decorated.child_by_field_name("definition")
                if definition is None or definition.type != "function_definition":
                    continue
                decorators = [
                    _text(self.source, child).lstrip("@").split("(", 1)[0].strip()
                    for child in decorated.named_children
                    if child.type == "decorator"
                ]
                if not any(value in {
                        "property", "cached_property", "functools.cached_property",
                } for value in decorators):
                    continue
                prop = self._sym_for_node(definition, {"function", "method"})
                if prop is not None:
                    self._keys.discard((prop.fqn, prop.kind))
                    prop.kind = "property"
                    self._keys.add((prop.fqn, prop.kind))
        class_fqns = {sym.fqn for sym in self.syms
                      if sym.kind in {"class", "interface", "enum"}}
        for sym in self.syms:
            if sym.kind == "variable" and sym.parent_fqn in class_fqns:
                sym.kind = "field"
        callable_nodes = (_PY_CALLABLE_NODES if self.language == "python"
                          else _JAVA_CALLABLE_NODES)
        for node in _walk(self.tree.root_node):
            if node.type in callable_nodes:
                self._callable(node)

        # Containment is an explicit graph edge as well as parent_id.  The edge
        # makes the same relation available to generic graph consumers.
        known_fqns = {sym.fqn for sym in self.syms}
        for sym in list(self.syms):
            if sym.kind == "file":
                continue
            parent = sym.parent_fqn or self.file_sym.fqn
            if parent in known_fqns:
                self.refs.append(Ref(
                    kind="contains", src_fqn=parent, dst_name=sym.fqn,
                    line=sym.start_line, col=sym.start_col,
                    confidence="certain", resolver="l0",
                ))


def enrich_structural(language: str, source: bytes, tree, syms: list[Sym],
                      refs: list[Ref]) -> None:
    """Add shared structural facts in place for Java and Python."""
    if language not in {"java", "python"}:
        return
    _Augmenter(language, source, tree, syms, refs).run()
