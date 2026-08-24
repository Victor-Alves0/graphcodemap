"""Conservative, syntax-proven Java framework declarations.

This module intentionally models only explicit Spring annotations.  It is
shared by the graph extractor and data-flow facts so navigation and SAST do not
disagree about what constitutes a controller entry point.
"""

from __future__ import annotations

from collections.abc import Mapping

from .util import byte_column


SPRING_COMPONENTS = frozenset({
    "org.springframework.stereotype.Component",
    "org.springframework.stereotype.Controller",
    "org.springframework.stereotype.Repository",
    "org.springframework.stereotype.Service",
    "org.springframework.web.bind.annotation.RestController",
    "org.springframework.context.annotation.Configuration",
})
SPRING_CONTROLLERS = frozenset({
    "org.springframework.stereotype.Controller",
    "org.springframework.web.bind.annotation.RestController",
})
SPRING_MAPPINGS = frozenset({
    "org.springframework.web.bind.annotation.RequestMapping",
    "org.springframework.web.bind.annotation.GetMapping",
    "org.springframework.web.bind.annotation.PostMapping",
    "org.springframework.web.bind.annotation.PutMapping",
    "org.springframework.web.bind.annotation.PatchMapping",
    "org.springframework.web.bind.annotation.DeleteMapping",
})
SPRING_CALLBACKS = frozenset({
    "org.springframework.context.event.EventListener",
    "org.springframework.transaction.event.TransactionalEventListener",
    "org.springframework.scheduling.annotation.Scheduled",
})
SPRING_WEB_INPUTS = frozenset({
    "org.springframework.web.bind.annotation.RequestParam",
    "org.springframework.web.bind.annotation.PathVariable",
    "org.springframework.web.bind.annotation.RequestHeader",
    "org.springframework.web.bind.annotation.CookieValue",
    "org.springframework.web.bind.annotation.RequestBody",
    "org.springframework.web.bind.annotation.ModelAttribute",
    "org.springframework.web.bind.annotation.RequestPart",
    "org.springframework.web.bind.annotation.MatrixVariable",
})
SPRING_CONFIGURATION = "org.springframework.context.annotation.Configuration"
SPRING_BEAN = "org.springframework.context.annotation.Bean"


def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def explicit_imports(source: bytes, node) -> dict[str, str]:
    """Return simple-name aliases proven by Java single-type imports."""
    root = node
    while root.parent is not None:
        root = root.parent
    aliases: dict[str, str] = {}
    ambiguous: set[str] = set()
    for declaration in root.named_children:
        if declaration.type != "import_declaration":
            continue
        raw = _text(source, declaration)
        if " static " in f" {raw} " or "*" in raw:
            continue
        target = next(
            (child for child in declaration.named_children
             if child.type == "scoped_identifier"),
            None,
        )
        if target is None:
            continue
        fqn = _text(source, target)
        simple = fqn.rsplit(".", 1)[-1]
        previous = aliases.get(simple)
        if previous is not None and previous != fqn:
            ambiguous.add(simple)
        else:
            aliases[simple] = fqn
    for simple in ambiguous:
        aliases.pop(simple, None)
    return aliases


def annotation_fqns(
        source: bytes, node, aliases: Mapping[str, str]
) -> frozenset[str]:
    """Resolve annotation owners only through an FQN or explicit import."""
    modifiers = next(
        (child for child in node.children if child.type == "modifiers"),
        None,
    )
    if modifiers is None:
        return frozenset()
    out: set[str] = set()
    for annotation in modifiers.named_children:
        if annotation.type not in ("annotation", "marker_annotation"):
            continue
        name = annotation.child_by_field_name("name")
        if name is None:
            continue
        raw = _text(source, name)
        if "." in raw:
            out.add(raw)
        elif raw in aliases:
            out.add(aliases[raw])
    return frozenset(out)


def spring_entry_kind(
        roles: frozenset[str], annotations: frozenset[str]
) -> str | None:
    if roles.intersection(SPRING_CONTROLLERS) and annotations.intersection(
            SPRING_MAPPINGS):
        return "controller"
    if roles.intersection(SPRING_COMPONENTS) and annotations.intersection(
            SPRING_CALLBACKS):
        return "callback"
    if SPRING_CONFIGURATION in roles and SPRING_BEAN in annotations:
        return "bean"
    return None


def spring_web_parameter_sources(source: bytes, method) -> tuple[tuple, ...]:
    """Return explicitly request-bound parameters of a proven controller.

    Each item is ``(name, annotation_fqn, line, col, byte_span)``. Unannotated
    MVC arguments, custom composed annotations and unmanaged classes remain
    unmodeled rather than being treated as attacker-controlled by convention.
    """
    aliases = explicit_imports(source, method)
    owner = method.parent
    while owner is not None and owner.type not in {
            "class_declaration", "record_declaration"}:
        owner = owner.parent
    if owner is None:
        return ()
    roles = annotation_fqns(source, owner, aliases)
    annotations = annotation_fqns(source, method, aliases)
    if not (roles.intersection(SPRING_CONTROLLERS)
            and annotations.intersection(SPRING_MAPPINGS)):
        return ()
    params = method.child_by_field_name("parameters")
    if params is None:
        return ()
    out = []
    for parameter in params.named_children:
        names = annotation_fqns(source, parameter, aliases)
        source_annotation = next(
            (name for name in sorted(names) if name in SPRING_WEB_INPUTS),
            None,
        )
        name_node = parameter.child_by_field_name("name")
        if source_annotation is None or name_node is None:
            continue
        def resolved_annotation(child) -> str | None:
            name = child.child_by_field_name("name")
            if name is None:
                return None
            raw = _text(source, name)
            return raw if "." in raw else aliases.get(raw)

        annotation_node = next((
            child for modifiers in parameter.children
            if modifiers.type == "modifiers"
            for child in modifiers.named_children
            if child.type in ("annotation", "marker_annotation")
            and resolved_annotation(child) == source_annotation
        ), None)
        site = annotation_node or name_node
        out.append((
            _text(source, name_node), source_annotation,
            site.start_point[0] + 1, byte_column(source, site.start_byte),
            (site.start_byte, site.end_byte),
        ))
    return tuple(out)
