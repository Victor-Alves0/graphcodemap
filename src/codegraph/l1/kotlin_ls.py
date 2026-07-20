"""Resolver L1 para Kotlin via kotlin-language-server. Config sobre LspResolver.

Ativa quando `kotlin-language-server` está no PATH (ou CODEGRAPH_KOTLIN_LS). É
um launcher que sobe uma JVM (requer Java) e serve LSP por stdio. Resolve o
projeto de forma assíncrona (Gradle/Maven) — o _warmup espera. NÃO validado
nesta máquina (binário ausente); protocolo idêntico ao do gopls (validado).
"""

from __future__ import annotations

from .lsp_base import LspResolver


class KotlinLsResolver(LspResolver):
    languages = ("kotlin",)
    language_id = "kotlin"
    cmd_name = "kotlin-language-server"
    cmd_env = "CODEGRAPH_KOTLIN_LS"
    ready_timeout = 60.0       # resolução do projeto (Gradle) pode ser lenta
