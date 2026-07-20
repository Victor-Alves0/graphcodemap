"""Resolver L1 para Lua/Luau via lua-language-server (LuaLS). Config sobre
LspResolver.

Ativa quando `lua-language-server` está no PATH (ou CODEGRAPH_LUA_LS). Binário
único, serve LSP por stdio. Faz preload assíncrono do workspace — o _warmup do
lsp_base espera a prontidão antes de consultar. VALIDADO: promove chamada
cross-file (require) a `certain` (tests/test_l1_extra.py, com LuaLS real).
"""

from __future__ import annotations

from .lsp_base import LspResolver


class LuaLsResolver(LspResolver):
    languages = ("lua", "luau")
    language_id = "lua"
    cmd_name = "lua-language-server"
    cmd_env = "CODEGRAPH_LUA_LS"
