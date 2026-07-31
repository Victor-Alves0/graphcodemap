"""Explicação legível de COMO uma aresta foi resolvida.

O grafo distingue resolução heurística (L0, por nome/tipo) de semântica (L1, um
language server respondendo `textDocument/definition`). Saber qual rodou — e por
quê — é o que deixa o agente confiar numa aresta sem reler o código, e é o que
torna a resolução depurável. Tudo aqui é DERIVADO de (resolver, confidence) + a
linguagem do site; nada é armazenado por aresta (o índice tem milhões delas)."""

from __future__ import annotations

# linguagem L0 → nome do motor/servidor L1, só para o texto do `reason`.
_L1_ENGINE = {
    "python": "jedi",
    "typescript": "TypeScript language service",
    "tsx": "TypeScript language service",
    "javascript": "TypeScript language service",
    "go": "gopls",
    "rust": "rust-analyzer",
    "c": "clangd", "cpp": "clangd", "cuda": "clangd",
    "java": "jdtls",
    "kotlin": "kotlin-language-server",
    "csharp": "csharp-ls",
    "php": "intelephense",
    "ruby": "solargraph",
    "lua": "lua-language-server", "luau": "lua-language-server",
    "scala": "metals",
    "swift": "sourcekit-lsp",
    "clojure": "clojure-lsp",
}


def resolver_label(resolver: str | None, language: str | None = None) -> str:
    """Rótulo curto do resolver: ``l1/typescript``, ``l0`` ou ``none``.

    ``resolver`` é o valor guardado na aresta (``l0``/``l1``); ``language`` é a
    da linguagem do site, usada só para qualificar o L1 (qual servidor)."""
    if resolver == "l1":
        return f"l1/{language}" if language else "l1"
    if not resolver:
        return "none"
    return resolver


def reason(label: str, confidence: str | None, *, resolved: bool = True) -> str:
    """Frase única explicando um par (rótulo, confiança) — para legenda/debug."""
    if not resolved or confidence is None:
        return ("não resolvido no repo: dependência externa, chamada dinâmica "
                "ou definição ausente (L0 mantém a incerteza honesta)")
    if label.startswith("l1/"):
        engine = _L1_ENGINE.get(label.split("/", 1)[1], "language server")
        return f"{engine} resolveu exatamente 1 definição (LSP textDocument/definition)"
    if confidence == "certain":
        return "resolução semântica encontrou 1 definição"
    if confidence == "inferred":
        return "L0: alvo único por nome+tipo no índice (ou no mesmo arquivo)"
    if confidence == "possible":
        return "L0: casado por nome, homônimos ambíguos — verificar lendo o código"
    return ""


def annotate(row: dict) -> dict:
    """Adiciona ``resolver`` (rótulo) ao dict de uma aresta, consumindo a coluna
    auxiliar ``site_language``. Idempotente e tolerante a colunas ausentes."""
    label = resolver_label(row.get("resolver"), row.pop("site_language", None))
    row["resolver"] = label
    return row
