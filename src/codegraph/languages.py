"""Mapeamento extensão → linguagem tree-sitter e carregamento de parsers.

Três níveis de suporte (docs/DESIGN.md §6.2):
- DEDICATED: extractor específico (fqn/imports/calls refinados);
- genérico: qualquer gramática do language-pack via heurística estrutural;
- dados/docs: markdown (headings), json/yaml/toml (chaves).

Fora do grafo (deliberado): binários e formatos sem estrutura de símbolos —
.pdf/.docx (pipeline de docs é outra camada), .sln (texto proprietário),
.toc/.dfm/.lfm (metadata/serialização), .dm*/BYOND (sem gramática).
"""

from __future__ import annotations

from functools import lru_cache

EXT_TO_LANG: dict[str, str] = {
    # --- extractors dedicados ---
    ".py": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp",
    ".c": "c",
    ".h": "cpp",       # ambíguo; fallback C no indexer quando o parse falha
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cu": "cuda", ".cuh": "cuda",   # gramática CUDA; extractor C/C++
    ".metal": "cpp",                 # MSL é C++14-based
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".lua": "lua", ".luau": "luau",
    ".scala": "scala",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure",
    ".edn": "clojure",
    # --- nível genérico (heurística estrutural) ---
    ".zig": "zig",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".ex": "elixir", ".exs": "elixir",
    ".m": "objc", ".mm": "objc",
    ".jl": "julia",
    ".vue": "vue", ".svelte": "svelte", ".astro": "astro",
    ".groovy": "groovy", ".gradle": "groovy",
    ".dart": "dart",
    ".v": "verilog", ".sv": "systemverilog", ".svh": "systemverilog",
    ".sql": "sql",
    ".f": "fortran", ".f90": "fortran", ".f95": "fortran",
    ".f03": "fortran", ".f08": "fortran",
    ".pas": "pascal", ".pp": "pascal", ".dpr": "pascal", ".dpk": "pascal",
    ".lpr": "pascal", ".inc": "pascal",
    ".sh": "bash", ".bash": "bash",
    ".cls": "apex", ".trigger": "apex",
    ".razor": "razor", ".cshtml": "razor",
    ".csproj": "xml", ".fsproj": "xml", ".vbproj": "xml",
    ".xaml": "xml", ".slnx": "xml", ".lpk": "xml",
    # --- dados/docs ---
    ".md": "markdown",
    ".json": "json",
    ".yml": "yaml", ".yaml": "yaml",
    ".toml": "toml",
}

# linguagens com extractor dedicado (extract/__init__.py)
DEDICATED = {"python", "typescript", "tsx", "javascript", "rust", "go", "java",
             "kotlin", "csharp", "c", "cpp", "cuda", "php",
             "ruby", "lua", "luau", "swift", "scala", "clojure"}


def language_for(path: str) -> str | None:
    dot = path.rfind(".")
    if dot == -1:
        return None
    return EXT_TO_LANG.get(path[dot:].lower())


@lru_cache(maxsize=None)
def get_parser(lang: str):
    from tree_sitter_language_pack import get_parser as _get

    return _get(lang)
