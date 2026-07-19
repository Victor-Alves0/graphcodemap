"""Resolver L1 para C/C++ via clangd (LSP). Config sobre LspResolver.

Ativa quando `clangd` está no PATH (ou CODEGRAPH_CLANGD). A resolução
cross-file depende de um `compile_commands.json` no repo (ou flags no
`.clangd`); sem ele, clangd ainda resolve o que consegue por heurística e o
resto permanece `possible` (honesto). NÃO validado nesta máquina (binário
ausente); protocolo idêntico ao do gopls (validado).
"""

from __future__ import annotations

from .lsp_base import LspResolver


class ClangdResolver(LspResolver):
    languages = ("c", "cpp", "cuda")
    language_id = "cpp"        # clangd infere pela extensão; 'cpp' é seguro
    cmd_name = "clangd"
    cmd_env = "CODEGRAPH_CLANGD"
