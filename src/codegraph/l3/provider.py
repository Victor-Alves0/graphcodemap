"""Provider LLM para a camada L3 — model-agnostic, zero dependência.

Default: OpenRouter (API OpenAI-compatível) via stdlib. Qualquer callable
`(system, user) -> str` com atributo `.model` serve como provider.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"  # override: CODEGRAPH_L3_MODEL
API_URL = "https://openrouter.ai/api/v1/chat/completions"


class L3Unavailable(RuntimeError):
    pass


def _load_dotenv(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    env_file = root / ".env"
    if not env_file.is_file():
        return out
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out

def openrouter_key_model(root: Path) -> tuple[str, str] | None:
    """(api_key, model) do env, do .env do root ou do .env do cwd (para
    avaliar repos externos sem espalhar segredos neles)."""
    dotenv = _load_dotenv(root)
    cwd_env = _load_dotenv(Path.cwd()) if Path.cwd() != root else {}

    def get(name: str) -> str | None:
        return os.environ.get(name) or dotenv.get(name) or cwd_env.get(name)

    key = get("OPENROUTER_API_KEY") or get("OPENROUTER_API")
    if not key:
        return None
    return key, (get("CODEGRAPH_L3_MODEL") or DEFAULT_MODEL)


def provider_from_env(root: Path):
    dotenv = _load_dotenv(root)

    def get(name: str) -> str | None:
        return os.environ.get(name) or dotenv.get(name)

    key = get("OPENROUTER_API_KEY") or get("OPENROUTER_API")
    if not key:
        return None
    model = get("CODEGRAPH_L3_MODEL") or DEFAULT_MODEL
    return OpenRouterProvider(key, model)


class OpenRouterProvider:
    def __init__(self, api_key: str, model: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def __call__(self, system: str, user: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 600,
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(
            API_URL, data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/Victor-Alves0/CodeGraph",
                "X-Title": "CodeGraph",
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # rede/HTTP/parse
            raise L3Unavailable(f"falha no provider L3 ({self.model}): {e}") from e
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise L3Unavailable(f"resposta inesperada do provider: {data}") from e
