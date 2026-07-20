"""Observabilidade: doctor (saúde do índice), logging opt-in e custo do L3."""

from __future__ import annotations

import logging
import sys

import pytest

from codegraph import log as clog
from codegraph.l3.provider import OpenRouterProvider


# -- doctor -------------------------------------------------------------------

def test_doctor_reports_health(cg):
    d = cg.doctor()
    assert d["files"] > 0 and d["symbols"] > 0
    # o repo de fixture parseia limpo
    assert d["parse_failed_total"] == 0
    assert d["parse_failed_sample"] == []
    # distribuição de confiança das chamadas soma o total de arestas 'calls'
    assert d["call_edges"] == sum(d["confidence"].values())
    assert 0.0 <= d["certain_pct"] <= 100.0
    assert isinstance(d["l1_resolvers"], list)
    assert "python" in d["by_language"]
    # scan completo acabou de rodar → idade pequena e não-nula
    assert d["last_full_scan_age_s"] is not None
    assert d["last_full_scan_age_s"] >= 0


def test_doctor_surfaces_failed_files(cg):
    conn = cg.indexer.conn
    conn.execute("UPDATE files SET parse_status='failed' "
                 "WHERE path LIKE '%auth.py'")
    conn.commit()
    d = cg.doctor()
    assert d["parse_failed_total"] == 1
    assert any(p.endswith("auth.py") for p in d["parse_failed_sample"])


def test_compact_preserves_l3_and_stays_stable(cg):
    from test_l3 import FakeProvider

    p = FakeProvider()
    cg.query.l3_provider = p
    cg.describe("app.auth.TokenService.validate")          # gera + cacheia L3
    edges_before = cg.indexer.conn.execute(
        "SELECT COUNT(*) FROM edges").fetchone()[0]

    r = cg.compact()
    assert r["errors"] == 0
    # a descrição L3 sobrevive ao rebuild (ids de símbolo estáveis)
    data, _ = cg.describe("app.auth.TokenService.validate")
    assert not data["generated_now"]                       # veio do cache
    assert p.calls == 1
    # sem inflar: rebuild é idempotente
    assert cg.indexer.conn.execute(
        "SELECT COUNT(*) FROM edges").fetchone()[0] == edges_before


def test_diagnose_valid_file_is_none(cg, repo):
    assert cg.indexer.diagnose_file("app/auth.py") is None


def test_diagnose_reports_extract_error(cg, repo, monkeypatch):
    from codegraph import indexer as ix_mod

    def boom(*a, **k):
        raise ValueError("extractor quebrou")

    monkeypatch.setattr(ix_mod.extract, "extract", boom)
    reason = cg.indexer.diagnose_file("app/auth.py")
    assert reason is not None and "extractor quebrou" in reason


def test_render_doctor_flags_problems():
    from codegraph import render

    d = {
        "root": "/x", "indexer_version": "13", "files": 3, "symbols": 10,
        "parse": {"ok": 2, "failed": 1}, "parse_failed_total": 1,
        "parse_failed_sample": ["a/b.py"], "call_edges": 10,
        "confidence": {"possible": 9, "certain": 1}, "certain_pct": 10.0,
        "dangling": 2, "l1_resolvers": [], "last_full_scan": 1,
        "last_full_scan_age_s": 3, "by_language": {"python": 3},
    }
    out = render.doctor(d)
    assert "a/b.py" in out                       # arquivo com falha listado
    assert "falharam no parse" in out            # flag de parse
    assert "nenhum resolver L1" in out           # flag de L1 ausente
    assert "3s atrás" in out                     # staleness humanizado


# -- logging opt-in -----------------------------------------------------------

@pytest.fixture()
def reset_log():
    """Reseta o estado global de configuração do logger entre casos."""
    root = logging.getLogger("codegraph")
    saved = list(root.handlers)
    root.handlers.clear()
    clog._configured = False
    yield
    root.handlers.clear()
    root.handlers.extend(saved)
    clog._configured = False


def _writes_stderr(root) -> bool:
    # o handler DELE escreve em sys.stderr; o do pytest usa um buffer interno
    return any(getattr(h, "stream", None) is sys.stderr for h in root.handlers)


def test_log_silent_by_default(monkeypatch, reset_log):
    monkeypatch.delenv("CODEGRAPH_LOG", raising=False)
    monkeypatch.delenv("CODEGRAPH_DEBUG", raising=False)
    assert clog.enabled() is False
    logger = clog.get("codegraph.test")
    root = logging.getLogger("codegraph")
    # silencioso: nada é escrito em stderr; há um NullHandler nosso
    assert not _writes_stderr(root)
    assert any(isinstance(h, logging.NullHandler) for h in root.handlers)
    assert logger.name == "codegraph.test"


def test_log_debug_env_attaches_handler(monkeypatch, reset_log):
    monkeypatch.setenv("CODEGRAPH_DEBUG", "1")
    monkeypatch.delenv("CODEGRAPH_LOG", raising=False)
    assert clog.enabled() is True
    clog.get("x")
    root = logging.getLogger("codegraph")
    assert root.level == logging.DEBUG
    assert _writes_stderr(root)                  # agora escreve em stderr


def test_log_level_from_name(monkeypatch, reset_log):
    monkeypatch.delenv("CODEGRAPH_DEBUG", raising=False)
    monkeypatch.setenv("CODEGRAPH_LOG", "warning")
    clog.get("x")
    assert logging.getLogger("codegraph").level == logging.WARNING


# -- custo do L3 --------------------------------------------------------------

def test_provider_accounts_usage():
    p = OpenRouterProvider("k", "vendor/model")
    assert p.total_tokens == 0 and p.calls == 0
    p._account({"usage": {"prompt_tokens": 30, "completion_tokens": 12,
                          "total_tokens": 42}})
    p._account({"usage": {"prompt_tokens": 8, "completion_tokens": 2,
                          "total_tokens": 10}})
    u = p.usage
    assert u["calls"] == 2
    assert u["prompt_tokens"] == 38
    assert u["completion_tokens"] == 14
    assert u["total_tokens"] == 52
    assert u["model"] == "vendor/model"


def test_provider_account_tolerates_missing_usage():
    p = OpenRouterProvider("k", "m")
    p._account({})                                # resposta sem 'usage'
    assert p.calls == 1 and p.total_tokens == 0


class _CostingProvider:
    """Fake com a mesma superfície de custo do OpenRouterProvider."""
    model = "fake/cost"

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, system: str, user: str) -> str:
        self.calls += 1
        return "resumo."

    @property
    def usage(self) -> dict:
        return {"calls": self.calls, "prompt_tokens": 100,
                "completion_tokens": 20, "total_tokens": 120,
                "model": self.model}


def test_describe_attaches_and_renders_cost(cg):
    from codegraph import render

    cg.query.l3_provider = _CostingProvider()
    data, env = cg.describe("app.auth.TokenService.validate")
    assert data["generated_now"]
    assert data["usage"]["total_tokens"] == 120
    out = render.describe(data, env)
    assert "custo:" in out and "120 tokens" in out

    # cache hit não anexa custo novo (nada foi gerado)
    data2, _ = cg.describe("app.auth.TokenService.validate")
    assert not data2["generated_now"]
    assert "usage" not in data2
