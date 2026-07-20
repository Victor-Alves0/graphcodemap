"""Resolver L1 para Rust via rust-analyzer (LSP). Config sobre LspResolver.

Ativa quando `rust-analyzer` está no PATH (ou CODEGRAPH_RUST_ANALYZER). A
qualidade da resolução depende do rust-analyzer carregar o crate (Cargo.toml +
toolchain para o sysroot). VALIDADO: promove chamada cross-file a `certain`
(tests/test_l1_rust.py, com rust-analyzer real).
"""

from __future__ import annotations

from .lsp_base import LspResolver


class RustAnalyzerResolver(LspResolver):
    languages = ("rust",)
    language_id = "rust"
    cmd_name = "rust-analyzer"
    cmd_env = "CODEGRAPH_RUST_ANALYZER"
