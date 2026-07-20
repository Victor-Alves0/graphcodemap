"""Resolver L1 para PHP via intelephense. Config sobre LspResolver.

Ativa quando `intelephense` está no PATH (ou CODEGRAPH_INTELEPHENSE); instala
via `npm i -g intelephense` e serve LSP por stdio com `--stdio`. As features
premium exigem licença, mas goto-definition (o que usamos) funciona no modo
livre. Indexa o workspace de forma assíncrona — o _warmup espera. NÃO validado
nesta máquina (binário ausente); protocolo idêntico ao do gopls (validado).
"""

from __future__ import annotations

from .lsp_base import LspResolver


class IntelephenseResolver(LspResolver):
    languages = ("php",)
    language_id = "php"
    cmd_name = "intelephense"
    cmd_env = "CODEGRAPH_INTELEPHENSE"
    cmd_args = ("--stdio",)
