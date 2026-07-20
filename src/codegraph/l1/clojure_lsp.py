"""Resolver L1 para Clojure/ClojureScript via clojure-lsp. Config sobre
LspResolver.

Ativa quando `clojure-lsp` está no PATH (ou CODEGRAPH_CLOJURE_LSP). Binário
único (nativo ou uberjar), serve LSP por stdio. Analisa o projeto de forma
assíncrona (pode demorar em repos grandes) — ready_timeout maior. VALIDADO:
promove chamada cross-namespace (alias ns/) a `certain` (tests/test_l1_extra.py,
com clojure-lsp nativo real).
"""

from __future__ import annotations

from .lsp_base import LspResolver


class ClojureLspResolver(LspResolver):
    languages = ("clojure",)
    language_id = "clojure"
    cmd_name = "clojure-lsp"
    cmd_env = "CODEGRAPH_CLOJURE_LSP"
    ready_timeout = 60.0       # a análise inicial do clojure-lsp costuma ser lenta
