"""Resolver L1 para Scala via Metals (LSP). Config sobre LspResolver.

Ativa quando um launcher `metals` está no PATH (ou CODEGRAPH_METALS). Metals é
JVM e normalmente gerado por coursier
(`cs bootstrap ... org.scalameta:metals ... -o metals`). A resolução depende
do import do build (Bloop/sbt); sem ele fica `possible`. NÃO validado nesta
máquina (sem launcher metals); protocolo idêntico ao dos demais servidores.
"""

from __future__ import annotations

from .lsp_base import LspResolver


class MetalsResolver(LspResolver):
    languages = ("scala",)
    language_id = "scala"
    cmd_name = "metals"
    cmd_env = "CODEGRAPH_METALS"
    ready_timeout = 120.0       # import de build (Bloop) é lento
    init_options = {"isHttpEnabled": False}
