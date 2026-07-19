"""Toolsets dos braços da avaliação.

Baseline = list_files/grep/read_file (o que Claude Code/Cursor usam nativamente).
CodeGraph = baseline + tools do grafo — mede o valor MARGINAL do grafo,
conforme docs/DESIGN.md §0.4 (complementar, não substituir).
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import render
from ..indexer import load_ignore_spec
from ..query import AmbiguousSymbol, QueryEngine, SymbolNotFound

_TEXT_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".go",
              ".java", ".kt", ".cs", ".c", ".h", ".cpp", ".cc", ".hpp", ".php",
              ".rb", ".lua", ".swift", ".scala", ".clj", ".cljs", ".cljc", ".edn",
              ".md", ".toml", ".json", ".yaml", ".yml", ".txt", ".sql", ".html"}


def _schema(name: str, description: str, params: dict, required: list) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": params,
                       "required": required}}}


class BaselineTools:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.spec = load_ignore_spec(self.root)

    def _files(self):
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root).as_posix()
            if self.spec.match_file(rel) or p.suffix.lower() not in _TEXT_EXTS:
                continue
            yield rel

    # -- tools ----------------------------------------------------------------

    def list_files(self, pattern: str | None = None, limit: int = 200) -> str:
        rels = [r for r in self._files()
                if pattern is None or re.search(pattern, r)]
        out = rels[:limit]
        suffix = f"\n… ({len(rels) - limit} omitidos)" if len(rels) > limit else ""
        return "\n".join(out) + suffix if out else "nenhum arquivo"

    def grep(self, pattern: str, path_pattern: str | None = None,
             max_results: int = 50) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"regex inválida: {e}"
        hits: list[str] = []
        for rel in self._files():
            if path_pattern and not re.search(path_pattern, rel):
                continue
            try:
                text = (self.root / rel).read_text(encoding="utf-8",
                                                   errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        return "\n".join(hits) + "\n… (limite atingido)"
        return "\n".join(hits) if hits else "nenhum resultado"

    def read_file(self, path: str, start_line: int = 1,
                  end_line: int | None = None) -> str:
        rel = path.replace("\\", "/").strip("/")
        target = self.root / rel
        if not target.is_file():
            return f"arquivo não encontrado: {rel}"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        end = min(end_line or start_line + 199, start_line + 399, len(lines))
        chunk = lines[start_line - 1:end]
        numbered = [f"{i}\t{ln}" for i, ln in enumerate(chunk, start_line)]
        note = f"\n… (arquivo tem {len(lines)} linhas)" if end < len(lines) else ""
        return "\n".join(numbered) + note

    # -- interface ------------------------------------------------------------

    def schemas(self) -> list:
        return [
            _schema("list_files", "Lista arquivos (regex opcional).",
                    {"pattern": {"type": "string"}}, []),
            _schema("grep", "Busca regex nos arquivos.",
                    {"pattern": {"type": "string"},
                     "path_pattern": {"type": "string"}}, ["pattern"]),
            _schema("read_file", "Lê arquivo com números de linha.",
                    {"path": {"type": "string"},
                     "start_line": {"type": "integer"},
                     "end_line": {"type": "integer"}}, ["path"]),
        ]

    def call(self, name: str, args: dict) -> str:
        fn = getattr(self, name, None)
        if fn is None:
            return f"tool desconhecida: {name}"
        return fn(**args)


class CodeGraphTools(BaselineTools):
    def __init__(self, root: str | Path, engine: QueryEngine) -> None:
        super().__init__(root)
        self.engine = engine

    def _guard(self, fn) -> str:
        try:
            return fn()
        except (AmbiguousSymbol, SymbolNotFound) as e:
            return f"erro: {e}"

    def overview(self, scope: str | None = None, token_budget: int = 1500) -> str:
        return render.overview(*self.engine.overview(scope=scope,
                                                     token_budget=token_budget))

    def find_symbol(self, query: str, kind: str | None = None,
                    limit: int = 10) -> str:
        return render.find(query, *self.engine.find_symbol(query, kind=kind,
                                                           limit=limit))

    def symbol_info(self, symbol: str) -> str:
        return self._guard(lambda: render.info(*self.engine.symbol_info(symbol)))

    def references(self, symbol: str, kind: str | None = None) -> str:
        return self._guard(
            lambda: render.refs(*self.engine.references(symbol, kind=kind)))

    def callers(self, symbol: str, depth: int = 1) -> str:
        return self._guard(lambda: render.calls(
            *self.engine.callers(symbol, depth=depth), "callers de", "in"))

    def callees(self, symbol: str, depth: int = 1) -> str:
        return self._guard(lambda: render.calls(
            *self.engine.callees(symbol, depth=depth), "callees de", "out"))

    def impact(self, symbol: str, depth: int = 3) -> str:
        return self._guard(
            lambda: render.impact(*self.engine.impact(symbol, depth=depth)))

    def ego_graph(self, symbol: str) -> str:
        return self._guard(
            lambda: render.ego(*self.engine.ego_graph(symbol)))

    def dataflow(self, symbol: str, depth: int = 2) -> str:
        return self._guard(
            lambda: render.dataflow(*self.engine.data_flow(symbol, depth=depth)))

    def taint(self, entry: str | None = None, scope: str | None = None) -> str:
        return self._guard(
            lambda: render.taint(*self.engine.taint(entry=entry, scope=scope)))

    def reaches(self, symbol: str, sink: str = "http",
                via: str | None = None) -> str:
        return self._guard(
            lambda: render.reaches(*self.engine.reaches(symbol, sink=sink, via=via)))

    def schemas(self) -> list:
        sym = {"symbol": {"type": "string"}}
        return super().schemas() + [
            _schema("overview", "Mapa ranqueado do repo.",
                    {"scope": {"type": "string"}}, []),
            _schema("find_symbol", "Localiza símbolos por nome/fqn.",
                    {"query": {"type": "string"}, "kind": {"type": "string"}},
                    ["query"]),
            _schema("symbol_info", "Ficha do símbolo (assinatura/span/contagens).",
                    sym, ["symbol"]),
            _schema("references", "Usos do símbolo.",
                    {**sym, "kind": {"type": "string"}}, ["symbol"]),
            _schema("callers", "Quem chama o símbolo.",
                    {**sym, "depth": {"type": "integer"}}, ["symbol"]),
            _schema("callees", "O que o símbolo chama.",
                    {**sym, "depth": {"type": "integer"}}, ["symbol"]),
            _schema("impact", "Dependentes transitivos (o que quebra ao mudar).",
                    {**sym, "depth": {"type": "integer"}}, ["symbol"]),
            _schema("ego_graph", "Vizinhança do símbolo no grafo.", sym, ["symbol"]),
            _schema("dataflow", "Para onde os parâmetros de uma função fluem "
                    "(calls/returns que alcançam; interprocedural).",
                    {**sym, "depth": {"type": "integer"}}, ["symbol"]),
            _schema("taint", "Rastreio input-não-confiável→sink perigoso. Passe "
                    "entry=fqn p/ assumir os params da função como não-confiáveis.",
                    {"entry": {"type": "string"}, "scope": {"type": "string"}}, []),
            _schema("reaches", "Reachability endpoint→sink numa resposta só: "
                    "segue o call graph de `symbol` e devolve os caminhos que "
                    "chegam a um sink (sink='http'|'sql'|'exec'|'file' ou regex), "
                    "com a cadeia de funções e se o validador `via` (ex.: "
                    "'sanitize') aparece no caminho. Evita montar a travessia à "
                    "mão. Se a resposta vier [certain], a cadeia é um fato "
                    "resolvido (L1) — confie e pare, não releia o código.",
                    {**sym, "sink": {"type": "string"},
                     "via": {"type": "string"}}, ["symbol"]),
        ]
