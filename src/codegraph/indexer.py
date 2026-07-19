"""Indexação L0: walk do repo, transação por arquivo, resolução de arestas.

Invariantes (docs/DESIGN.md §1.2 e §2):
- re-indexar o arquivo F só toca linhas com file_id=F (transação por arquivo);
- arestas nunca perdem `dst_name`; alvo que sumiu vira dangling (dst=NULL)
  e é religado pela resolução na próxima passada;
- confiança L0: 'inferred' (alvo único via fqn/import ou mesmo arquivo),
  'possible' (match por nome, até MAX_CANDIDATES candidatos). 'certain' é L1.
"""

from __future__ import annotations

import time
from pathlib import Path

import pathspec

from . import community, extract, rank
from .db import connect, default_db_path
from .languages import get_parser, language_for
from .util import content_hash, like_escape, symbol_uid

MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_CANDIDATES = 5
CALLABLE_KINDS = ("function", "method", "class")

# Versão da lógica de extração/resolução: mudou → força re-index completo,
# mesmo com content-hashes iguais (o índice é derivado de código+extractor).
INDEXER_VERSION = "13"

DEFAULT_IGNORES = [
    ".git/", ".codegraph/", "__pycache__/", ".venv/", "venv/", "node_modules/",
    "dist/", "build/", ".next/", "coverage/", "target/", ".mypy_cache/",
    ".pytest_cache/", ".ruff_cache/", "*.min.js",
]


def load_ignore_spec(root: Path) -> pathspec.PathSpec:
    lines = list(DEFAULT_IGNORES)
    for name in (".gitignore", ".codegraphignore"):
        p = root / name
        if p.is_file():
            lines += p.read_text(encoding="utf-8", errors="replace").splitlines()
    return pathspec.GitIgnoreSpec.from_lines(lines)


def iter_source_files(root: Path, spec: pathspec.PathSpec | None = None):
    spec = spec or load_ignore_spec(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if spec.match_file(rel) or language_for(rel) is None:
            continue
        yield rel


def module_fqn_for(rel: str) -> str:
    dot = rel.rfind(".")
    stem = rel[:dot] if dot != -1 else rel
    parts = stem.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class Indexer:
    def __init__(self, root: str | Path, db_path: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.conn = connect(Path(db_path) if db_path else default_db_path(self.root))

    def close(self) -> None:
        self.conn.close()

    # -- indexação -----------------------------------------------------------

    def index_repo(self, force: bool = False) -> dict:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='indexer_version'").fetchone()
        if row is None or row["value"] != INDEXER_VERSION:
            force = True
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('indexer_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (INDEXER_VERSION,))
            self.conn.commit()
        spec = load_ignore_spec(self.root)
        seen: set[str] = set()
        stats = {"scanned": 0, "indexed": 0, "removed": 0, "errors": 0}
        for rel in iter_source_files(self.root, spec):
            seen.add(rel)
            stats["scanned"] += 1
            try:
                if self.index_file(rel, force=force):
                    stats["indexed"] += 1
            except Exception:
                stats["errors"] += 1
                self.conn.execute(
                    """INSERT INTO files(path, language, content_hash, size, mtime,
                       parse_status, indexed_at) VALUES(?,?,?,?,?,'failed',?)
                       ON CONFLICT(path) DO UPDATE SET parse_status='failed'""",
                    (rel, language_for(rel), "", 0, 0, int(time.time())),
                )
                self.conn.commit()
        # arquivos que sumiram do disco (delete/rename/branch switch)
        for row in self.conn.execute("SELECT path FROM files").fetchall():
            if row["path"] not in seen:
                self.remove_file(row["path"])
                stats["removed"] += 1
        self.resolve_edges()
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_full_scan', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(int(time.time())),),
        )
        self.conn.commit()
        return stats

    def index_file(self, rel: str, force: bool = False, data: bytes | None = None) -> bool:
        """Re-indexa um arquivo. Retorna True se o índice mudou."""
        path = self.root / rel
        lang = language_for(rel)
        if lang is None:
            return False
        if data is None:
            data = path.read_bytes()
        if len(data) > MAX_FILE_SIZE:
            return False
        h = content_hash(data)
        st = path.stat()
        row = self.conn.execute(
            "SELECT id, content_hash FROM files WHERE path=?", (rel,)
        ).fetchone()
        if row is not None and row["content_hash"] == h and not force:
            self.conn.execute(
                "UPDATE files SET mtime=?, size=? WHERE id=?",
                (int(st.st_mtime), st.st_size, row["id"]),
            )
            self.conn.commit()
            return False

        tree = get_parser(lang).parse(data)
        if lang == "cpp" and rel.endswith(".h") and tree.root_node.has_error:
            # .h é ambíguo: header C parseado como C++ pode falhar — tenta C
            c_tree = get_parser("c").parse(data)
            if not c_tree.root_node.has_error:
                lang, tree = "c", c_tree
        syms, refs = extract.extract(lang, data, module_fqn_for(rel), tree)
        status = "partial" if tree.root_node.has_error else "ok"

        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN")
            saved_descriptions: list = []
            if row is not None:
                file_id = row["id"]
                # descrições L3 sobrevivem ao re-index (ids de símbolo são
                # estáveis); source_hash antigo preservado → stale detectável
                saved_descriptions = cur.execute(
                    "SELECT d.symbol_id, d.scope, d.content, d.source_hash, "
                    "d.model, d.generated_at FROM descriptions d "
                    "JOIN symbols s ON d.symbol_id=s.id WHERE s.file_id=?",
                    (file_id,)).fetchall()
                cur.execute(
                    "DELETE FROM symbols_fts WHERE symbol_id IN "
                    "(SELECT id FROM symbols WHERE file_id=?)", (file_id,))
                cur.execute("DELETE FROM edges WHERE file_id=?", (file_id,))
                cur.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
                cur.execute(
                    "UPDATE files SET language=?, content_hash=?, size=?, mtime=?, "
                    "parse_status=?, indexed_at=? WHERE id=?",
                    (lang, h, st.st_size, int(st.st_mtime), status, int(time.time()), file_id),
                )
            else:
                cur.execute(
                    "INSERT INTO files(path, language, content_hash, size, mtime, "
                    "parse_status, indexed_at) VALUES(?,?,?,?,?,?,?)",
                    (rel, lang, h, st.st_size, int(st.st_mtime), status, int(time.time())),
                )
                file_id = cur.lastrowid

            ordinals: dict[tuple[str, str], int] = {}
            fqn_to_uid: dict[str, str] = {}
            all_uids: set[str] = set()
            for s in syms:
                ordinal = ordinals.get((s.fqn, s.kind), 0)
                ordinals[(s.fqn, s.kind)] = ordinal + 1
                uid = symbol_uid(rel, s.fqn, s.kind, ordinal)
                fqn_to_uid.setdefault(s.fqn, uid)
                all_uids.add(uid)
                cur.execute(
                    "INSERT INTO symbols(id, file_id, kind, name, fqn, signature, doc, "
                    "start_line, start_col, end_line, end_col, body_hash, visibility) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, file_id, s.kind, s.name, s.fqn, s.signature, s.doc,
                     s.start_line, s.start_col, s.end_line, s.end_col,
                     s.body_hash, s.visibility),
                )
                cur.execute(
                    "INSERT INTO symbols_fts(symbol_id, name, fqn, doc) VALUES(?,?,?,?)",
                    (uid, s.name, s.fqn, s.doc or ""),
                )
            for d in saved_descriptions:
                if d["symbol_id"] in all_uids:
                    cur.execute(
                        "INSERT INTO descriptions(symbol_id, scope, content, "
                        "source_hash, model, generated_at) VALUES(?,?,?,?,?,?)",
                        (d["symbol_id"], d["scope"], d["content"],
                         d["source_hash"], d["model"], d["generated_at"]))
            for s in syms:
                if s.parent_fqn and s.parent_fqn in fqn_to_uid:
                    cur.execute(
                        "UPDATE symbols SET parent_id=? WHERE id=?",
                        (fqn_to_uid[s.parent_fqn], fqn_to_uid[s.fqn]),
                    )
            for r in refs:
                cur.execute(
                    "INSERT INTO edges(kind, src, dst, dst_name, file_id, line, col, "
                    "confidence, resolver) VALUES(?,?,NULL,?,?,?,?,'possible','l0')",
                    (r.kind, fqn_to_uid.get(r.src_fqn) if r.src_fqn else None,
                     r.dst_name, file_id, r.line, r.col),
                )
            rank.mark_dirty(self.conn)
            community.mark_dirty(self.conn)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return True

    def remove_file(self, rel: str) -> None:
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()
        if row is None:
            return
        self.conn.execute(
            "DELETE FROM symbols_fts WHERE symbol_id IN "
            "(SELECT id FROM symbols WHERE file_id=?)", (row["id"],))
        self.conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
        rank.mark_dirty(self.conn)
        community.mark_dirty(self.conn)
        self.conn.commit()

    # -- resolução de arestas (docs/DESIGN.md §1.3) ---------------------------

    def resolve_edges(self) -> None:
        danglings = self.conn.execute(
            "SELECT id, kind, src, dst_name, file_id, line FROM edges WHERE dst IS NULL"
        ).fetchall()
        if not danglings:
            return
        cur = self.conn.cursor()
        lang_of = {r["id"]: r["language"] for r in
                   cur.execute("SELECT id, language FROM files")}
        # guesses repetem-se aos milhares (o mesmo alvo chamado de N sites) e os
        # símbolos não mudam durante a passada — memoizar por guess
        qual_cache: dict[str, list] = {}
        bare_cache: dict[tuple, list] = {}
        for e in danglings:
            guess = e["dst_name"]
            if not guess or guess.endswith(".*") or "*" in guess:
                continue
            if "." in guess:
                # guess qualificado (via import/escopo): match por fqn exato/sufixo.
                # name=último segmento usa idx_symbols_name; fqn = escopo.name,
                # então todo fqn com sufixo .guess termina no mesmo name
                cands = qual_cache.get(guess)
                if cands is None:
                    cands = cur.execute(
                        "SELECT id, file_id, fqn FROM symbols "
                        "WHERE name=? AND (fqn=? OR fqn LIKE ? ESCAPE '\\') LIMIT ?",
                        (guess.rsplit(".", 1)[-1], guess,
                         f"%.{like_escape(guess)}", MAX_CANDIDATES + 3),
                    ).fetchall()
                    qual_cache[guess] = cands
            elif e["kind"] in ("calls", "inherits"):
                # nome puro (receptor desconhecido): NUNCA entra no match de fqn —
                # busca por nome, restrita à MESMA linguagem do site da referência
                key = (guess, e["kind"], lang_of.get(e["file_id"]))
                cands = bare_cache.get(key)
                if cands is None:
                    kinds = (CALLABLE_KINDS if e["kind"] == "calls"
                             else ("class", "interface", "struct"))
                    placeholders = ",".join("?" * len(kinds))
                    cands = cur.execute(
                        f"SELECT s.id, s.file_id, s.fqn FROM symbols s "
                        f"JOIN files f ON s.file_id=f.id "
                        f"WHERE s.name=? AND s.kind IN ({placeholders}) "
                        f"AND f.language=? LIMIT ?",
                        (guess, *kinds, key[2], MAX_CANDIDATES + 3),
                    ).fetchall()
                    bare_cache[key] = cands
            else:
                continue
            # declaração+definição (C/C++) ou redefinições compartilham fqn:
            # contam como UM candidato (senão viram falsa ambiguidade)
            by_fqn: dict[str, object] = {}
            for c in cands:
                by_fqn.setdefault(c["fqn"], c)
            cands = list(by_fqn.values())[: MAX_CANDIDATES + 1]
            if not cands or len(cands) > MAX_CANDIDATES:
                continue  # permanece dangling — contado na completeness
            if len(cands) == 1:
                cur.execute(
                    "UPDATE edges SET dst=?, confidence='inferred' WHERE id=?",
                    (cands[0]["id"], e["id"]),
                )
                continue
            same_file = [c for c in cands if c["file_id"] == e["file_id"]]
            if len(same_file) == 1:
                cur.execute(
                    "UPDATE edges SET dst=?, confidence='inferred' WHERE id=?",
                    (same_file[0]["id"], e["id"]),
                )
                continue
            cur.execute(
                "UPDATE edges SET dst=?, confidence='possible' WHERE id=?",
                (cands[0]["id"], e["id"]),
            )
            for c in cands[1:]:
                cur.execute(
                    "INSERT INTO edges(kind, src, dst, dst_name, file_id, line, "
                    "confidence, resolver) VALUES(?,?,?,?,?,?,'possible','l0')",
                    (e["kind"], e["src"], c["id"], guess, e["file_id"], e["line"]),
                )
        self.conn.commit()
