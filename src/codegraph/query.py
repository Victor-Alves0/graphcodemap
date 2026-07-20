"""Query engine com read-repair e envelope de frescor/completeness.

Garantia (docs/DESIGN.md §2.3): nenhuma resposta sai sem verificar o
content-hash dos arquivos envolvidos. Divergiu → re-indexa (ms) e re-executa
a consulta; arquivo sumiu → sai do índice; tudo anotado no envelope.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .community import ensure_communities
from .indexer import Indexer, get_index_scopes, scan_source_stats
from .languages import get_parser
from .rank import ensure_ranks
from .util import like_escape

CALL_KINDS = ("calls",)
IMPACT_KINDS = ("calls", "imports", "inherits", "references")
_CONF_ORD = {"certain": 2, "inferred": 1, "possible": 0}

# presets de sink para reaches(): input não-confiável alcançando operação sensível.
# Casados contra o NOME do alvo da chamada (dst_name), case-insensitive.
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
    warnings: list[str] = field(default_factory=list)

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
                continue
            st = path.stat()
            if st.st_size == row["size"] and int(st.st_mtime) == row["mtime"]:
                continue  # fast-path: stat igual → assume fresco
            if self.ix.index_file(rel):
                env.warn(f"freshness: {rel} mudou desde a indexação; re-indexado agora (L0).")
                changed = True
        if changed:
            self.ix.resolve_edges()
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
        on_disk = scan_source_stats(self.root, scopes=get_index_scopes(self.conn) or None)
        stale: set[str] = set()
        for r in self.conn.execute("SELECT path, size, mtime FROM files"):
            cur = on_disk.get(r["path"])
            if cur is None:                          # sumiu do disco
                stale.add(r["path"])
            elif cur[0] != r["size"] or cur[1] != r["mtime"]:
                stale.add(r["path"])
        return self._repair(stale, env) if stale else False

    def _warn_partial(self, rels: set[str], env: Envelope) -> None:
        for rel in sorted(rels):
            row = self.conn.execute(
                "SELECT parse_status FROM files WHERE path=?", (rel,)
            ).fetchone()
            if row is not None and row["parse_status"] != "ok":
                env.warn(f"freshness: {rel} indexado parcialmente (erro de sintaxe no parse).")

    # -- seleção de símbolo ---------------------------------------------------

    def _find_rows(self, query: str, kind: str | None, limit: int) -> list:
        kind_sql = " AND s.kind=?" if kind else ""
        base = (
            "SELECT s.*, f.path, f.parse_status FROM symbols s "
            "JOIN files f ON s.file_id=f.id WHERE {} " + kind_sql
        )
        args_kind = [kind] if kind else []
        seen: dict[str, dict] = {}

        def take(rows):
            for r in rows:
                if r["id"] not in seen:
                    seen[r["id"]] = dict(r)

        take(self.conn.execute(base.format("s.fqn=?"), [query, *args_kind]).fetchall())
        if len(seen) < limit:
            take(self.conn.execute(
                base.format("s.fqn LIKE ? ESCAPE '\\'") + " LIMIT ?",
                [f"%.{like_escape(query)}", *args_kind, limit]).fetchall())
        if len(seen) < limit:
            take(self.conn.execute(
                base.format("s.name=?") + " LIMIT ?", [query, *args_kind, limit]).fetchall())
        if len(seen) < limit:
            tokens = [t for t in query.replace(".", " ").split() if t]
            if tokens:
                match = " ".join(f'"{t}"' for t in tokens)
                try:
                    ids = [r["symbol_id"] for r in self.conn.execute(
                        "SELECT symbol_id FROM symbols_fts WHERE symbols_fts MATCH ? LIMIT ?",
                        (match, limit)).fetchall()]
                except Exception:
                    ids = []
                if ids:
                    ph = ",".join("?" * len(ids))
                    take(self.conn.execute(
                        base.format(f"s.id IN ({ph})"), [*ids, *args_kind]).fetchall())
        if len(seen) < limit:
            take(self.conn.execute(
                base.format("s.name LIKE ? ESCAPE '\\'") + " LIMIT ?",
                [f"%{like_escape(query)}%", *args_kind, limit]).fetchall())
        return list(seen.values())[:limit]

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
                f"SELECT e.kind, e.line, e.confidence, f.path AS site_path, "
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
        self._warn_partial({r["site_path"] for r in rows}, env)
        return sym, [dict(r) for r in rows], env

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
                        f"SELECT e.line, e.confidence, e.dst_name, f.path AS site_path, "
                        f"s.id AS other_id, s.fqn AS other_fqn, s.kind AS other_kind "
                        f"FROM edges e JOIN files f ON e.file_id=f.id "
                        f"LEFT JOIN symbols s ON e.src=s.id "
                        f"WHERE e.dst IN ({ph}) AND e.kind='calls' "
                        f"ORDER BY f.path, e.line", list(frontier)).fetchall()
                else:
                    rows = self.conn.execute(
                        f"SELECT e.line, e.confidence, e.dst_name, f.path AS site_path, "
                        f"s.id AS other_id, s.fqn AS other_fqn, s.kind AS other_kind "
                        f"FROM edges e LEFT JOIN symbols s ON e.dst=s.id "
                        f"JOIN files f ON e.file_id=f.id "
                        f"WHERE e.src IN ({ph}) AND e.kind='calls' "
                        f"ORDER BY f.path, e.line", list(frontier)).fetchall()
                nxt = set()
                for r in rows:
                    results.append({**dict(r), "depth": d})
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
                    f"SELECT e.src, e.dst, e.kind, e.confidence, s.fqn, s.kind AS skind, "
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
                depth: int = 8, max_paths: int = 20):
        """Reachability entry→sink numa resposta só: seguindo o call graph a
        partir de `selector`, quais caminhos chegam a uma chamada que casa com
        `sink` (preset em SINK_PRESETS ou regex livre), e um validador/sanitizer
        `via` (ex.: um sanitizer) aparece em algum ponto do caminho?

        Substitui o LLM montando o caminho salto a salto lendo código: o grafo
        entrega a cadeia + o veredito de validação já pronto. Interprocedural,
        sob demanda (sempre fresco). Confiança do caminho = mínima das arestas.
        """
        import re as _re

        env = Envelope()
        sym = self._resolve_fresh(selector, env)
        sink_rx = _re.compile(SINK_PRESETS.get(sink, sink), _re.I)
        via_rx = _re.compile(_re.escape(via), _re.I) if via else None

        def walk():
            # BFS forward; parent[]/pconf[] p/ reconstruir cadeia e confiança
            parent = {sym["id"]: None}
            pconf = {sym["id"]: "certain"}
            calls_via = set()          # ids de funções que chamam o validador
            hits = []                  # (node_id, sink_name, line, path, conf)
            frontier = {sym["id"]}
            seen = {sym["id"]}
            for _d in range(depth):
                if not frontier:
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
            return out[:max_paths]

        parent, pconf, calls_via, hits = walk()
        paths = build(parent, pconf, calls_via, hits)
        involved = {p["site_path"] for p in paths} | {sym["path"]}
        if self._repair(involved, env):
            try:
                sym = self._resolve_selector(sym["fqn"])
            except SymbolNotFound:
                env.warn(f"freshness: '{sym['fqn']}' não existe mais após re-indexação.")
                return sym, {"sink": sink, "via": via, "paths": []}, env
            parent, pconf, calls_via, hits = walk()
            paths = build(parent, pconf, calls_via, hits)
        env.warn("reachability: estática (arestas 'calls'); chamadas dinâmicas/"
                 "reflexivas podem faltar. Confiança = mínima do caminho.")
        return sym, {"sink": sink, "via": via, "paths": paths}, env

    def ego_graph(self, selector: str):
        """Vizinhança imediata de um símbolo: todas as arestas tipadas, in e out."""
        env = Envelope()
        sym = self._resolve_fresh(selector, env)
        if self._repair({sym["path"]}, env):
            sym = self._resolve_selector(sym["fqn"])
        out_rows = self.conn.execute(
            "SELECT e.kind, e.confidence, e.line, e.dst_name, s.fqn AS other_fqn "
            "FROM edges e LEFT JOIN symbols s ON e.dst=s.id "
            "WHERE e.src=? ORDER BY e.kind, e.line", (sym["id"],)).fetchall()
        in_rows = self.conn.execute(
            "SELECT e.kind, e.confidence, e.line, f.path AS site_path, "
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
            "out": [dict(r) for r in out_rows],
            "in": [dict(r) for r in in_rows],
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
        """Extrai os fatos de fluxo de uma função. Retorna (FnFacts|None, lang)."""
        from . import dataflow as df

        lang = self.conn.execute(
            "SELECT language FROM files WHERE id=?",
            (sym_row["file_id"],)).fetchone()["language"]
        if not df.supported(lang):
            return None, lang
        data, tree = self._df_parse(sym_row["path"], lang, cache)
        if tree is None:
            return None, lang
        fn = df.find_function_node(tree.root_node, sym_row["start_line"], lang)
        if fn is None:
            return None, lang
        return df.extract_facts(data, fn, lang), lang

    def _df_resolve_call(self, src_id, line):
        rows = self.conn.execute(
            "SELECT e.dst, e.confidence, s.fqn, s.kind, s.start_line, "
            "f.path, f.language FROM edges e JOIN symbols s ON e.dst=s.id "
            "JOIN files f ON s.file_id=f.id WHERE e.src=? AND e.kind='calls' "
            "AND e.line=? AND e.dst IS NOT NULL "
            "ORDER BY CASE e.confidence WHEN 'certain' THEN 0 "
            "WHEN 'inferred' THEN 1 ELSE 2 END LIMIT 1", (src_id, line)).fetchall()
        return dict(rows[0]) if rows else None

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
            f, _ = self._df_facts(sym_row, cache)
            if f is None:
                return []
            flow = df.analyze_facts(f, tainted)
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
            flow = df.analyze_facts(facts, {p})
            sinks = trace(sym, {p}, 1, {(sym["id"], i)})
            result_params.append({
                "name": p, "reaches_return": flow.reaches_return, "sinks": sinks})
        env.warn("dataflow: intra-procedural may-taint (flow-insensitive, "
                 "over-aproxima) + call graph.")
        return {"function": sym, "supported": True, "params": result_params}, env

    def taint(self, scope: str | None = None, entry: str | None = None,
              depth: int = 4, max_findings: int = 100):
        """Análise de taint fonte→sink: input não-confiável alcançando operação
        perigosa. Sanitizers cortam o fluxo. Interprocedural via call graph.

        Dois modos: varredura do repo (fontes = chamadas a `sources`), ou
        `entry=func` (assume os parâmetros de `func` como não-confiáveis).
        Regras em .codegraph/taint.json. Ver docs/RESEARCH.md §6.
        """
        from . import dataflow as df
        from .taint_rules import load_rules

        env = Envelope()
        rules = load_rules(self.root)
        cache: dict = {}
        findings: list = []
        order = {"certain": 2, "inferred": 1, "possible": 0}

        def conf_min(a, b):
            if a is None:
                return b
            if b is None:
                return a
            return a if order[a] <= order[b] else b

        def trace(sym_row, tainted, origin, steps, d, visited, path_conf):
            f, _ = self._df_facts(sym_row, cache)
            if f is None:
                return
            flow = df.analyze_facts(f, tainted, rules.sanitizers)
            for af in flow.arg_flows:
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
                if af.callee in rules.sinks and len(findings) < max_findings:
                    findings.append({
                        "origin": origin,
                        "sink": {"callee": af.callee, "callee_fqn": step["callee_fqn"],
                                 "site_path": sym_row["path"], "line": af.line,
                                 "arg_index": af.arg_index, "via": af.via,
                                 "func_fqn": sym_row["fqn"]},
                        "confidence": cur_conf or "possible",
                        "steps": steps + [step],
                    })
                if (callee and d < depth and af.arg_index >= 0
                        and df.supported(callee["language"])):
                    key = (callee["dst"], af.arg_index)
                    if key not in visited:
                        visited.add(key)
                        crow = self._crow(callee["dst"])
                        cf, _ = self._df_facts(crow, cache) if crow else (None, None)
                        if cf and af.arg_index < len(cf.params):
                            trace(crow, {cf.params[af.arg_index]}, origin,
                                  steps + [step], d + 1, visited, cur_conf)

        if entry:
            sym = self._resolve_fresh(entry, env)
            self._repair({sym["path"]}, env)
            sym = self._resolve_selector(sym["fqn"])
            f, lang = self._df_facts(sym, cache)
            if f is None:
                env.warn(f"taint: '{entry}' em linguagem '{lang}' sem análise de fluxo.")
                return {"mode": "entry", "findings": [], "scanned": 0}, env
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
                f"WHERE s.kind IN ('function','method'){where}", args).fetchall()
            # 1ª passada: funções que RETORNAM dado de fonte viram elas próprias
            # fontes (pega o idioma comum do wrapper `x = get_input()`)
            collected: list = []
            src_funcs: set[str] = set()
            for r in rows:
                f, _ = self._df_facts(dict(r), cache)
                if f is None:
                    continue
                collected.append((dict(r), f))
                direct = any(rt.top_call in rules.sources for rt in f.returns)
                seed = df.source_vars(f, rules.sources)
                if direct or (seed and df.analyze_facts(f, seed).reaches_return):
                    src_funcs.add(r["name"])
            eff_sources = rules.sources | src_funcs
            scanned = 0
            for r, f in collected:
                scanned += 1
                seeds = df.source_sites(f, eff_sources)
                if not seeds:
                    continue
                names = {n for n, _, _ in seeds}
                origin = {"kind": "source", "func_fqn": r["fqn"], "path": r["path"],
                          "line": seeds[0][1], "what": seeds[0][2] + "()"}
                trace(r, names, origin, [], 1, {(r["id"], -2)}, None)
                if len(findings) >= max_findings:
                    env.warn(f"taint: {max_findings} achados (limite) — refine com --scope.")
                    break

        findings.sort(key=lambda x: -order[x["confidence"]])
        env.warn("taint: may-taint estático (over-aproxima) — achados são "
                 "candidatos a verificar; ajuste regras em .codegraph/taint.json.")
        return {"mode": "entry" if entry else "scan",
                "findings": findings, "scanned": scanned}, env

    def visualize(self, level: str = "file", scope: str | None = None,
                  top: int = 250) -> tuple[dict, Envelope]:
        """Monta os dados do grafo para export visual (HTML/JSON).

        Fresco como qualquer consulta: repara o índice, garante ranks e
        domínios antes de exportar.
        """
        from .viz import build_graph_data

        env = Envelope()
        self._repair_all(env)
        ensure_ranks(self.conn)
        ensure_communities(self.conn)
        data = build_graph_data(self.conn, level=level, scope=scope, top=top)
        return data, env

    def describe(self, target: str, refresh: bool = False):
        """Camada L3: descrição de comportamento (símbolo ou módulo/arquivo).

        Frescor verificado na leitura: código mudou → STALE declarado no
        envelope (refresh=True re-gera).
        """
        from .l3 import Describer

        env = Envelope()
        describer = Describer(self.root, self.conn, provider=self.l3_provider)
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

    # -- envelope de completeness (docs/DESIGN.md §3.1) -----------------------

    def _completeness(self, sym: dict, rows: list, env: Envelope) -> None:
        n_possible = sum(1 for r in rows if r["confidence"] == "possible")
        name = sym["name"]
        n_dangling = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE dst IS NULL AND kind='calls' "
            "AND (dst_name=? OR dst_name LIKE ? ESCAPE '\\')",
            (name, f"%.{like_escape(name)}")).fetchone()["c"]
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

        try:
            from .l1 import available_resolvers
            resolvers = sorted({lang for cls in available_resolvers()
                                for lang in cls.languages})
        except Exception:  # dependências L1 ausentes não devem quebrar o doctor
            resolvers = []

        call_edges = sum(conf.values()) or 1
        last_scan = meta.get("last_full_scan")
        age = (int(_time.time()) - int(last_scan)) if last_scan else None
        return {
            "root": str(self.root),
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
            "last_full_scan": int(last_scan) if last_scan else None,
            "last_full_scan_age_s": age,
            "by_language": {
                r["language"]: r["c"] for r in self.conn.execute(
                    "SELECT language, COUNT(*) c FROM files GROUP BY language "
                    "ORDER BY c DESC")
            },
        }
