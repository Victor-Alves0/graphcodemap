"""Camada de dataflow (CPG-lite): análise intra-procedural de fluxo de dados.

Arquitetura (docs/RESEARCH.md §6): esqueleto de Code Property Graph pragmático
e incremental, estilo Semgrep — não whole-program (Joern) nem IFDS pesado.

Duas partes:
- EXTRAÇÃO DE FATOS por linguagem (`extract_facts`): normaliza o corpo de uma
  função em params, atribuições, chamadas e returns — abstraindo a gramática.
- MOTOR DE TAINT: `analyze()` despacha para o motor FLOW-SENSITIVE
  (`flowsens.py`: CFG estruturada + kill na redefinição) nas linguagens de
  `FLOW_SENSITIVE`, e cai em `analyze_facts` (fixpoint may-taint
  flow-INsensitive, que over-aproxima) no resto. Sanitizers cortam a
  propagação; sources semeiam. Os dois são may-taint: sujo em ALGUM caminho
  basta.

INTER-procedural fica no query.py, compondo estes sumários ao longo do call
graph. Computado sob demanda do código no disco (sempre fresco). Python,
JavaScript/TypeScript e Java têm validação em repositórios reais; Java inclui
um primeiro domínio de estado de campos no mesmo receptor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

# Config de extração de fatos por linguagem. As irregularidades das gramáticas
# ficam AQUI (declarativas); o motor de taint continua compartilhado.
#   func    : node types de função
#   id      : node types-folha que são referência de variável
#   params  : ("field",nome) | ("children",{types}) | ("search",{container_types})
#   assigns : passos ("lr",{t},lf,rf) | ("decl",{t},nf,vf) | ("decl_last",{t},nf|None)
#             | ("poslr",{t}) p/ gramáticas que NÃO nomeiam os campos do assign
#               (Kotlin): alvo = 1º filho nomeado, valor = último
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
             # `for (T item : values)` binds each element read from `values`
             # to `item`.  Without this normalized assignment, taint on a
             # request-derived array/collection stops at the loop boundary.
             "foreach": ({"enhanced_for_statement"}, "name", "value"),
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
               # `assignment` = REATRIBUIÇÃO (x = ...). Sem ela o motor não vê a
               # redefinição e não há o que matar — lacuna que o P1b expôs.
               "assigns": [("decl_last", {"property_declaration"}, None),
                           ("poslr", {"assignment"})],
               "calls": {"call_expression"}, "returns": {"jump_expression"},
               "tail": True},
    "swift": {"func": {"function_declaration", "init_declaration"},
              "id": {"simple_identifier"}, "params": ("children", {"parameter"}),
              "assigns": [("decl", {"property_declaration"}, "name", "value"),
                          ("lr", {"assignment"}, "target", "result")],
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

# Raiz de arquivo. Em linguagem de script o código perigoso mora FORA de
# qualquer função — em PHP é a norma, e o DVWA inteiro é assim. Sem tratar a
# raiz como um corpo, a varredura (que itera símbolos de função) não enxerga
# nada do arquivo.
_ROOT_TYPES = {"program", "module", "source_file", "translation_unit",
               "compilation_unit", "chunk", "document"}


# Linguagens cujo taint é FLOW-SENSITIVE (flowsens.py: CFG estruturada + kill na
# redefinição). Rollout por etapas e DECLARADO — o mapa `codegraph capabilities`
# mostra quem tem e quem não tem. As demais usam o motor flow-insensitive, que
# over-aproxima (mais falso positivo, nunca menos recall).
FLOW_SENSITIVE: frozenset[str] = frozenset({
    "python", "javascript", "typescript", "tsx",
    "java", "go", "c", "cpp", "cuda", "csharp", "php", "ruby", "rust",
    "kotlin", "swift", "scala", "lua", "luau",
})


# --- FONTES DE FRAMEWORK -----------------------------------------------------
# Sem isto o motor não vê nada num app web real: `input()` quase não aparece em
# produção, e o que aparece é `request.POST.get(...)` / `req.query.q`. Medido
# nos apps vulneráveis reais pygoat e dvpwa: request.POST.get 32x,
# request.POST 31x, request.form.get 9x.
#
# Duas formas, porque as duas ocorrem:
#   CHAMADA QUALIFICADA  x = request.POST.get("q")   → receptor.método "POST.get"
#   ACESSO A ATRIBUTO    x = req.query.q             → caminho ("req","query",…)
#
# O casamento é qualificado de propósito: marcar o nome nu `get` como fonte
# tornaria toda leitura de dicionário uma fonte não-confiável.

# receptor (objeto de requisição) → atributos que carregam dado do usuário
_REQ_RECEIVERS = frozenset({"request", "req", "self.request"})
_REQ_ATTRS = frozenset({
    # Django / Flask / FastAPI / aiohttp
    "POST", "GET", "form", "args", "json", "data", "body", "files",
    "cookies", "headers", "values", "query_params", "path_params",
    "match_info", "rel_url", "raw_post_data", "FILES",
    # Express / Koa / Fastify
    "query", "params", "payload",
    # aiohttp / Starlette: o corpo vem de MÉTODO da própria request
    # (`await request.post()`, `await request.json()`), não de atributo.
    # Só casam com receptor de requisição, então o risco de colisão é baixo.
    "post", "text", "read", "multipart", "stream",
})

# `<attr>.get(...)` onde <attr> é um atributo de requisição
FRAMEWORK_SOURCE_CALLS: frozenset[str] = frozenset(
    {f"{a}.{m}" for a in _REQ_ATTRS for m in ("get", "getlist", "getall", "get_all")}
)


# Nomes que são a PRÓPRIA fonte, sem receptor: as superglobais do PHP.
# `$_GET["id"]` não tem objeto de requisição — a variável global É a
# requisição. São seguras de casar por nome nu porque nenhuma outra linguagem
# tem variável chamada `_GET`. O extractor de PHP entrega o nome sem o `$`,
# mas as duas formas entram para não depender disso.
#
# `_SERVER` fica de fora: metade dele é cabeçalho do usuário e metade é
# configuração do servidor (`DOCUMENT_ROOT`), e sem distinguir a chave o
# resultado seria acusar todo `include` de app PHP.
_BARE_SOURCE_NAMES = frozenset({
    "_GET", "_POST", "_REQUEST", "_COOKIE", "_FILES",
    "$_GET", "$_POST", "$_REQUEST", "$_COOKIE", "$_FILES",
})


# Fontes nuas AMBÍGUAS: só valem na linguagem certa. `params` é *a* fonte do
# Rails (`params[:id]`, 62 usos no RailsGoat contra 5 de `params.require`), mas
# `params` também é nome de variável comum em Python e JS — o próprio dvpwa tem
# um dicionário chamado assim. Por isso estas não entram em
# `_BARE_SOURCE_NAMES`, que é global: são resolvidas na EXTRAÇÃO, onde a
# linguagem é conhecida.
_LANG_BARE_SOURCES = {
    "ruby": frozenset({"params", "cookies"}),
}


def lang_bare_sources(lang) -> frozenset:
    return _LANG_BARE_SOURCES.get(lang or "", frozenset())


def is_framework_source_path(path) -> bool:
    """O caminho de acesso lê dado de requisição? (`req.query.q`, `$_GET`)

    Com receptor, exige receptor de requisição E atributo conhecido — só o
    atributo seria frouxo demais (`x.data` é qualquer coisa). Sem receptor,
    só as superglobais do PHP, que não são ambíguas."""
    if not isinstance(path, tuple) or not path:
        return False
    if len(path) == 1:
        return path[0] in _BARE_SOURCE_NAMES
    return ((path[0] in _REQ_RECEIVERS and path[1] in _REQ_ATTRS)
            or path[0] in _BARE_SOURCE_NAMES)


def is_framework_source_call(qualified: str | None, paths=()) -> bool:
    """A qualified framework call backed by a full request access path.

    ``values.get`` alone is not enough evidence: ``values`` is also a common
    local ``Map`` name.  The extracted paths retain the missing outer receiver
    in real calls such as ``request.values.get(...)``, so require that evidence
    instead of trusting the lossy two-segment spelling.
    """
    return (qualified is not None and qualified in FRAMEWORK_SOURCE_CALLS
            and any(is_framework_source_path(path) for path in paths))


def _nested_named_source(call, sources, sanitizers=frozenset()) -> str | None:
    """Return the exact configured source nested in a call, if unsanitized."""
    typed = (f"{call.receiver_type}.{call.callee}"
             if call.receiver_type else None)
    label = (typed if typed in sources
             else call.qualified if call.qualified in sources
             else call.callee if call.callee in sources
             else None)
    if label is None or any(guard in sanitizers for guard in call.guards):
        return None
    return label


def assign_reads_named_source(a, sources=frozenset(),
                              sanitizers=frozenset()) -> bool:
    """Assignment RHS is a configured source, simple or receiver-qualified.

    Exact qualified names let catalogs model APIs such as `System.getProperty`.
    When extraction knows the receiver's declared type, type-qualified entries
    such as `BufferedReader.readLine` avoid treating a same-named domain method
    as external input.
    """
    typed = (f"{a.rhs_receiver_type}.{a.rhs_call}"
             if (getattr(a, "rhs_receiver_type", None) is not None
                 and getattr(a, "rhs_call", None) is not None)
             else None)
    return ((getattr(a, "rhs_call", None) is not None
             and a.rhs_call in sources
             and a.rhs_call not in sanitizers)
            or (getattr(a, "rhs_qualified", None) is not None
                and a.rhs_qualified in sources)
            or (typed is not None and typed in sources)
            or any(_nested_named_source(call, sources, sanitizers) is not None
                   for call in getattr(a, "nested_calls", ())))


def assign_reads_framework_source(a, sanitizers=frozenset()) -> bool:
    """Esta atribuição nasce de uma fonte de framework, por chamada ou acesso.

    `sanitizers` NÃO é opcional na prática: `cid = int(request.match_info[...])`
    lê uma fonte, mas o valor sai limpo. Sem esta checagem a SEMEADURA da
    varredura marcaria `cid` como sujo enquanto o motor de propagação o trataria
    como limpo — as duas metades discordando, e a discordância vira falso
    positivo."""
    if getattr(a, "rhs_call", None) is not None and a.rhs_call in sanitizers:
        return False
    return (getattr(a, "rhs_framework_source", False)
            or is_framework_source_call(getattr(a, "rhs_qualified", None),
                                        getattr(a, "rhs_ids", ()))
            or any(is_framework_source_path(p) for p in a.rhs_ids))


def _source_reads(source, node, chain, member, call_types, callee,
                  guards=(), out=None, bare=frozenset()):
    """Leituras de requisição dentro de uma expressão, cada uma com a cadeia de
    chamadas que a envolve. `chain(source, node)` devolve o caminho de acesso da
    gramática em questão; o resto é igual para todas."""
    if out is None:
        out = []
    if node is None:
        return out
    t = node.type
    # tenta resolver o caminho em QUALQUER nó, não só nos de acesso a membro:
    # `$_GET['id']` não tem receptor, e a fonte é a folha lá no fundo. Testar
    # só `t in member` fazia a varredura passar direto por ela.
    p = chain(source, node)
    if p is not None and (is_framework_source_path(p)
                          or (len(p) == 1 and p[0] in bare)):
        out.append((".".join(p), guards))
        return out
    if t in member and p is not None:
        return out                              # caminho maximal: não desce
    if t in call_types:
        guards = guards + (callee(source, node),)
    for c in node.named_children:
        _source_reads(source, c, chain, member, call_types, callee, guards, out,
                      bare)
    return out


def direct_source_args(c, sanitizers=frozenset()):
    """Argumentos desta chamada que LEEM a requisição na própria expressão.

    O motor sempre exigiu que a sujeira passasse por uma variável: semeava em
    `x = req.body.q` e depois via `x` chegar no sink. Só que a forma mais
    comum de escrever a vulnerabilidade não tem variável nenhuma —
    `eval(req.body.preTax)`, `exec('ping ' + req.body.address)` — e nessas o
    motor ficava calado. Dos seis casos indefensáveis em dvna e NodeGoat,
    cinco eram assim; o único que achávamos era justamente o que passava por
    variável.

    Rende `(índice, caminho_lido)`. Uma leitura envolvida por sanitizer em
    QUALQUER nível é descartada: `res.send("olá " + escapeHtml(req.params.uid))`
    é a forma correta de escrever, e acusá-la seria punir quem acertou."""
    for idx, reads in c.arg_sources.items():
        limpas = sorted(p for p, guards in reads
                        if not any(g in sanitizers for g in guards))
        if limpas:
            yield idx, limpas[0]


def uses_flow_sensitive(facts, lang: str | None) -> bool:
    """O motor FLOW-SENSITIVE roda mesmo para estes fatos?

    Não basta a linguagem estar em `FLOW_SENSITIVE`: se a extração não montou a
    CFG (`facts.regions`), `analyze` cai no motor que over-aproxima. Quem
    reporta a evidência ao usuário precisa saber o que REALMENTE rodou — senão
    o rótulo vira decoração."""
    return bool(lang in FLOW_SENSITIVE and getattr(facts, "regions", None) is not None)


def analyze(facts, tainted, sanitizers=frozenset(), lang: str | None = None,
            sources=frozenset(), nonprop=frozenset(), source_spans=frozenset()):
    """Ponto único de entrada do motor de taint.

    Usa o motor FLOW-SENSITIVE quando a linguagem suporta e os fatos têm CFG;
    senão cai no fixpoint flow-insensitive (over-aproxima). Concentrar o
    despacho aqui mantém o fallback com uma regra só, e deixa o rollout por
    linguagem visível em `FLOW_SENSITIVE`."""
    if lang in FLOW_SENSITIVE:
        from .flowsens import analyze_flow

        flow = analyze_flow(facts, tainted, sanitizers, sources, nonprop,
                            source_spans)
        if flow is not None:
            return flow
    return analyze_facts(facts, tainted, sanitizers, nonprop, source_spans)


def _build_regions(body_node, family: str, source=None, assigns=None):
    """CFG estruturada para o motor flow-sensitive. Import tardio: flowsens
    importa deste módulo (Flow/ArgFlow/_is_tainted)."""
    if body_node is None:
        return None
    from .flowsens import build_regions

    consts = _const_env(assigns or ())
    _resolve_ternaries(assigns or (), consts)
    return build_regions(body_node, family, source, consts)


# Texto de RHS que PODE ser constante: literal, ou cadeia de acessos e chamadas
# sobre nomes. Filtro grosseiro de propósito — quem decide de verdade é o
# avaliador de `flowsens`, que só aceita métodos puros sobre valores conhecidos.
_TALVEZ_CONST = re.compile(r"""^[\w.'"\s()\[\],+\-*%]+$""")


def _const_text(source: bytes, node) -> str | None:
    """Texto do RHS quando ele pode ser uma expressão constante; senão None."""
    if node is None:
        return None
    t = _text(source, node).strip()
    if len(t) > 120 or not t or not _TALVEZ_CONST.match(t):
        return None
    return t


def _py_colher_de(source):
    def colher(n):
        out: set = set()
        _paths(source, n, out, _PY_MEMBER)
        return out
    return colher


def _js_colher_de(source):
    def colher(n):
        out: set = set()
        _paths(source, n, out, _JS_MEMBER)
        return out
    return colher


def _gen_colher(source, n, idset):
    out: set = set()
    _gen_paths(source, n, idset, out)
    return out


def _ternary(source, node, colher) -> tuple | None:
    """(condição, ids do então, ids do senão) se o RHS for um ternário.

    `colher(nó)` monta o conjunto de caminhos daquele lado, na gramática certa.
    Os campos `condition`/`consequence`/`alternative` cobrem Java, C, C#, JS e
    PHP; Python não nomeia os campos e escreve na ordem `A if C else B`, tratada
    à parte."""
    if node is None:
        return None
    if "ternary" not in node.type and "conditional_expression" not in node.type:
        return None
    cond = node.child_by_field_name("condition")
    cons = node.child_by_field_name("consequence")
    alt = node.child_by_field_name("alternative")
    if cond is None and len(node.named_children) == 3:
        cons, cond, alt = node.named_children
    if cond is None or cons is None or alt is None:
        return None
    return (_text(source, cond), colher(cons), colher(alt))


def _resolve_ternaries(assigns, consts) -> None:
    """Com as constantes conhecidas, mantém só o lado do ternário que executa."""
    from .flowsens import fold_condition

    for a in assigns:
        if a.ternary is None:
            continue
        cond, ids_entao, ids_senao = a.ternary
        v = fold_condition(cond, consts)
        if v is not None:
            a.rhs_ids = ids_entao if v else ids_senao


def _const_env(assigns) -> dict:
    """{nome: valor} das variáveis locais que valem uma constante.

    Regra deliberadamente estreita: o nome tem que ser atribuído UMA ÚNICA vez
    em toda a função. Qualquer segunda atribuição — mesmo de outro literal —
    elimina o nome, porque a ordem entre ela e o uso não é considerada aqui.
    Um valor errado neste mapa apagaria um achado real em silêncio; um valor
    faltando só desliga o folding, que é o lado seguro do erro.

    Resolve em rodadas porque as constantes se encadeiam:
    `String guess = "ABC"; char alvo = guess.charAt(1);` — `alvo` só vira
    conhecido depois de `guess`."""
    from .flowsens import eval_const

    vistos: dict = {}
    for a in assigns:
        for alvo in a.targets:
            if len(alvo) != 1:                # só nome nu, não `x.campo`
                continue
            nome = alvo[0]
            if nome in vistos:
                vistos[nome] = None           # reatribuído: não é constante
            else:
                vistos[nome] = a.rhs_const
    pendentes = {n: t for n, t in vistos.items() if t is not None}
    resolvidos: dict = {}
    for _ in range(4):                        # teto: cadeias reais são curtas
        mudou = False
        for nome, texto in list(pendentes.items()):
            v = eval_const(texto, resolvidos)
            if v is not None:
                resolvidos[nome] = v
                del pendentes[nome]
                mudou = True
        if not mudou:
            break
    return resolvidos


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

# `span` = (start_byte, end_byte) do nó que originou o fato. É o que permite ao
# motor FLOW-SENSITIVE (flowsens.py) situar cada fato na CFG estruturada e
# ordená-los. Opcional: extractor que não popula → cai no motor antigo.
@dataclass
class Assign:
    targets: set[str]
    rhs_ids: set[str]
    is_aug: bool
    rhs_call: str | None  # nome do callee se o RHS é uma única chamada
    line: int = 0
    span: tuple[int, int] | None = None
    # RECEPTOR.MÉTODO do RHS (ex.: "POST.get" em `x = request.POST.get("q")`).
    # É o que permite reconhecer fonte de framework sem marcar o genérico `get`.
    rhs_qualified: str | None = None
    # Texto do RHS quando ele é um LITERAL simples (`86`, `"a"`, `'A'`, `true`).
    # É a matéria-prima do folding de condição: sem saber que `num` vale 86 não
    # dá para decidir `(7*42) - num > 200`.
    rhs_const: str | None = None
    # `bar = cond ? "constante" : sujo` — (texto da condição, ids do então,
    # ids do senão). Um ternário não é um `if` e não vira região de controle,
    # então sem isto os dois lados sempre entram em `rhs_ids` juntos.
    # Resolvido em `_resolve_ternaries`, depois que as constantes são conhecidas.
    ternary: tuple | None = None
    # O RHS lê uma fonte cujo reconhecimento DEPENDE DA LINGUAGEM (`params` do
    # Rails). Decidido na extração, que é o único ponto onde a linguagem é
    # conhecida sem espalhá-la por todo o motor.
    rhs_framework_source: bool = False
    # Declared type of a call receiver on the RHS, when syntax alone can
    # resolve it. Java uses this to distinguish standard input readers from
    # unrelated domain objects exposing the same method name.
    rhs_receiver_type: str | None = None
    # All calls inside the RHS, including their sanitizer ancestry.  A source
    # passed to a constructor/container still taints the assigned object; a
    # source below ``sanitize(...)`` does not.
    nested_calls: list[NestedCall] = field(default_factory=list)


@dataclass
class CallSite:
    callee: str
    line: int
    args: list[tuple[int, set[str]]]  # (arg_index 0-based; -1=kwarg, ids)
    span: tuple[int, int] | None = None
    # RECEPTOR.MÉTODO (ex.: "getWriter.println"). Distingue o sink de XSS
    # `response.getWriter().println` do inofensivo `System.out.println`, que
    # pelo último segmento seriam o mesmo nome. Ver flowsens/taint.
    qualified: str | None = None
    # índice do argumento → leituras de requisição escritas DENTRO dele, cada
    # uma com a CADEIA de chamadas que a envolve:
    #     res.send("olá " + escapeHtml(req.params.uid))
    #       → {0: [("req.params.uid", ("escapeHtml",))]}
    # A cadeia é o que permite o sanitizer cortar em qualquer nível. Olhar só o
    # topo do argumento (como `Assign.rhs_call` faz) deixaria passar justamente
    # a forma mais comum de escrever a versão SEGURA — concatenar texto com o
    # valor já escapado — e transformá-la em falso positivo.
    arg_sources: dict[int, list[tuple[str, tuple[str, ...]]]] = field(
        default_factory=dict)
    # Texto curto do argumento quando a extração consegue preservá-lo.
    # IDs dizem se `list.add(param)` recebe algo sujo; o texto distingue
    # `list.remove(0)` / `list.get(1)`. Sem ambos, um resumo de retorno teria de
    # tratar toda coleção como propagadora ou, pior, adivinhar o índice.
    arg_values: dict[int, str] = field(default_factory=dict)
    # Java receiver identity used by the first heap-sensitive boundary.  Only
    # literal/implicit ``this`` is safe to transport instance-field state.
    receiver_kind: str = "unknown"


@dataclass
class NestedCall:
    """Call nested in an assignment or return, with its enclosing call chain.

    ``guards`` contains only outer calls.  It lets source-wrapper discovery
    distinguish ``return prefix + request.getX()`` from
    ``return sanitize(request.getX())`` without retaining tree-sitter nodes in
    the facts cache.
    """
    callee: str
    qualified: str | None = None
    receiver_type: str | None = None
    guards: tuple[str, ...] = ()


@dataclass
class ReturnExpr:
    ids: set[str]
    top_call: str | None
    span: tuple[int, int] | None = None
    nested_calls: list[NestedCall] = field(default_factory=list)


@dataclass
class FnFacts:
    params: list[str]
    assigns: list[Assign] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    returns: list[ReturnExpr] = field(default_factory=list)
    # CFG estruturada (flowsens.Seq) quando a linguagem suporta flow-sensitivity;
    # montada na extração para não reter nós tree-sitter no cache de fatos.
    regions: object | None = None
    # Direct, non-static fields of the enclosing Java class and names that can
    # shadow an unqualified field access inside this method.
    instance_fields: frozenset[str] = frozenset()
    local_names: frozenset[str] = frozenset()


@dataclass
class ArgFlow:
    callee: str
    arg_index: int
    line: int
    via: str
    qualified: str | None = None
    # Preenchido quando a sujeira NÃO veio de uma variável e sim de uma leitura
    # de requisição escrita dentro do próprio argumento (`eval(req.body.x)`).
    # Quem reporta usa isto como ORIGEM: a origem é aqui mesmo, não uma
    # atribuição anterior que não existe.
    source: str | None = None
    # Exact source span lets the flow-sensitive engine retract a non-I/O
    # surrogate such as `new File(tainted)` only when that exact constructed
    # value is subsequently proven contained.  A line number is insufficient:
    # Java permits multiple independent expressions on one physical line.
    span: tuple[int, int] | None = None


@dataclass
class ReceiverFlow:
    """Tainted ``this`` fields visible at a same-receiver call site."""
    callee: str
    line: int
    fields: frozenset[str]
    qualified: str | None = None
    span: tuple[int, int] | None = None


@dataclass
class Flow:
    arg_flows: list[ArgFlow] = field(default_factory=list)
    receiver_flows: list[ReceiverFlow] = field(default_factory=list)
    reaches_return: bool = False


_JAVA_LIST_CTOR = re.compile(r"^(?:ArrayList|LinkedList)(?:<.*>)?$")
_JAVA_MAP_CTOR = re.compile(r"^(?:HashMap|LinkedHashMap|TreeMap)(?:<.*>)?$")
_JAVA_INT_LITERAL = re.compile(r"^[+-]?\d+$")
_JAVA_MAP_KEY_LITERAL = re.compile(
    r'''^(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])'|'''
    r'''[+-]?(?:\d+(?:\.\d+)?|\.\d+)[a-zA-Z]*|true|false|null)$'''
)
_JAVA_COLLECTION_SENTINEL = ("__graphcodemap_collection_taint__",)


def analyze_java_constant_collections(
        facts: FnFacts, tainted, sanitizers=frozenset(), sources=frozenset(),
        nonprop=frozenset(), source_spans=frozenset(), *,
        allow_unrelated_calls: bool = False):
    """Refina Java com listas e mapas locais de operações determinísticas.

    O analisador escalar é conservador sobre aliasing: `map.put(key, value)`
    não altera a variável `map` no ambiente. Este domínio fechado interpreta
    `ArrayList`/`LinkedList` e `HashMap`/`LinkedHashMap`/`TreeMap` criados na
    função. Mapas admitem somente `put`/`get`/`remove` com chave literal e
    overwrite por chave.

    Alias, escape, mutação condicional, chave dinâmica ou método desconhecido
    no container devolvem ``None``. Para summaries, chamadas alheias também
    abortam a prova; no fluxo normal elas continuam no analisador compartilhado.
    """
    if not facts.calls or facts.regions is None:
        return None

    local_lists: dict[str, list[bool]] = {}
    local_maps: dict[str, dict[str, bool]] = {}
    list_ctor_assignments: dict[str, list[Assign]] = {}
    map_ctor_assignments: list[Assign] = []
    map_ctor_counts: dict[str, int] = {}
    for assignment in facts.assigns:
        if assignment.rhs_call and len(assignment.targets) == 1:
            target = next(iter(assignment.targets))
            if len(target) != 1:
                return None
            if _JAVA_LIST_CTOR.match(assignment.rhs_call):
                local_lists[target[0]] = []
                list_ctor_assignments.setdefault(target[0], []).append(assignment)
            elif _JAVA_MAP_CTOR.match(assignment.rhs_call):
                local_maps[target[0]] = {}
                map_ctor_assignments.append(assignment)
                map_ctor_counts[target[0]] = map_ctor_counts.get(target[0], 0) + 1
    if not local_lists and not local_maps:
        return None

    local_names = set(local_lists) | set(local_maps)

    # Source-order flattening could mistake a conditional safe overwrite for
    # an unconditional one. The new Map proof rejects reads/mutations under a
    # Branch/Loop until collection state lives inside the structured CFG.
    from .flowsens import Branch, Loop, Seq, Span

    # Each leaf span carries its exact branch-arm path and whether it is under
    # a loop. This is stricter than a boolean "conditional": a List created and
    # consumed inside the same arm is deterministic for that path, while a
    # nested/different arm can make a linearized overwrite optional.
    flow_spans: list[tuple[int, int, tuple, bool]] = []
    branch_serial = 0

    def collect_flow_context(region, context=(), in_loop=False):
        nonlocal branch_serial
        if isinstance(region, Span):
            flow_spans.append((region.start, region.end, context, in_loop))
        elif isinstance(region, Seq):
            for item in region.items:
                collect_flow_context(item, context, in_loop)
        elif isinstance(region, Branch):
            branch_serial += 1
            branch_id = branch_serial
            for arm_index, arm in enumerate(region.arms):
                collect_flow_context(
                    arm, context + ((branch_id, arm_index),), in_loop)
        elif isinstance(region, Loop):
            collect_flow_context(region.body, context, True)

    collect_flow_context(facts.regions)

    def span_context(span) -> tuple[tuple, bool]:
        pos = (span or (-1, -1))[0]
        matches = [(context, in_loop, end - start)
                   for start, end, context, in_loop in flow_spans
                   if start <= pos < end]
        if not matches:
            return (), False
        context, in_loop, _width = max(
            matches, key=lambda item: (len(item[0]), item[1], -item[2]))
        return context, in_loop

    def span_is_conditional(span) -> bool:
        context, in_loop = span_context(span)
        return bool(context) or in_loop

    # List operations may be linearized only within one exact arm. A loop can
    # execute zero/N times, so it is never safe for this source-order domain.
    list_contexts: dict[str, tuple] = {}
    for name, constructors in list_ctor_assignments.items():
        if len(constructors) != 1:
            return None
        context, in_loop = span_context(constructors[0].span)
        if in_loop:
            return None
        list_contexts[name] = context

    # Reinitialization would reset the abstract state; conditional creation
    # additionally means the container may not exist on every path. Both are
    # outside this deliberately small domain.
    if any(count != 1 for count in map_ctor_counts.values()) \
            or any(span_is_conditional(a.span) for a in map_ctor_assignments):
        return None

    def refs_local(paths) -> bool:
        return any(path and path[0] in local_names for path in paths)

    def contained_collection_call(span, exclude=None) -> bool:
        if not span:
            return False
        return any(
            call is not exclude and call.span
            and span[0] <= call.span[0] < call.span[1] <= span[1]
            and call.qualified and "." in call.qualified
            and call.qualified.rsplit(".", 1)[0] in local_names
            and call.callee in {"get", "put", "remove"}
            for call in facts.calls
        )

    # A collection local não pode adquirir alias, escapar como argumento ou
    # ser retornada diretamente. `x = map.get("k")` menciona o receiver nos
    # IDs genéricos e só é liberado pela chamada suportada contida no span.
    for assignment in facts.assigns:
        if refs_local(assignment.rhs_ids) \
                and not contained_collection_call(assignment.span):
            return None
    for returned in facts.returns:
        if refs_local(returned.ids) and not contained_collection_call(returned.span):
            return None

    # Valida chamadas antes de interpretar estado. Operação desconhecida no
    # container e escape real sempre abortam. Uma leitura aninhada suportada
    # (`sink(map.get("k"))`) não é escape do mapa.
    for call in facts.calls:
        if call.callee and (_JAVA_LIST_CTOR.match(call.callee)
                            or _JAVA_MAP_CTOR.match(call.callee)):
            continue
        receiver, method = (call.qualified.rsplit(".", 1)
                            if call.qualified and "." in call.qualified
                            else (None, call.callee))
        local_args = [paths for _index, paths in call.args
                      if refs_local(paths)]
        if local_args:
            nested = contained_collection_call(call.span, exclude=call)
            # More than one collection-bearing argument is ambiguous without
            # per-argument AST spans (`f(map, map.get("k"))` must be treated as
            # an escape). A single argument is accepted only when its IDs show
            # the result of the supported nested operation, not the map alone.
            operation_id = any(
                path and path[0] in {"get", "put", "remove"}
                for path in local_args[0]
            )
            if len(local_args) != 1 or not nested or not operation_id:
                return None
        if receiver in local_lists:
            context, in_loop = span_context(call.span)
            if (method not in {"add", "remove", "get"} or in_loop
                    or context != list_contexts.get(receiver)):
                return None
        elif receiver in local_maps:
            if (method not in {"put", "get", "remove"}
                    or span_is_conditional(call.span)):
                return None
        elif not allow_unrelated_calls:
            return None

    cloned_assigns = [replace(a, rhs_ids=set(a.rhs_ids)) for a in facts.assigns]
    cloned_calls = [replace(c, args=[(i, set(ids)) for i, ids in c.args])
                    for c in facts.calls]
    cloned_returns = [replace(r, ids=set(r.ids)) for r in facts.returns]
    events = []
    for call in cloned_calls:
        events.append(((call.span or (0, 0))[0], 1, call))
    for assignment in cloned_assigns:
        events.append(((assignment.span or (0, 0))[0], 0, assignment))
    # Chamada antes da atribuição que recebe seu retorno.
    events.sort(key=lambda event: (event[0], -event[1]))

    for _pos, kind, fact in events:
        if kind == 0:
            continue
        call = fact
        if call.callee and (_JAVA_LIST_CTOR.match(call.callee)
                            or _JAVA_MAP_CTOR.match(call.callee)):
            continue
        if not call.qualified or "." not in call.qualified:
            continue
        receiver, method = call.qualified.rsplit(".", 1)
        if receiver not in local_names:
            continue
        args = dict(call.args)
        if receiver in local_lists and method == "add":
            if set(args) != {0}:
                return None
            local_lists[receiver].append(bool(args[0]))
            continue

        dirty_result = False
        if receiver in local_lists:
            state = local_lists[receiver]
            raw_index = call.arg_values.get(0, "").strip()
            if not _JAVA_INT_LITERAL.match(raw_index):
                return None
            index = int(raw_index)
            if index < 0 or index >= len(state):
                return None
            dirty_result = state[index]
            if method == "remove":
                state.pop(index)
        else:
            state = local_maps[receiver]
            raw_key = call.arg_values.get(0, "").strip()
            if not _JAVA_MAP_KEY_LITERAL.match(raw_key):
                return None
            if method == "put":
                if set(args) != {0, 1}:
                    return None
                dirty_result = state.get(raw_key, False)  # previous value
                state[raw_key] = bool(args[1])
            elif method == "get":
                if set(args) != {0}:
                    return None
                dirty_result = state.get(raw_key, False)
            else:  # remove(key) returns the removed value
                if set(args) != {0}:
                    return None
                dirty_result = state.pop(raw_key, False)

        if not dirty_result:
            continue
        assignment = next((a for a in cloned_assigns
                           if a.span and call.span
                           and a.span[0] <= call.span[0] < a.span[1]), None)
        if assignment is not None:
            assignment.rhs_ids.add(_JAVA_COLLECTION_SENTINEL)
            continue
        returned = next((r for r in cloned_returns
                         if r.span and call.span
                         and r.span[0] <= call.span[0] < r.span[1]), None)
        if returned is not None:
            returned.ids.add(_JAVA_COLLECTION_SENTINEL)
            continue
        # Nested `sink(map.get("k"))`: mark the enclosing argument that
        # references this receiver.
        for outer in cloned_calls:
            if not outer.span or not call.span or outer is call:
                continue
            if outer.span[0] <= call.span[0] < call.span[1] <= outer.span[1]:
                for _idx, paths in outer.args:
                    if any(path and path[0] == receiver for path in paths):
                        paths.add(_JAVA_COLLECTION_SENTINEL)
        # A standalone `put`/`get`/`remove` may discard its return value. The
        # collection state change above still applies; there is no scalar flow
        # to mark in that case.

    refined = replace(facts, assigns=cloned_assigns, calls=cloned_calls,
                      returns=cloned_returns)
    initial = set(tainted) | {_JAVA_COLLECTION_SENTINEL}
    return analyze(refined, initial, sanitizers, lang="java", sources=sources,
                   nonprop=nonprop, source_spans=source_spans)


def analyze_java_constant_list(facts: FnFacts, tainted,
                               sanitizers=frozenset()):
    """Compatibilidade para callers antigos do domínio de listas."""
    return analyze_java_constant_collections(facts, tainted, sanitizers)


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


def _rhs_qualified(source: bytes, node, call_types) -> str | None:
    """`request.POST.get("q")` → "POST.get". None se o RHS não for chamada."""
    if node is None or node.type not in call_types:
        return None
    recv = _receiver_last(source, node)
    if not recv:
        return None
    fn = None
    for f in ("function", "name", "method"):
        fn = node.child_by_field_name(f)
        if fn is not None:
            break
    if fn is None:
        return None
    meth = _text(source, fn).rsplit(".", 1)[-1].split("(", 1)[0].strip()
    return f"{recv}.{meth}" if meth and meth != recv else None


def _rhs_receiver_type(source: bytes, node, call_types,
                       declared_types: dict[str, str]) -> str | None:
    """Resolve the declared type of a simple receiver without a type solver."""
    if node is None or node.type not in call_types:
        return None
    obj = node.child_by_field_name("object")
    if obj is None:
        return None
    receiver = _text(source, obj).strip()
    if re.fullmatch(r"[A-Za-z_$][\w$]*", receiver):
        return declared_types.get(receiver)
    if re.fullmatch(r"(?:this|super)\.[A-Za-z_$][\w$]*", receiver):
        return declared_types.get(receiver.rsplit(".", 1)[-1])
    # A constructor receiver is stronger evidence than an unresolved call-graph
    # edge: ``new ConcreteSource().read()`` names the concrete runtime class in
    # the syntax itself.  Keep this lexical and fail closed (casts, factories,
    # conditional receivers and arbitrary call chains remain unresolved).
    created = obj
    while created.type == "parenthesized_expression":
        named = created.named_children
        if len(named) != 1:
            return None
        created = named[0]
    if created.type == "object_creation_expression":
        typ = created.child_by_field_name("type")
        if typ is None:
            return None
        raw = _text(source, typ).strip().split("<", 1)[0].strip()
        if not re.fullmatch(
                r"[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*", raw):
            return None
        name = re.sub(r"\s+", "", raw)
        if "." in name:
            return name
        text = source.decode("utf-8", "replace")
        explicit = re.findall(
            rf"(?m)^\s*import\s+(?!static\b)"
            rf"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.{re.escape(name)})\s*;",
            text,
        )
        if len(set(explicit)) == 1:
            return explicit[0]
        package = re.search(
            r"(?m)^\s*package\s+"
            r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;",
            text,
        )
        return f"{package.group(1)}.{name}" if package else name
    return None


def _java_receiver_kind(source: bytes, node) -> str:
    """Classify only receiver forms whose object identity is syntactic."""
    if node is None:
        return "unknown"
    if node.type == "object_creation_expression":
        return "other"
    obj = node.child_by_field_name("object")
    if obj is None:
        return "implicit_this"
    return ("explicit_this"
            if _text(source, obj).strip() == "this" else "other")


def _nested_calls(source: bytes, node, call_types, idset,
                  declared_types: dict[str, str], guards=()) -> list[NestedCall]:
    """Collect calls inside an expression and remember sanitizer ancestry."""
    if node is None:
        return []
    out: list[NestedCall] = []
    child_guards = guards
    if node.type in call_types:
        callee = _callee_of(source, node, idset)
        out.append(NestedCall(
            callee,
            _rhs_qualified(source, node, call_types),
            _rhs_receiver_type(source, node, call_types, declared_types),
            tuple(guards),
        ))
        child_guards = guards + (callee,)
    for child in node.named_children:
        out.extend(_nested_calls(source, child, call_types, idset,
                                 declared_types, child_guards))
    return out


def _receiver_last(source: bytes, call_node) -> str | None:
    """Último segmento do RECEPTOR da chamada.

    `response.getWriter().println` → "getWriter"; `System.out.println` → "out".
    É a informação mínima que separa um sink real de um homônimo inofensivo,
    sem precisar de inferência de tipos."""
    obj = None
    for f in ("object", "receiver", "operand"):
        obj = call_node.child_by_field_name(f)
        if obj is not None:
            break
    # Python e JS não têm campo de receptor: `function` guarda o callee INTEIRO
    # (`res.redirect`), então o receptor é o que vem ANTES do último segmento.
    # Sem essa distinção o resultado era o PRÓPRIO método — e como quem chama
    # descarta `recv == callee`, todo `qualified` fora do Java saía None. A
    # regra qualificada existia desde o começo, mas só o Java a exercitava:
    # `res.redirect`, `fs.readFile` e `POST.get` nunca chegaram a casar.
    whole = obj is None
    if whole:
        obj = call_node.child_by_field_name("function")
    if obj is None:
        return None
    txt = _text(source, obj).strip()
    if whole:
        head, sep, _method = txt.rpartition(".")
        if not sep:
            return None                    # chamada sem receptor: `eval(x)`
        txt = head
    txt = txt.split("(", 1)[0] if txt.endswith(")") is False else txt
    # remove a lista de argumentos de uma chamada encadeada: `a.b(x)` → `a.b`
    if txt.endswith(")"):
        depth, cut = 0, len(txt)
        for i in range(len(txt) - 1, -1, -1):
            if txt[i] == ")":
                depth += 1
            elif txt[i] == "(":
                depth -= 1
                if depth == 0:
                    cut = i
                    break
        txt = txt[:cut]
    seg = txt.rsplit(".", 1)[-1].strip()
    return seg or None


def _callee_site(call_node):
    """Nó do NOME do callee — é dele que sai a linha reportada.

    Numa cadeia fluente quebrada em linhas (`Todo.\\n find({}).\\n exec(cb)`) a
    expressão de chamada COMEÇA em `Todo`, três linhas antes do `exec`. Usar o
    início da expressão dava um achado apontando para uma linha onde o sink não
    aparece — e a promessa do produto é exatamente que a linha se confira. É
    também a posição em que o extractor grava a aresta `calls`, então usar a
    mesma casa o fato com a aresta."""
    fn = None
    for f in ("function", "name", "method", "target"):
        fn = call_node.child_by_field_name(f)
        if fn is not None:
            break
    if fn is None:
        return call_node
    fld = _MEMBER_UNWRAP.get(fn.type)
    seg = fn.child_by_field_name(fld) if fld else None
    return seg if seg is not None else fn


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
    body = fn if fn.type in _ROOT_TYPES else fn.child_by_field_name("body")
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
        rhs_q = _rhs_qualified(source, right, {"call"})
        facts.assigns.append(Assign(_target_paths(source, left, _PY_MEMBER), rids,
                                    a.type == "augmented_assignment", rhs_call,
                                    a.start_point[0] + 1,
                                    (a.start_byte, a.end_byte), rhs_q,
                                    _const_text(source, right),
                                    _ternary(source, right, _py_colher_de(source))))

    calls: list = []
    _walk(body, {"call"}, stop, calls)
    for call in calls:
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        callee = _callee_name(source, call.child_by_field_name("function"), "py")
        recv = _receiver_last(source, call)
        cs = CallSite(callee, _callee_site(call).start_point[0] + 1, [],
                      (call.start_byte, call.end_byte),
                      f"{recv}.{callee}" if recv and recv != callee else None)
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
                reads = _source_reads(
                    source, val, lambda s, n: _chain_path(s, n, _PY_MEMBER),
                    _PY_MEMBER, {"call"},
                    lambda s, n: _callee_name(
                        s, n.child_by_field_name("function"), "py"))
                if reads:
                    cs.arg_sources[idx] = reads
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
        facts.returns.append(ReturnExpr(ids, top_call, (r.start_byte, r.end_byte)))
    facts.regions = _build_regions(body, "py", source, facts.assigns)
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
    body = fn if fn.type in _ROOT_TYPES else fn.child_by_field_name("body")
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
                                    rhs_call_name(value), d.start_point[0] + 1,
                                    (d.start_byte, d.end_byte),
                                    _rhs_qualified(source, value,
                                                   {"call_expression"}),
                                    _const_text(source, value),
                                    _ternary(source, value, _js_colher_de(source))))

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
                                    rhs_call_name(right), a.start_point[0] + 1,
                                    (a.start_byte, a.end_byte),
                                    _rhs_qualified(source, right,
                                                   {"call_expression"}),
                                    _const_text(source, right),
                                    _ternary(source, right, _js_colher_de(source))))

    calls: list = []
    _walk(body, {"call_expression"}, stop, calls)
    for call in calls:
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        callee = _callee_name(source, call.child_by_field_name("function"), "js")
        recv = _receiver_last(source, call)
        cs = CallSite(callee, _callee_site(call).start_point[0] + 1, [],
                      (call.start_byte, call.end_byte),
                      f"{recv}.{callee}" if recv and recv != callee else None)
        pos = 0
        for arg in args.named_children:
            if arg.type == "comment":
                continue
            ids: set = set()
            _paths(source, arg, ids, _JS_MEMBER)
            reads = _source_reads(
                source, arg, lambda s, n: _chain_path(s, n, _JS_MEMBER),
                _JS_MEMBER, {"call_expression"},
                lambda s, n: _callee_name(
                    s, n.child_by_field_name("function"), "js"))
            if reads:
                cs.arg_sources[pos] = reads
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
        facts.returns.append(ReturnExpr(ids, top_call, (r.start_byte, r.end_byte)))
    facts.regions = _build_regions(body, "js", source, facts.assigns)
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
    if t in {"this", "this_expression"}:
        return ("this",)
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
    if fn.type in _ROOT_TYPES:
        return fn                      # arquivo inteiro: o corpo é a raiz
    b = fn.child_by_field_name("body")
    if b is not None:
        return b
    for c in fn.named_children:
        if c.type in _BODY_TYPES:
            return c
    return None


def _java_declared_types(source: bytes, fn, body, stop) -> dict[str, str]:
    """Collect unambiguous Java receiver types visible in a function.

    This is deliberately lexical and fail-closed: if a name is declared with
    multiple types (shadowing), it is omitted instead of guessing. It covers
    parameters, locals, try-with-resources and enhanced-for bindings, which are
    all represented with explicit types by tree-sitter-java.
    """
    candidates: dict[str, set[str]] = {}

    text = source.decode("utf-8", "replace")
    package_match = re.search(
        r"(?m)^\s*package\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;",
        text,
    )
    package_name = package_match.group(1) if package_match else None
    imported: dict[str, set[str]] = {}
    for match in re.finditer(
            r"(?m)^\s*import\s+(?!static\b)"
            r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*;",
            text):
        fqn = match.group(1)
        imported.setdefault(fqn.rsplit(".", 1)[-1], set()).add(fqn)
    explicit_imports = {
        simple: next(iter(fqns))
        for simple, fqns in imported.items()
        if len(fqns) == 1
    }
    wildcard_imports = set(re.findall(
        r"(?m)^\s*import\s+(?!static\b)"
        r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\.\*\s*;",
        text,
    ))
    # Only source-relevant JDK classes are inferred through a wildcard.  This
    # is intentionally not a generic wildcard resolver: arbitrary packages or
    # application classes remain ambiguous without the semantic index.
    java_io_types = {
        "BufferedReader", "Console", "DataInputStream",
        "LineNumberReader", "RandomAccessFile",
    }

    def normalized(node) -> str | None:
        if node is None:
            return None
        raw = _text(source, node).strip().split("<", 1)[0].strip()
        raw = raw.replace("[]", "").strip()
        match = re.search(
            r"([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)$",
            raw,
        )
        if match is None:
            return None
        name = re.sub(r"\s+", "", match.group(1))
        if "." in name:
            return name
        if name in explicit_imports:
            return explicit_imports[name]
        if "java.io" in wildcard_imports and name in java_io_types:
            return f"java.io.{name}"
        # A simple name in a named package resolves to that package unless an
        # explicit single-type import above proves otherwise.  Wildcard
        # imports are intentionally not guessed: a security source requiring
        # an FQN must fail closed when lexical evidence cannot disambiguate it.
        return f"{package_name}.{name}" if package_name else name

    def add(name_node, type_node):
        if name_node is None:
            return
        name = _text(source, name_node).strip()
        typ = normalized(type_node)
        if re.fullmatch(r"[A-Za-z_$][\w$]*", name) and typ:
            candidates.setdefault(name, set()).add(typ)

    params = fn.child_by_field_name("parameters")
    if params is not None:
        for param in params.named_children:
            add(param.child_by_field_name("name"),
                param.child_by_field_name("type"))

    # Include direct fields of the enclosing Java type. `_rhs_receiver_type`
    # accepts `this.reader`, and a local shadow with a different type makes the
    # name ambiguous below rather than silently selecting either declaration.
    class_body = fn.parent
    while class_body is not None and class_body.type != "class_body":
        class_body = class_body.parent
    if class_body is not None:
        for declaration in class_body.named_children:
            if declaration.type != "field_declaration":
                continue
            typ = declaration.child_by_field_name("type")
            for child in declaration.named_children:
                if child.type == "variable_declarator":
                    add(child.child_by_field_name("name"), typ)

    declarations: list = []
    _walk(body, {"local_variable_declaration", "resource",
                 "enhanced_for_statement"}, stop, declarations)
    for declaration in declarations:
        typ = declaration.child_by_field_name("type")
        if declaration.type == "local_variable_declaration":
            for child in declaration.named_children:
                if child.type == "variable_declarator":
                    add(child.child_by_field_name("name"), typ)
        else:
            add(declaration.child_by_field_name("name"), typ)

    return {name: next(iter(types)) for name, types in candidates.items()
            if len(types) == 1}


def _java_instance_scope(source: bytes, fn, body, stop):
    """Return direct instance fields and method-local names, fail-closed."""
    fields: set[str] = set()
    locals_: set[str] = set()

    def name_of(node) -> str | None:
        if node is None:
            return None
        name = _text(source, node).strip()
        return name if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else None

    params = fn.child_by_field_name("parameters")
    if params is not None:
        for param in params.named_children:
            name = name_of(param.child_by_field_name("name"))
            if name:
                locals_.add(name)

    class_body = fn.parent
    while class_body is not None and class_body.type != "class_body":
        class_body = class_body.parent
    if class_body is not None:
        for declaration in class_body.named_children:
            if declaration.type != "field_declaration":
                continue
            raw = _text(source, declaration)
            if re.search(r"\bstatic\b", raw.split("=", 1)[0]):
                continue
            for child in declaration.named_children:
                if child.type == "variable_declarator":
                    name = name_of(child.child_by_field_name("name"))
                    if name:
                        fields.add(name)

    declarations: list = []
    _walk(body, {"local_variable_declaration", "resource",
                 "enhanced_for_statement", "catch_formal_parameter"},
          stop, declarations)
    for declaration in declarations:
        if declaration.type == "local_variable_declaration":
            candidates = [
                child.child_by_field_name("name")
                for child in declaration.named_children
                if child.type == "variable_declarator"
            ]
        else:
            candidates = [declaration.child_by_field_name("name")]
        for candidate in candidates:
            name = name_of(candidate)
            if name:
                locals_.add(name)
    return frozenset(fields), frozenset(locals_)


def instance_field_name(facts: FnFacts, path) -> str | None:
    """Canonical field name for an access proven to refer to ``this``."""
    if not isinstance(path, tuple):
        return None
    if (len(path) == 2 and path[0] == "this"
            and path[1] in facts.instance_fields):
        return path[1]
    if (len(path) == 1 and path[0] in facts.instance_fields
            and path[0] not in facts.local_names):
        return path[0]
    return None


def _facts_generic(source, fn, lang) -> FnFacts:
    # fontes que só são fontes NESTA linguagem (`params` do Rails)
    nuas = lang_bare_sources(lang)
    cfg = GEN[lang]
    idset = cfg["id"]
    calls_t = cfg["calls"]
    facts = FnFacts(params=_params_generic(source, fn, cfg, idset))
    body = _body_of(fn)
    if body is None:
        return facts
    stop = _scope_stop(lang)
    declared_types = (_java_declared_types(source, fn, body, stop)
                      if lang == "java" else {})
    if lang == "java":
        fields, locals_ = _java_instance_scope(source, fn, body, stop)
        facts.instance_fields = fields
        facts.local_names = locals_

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
        elif kind == "poslr":
            # gramática sem field names (Kotlin): posicional
            kids = [c for c in n.named_children if c.type != "comment"]
            if len(kids) >= 2:
                targets |= _gen_target_paths(source, kids[0], idset)
                rhs_node = kids[-1]
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
                                    n.start_point[0] + 1,
                                    (n.start_byte, n.end_byte),
                                    _rhs_qualified(source, rhs_node, calls_t),
                                    _const_text(source, rhs_node),
                                    _ternary(source, rhs_node,
                                             lambda n: _gen_colher(source, n, idset)),
                                    any(len(q) == 1 and q[0] in nuas for q in rids),
                                    _rhs_receiver_type(source, rhs_node, calls_t,
                                                       declared_types),
                                    _nested_calls(source, rhs_node, calls_t,
                                                  idset, declared_types)))

    # Iteration is also a data-flow assignment: in `for (T item : values)`,
    # every value observed through `item` came from `values`.  Tree-sitter
    # represents the binding on the loop node rather than as a regular
    # declaration, so the normal assignment walk above cannot see it.
    #
    # Place the synthetic fact on the iterable expression.  The structured
    # CFG evaluates that header before entering its Loop region, which is
    # equivalent for may-taint and also lets a later safe foreach binding kill
    # taint left by an earlier loop using the same variable name.
    foreach = cfg.get("foreach")
    if foreach:
        loop_types, target_field, value_field = foreach
        loops: list = []
        _walk(body, loop_types, stop, loops)
        loop_assigns = []
        for loop in loops:
            target_node = loop.child_by_field_name(target_field)
            value_node = loop.child_by_field_name(value_field)
            targets = _gen_target_paths(source, target_node, idset)
            rids: set = set()
            _gen_paths(source, value_node, idset, rids)
            if not targets or value_node is None or not rids:
                continue
            loop_assigns.append(Assign(
                targets, rids, False,
                _rhs_call(source, value_node, calls_t, idset),
                loop.start_point[0] + 1,
                (value_node.start_byte, value_node.end_byte),
                _rhs_qualified(source, value_node, calls_t),
                _const_text(source, value_node),
                _ternary(source, value_node,
                         lambda n: _gen_colher(source, n, idset)),
                any(len(q) == 1 and q[0] in nuas for q in rids),
                _rhs_receiver_type(source, value_node, calls_t,
                                   declared_types),
                _nested_calls(source, value_node, calls_t, idset,
                              declared_types),
            ))
        facts.assigns.extend(loop_assigns)

    # chamadas
    calls: list = []
    _walk(body, calls_t, stop, calls)
    for call in calls:
        args = _args_of(call)
        callee = _callee_of(source, call, idset)
        recv = _receiver_last(source, call)
        cs = CallSite(callee, _callee_site(call).start_point[0] + 1, [],
                      (call.start_byte, call.end_byte),
                      f"{recv}.{callee}" if recv else None,
                      receiver_kind=(
                          _java_receiver_kind(source, call)
                          if lang == "java" else "unknown"))
        if args is not None:
            pos = 0
            for arg in args.named_children:
                if arg.type in (",", "comment"):
                    continue
                ids: set = set()
                _arg_paths(source, arg, idset, ids)
                reads = _source_reads(
                    source, arg, lambda s, n: _gen_chain(s, n, idset),
                    _GEN_MEMBER, calls_t,
                    lambda s, n: _callee_of(s, n, idset), bare=nuas)
                if reads:
                    cs.arg_sources[pos] = reads
                cs.args.append((pos, ids))
                value = _const_text(source, arg)
                if value is not None:
                    cs.arg_values[pos] = value
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
        facts.returns.append(ReturnExpr(
            ids, top, (r.start_byte, r.end_byte),
            _nested_calls(source, child, calls_t, idset, declared_types),
        ))
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
                # span OBRIGATÓRIO: sem ele o motor flow-sensitive nunca situa
                # este fato numa região e o retorno implícito some (regressão
                # pega pela suíte em Rust/Ruby/Scala, as linguagens com cauda).
                facts.returns.append(ReturnExpr(
                    ids, _rhs_call(source, last, calls_t, idset),
                    (last.start_byte, last.end_byte),
                    _nested_calls(source, last, calls_t, idset, declared_types),
                ))
    facts.regions = _build_regions(body, lang, source, facts.assigns)
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

def analyze_facts(facts: FnFacts, tainted_init, sanitizers=frozenset(),
                  nonprop=frozenset(), source_spans=frozenset()) -> Flow:
    """Fixpoint may-taint FIELD-SENSITIVE. O conjunto sujo guarda *caminhos de
    acesso* (tuplas); ler um caminho está sujo se ele ou qualquer prefixo seu
    estiver sujo (`_is_tainted`). `tainted_init` deve conter caminhos — um nome
    nu `x` é o caminho `("x",)`."""
    tainted: set = {p if isinstance(p, tuple) else (p,) for p in tainted_init}
    changed = True
    while changed:
        changed = False
        for a in facts.assigns:
            if ((a.rhs_call is not None and a.rhs_call in sanitizers)
                    or a.line in nonprop):
                continue  # sanitizado, ou chamada que não devolve o argumento
            rhs_hit = any(_is_tainted(p, tainted) for p in a.rhs_ids)
            aug_hit = a.is_aug and any(_is_tainted(t, tainted) for t in a.targets)
            if rhs_hit or aug_hit or a.span in source_spans:
                for t in a.targets:
                    if t not in tainted:
                        tainted.add(t)
                        changed = True
    flow = Flow()
    for c in facts.calls:
        direto = dict(direct_source_args(c, sanitizers))
        for idx, ids in c.args:
            hit = [p for p in ids if _is_tainted(p, tainted)]
            if hit:
                via = ".".join(sorted(hit)[0])
                flow.arg_flows.append(
                    ArgFlow(c.callee, idx, c.line, via, c.qualified,
                            direto.get(idx), c.span))
            elif idx in direto:
                flow.arg_flows.append(
                    ArgFlow(c.callee, idx, c.line, direto[idx], c.qualified,
                            direto[idx], c.span))
    for r in facts.returns:
        if r.top_call is not None and r.top_call in sanitizers:
            continue
        if any(_is_tainted(p, tainted) for p in r.ids):
            flow.reaches_return = True
            break
    return flow


def source_vars(facts: FnFacts, sources, sanitizers=frozenset(),
                source_spans=frozenset()) -> set:
    """Caminhos cujo valor nasce de uma chamada a uma fonte (input não-confiável)."""
    out: set = set()
    for a in facts.assigns:
        if (assign_reads_named_source(a, sources, sanitizers)
                or assign_reads_framework_source(a, sanitizers)
                or a.span in source_spans):
            out |= a.targets
    return out


def return_reads_named_source(returned: ReturnExpr, sources,
                              sanitizers=frozenset()) -> bool:
    """Whether a return expression contains a configured, unsanitized source."""
    for call in returned.nested_calls:
        typed = (f"{call.receiver_type}.{call.callee}"
                 if call.receiver_type else None)
        if (call.callee in sources or call.qualified in sources
                or typed in sources):
            if not any(guard in sanitizers for guard in call.guards):
                return True
    return False


def source_sites(facts: FnFacts, sources, sanitizers=frozenset(),
                 source_spans=frozenset()) -> list[tuple]:
    """(caminho, linha, fonte) para cada atribuição a partir de uma fonte.
    O caminho é uma tupla (semente para o motor); renderize com '.'.join()."""
    out = []
    for a in facts.assigns:
        rotulo = None
        if assign_reads_named_source(a, sources, sanitizers):
            rotulo = (a.rhs_qualified if a.rhs_qualified in sources
                      else next((label for call in a.nested_calls
                                 if (label := _nested_named_source(
                                     call, sources, sanitizers)) is not None),
                                a.rhs_call))
        elif assign_reads_framework_source(a, sanitizers):
            rotulo = a.rhs_qualified or next(
                (".".join(p) for p in sorted(a.rhs_ids)
                 if is_framework_source_path(p)), "request")
        elif a.span in source_spans:
            rotulo = a.rhs_call or "source-wrapper"
        if rotulo is None:
            continue
        for t in sorted(a.targets):
            out.append((t, a.line, rotulo))
    return out
