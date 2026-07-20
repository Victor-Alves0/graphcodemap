"""Camada de dataflow (CPG-lite): análise intra-procedural de fluxo de dados.

Arquitetura (docs/RESEARCH.md §6): esqueleto de Code Property Graph pragmático
e incremental, estilo Semgrep — não whole-program (Joern) nem IFDS pesado.

Duas partes:
- EXTRAÇÃO DE FATOS por linguagem (`extract_facts`): normaliza o corpo de uma
  função em params, atribuições, chamadas e returns — abstraindo a gramática.
- MOTOR DE TAINT compartilhado (`analyze_facts`): fixpoint may-taint
  (flow-insensitive → over-aproxima, lado seguro p/ segurança); sanitizers
  cortam a propagação; sources semeiam.

INTER-procedural fica no query.py, compondo estes sumários ao longo do call
graph. Computado sob demanda do código no disco (sempre fresco). Suporte:
Python e JavaScript/TypeScript.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Config de extração de fatos por linguagem. As irregularidades das gramáticas
# ficam AQUI (declarativas); o motor de taint continua compartilhado.
#   func    : node types de função
#   id      : node types-folha que são referência de variável
#   params  : ("field",nome) | ("children",{types}) | ("search",{container_types})
#   assigns : passos ("lr",{t},lf,rf) | ("decl",{t},nf,vf) | ("decl_last",{t},nf|None)
#             | ("lr",{t},lf,rf) com left/right em expression_list (tratado igual)
#             | ("bytype",{t},left_ctype,right_ctype)
#   calls   : node types de chamada
#   returns : node types de return; tail=True se a última expr do corpo é retorno
_JS_FUNCS = {"function_declaration", "arrow_function", "method_definition",
             "function_expression", "generator_function_declaration",
             "generator_function"}

GEN: dict[str, dict] = {
    "java": {"func": {"method_declaration", "constructor_declaration"},
             "id": {"identifier"}, "params": ("field", "parameters"),
             "assigns": [("lr", {"assignment_expression"}, "left", "right"),
                         ("decl", {"variable_declarator"}, "name", "value")],
             "calls": {"method_invocation", "object_creation_expression"},
             "returns": {"return_statement"}, "tail": False},
    "csharp": {"func": {"method_declaration", "constructor_declaration",
                        "local_function_statement"},
               "id": {"identifier"}, "params": ("field", "parameters"),
               "assigns": [("lr", {"assignment_expression"}, "left", "right"),
                           ("decl_last", {"variable_declarator"}, "name")],
               "calls": {"invocation_expression", "object_creation_expression"},
               "returns": {"return_statement"}, "tail": False},
    "c": {"func": {"function_definition"}, "id": {"identifier"},
          "params": ("search", {"parameter_list"}),
          "assigns": [("decl", {"init_declarator"}, "declarator", "value"),
                      ("lr", {"assignment_expression"}, "left", "right")],
          "calls": {"call_expression"}, "returns": {"return_statement"},
          "tail": False},
    "php": {"func": {"function_definition", "method_declaration"},
            "id": {"name"}, "params": ("field", "parameters"),
            "assigns": [("lr", {"assignment_expression",
                                "augmented_assignment_expression"}, "left", "right")],
            "calls": {"function_call_expression", "member_call_expression",
                      "scoped_call_expression", "object_creation_expression"},
            "returns": {"return_statement"}, "tail": False},
    "rust": {"func": {"function_item"}, "id": {"identifier"},
             "params": ("field", "parameters"),
             "assigns": [("decl", {"let_declaration"}, "pattern", "value"),
                         ("lr", {"assignment_expression"}, "left", "right")],
             "calls": {"call_expression"}, "returns": {"return_expression"},
             "tail": True},
    "go": {"func": {"function_declaration", "method_declaration"},
           "id": {"identifier"}, "params": ("field", "parameters"),
           "assigns": [("lr", {"short_var_declaration", "assignment_statement"},
                        "left", "right"),
                       ("decl", {"var_spec"}, "name", "value")],
           "calls": {"call_expression"}, "returns": {"return_statement"},
           "tail": False},
    "ruby": {"func": {"method", "singleton_method"},
             "id": {"identifier", "constant"}, "params": ("field", "parameters"),
             "assigns": [("lr", {"assignment", "operator_assignment"},
                          "left", "right")],
             "calls": {"call"}, "returns": {"return"}, "tail": True},
    "lua": {"func": {"function_declaration"}, "id": {"identifier"},
            "params": ("field", "parameters"),
            "assigns": [("bytype", {"assignment_statement"},
                         "variable_list", "expression_list")],
            "calls": {"function_call"}, "returns": {"return_statement"},
            "tail": False},
    "scala": {"func": {"function_definition"}, "id": {"identifier"},
              "params": ("field", "parameters"),
              "assigns": [("decl", {"val_definition", "var_definition"},
                           "pattern", "value"),
                          ("lr", {"assignment_expression"}, "left", "right")],
              "calls": {"call_expression"}, "returns": set(), "tail": True},
    "kotlin": {"func": {"function_declaration"}, "id": {"simple_identifier"},
               "params": ("search", {"function_value_parameters"}),
               "assigns": [("decl_last", {"property_declaration"}, None)],
               "calls": {"call_expression"}, "returns": {"jump_expression"},
               "tail": True},
    "swift": {"func": {"function_declaration", "init_declaration"},
              "id": {"simple_identifier"}, "params": ("children", {"parameter"}),
              "assigns": [("decl", {"property_declaration"}, "name", "value")],
              "calls": {"call_expression"},
              "returns": {"control_transfer_statement"}, "tail": True},
}
GEN["cpp"] = GEN["cuda"] = GEN["c"]
GEN["luau"] = GEN["lua"]

# família de gramática por linguagem
_FAMILY = {"python": "py", "javascript": "js", "typescript": "js", "tsx": "js",
           "clojure": "clj"}
for _l in GEN:
    _FAMILY[_l] = "gen"

_BODY_TYPES = {"block", "function_body", "statement_block", "compound_statement",
               "do_block", "statements", "body_statement", "statement_list"}


def _func_types(lang: str) -> set[str]:
    fam = _FAMILY.get(lang)
    if fam == "py":
        return {"function_definition"}
    if fam == "js":
        return _JS_FUNCS
    if fam == "clj":
        return {"list_lit"}
    return GEN[lang]["func"]


def _scope_stop(lang: str) -> set[str]:
    fam = _FAMILY.get(lang)
    if fam == "py":
        return {"function_definition", "lambda"}
    if fam == "js":
        return _JS_FUNCS
    if fam == "clj":
        return set()
    return GEN[lang]["func"] | {"lambda_literal", "lambda"}


def supported(lang: str) -> bool:
    return lang in _FAMILY


def supported_langs() -> list[str]:
    return sorted(_FAMILY)


# -- estruturas normalizadas --------------------------------------------------

@dataclass
class Assign:
    targets: set[str]
    rhs_ids: set[str]
    is_aug: bool
    rhs_call: str | None  # nome do callee se o RHS é uma única chamada
    line: int = 0


@dataclass
class CallSite:
    callee: str
    line: int
    args: list[tuple[int, set[str]]]  # (arg_index 0-based; -1=kwarg, ids)


@dataclass
class ReturnExpr:
    ids: set[str]
    top_call: str | None


@dataclass
class FnFacts:
    params: list[str]
    assigns: list[Assign] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    returns: list[ReturnExpr] = field(default_factory=list)


@dataclass
class ArgFlow:
    callee: str
    arg_index: int
    line: int
    via: str


@dataclass
class Flow:
    arg_flows: list[ArgFlow] = field(default_factory=list)
    reaches_return: bool = False


# -- helpers de árvore --------------------------------------------------------

def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _ids(source: bytes, node, out: set[str]) -> None:
    if node.type == "identifier":
        out.add(_text(source, node))
        return
    for c in node.named_children:
        _ids(source, c, out)


# -- field-sensitivity: caminhos de acesso ------------------------------------
# Um FATO tainted agora é um *caminho de acesso* (tupla), não um nome nu:
#   ("user",)              -> a variável inteira
#   ("user", "password")   -> só esse campo
# Regra de prefixo (ver `_is_tainted`): ler `a.b` está sujo se `a` OU `a.b`
# estiver sujo — marcar o objeto inteiro contamina os campos, mas marcar um
# campo NÃO contamina os irmãos. Profundidade limitada (truncar mantém o
# prefixo = super-aproximação segura). Caminho não-reconstruível cai no
# comportamento antigo (coleta os identificadores-base = profundidade 1).
MAX_PATH_DEPTH = 3
_NAMEISH = {"identifier", "property_identifier", "field_identifier",
            "simple_identifier", "name", "constant", "shorthand_property_identifier"}

_PY_MEMBER = {"attribute": ("object", "attribute"), "subscript": ("value", None)}
_JS_MEMBER = {"member_expression": ("object", "property"),
              "subscript_expression": ("object", None)}


def _chain_path(source, node, member):
    """Caminho de acesso de um id/cadeia-de-membros pura, ou None."""
    if node is None:
        return None
    t = node.type
    if t in _NAMEISH:
        return (_text(source, node),)
    spec = member.get(t)
    if spec is None:
        return None
    objf, fldf = spec
    base = _chain_path(source, node.child_by_field_name(objf), member)
    if base is None:
        return None
    if fldf is None:                       # subscript a[i]: descarta índice
        return base
    fld = node.child_by_field_name(fldf)
    if fld is None or fld.type not in _NAMEISH:
        return base                        # campo dinâmico a[expr] → base (conflita)
    return tuple((base + (_text(source, fld),))[:MAX_PATH_DEPTH])


def _paths(source, node, out: set, member) -> None:
    """Coleta os caminhos de acesso máximos lidos numa subárvore (py/js)."""
    if node is None:
        return
    t = node.type
    if t in _NAMEISH:
        out.add((_text(source, node),))
        return
    if t in member:
        p = _chain_path(source, node, member)
        if p is not None:
            out.add(p)
            return                          # não desce: caminho já é maximal
    for c in node.named_children:
        _paths(source, c, out, member)


def _target_paths(source, node, member) -> set:
    """Caminhos escritos por um alvo de atribuição (id, membro ou pattern)."""
    if node is None:
        return set()
    if node.type in ("pattern_list", "tuple_pattern", "list_pattern",
                     "array_pattern", "tuple", "expression_list"):
        out: set = set()
        for c in node.named_children:
            out |= _target_paths(source, c, member)
        return out
    p = _chain_path(source, node, member)
    return {p} if p is not None else set()


def _is_tainted(path, tainted) -> bool:
    """Prefixo: `a.b.c` sujo se qualquer prefixo (`a`, `a.b`, `a.b.c`) o estiver."""
    for i in range(1, len(path) + 1):
        if path[:i] in tainted:
            return True
    return False


def find_function_node(root, start_line: int, lang: str):
    types = _func_types(lang)
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in types and n.start_point[0] + 1 == start_line:
            return n
        stack.extend(reversed(n.named_children))
    return None


def _callee_name(source: bytes, fn, family: str) -> str:
    if fn is None:
        return "?"
    if fn.type == "identifier":
        return _text(source, fn)
    if fn.type == "attribute":  # python obj.attr
        attr = fn.child_by_field_name("attribute")
        if attr is not None:
            return _text(source, attr)
    if fn.type == "member_expression":  # js obj.prop
        prop = fn.child_by_field_name("property")
        if prop is not None:
            return _text(source, prop)
    return _text(source, fn).rsplit(".", 1)[-1].split("(", 1)[0].strip()


def _body_of(fn_node, family: str):
    return fn_node.child_by_field_name("body")


def _walk(node, kinds: set[str], stop: set[str], out: list) -> None:
    for c in node.named_children:
        if c.type in kinds:
            out.append(c)
        if c.type not in stop:
            _walk(c, kinds, stop, out)


# -- extração de fatos: Python ------------------------------------------------

def _param_name_py(source, node):
    t = node.type
    if t == "identifier":
        return _text(source, node)
    if t in ("default_parameter", "typed_default_parameter", "typed_parameter"):
        name = node.child_by_field_name("name")
        if name is not None:
            return _text(source, name)
        for c in node.named_children:
            if c.type == "identifier":
                return _text(source, c)
    if t in ("list_splat_pattern", "dictionary_splat_pattern"):
        for c in node.named_children:
            if c.type == "identifier":
                return _text(source, c)
    return None


def _assign_targets_py(source, left):
    if left.type == "identifier":
        return {_text(source, left)}
    if left.type in ("pattern_list", "tuple_pattern", "list_pattern"):
        out: set[str] = set()
        for c in left.named_children:
            out |= _assign_targets_py(source, c)
        return out
    return set()


def _facts_py(source, fn) -> FnFacts:
    params_node = fn.child_by_field_name("parameters")
    params = []
    if params_node is not None:
        for c in params_node.named_children:
            n = _param_name_py(source, c)
            if n:
                params.append(n)
    facts = FnFacts(params=params)
    body = fn.child_by_field_name("body")
    if body is None:
        return facts
    stop = {"function_definition", "lambda"}

    assigns: list = []
    _walk(body, {"assignment", "augmented_assignment"}, stop, assigns)
    for a in assigns:
        left = a.child_by_field_name("left")
        right = a.child_by_field_name("right")
        if left is None or right is None:
            continue
        rids: set = set()
        _paths(source, right, rids, _PY_MEMBER)
        rhs_call = (_callee_name(source, right.child_by_field_name("function"), "py")
                    if right.type == "call" else None)
        facts.assigns.append(Assign(_target_paths(source, left, _PY_MEMBER), rids,
                                    a.type == "augmented_assignment", rhs_call,
                                    a.start_point[0] + 1))

    calls: list = []
    _walk(body, {"call"}, stop, calls)
    for call in calls:
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        callee = _callee_name(source, call.child_by_field_name("function"), "py")
        cs = CallSite(callee, call.start_point[0] + 1, [])
        pos = 0
        for arg in args.named_children:
            if arg.type == "keyword_argument":
                val, idx = arg.child_by_field_name("value"), -1
            else:
                val, idx = arg, pos
                pos += 1
            ids: set = set()
            if val is not None:
                _paths(source, val, ids, _PY_MEMBER)
            cs.args.append((idx, ids))
        facts.calls.append(cs)

    rets: list = []
    _walk(body, {"return_statement"}, stop, rets)
    for r in rets:
        ids = set()
        _paths(source, r, ids, _PY_MEMBER)
        child = r.named_children[0] if r.named_children else None
        top_call = (_callee_name(source, child.child_by_field_name("function"), "py")
                    if child is not None and child.type == "call" else None)
        facts.returns.append(ReturnExpr(ids, top_call))
    return facts


# -- extração de fatos: JavaScript/TypeScript ---------------------------------

def _param_name_js(source, node):
    if node.type == "identifier":
        return _text(source, node)
    if node.type in ("required_parameter", "optional_parameter"):
        pat = node.child_by_field_name("pattern")
        if pat is not None and pat.type == "identifier":
            return _text(source, pat)
    for c in node.named_children:  # fallback: primeiro identifier
        if c.type == "identifier":
            return _text(source, c)
    return None


def _facts_js(source, fn) -> FnFacts:
    params_node = fn.child_by_field_name("parameters")
    params = []
    if params_node is not None:
        for c in params_node.named_children:
            n = _param_name_js(source, c)
            if n:
                params.append(n)
    facts = FnFacts(params=params)
    body = fn.child_by_field_name("body")
    if body is None:
        return facts
    stop = _JS_FUNCS

    def rhs_call_name(value):
        return (_callee_name(source, value.child_by_field_name("function"), "js")
                if value is not None and value.type == "call_expression" else None)

    decls: list = []
    _walk(body, {"variable_declarator"}, stop, decls)
    for d in decls:
        name = d.child_by_field_name("name")
        value = d.child_by_field_name("value")
        if name is None or value is None:
            continue
        targets = _target_paths(source, name, _JS_MEMBER)
        if not targets:
            continue
        rids: set = set()
        _paths(source, value, rids, _JS_MEMBER)
        facts.assigns.append(Assign(targets, rids, False,
                                    rhs_call_name(value), d.start_point[0] + 1))

    reassigns: list = []
    _walk(body, {"assignment_expression", "augmented_assignment_expression"},
          stop, reassigns)
    for a in reassigns:
        left = a.child_by_field_name("left")
        right = a.child_by_field_name("right")
        if left is None or right is None:
            continue
        targets = _target_paths(source, left, _JS_MEMBER)
        if not targets:
            continue
        rids = set()
        _paths(source, right, rids, _JS_MEMBER)
        facts.assigns.append(Assign(targets, rids,
                                    a.type == "augmented_assignment_expression",
                                    rhs_call_name(right), a.start_point[0] + 1))

    calls: list = []
    _walk(body, {"call_expression"}, stop, calls)
    for call in calls:
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        callee = _callee_name(source, call.child_by_field_name("function"), "js")
        cs = CallSite(callee, call.start_point[0] + 1, [])
        pos = 0
        for arg in args.named_children:
            if arg.type == "comment":
                continue
            ids: set = set()
            _paths(source, arg, ids, _JS_MEMBER)
            cs.args.append((pos, ids))
            pos += 1
        facts.calls.append(cs)

    rets: list = []
    _walk(body, {"return_statement"}, stop, rets)
    for r in rets:
        ids = set()
        _paths(source, r, ids, _JS_MEMBER)
        child = r.named_children[0] if r.named_children else None
        top_call = (_callee_name(source, child.child_by_field_name("function"), "js")
                    if child is not None and child.type == "call_expression" else None)
        facts.returns.append(ReturnExpr(ids, top_call))
    return facts


# -- extração de fatos: genérica dirigida por config (GEN) --------------------

_MEMBER_UNWRAP = {  # node de acesso a membro → field do último segmento
    "attribute": "attribute", "member_expression": "property",
    "field_expression": "field", "selector_expression": "field",
    "member_access_expression": "name", "member_call_expression": "name",
    "dot_index_expression": "field", "method_index_expression": "method",
    "scoped_call_expression": "name", "qualified_identifier": "name",
    "scoped_identifier": "name",
}


def _first_id(source, node, idset):
    if node is None:
        return None
    if node.type in idset:
        return _text(source, node)
    for c in node.named_children:
        r = _first_id(source, c, idset)
        if r:
            return r
    return None


def _ids_of(source, node, idset, out):
    if node is None:
        return
    if node.type in idset:
        out.add(_text(source, node))
        return
    for c in node.named_children:
        _ids_of(source, c, idset, out)


# Membros de acesso do tier genérico → (field do objeto, field do último
# segmento). Best-effort por gramática; se os fields não baterem, `_gen_chain`
# devolve o prefixo/None e `_gen_paths` cai no comportamento antigo (coleta os
# ids-base). Nunca perde um fluxo — no pior caso perde só a precisão de campo.
_GEN_MEMBER = {
    "field_expression": ("argument", "field"),          # C/C++  a.b / a->b
    "selector_expression": ("operand", "field"),        # Go     a.b
    "member_access_expression": ("object", "name"),     # C#/PHP a.b / $a->b
    "field_access": ("object", "field"),                # Java   a.b
    "dot_index_expression": ("table", "field"),         # Lua    a.b
    "attribute": ("object", "attribute"),               # genérico estilo-py
}


def _gen_chain(source, node, idset):
    if node is None:
        return None
    t = node.type
    if t in idset or t in _NAMEISH:
        return (_text(source, node),)
    spec = _GEN_MEMBER.get(t)
    if spec is None:
        return None
    objf, fldf = spec
    base = _gen_chain(source, node.child_by_field_name(objf), idset)
    if base is None:
        return None
    fld = node.child_by_field_name(fldf)
    if fld is None or (fld.type not in idset and fld.type not in _NAMEISH):
        return base
    return tuple((base + (_text(source, fld),))[:MAX_PATH_DEPTH])


def _gen_paths(source, node, idset, out) -> None:
    if node is None:
        return
    t = node.type
    if t in idset:
        out.add((_text(source, node),))
        return
    if t in _GEN_MEMBER:
        p = _gen_chain(source, node, idset)
        if p is not None:
            out.add(p)
            return
    for c in node.named_children:
        _gen_paths(source, c, idset, out)


def _gen_target_paths(source, node, idset) -> set:
    p = _gen_chain(source, node, idset)
    if p is not None:
        return {p}
    out: set = set()               # pattern/tupla → ids-base (profundidade 1)
    _gen_paths(source, node, idset, out)
    return out


def _callee_of(source, call_node, idset):
    fn = None
    for f in ("function", "name", "method", "target"):
        fn = call_node.child_by_field_name(f)
        if fn is not None:
            break
    if fn is None and call_node.named_children:
        fn = call_node.named_children[0]
    if fn is None:
        return "?"
    seen = 0
    while fn is not None and fn.type in _MEMBER_UNWRAP and seen < 6:
        nxt = fn.child_by_field_name(_MEMBER_UNWRAP[fn.type])
        if nxt is None:
            break
        fn, seen = nxt, seen + 1
    if fn.type == "navigation_expression":
        suf = fn.child_by_field_name("suffix")
        inner = suf.child_by_field_name("suffix") if suf is not None else None
        if inner is not None:
            return _text(source, inner)
    return _text(source, fn).rsplit(".", 1)[-1].split("(", 1)[0].strip()


_ARG_CONTAINERS = {"arguments", "argument_list", "value_arguments"}


def _args_of(call_node):
    for f in ("arguments",):
        a = call_node.child_by_field_name(f)
        if a is not None:
            return a
    stack = list(call_node.named_children)
    seen = 0
    while stack and seen < 40:
        n = stack.pop(0)
        seen += 1
        if n.type in _ARG_CONTAINERS:
            return n
        stack.extend(n.named_children)
    return None


def _arg_ids(source, arg, idset, out):
    # desce por wrappers (argument, value_argument, spread_element)
    if arg.type in ("argument", "value_argument", "spread_element",
                    "keyword_argument"):
        for c in arg.named_children:
            _ids_of(source, c, idset, out)
    else:
        _ids_of(source, arg, idset, out)


def _arg_paths(source, arg, idset, out):
    # idem _arg_ids, mas coletando caminhos de acesso (field-sensitive)
    if arg.type in ("argument", "value_argument", "spread_element",
                    "keyword_argument"):
        for c in arg.named_children:
            _gen_paths(source, c, idset, out)
    else:
        _gen_paths(source, arg, idset, out)


def _rhs_call(source, node, call_types, idset):
    """Se o RHS é (recursivamente) uma única chamada, devolve o callee."""
    seen = 0
    while node is not None and seen < 6:
        if node.type in call_types:
            return _callee_of(source, node, idset)
        if node.type in ("expression_list", "parenthesized_expression",
                         "argument_list", "await_expression"):
            kids = node.named_children
            if len(kids) == 1:
                node, seen = kids[0], seen + 1
                continue
        return None
    return None


def _params_generic(source, fn, cfg, idset):
    kind, spec = cfg["params"]
    containers = []
    if kind == "field":
        c = fn.child_by_field_name(spec)
        if c is not None:
            containers = [c]
    elif kind == "children":
        return [n for c in fn.named_children if c.type in spec
                for n in [_first_id(source, c.child_by_field_name("name") or c, idset)]
                if n]
    elif kind == "search":
        stack, seen = list(fn.named_children), 0
        while stack and seen < 30:
            n = stack.pop(0)
            seen += 1
            if n.type in spec:
                containers = [n]
                break
            if n.type not in _BODY_TYPES:
                stack.extend(n.named_children)
    out = []
    for cont in containers:
        for p in cont.named_children:
            if p.type in (",",):
                continue
            name = _first_id(source, p.child_by_field_name("name") or p, idset)
            if name:
                out.append(name)
    return out


def _body_of(fn):
    b = fn.child_by_field_name("body")
    if b is not None:
        return b
    for c in fn.named_children:
        if c.type in _BODY_TYPES:
            return c
    return None


def _facts_generic(source, fn, lang) -> FnFacts:
    cfg = GEN[lang]
    idset = cfg["id"]
    calls_t = cfg["calls"]
    facts = FnFacts(params=_params_generic(source, fn, cfg, idset))
    body = _body_of(fn)
    if body is None:
        return facts
    stop = _scope_stop(lang)

    # atribuições
    assign_types: set[str] = set()
    for step in cfg["assigns"]:
        assign_types |= step[1]
    nodes: list = []
    _walk(body, assign_types, stop, nodes)
    for n in nodes:
        step = next((s for s in cfg["assigns"] if n.type in s[1]), None)
        if step is None:
            continue
        kind = step[0]
        targets: set = set()
        rhs_node = None
        if kind == "lr":
            left = n.child_by_field_name(step[2])
            rhs_node = n.child_by_field_name(step[3])
            targets |= _gen_target_paths(source, left, idset)
        elif kind == "decl":
            nm = n.child_by_field_name(step[2])
            rhs_node = n.child_by_field_name(step[3])
            targets |= _gen_target_paths(source, nm, idset)
        elif kind == "decl_last":
            nm = n.child_by_field_name(step[2]) if step[2] else None
            if nm is None:
                nm = next((c for c in n.named_children
                           if c.type in ("variable_declaration", "variable_declarator")),
                          None)
            targets |= _gen_target_paths(source, nm, idset)
            kids = [c for c in n.named_children if c is not nm
                    and c.type not in ("binding_pattern_kind", "modifiers", "=")]
            rhs_node = kids[-1] if kids else None
        elif kind == "bytype":
            lc = next((c for c in n.named_children if c.type == step[2]), None)
            rc = next((c for c in n.named_children if c.type == step[3]), None)
            targets |= _gen_target_paths(source, lc, idset)
            rhs_node = rc
        if not targets or rhs_node is None:
            continue
        rids: set = set()
        _gen_paths(source, rhs_node, idset, rids)
        facts.assigns.append(Assign(targets, rids, False,
                                    _rhs_call(source, rhs_node, calls_t, idset),
                                    n.start_point[0] + 1))

    # chamadas
    calls: list = []
    _walk(body, calls_t, stop, calls)
    for call in calls:
        args = _args_of(call)
        callee = _callee_of(source, call, idset)
        cs = CallSite(callee, call.start_point[0] + 1, [])
        if args is not None:
            pos = 0
            for arg in args.named_children:
                if arg.type in (",", "comment"):
                    continue
                ids: set = set()
                _arg_paths(source, arg, idset, ids)
                cs.args.append((pos, ids))
                pos += 1
        facts.calls.append(cs)

    # returns explícitos
    rets: list = []
    if cfg["returns"]:
        _walk(body, cfg["returns"], stop, rets)
    for r in rets:
        ids: set = set()
        _gen_paths(source, r, idset, ids)
        child = r.named_children[0] if r.named_children else None
        top = (_rhs_call(source, child, calls_t, idset)
               if child is not None else None)
        facts.returns.append(ReturnExpr(ids, top))
    # expressão-cauda (Rust/Scala/Ruby/Kotlin/Swift): última expr do corpo
    if cfg.get("tail"):
        last = None
        for c in body.named_children:
            last = c
        # em Go/py o corpo tem statement_list; desce um nível se preciso
        while last is not None and last.type in ("statement_list",):
            kids = last.named_children
            last = kids[-1] if kids else None
        if last is not None and not last.type.endswith(
                ("_statement", "_declaration", "_definition", "_declarator")) \
                and last.type not in _BODY_TYPES and last.type not in cfg["returns"]:
            ids = set()
            _gen_paths(source, last, idset, ids)
            if ids:
                facts.returns.append(
                    ReturnExpr(ids, _rhs_call(source, last, calls_t, idset)))
    return facts


# -- extração de fatos: Clojure (Lisp) ----------------------------------------

# formas de binding: primeira vec_lit são pares [alvo expr alvo expr ...].
_CLJ_LET = {"let", "if-let", "when-let", "if-some", "when-some", "loop",
            "binding", "letfn", "with-open", "with-redefs", "with-local-vars",
            "for", "doseq", "when-first"}
# formas especiais que NÃO são aplicação de função (não geram CallSite).
_CLJ_SPECIAL = {"if", "if-not", "when", "when-not", "cond", "condp", "case",
                "cond->", "cond->>", "do", "doto", "fn", "fn*", "quote", "var",
                "new", "set!", "recur", "try", "catch", "finally", "throw",
                "and", "or", "not", "->", "->>", "some->", "some->>", "as->",
                "comment", "declare", "assert", "reify", "proxy", "dotimes",
                "while", "lazy-seq", "delay", "ns", "def", "defn", "defn-",
                "defmacro", "defmethod", "defmulti", "defprotocol", "defrecord",
                "deftype", "defonce", "defsetting", "definterface"} | _CLJ_LET


def _clj_sym(node):
    """(ns|None, nome|None) de um sym_lit (ignora meta_lit)."""
    ns = next((c for c in node.children if c.type == "sym_ns"), None)
    nm = next((c for c in node.children if c.type == "sym_name"), None)
    return ns, nm


def _clj_local_ids(source, node, out: set[str]) -> None:
    """Nomes de símbolos NÃO-qualificados (candidatos a variável local)."""
    if node.type == "sym_lit":
        ns, nm = _clj_sym(node)
        if ns is None and nm is not None:
            out.add(_text(source, nm))
        return
    for c in node.named_children:
        _clj_local_ids(source, c, out)


def _clj_local_paths(source, node, out: set) -> None:
    """Como _clj_local_ids, mas como caminhos profundidade-1 (Lisp: sem campos)."""
    names: set[str] = set()
    _clj_local_ids(source, node, names)
    for n in names:
        out.add((n,))


def _clj_callee(source, form):
    """Último segmento da cabeça de uma aplicação (list_lit), ou None."""
    if form.type != "list_lit":
        return None
    head = next((c for c in form.named_children), None)
    if head is None or head.type != "sym_lit":
        return None
    _, nm = _clj_sym(head)
    return _text(source, nm) if nm is not None else None


def _clj_arities(fn):
    """Gera (param_vec, [body_forms]) para cada aridade da defn."""
    kids = [c for c in fn.named_children][2:]  # pula 'defn' e o nome
    # pula docstring/metadata/attr-map antes dos params
    i = 0
    while i < len(kids) and kids[i].type in ("str_lit", "map_lit", "meta_lit"):
        i += 1
    rest = kids[i:]
    if rest and rest[0].type == "vec_lit":            # aridade única
        yield rest[0], rest[1:]
    else:                                             # multi-aridade
        for a in rest:
            if a.type != "list_lit":
                continue
            inner = [c for c in a.named_children]
            pv = next((c for c in inner if c.type == "vec_lit"), None)
            if pv is not None:
                yield pv, [c for c in inner if c is not pv]


def _facts_clojure(source, fn) -> FnFacts:
    facts = FnFacts(params=[])
    seen_params: set[str] = set()
    for pvec, body in _clj_arities(fn):
        pids: set[str] = set()
        _clj_local_ids(source, pvec, pids)
        for p in sorted(pids):
            if p not in seen_params:
                seen_params.add(p)
                facts.params.append(p)
        for form in body:
            _clj_facts_visit(source, form, facts)
        if body:                                      # valor de retorno = última forma
            last = body[-1]
            rids: set = set()
            _clj_local_paths(source, last, rids)
            facts.returns.append(ReturnExpr(rids, _clj_callee(source, last)))
    return facts


def _clj_facts_visit(source, node, facts: FnFacts) -> None:
    if node.type != "list_lit":
        return
    base = _clj_callee(source, node)
    if base is None:
        for c in node.named_children:
            _clj_facts_visit(source, c, facts)
        return
    if base in _CLJ_LET:
        vec = next((c for c in node.named_children if c.type == "vec_lit"), None)
        if vec is not None:
            pairs = [c for c in vec.named_children]
            for i in range(0, len(pairs) - 1, 2):
                tgt, expr = pairs[i], pairs[i + 1]
                targets: set = set()
                _clj_local_paths(source, tgt, targets)
                rids: set = set()
                _clj_local_paths(source, expr, rids)
                facts.assigns.append(Assign(targets, rids, False,
                                            _clj_callee(source, expr),
                                            expr.start_point[0] + 1))
                _clj_facts_visit(source, expr, facts)
        for c in node.named_children:
            if c is not vec:
                _clj_facts_visit(source, c, facts)
        return
    if base not in _CLJ_SPECIAL:                      # aplicação de função → CallSite
        cs = CallSite(base, node.start_point[0] + 1, [])
        for pos, arg in enumerate(node.named_children[1:]):
            ids: set = set()
            _clj_local_paths(source, arg, ids)
            cs.args.append((pos, ids))
        facts.calls.append(cs)
    for c in node.named_children[1:]:                 # desce em args/corpo
        _clj_facts_visit(source, c, facts)


def extract_facts(source: bytes, fn_node, lang: str) -> FnFacts:
    fam = _FAMILY[lang]
    if fam == "py":
        return _facts_py(source, fn_node)
    if fam == "js":
        return _facts_js(source, fn_node)
    if fam == "clj":
        return _facts_clojure(source, fn_node)
    return _facts_generic(source, fn_node, lang)


# -- motor de taint (compartilhado) -------------------------------------------

def analyze_facts(facts: FnFacts, tainted_init, sanitizers=frozenset()) -> Flow:
    """Fixpoint may-taint FIELD-SENSITIVE. O conjunto sujo guarda *caminhos de
    acesso* (tuplas); ler um caminho está sujo se ele ou qualquer prefixo seu
    estiver sujo (`_is_tainted`). `tainted_init` deve conter caminhos — um nome
    nu `x` é o caminho `("x",)`."""
    tainted: set = {p if isinstance(p, tuple) else (p,) for p in tainted_init}
    changed = True
    while changed:
        changed = False
        for a in facts.assigns:
            if a.rhs_call is not None and a.rhs_call in sanitizers:
                continue  # RHS sanitizado → alvo limpo
            rhs_hit = any(_is_tainted(p, tainted) for p in a.rhs_ids)
            aug_hit = a.is_aug and any(_is_tainted(t, tainted) for t in a.targets)
            if rhs_hit or aug_hit:
                for t in a.targets:
                    if t not in tainted:
                        tainted.add(t)
                        changed = True
    flow = Flow()
    for c in facts.calls:
        for idx, ids in c.args:
            hit = [p for p in ids if _is_tainted(p, tainted)]
            if hit:
                via = ".".join(sorted(hit)[0])
                flow.arg_flows.append(ArgFlow(c.callee, idx, c.line, via))
    for r in facts.returns:
        if r.top_call is not None and r.top_call in sanitizers:
            continue
        if any(_is_tainted(p, tainted) for p in r.ids):
            flow.reaches_return = True
            break
    return flow


def source_vars(facts: FnFacts, sources) -> set:
    """Caminhos cujo valor nasce de uma chamada a uma fonte (input não-confiável)."""
    out: set = set()
    for a in facts.assigns:
        if a.rhs_call is not None and a.rhs_call in sources:
            out |= a.targets
    return out


def source_sites(facts: FnFacts, sources) -> list[tuple]:
    """(caminho, linha, fonte) para cada atribuição a partir de uma fonte.
    O caminho é uma tupla (semente para o motor); renderize com '.'.join()."""
    out = []
    for a in facts.assigns:
        if a.rhs_call is not None and a.rhs_call in sources:
            for t in sorted(a.targets):
                out.append((t, a.line, a.rhs_call))
    return out
