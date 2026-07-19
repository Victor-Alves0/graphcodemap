"""Resolver L1 para Go via gopls (LSP). Config sobre LspResolver.

Requer `gopls` no PATH (ou CODEGRAPH_GOPLS) e o toolchain Go para carregar o
módulo. Validado no benchrepos/gin (0→4705 arestas promovidas a `certain`).
"""

from __future__ import annotations

from .lsp_base import LspResolver


class GoplsResolver(LspResolver):
    languages = ("go",)
    language_id = "go"
    cmd_name = "gopls"
    cmd_env = "CODEGRAPH_GOPLS"
