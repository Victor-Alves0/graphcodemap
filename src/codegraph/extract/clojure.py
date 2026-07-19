"""Extractor L0 dedicado para Clojure/ClojureScript (tree-sitter).

Clojure é um Lisp: toda forma é um ``list_lit`` cuja *cabeça* (primeiro
``sym_lit``) decide o que ela é. Não existe um nó ``function_declaration`` —
o significado é semântico. Este extractor interpreta as formas de definição
pela cabeça:

- ``ns``                         → define o namespace (vira o module_fqn) e as
                                   dependências (:require [... :as/:refer ...]);
- ``def`` / ``defonce`` / ``defsetting`` (macros de definição) → constante/variável;
- ``defn`` / ``defn-`` / ``defmacro`` / ``defmulti`` → função;
- ``defmethod``                  → método (nome = multifn + dispatch-val);
- ``defprotocol`` / ``definterface`` → interface;
- ``defrecord`` / ``deftype``    → classe;
- ``defendpoint`` (api.macros)   → função-endpoint (nome = método + rota).

Refs: ``calls`` (cabeça de cada forma aplicada, resolvendo alias ns/), e
``imports`` (cada spec de :require). Símbolos namespaced ``alias/nome`` têm o
alias reescrito para o fqn do namespace via mapa de :require, casando com o fqn
das definições (``myapp.util.net.check-host``).
"""

from __future__ import annotations

from .base import BaseExtractor

# formas de definição: cabeça (sym_name, ignorando namespace) -> kind do símbolo
_DEF_KINDS = {
    "def": "constant", "defonce": "constant", "defsetting": "constant",
    "def-": "constant",
    "defn": "function", "defn-": "function", "defmacro": "function",
    "defmulti": "function", "deftest": "function", "deftask": "function",
    "defprotocol": "interface", "definterface": "interface",
    "defrecord": "class", "deftype": "class",
}

# formas especiais / macros de fluxo que NÃO são arestas de chamada úteis.
_STOPWORDS = frozenset({
    "ns", "let", "letfn", "if", "if-not", "if-let", "if-some", "when",
    "when-not", "when-let", "when-some", "when-first", "cond", "condp",
    "case", "cond->", "cond->>", "do", "doto", "fn", "fn*", "loop", "recur",
    "for", "doseq", "dotimes", "while", "try", "catch", "finally", "throw",
    "and", "or", "not", "quote", "var", "new", "set!", "binding", "locking",
    "->", "->>", "some->", "some->>", "as->", "comment", "declare",
    "assert", "lazy-seq", "delay", "future", "reify", "proxy",
})


class ClojureExtractor(BaseExtractor):
    def visit(self, node) -> None:
        if node.type == "list_lit":
            self._form(node)
            return
        for c in node.named_children:
            self.visit(c)

    # -- helpers de símbolo ---------------------------------------------------

    @staticmethod
    def _sym_name_node(sym_lit):
        return sym_lit.child_by_field_name("name") or next(
            (c for c in sym_lit.children if c.type == "sym_name"), None)

    def _sym_parts(self, sym_lit):
        """(namespace|None, nome|None, is_private) de um sym_lit (ignora meta)."""
        if sym_lit is None or sym_lit.type != "sym_lit":
            return None, None, False
        ns = next((c for c in sym_lit.children if c.type == "sym_ns"), None)
        nm = self._sym_name_node(sym_lit)
        meta = next((c for c in sym_lit.children if c.type == "meta_lit"), None)
        private = meta is not None and ":private" in self.text(meta)
        return (self.text(ns) if ns else None,
                self.text(nm) if nm else None,
                private)

    def _qualify_clj(self, ns_part, name):
        if ns_part:
            mapped = self.aliases.get(ns_part, ns_part)
            return f"{mapped}.{name}"
        if name in self.aliases:            # nome trazido por :refer
            return self.aliases[name]
        return name

    # -- formas ---------------------------------------------------------------

    def _form(self, node) -> None:
        kids = [c for c in node.named_children]
        head = kids[0] if kids else None
        if head is None or head.type != "sym_lit":
            # não é uma forma aplicada nomeada (ex.: literal); desce mesmo assim
            for c in node.named_children:
                self.visit(c)
            return
        _, op, _ = self._sym_parts(head)
        if op == "ns":
            self._ns(node, kids)
            return
        if op == "defmethod":
            self._defmethod(node, kids)
            return
        if op == "defendpoint":
            self._defendpoint(node, kids)
            return
        if op in _DEF_KINDS:
            self._def(node, kids, op)
            return
        # forma comum → possível chamada da cabeça
        if op and op not in _STOPWORDS:
            self.add_ref(self._sym_name_node(head) or head, "calls",
                         self._qualify_clj(*self._sym_parts(head)[:2]))
        for c in node.named_children[1:]:
            self.visit(c)

    def _ns(self, node, kids) -> None:
        # nome do namespace = primeiro sym_lit após a cabeça
        name_sym = next((k for k in kids[1:] if k.type == "sym_lit"), None)
        _, nsname, _ = self._sym_parts(name_sym)
        if nsname:
            self.module_fqn = nsname       # fqn das defs passa a usar o ns real
        # :require dentro do ns
        for k in kids[1:]:
            if k.type != "list_lit":
                continue
            khead = next((c for c in k.named_children), None)
            if khead is not None and khead.type == "kwd_lit" and \
                    ":require" in self.text(khead):
                for spec in k.named_children[1:]:
                    self._require_spec(spec)

    def _require_spec(self, spec) -> None:
        """[fqn :as alias] | [fqn :refer [a b]] | [fqn] | fqn"""
        if spec.type == "sym_lit":
            _, nm, _ = self._sym_parts(spec)
            fqn = spec.child_by_field_name("name")
            full = self.text(spec)
            if full:
                self.aliases.setdefault(full.rsplit(".", 1)[-1], full)
                self.add_ref(spec, "imports", full)
            return
        if spec.type != "vec_lit":
            return
        items = [c for c in spec.named_children]
        if not items or items[0].type != "sym_lit":
            return
        fqn = self.text(items[0])
        self.add_ref(items[0], "imports", fqn)
        self.aliases.setdefault(fqn.rsplit(".", 1)[-1], fqn)
        i = 1
        while i < len(items):
            it = items[i]
            if it.type == "kwd_lit":
                key = self.text(it)
                if ":as" in key and i + 1 < len(items):
                    alias = self.text(items[i + 1])
                    self.aliases[alias] = fqn
                    i += 2
                    continue
                if ":refer" in key and i + 1 < len(items) and \
                        items[i + 1].type == "vec_lit":
                    for r in items[i + 1].named_children:
                        _, rn, _ = self._sym_parts(r)
                        if rn:
                            self.aliases[rn] = f"{fqn}.{rn}"
                    i += 2
                    continue
            i += 1

    def _def(self, node, kids, op) -> None:
        name_sym = kids[1] if len(kids) > 1 else None
        _, name, private = self._sym_parts(name_sym)
        if not name:
            return
        kind = _DEF_KINDS[op]
        vis = "private" if (private or op.endswith("-")) else None
        # assinatura: cabeça + nome + primeiro vetor de params (se houver)
        params = self._first_param_vec(kids[2:])
        sig = f"({op} {name}{' ' + self.text(params) if params else ''})"
        self.add_sym(node, kind, name, signature=sig, visibility=vis)
        self.scope.append((name, "class" if kind in ("class", "interface") else "function"))
        for c in node.named_children[2:]:
            self.visit(c)
        self.scope.pop()

    def _defmethod(self, node, kids) -> None:
        _, multifn, _ = self._sym_parts(kids[1]) if len(kids) > 1 else (None, None, False)
        if not multifn:
            return
        dispatch = self.text(kids[2]).strip() if len(kids) > 2 else ""
        disp = dispatch.lstrip(":")
        name = f"{multifn}:{disp}" if disp and dispatch[0] not in "([" else multifn
        self.add_sym(node, "method", name, signature=f"(defmethod {multifn} {dispatch})")
        self.scope.append((name, "function"))
        for c in node.named_children[2:]:
            self.visit(c)
        self.scope.pop()

    def _defendpoint(self, node, kids) -> None:
        method = self.text(kids[1]).strip(": ") if len(kids) > 1 else ""
        route = self.text(kids[2]).strip('"') if len(kids) > 2 else ""
        name = f"{method.upper()}_{route}" if route else (method or "endpoint")
        self.add_sym(node, "function", name,
                     signature=f"(defendpoint {method} {route})")
        self.scope.append((name, "function"))
        for c in node.named_children[3:]:
            self.visit(c)
        self.scope.pop()

    @staticmethod
    def _first_param_vec(nodes):
        for n in nodes:
            if n.type == "vec_lit":
                return n
            if n.type == "list_lit":          # multi-arity: ([params] ...)
                for c in n.named_children:
                    if c.type == "vec_lit":
                        return c
                    break
        return None
