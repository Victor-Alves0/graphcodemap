"""M6: harness de avaliação — tools e loop do agente (sem rede)."""

from __future__ import annotations

import json

from codegraph.eval.agent import run_task
from codegraph.eval.tools import BaselineTools, CodeGraphTools


class FakeChat:
    """Roteiro fixo: 1ª resposta chama grep, 2ª entrega resposta final."""

    model = "fake"

    def __init__(self) -> None:
        self.rounds = 0

    def complete(self, messages, tools=None, tool_choice="auto"):
        self.rounds += 1
        if self.rounds == 1:
            return {"usage": {"total_tokens": 100},
                    "choices": [{"message": {"content": "", "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "grep",
                                      "arguments": json.dumps(
                                          {"pattern": "issue_token"})}}]}}]}
        assert messages[-1]["role"] == "tool"  # resultado da tool entrou no loop
        return {"usage": {"total_tokens": 50},
                "choices": [{"message":
                             {"content": "issue_token está em app/auth.py."}}]}


def test_baseline_tools(repo):
    t = BaselineTools(repo)
    files = t.list_files()
    assert "app/auth.py" in files
    hits = t.grep("def issue_token")
    assert "app/auth.py" in hits
    content = t.read_file("app/auth.py", start_line=1, end_line=5)
    assert content.splitlines()[0].startswith("1\t")
    assert "arquivo não encontrado" in t.read_file("nao/existe.py")


def test_codegraph_tools(cg, repo):
    t = CodeGraphTools(repo, cg.query)
    out = t.find_symbol("issue_token")
    assert "app.auth.issue_token" in out
    out = t.callers("app.auth.issue_token")
    assert "login" in out and "completeness" in out
    out = t.impact("app.db.get_session")
    assert "_check" in out
    assert "erro:" in t.callers("simbolo_inexistente_xyz")
    # schemas: baseline + grafo
    names = {s["function"]["name"] for s in t.schemas()}
    assert {"grep", "read_file", "callers", "impact", "overview"} <= names


def test_run_task_loop(repo):
    chat = FakeChat()
    result = run_task(chat, BaselineTools(repo),
                      "system", "Onde está issue_token?", max_steps=5)
    assert result["answer"] == "issue_token está em app/auth.py."
    assert result["tokens"] == 150
    assert result["tool_calls"] == 1


def test_judge_parsing():
    from codegraph.eval import judge

    class JudgeChat:
        def complete(self, messages, tools=None, tool_choice="auto"):
            return {"choices": [{"message": {"content":
                    'Aqui está: {"score": 8, "reason": "quase completo"}'}}]}

    r = judge(JudgeChat(), "q", "ref", "answer")
    assert r["score"] == 8 and "quase" in r["reason"]
