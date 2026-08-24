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

from threading import local

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
    ".tf": "terraform", ".tfvars": "terraform", ".hcl": "terraform",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss",
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

# Marcação/estilo: têm extractor dedicado (estrutura navegável: seletores,
# âncoras, dependências entre arquivos), mas NÃO têm fluxo de dados — dataflow/
# taint não se aplica a HTML/CSS. Separado para a paridade "toda linguagem de
# programação dedicada tem dataflow" continuar significando o que diz.
MARKUP = {"html", "css", "scss"}

# Config declarativa dedicada (Terraform/HCL): extractor específico com grafo de
# dependência real (recursos/vars/módulos e as referências entre eles), mas SEM
# fluxo de dados — não há função/parâmetro/taint a rastrear. Fora da paridade de
# dataflow pela mesma razão do MARKUP.
CONFIG = {"terraform"}

# linguagens com extractor dedicado (extract/__init__.py) que NÃO têm dataflow.
NO_DATAFLOW = MARKUP | CONFIG

# linguagens com extractor dedicado (extract/__init__.py)
DEDICATED = {"python", "typescript", "tsx", "javascript", "rust", "go", "java",
             "kotlin", "csharp", "c", "cpp", "cuda", "php",
             "ruby", "lua", "luau", "swift", "scala", "clojure"} | NO_DATAFLOW


def language_for(path: str) -> str | None:
    dot = path.rfind(".")
    if dot == -1:
        return None
    return EXT_TO_LANG.get(path[dot:].lower())


_parser_state = local()


def get_parser(lang: str):
    """Retorna um parser reutilizável, mas exclusivo da thread atual.

    ``tree_sitter.Parser`` mantém estado nativo durante ``parse``. Compartilhar
    a mesma instância entre os workers do indexador pode causar access violation
    no processo (não uma exceção Python). O cache thread-local preserva o custo
    baixo das chamadas repetidas sem permitir uso concorrente do mesmo parser.
    """
    from tree_sitter_language_pack import get_parser as _get

    parsers = getattr(_parser_state, "parsers", None)
    if parsers is None:
        parsers = _parser_state.parsers = {}
    parser = parsers.get(lang)
    if parser is None:
        parser = parsers[lang] = _get(lang)
    return parser
