"""Agente tool-loop mínimo sobre a API OpenAI-compatível (OpenRouter).

Deliberadamente simples: o harness mede o valor MARGINAL das tools de grafo,
não a sofisticação do agente. Mesmo loop, mesmo modelo, mesmos prompts — a
única variável entre os braços é o conjunto de tools.
"""

from __future__ import annotations

import json
import time
import urllib.request

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOOL_RESULT_CHARS = 4000


class OpenRouterChat:
    def __init__(self, api_key: str, model: str, timeout: float = 180.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list, tools: list | None = None,
                 tool_choice: str = "auto") -> dict:
        body: dict = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "X-Title": "CodeGraph-eval"})
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"OpenRouter falhou após retries: {last_err}")


def run_task(chat: OpenRouterChat, toolset, system: str, question: str,
             max_steps: int = 12) -> dict:
    """Executa uma task; retorna answer + métricas (tokens, tool calls, tempo)."""
    messages: list = [{"role": "system", "content": system},
                      {"role": "user", "content": question}]
    schemas = toolset.schemas()
    tokens = 0
    cached = 0
    tool_calls = 0
    t0 = time.perf_counter()
    answer = ""
    for step in range(max_steps):
        final_round = step == max_steps - 1
        if final_round:
            # sem tools na última rodada: instruir explicitamente, senão o
            # modelo emite sintaxe interna de tool-call como texto
            messages.append({"role": "user", "content":
                             "Sem mais tools. Dê a resposta final agora com o "
                             "que você já apurou."})
        resp = chat.complete(messages, tools=None if final_round else schemas)
        usage = resp.get("usage") or {}
        tokens += usage.get("total_tokens", 0)
        # prompt caching (OpenRouter/DeepSeek): prefixo estável = mensagens
        # append-only + schemas fixos; tokens cacheados custam ~1/10
        cached += (usage.get("prompt_tokens_details") or {}).get(
            "cached_tokens", 0)
        msg = (resp.get("choices") or [{}])[0].get("message", {})
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            answer = msg.get("content") or ""
            if "<｜" in answer or "tool_calls>" in answer:
                # sintaxe de tool-call vazou como texto: pedir resposta limpa
                messages.append({"role": "user", "content":
                                 "Sua última mensagem veio como chamada de "
                                 "tool inválida. Responda em texto simples."})
                resp = chat.complete(messages)
                usage = resp.get("usage") or {}
                tokens += usage.get("total_tokens", 0)
                answer = ((resp.get("choices") or [{}])[0]
                          .get("message", {}).get("content") or "")
            break
        for call in calls:
            tool_calls += 1
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
                result = toolset.call(name, args)
            except Exception as e:
                result = f"erro na tool {name}: {e}"
            messages.append({"role": "tool",
                             "tool_call_id": call.get("id", ""),
                             "content": str(result)[:MAX_TOOL_RESULT_CHARS]})
    return {"answer": answer, "tokens": tokens, "cached_tokens": cached,
            "tool_calls": tool_calls,
            "seconds": round(time.perf_counter() - t0, 1)}
