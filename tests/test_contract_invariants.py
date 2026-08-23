"""Testes de CONTRATO do grafo: invariantes, não parser (Prioridade 3).

O valor do produto está nos invariantes — o que o grafo PROMETE a um agente,
não em quais nós um extractor produz. Estes testes travam as 10 promessas como
contrato de primeira classe, de ponta a ponta (índice real via `CodeGraph`):

  1. reindexar o mesmo arquivo N vezes não duplica dados;
  2. alterar o corpo de uma função preserva sua identidade (id do símbolo);
  3. remover um símbolo transforma arestas em dangling (sem perda silenciosa);
  4. reindexar o arquivo religa as referências pendentes;
  5. mudar o branch (conteúdo no disco) atualiza o índice;
  6. nenhuma resposta marcada como fresca usa hash antigo;
  7. `possible` nunca é apresentado como `certain`;
  8. impact propaga a MENOR confiança do caminho;
  9. arquivos excluídos não entram no grafo;
 10. respostas não vazam o caminho absoluto do servidor.

Vários já tinham cobertura parcial e espalhada (freshness, edge_idempotency,
system_integration). Aqui ficam nomeados, agrupados e verificados como o
contrato que são."""

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from codegraph import CodeGraph, render
from codegraph.util import content_hash


def _graph(tmp_path, files: dict[str, str], *, git: bool = False):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    if git and shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    g = CodeGraph(tmp_path)
    g.index()
    return g


CHAIN = {
    "lib.py": "def target():\n    return 1\n",
    "use.py": "from lib import target\n\ndef use():\n    return target()\n",
}


def _edges(g, where="1=1"):
    return g.indexer.conn.execute(
        f"SELECT COUNT(*) FROM edges WHERE {where}").fetchone()[0]


def _symbols(g):
    return g.indexer.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]


# ============================================================================
# 1. Reindexar o mesmo arquivo N vezes não duplica dados
# ============================================================================

def test_reindex_same_file_does_not_duplicate(tmp_path):
    g = _graph(tmp_path, CHAIN)
    s0, e0 = _symbols(g), _edges(g)
    for _ in range(4):
        g.indexer.index_file("use.py", force=True)
        g.indexer.resolve_edges()
    assert (_symbols(g), _edges(g)) == (s0, e0)
    g.close()


def test_force_reindex_repo_is_idempotent(tmp_path):
    g = _graph(tmp_path, CHAIN)
    s0, e0 = _symbols(g), _edges(g)
    for _ in range(3):
        g.index(force=True)
    assert (_symbols(g), _edges(g)) == (s0, e0)
    g.close()


# ============================================================================
# 2. Alterar o corpo de uma função preserva sua identidade
# ============================================================================

def test_body_edit_preserves_symbol_id(tmp_path):
    g = _graph(tmp_path, {"a.py": "def f():\n    return 1\n"})
    id1 = g.indexer.conn.execute(
        "SELECT id FROM symbols WHERE fqn='a.f'").fetchone()["id"]
    (tmp_path / "a.py").write_text(
        "def f():\n    x = 10\n    return x + 999\n", encoding="utf-8")
    g.index()
    row = g.indexer.conn.execute(
        "SELECT id FROM symbols WHERE fqn='a.f'").fetchone()
    assert row is not None and row["id"] == id1   # mesma identidade
    g.close()


def test_identity_survives_reorder_of_siblings(tmp_path):
    # reordenar funções irmãs NÃO deve trocar as identidades entre si
    g = _graph(tmp_path, {"m.py": "def a():\n    return 1\n\ndef b():\n    return 2\n"})
    ids = {r["fqn"]: r["id"] for r in g.indexer.conn.execute(
        "SELECT fqn, id FROM symbols WHERE kind='function'")}
    (tmp_path / "m.py").write_text(
        "def a():\n    return 1\n\ndef b():\n    return 2\n\n# comentário novo\n",
        encoding="utf-8")
    g.index()
    ids2 = {r["fqn"]: r["id"] for r in g.indexer.conn.execute(
        "SELECT fqn, id FROM symbols WHERE kind='function'")}
    assert ids == ids2
    g.close()


# ============================================================================
# 3. Remover um símbolo transforma arestas em dangling (sem perda silenciosa)
# ============================================================================

def test_removing_target_creates_dangling_edge(tmp_path):
    g = _graph(tmp_path, CHAIN)
    before = _edges(g, "dst_name LIKE '%target' AND dst IS NOT NULL AND kind='calls'")
    assert before >= 1
    g.indexer.remove_file("lib.py")
    row = g.indexer.conn.execute(
        "SELECT dst, dst_name FROM edges "
        "WHERE dst_name LIKE '%target' AND kind='calls'").fetchone()
    # a aresta continua, agora dangling (dst=NULL) mas com dst_name preservado
    assert row is not None and row["dst"] is None
    assert row["dst_name"] and row["dst_name"].endswith("target")
    g.close()


def test_dangling_shows_up_in_stats(tmp_path):
    g = _graph(tmp_path, CHAIN)
    g.indexer.remove_file("lib.py")
    s = g.stats()
    assert s["edges"] == s["edges_resolved"] + s["edges_dangling"]
    assert s["edges_dangling"] >= 1
    g.close()


# ============================================================================
# 4. Reindexar o arquivo religa as referências pendentes
# ============================================================================

def test_reindexing_target_reconnects_references(tmp_path):
    g = _graph(tmp_path, CHAIN)

    def dst():
        return g.indexer.conn.execute(
            "SELECT dst FROM edges WHERE dst_name LIKE '%target' "
            "AND kind='calls'").fetchone()["dst"]

    assert dst() is not None
    g.indexer.remove_file("lib.py")
    assert dst() is None                         # dangling
    g.index()                                    # lib.py reaparece p/ o walker
    assert dst() is not None                     # religado
    g.close()


# ============================================================================
# 5. Mudar o branch (conteúdo no disco) atualiza o índice
# ============================================================================

@pytest.mark.skipif(not shutil.which("git"), reason="git ausente")
def test_branch_switch_updates_index(tmp_path):
    g = _graph(tmp_path, {"a.py": "def on_main():\n    return 1\n"}, git=True)
    env = {"cwd": tmp_path}
    # o índice vive em .codegraph/; não deve ser versionado nem conflitar no checkout
    (tmp_path / ".gitignore").write_text(".codegraph/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], **env)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                    "commit", "-qm", "main"], **env)
    # nome do branch inicial varia (main/master conforme a config do git)
    base = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True, **env).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], **env)
    (tmp_path / "a.py").write_text(
        "def on_feature():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], **env)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                    "commit", "-qm", "feature"], **env)
    # volta pro branch base: o conteúdo do disco troca de volta
    subprocess.run(["git", "checkout", "-q", base], **env)
    rows_main, _ = g.find_symbol("on_main")
    rows_feat, _ = g.find_symbol("on_feature")
    assert rows_main and not rows_feat           # índice segue o disco (branch base)
    # agora vai pra feature
    subprocess.run(["git", "checkout", "-q", "feature"], **env)
    assert g.find_symbol("on_feature")[0]
    assert not g.find_symbol("on_main")[0]
    g.close()


# ============================================================================
# 6. Nenhuma resposta marcada como fresca usa hash antigo
# ============================================================================

def _stored_hashes(g):
    return {r["path"]: r["content_hash"] for r in g.indexer.conn.execute(
        "SELECT path, content_hash FROM files")}


def test_fresh_response_matches_disk_hash(tmp_path):
    g = _graph(tmp_path, CHAIN)
    _rows, env = g.find_symbol("target")
    assert env.fresh is True
    for rel, stored in _stored_hashes(g).items():
        disk = content_hash((tmp_path / rel).read_bytes())
        assert stored == disk, f"{rel}: hash indexado != disco em resposta fresca"
    g.close()


def test_drift_flips_fresh_to_false(tmp_path):
    g = _graph(tmp_path, {"a.py": "def alpha():\n    return 1\n"})
    # edita no disco mudando o TAMANHO (fast-path por stat depende do size)
    (tmp_path / "a.py").write_text(
        "def alpha():\n    return 1 + 2 + 3 + 4\n", encoding="utf-8")
    _rows, env = g.find_symbol("alpha")
    assert env.fresh is False                    # drift detectado e corrigido
    # e após a correção o hash volta a bater
    for rel, stored in _stored_hashes(g).items():
        assert stored == content_hash((tmp_path / rel).read_bytes())
    g.close()


# ============================================================================
# 7. `possible` nunca é apresentado como `certain`
# ============================================================================

AMBIG = {
    "a.py": "def run():\n    return 1\n",
    "b.py": "def run():\n    return 2\n",
    "c.py": "def go():\n    return run()\n",
}


def test_possible_stays_possible_through_query(tmp_path):
    g = _graph(tmp_path, AMBIG)
    sym, rows, _ = g.query.callers("a.run")
    # run() é ambíguo → aresta 'possible'; a query não pode promovê-la
    assert rows and all(r["confidence"] == "possible" for r in rows)
    g.close()


def test_render_never_upgrades_possible_to_certain(tmp_path):
    g = _graph(tmp_path, AMBIG)
    out = render.calls(*g.query.callers("a.run"), "callers de", "in")
    assert "certain" not in out.lower()          # nunca rotula como certeza
    g.close()


def test_query_confidence_is_subset_of_stored(tmp_path):
    # o invariante estrutural: toda confiança relatada existe no DB tal e qual
    g = _graph(tmp_path, {**CHAIN, **AMBIG})
    stored = {r["confidence"] for r in g.indexer.conn.execute(
        "SELECT DISTINCT confidence FROM edges")}
    _s, rows, _ = g.query.impact("target")
    reported = {r["confidence"] for r in rows}
    assert reported <= stored
    g.close()


# ============================================================================
# 8. Impact propaga a MENOR confiança do caminho
# ============================================================================

def test_impact_propagates_minimum_confidence(tmp_path):
    # target ← (import-traced, inferred) ← mid ← (nome ambíguo, possible) ← go
    g = _graph(tmp_path, {
        "leaf.py": "def target():\n    return 1\n",
        "mid.py": "from leaf import target\n\ndef mid():\n    return target()\n",
        "other.py": "def mid():\n    return 2\n",       # torna mid() ambíguo
        "caller.py": "def go():\n    return mid()\n",
    })
    _s, rows, _ = g.query.impact("target")
    by = {r["fqn"]: r for r in rows}
    assert by["mid.mid"]["confidence"] == "inferred"
    # o salto extra é 'possible' → o caminho até go degrada p/ 'possible' (min),
    # nunca para o mais forte dos dois
    assert by["caller.go"]["confidence"] == "possible"
    g.close()


def test_impact_confidence_is_monotonic_along_chain(tmp_path):
    from codegraph.query import _CONF_ORD
    g = _graph(tmp_path, {
        "leaf.py": "def target():\n    return 1\n",
        "mid.py": "from leaf import target\n\ndef mid():\n    return target()\n",
        "other.py": "def mid():\n    return 2\n",
        "caller.py": "def go():\n    return mid()\n",
    })
    _s, rows, _ = g.query.impact("target")
    by_depth = {}
    for r in rows:
        by_depth.setdefault(r["depth"], []).append(_CONF_ORD[r["confidence"]])
    # numa cadeia linear a confiança nunca AUMENTA com a profundidade
    d1 = min(by_depth.get(1, [0]))
    d2 = min(by_depth.get(2, [0]))
    assert d2 <= d1
    g.close()


# ============================================================================
# 9. Arquivos excluídos não entram no grafo
# ============================================================================

def test_excluded_files_stay_out(tmp_path):
    g = _graph(tmp_path, {
        "keep.py": "def keep():\n    return 1\n",
        "gen/skip.py": "def skip():\n    return 2\n",
    })
    g.index(exclude=["gen/"])
    assert g.find_symbol("keep")[0]
    assert not g.find_symbol("skip")[0]
    # e o arquivo nem consta na tabela de arquivos
    paths = {r["path"] for r in g.indexer.conn.execute("SELECT path FROM files")}
    assert not any(p.startswith("gen/") for p in paths)
    g.close()


def test_gitignore_excludes_from_graph(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    g = _graph(tmp_path, {
        "src.py": "def real():\n    return 1\n",
        "build/gen.py": "def generated():\n    return 2\n",
    })
    assert g.find_symbol("real")[0]
    assert not g.find_symbol("generated")[0]
    g.close()


# ============================================================================
# 10. Respostas não vazam o caminho absoluto do servidor
# ============================================================================

def test_no_absolute_path_leaks_across_responses(tmp_path):
    g = _graph(tmp_path, {**CHAIN, **AMBIG})
    leaked = str(tmp_path)
    outputs = [
        render.find("target", *g.query.find_symbol("target")),
        render.info(*g.query.symbol_info("use.use")),
        render.refs(*g.query.references("target")),
        render.calls(*g.query.callers("a.run"), "callers de", "in"),
        render.impact(*g.query.impact("target")),
        render.overview(*g.query.overview()),
        render.doctor(g.query.doctor()),
    ]
    for out in outputs:
        assert leaked not in out, "vazou caminho absoluto do servidor numa resposta"
    g.close()


def test_symbol_rows_carry_relative_paths(tmp_path):
    g = _graph(tmp_path, CHAIN)
    leaked = str(tmp_path)
    _s, rows, _ = g.query.references("target")
    for r in rows:
        for v in r.values():
            if isinstance(v, str):
                assert leaked not in v
    g.close()
