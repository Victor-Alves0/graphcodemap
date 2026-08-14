"""Taint FLOW-SENSITIVE: o ambiente sujo caminha pelo fluxo de controle (P1).

O motor antigo (`dataflow.analyze_facts`) mantém UM conjunto `tainted`
monotônico para a função inteira: uma vez que `x` entra, nunca sai. Isso ignora
ordem e redefinição, e produz falso positivo no caso canônico

    x = input(); x = escape(x); sink(x)      # limpo, mas era reportado

Aqui o ambiente é avaliado POR PONTO DO PROGRAMA sobre uma CFG estruturada
(Python/JS não têm goto, então a estrutura da AST já é a CFG):

    Seq    executa em ordem          → transferência encadeada
    Branch um braço executa          → meet = UNIÃO das saídas (may-taint);
                                       sem `else` existe o caminho que pula,
                                       então o ambiente de entrada entra na união
    Loop   0..N iterações            → fixpoint, unido ao ambiente de entrada

Transferência de uma atribuição, no formato clássico `out = gen ∪ (in − kill)`:
RHS sujo → o alvo entra (gen); RHS limpo/sanitizado → o alvo SAI (kill). O kill
é FIELD-AWARE: reatribuir `x` mata `x.a`, `x.a.b`, … (regra do Joern — um objeto
novo não herda a sujeira dos campos do antigo).

Continua sendo may-taint (sujo em ALGUM caminho basta) e sem análise de alias —
a mesma fronteira de escopo do Semgrep/Opengrep. O ganho é só precisão de fluxo.

Referências estudadas (reimplementação limpa): Joern (Apache-2.0)
`passes/reachingdef` — gen/kill e kill ciente de campos; Opengrep (LGPL, apenas
estudo) `Taint_lval_env` — ambiente por-lvalue com add/clean.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from .dataflow import (ArgFlow, Flow, ReceiverEffect, ReceiverFlow, _is_tainted,
                       assign_reads_framework_source, assign_reads_named_source,
                       direct_named_source_args, direct_source_args,
                       instance_field_name)

# --- node types de controle de fluxo, por família de gramática ---------------
# `body`: filhos que são CORPOS (executam condicionalmente); o que não é corpo
# (a condição) executa incondicionalmente e é processado antes.
_CTRL = {
    "py": {
        "branch": {"if_statement", "try_statement", "match_statement",
                   "with_statement"},
        "loop": {"for_statement", "while_statement"},
        "body": {"block", "else_clause", "elif_clause", "except_clause",
                 "finally_clause", "case_clause", "match_block"},
    },
    "js": {
        "branch": {"if_statement", "try_statement", "switch_statement"},
        "loop": {"for_statement", "while_statement", "do_statement",
                 "for_in_statement", "for_of_statement"},
        "body": {"statement_block", "else_clause", "switch_body", "switch_case",
                 "switch_default", "catch_clause", "finally_clause"},
    },
    # --- família GEN: uma entrada por linguagem (node types verificados
    # parseando código real de cada gramática, não por suposição) ---
    "java": {
        "branch": {"if_statement", "try_statement", "try_with_resources_statement",
                   "switch_expression", "switch_statement"},
        "loop": {"for_statement", "enhanced_for_statement", "while_statement",
                 "do_statement"},
        "body": {"block", "switch_block", "switch_block_statement_group",
                 "catch_clause", "finally_clause"},
    },
    "go": {
        "branch": {"if_statement", "expression_switch_statement",
                   "type_switch_statement", "select_statement"},
        "loop": {"for_statement"},
        "body": {"block", "statement_list", "expression_case", "default_case",
                 "type_case", "communication_case"},
    },
    "c": {
        "branch": {"if_statement", "switch_statement", "try_statement"},
        "loop": {"for_statement", "while_statement", "do_statement",
                 "for_range_loop"},
        "body": {"compound_statement", "else_clause", "case_statement",
                 "catch_clause"},
    },
    "csharp": {
        "branch": {"if_statement", "try_statement", "switch_statement"},
        "loop": {"for_statement", "foreach_statement", "while_statement",
                 "do_statement"},
        "body": {"block", "catch_clause", "finally_clause", "switch_body",
                 "switch_section"},
    },
    "php": {
        "branch": {"if_statement", "try_statement", "switch_statement",
                   "match_expression"},
        "loop": {"for_statement", "foreach_statement", "while_statement",
                 "do_statement"},
        "body": {"compound_statement", "else_clause", "else_if_clause",
                 "catch_clause", "finally_clause", "switch_block",
                 "case_statement", "default_statement"},
    },
    "ruby": {
        "branch": {"if", "unless", "case", "begin"},
        "loop": {"while", "until", "for", "do_block"},
        "body": {"then", "else", "body_statement", "do_block", "block",
                 "ensure", "rescue", "when"},
    },
    "rust": {
        "branch": {"if_expression", "match_expression"},
        "loop": {"for_expression", "while_expression", "loop_expression"},
        "body": {"block", "else_clause", "match_block", "match_arm"},
    },
    "kotlin": {
        "branch": {"if_expression", "try_expression", "when_expression"},
        "loop": {"for_statement", "while_statement", "do_while_statement"},
        "body": {"control_structure_body", "catch_block", "finally_block",
                 "when_entry"},
    },
    "swift": {
        "branch": {"if_statement", "guard_statement", "switch_statement",
                   "do_statement"},
        "loop": {"for_statement", "while_statement", "repeat_while_statement"},
        "body": {"statements", "else", "switch_entry", "catch_block"},
    },
    "scala": {
        "branch": {"if_expression", "match_expression", "try_expression"},
        "loop": {"for_expression", "while_expression"},
        "body": {"block", "indented_block", "case_block", "case_clause",
                 "finally_clause", "catch_clause"},
    },
    "lua": {
        "branch": {"if_statement"},
        "loop": {"for_statement", "while_statement", "repeat_statement"},
        "body": {"block", "else_statement", "elseif_statement"},
    },
}
_CTRL["cpp"] = _CTRL["cuda"] = _CTRL["c"]
_CTRL["luau"] = _CTRL["lua"]


# --- regiões -----------------------------------------------------------------

@dataclass
class Span:
    """Trecho linear: os fatos contidos nele, em ordem de código."""
    start: int
    end: int


@dataclass
class Branch:
    arms: list                 # list[Seq]
    has_else: bool             # sem else existe o caminho que PULA o bloco
    # Índice do ÚNICO braço que executa, quando a condição é decidível em tempo
    # de compilação; -1 quando nenhum executa; None quando não dá para decidir
    # (o caso normal). Ver `fold_condition`.
    taken: int | None = None
    # Java containment guards of the form `if (!safe(candidate, base)) return`.
    # Each pair is (validated path, trusted base path). They apply only after
    # the rejecting arm, so a sink inside/before the guard remains visible.
    post_sanitizes: list[
        tuple[tuple[str, ...], tuple[str, ...], int]
    ] = field(
        default_factory=list)
    # Positive containment checks sanitize only their accepted arm.  This is
    # deliberately arm-local: the rejected/fall-through path stays tainted.
    arm_sanitizes: list[
        tuple[int, tuple[str, ...], tuple[str, ...]]
    ] = field(default_factory=list)


@dataclass
class Loop:
    body: object               # Seq


@dataclass
class Seq:
    items: list = field(default_factory=list)


def _ctrl(family: str):
    return _CTRL.get(family)


def _unwrap_parenthesized(node):
    while node is not None and node.type == "parenthesized_expression":
        kids = node.named_children
        if len(kids) != 1:
            return None
        node = kids[0]
    return node


def _java_value_path(source: bytes, node) -> tuple[str, ...] | None:
    """Identifier/field path used as a containment candidate or trusted base."""
    node = _unwrap_parenthesized(node)
    if node is None:
        return None
    if node.type in {"identifier", "type_identifier"}:
        return (source[node.start_byte:node.end_byte].decode("utf-8", "replace"),)
    if node.type != "field_access":
        return None
    obj = _java_value_path(source, node.child_by_field_name("object"))
    fld = node.child_by_field_name("field")
    if obj is None or fld is None:
        return None
    return obj + (source[fld.start_byte:fld.end_byte].decode("utf-8", "replace"),)


def _java_empty_call_chain(source: bytes, node):
    """Return (receiver path, methods) for a Java chain of zero-arg calls."""
    node = _unwrap_parenthesized(node)
    if node is None:
        return None
    if node.type != "method_invocation":
        path = _java_value_path(source, node)
        return (path, ()) if path is not None else None
    args = node.child_by_field_name("arguments")
    if args is None or args.named_children:
        return None
    obj = node.child_by_field_name("object")
    name = node.child_by_field_name("name")
    previous = _java_empty_call_chain(source, obj)
    if previous is None or name is None:
        return None
    method = source[name.start_byte:name.end_byte].decode("utf-8", "replace")
    return previous[0], previous[1] + (method,)


def _java_starts_with_call(source: bytes, node):
    node = _unwrap_parenthesized(node)
    if node is None or node.type != "method_invocation":
        return None
    name = node.child_by_field_name("name")
    obj = node.child_by_field_name("object")
    args = node.child_by_field_name("arguments")
    if name is None or obj is None or args is None:
        return None
    if source[name.start_byte:name.end_byte] != b"startsWith":
        return None
    values = args.named_children
    if len(values) != 1:
        return None
    return obj, values[0]


def _java_path_operand(source: bytes, node):
    chain = _java_empty_call_chain(source, node)
    if chain is None:
        return None
    path, methods = chain
    # Path component semantics, not String prefix semantics. `toPath` is
    # optional for values already declared as Path; normalization is required.
    allowed = {
        ("normalize",),
        ("normalize", "toAbsolutePath"),
        ("toAbsolutePath", "normalize"),
        ("toPath", "normalize"),
        ("toPath", "normalize", "toAbsolutePath"),
        ("toPath", "toAbsolutePath", "normalize"),
    }
    return (path, methods) if methods in allowed else None


def _java_canonical_base(source: bytes, node) -> tuple[str, ...] | None:
    """Base in `canonicalCandidate.startsWith(baseCanonical + separator)`."""
    node = _unwrap_parenthesized(node)
    if node is None or node.type != "binary_expression":
        return None
    parts = node.named_children
    if len(parts) != 2:
        return None
    between = source[parts[0].end_byte:parts[1].start_byte].strip()
    if between != b"+":
        return None
    separator = source[parts[1].start_byte:parts[1].end_byte].decode(
        "utf-8", "replace").replace(" ", "")
    if not (separator.endswith("File.separator")
            or separator.endswith("File.separatorChar")):
        return None
    base_chain = _java_empty_call_chain(source, parts[0])
    if base_chain is not None and base_chain[1] == ("getCanonicalPath",):
        return base_chain[0]
    # Do not infer that an arbitrary clean String variable is canonical.  A
    # proof would require tracking the defining expression and every later
    # reassignment; accepting `candidate.getCanonicalPath().startsWith(base +
    # separator)` here could suppress a real issue when `base` contains `..`,
    # is relative, or was computed by a non-canonical helper.
    return None


def _java_rejecting_path_guard(source: bytes, if_node):
    """Recognize a narrow negated containment check, returning candidate/base."""
    condition = _unwrap_parenthesized(if_node.child_by_field_name("condition"))
    if condition is None or condition.type != "unary_expression":
        return None
    raw = source[condition.start_byte:condition.end_byte].lstrip()
    kids = condition.named_children
    if not raw.startswith(b"!") or len(kids) != 1:
        return None
    checked = _java_starts_with_call(source, kids[0])
    if checked is None:
        return None
    candidate_expr, base_expr = checked

    candidate_path = _java_path_operand(source, candidate_expr)
    base_path = _java_path_operand(source, base_expr)
    if candidate_path is not None and base_path is not None:
        # Both operands must use the same Path normalization shape. This keeps
        # `normalize`/`startsWith` in unrelated expressions from becoming a
        # global sanitizer.
        if candidate_path[1] == base_path[1]:
            return candidate_path[0], base_path[0]
        return None

    canonical = _java_empty_call_chain(source, candidate_expr)
    if canonical is None or canonical[1] != ("getCanonicalPath",):
        return None
    canonical_base = _java_canonical_base(source, base_expr)
    if canonical_base is None:
        return None
    return canonical[0], canonical_base


def _java_prior_initializer(source: bytes, node, name: str):
    """Unique initializer of ``name`` before ``node`` in the same method.

    Ambiguous reassignment or a definition hidden behind control flow is not a
    proof.  The conservative uniqueness rule is sufficient for canonical-path
    validation helpers while keeping the summary fail-closed.
    """
    scope = node
    while scope is not None and scope.type not in {
            "method_declaration", "constructor_declaration"}:
        scope = scope.parent
    if scope is None:
        return None
    found = []

    def visit(current):
        if current.start_byte >= node.start_byte:
            return
        if current is not scope and current.type in {
                "method_declaration", "constructor_declaration",
                "class_declaration", "lambda_expression"}:
            return
        if current.type == "variable_declarator":
            target = current.child_by_field_name("name")
            value = current.child_by_field_name("value")
            if (target is not None and value is not None
                    and source[target.start_byte:target.end_byte].decode(
                        "utf-8", "replace") == name):
                found.append(value)
        elif current.type == "assignment_expression":
            target = current.child_by_field_name("left")
            value = current.child_by_field_name("right")
            if (target is not None and value is not None
                    and _java_value_path(source, target) == (name,)):
                found.append(value)
        for child in current.named_children:
            visit(child)

    visit(scope)
    return found[0] if len(found) == 1 else None


def _java_resolve_expr(source: bytes, if_node, expression):
    path = _java_value_path(source, expression)
    if path is None or len(path) != 1:
        return expression
    return _java_prior_initializer(source, if_node, path[0]) or expression


def _java_canonical_candidate(source: bytes, if_node, expression):
    expression = _java_resolve_expr(source, if_node, expression)
    chain = _java_empty_call_chain(source, expression)
    if chain is None:
        return None
    path, methods = chain
    if methods in {("getCanonicalPath",),
                   ("toFile", "getCanonicalPath")}:
        return path
    return None


def _java_canonical_base_value(source: bytes, if_node, expression):
    expression = _java_resolve_expr(source, if_node, expression)
    expression = _unwrap_parenthesized(expression)
    if expression is None or expression.type != "binary_expression":
        return None
    parts = expression.named_children
    if len(parts) != 2:
        return None
    if source[parts[0].end_byte:parts[1].start_byte].strip() != b"+":
        return None
    separator = source[parts[1].start_byte:parts[1].end_byte].decode(
        "utf-8", "replace").replace(" ", "")
    if not (separator.endswith("File.separator")
            or separator.endswith("File.separatorChar")):
        return None
    canonical_call = _unwrap_parenthesized(parts[0])
    if canonical_call is None or canonical_call.type != "method_invocation":
        return None
    name = canonical_call.child_by_field_name("name")
    args = canonical_call.child_by_field_name("arguments")
    receiver = canonical_call.child_by_field_name("object")
    if (name is None or args is None or receiver is None
            or args.named_children
            or source[name.start_byte:name.end_byte] != b"getCanonicalPath"):
        return None
    receiver = _unwrap_parenthesized(receiver)
    if receiver is not None and receiver.type == "object_creation_expression":
        ctor_type = receiver.child_by_field_name("type")
        ctor_args = receiver.child_by_field_name("arguments")
        if (ctor_type is None or ctor_args is None
                or not source[ctor_type.start_byte:ctor_type.end_byte].decode(
                    "utf-8", "replace").endswith("File")
                or len(ctor_args.named_children) != 1):
            return None
        return _java_value_path(source, ctor_args.named_children[0])
    return _java_value_path(source, receiver)


def _java_accepting_path_guard(source: bytes, if_node):
    """Narrow positive canonical containment proof for the accepted arm."""
    condition = _unwrap_parenthesized(if_node.child_by_field_name("condition"))
    checked = _java_starts_with_call(source, condition)
    if checked is None:
        return None
    candidate_expr, base_expr = checked
    candidate = _java_canonical_candidate(source, if_node, candidate_expr)
    base = _java_canonical_base_value(source, if_node, base_expr)
    if candidate is None or base is None or candidate == base:
        return None
    return candidate, base


def _definitely_terminates(node) -> bool:
    """Conservative Java rejection-arm termination proof."""
    if node is None:
        return False
    if node.type in {"return_statement", "throw_statement"}:
        return True
    if node.type == "if_statement":
        consequence = node.child_by_field_name("consequence")
        alternative = node.child_by_field_name("alternative")
        return (alternative is not None
                and _definitely_terminates(consequence)
                and _definitely_terminates(alternative))
    if node.type == "block":
        return any(_definitely_terminates(child)
                   for child in node.named_children)
    return False


# --- condição decidível em tempo de compilação -------------------------------
#
# Medido no OWASP Benchmark: 51% de TODOS os falsos positivos são ramo cuja
# condição não depende de nada externo:
#
#     int num = 86;
#     if ((7 * 42) - num > 200) bar = "constante"; else bar = param;
#
# `(7*42)-86 = 208 > 200` é sempre verdadeiro, então `bar` NUNCA recebe o dado
# sujo. Unir os dois braços é o comportamento correto de uma may-analysis — e
# é também o que produz o achado errado.
#
# Um fold ERRADO apaga vulnerabilidade real em silêncio, que é o pior defeito
# possível aqui. Por isso o avaliador é por LISTA DE PERMISSÃO e devolve None a
# qualquer sinal de dúvida: nome não resolvido, operador fora da lista, chamada,
# acesso a campo, índice. Divisão fica de fora de propósito — `/` é inteira em
# Java e real em Python, e um avaliador que erra a semântica é pior que um que
# se recusa a decidir.

_LITERAL = re.compile(r"""^(?:-?\d+|'[^'\\]'|"[^"\\]*"|true|false|True|False)$""")

_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.Mod: lambda a, b: a % b}
_CMP = {ast.Lt: lambda a, b: a < b, ast.Gt: lambda a, b: a > b,
        ast.LtE: lambda a, b: a <= b, ast.GtE: lambda a, b: a >= b,
        ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b}


def is_literal(text: str) -> bool:
    """O texto é um literal simples? (inteiro, char, string, booleano)

    Comparação textual de propósito: serve às 19 gramáticas sem uma tabela de
    node types por linguagem, e o custo de errar é só deixar de folding."""
    return bool(_LITERAL.match(text.strip()))


def py_literal(text: str):
    """Valor Python de um literal escrito em qualquer uma das linguagens."""
    t = text.strip()
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    try:
        return ast.literal_eval(t)
    except (ValueError, SyntaxError):
        return None


# Métodos PUROS sobre constante. `String guess = "ABC"; guess.charAt(1)` é uma
# expressão constante de verdade, não um truque: o valor está inteiramente
# escrito no arquivo. Só entram métodos sem efeito colateral e com semântica
# idêntica entre as linguagens — nada de `format`, `replaceAll` (regex difere)
# ou qualquer coisa dependente de locale.
_PUROS = {
    "charAt": lambda s, a: s[a[0]] if isinstance(s, str) and a and
    isinstance(a[0], int) and 0 <= a[0] < len(s) else None,
    "length": lambda s, a: len(s) if isinstance(s, str) and not a else None,
    "toUpperCase": lambda s, a: s.upper() if isinstance(s, str) and not a else None,
    "toLowerCase": lambda s, a: s.lower() if isinstance(s, str) and not a else None,
    "upper": lambda s, a: s.upper() if isinstance(s, str) and not a else None,
    "lower": lambda s, a: s.lower() if isinstance(s, str) and not a else None,
    "trim": lambda s, a: s.strip() if isinstance(s, str) and not a else None,
    "strip": lambda s, a: s.strip() if isinstance(s, str) and not a else None,
}


def _eval(node, consts):
    """Valor de um nó da AST Python, ou None se não for decidível."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, (int, bool, str)) else None
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Call):
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in _PUROS:
            return None
        if node.keywords:
            return None
        alvo = _eval(fn.value, consts)
        if alvo is None:
            return None
        args = [_eval(a, consts) for a in node.args]
        if any(a is None for a in args):
            return None
        try:
            return _PUROS[fn.attr](alvo, args)
        except (TypeError, ValueError, IndexError):
            return None
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, consts)
        if v is None or isinstance(v, str):
            return None
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        if isinstance(node.op, ast.Not):
            return not v
        return None
    if isinstance(node, ast.BinOp):
        fn = _BIN.get(type(node.op))
        a, b = _eval(node.left, consts), _eval(node.right, consts)
        if fn is None or a is None or b is None:
            return None
        if isinstance(a, str) or isinstance(b, str):
            return None
        try:
            return fn(a, b)
        except (ZeroDivisionError, TypeError, OverflowError):
            return None
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            return None
        fn = _CMP.get(type(node.ops[0]))
        a, b = _eval(node.left, consts), _eval(node.comparators[0], consts)
        if fn is None or a is None or b is None:
            return None
        try:
            return fn(a, b)
        except TypeError:
            return None
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, consts) for v in node.values]
        if any(v is None for v in vals):
            return None
        return (all(vals) if isinstance(node.op, ast.And) else any(vals))
    return None


def eval_const(text: str, consts: dict):
    """Valor da expressão, ou None quando não é decidível. Base de tudo aqui."""
    if not text or len(text) > 200:
        return None
    t = text.strip()
    while t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()                 # `if (cond)` / `switch (x)` do C/Java
    t = t.replace("&&", " and ").replace("||", " or ")
    protegido = t.replace("==", "\0").replace("!=", "\0").replace("<=", "\0") \
                 .replace(">=", "\0")
    if any(bad in protegido for bad in ("=", "!", "~", "&", "|", "^", "/",
                                        "<<", ">>")):
        # `=` sozinho seria atribuição; `!`/`~` e os bit-a-bit não estão
        # modelados; `/` é inteira em Java e real em Python, e um avaliador que
        # erra a semântica é pior que um que se recusa a decidir.
        return None
    try:
        tree = ast.parse(t, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    return _eval(tree.body, consts)


def fold_condition(text: str, consts: dict) -> bool | None:
    """A condição é decidível com o que sabemos? True/False, ou None."""
    v = eval_const(text, consts)
    return None if v is None or isinstance(v, str) else bool(v)



def build_regions(body_node, key: str, source: bytes | None = None,
                  consts: dict | None = None) -> Seq:
    """CFG estruturada a partir da AST do corpo da função.

    `key` é "py"/"js" ou o nome da linguagem (família GEN).

    Duas decisões que valem por 15 gramáticas:

    1. `has_else` = "tem 2+ corpos", NÃO um node type `else_clause`. Em Java/C#
       o else é só outro `block`; em C é `else_clause`; em Python é `else_clause`.
       Contar corpos funciona em todas, procurar por tipo não.

    2. Se a gramática não expõe corpo nenhum (Swift põe os statements direto no
       `if_statement`), o nó inteiro vira UM braço com `has_else=False`. Assim o
       ambiente de entrada entra na união e nenhum kill de dentro do bloco
       escapa para fora — conservador, preservando RECALL, que é o lado seguro:
       um kill indevido apagaria um achado real."""
    cfg = _ctrl(key)
    seq = Seq()
    if body_node is None or cfg is None:
        return seq
    for child in body_node.named_children:
        t = child.type
        if t in cfg["branch"] or t in cfg["loop"]:
            bodies = [s for s in child.named_children if s.type in cfg["body"]]
            if "if" in t:
                # `if (c) a = 1; else a = 2;` — SEM chaves os ramos não são nós
                # de corpo, e a varredura por tipo devolvia lista vazia: o `if`
                # inteiro virava um braço só. Os campos `consequence`/
                # `alternative` existem nas duas formas, então usá-los cobre a
                # com chaves (dá o mesmo resultado) e a sem (que estava errada).
                campos = [child.child_by_field_name(f)
                          for f in ("consequence", "alternative")]
                campos = [c for c in campos if c is not None]
                if campos:
                    bodies = campos
            eh_switch = any(k in t for k in ("switch", "match", "when", "case"))
            if eh_switch:
                # `switch (x) { case 'A': …  case 'B': … }` — o corpo é UM
                # contêiner (`switch_block`) e os grupos de case ficavam dentro
                # dele, virando trecho SEQUENCIAL. Com isso `case 'A': bar =
                # sujo;` seguido de `case 'B': bar = "limpo";` apagava a
                # sujeira do primeiro: exatamente o mesmo defeito do `if` sem
                # chaves, e a mesma perda silenciosa de recall. Um corpo que
                # contém outros corpos é contêiner de BRAÇOS.
                expandido = []
                for b in bodies:
                    filhos = [c for c in b.named_children
                              if c.type in cfg["body"]]
                    expandido.extend(filhos or [b])
                bodies = expandido
            if not bodies:
                # gramática sem nó de corpo: trata o nó todo como braço único
                arm = Seq([Span(child.start_byte, child.end_byte)])
                seq.items.append(Loop(arm) if t in cfg["loop"]
                                 else Branch([arm], has_else=False))
                continue
            # a condição (filhos que NÃO são corpo) executa incondicionalmente.
            # Comparar por POSIÇÃO, não por node type: com `if` sem chaves o
            # ramo é um `expression_statement`, que não está em `cfg["body"]` —
            # comparando por tipo ele entrava aqui E como braço, e um fato
            # avaliado duas vezes, uma delas fora do ramo, é fato avaliado no
            # ambiente errado.
            corpos = {(b.start_byte, b.end_byte) for b in bodies}
            for sub in child.named_children:
                if (sub.start_byte, sub.end_byte) not in corpos:
                    seq.items.append(Span(sub.start_byte, sub.end_byte))
            if t in cfg["loop"]:
                inner = Seq([build_regions(b, key, source, consts)
                             for b in bodies])
                seq.items.append(Loop(inner))
            else:
                arms = [build_regions(b, key, source, consts) for b in bodies]
                # Num `switch`, "tem 2+ braços" NÃO significa que algum sempre
                # executa: sem `default` o seletor pode não casar com nenhum e
                # o bloco inteiro é pulado. Quem decide é a presença do rótulo
                # padrão; na dúvida, assume que não há — o ambiente de entrada
                # entra na união e nenhum kill escapa, que é o lado seguro.
                tem_else = (_tem_default(bodies, source) if eh_switch
                            else len(bodies) >= 2)
                if source is None or not consts:
                    escolhido = None
                elif eh_switch:
                    escolhido = _switch_taken(child, source, consts, bodies)
                else:
                    escolhido = _arm_taken(child, cfg, source, consts,
                                           len(bodies))
                post_sanitizes = []
                arm_sanitizes = []
                if (key == "java" and t == "if_statement"
                        and child.child_by_field_name("alternative") is None):
                    consequence = child.child_by_field_name("consequence")
                    guard = (_java_rejecting_path_guard(source, child)
                             if source is not None else None)
                    if guard is not None and _definitely_terminates(consequence):
                        post_sanitizes.append((*guard, child.start_byte))
                if key == "java" and t == "if_statement":
                    accepting = (_java_accepting_path_guard(source, child)
                                 if source is not None else None)
                    if accepting is not None:
                        arm_sanitizes.append((0, *accepting))
                seq.items.append(Branch(arms, has_else=tem_else,
                                        taken=escolhido,
                                        post_sanitizes=post_sanitizes,
                                        arm_sanitizes=arm_sanitizes))
        else:
            seq.items.append(Span(child.start_byte, child.end_byte))
    return seq


_DEFAULT = re.compile(r"^\s*(?:default\b|else\b|case\s+_\b|_\s*(?:=>|->))")


def _tem_default(bodies, source) -> bool:
    """Algum braço do switch é o rótulo padrão?"""
    if source is None:
        return False
    for b in bodies:
        cabeca = source[b.start_byte:b.start_byte + 40].decode("utf-8", "replace")
        if _DEFAULT.match(cabeca):
            return True
    return False


_ROTULO = re.compile(r"\bcase\s+([^:\n]+?)\s*(?::|->)")
_CORTA = re.compile(r"\b(?:break|return|throw|continue)\b")


def _switch_taken(node, source, consts, bodies) -> int | None:
    """Índice do único grupo de `case` que executa, se o seletor for constante.

    Recusa quando o grupo escolhido pode CAIR no seguinte (sem `break`/`return`):
    aí mais de um corpo executa e escolher um só apagaria o outro."""
    sel = None
    for f in ("condition", "value", "subject"):
        sel = node.child_by_field_name(f)
        if sel is not None:
            break
    if sel is None:
        return None
    alvo = eval_const(source[sel.start_byte:sel.end_byte].decode("utf-8", "replace"),
                      consts)
    if alvo is None or isinstance(alvo, bool):
        return None
    padrao = None
    escolhido = None
    for i, b in enumerate(bodies):
        texto = source[b.start_byte:b.end_byte].decode("utf-8", "replace")
        cabeca = texto[:60]
        if _DEFAULT.match(cabeca):
            padrao = i
            continue
        for bruto in _ROTULO.findall(texto[:200]):
            v = py_literal(bruto)
            if v is None:
                return None            # rótulo que não sabemos ler: desiste
            if v == alvo:
                if escolhido is not None:
                    return None        # dois grupos casando: não deveria, desiste
                escolhido = i
    if escolhido is None:
        return padrao if padrao is not None else -1
    corpo = source[bodies[escolhido].start_byte:bodies[escolhido].end_byte]
    if not _CORTA.search(corpo.decode("utf-8", "replace")):
        return None                    # pode cair no próximo grupo
    return escolhido


def _arm_taken(node, cfg, source, consts, n_bodies) -> int | None:
    """Índice do único braço que executa, se a condição for decidível.

    Só para `if`/`unless`: um `switch` precisaria casar o seletor com cada
    rótulo, e um `try` não tem condição nenhuma. Fora desses casos devolve None
    e o motor volta a unir os braços, como sempre fez."""
    if source is None or not consts or n_bodies == 0 or n_bodies > 2:
        return None
    if "if" not in node.type:                  # if_statement / if_expression
        return None
    cond = node.child_by_field_name("condition")
    if cond is None:
        return None
    texto = source[cond.start_byte:cond.end_byte].decode("utf-8", "replace")
    v = fold_condition(texto, consts)
    if v is None:
        return None
    if v:
        return 0                               # só o "então"
    return 1 if n_bodies >= 2 else -1          # o "senão", ou nada


# --- avaliação ---------------------------------------------------------------

def _kill(env: set, targets) -> set:
    """Remove os alvos E tudo que tem eles como PREFIXO: reatribuir `x` mata
    `x.a`, `x.a.b`… (um objeto novo não herda a sujeira dos campos do antigo)."""
    out = set()
    for p in env:
        if any(p == t or (len(p) > len(t) and p[:len(t)] == t) for t in targets):
            continue
        out.add(p)
    return out


class _Eval:
    """Interpreta a CFG estruturada propagando o ambiente sujo."""

    def __init__(self, facts, sanitizers, sinks_out: Flow, sources=frozenset(),
                 nonprop=frozenset(), source_spans=frozenset(),
                 receiver_effects=None, trusted_source_literals=()):
        self.sanitizers = sanitizers
        self.sources = sources
        self.trusted_source_literals = trusted_source_literals
        # SPANS cuja chamada foi resolvida SEMANTICAMENTE (L1) e cujo alvo
        # comprovadamente não devolve o argumento recebido.  Linha não basta:
        # Java permite várias atribuições independentes no mesmo statement.
        self.nonprop = nonprop
        self.source_spans = source_spans
        self.receiver_effects = receiver_effects or {}
        self.flow = sinks_out
        self.facts = facts
        # todos os fatos ordenados por posição, para o casamento por span
        self.assigns = sorted(facts.assigns, key=lambda a: (a.span or (0, 0))[0])
        self.calls = sorted(facts.calls, key=lambda c: (c.span or (0, 0))[0])
        self.returns = sorted(facts.returns, key=lambda r: (r.span or (0, 0))[0])

    # -- transferências --

    def _field_aliases(self, field: str) -> set[tuple[str, ...]]:
        aliases: set[tuple[str, ...]] = {("this", field)}
        if field not in self.facts.local_names:
            aliases.add((field,))
        return aliases

    def _expanded_targets(self, targets) -> set[tuple[str, ...]]:
        """Targets for dirty generation: subfields conservatively taint root."""
        expanded: set[tuple[str, ...]] = set()
        for target in targets:
            field = instance_field_name(self.facts, target)
            expanded |= self._field_aliases(field) if field else {target}
        return expanded

    def _kill_targets(self, targets) -> set[tuple[str, ...]]:
        """Targets a clean assignment definitely overwrites.

        Assigning ``this.root.subfield`` cannot clean the whole ``root`` even
        though a dirty write to that subfield taints the root in our summary
        domain.  Only a direct root assignment may kill both field aliases.
        """
        killed: set[tuple[str, ...]] = set()
        for target in targets:
            field = instance_field_name(self.facts, target)
            direct_root = field is not None and (
                target == ("this", field) or target == (field,))
            killed |= self._field_aliases(field) if direct_root else {target}
        return killed

    def _apply_assign(self, a, env: set) -> set:
        # `x = f(sujo)` só suja `x` se `f` DEVOLVER o que recebeu. A pergunta
        # só é feita quando o L1 resolveu a chamada: por NOME o alvo pode ser
        # outro, e matar sujeira do alvo errado apaga vulnerabilidade real.
        sanitized = ((a.rhs_call is not None and a.rhs_call in self.sanitizers)
                     or a.span in self.nonprop)
        # uma FONTE gera sujeira no ponto do programa. Sem isto o motor mataria
        # a própria semente da varredura: `x = input()` tem RHS sem ids, então
        # cairia no kill — o bug que a bateria de recall pegou.
        from_source = not sanitized and (
            assign_reads_named_source(
                a, self.sources, self.sanitizers,
                self.trusted_source_literals)
            # fonte de FRAMEWORK: `x = request.POST.get(..)` / `x = req.query.q`
            or assign_reads_framework_source(a, self.sanitizers)
            or a.span in self.source_spans)
        targets = self._expanded_targets(a.targets)
        rhs_hit = (not sanitized) and any(_is_tainted(p, env) for p in a.rhs_ids)
        aug_hit = a.is_aug and any(_is_tainted(t, env) for t in targets)
        if from_source or rhs_hit or aug_hit:
            return env | targets                        # gen
        if a.is_aug:
            return env                                  # `x += limpo` não limpa x
        return _kill(env, self._kill_targets(a.targets))  # definite clean only

    def _record_call(self, c, env: set) -> None:
        # leitura de requisição escrita DENTRO do argumento: suja no ponto da
        # chamada, sem depender do ambiente — não há variável para o ambiente
        # carregar. É a mesma ideia do `from_source` no assign.
        direto = dict(direct_source_args(c, self.sanitizers))
        direto.update(dict(direct_named_source_args(
            c, self.sources, self.sanitizers,
            self.trusted_source_literals)))
        for idx, ids in c.args:
            hit = [p for p in ids if _is_tainted(p, env)]
            if hit:
                self.flow.arg_flows.append(
                    ArgFlow(c.callee, idx, c.line, ".".join(sorted(hit)[0]),
                            c.qualified, direto.get(idx), c.span))
            elif idx in direto:
                self.flow.arg_flows.append(
                    ArgFlow(c.callee, idx, c.line, direto[idx], c.qualified,
                            direto[idx], c.span))
        if c.receiver_kind in {"implicit_this", "explicit_this"}:
            fields = frozenset(
                name for path in env
                if (name := instance_field_name(self.facts, path)) is not None
            )
            if fields:
                self.flow.receiver_flows.append(ReceiverFlow(
                    c.callee, c.line, fields, c.qualified, c.span))

    def _apply_receiver_effect(self, c, env: set) -> set:
        effect: ReceiverEffect | None = self.receiver_effects.get(c.span)
        if effect is None:
            return env
        incoming = set(env)
        overwritten = set().union(*(
            self._field_aliases(field) for field in effect.overwrites
        )) if effect.overwrites else set()
        env = _kill(env, overwritten)
        dirty = set(effect.always_dirty)
        direct = dict(direct_source_args(c, self.sanitizers))
        direct.update(dict(direct_named_source_args(
            c, self.sources, self.sanitizers,
            self.trusted_source_literals)))
        arg_dirty = {
            index for index, paths in c.args
            if index in direct or any(_is_tainted(path, incoming) for path in paths)
        }
        for field_name, indexes in effect.from_params:
            if indexes & arg_dirty:
                dirty.add(field_name)
        for field_name in dirty:
            env |= self._field_aliases(field_name)
        return env

    def _clear_validated_file_constructors(
            self, candidate: tuple[str, ...], base: tuple[str, ...],
            guard_start: int) -> None:
        """Retract only the exact non-I/O File construction proven contained.

        ``File`` is a deliberate surrogate sink until receiver-to-call taint is
        available.  A later dominating containment guard makes that surrogate
        safe, but only if the constructed value did not escape or get used
        before validation.  Real I/O sinks are never retracted here.
        """
        safe_spans: set[tuple[int, int]] = set()
        for assignment in self.assigns:
            span = assignment.span
            if (span is None or span[1] > guard_start
                    or assignment.targets != {candidate}):
                continue
            constructors = [
                call for call in self.calls
                if call.callee == "File" and call.span is not None
                and span[0] <= call.span[0] < call.span[1] <= span[1]
            ]
            if not constructors:
                continue
            if assignment.rhs_call == "File":
                # `File candidate = wrap(new File(raw))` has a File call
                # inside the initializer, but that object escaped to `wrap`;
                # it is not the value validated later.  A top-level File RHS
                # proves which constructor produced the guarded candidate.
                constructors = [max(
                    constructors,
                    key=lambda call: call.span[1] - call.span[0],
                )]
            elif assignment.ternary is not None and len(constructors) == 1:
                # Common safe fallback: `candidate = input != null
                # ? new File(base, input) : base;` followed immediately by a
                # rejecting canonical containment guard.  Accept only when the
                # non-constructor arm is exactly the same trusted base.  This
                # keeps nested helpers, two constructors and unrelated clean
                # alternatives fail-closed.
                _condition, then_ids, else_ids = assignment.ternary
                if not ({base} in (then_ids, else_ids)
                        and base in (then_ids | else_ids)):
                    continue
            else:
                continue

            # Any use between construction and validation may have observed
            # the unvalidated path.  Calls on the candidate receiver count as
            # uses even when the extractor records no explicit argument.
            escaped = any(
                later.span is not None
                and span[1] <= later.span[0] < guard_start
                and candidate not in later.targets
                and any(_is_tainted(path, {candidate})
                        for path in later.rhs_ids)
                for later in self.assigns
            )
            escaped = escaped or any(
                call.span is not None
                and span[1] <= call.span[0] < guard_start
                and (
                    any(any(_is_tainted(path, {candidate}) for path in paths)
                        for _index, paths in call.args)
                    or (call.qualified is not None
                        and (call.qualified == ".".join(candidate)
                             or call.qualified.startswith(
                                 ".".join(candidate) + ".")))
                )
                for call in self.calls
            )
            escaped = escaped or any(
                returned.span is not None
                and span[1] <= returned.span[0] < guard_start
                and any(_is_tainted(path, {candidate})
                        for path in returned.ids)
                for returned in self.returns
            )
            if not escaped:
                safe_spans.update(call.span for call in constructors)

        if safe_spans:
            self.flow.arg_flows = [
                arg_flow for arg_flow in self.flow.arg_flows
                if not (arg_flow.callee == "File"
                        and arg_flow.span in safe_spans)
            ]

    def _record_return(self, r, env: set) -> None:
        if r.top_call is not None and r.top_call in self.sanitizers:
            return
        if any(_is_tainted(p, env) for p in r.ids):
            self.flow.reaches_return = True

    def _in(self, fact, start: int, end: int) -> bool:
        s = (fact.span or (-1, -1))[0]
        return start <= s < end

    # -- regiões --

    def span(self, sp: Span, env: set, record: bool) -> set:
        """Fatos do trecho, em ordem. `record` desliga o registro de achados nas
        iterações intermediárias do fixpoint de laço (evita duplicar)."""
        events = []
        for a in self.assigns:
            if self._in(a, sp.start, sp.end):
                events.append(((a.span or (0, 0))[1], 0, a))
        for c in self.calls:
            if self._in(c, sp.start, sp.end):
                events.append(((c.span or (0, 0))[1], 1, c))
        for r in self.returns:
            if self._in(r, sp.start, sp.end):
                events.append(((r.span or (0, 0))[1], 2, r))
        # ordem de código; num mesmo ponto a chamada é avaliada ANTES do assign
        # completar (`x = f(sujo)` lê o argumento com o ambiente de entrada)
        events.sort(key=lambda e: (e[0], -e[1]))
        for _pos, kind, fact in events:
            if kind == 0:
                env = self._apply_assign(fact, env)
            elif kind == 1:
                if record:
                    self._record_call(fact, env)
                env = self._apply_receiver_effect(fact, env)
            else:
                if record:
                    self._record_return(fact, env)
        return env

    def run(self, region, env: set, record: bool = True) -> set:
        if isinstance(region, Seq):
            for item in region.items:
                env = self.run(item, env, record)
            return env
        if isinstance(region, Span):
            return self.span(region, env, record)
        if isinstance(region, Branch):
            if region.taken is not None:
                # condição decidida: só um caminho existe de verdade. Unir com
                # o braço morto seria propagar sujeira que nunca chega lá.
                if region.taken < 0:
                    return set(env)                       # nenhum braço executa
                return self.run(region.arms[region.taken], set(env), record)
            incoming = set(env)
            out = set() if region.has_else else set(env)   # sem else: pode pular
            arm_sanitizes = {
                index: (candidate, base)
                for index, candidate, base in region.arm_sanitizes
            }
            for index, arm in enumerate(region.arms):
                arm_env = set(env)
                if index in arm_sanitizes:
                    candidate, base = arm_sanitizes[index]
                    if not _is_tainted(base, env):
                        arm_env = _kill(arm_env, {candidate})
                        if _is_tainted(candidate, env):
                            self.flow.proven_sanitized_return = True
                out |= self.run(arm, arm_env, record)
            for candidate, base, guard_start in region.post_sanitizes:
                # A user-controlled base makes containment meaningless. Check
                # the environment entering the guard, before the rejecting arm.
                if not _is_tainted(base, incoming):
                    self._clear_validated_file_constructors(
                        candidate, base, guard_start)
                    out = _kill(out, {candidate})
            return out
        if isinstance(region, Loop):
            # 0..N iterações: o ambiente de entrada entra na união; itera até
            # estabilizar (sem registrar achados nas passadas de convergência)
            cur = set(env)
            for _ in range(8):
                nxt = cur | self.run(region.body, set(cur), record=False)
                if nxt == cur:
                    break
                cur = nxt
            if record:                       # passada final: agora registra
                self.run(region.body, set(cur), record=True)
            return cur
        return env


def analyze_flow(facts, tainted_init, sanitizers=frozenset(), sources=frozenset(),
                 nonprop=frozenset(), source_spans=frozenset(),
                 receiver_effects=None, trusted_source_literals=()):
    """Versão flow-sensitive de `dataflow.analyze_facts`.

    Usa `facts.regions`, a CFG estruturada montada na EXTRAÇÃO (estrutura pura
    de inteiros — guardar nós tree-sitter aqui manteria árvores inteiras vivas
    no cache LRU de fatos).

    Devolve `None` quando não dá para ser flow-sensitive (linguagem sem config
    de controle, ou fatos sem span) — o chamador cai no motor flow-insensitive,
    que over-aproxima. Degradar é honesto; fingir precisão não seria."""
    regions = getattr(facts, "regions", None)
    if regions is None:
        return None
    if (facts.assigns or facts.calls) and not any(
            getattr(f, "span", None) is not None
            for f in (list(facts.assigns) + list(facts.calls))):
        return None                      # extractor ainda não popula spans
    env = {p if isinstance(p, tuple) else (p,) for p in tainted_init}
    flow = Flow()
    ev = _Eval(facts, sanitizers, flow, sources, nonprop, source_spans,
               receiver_effects, trusted_source_literals)
    flow.exit_taint = frozenset(ev.run(regions, env))
    # dedupe: o fixpoint de laço pode registrar o mesmo arg_flow 2x
    seen, uniq = set(), []
    for af in flow.arg_flows:
        key = (af.callee, af.arg_index, af.line)
        if key not in seen:
            seen.add(key)
            uniq.append(af)
    flow.arg_flows = uniq
    seen_receiver, receiver_uniq = set(), []
    for receiver_flow in flow.receiver_flows:
        receiver_key = (receiver_flow.callee, receiver_flow.line,
                        receiver_flow.fields)
        if receiver_key not in seen_receiver:
            seen_receiver.add(receiver_key)
            receiver_uniq.append(receiver_flow)
    flow.receiver_flows = receiver_uniq
    return flow
