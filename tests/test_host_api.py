"""API para HOSTS que embarcam o GraphCodeMap (não a CLI).

Quatro necessidades de quem integra como serviço:
1. `index()` devolve QUAIS símbolos mudaram — fecha o loop "editei → o que
   quebra?" sem diff de git nem adivinhar o símbolo;
2. credencial L3 INJETADA (por instância ou por chamada) em vez de os.environ —
   num host multi-usuário a chave é do usuário e o custo precisa ser atribuível;
3. `exclude=` guardado no índice — política do host, sem escrever
   `.codegraphignore` na working copy do usuário;
4. `doctor()` não devolve caminho absoluto do servidor.
"""

from __future__ import annotations

import os

import pytest

from codegraph import CodeGraph

SRC = "def save_user(name):\n    return name\n\n\ndef old_fn():\n    return 1\n"
SRC2 = "def save_user(name, email):\n    return name\n\n\ndef nova():\n    return 2\n"


def _touch_newer(p):
    st = p.stat()
    os.utime(p, (st.st_atime + 2, st.st_mtime + 2))


# -- 1. index() devolve os símbolos que mudaram ------------------------------

def test_index_reports_added_removed_and_signature_change(tmp_path):
    (tmp_path / "u.py").write_text(SRC, encoding="utf-8")
    cg = CodeGraph(tmp_path)
    first = cg.index()
    assert first["changes"]["counts"]["added"] == 2      # índice inicial

    (tmp_path / "u.py").write_text(SRC2, encoding="utf-8")
    _touch_newer(tmp_path / "u.py")
    ch = cg.index()["changes"]

    assert ch["added"] == ["u.nova"]
    assert ch["removed"] == ["u.old_fn"]
    assert len(ch["signature_changed"]) == 1
    sig = ch["signature_changed"][0]
    assert sig["fqn"] == "u.save_user"
    assert "email" not in (sig["before"] or "")
    assert "email" in (sig["after"] or "")
    cg.close()


def test_index_reports_symbols_of_deleted_file(tmp_path):
    (tmp_path / "u.py").write_text(SRC, encoding="utf-8")
    cg = CodeGraph(tmp_path)
    cg.index()
    (tmp_path / "u.py").unlink()
    ch = cg.index()["changes"]
    assert set(ch["removed"]) == {"u.save_user", "u.old_fn"}
    cg.close()


def test_index_file_exposes_last_changes(tmp_path):
    (tmp_path / "u.py").write_text(SRC, encoding="utf-8")
    cg = CodeGraph(tmp_path)
    cg.index()
    (tmp_path / "u.py").write_text(SRC2, encoding="utf-8")
    _touch_newer(tmp_path / "u.py")
    cg.indexer.index_file("u.py")
    ch = cg.indexer.last_changes
    assert ch is not None and ch["counts"]["signature_changed"] == 1
    cg.close()


# -- 2. credencial L3 injetada (sem env) -------------------------------------

class _FakeLLM:
    model = "fake/model"

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, system: str, user: str) -> str:
        self.calls += 1
        return "Resumo determinístico para teste."


def test_describe_accepts_per_call_llm(tmp_path, monkeypatch):
    # sem chave no ambiente: só funciona porque a credencial foi INJETADA
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API", raising=False)
    (tmp_path / "u.py").write_text(SRC, encoding="utf-8")
    cg = CodeGraph(tmp_path)
    cg.index()
    llm = _FakeLLM()
    data, _ = cg.describe("u.save_user", llm=llm)
    assert llm.calls == 1
    assert data["content"]
    cg.close()


def test_constructor_llm_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API", raising=False)
    (tmp_path / "u.py").write_text(SRC, encoding="utf-8")
    llm = _FakeLLM()
    cg = CodeGraph(tmp_path, llm=llm)
    cg.index()
    cg.describe("u.save_user")
    assert llm.calls == 1
    cg.close()


def test_api_key_string_is_coerced_to_provider():
    from codegraph.l3.provider import coerce_provider

    p = coerce_provider("sk-teste-123")
    assert getattr(p, "api_key", None) == "sk-teste-123"
    assert hasattr(p, "usage")            # custo atribuível pelo host
    assert coerce_provider(None) is None


# -- 3. exclude= guardado no índice ------------------------------------------

def _paths(cg):
    return {r["path"] for r in cg.indexer.conn.execute("SELECT path FROM files")}


def test_exclude_is_persisted_and_respected(tmp_path):
    (tmp_path / "keep.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "s.py").write_text("def b():\n    return 2\n", encoding="utf-8")

    cg = CodeGraph(tmp_path)
    cg.index(exclude=["secret/"])
    assert _paths(cg) == {"keep.py"}

    cg.index()                      # sem exclude → mantém a política salva
    assert _paths(cg) == {"keep.py"}

    cg.index(exclude=[])            # limpa a política → volta a indexar
    assert _paths(cg) == {"keep.py", "secret/s.py"}
    cg.close()


def test_exclude_does_not_write_into_the_repo(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    cg = CodeGraph(tmp_path)
    cg.index(exclude=["*.log"])
    # política vive no índice, não na working copy do usuário
    assert not (tmp_path / ".codegraphignore").exists()
    from codegraph.indexer import get_index_excludes
    assert get_index_excludes(cg.indexer.conn) == ["*.log"]
    cg.close()


# -- 4. doctor() sem caminho absoluto ----------------------------------------

def test_doctor_does_not_leak_absolute_path(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    cg = CodeGraph(tmp_path)
    cg.index()
    d = cg.doctor()
    assert "root" not in d
    assert d["root_name"] == tmp_path.name
    flat = repr(d)
    assert str(tmp_path) not in flat and os.sep + os.sep not in flat
    cg.close()
