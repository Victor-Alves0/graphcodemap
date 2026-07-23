"""Extractors dedicados para a camada web: HTML e CSS/SCSS.

Modelagem (o ponto que o tier genérico erra): **o CSS define, o HTML usa.**
- CSS/SCSS: cada seletor de classe/id é uma DEFINIÇÃO → vira símbolo
  (`css_class` / `css_id`), assim como `@mixin`/`@function` do SCSS.
- HTML: `id="x"` é a âncora daquele elemento → símbolo (`html_id`). Já
  `class="a b"` é USO, não definição → vira referência (o alvo é o seletor
  definido no CSS). E `<script src>` / `<link href>` / `<img src>` viram
  `imports`, registrando a dependência entre arquivos.

O consumidor de uma classe raramente é HTML: num app React/Vue ele é o
`className=` do TSX. Por isso o uso é emitido também pelo extractor TS/JS, e o
resolver religa `references` → `css_class`/`html_id` SEM filtro de língua
(indexer.STYLE_DEF_KINDS). É a única aresta cross-language do L0.

Limite declarado (docs/DESIGN.md §3.1 — nunca esconder o que não se sabe): os
refs de ASSET (`<script src>`, `@import`) seguem *dangling* (`dst=NULL`) com o
`dst_name` preservado — o grafo não tem símbolo de ARQUIVO para ser o alvo. A
dependência fica registrada e legível, mas não navegável como aresta resolvida;
resolvê-la exige símbolo de módulo/arquivo, decisão de design separada.

`<script>` inline não é parseado como JS aqui (o span do elemento é indexado;
a análise de JS embutido seria outro extractor).
"""

from __future__ import annotations

from .base import BaseExtractor

# tags cujo src/href é dependência real de outro arquivo do projeto
_ASSET_TAGS = {"script": "src", "link": "href", "img": "src", "iframe": "src",
               "source": "src", "audio": "src", "video": "src", "embed": "src",
               "track": "src", "object": "data", "use": "href"}
# alvos que não são arquivo do repo
_EXTERNAL = ("http://", "https://", "//", "data:", "mailto:", "tel:",
             "javascript:", "#", "{{", "{%", "<%")


def _is_local_asset(value: str) -> bool:
    v = value.strip()
    return bool(v) and not v.startswith(_EXTERNAL)


def _clean_path(value: str) -> str:
    """Remove ./ inicial e query/fragmento (`app.js?v=2#x` → `app.js`)."""
    v = value.strip()
    for sep in ("?", "#"):
        cut = v.find(sep)
        if cut > 0:
            v = v[:cut]
    while v.startswith("./"):
        v = v[2:]
    return v


class HtmlExtractor(BaseExtractor):
    def visit(self, node) -> None:
        if node.type in ("start_tag", "self_closing_tag"):
            self._tag(node)
        for c in node.named_children:
            self.visit(c)

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _attr_name(attr) -> str | None:
        n = next((c for c in attr.named_children if c.type == "attribute_name"),
                 None)
        return n

    def _attr_value(self, attr) -> str | None:
        """Valor do atributo, com ou sem aspas."""
        for c in attr.named_children:
            if c.type == "attribute_value":
                return self.text(c)
            if c.type == "quoted_attribute_value":
                inner = next((g for g in c.named_children
                              if g.type == "attribute_value"), None)
                return self.text(inner) if inner is not None else ""
        return None

    def _tag(self, tag) -> None:
        name_node = next((c for c in tag.named_children if c.type == "tag_name"),
                         None)
        tag_name = self.text(name_node).lower() if name_node is not None else ""
        asset_attr = _ASSET_TAGS.get(tag_name)
        for attr in tag.named_children:
            if attr.type != "attribute":
                continue
            key_node = self._attr_name(attr)
            if key_node is None:
                continue
            key = self.text(key_node).lower()
            value = self._attr_value(attr)
            if not value:
                continue
            if key == "id":
                # âncora do elemento: definição navegável
                self.add_sym(attr, "html_id", value.strip(),
                             signature=f"<{tag_name} id={value.strip()}>")
            elif key == "class":
                # USO de seletores definidos no CSS → referência
                for token in value.split():
                    self.add_ref(attr, "references", token)
            elif key == asset_attr and _is_local_asset(value):
                self.add_ref(attr, "imports", _clean_path(value))


class CssExtractor(BaseExtractor):
    """CSS e SCSS. Seletores de classe/id viram símbolos (as definições);
    `@import`/`@use` viram imports; SCSS acrescenta `@mixin`/`@function`."""

    def __init__(self, source: bytes, module_fqn: str) -> None:
        super().__init__(source, module_fqn)
        self._seen: set[tuple[str, str]] = set()

    def visit(self, node) -> None:
        t = node.type
        if t == "class_selector":
            self._selector(node, "class_name", "css_class")
        elif t == "id_selector":
            self._selector(node, "id_name", "css_id")
        elif t in ("mixin_statement", "function_statement"):
            self._named_at_rule(node)
        elif t in ("import_statement", "use_statement", "forward_statement"):
            self._at_import(node)
        for c in node.named_children:
            self.visit(c)

    def _selector(self, node, name_type: str, kind: str) -> None:
        # filho DIRETO: evita capturar o nome da pseudo-classe (`.btn:hover`
        # tem um class_name 'hover' irmão, que não é um seletor definido aqui)
        n = next((c for c in node.named_children if c.type == name_type), None)
        if n is None:
            return                      # ex.: `&__inner` (nesting SCSS), sem nome
        # o seletor escapa o que não é identificador (`.mt-1\.5`,
        # `.hover\:bg-blue` — o padrão do Tailwind), mas o atributo no HTML/JSX
        # traz o nome CRU: sem desescapar, as duas pontas nunca se encontram
        name = self.text(n).strip().replace("\\", "")
        if not name or (kind, name) in self._seen:
            return                      # mesma classe redefinida (ex.: @media)
        self._seen.add((kind, name))
        self.add_sym(node, kind, name, signature=self.text(node).strip())
        if kind == "css_id":
            # `#app` é seletor (definição de regra) E uso do elemento que o
            # HTML declara com `id="app"` — sem isto o `css_id` fica ilhado
            self.add_ref(node, "references", name)

    def _named_at_rule(self, node) -> None:
        n = node.child_by_field_name("name")
        if n is None:
            n = next((c for c in node.named_children if c.type == "identifier"),
                     None)
        if n is None:
            return
        name = self.text(n).strip()
        if not name or ("function", name) in self._seen:
            return
        self._seen.add(("function", name))
        kind = "mixin" if node.type == "mixin_statement" else "function"
        params = next((c for c in node.named_children if c.type == "parameters"),
                      None)
        sig = f"@{kind} {name}{self.text(params) if params is not None else '()'}"
        self.add_sym(node, kind, name, signature=sig)

    def _at_import(self, node) -> None:
        for c in node.named_children:
            if c.type == "string_value":
                raw = self.text(c).strip().strip('"').strip("'")
                if raw and _is_local_asset(raw):
                    self.add_ref(node, "imports", _clean_path(raw))
                return
