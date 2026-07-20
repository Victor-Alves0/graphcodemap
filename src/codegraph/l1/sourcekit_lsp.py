"""Resolver L1 para Swift via sourcekit-lsp (LSP). Config sobre LspResolver.

Ativa quando `sourcekit-lsp` está no PATH (ou CODEGRAPH_SOURCEKIT_LSP). Vem
junto com o toolchain do Swift (Xcode no macOS; Swift for Windows/Linux). A
resolução depende do servidor achar o pacote (`Package.swift`); sem ele resolve
o que consegue e o resto fica `possible`. NÃO validado nesta máquina (sem
toolchain Swift); protocolo idêntico ao dos demais servidores.
"""

from __future__ import annotations

from .lsp_base import LspResolver


class SourceKitLspResolver(LspResolver):
    languages = ("swift",)
    language_id = "swift"
    cmd_name = "sourcekit-lsp"
    cmd_env = "CODEGRAPH_SOURCEKIT_LSP"
    ready_timeout = 90.0
