"""Reachability pura em repo PYTHON (arestas 'certain' via L1/jedi).

Testa a hipótese: onde o L1 resolve as chamadas e o grafo devolve a cadeia como
[certain], o agente CONFIA no `reaches` e para — extraindo o ganho de tokens que
sem L1 (arestas 'possible') ele desperdiçava re-verificando. Pergunta grep-hard:
"seguindo o call graph a partir de E, a execução chega à função T? qual a
cadeia?". Gold computado pelo grafo (reaches) e verificado. Dois braços
(baseline grep/read vs codegraph +grafo).

Uso:  python evals/reachbench.py [--repo benchrepos/flask] [--model ...] [--max-steps 12]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph.eval.agent import MAX_TOOL_RESULT_CHARS, OpenRouterChat  # noqa: E402
from codegraph.eval.tools import BaselineTools, CodeGraphTools  # noqa: E402
from codegraph.indexer import Indexer  # noqa: E402
from codegraph.l3.provider import openrouter_key_model  # noqa: E402
from codegraph.query import QueryEngine  # noqa: E402

SYSTEM = (
    "You are analyzing REACHABILITY in a codebase. Given an entry function "
    "and a target function, determine — by following the call graph — whether "
    "execution can reach the target from the entry, and give the exact chain of "
    "functions connecting them. The target name may appear in many places; you "
    "need the path actually reachable from THIS entry. Be efficient: if a tool "
    "answers with high confidence ([certain]), trust it and STOP — do not re-"
    "verify by reading every file. End with a JSON block, nothing after it:\n"
    "```json\n{\"reaches\": true, \"target\": \"fn\", "
    "\"chain\": [\"entry\", \"...\", \"target\"]}\n```"
)
_EXTRACT = ("Output ONLY the JSON verdict: whether the target is reachable from "
            "the entry, and the call chain.\n"
            "```json\n{\"reaches\": true, \"target\": \"...\", \"chain\": [\"...\"]}\n```")
_OBJ = re.compile(r"\{.*\}", re.S)


def run_agent(chat, toolset, question, max_steps):
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": question}]
    schemas = toolset.schemas()
    tok = cached = calls_n = 0
    names = []
    t0 = time.perf_counter()

    def acct(resp):
        nonlocal tok, cached
        u = resp.get("usage") or {}
        tok += u.get("total_tokens", 0)
        cached += (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return (resp.get("choices") or [{}])[0].get("message", {})

    for _ in range(max_steps):
        msg = acct(chat.complete(messages, tools=schemas))
        tc = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         **({"tool_calls": tc} if tc else {})})
        if not tc:
            break
        for c in tc:
            calls_n += 1
            fn = c.get("function", {})
            names.append(fn.get("name", ""))
            try:
                res = toolset.call(fn.get("name", ""), json.loads(fn.get("arguments") or "{}"))
            except Exception as e:
                res = f"erro: {e}"
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": str(res)[:MAX_TOOL_RESULT_CHARS]})

    answer = ""
    for i in range(4):
        messages.append({"role": "user", "content": _EXTRACT if i == 0 else
                         "Reply with ONLY the JSON object, plain text, no tool calls."})
        msg = acct(chat.complete(messages, tools=None))
        answer = msg.get("content") or ""
        if answer.strip() and "DSML" not in answer and "tool_calls>" not in answer:
            break
    return {"answer": answer, "tokens": tok, "cached": cached, "calls": calls_n,
            "names": names, "seconds": round(time.perf_counter() - t0, 1)}


def _key(s):
    return re.split(r"[./]", (s or "").strip().strip(":").split(":", 1)[0])[-1].strip()


def parse(ans):
    for c in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", ans, re.S) or _OBJ.findall(ans)):
        try:
            d = json.loads(c)
        except ValueError:
            continue
        if isinstance(d, dict) and "reaches" in d:
            return d
    return {}


def score(pred, task):
    target_ok = _key(pred.get("target", "")) == _key(task["target"])
    gold = {_key(x) for x in task["gold_chain"]}
    got = {_key(x) for x in pred.get("chain", []) if isinstance(x, str)}
    recall = round(len(gold & got) / len(gold), 2) if gold else 0.0
    return {"reach_ok": pred.get("reaches") is True, "target_ok": target_ok,
            "chain_recall": recall, "correct": bool(pred.get("reaches") and target_ok),
            "pred_target": pred.get("target"), "pred_chain": pred.get("chain")}


def run(repo, tasks_path, max_steps, model):
    cfg = openrouter_key_model(Path.cwd())
    if cfg is None:
        raise SystemExit("OPENROUTER_API_KEY não configurada")
    key, default = cfg
    chat = OpenRouterChat(key, model or default)
    print(f"indexando {repo} …", flush=True)
    ix = Indexer(repo); ix.index_repo(); eng = QueryEngine(ix)
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    report = {"model": model or default, "timestamp": int(time.time()),
              "repo": str(repo), "n": len(tasks), "results": []}
    for i, t in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {t['id']}", flush=True)
        q = (f"Entry function: {t['entry']}. Target function: {t['target']}. "
             "Following the call graph, can execution reach the target from the "
             "entry? Give the exact chain of function names.")
        row = {"id": t["id"], "arms": {}}
        for arm, ts in (("baseline", BaselineTools(repo)),
                        ("codegraph", CodeGraphTools(repo, eng))):
            r = run_agent(chat, ts, q, max_steps)
            sc = score(parse(r["answer"]), t)
            row["arms"][arm] = {**sc, "tokens": r["tokens"], "calls": r["calls"],
                                "names": r["names"], "seconds": r["seconds"],
                                "answer": r["answer"][-500:]}
            print(f"    {arm:10} correct={sc['correct']} target={sc['target_ok']} "
                  f"chain={sc['chain_recall']} calls={r['calls']} tok={r['tokens']} "
                  f"({','.join(dict.fromkeys(r['names']))})", flush=True)
        report["results"].append(row)
    ix.close()
    report["summary"] = {arm: {
        "correct": round(sum(r["arms"][arm]["correct"] for r in report["results"]) / len(tasks), 2),
        "chain_recall": round(sum(r["arms"][arm]["chain_recall"] for r in report["results"]) / len(tasks), 2),
        "avg_tokens": round(sum(r["arms"][arm]["tokens"] for r in report["results"]) / len(tasks)),
        "avg_calls": round(sum(r["arms"][arm]["calls"] for r in report["results"]) / len(tasks), 1),
    } for arm in ("baseline", "codegraph")}
    return report


def render(report):
    s = report["summary"]
    out = [f"\nReachBench (Python/flask, arestas certain via L1) — {report['n']} tarefas, "
           f"{report['model']}", "-" * 60,
           f"{'braço':12}{'correto':>9}{'cadeia':>8}{'tokens':>9}{'calls':>7}"]
    for arm in ("baseline", "codegraph"):
        a = s[arm]
        out.append(f"{arm:12}{a['correct']:>9.0%}{a['chain_recall']:>8.2f}"
                   f"{a['avg_tokens']:>9}{a['avg_calls']:>7}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="benchrepos/flask")
    ap.add_argument("--tasks", default="evals/reach-flask.json")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    report = run(Path(args.repo), Path(args.tasks), args.max_steps, args.model)
    out = Path("evals") / f"reachbench-{report['timestamp']}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report))
    print(f"\nrelatório: {out}")


if __name__ == "__main__":
    main()
