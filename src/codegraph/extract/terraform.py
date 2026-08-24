"""Extractor L0 dedicado para Terraform / HCL (tree-sitter).

HCL é orientado a BLOCOS: ``resource "type" "name" { ... }``. O primeiro
identifier decide o tipo do bloco; os rótulos (``string_lit``) formam o
ENDEREÇO pelo qual ele é referenciado no resto do módulo. Este extractor modela
o grafo de dependência real do Terraform:

- ``resource "T" "N"``  → símbolo ``resource``  endereçado ``T.N``;
- ``data "T" "N"``      → símbolo ``data``      endereçado ``data.T.N``;
- ``variable "N"``      → símbolo ``variable``  endereçado ``var.N``;
- ``output "N"``        → símbolo ``output``    endereçado ``output.N``;
- ``module "N"``        → símbolo ``module``    (+ ``imports`` do ``source``);
- ``provider "N"``      → símbolo ``provider``  endereçado ``provider.N``;
- ``locals { a = … }``  → cada atributo vira símbolo ``local`` (``local.a``).

Dentro de qualquer valor, uma travessia ``var.x`` / ``local.x`` / ``module.x`` /
``data.T.N`` / ``T.N`` (recurso) vira uma aresta ``references`` para aquele
endereço — "quem depende de quem". O fqn de cada símbolo embute o endereço
(``<módulo>.aws_instance.web``), então a resolução por sufixo de fqn liga a
referência à definição mesmo entre arquivos do mesmo diretório (um módulo TF é
um diretório: os ``.tf`` compartilham o namespace).
"""

from __future__ import annotations

from ..util import byte_column, content_hash
from .base import BaseExtractor, Sym

# tipo de bloco → (kind, prefixo de endereço, nº de rótulos). Prefixo None =
# o endereço são os PRÓPRIOS rótulos (recurso: ``T.N``).
_LABELED = {
    "resource": ("resource", None, 2),
    "data": ("data", "data", 2),
    "variable": ("variable", "var", 1),
    "output": ("output", "output", 1),
    "module": ("module", "module", 1),
    "provider": ("provider", "provider", 1),
}

# heads de travessia que NÃO são referências a símbolos do módulo (symbols
# implícitos do Terraform: iteração, self, caminhos, o próprio bloco terraform).
_TRAVERSAL_STOP = frozenset({"count", "each", "self", "path", "terraform"})


class TerraformExtractor(BaseExtractor):
    def __init__(self, source: bytes, module_fqn: str) -> None:
        super().__init__(source, module_fqn)
        self._loopvars: set[str] = set()   # vars de `for` (não são recursos)

    def visit(self, node) -> None:
        body = next((c for c in node.children if c.type == "body"), None)
        if body is None:
            return
        self._collect_loopvars(body)
        for blk in body.named_children:
            if blk.type == "block":
                self._block(blk)

    # -- helpers de bloco ----------------------------------------------------

    def _collect_loopvars(self, node) -> None:
        if node.type == "for_intro":
            for c in node.named_children:
                if c.type == "identifier":
                    self._loopvars.add(self.text(c))
        for c in node.named_children:
            self._collect_loopvars(c)

    def _labels(self, block) -> list[str]:
        return [self._label(c) for c in block.named_children
                if c.type == "string_lit"]

    def _label(self, string_lit) -> str:
        lit = next((c for c in string_lit.named_children
                    if c.type == "template_literal"), None)
        return self.text(lit) if lit is not None else self.text(string_lit).strip('"')

    @staticmethod
    def _body_of(block):
        return next((c for c in block.named_children if c.type == "body"), None)

    def _block(self, block) -> None:
        kids = block.named_children
        head = kids[0] if kids else None
        if head is None or head.type != "identifier":
            return
        btype = self.text(head)
        body = self._body_of(block)
        if btype == "locals":
            self._locals(body)
            return
        spec = _LABELED.get(btype)
        if spec is None:
            # bloco de config (terraform{}, moved{}, …): sem símbolo, mas o corpo
            # ainda pode conter travessias — colhe no nível de módulo.
            if body is not None:
                self._walk_expr(body)
            return
        kind, prefix, nlabels = spec
        labels = self._labels(block)
        if len(labels) < nlabels:
            return
        address = ".".join([prefix, *labels[:nlabels]] if prefix
                           else labels[:nlabels])
        doc = self._string_attr(body, "description") if body is not None else None
        self._define(block, kind, address,
                     signature=self._header(block, body), doc=doc)
        if btype == "module" and body is not None:
            src = self._string_attr(body, "source")
            if src:
                self.add_ref(head, "imports", src)
        if body is not None:
            self._collect_body(body, address)

    def _header(self, block, body) -> str:
        end = body.start_byte if body is not None else block.end_byte
        return self.source[block.start_byte:end].decode("utf-8", "replace") \
            .replace("{", "").strip()

    def _define(self, node, kind: str, address: str, *,
                signature: str | None, doc: str | None) -> None:
        fqn = ".".join(p for p in (self.module_fqn, address) if p)
        self.syms.append(Sym(
            kind=kind, name=address.rsplit(".", 1)[-1], fqn=fqn, parent_fqn=None,
            signature=signature, doc=doc,
            start_line=node.start_point[0] + 1,
            start_col=byte_column(self.source, node.start_byte),
            end_line=node.end_point[0] + 1,
            end_col=byte_column(self.source, node.end_byte),
            body_hash=content_hash(self.source[node.start_byte:node.end_byte]),
            visibility=None))

    def _collect_body(self, body, address: str) -> None:
        """Colhe travessias do corpo atribuindo-as ao bloco (src = fqn do bloco).

        O nome de escopo é o endereço inteiro, então ``enclosing_fqn`` reconstrói
        ``<módulo>.<endereço>`` — o fqn do símbolo do bloco."""
        self.scope.append((address, "block"))
        self._walk_expr(body)
        self.scope.pop()

    def _locals(self, body) -> None:
        if body is None:
            return
        for attr in body.named_children:
            if attr.type != "attribute":
                continue
            name_node = next((c for c in attr.named_children
                              if c.type == "identifier"), None)
            if name_node is None:
                continue
            address = f"local.{self.text(name_node)}"
            self._define(attr, "local", address,
                         signature=None, doc=None)
            self._collect_body(attr, address)

    # -- valores / travessias ------------------------------------------------

    def _string_attr(self, body, name: str) -> str | None:
        for attr in body.named_children:
            if attr.type != "attribute":
                continue
            ident = next((c for c in attr.named_children
                          if c.type == "identifier"), None)
            if ident is None or self.text(ident) != name:
                continue
            expr = next((c for c in attr.named_children
                         if c.type == "expression"), None)
            return self._string_value(expr) if expr is not None else None
        return None

    def _string_value(self, expr) -> str | None:
        """Texto de um valor que é literal string simples (sem interpolação).

        String pura parseia como ``literal_value → string_lit``; uma com
        ``${…}`` vira ``template_expr`` — que ignoramos (não é literal)."""
        lv = next((c for c in expr.named_children
                   if c.type == "literal_value"), None)
        if lv is None:
            return None
        sl = next((c for c in lv.named_children
                   if c.type == "string_lit"), None)
        return self._label(sl) if sl is not None else None

    def _walk_expr(self, node) -> None:
        """Percorre um valor colhendo travessias de escopo como referências.

        Uma travessia é um ``variable_expr`` (head) seguido, no mesmo
        ``expression``, de ``get_attr`` irmãos. O resto (índices, funções,
        templates, coleções) é visitado recursivamente."""
        if node.type == "expression":
            kids = node.named_children
            if kids and kids[0].type == "variable_expr":
                attrs = []
                i = 1
                while i < len(kids) and kids[i].type == "get_attr":
                    attrs.append(kids[i])
                    i += 1
                self._emit_traversal(kids[0], attrs)
                for k in kids[i:]:           # índices/chamadas após a travessia
                    self._walk_expr(k)
                return
        for c in node.named_children:
            self._walk_expr(c)

    def _emit_traversal(self, head, attr_nodes) -> None:
        head_id = next((c for c in head.named_children
                        if c.type == "identifier"), None)
        if head_id is None:
            return
        name = self.text(head_id)
        if name in _TRAVERSAL_STOP:
            return
        attrs = [self._attr_name(a) for a in attr_nodes]
        attrs = [a for a in attrs if a]
        if name in ("var", "local", "module"):
            if attrs:
                self.add_ref(head, "references", f"{name}.{attrs[0]}")
        elif name == "data":
            if len(attrs) >= 2:
                self.add_ref(head, "references", f"data.{attrs[0]}.{attrs[1]}")
        elif attrs and name not in self._loopvars:
            # recurso: <tipo>.<nome>. Head sem atributo = identificador simples
            # (var de laço/builtin) — não é endereço de recurso.
            self.add_ref(head, "references", f"{name}.{attrs[0]}")

    def _attr_name(self, get_attr) -> str | None:
        ident = next((c for c in get_attr.named_children
                      if c.type == "identifier"), None)
        return self.text(ident) if ident is not None else None
