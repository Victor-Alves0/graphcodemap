"""Detecção de comunidades (domínios): Louvain, recompute lazy, labels L3."""

from __future__ import annotations

import pytest

from codegraph.community import louvain, recompute


# -- algoritmo (isolado) ------------------------------------------------------

def _edge(adj, a, b, w=1.0):
    adj.setdefault(a, {})[b] = w
    adj.setdefault(b, {})[a] = w


def test_louvain_separates_two_cliques():
    # dois triângulos ligados por uma única aresta → dois domínios
    adj = {}
    for a, b in [(0, 1), (1, 2), (0, 2), (3, 4), (4, 5), (3, 5), (2, 3)]:
        _edge(adj, a, b)
    part = louvain(adj)
    coms = {}
    for n, c in part.items():
        coms.setdefault(c, set()).add(n)
    groups = sorted(sorted(v) for v in coms.values())
    assert groups == [[0, 1, 2], [3, 4, 5]]


def test_louvain_deterministic():
    adj = {}
    for a, b in [(0, 1), (1, 2), (0, 2), (3, 4), (4, 5), (3, 5), (2, 3),
                 (6, 7), (7, 8), (6, 8), (5, 6)]:
        _edge(adj, a, b)
    assert louvain(adj) == louvain(adj)


def test_louvain_empty():
    assert louvain({}) == {}


# -- integração com o repo ----------------------------------------------------

def test_communities_assigns_and_lists(cg):
    items, meta, env = cg.communities(min_size=2)
    assert meta["total"] >= 1
    assert meta["assigned"] >= 2
    # cada item traz hubs e arquivos
    top = items[0]
    assert top["size"] >= 2
    assert top["top_symbols"]
    assert top["top_files"]


def test_symbol_gets_a_domain(cg):
    cg.communities(min_size=2)  # dispara a detecção
    info, env = cg.symbol_info("app.auth.TokenService.validate")
    # o símbolo participa do call graph → tem domínio
    assert info["domain"] is not None
    assert isinstance(info["domain"]["id"], int)


def test_recompute_is_lazy_and_marked(cg):
    conn = cg.query.conn
    # após index, está sujo
    row = conn.execute(
        "SELECT value FROM meta WHERE key='community_dirty'").fetchone()
    assert row is not None and row["value"] == "1"
    cg.communities(min_size=2)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='community_dirty'").fetchone()
    assert row["value"] == "0"  # limpo após recompute


def test_edit_invalidates_communities(cg, repo):
    cg.communities(min_size=2)
    conn = cg.query.conn
    assert conn.execute(
        "SELECT value FROM meta WHERE key='community_dirty'").fetchone()["value"] == "0"
    # editar um arquivo re-indexa (read-repair) e marca sujo
    auth = repo / "app" / "auth.py"
    auth.write_text(auth.read_text(encoding="utf-8") +
                    "\n\ndef extra():\n    return TokenService()\n", encoding="utf-8")
    cg.find_symbol("extra")  # força read-repair
    assert conn.execute(
        "SELECT value FROM meta WHERE key='community_dirty'").fetchone()["value"] == "1"


# -- labels L3 (com provider falso) -------------------------------------------

class FakeProvider:
    model = "fake/test-model"

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, system: str, user: str) -> str:
        self.calls += 1
        return f"Auth Domain\nGerencia autenticação e tokens (#{self.calls})."


def test_domain_label_generates_and_caches(cg):
    cg.communities(min_size=2)
    p = FakeProvider()
    cg.query.l3_provider = p
    data, env = cg.describe("domain:0")
    assert data["scope"] == "domain"
    assert data["label"] == "Auth Domain"
    assert data["generated_now"] and data["fresh"]
    assert p.calls == 1
    # segunda chamada usa cache (label preservado na tabela communities)
    data2, _ = cg.describe("domain:0")
    assert not data2["generated_now"]
    assert p.calls == 1


def test_domain_label_survives_recompute_when_stable(cg):
    """Label preservado entre recomputações se a composição não muda (signature)."""
    cg.communities(min_size=2)
    cg.query.l3_provider = FakeProvider()
    cg.describe("domain:0")
    # força nova detecção sem mudar código
    from codegraph.community import mark_dirty
    mark_dirty(cg.query.conn)
    recompute(cg.query.conn)
    # o domínio equivalente ainda tem label (reaproveitado por assinatura)
    labeled = cg.query.conn.execute(
        "SELECT COUNT(*) c FROM communities WHERE label IS NOT NULL").fetchone()["c"]
    assert labeled >= 1
