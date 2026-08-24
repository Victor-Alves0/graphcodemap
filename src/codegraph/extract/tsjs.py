"""Extractor L0 para TypeScript/TSX/JavaScript (tree-sitter).

Símbolos: funções, arrow functions nomeadas, classes, métodos, interfaces,
type aliases, enums, const/let de módulo.
Refs: calls (com rastreio de import), new (→ calls da classe), imports, inherits,
references (`className=`/`class=` no JSX → seletor definido no CSS/SCSS).
"""

from __future__ import annotations

import posixpath
import re

from ..util import byte_column
from .base import BaseExtractor

_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
# `class` cobre Preact/Solid, que não renomeiam o atributo
_CLASS_ATTRS = {"className", "class"}
_TEST_CALLBACKS = frozenset({"describe", "it", "test", "suite", "context"})
# marcador de trecho interpolado: não pode ser espaço, senão partiria o token
_HOLE = "\x00"


class TsJsExtractor(BaseExtractor):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # Callback anônimo descoberto num call_expression. O registro é feito
        # antes da descida normal e usa offsets estáveis, não ``Node.id``. Mais
        # importante: não percorremos a árvore enquanto iteramos
        # ``arguments.named_children``; bindings nativas do tree-sitter podem
        # invalidar o cursor nessa reentrada em árvores JS grandes.
        self._callbacks: dict[tuple[int, int], str] = {}

    def visit(self, node) -> None:
        t = node.type
        callback_name = self._callbacks.get((node.start_byte, node.end_byte))
        if callback_name is not None and t in _FUNCTION_VALUES:
            self._callback_function(node, callback_name)
            return
        if t == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is not None:
                self.visit(decl)
            else:
                for c in node.named_children:
                    self.visit(c)
            return
        if t == "import_statement":
            self._import(node)
            return
        if t in ("function_declaration", "generator_function_declaration"):
            self._function(node, kind="function")
            return
        if t in ("class_declaration", "abstract_class_declaration"):
            self._class(node)
            return
        if t == "method_definition":
            self._function(node, kind="method" if self.in_class() else "function")
            return
        if t == "public_field_definition" and self.in_class():
            self._field(node)
            return
        if t in ("lexical_declaration", "variable_declaration"):
            self._var_declaration(node)
            return
        if t == "assignment_expression":
            self._assignment(node)
            return
        if t == "interface_declaration":
            self._named_container(node, "interface")
            return
        if t == "type_alias_declaration":
            self._simple_named(node, "type_alias")
            return
        if t == "enum_declaration":
            self._enum(node)
            return
        if t == "call_expression":
            self._call(node)
            self._register_callback_args(node)
            for c in node.children:
                self.visit(c)
            return
        if t == "jsx_attribute":
            self._jsx_attribute(node)
            # SEM return: o valor pode conter chamadas (clsx(...), cn(...))
            # que continuam sendo `calls` como em qualquer outra expressão
        if t == "new_expression":
            ctor = node.child_by_field_name("constructor")
            if ctor is not None and ctor.type == "identifier":
                self.add_ref(node, "calls", self._qualify(self.text(ctor)))
            for c in node.children:
                self.visit(c)
            return
        for c in node.children:
            self.visit(c)

    # -- defs ----------------------------------------------------------------

    def _function(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        vis = self._member_visibility(node, name_node) if kind == "method" else None
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc_comment(node), visibility=vis)
        self.scope.append((name, "function"))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    @staticmethod
    def _member_visibility(node, name_node) -> str:
        """Acessibilidade de um membro de classe. `#campo` é sempre privado;
        senão o `accessibility_modifier` explícito; default TS é public."""
        if name_node.type == "private_property_identifier":   # #secret
            return "private"
        mod = next((c for c in node.children
                    if c.type == "accessibility_modifier"), None)
        return mod.text.decode("utf-8", "replace") if mod is not None else "public"

    def _field(self, node) -> None:
        """Propriedade de classe (`public_field_definition`). Campo-função
        (`onClick = () => …`) é método; o resto é variable/constant."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        value = node.child_by_field_name("value")
        vis = self._member_visibility(node, name_node)
        if value is not None and value.type in _FUNCTION_VALUES:
            body = value.child_by_field_name("body")
            self.add_sym(node, "method", name, signature=self.sig_of(node, body),
                         doc=self._doc_comment(node), visibility=vis)
            self.scope.append((name, "function"))
            self.visit(value)
            self.scope.pop()
            return
        raw = self.text(node)
        kind = "constant" if (name.isupper() or "readonly " in raw) else "variable"
        self.add_sym(node, kind, name, signature=None,
                     doc=self._doc_comment(node), visibility=vis)
        if value is not None:
            self.visit(value)

    def _enum(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "enum", name, signature=self.sig_of(node, body),
                     doc=self._doc_comment(node))
        if body is None:
            return
        self.scope.append((name, "class"))
        for c in body.named_children:
            member = None
            if c.type == "property_identifier":              # `Red`
                member = c
            elif c.type == "enum_assignment":                # `Red = 1`
                member = c.child_by_field_name("name")
            if member is not None:
                self.add_sym(member, "constant", self.text(member),
                             signature=None, doc=None, visibility="public")
        self.scope.pop()

    def _class(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, "class", name, signature=self.sig_of(node, body),
                     doc=self._doc_comment(node))
        self.scope.append((name, "class"))
        for c in node.children:
            if c.type == "class_heritage":
                for h in self._heritage_names(c):
                    self.add_ref(c, "inherits", self._qualify(h))
        if body is not None:
            for c in body.children:
                self.visit(c)
        self.scope.pop()

    def _heritage_names(self, heritage):
        for c in heritage.named_children:
            if c.type in ("identifier", "member_expression", "nested_type_identifier",
                          "type_identifier", "generic_type"):
                yield self.text(c).split("<", 1)[0]
            elif c.type in ("extends_clause", "implements_clause", "class_heritage",
                            "extends_type_clause"):
                yield from self._heritage_names(c)

    def _named_container(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        body = node.child_by_field_name("body")
        self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                     doc=self._doc_comment(node))
        # interface C extends A, B — usa `extends_type_clause`, não `class_heritage`
        for c in node.children:
            if c.type == "extends_type_clause":
                for h in self._heritage_names(c):
                    self.add_ref(c, "inherits", self._qualify(h))

    def _simple_named(self, node, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        self.add_sym(node, kind, self.text(name_node),
                     signature=self.sig_of(node, node.child_by_field_name("body")),
                     doc=self._doc_comment(node))

    def _var_declaration(self, node) -> None:
        is_const = node.type == "lexical_declaration" and self.text(node).lstrip().startswith("const")
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            name_node = decl.child_by_field_name("name")
            value = decl.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier":
                continue
            name = self.text(name_node)
            if value is not None and value.type in _FUNCTION_VALUES:
                body = value.child_by_field_name("body")
                self.add_sym(decl, "function", name,
                             signature=self.sig_of(decl, body),
                             doc=self._doc_comment(node))
                self.scope.append((name, "function"))
                self.visit(value)
                self.scope.pop()
            elif not self.scope:
                kind = "constant" if is_const and name.isupper() else ("constant" if is_const else "variable")
                self.add_sym(decl, kind, name, signature=None, doc=None)
                if value is not None:
                    if value.type == "object":
                        self._object_members(value, name)
                    else:
                        self.visit(value)
            elif value is not None:
                self.visit(value)

    def _object_members(self, node, owner: str) -> None:
        """Funções dentro de object literals viram métodos endereçáveis.

        ``const obj = { m: function () {}, n() {} }`` é um padrão central de
        módulos JS. Antes só existia o símbolo ``obj``; o TypeScript encontrava
        a definição de ``m``, mas a promoção só podia escolher a constante
        envolvente. Modelar ``obj.m`` recupera a aresta sem mentir sobre o alvo.
        """
        self.scope.append((owner, "class"))
        for member in node.named_children:
            if member.type == "method_definition":
                self.visit(member)
                continue
            if member.type != "pair":
                self.visit(member)
                continue
            key = member.child_by_field_name("key")
            value = member.child_by_field_name("value")
            if key is None or value is None:
                continue
            name = self.text(key).strip("'\"")
            if not name:
                continue
            if value.type in _FUNCTION_VALUES:
                body = value.child_by_field_name("body")
                self.add_sym(member, "method", name,
                             signature=self.sig_of(member, body))
                self.scope.append((name, "function"))
                self.visit(value)
                self.scope.pop()
            elif value.type == "object":
                self.add_sym(member, "field", name, signature=None)
                self._object_members(value, name)
            else:
                self.visit(value)
        self.scope.pop()

    def _assignment(self, node) -> None:
        """Definições por atribuição — o idioma de módulos JS clássicos:
        `res.send = function send() {…}`, `Router.prototype.use = fn`,
        `exports.foo = () => {…}`. Sem isso, express e afins ficam invisíveis."""
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or right.type not in _FUNCTION_VALUES:
            for c in node.children:
                self.visit(c)
            return
        if left.type == "member_expression":
            prop = left.child_by_field_name("property")
            obj = left.child_by_field_name("object")
            if prop is None or prop.type != "property_identifier":
                return
            name = self.text(prop)
            chain = self.text(obj) if obj is not None else ""
            pushed = 0
            if chain and not any(ch in chain for ch in "\n(["):
                for part in chain.split("."):
                    if part not in ("module", "exports", "this", "window",
                                    "globalThis") and part:
                        self.scope.append((part, "class"))
                        pushed += 1
            kind = "method" if pushed else "function"
            body = right.child_by_field_name("body")
            self.add_sym(node, kind, name, signature=self.sig_of(node, body),
                         doc=self._doc_comment(node))
            self.scope.append((name, "function"))
            self.visit(right)
            self.scope.pop()
            for _ in range(pushed):
                self.scope.pop()
        elif left.type == "identifier" and not self.scope:
            name = self.text(left)
            body = right.child_by_field_name("body")
            self.add_sym(node, "function", name,
                         signature=self.sig_of(node, body),
                         doc=self._doc_comment(node))
            self.scope.append((name, "function"))
            self.visit(right)
            self.scope.pop()
        else:
            for c in node.children:
                self.visit(c)

    def _register_callback_args(self, node) -> None:
        """Função passada como ARGUMENTO vira símbolo próprio.

        `app.get("/learn", isLoggedIn, (req, res) => {…})` é *o* idioma de rota
        do Node, e a função ali não tem nome para se pendurar. Sem símbolo o
        corpo do handler fica sem dono — e como a varredura de taint itera
        símbolos, o handler inteiro era invisível. Medido em código real: o
        open redirect do NodeGoat (`res.redirect(req.query.url)`) mora
        exatamente dentro de um desses.

        O nome é `callee#índice` (`get#2`): curto, determinístico e sem fingir
        que existe um identificador no código. Nomes repetidos no mesmo escopo
        se separam pelo ordinal, como qualquer homônimo.
        """
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        fn = node.child_by_field_name("function")
        callee = self.text(fn).rsplit(".", 1)[-1].strip() if fn is not None else "call"
        if not callee or any(ch in callee for ch in "\n([?!"):
            return                        # callee não-nomeável: não inventa nome
        idx = 0
        for arg in args.named_children:
            if arg.type == "comment":
                continue
            if arg.type in _FUNCTION_VALUES:
                name_node = arg.child_by_field_name("name")
                name = (self.text(name_node) if name_node is not None else
                        self._anonymous_callback_name(callee, idx, args, arg))
                self._callbacks[(arg.start_byte, arg.end_byte)] = name
            idx += 1

    def _callback_function(self, node, name: str) -> None:
        """Materializa e percorre callback já fora do cursor de argumentos."""
        body = node.child_by_field_name("body")
        self.add_sym(node, "function", name, signature=self.sig_of(node, body))
        self.scope.append((name, "function"))
        for child in node.children:
            self.visit(child)
        self.scope.pop()

    def _anonymous_callback_name(self, callee: str, index: int, args,
                                 callback) -> str:
        """Nome estável e não-colapsável para suites/casos de teste TS/JS.

        ``describe#1`` sozinho se repetia em todas as suites do arquivo; o FQN
        deixava de identificar qual corpo o usuário estava consultando. Para as
        APIs de teste conhecidas, preservamos um rótulo legível e a posição do
        callback. Outros callbacks mantêm o contrato histórico curto.
        """
        base = f"{callee}#{index}"
        if callee.lower() not in _TEST_CALLBACKS:
            return base
        label = ""
        first = next((child for child in args.named_children
                      if child.type != "comment"), None)
        if first is not None and first.type in {"string", "template_string"}:
            raw = self.text(first).strip("'\"`")
            if "${" not in raw:
                label = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
                label = label[:48]
        row = callback.start_point[0]
        col = byte_column(self.source, callback.start_byte)
        readable = f":{label}" if label else ""
        return f"{base}{readable}@{row + 1}:{col}"

    def _doc_comment(self, node) -> str | None:
        prev = node.prev_sibling
        if prev is not None and prev.type == "comment":
            raw = self.text(prev)
            if raw.startswith("/**"):
                lines = [ln.strip().lstrip("*").strip() for ln in raw[3:-2].splitlines()]
                return "\n".join(ln for ln in lines if ln).strip() or None
        return None

    # -- imports -------------------------------------------------------------

    def _import(self, node) -> None:
        source = node.child_by_field_name("source")
        if source is None:
            return
        spec = self.text(source).strip("'\"`")
        module = self._module_from_source(spec)
        # `import "./styles.css"` é o padrão de todo app React, e virar fqn
        # pontilhado (`src.styles.css`) destruía a única coisa útil ali: o
        # caminho. Para ASSET relativo o alvo é o arquivo, então o ref preserva
        # o caminho; `module` continua sendo o fqn para os aliases, que servem
        # a outra coisa (qualificar chamadas).
        target = spec if self._is_relative_asset(spec) else module
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            self.add_ref(node, "imports", target)
            return
        for c in clause.named_children:
            if c.type == "identifier":  # default import
                self.aliases[self.text(c)] = f"{module}.{self.text(c)}"
                self.add_ref(node, "imports", target)
            elif c.type == "namespace_import":
                ident = next((n for n in c.named_children if n.type == "identifier"), None)
                if ident is not None:
                    self.aliases[self.text(ident)] = module
                    self.add_ref(node, "imports", target)
            elif c.type == "named_imports":
                for spec in c.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name_node = spec.child_by_field_name("name")
                    alias_node = spec.child_by_field_name("alias")
                    if name_node is None:
                        continue
                    name = self.text(name_node)
                    local = self.text(alias_node) if alias_node is not None else name
                    self.aliases[local] = f"{module}.{name}"
                    self.add_ref(node, "imports", f"{module}.{name}")

    @staticmethod
    def _is_relative_asset(spec: str) -> bool:
        """`./x.css` sim; `./utils` e `./a.ts` não (módulo, resolve por fqn);
        `react` não (specifier bare — resolveria para node_modules)."""
        if not spec.startswith("."):
            return False
        ext = posixpath.splitext(spec)[1]
        return bool(ext) and ext not in _JS_EXTS

    def _module_from_source(self, spec: str) -> str:
        if not spec.startswith("."):
            return spec.replace("/", ".")
        # relativo ao diretório do módulo atual
        cur_dir = "/".join(self.module_fqn.split(".")[:-1])
        joined = posixpath.normpath(posixpath.join(cur_dir, spec))
        for ext in _JS_EXTS:
            if joined.endswith(ext):
                joined = joined[: -len(ext)]
                break
        return joined.replace("/", ".").lstrip(".")

    # -- uso de estilo (JSX) --------------------------------------------------

    def _jsx_attribute(self, node) -> None:
        """`className="card btn"` → uma referência por classe usada.

        Quem DEFINE a classe é a folha de estilo (`.card` vira `css_class`);
        a marcação apenas usa. O resolver religa esses nomes cross-language.
        """
        key = node.named_children[0] if node.named_children else None
        if key is None or key.type != "property_identifier":
            return
        if self.text(key) not in _CLASS_ATTRS:
            return
        for value in node.named_children[1:]:
            tokens: list[str] = []
            self._collect_classes(value, tokens)
            for token in tokens:
                self.add_ref(value, "references", token)

    def _collect_classes(self, node, out: list[str]) -> None:
        """Nomes de classe ESTÁTICOS sob um valor de atributo.

        `className={styles.card}` (CSS Modules) não tem literal → nada a
        registrar, e é melhor não registrar do que inventar.
        """
        t = node.type
        if t == "template_string":
            # um pedaço colado numa interpolação (`col-${n}`) é PREFIXO, não
            # nome de classe: monta o texto com um furo e descarta o que o toca
            parts: list[str] = []
            for c in node.children:
                if c.type == "string_fragment":
                    parts.append(self.text(c))
                elif c.type == "template_substitution":
                    parts.append(_HOLE)
                    self._collect_classes(c, out)   # literais dentro do ${...}
            out.extend(tok for tok in "".join(parts).split() if _HOLE not in tok)
            return
        if t == "string":
            for c in node.named_children:
                if c.type == "string_fragment":
                    out.extend(self.text(c).split())
            return
        for c in node.named_children:
            self._collect_classes(c, out)

    # -- calls ---------------------------------------------------------------

    def _call(self, node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            # ref na posição do NOME (resolvers L1 resolvem por linha+coluna)
            self.add_ref(fn, "calls", self._qualify(self.text(fn)))
        elif fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            site = prop if prop is not None else fn
            dotted = self.text(fn)
            if any(ch in dotted for ch in "\n([?!"):
                if prop is not None:
                    self.add_ref(prop, "calls", self.text(prop))
                return
            base, _, rest = dotted.partition(".")
            if base == "this":
                self.add_ref(site, "calls", rest.rsplit(".", 1)[-1] if rest else dotted)
            elif base in self.aliases:
                self.add_ref(site, "calls", f"{self.aliases[base]}.{rest}" if rest else self.aliases[base])
            else:
                self.add_ref(site, "calls", dotted.rsplit(".", 1)[-1])
