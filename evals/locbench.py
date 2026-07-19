"""Benchmark de LOCALIZAÇÃO sobre SWE-bench-Lite (dados reais).

Por que localização e não geração+execução de patch: a harness oficial do
SWE-bench precisa de Docker + imagens por-tarefa + execução da suíte de cada
repo — inviável neste ambiente. Mas o gold patch de cada tarefa diz QUAIS
arquivos/símbolos foram alterados: é ground truth de localização (o que o
LocBench/LocAgent medem), e é exatamente o eixo onde a nossa tese vive
(grafo ajuda em localização estrutural multi-hop).

Mede o valor MARGINAL do grafo: mesmo agente/modelo/prompt, dois braços —
baseline (grep/read/list) vs +grafo (tools do CodeGraph). Métrica principal:
o braço encontrou o(s) arquivo(s) que o gold patch realmente edita?

Uso:  python evals/locbench.py [--tasks evals/swe-lite-pilot.json]
             [--repos-dir benchrepos/swe] [--max-steps 8] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph.eval.agent import MAX_TOOL_RESULT_CHARS, OpenRouterChat  # noqa: E402
from codegraph.eval.tools import BaselineTools, CodeGraphTools  # noqa: E402
from codegraph.indexer import Indexer  # noqa: E402
from codegraph.l3.provider import openrouter_key_model  # noqa: E402
from codegraph.query import QueryEngine  # noqa: E402

_REPO_DIR = {"pallets/flask": "flask", "psf/requests": "requests",
             "pytest-dev/pytest": "pytest"}

SYSTEM = (
    "You are a senior engineer doing FAULT LOCALIZATION. Given a bug report, "
    "find the exact source file(s) that must be edited to fix it — not tests. "
    "Explore with the tools, then STOP. Be efficient. End your final message "
    "with a JSON block and nothing after it:\n"
    "```json\n{\"files\": [\"path/rel/to/repo.py\"], \"symbols\": [\"func_or_Class\"]}\n```\n"
    "Paths must be repo-relative. List only the files you are confident need editing."
)

_JSON_RE = re.compile(r"\{.*\}", re.S)

_EXTRACT = ("Investigation done. Output ONLY the JSON block with the file(s) that "
            "must be edited to fix the bug (repo-relative source paths, not tests) "
            "and the function/class symbols. No prose before or after:\n"
            "```json\n{\"files\": [\"...\"], \"symbols\": [\"...\"]}\n```")


def localize(chat: OpenRouterChat, toolset, question: str, max_steps: int) -> dict:
    """Loop de tools + chamada final DEDICADA de extração do JSON (robusta e
    idêntica nos dois braços — a variável é só o conjunto de tools)."""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": question}]
    schemas = toolset.schemas()
    tokens = cached = tool_calls = 0
    t0 = time.perf_counter()

    def acct(resp):
        nonlocal tokens, cached
        u = resp.get("usage") or {}
        tokens += u.get("total_tokens", 0)
        cached += (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return (resp.get("choices") or [{}])[0].get("message", {})

    for _ in range(max_steps):
        msg = acct(chat.complete(messages, tools=schemas))
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            break
        for call in calls:
            tool_calls += 1
            fn = call.get("function", {})
            try:
                result = toolset.call(fn.get("name", ""),
                                      json.loads(fn.get("arguments") or "{}"))
            except Exception as e:
                result = f"erro: {e}"
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": str(result)[:MAX_TOOL_RESULT_CHARS]})

    answer = ""
    for _ in range(2):  # extração dedicada, com 1 retry se vier vazia
        messages.append({"role": "user", "content": _EXTRACT})
        msg = acct(chat.complete(messages, tools=None))
        answer = msg.get("content") or ""
        if answer.strip():
            break
    return {"answer": answer, "tokens": tokens, "cached_tokens": cached,
            "tool_calls": tool_calls, "seconds": round(time.perf_counter() - t0, 1)}


def parse_prediction(answer: str) -> dict:
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.S)
    candidates = blocks or _JSON_RE.findall(answer)
    for c in reversed(candidates):
        try:
            d = json.loads(c)
            if isinstance(d, dict) and "files" in d:
                files = [_norm(f) for f in d.get("files", []) if isinstance(f, str)]
                syms = [s for s in d.get("symbols", []) if isinstance(s, str)]
                return {"files": files, "symbols": syms}
        except ValueError:
            continue
    return {"files": [], "symbols": []}


def _norm(p: str) -> str:
    return p.replace("\\", "/").lstrip("./").strip()


def _sym_tail(s: str) -> str:
    return s.replace("::", ".").rsplit(".", 1)[-1].strip()


def score(pred: dict, gold_files: list[str], gold_syms: list[str]) -> dict:
    gf, pf = set(gold_files), set(pred["files"])
    inter = gf & pf
    file_recall = len(inter) / len(gf) if gf else 0.0
    all_files = 1.0 if gf and gf <= pf else 0.0
    any_file = 1.0 if inter else 0.0
    gs = {_sym_tail(s) for s in gold_syms}
    ps = {_sym_tail(s) for s in pred["symbols"]}
    sym_hit = 1.0 if gs and (gs & ps) else (None if not gs else 0.0)
    return {"file_recall": round(file_recall, 3), "all_files": all_files,
            "any_file": any_file, "sym_hit": sym_hit,
            "pred_files": sorted(pf), "gold_files": sorted(gf)}


def prepare_repo(repos_dir: Path, repo: str, base_commit: str) -> Path:
    path = repos_dir / _REPO_DIR[repo]
    cg = path / ".codegraph"
    if cg.exists():
        shutil.rmtree(cg, ignore_errors=True)  # índice fresco por tarefa
    subprocess.run(["git", "-C", str(path), "checkout", "-f", base_commit],
                   check=True, capture_output=True)
    # clean de untracked é best-effort (não crítico: indexação ignora .git e
    # o .codegraph já foi removido); alguns arquivos podem estar travados
    subprocess.run(["git", "-C", str(path), "clean", "-fdq"], capture_output=True)
    if cg.exists():
        shutil.rmtree(cg, ignore_errors=True)
    return path


def run(tasks_path: Path, repos_dir: Path, max_steps: int, limit: int | None,
        model: str | None) -> dict:
    cfg = openrouter_key_model(Path.cwd())
    if cfg is None:
        raise SystemExit("OPENROUTER_API_KEY não configurada (.env)")
    key, default_model = cfg
    chat = OpenRouterChat(key, model or default_model)
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    if limit:
        tasks = tasks[:limit]

    report = {"model": model or default_model, "timestamp": int(time.time()),
              "n": len(tasks), "results": []}
    for i, t in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {t['instance_id']} …", flush=True)
        repo_path = prepare_repo(repos_dir, t["repo"], t["base_commit"])
        ix = Indexer(repo_path)
        ix.index_repo()
        engine = QueryEngine(ix)
        question = (f"Bug report for repo {t['repo']}:\n\n"
                    f"{t['problem_statement'][:6000]}\n\n"
                    "Which source file(s) must be edited to fix this?")
        arms = {"baseline": BaselineTools(repo_path),
                "codegraph": CodeGraphTools(repo_path, engine)}
        row = {"instance_id": t["instance_id"], "repo": t["repo"],
               "gold_files": t["gold_files"], "arms": {}}
        for arm, toolset in arms.items():
            r = localize(chat, toolset, question, max_steps=max_steps)
            pred = parse_prediction(r["answer"])
            sc = score(pred, t["gold_files"], t.get("gold_syms", []))
            row["arms"][arm] = {**sc, "tokens": r["tokens"],
                                "cached_tokens": r["cached_tokens"],
                                "tool_calls": r["tool_calls"], "seconds": r["seconds"],
                                "answer_tail": r["answer"][-700:]}
            print(f"    {arm:10} any={sc['any_file']:.0f} all={sc['all_files']:.0f} "
                  f"recall={sc['file_recall']:.2f} calls={r['tool_calls']} "
                  f"tok={r['tokens']}", flush=True)
        ix.close()
        report["results"].append(row)
    report["summary"] = summarize(report["results"])
    return report


def summarize(results: list) -> dict:
    out = {}
    for arm in ("baseline", "codegraph"):
        rs = [r["arms"][arm] for r in results if arm in r["arms"]]
        if not rs:
            continue
        n = len(rs)
        syms = [r["sym_hit"] for r in rs if r["sym_hit"] is not None]
        out[arm] = {
            "any_file_rate": round(sum(r["any_file"] for r in rs) / n, 3),
            "all_files_rate": round(sum(r["all_files"] for r in rs) / n, 3),
            "mean_file_recall": round(sum(r["file_recall"] for r in rs) / n, 3),
            "sym_hit_rate": round(sum(syms) / len(syms), 3) if syms else None,
            "avg_tokens": round(sum(r["tokens"] for r in rs) / n),
            "avg_cached": round(sum(r["cached_tokens"] for r in rs) / n),
            "avg_tool_calls": round(sum(r["tool_calls"] for r in rs) / n, 1),
            "avg_seconds": round(sum(r["seconds"] for r in rs) / n, 1),
        }
    return out


def render(report: dict) -> str:
    s = report["summary"]
    lines = [f"\nLocBench-lite (SWE-bench-Lite, localização) — {report['n']} tarefas, "
             f"modelo {report['model']}", "-" * 72,
             f"{'braço':12}{'achou-1':>9}{'achou-todos':>13}{'recall':>9}"
             f"{'sym':>7}{'tokens':>9}{'calls':>7}{'seg':>7}"]
    for arm in ("baseline", "codegraph"):
        a = s.get(arm)
        if not a:
            continue
        sym = f"{a['sym_hit_rate']:.0%}" if a["sym_hit_rate"] is not None else "-"
        lines.append(f"{arm:12}{a['any_file_rate']:>8.0%}{a['all_files_rate']:>13.0%}"
                     f"{a['mean_file_recall']:>9.2f}{sym:>7}{a['avg_tokens']:>9}"
                     f"{a['avg_tool_calls']:>7}{a['avg_seconds']:>7}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="evals/swe-lite-pilot.json")
    ap.add_argument("--repos-dir", default="benchrepos/swe")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    report = run(Path(args.tasks), Path(args.repos_dir), args.max_steps,
                 args.limit, args.model)
    out = Path("evals") / f"locbench-{report['timestamp']}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report))
    print(f"\nrelatório: {out}")


if __name__ == "__main__":
    main()
