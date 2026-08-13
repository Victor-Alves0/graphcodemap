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

from dataclasses import dataclass, field

from .dataflow import (ArgFlow, Flow, _is_tainted,
                       assign_reads_framework_source)

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


@dataclass
class Loop:
    body: object               # Seq


@dataclass
class Seq:
    items: list = field(default_factory=list)


def _ctrl(family: str):
    return _CTRL.get(family)


def build_regions(body_node, key: str) -> Seq:
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
            if not bodies:
                # gramática sem nó de corpo: trata o nó todo como braço único
                arm = Seq([Span(child.start_byte, child.end_byte)])
                seq.items.append(Loop(arm) if t in cfg["loop"]
                                 else Branch([arm], has_else=False))
                continue
            # a condição (filhos que NÃO são corpo) executa incondicionalmente
            for sub in child.named_children:
                if sub.type not in cfg["body"]:
                    seq.items.append(Span(sub.start_byte, sub.end_byte))
            if t in cfg["loop"]:
                inner = Seq([build_regions(b, key) for b in bodies])
                seq.items.append(Loop(inner))
            else:
                arms = [build_regions(b, key) for b in bodies]
                seq.items.append(Branch(arms, has_else=len(bodies) >= 2))
        else:
            seq.items.append(Span(child.start_byte, child.end_byte))
    return seq


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

    def __init__(self, facts, sanitizers, sinks_out: Flow, sources=frozenset()):
        self.sanitizers = sanitizers
        self.sources = sources
        self.flow = sinks_out
        # todos os fatos ordenados por posição, para o casamento por span
        self.assigns = sorted(facts.assigns, key=lambda a: (a.span or (0, 0))[0])
        self.calls = sorted(facts.calls, key=lambda c: (c.span or (0, 0))[0])
        self.returns = sorted(facts.returns, key=lambda r: (r.span or (0, 0))[0])

    # -- transferências --

    def _apply_assign(self, a, env: set) -> set:
        sanitized = a.rhs_call is not None and a.rhs_call in self.sanitizers
        # uma FONTE gera sujeira no ponto do programa. Sem isto o motor mataria
        # a própria semente da varredura: `x = input()` tem RHS sem ids, então
        # cairia no kill — o bug que a bateria de recall pegou.
        from_source = not sanitized and (
            (a.rhs_call is not None and a.rhs_call in self.sources)
            # fonte de FRAMEWORK: `x = request.POST.get(..)` / `x = req.query.q`
            or assign_reads_framework_source(a, self.sanitizers))
        rhs_hit = (not sanitized) and any(_is_tainted(p, env) for p in a.rhs_ids)
        aug_hit = a.is_aug and any(_is_tainted(t, env) for t in a.targets)
        if from_source or rhs_hit or aug_hit:
            return env | set(a.targets)                 # gen
        if a.is_aug:
            return env                                  # `x += limpo` não limpa x
        return _kill(env, a.targets)                    # kill: alvo vira limpo

    def _record_call(self, c, env: set) -> None:
        for idx, ids in c.args:
            hit = [p for p in ids if _is_tainted(p, env)]
            if hit:
                self.flow.arg_flows.append(
                    ArgFlow(c.callee, idx, c.line, ".".join(sorted(hit)[0]),
                            c.qualified))

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
                events.append(((a.span or (0, 0))[0], 0, a))
        for c in self.calls:
            if self._in(c, sp.start, sp.end):
                events.append(((c.span or (0, 0))[0], 1, c))
        for r in self.returns:
            if self._in(r, sp.start, sp.end):
                events.append(((r.span or (0, 0))[0], 2, r))
        # ordem de código; num mesmo ponto a chamada é avaliada ANTES do assign
        # completar (`x = f(sujo)` lê o argumento com o ambiente de entrada)
        events.sort(key=lambda e: (e[0], -e[1]))
        for _pos, kind, fact in events:
            if kind == 0:
                env = self._apply_assign(fact, env)
            elif kind == 1:
                if record:
                    self._record_call(fact, env)
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
            out = set() if region.has_else else set(env)   # sem else: pode pular
            for arm in region.arms:
                out |= self.run(arm, set(env), record)
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


def analyze_flow(facts, tainted_init, sanitizers=frozenset(), sources=frozenset()):
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
    ev = _Eval(facts, sanitizers, flow, sources)
    ev.run(regions, env)
    # dedupe: o fixpoint de laço pode registrar o mesmo arg_flow 2x
    seen, uniq = set(), []
    for af in flow.arg_flows:
        key = (af.callee, af.arg_index, af.line)
        if key not in seen:
            seen.add(key)
            uniq.append(af)
    flow.arg_flows = uniq
    return flow
