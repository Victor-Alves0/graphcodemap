"""Query engine com read-repair e envelope de frescor/completeness.

Garantia (docs/DESIGN.md §2.3): nenhuma resposta sai sem verificar o
content-hash dos arquivos envolvidos. Divergiu → re-indexa (ms) e re-executa
a consulta; arquivo sumiu → sai do índice; tudo anotado no envelope.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field

from . import explain
from .community import ensure_communities
from .indexer import (Indexer, get_index_excludes, get_index_scopes,
                      scan_source_stats, _repo_rel)
from .languages import get_parser
from .rank import ensure_ranks
from .util import like_escape

CALL_KINDS = ("calls",)
# Kinds que não são código declarado: numerosos e pouco informativos numa busca
# por nome (uma folha de estilo sozinha rende centenas de `css_class`). Não são
# excluídos — só não podem tomar mais que metade dos resultados.
LOW_INFO_KINDS = ("css_class", "css_id", "html_id", "file")
# o FTS não conhece rank; buscar com folga evita que o corte dele decida o
# resultado antes da ordenação
FTS_OVERFETCH = 4
IMPACT_KINDS = ("calls", "imports", "inherits", "references")
_CONF_ORD = {"certain": 2, "inferred": 1, "possible": 0}
_MISS = object()          # sentinela p/ cache LRU (distingue "None cacheado" de ausente)

# defaults de profundidade por MODO de taint (embutidos na lib para que todo
# consumidor herde o comportamento seguro): varredura do repo é O(fontes ×
# ramif^depth) → raso; entry=<func> parte de uma raiz só → pode ir mais fundo.
TAINT_DEPTH_SCAN = 3
TAINT_DEPTH_ENTRY = 6

_SUGGEST_STOPWORDS = frozenset({
    # Português: intenção/conversa, não conceitos do repositório.
    "ajuda", "ajudar", "altera", "alterar", "analise", "analisar",
    "arquivo", "arquivos", "codigo", "com", "como", "consertar",
    "corrigir", "das", "desse", "desta", "deste", "dos", "essa", "esse",
    "favor", "funcao", "funcoes", "gostaria", "implementar", "mexer", "meu",
    "minha", "modulo", "modulos", "mudar", "nesta", "neste", "onde", "para",
    "parte", "pode", "poderia", "por", "preciso", "projeto", "qual", "quais", "que",
    "quero", "remover", "sem", "sobre", "uma", "voce",
    # Equivalentes frequentes em prompts mistos.
    "add", "analyze", "and", "change", "code", "could", "file", "files", "fix",
    "function", "functions", "help", "implement", "module", "need", "please",
    "project", "remove", "should", "the", "want", "with", "would",
})


def _suggest_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii").lower().strip("_")


class _Budget:
    """Teto cooperativo para análises de fecho transitivo (taint/reaches).

    Três limites, todos opcionais e checados no mesmo ponto quente:
    - `deadline_ms`: teto de tempo (varia por máquina — bom p/ SLA do host).
    - `max_steps`: teto DETERMINÍSTICO de passos (reprodutível entre máquinas).
    - `should_cancel`: callback do host (ex.: usuário abortou) — evita a
      thread-zumbi, já que Python não mata thread.

    Quando um limite estoura, `limit_hit` guarda QUAL (`deadline`/`steps`/
    `cancelled`); o chamador ainda pode registrar `findings`/`paths` (o corte
    do resultado) via `note()`. `hit()` é o predicado de parada — checado a
    cada passo; uma vez disparado, permanece disparado (parada limpa)."""

    __slots__ = ("t0", "deadline_s", "max_steps", "should_cancel",
                 "steps", "explored", "limit_hit")

    def __init__(self, deadline_ms=None, max_steps=None, should_cancel=None):
        self.t0 = time.monotonic()
        self.deadline_s = (deadline_ms / 1000.0) if deadline_ms else None
        self.max_steps = max_steps
        self.should_cancel = should_cancel
        self.steps = 0
        self.explored = 0
        self.limit_hit = None

    def hit(self) -> str | None:
        """True (o nome do limite) se deve parar AGORA. Sticky."""
        if self.limit_hit:
            return self.limit_hit
        if self.should_cancel is not None and self.should_cancel():
            self.limit_hit = "cancelled"
        elif self.deadline_s is not None and (time.monotonic() - self.t0) >= self.deadline_s:
            self.limit_hit = "deadline"
        elif self.max_steps is not None and self.steps >= self.max_steps:
            self.limit_hit = "steps"
        return self.limit_hit

    def tick(self) -> None:
        self.steps += 1

    def note(self, why: str) -> None:
        """Registra um corte do RESULTADO (findings/paths) sem sobrescrever um
        limite de recurso já disparado (recurso é mais informativo)."""
        if self.limit_hit is None:
            self.limit_hit = why

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

# presets de sink para reaches(): input não-confiável alcançando operação sensível.
# Casados contra o NOME do alvo da chamada (dst_name), case-insensitive.
# padrões de arquivo/símbolo de TESTE (find_related_tests). Diretórios comuns +
# convenções de nome por linguagem (test_*, *_test, *Test, *Spec, *.test., …).
_TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__", "testing"}
_TEST_STEM = re.compile(r"(^test$|^test[_A-Z]|_test$|_spec$|Test$|Tests$|Spec$|Specs$|IT$)")


def _is_test_path(p: str) -> bool:
    p = p.replace("\\", "/")
    segs = p.split("/")
    if any(s.lower() in _TEST_DIRS for s in segs[:-1]):
        return True
    base = segs[-1]
    low = base.lower()
    if ".test." in low or ".spec." in low:
        return True
    stem = base.rsplit(".", 1)[0]
    return bool(_TEST_STEM.search(stem))


def _paths_from_target(target: str) -> list[str]:
    """Extrai caminhos repo-relativos de um diff unificado OU de uma lista
    (separada por vírgula/espaço/linha). Diff: pega os `+++ b/…` e `diff --git`;
    ignora /dev/null (arquivo deletado)."""
    t = target.strip()
    paths: set[str] = set()
    if "diff --git" in t or "+++ " in t or "--- " in t:
        for m in re.finditer(r"^\+\+\+ [ab]/(.+?)(?:\t.*)?$", t, re.M):
            paths.add(m.group(1).strip())
        for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)", t, re.M):
            paths.add(m.group(2))
        paths.discard("/dev/null")
    else:
        for part in re.split(r"[,\s]+", t):
            if part and part not in ("/dev/null",):
                paths.add(part.replace("\\", "/").lstrip("./"))
    safe: set[str] = set()
    for path in paths:
        norm = path.replace("\\", "/")
        # Primeira barreira antes de qualquer acesso ao filesystem. A defesa em
        # profundidade de _repair/Indexer ainda confere symlinks e junctions.
        if (not norm or norm.startswith("/") or re.match(r"^[A-Za-z]:", norm)
                or any(part == ".." for part in norm.split("/"))):
            continue
        parts = [part for part in norm.split("/") if part not in ("", ".")]
        if parts:
            safe.add("/".join(parts))
    return sorted(safe)


SINK_PRESETS = {
    "http": r"(clj-http|okhttp|httpclient|webclient|urlopen|requests?[._]|"
            r"\bfetch\b|axios|http[-_./](get|post|put|delete|patch|request)|"
            r"\bhttp/(get|post|put|delete|patch|request)\b)",
    "sql": r"(execute[-_]?(query|update|batch)?|raw[-_]?query|jdbc|"
           r"createstatement|\brawsql\b|honeysql|\bquery!\b)",
    "exec": r"(subprocess|popen|\bexec\b|system\(|runtime\.|processbuilder|"
            r"sh\(|shell[-_]?out|\beval\b)",
    "file": r"(\bopen\(|readfile|writefile|\bslurp\b|\bspit\b|createreadstream|"
            r"file\(|fileinputstream)",
}


@dataclass
class Envelope:
    """Sinais estruturados de uma resposta, ao lado dos avisos em texto.

    Os mesmos fatos que os avisos ⚠ carregam, mas em campos estáveis de máquina
    (para a camada agent/MCP): frescor, truncamento e completude (análise
    estática, arestas não resolvidas, dispatch dinâmico possível)."""

    warnings: list[str] = field(default_factory=list)
    fresh: bool = True                 # índice batia com o disco (sem drift)
    truncated: bool = False            # resultado cortado num limite
    dynamic_dispatch: bool = False     # chamadas dinâmicas podem faltar
    unresolved_edges: int = 0          # arestas relevantes ainda não resolvidas
    static_analysis: bool = True       # a resposta é de análise estática

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)


class AmbiguousSymbol(Exception):
    def __init__(self, selector: str, candidates: list) -> None:
        self.selector = selector
        self.candidates = candidates
        opts = ", ".join(c["fqn"] for c in candidates[:8])
        super().__init__(f"'{selector}' é ambíguo — candidatos: {opts}")


class SymbolNotFound(Exception):
    pass


class QueryEngine:
    def __init__(self, indexer: Indexer) -> None:
        self.ix = indexer
        self.conn = indexer.conn
        self.root = indexer.root
        self.l3_provider = None  # injetável (testes/outros providers)
        # frescor watcher-aware: quando um watcher vivo mantém o índice quente, a
        # varredura O(N) da query é redundante e é pulada — com um backstop
        # periódico p/ cobrir eventos que o watchdog possa ter perdido. Sem
        # watcher, a varredura roda a cada miss (garantia forte inalterada).
        self._watcher = None
        self._last_full_sweep = 0.0
        self._sweep_backstop = 30.0
        # cache LRU de fatos de fluxo (dataflow/taint) ENTRE chamadas: num loop
        # de agente (várias análises no mesmo repo) evita re-extrair os mesmos
        # fatos. Chaveado por (file_id, content_hash, start_line) → invalida
        # sozinho no re-index (content_hash muda; o cap despeja o morto).
        from collections import OrderedDict
        self._facts_cache: "OrderedDict" = OrderedDict()
        self._facts_cache_cap = 4096

    def attach_watcher(self, watcher) -> None:
        """Liga um Watcher vivo a este engine (o MCP server faz isso). Só precisa
        expor `is_current()`. Sem isto, o comportamento é o de sempre."""
        self._watcher = watcher

    # -- read-repair ----------------------------------------------------------

    def _repair(self, rels: set[str], env: Envelope) -> bool:
        """Confere frescor dos arquivos; re-indexa/remoção conforme o disco. True se algo mudou."""
        changed = False
        safe_rels: set[str] = set()
        for rel in rels:
            try:
                safe_rels.add(_repo_rel(self.root, rel))
            except ValueError:
                env.warn(f"freshness: caminho fora da raiz ignorado: {rel}")
        for rel in sorted(safe_rels):
            path = self.root / rel
            if not path.is_file():
                self.ix.remove_file(rel)
                env.warn(f"freshness: {rel} sumiu do disco; removido do índice agora.")
                changed = True
                continue
            row = self.conn.execute(
                "SELECT size, mtime, content_hash FROM files WHERE path=?", (rel,)
            ).fetchone()
            if row is None:
                # arquivo NOVO no disco (apareceu após a indexação): a resposta
                # pode estar nele. index_file ignora extensão não-fonte, então é
                # seguro tentar — a mesma garantia anti-staleness do 'sumiu'.
                if self.ix.index_file(rel):
                    env.warn(f"freshness: {rel} é novo no disco; indexado agora (L0).")
                    changed = True
                continue
            st = path.stat()
            if st.st_size == row["size"] and st.st_mtime_ns == row["mtime"]:
                continue  # fast-path: stat igual → assume fresco
            if self.ix.index_file(rel):
                env.warn(f"freshness: {rel} mudou desde a indexação; re-indexado agora (L0).")
                changed = True
        if changed:
            self.ix.resolve_edges()
            env.fresh = False           # havia drift (corrigido agora)
        return changed

    def _repair_all(self, env: Envelope, *, backstop_only: bool = False) -> bool:
        """Varredura de frescor sobre todos os arquivos indexados.

        Usada quando uma busca vem vazia: o índice pode estar velho justamente
        no arquivo que conteria a resposta — resultado vazio também é resposta
        e precisa da mesma garantia de frescor.

        Escala: `scan_source_stats` lê size/mtime via os.scandir (sem syscall por
        arquivo), ~60x mais rápido que stat individual. Só os arquivos com stat
        divergente (ou sumidos) entram no _repair.

        Watcher-aware: se um watcher vivo está drenado (`is_current()`), o índice
        já reflete tudo que ele observou → a varredura é pulada, com um backstop
        periódico (a cada `_sweep_backstop`s) p/ cobrir eventos perdidos pelo
        watchdog. Sem watcher (ou durante o debounce dele), a varredura roda a
        cada miss — a garantia forte anti-staleness é preservada nesse caminho.
        """
        w = self._watcher
        since_sweep = time.monotonic() - self._last_full_sweep
        # O throttle só é correto quando há outra fonte de verdade mantendo o
        # índice quente. Sem watcher, mesmo uma consulta já não-vazia precisa
        # varrer stats para descobrir arquivos/callers novos imediatamente.
        watcher_current = w is not None and w.is_current()
        if watcher_current and since_sweep < self._sweep_backstop:
            return False  # watcher garante frescor; pula a varredura O(N)
        self._last_full_sweep = time.monotonic()
        # com índice parcial, varre só as subárvores indexadas (barato em
        # monorepo grande); sem escopo, o repo inteiro.
        on_disk = scan_source_stats(
            self.root, scopes=get_index_scopes(self.conn) or None,
            excludes=get_index_excludes(self.conn) or None)
        stale: set[str] = set()
        indexed: set[str] = set()
        for r in self.conn.execute("SELECT path, size, mtime FROM files"):
            indexed.add(r["path"])
            cur = on_disk.get(r["path"])
            if cur is None:                          # sumiu ou passou a ser excluído
                stale.add(r["path"])
            elif cur[0] != r["size"] or cur[1] != r["mtime"]:
                stale.add(r["path"])
        # Um arquivo ainda existente pode desaparecer de ``on_disk`` quando a
        # política de exclusão muda. Não o envie ao _repair: olhando apenas o
        # filesystem ele pareceria fresco e continuaria vazando dados ignorados.
        removed = indexed - set(on_disk)
        changed = False
        for rel in sorted(removed):
            self.ix.remove_file(rel)
            env.warn(
                f"freshness: {rel} sumiu do disco ou passou a ser excluído; "
                "removido do índice agora."
            )
            changed = True
        stale -= removed

        # arquivos NOVOS no disco que o índice ainda não viu: a resposta vazia
        # pode ser justamente por causa de um deles. _repair os indexa.
        stale |= set(on_disk) - indexed
        if stale:
            return self._repair(stale, env) or changed
        if changed:
            self.ix.resolve_edges()
            env.fresh = False
        return changed

    def _warn_partial(self, rels: set[str], env: Envelope) -> None:
        for rel in sorted(rels):
            row = self.conn.execute(
                "SELECT parse_status FROM files WHERE path=?", (rel,)
            ).fetchone()
            if row is not None and row["parse_status"] != "ok":
                env.warn(f"freshness: {rel} indexado parcialmente (erro de sintaxe no parse).")

    # -- seleção de símbolo ---------------------------------------------------

    def _search_tiers(self, query: str, kind: str | None, limit: int,
                      only_code: bool) -> list:
        """Níveis de casamento, do mais preciso ao mais frouxo.

        A ordem ENTRE níveis (fqn exato > sufixo > nome > FTS > substring) é a
        precisão do casamento e é o que manda. `order` só desempata DENTRO de um
        nível, o que antes vinha arbitrário do SQLite: rank primeiro (símbolo
        central ganha do obscuro) e `file` por último — casar o nome de um
        arquivo costuma valer menos que casar algo declarado dentro dele.
        """
        kind_sql = " AND s.kind=?" if kind else ""
        if only_code:
            ph_low = ",".join("?" * len(LOW_INFO_KINDS))
            kind_sql += f" AND s.kind NOT IN ({ph_low})"
        base = (
            "SELECT s.*, f.path, f.parse_status FROM symbols s "
            "JOIN files f ON s.file_id=f.id WHERE {} " + kind_sql
        )
        order = " ORDER BY (s.kind='file'), s.rank DESC, s.fqn"
        args_kind = [kind] if kind else []
        if only_code:
            args_kind = [*args_kind, *LOW_INFO_KINDS]
        seen: dict[str, dict] = {}

        def take(rows):
            for r in rows:
                if r["id"] not in seen:
                    seen[r["id"]] = dict(r)

        take(self.conn.execute(base.format("s.fqn=?") + order,
                               [query, *args_kind]).fetchall())
        if len(seen) < limit:
            take(self._java_canonical_rows(
                query, kind, limit - len(seen), only_code=only_code))
        if len(seen) < limit:
            take(self._java_legacy_rows(
                query, kind, limit - len(seen), only_code=only_code))
        if len(seen) < limit:
            take(self.conn.execute(
                base.format("s.fqn LIKE ? ESCAPE '\\'") + order + " LIMIT ?",
                [f"%.{like_escape(query)}", *args_kind, limit]).fetchall())
        if len(seen) < limit:
            take(self.conn.execute(
                base.format("s.name=?") + order + " LIMIT ?",
                [query, *args_kind, limit]).fetchall())
        if len(seen) < limit:
            tokens = [t for t in query.replace(".", " ").split() if t]
            if tokens:
                match = " ".join(f'"{t}"' for t in tokens)
                try:
                    # folga sobre o limite: o FTS não conhece rank, então cortar
                    # nele decidiria o resultado antes de qualquer ordenação
                    ids = [r["symbol_id"] for r in self.conn.execute(
                        "SELECT symbol_id FROM symbols_fts WHERE symbols_fts MATCH ? LIMIT ?",
                        (match, limit * FTS_OVERFETCH)).fetchall()]
                except Exception:
                    ids = []
                if ids:
                    ph = ",".join("?" * len(ids))
                    take(self.conn.execute(
                        base.format(f"s.id IN ({ph})") + order + " LIMIT ?",
                        [*ids, *args_kind, limit]).fetchall())
        if len(seen) < limit:
            # Último nível: substring difusa, e o único que pode ignorar caixa.
            # O banco roda com case_sensitive_like=ON porque identificador é
            # case-sensitive e os níveis acima dependem disso — mas aqui isso
            # tornava `openMenu` inalcançável por "menu" (só `Submenu` casava,
            # pelo 'menu' minúsculo interno). `%x%` já não usa índice, então
            # normalizar a caixa não custa plano nenhum.
            take(self.conn.execute(
                base.format("lower(s.name) LIKE ? ESCAPE '\\'") + order + " LIMIT ?",
                [f"%{like_escape(query).lower()}%", *args_kind, limit]).fetchall())
        return list(seen.values())

    def _find_rows(self, query: str, kind: str | None, limit: int) -> list:
        rows = self._search_tiers(query, kind, limit, only_code=False)[:limit]
        # Piso de código. Marcação/estilo são numerosos e pouco informativos:
        # num app React `.menu-item`, `.title-menu` etc. tomavam 10 de 10 slots
        # de find_symbol("menu"). Pior, um símbolo camelCase como
        # `changeModelMenuSubmenu` só é alcançável no ÚLTIMO nível (substring) —
        # que nunca rodava, porque os níveis anteriores já tinham enchido o
        # limite. Uma segunda passada restrita a código não é reordenação: é
        # alcançar o que a primeira nem chegou a consultar.
        #   `kind=` explícito é escolha do chamador — o piso não se aplica.
        #   Resultado vazio: a passada restrita é um SUBCONJUNTO da primeira,
        #   então também viria vazia; e é o caminho caro (dispara a varredura
        #   de frescor), onde queries à toa custam.
        quota = limit // 2
        if rows and kind is None:
            code = [r for r in rows if r["kind"] not in LOW_INFO_KINDS]
            if len(code) < quota:
                have = {r["id"] for r in rows}
                more = [r for r in self._search_tiers(query, kind, limit,
                                                      only_code=True)
                        if r["id"] not in have]
                need = min(quota - len(code), len(more))
                if need:
                    rows = rows[:limit - need] + more[:need]
        # Estar no resultado não basta: no fim da lista o código continua sem
        # ser visto. Casamento EXATO no topo (buscar "menu" deve mostrar `.menu`
        # primeiro, seja ele CSS), código antes de marcação, e o resto na ordem
        # dos níveis — `sort` é estável, então a precisão do casamento sobrevive
        # como desempate dentro de cada grupo. Aplicado SEMPRE: ordenar só no
        # ramo do piso deixaria a ordenação dependendo de quanta marcação o repo
        # tem, que é exatamente o tipo de inconsistência difícil de depurar.
        rows.sort(key=lambda r: (r["fqn"] != query and r["name"] != query,
                                 r["kind"] in LOW_INFO_KINDS))
        return rows

    @staticmethod
    def _java_canonical_fqn(row: dict, package: str) -> str | None:
        """Ponte de leitura para identidades Java path-based de bancos antigos.

        Desde INDEXER_VERSION 37 o extrator persiste o pacote declarado. Bancos
        criados por versões anteriores podem guardar, por exemplo,
        ``src.main.java.com.acme.Svc.Svc.run``. O nome que ferramentas Java e
        usuários conhecem é ``com.acme.Svc.run``. Esta função remove apenas o
        módulo calculado para aquele arquivo, preservando compatibilidade.
        """
        if row["kind"] == "file":
            return None
        path = row["path"]
        stem = path.rsplit(".", 1)[0]
        module = stem.replace("/", ".")
        prefix = f"{module}."
        stored = row["fqn"]
        if not stored.startswith(prefix):
            return None
        declared = stored[len(prefix):]
        if not declared:
            return None
        return f"{package}.{declared}" if package else declared

    def _java_canonical_rows(self, selector: str, kind: str | None,
                             limit: int | None = None, *,
                             only_code: bool = False) -> list[dict]:
        """Localiza aliases legados somente entre símbolos de arquivos Java."""
        if "." not in selector:
            return []
        terminal = selector.rsplit(".", 1)[-1]
        where = ["f.language='java'", "s.name=?"]
        args: list = [terminal]
        if kind:
            where.append("s.kind=?")
            args.append(kind)
        if only_code:
            where.append(
                "s.kind NOT IN (" + ",".join("?" * len(LOW_INFO_KINDS)) + ")"
            )
            args.extend(LOW_INFO_KINDS)
        rows = self.conn.execute(
            "SELECT s.*, f.path, f.parse_status FROM symbols s "
            "JOIN files f ON s.file_id=f.id WHERE " + " AND ".join(where)
            + " ORDER BY (s.kind='file'), s.rank DESC, s.fqn",
            args,
        ).fetchall()
        packages: dict[str, str | None] = {}
        matched: list[dict] = []
        package_re = re.compile(
            r"(?m)^\s*package\s+"
            r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;"
        )
        comments_re = re.compile(r"//[^\r\n]*|/\*.*?\*/", re.DOTALL)
        for raw in rows:
            row = dict(raw)
            declaration = self._java_canonical_fqn(row, "")
            if (declaration is None
                    or (selector != declaration
                        and not selector.endswith(f".{declaration}"))):
                continue
            path = row["path"]
            if path not in packages:
                try:
                    rel = _repo_rel(self.root, path)
                    source = (self.root / rel).read_text(
                        encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    packages[path] = None
                else:
                    match = package_re.search(comments_re.sub("", source))
                    packages[path] = match.group(1) if match else ""
            package = packages[path]
            if package is None:
                continue
            if self._java_canonical_fqn(row, package) == selector:
                matched.append(row)
                if limit is not None and len(matched) >= limit:
                    break
        return matched

    def _java_legacy_rows(self, selector: str, kind: str | None,
                          limit: int | None = None, *,
                          only_code: bool = False) -> list[dict]:
        """Aceita seletores path-based emitidos antes do INDEXER_VERSION 37."""
        if "." not in selector:
            return []
        terminal = selector.rsplit(".", 1)[-1]
        where = ["f.language='java'", "s.name=?", "s.kind!='file'"]
        args: list = [terminal]
        if kind:
            where.append("s.kind=?")
            args.append(kind)
        if only_code:
            where.append(
                "s.kind NOT IN (" + ",".join("?" * len(LOW_INFO_KINDS)) + ")"
            )
            args.extend(LOW_INFO_KINDS)
        rows = self.conn.execute(
            "SELECT s.*, f.path, f.parse_status FROM symbols s "
            "JOIN files f ON s.file_id=f.id WHERE " + " AND ".join(where)
            + " ORDER BY (s.kind='file'), s.rank DESC, s.fqn",
            args,
        ).fetchall()
        package_re = re.compile(
            r"(?m)^\s*package\s+"
            r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;"
        )
        comments_re = re.compile(r"//[^\r\n]*|/\*.*?\*/", re.DOTALL)
        matched: list[dict] = []
        for raw in rows:
            row = dict(raw)
            try:
                rel = _repo_rel(self.root, row["path"])
                source = (self.root / rel).read_text(
                    encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
            match = package_re.search(comments_re.sub("", source))
            package = match.group(1) if match else ""
            canonical = row["fqn"]
            prefix = f"{package}." if package else ""
            if prefix and not canonical.startswith(prefix):
                continue
            declaration = canonical[len(prefix):] if prefix else canonical
            module = row["path"].rsplit(".", 1)[0].replace("/", ".")
            if selector == f"{module}.{declaration}":
                matched.append(row)
                if limit is not None and len(matched) >= limit:
                    break
        return matched

    def _resolve_selector(self, selector: str) -> dict:
        exact = self.conn.execute(
            "SELECT s.*, f.path, f.language FROM symbols s "
            "JOIN files f ON s.file_id=f.id WHERE s.fqn=?",
            (selector,)).fetchall()
        if len(exact) == 1:
            return dict(exact[0])
        if len(exact) > 1:
            # Compatibilidade com o contrato anterior à identidade Java
            # canônica: um alias Java não eclipsava uma identidade exata de
            # outra linguagem. Preserve esse resultado quando existe um único
            # símbolo não-Java; overloads Java continuam ambíguos.
            non_java = [row for row in exact if row["language"] != "java"]
            if len(non_java) == 1:
                return dict(non_java[0])
            raise AmbiguousSymbol(selector, [dict(r) for r in exact])
        canonical = self._java_canonical_rows(selector, None, 9)
        if len(canonical) == 1:
            return canonical[0]
        if len(canonical) > 1:
            raise AmbiguousSymbol(selector, canonical)
        legacy = self._java_legacy_rows(selector, None, 9)
        if len(legacy) == 1:
            return legacy[0]
        if len(legacy) > 1:
            raise AmbiguousSymbol(selector, legacy)
        for where, arg in (("s.fqn LIKE ? ESCAPE '\\'", f"%.{like_escape(selector)}"),
                           ("s.name=?", selector)):
            rows = self.conn.execute(
                f"SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id=f.id "
                f"WHERE {where} LIMIT 9", (arg,)).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
            if len(rows) > 1:
                raise AmbiguousSymbol(selector, [dict(r) for r in rows])
        raise SymbolNotFound(f"símbolo não encontrado: '{selector}'")

    # -- tools ----------------------------------------------------------------

    def find_symbol(self, query: str, kind: str | None = None, limit: int = 10):
        env = Envelope()
        rows = self._find_rows(query, kind, limit)
        if rows:
            repaired = self._repair({r["path"] for r in rows}, env)
            repaired = self._repair_all(env, backstop_only=True) or repaired
        else:
            repaired = self._repair_all(env)
        if repaired:
            rows = self._find_rows(query, kind, limit)
        if len(rows) >= limit:
            env.truncated = True
        self._warn_partial({r["path"] for r in rows}, env)
        return rows, env

    def _resolve_fresh(self, selector: str, env: Envelope) -> dict:
        """Resolve o seletor; se falhar, confere frescor do índice inteiro e tenta de novo."""
        try:
            sym = self._resolve_selector(selector)
            if self._repair_all(env, backstop_only=True):
                try:
                    sym = self._resolve_selector(selector)
                except SymbolNotFound:
                    # Mantém o snapshot apenas como rótulo para a tool poder
                    # devolver resultado vazio + aviso estruturado, em vez de
                    # transformar uma remoção detectada em exceção.
                    env.warn(
                        f"freshness: '{sym['fqn']}' não existe mais após re-indexação."
                    )
            return sym
        except SymbolNotFound:
            if self._repair_all(env):
                return self._resolve_selector(selector)
            raise

    def symbol_info(self, selector: str):
        env = Envelope()
        sym = self._resolve_fresh(selector, env)
        if self._repair({sym["path"]}, env):
            sym = self._resolve_selector(sym["fqn"])
        children = self.conn.execute(
            "SELECT kind, name, fqn, start_line FROM symbols WHERE parent_id=? "
            "ORDER BY start_line", (sym["id"],)).fetchall()
        n_callers = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE dst=? AND kind='calls'", (sym["id"],)
        ).fetchone()["c"]
        n_callees = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE src=? AND kind='calls'", (sym["id"],)
        ).fetchone()["c"]
        n_refs = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE dst=?", (sym["id"],)).fetchone()["c"]
        self._warn_partial({sym["path"]}, env)
        domain = None
        if sym.get("community") is not None:
            drow = self.conn.execute(
                "SELECT id, label, size FROM communities WHERE id=?",
                (sym["community"],)).fetchone()
            if drow is not None:
                domain = {"id": drow["id"], "label": drow["label"], "size": drow["size"]}
        info = {
            "symbol": sym,
            "children": [dict(c) for c in children],
            "counts": {"callers": n_callers, "callees": n_callees, "references": n_refs},
            "domain": domain,
        }
        return info, env

    def references(self, selector: str, kind: str | None = None, limit: int = 60):
        env = Envelope()
        sym = self._resolve_fresh(selector, env)

        def q():
            kind_sql = " AND e.kind=?" if kind else ""
            args = [sym["id"], *([kind] if kind else []), limit]
            return self.conn.execute(
                f"SELECT e.kind, e.line, e.confidence, e.resolver, "
                f"f.language AS site_language, f.path AS site_path, "
                f"s.fqn AS src_fqn FROM edges e JOIN files f ON e.file_id=f.id "
                f"LEFT JOIN symbols s ON e.src=s.id WHERE e.dst=?{kind_sql} "
                f"ORDER BY f.path, e.line LIMIT ?", args).fetchall()

        rows = q()
        involved = {r["site_path"] for r in rows} | {sym["path"]}
        if self._repair(involved, env):
            try:
                sym = self._resolve_selector(sym["fqn"])
            except SymbolNotFound:
                env.warn(f"freshness: '{sym['fqn']}' não existe mais após re-indexação.")
                return sym, [], env
            rows = q()
        self._completeness(sym, rows, env)
        if len(rows) >= limit:
            env.truncated = True
        self._warn_partial({r["site_path"] for r in rows}, env)
        return sym, [explain.annotate(dict(r)) for r in rows], env

    def callers(self, selector: str, depth: int = 1):
        return self._call_walk(selector, depth, direction="in")

    def callees(self, selector: str, depth: int = 1):
        return self._call_walk(selector, depth, direction="out")

    def _call_walk(self, selector: str, depth: int, direction: str):
        env = Envelope()
        sym = self._resolve_fresh(selector, env)

        def walk():
            results, frontier, seen = [], {sym["id"]}, {sym["id"]}
            for d in range(1, depth + 1):
                ph = ",".join("?" * len(frontier))
                if direction == "in":
                    rows = self.conn.execute(
                        f"SELECT e.line, e.confidence, e.resolver, "
                        f"f.language AS site_language, e.dst_name, f.path AS site_path, "
                        f"s.id AS other_id, s.fqn AS other_fqn, s.kind AS other_kind "
                        f"FROM edges e JOIN files f ON e.file_id=f.id "
                        f"LEFT JOIN symbols s ON e.src=s.id "
                        f"WHERE e.dst IN ({ph}) AND e.kind='calls' "
                        f"ORDER BY f.path, e.line", list(frontier)).fetchall()
                else:
                    rows = self.conn.execute(
                        f"SELECT e.line, e.confidence, e.resolver, "
                        f"f.language AS site_language, e.dst_name, f.path AS site_path, "
                        f"s.id AS other_id, s.fqn AS other_fqn, s.kind AS other_kind "
                        f"FROM edges e LEFT JOIN symbols s ON e.dst=s.id "
                        f"JOIN files f ON e.file_id=f.id "
                        f"WHERE e.src IN ({ph}) AND e.kind='calls' "
                        f"ORDER BY f.path, e.line", list(frontier)).fetchall()
                nxt = set()
                for r in rows:
                    results.append(explain.annotate({**dict(r), "depth": d}))
                    oid = r["other_id"]
                    if oid and oid not in seen:
                        seen.add(oid)
                        nxt.add(oid)
                if not nxt:
                    break
                frontier = nxt
            return results

        rows = walk()
        involved = {r["site_path"] for r in rows} | {sym["path"]}
        if self._repair(involved, env):
            try:
                sym = self._resolve_selector(sym["fqn"])
            except SymbolNotFound:
                env.warn(f"freshness: '{sym['fqn']}' não existe mais após re-indexação.")
                return sym, [], env
            rows = walk()
        self._completeness(sym, rows, env)
        self._warn_partial(involved, env)
        return sym, rows, env

    def _impact_rows(self, seed_id: str, depth: int) -> list[dict]:
        """Impacto por id sobre o snapshot atual, sem acionar read-repair.

        O helper permite que ``change_impact`` capture dependentes do snapshot
        antigo antes que um rename/delete remova o símbolo do banco.
        """
        results: list[dict] = []
        frontier: dict[str, str] = {seed_id: "certain"}
        seen = {seed_id}
        kinds_ph = ",".join("?" * len(IMPACT_KINDS))
        for d in range(1, depth + 1):
            ph = ",".join("?" * len(frontier))
            rows = self.conn.execute(
                f"SELECT e.src, e.dst, e.kind, e.confidence, e.resolver, "
                f"f.language AS site_language, s.fqn, s.kind AS skind, "
                f"s.rank, s.start_line, f.path FROM edges e "
                f"JOIN symbols s ON e.src=s.id JOIN files f ON s.file_id=f.id "
                f"WHERE e.dst IN ({ph}) AND e.kind IN ({kinds_ph}) "
                f"AND e.src IS NOT NULL",
                [*frontier.keys(), *IMPACT_KINDS]).fetchall()
            nxt: dict[str, str] = {}
            for r in rows:
                path_conf = min(frontier[r["dst"]], r["confidence"],
                                key=lambda c: _CONF_ORD[c])
                if r["src"] in seen:
                    continue
                seen.add(r["src"])
                nxt[r["src"]] = path_conf
                results.append({
                    "fqn": r["fqn"], "kind": r["skind"], "rank": r["rank"],
                    "path": r["path"], "start_line": r["start_line"],
                    "depth": d, "confidence": path_conf, "via": r["kind"],
                    "resolver": explain.resolver_label(
                        r["resolver"], r["site_language"]),
                })
            if not nxt:
                break
            frontier = nxt
        results.sort(key=lambda r: (r["depth"], -r["rank"]))
        return results

    def impact(self, selector: str, depth: int = 3):
        """Fecho transitivo de dependentes: o que pode quebrar se eu mudar isto.

        Confiança do caminho = mínima entre as arestas percorridas.
        """
        env = Envelope()
        sym = self._resolve_fresh(selector, env)
        ensure_ranks(self.conn)
        rows = self._impact_rows(sym["id"], depth)
        if self._repair({r["path"] for r in rows} | {sym["path"]}, env):
            try:
                sym = self._resolve_selector(sym["fqn"])
            except SymbolNotFound:
                env.warn(f"freshness: '{sym['fqn']}' não existe mais após re-indexação.")
                return sym, [], env
            rows = self._impact_rows(sym["id"], depth)
        self._completeness(sym, rows, env)
        return sym, rows, env

    def reaches(self, selector: str, sink: str = "http", via: str | None = None,
                depth: int = 8, max_paths: int = 20, deadline_ms: int | None = None,
                max_steps: int | None = None, should_cancel=None):
        """Reachability entry→sink numa resposta só: seguindo o call graph a
        partir de `selector`, quais caminhos chegam a uma chamada que casa com
        `sink` (preset em SINK_PRESETS ou regex livre), e um validador/sanitizer
        `via` (ex.: um sanitizer) aparece em algum ponto do caminho?

        Substitui o LLM montando o caminho salto a salto lendo código: o grafo
        entrega a cadeia + o veredito de validação já pronto. Interprocedural,
        sob demanda (sempre fresco). Confiança do caminho = mínima das arestas.

        Mesma anti-explosão de `taint`: `deadline_ms`/`max_steps`/`should_cancel`
        limitam a enumeração e devolvem PARCIAL; o cap `max_paths` e qualquer
        limite marcam `env.truncated=True` e `limit_hit` no retorno.
        """
        import re as _re

        env = Envelope()
        sym = self._resolve_fresh(selector, env)
        sink_rx = _re.compile(SINK_PRESETS.get(sink, sink), _re.I)
        via_rx = _re.compile(_re.escape(via), _re.I) if via else None
        budget = _Budget(deadline_ms, max_steps, should_cancel)

        def walk():
            # BFS forward; parent[]/pconf[] p/ reconstruir cadeia e confiança.
            # `seen` já dedupe nós globalmente (uma visita por nó no call graph).
            parent = {sym["id"]: None}
            pconf = {sym["id"]: "certain"}
            calls_via = set()          # ids de funções que chamam o validador
            hits = []                  # (node_id, sink_name, line, path, conf)
            frontier = {sym["id"]}
            seen = {sym["id"]}
            for _d in range(depth):
                if not frontier or budget.hit():
                    break
                ph = ",".join("?" * len(frontier))
                rows = self.conn.execute(
                    f"SELECT e.src, e.dst, e.dst_name, e.confidence, e.line, "
                    f"f.path AS site_path, s.fqn AS dst_fqn FROM edges e "
                    f"JOIN files f ON e.file_id=f.id "
                    f"LEFT JOIN symbols s ON e.dst=s.id "
                    f"WHERE e.src IN ({ph}) AND e.kind='calls' "
                    f"ORDER BY e.line", list(frontier)).fetchall()
                nxt = set()
                for r in rows:
                    if budget.hit():
                        break
                    budget.tick()
                    src, tgt = r["src"], r["dst_name"] or ""
                    econf = min(pconf.get(src, "certain"), r["confidence"],
                                key=lambda c: _CONF_ORD[c])
                    if via_rx and via_rx.search(tgt):
                        calls_via.add(src)
                    if sink_rx.search(tgt):
                        hits.append((src, tgt, r["line"], r["site_path"], econf))
                    dst = r["dst"]
                    if dst and dst not in seen:
                        seen.add(dst)
                        parent[dst] = src
                        pconf[dst] = econf
                        nxt.add(dst)
                frontier = nxt
            return parent, pconf, calls_via, hits

        def _chain_ids(parent, node):
            ids, cur, guard = [], node, 0
            while cur is not None and guard < depth + 2:
                ids.append(cur)
                cur = parent.get(cur)
                guard += 1
            ids.reverse()
            return ids

        def build(parent, pconf, calls_via, hits):
            # 1 caminho (o mais curto) por função-sink
            best = {}
            for node, sink_name, line, spath, conf in hits:
                ids = _chain_ids(parent, node)
                if node not in best or len(ids) < len(best[node][0]):
                    best[node] = (ids, sink_name, line, spath, conf)
            # resolve fqns de TODOS os nós de TODAS as cadeias numa query
            allids = {sym["id"]} | {i for ids, *_ in best.values() for i in ids}
            id2fqn = {sym["id"]: sym["fqn"]}
            miss = [i for i in allids if i not in id2fqn]
            if miss:
                ph = ",".join("?" * len(miss))
                for row in self.conn.execute(
                        f"SELECT id, fqn FROM symbols WHERE id IN ({ph})", miss):
                    id2fqn[row["id"]] = row["fqn"]
            out = []
            for ids, sink_name, line, spath, conf in best.values():
                out.append({"chain": [id2fqn.get(i, "?") for i in ids],
                            "sink_call": sink_name, "line": line, "site_path": spath,
                            "confidence": conf,
                            "via_present": any(i in calls_via for i in ids)})
            out.sort(key=lambda r: (len(r["chain"]), r["site_path"]))
            if len(out) > max_paths:
                budget.note("paths")           # havia mais caminhos que o teto
            return out[:max_paths]

        parent, pconf, calls_via, hits = walk()
        paths = build(parent, pconf, calls_via, hits)
        involved = {p["site_path"] for p in paths} | {sym["path"]}
        if self._repair(involved, env):
            try:
                sym = self._resolve_selector(sym["fqn"])
            except SymbolNotFound:
                env.warn(f"freshness: '{sym['fqn']}' não existe mais após re-indexação.")
                return sym, {"sink": sink, "via": via, "paths": [],
                             "elapsed_ms": budget.elapsed_ms(),
                             "steps": budget.steps, "limit_hit": budget.limit_hit}, env
            parent, pconf, calls_via, hits = walk()
            paths = build(parent, pconf, calls_via, hits)
        if budget.limit_hit:
            env.truncated = True
            env.warn(f"truncated: reachability parada em '{budget.limit_hit}' — "
                     f"caminhos PARCIAIS. Reduza depth, ou aumente "
                     f"deadline_ms/max_steps/max_paths.")
        env.warn("reachability: estática (arestas 'calls'); chamadas dinâmicas/"
                 "reflexivas podem faltar. Confiança = mínima do caminho.")
        return sym, {"sink": sink, "via": via, "paths": paths,
                     "elapsed_ms": budget.elapsed_ms(), "steps": budget.steps,
                     "limit_hit": budget.limit_hit}, env

    def ego_graph(self, selector: str):
        """Vizinhança imediata de um símbolo: todas as arestas tipadas, in e out."""
        env = Envelope()
        sym = self._resolve_fresh(selector, env)
        if self._repair({sym["path"]}, env):
            sym = self._resolve_selector(sym["fqn"])
        out_rows = self.conn.execute(
            "SELECT e.kind, e.confidence, e.resolver, f.language AS site_language, "
            "e.line, e.dst_name, s.fqn AS other_fqn "
            "FROM edges e JOIN files f ON e.file_id=f.id "
            "LEFT JOIN symbols s ON e.dst=s.id "
            "WHERE e.src=? ORDER BY e.kind, e.line", (sym["id"],)).fetchall()
        in_rows = self.conn.execute(
            "SELECT e.kind, e.confidence, e.resolver, f.language AS site_language, "
            "e.line, f.path AS site_path, "
            "s.fqn AS other_fqn FROM edges e JOIN files f ON e.file_id=f.id "
            "LEFT JOIN symbols s ON e.src=s.id "
            "WHERE e.dst=? ORDER BY e.kind, f.path, e.line", (sym["id"],)).fetchall()
        children = self.conn.execute(
            "SELECT kind, name, start_line FROM symbols WHERE parent_id=? "
            "ORDER BY start_line", (sym["id"],)).fetchall()
        parent = None
        if sym.get("parent_id"):
            p = self.conn.execute(
                "SELECT fqn FROM symbols WHERE id=?", (sym["parent_id"],)).fetchone()
            parent = p["fqn"] if p else None
        self._completeness(sym, [], env)
        data = {
            "symbol": sym, "parent": parent,
            "children": [dict(c) for c in children],
            "out": [explain.annotate(dict(r)) for r in out_rows],
            "in": [explain.annotate(dict(r)) for r in in_rows],
        }
        return data, env

    def overview(self, scope: str | None = None, token_budget: int = 1200):
        """Mapa ranqueado do repo: arquivos e seus top símbolos por PageRank."""
        env = Envelope()
        ensure_ranks(self.conn)
        where, args = "", []
        if scope:
            where = "WHERE f.path LIKE ? ESCAPE '\\'"
            args = [like_escape(scope.rstrip('/').replace('\\', '/')) + "%"]
        files = self.conn.execute(
            f"SELECT f.id, f.path, f.parse_status, "
            f"(SELECT MAX(rank) FROM symbols s WHERE s.file_id=f.id) AS score "
            f"FROM files f {where} ORDER BY score DESC", args).fetchall()
        self._repair({f["path"] for f in files}, env)
        result = []
        char_budget = token_budget * 4
        used = 0
        for f in files:
            if f["score"] is None:
                continue
            syms = self.conn.execute(
                "SELECT kind, name, fqn, signature, start_line, rank FROM symbols "
                "WHERE file_id=? AND parent_id IS NULL ORDER BY rank DESC, start_line "
                "LIMIT 6", (f["id"],)).fetchall()
            entry = {"path": f["path"], "symbols": [dict(s) for s in syms]}
            cost = len(f["path"]) + sum(
                len(s["signature"] or s["name"]) + 12 for s in entry["symbols"])
            if used + cost > char_budget and result:
                env.truncated = True
                env.warn("truncated: budget de tokens atingido — use scope para "
                         "detalhar um diretório.")
                break
            used += cost
            result.append(entry)
        return result, env

    def communities(self, limit: int = 20, min_size: int = 3):
        """Domínios do repo: comunidades do grafo (Louvain) com seus símbolos-hub.

        Mapa de alto nível — 'que subsistemas existem e o que mora em cada um' —
        que não está escrito em nenhum arquivo. Estrutural, sem custo de LLM;
        o label opcional por domínio é gerado sob demanda via `describe domain:N`.
        """
        env = Envelope()
        self._repair_all(env)
        ensure_communities(self.conn)
        ensure_ranks(self.conn)
        totals = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM communities").fetchone()
        assigned = self.conn.execute(
            "SELECT COUNT(*) c FROM symbols WHERE community IS NOT NULL").fetchone()["c"]
        rows = self.conn.execute(
            "SELECT id, size, label, summary FROM communities "
            "WHERE size>=? ORDER BY size DESC LIMIT ?", (min_size, limit)).fetchall()
        result = []
        for c in rows:
            top = self.conn.execute(
                "SELECT s.fqn, s.kind FROM symbols s WHERE s.community=? "
                "ORDER BY s.rank DESC, s.fqn LIMIT 6", (c["id"],)).fetchall()
            files = self.conn.execute(
                "SELECT f.path, COUNT(*) c FROM symbols s "
                "JOIN files f ON s.file_id=f.id WHERE s.community=? "
                "GROUP BY f.path ORDER BY c DESC, f.path LIMIT 4", (c["id"],)).fetchall()
            result.append({
                "id": c["id"], "size": c["size"],
                "label": c["label"], "summary": c["summary"],
                "top_symbols": [dict(r) for r in top],
                "top_files": [dict(r) for r in files],
            })
        meta = {"total": totals["n"], "assigned": assigned,
                "shown": len(result), "min_size": min_size}
        return result, meta, env

    # -- dataflow / taint (docs/RESEARCH.md §6) -------------------------------

    def _df_parse(self, path: str, lang: str, cache: dict):
        if path not in cache:
            try:
                data = (self.root / path).read_bytes()
                cache[path] = (data, get_parser(lang).parse(data))
            except OSError:
                cache[path] = (None, None)
        return cache[path]

    def _df_facts(self, sym_row, cache: dict):
        """Extrai os fatos de fluxo de uma função. Retorna (FnFacts|None, lang).

        Dois níveis: `cache` (por chamada) guarda a árvore parseada; o LRU da
        engine (`self._facts_cache`) guarda os FATOS entre chamadas, chaveado por
        conteúdo (invalida no re-index sem passo explícito)."""
        from . import dataflow as df

        row = self.conn.execute(
            "SELECT language, content_hash FROM files WHERE id=?",
            (sym_row["file_id"],)).fetchone()
        lang = row["language"]
        if not df.supported(lang):
            return None, lang
        ck = (sym_row["file_id"], row["content_hash"],
              sym_row["start_line"], sym_row.get("start_col"))
        fc = self._facts_cache
        hit = fc.get(ck, _MISS)
        if hit is not _MISS:
            fc.move_to_end(ck)                 # LRU: renova o recém-usado
            return hit, lang
        data, tree = self._df_parse(sym_row["path"], lang, cache)
        if tree is None:
            return None, lang                  # erro de I/O: não cacheia (transitório)
        # símbolo de ARQUIVO: o corpo é a raiz. Em linguagem de script o código
        # perigoso mora fora de qualquer função (o DVWA inteiro é assim), e uma
        # varredura que só itera funções não enxerga nada dele.
        if sym_row.get("kind") == "file":
            fn = tree.root_node
        else:
            fn = df.find_function_node(
                tree.root_node, sym_row["start_line"], lang,
                sym_row.get("start_col"))
        facts = df.extract_facts(data, fn, lang) if fn is not None else None
        fc[ck] = facts
        if len(fc) > self._facts_cache_cap:
            fc.popitem(last=False)             # despeja o mais antigo
        return facts, lang

    def _df_resolve_calls(self, src_id, line, name=None, col=None):
        """Todos os alvos de uma chamada, preservando overloads/fan-out L1."""
        filters = []
        args = [src_id, line]
        if name:
            filters.append("s.name=?")
            args.append(name)
        if col is not None:
            filters.append("e.col=?")
            args.append(col)
        extra = (" AND " + " AND ".join(filters)) if filters else ""
        rows = self.conn.execute(
            "SELECT e.dst, e.col, e.confidence, s.fqn, s.kind, s.start_line, "
            "s.parent_id, s.signature, s.visibility, s.name, "
            "f.path, f.language FROM edges e JOIN symbols s ON e.dst=s.id "
            "JOIN files f ON s.file_id=f.id WHERE e.src=? AND e.kind='calls' "
            f"AND e.line=? AND e.dst IS NOT NULL{extra} "
            "ORDER BY CASE e.confidence WHEN 'certain' THEN 0 "
            "WHEN 'inferred' THEN 1 ELSE 2 END, e.dst", tuple(args)).fetchall()
        if col is None and len({row["col"] for row in rows}) > 1:
            return []
        return [dict(row) for row in rows]

    def _df_resolve_call(self, src_id, line, name=None, col=None):
        """Alvo preferido da chamada; consumidores conservadores usam todos.

        `name` desempata quando há mais de uma chamada na linha. Consumidores
        que produzem uma prova negativa (por exemplo, ``não propaga``) não podem
        escolher este primeiro representante: devem unir ``_df_resolve_calls``.
        """
        rows = self._df_resolve_calls(src_id, line, name, col)
        return rows[0] if rows else None

    def _df_call_identity(self, src_id, line, name, col=None) -> str | None:
        """Canonical extractor identity at a callsite, resolved or dangling.

        Python dataflow sees the syntax ``alias.loads``; the graph extractor
        also knows that ``import pickle as alias`` means ``pickle.loads``.
        Security classification must use the latter identity so a misleading
        alias cannot turn an unsafe deserializer into stdlib JSON.
        """
        col_filter = " AND e.col=?" if col is not None else ""
        args = [src_id, line, name, f"%.{name}"]
        if col is not None:
            args.append(col)
        rows = self.conn.execute(
            "SELECT DISTINCT e.dst_name FROM edges e WHERE e.src=? "
            "AND e.kind='calls' AND e.line=? "
            "AND (e.dst_name=? OR e.dst_name LIKE ?)" + col_filter,
            tuple(args),
        ).fetchall()
        identities = {row["dst_name"] for row in rows if row["dst_name"]}
        return next(iter(identities)) if len(identities) == 1 else None

    def _df_unique_call_target(self, src_id, line, name, col=None) -> bool:
        """True only for one resolved (non-possible) target at a callsite."""
        col_filter = " AND e.col=?" if col is not None else ""
        args = [src_id, line, name, f"%.{name}"]
        if col is not None:
            args.append(col)
        rows = self.conn.execute(
            "SELECT e.dst, e.col FROM edges e WHERE e.src=? AND e.kind='calls' "
            "AND e.line=? AND e.dst IS NOT NULL "
            "AND (e.dst_name=? OR e.dst_name LIKE ?) "
            "AND e.confidence IN ('certain','inferred')" + col_filter,
            tuple(args),
        ).fetchall()
        if col is None and len({row["col"] for row in rows}) > 1:
            return False
        return len({row["dst"] for row in rows}) == 1

    def _df_java_exact_receiver_call(self, facts, arg_flow):
        """Resolve ``Base x = new Concrete(); x.call(...)`` without guessing."""
        calls = [
            call for call in facts.calls
            if call.span == arg_flow.span and call.callee == arg_flow.callee
            and call.qualified and "." in call.qualified
        ]
        if len(calls) != 1:
            return None
        receiver = calls[0].qualified.rsplit(".", 1)[0]
        if "." in receiver or receiver not in facts.local_names:
            return None
        definitions = [
            assignment for assignment in facts.assigns
            if assignment.span and calls[0].span
            and assignment.span[0] < calls[0].span[0]
            and assignment.targets == {(receiver,)}
        ]
        if len(definitions) != 1 or definitions[0].rhs_call is None:
            return None
        concrete = definitions[0].rhs_call.rsplit(".", 1)[-1].split("<", 1)[0]
        rows = self.conn.execute(
            "SELECT m.id AS dst, m.fqn, m.kind, m.start_line, f.path, "
            "f.language FROM symbols m JOIN symbols owner ON m.parent_id=owner.id "
            "JOIN files f ON m.file_id=f.id "
            "WHERE owner.name=? AND m.name=? "
            "AND m.kind IN ('method','function')",
            (concrete, arg_flow.callee),
        ).fetchall()
        if len(rows) != 1:
            return None
        out = dict(rows[0])
        out["confidence"] = "certain"
        return out

    def _nonprop_spans(self, sym_row, facts, return_dependencies, cache):
        """Spans cuja atribuição chama função que NÃO devolve o argumento.

        Exige `confidence='certain'`, isto é, chamada resolvida SEMANTICAMENTE
        pela camada L1 (LSP). Esta é a diferença entre otimização e defeito:
        uma tentativa anterior resolvia por nome e apagou 109 vulnerabilidades
        reais do OWASP Benchmark, porque `doSomething` existe em centenas de
        arquivos com corpos diferentes. Sem L1 rodado, nada é morto — o motor
        volta a over-aproximar, que é o lado seguro."""
        if not return_dependencies:
            return frozenset()
        chave = sym_row["id"]
        if chave in cache:
            return cache[chave]
        out = {}
        for a in facts.assigns:
            if a.rhs_call is None:
                continue
            sites = [
                call for call in facts.calls
                if call.callee == a.rhs_call and call.span and a.span
                and a.span[0] <= call.span[0]
                and call.span[1] <= a.span[1]
            ]
            site = sites[0] if len(sites) == 1 else None
            targets = self._df_resolve_calls(
                chave, a.line, a.rhs_call,
                site.col if site is not None else None)
            if not targets or a.span is None:
                continue
            dependencies: set[int] = set()
            proven = True
            for target in targets:
                typed_closed = False
                if site is not None and site.receiver_type:
                    owner = self.conn.execute(
                        "SELECT name FROM symbols WHERE id=?",
                        (target.get("parent_id"),),
                    ).fetchone()
                    typed_closed = bool(
                        owner
                        and owner["name"]
                        == site.receiver_type.rsplit(".", 1)[-1]
                        and self._java_dispatch_is_closed(target)
                    )
                target_dependencies = return_dependencies.get(target["dst"])
                if ((target["confidence"] != "certain" and not typed_closed)
                        or target_dependencies is None):
                    proven = False
                    break
                dependencies.update(target_dependencies)
            if proven:
                out[a.span] = frozenset(dependencies)
        cache[chave] = out
        return cache[chave]

    def _source_wrapper_spans(self, sym_row, facts, wrapper_fqns):
        """Assignments whose RHS resolves uniquely to a source-wrapper FQN."""
        if not wrapper_fqns:
            return frozenset()
        out = set()
        for assignment in facts.assigns:
            if assignment.rhs_call is None:
                continue
            same_this = any(
                call.span is not None and assignment.span is not None
                and assignment.span[0] <= call.span[0]
                and call.span[1] <= assignment.span[1]
                and call.callee == assignment.rhs_call
                and call.receiver_kind in {"implicit_this", "explicit_this"}
                for call in facts.calls
            )
            if same_this and sym_row.get("parent_id") is not None:
                local = self.conn.execute(
                    "SELECT id, fqn FROM symbols WHERE parent_id=? AND name=?",
                    (sym_row["parent_id"], assignment.rhs_call),
                ).fetchall()
                local_targets = {row["id"]: row["fqn"] for row in local}
                if (len(local_targets) == 1
                        and next(iter(local_targets.values())) in wrapper_fqns):
                    out.add(assignment.span)
                    continue
            # ``new ConcreteType().wrapper()`` carries an exact receiver type
            # in Java syntax.  Resolve it against the wrapper's enclosing type
            # instead of falling back to a globally ambiguous method name.
            # FQNs are matched by suffix because Java symbols retain the source
            # root prefix (for example ``Java.src.com.acme.Type``).
            receiver_type = assignment.rhs_receiver_type
            if receiver_type:
                receiver_simple = receiver_type.rsplit(".", 1)[-1]
                constructors = self.conn.execute(
                    "SELECT DISTINCT e.dst FROM edges e "
                    "JOIN symbols c ON e.dst=c.id "
                    "WHERE e.src=? AND e.kind='calls' AND e.line=? "
                    "AND e.dst IS NOT NULL AND c.name=? "
                    "AND c.kind IN ('class','struct','type')",
                    (sym_row["id"], assignment.line, receiver_simple),
                ).fetchall()
                class_ids = {row["dst"] for row in constructors}
                typed = []
                if len(class_ids) == 1:
                    typed = self.conn.execute(
                        "SELECT id, fqn FROM symbols "
                        "WHERE parent_id=? AND name=?",
                        (next(iter(class_ids)), assignment.rhs_call),
                    ).fetchall()
                typed_targets = {row["id"]: row["fqn"] for row in typed}
                if (len(typed_targets) == 1
                        and next(iter(typed_targets.values())) in wrapper_fqns):
                    out.add(assignment.span)
                    continue
            rows = self.conn.execute(
                "SELECT DISTINCT e.dst, s.fqn FROM edges e "
                "JOIN symbols s ON e.dst=s.id "
                "WHERE e.src=? AND e.kind='calls' AND e.line=? "
                "AND e.dst IS NOT NULL AND s.name=?",
                (sym_row["id"], assignment.line, assignment.rhs_call),
            ).fetchall()
            if len(rows) == 1 and rows[0]["fqn"] in wrapper_fqns:
                out.add(assignment.span)
        return frozenset(out)

    def _df_resolve_same_this_calls(self, sym_row, receiver_flow):
        """Possible call targets on the same concrete enclosing class."""
        parent_id = sym_row.get("parent_id")
        if parent_id is None:
            return []
        col_filter = " AND e.col=?" if receiver_flow.col is not None else ""
        args = [sym_row["id"], receiver_flow.line, receiver_flow.callee,
                parent_id]
        if receiver_flow.col is not None:
            args.append(receiver_flow.col)
        rows = self.conn.execute(
            "SELECT DISTINCT e.dst, e.confidence, s.fqn, s.kind, "
            "s.start_line, s.parent_id, s.signature, s.visibility, s.name, "
            "f.path, f.language "
            "FROM edges e JOIN symbols s ON e.dst=s.id "
            "JOIN files f ON s.file_id=f.id "
            "WHERE e.src=? AND e.kind='calls' AND e.line=? "
            "AND e.dst IS NOT NULL AND s.name=? AND s.parent_id=?"
            + col_filter,
            tuple(args),
        ).fetchall()
        targets = {row["dst"]: dict(row) for row in rows}
        out = []
        for target in targets.values():
            target["closed_dispatch"] = self._java_dispatch_is_closed(target)
            out.append(target)
        return sorted(out, key=lambda target: target["dst"])

    def _df_resolve_same_this_call(self, sym_row, receiver_flow):
        """Unique same-receiver target, for callers that cannot join fan-out."""
        targets = self._df_resolve_same_this_calls(sym_row, receiver_flow)
        return targets[0] if len(targets) == 1 else None

    def _java_dispatch_is_closed(self, target) -> bool:
        """Whether a same-``this`` Java call may safely export field kills.

        Dirty effects are monotone and may be applied conservatively.  A kill
        is different: it is valid only when dispatch cannot select an override
        with a different post-state.  Language modifiers prove this directly;
        otherwise the indexed inheritance graph must contain no overriding
        descendant and no unresolved homonymous inheritance edge.
        """
        signature = target.get("signature") or ""
        modifiers = set(re.findall(r"[A-Za-z_$][\w$]*", signature))
        if (target.get("visibility") == "private"
                or modifiers & {"final", "static"}):
            return True
        parent_id = target.get("parent_id")
        if parent_id is None:
            return False
        owner = self.conn.execute(
            "SELECT name, signature FROM symbols WHERE id=?",
            (parent_id,),
        ).fetchone()
        if owner is None:
            return False
        owner_modifiers = set(re.findall(
            r"[A-Za-z_$][\w$]*", owner["signature"] or ""))
        if "final" in owner_modifiers:
            return True
        # An unresolved `extends Owner` means the graph cannot prove the set of
        # descendants closed.  Stay fail-closed even if no resolved child exists.
        unresolved = self.conn.execute(
            "SELECT 1 FROM edges WHERE kind='inherits' AND dst IS NULL "
            "AND dst_name=? LIMIT 1",
            (owner["name"],),
        ).fetchone()
        if unresolved is not None:
            return False
        override = self.conn.execute(
            "WITH RECURSIVE descendants(id) AS ("
            " SELECT src FROM edges WHERE kind='inherits' AND dst=? "
            " UNION "
            " SELECT e.src FROM edges e JOIN descendants d ON e.dst=d.id "
            " WHERE e.kind='inherits'"
            ") SELECT 1 FROM descendants d JOIN symbols m ON m.parent_id=d.id "
            "WHERE m.name=? AND m.kind IN ('method','function') LIMIT 1",
            (parent_id, target["name"]),
        ).fetchone()
        return override is None

    def _crow(self, sym_id):
        r = self.conn.execute(
            "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id=f.id "
            "WHERE s.id=?", (sym_id,)).fetchone()
        return dict(r) if r is not None else None

    def data_flow(self, selector: str, depth: int = 2):
        """Fluxo de dados de uma função: para onde vão os dados de cada parâmetro.

        Intra-procedural (def-use, may-taint) por função, composto ao longo do
        call graph (inter-procedural) até `depth` saltos. Responde 'esta função
        recebe X e o repassa para quem'. Sempre fresco. Ver docs/RESEARCH.md §6.
        """
        from . import dataflow as df

        env = Envelope()
        sym = self._resolve_fresh(selector, env)
        if self._repair({sym["path"]}, env):
            sym = self._resolve_selector(sym["fqn"])
        cache: dict = {}
        receiver_summary_cache: dict = {}
        receiver_summary_building: set = set()
        facts, lang = self._df_facts(sym, cache)
        if facts is None:
            env.warn(f"dataflow: linguagem '{lang}' ainda sem análise de fluxo "
                     f"(suportadas: {', '.join(df.supported_langs())}).")
            return {"function": sym, "supported": False, "params": []}, env

        def receiver_effects(sym_row, f):
            effects = {}
            for call in f.calls:
                if (call.span is None
                        or call.receiver_kind not in {
                            "implicit_this", "explicit_this"}):
                    continue
                callees = self._df_resolve_same_this_calls(sym_row, call)
                if not callees:
                    continue
                candidates = []
                for callee in callees:
                    summary_key = callee["dst"]
                    if summary_key not in receiver_summary_cache:
                        if summary_key in receiver_summary_building:
                            continue
                        receiver_summary_building.add(summary_key)
                        try:
                            crow = self._crow(summary_key)
                            cf, clang = (self._df_facts(crow, cache)
                                         if crow else (None, None))
                            if cf is None or clang != "java":
                                receiver_summary_cache[summary_key] = None
                            else:
                                nested = receiver_effects(crow, cf)
                                same_receiver_calls = [
                                    site for site in cf.calls
                                    if site.receiver_kind in {
                                        "implicit_this", "explicit_this"}
                                ]
                                receiver_summary_cache[summary_key] = (
                                    df.summarize_java_receiver_effect(
                                        cf, receiver_effects=nested,
                                        allow_overwrites=(
                                            callee["closed_dispatch"]
                                            and not df.java_receiver_may_escape(cf)
                                            and all(site.span in nested
                                                    for site in
                                                    same_receiver_calls))))
                        finally:
                            receiver_summary_building.discard(summary_key)
                    effect = receiver_summary_cache.get(summary_key)
                    if effect is not None:
                        candidates.append(effect)
                if candidates:
                    if len(candidates) != len(callees):
                        # An unresolved/recursive candidate contributes no
                        # proven kills.  Joining a neutral effect preserves
                        # dirty unions while forcing overwrite intersection
                        # to empty.
                        candidates.append(df.ReceiverEffect())
                    effects[call.span] = df.merge_receiver_effects(candidates)
            return effects

        def trace(sym_row, tainted, d, visited):
            f, flang = self._df_facts(sym_row, cache)
            if f is None:
                return []
            same_this = receiver_effects(sym_row, f) if flang == "java" else None
            flow = df.analyze(f, tainted, lang=flang,
                              receiver_effects=same_this)
            sinks = []
            for af in flow.arg_flows:
                callee = self._df_resolve_call(
                    sym_row["id"], af.line, af.callee, af.col)
                if flang == "java":
                    callee = (self._df_java_exact_receiver_call(f, af)
                              or callee)
                sinks.append({
                    "callee_name": af.callee, "arg_index": af.arg_index,
                    "line": af.line, "column": af.col,
                    "byte_span": af.span, "via": af.via, "depth": d,
                    "site_path": sym_row["path"], "resolved": callee is not None,
                    "callee_fqn": callee["fqn"] if callee else None,
                    "confidence": callee["confidence"] if callee else None,
                    "callee_path": callee["path"] if callee else None,
                    "callee_line": callee["start_line"] if callee else None,
                })
                free_forwarder = (
                    flang == "java"
                    and df.java_transparent_forwarder_span(f) == af.span
                    and self._df_unique_call_target(
                        sym_row["id"], af.line, af.callee, af.col)
                )
                next_depth = d if free_forwarder else d + 1
                if (callee and next_depth <= depth and af.arg_index >= 0
                        and df.supported(callee["language"])):
                    key = (callee["dst"], af.arg_index)
                    if key not in visited:
                        visited.add(key)
                        crow = self._crow(callee["dst"])
                        cf, _ = self._df_facts(crow, cache) if crow else (None, None)
                        if cf and af.arg_index < len(cf.params):
                            sinks.extend(trace(crow, {cf.params[af.arg_index]},
                                               next_depth, visited))
            for receiver_flow in flow.receiver_flows:
                if d >= depth:
                    continue
                for callee in self._df_resolve_same_this_calls(
                        sym_row, receiver_flow):
                    crow = self._crow(callee["dst"])
                    cf, _ = (self._df_facts(crow, cache)
                             if crow else (None, None))
                    if cf is None:
                        continue
                    fields = receiver_flow.fields & cf.instance_fields
                    if not fields:
                        continue
                    receiver_visit_key = (
                        callee["dst"], "this", tuple(sorted(fields)))
                    if receiver_visit_key in visited:
                        continue
                    visited.add(receiver_visit_key)
                    field_seeds: set[tuple[str, ...]] = {
                        ("this", field) for field in fields
                    }
                    field_seeds |= {
                        (field,) for field in fields
                        if field not in cf.local_names
                    }
                    sinks.extend(trace(crow, field_seeds, d + 1, visited))
            return sinks

        result_params = []
        for i, p in enumerate(facts.params):
            same_this = receiver_effects(sym, facts) if lang == "java" else None
            flow = df.analyze(facts, {p}, lang=lang,
                              receiver_effects=same_this)
            sinks = trace(sym, {p}, 1, {(sym["id"], i)})
            result_params.append({
                "name": p, "reaches_return": flow.reaches_return, "sinks": sinks})
        env.warn("dataflow: intra-procedural may-taint (flow-insensitive, "
                 "over-aproxima) + call graph.")
        return {"function": sym, "supported": True, "params": result_params}, env

    def taint(self, scope: str | None = None, entry: str | None = None,
              depth: int | None = None, max_findings: int = 100,
              deadline_ms: int | None = None, max_steps: int | None = None,
              should_cancel=None):
        """Análise de taint fonte→sink: input não-confiável alcançando operação
        perigosa. Sanitizers cortam o fluxo. Interprocedural via call graph.

        Dois modos: varredura do repo (fontes = chamadas a `sources`), ou
        `entry=func` (assume os parâmetros de `func` como não-confiáveis).
        Regras em .codegraph/taint.json. Ver docs/RESEARCH.md §6.

        Anti-explosão (o custo é O(fontes × ramif^depth) — ver docs/RESEARCH.md):
        - `depth=None` usa o default por modo (entry fundo, scan raso);
        - `deadline_ms`/`max_steps` limitam tempo/passos e devolvem PARCIAL;
        - `should_cancel()` é o hook cooperativo do host (abortar sem thread-zumbi);
        - memoização GLOBAL `explored` colapsa subárvores compartilhadas para a
          mesma proveniência; fontes distintas preservam explicações próprias.
          Qualquer corte marca `env.truncated=True` e devolve `limit_hit`.
        """
        from . import dataflow as df
        from .taint_rules import load_rules

        if depth is None:
            depth = TAINT_DEPTH_ENTRY if entry else TAINT_DEPTH_SCAN
        env = Envelope()
        # linguagens presentes no índice → catálogo de framework só delas
        langs = {r["language"] for r in self.conn.execute(
            "SELECT DISTINCT language FROM files WHERE language IS NOT NULL")}
        rules = load_rules(self.root, langs)
        cache: dict = {}
        findings: list = []
        order = {"certain": 2, "inferred": 1, "possible": 0}
        budget = _Budget(deadline_ms, max_steps, should_cancel)
        # Memo global por subtree *e proveniência*. Duas rotas da mesma fonte
        # continuam colapsadas; fontes distintas que convergem no mesmo helper
        # precisam atravessá-lo separadamente para publicar origens corretas.
        explored: set = set()
        # fontes EFETIVAS p/ o motor flow-sensitive: ele precisa saber que
        # `x = fonte()` GERA sujeira no ponto do programa (senão mataria a
        # própria semente). Na varredura passa a incluir os wrappers.
        eff_src: set = set(rules.sources)
        profiles = ("xss", "non-xss") if "java" in langs else ("all",)
        analysis_stats = {"summary_flow_runs": 0}
        return_dependencies: dict[str, dict[str, frozenset[int]]] = {
            profile: {} for profile in profiles
        }
        nonprop_cache: dict[str, dict] = {profile: {} for profile in profiles}
        src_func_fqns: dict[str, set[str]] = {
            profile: set() for profile in profiles
        }
        receiver_summary_cache: dict = {}
        receiver_summary_building: set = set()

        def profile_sanitizers(profile, language):
            if profile == "all":
                return rules.sanitizers
            return rules.sanitizers_for_context(language, profile)

        def nested_java_facts(facts):
            """Yield a Java callable and its deferred lambda flow units."""
            pending = [facts]
            seen = set()
            while pending:
                current = pending.pop()
                identity = id(current)
                if identity in seen:
                    continue
                seen.add(identity)
                yield current
                pending.extend(
                    unit.facts for unit in getattr(current, "lambda_units", ())
                )

        def scan_profiles(collected):
            """Use contextual Java passes only when their semantic delta occurs.

            The XSS and non-XSS sanitizer sets differ only for contextual HTML
            encoders.  With no such call in the scanned facts, one ``all`` pass
            is exactly equivalent to both.  If encoders exist but no XSS sink
            does, only the conservative non-XSS pass can produce a finding.
            """
            if "java" not in langs:
                return ("all",)
            # A scoped scan may recurse into callees outside the selected rows.
            # Without a closed-world proof over that transitive closure, keep
            # both Java contexts rather than infer absence from an incomplete
            # sample. Full-repository scans (the benchmark/default) are closed.
            if scope:
                return ("xss", "non-xss")
            xss_sanitizers = rules.sanitizers_for_context("java", "xss")
            non_xss_sanitizers = rules.sanitizers_for_context(
                "java", "non-xss")
            contextual = xss_sanitizers - non_xss_sanitizers
            if not contextual:
                return ("all",)
            contextual_seen = False
            xss_sink_seen = False
            for _row, facts, language in collected:
                if language != "java":
                    continue
                for unit_facts in nested_java_facts(facts):
                    for call in unit_facts.calls:
                        names = {call.callee, call.qualified}
                        if names & contextual:
                            contextual_seen = True
                        if (rules.sink_context(
                                call.callee, call.qualified, "java") == "xss"
                                and any(
                                    rules.is_sink(
                                        call.callee, call.qualified, index,
                                        language="java",
                                        receiver_type=call.receiver_type,
                                    )
                                    for index, _paths in call.args
                                )):
                            xss_sink_seen = True
                        if contextual_seen and xss_sink_seen:
                            return ("xss", "non-xss")
            if not contextual_seen:
                return ("all",)
            return ("non-xss",)

        def conf_min(a, b):
            if a is None:
                return b
            if b is None:
                return a
            return a if order[a] <= order[b] else b

        def span_json(span):
            if span is None:
                return None
            return {"start": span[0], "end": span[1]}

        def provenance_identity(value):
            """Forma hashable e estável da origem publicada no finding."""
            if isinstance(value, dict):
                return tuple(sorted(
                    (str(key), provenance_identity(item))
                    for key, item in value.items()
                ))
            if isinstance(value, (list, tuple)):
                return tuple(provenance_identity(item) for item in value)
            if isinstance(value, set):
                return tuple(sorted(provenance_identity(item)
                                    for item in value))
            try:
                hash(value)
            except TypeError:
                return repr(value)
            return value

        def source_origin(sym_row, evidence):
            origin = {
                "kind": "source", "func_fqn": sym_row["fqn"],
                "path": sym_row["path"], "line": evidence.line,
                "what": evidence.label + "()",
            }
            if evidence.col is not None:
                origin["column"] = evidence.col
            if evidence.span is not None:
                origin["byte_span"] = span_json(evidence.span)
            published = rules.published_source_arguments(
                evidence.label, evidence.arg_literals)
            if published:
                origin["argument_literals"] = dict(published)
                origin["parameter"] = published[0][1]
            return origin

        def java_receiver_effects(sym_row, facts, profile):
            """Exact-call effects for literal/implicit ``this`` receivers."""
            sanitizers = profile_sanitizers(profile, "java")
            effects = {}
            for call in facts.calls:
                if (call.span is None
                        or call.receiver_kind not in {
                            "implicit_this", "explicit_this"}):
                    continue
                callees = self._df_resolve_same_this_calls(sym_row, call)
                if not callees:
                    continue
                candidates = []
                for callee in callees:
                    summary_key = (profile, callee["dst"])
                    if summary_key not in receiver_summary_cache:
                        if summary_key in receiver_summary_building:
                            continue
                        receiver_summary_building.add(summary_key)
                        try:
                            crow = self._crow(callee["dst"])
                            cf, clang = (self._df_facts(crow, cache)
                                         if crow else (None, None))
                            if cf is None or clang != "java":
                                receiver_summary_cache[summary_key] = None
                            else:
                                nested = java_receiver_effects(crow, cf, profile)
                                same_receiver_calls = [
                                    site for site in cf.calls
                                    if site.receiver_kind in {
                                        "implicit_this", "explicit_this"}
                                ]
                                callee_nonprop = self._nonprop_spans(
                                    crow, cf, return_dependencies[profile],
                                    nonprop_cache[profile])
                                callee_sources = self._source_wrapper_spans(
                                    crow, cf, src_func_fqns[profile])
                                receiver_summary_cache[summary_key] = (
                                    df.summarize_java_receiver_effect(
                                        cf, sanitizers, eff_src,
                                        callee_nonprop, callee_sources, nested,
                                        allow_overwrites=(
                                            callee["closed_dispatch"]
                                            and not df.java_receiver_may_escape(cf)
                                            and all(site.span in nested
                                                    for site in
                                                    same_receiver_calls)),
                                        trusted_source_literals=(
                                            rules.trusted_source_literals)))
                        finally:
                            receiver_summary_building.discard(summary_key)
                    effect = receiver_summary_cache.get(summary_key)
                    if effect is not None:
                        candidates.append(effect)
                if candidates:
                    if len(candidates) != len(callees):
                        candidates.append(df.ReceiverEffect())
                    effects[call.span] = df.merge_receiver_effects(candidates)
            return effects

        def trace(sym_row, tainted, origin, steps, d, visited, path_conf,
                  path_flow="flow-sensitive", seed_map=None,
                  source_spans=frozenset(), profile="all"):
            if budget.hit():
                return
            budget.tick()
            f, flang = self._df_facts(sym_row, cache)
            if f is None:
                return
            # EIXO 2: evidência do FLUXO, independente da resolução da chamada.
            # Degrada pelo elo mais fraco, como a confiança: basta uma função do
            # caminho ter rodado no motor que over-aproxima.
            if not df.uses_flow_sensitive(f, flang):
                path_flow = "over-approximated"
            sanitizers = profile_sanitizers(profile, flang)
            if flang == "java":
                source_spans = (set(source_spans)
                                | set(df.java_properties_source_spans(f)))
            nonprop_spans = self._nonprop_spans(
                sym_row, f, return_dependencies[profile],
                nonprop_cache[profile])
            receiver_effects = (java_receiver_effects(sym_row, f, profile)
                                if flang == "java" else None)
            flow = df.analyze(f, tainted, sanitizers, lang=flang,
                              sources=eff_src, nonprop=nonprop_spans,
                              source_spans=source_spans,
                              receiver_effects=receiver_effects,
                              trusted_source_literals=(
                                  rules.trusted_source_literals))
            if flang == "java":
                collection_flow = df.analyze_java_constant_collections(
                    f, tainted, sanitizers, sources=eff_src,
                    nonprop=nonprop_spans, source_spans=source_spans,
                    allow_unrelated_calls=True,
                    receiver_effects=receiver_effects,
                    trusted_source_literals=rules.trusted_source_literals)
                if collection_flow is not None:
                    flow = collection_flow
            for af in flow.arg_flows:
                if budget.hit():
                    return
                callee = self._df_resolve_call(
                    sym_row["id"], af.line, af.callee, af.col)
                if flang == "java":
                    callee = (self._df_java_exact_receiver_call(f, af)
                              or callee)
                step = {
                    "func_fqn": sym_row["fqn"], "callee": af.callee,
                    "callee_fqn": callee["fqn"] if callee else None,
                    "site_path": sym_row["path"], "line": af.line,
                    "column": af.col, "byte_span": span_json(af.span),
                    "arg_index": af.arg_index, "via": af.via,
                    "via_candidates": af.via_candidates,
                    "confidence": callee["confidence"] if callee else None,
                    "resolved": callee is not None,
                }
                cur_conf = conf_min(path_conf, step["confidence"]) if callee else path_conf
                # leitura escrita DENTRO do argumento (`eval(req.body.x)`): a
                # origem é aqui mesmo. Herdar a origem da função inteira daria
                # um achado verdadeiro com uma explicação inventada.
                here = origin
                if af.source_evidence is not None:
                    here = source_origin(sym_row, af.source_evidence)
                elif af.source is not None:
                    here = {
                        "kind": "source", "func_fqn": sym_row["fqn"],
                        "path": sym_row["path"], "line": af.line,
                        "what": af.source,
                    }
                    if af.col is not None:
                        here["column"] = af.col
                    if af.span is not None:
                        here["byte_span"] = span_json(af.span)
                elif seed_map is not None and (
                        matching_seed := next((
                            candidate for candidate in af.via_candidates
                            if candidate in seed_map
                        ), af.via if af.via in seed_map else None)
                ) is not None:
                    # a função pode ter VÁRIAS fontes; atribuir a todos os
                    # achados a primeira delas dá um achado certo com uma
                    # origem errada. Quando a variável que chega ao sink é ela
                    # própria semente, a origem é a linha DELA.
                    here = source_origin(sym_row, seed_map[matching_seed])
                # casa pelo nome simples OU pelo qualificado receptor.método:
                # `getWriter.println` é sink de XSS, `out.println` não é.
                sink_identity = af.qualified
                if flang == "python":
                    sink_identity = (self._df_call_identity(
                        sym_row["id"], af.line, af.callee, af.col)
                        or af.qualified)
                sink_context = rules.sink_context(
                    af.callee, sink_identity, flang)
                profile_matches = (
                    profile == "all" or sink_context == profile
                    or (profile == "non-xss" and sink_context is None)
                )
                if (profile_matches
                        and rules.is_sink(
                            af.callee, sink_identity, af.arg_index,
                            language=flang,
                            receiver_type=af.receiver_type)):
                    if len(findings) < max_findings:
                        findings.append({
                            "origin": here,
                            "sink": {"callee": af.callee, "callee_fqn": step["callee_fqn"],
                                     "qualified": sink_identity,
                                     "site_path": sym_row["path"], "line": af.line,
                                     "column": af.col,
                                     "byte_span": span_json(af.span),
                                     "arg_index": af.arg_index, "via": af.via,
                                     "via_candidates": af.via_candidates,
                                     "func_fqn": sym_row["fqn"]},
                            "confidence": cur_conf or "possible",
                            "flow_evidence": path_flow,
                            "steps": steps + [step],
                        })
                    else:
                        budget.note("findings")     # havia mais achados que o teto
                free_forwarder = (
                    flang == "java"
                    and df.java_transparent_forwarder_span(f) == af.span
                    and self._df_unique_call_target(
                        sym_row["id"], af.line, af.callee, af.col)
                )
                next_depth = d if free_forwarder else d + 1
                if (callee and next_depth <= depth and af.arg_index >= 0
                        and df.supported(callee["language"])):
                    key = (
                        profile, sym_row["id"], af.line, af.col, af.span,
                        callee["dst"], af.arg_index,
                        provenance_identity(here),
                    )
                    # per-path `visited` corta ciclos; `explored` colapsa a
                    # subárvore somente para a mesma proveniência de fonte.
                    if key not in visited and key not in explored:
                        visited.add(key)
                        explored.add(key)
                        crow = self._crow(callee["dst"])
                        cf, _ = self._df_facts(crow, cache) if crow else (None, None)
                        if cf and af.arg_index < len(cf.params):
                            trace(crow, {cf.params[af.arg_index]}, here,
                                  steps + [step], next_depth, visited, cur_conf,
                                  path_flow, profile=profile)
            for receiver_flow in flow.receiver_flows:
                if budget.hit() or d >= depth:
                    return
                for callee in self._df_resolve_same_this_calls(
                        sym_row, receiver_flow):
                    crow = self._crow(callee["dst"])
                    cf, _ = (self._df_facts(crow, cache)
                             if crow else (None, None))
                    if cf is None:
                        continue
                    fields = frozenset(
                        receiver_flow.fields & cf.instance_fields)
                    if not fields:
                        continue
                    receiver_visit_key = (
                        profile, sym_row["id"], receiver_flow.line,
                        receiver_flow.col, receiver_flow.span, callee["dst"],
                        "this", tuple(sorted(fields)),
                        provenance_identity(origin))
                    if (receiver_visit_key in visited
                            or receiver_visit_key in explored):
                        continue
                    field_seeds: set[tuple[str, ...]] = {
                        ("this", field) for field in fields
                    }
                    field_seeds |= {
                        (field,) for field in fields
                        if field not in cf.local_names
                    }
                    step = {
                        "func_fqn": sym_row["fqn"],
                        "callee": receiver_flow.callee,
                        "callee_fqn": callee["fqn"],
                        "site_path": sym_row["path"],
                        "line": receiver_flow.line,
                        "column": receiver_flow.col,
                        "byte_span": span_json(receiver_flow.span),
                        "arg_index": -2,
                        "via": ", ".join(f"this.{field}"
                                         for field in sorted(fields)),
                        "confidence": callee["confidence"],
                        "resolved": True,
                    }
                    cur_conf = conf_min(path_conf, callee["confidence"])
                    visited.add(receiver_visit_key)
                    explored.add(receiver_visit_key)
                    trace(crow, field_seeds, origin, steps + [step], d + 1,
                          visited, cur_conf, path_flow, profile=profile)
            for static_flow in flow.static_flows:
                if budget.hit() or d >= depth:
                    return
                callee = self._df_resolve_call(
                    sym_row["id"], static_flow.line, static_flow.callee,
                    static_flow.col)
                if callee is None or not df.supported(callee["language"]):
                    continue
                crow = self._crow(callee["dst"])
                cf, _ = (self._df_facts(crow, cache)
                         if crow else (None, None))
                if cf is None:
                    continue
                field_seeds: set[tuple[str, ...]] = set()
                candidate_paths = set().union(*(
                    [assignment.rhs_ids for assignment in cf.assigns]
                    + [returned.ids for returned in cf.returns]
                    + [paths for call in cf.calls
                       for _index, paths in call.args]
                )) if (cf.assigns or cf.returns or cf.calls) else set()
                for field_name in static_flow.fields:
                    field_seeds.add((field_name,))
                    field_seeds |= {
                        path for path in candidate_paths
                        if path and path[-1] == field_name
                    }
                if not field_seeds:
                    continue
                visit_key = (
                    profile, sym_row["id"], static_flow.line,
                    static_flow.col, static_flow.span, callee["dst"], "static",
                    tuple(sorted(static_flow.fields)),
                    provenance_identity(origin),
                )
                if visit_key in visited or visit_key in explored:
                    continue
                step = {
                    "func_fqn": sym_row["fqn"],
                    "callee": static_flow.callee,
                    "callee_fqn": callee["fqn"],
                    "site_path": sym_row["path"],
                    "line": static_flow.line,
                    "column": static_flow.col,
                    "byte_span": span_json(static_flow.span),
                    "arg_index": -3,
                    "via": ", ".join(sorted(static_flow.fields)),
                    "confidence": callee["confidence"],
                    "resolved": True,
                }
                cur_conf = conf_min(path_conf, callee["confidence"])
                visited.add(visit_key)
                explored.add(visit_key)
                trace(crow, field_seeds, origin, steps + [step], d + 1,
                      visited, cur_conf, path_flow, profile=profile)

        if entry:
            sym = self._resolve_fresh(entry, env)
            self._repair({sym["path"]}, env)
            sym = self._resolve_selector(sym["fqn"])
            f, lang = self._df_facts(sym, cache)
            if f is None:
                env.warn(f"taint: '{entry}' em linguagem '{lang}' sem análise de fluxo.")
                return {"mode": "entry", "findings": [], "scanned": 0,
                        "elapsed_ms": budget.elapsed_ms(), "explored": 0,
                        "steps": budget.steps, "limit_hit": None}, env
            origin = {"kind": "param", "func_fqn": sym["fqn"], "path": sym["path"],
                      "line": sym["start_line"],
                      "what": "parâmetros (assumidos não-confiáveis)"}
            for profile in profiles:
                trace(sym, set(f.params), origin, [], 1,
                      {(profile, sym["id"], -1)}, None, profile=profile)
            scanned = 1
        else:
            self._repair_all(env)
            where, args = "", []
            if scope:
                where = " AND f.path LIKE ? ESCAPE '\\'"
                args = [like_escape(scope.rstrip("/").replace("\\", "/")) + "%"]
            rows = self.conn.execute(
                f"SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id=f.id "
                f"WHERE s.kind IN ('function','method','file'){where}", args).fetchall()
            # 1ª passada: funções que RETORNAM dado de fonte viram elas próprias
            # fontes (pega o idioma comum do wrapper `x = get_input()`)
            collected: list = []
            for r in rows:
                if budget.hit():
                    break
                f, flang = self._df_facts(dict(r), cache)
                if f is None:
                    continue
                collected.append((dict(r), f, flang))
            profiles = scan_profiles(collected)
            return_dependencies = {profile: {} for profile in profiles}
            nonprop_cache = {profile: {} for profile in profiles}
            src_func_fqns = {profile: set() for profile in profiles}
            receiver_summary_cache.clear()
            receiver_summary_building.clear()

            for r, f, flang in collected:
                if budget.hit():
                    break
                row_profiles = (profiles if flang == "java"
                                else (profiles[-1],))
                for profile in row_profiles:
                    sanitizers = profile_sanitizers(profile, flang)
                    property_spans = (df.java_properties_source_spans(f)
                                      if flang == "java" else frozenset())
                    direct = any(df.return_reads_named_source(
                        rt, rules.sources, sanitizers,
                        rules.trusted_source_literals) for rt in f.returns)
                    seed = df.source_vars(
                        f, rules.sources, sanitizers, property_spans,
                        trusted_source_literals=rules.trusted_source_literals)
                    if direct or (seed and df.analyze(
                            f, seed, sanitizers, lang=flang, sources=eff_src,
                            source_spans=property_spans,
                            trusted_source_literals=(
                                rules.trusted_source_literals)).reaches_return):
                        src_func_fqns[profile].add(r["fqn"])

                    # Nothing below can produce a callable RHS summary without
                    # both formal parameters and an observable return.  Source
                    # wrappers are deliberately excluded from non-propagation.
                    if (not f.params or not f.returns
                            or r["fqn"] in src_func_fqns[profile]):
                        continue

                    # SUMÁRIO PARAMÉTRICO DE RETORNO. Primeiro preservamos
                    # as provas maduras do CFG/folding e do domínio fechado de
                    # coleções. O slice paramétrico é apenas fallback para o
                    # caso que elas recusam (por exemplo, trabalho morto seguido
                    # de chamada virtual sobre receiver/argumentos limpos).
                    sanitized_return = df.proves_sanitized_return(
                        f, sanitizers)
                    dependencies: frozenset[int] | None = None
                    if flang == "java":
                        structural_dependencies = (
                            frozenset() if sanitized_return else
                            df.java_return_param_dependencies(f, sanitizers))
                        pure_constant_calls = {
                            "charAt", "length", "toUpperCase", "toLowerCase",
                            "upper", "lower", "trim", "strip",
                        }
                        summary_safe = (sanitized_return or all(
                            call.callee in pure_constant_calls
                            for call in f.calls))
                        summary_flow = None
                        param_flow = None
                        if not summary_safe:
                            analysis_stats["summary_flow_runs"] += 1
                            param_flow = df.analyze(
                                f, set(f.params), sanitizers, lang=flang,
                                trusted_source_literals=(
                                    rules.trusted_source_literals))
                            if (param_flow.proven_sanitized_return
                                    and not param_flow.reaches_return):
                                summary_flow = param_flow
                                summary_safe = True
                            else:
                                summary_flow = (
                                    df.analyze_java_constant_collections(
                                        f, set(f.params), sanitizers,
                                        trusted_source_literals=(
                                            rules.trusted_source_literals)))
                                summary_safe = summary_flow is not None
                        effective_flow = summary_flow or param_flow
                        if effective_flow is None:
                            analysis_stats["summary_flow_runs"] += 1
                            effective_flow = df.analyze(
                                f, set(f.params), sanitizers, lang=flang,
                                trusted_source_literals=(
                                    rules.trusted_source_literals))
                        reaches = effective_flow.reaches_return
                        if (summary_flow is not None and not reaches):
                            # O domínio fechado já provou separadamente que
                            # as mutações da coleção não escapam.
                            dependencies = frozenset()
                        elif (structural_dependencies is not None
                              and summary_safe and not reaches):
                            dependencies = frozenset()
                        else:
                            dependencies = structural_dependencies
                    else:
                        analysis_stats["summary_flow_runs"] += 1
                        reaches = df.analyze(
                                    f, set(f.params), sanitizers, lang=flang,
                                    trusted_source_literals=(
                                        rules.trusted_source_literals)
                                ).reaches_return
                        if sanitized_return or not reaches:
                            dependencies = frozenset()
                    if (dependencies is not None
                            and all(index < len(f.params)
                                    for index in dependencies)):
                        return_dependencies[profile][r["id"]] = dependencies
            scanned = 0
            for r, f, flang in collected:
                if budget.hit():
                    break
                scanned += 1
                row_profiles = (profiles if flang == "java"
                                else (profiles[-1],))
                for profile in row_profiles:
                    sanitizers = profile_sanitizers(profile, flang)
                    wrapper_spans = self._source_wrapper_spans(
                        r, f, src_func_fqns[profile])
                    if flang == "java":
                        wrapper_spans = (set(wrapper_spans)
                                         | set(df.java_properties_source_spans(f)))
                    seeds = df.source_site_evidence(
                        f, rules.sources, sanitizers, wrapper_spans,
                        rules.trusted_source_literals)
                    direto = next((
                        evidence for call in f.calls
                        for _, evidence in df.direct_source_evidence(
                            call, eff_src, sanitizers,
                            rules.trusted_source_literals)
                    ), None)
                    heap_source = None
                    if flang == "java":
                        effects = java_receiver_effects(r, f, profile)
                        heap_source = next((
                            df.SourceEvidence(
                                call.callee, call.line, call.col, call.span)
                            for call in f.calls
                            if call.span in effects
                            and effects[call.span].always_dirty
                        ), None)
                    if not seeds and direto is None and heap_source is None:
                        continue
                    names = {site.path for site in seeds}
                    if seeds:
                        origin = source_origin(r, seeds[0].evidence)
                    elif direto is not None:
                        origin = source_origin(r, direto)
                    else:
                        assert heap_source is not None
                        origin = source_origin(r, heap_source)
                    seed_map = {
                        ".".join(site.path): site.evidence
                        for site in seeds
                    }
                    initial = set() if f.regions is not None else names
                    trace(
                        r, initial, origin, [], 1,
                        {(profile, r["id"], -2)}, None,
                        seed_map=seed_map, source_spans=wrapper_spans,
                        profile=profile,
                    )
                    if len(findings) >= max_findings:
                        budget.note("findings")
                        break
                if len(findings) >= max_findings:
                    break

        # Um mesmo par (origem, sink) pode ser alcançado por mais de um caminho —
        # inclusive pela função chamando a si mesma, quando o resolvedor liga
        # `res.redirect` à função `redirect` exportada pelo próprio módulo.
        # Reportar o mesmo defeito duas vezes não acrescenta informação e faz o
        # relatório parecer maior do que é. Fica a versão mais CONFIÁVEL e, em
        # empate, a de cadeia mais curta — a explicação mais direta de conferir.
        unicos: dict = {}
        for f in findings:
            origin_span = f["origin"].get("byte_span") or {}
            sink_span = f["sink"].get("byte_span") or {}
            arguments = f["origin"].get("argument_literals") or {}
            k = (
                f["origin"]["path"], f["origin"]["line"],
                f["origin"].get("column"), origin_span.get("start"),
                origin_span.get("end"), tuple(sorted(arguments.items())),
                f["sink"]["site_path"], f["sink"]["line"],
                f["sink"].get("column"), sink_span.get("start"),
                sink_span.get("end"), f["sink"]["callee"],
                f["sink"]["arg_index"], f["sink"].get("via"),
                tuple(f["sink"].get("via_candidates") or ()),
            )
            atual = unicos.get(k)
            if atual is None or (order[f["confidence"]], -len(f["steps"])) > (
                    order[atual["confidence"]], -len(atual["steps"])):
                unicos[k] = f
        findings = list(unicos.values())
        # Fixture de teste é código de verdade e não some do relatório — mas vai
        # para o fim. Medido: varrer o Express dá 73 achados, dos quais 68 são
        # `res.send(req.params.id)` na SUÍTE DELE, que existe justamente para
        # ecoar a requisição. Deixá-los no meio afoga os 5 do código de
        # produção, e um relatório que afoga o próprio sinal não é usado.
        for f in findings:
            f["in_test"] = _is_test_path(f["sink"]["site_path"])
        findings.sort(key=lambda x: (x["in_test"], -order[x["confidence"]]))
        if budget.limit_hit:
            env.truncated = True
            env.warn(f"truncated: análise parada em '{budget.limit_hit}' — "
                     f"resultado PARCIAL. Estreite com entry=<func>/scope, ou "
                     f"aumente deadline_ms/max_steps/max_findings.")
        env.warn("taint: may-taint estático (over-aproxima) — achados são "
                 "candidatos a verificar; ajuste regras em .codegraph/taint.json.")
        return {"mode": "entry" if entry else "scan",
                "findings": findings, "scanned": scanned,
                "elapsed_ms": budget.elapsed_ms(), "explored": len(explored),
                "steps": budget.steps, "limit_hit": budget.limit_hit,
                "analysis": {
                    "profiles": list(profiles),
                    "summary_flow_runs": analysis_stats["summary_flow_runs"],
                }}, env

    def visualize(self, mode: str | None = None, *, level: str | None = None,
                  scope: str | None = None, top: int = 250,
                  symbol: str | None = None, depth: int = 3,
                  min_confidence: str | None = None, language: str | None = None,
                  changed: str | None = None, git: bool = False,
                  git_ref: str | None = None,
                  staged: bool = False) -> tuple[dict, Envelope]:
        """Monta os dados de um subgrafo de INVESTIGAÇÃO para export visual.

        `mode` (ou o legado `level`): `file`/`symbol` (mapa), `neighborhood`,
        `callers`, `callees`, `impact` (semeados por `symbol` ou pelos arquivos
        alterados), `domains` (grafo entre comunidades). Filtros: `min_confidence`
        (certain/inferred/possible), `language`, e o conjunto de arquivos
        alterados via `changed` (paths/diff) ou `git_ref`/`staged` (git diff).

        Fresco como qualquer consulta: repara o índice, garante ranks/domínios.
        """
        from .viz import build_graph_data, git_changed_files

        env = Envelope()
        self._repair_all(env)
        ensure_ranks(self.conn)
        ensure_communities(self.conn)
        mode = mode or level or "file"

        seed_ids: list = []
        seed_label = None
        if symbol:
            sym = self._resolve_fresh(symbol, env)
            if self._repair({sym["path"]}, env):
                sym = self._resolve_selector(sym["fqn"])
            seed_ids = [sym["id"]]
            seed_label = sym["fqn"]

        changed_files: set | None = None
        if changed:
            changed_files = set(_paths_from_target(changed))
        elif git or git_ref is not None or staged:
            changed_files = git_changed_files(self.root, ref=git_ref, staged=staged)

        # modos semeados sem símbolo explícito → semeia pelos arquivos alterados
        if mode in ("neighborhood", "callers", "callees", "impact") \
                and not seed_ids and changed_files:
            ph = ",".join("?" * len(changed_files))
            seed_ids = [r["id"] for r in self.conn.execute(
                f"SELECT s.id FROM symbols s JOIN files f ON s.file_id=f.id "
                f"WHERE f.path IN ({ph}) AND s.parent_id IS NULL "
                f"AND s.kind<>'file'", list(changed_files)).fetchall()]
            seed_label = f"{len(changed_files)} arquivo(s) alterado(s)"

        data = build_graph_data(
            self.conn, mode=mode, scope=scope, top=top, seed_ids=seed_ids,
            seed_label=seed_label, depth=depth, min_confidence=min_confidence,
            language=language, changed_files=changed_files)
        return data, env

    def describe(self, target: str, refresh: bool = False, llm=None):
        """Camada L3: descrição de comportamento (símbolo ou módulo/arquivo).

        Frescor verificado na leitura: código mudou → STALE declarado no
        envelope (refresh=True re-gera).

        `llm` injeta a credencial POR CHAMADA (callable `(system, user) -> str`
        ou a própria chave de API). Sem ele cai no provider do engine e, por
        último, no env. Num host multi-usuário, injetar evita a corrida de mexer
        em os.environ por requisição e mantém o custo atribuível.
        """
        from .l3 import Describer
        from .l3.provider import coerce_provider

        env = Envelope()
        describer = Describer(
            self.root, self.conn,
            provider=coerce_provider(llm) or self.l3_provider)
        norm = target.replace("\\", "/").strip("/")
        if norm.startswith("domain:"):
            ensure_communities(self.conn)
            ensure_ranks(self.conn)
            data = describer.describe_domain(int(norm.split(":", 1)[1]), refresh=refresh)
            data["target"] = norm
            return data, env
        frow = self.conn.execute(
            "SELECT * FROM files WHERE path=?", (norm,)).fetchone()
        if frow is not None:
            self._repair({norm}, env)
            frow = self.conn.execute(
                "SELECT * FROM files WHERE path=?", (norm,)).fetchone()
            data = describer.describe_module(dict(frow), refresh=refresh)
            data["target"] = norm
        else:
            sym = self._resolve_fresh(target, env)
            if self._repair({sym["path"]}, env):
                sym = self._resolve_selector(sym["fqn"])
            data = describer.describe_symbol(sym, refresh=refresh)
            data["target"] = sym["fqn"]
        if not data["fresh"]:
            env.warn("stale: o código mudou desde a geração desta descrição — "
                     "use refresh para re-gerar.")
        usage = getattr(describer._provider, "usage", None)
        if data.get("generated_now") and usage:
            data["usage"] = usage
        return data, env

    # -- tools de alto nível (orientadas a agentes) ---------------------------

    def _symbols_in_paths(self, paths: list[str], *, include_id: bool = False) -> list[dict]:
        """Símbolos de TOPO declarados nos arquivos dados (o que 'mudou')."""
        out: list[dict] = []
        for rel in paths:
            frow = self.conn.execute(
                "SELECT id FROM files WHERE path=?", (rel,)).fetchone()
            if frow is None:
                continue
            for s in self.conn.execute(
                "SELECT id, fqn, kind, start_line FROM symbols WHERE file_id=? "
                    "AND parent_id IS NULL AND kind<>'file' ORDER BY start_line",
                    (frow["id"],)):
                item = {"fqn": s["fqn"], "kind": s["kind"], "path": rel,
                        "start_line": s["start_line"]}
                if include_id:
                    item["id"] = s["id"]
                out.append(item)
        return out

    def change_impact(self, target: str, depth: int = 3):
        """Impacto de um conjunto de mudanças: dados PATHS ou um DIFF, quais
        símbolos declarados neles têm dependentes, e o fecho transitivo desses
        dependentes (o que revisar/re-testar). Orientado ao fluxo real do agente
        (trabalha a partir de um diff), não de um fqn."""
        env = Envelope()
        paths = _paths_from_target(target)
        ensure_ranks(self.conn)
        before = self._symbols_in_paths(paths, include_id=True)
        before_impacts = [self._impact_rows(c["id"], depth) for c in before]
        self._repair(set(paths), env)
        ensure_ranks(self.conn)
        after = self._symbols_in_paths(paths, include_id=True)
        changed_by_fqn = {
            item["fqn"]: {k: v for k, v in item.items() if k != "id"}
            for item in [*before, *after]
        }
        changed = list(changed_by_fqn.values())
        impacted: dict[str, dict] = {}
        for rows in before_impacts:
            for r in rows:
                cur = impacted.get(r["fqn"])
                if cur is None or r["depth"] < cur["depth"]:
                    impacted[r["fqn"]] = r
        for c in after:
            rows = self._impact_rows(c["id"], depth)
            for r in rows:
                cur = impacted.get(r["fqn"])
                if cur is None or r["depth"] < cur["depth"]:
                    impacted[r["fqn"]] = r
        result = sorted(impacted.values(),
                        key=lambda r: (r["depth"], -(r.get("rank") or 0)))
        env.dynamic_dispatch = True
        env.unresolved_edges = sum(1 for r in result
                                   if r.get("confidence") == "possible")
        data = {"changed_files": paths, "changed_symbols": changed,
                "impacted": result, "n_changed": len(changed),
                "n_impacted": len(result)}
        return data, env

    def find_affected_modules(self, target: str, depth: int = 3):
        """`change_impact` agregado por ARQUIVO: quais módulos são tocados por uma
        mudança e com que profundidade — visão de alto nível para o agente decidir
        o que abrir."""
        data, env = self.change_impact(target, depth=depth)
        by_file: dict[str, list] = {}
        for r in data["impacted"]:
            by_file.setdefault(r["path"], []).append(r)
        modules = []
        for path, rs in by_file.items():
            top = sorted(rs, key=lambda x: (x["depth"], -(x.get("rank") or 0)))
            modules.append({"path": path, "count": len(rs),
                            "min_depth": min(x["depth"] for x in rs),
                            "symbols": [x["fqn"] for x in top[:6]]})
        modules.sort(key=lambda m: (m["min_depth"], -m["count"]))
        return {"changed_files": data["changed_files"], "modules": modules,
                "n_modules": len(modules)}, env

    def find_related_tests(self, selector: str, depth: int = 3):
        """Testes que exercitam um símbolo: callers transitivos que moram em
        arquivos de teste (test_*, *_test, *Test, *Spec, tests/…). Heurística
        sobre o call graph — o que já cobre este símbolo hoje."""
        env = Envelope()
        sym, rows, cenv = self.callers(selector, depth=depth)
        env.fresh = cenv.fresh
        env.dynamic_dispatch = True
        tests, seen = [], set()
        for r in rows:
            p = r.get("site_path")
            if not p or not _is_test_path(p):
                continue
            key = (r.get("other_fqn"), p, r.get("line"))
            if key in seen:
                continue
            seen.add(key)
            tests.append({"test": r.get("other_fqn") or "<módulo>", "path": p,
                          "line": r.get("line"), "depth": r.get("depth"),
                          "confidence": r.get("confidence"),
                          "resolver": r.get("resolver")})
        tests.sort(key=lambda t: (t["depth"] or 0, t["path"]))
        env.unresolved_edges = sum(1 for t in tests
                                   if t.get("confidence") == "possible")
        return {"symbol": sym, "tests": tests, "n": len(tests)}, env

    def explain_symbol(self, selector: str):
        """Ficha rica de um símbolo para o agente decidir sem reler o código:
        assinatura/doc/span/contagens (symbol_info) + vizinhança imediata (top
        callers/callees com confiança) + domínio. Sem custo de LLM."""
        env = Envelope()
        info, ienv = self.symbol_info(selector)
        env.fresh = ienv.fresh
        env.dynamic_dispatch = True
        sym = info["symbol"]

        def _top(rows, n=5):
            out, seen = [], set()
            for r in rows:
                v = r.get("other_fqn")
                if v and v not in seen:
                    seen.add(v)
                    out.append({"fqn": v, "confidence": r.get("confidence"),
                                "resolver": r.get("resolver")})
                if len(out) >= n:
                    break
            return out

        _s1, callers, _ = self.callers(sym["fqn"], depth=1)
        _s2, callees, _ = self.callees(sym["fqn"], depth=1)
        data = {"symbol": sym, "children": info["children"],
                "counts": info["counts"], "domain": info["domain"],
                "callers": _top(callers), "callees": _top(callees)}
        return data, env

    def suggest_files_to_read(self, task: str, limit: int = 8):
        """Arquivos mais relevantes para uma TAREFA em linguagem natural: extrai
        termos, casa símbolos (find_symbol) e ranqueia os arquivos por importância
        no grafo (PageRank) + nº de casamentos. Ponto de partida para o agente."""
        env = Envelope()
        tokens = [_suggest_token(value) for value in re.findall(r"\w+", task)]
        tokens = [token for token in tokens
                  if len(token) >= 3 and not token.isdigit()
                  and token not in _SUGGEST_STOPWORDS]
        tokens = list(dict.fromkeys(tokens))[:8]
        score: dict[str, float] = {}
        why: dict[str, list] = {}
        for tok in tokens:
            rows, renv = self.find_symbol(tok, limit=8)
            env.fresh = env.fresh and renv.fresh
            for r in rows:
                p = r["path"]
                score[p] = score.get(p, 0.0) + (r["rank"] or 0.0) + 1.0
                why.setdefault(p, []).append(r["fqn"])
        files = [{"path": p, "score": round(sc, 4),
                  "matches": list(dict.fromkeys(why[p]))[:5]}
                 for p, sc in score.items()]
        files.sort(key=lambda f: -f["score"])
        if len(files) > limit:
            env.truncated = True
        return {"task": task, "tokens": tokens, "files": files[:limit]}, env

    # -- envelope de completeness (docs/DESIGN.md §3.1) -----------------------

    def _completeness(self, sym: dict, rows: list, env: Envelope) -> None:
        n_possible = sum(1 for r in rows if r["confidence"] == "possible")
        name = sym["name"]
        n_dangling = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE dst IS NULL AND kind='calls' "
            "AND (dst_name=? OR dst_name LIKE ? ESCAPE '\\')",
            (name, f"%.{like_escape(name)}")).fetchone()["c"]
        # espelho estruturado dos avisos (camada agent/MCP)
        env.dynamic_dispatch = True
        env.unresolved_edges = n_possible + n_dangling
        parts = ["completeness: estático — chamadas dinâmicas podem faltar"]
        if n_possible:
            parts.append(f"{n_possible} 'possible' (verificar)")
        if n_dangling:
            parts.append(f"{n_dangling} '{name}' não resolvidas")
        env.warn("; ".join(parts) + ".")

    # -- stats ----------------------------------------------------------------

    def stats(self) -> dict:
        g = lambda q: self.conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "files": g("SELECT COUNT(*) FROM files"),
            "symbols": g("SELECT COUNT(*) FROM symbols"),
            "edges": g("SELECT COUNT(*) FROM edges"),
            "edges_resolved": g("SELECT COUNT(*) FROM edges WHERE dst IS NOT NULL"),
            "edges_dangling": g("SELECT COUNT(*) FROM edges WHERE dst IS NULL"),
            "parse_partial": g("SELECT COUNT(*) FROM files WHERE parse_status!='ok'"),
            "by_language": {
                r["language"]: r["c"] for r in self.conn.execute(
                    "SELECT language, COUNT(*) c FROM files GROUP BY language")
            },
        }

    def doctor(self, failed_limit: int = 20) -> dict:
        """Diagnóstico de saúde do índice: parse, confiança das arestas,
        arquivos que falharam, resolvers L1 ativos e frescor (staleness).

        Read-only e barato — pensado para o usuário inspecionar o estado antes
        de confiar nas respostas, ou depois de um `index` com erros."""
        import time as _time

        g = lambda q: self.conn.execute(q).fetchone()[0]  # noqa: E731
        meta = {r["key"]: r["value"] for r in self.conn.execute(
            "SELECT key, value FROM meta")}
        try:
            l1_last_run = json.loads(meta.get("l1_last_run", "null"))
        except (TypeError, ValueError):
            l1_last_run = None
        conf = {r["confidence"] or "none": r["c"] for r in self.conn.execute(
            "SELECT confidence, COUNT(*) c FROM edges WHERE kind='calls' "
            "GROUP BY confidence")}
        parse = {r["parse_status"] or "unknown": r["c"] for r in self.conn.execute(
            "SELECT parse_status, COUNT(*) c FROM files GROUP BY parse_status")}
        failed = [r["path"] for r in self.conn.execute(
            "SELECT path FROM files WHERE parse_status='failed' ORDER BY path "
            "LIMIT ?", (failed_limit,)).fetchall()]
        failed_total = g("SELECT COUNT(*) FROM files WHERE parse_status='failed'")

        repo_langs = {r["language"] for r in self.conn.execute(
            "SELECT DISTINCT language FROM files")}
        try:
            from .l1 import available_resolvers, missing_resolvers
            resolvers = sorted({lang for cls in available_resolvers(self.root)
                                for lang in cls.languages})
            # degradação visível: linguagens do repo cujo LSP não está no PATH →
            # as arestas ficam em inferred/possible em vez de certain.
            l1_missing = missing_resolvers(repo_langs, root=self.root)
        except Exception:  # dependências L1 ausentes não devem quebrar o doctor
            resolvers = []
            l1_missing = []

        call_edges = sum(conf.values()) or 1
        last_scan = meta.get("last_full_scan")
        age = (int(_time.time()) - int(last_scan)) if last_scan else None
        return {
            # só o NOME do diretório: o caminho absoluto do servidor não é
            # informação do consumidor (todo host teria de removê-lo), e o
            # payload do MCP/API não deve expor o layout do disco.
            "root_name": self.root.name,
            "indexer_version": meta.get("indexer_version"),
            "files": g("SELECT COUNT(*) FROM files"),
            "symbols": g("SELECT COUNT(*) FROM symbols"),
            "parse": parse,
            "parse_failed_total": failed_total,
            "parse_failed_sample": failed,
            "call_edges": sum(conf.values()),
            "confidence": conf,
            "certain_pct": round(100 * conf.get("certain", 0) / call_edges, 1),
            "dangling": g("SELECT COUNT(*) FROM edges WHERE kind='calls' AND dst IS NULL"),
            "l1_resolvers": resolvers,
            "l1_missing": l1_missing,
            "l1_last_run": l1_last_run,
            "last_full_scan": int(last_scan) if last_scan else None,
            "last_full_scan_age_s": age,
            "by_language": {
                r["language"]: r["c"] for r in self.conn.execute(
                    "SELECT language, COUNT(*) c FROM files GROUP BY language "
                    "ORDER BY c DESC")
            },
        }
