"""M6: harness de avaliação (docs/DESIGN.md §7).

Compara o MESMO agente/modelo com dois toolsets:
- baseline  = list_files/grep/read_file (busca agêntica pura)
- codegraph = baseline + tools do grafo

Métricas por task: acerto objetivo (must_contain), nota do juiz LLM (0-10 com
gabarito), tokens totais, tool calls e tempo. O que queremos provar (ou
refutar honestamente): o grafo melhora qualidade E reduz tokens/chamadas
versus busca agêntica — o teste que a pesquisa exige (RESEARCH.md §4).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..indexer import Indexer
from ..l3.provider import openrouter_key_model
from ..query import QueryEngine
from .agent import OpenRouterChat, run_task
from .tools import BaselineTools, CodeGraphTools

_SYSTEM = (
    "You are a coding agent answering questions about the repository you have "
    "tool access to. Investigate with the tools, then give a precise final "
    "answer in Portuguese, citing file paths (and symbol names) that justify "
    "it. Be concise and factual."
)

_JUDGE_SYSTEM = (
    "You grade answers about a codebase. Compare the candidate answer to the "
    "reference. Score 0-10: 10 = fully correct and specific; 5 = partially "
    "correct or vague; 0 = wrong or unsupported. Reply ONLY with JSON: "
    '{"score": <int>, "reason": "<short>"}'
)


def load_tasks(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def judge(chat: OpenRouterChat, question: str, reference: str, answer: str) -> dict:
    user = (f"Question: {question}\n\nReference (ground truth): {reference}\n\n"
            f"Candidate answer: {answer or '(vazio)'}")
    resp = chat.complete([{"role": "system", "content": _JUDGE_SYSTEM},
                          {"role": "user", "content": user}])
    text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0) if match else text)
        return {"score": max(0, min(10, int(data.get("score", 0)))),
                "reason": str(data.get("reason", ""))[:300]}
    except Exception:
        return {"score": 0, "reason": f"juiz ilegível: {text[:120]}"}


def run_eval(root: str | Path, tasks_path: str | Path,
             arms: list[str] | None = None, max_steps: int = 12,
             model: str | None = None,
             progress=print) -> dict:
    root = Path(root).resolve()
    config = openrouter_key_model(root)
    if config is None:
        raise RuntimeError("OPENROUTER_API_KEY não configurada (env ou .env)")
    key, default_model = config
    model = model or default_model
    chat = OpenRouterChat(key, model)
    tasks = load_tasks(Path(tasks_path))
    arms = arms or ["baseline", "codegraph"]

    indexer = Indexer(root)
    indexer.index_repo()
    engine = QueryEngine(indexer)
    toolsets = {"baseline": BaselineTools(root),
                "codegraph": CodeGraphTools(root, engine)}

    report: dict = {"model": model, "root": str(root), "max_steps": max_steps,
                    "timestamp": int(time.time()), "arms": {}}
    for arm in arms:
        toolset = toolsets[arm]
        results = []
        for task in tasks:
            progress(f"[{arm}] {task['id']} …")
            r = run_task(chat, toolset, _SYSTEM, task["question"],
                         max_steps=max_steps)
            answer_low = r["answer"].lower()
            r["contains_all"] = all(m.lower() in answer_low
                                    for m in task.get("must_contain", []))
            r.update({"id": task["id"], "question": task["question"]})
            r["judge"] = judge(chat, task["question"], task["reference"],
                               r["answer"])
            results.append(r)
            progress(f"    nota={r['judge']['score']} objetivo="
                     f"{'ok' if r['contains_all'] else 'MISS'} "
                     f"tokens={r['tokens']} calls={r['tool_calls']} "
                     f"{r['seconds']}s")
        n = len(results) or 1
        avg_tokens = round(sum(r["tokens"] for r in results) / n)
        avg_cached = round(sum(r.get("cached_tokens", 0) for r in results) / n)
        report["arms"][arm] = {
            "tasks": results,
            "avg_score": round(sum(r["judge"]["score"] for r in results) / n, 2),
            "objective_rate": round(
                sum(1 for r in results if r["contains_all"]) / n, 2),
            "avg_tokens": avg_tokens,
            "avg_cached_tokens": avg_cached,
            # custo efetivo aproximado: token cacheado ≈ 1/10 do preço
            "avg_effective_tokens": round(avg_tokens - 0.9 * avg_cached),
            "avg_tool_calls": round(
                sum(r["tool_calls"] for r in results) / n, 1),
            "avg_seconds": round(sum(r["seconds"] for r in results) / n, 1),
        }
    return report


def render_report(report: dict) -> str:
    lines = [f"avaliação — modelo {report['model']} "
             f"(max_steps={report['max_steps']})", ""]
    header = f"{'braço':<11} {'nota juiz':>9} {'objetivo':>9} {'tokens':>8} " \
             f"{'cached':>8} {'efetivo':>8} {'calls':>6} {'seg':>6}"
    lines += [header, "-" * len(header)]
    for arm, s in report["arms"].items():
        lines.append(f"{arm:<11} {s['avg_score']:>9} "
                     f"{int(s['objective_rate'] * 100):>8}% "
                     f"{s['avg_tokens']:>8} {s.get('avg_cached_tokens', 0):>8} "
                     f"{s.get('avg_effective_tokens', s['avg_tokens']):>8} "
                     f"{s['avg_tool_calls']:>6} {s['avg_seconds']:>6}")
    return "\n".join(lines)
