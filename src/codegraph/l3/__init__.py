"""Camada L3: descrições de comportamento geradas por LLM (docs/DESIGN.md §3.4, M5).

Escopo deliberado (baseado na pesquisa, docs/RESEARCH.md):
- lazy e hub-first — nunca gerar em massa por default; função pequena o agente
  lê em 1 tool call, resumo dela é custo sem ganho;
- o valor real está em módulos (informação que não existe em arquivo nenhum)
  e em símbolos-hub consultados repetidamente;
- toda descrição carrega proveniência (modelo, data, hash de origem) e o
  frescor é verificado NA LEITURA: código mudou → servida marcada STALE,
  nunca como verdade.
"""

from __future__ import annotations

import time
from pathlib import Path

from .provider import L3Unavailable, provider_from_env

_SYSTEM = (
    "You summarize source code for AI coding agents. Be factual and dense. "
    "Describe observable behavior, side effects, invariants, error paths and "
    "how the code participates in the wider system. No praise, no fluff, no "
    "restating the signature. Plain text, no markdown headers."
)

_MAX_BODY_LINES = 220


class Describer:
    def __init__(self, root: Path, conn, provider=None) -> None:
        self.root = root
        self.conn = conn
        self._provider = provider  # Callable[(system, user)] -> str | None

    @property
    def provider(self):
        if self._provider is None:
            self._provider = provider_from_env(self.root)
        return self._provider

    # -- símbolo --------------------------------------------------------------

    def describe_symbol(self, sym: dict, refresh: bool = False) -> dict:
        cached = self.conn.execute(
            "SELECT content, source_hash, model, generated_at FROM descriptions "
            "WHERE symbol_id=? AND scope='symbol'", (sym["id"],)).fetchone()
        fresh = cached is not None and cached["source_hash"] == sym["body_hash"]
        if cached is not None and (fresh or not refresh):
            return {"content": cached["content"], "model": cached["model"],
                    "generated_at": cached["generated_at"], "fresh": fresh,
                    "generated_now": False, "scope": "symbol"}
        content, model = self._generate_symbol(sym)
        self.conn.execute(
            "INSERT INTO descriptions(symbol_id, scope, content, source_hash, "
            "model, generated_at) VALUES(?,'symbol',?,?,?,?) "
            "ON CONFLICT(symbol_id, scope) DO UPDATE SET content=excluded.content, "
            "source_hash=excluded.source_hash, model=excluded.model, "
            "generated_at=excluded.generated_at",
            (sym["id"], content, sym["body_hash"], model, int(time.time())))
        self.conn.commit()
        return {"content": content, "model": model,
                "generated_at": int(time.time()), "fresh": True,
                "generated_now": True, "scope": "symbol"}

    def _generate_symbol(self, sym: dict) -> tuple[str, str]:
        body = self._read_span(sym["path"], sym["start_line"], sym["end_line"])
        callers = [r["fqn"] for r in self.conn.execute(
            "SELECT DISTINCT s.fqn FROM edges e JOIN symbols s ON e.src=s.id "
            "WHERE e.dst=? AND e.kind='calls' AND e.confidence!='possible' LIMIT 8",
            (sym["id"],)).fetchall()]
        callees = [r["dst_name"] for r in self.conn.execute(
            "SELECT DISTINCT dst_name FROM edges WHERE src=? AND kind='calls' LIMIT 8",
            (sym["id"],)).fetchall()]
        user = (
            f"Symbol: {sym['fqn']} ({sym['kind']})\n"
            f"File: {sym['path']}\n"
            + (f"Called by: {', '.join(callers)}\n" if callers else "")
            + (f"Calls: {', '.join(callees)}\n" if callees else "")
            + f"\nSource:\n```\n{body}\n```\n\n"
            "Summarize behavior in 3-6 sentences."
        )
        return self._complete(user)

    # -- módulo ---------------------------------------------------------------

    def describe_module(self, frow: dict, refresh: bool = False) -> dict:
        cached = self.conn.execute(
            "SELECT content, source_hash, model, generated_at "
            "FROM module_descriptions WHERE file_id=?", (frow["id"],)).fetchone()
        fresh = cached is not None and cached["source_hash"] == frow["content_hash"]
        if cached is not None and (fresh or not refresh):
            return {"content": cached["content"], "model": cached["model"],
                    "generated_at": cached["generated_at"], "fresh": fresh,
                    "generated_now": False, "scope": "module"}
        content, model = self._generate_module(frow)
        self.conn.execute(
            "INSERT INTO module_descriptions(file_id, content, source_hash, "
            "model, generated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET content=excluded.content, "
            "source_hash=excluded.source_hash, model=excluded.model, "
            "generated_at=excluded.generated_at",
            (frow["id"], content, frow["content_hash"], model, int(time.time())))
        self.conn.commit()
        return {"content": content, "model": model,
                "generated_at": int(time.time()), "fresh": True,
                "generated_now": True, "scope": "module"}

    def _generate_module(self, frow: dict) -> tuple[str, str]:
        syms = self.conn.execute(
            "SELECT s.fqn, s.kind, s.signature, s.doc, d.content AS summary, "
            "d.source_hash, s.body_hash FROM symbols s "
            "LEFT JOIN descriptions d ON d.symbol_id=s.id AND d.scope='symbol' "
            "WHERE s.file_id=? AND s.parent_id IS NULL "
            "ORDER BY s.rank DESC LIMIT 20", (frow["id"],)).fetchall()
        lines = []
        for s in syms:
            entry = f"- {s['kind']} {s['signature'] or s['fqn']}"
            if s["summary"] and s["source_hash"] == s["body_hash"]:
                entry += f" — {s['summary']}"
            elif s["doc"]:
                entry += f" — {s['doc'].splitlines()[0]}"
            lines.append(entry)
        imports = [r["dst_name"] for r in self.conn.execute(
            "SELECT DISTINCT dst_name FROM edges WHERE file_id=? AND kind='imports' "
            "LIMIT 15", (frow["id"],)).fetchall()]
        user = (
            f"Module: {frow['path']} ({frow['language']})\n"
            + (f"Imports: {', '.join(imports)}\n" if imports else "")
            + "Top declarations:\n" + "\n".join(lines)
            + "\n\nSummarize this module's purpose, key components and how they "
              "interact, in 4-8 sentences."
        )
        return self._complete(user)

    # -- domínio (comunidade) -------------------------------------------------

    def describe_domain(self, community_id: int, refresh: bool = False) -> dict:
        """Rotula um domínio (comunidade do grafo) via LLM.

        O label é preservado entre recomputações enquanto o conjunto de membros
        não muda (communities.signature); presente ⇒ fresco para a composição
        atual. Membros mudaram ⇒ label foi descartado na detecção ⇒ re-gera.
        """
        c = self.conn.execute(
            "SELECT id, size, label, summary, model, generated_at "
            "FROM communities WHERE id=?", (community_id,)).fetchone()
        if c is None:
            raise ValueError(f"domínio inexistente: {community_id}")
        if c["label"] is not None and not refresh:
            return {"content": (c["summary"] or ""), "label": c["label"],
                    "model": c["model"], "generated_at": c["generated_at"],
                    "fresh": True, "generated_now": False, "scope": "domain"}
        label, summary, model = self._generate_domain(c)
        self.conn.execute(
            "UPDATE communities SET label=?, summary=?, model=?, generated_at=? "
            "WHERE id=?", (label, summary, model, int(time.time()), c["id"]))
        self.conn.commit()
        return {"content": summary, "label": label, "model": model,
                "generated_at": int(time.time()), "fresh": True,
                "generated_now": True, "scope": "domain"}

    def _generate_domain(self, c: dict) -> tuple[str, str, str]:
        syms = self.conn.execute(
            "SELECT s.fqn, s.kind FROM symbols s WHERE s.community=? "
            "ORDER BY s.rank DESC, s.fqn LIMIT 25", (c["id"],)).fetchall()
        files = self.conn.execute(
            "SELECT f.path, COUNT(*) n FROM symbols s JOIN files f ON s.file_id=f.id "
            "WHERE s.community=? GROUP BY f.path ORDER BY n DESC, f.path LIMIT 8",
            (c["id"],)).fetchall()
        listing = "\n".join(f"- {s['kind']} {s['fqn']}" for s in syms)
        flist = "\n".join(f"- {f['path']} ({f['n']} symbols)" for f in files)
        user = (
            f"A code community of {c['size']} symbols was detected in a repository "
            f"(graph clustering). Top members by importance:\n{listing}\n\n"
            f"Concentrated in:\n{flist}\n\n"
            "Name this subsystem/domain. Reply with a 2-4 word label on the first "
            "line, then one or two sentences on what it does and its role. "
            "No markdown."
        )
        raw = self._complete(user)
        content, model = raw
        lines = [ln for ln in content.splitlines() if ln.strip()]
        label = lines[0].strip(" .#*-") if lines else f"domain {c['id']}"
        summary = " ".join(lines[1:]).strip() if len(lines) > 1 else ""
        return label[:60], summary, model

    # -- infra ----------------------------------------------------------------

    def _complete(self, user: str) -> tuple[str, str]:
        provider = self.provider
        if provider is None:
            raise L3Unavailable(
                "camada L3 desabilitada: configure OPENROUTER_API_KEY (ou "
                "OPENROUTER_API) no ambiente ou no .env da raiz do repo.")
        content = provider(_SYSTEM, user).strip()
        model = getattr(provider, "model", "unknown")
        return content, model

    def _read_span(self, rel: str, start: int, end: int) -> str:
        try:
            all_lines = (self.root / rel).read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        span = all_lines[max(start - 1, 0):end]
        if len(span) > _MAX_BODY_LINES:
            span = span[:_MAX_BODY_LINES] + ["… (truncado)"]
        return "\n".join(span)


__all__ = ["Describer", "L3Unavailable"]
