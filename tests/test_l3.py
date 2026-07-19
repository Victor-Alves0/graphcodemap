"""M5: camada L3 — describe com cache, invalidação por hash e proveniência."""

from __future__ import annotations

import pytest

from codegraph.l3 import L3Unavailable


class FakeProvider:
    model = "fake/test-model"

    def __init__(self) -> None:
        self.calls = 0
        self.last_user = ""

    def __call__(self, system: str, user: str) -> str:
        self.calls += 1
        self.last_user = user
        return f"Resumo determinístico #{self.calls}."


@pytest.fixture()
def provider(cg):
    p = FakeProvider()
    cg.query.l3_provider = p
    return p


def test_describe_symbol_generates_and_caches(cg, provider):
    data, env = cg.describe("app.auth.TokenService.validate")
    assert data["generated_now"] and data["fresh"]
    assert data["model"] == "fake/test-model"
    assert provider.calls == 1
    # contexto do grafo entra no prompt
    assert "Called by" in provider.last_user

    data2, _ = cg.describe("app.auth.TokenService.validate")
    assert not data2["generated_now"]          # cache
    assert provider.calls == 1                 # provider não foi chamado
    assert data2["content"] == data["content"]


def test_describe_stale_is_declared_not_hidden(cg, provider, repo):
    cg.describe("app.auth.TokenService.validate")
    auth = repo / "app" / "auth.py"
    auth.write_text(auth.read_text(encoding="utf-8").replace(
        "Confere assinatura do token", "Valida token com política nova"),
        encoding="utf-8")

    data, env = cg.describe("app.auth.TokenService.validate")
    assert not data["fresh"]
    assert not data["generated_now"]           # serve o cache, mas marcado
    assert any("stale" in w.lower() for w in env.warnings)
    assert provider.calls == 1

    data3, env3 = cg.describe("app.auth.TokenService.validate", refresh=True)
    assert data3["generated_now"] and data3["fresh"]
    assert provider.calls == 2


def test_describe_module(cg, provider):
    data, env = cg.describe("app/auth.py")
    assert data["scope"] == "module" and data["generated_now"]
    assert "Top declarations" in provider.last_user
    data2, _ = cg.describe("app/auth.py")
    assert not data2["generated_now"]


def test_module_stale_after_edit(cg, provider, repo):
    cg.describe("app/auth.py")
    (repo / "app" / "auth.py").write_text(
        (repo / "app" / "auth.py").read_text(encoding="utf-8") +
        "\n\ndef nova_funcao():\n    pass\n", encoding="utf-8")
    data, env = cg.describe("app/auth.py")
    assert not data["fresh"]
    assert any("stale" in w.lower() for w in env.warnings)


def test_no_provider_is_explicit_error(cg, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API", raising=False)
    cg.query.l3_provider = None
    with pytest.raises(L3Unavailable):
        cg.describe("app.auth.TokenService.validate")


def test_dotenv_parsing(tmp_path):
    from codegraph.l3.provider import _load_dotenv

    (tmp_path / ".env").write_text(
        "# comentário\nOPENROUTER_API_KEY = 'abc123'\nCODEGRAPH_L3_MODEL=x/y\n",
        encoding="utf-8")
    env = _load_dotenv(tmp_path)
    assert env["OPENROUTER_API_KEY"] == "abc123"
    assert env["CODEGRAPH_L3_MODEL"] == "x/y"
