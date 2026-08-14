"""Query engine com read-repair e envelope de frescor/completeness.

Garantia (docs/DESIGN.md §2.3): nenhuma resposta sai sem verificar o
content-hash dos arquivos envolvidos. Divergiu → re-indexa (ms) e re-executa
a consulta; arquivo sumiu → sai do índice; tudo anotado no envelope.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import explain
from .community import ensure_communities
from .indexer import (Indexer, get_index_excludes, get_index_scopes,
                      scan_source_stats)
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
    return sorted(p for p in paths if p)


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
        for rel in sorted(rels):
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
            if st.st_size == row["size"] and int(st.st_mtime) == row["mtime"]:
                continue  # fast-path: stat igual → assume fresco
            if self.ix.index_file(rel):
                env.warn(f"freshness: {rel} mudou desde a indexação; re-indexado agora (L0).")
                changed = True
        if changed:
            self.ix.resolve_edges()
            env.fresh = False           # havia drift (corrigido agora)
        return changed

    def _repair_all(self, env: Envelope) -> bool:
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
        if (w is not None and w.is_current()
                and (time.monotonic() - self._last_full_sweep) < self._sweep_backstop):
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

    def _resolve_selector(self, selector: str) -> dict:
        exact = self.conn.execute(
            "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id=f.id WHERE s.fqn=?",
            (selector,)).fetchall()
        if len(exact) == 1:
            return dict(exact[0])
        if len(exact) > 1:
            raise AmbiguousSymbol(selector, [dict(r) for r in exact])
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
        repaired = (self._repair({r["path"] for r in rows}, env) if rows
                    else self._repair_all(env))
        if repaired:
            rows = self._find_rows(query, kind, limit)
        if len(rows) >= limit:
            env.truncated = True
        self._warn_partial({r["path"] for r in rows}, env)
        return rows, env

    def _resolve_fresh(self, selector: str, env: Envelope) -> dict:
        """Resolve o seletor; se falhar, confere frescor do índice inteiro e tenta de novo."""
        try:
            return self._resolve_selector(selector)
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

    def impact(self, selector: str, depth: int = 3):
        """Fecho transitivo de dependentes: o que pode quebrar se eu mudar isto.

        Confiança do caminho = mínima entre as arestas percorridas.
        """
        env = Envelope()
        sym = self._resolve_fresh(selector, env)
        ensure_ranks(self.conn)

        def walk():
            results: list[dict] = []
            frontier: dict[str, str] = {sym["id"]: "certain"}
            seen = {sym["id"]}
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

        rows = walk()
        if self._repair({r["path"] for r in rows} | {sym["path"]}, env):
            try:
                sym = self._resolve_selector(sym["fqn"])
            except SymbolNotFound:
                env.warn(f"freshness: '{sym['fqn']}' não existe mais após re-indexação.")
                return sym, [], env
            rows = walk()
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
        ck = (sym_row["file_id"], row["content_hash"], sym_row["start_line"])
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
            fn = df.find_function_node(tree.root_node, sym_row["start_line"], lang)
        facts = df.extract_facts(data, fn, lang) if fn is not None else None
        fc[ck] = facts
        if len(fc) > self._facts_cache_cap:
            fc.popitem(last=False)             # despeja o mais antigo
        return facts, lang

    def _df_resolve_call(self, src_id, line, name=None):
        """Alvo da chamada nesta linha. `name` desempata quando há mais de uma:
        em `new Test().doSomething(x)` a linha tem duas arestas — a do `new`,
        para a classe, e a do método — e sem o filtro sai a errada."""
        extra = " AND s.name=?" if name else ""
        args = (src_id, line, name) if name else (src_id, line)
        rows = self.conn.execute(
            "SELECT e.dst, e.confidence, s.fqn, s.kind, s.start_line, "
            "f.path, f.language FROM edges e JOIN symbols s ON e.dst=s.id "
            "JOIN files f ON s.file_id=f.id WHERE e.src=? AND e.kind='calls' "
            f"AND e.line=? AND e.dst IS NOT NULL{extra} "
            "ORDER BY CASE e.confidence WHEN 'certain' THEN 0 "
            "WHEN 'inferred' THEN 1 ELSE 2 END LIMIT 1", args).fetchall()
        return dict(rows[0]) if rows else None

    def _nonprop_lines(self, sym_row, facts, nao_propaga_fqn, cache):
        """Linhas cuja atribuição chama função que NÃO devolve o argumento.

        Exige `confidence='certain'`, isto é, chamada resolvida SEMANTICAMENTE
        pela camada L1 (LSP). Esta é a diferença entre otimização e defeito:
        uma tentativa anterior resolvia por nome e apagou 109 vulnerabilidades
        reais do OWASP Benchmark, porque `doSomething` existe em centenas de
        arquivos com corpos diferentes. Sem L1 rodado, nada é morto — o motor
        volta a over-aproximar, que é o lado seguro."""
        if not nao_propaga_fqn:
            return frozenset()
        chave = sym_row["id"]
        if chave in cache:
            return cache[chave]
        out = set()
        for a in facts.assigns:
            if a.rhs_call is None:
                continue
            alvo = self._df_resolve_call(chave, a.line, a.rhs_call)
            if (alvo and alvo["confidence"] == "certain"
                    and alvo["fqn"] in nao_propaga_fqn):
                out.add(a.line)
        cache[chave] = frozenset(out)
        return cache[chave]

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
        facts, lang = self._df_facts(sym, cache)
        if facts is None:
            env.warn(f"dataflow: linguagem '{lang}' ainda sem análise de fluxo "
                     f"(suportadas: {', '.join(df.supported_langs())}).")
            return {"function": sym, "supported": False, "params": []}, env

        def trace(sym_row, tainted, d, visited):
            f, flang = self._df_facts(sym_row, cache)
            if f is None:
                return []
            flow = df.analyze(f, tainted, lang=flang)
            sinks = []
            for af in flow.arg_flows:
                callee = self._df_resolve_call(sym_row["id"], af.line)
                sinks.append({
                    "callee_name": af.callee, "arg_index": af.arg_index,
                    "line": af.line, "via": af.via, "depth": d,
                    "site_path": sym_row["path"], "resolved": callee is not None,
                    "callee_fqn": callee["fqn"] if callee else None,
                    "confidence": callee["confidence"] if callee else None,
                    "callee_path": callee["path"] if callee else None,
                    "callee_line": callee["start_line"] if callee else None,
                })
                if (callee and d < depth and af.arg_index >= 0
                        and df.supported(callee["language"])):
                    key = (callee["dst"], af.arg_index)
                    if key not in visited:
                        visited.add(key)
                        crow = self._crow(callee["dst"])
                        cf, _ = self._df_facts(crow, cache) if crow else (None, None)
                        if cf and af.arg_index < len(cf.params):
                            sinks.extend(trace(crow, {cf.params[af.arg_index]},
                                               d + 1, visited))
            return sinks

        result_params = []
        for i, p in enumerate(facts.params):
            flow = df.analyze(facts, {p}, lang=lang)
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
        - memoização GLOBAL `explored` colapsa subárvores compartilhadas entre
          fontes. Qualquer corte marca `env.truncated=True` e devolve `limit_hit`.
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
        explored: set = set()          # memo GLOBAL (func_id, arg_index) entre traces
        # fontes EFETIVAS p/ o motor flow-sensitive: ele precisa saber que
        # `x = fonte()` GERA sujeira no ponto do programa (senão mataria a
        # própria semente). Na varredura passa a incluir os wrappers.
        eff_src: set = set(rules.sources)
        nao_propaga_fqn: set = set()      # preenchido na 1ª passada da varredura
        nonprop_cache: dict = {}

        def conf_min(a, b):
            if a is None:
                return b
            if b is None:
                return a
            return a if order[a] <= order[b] else b

        def trace(sym_row, tainted, origin, steps, d, visited, path_conf,
                  path_flow="flow-sensitive", seed_map=None):
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
            nonprop_lines = self._nonprop_lines(
                sym_row, f, nao_propaga_fqn, nonprop_cache)
            flow = df.analyze(f, tainted, rules.sanitizers, lang=flang,
                              sources=eff_src, nonprop=nonprop_lines)
            if flang == "java":
                collection_flow = df.analyze_java_constant_collections(
                    f, tainted, rules.sanitizers, sources=eff_src,
                    nonprop=nonprop_lines, allow_unrelated_calls=True)
                if collection_flow is not None:
                    flow = collection_flow
            for af in flow.arg_flows:
                if budget.hit():
                    return
                callee = self._df_resolve_call(sym_row["id"], af.line)
                step = {
                    "func_fqn": sym_row["fqn"], "callee": af.callee,
                    "callee_fqn": callee["fqn"] if callee else None,
                    "site_path": sym_row["path"], "line": af.line,
                    "arg_index": af.arg_index, "via": af.via,
                    "confidence": callee["confidence"] if callee else None,
                    "resolved": callee is not None,
                }
                cur_conf = conf_min(path_conf, step["confidence"]) if callee else path_conf
                # leitura escrita DENTRO do argumento (`eval(req.body.x)`): a
                # origem é aqui mesmo. Herdar a origem da função inteira daria
                # um achado verdadeiro com uma explicação inventada.
                here = origin
                if af.source is not None:
                    here = {"kind": "source", "func_fqn": sym_row["fqn"],
                            "path": sym_row["path"], "line": af.line,
                            "what": af.source}
                elif seed_map is not None and af.via in seed_map:
                    # a função pode ter VÁRIAS fontes; atribuir a todos os
                    # achados a primeira delas dá um achado certo com uma
                    # origem errada. Quando a variável que chega ao sink é ela
                    # própria semente, a origem é a linha DELA.
                    ln, rotulo = seed_map[af.via]
                    here = {"kind": "source", "func_fqn": sym_row["fqn"],
                            "path": sym_row["path"], "line": ln,
                            "what": rotulo + "()"}
                # casa pelo nome simples OU pelo qualificado receptor.método:
                # `getWriter.println` é sink de XSS, `out.println` não é.
                if rules.is_sink(af.callee, af.qualified, af.arg_index):
                    if len(findings) < max_findings:
                        findings.append({
                            "origin": here,
                            "sink": {"callee": af.callee, "callee_fqn": step["callee_fqn"],
                                     "qualified": af.qualified,
                                     "site_path": sym_row["path"], "line": af.line,
                                     "arg_index": af.arg_index, "via": af.via,
                                     "func_fqn": sym_row["fqn"]},
                            "confidence": cur_conf or "possible",
                            "flow_evidence": path_flow,
                            "steps": steps + [step],
                        })
                    else:
                        budget.note("findings")     # havia mais achados que o teto
                if (callee and d < depth and af.arg_index >= 0
                        and df.supported(callee["language"])):
                    key = (callee["dst"], af.arg_index)
                    # per-path `visited` (ciclos) + `explored` GLOBAL (dedupe entre
                    # fontes): uma subárvore (func,arg) é expandida uma vez só.
                    if key not in visited and key not in explored:
                        visited.add(key)
                        explored.add(key)
                        crow = self._crow(callee["dst"])
                        cf, _ = self._df_facts(crow, cache) if crow else (None, None)
                        if cf and af.arg_index < len(cf.params):
                            trace(crow, {cf.params[af.arg_index]}, here,
                                  steps + [step], d + 1, visited, cur_conf,
                                  path_flow)

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
            trace(sym, set(f.params), origin, [], 1, {(sym["id"], -1)}, None)
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
            src_funcs: set[str] = set()
            for r in rows:
                if budget.hit():
                    break
                f, flang = self._df_facts(dict(r), cache)
                if f is None:
                    continue
                collected.append((dict(r), f))
                direct = any(rt.top_call in rules.sources for rt in f.returns)
                seed = df.source_vars(f, rules.sources, rules.sanitizers)
                if direct or (seed and df.analyze(
                        f, seed, lang=flang, sources=eff_src).reaches_return):
                    src_funcs.add(r["name"])
                # SUMÁRIO DE RETORNO: esta função devolve o que recebe?
                # Só funções COM parâmetros — `x = obj.metodo()` sem argumento
                # ainda pode devolver dado do RECEPTOR (`sb.toString()`), e
                # matá-lo apagaria fluxo real. Wrapper de fonte também fica de
                # fora: ele não propaga o argumento, mas devolve dado sujo.
                # Sem um `return` observável não há prova de que o alvo não
                # propaga. Em Java, JDTLS resolve despacho de interface para a
                # declaração abstrata (`String apply(String x);`): ela tem
                # parâmetros, mas nenhum corpo/fato. Classificá-la como
                # non-propagating apagou 142 TPs no OWASP Benchmark. Funções
                # realmente usadas no RHS e que retornam constante/sanitizado
                # possuem ao menos um ReturnFact e continuam elegíveis.
                summary_safe = True
                summary_flow = None
                if flang == "java":
                    # JDTLS resolves the call target, but arbitrary collection
                    # aliases, virtual dispatch and context-specific encoders
                    # are not return-flow proofs. The flow engine used to call
                    # all of those "non-propagating" and erased 145 real OWASP
                    # vulnerabilities. Accept call-free control flow, a tiny
                    # set of pure operations, or the separately checked local
                    # List add/remove/get domain with constant indices.
                    pure_constant_calls = {
                        "charAt", "length", "toUpperCase", "toLowerCase",
                        "upper", "lower", "trim", "strip",
                    }
                    summary_safe = all(c.callee in pure_constant_calls
                                       for c in f.calls)
                    if not summary_safe:
                        summary_flow = df.analyze_java_constant_collections(
                            f, set(f.params), rules.sanitizers)
                        summary_safe = summary_flow is not None
                if (summary_safe and f.params and f.returns
                        and r["name"] not in src_funcs
                        and not (summary_flow or df.analyze(
                            f, set(f.params), rules.sanitizers,
                            lang=flang)).reaches_return):
                    nao_propaga_fqn.add(r["fqn"])
            eff_sources = rules.sources | src_funcs
            eff_src |= src_funcs               # o motor flow-sensitive também vê
            scanned = 0
            for r, f in collected:
                if budget.hit():
                    break
                scanned += 1
                seeds = df.source_sites(f, eff_sources, rules.sanitizers)
                # a fonte também pode estar escrita DENTRO do argumento, sem
                # passar por variável — aí não há semente, mas há vulnerabilidade
                # (`eval(req.body.x)`). Sem esta linha a função nem seria varrida.
                direto = next(((c.line, s) for c in f.calls
                               for _, s in df.direct_source_args(c, rules.sanitizers)),
                              None)
                if not seeds and direto is None:
                    continue
                names = {n for n, _, _ in seeds}
                origin = ({"kind": "source", "func_fqn": r["fqn"], "path": r["path"],
                           "line": seeds[0][1], "what": seeds[0][2] + "()"} if seeds
                          else {"kind": "source", "func_fqn": r["fqn"],
                                "path": r["path"], "line": direto[0],
                                "what": direto[1]})
                seed_map = {".".join(p): (ln, rot) for p, ln, rot in seeds}
                trace(r, names, origin, [], 1, {(r["id"], -2)}, None,
                      seed_map=seed_map)
                if len(findings) >= max_findings:
                    budget.note("findings")
                    break

        # Um mesmo par (origem, sink) pode ser alcançado por mais de um caminho —
        # inclusive pela função chamando a si mesma, quando o resolvedor liga
        # `res.redirect` à função `redirect` exportada pelo próprio módulo.
        # Reportar o mesmo defeito duas vezes não acrescenta informação e faz o
        # relatório parecer maior do que é. Fica a versão mais CONFIÁVEL e, em
        # empate, a de cadeia mais curta — a explicação mais direta de conferir.
        unicos: dict = {}
        for f in findings:
            k = (f["origin"]["path"], f["origin"]["line"], f["sink"]["site_path"],
                 f["sink"]["line"], f["sink"]["callee"], f["sink"]["arg_index"])
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
                "steps": budget.steps, "limit_hit": budget.limit_hit}, env

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

    def _symbols_in_paths(self, paths: list[str]) -> list[dict]:
        """Símbolos de TOPO declarados nos arquivos dados (o que 'mudou')."""
        out: list[dict] = []
        for rel in paths:
            frow = self.conn.execute(
                "SELECT id FROM files WHERE path=?", (rel,)).fetchone()
            if frow is None:
                continue
            for s in self.conn.execute(
                    "SELECT fqn, kind, start_line FROM symbols WHERE file_id=? "
                    "AND parent_id IS NULL AND kind<>'file' ORDER BY start_line",
                    (frow["id"],)):
                out.append({"fqn": s["fqn"], "kind": s["kind"], "path": rel,
                            "start_line": s["start_line"]})
        return out

    def change_impact(self, target: str, depth: int = 3):
        """Impacto de um conjunto de mudanças: dados PATHS ou um DIFF, quais
        símbolos declarados neles têm dependentes, e o fecho transitivo desses
        dependentes (o que revisar/re-testar). Orientado ao fluxo real do agente
        (trabalha a partir de um diff), não de um fqn."""
        env = Envelope()
        paths = _paths_from_target(target)
        self._repair(set(paths), env)
        changed = self._symbols_in_paths(paths)
        impacted: dict[str, dict] = {}
        for c in changed:
            try:
                _s, rows, _e = self.impact(c["fqn"], depth=depth)
            except (SymbolNotFound, AmbiguousSymbol):
                continue
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
        tokens = [t for t in re.split(r"[^A-Za-z0-9_]+", task) if len(t) >= 3]
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
            "last_full_scan": int(last_scan) if last_scan else None,
            "last_full_scan_age_s": age,
            "by_language": {
                r["language"]: r["c"] for r in self.conn.execute(
                    "SELECT language, COUNT(*) c FROM files GROUP BY language "
                    "ORDER BY c DESC")
            },
        }
