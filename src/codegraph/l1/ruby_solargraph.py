"""Resolver L1 para Ruby via solargraph. Config sobre LspResolver.

Ativa quando `solargraph` está no PATH (ou CODEGRAPH_SOLARGRAPH); instala via
`gem install solargraph` e serve LSP por stdio com o subcomando `stdio`. NÃO
validado nesta máquina (binário ausente); protocolo idêntico ao do gopls
(validado).
"""

from __future__ import annotations

from .lsp_base import LspResolver


class SolargraphResolver(LspResolver):
    languages = ("ruby",)
    language_id = "ruby"
    cmd_name = "solargraph"
    cmd_env = "CODEGRAPH_SOLARGRAPH"
    cmd_args = ("stdio",)
