"""Resolver L1 para C# via csharp-ls (LSP sobre Roslyn). Config sobre LspResolver.

Ativa quando `csharp-ls` está no PATH (ou CODEGRAPH_CSHARP_LS). Instala via
`dotnet tool install --global csharp-ls` (requer .NET SDK). A resolução
cross-file depende do Roslyn achar o projeto (`.csproj`/`.sln`); sem ele
resolve o que consegue e o resto fica `possible`. NÃO validado nesta máquina
(sem .NET); protocolo idêntico ao dos demais servidores.
"""

from __future__ import annotations

from .lsp_base import LspResolver


class CSharpLsResolver(LspResolver):
    languages = ("csharp",)
    language_id = "csharp"
    cmd_name = "csharp-ls"
    cmd_env = "CODEGRAPH_CSHARP_LS"
    ready_timeout = 90.0        # Roslyn carrega a solução de forma assíncrona
